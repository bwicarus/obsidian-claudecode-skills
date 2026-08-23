using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace BwReader.ComputerVoiceAudio;

internal sealed record ReaderRealtimeOutputOutboxEntry(
    ReaderRealtimeOutputRequest Request,
    string State,
    long CreatedAtUtcMs,
    long UpdatedAtUtcMs,
    int Attempts,
    string? LastError);

/// <summary>
/// Crash-safe holding area for idempotent Reader card mutations that were
/// accepted before an App/extension source was online.  The source lease is
/// deliberately not persisted: sourceInstanceId belongs to one WebView
/// lifetime, while the durable identity is the document + mutation payload.
/// </summary>
internal sealed class ReaderRealtimeOutputOutbox
{
    internal const string Contract = "reader-realtime-output-outbox/1";
    internal const string FileName = "reader-realtime-output-outbox.json";
    private const int MaximumEntries = 64;
    private const int MaximumFileBytes = 16 * 1024 * 1024;
    private static readonly TimeSpan MaximumAge = TimeSpan.FromDays(30);

    private readonly string _path;
    private readonly SemaphoreSlim _gate = new(1, 1);

    internal ReaderRealtimeOutputOutbox(string path)
    {
        if (string.IsNullOrWhiteSpace(path) || !Path.IsPathFullyQualified(path))
        {
            throw new ArgumentException(
                "Reader output outbox path must be absolute",
                nameof(path));
        }
        _path = Path.GetFullPath(path);
    }

