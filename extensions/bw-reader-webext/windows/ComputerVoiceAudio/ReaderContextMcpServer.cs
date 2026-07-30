using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.AspNetCore.Http;

namespace BwReader.ComputerVoiceAudio;

internal sealed class ReaderContextMcpServer
{
    internal const string ToolName = "reader_context_snapshot";
    internal const string ServerName = "bw-reader-context-snapshot";
    internal const string ServerVersion = "1.0.0";
    internal const string LatestProtocolVersion = "2025-11-25";
    internal static readonly TimeSpan FreshnessWindow =
        TimeSpan.FromMinutes(3);
    private static readonly HashSet<string> SupportedProtocolVersions =
        new(StringComparer.Ordinal)
        {
            LatestProtocolVersion,
            "2025-06-18",
            "2025-03-26",
            "2024-11-05",
            "2024-10-07",
        };

    private const int MaximumMessageCharacters = 1024 * 1024;
    private const int MaximumSnapshotBytes = 128 * 1024;
    private static readonly UTF8Encoding Utf8WithoutBom = new(
        encoderShouldEmitUTF8Identifier: false);

    private readonly string _statePath;
    private readonly TextReader _input;
    private readonly TextWriter _output;
    private readonly Func<DateTimeOffset> _utcNow;
    private readonly string _instanceId;
    private readonly string _startedAt;
    private readonly SemaphoreSlim _messageGate = new(1, 1);
    private JsonObject? _latestSnapshot;
    private long _latestRevision = -1;
    private long _loadSequence;
    private long _loadErrors;
    private long _callSequence;
    private bool _initialized;

    internal string InstanceId => _instanceId;

    internal ReaderContextMcpServer(
        string statePath,
        TextReader input,
        TextWriter output,
        Func<DateTimeOffset>? utcNow = null,
        string? instanceId = null)
    {
        if (!Path.IsPathFullyQualified(statePath))
        {
            throw new ArgumentException(
                "snapshot state path must be absolute",
                nameof(statePath));
        }
        _statePath = Path.GetFullPath(statePath);
        _input = input;
        _output = output;
        _utcNow = utcNow ?? (() => DateTimeOffset.UtcNow);
        _instanceId = instanceId ?? Guid.NewGuid().ToString();
        _startedAt = _utcNow().ToString("O");
    }

    internal async Task<int> RunAsync(
        CancellationToken cancellationToken)
    {
        await TryLoadLatestAsync(cancellationToken).ConfigureAwait(false);
        while (!cancellationToken.IsCancellationRequested)
        {
            string? line = await _input.ReadLineAsync(
                cancellationToken).ConfigureAwait(false);
            if (line is null)
            {
                return 0;
            }
            if (
                line.Length == 0
                || line.Length > MaximumMessageCharacters
            )
            {
                await WriteMessageAsync(
                    BuildError(
                        id: null,
                        code: -32700,
                        message: "Invalid JSON-RPC message"),
                    cancellationToken).ConfigureAwait(false);
                continue;
            }
            JsonObject? response = await ProcessMessageAsync(
                line,
                cancellationToken).ConfigureAwait(false);
            if (response is not null)
            {
                await WriteMessageAsync(response, cancellationToken)
                    .ConfigureAwait(false);
            }
        }
        return 0;
    }

    internal async Task<JsonObject?> ProcessMessageAsync(
        string message,
        CancellationToken cancellationToken)
    {
        await _messageGate.WaitAsync(cancellationToken)
            .ConfigureAwait(false);
        try
        {
            return await ProcessMessageCoreAsync(
                message,
                cancellationToken).ConfigureAwait(false);
        }
        finally
        {
            _messageGate.Release();
        }
    }

