using System.IO.Pipes;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace BwReader.ComputerVoiceAudio;

internal static class ReaderRealtimeOutputRpcProtocol
{
    internal const string RequestType = "output-request";
    internal const string ResponseType = "output-response";

    internal static JsonObject Request(ReaderRealtimeOutputRequest request) =>
        new()
        {
            ["contract"] = ReaderRealtimeOutputProtocol.OutputContract,
            ["type"] = RequestType,
            ["correlation"] = request.Correlation,
            ["sourceInstanceId"] = request.SourceInstanceId,
            ["snapshotRevision"] = request.SnapshotRevision,
            ["file"] = request.File,
            ["page"] = request.Page.DeepClone(),
            ["kind"] = request.Kind,
            ["payload"] = request.Payload.DeepClone(),
        };

    internal static ReaderRealtimeOutputRequest ValidateRequest(
        JsonElement root)
    {
        RequireExact(
            root,
            "contract",
            "type",
            "correlation",
            "sourceInstanceId",
            "snapshotRevision",
            "file",
            "page",
            "kind",
            "payload");
        if (
            RequiredString(root, "contract", 128)
                != ReaderRealtimeOutputProtocol.OutputContract
            || RequiredString(root, "type", 64) != RequestType
            || !root.GetProperty("snapshotRevision")
                .TryGetInt64(out long revision)
        )
        {
            throw Invalid("Reader 输出 RPC 合同无效");
        }
        JsonNode page = JsonNode.Parse(
            root.GetProperty("page").GetRawText())
            ?? throw Invalid("Reader 输出 RPC page 无效");
        JsonNode payload = JsonNode.Parse(
            root.GetProperty("payload").GetRawText())
            ?? throw Invalid("Reader 输出 RPC payload 无效");
        return ReaderRealtimeOutputProtocol.Create(
            RequiredString(root, "correlation", 160),
            RequiredString(root, "sourceInstanceId", 160),
            revision,
            RequiredString(root, "file", 4096),
            page,
            RequiredString(root, "kind", 32),
            payload);
    }

    internal static JsonObject Success(
        ReaderRealtimeOutputRequest request,
        ReaderRealtimeOutputAck ack) => new()
    {
        ["contract"] = ReaderRealtimeOutputProtocol.OutputContract,
        ["type"] = ResponseType,
        ["ok"] = true,
        ["correlation"] = request.Correlation,
        ["sourceInstanceId"] = request.SourceInstanceId,
        ["snapshotRevision"] = request.SnapshotRevision,
        ["file"] = request.File,
        ["page"] = request.Page.DeepClone(),
        ["kind"] = request.Kind,
        ["outcome"] = ack.Outcome,
        ["code"] = null,
        ["message"] = null,
        ["retryable"] = false,
    };

    internal static JsonObject Failure(
        ReaderRealtimeOutputRequest? request,
        ReaderRealtimeOutputException exception) => new()
    {
        ["contract"] = ReaderRealtimeOutputProtocol.OutputContract,
        ["type"] = ResponseType,
        ["ok"] = false,
        ["correlation"] = request?.Correlation,
        ["sourceInstanceId"] = request?.SourceInstanceId,
        ["snapshotRevision"] = request?.SnapshotRevision,
        ["file"] = request?.File,
        ["page"] = request?.Page.DeepClone(),
        ["kind"] = request?.Kind,
        ["outcome"] = null,
        ["code"] = exception.Code,
        ["message"] = exception.Message,
        ["retryable"] = exception.Retryable,
    };

