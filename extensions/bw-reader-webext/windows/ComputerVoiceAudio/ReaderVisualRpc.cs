using System.Buffers.Binary;
using System.IO.Pipes;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace BwReader.ComputerVoiceAudio;

internal static class ReaderVisualRpcProtocol
{
    internal const string Contract = "reader-visual-rpc/1";
    internal const int MaximumPayloadBytes = 1_100_000;

    internal static JsonObject Request(
        ReaderVisualDeliveryRequest request) => new()
    {
        ["contract"] = Contract,
        ["type"] = "request",
        ["request"] = RequestValue(request),
    };

    internal static JsonObject RequestValue(
        ReaderVisualDeliveryRequest request) => new()
    {
        ["correlation"] = request.Correlation,
        ["sourceInstanceId"] = request.SourceInstanceId,
        ["snapshotRevision"] = request.SnapshotRevision,
        ["file"] = request.File,
        ["page"] = request.Page.DeepClone(),
        ["drawingRevision"] = request.DrawingRevision,
        ["scope"] = request.Scope,
        ["selectionId"] = request.SelectionId,
    };

    internal static ReaderVisualDeliveryRequest ValidateRequest(
        JsonElement root)
    {
        RequireExact(root, "contract", "type", "request");
        if (
            String(root, "contract", 128) != Contract
            || String(root, "type", 32) != "request"
            || root.GetProperty("request").ValueKind
                != JsonValueKind.Object
        )
        {
            throw Invalid("Reader 视觉 RPC 请求合同无效");
        }
        JsonElement value = root.GetProperty("request");
        RequireExact(
            value,
            "correlation",
            "sourceInstanceId",
            "snapshotRevision",
            "file",
            "page",
            "drawingRevision",
            "scope",
            "selectionId");
        string correlation = SafeId(value, "correlation");
        string sourceInstanceId = SafeId(value, "sourceInstanceId");
        long snapshotRevision = Int64(value, "snapshotRevision");
        string file = String(value, "file", 4096);
        JsonElement page = value.GetProperty("page");
        if (
            snapshotRevision < 0
            || file.Any(char.IsControl)
            || !ValidPage(page)
        )
        {
            throw Invalid("Reader 视觉 RPC 请求身份无效");
        }
        string? drawingRevision = OptionalSafeId(
            value,
            "drawingRevision");
        string scope = String(value, "scope", 32);
        string? selectionId = OptionalSafeId(value, "selectionId");
        if (
            !ReaderVisualDeliveryProtocol.IsScope(scope)
            || (selectionId is not null && scope != "selection-near")
        )
        {
            throw Invalid("Reader 视觉 RPC 请求范围无效");
        }
        return new ReaderVisualDeliveryRequest(
            correlation,
            sourceInstanceId,
            snapshotRevision,
            file,
            JsonNode.Parse(page.GetRawText())
                ?? throw Invalid("Reader 视觉 RPC page 无效"),
            drawingRevision,
            scope,
            selectionId);
    }

    internal static JsonObject Success(
        ReaderVisualDeliveryRequest request,
        ReaderVisualCapture? capture) => new()
    {
        ["contract"] = Contract,
        ["type"] = "response",
        ["ok"] = true,
        ["request"] = RequestValue(request),
        ["capture"] = capture is null
            ? null
            : new JsonObject
            {
                ["mimeType"] = capture.MimeType,
                ["data"] = Convert.ToBase64String(capture.Data),
            },
    };

    internal static JsonObject Failure(
        ReaderVisualDeliveryRequest? request,
        string code,
        string message,
        bool retryable) => new()
    {
        ["contract"] = Contract,
        ["type"] = "response",
        ["ok"] = false,
        ["request"] = request is null
            ? null
            : RequestValue(request),
        ["error"] = new JsonObject
        {
            ["code"] = code,
            ["message"] = message,
            ["retryable"] = retryable,
        },
    };