    private async Task<JsonObject?> ProcessMessageCoreAsync(
        string message,
        CancellationToken cancellationToken)
    {
        if (
            message.Length == 0
            || message.Length > MaximumMessageCharacters
        )
        {
            return BuildError(
                id: null,
                code: -32700,
                message: "Invalid JSON-RPC message");
        }
        JsonDocument document;
        try
        {
            document = JsonDocument.Parse(
                message,
                new JsonDocumentOptions
                {
                    AllowTrailingCommas = false,
                    CommentHandling = JsonCommentHandling.Disallow,
                    MaxDepth = 32,
                });
        }
        catch (JsonException)
        {
            return BuildError(
                id: null,
                code: -32700,
                message: "Parse error");
        }

        using (document)
        {
            JsonElement root = document.RootElement;
            if (
                root.ValueKind != JsonValueKind.Object
                || !root.TryGetProperty(
                    "jsonrpc",
                    out JsonElement jsonrpc)
                || jsonrpc.ValueKind != JsonValueKind.String
                || jsonrpc.GetString() != "2.0"
                || !root.TryGetProperty(
                    "method",
                    out JsonElement methodValue)
                || methodValue.ValueKind != JsonValueKind.String
            )
            {
                return BuildError(
                    CloneId(root),
                    -32600,
                    "Invalid Request");
            }

            string method = methodValue.GetString()!;
            JsonNode? id = CloneId(root);
            bool notification = id is null;
            if (notification)
            {
                HandleNotification(method);
                return null;
            }
            JsonNode requestId = id!;

            JsonElement parameters = root.TryGetProperty(
                "params",
                out JsonElement paramsValue)
                ? paramsValue
                : default;
            switch (method)
            {
                case "initialize":
                    return HandleInitialize(
                        requestId,
                        parameters);
                case "ping":
                    return BuildResult(
                        requestId,
                        new JsonObject());
                case "tools/list":
                    if (!RequireInitialized())
                    {
                        return BuildError(
                            id,
                            -32002,
                            "Server not initialized");
                    }
                    return BuildResult(
                        requestId,
                        BuildToolList());
                case "tools/call":
                    if (!RequireInitialized())
                    {
                        return BuildError(
                            id,
                            -32002,
                            "Server not initialized");
                    }
                    return await HandleToolCallAsync(
                        requestId,
                        parameters,
                        cancellationToken).ConfigureAwait(false);
                default:
                    return BuildError(
                        id,
                        -32601,
                        "Method not found");
            }
        }
    }

    private JsonObject HandleInitialize(
        JsonNode id,
        JsonElement parameters)
    {
        if (
            parameters.ValueKind != JsonValueKind.Object
            || !parameters.TryGetProperty(
                "protocolVersion",
                out JsonElement protocolValue)
            || protocolValue.ValueKind != JsonValueKind.String
            || string.IsNullOrWhiteSpace(protocolValue.GetString())
        )
        {
            return BuildError(
                id,
                -32602,
                "Invalid initialize parameters");
        }
        _initialized = true;
        string requestedProtocolVersion = protocolValue.GetString()!;
        string negotiatedProtocolVersion =
            IsSupportedProtocolVersion(requestedProtocolVersion)
                ? requestedProtocolVersion
                : LatestProtocolVersion;
        return BuildResult(
            id,
            new JsonObject
            {
                ["protocolVersion"] = negotiatedProtocolVersion,
                ["capabilities"] = new JsonObject
                {
                    ["tools"] = new JsonObject
                    {
                        ["listChanged"] = false,
                    },
                },
                ["serverInfo"] = new JsonObject
                {
                    ["name"] = ServerName,
                    ["version"] = ServerVersion,
                },
                ["instructions"] =
                    "Use reader_context_snapshot only when the user asks "
                    + "about the current Reader page or selection. Respect "
                    + "contextStatus; pending or stale means the current "
                    + "page text is unavailable.",
            });
    }

    private static JsonObject BuildToolList() =>
        new()
        {
            ["tools"] = new JsonArray
            {
                new JsonObject
                {
                    ["name"] = ToolName,
                    ["description"] =
                        "Read the newest Windows-local Reader page and "
                        + "selection snapshot. The tool is read-only. "
                        + "Check contextStatus before using currentPage; "
                        + "never reuse text when it is pending or stale.",
                    ["inputSchema"] = new JsonObject
                    {
                        ["type"] = "object",
                        ["additionalProperties"] = false,
                        ["properties"] = new JsonObject(),
                    },
                    ["annotations"] = new JsonObject
                    {
                        ["readOnlyHint"] = true,
                        ["destructiveHint"] = false,
                        ["idempotentHint"] = true,
                        ["openWorldHint"] = false,
                    },
                },
            },
        };

