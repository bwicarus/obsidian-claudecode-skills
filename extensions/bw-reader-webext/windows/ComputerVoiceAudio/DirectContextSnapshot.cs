using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace BwReader.ComputerVoiceAudio;

internal sealed record DirectActiveReading(
    string Kind,
    string File,
    string? Title,
    JsonElement Page,
    string SelectionState,
    string? Selection,
    long ObservedAtEpochMilliseconds);

internal sealed record DirectSnapshotForwardResult(
    string Outcome,
    long Revision);

internal interface IDirectSnapshotContextAdapter
{
    Task<DirectSnapshotForwardResult> ForwardJournalAsync(
        string requestId,
        string sessionId,
        DirectContextEvent contextEvent,
        CancellationToken cancellationToken);

    Task<DirectSnapshotForwardResult> ForwardActiveReadingAsync(
        string requestId,
        string sessionId,
        DirectActiveReading activeReading,
        CancellationToken cancellationToken);

    Task<DirectSnapshotForwardResult> ClearAsync(
        string requestId,
        string sessionId,
        CancellationToken cancellationToken);
}

internal sealed class UnwiredDirectSnapshotContextAdapter :
    IDirectSnapshotContextAdapter
{
    public Task<DirectSnapshotForwardResult> ForwardJournalAsync(
        string requestId,
        string sessionId,
        DirectContextEvent contextEvent,
        CancellationToken cancellationToken) =>
        Task.FromException<DirectSnapshotForwardResult>(Unavailable());

    public Task<DirectSnapshotForwardResult> ForwardActiveReadingAsync(
        string requestId,
        string sessionId,
        DirectActiveReading activeReading,
        CancellationToken cancellationToken) =>
        Task.FromException<DirectSnapshotForwardResult>(Unavailable());

    public Task<DirectSnapshotForwardResult> ClearAsync(
        string requestId,
        string sessionId,
        CancellationToken cancellationToken) =>
        Task.FromException<DirectSnapshotForwardResult>(Unavailable());

    private static DirectProtocolException Unavailable() =>
        new(
            "BW_READER_CONTEXT_SNAPSHOT_UNAVAILABLE",
            "Windows 本地 Reader 快照存储尚未接线",
            retryable: true);
}

