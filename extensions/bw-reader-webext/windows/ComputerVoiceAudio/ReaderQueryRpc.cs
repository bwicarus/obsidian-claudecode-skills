using System.IO.Pipes;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace BwReader.ComputerVoiceAudio;

// MCP 进程与桥接主进程之间的那一跳。形状与浏览控制那条一致，只是载荷不同：
// 那边回的是滚动位置，这边回的是一段结果 JSON 加一个"是否被截断"。
//
// truncated 单独成字段而不是塞进结果里，是有意的：它必须在协议层就存在，
// 任何一端都不能"忘了带"。一个被悄悄截断的列表看起来跟完整的一模一样。
internal static class ReaderQueryRpcProtocol
{
    internal const string RequestType = "query-request";
    internal const string ResponseType = "query-response";

    internal static JsonObject Request(ReaderQueryRequest request) =>
        new()
        {
            ["contract"] = ReaderQueryProtocol.QueryContract,
            ["type"] = RequestType,
            ["correlation"] = request.Correlation,
            ["sourceInstanceId"] = request.SourceInstanceId,
            ["snapshotRevision"] = request.SnapshotRevision,
            ["file"] = request.File,
            ["query"] = request.Query,
            ["params"] = request.Parameters.DeepClone(),
        };

    internal static ReaderQueryRequest ValidateRequest(JsonElement root)
    {
        RequireExact(
            root,
            "contract",
            "type",
            "correlation",
            "sourceInstanceId",
            "snapshotRevision",
            "file",
            "query",
            "params");
        if (
            String(root, "contract", 128)
                != ReaderQueryProtocol.QueryContract
            || String(root, "type", 64) != RequestType
        )
        {
            throw Invalid("Reader 查询 RPC 合同无效");
        }
        string correlation = SafeId(root, "correlation");
        string sourceInstanceId = SafeId(root, "sourceInstanceId");
        long snapshotRevision = Integer(root, "snapshotRevision");
        if (snapshotRevision < 0)
        {
            throw Invalid("Reader 查询 RPC revision 无效");
        }
        string file = String(root, "file", 4096);
        string query = String(root, "query", 64);
        if (!ReaderQueryProtocol.IsQuery(query))
        {
            throw Invalid("Reader 查询 RPC 名称不在名单内");
        }
        JsonElement parametersElement = root.GetProperty("params");
        if (parametersElement.ValueKind != JsonValueKind.Object)
        {
            throw Invalid("Reader 查询 RPC 参数必须是对象");
        }
        ReaderQueryProtocol.RequireBoundedJson(parametersElement);
        JsonNode? parameters = JsonNode.Parse(
            parametersElement.GetRawText());
        if (parameters is null)
        {
            throw Invalid("Reader 查询 RPC 参数无效");
        }
        return new ReaderQueryRequest(
            correlation,
            sourceInstanceId,
            snapshotRevision,
            file,
            query,
            parameters);
    }

    internal static JsonObject Success(
        ReaderQueryRequest request,
        ReaderQueryResponse response) => new()
    {
        ["contract"] = ReaderQueryProtocol.QueryContract,
        ["type"] = ResponseType,
        ["ok"] = true,
        ["correlation"] = request.Correlation,
        ["sourceInstanceId"] = request.SourceInstanceId,
        ["snapshotRevision"] = request.SnapshotRevision,
        ["file"] = request.File,
        ["query"] = request.Query,
        ["status"] = response.Status,
        ["result"] = JsonNode.Parse(response.Result.GetRawText()),
        ["truncated"] = response.Truncated,
        ["code"] = null,
        ["message"] = null,
        ["retryable"] = false,
    };

    internal static JsonObject Failure(
        ReaderQueryRequest? request,
        ReaderQueryException exception) => new()
    {
        ["contract"] = ReaderQueryProtocol.QueryContract,
        ["type"] = ResponseType,
        ["ok"] = false,
        ["correlation"] = request?.Correlation,
        ["sourceInstanceId"] = request?.SourceInstanceId,
        ["snapshotRevision"] = request?.SnapshotRevision,
        ["file"] = request?.File,
        ["query"] = request?.Query,
        ["status"] = null,
        ["result"] = null,
        ["truncated"] = null,
        ["code"] = exception.Code,
        ["message"] = exception.Message,
        ["retryable"] = exception.Retryable,
    };