    private async Task<JsonObject> HandleToolCallAsync(
        JsonNode id,
        JsonElement parameters,
        CancellationToken cancellationToken)
    {
        if (
            parameters.ValueKind != JsonValueKind.Object
            || !parameters.TryGetProperty(
                "name",
                out JsonElement nameValue)
            || nameValue.ValueKind != JsonValueKind.String
            || nameValue.GetString() != ToolName
            || (
                parameters.TryGetProperty(
                    "arguments",
                    out JsonElement arguments)
                && (
                    arguments.ValueKind != JsonValueKind.Object
                    || arguments.EnumerateObject().Any()
                )
            )
        )
        {
            return BuildError(
                id,
                -32602,
                "Invalid tool call");
        }

        _callSequence = checked(_callSequence + 1);
        await TryLoadLatestAsync(cancellationToken).ConfigureAwait(false);
        JsonObject payload = BuildToolPayload();
        return BuildResult(
            id,
            new JsonObject
            {
                ["content"] = new JsonArray
                {
                    new JsonObject
                    {
                        ["type"] = "text",
                        ["text"] = payload.ToJsonString(
                            DirectBridgeContract.JsonOptions),
                    },
                },
            });
    }

    private JsonObject BuildToolPayload()
    {
        JsonObject? snapshot = _latestSnapshot?.DeepClone()
            as JsonObject;
        if (snapshot is not null)
        {
            ApplyFreshness(snapshot, _utcNow());
            snapshot["mcp"] = new JsonObject
            {
                ["pid"] = Environment.ProcessId,
                ["instanceId"] = _instanceId,
                ["startedAtUtc"] = _startedAt,
                ["callSequence"] = _callSequence,
                ["loadSequence"] = _loadSequence,
                ["loadErrors"] = _loadErrors,
            };
            return snapshot;
        }
        return new JsonObject
        {
            ["schema"] =
                FileDirectSnapshotContextAdapter.SnapshotContract,
            ["revision"] = 0,
            ["updatedAtUtc"] = null,
            ["latestEvent"] = null,
            ["activeReading"] = null,
            ["contextStatus"] = "pending",
            ["currentPage"] = null,
            ["selection"] = new JsonObject
            {
                ["state"] = "unknown",
                ["text"] = null,
                ["ref"] = null,
                ["reason"] = "snapshot-not-received",
            },
            ["mcp"] = new JsonObject
            {
                ["pid"] = Environment.ProcessId,
                ["instanceId"] = _instanceId,
                ["startedAtUtc"] = _startedAt,
                ["callSequence"] = _callSequence,
                ["loadSequence"] = _loadSequence,
                ["loadErrors"] = _loadErrors,
            },
        };
    }

    internal static void ApplyFreshness(
        JsonObject snapshot,
        DateTimeOffset now)
    {
        if (snapshot["activeReading"] is not JsonObject active)
        {
            return;
        }
        long? observedAt = LongValue(active["receivedAtEpochMs"])
            ?? LongValue(active["observedAtEpochMs"]);
        if (observedAt is null)
        {
            MarkStale(snapshot, active, ageSeconds: null);
            return;
        }
        long nowMilliseconds = now.ToUnixTimeMilliseconds();
        long ageMilliseconds = Math.Max(
            0,
            nowMilliseconds - observedAt.Value);
        long ageSeconds = ageMilliseconds / 1000;
        active["ageSec"] = ageSeconds;
        bool fresh = ageMilliseconds <=
            (long)FreshnessWindow.TotalMilliseconds;
        active["fresh"] = fresh;
        if (!fresh)
        {
            MarkStale(snapshot, active, ageSeconds);
            return;
        }
        ApplyDrawingFreshness(snapshot, now);
    }

