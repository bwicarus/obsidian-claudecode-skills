using System.IO.Pipes;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace BwReader.ComputerVoiceAudio;

internal sealed record ReaderRealtimeOutputSourceStatus(
    string SourceInstanceId,
    bool Online);

internal static class ReaderRealtimeOutputRpcProtocol
{
    internal const string RequestType = "output-request";
    internal const string ResponseType = "output-response";
    internal const string SourceStatusRequestType = "source-status-request";
    internal const string SourceStatusResponseType = "source-status-response";

    internal static JsonObject SourceStatusRequest(string sourceInstanceId)
    {
        if (!DirectBridgeContract.IsSafeId(sourceInstanceId))
        {
            throw Invalid("Reader 输出 RPC sourceInstanceId 无效");
        }
        return new JsonObject
        {
            ["contract"] = ReaderRealtimeOutputProtocol.OutputContract,
            ["type"] = SourceStatusRequestType,
            ["sourceInstanceId"] = sourceInstanceId,
        };
    }

    internal static string ValidateSourceStatusRequest(JsonElement root)
    {
        RequireExact(root, "contract", "type", "sourceInstanceId");
        if (
            RequiredString(root, "contract", 128)
                != ReaderRealtimeOutputProtocol.OutputContract
            || RequiredString(root, "type", 64) != SourceStatusRequestType
        )
        {
            throw Invalid("Reader 输出来源状态 RPC 合同无效");
        }
        string sourceInstanceId = RequiredString(
            root,
            "sourceInstanceId",
            160);
        if (!DirectBridgeContract.IsSafeId(sourceInstanceId))
        {
            throw Invalid("Reader 输出来源状态身份无效");
        }
        return sourceInstanceId;
    }

    internal static JsonObject SourceStatusResponse(
        ReaderRealtimeOutputSourceStatus status) => new()
    {
        ["contract"] = ReaderRealtimeOutputProtocol.OutputContract,
        ["type"] = SourceStatusResponseType,
        ["sourceInstanceId"] = status.SourceInstanceId,
        ["online"] = status.Online,
    };

    internal static ReaderRealtimeOutputSourceStatus
        ValidateSourceStatusResponse(
            JsonElement root,
            string expectedSourceInstanceId)
    {
        RequireExact(
            root,
            "contract",
            "type",
            "sourceInstanceId",
            "online");
        if (
            RequiredString(root, "contract", 128)
                != ReaderRealtimeOutputProtocol.OutputContract
            || RequiredString(root, "type", 64) != SourceStatusResponseType
            || root.GetProperty("online").ValueKind
                is not (JsonValueKind.True or JsonValueKind.False)
        )
        {
            throw Invalid("Reader 输出来源状态 RPC 回执无效");
        }
        string sourceInstanceId = RequiredString(
            root,
            "sourceInstanceId",
            160);
        if (!string.Equals(
            sourceInstanceId,
            expectedSourceInstanceId,
            StringComparison.Ordinal))
        {
            throw Invalid("Reader 输出来源状态 RPC 身份不匹配");
        }
        return new ReaderRealtimeOutputSourceStatus(
            sourceInstanceId,
            root.GetProperty("online").GetBoolean());
    }

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
        // 卡片钉在正文上没有 —— 沿途每一处都是重建，不显式搬就在这里断掉
        ["bindOutcome"] = ack.BindOutcome,
        ["bindReason"] = ack.BindReason,
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
        ["bindOutcome"] = null,
        ["bindReason"] = null,
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
            // ⚠ RequireExact 是 SetEquals，且它在读 ok 之前就跑 —— 所以**失败包
            //   也必须带这两个键**（填 null），否则一出错就变成「回包字段不匹配」
            //   这另一种错，真正的原因被盖掉。
            "bindOutcome",
            "bindReason",
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
        if (outcome is not ("applied" or "replay" or "queued"))
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
        // ⚠ 这里五个位置参数里有三个是**凭空填的**（sessionId 空串、error null，
        //   correlation/sourceInstanceId 来自 request 的回声）。新加的两个必须
        //   从回包里真的取出来，否则这一步会把上游的结果抹平。
        string? bindOutcome = root.GetProperty("bindOutcome").ValueKind == JsonValueKind.Null
            ? null : RequiredString(root, "bindOutcome", 32);
        if (bindOutcome is not (null or "none" or "bound" or "floating" or "unknown"))
        {
            throw Invalid("Reader 输出 RPC bindOutcome 无效");
        }
        string? bindReason = root.GetProperty("bindReason").ValueKind == JsonValueKind.Null
            ? null : RequiredString(root, "bindReason", 120);
        return new ReaderRealtimeOutputAck(
            string.Empty,
            expected.Correlation,
            expected.SourceInstanceId,
            outcome,
            null,
            bindOutcome,
            bindReason);
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
    // The broker deliberately allows up to 20 seconds for an exact-text
    // highlight to materialize an off-screen page, persist the idempotent
    // mutation, and confirm the rendered rectangle.  The pipe client must
    // outlive that owner deadline; otherwise it closes the RPC at 14 seconds
    // while the broker is still validly working and reports a false timeout
    // even when the Reader applies the highlight moments later.
    private static readonly TimeSpan ExchangeTimeout = TimeSpan.FromSeconds(24);
    private static readonly TimeSpan StatusConnectTimeout =
        TimeSpan.FromSeconds(1);
    private static readonly TimeSpan StatusExchangeTimeout =
        TimeSpan.FromSeconds(2);
    private readonly SemaphoreSlim _exchangeGate = new(1, 1);
    private readonly SemaphoreSlim _statusGate = new(1, 1);

