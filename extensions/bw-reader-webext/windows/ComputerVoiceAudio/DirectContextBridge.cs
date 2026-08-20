using System.Buffers.Binary;
using System.IO.Pipes;
using System.Text;
using System.Text.Json;

namespace BwReader.ComputerVoiceAudio;

internal sealed record DirectContextEvent(
    long Sequence,
    string Type,
    string EventId,
    JsonElement Payload);

internal sealed record DirectContextForwardResult(string Outcome);

internal interface IDirectContextAdapter
{
    Task<DirectContextForwardResult> ForwardAsync(
        string requestId,
        string sessionId,
        string contextContract,
        DirectContextEvent contextEvent,
        CancellationToken cancellationToken);
}

internal sealed class UnwiredDirectContextAdapter :
    IDirectContextAdapter
{
    public Task<DirectContextForwardResult> ForwardAsync(
        string requestId,
        string sessionId,
        string contextContract,
        DirectContextEvent contextEvent,
        CancellationToken cancellationToken) =>
        Task.FromException<DirectContextForwardResult>(
            new DirectProtocolException(
                "BW_COMPUTER_VOICE_CONTEXT_IPC_UNAVAILABLE",
                "Voice Typist 上下文管道尚未连接",
                retryable: true));
}

internal interface IDirectContextIpcTransport
{
    Task<byte[]> ExchangeAsync(
        ReadOnlyMemory<byte> request,
        CancellationToken cancellationToken);
}

internal sealed class NamedPipeDirectContextTransport :
    IDirectContextIpcTransport
{
    internal const string PipeName = "bw-reader-voice-typist-v1";
    internal const string PipePath =
        @"\\.\pipe\bw-reader-voice-typist-v1";
    internal const int MaximumPayloadBytes = 65_536;
    internal const int MaximumServerInstances = 1;
    internal static readonly TimeSpan ExchangeTimeout =
        TimeSpan.FromSeconds(3);
    internal static readonly PipeOptions RequiredPipeOptions =
        PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly;

    private readonly SemaphoreSlim _exchangeGate = new(1, 1);

    public async Task<byte[]> ExchangeAsync(
        ReadOnlyMemory<byte> request,
        CancellationToken cancellationToken)
    {
        if (request.Length is < 1 or > MaximumPayloadBytes)
        {
            throw IpcFailure(
                "BW_COMPUTER_VOICE_CONTEXT_IPC_FRAME_INVALID",
                "Voice Typist IPC 请求大小无效");
        }
        await _exchangeGate.WaitAsync(cancellationToken)
            .ConfigureAwait(false);
        try
        {
            using CancellationTokenSource timeout =
                CancellationTokenSource.CreateLinkedTokenSource(
                    cancellationToken);
            timeout.CancelAfter(ExchangeTimeout);
            try
            {
                await using NamedPipeServerStream pipe = new(
                    PipeName,
                    PipeDirection.InOut,
                    MaximumServerInstances,
                    PipeTransmissionMode.Byte,
                    RequiredPipeOptions,
                    MaximumPayloadBytes + sizeof(int),
                    MaximumPayloadBytes + sizeof(int));
                await pipe.WaitForConnectionAsync(timeout.Token)
                    .ConfigureAwait(false);
                await DirectContextIpcFraming.WriteAsync(
                    pipe,
                    request,
                    timeout.Token).ConfigureAwait(false);
                return await DirectContextIpcFraming.ReadAsync(
                    pipe,
                    timeout.Token).ConfigureAwait(false);
            }
            catch (OperationCanceledException exception)
                when (
                    !cancellationToken.IsCancellationRequested
                    && timeout.IsCancellationRequested
                )
            {
                throw IpcFailure(
                    "BW_COMPUTER_VOICE_CONTEXT_IPC_TIMEOUT",
                    "Voice Typist IPC 在 3 秒内未确认",
                    exception);
            }
            catch (DirectProtocolException)
            {
                throw;
            }
            catch (
                Exception exception
            ) when (
                exception is IOException
                or UnauthorizedAccessException
                or ObjectDisposedException
            )
            {
                throw IpcFailure(
                    "BW_COMPUTER_VOICE_CONTEXT_IPC_FAILED",
                    "Voice Typist IPC 失败",
                    exception);
            }
        }
        finally
        {
            _exchangeGate.Release();
        }
    }

    private static DirectProtocolException IpcFailure(
        string code,
        string message,
        Exception? inner = null) =>
        new(
            code,
            message,
            retryable: true,
            innerException: inner);
}