    private static void ApplyDrawingFreshness(
        JsonObject snapshot,
        DateTimeOffset now)
    {
        if (
            snapshot["currentPage"] is not JsonObject page
            || page["visual"] is not JsonObject visual
            || visual["drawing"] is not JsonObject drawing
            || drawing["empty"]?.GetValue<bool?>() == true
        )
        {
            return;
        }
        double? lastEditedAt = DoubleValue(
            drawing["lastEditedAt"]);
        double? freshWindow = DoubleValue(
            drawing["freshWindowS"]);
        if (
            lastEditedAt is null
            || freshWindow is null
            || freshWindow <= 0
        )
        {
            return;
        }
        double nowSeconds = now.ToUnixTimeMilliseconds() / 1000.0;
        double age = Math.Max(
            0,
            nowSeconds - lastEditedAt.Value);
        drawing["freshness"] =
            age <= freshWindow.Value
                ? "recent"
                : "stale";
    }

    private static void MarkStale(
        JsonObject snapshot,
        JsonObject active,
        long? ageSeconds)
    {
        active["fresh"] = false;
        active["ageSec"] = ageSeconds;
        snapshot["contextStatus"] = "stale";
        snapshot["currentPage"] = new JsonObject
        {
            ["file"] = active["file"]?.DeepClone(),
            ["title"] = active["title"]?.DeepClone(),
            ["page"] = active["page"]?.DeepClone(),
            ["stable"] = false,
            ["text"] = "",
            ["textAvailable"] = false,
        };
        snapshot["selection"] = new JsonObject
        {
            ["state"] = "unknown",
            ["text"] = null,
            ["ref"] = null,
            ["reason"] = "active-reading-stale",
        };
    }

    private static double? DoubleValue(JsonNode? value)
    {
        if (value is not JsonValue jsonValue)
        {
            return null;
        }
        if (
            jsonValue.TryGetValue(out double number)
            && double.IsFinite(number)
        )
        {
            return number;
        }
        if (jsonValue.TryGetValue(out long integer))
        {
            return integer;
        }
        return null;
    }

    private async Task TryLoadLatestAsync(
        CancellationToken cancellationToken)
    {
        try
        {
            FileInfo info = new(_statePath);
            if (
                !info.Exists
                || info.Length is <= 0 or > MaximumSnapshotBytes
            )
            {
                if (info.Exists)
                {
                    _loadErrors = checked(_loadErrors + 1);
                }
                return;
            }
            await using FileStream stream = new(
                _statePath,
                FileMode.Open,
                FileAccess.Read,
                FileShare.ReadWrite | FileShare.Delete,
                bufferSize: 4096,
                options:
                    FileOptions.Asynchronous
                    | FileOptions.SequentialScan);
            if (stream.Length is <= 0 or > MaximumSnapshotBytes)
            {
                _loadErrors = checked(_loadErrors + 1);
                return;
            }
            using StreamReader reader = new(
                stream,
                Utf8WithoutBom,
                detectEncodingFromByteOrderMarks: false,
                bufferSize: 4096,
                leaveOpen: false);
            string raw = await reader.ReadToEndAsync(
                cancellationToken).ConfigureAwait(false);
            JsonObject? parsed = JsonNode.Parse(raw) as JsonObject;
            if (
                parsed?["schema"]?.GetValue<string>()
                    != FileDirectSnapshotContextAdapter.SnapshotContract
                || LongValue(parsed["revision"]) is not long revision
                || revision < 0
            )
            {
                _loadErrors = checked(_loadErrors + 1);
                return;
            }
            if (revision >= _latestRevision)
            {
                _latestSnapshot = parsed;
                _latestRevision = revision;
                _loadSequence = checked(_loadSequence + 1);
            }
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (
            Exception exception
        ) when (
            exception is IOException
            or UnauthorizedAccessException
            or JsonException
            or InvalidOperationException
        )
        {
            _loadErrors = checked(_loadErrors + 1);
        }
    }

    private static long? LongValue(JsonNode? value)
    {
        if (value is null)
        {
            return null;
        }
        try
        {
            return value.GetValue<long>();
        }
        catch (InvalidOperationException)
        {
            return null;
        }
    }

    private void HandleNotification(string method)
    {
        if (method == "notifications/initialized")
        {
            _initialized = true;
        }
    }

    private bool RequireInitialized() => _initialized;

    internal static bool IsSupportedProtocolVersion(string value) =>
        SupportedProtocolVersions.Contains(value);

    private static JsonObject BuildResult(
        JsonNode id,
        JsonNode result) =>
        new()
        {
            ["jsonrpc"] = "2.0",
            ["id"] = id.DeepClone(),
            ["result"] = result,
        };

    private static JsonObject BuildError(
        JsonNode? id,
        int code,
        string message) =>
        new()
        {
            ["jsonrpc"] = "2.0",
            ["id"] = id?.DeepClone(),
            ["error"] = new JsonObject
            {
                ["code"] = code,
                ["message"] = message,
            },
        };

    private async Task WriteMessageAsync(
        JsonObject message,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        await _output.WriteLineAsync(
            message.ToJsonString(DirectBridgeContract.JsonOptions))
            .ConfigureAwait(false);
        await _output.FlushAsync(cancellationToken)
            .ConfigureAwait(false);
    }

    private static JsonNode? CloneId(JsonElement root)
    {
        if (
            root.ValueKind != JsonValueKind.Object
            || !root.TryGetProperty("id", out JsonElement id)
            || id.ValueKind == JsonValueKind.Null
        )
        {
            return null;
        }
        if (
            id.ValueKind is not (
                JsonValueKind.String
                or JsonValueKind.Number)
        )
        {
            return null;
        }
        return JsonNode.Parse(id.GetRawText());
    }
}

internal sealed class ReaderContextMcpHttpEndpoint
{
    internal const string Path = "/mcp";