    internal static ReaderVisualCapture? ValidateResponse(
        JsonElement root,
        ReaderVisualDeliveryRequest expected)
    {
        if (root.ValueKind != JsonValueKind.Object)
        {
            throw Invalid("Reader 视觉 RPC 响应无效");
        }
        DirectJsonValidation.RequireNoDuplicateKeys(root);
        if (
            !root.TryGetProperty("contract", out JsonElement contract)
            || contract.GetString() != Contract
            || !root.TryGetProperty("type", out JsonElement type)
            || type.GetString() != "response"
            || !root.TryGetProperty("ok", out JsonElement ok)
            || ok.ValueKind is not (
                JsonValueKind.True or JsonValueKind.False)
        )
        {
            throw Invalid("Reader 视觉 RPC 响应合同无效");
        }
        HashSet<string> fields = root.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        if (ok.GetBoolean())
        {
            if (!fields.SetEquals(
                new[] { "contract", "type", "ok", "request", "capture" }))
            {
                throw Invalid("Reader 视觉 RPC 成功响应字段无效");
            }
            ReaderVisualDeliveryRequest echoed = ValidateRequest(
                WrapRequest(root.GetProperty("request")));
            RequireSameRequest(expected, echoed);
            JsonElement capture = root.GetProperty("capture");
            if (capture.ValueKind == JsonValueKind.Null)
            {
                return null;
            }
            RequireExact(capture, "mimeType", "data");
            string mimeType = String(capture, "mimeType", 32);
            string data = String(
                capture,
                "data",
                MaximumPayloadBytes);
            if (mimeType != ReaderVisualDeliveryProtocol.MimeType)
            {
                throw Invalid("Reader 视觉 RPC MIME 无效");
            }
            byte[] bytes;
            try
            {
                bytes = Convert.FromBase64String(data);
            }
            catch (FormatException exception)
            {
                throw Invalid("Reader 视觉 RPC base64 无效", exception);
            }
            if (
                bytes.Length is < 1
                    or > ReaderVisualDeliveryProtocol.MaximumImageBytes
                || !ReaderVisualDeliveryProtocol.IsJpeg(bytes)
            )
            {
                Array.Clear(bytes);
                throw Invalid("Reader 视觉 RPC JPEG 无效");
            }
            return new ReaderVisualCapture(mimeType, bytes);
        }
        if (!fields.SetEquals(
            new[] { "contract", "type", "ok", "request", "error" }))
        {
            throw Invalid("Reader 视觉 RPC 失败响应字段无效");
        }
        JsonElement request = root.GetProperty("request");
        if (request.ValueKind == JsonValueKind.Object)
        {
            ReaderVisualDeliveryRequest echoed = ValidateRequest(
                WrapRequest(request));
            RequireSameRequest(expected, echoed);
        }
        else if (request.ValueKind != JsonValueKind.Null)
        {
            throw Invalid("Reader 视觉 RPC 失败请求身份无效");
        }
        JsonElement error = root.GetProperty("error");
        RequireExact(error, "code", "message", "retryable");
        string code = SafeId(error, "code");
        string message = String(error, "message", 500);
        JsonElement retryable = error.GetProperty("retryable");
        if (retryable.ValueKind is not (
            JsonValueKind.True or JsonValueKind.False))
        {
            throw Invalid("Reader 视觉 RPC retryable 无效");
        }
        throw new ReaderVisualDeliveryException(
            code,
            message,
            retryable.GetBoolean());
    }

    private static JsonElement WrapRequest(JsonElement value)
    {
        return JsonSerializer.SerializeToElement(new
        {
            contract = Contract,
            type = "request",
            request = value,
        });
    }

    private static void RequireSameRequest(
        ReaderVisualDeliveryRequest expected,
        ReaderVisualDeliveryRequest actual)
    {
        if (
            expected.Correlation != actual.Correlation
            || expected.SourceInstanceId != actual.SourceInstanceId
            || expected.SnapshotRevision != actual.SnapshotRevision
            || expected.File != actual.File
            || !JsonNode.DeepEquals(expected.Page, actual.Page)
            || expected.DrawingRevision != actual.DrawingRevision
            || expected.Scope != actual.Scope
            || expected.SelectionId != actual.SelectionId
        )
        {
            throw Invalid("Reader 视觉 RPC 响应身份不匹配");
        }
    }

    private static bool ValidPage(JsonElement page) =>
        page.ValueKind == JsonValueKind.Number
            && page.TryGetInt64(out long number)
            && number >= 0
        || page.ValueKind == JsonValueKind.String
            && page.GetString() is string text
            && text.Length is >= 1 and <= 256
            && !text.Any(char.IsControl);

    private static string? OptionalSafeId(
        JsonElement value,
        string name)
    {
        JsonElement field = value.GetProperty(name);
        if (field.ValueKind == JsonValueKind.Null)
        {
            return null;
        }
        return SafeId(value, name);
    }

    private static string SafeId(JsonElement value, string name)
    {
        string result = String(value, name, 160);
        if (!DirectBridgeContract.IsSafeId(result))
        {
            throw Invalid($"Reader 视觉 RPC {name} 无效");
        }
        return result;
    }

    private static string String(
        JsonElement value,
        string name,
        int maximumLength)
    {
        if (
            !value.TryGetProperty(name, out JsonElement field)
            || field.ValueKind != JsonValueKind.String
            || field.GetString() is not string result
            || result.Length is < 1
            || result.Length > maximumLength
        )
        {
            throw Invalid($"Reader 视觉 RPC {name} 无效");
        }
        return result;
    }