    internal async Task EnqueueAsync(
        ReaderRealtimeOutputRequest request,
        CancellationToken cancellationToken)
    {
        if (!ReaderRealtimeOutputProtocol.IsDurableMutation(request))
        {
            throw new ReaderRealtimeOutputException(
                "BW_READER_REALTIME_OUTPUT_NOT_QUEUEABLE",
                "该 Reader 输出缺少可幂等重放的卡片身份",
                retryable: false);
        }
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            List<ReaderRealtimeOutputOutboxEntry> entries =
                await ReadLockedAsync(cancellationToken).ConfigureAwait(false);
            ReaderRealtimeOutputOutboxEntry? existing = entries.FirstOrDefault(
                item => item.Request.Correlation == request.Correlation);
            if (existing is not null)
            {
                if (!SameRequest(existing.Request, request))
                {
                    throw new ReaderRealtimeOutputException(
                        "BW_READER_REALTIME_OUTPUT_OUTBOX_CONFLICT",
                        "Reader 输出 mutationId 已被另一份内容占用",
                        retryable: false);
                }
                return;
            }
            if (entries.Count >= MaximumEntries)
            {
                throw new ReaderRealtimeOutputException(
                    "BW_READER_REALTIME_OUTPUT_OUTBOX_CAPACITY",
                    "Reader 后台卡片队列已满，请先打开 App 完成同步",
                    retryable: true);
            }
            long now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
            entries.Add(new ReaderRealtimeOutputOutboxEntry(
                CloneWithoutEphemeralSource(request),
                "pending",
                now,
                now,
                0,
                null));
            await WriteLockedAsync(entries, cancellationToken)
                .ConfigureAwait(false);
        }
        finally
        {
            _gate.Release();
        }
    }

    internal async Task<IReadOnlyList<ReaderRealtimeOutputOutboxEntry>>
        ReplayableAsync(CancellationToken cancellationToken)
    {
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            List<ReaderRealtimeOutputOutboxEntry> entries =
                await ReadLockedAsync(cancellationToken).ConfigureAwait(false);
            // A Reader rejection is scoped to the source/WebView that
            // produced it.  An older or wrong-page source can reject a valid,
            // already-validated mutation while a later healthy source can
            // apply the same stable mutation ID.  Keep the failure visible on
            // disk, but retry it only when the broker receives a later replay
            // trigger (normally a new source attach).  "unknown" is excluded:
            // its write result is not safe to repeat.
            return entries
                .Where(item => item.State is "pending" or "failed")
                .Select(CloneEntry)
                .ToArray();
        }
        finally
        {
            _gate.Release();
        }
    }

    internal Task MarkAppliedAsync(
        string correlation,
        CancellationToken cancellationToken) =>
        UpdateAsync(correlation, remove: true, "applied", null,
            cancellationToken);

    internal Task MarkDeferredAsync(
        string correlation,
        string? error,
        CancellationToken cancellationToken) =>
        UpdateAsync(correlation, remove: false, "pending", error,
            cancellationToken);

    internal Task MarkUnknownAsync(
        string correlation,
        string? error,
        CancellationToken cancellationToken) =>
        UpdateAsync(correlation, remove: false, "unknown", error,
            cancellationToken);

    internal Task MarkFailedAsync(
        string correlation,
        string? error,
        CancellationToken cancellationToken) =>
        UpdateAsync(correlation, remove: false, "failed", error,
            cancellationToken);

    internal async Task<int> CountAsync(CancellationToken cancellationToken)
    {
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            return (await ReadLockedAsync(cancellationToken)
                .ConfigureAwait(false)).Count;
        }
        finally
        {
            _gate.Release();
        }
    }

    private async Task UpdateAsync(
        string correlation,
        bool remove,
        string state,
        string? error,
        CancellationToken cancellationToken)
    {
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            List<ReaderRealtimeOutputOutboxEntry> entries =
                await ReadLockedAsync(cancellationToken).ConfigureAwait(false);
            int index = entries.FindIndex(
                item => item.Request.Correlation == correlation);
            if (index < 0)
            {
                return;
            }
            if (remove)
            {
                entries.RemoveAt(index);
            }
            else
            {
                ReaderRealtimeOutputOutboxEntry current = entries[index];
                entries[index] = current with
                {
                    State = state,
                    UpdatedAtUtcMs = DateTimeOffset.UtcNow
                        .ToUnixTimeMilliseconds(),
                    Attempts = checked(current.Attempts + 1),
                    LastError = BoundedError(error),
                };
            }
            await WriteLockedAsync(entries, cancellationToken)
                .ConfigureAwait(false);
        }
        finally
        {
            _gate.Release();
        }
    }

    private async Task<List<ReaderRealtimeOutputOutboxEntry>> ReadLockedAsync(
        CancellationToken cancellationToken)
    {
        if (!File.Exists(_path))
        {
            return new List<ReaderRealtimeOutputOutboxEntry>();
        }
        FileInfo info = new(_path);
        if (info.Length is <= 0 or > MaximumFileBytes)
        {
            throw Corrupt("Reader 后台卡片队列大小无效");
        }
        byte[] bytes = await File.ReadAllBytesAsync(_path, cancellationToken)
            .ConfigureAwait(false);
        try
        {
            using JsonDocument document = JsonDocument.Parse(
                bytes,
                new JsonDocumentOptions
                {
                    AllowTrailingCommas = false,
                    CommentHandling = JsonCommentHandling.Disallow,
                    MaxDepth = 16,
                });
            JsonElement root = document.RootElement;
            DirectJsonValidation.RequireNoDuplicateKeys(root);
            RequireFields(root, "contract", "entries");
            if (root.GetProperty("contract").ValueKind != JsonValueKind.String
                || root.GetProperty("contract").GetString() != Contract
                || root.GetProperty("entries").ValueKind
                    != JsonValueKind.Array
                || root.GetProperty("entries").GetArrayLength()
                    > MaximumEntries)
            {
                throw Corrupt("Reader 后台卡片队列合同无效");
            }
            long cutoff = DateTimeOffset.UtcNow.Subtract(MaximumAge)
                .ToUnixTimeMilliseconds();
            List<ReaderRealtimeOutputOutboxEntry> entries = new();
            foreach (JsonElement element in root.GetProperty("entries")
                .EnumerateArray())
            {
                ReaderRealtimeOutputOutboxEntry entry = ParseEntry(element);
                if (entry.CreatedAtUtcMs >= cutoff)
                {
                    entries.Add(entry);
                }
            }
            return entries;
        }
        catch (ReaderRealtimeOutputException)
        {
            throw;
        }
        catch (Exception exception) when (
            exception is JsonException
            or InvalidOperationException
            or FormatException
            or OverflowException)
        {
            throw Corrupt("Reader 后台卡片队列损坏", exception);
        }
        finally
        {
            Array.Clear(bytes);
        }
    }

    private async Task WriteLockedAsync(
        IReadOnlyList<ReaderRealtimeOutputOutboxEntry> entries,
        CancellationToken cancellationToken)
    {
        JsonObject root = new()
        {
            ["contract"] = Contract,
            ["entries"] = new JsonArray(entries.Select(SerializeEntry)
                .ToArray()),
        };
        byte[] bytes = Encoding.UTF8.GetBytes(
            root.ToJsonString(DirectBridgeContract.JsonOptions));
        if (bytes.Length > MaximumFileBytes)
        {
            Array.Clear(bytes);
            throw new ReaderRealtimeOutputException(
                "BW_READER_REALTIME_OUTPUT_OUTBOX_CAPACITY",
                "Reader 后台卡片队列超过安全大小",
                retryable: true);
        }
        string? directory = Path.GetDirectoryName(_path);
        if (string.IsNullOrWhiteSpace(directory))
        {
            Array.Clear(bytes);
            throw Corrupt("Reader 后台卡片队列目录无效");
        }
        Directory.CreateDirectory(directory);
        string temporary = _path + ".tmp-" + Environment.ProcessId + "-"
            + Guid.NewGuid().ToString("N");
        try
        {
            await File.WriteAllBytesAsync(
                temporary,
                bytes,
                cancellationToken).ConfigureAwait(false);
            File.Move(temporary, _path, overwrite: true);
        }
        catch (Exception exception) when (
            exception is IOException
            or UnauthorizedAccessException)
        {
            throw new ReaderRealtimeOutputException(
                "BW_READER_REALTIME_OUTPUT_OUTBOX_IO",
                "Reader 后台卡片队列无法持久化",
                retryable: true,
                exception);
        }
        finally
        {
            Array.Clear(bytes);
            try { if (File.Exists(temporary)) File.Delete(temporary); }
            catch { }
        }
    }

    private static ReaderRealtimeOutputOutboxEntry ParseEntry(
        JsonElement element)
    {
        DirectJsonValidation.RequireNoDuplicateKeys(element);
        RequireFields(
            element,
            "correlation",
            "snapshotRevision",
            "file",
            "page",
            "kind",
            "payload",
            "state",
            "createdAtUtcMs",
            "updatedAtUtcMs",
            "attempts",
            "lastError");
        string correlation = RequiredString(element, "correlation", 160);
        string file = RequiredString(element, "file", 4096);
        string kind = RequiredString(element, "kind", 32);
        if (!element.GetProperty("snapshotRevision")
                .TryGetInt64(out long snapshotRevision)
            || snapshotRevision < 0
            || !element.GetProperty("createdAtUtcMs")
                .TryGetInt64(out long createdAt)
            || createdAt <= 0
            || !element.GetProperty("updatedAtUtcMs")
                .TryGetInt64(out long updatedAt)
            || updatedAt <= 0
            || !element.GetProperty("attempts").TryGetInt32(out int attempts)
            || attempts is < 0 or > 1_000_000)
        {
            throw Corrupt("Reader 后台卡片队列数字字段无效");
        }
        string state = RequiredString(element, "state", 16);
        if (state is not ("pending" or "unknown" or "failed"))
        {
            throw Corrupt("Reader 后台卡片队列状态无效");
        }
        string? lastError = element.GetProperty("lastError").ValueKind
            == JsonValueKind.Null
                ? null
                : RequiredString(element, "lastError", 500);
        JsonNode page = JsonNode.Parse(
            element.GetProperty("page").GetRawText())
            ?? throw Corrupt("Reader 后台卡片队列 page 无效");
        JsonNode payload = JsonNode.Parse(
            element.GetProperty("payload").GetRawText())
            ?? throw Corrupt("Reader 后台卡片队列 payload 无效");
        ReaderRealtimeOutputRequest request =
            ReaderRealtimeOutputProtocol.Create(
                correlation,
                "outbox-replay",
                snapshotRevision,
                file,
                page,
                kind,
                payload);
        if (!ReaderRealtimeOutputProtocol.IsDurableMutation(request))
        {
            throw Corrupt("Reader 后台卡片队列含不可重放操作");
        }
        return new ReaderRealtimeOutputOutboxEntry(
            request,
            state,
            createdAt,
            updatedAt,
            attempts,
            lastError);
    }

    private static JsonObject SerializeEntry(
        ReaderRealtimeOutputOutboxEntry entry) => new()
    {
        ["correlation"] = entry.Request.Correlation,
        ["snapshotRevision"] = entry.Request.SnapshotRevision,
        ["file"] = entry.Request.File,
        ["page"] = entry.Request.Page.DeepClone(),
        ["kind"] = entry.Request.Kind,
        ["payload"] = entry.Request.Payload.DeepClone(),
        ["state"] = entry.State,
        ["createdAtUtcMs"] = entry.CreatedAtUtcMs,
        ["updatedAtUtcMs"] = entry.UpdatedAtUtcMs,
        ["attempts"] = entry.Attempts,
        ["lastError"] = entry.LastError,
    };

    private static ReaderRealtimeOutputRequest CloneWithoutEphemeralSource(
        ReaderRealtimeOutputRequest request) =>
        ReaderRealtimeOutputProtocol.Create(
            request.Correlation,
            "outbox-replay",
            request.SnapshotRevision,
            request.File,
            request.Page.DeepClone(),
            request.Kind,
            request.Payload.DeepClone());

    private static ReaderRealtimeOutputOutboxEntry CloneEntry(
        ReaderRealtimeOutputOutboxEntry entry) => entry with
    {
        Request = CloneWithoutEphemeralSource(entry.Request),
    };

    private static bool SameRequest(
        ReaderRealtimeOutputRequest left,
        ReaderRealtimeOutputRequest right) =>
        left.Correlation == right.Correlation
        && left.SnapshotRevision == right.SnapshotRevision
        && left.File == right.File
        && left.Kind == right.Kind
        && JsonNode.DeepEquals(left.Page, right.Page)
        && JsonNode.DeepEquals(left.Payload, right.Payload);

    private static string? BoundedError(string? value)
    {
        value = string.IsNullOrWhiteSpace(value) ? null : value.Trim();
        if (value is null)
        {
            return null;
        }
        string safe = new(value.Select(character =>
            char.IsControl(character) ? ' ' : character).ToArray());
        safe = string.Join(
            ' ',
            safe.Split(
                (char[]?)null,
                StringSplitOptions.RemoveEmptyEntries));
        return safe[..Math.Min(safe.Length, 500)];
    }

    private static void RequireFields(
        JsonElement element,
        params string[] fields)
    {
        if (element.ValueKind != JsonValueKind.Object)
        {
            throw Corrupt("Reader 后台卡片队列项必须是对象");
        }
        HashSet<string> actual = element.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        if (!actual.SetEquals(fields))
        {
            throw Corrupt("Reader 后台卡片队列字段不匹配");
        }
    }

    private static string RequiredString(
        JsonElement element,
        string name,
        int maximum)
    {
        JsonElement value = element.GetProperty(name);
        if (value.ValueKind != JsonValueKind.String
            || value.GetString() is not string text
            || string.IsNullOrWhiteSpace(text)
            || text.Length > maximum
            || text.Any(char.IsControl))
        {
            throw Corrupt($"Reader 后台卡片队列 {name} 无效");
        }
        return text;
    }

    private static ReaderRealtimeOutputException Corrupt(
        string message,
        Exception? inner = null) => new(
            "BW_READER_REALTIME_OUTPUT_OUTBOX_CORRUPT",
            message,
            retryable: false,
            inner);
}