    internal static ReaderQueryResponse ValidateResponse(
        JsonElement root,
        ReaderQueryRequest expected)
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
            "query",
            "status",
            "result",
            "truncated",
            "code",
            "message",
            "retryable");
        if (
            String(root, "contract", 128)
                != ReaderQueryProtocol.QueryContract
            || String(root, "type", 64) != ResponseType
            || root.GetProperty("ok").ValueKind
                is not (JsonValueKind.True or JsonValueKind.False)
        )
        {
            throw Invalid("Reader 查询 RPC 回执无效");
        }
        bool ok = root.GetProperty("ok").GetBoolean();
        if (!ok)
        {
            bool unbound = root.GetProperty("correlation").ValueKind
                == JsonValueKind.Null;
            if (unbound)
            {
                foreach (string field in new[]
                {
                    "sourceInstanceId",
                    "snapshotRevision",
                    "file",
                    "query",
                })
                {
                    if (root.GetProperty(field).ValueKind
                        != JsonValueKind.Null)
                    {
                        throw Invalid("Reader 查询 RPC 未绑定错误回执无效");
                    }
                }
            }
            else
            {
                RequireEcho(root, expected);
            }
            RequireNullResultFields(root);
            string code = String(root, "code", 160);
            string message = String(root, "message", 1024);
            if (root.GetProperty("retryable").ValueKind
                is not (JsonValueKind.True or JsonValueKind.False))
            {
                throw Invalid("Reader 查询 RPC retryable 无效");
            }
            throw new ReaderQueryException(
                code,
                message,
                root.GetProperty("retryable").GetBoolean());
        }
        RequireEcho(root, expected);
        string status = String(root, "status", 16);
        if (status is not ("ok" or "unsupported" or "unavailable"))
        {
            throw Invalid("Reader 查询 RPC status 无效");
        }
        JsonElement result = root.GetProperty("result");
        if (result.ValueKind != JsonValueKind.Object)
        {
            throw Invalid("Reader 查询 RPC 结果必须是对象");
        }
        ReaderQueryProtocol.RequireBoundedJson(result);
        if (root.GetProperty("truncated").ValueKind
            is not (JsonValueKind.True or JsonValueKind.False))
        {
            throw Invalid("Reader 查询 RPC truncated 无效");
        }
        if (
            root.GetProperty("code").ValueKind != JsonValueKind.Null
            || root.GetProperty("message").ValueKind != JsonValueKind.Null
            || root.GetProperty("retryable").ValueKind
                != JsonValueKind.False
        )
        {
            throw Invalid("Reader 查询 RPC 成功回执字段无效");
        }
        return new ReaderQueryResponse(
            string.Empty,
            expected.Correlation,
            expected.SourceInstanceId,
            expected.SnapshotRevision,
            expected.File,
            expected.Query,
            status,
            result.Clone(),
            root.GetProperty("truncated").ValueKind == JsonValueKind.True);
    }

    private static void RequireEcho(
        JsonElement root,
        ReaderQueryRequest expected)
    {
        if (
            String(root, "correlation", 160) != expected.Correlation
            || String(root, "sourceInstanceId", 160)
                != expected.SourceInstanceId
            || Integer(root, "snapshotRevision")
                != expected.SnapshotRevision
            || String(root, "file", 4096) != expected.File
            || String(root, "query", 64) != expected.Query
        )
        {
            throw Invalid("Reader 查询 RPC 回执身份不匹配");
        }
    }

    private static void RequireNullResultFields(JsonElement root)
    {
        foreach (string field in new[] { "status", "result", "truncated" })
        {
            if (root.GetProperty(field).ValueKind != JsonValueKind.Null)
            {
                throw Invalid("Reader 查询 RPC 错误回执字段无效");
            }
        }
    }

    private static void RequireExact(
        JsonElement root,
        params string[] fields)
    {
        if (root.ValueKind != JsonValueKind.Object)
        {
            throw Invalid("Reader 查询 RPC 必须是对象");
        }
        DirectJsonValidation.RequireNoDuplicateKeys(root);
        HashSet<string> actual = root.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        if (!actual.SetEquals(fields))
        {
            throw Invalid("Reader 查询 RPC 字段不匹配");
        }
    }

    private static string SafeId(JsonElement root, string name)
    {
        string value = String(root, name, 160);
        if (!DirectBridgeContract.IsSafeId(value))
        {
            throw Invalid($"Reader 查询 RPC {name} 无效");
        }
        return value;
    }

    private static string String(
        JsonElement root,
        string name,
        int max,
        bool allowEmpty = false)
    {
        JsonElement value = root.GetProperty(name);
        if (
            value.ValueKind != JsonValueKind.String
            || value.GetString() is not string text
            || (!allowEmpty && text.Length == 0)
            || text.Length > max
            || text.Any(char.IsControl)
        )
        {
            throw Invalid($"Reader 查询 RPC {name} 无效");
        }
        return text;
    }

    private static long Integer(JsonElement root, string name)
    {
        JsonElement value = root.GetProperty(name);
        if (
            value.ValueKind != JsonValueKind.Number
            || !value.TryGetInt64(out long result)
        )
        {
            throw Invalid($"Reader 查询 RPC {name} 无效");
        }
        return result;
    }

    private static ReaderQueryException Invalid(string message) =>
        new("BW_READER_QUERY_RPC_INVALID", message, retryable: false);
}