    internal static ReaderRealtimeOutputAck ValidateResponse(
        JsonElement root,
        ReaderRealtimeOutputRequest expected)
    {
        RequireExact(
            root,
            "contract",
            "type",
            "ok",
            "correlation",
            "sourceInstanceId",
            "snapshotRevision",
            "file",
            "page",
            "kind",
            "outcome",
            "code",
            "message",
            "retryable");
        if (
            RequiredString(root, "contract", 128)
                != ReaderRealtimeOutputProtocol.OutputContract
            || RequiredString(root, "type", 64) != ResponseType
            || root.GetProperty("ok").ValueKind
                is not (JsonValueKind.True or JsonValueKind.False)
        )
        {
            throw Invalid("Reader 输出 RPC 回执无效");
        }
        bool ok = root.GetProperty("ok").GetBoolean();
        if (!ok)
        {
            string code = RequiredString(root, "code", 160);
            string message = RequiredString(root, "message", 1024);
            bool retryable = root.GetProperty("retryable").ValueKind
                is JsonValueKind.True or JsonValueKind.False
                && root.GetProperty("retryable").GetBoolean();
            throw new ReaderRealtimeOutputException(
                code,
                message,
                retryable);
        }
        RequireEcho(root, expected);
        string outcome = RequiredString(root, "outcome", 16);
        if (outcome is not ("applied" or "replay"))
        {
            throw Invalid("Reader 输出 RPC outcome 无效");
        }
        if (
            root.GetProperty("code").ValueKind != JsonValueKind.Null
            || root.GetProperty("message").ValueKind != JsonValueKind.Null
            || root.GetProperty("retryable").ValueKind
                != JsonValueKind.False
        )
        {
            throw Invalid("Reader 输出 RPC 成功回执字段无效");
        }
        return new ReaderRealtimeOutputAck(
            string.Empty,
            expected.Correlation,
            expected.SourceInstanceId,
            outcome,
            null);
    }

    private static void RequireEcho(
        JsonElement root,
        ReaderRealtimeOutputRequest expected)
    {
        if (
            RequiredString(root, "correlation", 160)
                != expected.Correlation
            || RequiredString(root, "sourceInstanceId", 160)
                != expected.SourceInstanceId
            || !root.GetProperty("snapshotRevision")
                .TryGetInt64(out long revision)
            || revision != expected.SnapshotRevision
            || RequiredString(root, "file", 4096) != expected.File
            || RequiredString(root, "kind", 32) != expected.Kind
            || !ReaderVisualDeliveryProtocol.PageEquivalent(
                expected.Page,
                root.GetProperty("page"))
        )
        {
            throw Invalid("Reader 输出 RPC 回执身份不匹配");
        }
    }

    private static void RequireExact(
        JsonElement root,
        params string[] fields)
    {
        if (root.ValueKind != JsonValueKind.Object)
        {
            throw Invalid("Reader 输出 RPC 必须是对象");
        }
        DirectJsonValidation.RequireNoDuplicateKeys(root);
        HashSet<string> actual = root.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        if (!actual.SetEquals(fields))
        {
            throw Invalid("Reader 输出 RPC 字段不匹配");
        }
    }

    private static string RequiredString(
        JsonElement root,
        string name,
        int maximum)
    {
        JsonElement value = root.GetProperty(name);
        if (
            value.ValueKind != JsonValueKind.String
            || value.GetString() is not string text
            || string.IsNullOrWhiteSpace(text)
            || text.Length > maximum
            || text.Any(character => character == '\0')
        )
        {
            throw Invalid($"Reader 输出 RPC {name} 无效");
        }
        return text;
    }

    private static ReaderRealtimeOutputException Invalid(string message) =>
        new(
            "BW_READER_REALTIME_OUTPUT_RPC_INVALID",
            message,
            retryable: false);
}

internal sealed class NamedPipeReaderRealtimeOutputRpcClient
{
    internal const string PipeName = "bw-reader-realtime-output-rpc-v1";
    internal static readonly PipeOptions RequiredPipeOptions =
        PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly;
    private static readonly TimeSpan ConnectTimeout = TimeSpan.FromSeconds(3);
    private static readonly TimeSpan ExchangeTimeout = TimeSpan.FromSeconds(14);
    private readonly SemaphoreSlim _exchangeGate = new(1, 1);

