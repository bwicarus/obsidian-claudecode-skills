using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace BwReader.ComputerVoiceAudio;

internal sealed record ReaderVisualDeliveryRequest(
    string Correlation,
    string SourceInstanceId,
    long SnapshotRevision,
    string File,
    JsonNode Page,
    string? DrawingRevision,
    string Scope,
    string? SelectionId);

internal sealed record ReaderVisualDeliveryChunk(
    string SessionId,
    string Correlation,
    string SourceInstanceId,
    long SnapshotRevision,
    string File,
    JsonElement Page,
    string? DrawingRevision,
    string Scope,
    string? SelectionId,
    string Status,
    string MimeType,
    uint ChunkIndex,
    uint ChunkCount,
    uint TotalBytes,
    string Data);

internal sealed record ReaderVisualDeliveryAck(
    string Correlation,
    uint ChunkIndex,
    bool Accepted,
    bool Complete);

internal sealed record ReaderVisualCapture(
    string MimeType,
    byte[] Data);

internal sealed class ReaderVisualDeliveryException : Exception
{
    internal ReaderVisualDeliveryException(
        string code,
        string message,
        bool retryable,
        Exception? innerException = null)
        : base(message, innerException)
    {
        Code = code;
        Retryable = retryable;
    }

    internal string Code { get; }
    internal bool Retryable { get; }
}

internal static class ReaderVisualDeliveryProtocol
{
    internal const string DeliveryContract =
        "reader-visual-delivery/2";
    internal const string EventName = "reader-visual-request";
    internal const string ChunkType = "reader-visual";
    internal const string RegisterType = "visual-register";
    internal const string MimeType = "image/jpeg";
    internal const int MaximumImageBytes = 768 * 1024;
    internal const int ChunkCharacters = 48_000;
    internal const int MaximumChunkCount = 24;

    internal static bool IsScope(string value) => value is
        "viewport-context" or "drawing-nearby" or "selection-near";

    internal static object Event(ReaderVisualDeliveryRequest request) =>
        new
        {
            contract = DirectBridgeContract.Contract,
            type = "event",
            @event = EventName,
            payload = new
            {
                contract = DeliveryContract,
                commandKind = "capture-composite",
                correlation = request.Correlation,
                sourceInstanceId = request.SourceInstanceId,
                snapshotRevision = request.SnapshotRevision,
                file = request.File,
                page = request.Page,
                drawingRevision = request.DrawingRevision,
                scope = request.Scope,
                selectionId = request.SelectionId,
                maxBytes = MaximumImageBytes,
                chunkCharacters = ChunkCharacters,
            },
        };