internal sealed class FileDirectSnapshotContextAdapter :
    IDirectSnapshotContextAdapter
{
    internal const string SnapshotContract = "reader-context-snapshot/1";
    internal const string ActiveReadingContract =
        "reader-active-reading/1";
    internal const string SnapshotFileName =
        "reader-context-snapshot.json";

    private const int MaximumSnapshotBytes = 128 * 1024;
    private const int RecentEventLimit = 256;
    private static readonly UTF8Encoding Utf8WithoutBom = new(
        encoderShouldEmitUTF8Identifier: false);

    private readonly string _statePath;
    private readonly Func<DateTimeOffset> _utcNow;
    private readonly SemaphoreSlim _gate = new(1, 1);
    private readonly Queue<string> _recentEventOrder = new();
    private readonly HashSet<string> _recentEventIds =
        new(StringComparer.Ordinal);
    private long _revision;
    private JsonObject? _stablePage;
    private JsonObject? _activeReading;
    private JsonObject _selection = UnknownSelection(
        "snapshot-not-received");
    private JsonObject? _latestEvent;

    internal FileDirectSnapshotContextAdapter(
        string statePath,
        Func<DateTimeOffset>? utcNow = null)
    {
        if (!System.IO.Path.IsPathFullyQualified(statePath))
        {
            throw new ArgumentException(
                "snapshot state path must be absolute",
                nameof(statePath));
        }
        _statePath = System.IO.Path.GetFullPath(statePath);
        _utcNow = utcNow ?? (() => DateTimeOffset.UtcNow);
        LoadExistingState();
    }

    internal string StatePath => _statePath;

    public async Task<DirectSnapshotForwardResult> ForwardJournalAsync(
        string requestId,
        string sessionId,
        DirectContextEvent contextEvent,
        CancellationToken cancellationToken)
    {
        ValidateRequestIdentity(requestId, sessionId);
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            if (_recentEventIds.Contains(contextEvent.EventId))
            {
                return new DirectSnapshotForwardResult(
                    "duplicate",
                    _revision);
            }
            FoldJournal(contextEvent);
            RememberEvent(contextEvent.EventId);
            await PersistAsync(cancellationToken).ConfigureAwait(false);
            return new DirectSnapshotForwardResult("accepted", _revision);
        }
        finally
        {
            _gate.Release();
        }
    }

    public async Task<DirectSnapshotForwardResult>
        ForwardActiveReadingAsync(
            string requestId,
            string sessionId,
            DirectActiveReading activeReading,
            CancellationToken cancellationToken)
    {
        ValidateRequestIdentity(requestId, sessionId);
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            JsonObject next = new()
            {
                ["kind"] = activeReading.Kind,
                ["file"] = activeReading.File,
                ["title"] = activeReading.Title,
                ["page"] = JsonNode.Parse(
                    activeReading.Page.GetRawText()),
                ["fresh"] = true,
                ["ageSec"] = 0,
                ["observedAtEpochMs"] =
                    activeReading.ObservedAtEpochMilliseconds,
                ["receivedAtEpochMs"] =
                    _utcNow().ToUnixTimeMilliseconds(),
            };
            bool changedPage = _activeReading is not null
                && !SamePage(_activeReading, next);
            _activeReading = next;
            if (activeReading.SelectionState == "active")
            {
                _selection = ActiveSelection(
                    activeReading.Selection!,
                    next);
            }
            else if (activeReading.SelectionState == "cleared")
            {
                _selection = ClearedSelection(
                    "active-reading-cleared");
            }
            else if (
                changedPage
                || (
                    _selection["ref"] is JsonObject selectionRef
                    && !SamePage(selectionRef, next)
                )
            )
            {
                _selection = UnknownSelection(
                    "active-page-changed");
            }
            _revision = checked(_revision + 1);
            _latestEvent = new JsonObject
            {
                ["source"] = "active-reading",
                ["seq"] = null,
                ["id"] = requestId,
                ["type"] = "active.reading",
                ["ts"] = activeReading.ObservedAtEpochMilliseconds,
            };
            await PersistAsync(cancellationToken).ConfigureAwait(false);
            return new DirectSnapshotForwardResult("accepted", _revision);
        }
        finally
        {
            _gate.Release();
        }
    }

    public async Task<DirectSnapshotForwardResult> ClearAsync(
        string requestId,
        string sessionId,
        CancellationToken cancellationToken)
    {
        ValidateRequestIdentity(requestId, sessionId);
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            _revision = checked(_revision + 1);
            _stablePage = null;
            _activeReading = null;
            _selection = UnknownSelection("snapshot-cleared");
            _latestEvent = new JsonObject
            {
                ["source"] = "context-control",
                ["seq"] = null,
                ["id"] = requestId,
                ["type"] = "context.clear",
                ["ts"] = _utcNow().ToUnixTimeMilliseconds(),
            };
            await PersistAsync(cancellationToken).ConfigureAwait(false);
            return new DirectSnapshotForwardResult(
                "accepted",
                _revision);
        }
        finally
        {
            _gate.Release();
        }
    }

    internal static DirectActiveReading ValidateActiveReading(
        JsonElement value)
    {
        if (value.ValueKind != JsonValueKind.Object)
        {
            throw ActiveReadingInvalid();
        }
        try
        {
            DirectJsonValidation.RequireNoDuplicateKeys(value);
        }
        catch (DirectProtocolException exception)
        {
            throw ActiveReadingInvalid(exception);
        }
        HashSet<string> keys = value.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        if (!keys.SetEquals(new[]
        {
            "kind",
            "file",
            "title",
            "page",
            "selectionState",
            "selection",
            "observedAtEpochMs",
        }))
        {
            throw ActiveReadingInvalid();
        }
        if (
            value.GetProperty("kind").ValueKind != JsonValueKind.String
            || value.GetProperty("kind").GetString() is not string kind
            || kind is not ("pdf" or "epub" or "html" or "web")
            || value.GetProperty("file").ValueKind != JsonValueKind.String
            || value.GetProperty("file").GetString() is not string file
            || file.Length is < 1 or > 4096
            || file.Any(char.IsControl)
        )
        {
            throw ActiveReadingInvalid();
        }
        JsonElement titleValue = value.GetProperty("title");
        string? title = titleValue.ValueKind switch
        {
            JsonValueKind.Null => null,
            JsonValueKind.String => titleValue.GetString(),
            _ => throw ActiveReadingInvalid(),
        };
        if (
            title is { Length: > 1024 }
            || (title is not null && title.Any(char.IsControl))
        )
        {
            throw ActiveReadingInvalid();
        }
        JsonElement page = value.GetProperty("page");
        bool pageValid = page.ValueKind switch
        {
            JsonValueKind.Null => true,
            JsonValueKind.Number =>
                page.TryGetInt64(out long number)
                && number is >= 0 and <= 9_007_199_254_740_991,
            JsonValueKind.String =>
                page.GetString() is string text
                && text.Length is >= 1 and <= 256
                && !text.Any(char.IsControl),
            _ => false,
        };
        if (
            !pageValid
            || value.GetProperty("selectionState").ValueKind
                != JsonValueKind.String
            || value.GetProperty("selectionState").GetString()
                is not string selectionState
            || selectionState is not (
                "active" or "cleared" or "unknown")
            || !value.GetProperty("observedAtEpochMs")
                .TryGetInt64(out long observedAt)
            || observedAt is < 1 or > 9_007_199_254_740_991
        )
        {
            throw ActiveReadingInvalid();
        }
        JsonElement selectionValue = value.GetProperty("selection");
        string? selection = selectionValue.ValueKind switch
        {
            JsonValueKind.Null => null,
            JsonValueKind.String => selectionValue.GetString(),
            _ => throw ActiveReadingInvalid(),
        };
        if (
            selection is { Length: > 400 }
            || (
                selectionState == "active"
                && string.IsNullOrWhiteSpace(selection)
            )
            || (
                selectionState != "active"
                && selection is not null
            )
        )
        {
            throw ActiveReadingInvalid();
        }
        return new DirectActiveReading(
            kind,
            file,
            title,
            page.Clone(),
            selectionState,
            selection,
            observedAt);
    }

    private void FoldJournal(DirectContextEvent contextEvent)
    {
        JsonObject value = JsonNode.Parse(
            contextEvent.Payload.GetRawText()) as JsonObject
            ?? throw JournalInvalid();
        _revision = checked(_revision + 1);
        _latestEvent = new JsonObject
        {
            ["source"] = "outgoing-context",
            ["seq"] = contextEvent.Sequence,
            ["id"] = contextEvent.EventId,
            ["type"] = contextEvent.Type,
            ["ts"] = value["ts"]?.DeepClone(),
        };

        if (contextEvent.Type == "page.context")
        {
            FoldPageContext(value);
            return;
        }
        if (contextEvent.Type == "focus")
        {
            FoldFocus(value);
        }
    }

    private void FoldPageContext(JsonObject value)
    {
        string? file = StringValue(value["file"])
            ?? StringValue(value["book_id"]);
        JsonNode? page = value["page"]?.DeepClone();
        JsonObject? pageContext = value["page_context"] as JsonObject;
        if (
            string.IsNullOrWhiteSpace(file)
            || page is null
            || pageContext is null
        )
        {
            throw JournalInvalid();
        }
        JsonObject next = new()
        {
            ["file"] = file,
            ["title"] = StringValue(value["title"]),
            ["page"] = page,
            ["stable"] = value["stable"]?.GetValue<bool>() == true,
            ["reason"] = StringValue(pageContext["reason"]),
            ["text"] = StringValue(pageContext["text"]) ?? "",
            ["textAvailable"] =
                pageContext["text_available"]?.GetValue<bool?>(),
            ["textSource"] =
                StringValue(pageContext["text_source"]),
            ["truncated"] =
                pageContext["truncated"]?.GetValue<bool?>(),
        };
        bool changedPage = _stablePage is not null
            && !SamePage(_stablePage, next);
        _stablePage = next;
        _activeReading = new JsonObject
        {
            ["kind"] = StringValue(value["kind"]),
            ["file"] = file,
            ["title"] = StringValue(value["title"]),
            ["page"] = page.DeepClone(),
            ["fresh"] = true,
            ["ageSec"] = 0,
            ["observedAtEpochMs"] =
                EpochSecondsToMilliseconds(value["ts"]),
            ["receivedAtEpochMs"] =
                _utcNow().ToUnixTimeMilliseconds(),
        };
        if (changedPage)
        {
            _selection = ClearedSelection("stable-page-changed");
        }
    }

    private void FoldFocus(JsonObject value)
    {
        string? action = StringValue(value["action"]);
        if (action == "cancel")
        {
            _selection = new JsonObject
            {
                ["state"] = "cleared",
                ["text"] = null,
                ["ref"] = value["cancelledObject"]?["ref"]?.DeepClone(),
                ["reason"] = "explicit-cancel",
            };
            return;
        }
        if (action != "set")
        {
            throw JournalInvalid();
        }
        JsonObject? reference = value["ref"] as JsonObject;
        if (reference is null)
        {
            throw JournalInvalid();
        }
        _selection = new JsonObject
        {
            ["state"] = "active",
            ["text"] = StringValue(reference["text"]),
            ["ref"] = reference.DeepClone(),
            ["reason"] = null,
        };
    }

    private JsonObject BuildSnapshot()
    {
        string contextStatus = _stablePage is null
            ? "pending"
            : "ready";
        JsonObject? effectivePage = _stablePage?.DeepClone()
            as JsonObject;
        if (_activeReading is not null)
        {
            if (
                _stablePage is null
                || !SamePage(_stablePage, _activeReading)
            )
            {
                contextStatus = "pending";
                effectivePage = PendingPage(_activeReading);
            }
        }
        return new JsonObject
        {
            ["schema"] = SnapshotContract,
            ["revision"] = _revision,
            ["updatedAtUtc"] = _utcNow()
                .ToString("O"),
            ["latestEvent"] = _latestEvent?.DeepClone(),
            ["activeReading"] = _activeReading?.DeepClone(),
            ["contextStatus"] = contextStatus,
            ["currentPage"] = effectivePage,
            ["selection"] = _selection.DeepClone(),
        };
    }

    private async Task PersistAsync(CancellationToken cancellationToken)
    {
        JsonObject snapshot = BuildSnapshot();
        byte[] payload = JsonSerializer.SerializeToUtf8Bytes(
            snapshot,
            DirectBridgeContract.JsonOptions);
        if (payload.Length is < 1 or > MaximumSnapshotBytes)
        {
            throw new DirectProtocolException(
                "BW_READER_CONTEXT_SNAPSHOT_TOO_LARGE",
                "Windows 本地 Reader 快照超过大小上限");
        }
        string? directory = System.IO.Path.GetDirectoryName(_statePath);
        if (directory is null)
        {
            throw SnapshotWriteFailed();
        }
        Directory.CreateDirectory(directory);
        string temporaryPath = _statePath + ".tmp";
        try
        {
            await File.WriteAllTextAsync(
                temporaryPath,
                Utf8WithoutBom.GetString(payload),
                Utf8WithoutBom,
                cancellationToken).ConfigureAwait(false);
            File.Move(temporaryPath, _statePath, overwrite: true);
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (Exception exception) when (
            exception is IOException
            or UnauthorizedAccessException
            or ArgumentException
        )
        {
            throw SnapshotWriteFailed(exception);
        }
        finally
        {
            Array.Clear(payload);
        }
    }

    private void LoadExistingState()
    {
        try
        {
            if (!File.Exists(_statePath))
            {
                return;
            }
            FileInfo info = new(_statePath);
            if (info.Length is <= 0 or > MaximumSnapshotBytes)
            {
                return;
            }
            JsonObject? root = JsonNode.Parse(
                File.ReadAllText(_statePath, Encoding.UTF8)) as JsonObject;
            if (
                root?["schema"]?.GetValue<string>() != SnapshotContract
                || root["revision"]?.GetValue<long?>() is not long revision
                || revision < 0
            )
            {
                return;
            }
            _revision = revision;
            _activeReading = root["activeReading"]?.DeepClone()
                as JsonObject;
            JsonObject? currentPage = root["currentPage"]?.DeepClone()
                as JsonObject;
            if (currentPage?["stable"]?.GetValue<bool?>() == true)
            {
                _stablePage = currentPage;
            }
            _selection = root["selection"]?.DeepClone()
                as JsonObject
                ?? ClearedSelection("unknown");
            _latestEvent = root["latestEvent"]?.DeepClone()
                as JsonObject;
        }
        catch
        {
            // A damaged prior cache is not authoritative. Keep it untouched
            // until the first validated update atomically replaces it.
            _revision = 0;
            _stablePage = null;
            _activeReading = null;
            _selection = UnknownSelection("snapshot-invalid");
            _latestEvent = null;
        }
    }

    private void RememberEvent(string eventId)
    {
        if (!_recentEventIds.Add(eventId))
        {
            return;
        }
        _recentEventOrder.Enqueue(eventId);
        while (_recentEventOrder.Count > RecentEventLimit)
        {
            _recentEventIds.Remove(_recentEventOrder.Dequeue());
        }
    }

    private static JsonObject PendingPage(JsonObject active) =>
        new()
        {
            ["file"] = active["file"]?.DeepClone(),
            ["title"] = active["title"]?.DeepClone(),
            ["page"] = active["page"]?.DeepClone(),
            ["stable"] = false,
            ["text"] = "",
            ["textAvailable"] = false,
        };

    private static JsonObject ClearedSelection(string reason) =>
        new()
        {
            ["state"] = "cleared",
            ["text"] = null,
            ["ref"] = null,
            ["reason"] = reason,
        };

    private static JsonObject UnknownSelection(string reason) =>
        new()
        {
            ["state"] = "unknown",
            ["text"] = null,
            ["ref"] = null,
            ["reason"] = reason,
        };

    private static JsonObject ActiveSelection(
        string text,
        JsonObject active) =>
        new()
        {
            ["state"] = "active",
            ["text"] = text,
            ["ref"] = new JsonObject
            {
                ["file"] = active["file"]?.DeepClone(),
                ["page"] = active["page"]?.DeepClone(),
            },
            ["reason"] = null,
        };

    private static bool SamePage(JsonObject left, JsonObject right) =>
        string.Equals(
            StringValue(left["file"]),
            StringValue(right["file"]),
            StringComparison.Ordinal)
        && JsonNode.DeepEquals(left["page"], right["page"]);

    private static string? StringValue(JsonNode? value)
    {
        if (value is null)
        {
            return null;
        }
        try
        {
            return value.GetValue<string>();
        }
        catch (InvalidOperationException)
        {
            return null;
        }
    }

    private static long? EpochSecondsToMilliseconds(JsonNode? value)
    {
        try
        {
            return checked(value?.GetValue<long>() * 1000);
        }
        catch (Exception exception) when (
            exception is InvalidOperationException or OverflowException
        )
        {
            return null;
        }
    }

    private static void ValidateRequestIdentity(
        string requestId,
        string sessionId)
    {
        if (
            !DirectBridgeContract.IsSafeId(requestId)
            || DirectPcmFrameCodec.ParseSessionId(sessionId).Length != 16
        )
        {
            throw new DirectProtocolException(
                "BW_READER_CONTEXT_SNAPSHOT_SCHEMA_INVALID",
                "Windows 本地 Reader 快照请求标识无效");
        }
    }

    private static DirectProtocolException ActiveReadingInvalid(
        Exception? inner = null) =>
        new(
            "BW_READER_ACTIVE_READING_SCHEMA_INVALID",
            "Reader active-reading 更新无效",
            retryable: false,
            innerException: inner);

    private static DirectProtocolException JournalInvalid() =>
        new(
            "BW_READER_CONTEXT_SNAPSHOT_EVENT_INVALID",
            "Reader outgoing context 无法折叠为本地快照");

    private static DirectProtocolException SnapshotWriteFailed(
        Exception? inner = null) =>
        new(
            "BW_READER_CONTEXT_SNAPSHOT_WRITE_FAILED",
            "Windows 本地 Reader 快照写入失败",
            retryable: true,
            innerException: inner);
}
