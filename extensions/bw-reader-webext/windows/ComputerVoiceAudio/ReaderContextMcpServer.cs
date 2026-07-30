using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Microsoft.AspNetCore.Http;

namespace BwReader.ComputerVoiceAudio;

internal sealed class ReaderContextMcpServer
{
    internal const string ToolName = "reader_context_snapshot";
    internal const string DrawingImageToolName =
        "reader_drawing_image";
    internal const string ResultToolName = "reader_result_present";
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
    private const int MaximumResultItems = 20;
    private const int MaximumResultTextCharacters = 2000;
    private static readonly Regex CorrelationPattern = new(
        "^[A-Za-z0-9._:-]{1,40}$",
        RegexOptions.CultureInvariant);
    private static readonly HashSet<string> ResultKinds =
        new(StringComparer.Ordinal)
        {
            "weather",
            "news",
            "images",
            "videos",
            "fact",
            "general",
            "cards",
        };
    private static readonly UTF8Encoding Utf8WithoutBom = new(
        encoderShouldEmitUTF8Identifier: false);

    private readonly string _statePath;
    private readonly TextReader _input;
    private readonly TextWriter _output;
    private readonly Func<DateTimeOffset> _utcNow;
    private readonly string _instanceId;
    private readonly string _startedAt;
    private readonly Func<
        ReaderResultDeliveryRequest,
        CancellationToken,
        Task<ReaderResultDeliveryAck>>? _deliverResultAsync;
    private readonly Func<
        ReaderVisualDeliveryRequest,
        CancellationToken,
        Task<ReaderVisualCapture?>>? _fetchVisualAsync;
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
        string? instanceId = null,
        Func<
            ReaderResultDeliveryRequest,
            CancellationToken,
            Task<ReaderResultDeliveryAck>>? deliverResultAsync = null,
        Func<
            ReaderVisualDeliveryRequest,
            CancellationToken,
            Task<ReaderVisualCapture?>>? fetchVisualAsync = null)
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
        _deliverResultAsync = deliverResultAsync;
        _fetchVisualAsync = fetchVisualAsync;
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
                ["instructions"] = _deliverResultAsync is null
                    ? "Use reader_context_snapshot only when the user asks "
                        + "about the current Reader page or selection. Respect "
                        + "contextStatus; pending or stale means the current "
                        + "page text is unavailable. The snapshot is text-only; "
                        + "when it names reader_drawing_image, call that "
                        + "separate read-only tool only if the user's request "
                        + "requires the current drawing."
                    : "Use reader_context_snapshot when the user asks about "
                        + "the current Reader page or selection. The snapshot "
                        + "is text-only; use reader_drawing_image separately "
                        + "only when the user's request requires the current "
                        + "drawing. During a "
                        + "Reader voice session, after obtaining weather, "
                        + "news, images or videos, or after preparing display "
                        + "cards, call reader_result_present exactly once so "
                        + "the completed result appears in the Reader sidebar.",
            });
    }

    private JsonObject BuildToolList()
    {
        JsonArray tools =
        [
            new JsonObject
            {
                ["name"] = ToolName,
                ["description"] =
                    "Read the newest Windows-local Reader page and "
                    + "selection snapshot as text only. The tool never "
                    + "captures or returns an image. "
                    + "Check contextStatus before using currentPage; "
                    + "never reuse text when it is pending or stale. "
                    + "For a current drawing, lastEditedAgeSec is computed "
                    + "from a Windows-local receive-time anchor and "
                    + "drawingImageTool names the separate image tool.",
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
        ];
        if (_fetchVisualAsync is not null)
        {
            tools.Add(new JsonObject
            {
                ["name"] = DrawingImageToolName,
                ["description"] =
                    "Return the current Reader drawing as the existing "
                    + "Reader-composited JPEG. This parameterless read-only "
                    + "tool captures only when the Windows snapshot is ready "
                    + "and the current page has a stable, non-empty drawing. "
                    + "File, page and drawing revision must still match after "
                    + "capture; otherwise no image is returned.",
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
            });
        }
        if (_deliverResultAsync is not null)
        {
            tools.Add(new JsonObject
            {
                ["name"] = ResultToolName,
                ["description"] =
                    "Present one completed structured result in the current "
                    + "Reader sidebar over the authenticated Windows-to-Reader "
                    + "connection. Windows derives the current book/page "
                    + "anchor; do not include an anchor. In Reader voice "
                    + "sessions, use this after weather, news, image, video "
                    + "or flashcard work so the result is visible as a card.",
                ["inputSchema"] = new JsonObject
                {
                    ["type"] = "object",
                    ["additionalProperties"] = false,
                    ["required"] = new JsonArray(
                        "correlation",
                        "kind",
                        "payload"),
                    ["properties"] = new JsonObject
                    {
                        ["correlation"] = new JsonObject
                        {
                            ["type"] = "string",
                            ["pattern"] =
                                "^[A-Za-z0-9._:-]{1,40}$",
                        },
                        ["kind"] = new JsonObject
                        {
                            ["type"] = "string",
                            ["enum"] = new JsonArray(
                                "weather",
                                "news",
                                "images",
                                "videos",
                                "fact",
                                "general",
                                "cards"),
                        },
                        ["payload"] = new JsonObject
                        {
                            ["type"] = "object",
                        },
                        ["title"] = new JsonObject
                        {
                            ["type"] = "string",
                            ["maxLength"] =
                                MaximumResultTextCharacters,
                        },
                        ["brief"] = new JsonObject
                        {
                            ["type"] = "string",
                            ["maxLength"] =
                                MaximumResultTextCharacters,
                        },
                        ["sources"] = new JsonObject
                        {
                            ["type"] = "array",
                            ["minItems"] = 1,
                            ["maxItems"] = 5,
                        },
                    },
                },
                ["annotations"] = new JsonObject
                {
                    ["readOnlyHint"] = false,
                    ["destructiveHint"] = false,
                    ["idempotentHint"] = true,
                    ["openWorldHint"] = false,
                },
            });
        }
        return new JsonObject
        {
            ["tools"] = tools,
        };
    }

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
        )
        {
            return BuildError(
                id,
                -32602,
                "Invalid tool call");
        }

        string toolName = nameValue.GetString()!;
        JsonElement arguments = parameters.TryGetProperty(
            "arguments",
            out JsonElement argumentValue)
            ? argumentValue
            : default;
        _callSequence = checked(_callSequence + 1);
        if (toolName == ToolName)
        {
            if (!HasNoArguments(arguments))
            {
                return BuildError(
                    id,
                    -32602,
                    "Invalid tool call");
            }
            await TryLoadLatestAsync(cancellationToken)
                .ConfigureAwait(false);
            return BuildSnapshotToolResult(
                id,
                BuildToolPayload());
        }
        if (
            toolName == DrawingImageToolName
            && _fetchVisualAsync is not null
        )
        {
            if (!HasNoArguments(arguments))
            {
                return BuildError(
                    id,
                    -32602,
                    "Invalid tool call");
            }
            await TryLoadLatestAsync(cancellationToken)
                .ConfigureAwait(false);
            JsonObject payload = BuildToolPayload();
            ReaderVisualDeliveryRequest? visualRequest =
                BuildVisualRequest(payload);
            if (visualRequest is null)
            {
                return BuildDrawingImageError(
                    id,
                    "no-current-stable-drawing");
            }
            ReaderVisualCapture? visual;
            try
            {
                visual = await _fetchVisualAsync(
                    visualRequest,
                    cancellationToken).ConfigureAwait(false);
            }
            catch (ReaderVisualDeliveryException)
            {
                return BuildDrawingImageError(
                    id,
                    "drawing-image-unavailable");
            }
            await TryLoadLatestAsync(cancellationToken)
                .ConfigureAwait(false);
            payload = BuildToolPayload();
            if (!VisualRequestStillCurrent(
                payload,
                visualRequest))
            {
                return BuildDrawingImageError(
                    id,
                    "drawing-revision-superseded");
            }
            if (
                visual is null
                || visual.MimeType
                    != ReaderVisualDeliveryProtocol.MimeType
                || visual.Data.Length == 0
            )
            {
                return BuildDrawingImageError(
                    id,
                    "drawing-image-unavailable");
            }
            return BuildDrawingImageResult(
                id,
                visualRequest,
                visual);
        }
        if (
            toolName != ResultToolName
            || _deliverResultAsync is null
            || arguments.ValueKind != JsonValueKind.Object
        )
        {
            return BuildError(
                id,
                -32602,
                "Invalid tool call");
        }

        try
        {
            await TryLoadLatestAsync(cancellationToken)
                .ConfigureAwait(false);
            ReaderResultDeliveryRequest request =
                BuildReaderResultRequest(arguments);
            ReaderResultDeliveryAck ack =
                await _deliverResultAsync(
                    request,
                    cancellationToken).ConfigureAwait(false);
            JsonObject payload = new()
            {
                ["ok"] = ack.Outcome
                    != ReaderResultDeliveryProtocol.RejectedOutcome,
                ["contract"] =
                    ReaderResultDeliveryProtocol.DeliveryContract,
                ["correlation"] = ack.Correlation,
                ["outcome"] = ack.Outcome,
                ["anchor"] = request.Anchor.DeepClone(),
            };
            if (ack.Error is not null)
            {
                payload["error"] = ack.Error;
            }
            return BuildToolResult(
                id,
                payload,
                isError: ack.Outcome
                    == ReaderResultDeliveryProtocol.RejectedOutcome);
        }
        catch (
            Exception exception
        ) when (
            exception is ArgumentException
            or InvalidOperationException
        )
        {
            return BuildError(
                id,
                -32602,
                exception.Message);
        }
        catch (ReaderResultDeliveryException exception)
        {
            return BuildToolResult(
                id,
                new JsonObject
                {
                    ["ok"] = false,
                    ["contract"] =
                        ReaderResultDeliveryProtocol.DeliveryContract,
                    ["code"] = exception.Code,
                    ["error"] = exception.Message,
                    ["retryable"] = exception.Retryable,
                },
                isError: true);
        }
    }

    private static JsonObject BuildToolResult(
        JsonNode id,
        JsonObject payload,
        bool isError = false) =>
        BuildToolResult(id, payload, visual: null, isError);

    private static JsonObject BuildSnapshotToolResult(
        JsonNode id,
        JsonObject payload) =>
        BuildResult(
            id,
            new JsonObject
            {
                ["content"] = new JsonArray
                {
                    new JsonObject
                    {
                        ["type"] = "text",
                        ["text"] =
                            BuildAssistantContext(payload),
                    },
                    new JsonObject
                    {
                        ["type"] = "text",
                        ["text"] = payload.ToJsonString(
                            DirectBridgeContract.JsonOptions),
                    },
                },
            });

    private static JsonObject BuildToolResult(
        JsonNode id,
        JsonObject payload,
        ReaderVisualCapture? visual,
        bool isError = false)
    {
        JsonArray content =
        [
            new JsonObject
            {
                ["type"] = "text",
                ["text"] = payload.ToJsonString(
                    DirectBridgeContract.JsonOptions),
            },
        ];
        if (visual is not null)
        {
            content.Add(new JsonObject
            {
                ["type"] = "image",
                ["data"] = Convert.ToBase64String(visual.Data),
                ["mimeType"] = visual.MimeType,
                ["_meta"] = new JsonObject
                {
                    ["codex/imageDetail"] = "original",
                },
            });
        }
        JsonObject result = new()
        {
            ["content"] = content,
        };
        if (isError)
        {
            result["isError"] = true;
        }
        return BuildResult(id, result);
    }

    private static JsonObject BuildDrawingImageResult(
        JsonNode id,
        ReaderVisualDeliveryRequest request,
        ReaderVisualCapture visual)
    {
        JsonObject identity = new()
        {
            ["file"] = request.File,
            ["page"] = request.Page.DeepClone(),
            ["drawingRevision"] = request.DrawingRevision,
        };
        return BuildToolResult(
            id,
            identity,
            visual);
    }

    private static JsonObject BuildDrawingImageError(
        JsonNode id,
        string reason) =>
        BuildToolResult(
            id,
            new JsonObject
            {
                ["imageAvailable"] = false,
                ["reason"] = reason,
            },
            isError: true);

    private static bool HasNoArguments(JsonElement arguments) =>
        arguments.ValueKind == JsonValueKind.Undefined
        || (
            arguments.ValueKind == JsonValueKind.Object
            && !arguments.EnumerateObject().Any()
        );

    internal static string BuildAssistantContext(JsonObject payload)
    {
        StringBuilder text = new();
        text.AppendLine(
            "Reader assistant context (silent state: do not reply merely "
            + "because this context was read).");
        JsonObject? page = payload["currentPage"] as JsonObject;
        JsonObject? active = payload["activeReading"] as JsonObject;
        string? file = page?["file"]?.GetValue<string>()
            ?? active?["file"]?.GetValue<string>();
        string? title = page?["title"]?.GetValue<string>()
            ?? active?["title"]?.GetValue<string>();
        JsonNode? pageNumber = page?["page"]
            ?? active?["page"];
        text.Append("Location: ");
        if (string.IsNullOrWhiteSpace(file) && pageNumber is null)
        {
            text.AppendLine("no current Reader page.");
        }
        else
        {
            if (!string.IsNullOrWhiteSpace(title))
            {
                text.Append(title);
                text.Append(" — ");
            }
            text.Append(file ?? "unknown file");
            text.Append(", page ");
            text.AppendLine(
                pageNumber?.ToJsonString() ?? "unknown");
        }

        if (
            payload["selection"] is JsonObject selection
            && selection["state"]?.GetValue<string>() == "active"
            && selection["text"]?.GetValue<string>()
                is string selectionText
            && !string.IsNullOrWhiteSpace(selectionText)
        )
        {
            text.AppendLine("Active selection:");
            text.AppendLine(selectionText);
        }

        bool pageReady =
            payload["contextStatus"]?.GetValue<string>() == "ready"
            && page?["stable"]?.GetValue<bool?>() == true;
        string pageText = page?["text"]?.GetValue<string>() ?? "";
        bool textAvailable =
            pageReady
            && page?["textAvailable"]?.GetValue<bool?>() == true
            && !string.IsNullOrWhiteSpace(pageText);
        text.AppendLine("Page text:");
        text.AppendLine(
            textAvailable
                ? pageText
                : $"unavailable (contextStatus="
                    + (
                        payload["contextStatus"]?.GetValue<string>()
                        ?? "unknown"
                    )
                    + ").");

        JsonObject? drawing = page?["visual"]?["drawing"]
            as JsonObject;
        text.Append("Drawing: ");
        if (
            drawing is null
            || drawing["empty"]?.GetValue<bool?>() == true
        )
        {
            text.AppendLine("none.");
        }
        else
        {
            bool stable =
                drawing["stable"]?.GetValue<bool?>() == true;
            bool inProgress =
                drawing["inProgress"]?.GetValue<bool?>() == true;
            if (!stable || inProgress)
            {
                text.Append("waiting/not yet available, ");
            }
            text.Append("stable=");
            text.Append(
                stable.ToString().ToLowerInvariant());
            text.Append(", inProgress=");
            text.Append(
                inProgress.ToString().ToLowerInvariant());
            text.Append(", lastEditedAgeSec=");
            text.AppendLine(
                drawing["lastEditedAgeSec"]?.ToJsonString()
                ?? "unknown");
        }
        string? drawingTool =
            payload["drawingImageTool"]?.GetValue<string>();
        if (
            !string.IsNullOrWhiteSpace(drawingTool)
            && TryGetVisualIdentity(
                payload,
                out _,
                out _,
                out _)
        )
        {
            text.Append(
                "Drawing metadata does not reveal ink content. "
                + "For a user question about the current drawing, call ");
            text.Append(drawingTool);
            text.AppendLine(
                "; do not request an image for ordinary page or selection "
                + "questions.");
        }
        return text.ToString().TrimEnd();
    }

    private ReaderResultDeliveryRequest BuildReaderResultRequest(
        JsonElement arguments)
    {
        RequireExactFields(
            arguments,
            ["correlation", "kind", "payload"],
            ["title", "brief", "sources"],
            "reader_result_present");
        string correlation = RequireResultString(
            arguments,
            "correlation",
            40,
            required: true);
        if (!CorrelationPattern.IsMatch(correlation))
        {
            throw new ArgumentException(
                "correlation 必须匹配 [A-Za-z0-9._:-]{1,40}");
        }
        string kind = RequireResultString(
            arguments,
            "kind",
            32,
            required: true);
        if (!ResultKinds.Contains(kind))
        {
            throw new ArgumentException(
                "kind 不受支持");
        }
        JsonElement rawPayload = arguments.GetProperty("payload");
        if (rawPayload.ValueKind != JsonValueKind.Object)
        {
            throw new ArgumentException("payload 必须是对象");
        }
        if (
            Encoding.UTF8.GetByteCount(arguments.GetRawText())
                > DirectBridgeContract.MaximumMessageBytes - 4096
        )
        {
            throw new ArgumentException("Reader 结果超过直连大小上限");
        }
        JsonObject anchor = CurrentResultAnchor();
        JsonArray parts = kind == "cards"
            ? BuildCardsPart(arguments, rawPayload)
            : BuildCardPart(arguments, rawPayload, kind);
        return new ReaderResultDeliveryRequest(
            correlation,
            anchor,
            parts);
    }

    private JsonObject CurrentResultAnchor()
    {
        JsonObject snapshot = _latestSnapshot?.DeepClone()
            as JsonObject
            ?? throw new InvalidOperationException(
                "Reader 当前页快照尚未到达");
        ApplyFreshness(snapshot, _utcNow());
        if (
            snapshot["contextStatus"]?.GetValue<string>() != "ready"
            || snapshot["currentPage"] is not JsonObject currentPage
            || currentPage["stable"]?.GetValue<bool?>() != true
            || currentPage["file"]?.GetValue<string>()
                is not string file
            || string.IsNullOrWhiteSpace(file)
            || LongValue(currentPage["page"]) is not long page
            || page is < 1 or > int.MaxValue
        )
        {
            throw new InvalidOperationException(
                "Reader 当前页不是可锚定的 ready 稳定页");
        }
        RequireSafeRelativeFile(file);
        return new JsonObject
        {
            ["file"] = file,
            ["page"] = page,
        };
    }

    private static JsonArray BuildCardPart(
        JsonElement arguments,
        JsonElement payload,
        string kind)
    {
        JsonObject card = new()
        {
            ["kind"] = kind,
            // The PWA validates the renderer-specific payload before touching
            // the UI. Windows owns the narrow envelope/anchor mapping and the
            // bounded ACK, so it need not duplicate that renderer contract.
            ["data"] = CloneObject(payload, $"payload({kind})"),
        };
        foreach (string field in new[] { "title", "brief" })
        {
            if (arguments.TryGetProperty(field, out _))
            {
                card[field] = RequireResultString(
                    arguments,
                    field,
                    MaximumResultTextCharacters,
                    required: false);
            }
        }
        if (
            arguments.TryGetProperty(
                "sources",
                out JsonElement sources)
        )
        {
            if (
                sources.ValueKind != JsonValueKind.Array
                || sources.GetArrayLength() is < 1 or > 5
            )
            {
                throw new ArgumentException("sources 数量无效");
            }
            card["sources"] = JsonNode.Parse(
                sources.GetRawText());
        }
        return new JsonArray
        {
            new JsonObject
            {
                ["kind"] = "card",
                ["card"] = card,
            },
        };
    }

    private static JsonArray BuildCardsPart(
        JsonElement arguments,
        JsonElement payload)
    {
        if (
            arguments.TryGetProperty("title", out _)
            || arguments.TryGetProperty("brief", out _)
            || arguments.TryGetProperty("sources", out _)
        )
        {
            throw new ArgumentException(
                "kind=cards 不支持 title/brief/sources");
        }
        RequireExactFields(
            payload,
            ["cards"],
            ["draft"],
            "payload(cards)");
        if (
            payload.TryGetProperty("draft", out JsonElement draft)
            && (draft.ValueKind != JsonValueKind.True)
        )
        {
            throw new ArgumentException(
                "payload(cards).draft 当前只能省略或为 true");
        }
        JsonElement rawCards = payload.GetProperty("cards");
        if (
            rawCards.ValueKind != JsonValueKind.Array
            || rawCards.GetArrayLength() is < 1
                or > MaximumResultItems
        )
        {
            throw new ArgumentException(
                "payload(cards).cards 数量无效");
        }
        JsonArray cards = [];
        int index = 0;
        foreach (JsonElement rawCard in rawCards.EnumerateArray())
        {
            JsonObject card = CloneObject(
                rawCard,
                $"payload(cards).cards[{index}]");
            string? cardType = card["type"]?.GetValue<string>();
            string? front = card["front"]?.GetValue<string>();
            string? cloze = card["cloze"]?.GetValue<string>()
                ?? card["text"]?.GetValue<string>();
            cardType ??= !string.IsNullOrWhiteSpace(front)
                ? "basic"
                : "cloze";
            if (
                cardType is not ("basic" or "cloze")
                || (
                    cardType == "basic"
                    && string.IsNullOrWhiteSpace(front)
                )
                || (
                    cardType == "cloze"
                    && string.IsNullOrWhiteSpace(cloze)
                )
            )
            {
                throw new ArgumentException(
                    $"payload(cards).cards[{index}] 无效");
            }
            foreach (
                KeyValuePair<string, JsonNode?> field in card
            )
            {
                if (
                    field.Key
                        is not (
                            "type"
                            or "front"
                            or "back"
                            or "cloze"
                            or "text")
                    || field.Value is not JsonValue jsonValue
                    || !jsonValue.TryGetValue(out string? text)
                    || text.Length > MaximumResultTextCharacters
                )
                {
                    throw new ArgumentException(
                        $"payload(cards).cards[{index}] 字段无效");
                }
            }
            card["type"] = cardType;
            cards.Add(card);
            index += 1;
        }
        return new JsonArray
        {
            new JsonObject
            {
                ["kind"] = "cards",
                ["cards"] = cards,
                ["draft"] = true,
            },
        };
    }

    private static string RequireResultString(
        JsonElement value,
        string field,
        int maximum,
        bool required)
    {
        if (
            !value.TryGetProperty(field, out JsonElement property)
            || property.ValueKind != JsonValueKind.String
            || property.GetString() is not string text
            || text.Length > maximum
            || (required && string.IsNullOrWhiteSpace(text))
        )
        {
            throw new ArgumentException(
                $"{field} 字段无效");
        }
        return text;
    }

    private static void RequireExactFields(
        JsonElement value,
        IEnumerable<string> required,
        IEnumerable<string> optional,
        string label)
    {
        if (value.ValueKind != JsonValueKind.Object)
        {
            throw new ArgumentException($"{label} 必须是对象");
        }
        HashSet<string> requiredSet = required.ToHashSet(
            StringComparer.Ordinal);
        HashSet<string> allowed = requiredSet.Concat(optional)
            .ToHashSet(StringComparer.Ordinal);
        HashSet<string> actual = value.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        if (!requiredSet.IsSubsetOf(actual) || !actual.IsSubsetOf(allowed))
        {
            throw new ArgumentException(
                $"{label} 字段不匹配");
        }
    }

    private static void RequireSafeRelativeFile(string file)
    {
        if (
            file != file.Trim()
            || Path.IsPathRooted(file)
            || Path.IsPathFullyQualified(file)
            || file.Contains('\0')
            || file.Contains(':')
            || file.Split(
                ['/', '\\'],
                StringSplitOptions.RemoveEmptyEntries)
                .Any(part => part == "..")
        )
        {
            throw new InvalidOperationException(
                "Reader 当前页 file 不是安全相对路径");
        }
    }

    private static JsonObject CloneObject(
        JsonElement value,
        string label) =>
        JsonNode.Parse(value.GetRawText()) as JsonObject
        ?? throw new ArgumentException($"{label} 必须是对象");

    private JsonObject BuildToolPayload()
    {
        JsonObject? snapshot = _latestSnapshot?.DeepClone()
            as JsonObject;
        if (snapshot is not null)
        {
            ApplyFreshness(snapshot, _utcNow());
            snapshot["drawingImageTool"] =
                _fetchVisualAsync is null
                    ? null
                    : DrawingImageToolName;
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
            ["drawingImageTool"] =
                _fetchVisualAsync is null
                    ? null
                    : DrawingImageToolName,
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

    private static ReaderVisualDeliveryRequest? BuildVisualRequest(
        JsonObject payload)
    {
        if (!TryGetVisualIdentity(
            payload,
            out string? file,
            out JsonNode? page,
            out string? revision))
        {
            return null;
        }
        return new ReaderVisualDeliveryRequest(
            "visual-" + Guid.NewGuid().ToString("N"),
            file!,
            page!.DeepClone(),
            revision!);
    }

    private static bool VisualRequestStillCurrent(
        JsonObject payload,
        ReaderVisualDeliveryRequest request)
    {
        return TryGetVisualIdentity(
                payload,
                out string? file,
                out JsonNode? page,
                out string? revision)
            && file == request.File
            && JsonNode.DeepEquals(page, request.Page)
            && revision == request.DrawingRevision;
    }

    private static bool TryGetVisualIdentity(
        JsonObject payload,
        out string? file,
        out JsonNode? page,
        out string? revision)
    {
        file = null;
        page = null;
        revision = null;
        if (
            payload["contextStatus"]?.GetValue<string>() != "ready"
            || payload["currentPage"] is not JsonObject currentPage
            || currentPage["stable"]?.GetValue<bool?>() != true
            || currentPage["file"]?.GetValue<string>()
                is not string currentFile
            || string.IsNullOrWhiteSpace(currentFile)
            || currentPage["page"] is not JsonNode currentPageNumber
            || currentPage["visual"] is not JsonObject visual
            || visual["has_ink"]?.GetValue<bool?>() != true
            || visual["drawing"] is not JsonObject drawing
            || drawing["stable"]?.GetValue<bool?>() != true
            || drawing["inProgress"]?.GetValue<bool?>() != false
            || drawing["empty"]?.GetValue<bool?>() != false
            || drawing["drawingRevision"]?.GetValue<string>()
                is not string currentRevision
            || drawing["file"]?.GetValue<string>() != currentFile
            || !JsonNode.DeepEquals(
                drawing["page"],
                currentPageNumber)
            || drawing["ref"] is not JsonObject reference
            || reference["kind"]?.GetValue<string>() != "drawing"
            || reference["file"]?.GetValue<string>() != currentFile
            || !JsonNode.DeepEquals(
                reference["page"],
                currentPageNumber)
            || reference["revision"]?.GetValue<string>()
                != currentRevision
        )
        {
            return false;
        }
        file = currentFile;
        page = currentPageNumber;
        revision = currentRevision;
        return true;
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
        )
        {
            return;
        }
        if (drawing["empty"]?.GetValue<bool?>() == true)
        {
            drawing["lastEditedAgeSec"] = null;
            drawing.Remove("lastEditedAgeAtReceiveSec");
            drawing.Remove("lastEditedReceivedAtEpochMs");
            return;
        }
        double? lastEditedAt = DoubleValue(
            drawing["lastEditedAt"]);
        double? freshWindow = DoubleValue(
            drawing["freshWindowS"]);
        double? ageAtReceive = DoubleValue(
            drawing["lastEditedAgeAtReceiveSec"]);
        long? ageAnchorAt = LongValue(
            drawing["lastEditedReceivedAtEpochMs"]);
        if (
            (
                ageAtReceive is null
                || ageAtReceive < 0
                || ageAnchorAt is null
                || ageAnchorAt < 1
            )
            && snapshot["activeReading"] is JsonObject active
            && LongValue(active["observedAtEpochMs"])
                is long observedAt
            && LongValue(active["receivedAtEpochMs"])
                is long receivedAt
            && lastEditedAt is not null
        )
        {
            ageAtReceive = Math.Max(
                0,
                (observedAt / 1000.0) - lastEditedAt.Value);
            ageAnchorAt = receivedAt;
        }
        double? age = null;
        if (
            ageAtReceive is not null
            && ageAtReceive >= 0
            && ageAnchorAt is not null
            && ageAnchorAt >= 1
        )
        {
            age = ageAtReceive.Value
                + Math.Max(
                    0,
                    (
                        now.ToUnixTimeMilliseconds()
                        - ageAnchorAt.Value
                    ) / 1000.0);
            drawing["lastEditedAgeSec"] = Math.Round(
                age.Value,
                3,
                MidpointRounding.AwayFromZero);
        }
        else
        {
            drawing["lastEditedAgeSec"] = null;
        }
        drawing.Remove("lastEditedAgeAtReceiveSec");
        drawing.Remove("lastEditedReceivedAtEpochMs");
        if (
            age is null
            || freshWindow is null
            || freshWindow <= 0
        )
        {
            return;
        }
        drawing["freshness"] =
            age.Value <= freshWindow.Value
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