internal static class DirectContextIpcFraming
{
    private static readonly UTF8Encoding StrictUtf8 = new(
        encoderShouldEmitUTF8Identifier: false,
        throwOnInvalidBytes: true);

    internal static async Task WriteAsync(
        Stream stream,
        ReadOnlyMemory<byte> payload,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(stream);
        if (
            payload.Length is < 1
                or > NamedPipeDirectContextTransport.MaximumPayloadBytes
            || !IsStrictUtf8(payload.Span)
        )
        {
            throw InvalidFrame();
        }
        byte[] header = new byte[sizeof(int)];
        BinaryPrimitives.WriteInt32LittleEndian(
            header,
            payload.Length);
        await stream.WriteAsync(header, cancellationToken)
            .ConfigureAwait(false);
        await stream.WriteAsync(payload, cancellationToken)
            .ConfigureAwait(false);
        await stream.FlushAsync(cancellationToken)
            .ConfigureAwait(false);
    }

    internal static async Task<byte[]> ReadAsync(
        Stream stream,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(stream);
        byte[] header = new byte[sizeof(int)];
        await ReadExactlyAsync(
            stream,
            header,
            cancellationToken).ConfigureAwait(false);
        int length = BinaryPrimitives.ReadInt32LittleEndian(header);
        if (
            length is < 1
                or > NamedPipeDirectContextTransport.MaximumPayloadBytes
        )
        {
            throw InvalidFrame();
        }
        byte[] payload = new byte[length];
        await ReadExactlyAsync(
            stream,
            payload,
            cancellationToken).ConfigureAwait(false);
        if (!IsStrictUtf8(payload))
        {
            Array.Clear(payload);
            throw InvalidFrame();
        }
        return payload;
    }

    private static async Task ReadExactlyAsync(
        Stream stream,
        Memory<byte> destination,
        CancellationToken cancellationToken)
    {
        int offset = 0;
        while (offset < destination.Length)
        {
            int read = await stream.ReadAsync(
                destination[offset..],
                cancellationToken).ConfigureAwait(false);
            if (read == 0)
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_CONTEXT_IPC_FRAME_INVALID",
                    "Voice Typist IPC 帧被截断",
                    retryable: true);
            }
            offset += read;
        }
    }

    private static bool IsStrictUtf8(ReadOnlySpan<byte> payload)
    {
        try
        {
            _ = StrictUtf8.GetCharCount(payload);
            return true;
        }
        catch (DecoderFallbackException)
        {
            return false;
        }
    }

    private static DirectProtocolException InvalidFrame() =>
        new(
            "BW_COMPUTER_VOICE_CONTEXT_IPC_FRAME_INVALID",
            "Voice Typist IPC 帧无效",
            retryable: true);
}

