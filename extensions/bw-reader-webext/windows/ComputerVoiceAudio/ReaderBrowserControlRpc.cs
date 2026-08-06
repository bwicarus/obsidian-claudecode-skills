using System.IO.Pipes;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace BwReader.ComputerVoiceAudio;

internal static class ReaderBrowserControlRpcProtocol
{
    internal const string RequestType = "browser-control-request";
    internal const string ResponseType = "browser-control-response";

    internal static JsonObject Request(ReaderBrowserControlRequest request) =>
        new()
        {
            ["contract"] = ReaderBrowserControlProtocol.ControlContract,
            ["type"] = RequestType,
            ["correlation"] = request.Correlation,
            ["sourceInstanceId"] = request.SourceInstanceId,
            ["snapshotRevision"] = request.SnapshotRevision,
            ["file"] = request.File,
            ["page"] = request.Page.DeepClone(),
            ["action"] = request.Action,
            ["target"] = request.Target,
            ["selectionId"] = request.SelectionId,
        };

    internal static ReaderBrowserControlRequest ValidateRequest(
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
            "action",
            "target",
            "selectionId");
        if (
            String(root, "contract", 128)
                != ReaderBrowserControlProtocol.ControlContract
            || String(root, "type", 64) != RequestType
        )
        {
            throw Invalid("Reader 浏览控制 RPC 合同无效");
        }
        string correlation = SafeId(root, "correlation");
        string sourceInstanceId = SafeId(root, "sourceInstanceId");
        long snapshotRevision = Integer(root, "snapshotRevision");
        if (snapshotRevision < 0)
        {
            throw Invalid("Reader 浏览控制 RPC revision 无效");
        }
        string file = String(root, "file", 4096);
        JsonElement pageElement = root.GetProperty("page");
        JsonNode? page = JsonNode.Parse(pageElement.GetRawText());
        if (
            page is null
            || !ReaderVisualDeliveryProtocol.PageEquivalent(
                page,
                pageElement)
        )
        {
            throw Invalid("Reader 浏览控制 RPC page 无效");
        }
        string action = String(root, "action", 32);
        if (!ReaderBrowserControlProtocol.IsAction(action))
        {
            throw Invalid("Reader 浏览控制 RPC action 无效");
        }
        string? target = NullableString(
            root,
            "target",
            ReaderBrowserControlProtocol.MaximumTargetCharacters,
            safeId: false);
        string? selectionId = NullableString(
            root,
            "selectionId",
            160,
            safeId: true);
        if (
            action is "next-viewport" or "previous-viewport"
                ? target is not null || selectionId is not null
                : action is "scroll-to-text" or "scroll-to-heading"
                    ? target is null || selectionId is not null
                    : action == "scroll-to-selection"
                        ? target is not null || selectionId is null
                        : true
        )
        {
            throw Invalid("Reader 浏览控制 RPC 参数与动作不匹配");
        }
        return new ReaderBrowserControlRequest(
            correlation,
            sourceInstanceId,
            snapshotRevision,
            file,
            page,
            action,
            target,
            selectionId);
    }

    internal static JsonObject Success(
        ReaderBrowserControlRequest request,
        ReaderBrowserControlResponse response) => new()
    {
        ["contract"] = ReaderBrowserControlProtocol.ControlContract,
        ["type"] = ResponseType,
        ["ok"] = true,
        ["correlation"] = request.Correlation,
        ["sourceInstanceId"] = request.SourceInstanceId,
        ["snapshotRevision"] = request.SnapshotRevision,
        ["file"] = request.File,
        ["page"] = request.Page.DeepClone(),
        ["action"] = request.Action,
        ["status"] = response.Status,
        ["scrollX"] = response.ScrollX,
        ["scrollY"] = response.ScrollY,
        ["url"] = response.Url,
        ["title"] = response.Title,
        ["code"] = null,
        ["message"] = null,
        ["retryable"] = false,
    };

    internal static JsonObject Failure(
        ReaderBrowserControlRequest? request,
        ReaderBrowserControlException exception) => new()
    {
        ["contract"] = ReaderBrowserControlProtocol.ControlContract,
        ["type"] = ResponseType,
        ["ok"] = false,
        ["correlation"] = request?.Correlation,
        ["sourceInstanceId"] = request?.SourceInstanceId,
        ["snapshotRevision"] = request?.SnapshotRevision,
        ["file"] = request?.File,
        ["page"] = request?.Page.DeepClone(),
        ["action"] = request?.Action,
        ["status"] = null,
        ["scrollX"] = null,
        ["scrollY"] = null,
        ["url"] = null,
        ["title"] = null,
        ["code"] = exception.Code,
        ["message"] = exception.Message,
        ["retryable"] = exception.Retryable,
    };

    internal static ReaderBrowserControlResponse ValidateResponse(
        JsonElement root,
        ReaderBrowserControlRequest expected)
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
            "action",
            "status",
            "scrollX",
            "scrollY",
            "url",
            "title",
            "code",
            "message",
            "retryable");
        if (
            String(root, "contract", 128)
                != ReaderBrowserControlProtocol.ControlContract
            || String(root, "type", 64) != ResponseType
            || root.GetProperty("ok").ValueKind
                is not (JsonValueKind.True or JsonValueKind.False)
        )
        {
            throw Invalid("Reader 浏览控制 RPC 回执无效");
        }
        bool ok = root.GetProperty("ok").GetBoolean();
        if (!ok)
        {
            bool unbound = root.GetProperty("correlation").ValueKind ==
                JsonValueKind.Null;
            if (unbound)
            {
                foreach (string field in new[]
                {
                    "sourceInstanceId",
                    "snapshotRevision",
                    "file",
                    "page",
                    "action",
                })
                {
                    if (root.GetProperty(field).ValueKind !=
                        JsonValueKind.Null)
                    {
                        throw Invalid(
                            "Reader 浏览控制 RPC 未绑定错误回执无效");
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
                throw Invalid(
                    "Reader 浏览控制 RPC retryable 无效");
            }
            bool retryable = root.GetProperty("retryable").GetBoolean();
            throw new ReaderBrowserControlException(
                code,
                message,
                retryable);
        }
        RequireEcho(root, expected);
        string status = String(root, "status", 16);
        if (status is not ("success" or "not-found" or "rejected"))
        {
            throw Invalid("Reader 浏览控制 RPC status 无效");
        }
        double scrollX = Number(root, "scrollX");
        double scrollY = Number(root, "scrollY");
        string url = String(root, "url", 4096);
        string title = String(root, "title", 1024, allowEmpty: true);
        if (
            root.GetProperty("code").ValueKind != JsonValueKind.Null
            || root.GetProperty("message").ValueKind != JsonValueKind.Null
            || root.GetProperty("retryable").ValueKind
                != JsonValueKind.False
        )
        {
            throw Invalid("Reader 浏览控制 RPC 成功回执字段无效");
        }
        return new ReaderBrowserControlResponse(
            string.Empty,
            expected.Correlation,
            expected.SourceInstanceId,
            expected.SnapshotRevision,
            expected.File,
            JsonDocument.Parse(expected.Page.ToJsonString()).RootElement
                .Clone(),
            expected.Action,
            status,
            scrollX,
            scrollY,
            url,
            title);
    }

    private static void RequireEcho(
        JsonElement root,
        ReaderBrowserControlRequest expected)
    {
        if (
            String(root, "correlation", 160) != expected.Correlation
            || String(root, "sourceInstanceId", 160)
                != expected.SourceInstanceId
            || Integer(root, "snapshotRevision")
                != expected.SnapshotRevision
            || String(root, "file", 4096) != expected.File
            || String(root, "action", 32) != expected.Action
            || !ReaderVisualDeliveryProtocol.PageEquivalent(
                expected.Page,
                root.GetProperty("page"))
        )
        {
            throw Invalid("Reader 浏览控制 RPC 回执身份不匹配");
        }
    }

    private static void RequireNullResultFields(JsonElement root)
    {
        foreach (string field in new[]
        {
            "status",
            "scrollX",
            "scrollY",
            "url",
            "title",
        })
        {
            if (root.GetProperty(field).ValueKind != JsonValueKind.Null)
            {
                throw Invalid("Reader 浏览控制 RPC 错误回执字段无效");
            }
        }
    }

    private static void RequireExact(
        JsonElement root,
        params string[] fields)
    {
        if (root.ValueKind != JsonValueKind.Object)
        {
            throw Invalid("Reader 浏览控制 RPC 必须是对象");
        }
        DirectJsonValidation.RequireNoDuplicateKeys(root);
        HashSet<string> actual = root.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        if (!actual.SetEquals(fields))
        {
            throw Invalid("Reader 浏览控制 RPC 字段不匹配");
        }
    }

    private static string SafeId(JsonElement root, string name)
    {
        string value = String(root, name, 160);
        if (!DirectBridgeContract.IsSafeId(value))
        {
            throw Invalid($"Reader 浏览控制 RPC {name} 无效");
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
            throw Invalid($"Reader 浏览控制 RPC {name} 无效");
        }
        return text;
    }

    private static string? NullableString(
        JsonElement root,
        string name,
        int max,
        bool safeId)
    {
        JsonElement value = root.GetProperty(name);
        if (value.ValueKind == JsonValueKind.Null)
        {
            return null;
        }
        string text = String(root, name, max);
        if (safeId && !DirectBridgeContract.IsSafeId(text))
        {
            throw Invalid($"Reader 浏览控制 RPC {name} 无效");
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
            throw Invalid($"Reader 浏览控制 RPC {name} 无效");
        }
        return result;
    }

    private static double Number(JsonElement root, string name)
    {
        JsonElement value = root.GetProperty(name);
        if (
            value.ValueKind != JsonValueKind.Number
            || !value.TryGetDouble(out double result)
            || !double.IsFinite(result)
        )
        {
            throw Invalid($"Reader 浏览控制 RPC {name} 无效");
        }
        return result;
    }

    private static ReaderBrowserControlException Invalid(string message) =>
        new(
            "BW_READER_BROWSER_CONTROL_RPC_INVALID",
            message,
            retryable: false);
}

