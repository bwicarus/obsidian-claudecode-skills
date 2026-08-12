using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace BwReader.ComputerVoiceAudio;

internal sealed class ReaderContextMcpServer
{
    internal const string ToolName = "reader_context_snapshot";
    internal const string VisualToolName = "reader_visual_image";
    internal const string BrowserControlToolName = "reader_browser_control";
    internal const string CardToolName = "reader_card";
    internal const string CommandToolName = "reader_command";
    internal const string CapabilityGuideToolName =
        "reader_capability_guide";
    internal const string ServerName = "bw-reader-context-snapshot";
    internal const string ServerVersion = "1.2.0";
    internal static readonly TimeSpan FreshnessWindow =
        TimeSpan.FromMinutes(3);

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
    private readonly ReaderDocumentCorpusStore _documentCorpus;
    private readonly ReaderContextReadLedger _readLedger;
    private readonly ReaderCapabilityCatalog _capabilityCatalog;
    private readonly HashSet<string> _unscopedDocumentReads =
        new(StringComparer.Ordinal);
    private readonly Func<
        ReaderVisualDeliveryRequest,
        CancellationToken,
        Task<ReaderVisualCapture?>>? _fetchVisualAsync;
    private readonly Func<
        ReaderBrowserControlRequest,
        CancellationToken,
        Task<ReaderBrowserControlResponse>>? _controlBrowserAsync;
    private readonly Func<
        ReaderRealtimeOutputRequest,
        CancellationToken,
        Task<ReaderRealtimeOutputAck>>? _sendOutputAsync;
    private JsonObject? _latestSnapshot;
    private long _latestRevision = -1;
    private string? _latestProducerInstanceId;
    private long _loadSequence;
    private long _loadErrors;
    private long _callSequence;
    private bool _initialized;