    internal static ReaderVisualDeliveryChunk ValidateChunk(
        JsonElement message)
    {
        RequireExactFields(
            message,
            "contract",
            "type",
            "requestId",
            "sessionId",
            "correlation",
            "sourceInstanceId",
            "snapshotRevision",
            "file",
            "page",
            "drawingRevision",
            "scope",
            "selectionId",
            "status",
            "mimeType",
            "chunkIndex",
            "chunkCount",
            "totalBytes",
            "data");
        if (
            RequiredString(message, "contract", 128)
                != DirectBridgeContract.Contract
            || RequiredString(message, "type", 32) != ChunkType
        )
        {
            throw Invalid("Reader 视觉消息合同无效");
        }
        string sessionId = RequiredSafeId(message, "sessionId");
        _ = DirectPcmFrameCodec.ParseSessionId(sessionId);
        string correlation = RequiredSafeId(message, "correlation");
        string sourceInstanceId = RequiredSafeId(
            message,
            "sourceInstanceId");
        long snapshotRevision = RequiredInt64(
            message,
            "snapshotRevision");
        if (snapshotRevision < 0)
        {
            throw Invalid("Reader 视觉 snapshotRevision 无效");
        }
        string file = RequiredString(message, "file", 4096);
        if (file.Any(char.IsControl))
        {
            throw Invalid("Reader 视觉 file 无效");
        }
        JsonElement page = message.GetProperty("page");
        if (!ValidPage(page))
        {
            throw Invalid("Reader 视觉 page 无效");
        }
        string? drawingRevision = OptionalSafeString(
            message,
            "drawingRevision",
            160);
        string scope = RequiredString(message, "scope", 32);
        if (!IsScope(scope))
        {
            throw Invalid("Reader 视觉 scope 无效");
        }
        string? selectionId = OptionalSafeString(
            message,
            "selectionId",
            160);
        if ((scope == "selection-near") != (selectionId is not null))
        {
            throw Invalid("Reader 视觉 selectionId 与 scope 不匹配");
        }
        string status = RequiredString(message, "status", 16);
        string mimeType = RequiredString(
            message,
            "mimeType",
            32,
            allowEmpty: status == "unavailable");
        uint chunkIndex = RequiredUInt32(message, "chunkIndex");
        uint chunkCount = RequiredUInt32(message, "chunkCount");
        uint totalBytes = RequiredUInt32(message, "totalBytes");
        string data = RequiredString(
            message,
            "data",
            ChunkCharacters,
            allowEmpty: status == "unavailable");
        if (status == "unavailable")
        {
            if (
                mimeType.Length != 0
                || chunkIndex != 0
                || chunkCount != 0
                || totalBytes != 0
                || data.Length != 0
            )
            {
                throw Invalid("Reader 视觉 unavailable 字段无效");
            }
        }
        else if (
            status != "chunk"
            || mimeType != MimeType
            || chunkCount is < 1 or > MaximumChunkCount
            || chunkIndex >= chunkCount
            || totalBytes is < 1 or > MaximumImageBytes
            || data.Length == 0
            || data.Length % 4 != 0
            || (
                chunkIndex + 1 < chunkCount
                && data.Length != ChunkCharacters
            )
            || !IsBase64(data)
        )
        {
            throw Invalid("Reader 视觉 chunk 字段无效");
        }
        return new ReaderVisualDeliveryChunk(
            sessionId,
            correlation,
            sourceInstanceId,
            snapshotRevision,
            file,
            page.Clone(),
            drawingRevision,
            scope,
            selectionId,
            status,
            mimeType,
            chunkIndex,
            chunkCount,
            totalBytes,
            data);
    }

    internal static bool PageEquivalent(
        JsonNode? expected,
        JsonElement actual)
    {
        if (expected is null)
        {
            return false;
        }
        if (
            expected.GetValueKind() == JsonValueKind.Number
            && actual.ValueKind == JsonValueKind.Number
        )
        {
            JsonValue expectedValue = expected.AsValue();
            long? expectedNumber =
                expectedValue.TryGetValue(out long longValue)
                    ? longValue
                    : expectedValue.TryGetValue(out int intValue)
                        ? intValue
                        : null;
            return expectedNumber is not null
                && actual.TryGetInt64(out long actualNumber)
                && expectedNumber.Value == actualNumber;
        }
        if (
            expected.GetValueKind() == JsonValueKind.String
            && actual.ValueKind == JsonValueKind.String
        )
        {
            return string.Equals(
                expected.GetValue<string>(),
                actual.GetString(),
                StringComparison.Ordinal);
        }
        return false;
    }

    internal static bool IsJpeg(ReadOnlySpan<byte> bytes) =>
        bytes.Length >= 4
        && bytes[0] == 0xff
        && bytes[1] == 0xd8
        && bytes[^2] == 0xff
        && bytes[^1] == 0xd9;

    private static bool ValidPage(JsonElement page)
    {
        if (
            page.ValueKind == JsonValueKind.Number
            && page.TryGetInt64(out long number)
        )
        {
            return number >= 0;
        }
        return page.ValueKind == JsonValueKind.String
            && page.GetString() is string text
            && text.Length is >= 1 and <= 256
            && !text.Any(char.IsControl);
    }