internal sealed class NamedPipeReaderBrowserControlRpcClient
{
    internal const string PipeName = "bw-reader-browser-control-rpc-v1";
    internal static readonly PipeOptions RequiredPipeOptions =
        PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly;
    private static readonly TimeSpan ConnectTimeout = TimeSpan.FromSeconds(3);
    private static readonly TimeSpan ExchangeTimeout = TimeSpan.FromSeconds(14);
    private readonly SemaphoreSlim _exchangeGate = new(1, 1);

    internal async Task<ReaderBrowserControlResponse> RequestAsync(
        ReaderBrowserControlRequest request,
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
                throw Failure(
                    "BW_READER_BROWSER_CONTROL_RPC_UNAVAILABLE",
                    "Windows Reader 浏览控制服务未连接",
                    exception);
            }
            byte[] requestBytes = Encoding.UTF8.GetBytes(
                ReaderBrowserControlRpcProtocol.Request(request)
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
                    return ReaderBrowserControlRpcProtocol.ValidateResponse(
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
                    "BW_READER_BROWSER_CONTROL_RPC_TIMEOUT",
                    "Windows Reader 浏览控制 RPC 超时",
                    exception);
            }
            finally
            {
                Array.Clear(requestBytes);
            }
        }
        catch (ReaderBrowserControlException)
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
                "BW_READER_BROWSER_CONTROL_RPC_FAILED",
                "Windows Reader 浏览控制 RPC 失败",
                exception);
        }
        finally
        {
            _exchangeGate.Release();
        }
    }

    private static ReaderBrowserControlException Failure(
        string code,
        string message,
        Exception exception) =>
        new(code, message, retryable: true, exception);
}