    internal async Task<ReaderRealtimeOutputAck> SendAsync(
        ReaderRealtimeOutputRequest request,
        CancellationToken cancellationToken)
    {
        await _exchangeGate.WaitAsync(cancellationToken).ConfigureAwait(false);
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
                await pipe.ConnectAsync(connect.Token).ConfigureAwait(false);
            }
            catch (OperationCanceledException exception)
                when (!cancellationToken.IsCancellationRequested)
            {
                throw Failure(
                    "BW_READER_REALTIME_OUTPUT_RPC_UNAVAILABLE",
                    "Windows Reader 输出服务未连接",
                    exception);
            }
            byte[] requestBytes = Encoding.UTF8.GetBytes(
                ReaderRealtimeOutputRpcProtocol.Request(request)
                    .ToJsonString(DirectBridgeContract.JsonOptions));
            using CancellationTokenSource exchange =
                CancellationTokenSource.CreateLinkedTokenSource(
                    cancellationToken);
            exchange.CancelAfter(ExchangeTimeout);
            try
            {
                await ReaderVisualRpcFraming.WriteAsync(
                    pipe,
                    requestBytes,
                    exchange.Token).ConfigureAwait(false);
                byte[] responseBytes = await ReaderVisualRpcFraming.ReadAsync(
                    pipe,
                    exchange.Token).ConfigureAwait(false);
                try
                {
                    using JsonDocument document = JsonDocument.Parse(
                        responseBytes);
                    return ReaderRealtimeOutputRpcProtocol.ValidateResponse(
                        document.RootElement,
                        request);
                }
                finally
                {
                    Array.Clear(responseBytes);
                }
            }
            catch (OperationCanceledException exception)
                when (!cancellationToken.IsCancellationRequested)
            {
                throw Failure(
                    "BW_READER_REALTIME_OUTPUT_RPC_TIMEOUT",
                    "Windows Reader 输出 RPC 超时",
                    exception);
            }
            finally
            {
                Array.Clear(requestBytes);
            }
        }
        catch (ReaderRealtimeOutputException)
        {
            throw;
        }
        catch (Exception exception) when (
            exception is IOException
            or UnauthorizedAccessException
            or JsonException
            or ReaderVisualDeliveryException)
        {
            throw Failure(
                "BW_READER_REALTIME_OUTPUT_RPC_FAILED",
                "Windows Reader 输出 RPC 失败",
                exception);
        }
        finally
        {
            _exchangeGate.Release();
        }
    }

    private static ReaderRealtimeOutputException Failure(
        string code,
        string message,
        Exception exception) =>
        new(code, message, retryable: true, exception);
}

internal sealed class NamedPipeReaderRealtimeOutputRpcServer
{
    internal static readonly PipeOptions RequiredPipeOptions =
        PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly;
    private readonly ReaderRealtimeOutputBroker _broker;

    internal NamedPipeReaderRealtimeOutputRpcServer(
        ReaderRealtimeOutputBroker broker)
    {
        _broker = broker;
    }

    internal async Task RunAsync(CancellationToken cancellationToken)
    {
        while (!cancellationToken.IsCancellationRequested)
        {
            await using NamedPipeServerStream pipe = new(
                NamedPipeReaderRealtimeOutputRpcClient.PipeName,
                PipeDirection.InOut,
                1,
                PipeTransmissionMode.Byte,
                RequiredPipeOptions);
            try
            {
                await pipe.WaitForConnectionAsync(cancellationToken)
                    .ConfigureAwait(false);
                await HandleAsync(pipe, cancellationToken)
                    .ConfigureAwait(false);
            }
            catch (OperationCanceledException)
                when (cancellationToken.IsCancellationRequested)
            {
                return;
            }
            catch (Exception exception) when (
                exception is IOException
                or UnauthorizedAccessException
                or ObjectDisposedException
                or JsonException
                or ReaderVisualDeliveryException
                or ReaderRealtimeOutputException)
            {
            }
        }
    }

    private async Task HandleAsync(
        Stream pipe,
        CancellationToken cancellationToken)
    {
        ReaderRealtimeOutputRequest? request = null;
        JsonObject response;
        try
        {
            byte[] bytes = await ReaderVisualRpcFraming.ReadAsync(
                pipe,
                cancellationToken).ConfigureAwait(false);
            try
            {
                using JsonDocument document = JsonDocument.Parse(bytes);
                request = ReaderRealtimeOutputRpcProtocol.ValidateRequest(
                    document.RootElement);
            }
            finally
            {
                Array.Clear(bytes);
            }
            ReaderRealtimeOutputAck result = await _broker.SendAsync(
                request,
                cancellationToken).ConfigureAwait(false);
            response = ReaderRealtimeOutputRpcProtocol.Success(request, result);
        }
        catch (ReaderRealtimeOutputException exception)
        {
            response = ReaderRealtimeOutputRpcProtocol.Failure(
                request,
                exception);
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