    private static bool IsBase64(string value)
    {
        foreach (char character in value)
        {
            if (!(
                character is >= 'A' and <= 'Z'
                or >= 'a' and <= 'z'
                or >= '0' and <= '9'
                or '+' or '/' or '='
            ))
            {
                return false;
            }
        }
        return true;
    }

    private static string? OptionalSafeString(
        JsonElement message,
        string name,
        int maximumLength)
    {
        JsonElement value = message.GetProperty(name);
        if (value.ValueKind == JsonValueKind.Null)
        {
            return null;
        }
        if (
            value.ValueKind != JsonValueKind.String
            || value.GetString() is not string result
            || result.Length is < 1
            || result.Length > maximumLength
            || !DirectBridgeContract.IsSafeId(result)
        )
        {
            throw Invalid($"Reader 视觉 {name} 无效");
        }
        return result;
    }

    private static string RequiredSafeId(
        JsonElement message,
        string name)
    {
        string result = RequiredString(message, name, 160);
        if (!DirectBridgeContract.IsSafeId(result))
        {
            throw Invalid($"Reader 视觉 {name} 无效");
        }
        return result;
    }

    private static string RequiredString(
        JsonElement message,
        string name,
        int maximumLength,
        bool allowEmpty = false)
    {
        if (
            !message.TryGetProperty(name, out JsonElement value)
            || value.ValueKind != JsonValueKind.String
            || value.GetString() is not string result
            || (!allowEmpty && result.Length == 0)
            || result.Length > maximumLength
        )
        {
            throw Invalid($"Reader 视觉 {name} 无效");
        }
        return result;
    }

    private static long RequiredInt64(
        JsonElement message,
        string name)
    {
        if (
            !message.TryGetProperty(name, out JsonElement value)
            || value.ValueKind != JsonValueKind.Number
            || !value.TryGetInt64(out long result)
        )
        {
            throw Invalid($"Reader 视觉 {name} 无效");
        }
        return result;
    }

    private static uint RequiredUInt32(
        JsonElement message,
        string name)
    {
        if (
            !message.TryGetProperty(name, out JsonElement value)
            || value.ValueKind != JsonValueKind.Number
            || !value.TryGetUInt32(out uint result)
        )
        {
            throw Invalid($"Reader 视觉 {name} 无效");
        }
        return result;
    }

    private static void RequireExactFields(
        JsonElement value,
        params string[] expected)
    {
        if (value.ValueKind != JsonValueKind.Object)
        {
            throw Invalid("Reader 视觉消息必须是对象");
        }
        DirectJsonValidation.RequireNoDuplicateKeys(value);
        HashSet<string> actual = value.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        if (!actual.SetEquals(expected))
        {
            throw Invalid("Reader 视觉消息字段不匹配");
        }
    }

    private static DirectProtocolException Invalid(string message) =>
        new(
            "BW_READER_VISUAL_SCHEMA_INVALID",
            message,
            retryable: false);
}

internal sealed class ReaderContextSourceLease
{
    private readonly TaskCompletionSource<bool> _retired = new(
        TaskCreationOptions.RunContinuationsAsynchronously);

    internal ReaderContextSourceLease(
        string sourceInstanceId,
        string connectionId,
        long generation)
    {
        SourceInstanceId = sourceInstanceId;
        ConnectionId = connectionId;
        Generation = generation;
    }

    internal string SourceInstanceId { get; }
    internal string ConnectionId { get; }
    internal long Generation { get; }
    internal Task LeaseRetired => _retired.Task;
    internal void Retire() => _retired.TrySetResult(true);
}

internal sealed class ReaderContextSourceRouter
{
    private sealed record SourceRegistration(
        ReaderContextSourceLease Lease,
        Func<object, CancellationToken, Task> SendAsync);

    private readonly object _gate = new();
    private readonly Dictionary<string, SourceRegistration> _sources =
        new(StringComparer.Ordinal);
    private long _generation;