internal sealed class NamedPipeReaderBrowserControlRpcServer
{
    internal static readonly PipeOptions RequiredPipeOptions =
        PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly;
    private readonly ReaderBrowserControlBroker _broker;

    internal NamedPipeReaderBrowserControlRpcServer(
        ReaderBrowserControlBroker broker)
    {
        _broker = broker;
    }

    internal async Task RunAsync(CancellationToken cancellationToken)
    {
        while (!cancellationToken.IsCancellationRequested)
        {
            await using NamedPipeServerStream pipe = new(
                NamedPipeReaderBrowserControlRpcClient.PipeName,
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
                or ReaderBrowserControlException
            )
            {
            }
        }
    }

    private async Task HandleAsync(
        Stream pipe,
        CancellationToken cancellationToken)
    {
        ReaderBrowserControlRequest? request = null;
        JsonObject response;
        try
        {
            byte[] bytes = await ReaderVisualRpcFraming.ReadAsync(
                pipe,
                cancellationToken).ConfigureAwait(false);
            try
            {
                using JsonDocument document = JsonDocument.Parse(bytes);
                request = ReaderBrowserControlRpcProtocol.ValidateRequest(
                    document.RootElement);
            }
            finally
            {
                Array.Clear(bytes);
            }
            ReaderBrowserControlResponse result = await _broker.RequestAsync(
                request,
                cancellationToken).ConfigureAwait(false);
            response = ReaderBrowserControlRpcProtocol.Success(
                request,
                result);
        }
        catch (ReaderBrowserControlException exception)
        {
            response = ReaderBrowserControlRpcProtocol.Failure(
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