    internal Task<ReaderRealtimeOutputAck> SendAsync(
        ReaderRealtimeOutputRequest request,
        CancellationToken cancellationToken) => ExchangeAsync(
            ReaderRealtimeOutputRpcProtocol.Request(request),
            root => ReaderRealtimeOutputRpcProtocol.ValidateResponse(
                root,
                request),
            _exchangeGate,
            ConnectTimeout,
            ExchangeTimeout,
            cancellationToken);

    internal Task<ReaderRealtimeOutputSourceStatus> ProbeSourceAsync(
        string sourceInstanceId,
        CancellationToken cancellationToken) => ExchangeAsync(
            ReaderRealtimeOutputRpcProtocol.SourceStatusRequest(
                sourceInstanceId),
            root => ReaderRealtimeOutputRpcProtocol
                .ValidateSourceStatusResponse(root, sourceInstanceId),
            _statusGate,
            StatusConnectTimeout,
            StatusExchangeTimeout,
            cancellationToken);

    private async Task<T> ExchangeAsync<T>(
        JsonObject request,
        Func<JsonElement, T> validateResponse,
        SemaphoreSlim gate,
        TimeSpan connectTimeout,
        TimeSpan exchangeTimeout,
        CancellationToken cancellationToken)
    {
        await gate.WaitAsync(cancellationToken).ConfigureAwait(false);
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
            connect.CancelAfter(connectTimeout);
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
                request.ToJsonString(DirectBridgeContract.JsonOptions));
            using CancellationTokenSource exchange =
                CancellationTokenSource.CreateLinkedTokenSource(
                    cancellationToken);
            exchange.CancelAfter(exchangeTimeout);
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
                    return validateResponse(document.RootElement);
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
            gate.Release();
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
        string? statusSourceInstanceId = null;
        JsonObject response;
        try
        {
            byte[] bytes = await ReaderVisualRpcFraming.ReadAsync(
                pipe,
                cancellationToken).ConfigureAwait(false);
            try
            {
                using JsonDocument document = JsonDocument.Parse(bytes);
                JsonElement root = document.RootElement;
                string type = root.ValueKind == JsonValueKind.Object
                    && root.TryGetProperty("type", out JsonElement typeValue)
                    && typeValue.ValueKind == JsonValueKind.String
                        ? typeValue.GetString() ?? string.Empty
                        : string.Empty;
                if (type == ReaderRealtimeOutputRpcProtocol
                    .SourceStatusRequestType)
                {
                    statusSourceInstanceId = ReaderRealtimeOutputRpcProtocol
                        .ValidateSourceStatusRequest(root);
                }
                else
                {
                    request = ReaderRealtimeOutputRpcProtocol.ValidateRequest(
                        root);
                }
            }
            finally
            {
                Array.Clear(bytes);
            }
            if (statusSourceInstanceId is not null)
            {
                response = ReaderRealtimeOutputRpcProtocol
                    .SourceStatusResponse(
                        _broker.GetSourceStatus(statusSourceInstanceId));
            }
            else
            {
                ReaderRealtimeOutputAck result = await _broker.SendAsync(
                    request!,
                    cancellationToken).ConfigureAwait(false);
                response = ReaderRealtimeOutputRpcProtocol.Success(
                    request!,
                    result);
            }
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