    internal ReaderContextMcpServer(
        string statePath,
        TextReader input,
        TextWriter output,
        Func<DateTimeOffset>? utcNow = null,
        string? instanceId = null,
        Func<
            ReaderVisualDeliveryRequest,
            CancellationToken,
            Task<ReaderVisualCapture?>>? fetchVisualAsync = null,
        Func<
            ReaderBrowserControlRequest,
            CancellationToken,
            Task<ReaderBrowserControlResponse>>? controlBrowserAsync = null,
        Func<
            ReaderRealtimeOutputRequest,
            CancellationToken,
            Task<ReaderRealtimeOutputAck>>? sendOutputAsync = null,
        ReaderCapabilityCatalog? capabilityCatalog = null)
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
        _fetchVisualAsync = fetchVisualAsync;
        _controlBrowserAsync = controlBrowserAsync;
        _sendOutputAsync = sendOutputAsync;
        _capabilityCatalog = capabilityCatalog ?? new ReaderCapabilityCatalog();
        _startedAt = _utcNow().ToString("O");
        string directory = Path.GetDirectoryName(_statePath)
            ?? throw new ArgumentException(
                "snapshot state directory is invalid",
                nameof(statePath));
        _documentCorpus = new ReaderDocumentCorpusStore(
            Path.Combine(
                directory,
                ReaderDocumentCorpusStore.CorpusFileName),
            _utcNow);
        _readLedger = new ReaderContextReadLedger(
            Path.Combine(
                directory,
                ReaderContextReadLedger.LedgerFileName),
            _utcNow);
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
                await WriteErrorAsync(
                    id: null,
                    code: -32700,
                    message: "Invalid JSON-RPC message",
                    cancellationToken).ConfigureAwait(false);
                continue;
            }
            await HandleLineAsync(line, cancellationToken)
                .ConfigureAwait(false);
        }
        return 0;
    }

    private async Task HandleLineAsync(
        string line,
        CancellationToken cancellationToken)
    {
        JsonDocument document;
        try
        {
            document = JsonDocument.Parse(
                line,
                new JsonDocumentOptions
                {
                    AllowTrailingCommas = false,
                    CommentHandling = JsonCommentHandling.Disallow,
                    MaxDepth = 32,
                });
        }
        catch (JsonException)
        {
            await WriteErrorAsync(
                id: null,
                code: -32700,
                message: "Parse error",
                cancellationToken).ConfigureAwait(false);
            return;
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
                await WriteErrorAsync(
                    CloneId(root),
                    -32600,
                    "Invalid Request",
                    cancellationToken).ConfigureAwait(false);
                return;
            }

            string method = methodValue.GetString()!;
            JsonNode? id = CloneId(root);
            bool notification = id is null;
            if (notification)
            {
                HandleNotification(method);
                return;
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
                    await HandleInitializeAsync(
                        requestId,
                        parameters,
                        cancellationToken).ConfigureAwait(false);
                    return;
                case "ping":
                    await WriteResultAsync(
                        requestId,
                        new JsonObject(),
                        cancellationToken).ConfigureAwait(false);
                    return;
                case "tools/list":
                    if (!RequireInitialized())
                    {
                        await WriteErrorAsync(
                            id,
                            -32002,
                            "Server not initialized",
                            cancellationToken).ConfigureAwait(false);
                        return;
                    }
                    await WriteResultAsync(
                        requestId,
                        BuildToolList(),
                        cancellationToken).ConfigureAwait(false);
                    return;
                case "tools/call":
                    if (!RequireInitialized())
                    {
                        await WriteErrorAsync(
                            id,
                            -32002,
                            "Server not initialized",
                            cancellationToken).ConfigureAwait(false);
                        return;
                    }
                    await HandleToolCallAsync(
                        requestId,
                        parameters,
                        cancellationToken).ConfigureAwait(false);
                    return;
                case "resources/list":
                    if (!RequireInitialized())
                    {
                        await WriteErrorAsync(
                            id,
                            -32002,
                            "Server not initialized",
                            cancellationToken).ConfigureAwait(false);
                        return;
                    }
                    await WriteResultAsync(
                        requestId,
                        _capabilityCatalog.List(),
                        cancellationToken).ConfigureAwait(false);
                    return;
                case "resources/read":
                    if (!RequireInitialized())
                    {
                        await WriteErrorAsync(
                            id,
                            -32002,
                            "Server not initialized",
                            cancellationToken).ConfigureAwait(false);
                        return;
                    }
                    if (!TryReadResourceUri(parameters, out string resourceUri))
                    {
                        await WriteErrorAsync(
                            id,
                            -32602,
                            "Invalid resource read",
                            cancellationToken).ConfigureAwait(false);
                        return;
                    }
                    try
                    {
                        await WriteResultAsync(
                            requestId,
                            await _capabilityCatalog.ReadAsync(
                                resourceUri,
                                cancellationToken).ConfigureAwait(false),
                            cancellationToken).ConfigureAwait(false);
                    }
                    catch (Exception exception) when (
                        exception is IOException
                        or UnauthorizedAccessException
                        or KeyNotFoundException
                        or DecoderFallbackException)
                    {
                        await WriteErrorAsync(
                            id,
                            -32004,
                            "Reader capability resource unavailable",
                            cancellationToken).ConfigureAwait(false);
                    }
                    return;
                default:
                    await WriteErrorAsync(
                        id,
                        -32601,
                        "Method not found",
                        cancellationToken).ConfigureAwait(false);
                    return;
            }
        }
    }

    private async Task HandleInitializeAsync(
        JsonNode id,
        JsonElement parameters,
        CancellationToken cancellationToken)
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
            await WriteErrorAsync(
                id,
                -32602,
                "Invalid initialize parameters",
                cancellationToken).ConfigureAwait(false);
            return;
        }
        _initialized = true;
        await WriteResultAsync(
            id,
            new JsonObject
            {
                ["protocolVersion"] = protocolValue.GetString(),
                ["capabilities"] = new JsonObject
                {
                    ["tools"] = new JsonObject
                    {
                        ["listChanged"] = false,
                    },
                    ["resources"] = new JsonObject
                    {
                        ["subscribe"] = false,
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
                    + "page text is unavailable. page_image being null does "
                    + "not mean the App image is unavailable: when "
                    + "visualAccess.available is true, call the named "
                    + "reader_visual_image tool to receive a fresh inline "
                    + "composite image. For a complex Reader task, use "
                    + CapabilityGuideToolName
                    + " to read exactly one task guide; read topic=index "
                    + "only when the correct topic is unknown. In Windows "
                    + "Codex voice, use native Codex tools and native "
                    + "subagents instead of starting a nested CLI worker. "
                    + "After a native Codex tool produces a structured "
                    + "weather/news/images/videos/fact/general result, call "
                    + CardToolName
                    + " in the same turn to mirror it to the exact App or "
                    + "extension. If a client allowlist hides that dedicated "
                    + "tool, call "
                    + CommandToolName
                    + " with the same typed {card:{kind,title,data}} input. "
                    + "Do not infer a card from final assistant "
                    + "text. Text chat-history synchronization carries only "
                    + "user/assistant text and never carries cards. "
                    + "Keep the existing Realtime and legacy CLI "
                    + "implementations unchanged as compatibility paths.",
            },
            cancellationToken).ConfigureAwait(false);
    }

    private static bool TryReadResourceUri(
        JsonElement parameters,
        out string uri)
    {
        uri = string.Empty;
        if (parameters.ValueKind != JsonValueKind.Object)
        {
            return false;
        }
        try
        {
            DirectJsonValidation.RequireNoDuplicateKeys(parameters);
        }
        catch (DirectProtocolException)
        {
            return false;
        }
        JsonProperty[] fields = parameters.EnumerateObject().ToArray();
        if (
            fields.Length != 1
            || fields[0].Name != "uri"
            || fields[0].Value.ValueKind != JsonValueKind.String
            || fields[0].Value.GetString() is not string value
            || value.Length is < 1 or > 256
            || !value.StartsWith(
                "reader://capabilities/",
                StringComparison.Ordinal)
        )
        {
            return false;
        }
        uri = value;
        return true;
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
                    + "selection snapshot. The tool is read-only. "
                    + "Check contextStatus before using currentPage; "
                    + "never reuse text when it is pending or stale. Read "
                    + "visualAccess to discover whether the exact App "
                    + "surface can be requested on demand; page_image=null "
                    + "alone does not mean that no image is available.",
                ["inputSchema"] = new JsonObject
                {
                    ["type"] = "object",
                    ["additionalProperties"] = false,
                    ["properties"] = new JsonObject(),
                },
                ["annotations"] = ReadOnlyAnnotations(),
            },
            new JsonObject
            {
                ["name"] = CapabilityGuideToolName,
                ["description"] =
                    "Read one allowlisted Reader workflow guide by topic. "
                    + "Use this only for complex Reader tasks or when the "
                    + "Reader orchestration Skill is unavailable; ordinary "
                    + "snapshot, image, navigation, or highlight requests "
                    + "should call their direct tool without this extra "
                    + "round trip. Request the exact task topic when known, "
                    + "and use index only to discover an unknown topic.",
                ["inputSchema"] = new JsonObject
                {
                    ["type"] = "object",
                    ["additionalProperties"] = false,
                    ["required"] = new JsonArray("topic"),
                    ["properties"] = new JsonObject
                    {
                        ["topic"] = new JsonObject
                        {
                            ["type"] = "string",
                            ["enum"] = ReaderCapabilityCatalog.TopicEnum(),
                        },
                    },
                },
                ["annotations"] = ReadOnlyAnnotations(),
            },
        ];
        if (_fetchVisualAsync is not null)
        {
            tools.Add(new JsonObject
            {
                ["name"] = VisualToolName,
                ["description"] =
                    "Request a fresh JPEG composite from the exact Reader "
                    + "document instance named by the current snapshot. "
                    + "Choose the current viewport, nearby drawing activity, "
                    + "or a custom selection region. For selection-near, use "
                    + "only an ID listed in currentPage.selectionRegions.items; "
                    + "never invent one. Truncated older regions are unavailable. "
                    + "The result is discarded "
                    + "if the page or snapshot changes while it is captured.",
                ["inputSchema"] = new JsonObject
                {
                    ["type"] = "object",
                    ["additionalProperties"] = false,
                    ["required"] = new JsonArray("scope"),
                    ["properties"] = new JsonObject
                    {
                        ["scope"] = new JsonObject
                        {
                            ["type"] = "string",
                            ["enum"] = new JsonArray(
                                "viewport-context",
                                "drawing-nearby",
                                "selection-near"),
                        },
                        ["selectionId"] = new JsonObject
                        {
                            ["type"] = "string",
                            ["pattern"] = "^[A-Za-z0-9._:-]{1,160}$",
                        },
                    },
                },
                ["annotations"] = ReadOnlyAnnotations(),
            });
        }
        if (_controlBrowserAsync is not null)
        {
            tools.Add(new JsonObject
            {
                ["name"] = BrowserControlToolName,
                ["description"] =
                    "Control only the exact focused Reader or browser source "
                    + "named by the current snapshot. Supported actions are "
                    + "bounded viewport scrolling and locating visible text, "
                    + "a heading, or a Reader selection. For "
                    + "scroll-to-selection, use only an ID listed in "
                    + "currentPage.selectionRegions.items; never invent one. "
                    + "Truncated older regions are unavailable. Arbitrary URLs, "
                    + "selectors, and scripts are not accepted.",
                ["inputSchema"] = new JsonObject
                {
                    ["type"] = "object",
                    ["additionalProperties"] = false,
                    ["required"] = new JsonArray("action"),
                    ["properties"] = new JsonObject
                    {
                        ["action"] = new JsonObject
                        {
                            ["type"] = "string",
                            ["enum"] = new JsonArray(
                                "next-viewport",
                                "previous-viewport",
                                "scroll-to-text",
                                "scroll-to-heading",
                                "scroll-to-selection"),
                        },
                        ["target"] = new JsonObject
                        {
                            ["type"] = "string",
                            ["minLength"] = 1,
                            ["maxLength"] =
                                ReaderBrowserControlProtocol
                                    .MaximumTargetCharacters,
                        },
                        ["selectionId"] = new JsonObject
                        {
                            ["type"] = "string",
                            ["pattern"] = "^[A-Za-z0-9._:-]{1,160}$",
                        },
                    },
                },
                ["annotations"] = new JsonObject
                {
                    ["readOnlyHint"] = false,
                    ["destructiveHint"] = false,
                    ["idempotentHint"] = false,
                    ["openWorldHint"] = false,
                },
            });
        }
        if (_sendOutputAsync is not null)
        {
            tools.Add(new JsonObject
            {
                ["name"] = CardToolName,
                ["description"] =
                    "Mirror one structured weather/news/images/videos/fact/"
                    + "general result to the exact App or extension named by "
                    + "the current Reader snapshot. Call this in the same "
                    + "Windows Codex voice turn that produced the native tool "
                    + "result; never infer a card from final assistant text or "
                    + "expect text history synchronization to carry it. The "
                    + "exact card envelope is {kind,title,data}; data shapes "
                    + "are weather={lo,hi,cond,loc?,date?,precip?,tip?}, "
                    + "news={items:[{t,s?,src?}]}, "
                    + "images={items:[{url,title?,aid?,src?}]}, "
                    + "videos={items:[{title,thumb?,url?,channel?,src?}]}, "
                    + "fact={answer,detail?}, or general={text?}. The server "
                    + "strictly revalidates the existing Realtime card "
                    + "protocol and waits for the same delivery receipt.",
                ["inputSchema"] = BuildTypedCardArgumentsSchema(),
                ["annotations"] = new JsonObject
                {
                    ["readOnlyHint"] = false,
                    ["destructiveHint"] = false,
                    ["idempotentHint"] = false,
                    ["openWorldHint"] = false,
                },
            });
            tools.Add(new JsonObject
            {
                ["name"] = CommandToolName,
                ["description"] =
                    "Send one bounded structured output to the exact App or "
                    + "extension instance named by the current Reader snapshot. "
                    + "Use the exact BWREADER/1 command schema already known "
                    + "for the requested action; only complex or unknown "
                    + "workflows should read one capability guide. This is an "
                    + "additive Reader output path; it does not replace or "
                    + "change existing Realtime or CLI invocation flows. For "
                    + "weather/news/images/videos/fact/general results, pass "
                    + "the same typed {card:{kind,title,data}} envelope here "
                    + "when the dedicated reader_card tool is filtered by a "
                    + "client allowlist; text history never carries cards. "
                    + "The BWREADER/1 card form remains compatible.",
                ["inputSchema"] = new JsonObject
                {
                    ["oneOf"] = new JsonArray
                    {
                        new JsonObject
                        {
                            ["type"] = "object",
                            ["additionalProperties"] = false,
                            ["required"] = new JsonArray("command"),
                            ["properties"] = new JsonObject
                            {
                                ["command"] = new JsonObject
                                {
                                    ["type"] = "string",
                                    ["minLength"] = 1,
                                    ["maxLength"] =
                                        ReaderRealtimeOutputProtocol
                                            .MaximumPayloadBytes,
                                    ["description"] =
                                        "BWREADER/1 <kind> <single JSON object>",
                                },
                            },
                        },
                        BuildTypedCardArgumentsSchema(),
                    },
                },
                ["annotations"] = new JsonObject
                {
                    ["readOnlyHint"] = false,
                    ["destructiveHint"] = false,
                    ["idempotentHint"] = false,
                    ["openWorldHint"] = false,
                },
            });
        }
        return new JsonObject
        {
            ["tools"] = tools,
        };
    }

    private static JsonObject BuildTypedCardArgumentsSchema() => new()
    {
        ["type"] = "object",
        ["additionalProperties"] = false,
        ["required"] = new JsonArray("card"),
        ["properties"] = new JsonObject
        {
            ["card"] = new JsonObject
            {
                ["type"] = "object",
                ["additionalProperties"] = false,
                ["required"] = new JsonArray(
                    "kind",
                    "title",
                    "data"),
                ["properties"] = new JsonObject
                {
                    ["kind"] = new JsonObject
                    {
                        ["type"] = "string",
                        ["enum"] = new JsonArray(
                            "weather",
                            "news",
                            "images",
                            "videos",
                            "fact",
                            "general"),
                    },
                    ["title"] = new JsonObject
                    {
                        ["anyOf"] = new JsonArray
                        {
                            new JsonObject
                            {
                                ["type"] = "string",
                                ["maxLength"] = 320,
                            },
                            new JsonObject
                            {
                                ["type"] = "null",
                            },
                        },
                    },
                    ["data"] = new JsonObject
                    {
                        ["type"] = "object",
                        ["description"] =
                            "Kind-specific exact object; see the "
                            + "reader://capabilities/cards guide.",
                    },
                },
            },
        },
    };

    private static JsonObject ReadOnlyAnnotations() => new()
    {
        ["readOnlyHint"] = true,
        ["destructiveHint"] = false,
        ["idempotentHint"] = true,
        ["openWorldHint"] = false,
    };

    private async Task HandleToolCallAsync(
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
            await WriteErrorAsync(
                id,
                -32602,
                "Invalid tool call",
                cancellationToken).ConfigureAwait(false);
            return;
        }

        string toolName = nameValue.GetString()!;
        JsonElement arguments = parameters.TryGetProperty(
            "arguments",
            out JsonElement argumentValue)
            ? argumentValue
            : default;
        _callSequence = checked(_callSequence + 1);
        if (toolName == CapabilityGuideToolName)
        {
            await HandleCapabilityGuideToolCallAsync(
                id,
                arguments,
                cancellationToken).ConfigureAwait(false);
            return;
        }
        if (toolName == VisualToolName && _fetchVisualAsync is not null)
        {
            await HandleVisualToolCallAsync(
                id,
                arguments,
                cancellationToken).ConfigureAwait(false);
            return;
        }
        if (
            toolName == BrowserControlToolName
            && _controlBrowserAsync is not null
        )
        {
            await HandleBrowserControlToolCallAsync(
                id,
                arguments,
                cancellationToken).ConfigureAwait(false);
            return;
        }
        if (
            toolName == CardToolName
            && _sendOutputAsync is not null
        )
        {
            await HandleReaderCardToolCallAsync(
                id,
                arguments,
                cancellationToken).ConfigureAwait(false);
            return;
        }
        if (
            toolName == CommandToolName
            && _sendOutputAsync is not null
        )
        {
            await HandleReaderCommandToolCallAsync(
                id,
                arguments,
                cancellationToken).ConfigureAwait(false);
            return;
        }
        if (
            toolName != ToolName
            || !HasNoArguments(arguments)
        )
        {
            await WriteErrorAsync(
                id,
                -32602,
                "Invalid tool call",
                cancellationToken).ConfigureAwait(false);
            return;
        }

        await TryLoadLatestAsync(cancellationToken).ConfigureAwait(false);
        JsonObject payload = BuildToolPayload();
        DocumentReadReceipt? receipt = await AttachDocumentContextAsync(
            payload,
            parameters,
            cancellationToken).ConfigureAwait(false);
        await WriteResultAsync(
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
            },
            cancellationToken).ConfigureAwait(false);
        // Mark only after the JSON-RPC result has been flushed to Codex.
        // A crash or ledger failure may repeat a document, but can never make
        // a new conversation silently miss its first full-page delivery.
        if (receipt is not null)
        {
            if (receipt.ThreadId is null)
            {
                _unscopedDocumentReads.Add(
                    receipt.DocumentKey
                    + "\n"
                    + receipt.ContentRevision);
            }
            else
            {
                try
                {
                    _ = await _readLedger.MarkReadAsync(
                        receipt.ThreadId,
                        receipt.DocumentKey,
                        receipt.ContentRevision,
                        cancellationToken).ConfigureAwait(false);
                }
                catch (
                    Exception exception
                ) when (
                    exception is IOException
                    or UnauthorizedAccessException
                    or JsonException
                )
                {
                }
            }
        }
    }

    private async Task HandleReaderCommandToolCallAsync(
        JsonNode id,
        JsonElement arguments,
        CancellationToken cancellationToken)
    {
        if (TryReadReaderCard(arguments, out JsonNode cardPayload))
        {
            await SendReaderOutputAsync(
                id,
                "card",
                cardPayload,
                cancellationToken).ConfigureAwait(false);
            return;
        }
        if (!TryReadReaderCommand(
            arguments,
            out string kind,
            out JsonNode payload))
        {
            await WriteErrorAsync(
                id,
                -32602,
                "Invalid Reader command",
                cancellationToken).ConfigureAwait(false);
            return;
        }

        await SendReaderOutputAsync(
            id,
            kind,
            payload,
            cancellationToken).ConfigureAwait(false);
    }

    private async Task HandleReaderCardToolCallAsync(
        JsonNode id,
        JsonElement arguments,
        CancellationToken cancellationToken)
    {
        if (!TryReadReaderCard(arguments, out JsonNode payload))
        {
            await WriteErrorAsync(
                id,
                -32602,
                "Invalid Reader card",
                cancellationToken).ConfigureAwait(false);
            return;
        }

        await SendReaderOutputAsync(
            id,
            "card",
            payload,
            cancellationToken).ConfigureAwait(false);
    }

    private async Task SendReaderOutputAsync(
        JsonNode id,
        string kind,
        JsonNode payload,
        CancellationToken cancellationToken)
    {
        await TryLoadLatestAsync(cancellationToken).ConfigureAwait(false);
        JsonObject current = BuildToolPayload();
        ReaderRealtimeOutputRequest? request = BuildRealtimeOutputRequest(
            current,
            kind,
            payload);
        if (request is null)
        {
            await WriteReaderOutputToolErrorAsync(
                id,
                "BW_READER_REALTIME_OUTPUT_SOURCE_NOT_READY",
                "当前快照没有可精确定位的在线 App 或扩展来源。请先重新读取 Reader 上下文。",
                cancellationToken).ConfigureAwait(false);
            return;
        }

        ReaderRealtimeOutputAck ack;
        try
        {
            ack = await _sendOutputAsync!(request, cancellationToken)
                .ConfigureAwait(false);
        }
        catch (ReaderRealtimeOutputException exception)
        {
            await WriteReaderOutputToolErrorAsync(
                id,
                exception.Code,
                exception.Message,
                cancellationToken).ConfigureAwait(false);
            return;
        }

        await WriteResultAsync(
            id,
            new JsonObject
            {
                ["content"] = new JsonArray
                {
                    new JsonObject
                    {
                        ["type"] = "text",
                        ["text"] = new JsonObject
                        {
                            ["contract"] =
                                ReaderRealtimeOutputProtocol.OutputContract,
                            ["ok"] = true,
                            ["kind"] = request.Kind,
                            ["outcome"] = ack.Outcome,
                            ["sourceInstanceId"] = request.SourceInstanceId,
                            ["snapshotRevision"] = request.SnapshotRevision,
                        }.ToJsonString(
                            DirectBridgeContract.JsonOptions),
                    },
                },
            },
            cancellationToken).ConfigureAwait(false);
    }

    private async Task HandleCapabilityGuideToolCallAsync(
        JsonNode id,
        JsonElement arguments,
        CancellationToken cancellationToken)
    {
        if (!TryReadCapabilityTopic(arguments, out string topic))
        {
            await WriteErrorAsync(
                id,
                -32602,
                "Invalid Reader capability topic",
                cancellationToken).ConfigureAwait(false);
            return;
        }

        (string uri, string text) guide;
        try
        {
            guide = await _capabilityCatalog.ReadTopicTextAsync(
                topic,
                cancellationToken).ConfigureAwait(false);
        }
        catch (KeyNotFoundException)
        {
            await WriteErrorAsync(
                id,
                -32602,
                "Invalid Reader capability topic",
                cancellationToken).ConfigureAwait(false);
            return;
        }
        catch (Exception exception) when (
            exception is IOException
            or UnauthorizedAccessException
            or DecoderFallbackException
            or InvalidDataException)
        {
            await WriteErrorAsync(
                id,
                -32004,
                "Reader capability guide unavailable",
                cancellationToken).ConfigureAwait(false);
            return;
        }

        await WriteResultAsync(
            id,
            new JsonObject
            {
                ["content"] = new JsonArray
                {
                    new JsonObject
                    {
                        ["type"] = "text",
                        ["text"] = guide.text,
                    },
                },
                ["structuredContent"] = new JsonObject
                {
                    ["topic"] = topic,
                    ["uri"] = guide.uri,
                },
            },
            cancellationToken).ConfigureAwait(false);
    }

    private static bool TryReadCapabilityTopic(
        JsonElement arguments,
        out string topic)
    {
        topic = string.Empty;
        if (arguments.ValueKind != JsonValueKind.Object)
        {
            return false;
        }
        try
        {
            DirectJsonValidation.RequireNoDuplicateKeys(arguments);
        }
        catch (DirectProtocolException)
        {
            return false;
        }
        JsonProperty[] fields = arguments.EnumerateObject().ToArray();
        if (
            fields.Length != 1
            || fields[0].Name != "topic"
            || fields[0].Value.ValueKind != JsonValueKind.String
            || fields[0].Value.GetString() is not string value
            || value.Length is < 1 or > 80
        )
        {
            return false;
        }
        topic = value;
        return true;
    }

    internal static ReaderRealtimeOutputRequest? BuildRealtimeOutputRequest(
        JsonObject payload,
        string kind,
        JsonNode outputPayload)
    {
        ReaderVisualDeliveryRequest? identity = BuildVisualRequest(
            payload,
            "viewport-context",
            null);
        if (identity is null)
        {
            return null;
        }
        try
        {
            return ReaderRealtimeOutputProtocol.Create(
                "output-" + Guid.NewGuid().ToString("N"),
                identity.SourceInstanceId,
                identity.SnapshotRevision,
                identity.File,
                identity.Page,
                kind,
                outputPayload);
        }
        catch (ReaderRealtimeOutputException)
        {
            return null;
        }
    }

    private static bool TryReadReaderCard(
        JsonElement arguments,
        out JsonNode payload)
    {
        payload = new JsonObject();
        if (arguments.ValueKind != JsonValueKind.Object)
        {
            return false;
        }
        try
        {
            DirectJsonValidation.RequireNoDuplicateKeys(arguments);
            JsonProperty[] fields = arguments.EnumerateObject().ToArray();
            if (
                fields.Length != 1
                || fields[0].Name != "card"
                || fields[0].Value.ValueKind != JsonValueKind.Object
            )
            {
                return false;
            }
            JsonNode card = JsonNode.Parse(fields[0].Value.GetRawText())
                ?? throw new JsonException("Reader card is empty");
            payload = ReaderRealtimeOutputProtocol.ValidatePayload(
                "card",
                new JsonObject
                {
                    ["card"] = card,
                });
            return true;
        }
        catch (Exception exception) when (
            exception is JsonException
            or DirectProtocolException
            or ReaderRealtimeOutputException)
        {
            return false;
        }
    }

    private static bool TryReadReaderCommand(
        JsonElement arguments,
        out string kind,
        out JsonNode payload)
    {
        kind = string.Empty;
        payload = new JsonObject();
        if (arguments.ValueKind != JsonValueKind.Object)
        {
            return false;
        }
        try
        {
            DirectJsonValidation.RequireNoDuplicateKeys(arguments);
        }
        catch (DirectProtocolException)
        {
            return false;
        }
        JsonProperty[] fields = arguments.EnumerateObject().ToArray();
        if (
            fields.Length != 1
            || fields[0].Name != "command"
            || fields[0].Value.ValueKind != JsonValueKind.String
            || fields[0].Value.GetString() is not string command
            || command.Length is < 1
                or > ReaderRealtimeOutputProtocol.MaximumPayloadBytes
            || command.Any(character => character == '\0')
        )
        {
            return false;
        }
        const string prefix = "BWREADER/1 ";
        if (!command.StartsWith(prefix, StringComparison.Ordinal))
        {
            return false;
        }
        int separator = command.IndexOf(' ', prefix.Length);
        if (separator <= prefix.Length || separator + 1 >= command.Length)
        {
            return false;
        }
        string requestedKind = command[prefix.Length..separator];
        if (requestedKind is not (
            "card" or "navigate" or "highlight" or "tool-status"))
        {
            return false;
        }
        try
        {
            using JsonDocument document = JsonDocument.Parse(
                command[(separator + 1)..],
                new JsonDocumentOptions
                {
                    AllowTrailingCommas = false,
                    CommentHandling = JsonCommentHandling.Disallow,
                    MaxDepth = 12,
                });
            DirectJsonValidation.RequireNoDuplicateKeys(document.RootElement);
            if (document.RootElement.ValueKind != JsonValueKind.Object)
            {
                return false;
            }
            JsonNode parsed = JsonNode.Parse(document.RootElement.GetRawText())
                ?? new JsonObject();
            _ = ReaderRealtimeOutputProtocol.ValidatePayload(
                requestedKind,
                parsed);
            kind = requestedKind;
            payload = parsed;
            return true;
        }
        catch (Exception exception) when (
            exception is JsonException
            or DirectProtocolException
            or ReaderRealtimeOutputException)
        {
            return false;
        }
    }

    private async Task WriteReaderOutputToolErrorAsync(
        JsonNode id,
        string code,
        string message,
        CancellationToken cancellationToken)
    {
        await WriteResultAsync(
            id,
            new JsonObject
            {
                ["content"] = new JsonArray
                {
                    new JsonObject
                    {
                        ["type"] = "text",
                        ["text"] = new JsonObject
                        {
                            ["ok"] = false,
                            ["code"] = code,
                            ["message"] = message,
                        }.ToJsonString(
                            DirectBridgeContract.JsonOptions),
                    },
                },
                ["isError"] = true,
            },
            cancellationToken).ConfigureAwait(false);
    }

    private async Task HandleBrowserControlToolCallAsync(
        JsonNode id,
        JsonElement arguments,
        CancellationToken cancellationToken)
    {
        if (!TryReadBrowserControlArguments(
            arguments,
            out string action,
            out string? target,
            out string? selectionId))
        {
            await WriteErrorAsync(
                id,
                -32602,
                "Invalid tool call",
                cancellationToken).ConfigureAwait(false);
            return;
        }

        await TryLoadLatestAsync(cancellationToken).ConfigureAwait(false);
        JsonObject before = BuildToolPayload();
        ReaderBrowserControlRequest? request =
            BuildBrowserControlRequest(
                before,
                action,
                target,
                selectionId);
        if (request is null)
        {
            await WriteBrowserControlToolErrorAsync(
                id,
                "browser-source-not-ready",
                "当前快照没有可精确定位的在线页面来源。请先重新读取 Reader 上下文。",
                cancellationToken).ConfigureAwait(false);
            return;
        }

        ReaderBrowserControlResponse response;
        try
        {
            response = await _controlBrowserAsync!(
                request,
                cancellationToken).ConfigureAwait(false);
        }
        catch (ReaderBrowserControlException exception)
        {
            await WriteBrowserControlToolErrorAsync(
                id,
                exception.Code,
                exception.Message,
                cancellationToken).ConfigureAwait(false);
            return;
        }

        JsonObject after = BuildToolPayload();
        bool snapshotAdvanced = false;
        bool requiresSnapshotAdvance =
            BrowserControlResponseRequiresSnapshotAdvance(
                response.Status);
        // A successful scroll is not complete until the exact viewport report
        // produced by that control arrives. Unrelated snapshot revisions can
        // advance while several readers are online, so revision alone is not
        // an acknowledgement. The page echoes request.Correlation in its next
        // reader-viewport/1 payload; wait about five seconds for that receipt.
        int attempts = requiresSnapshotAdvance ? 100 : 1;
        for (int attempt = 0; attempt < attempts; attempt += 1)
        {
            await TryLoadLatestAsync(cancellationToken)
                .ConfigureAwait(false);
            after = BuildToolPayload();
            if (!BrowserControlRequestStillCurrent(after, request))
            {
                await WriteBrowserControlToolErrorAsync(
                    id,
                    "BW_READER_BROWSER_CONTROL_SNAPSHOT_SUPERSEDED",
                    "控制期间当前页面来源或页面身份已变化，本次结果已丢弃。",
                    cancellationToken).ConfigureAwait(false);
                return;
            }
            if (
                !requiresSnapshotAdvance
                || BrowserControlSnapshotAdvanced(after, request)
            )
            {
                snapshotAdvanced = true;
                break;
            }
            if (attempt + 1 < attempts)
            {
                await Task.Delay(
                    TimeSpan.FromMilliseconds(50),
                    cancellationToken).ConfigureAwait(false);
            }
        }
        if (!snapshotAdvanced)
        {
            await WriteBrowserControlToolErrorAsync(
                id,
                "BW_READER_BROWSER_CONTROL_CONTEXT_REFRESH_TIMEOUT",
                "浏览器已执行控制，但新的视口快照未在限定时间内到达。",
                cancellationToken).ConfigureAwait(false);
            return;
        }

        await WriteResultAsync(
            id,
            new JsonObject
            {
                ["content"] = new JsonArray
                {
                    new JsonObject
                    {
                        ["type"] = "text",
                        ["text"] = new JsonObject
                        {
                            ["contract"] =
                                ReaderBrowserControlProtocol
                                    .ControlContract,
                            ["status"] = response.Status,
                            ["action"] = response.Action,
                            ["sourceInstanceId"] =
                                response.SourceInstanceId,
                            ["snapshotRevision"] =
                                LongValue(after["revision"])
                                    ?? response.SnapshotRevision,
                            ["scrollX"] = response.ScrollX,
                            ["scrollY"] = response.ScrollY,
                            ["url"] = response.Url,
                            ["title"] = response.Title,
                        }.ToJsonString(
                            DirectBridgeContract.JsonOptions),
                    },
                },
            },
            cancellationToken).ConfigureAwait(false);
    }

    private static bool TryReadBrowserControlArguments(
        JsonElement arguments,
        out string action,
        out string? target,
        out string? selectionId)
    {
        action = string.Empty;
        target = null;
        selectionId = null;
        if (arguments.ValueKind != JsonValueKind.Object)
        {
            return false;
        }
        try
        {
            DirectJsonValidation.RequireNoDuplicateKeys(arguments);
        }
        catch (DirectProtocolException)
        {
            return false;
        }
        HashSet<string> fields = arguments.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        if (
            !fields.Contains("action")
            || fields.Any(field => field is not (
                "action" or "target" or "selectionId"))
            || arguments.GetProperty("action").ValueKind
                != JsonValueKind.String
            || arguments.GetProperty("action").GetString()
                is not string requestedAction
            || !ReaderBrowserControlProtocol.IsAction(requestedAction)
        )
        {
            return false;
        }
        if (fields.Contains("target"))
        {
            JsonElement value = arguments.GetProperty("target");
            if (
                value.ValueKind != JsonValueKind.String
                || value.GetString() is not string requestedTarget
                || string.IsNullOrWhiteSpace(requestedTarget)
                || requestedTarget.Length
                    > ReaderBrowserControlProtocol
                        .MaximumTargetCharacters
                || requestedTarget.Any(char.IsControl)
            )
            {
                return false;
            }
            target = requestedTarget;
        }
        if (fields.Contains("selectionId"))
        {
            JsonElement value = arguments.GetProperty("selectionId");
            if (
                value.ValueKind != JsonValueKind.String
                || value.GetString() is not string requestedSelection
                || requestedSelection.Length is < 1 or > 160
                || !DirectBridgeContract.IsSafeId(requestedSelection)
            )
            {
                return false;
            }
            selectionId = requestedSelection;
        }
        bool shapeValid = requestedAction switch
        {
            "next-viewport" or "previous-viewport" =>
                fields.SetEquals(new[] { "action" }),
            "scroll-to-text" or "scroll-to-heading" =>
                fields.SetEquals(new[] { "action", "target" }),
            "scroll-to-selection" =>
                fields.SetEquals(new[] { "action", "selectionId" }),
            _ => false,
        };
        if (!shapeValid)
        {
            return false;
        }
        action = requestedAction;
        return true;
    }

    internal static ReaderBrowserControlRequest?
        BuildBrowserControlRequest(
            JsonObject payload,
            string action,
            string? target,
            string? selectionId)
    {
        if (
            !ReaderBrowserControlProtocol.IsAction(action)
            || LongValue(payload["revision"]) is not long revision
            || revision < 0
            || payload["contextStatus"]?.GetValue<string>() != "ready"
            || payload["activeReading"] is not JsonObject active
            || payload["currentPage"] is not JsonObject page
            || StringValue(active["kind"]) != "web"
            || StringValue(page["kind"]) != "web"
            || page["stable"]?.GetValue<bool?>() != true
            || StringValue(active["sourceInstanceId"])
                is not string activeSource
            || StringValue(page["sourceInstanceId"])
                is not string pageSource
            || activeSource != pageSource
            || !DirectBridgeContract.IsSafeId(activeSource)
            || StringValue(page["file"]) is not string file
            || string.IsNullOrWhiteSpace(file)
            || page["page"] is not JsonNode pageIdentity
        )
        {
            return null;
        }
        if (
            action == "scroll-to-selection"
                ? !FileDirectSnapshotContextAdapter.SelectionRegionExists(
                    page["selectionRegions"],
                    selectionId)
                : selectionId is not null
        )
        {
            return null;
        }
        return new ReaderBrowserControlRequest(
            "control-" + Guid.NewGuid().ToString("N"),
            activeSource,
            revision,
            file,
            pageIdentity.DeepClone(),
            action,
            target,
            selectionId);
    }

    internal static bool BrowserControlRequestStillCurrent(
        JsonObject payload,
        ReaderBrowserControlRequest request)
    {
        ReaderBrowserControlRequest? current =
            BuildBrowserControlRequest(
                payload,
                request.Action,
                request.Target,
                request.SelectionId);
        return current is not null
            && current.SourceInstanceId == request.SourceInstanceId
            && current.SnapshotRevision >= request.SnapshotRevision
            && current.File == request.File
            && JsonNode.DeepEquals(current.Page, request.Page);
    }

    internal static bool BrowserControlSnapshotAdvanced(
        JsonObject payload,
        ReaderBrowserControlRequest request) =>
        BrowserControlRequestStillCurrent(payload, request)
        && LongValue(payload["revision"])
            is long revision
        && revision > request.SnapshotRevision
        && payload["currentPage"] is JsonObject page
        && page["readingWindow"] is JsonObject readingWindow
        && string.Equals(
            StringValue(readingWindow["controlCorrelation"]),
            request.Correlation,
            StringComparison.Ordinal);

    internal static bool BrowserControlResponseRequiresSnapshotAdvance(
        string status) => status == "success";

    private async Task WriteBrowserControlToolErrorAsync(
        JsonNode id,
        string code,
        string message,
        CancellationToken cancellationToken)
    {
        await WriteResultAsync(
            id,
            new JsonObject
            {
                ["content"] = new JsonArray
                {
                    new JsonObject
                    {
                        ["type"] = "text",
                        ["text"] = new JsonObject
                        {
                            ["ok"] = false,
                            ["code"] = code,
                            ["message"] = message,
                        }.ToJsonString(
                            DirectBridgeContract.JsonOptions),
                    },
                },
                ["isError"] = true,
            },
            cancellationToken).ConfigureAwait(false);
    }

    private async Task HandleVisualToolCallAsync(
        JsonNode id,
        JsonElement arguments,
        CancellationToken cancellationToken)
    {
        if (!TryReadVisualArguments(
            arguments,
            out string scope,
            out string? selectionId))
        {
            await WriteErrorAsync(
                id,
                -32602,
                "Invalid tool call",
                cancellationToken).ConfigureAwait(false);
            return;
        }

        await TryLoadLatestAsync(cancellationToken).ConfigureAwait(false);
        JsonObject before = BuildToolPayload();
        ReaderVisualDeliveryRequest? request = BuildVisualRequest(
            before,
            scope,
            selectionId);
        if (request is null)
        {
            await WriteVisualToolErrorAsync(
                id,
                "visual-source-not-ready",
                "当前快照没有可精确定位的在线页面来源。请先重新读取 Reader 上下文。",
                cancellationToken).ConfigureAwait(false);
            return;
        }

        ReaderVisualCapture? capture;
        try
        {
            capture = await _fetchVisualAsync!(
                request,
                cancellationToken).ConfigureAwait(false);
        }
        catch (ReaderVisualDeliveryException exception)
        {
            await WriteVisualToolErrorAsync(
                id,
                exception.Code,
                exception.Message,
                cancellationToken).ConfigureAwait(false);
            return;
        }

        await TryLoadLatestAsync(cancellationToken).ConfigureAwait(false);
        JsonObject after = BuildToolPayload();
        if (!VisualRequestStillCurrent(after, request))
        {
            await WriteVisualToolErrorAsync(
                id,
                "BW_READER_VISUAL_SNAPSHOT_SUPERSEDED",
                "取图期间当前页面或笔迹版本已变化，本次图像已丢弃。",
                cancellationToken).ConfigureAwait(false);
            return;
        }
        if (
            capture is null
            || capture.MimeType
                != ReaderVisualDeliveryProtocol.MimeType
            || capture.Data.Length == 0
        )
        {
            await WriteVisualToolErrorAsync(
                id,
                "BW_READER_VISUAL_UNAVAILABLE",
                "当前页面没有返回可用的合成图。",
                cancellationToken).ConfigureAwait(false);
            return;
        }

        JsonObject metadata = new()
        {
            ["scope"] = request.Scope,
            ["sourceInstanceId"] = request.SourceInstanceId,
            ["snapshotRevision"] = request.SnapshotRevision,
            ["file"] = request.File,
            ["page"] = request.Page.DeepClone(),
            ["drawingRevision"] = request.DrawingRevision,
            ["selectionId"] = request.SelectionId,
        };
        await WriteResultAsync(
            id,
            new JsonObject
            {
                ["content"] = new JsonArray
                {
                    new JsonObject
                    {
                        ["type"] = "text",
                        ["text"] = metadata.ToJsonString(
                            DirectBridgeContract.JsonOptions),
                    },
                    new JsonObject
                    {
                        ["type"] = "image",
                        ["data"] = Convert.ToBase64String(capture.Data),
                        ["mimeType"] = capture.MimeType,
                        ["_meta"] = new JsonObject
                        {
                            ["codex/imageDetail"] = "original",
                        },
                    },
                },
            },
            cancellationToken).ConfigureAwait(false);
    }

    private static bool TryReadVisualArguments(
        JsonElement arguments,
        out string scope,
        out string? selectionId)
    {
        scope = string.Empty;
        selectionId = null;
        if (arguments.ValueKind != JsonValueKind.Object)
        {
            return false;
        }
        try
        {
            DirectJsonValidation.RequireNoDuplicateKeys(arguments);
        }
        catch (DirectProtocolException)
        {
            return false;
        }
        HashSet<string> fields = arguments.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        if (
            !fields.Contains("scope")
            || fields.Any(field => field is not (
                "scope" or "selectionId"))
            || arguments.GetProperty("scope").ValueKind
                != JsonValueKind.String
            || arguments.GetProperty("scope").GetString()
                is not string requestedScope
            || !ReaderVisualDeliveryProtocol.IsScope(requestedScope)
        )
        {
            return false;
        }
        if (fields.Contains("selectionId"))
        {
            JsonElement selection = arguments.GetProperty("selectionId");
            if (
                selection.ValueKind != JsonValueKind.String
                || selection.GetString() is not string requestedSelection
                || requestedSelection.Length is < 1 or > 160
                || !DirectBridgeContract.IsSafeId(requestedSelection)
                || requestedScope != "selection-near"
            )
            {
                return false;
            }
            selectionId = requestedSelection;
        }
        if (
            (requestedScope == "selection-near")
                != (selectionId is not null)
        )
        {
            return false;
        }
        scope = requestedScope;
        return true;
    }

    internal static ReaderVisualDeliveryRequest? BuildVisualRequest(
        JsonObject payload,
        string scope,
        string? selectionId)
    {
        if (
            !ReaderVisualDeliveryProtocol.IsScope(scope)
            || LongValue(payload["revision"]) is not long revision
            || revision < 0
            || payload["contextStatus"]?.GetValue<string>() != "ready"
            || payload["activeReading"] is not JsonObject active
            || payload["currentPage"] is not JsonObject page
            || page["stable"]?.GetValue<bool?>() != true
            || StringValue(active["sourceInstanceId"])
                is not string activeSource
            || StringValue(page["sourceInstanceId"])
                is not string pageSource
            || !string.Equals(
                activeSource,
                pageSource,
                StringComparison.Ordinal)
            || !DirectBridgeContract.IsSafeId(activeSource)
            || StringValue(page["file"]) is not string file
            || string.IsNullOrWhiteSpace(file)
            || page["page"] is not JsonNode pageIdentity
        )
        {
            return null;
        }
        if (
            scope == "selection-near"
                ? !FileDirectSnapshotContextAdapter.SelectionRegionExists(
                    page["selectionRegions"],
                    selectionId)
                : selectionId is not null
        )
        {
            return null;
        }

        string? drawingRevision = null;
        if (
            page["visual"] is JsonObject visual
            && visual["drawing"] is JsonObject drawing
            && drawing["drawingRevision"] is JsonValue revisionValue
            && revisionValue.TryGetValue(out string? candidateRevision)
        )
        {
            drawingRevision = candidateRevision;
        }
        if (
            scope == "drawing-nearby"
            && (
                page["visual"] is not JsonObject drawingVisual
                || drawingVisual["drawing"] is not JsonObject drawingState
                || drawingState["stable"]?.GetValue<bool?>() != true
                || drawingState["inProgress"]?.GetValue<bool?>() != false
                || drawingState["empty"]?.GetValue<bool?>() != false
                || string.IsNullOrEmpty(drawingRevision)
            )
        )
        {
            return null;
        }
        return new ReaderVisualDeliveryRequest(
            "visual-" + Guid.NewGuid().ToString("N"),
            activeSource,
            revision,
            file,
            pageIdentity.DeepClone(),
            drawingRevision,
            scope,
            selectionId);
    }

    internal static bool VisualRequestStillCurrent(
        JsonObject payload,
        ReaderVisualDeliveryRequest request)
    {
        ReaderVisualDeliveryRequest? current = BuildVisualRequest(
            payload,
            request.Scope,
            request.SelectionId);
        return current is not null
            && current.SourceInstanceId == request.SourceInstanceId
            && current.SnapshotRevision == request.SnapshotRevision
            && current.File == request.File
            && JsonNode.DeepEquals(current.Page, request.Page)
            && current.DrawingRevision == request.DrawingRevision;
    }

    private async Task WriteVisualToolErrorAsync(
        JsonNode id,
        string code,
        string message,
        CancellationToken cancellationToken)
    {
        await WriteResultAsync(
            id,
            new JsonObject
            {
                ["content"] = new JsonArray
                {
                    new JsonObject
                    {
                        ["type"] = "text",
                        ["text"] = new JsonObject
                        {
                            ["ok"] = false,
                            ["code"] = code,
                            ["message"] = message,
                        }.ToJsonString(
                            DirectBridgeContract.JsonOptions),
                    },
                },
                ["isError"] = true,
            },
            cancellationToken).ConfigureAwait(false);
    }

    private static bool HasNoArguments(JsonElement arguments) =>
        arguments.ValueKind == JsonValueKind.Undefined
        || (
            arguments.ValueKind == JsonValueKind.Object
            && !arguments.EnumerateObject().Any()
        );

    private sealed record DocumentReadReceipt(
        string? ThreadId,
        string DocumentKey,
        string ContentRevision);

    private async Task<DocumentReadReceipt?> AttachDocumentContextAsync(
        JsonObject payload,
        JsonElement parameters,
        CancellationToken cancellationToken)
    {
        string status = StringValue(payload["contextStatus"])
            ?? "pending";
        if (status != "ready")
        {
            SetDocumentDelivery(payload, "snapshot-not-ready");
            return null;
        }

        ReaderDocumentCorpusEntry? document;
        try
        {
            document = await _documentCorpus.ReadAsync(cancellationToken)
                .ConfigureAwait(false);
        }
        catch (
            Exception exception
        ) when (
            exception is IOException
            or UnauthorizedAccessException
            or JsonException
            or DirectProtocolException
        )
        {
            SetDocumentDelivery(payload, "corpus-invalid");
            return null;
        }
        if (document is null)
        {
            SetDocumentDelivery(payload, "corpus-missing");
            return null;
        }
        if (!DocumentMatchesSnapshot(payload, document))
        {
            SetDocumentDelivery(payload, "corpus-superseded");
            return null;
        }

        bool scoped = ReaderContextReadLedger.TryThreadId(
            parameters,
            out string threadId);
        string processDocumentKey = document.DocumentKey
            + "\n"
            + document.ContentRevision;
        bool alreadyDelivered = !scoped
            && _unscopedDocumentReads.Contains(processDocumentKey);
        if (scoped)
        {
            try
            {
                alreadyDelivered = await _readLedger.HasReadAsync(
                    threadId,
                    document.DocumentKey,
                    document.ContentRevision,
                    cancellationToken).ConfigureAwait(false);
            }
            catch (
                Exception exception
            ) when (
                exception is IOException
                or UnauthorizedAccessException
                or JsonException
            )
            {
                // Failure to read the ledger must repeat, never omit, text.
                alreadyDelivered = false;
            }
        }
        string delivery = scoped
            ? alreadyDelivered
                ? "already-delivered"
                : "first-in-thread"
            : alreadyDelivered
                ? "already-delivered-in-process"
                : "first-in-process";
        payload["documentContext"] = new JsonObject
        {
            ["contract"] = ReaderDocumentCorpusStore.DocumentContract,
            ["scope"] = scoped ? "thread" : "mcp-process",
            ["delivery"] = delivery,
            ["sourceInstanceId"] = document.SourceInstanceId,
            ["documentKey"] = document.DocumentKey,
            ["url"] = document.Url,
            ["title"] = document.Title,
            ["contentRevision"] = document.ContentRevision,
            ["truncated"] = document.Truncated,
            ["observedAtEpochMs"] =
                document.ObservedAtEpochMilliseconds,
            ["text"] = alreadyDelivered ? null : document.Text,
        };
        SetDocumentDelivery(payload, delivery);
        return !alreadyDelivered
            ? new DocumentReadReceipt(
                scoped ? threadId : null,
                document.DocumentKey,
                document.ContentRevision)
            : null;
    }

    private static bool DocumentMatchesSnapshot(
        JsonObject payload,
        ReaderDocumentCorpusEntry document)
    {
        JsonObject? active = payload["activeReading"] as JsonObject;
        JsonObject? page = payload["currentPage"] as JsonObject;
        JsonObject? readingWindow = page?["readingWindow"] as JsonObject;
        return active is not null
            && page is not null
            && readingWindow is not null
            && string.Equals(
                StringValue(active["sourceInstanceId"]),
                document.SourceInstanceId,
                StringComparison.Ordinal)
            && string.Equals(
                StringValue(page["sourceInstanceId"]),
                document.SourceInstanceId,
                StringComparison.Ordinal)
            && string.Equals(
                StringValue(readingWindow["documentKey"]),
                document.DocumentKey,
                StringComparison.Ordinal)
            && string.Equals(
                StringValue(page["file"]),
                document.Url,
                StringComparison.Ordinal);
    }

    private static void SetDocumentDelivery(
        JsonObject payload,
        string value)
    {
        if (payload["mcp"] is JsonObject mcp)
        {
            mcp["documentDelivery"] = value;
        }
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
            snapshot["visualAccess"] = BuildVisualAccess(
                snapshot,
                _fetchVisualAsync is not null);
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
            ["visualAccess"] = BuildVisualAccess(
                new JsonObject
                {
                    ["schema"] = FileDirectSnapshotContextAdapter
                        .SnapshotContract,
                    ["revision"] = 0,
                    ["contextStatus"] = "pending",
                    ["activeReading"] = null,
                    ["currentPage"] = null,
                },
                _fetchVisualAsync is not null),
        };
    }

    internal static JsonObject BuildVisualAccess(
        JsonObject payload,
        bool toolConfigured)
    {
        JsonArray scopes = [];
        if (toolConfigured)
        {
            if (BuildVisualRequest(
                payload,
                "viewport-context",
                null) is not null)
            {
                scopes.Add("viewport-context");
            }
            if (BuildVisualRequest(
                payload,
                "drawing-nearby",
                null) is not null)
            {
                scopes.Add("drawing-nearby");
            }
            if (
                payload["currentPage"] is JsonObject page
                && page["selectionRegions"] is JsonObject regions
                && regions["items"] is JsonArray items
            )
            {
                foreach (JsonNode? item in items)
                {
                    if (
                        item is JsonObject region
                        && StringValue(region["selectionId"])
                            is string selectionId
                        && BuildVisualRequest(
                            payload,
                            "selection-near",
                            selectionId) is not null
                    )
                    {
                        scopes.Add("selection-near");
                        break;
                    }
                }
            }
        }
        bool available = scopes.Count > 0;
        JsonObject access = new()
        {
            ["available"] = available,
            ["mode"] = "on-demand-mcp",
            ["tool"] = VisualToolName,
            ["returns"] = "inline-image",
            ["scopes"] = scopes,
        };
        if (!available)
        {
            access["reason"] = toolConfigured
                ? "source-not-ready"
                : "tool-not-configured";
        }
        return access;
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
            string? producerInstanceId = null;
            if (parsed["producerInstanceId"] is JsonNode producerNode)
            {
                producerInstanceId = producerNode.GetValue<string>();
                if (!Guid.TryParseExact(
                    producerInstanceId,
                    "N",
                    out _))
                {
                    _loadErrors = checked(_loadErrors + 1);
                    return;
                }
            }
            if (ShouldAdoptSnapshot(
                _latestRevision,
                _latestProducerInstanceId,
                revision,
                producerInstanceId))
            {
                _latestSnapshot = parsed;
                _latestRevision = revision;
                _latestProducerInstanceId = producerInstanceId;
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

    internal static bool ShouldAdoptSnapshot(
        long latestRevision,
        string? latestProducerInstanceId,
        long candidateRevision,
        string? candidateProducerInstanceId) =>
        candidateRevision >= latestRevision
        || (
            candidateProducerInstanceId is not null
            && !string.Equals(
                candidateProducerInstanceId,
                latestProducerInstanceId,
                StringComparison.Ordinal)
        );

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

    private static string? StringValue(JsonNode? value)
    {
        if (value is not JsonValue jsonValue)
        {
            return null;
        }
        return jsonValue.TryGetValue(out string? text)
            ? text
            : null;
    }

    private void HandleNotification(string method)
    {
        if (method == "notifications/initialized")
        {
            _initialized = true;
        }
    }

    private bool RequireInitialized() => _initialized;

    private async Task WriteResultAsync(
        JsonNode id,
        JsonNode result,
        CancellationToken cancellationToken)
    {
        await WriteMessageAsync(
            new JsonObject
            {
                ["jsonrpc"] = "2.0",
                ["id"] = id.DeepClone(),
                ["result"] = result,
            },
            cancellationToken).ConfigureAwait(false);
    }

    private async Task WriteErrorAsync(
        JsonNode? id,
        int code,
        string message,
        CancellationToken cancellationToken)
    {
        await WriteMessageAsync(
            new JsonObject
            {
                ["jsonrpc"] = "2.0",
                ["id"] = id?.DeepClone(),
                ["error"] = new JsonObject
                {
                    ["code"] = code,
                    ["message"] = message,
                },
            },
            cancellationToken).ConfigureAwait(false);
    }

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