internal sealed class NamedPipeDirectContextAdapter :
    IDirectContextAdapter
{
    internal const string IpcContract = "reader-voice-typist-ipc/1";
    internal const string ContextContract = "reader-outgoing-context/1";

    private static readonly UTF8Encoding StrictUtf8 = new(
        encoderShouldEmitUTF8Identifier: false,
        throwOnInvalidBytes: true);
    private readonly IDirectContextIpcTransport _transport;

    internal NamedPipeDirectContextAdapter()
        : this(new NamedPipeDirectContextTransport())
    {
    }

    internal NamedPipeDirectContextAdapter(
        IDirectContextIpcTransport transport)
    {
        _transport = transport;
    }

    public async Task<DirectContextForwardResult> ForwardAsync(
        string requestId,
        string sessionId,
        string contextContract,
        DirectContextEvent contextEvent,
        CancellationToken cancellationToken)
    {
        if (
            contextContract != ContextContract
            || !DirectBridgeContract.IsSafeId(requestId)
            || DirectPcmFrameCodec.ParseSessionId(sessionId).Length != 16
        )
        {
            throw ContextSchemaInvalid();
        }
        byte[] request = JsonSerializer.SerializeToUtf8Bytes(new
        {
            contract = IpcContract,
            requestId,
            sessionId,
            action = "context",
            @event = contextEvent.Payload,
        }, DirectBridgeContract.JsonOptions);
        if (
            request.Length is < 1
                or > NamedPipeDirectContextTransport.MaximumPayloadBytes
        )
        {
            throw ContextSchemaInvalid();
        }

        byte[] reply = await _transport.ExchangeAsync(
            request,
            cancellationToken).ConfigureAwait(false);
        try
        {
            using JsonDocument document = JsonDocument.Parse(
                StrictUtf8.GetString(reply),
                new JsonDocumentOptions
                {
                    AllowTrailingCommas = false,
                    CommentHandling = JsonCommentHandling.Disallow,
                    MaxDepth = 8,
                });
            JsonElement root = document.RootElement;
            if (
                root.ValueKind != JsonValueKind.Object
                || !root.TryGetProperty(
                    "contract",
                    out JsonElement replyContract)
                || replyContract.ValueKind != JsonValueKind.String
                || replyContract.GetString() != IpcContract
                || !root.TryGetProperty(
                    "requestId",
                    out JsonElement replyRequestId)
                || replyRequestId.ValueKind != JsonValueKind.String
                || replyRequestId.GetString() != requestId
                || !root.TryGetProperty(
                    "ok",
                    out JsonElement replyOk)
                || replyOk.ValueKind is not (
                    JsonValueKind.True or JsonValueKind.False)
            )
            {
                throw AckInvalid();
            }
            if (!replyOk.GetBoolean())
            {
                RequireExactKeys(
                    root,
                    "contract",
                    "requestId",
                    "ok",
                    "error");
                JsonElement error = root.GetProperty("error");
                RequireExactKeys(
                    error,
                    "code",
                    "message",
                    "retryable");
                if (
                    error.GetProperty("code").ValueKind
                        != JsonValueKind.String
                    || error.GetProperty("code").GetString()
                        is not string errorCode
                    || !DirectBridgeContract.IsSafeId(errorCode)
                    || error.GetProperty("message").ValueKind
                        != JsonValueKind.String
                    || error.GetProperty("message").GetString()
                        is not string errorMessage
                    || errorMessage.Length is < 1 or > 300
                    || error.GetProperty("retryable").ValueKind
                        is not (
                            JsonValueKind.True
                            or JsonValueKind.False)
                )
                {
                    throw AckInvalid();
                }
                throw new DirectProtocolException(
                    errorCode,
                    errorMessage,
                    error.GetProperty("retryable").GetBoolean());
            }
            RequireExactKeys(
                root,
                "contract",
                "requestId",
                "ok",
                "action",
                "payload");
            JsonElement payload = root.GetProperty("payload");
            RequireExactKeys(
                payload,
                "sessionId",
                "eventId",
                "seq",
                "outcome");
            if (
                root.GetProperty("contract").GetString() != IpcContract
                || root.GetProperty("requestId").GetString() != requestId
                || root.GetProperty("ok").ValueKind
                    != JsonValueKind.True
                || root.GetProperty("action").GetString() != "context"
                || payload.GetProperty("sessionId").GetString()
                    != sessionId
                || payload.GetProperty("eventId").GetString()
                    != contextEvent.EventId
                || !payload.GetProperty("seq").TryGetInt64(
                    out long sequence)
                || sequence != contextEvent.Sequence
                || payload.GetProperty("outcome").GetString()
                    is not ("accepted" or "duplicate")
            )
            {
                throw AckInvalid();
            }
            return new DirectContextForwardResult(
                payload.GetProperty("outcome").GetString()!);
        }
        catch (DirectProtocolException)
        {
            throw;
        }
        catch (
            Exception exception
        ) when (
            exception is JsonException
            or DecoderFallbackException
            or InvalidOperationException
        )
        {
            throw AckInvalid(exception);
        }
        finally
        {
            Array.Clear(request);
            Array.Clear(reply);
        }
    }

    internal static DirectContextEvent ValidateEvent(
        JsonElement value)
    {
        if (value.ValueKind != JsonValueKind.Object)
        {
            throw ContextSchemaInvalid();
        }
        try
        {
            DirectJsonValidation.RequireNoDuplicateKeys(value);
        }
        catch (DirectProtocolException)
        {
            throw ContextSchemaInvalid();
        }
        HashSet<string> keys = value.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        if (!new[] { "v", "seq", "type", "ts", "id" }
            .All(keys.Contains))
        {
            throw ContextSchemaInvalid();
        }
        if (
            !value.GetProperty("v").TryGetInt32(out int version)
            || version != 1
            || !value.GetProperty("seq").TryGetInt64(out long sequence)
            || sequence is < 1 or > 9_007_199_254_740_991
            || value.GetProperty("type").ValueKind
                != JsonValueKind.String
            || value.GetProperty("type").GetString() is not string type
            || type is not (
                "page.context"
                or "focus"
                or "drawing"
                or "command"
                or "command-failed")
            || !value.GetProperty("ts").TryGetInt64(out long timestamp)
            || Math.Abs((double)timestamp) > 9_007_199_254_740_991d
            || value.GetProperty("id").ValueKind
                != JsonValueKind.String
            || value.GetProperty("id").GetString() is not string eventId
            || eventId.Length != 16
            || eventId.Any(character =>
                character is not (>= '0' and <= '9')
                and not (>= 'a' and <= 'f'))
            || Encoding.UTF8.GetByteCount(value.GetRawText())
                > DirectBridgeContract.MaximumMessageBytes
        )
        {
            throw ContextSchemaInvalid();
        }
        return new DirectContextEvent(
            sequence,
            type,
            eventId,
            value.Clone());
    }

    private static void RequireExactKeys(
        JsonElement value,
        params string[] expected)
    {
        if (value.ValueKind != JsonValueKind.Object)
        {
            throw AckInvalid();
        }
        try
        {
            DirectJsonValidation.RequireNoDuplicateKeys(value);
        }
        catch (DirectProtocolException exception)
        {
            throw AckInvalid(exception);
        }
        HashSet<string> actual = value.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        if (!actual.SetEquals(expected))
        {
            throw AckInvalid();
        }
    }

    private static DirectProtocolException ContextSchemaInvalid() =>
        new(
            "BW_COMPUTER_VOICE_CONTEXT_SCHEMA_INVALID",
            "Reader outgoing context 事件无效");

    private static DirectProtocolException AckInvalid(
        Exception? inner = null) =>
        new(
            "BW_COMPUTER_VOICE_CONTEXT_ACK_INVALID",
            "Voice Typist IPC 回执无效",
            retryable: true,
            innerException: inner);
}

internal static class DirectJsonValidation
{
    internal static void RequireNoDuplicateKeys(JsonElement value)
    {
        if (value.ValueKind == JsonValueKind.Object)
        {
            HashSet<string> names = new(StringComparer.Ordinal);
            foreach (JsonProperty property in value.EnumerateObject())
            {
                if (!names.Add(property.Name))
                {
                    throw new DirectProtocolException(
                        "BW_COMPUTER_VOICE_DIRECT_MESSAGE_INVALID",
                        "JSON 对象含重复字段");
                }
                RequireNoDuplicateKeys(property.Value);
            }
        }
        else if (value.ValueKind == JsonValueKind.Array)
        {
            foreach (JsonElement item in value.EnumerateArray())
            {
                RequireNoDuplicateKeys(item);
            }
        }
    }
}
