using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Net.WebSockets;

namespace BwReader.ComputerVoiceAudio;

internal sealed record ReaderVisualDeliveryRequest(
    string Correlation,
    string File,
    JsonNode Page,
    string DrawingRevision);

internal sealed record ReaderVisualDeliveryChunk(
    string Correlation,
    string Status,
    string File,
    JsonElement Page,
    string DrawingRevision,
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

internal static class ReaderVisualDeliveryProtocol
{
    internal const string DeliveryContract =
        "reader-visual-delivery/1";
    internal const string EventName = "reader-visual-request";
    internal const string ChunkType = "reader-visual";
    internal const int MaximumImageBytes = 768 * 1024;
    internal const int ChunkCharacters = 48_000;
    internal const int MaximumChunkCount = 20;
    internal const string MimeType = "image/jpeg";

    internal static object Event(
        ReaderVisualDeliveryRequest request) =>
        new
        {
            contract = DirectBridgeContract.Contract,
            type = "event",
            @event = EventName,
            payload = new
            {
                contract = DeliveryContract,
                correlation = request.Correlation,
                file = request.File,
                page = request.Page,
                drawingRevision = request.DrawingRevision,
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
            "correlation",
            "status",
            "file",
            "page",
            "drawingRevision",
            "mimeType",
            "chunkIndex",
            "chunkCount",
            "totalBytes",
            "data");
        string correlation = RequiredSafeId(
            message,
            "correlation");
        string status = RequiredString(message, "status", 16);
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
        string revision = RequiredString(
            message,
            "drawingRevision",
            19);
        if (!IsDrawingRevision(revision))
        {
            throw Invalid("Reader 视觉 drawingRevision 无效");
        }
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
            correlation,
            status,
            file,
            page.Clone(),
            revision,
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
            return expected.GetValue<long>() == actual.GetInt64();
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

    private static bool ValidPage(JsonElement page)
    {
        if (
            page.ValueKind == JsonValueKind.Number
            && page.TryGetInt64(out long number)
        )
        {
            return number >= 0;
        }
        if (
            page.ValueKind == JsonValueKind.String
            && page.GetString() is string text
        )
        {
            return text.Length is >= 1 and <= 256
                && !text.Any(char.IsControl);
        }
        return false;
    }

    private static bool IsDrawingRevision(string revision) =>
        revision.Length == 19
        && revision.StartsWith("dr_", StringComparison.Ordinal)
        && revision.AsSpan(3).IndexOfAnyExcept(
            "0123456789abcdef") < 0;

    private static bool IsBase64(string value)
    {
        foreach (char character in value)
        {
            if (
                character is >= 'A' and <= 'Z'
                or >= 'a' and <= 'z'
                or >= '0' and <= '9'
                or '+'
                or '/'
                or '='
            )
            {
                continue;
            }
            return false;
        }
        int padding = value.EndsWith(
            "==",
            StringComparison.Ordinal)
            ? 2
            : value.EndsWith(
                "=",
                StringComparison.Ordinal)
                ? 1
                : 0;
        return value.AsSpan(0, value.Length - padding)
            .IndexOf('=') < 0;
    }

    private static string RequiredSafeId(
        JsonElement value,
        string field)
    {
        string result = RequiredString(value, field, 160);
        if (!DirectBridgeContract.IsSafeId(result))
        {
            throw Invalid($"Reader 视觉 {field} 无效");
        }
        return result;
    }

    private static string RequiredString(
        JsonElement value,
        string field,
        int maximumLength,
        bool allowEmpty = false)
    {
        if (
            !value.TryGetProperty(field, out JsonElement property)
            || property.ValueKind != JsonValueKind.String
            || property.GetString() is not string result
            || result.Length > maximumLength
            || (!allowEmpty && result.Length == 0)
        )
        {
            throw Invalid($"Reader 视觉 {field} 无效");
        }
        return result;
    }

    private static uint RequiredUInt32(
        JsonElement value,
        string field)
    {
        if (
            !value.TryGetProperty(field, out JsonElement property)
            || property.ValueKind != JsonValueKind.Number
            || !property.TryGetUInt32(out uint result)
        )
        {
            throw Invalid($"Reader 视觉 {field} 无效");
        }
        return result;
    }

    private static void RequireExactFields(
        JsonElement value,
        params string[] fields)
    {
        if (value.ValueKind != JsonValueKind.Object)
        {
            throw Invalid("Reader 视觉消息必须是对象");
        }
        HashSet<string> actual = value.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        if (!actual.SetEquals(fields))
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

internal sealed class ReaderVisualDeliveryException : Exception
{
    internal string Code { get; }

    internal bool Retryable { get; }

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
}

internal sealed class ReaderVisualDeliveryBroker
{
    private const int MaximumPendingDeliveries = 1;
    private static readonly TimeSpan DeliveryTimeout =
        TimeSpan.FromSeconds(12);

    private sealed class PendingDelivery
    {
        internal PendingDelivery(
            ReaderVisualDeliveryRequest request,
            TaskCompletionSource<ReaderVisualCapture?> completion)
        {
            Request = request;
            Completion = completion;
        }

        internal ReaderVisualDeliveryRequest Request { get; }
        internal TaskCompletionSource<ReaderVisualCapture?> Completion
        {
            get;
        }
        internal StringBuilder Base64 { get; } = new();
        internal uint ExpectedChunkIndex { get; set; }
        internal uint? ChunkCount { get; set; }
        internal uint? TotalBytes { get; set; }
    }

    private readonly object _gate = new();
    private readonly Dictionary<string, PendingDelivery> _pending =
        new(StringComparer.Ordinal);
    private string? _connectionId;
    private Func<object, CancellationToken, Task>? _sendAsync;

    internal void Attach(
        string connectionId,
        Func<object, CancellationToken, Task> sendAsync)
    {
        ArgumentNullException.ThrowIfNull(sendAsync);
        lock (_gate)
        {
            _connectionId = connectionId;
            _sendAsync = sendAsync;
        }
    }

    internal void Detach(string connectionId)
    {
        PendingDelivery[] abandoned;
        lock (_gate)
        {
            if (!string.Equals(
                _connectionId,
                connectionId,
                StringComparison.Ordinal))
            {
                return;
            }
            _connectionId = null;
            _sendAsync = null;
            abandoned = _pending.Values.ToArray();
            _pending.Clear();
        }
        ReaderVisualDeliveryException failure = new(
            "BW_READER_VISUAL_READER_DISCONNECTED",
            "Reader 视觉获取前直连已断开",
            retryable: true);
        foreach (PendingDelivery pending in abandoned)
        {
            pending.Completion.TrySetException(failure);
        }
    }

    internal ReaderVisualDeliveryAck Accept(
        string connectionId,
        ReaderVisualDeliveryChunk chunk)
    {
        PendingDelivery pending;
        ReaderVisualCapture? completed = null;
        lock (_gate)
        {
            if (
                !string.Equals(
                    _connectionId,
                    connectionId,
                    StringComparison.Ordinal)
                || !_pending.TryGetValue(
                    chunk.Correlation,
                    out pending!)
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
                return new ReaderVisualDeliveryAck(
                    chunk.Correlation,
                    0,
                    Accepted: true,
                    Complete: true);
            }
            if (
                chunk.ChunkIndex != pending.ExpectedChunkIndex
                || (
                    pending.ChunkCount is uint count
                    && count != chunk.ChunkCount
                )
                || (
                    pending.TotalBytes is uint expectedBytes
                    && expectedBytes != chunk.TotalBytes
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
                    > ReaderVisualDeliveryProtocol
                        .MaximumImageBytes * 4 / 3 + 4
            )
            {
                throw ProtocolFailure(
                    "BW_READER_VISUAL_CAPACITY",
                    "Reader 视觉数据超过大小上限");
            }
            if (pending.ExpectedChunkIndex == chunk.ChunkCount)
            {
                byte[] bytes;
                try
                {
                    bytes = Convert.FromBase64String(
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
                    bytes.Length != chunk.TotalBytes
                    || !IsJpeg(bytes)
                )
                {
                    throw ProtocolFailure(
                        "BW_READER_VISUAL_IMAGE_INVALID",
                        "Reader 视觉 JPEG 无效");
                }
                completed = new ReaderVisualCapture(
                    ReaderVisualDeliveryProtocol.MimeType,
                    bytes);
                _pending.Remove(chunk.Correlation);
                pending.Completion.TrySetResult(completed);
            }
        }
        return new ReaderVisualDeliveryAck(
            chunk.Correlation,
            chunk.ChunkIndex,
            Accepted: true,
            Complete: completed is not null);
    }

    internal async Task<ReaderVisualCapture?> RequestAsync(
        ReaderVisualDeliveryRequest request,
        CancellationToken cancellationToken)
    {
        Func<object, CancellationToken, Task> sendAsync;
        TaskCompletionSource<ReaderVisualCapture?> completion = new(
            TaskCreationOptions.RunContinuationsAsynchronously);
        lock (_gate)
        {
            if (_sendAsync is null || _connectionId is null)
            {
                throw new ReaderVisualDeliveryException(
                    "BW_READER_VISUAL_READER_OFFLINE",
                    "当前没有已认证的 Reader 直连可获取视觉",
                    retryable: true);
            }
            if (_pending.Count >= MaximumPendingDeliveries)
            {
                throw new ReaderVisualDeliveryException(
                    "BW_READER_VISUAL_CAPACITY",
                    "Reader 视觉请求仍在处理中",
                    retryable: true);
            }
            if (!_pending.TryAdd(
                request.Correlation,
                new PendingDelivery(request, completion)))
            {
                throw new ReaderVisualDeliveryException(
                    "BW_READER_VISUAL_DUPLICATE_PENDING",
                    "相同 Reader 视觉请求仍在处理中",
                    retryable: true);
            }
            sendAsync = _sendAsync;
        }
        try
        {
            await sendAsync(
                ReaderVisualDeliveryProtocol.Event(request),
                cancellationToken).ConfigureAwait(false);
            try
            {
                return await completion.Task.WaitAsync(
                    DeliveryTimeout,
                    cancellationToken).ConfigureAwait(false);
            }
            catch (TimeoutException exception)
            {
                throw new ReaderVisualDeliveryException(
                    "BW_READER_VISUAL_TIMEOUT",
                    "Reader 视觉获取超时",
                    retryable: true,
                    exception);
            }
        }
        catch (ReaderVisualDeliveryException)
        {
            throw;
        }
        catch (OperationCanceledException)
            when (cancellationToken.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception exception)
            when (
                exception is IOException
                or WebSocketException
                or ObjectDisposedException
                or InvalidOperationException
            )
        {
            throw new ReaderVisualDeliveryException(
                "BW_READER_VISUAL_SEND_FAILED",
                "Reader 视觉请求无法写入当前直连",
                retryable: true,
                exception);
        }
        finally
        {
            lock (_gate)
            {
                if (
                    _pending.TryGetValue(
                        request.Correlation,
                        out PendingDelivery? current)
                    && ReferenceEquals(
                        current.Completion,
                        completion)
                )
                {
                    _pending.Remove(request.Correlation);
                }
            }
        }
    }

    private static void RequireIdentity(
        ReaderVisualDeliveryRequest request,
        ReaderVisualDeliveryChunk chunk)
    {
        if (
            !string.Equals(
                request.File,
                chunk.File,
                StringComparison.Ordinal)
            || !ReaderVisualDeliveryProtocol.PageEquivalent(
                request.Page,
                chunk.Page)
            || !string.Equals(
                request.DrawingRevision,
                chunk.DrawingRevision,
                StringComparison.Ordinal)
        )
        {
            throw ProtocolFailure(
                "BW_READER_VISUAL_IDENTITY_MISMATCH",
                "Reader 视觉页或笔迹版本与请求不一致");
        }
    }

    private static bool IsJpeg(byte[] bytes) =>
        bytes.Length >= 4
        && bytes[0] == 0xff
        && bytes[1] == 0xd8
        && bytes[2] == 0xff
        && bytes[^2] == 0xff
        && bytes[^1] == 0xd9;

    private static DirectProtocolException ProtocolFailure(
        string code,
        string message,
        Exception? innerException = null) =>
        new(
            code,
            message,
            retryable: false,
            innerException);
}