    private const string JsonContentType =
        "application/json; charset=utf-8";
    private readonly ReaderContextMcpServer _server;
    private readonly int _listenPort;

    internal string InstanceId => _server.InstanceId;

    internal ReaderContextMcpHttpEndpoint(
        ReaderContextMcpServer server,
        int listenPort)
    {
        ArgumentNullException.ThrowIfNull(server);
        if (listenPort is < 1 or > 65535)
        {
            throw new ArgumentOutOfRangeException(nameof(listenPort));
        }
        _server = server;
        _listenPort = listenPort;
    }

    internal async Task HandleAsync(HttpContext context)
    {
        if (!IsLocalRequest(context))
        {
            context.Response.StatusCode = StatusCodes.Status403Forbidden;
            return;
        }
        if (!HttpMethods.IsPost(context.Request.Method))
        {
            context.Response.StatusCode =
                StatusCodes.Status405MethodNotAllowed;
            context.Response.Headers.Allow = HttpMethods.Post;
            return;
        }
        if (!HasJsonContentType(context.Request.ContentType))
        {
            context.Response.StatusCode =
                StatusCodes.Status415UnsupportedMediaType;
            return;
        }
        Microsoft.Extensions.Primitives.StringValues protocolVersions =
            context.Request.Headers["MCP-Protocol-Version"];
        if (
            protocolVersions.Count > 1
            || (
                protocolVersions.Count == 1
                && !ReaderContextMcpServer.IsSupportedProtocolVersion(
                    protocolVersions[0] ?? "")
            )
        )
        {
            await WriteJsonAsync(
                context,
                StatusCodes.Status400BadRequest,
                ProtocolError(
                    -32000,
                    "Bad Request: Unsupported MCP protocol version"))
                .ConfigureAwait(false);
            return;
        }
        if (
            context.Request.ContentLength is long contentLength
            && (
                contentLength < 0
                || contentLength
                    > DirectBridgeContract.MaximumMessageBytes
            )
        )
        {
            context.Response.StatusCode =
                StatusCodes.Status413PayloadTooLarge;
            return;
        }

        context.Response.Headers.CacheControl = "no-store, max-age=0";
        context.Response.Headers.Pragma = "no-cache";
        context.Response.Headers["Cross-Origin-Resource-Policy"] =
            "same-origin";
        context.Response.Headers["Referrer-Policy"] = "no-referrer";
        context.Response.Headers["X-Content-Type-Options"] = "nosniff";

        string message;
        try
        {
            using StreamReader reader = new(
                context.Request.Body,
                new UTF8Encoding(
                    encoderShouldEmitUTF8Identifier: false,
                    throwOnInvalidBytes: true),
                detectEncodingFromByteOrderMarks: false,
                bufferSize: 4096,
                leaveOpen: true);
            message = await reader.ReadToEndAsync(
                context.RequestAborted).ConfigureAwait(false);
        }
        catch (DecoderFallbackException)
        {
            await WriteJsonAsync(
                context,
                StatusCodes.Status400BadRequest,
                ProtocolError(-32700, "Parse error"))
                .ConfigureAwait(false);
            return;
        }
        if (
            Encoding.UTF8.GetByteCount(message)
                > DirectBridgeContract.MaximumMessageBytes
        )
        {
            context.Response.StatusCode =
                StatusCodes.Status413PayloadTooLarge;
            return;
        }

        JsonObject? response = await _server.ProcessMessageAsync(
            message,
            context.RequestAborted).ConfigureAwait(false);
        if (response is null)
        {
            context.Response.StatusCode = StatusCodes.Status202Accepted;
            context.Response.ContentLength = 0;
            return;
        }
        await WriteJsonAsync(
            context,
            StatusCodes.Status200OK,
            response).ConfigureAwait(false);
    }