internal sealed class NamedPipeReaderQueryRpcClient
{
    internal const string PipeName = "bw-reader-query-rpc-v1";
    internal static readonly PipeOptions RequiredPipeOptions =
        PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly;
    private static readonly TimeSpan ConnectTimeout = TimeSpan.FromSeconds(3);
    private static readonly TimeSpan ExchangeTimeout = TimeSpan.FromSeconds(12);
    private readonly SemaphoreSlim _exchangeGate = new(1, 1);

    internal async Task<ReaderQueryResponse> RequestAsync(
        ReaderQueryRequest request,
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
                await pipe.ConnectAsync(connect.Token).ConfigureAwait(false);
            }
            catch (OperationCanceledException exception)
                when (!cancellationToken.IsCancellationRequested)
            {
                throw Failure(
                    "BW_READER_QUERY_RPC_UNAVAILABLE",
                    "Windows Reader 查询服务未连接",
                    exception);
            }
            byte[] requestBytes = Encoding.UTF8.GetBytes(
                ReaderQueryRpcProtocol.Request(request)
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
                    return ReaderQueryRpcProtocol.ValidateResponse(
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
                    "BW_READER_QUERY_RPC_TIMEOUT",
                    "Windows Reader 查询 RPC 超时",
                    exception);
            }
            finally
            {
                Array.Clear(requestBytes);
            }
        }
        catch (ReaderQueryException)
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
                "BW_READER_QUERY_RPC_FAILED",
                "Windows Reader 查询 RPC 失败",
                exception);
        }
        finally
        {
            _exchangeGate.Release();
        }
    }

    // 这条通道只读，重试不会改变书里的任何东西 —— 传输层的失败因此一律可重试。
    private static ReaderQueryException Failure(
        string code,
        string message,
        Exception exception) =>
        new(code, message, retryable: true, exception);
}

internal sealed class NamedPipeReaderQueryRpcServer
{
    internal static readonly PipeOptions RequiredPipeOptions =
        PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly;
    private readonly ReaderQueryBroker _broker;

    internal NamedPipeReaderQueryRpcServer(ReaderQueryBroker broker)
    {
        _broker = broker;
    }

    internal async Task RunAsync(CancellationToken cancellationToken)
    {
        while (!cancellationToken.IsCancellationRequested)
        {
            await using NamedPipeServerStream pipe = new(
                NamedPipeReaderQueryRpcClient.PipeName,
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
            catch (
                Exception exception
            ) when (
                exception is IOException
                or UnauthorizedAccessException
                or ObjectDisposedException
                or JsonException
                or ReaderVisualDeliveryException
                or ReaderQueryException
            )
            {
            }
        }
    }

    private async Task HandleAsync(
        Stream pipe,
        CancellationToken cancellationToken)
    {
        ReaderQueryRequest? request = null;
        JsonObject response;
        try
        {
            byte[] bytes = await ReaderVisualRpcFraming.ReadAsync(
                pipe,
                cancellationToken).ConfigureAwait(false);
            try
            {
                using JsonDocument document = JsonDocument.Parse(bytes);
                request = ReaderQueryRpcProtocol.ValidateRequest(
                    document.RootElement);
            }
            finally
            {
                Array.Clear(bytes);
            }
            ReaderQueryResponse result = await _broker.RequestAsync(
                request,
                cancellationToken).ConfigureAwait(false);
            response = ReaderQueryRpcProtocol.Success(request, result);
        }
        catch (ReaderQueryException exception)
        {
            response = ReaderQueryRpcProtocol.Failure(request, exception);
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