    private static long Int64(JsonElement value, string name)
    {
        if (
            !value.TryGetProperty(name, out JsonElement field)
            || field.ValueKind != JsonValueKind.Number
            || !field.TryGetInt64(out long result)
        )
        {
            throw Invalid($"Reader 视觉 RPC {name} 无效");
        }
        return result;
    }

    private static void RequireExact(
        JsonElement value,
        params string[] expected)
    {
        if (value.ValueKind != JsonValueKind.Object)
        {
            throw Invalid("Reader 视觉 RPC 对象无效");
        }
        DirectJsonValidation.RequireNoDuplicateKeys(value);
        HashSet<string> actual = value.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        if (!actual.SetEquals(expected))
        {
            throw Invalid("Reader 视觉 RPC 字段不匹配");
        }
    }

    private static ReaderVisualDeliveryException Invalid(
        string message,
        Exception? inner = null) =>
        new(
            "BW_READER_VISUAL_RPC_INVALID",
            message,
            retryable: false,
            inner);
}

internal static class ReaderVisualRpcFraming
{
    private static readonly UTF8Encoding StrictUtf8 = new(
        encoderShouldEmitUTF8Identifier: false,
        throwOnInvalidBytes: true);

    internal static async Task WriteAsync(
        Stream stream,
        ReadOnlyMemory<byte> payload,
        CancellationToken cancellationToken)
    {
        if (
            payload.Length is < 1
                or > ReaderVisualRpcProtocol.MaximumPayloadBytes
            || !IsStrictUtf8(payload.Span)
        )
        {
            throw FrameInvalid();
        }
        byte[] header = new byte[sizeof(int)];
        BinaryPrimitives.WriteInt32LittleEndian(header, payload.Length);
        await stream.WriteAsync(header, cancellationToken)
            .ConfigureAwait(false);
        await stream.WriteAsync(payload, cancellationToken)
            .ConfigureAwait(false);
        await stream.FlushAsync(cancellationToken).ConfigureAwait(false);
    }