    private bool IsLocalRequest(HttpContext context)
    {
        System.Net.IPAddress? remote =
            context.Connection.RemoteIpAddress;
        if (
            remote is null
            || !System.Net.IPAddress.IsLoopback(
                remote.IsIPv4MappedToIPv6
                    ? remote.MapToIPv4()
                    : remote)
            || !string.Equals(
                context.Request.Host.Host,
                DirectBridgeContract.ListenHost,
                StringComparison.Ordinal)
            || context.Request.Host.Port != _listenPort
            || HasForwardingHeaders(context)
        )
        {
            return false;
        }
        Microsoft.Extensions.Primitives.StringValues origins =
            context.Request.Headers.Origin;
        return origins.Count == 0
            || (
                origins.Count == 1
                && string.Equals(
                    origins[0],
                    $"http://{DirectBridgeContract.ListenHost}:"
                        + _listenPort,
                    StringComparison.Ordinal)
            );
    }

    private static bool HasJsonContentType(string? contentType)
    {
        if (string.IsNullOrWhiteSpace(contentType))
        {
            return false;
        }
        int delimiter = contentType.IndexOf(';');
        string mediaType = delimiter < 0
            ? contentType
            : contentType[..delimiter];
        return string.Equals(
            mediaType.Trim(),
            "application/json",
            StringComparison.OrdinalIgnoreCase);
    }

    private static bool HasForwardingHeaders(HttpContext context) =>
        context.Request.Headers.ContainsKey("Forwarded")
        || context.Request.Headers.ContainsKey("X-Forwarded-For")
        || context.Request.Headers.ContainsKey("X-Forwarded-Host")
        || context.Request.Headers.ContainsKey("X-Forwarded-Proto")
        || context.Request.Headers.ContainsKey("X-Real-IP");

    private static JsonObject ProtocolError(
        int code,
        string message) =>
        new()
        {
            ["jsonrpc"] = "2.0",
            ["id"] = null,
            ["error"] = new JsonObject
            {
                ["code"] = code,
                ["message"] = message,
            },
        };

    private static async Task WriteJsonAsync(
        HttpContext context,
        int statusCode,
        JsonObject payload)
    {
        byte[] encoded = Encoding.UTF8.GetBytes(
            payload.ToJsonString(DirectBridgeContract.JsonOptions));
        context.Response.StatusCode = statusCode;
        context.Response.ContentType = JsonContentType;
        context.Response.ContentLength = encoded.Length;
        await context.Response.Body.WriteAsync(
            encoded,
            context.RequestAborted).ConfigureAwait(false);
    }
}