    internal ReaderContextSourceLease Attach(
        string sourceInstanceId,
        string connectionId,
        Func<object, CancellationToken, Task> sendAsync)
    {
        if (
            !DirectBridgeContract.IsSafeId(sourceInstanceId)
            || !DirectBridgeContract.IsSafeId(connectionId)
        )
        {
            throw new ArgumentException(
                "source and connection must be safe identifiers");
        }
        ArgumentNullException.ThrowIfNull(sendAsync);
        ReaderContextSourceLease? retired = null;
        ReaderContextSourceLease lease;
        lock (_gate)
        {
            if (_sources.TryGetValue(
                sourceInstanceId,
                out SourceRegistration? existing))
            {
                retired = existing.Lease;
            }
            lease = new ReaderContextSourceLease(
                sourceInstanceId,
                connectionId,
                checked(++_generation));
            _sources[sourceInstanceId] = new SourceRegistration(
                lease,
                sendAsync);
        }
        retired?.Retire();
        return lease;
    }

    internal void Detach(ReaderContextSourceLease lease)
    {
        ArgumentNullException.ThrowIfNull(lease);
        bool removed = false;
        lock (_gate)
        {
            if (
                _sources.TryGetValue(
                    lease.SourceInstanceId,
                    out SourceRegistration? current)
                && ReferenceEquals(current.Lease, lease)
            )
            {
                _sources.Remove(lease.SourceInstanceId);
                removed = true;
            }
        }
        if (removed)
        {
            lease.Retire();
        }
    }

    internal bool TryGetLease(
        string sourceInstanceId,
        out ReaderContextSourceLease? lease)
    {
        lock (_gate)
        {
            if (_sources.TryGetValue(
                sourceInstanceId,
                out SourceRegistration? registration))
            {
                lease = registration.Lease;
                return true;
            }
        }
        lease = null;
        return false;
    }

    // 找不到租约时，桥自己知道一件能立刻分清病因的事：它到底注册了几个来源。
    //
    //   一个都没有 → 那个页面从未完成 visual-register(连接没建/握手没走完)
    //   有别的     → 注册是通的,但注册下来的来源跟快照报的不是同一个
    //
    // 两者的修法完全不同,而「来源不在线」这句话对它们一视同仁。快照里
    // 明明有来源标识却取不到图时,分不清这两种就只能靠猜。
    //
    // 只回数量和前 8 位:够区分是谁,不把完整标识写进会流向模型的错误里。
    internal string DescribeRegisteredSources()
    {
        lock (_gate)
        {
            if (_sources.Count == 0)
            {
                return "桥上当前没有任何已注册的来源";
            }
            IEnumerable<string> heads = _sources.Keys
                .OrderBy(key => key, StringComparer.Ordinal)
                .Take(4)
                .Select(key => key.Length > 8 ? key[..8] + "…" : key);
            return $"桥上已注册 {_sources.Count} 个来源(" +
                string.Join(", ", heads) + ")，其中没有这一个";
        }
    }

    internal async Task SendAsync(
        ReaderContextSourceLease lease,
        object message,
        CancellationToken cancellationToken)
    {
        Func<object, CancellationToken, Task> sendAsync;
        lock (_gate)
        {
            if (
                !_sources.TryGetValue(
                    lease.SourceInstanceId,
                    out SourceRegistration? current)
                || !ReferenceEquals(current.Lease, lease)
            )
            {
                throw new ReaderVisualDeliveryException(
                    "BW_READER_SOURCE_OFFLINE",
                    "指定 Reader 页面来源已离线",
                    retryable: true);
            }
            sendAsync = current.SendAsync;
        }
        await sendAsync(message, cancellationToken).ConfigureAwait(false);
    }
}

internal sealed class ReaderVisualDeliveryBroker
{
    private static readonly TimeSpan DeliveryTimeout =
        TimeSpan.FromSeconds(12);
    private const int MaximumPendingDeliveries = 4;