    internal static async Task<byte[]> ReadAsync(
        Stream stream,
        CancellationToken cancellationToken)
    {
        byte[] header = new byte[sizeof(int)];
        await ReadExactlyAsync(stream, header, cancellationToken)
            .ConfigureAwait(false);
        int length = BinaryPrimitives.ReadInt32LittleEndian(header);
        if (length is < 1 or > ReaderVisualRpcProtocol.MaximumPayloadBytes)
        {
            throw FrameInvalid();
        }
        byte[] payload = new byte[length];
        await ReadExactlyAsync(stream, payload, cancellationToken)
            .ConfigureAwait(false);
        if (!IsStrictUtf8(payload))
        {
            Array.Clear(payload);
            throw FrameInvalid();
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
                throw FrameInvalid();
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

    private static ReaderVisualDeliveryException FrameInvalid() =>
        new(
            "BW_READER_VISUAL_RPC_FRAME_INVALID",
            "Reader 视觉 RPC 帧无效",
            retryable: true);
}

internal sealed class NamedPipeReaderVisualRpcClient
{
    internal const string PipeName = "bw-reader-visual-rpc-v1";
    internal const string PipePath =
        @"\\.\pipe\bw-reader-visual-rpc-v1";
    internal static readonly PipeOptions RequiredPipeOptions =
        PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly;
    private static readonly TimeSpan ConnectTimeout =
        TimeSpan.FromSeconds(3);
    private static readonly TimeSpan ExchangeTimeout =
        TimeSpan.FromSeconds(18);
    private readonly SemaphoreSlim _exchangeGate = new(1, 1);

    internal async Task<ReaderVisualCapture?> RequestAsync(
        ReaderVisualDeliveryRequest request,
        CancellationToken cancellationToken)
    {
        await _exchangeGate.WaitAsync(cancellationToken)
            .ConfigureAwait(false);
        try
        {
            await using NamedPipeClientStream pipe = new(
                ".",
                PipeName,
                PipeDirection.InOut,
                RequiredPipeOptions);
            using CancellationTokenSource connect =
                CancellationTokenSource.CreateLinkedTokenSource(
                    cancellationToken);
            connect.CancelAfter(ConnectTimeout);
            try
            {
                await pipe.ConnectAsync(connect.Token)
                    .ConfigureAwait(false);
            }
            catch (OperationCanceledException exception)
                when (!cancellationToken.IsCancellationRequested)
            {
                throw new ReaderVisualDeliveryException(
                    "BW_READER_VISUAL_RPC_UNAVAILABLE",
                    "Windows Reader 视觉服务未连接",
                    retryable: true,
                    exception);
            }
            byte[] payload = Encoding.UTF8.GetBytes(
                ReaderVisualRpcProtocol.Request(request).ToJsonString(
                    DirectBridgeContract.JsonOptions));
            using CancellationTokenSource exchange =
                CancellationTokenSource.CreateLinkedTokenSource(
                    cancellationToken);
            exchange.CancelAfter(ExchangeTimeout);
            try
            {
                await ReaderVisualRpcFraming.WriteAsync(
                    pipe,
                    payload,
                    exchange.Token).ConfigureAwait(false);
                byte[] response = await ReaderVisualRpcFraming.ReadAsync(
                    pipe,
                    exchange.Token).ConfigureAwait(false);
                try
                {
                    using JsonDocument document = JsonDocument.Parse(
                        response,
                        new JsonDocumentOptions
                        {
                            AllowTrailingCommas = false,
                            CommentHandling =
                                JsonCommentHandling.Disallow,
                            MaxDepth = 16,
                        });
                    return ReaderVisualRpcProtocol.ValidateResponse(
                        document.RootElement,
                        request);
                }
                finally
                {
                    Array.Clear(response);
                }
            }
            catch (OperationCanceledException exception)
                when (!cancellationToken.IsCancellationRequested)
            {
                throw new ReaderVisualDeliveryException(
                    "BW_READER_VISUAL_RPC_TIMEOUT",
                    "Windows Reader 视觉服务超时",
                    retryable: true,
                    exception);
            }
            finally
            {
                Array.Clear(payload);
            }
        }
        catch (ReaderVisualDeliveryException)
        {
            throw;
        }
        catch (
            Exception exception
        ) when (
            exception is IOException
            or UnauthorizedAccessException
            or ObjectDisposedException
            or JsonException
        )
        {
            throw new ReaderVisualDeliveryException(
                "BW_READER_VISUAL_RPC_FAILED",
                "Windows Reader 视觉 RPC 失败",
                retryable: true,
                exception);
        }
        finally
        {
            _exchangeGate.Release();
        }
    }
}

internal sealed class NamedPipeReaderVisualRpcServer
{
    internal static readonly PipeOptions RequiredPipeOptions =
        PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly;
    private readonly ReaderVisualDeliveryBroker _broker;

    internal NamedPipeReaderVisualRpcServer(
        ReaderVisualDeliveryBroker broker)
    {
        _broker = broker;
    }

    internal async Task RunAsync(CancellationToken cancellationToken)
    {
        while (!cancellationToken.IsCancellationRequested)
        {
            await using NamedPipeServerStream pipe = new(
                NamedPipeReaderVisualRpcClient.PipeName,
                PipeDirection.InOut,
                maxNumberOfServerInstances: 1,
                PipeTransmissionMode.Byte,
                RequiredPipeOptions,
                64 * 1024,
                64 * 1024);
            try
            {
                await pipe.WaitForConnectionAsync(cancellationToken)
                    .ConfigureAwait(false);
                await HandleClientAsync(pipe, cancellationToken)
                    .ConfigureAwait(false);
            }
            catch (OperationCanceledException)
                when (cancellationToken.IsCancellationRequested)
            {
                return;
            }
            catch (
                Exception exception
            ) when (
                exception is IOException
                or UnauthorizedAccessException
                or ObjectDisposedException
                or ReaderVisualDeliveryException
                or JsonException
            )
            {
                // A malformed or disconnected client must not stop the
                // dedicated service pipe.  Each exchange has one request.
            }
        }
    }

    private async Task HandleClientAsync(
        NamedPipeServerStream pipe,
        CancellationToken cancellationToken)
    {
        ReaderVisualDeliveryRequest? request = null;
        JsonObject response;
        try
        {
            byte[] payload = await ReaderVisualRpcFraming.ReadAsync(
                pipe,
                cancellationToken).ConfigureAwait(false);
            try
            {
                using JsonDocument document = JsonDocument.Parse(
                    payload,
                    new JsonDocumentOptions
                    {
                        AllowTrailingCommas = false,
                        CommentHandling = JsonCommentHandling.Disallow,
                        MaxDepth = 16,
                    });
                request = ReaderVisualRpcProtocol.ValidateRequest(
                    document.RootElement);
            }
            finally
            {
                Array.Clear(payload);
            }
            ReaderVisualCapture? capture = await _broker.RequestAsync(
                request,
                cancellationToken).ConfigureAwait(false);
            response = ReaderVisualRpcProtocol.Success(request, capture);
        }
        catch (ReaderVisualDeliveryException exception)
        {
            response = ReaderVisualRpcProtocol.Failure(
                request,
                exception.Code,
                exception.Message,
                exception.Retryable);
        }
        byte[] encoded = Encoding.UTF8.GetBytes(
            response.ToJsonString(DirectBridgeContract.JsonOptions));
        try
        {
            await ReaderVisualRpcFraming.WriteAsync(
                pipe,
                encoded,
                cancellationToken).ConfigureAwait(false);
        }
        finally
        {
            Array.Clear(encoded);
        }
    }
}