    private sealed class PendingDelivery
    {
        internal PendingDelivery(
            ReaderVisualDeliveryRequest request,
            ReaderContextSourceLease lease)
        {
            Request = request;
            Lease = lease;
        }

        internal ReaderVisualDeliveryRequest Request { get; }
        internal ReaderContextSourceLease Lease { get; }
        internal TaskCompletionSource<ReaderVisualCapture?> Completion
            { get; } = new(
                TaskCreationOptions.RunContinuationsAsynchronously);
        internal StringBuilder Base64 { get; } = new();
        internal uint ExpectedChunkIndex { get; set; }
        internal uint? ChunkCount { get; set; }
        internal uint? TotalBytes { get; set; }
    }

    private readonly ReaderContextSourceRouter _router;
    private readonly object _gate = new();
    private readonly Dictionary<string, PendingDelivery> _pending =
        new(StringComparer.Ordinal);

    internal ReaderVisualDeliveryBroker(
        ReaderContextSourceRouter router)
    {
        _router = router;
    }

    internal async Task<ReaderVisualCapture?> RequestAsync(
        ReaderVisualDeliveryRequest request,
        CancellationToken cancellationToken)
    {
        if (
            !_router.TryGetLease(
                request.SourceInstanceId,
                out ReaderContextSourceLease? lease)
            || lease is null
        )
        {
            // 「不在线」只说了结果。快照里明明带着来源标识却取不到图时,
            // 真正要分清的是:那个页面从没注册过,还是注册下来的是别人。
            // 桥自己知道答案,不说出来就只能靠猜。
            throw new ReaderVisualDeliveryException(
                "BW_READER_VISUAL_SOURCE_OFFLINE",
                "快照指定的 Reader 页面来源当前不在线（"
                    + _router.DescribeRegisteredSources() + "）",
                retryable: true);
        }
        PendingDelivery pending = new(request, lease);
        lock (_gate)
        {
            if (_pending.Count >= MaximumPendingDeliveries)
            {
                throw new ReaderVisualDeliveryException(
                    "BW_READER_VISUAL_CAPACITY",
                    "Reader 视觉请求仍在处理中",
                    retryable: true);
            }
            if (!_pending.TryAdd(request.Correlation, pending))
            {
                throw new ReaderVisualDeliveryException(
                    "BW_READER_VISUAL_DUPLICATE_PENDING",
                    "相同 Reader 视觉请求仍在处理中",
                    retryable: true);
            }
        }
        try
        {
            await _router.SendAsync(
                lease,
                ReaderVisualDeliveryProtocol.Event(request),
                cancellationToken).ConfigureAwait(false);
            Task winner = await Task.WhenAny(
                pending.Completion.Task,
                lease.LeaseRetired,
                Task.Delay(DeliveryTimeout, cancellationToken))
                .ConfigureAwait(false);
            cancellationToken.ThrowIfCancellationRequested();
            if (winner == pending.Completion.Task)
            {
                return await pending.Completion.Task.ConfigureAwait(false);
            }
            if (winner == lease.LeaseRetired)
            {
                throw new ReaderVisualDeliveryException(
                    "BW_READER_VISUAL_SOURCE_OFFLINE",
                    "取图期间指定 Reader 页面来源已离线",
                    retryable: true);
            }
            throw new ReaderVisualDeliveryException(
                "BW_READER_VISUAL_TIMEOUT",
                "Reader 视觉获取超时",
                retryable: true);
        }
        finally
        {
            lock (_gate)
            {
                if (
                    _pending.TryGetValue(
                        request.Correlation,
                        out PendingDelivery? current)
                    && ReferenceEquals(current, pending)
                )
                {
                    _pending.Remove(request.Correlation);
                }
            }
        }
    }

    internal ReaderVisualDeliveryAck Accept(
        ReaderContextSourceLease lease,
        ReaderVisualDeliveryChunk chunk)
    {
        PendingDelivery pending;
        ReaderVisualCapture? completed = null;
        lock (_gate)
        {
            if (
                !_pending.TryGetValue(chunk.Correlation, out pending!)
                || !ReferenceEquals(pending.Lease, lease)
            )
            {
                throw ProtocolFailure(
                    "BW_READER_VISUAL_NOT_PENDING",
                    "Reader 视觉请求不存在或已过期");
            }
            RequireIdentity(pending.Request, chunk);
            if (chunk.Status == "unavailable")
            {
                _pending.Remove(chunk.Correlation);
                pending.Completion.TrySetResult(null);
            }
            else
            {
                if (
                    chunk.ChunkIndex != pending.ExpectedChunkIndex
                    || (
                        pending.ChunkCount is uint count
                        && count != chunk.ChunkCount
                    )
                    || (
                        pending.TotalBytes is uint bytes
                        && bytes != chunk.TotalBytes
                    )
                )
                {
                    throw ProtocolFailure(
                        "BW_READER_VISUAL_SEQUENCE_INVALID",
                        "Reader 视觉分块顺序或元数据不一致");
                }
                pending.ChunkCount ??= chunk.ChunkCount;
                pending.TotalBytes ??= chunk.TotalBytes;
                pending.Base64.Append(chunk.Data);
                pending.ExpectedChunkIndex += 1;
                if (
                    pending.Base64.Length
                    > ReaderVisualDeliveryProtocol.MaximumImageBytes
                        * 4 / 3 + 4
                )
                {
                    throw ProtocolFailure(
                        "BW_READER_VISUAL_CAPACITY",
                        "Reader 视觉数据超过大小上限");
                }
                if (pending.ExpectedChunkIndex == chunk.ChunkCount)
                {
                    byte[] data;
                    try
                    {
                        data = Convert.FromBase64String(
                            pending.Base64.ToString());
                    }
                    catch (FormatException exception)
                    {
                        throw ProtocolFailure(
                            "BW_READER_VISUAL_SCHEMA_INVALID",
                            "Reader 视觉 base64 无效",
                            exception);
                    }
                    if (
                        data.Length != chunk.TotalBytes
                        || !ReaderVisualDeliveryProtocol.IsJpeg(data)
                    )
                    {
                        Array.Clear(data);
                        throw ProtocolFailure(
                            "BW_READER_VISUAL_IMAGE_INVALID",
                            "Reader 视觉 JPEG 无效");
                    }
                    completed = new ReaderVisualCapture(
                        ReaderVisualDeliveryProtocol.MimeType,
                        data);
                    _pending.Remove(chunk.Correlation);
                    pending.Completion.TrySetResult(completed);
                }
            }
        }
        return new ReaderVisualDeliveryAck(
            chunk.Correlation,
            chunk.ChunkIndex,
            Accepted: true,
            Complete:
                chunk.Status == "unavailable"
                || completed is not null);
    }

    private static void RequireIdentity(
        ReaderVisualDeliveryRequest request,
        ReaderVisualDeliveryChunk chunk)
    {
        if (
            request.SourceInstanceId != chunk.SourceInstanceId
            || request.SnapshotRevision != chunk.SnapshotRevision
            || request.File != chunk.File
            || !ReaderVisualDeliveryProtocol.PageEquivalent(
                request.Page,
                chunk.Page)
            || request.DrawingRevision != chunk.DrawingRevision
            || request.Scope != chunk.Scope
            || request.SelectionId != chunk.SelectionId
        )
        {
            throw ProtocolFailure(
                "BW_READER_VISUAL_IDENTITY_MISMATCH",
                "Reader 视觉来源、页面或版本与请求不一致");
        }
    }

    private static ReaderVisualDeliveryException ProtocolFailure(
        string code,
        string message,
        Exception? innerException = null) =>
        new(
            code,
            message,
            retryable: false,
            innerException);
}
