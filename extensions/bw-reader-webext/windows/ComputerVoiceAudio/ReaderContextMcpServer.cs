using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace BwReader.ComputerVoiceAudio;

internal sealed class ReaderContextMcpServer
{
    internal const string ToolName = "reader_context_snapshot";
    internal const string VisualToolName = "reader_visual_image";
    internal const string BrowserControlToolName = "reader_browser_control";
    internal const string HighlightTextToolName = "reader_highlight_text";
    internal const string HighlightRangeToolName =
        "reader_highlight_range";
    internal const string AnkiDraftToolName = "reader_anki_draft";
    internal const string CardToolName = "reader_card";
    internal const string CommandToolName = "reader_command";
    internal const string UndoLastToolName = "reader_undo_last";
    internal const string NoteCreateToolName = "reader_note_create";
    internal const string NoteEditToolName = "reader_note_edit";
    internal const string HighlightsToolName = "reader_highlights";
    internal const string NotesToolName = "reader_notes";
    internal const string SearchToolName = "reader_search";
    internal const string TocToolName = "reader_toc";
    internal const string PageTextToolName = "reader_page_text";
    internal const string MakeNoteToolName = "reader_make_note";
    internal const string LookupToolName = "reader_lookup_word";
    internal const string MarkVocabToolName = "reader_mark_vocab";
    internal const string WebHighlightToolName = "reader_web_highlight";
    internal const string CapabilityGuideToolName =
        "reader_capability_guide";
    internal const string ServerName = "bw-reader-context-snapshot";
    internal const string ServerVersion = "1.6.0";
    internal static readonly TimeSpan FreshnessWindow =
        TimeSpan.FromMinutes(3);

    private const int MaximumMessageCharacters = 1024 * 1024;
    private const int MaximumSnapshotBytes =
        FileDirectSnapshotContextAdapter.MaximumSnapshotBytes;
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
    private readonly ReaderLocalAnkiRegistry _localAnkiRegistry;
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
    private readonly Func<
        ReaderQueryRequest,
        CancellationToken,
        Task<ReaderQueryResponse>>? _queryReaderAsync;
    private readonly Func<
        string,
        CancellationToken,
        Task<ReaderRealtimeOutputSourceStatus>>? _probeOutputSourceAsync;
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
        Func<
            string,
            CancellationToken,
            Task<ReaderRealtimeOutputSourceStatus>>?
                probeOutputSourceAsync = null,
        ReaderCapabilityCatalog? capabilityCatalog = null,
        Func<
            ReaderQueryRequest,
            CancellationToken,
            Task<ReaderQueryResponse>>? queryReaderAsync = null)
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
        _queryReaderAsync = queryReaderAsync;
        _probeOutputSourceAsync = probeOutputSourceAsync;
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
        _localAnkiRegistry = new ReaderLocalAnkiRegistry(
            Path.Combine(
                directory,
                ReaderLocalAnkiRegistry.RegistryFileName),
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
                    "Before answering any request whose meaning depends on "
                    + "the live Reader state, call reader_context_snapshot "
                    + "first. This includes indirect references such as "
                    + "this, here, this page or paragraph, what is visible, "
                    + "selected, highlighted, drawn, or circled, even when "
                    + "the user does not say Reader or snapshot. Do not call "
                    + "it for unrelated requests. After obtaining any "
                    + "structured weather/news/images/videos/fact/general "
                    + "result, immediately call reader_card in the same turn "
                    + "or reader_command when reader_card is filtered; do not "
                    + "stop after promising to research it. Respect "
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
                    + "Use reader_card or reader_command with the same typed "
                    + "{card:{kind,title,data}} input. "
                    + "For a source highlight, call "
                    + "reader_context_snapshot first. When "
                    + "currentPage.highlightSource is present, copy its "
                    + "exact identity and choose two published markers for "
                    + "reader_highlight_range; never echo or search a long "
                    + "quote. reader_highlight_text remains only for an old "
                    + "client that has no marker source. For a "
                    + "book-referencing Anki draft, copy the exact current "
                    + "file identity and verbatim source text. Confirm "
                    + "outputAccess.available before either mutation. A "
                    + "normal non-reference Anki draft "
                    + "passes cards only and does not claim the current page "
                    + "as its source. Source-bound calls reject a wrong book, "
                    + "wrong page or section, missing text, or text that is "
                    + "not unique. reader_anki_draft only displays the "
                    + "existing confirmation UI; it never writes Anki. "
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
                    "Call this first whenever the request depends on the "
                    + "live Reader state, including implicit references to "
                    + "this/here, the visible page or paragraph, a selection, "
                    + "highlight, drawing, or circle. It reads the newest "
                    + "Windows-local Reader page and selection snapshot and "
                    + "is read-only. "
                    + "Check contextStatus before using currentPage; "
                    + "never reuse text when it is pending or stale. Read "
                    + "visualAccess to discover whether the exact App "
                    + "surface can be requested on demand; page_image=null "
                    + "alone does not mean that no image is available. Read "
                    + "outputAccess before calling a mutating Reader tool: "
                    + "a readable cached page can remain ready after its "
                    + "live App or extension source has disconnected.",
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
                ["name"] = HighlightRangeToolName,
                ["description"] =
                    "Persist one highlight from the App-owned marker range "
                    + "published in currentPage.highlightSource. Call "
                    + "reader_context_snapshot first, copy the exact source "
                    + "identity fields, and choose startMarker/endMarker "
                    + "from that source in document order. Every marker is "
                    + "the boundary before its text: startMarker is the first "
                    + "included segment, while endMarker is the first excluded "
                    + "segment (exclusive). To include through source end, "
                    + "use the final marker whose text is empty. Never return or "
                    + "search an entire source quote. The Reader rejects "
                    + "invented, reversed, expired, stale-book, stale-page "
                    + "or stale-revision ranges and never falls back to text "
                    + "search. Do not retry an unknown mutation outcome.",
                ["inputSchema"] = BuildHighlightRangeArgumentsSchema(),
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
                ["name"] = WebHighlightToolName,
                ["description"] =
                    "Highlight a passage on the web page the user is reading. "
                    + "Give the exact text as it appears in the page, copied "
                    + "verbatim from the snapshot - the page locates it "
                    + "itself, because you can name a sentence but not a DOM "
                    + "position. If that sentence occurs more than once, pass "
                    + "prefix and suffix (the text just before and just "
                    + "after) so the right occurrence is chosen. Fails "
                    + "plainly when the text cannot be found, rather than "
                    + "marking an approximate spot: a highlight in the wrong "
                    + "place is worse than none, because the user reads it as "
                    + "their own mistake. Works on ordinary web pages, not in "
                    + "books - use reader_highlight_range there. Do not retry "
                    + "an unknown outcome.",
                ["inputSchema"] = new JsonObject
                {
                    ["type"] = "object",
                    ["properties"] = new JsonObject
                    {
                        ["exact"] = new JsonObject
                        {
                            ["type"] = "string",
                            ["minLength"] = 1,
                            ["maxLength"] = 2000,
                            ["description"] =
                                "The passage, copied verbatim from the page.",
                        },
                        ["prefix"] = new JsonObject
                        {
                            ["type"] = "string",
                            ["maxLength"] = 200,
                            ["description"] =
                                "Text immediately before it, to disambiguate "
                                + "a repeated sentence. Empty if not needed.",
                        },
                        ["suffix"] = new JsonObject
                        {
                            ["type"] = "string",
                            ["maxLength"] = 200,
                            ["description"] = "Text immediately after it.",
                        },
                        ["color"] = new JsonObject
                        {
                            ["type"] = "string",
                            ["maxLength"] = 32,
                            ["description"] =
                                "CSS colour. Empty for the default.",
                        },
                        ["note"] = new JsonObject
                        {
                            ["type"] = "string",
                            ["maxLength"] = 2000,
                            ["description"] = "Optional note attached to it.",
                        },
                    },
                    ["required"] = new JsonArray
                    {
                        "exact", "prefix", "suffix", "color", "note",
                    },
                    ["additionalProperties"] = false,
                },
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
                ["name"] = MarkVocabToolName,
                ["description"] =
                    "Mark a word as known or unknown in the user's vocabulary. "
                    + "Needs the Pi, where that vocabulary lives; offline it "
                    + "fails rather than dropping the mark, because a 'known' "
                    + "that never landed means the word gets underlined as "
                    + "unfamiliar again next time. Ask before marking words "
                    + "the user did not bring up - this changes what their "
                    + "reader underlines and what Anki schedules.",
                ["inputSchema"] = new JsonObject
                {
                    ["type"] = "object",
                    ["properties"] = new JsonObject
                    {
                        ["word"] = new JsonObject
                        {
                            ["type"] = "string",
                            ["minLength"] = 1,
                            ["maxLength"] = 128,
                            ["description"] = "The word to mark.",
                        },
                        ["mark"] = new JsonObject
                        {
                            ["type"] = "string",
                            ["enum"] = new JsonArray { "known", "unknown" },
                            ["description"] =
                                "known removes the underline; unknown "
                                + "restores it.",
                        },
                    },
                    ["required"] = new JsonArray { "word", "mark" },
                    ["additionalProperties"] = false,
                },
                ["annotations"] = new JsonObject
                {
                    ["readOnlyHint"] = false,
                    ["destructiveHint"] = false,
                    ["idempotentHint"] = true,
                    ["openWorldHint"] = false,
                },
            });
            tools.Add(new JsonObject
            {
                ["name"] = MakeNoteToolName,
                ["description"] =
                    "Save a passage as a note file in the notes folder the "
                    + "App is configured with - a document, not the sticky "
                    + "note reader_note_create pins on the page. The book and "
                    + "page are filled in by the Reader. A title is optional: "
                    + "leave it out and the Reader names the note from the "
                    + "book and page it is actually on, which is more "
                    + "reliable than guessing. Returns the path it was "
                    + "written to. Do not retry an unknown outcome - a second "
                    + "attempt writes a second file.",
                ["inputSchema"] = new JsonObject
                {
                    ["type"] = "object",
                    ["properties"] = new JsonObject
                    {
                        ["text"] = new JsonObject
                        {
                            ["type"] = "string",
                            ["minLength"] = 1,
                            ["maxLength"] = 240000,
                            ["description"] = "The note's body.",
                        },
                        ["title"] = new JsonObject
                        {
                            ["type"] = "string",
                            ["maxLength"] = 240,
                            ["description"] =
                                "Optional title. Pass an empty string to let "
                                + "the Reader name it.",
                        },
                    },
                    ["required"] = new JsonArray { "text", "title" },
                    ["additionalProperties"] = false,
                },
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
                ["name"] = NoteEditToolName,
                ["description"] =
                    "Rewrite the text of a sticky note that already exists in "
                    + "the open book. Only the text changes: where the user "
                    + "put the note, and how big it is, stay as they left "
                    + "them. Needs the note's id. Reports a missing note "
                    + "separately from a failed write, because retrying helps "
                    + "only in the second case. Do not retry an unknown "
                    + "outcome.",
                ["inputSchema"] = new JsonObject
                {
                    ["type"] = "object",
                    ["properties"] = new JsonObject
                    {
                        ["id"] = new JsonObject
                        {
                            ["type"] = "string",
                            ["minLength"] = 1,
                            ["maxLength"] = 64,
                            ["description"] = "The note's id.",
                        },
                        ["text"] = new JsonObject
                        {
                            ["type"] = "string",
                            ["minLength"] = 1,
                            ["maxLength"] = 4000,
                            ["description"] = "The replacement contents.",
                        },
                    },
                    ["required"] = new JsonArray { "id", "text" },
                    ["additionalProperties"] = false,
                },
                ["annotations"] = new JsonObject
                {
                    ["readOnlyHint"] = false,
                    ["destructiveHint"] = false,
                    ["idempotentHint"] = true,
                    ["openWorldHint"] = false,
                },
            });
            tools.Add(new JsonObject
            {
                ["name"] = NoteCreateToolName,
                ["description"] =
                    "Write a sticky note into the open book, stored locally by "
                    + "the Reader. Pass only the text: the Reader anchors it "
                    + "using the page it is actually showing, because the "
                    + "bridge cannot know where the reader is. Works in PDF "
                    + "and EPUB. Refuses on an empty note, on a surface that "
                    + "has no notes, and when the write does not commit; each "
                    + "is a distinct error. Do not retry an unknown outcome.",
                ["inputSchema"] = new JsonObject
                {
                    ["type"] = "object",
                    ["properties"] = new JsonObject
                    {
                        ["text"] = new JsonObject
                        {
                            ["type"] = "string",
                            ["minLength"] = 1,
                            ["maxLength"] = 4000,
                            ["description"] = "The note's contents.",
                        },
                    },
                    ["required"] = new JsonArray { "text" },
                    ["additionalProperties"] = false,
                },
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
                ["name"] = UndoLastToolName,
                ["description"] =
                    "Undo the most recent undoable assistant change recorded "
                    + "by the App in the "
                    + "currently focused PDF or EPUB. The App chooses the "
                    + "trusted current surface; this tool accepts no surface, "
                    + "file, page or action parameters. HTML and ordinary web "
                    + "hosts are rejected. A timeout or unknown outcome must "
                    + "not be retried blindly.",
                ["inputSchema"] = new JsonObject
                {
                    ["type"] = "object",
                    ["additionalProperties"] = false,
                    ["properties"] = new JsonObject(),
                },
                ["annotations"] = new JsonObject
                {
                    ["readOnlyHint"] = false,
                    ["destructiveHint"] = true,
                    ["idempotentHint"] = false,
                    ["openWorldHint"] = false,
                },
            });
            tools.Add(new JsonObject
            {
                ["name"] = AnkiDraftToolName,
                ["description"] =
                    "Deliver editable Anki card drafts. For a normal card that "
                    + "does not quote the open book, pass cards only; the Reader "
                    + "will not invent current-page provenance. When the card "
                    + "does quote the book, also pass file, target and verbatim "
                    + "sourceText after reader_context_snapshot; all three are "
                    + "required together and the Reader requires exactly one "
                    + "match before showing its confirmation UI. This tool never writes "
                    + "Anki: success means only draft_delivered. The user "
                    + "must click the existing Add to Anki button.",
                ["inputSchema"] = BuildExactSourceArgumentsSchema(true),
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
                    + "images={items:[{url,title?,aid?,src?}]} where url is "
                    + "a public HTTPS address that directly returns image/* "
                    + "bytes, never the webpage containing the image, "
                    + "videos={items:[{title,thumb?,url?,channel?,src?}]} "
                    + "where url is a complete YouTube or Bilibili HTTPS "
                    + "watch/share URL (do not fabricate a video id), "
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

        // 这些工具的实现走查询通道，因此可见性必须跟着同一个依赖。挂在
        // _sendOutputAsync 上曾让声明与分发各用一个条件：只配其中一个时，
        // 工具要么列得出来却调不动，要么能调却不出现在清单里。
        if (_queryReaderAsync is not null)
        {
            tools.Add(new JsonObject
            {
                ["name"] = NotesToolName,
                ["description"] =
                    "Read the sticky notes in the book that is open, from the "
                    + "Reader's own store. Optionally narrow to one page or "
                    + "to notes containing a phrase. Returns each note's id, "
                    + "page and text; the id is what reader_note_edit takes. "
                    + "If truncated is true the list is a prefix, not the "
                    + "whole set. Works only while a PDF or EPUB is open. "
                    + "Safe to retry.",
                ["inputSchema"] = new JsonObject
                {
                    ["type"] = "object",
                    ["properties"] = new JsonObject
                    {
                        ["page"] = new JsonObject
                        {
                            ["type"] = "integer",
                            ["minimum"] = 1,
                            ["description"] =
                                "Restrict to this page (PDF) or section "
                                + "(EPUB). Omit for the whole book.",
                        },
                        ["contains"] = new JsonObject
                        {
                            ["type"] = "string",
                            ["minLength"] = 1,
                            ["maxLength"] = 256,
                            ["description"] =
                                "Restrict to notes containing this phrase.",
                        },
                    },
                    ["additionalProperties"] = false,
                },
                ["annotations"] = new JsonObject
                {
                    ["readOnlyHint"] = true,
                    ["destructiveHint"] = false,
                    ["idempotentHint"] = true,
                    ["openWorldHint"] = false,
                },
            });
            tools.Add(new JsonObject
            {
                ["name"] = TocToolName,
                ["description"] =
                    "Read the table of contents of the open PDF, with each "
                    + "entry's title, page and nesting level. An empty list "
                    + "is a real answer - the book may simply have no "
                    + "contents built yet - and is not the same as being "
                    + "unable to read it. PDF only; EPUB structure comes from "
                    + "its own manifest. Works offline. Safe to retry.",
                ["inputSchema"] = new JsonObject
                {
                    ["type"] = "object",
                    ["properties"] = new JsonObject(),
                    ["additionalProperties"] = false,
                },
                ["annotations"] = new JsonObject
                {
                    ["readOnlyHint"] = true,
                    ["destructiveHint"] = false,
                    ["idempotentHint"] = true,
                    ["openWorldHint"] = false,
                },
            });
            tools.Add(new JsonObject
            {
                ["name"] = LookupToolName,
                ["description"] =
                    "Look a word up in the user's dictionary. This one needs "
                    + "the Pi: the dictionary does not live on the device, so "
                    + "with no connection it fails plainly rather than "
                    + "answering from nothing. The lookup is not recorded "
                    + "against the user's vocabulary - you looking a word up "
                    + "is not the same as them meeting it while reading, and "
                    + "counting it would skew their statistics. Safe to "
                    + "retry.",
                ["inputSchema"] = new JsonObject
                {
                    ["type"] = "object",
                    ["properties"] = new JsonObject
                    {
                        ["word"] = new JsonObject
                        {
                            ["type"] = "string",
                            ["minLength"] = 1,
                            ["maxLength"] = 128,
                            ["description"] = "The word to look up.",
                        },
                    },
                    ["required"] = new JsonArray { "word" },
                    ["additionalProperties"] = false,
                },
                ["annotations"] = new JsonObject
                {
                    ["readOnlyHint"] = true,
                    ["destructiveHint"] = false,
                    ["idempotentHint"] = true,
                    ["openWorldHint"] = false,
                },
            });
            tools.Add(new JsonObject
            {
                ["name"] = PageTextToolName,
                ["description"] =
                    "Read the text of one page (PDF) or section (EPUB) of the "
                    + "open book, from the Reader's own extraction. The text "
                    + "is capped, and truncated says whether it hit that cap "
                    + "- with truncated true you are looking at the start of "
                    + "the page, not all of it. Works offline. Safe to retry.",
                ["inputSchema"] = new JsonObject
                {
                    ["type"] = "object",
                    ["properties"] = new JsonObject
                    {
                        ["page"] = new JsonObject
                        {
                            ["type"] = "integer",
                            ["minimum"] = 1,
                            ["description"] =
                                "The page (PDF) or section index (EPUB).",
                        },
                    },
                    ["required"] = new JsonArray { "page" },
                    ["additionalProperties"] = false,
                },
                ["annotations"] = new JsonObject
                {
                    ["readOnlyHint"] = true,
                    ["destructiveHint"] = false,
                    ["idempotentHint"] = true,
                    ["openWorldHint"] = false,
                },
            });
            tools.Add(new JsonObject
            {
                ["name"] = SearchToolName,
                ["description"] =
                    "Search the full text of the book that is open, using the "
                    + "Reader's own index. Returns matching pages with a "
                    + "snippet. Two different caveats come back separately "
                    + "and both matter: truncated means too many matches to "
                    + "fit, incomplete means some pages could not be searched "
                    + "at all - with incomplete true, absence of a match is "
                    + "not evidence the book lacks the phrase, so do not say "
                    + "it is not there. Works only while a PDF or EPUB is "
                    + "open. Safe to retry.",
                ["inputSchema"] = new JsonObject
                {
                    ["type"] = "object",
                    ["properties"] = new JsonObject
                    {
                        ["query"] = new JsonObject
                        {
                            ["type"] = "string",
                            ["minLength"] = 1,
                            ["maxLength"] = 256,
                            ["description"] = "The text to look for.",
                        },
                        ["limit"] = new JsonObject
                        {
                            ["type"] = "integer",
                            ["minimum"] = 1,
                            ["maximum"] = 200,
                            ["description"] =
                                "Maximum matches to return. Defaults to 50.",
                        },
                    },
                    ["required"] = new JsonArray { "query" },
                    ["additionalProperties"] = false,
                },
                ["annotations"] = new JsonObject
                {
                    ["readOnlyHint"] = true,
                    ["destructiveHint"] = false,
                    ["idempotentHint"] = true,
                    ["openWorldHint"] = false,
                },
            });
            tools.Add(new JsonObject
            {
                ["name"] = HighlightsToolName,
                ["description"] =
                    "Read the highlights the user has made in the book that "
                    + "is open, straight from the Reader's own store. "
                    + "Optionally narrow to one page, or to highlights whose "
                    + "text contains a phrase. Returns each highlight's id, "
                    + "page, colour and quoted text; the id is what "
                    + "reader_undo_last takes. This is everything in the "
                    + "store, including highlights the page text you were "
                    + "given could not mark inline - a highlight can fail to "
                    + "anchor there on a line-break difference or an overlap, "
                    + "so seeing one here that is not marked in the text is "
                    + "expected, not a contradiction. If the result did not fit, "
                    + "truncated is true and the list is a prefix, not the "
                    + "whole set - say so rather than concluding from it. "
                    + "Works only while a PDF or EPUB is open. Safe to retry.",
                ["inputSchema"] = new JsonObject
                {
                    ["type"] = "object",
                    ["properties"] = new JsonObject
                    {
                        ["page"] = new JsonObject
                        {
                            ["type"] = "integer",
                            ["minimum"] = 1,
                            ["description"] =
                                "Restrict to this page (PDF) or section "
                                + "(EPUB). Omit for the whole book.",
                        },
                        ["contains"] = new JsonObject
                        {
                            ["type"] = "string",
                            ["minLength"] = 1,
                            ["maxLength"] = 256,
                            ["description"] =
                                "Restrict to highlights whose quoted text "
                                + "contains this phrase.",
                        },
                    },
                    ["additionalProperties"] = false,
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

    private static JsonObject BuildExactSourceArgumentsSchema(
        bool includeCards)
    {
        JsonObject properties = new()
        {
            ["file"] = new JsonObject
            {
                ["type"] = "string",
                ["minLength"] = 1,
                ["maxLength"] = 4_096,
            },
            ["target"] = new JsonObject
            {
                ["oneOf"] = new JsonArray
                {
                    new JsonObject
                    {
                        ["type"] = "object",
                        ["additionalProperties"] = false,
                        ["required"] = new JsonArray("kind", "page"),
                        ["properties"] = new JsonObject
                        {
                            ["kind"] = new JsonObject
                            {
                                ["const"] = "pdf",
                            },
                            ["page"] = new JsonObject
                            {
                                ["type"] = "integer",
                                ["minimum"] = 1,
                            },
                        },
                    },
                    new JsonObject
                    {
                        ["type"] = "object",
                        ["additionalProperties"] = false,
                        ["required"] = new JsonArray(
                            "kind",
                            "section"),
                        ["properties"] = new JsonObject
                        {
                            ["kind"] = new JsonObject
                            {
                                ["const"] = "epub",
                            },
                            ["section"] = new JsonObject
                            {
                                ["type"] = "integer",
                                ["minimum"] = 0,
                            },
                        },
                    },
                },
            },
        };
        JsonArray required = includeCards
            ? new JsonArray("cards")
            : new JsonArray("file", "target");
        if (includeCards)
        {
            properties["sourceText"] = SourceTextSchema();
            properties["cards"] = new JsonObject
            {
                ["type"] = "array",
                ["minItems"] = 1,
                ["maxItems"] = 12,
                ["items"] = new JsonObject
                {
                    ["oneOf"] = new JsonArray
                    {
                        new JsonObject
                        {
                            ["type"] = "object",
                            ["additionalProperties"] = false,
                            ["required"] = new JsonArray(
                                "type",
                                "front",
                                "back"),
                            ["properties"] = new JsonObject
                            {
                                ["type"] = new JsonObject
                                {
                                    ["const"] = "basic",
                                },
                                ["front"] = CardFaceSchema(),
                                ["back"] = CardFaceSchema(true),
                            },
                        },
                        new JsonObject
                        {
                            ["type"] = "object",
                            ["additionalProperties"] = false,
                            ["required"] = new JsonArray(
                                "type",
                                "cloze"),
                            ["properties"] = new JsonObject
                            {
                                ["type"] = new JsonObject
                                {
                                    ["const"] = "cloze",
                                },
                                ["cloze"] = CardFaceSchema(),
                            },
                        },
                    },
                },
            };
        }
        else
        {
            required.Add("text");
            required.Add("color");
            required.Add("note");
            properties["text"] = SourceTextSchema();
            properties["color"] = new JsonObject
            {
                ["type"] = "string",
                ["enum"] = new JsonArray(
                    "yellow",
                    "green",
                    "blue",
                    "pink"),
            };
            properties["note"] = new JsonObject
            {
                ["anyOf"] = new JsonArray
                {
                    new JsonObject
                    {
                        ["type"] = "string",
                        ["maxLength"] = 2_000,
                    },
                    new JsonObject
                    {
                        ["type"] = "null",
                    },
                },
            };
        }
        JsonObject schema = new()
        {
            ["type"] = "object",
            ["additionalProperties"] = false,
            ["required"] = required,
            ["properties"] = properties,
        };
        if (includeCards)
        {
            schema["dependentRequired"] = new JsonObject
            {
                ["file"] = new JsonArray("target", "sourceText"),
                ["target"] = new JsonArray("file", "sourceText"),
                ["sourceText"] = new JsonArray("file", "target"),
            };
        }
        return schema;
    }

    private static JsonObject BuildHighlightRangeArgumentsSchema() => new()
    {
        ["type"] = "object",
        ["additionalProperties"] = false,
        ["required"] = new JsonArray("rangeRef", "color", "note"),
        ["properties"] = new JsonObject
        {
            ["rangeRef"] = new JsonObject
            {
                ["type"] = "object",
                ["description"] =
                    "Marker boundaries copied from one currentPage.highlightSource. "
                    + "Each marker precedes its text; endMarker is exclusive.",
                ["additionalProperties"] = false,
                ["required"] = new JsonArray(
                    "contract",
                    "snapshotId",
                    "documentId",
                    "target",
                    "sourceDigest",
                    "revision",
                    "startMarker",
                    "endMarker"),
                ["properties"] = new JsonObject
                {
                    ["contract"] = new JsonObject
                    {
                        ["const"] = "reader-source-range/1",
                    },
                    ["snapshotId"] = new JsonObject
                    {
                        ["type"] = "string",
                        ["pattern"] = "^hrs_[0-9a-f]{24}$",
                    },
                    ["documentId"] = new JsonObject
                    {
                        ["type"] = "string",
                        ["minLength"] = 1,
                        ["maxLength"] = 4_096,
                    },
                    ["target"] = BuildDocumentTargetSchema(),
                    ["sourceDigest"] = new JsonObject
                    {
                        ["type"] = "string",
                        ["pattern"] =
                            "^rsd1_[0-9a-f]{8}_[0-9a-f]{16}$",
                    },
                    ["revision"] = new JsonObject
                    {
                        ["type"] = "string",
                        ["minLength"] = 1,
                        ["maxLength"] = 160,
                    },
                    ["startMarker"] = new JsonObject
                    {
                        ["type"] = "string",
                        ["pattern"] = "^m_[0-9a-z]{1,4}$",
                        ["description"] =
                            "Boundary before the first included text segment.",
                    },
                    ["endMarker"] = new JsonObject
                    {
                        ["type"] = "string",
                        ["pattern"] = "^m_[0-9a-z]{1,4}$",
                        ["description"] =
                            "Exclusive boundary before the first excluded segment; use the final empty-text marker to include through source end.",
                    },
                },
            },
            ["color"] = new JsonObject
            {
                ["type"] = "string",
                ["enum"] = new JsonArray(
                    "yellow",
                    "green",
                    "blue",
                    "pink"),
            },
            ["note"] = new JsonObject
            {
                ["anyOf"] = new JsonArray
                {
                    new JsonObject
                    {
                        ["type"] = "string",
                        ["maxLength"] = 2_000,
                    },
                    new JsonObject { ["type"] = "null" },
                },
            },
        },
    };

    private static JsonObject BuildDocumentTargetSchema() => new()
    {
        ["oneOf"] = new JsonArray
        {
            new JsonObject
            {
                ["type"] = "object",
                ["additionalProperties"] = false,
                ["required"] = new JsonArray("kind", "page"),
                ["properties"] = new JsonObject
                {
                    ["kind"] = new JsonObject { ["const"] = "pdf" },
                    ["page"] = new JsonObject
                    {
                        ["type"] = "integer",
                        ["minimum"] = 1,
                        ["maximum"] = 10_000_000,
                    },
                },
            },
            new JsonObject
            {
                ["type"] = "object",
                ["additionalProperties"] = false,
                ["required"] = new JsonArray("kind", "section"),
                ["properties"] = new JsonObject
                {
                    ["kind"] = new JsonObject { ["const"] = "epub" },
                    ["section"] = new JsonObject
                    {
                        ["type"] = "integer",
                        ["minimum"] = 0,
                        ["maximum"] = 10_000_000,
                    },
                },
            },
        },
    };

    private static JsonObject SourceTextSchema() => new()
    {
        ["type"] = "string",
        ["minLength"] = 1,
        ["maxLength"] = 2_000,
    };

    private static JsonObject CardFaceSchema(bool allowEmpty = false) => new()
    {
        ["type"] = "string",
        ["minLength"] = allowEmpty ? 0 : 1,
        ["maxLength"] = 8_000,
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
            toolName == HighlightRangeToolName
            && _sendOutputAsync is not null
        )
        {
            await HandleHighlightRangeToolCallAsync(
                id,
                arguments,
                cancellationToken).ConfigureAwait(false);
            return;
        }
        // Legacy compatibility only: old clients may still call this exact
        // name, but tools/list intentionally advertises marker ranges instead.
        if (
            toolName == HighlightTextToolName
            && _sendOutputAsync is not null
        )
        {
            await HandleExactSourceOutputToolCallAsync(
                id,
                arguments,
                "highlight-text",
                cancellationToken).ConfigureAwait(false);
            return;
        }
        if (
            toolName == LookupToolName
            && _queryReaderAsync is not null
        )
        {
            if (arguments.ValueKind != JsonValueKind.Object
                || !arguments.TryGetProperty("word", out JsonElement wordValue)
                || wordValue.ValueKind != JsonValueKind.String
                || wordValue.GetString() is not string lookupWord
                || lookupWord.Trim().Length == 0
                || lookupWord.Length > 128)
            {
                await WriteErrorAsync(
                    id,
                    -32602,
                    "Invalid Reader lookup word",
                    cancellationToken).ConfigureAwait(false);
                return;
            }
            await RunReaderQueryAsync(
                id,
                "lookup",
                new JsonObject { ["word"] = lookupWord },
                cancellationToken).ConfigureAwait(false);
            return;
        }
        if (
            toolName == WebHighlightToolName
            && _sendOutputAsync is not null
        )
        {
            string Field(string name, int maximum)
            {
                if (arguments.ValueKind != JsonValueKind.Object
                    || !arguments.TryGetProperty(name, out JsonElement value)
                    || value.ValueKind != JsonValueKind.String)
                {
                    return string.Empty;
                }
                string text = value.GetString() ?? string.Empty;
                return text.Length > maximum ? string.Empty : text;
            }
            string webExact = Field("exact", 2_000);
            if (string.IsNullOrWhiteSpace(webExact))
            {
                await WriteErrorAsync(
                    id,
                    -32602,
                    "Invalid web highlight text",
                    cancellationToken).ConfigureAwait(false);
                return;
            }
            JsonObject webPayload = new()
            {
                ["fn"] = "_bwWebHighlightByText",
                ["args"] = new JsonArray
                {
                    new JsonObject
                    {
                        ["exact"] = webExact,
                        ["prefix"] = Field("prefix", 200),
                        ["suffix"] = Field("suffix", 200),
                        ["color"] = Field("color", 32),
                        ["note"] = Field("note", 2_000),
                    },
                },
            };
            await SendReaderOutputAsync(
                id,
                "client-action",
                webPayload,
                cancellationToken).ConfigureAwait(false);
            return;
        }
        if (
            toolName == MarkVocabToolName
            && _sendOutputAsync is not null
        )
        {
            string vocabWord = arguments.ValueKind == JsonValueKind.Object
                && arguments.TryGetProperty("word", out JsonElement vocabValue)
                && vocabValue.ValueKind == JsonValueKind.String
                ? vocabValue.GetString() ?? string.Empty
                : string.Empty;
            string vocabMark = arguments.ValueKind == JsonValueKind.Object
                && arguments.TryGetProperty("mark", out JsonElement markValue)
                && markValue.ValueKind == JsonValueKind.String
                ? markValue.GetString() ?? string.Empty
                : string.Empty;
            if (string.IsNullOrWhiteSpace(vocabWord)
                || vocabWord.Length > 128
                || vocabMark is not ("known" or "unknown"))
            {
                await WriteErrorAsync(
                    id,
                    -32602,
                    "Invalid Reader vocabulary mark",
                    cancellationToken).ConfigureAwait(false);
                return;
            }
            JsonObject vocabPayload = new()
            {
                ["fn"] = "_nativeReaderMarkVocabulary",
                ["args"] = new JsonArray
                {
                    new JsonObject
                    {
                        ["word"] = vocabWord,
                        ["mark"] = vocabMark,
                    },
                },
            };
            await SendReaderOutputAsync(
                id,
                "client-action",
                vocabPayload,
                cancellationToken).ConfigureAwait(false);
            return;
        }
        if (
            toolName == MakeNoteToolName
            && _sendOutputAsync is not null
        )
        {
            string madeText = arguments.ValueKind == JsonValueKind.Object
                && arguments.TryGetProperty("text", out JsonElement madeValue)
                && madeValue.ValueKind == JsonValueKind.String
                ? madeValue.GetString() ?? string.Empty
                : string.Empty;
            string madeTitle = arguments.ValueKind == JsonValueKind.Object
                && arguments.TryGetProperty("title", out JsonElement titleValue)
                && titleValue.ValueKind == JsonValueKind.String
                ? titleValue.GetString() ?? string.Empty
                : string.Empty;
            if (string.IsNullOrWhiteSpace(madeText)
                || madeText.Length > 240_000
                || madeTitle.Length > 240)
            {
                await WriteErrorAsync(
                    id,
                    -32602,
                    "Invalid Reader note document",
                    cancellationToken).ConfigureAwait(false);
                return;
            }
            JsonObject madePayload = new()
            {
                ["fn"] = "_nativeReaderMakeNote",
                ["args"] = new JsonArray
                {
                    new JsonObject
                    {
                        ["title"] = madeTitle,
                        ["text"] = madeText,
                    },
                },
            };
            await SendReaderOutputAsync(
                id,
                "client-action",
                madePayload,
                cancellationToken).ConfigureAwait(false);
            return;
        }
        if (
            toolName == TocToolName
            && _queryReaderAsync is not null
        )
        {
            await RunReaderQueryAsync(
                id,
                "toc",
                new JsonObject(),
                cancellationToken).ConfigureAwait(false);
            return;
        }
        if (
            toolName == PageTextToolName
            && _queryReaderAsync is not null
        )
        {
            if (arguments.ValueKind != JsonValueKind.Object
                || !arguments.TryGetProperty("page", out JsonElement pageOnly)
                || pageOnly.ValueKind != JsonValueKind.Number
                || !pageOnly.TryGetInt64(out long pageIndex)
                || pageIndex < 1
                || pageIndex > int.MaxValue)
            {
                await WriteErrorAsync(
                    id,
                    -32602,
                    "Invalid Reader page",
                    cancellationToken).ConfigureAwait(false);
                return;
            }
            await RunReaderQueryAsync(
                id,
                "page-text",
                new JsonObject { ["page"] = pageIndex },
                cancellationToken).ConfigureAwait(false);
            return;
        }
        if (
            toolName == SearchToolName
            && _queryReaderAsync is not null
        )
        {
            if (arguments.ValueKind != JsonValueKind.Object
                || !arguments.TryGetProperty("query", out JsonElement queryText)
                || queryText.ValueKind != JsonValueKind.String
                || queryText.GetString() is not string searchText
                || searchText.Trim().Length == 0
                || searchText.Length
                    > ReaderQueryProtocol.MaximumQueryTextCharacters)
            {
                await WriteErrorAsync(
                    id,
                    -32602,
                    "Invalid Reader search text",
                    cancellationToken).ConfigureAwait(false);
                return;
            }
            JsonObject searchParameters = new() { ["query"] = searchText };
            if (arguments.TryGetProperty("limit", out JsonElement limitValue))
            {
                if (limitValue.ValueKind != JsonValueKind.Number
                    || !limitValue.TryGetInt64(out long limit)
                    || limit < 1
                    || limit > 200)
                {
                    await WriteErrorAsync(
                        id,
                        -32602,
                        "Invalid Reader search limit",
                        cancellationToken).ConfigureAwait(false);
                    return;
                }
                searchParameters["limit"] = limit;
            }
            await RunReaderQueryAsync(
                id,
                "search",
                searchParameters,
                cancellationToken).ConfigureAwait(false);
            return;
        }
        if (
            (toolName == HighlightsToolName || toolName == NotesToolName)
            && _queryReaderAsync is not null
        )
        {
            JsonObject queryParameters = new();
            if (arguments.ValueKind == JsonValueKind.Object)
            {
                if (arguments.TryGetProperty("page", out JsonElement pageValue))
                {
                    if (pageValue.ValueKind != JsonValueKind.Number
                        || !pageValue.TryGetInt64(out long pageNumber)
                        || pageNumber < 1
                        || pageNumber > int.MaxValue)
                    {
                        await WriteErrorAsync(
                            id,
                            -32602,
                            "Invalid Reader highlight page",
                            cancellationToken).ConfigureAwait(false);
                        return;
                    }
                    queryParameters["page"] = pageNumber;
                }
                if (arguments.TryGetProperty(
                        "contains",
                        out JsonElement containsValue))
                {
                    if (containsValue.ValueKind != JsonValueKind.String
                        || containsValue.GetString() is not string contains
                        || contains.Length == 0
                        || contains.Length
                            > ReaderQueryProtocol.MaximumQueryTextCharacters)
                    {
                        await WriteErrorAsync(
                            id,
                            -32602,
                            "Invalid Reader highlight filter",
                            cancellationToken).ConfigureAwait(false);
                        return;
                    }
                    queryParameters["contains"] = contains;
                }
            }
            await RunReaderQueryAsync(
                id,
                toolName == NotesToolName ? "notes" : "highlights",
                queryParameters,
                cancellationToken).ConfigureAwait(false);
            return;
        }
        if (
            toolName == NoteEditToolName
            && _sendOutputAsync is not null
        )
        {
            string editId = arguments.ValueKind == JsonValueKind.Object
                && arguments.TryGetProperty("id", out JsonElement editIdValue)
                && editIdValue.ValueKind == JsonValueKind.String
                ? editIdValue.GetString() ?? string.Empty
                : string.Empty;
            string editText = arguments.ValueKind == JsonValueKind.Object
                && arguments.TryGetProperty("text", out JsonElement editValue)
                && editValue.ValueKind == JsonValueKind.String
                ? editValue.GetString() ?? string.Empty
                : string.Empty;
            if (editId.Length == 0
                || editId.Length > 64
                || string.IsNullOrWhiteSpace(editText)
                || editText.Length > 4_000)
            {
                await WriteErrorAsync(
                    id,
                    -32602,
                    "Invalid Reader note edit",
                    cancellationToken).ConfigureAwait(false);
                return;
            }
            JsonObject editPayload = new()
            {
                ["fn"] = "_nativeReaderEditNote",
                ["args"] = new JsonArray
                {
                    new JsonObject { ["id"] = editId, ["text"] = editText },
                },
            };
            await SendReaderOutputAsync(
                id,
                "client-action",
                editPayload,
                cancellationToken).ConfigureAwait(false);
            return;
        }
        if (
            toolName == NoteCreateToolName
            && _sendOutputAsync is not null
        )
        {
            string noteText = arguments.ValueKind == JsonValueKind.Object
                && arguments.TryGetProperty("text", out JsonElement noteValue)
                && noteValue.ValueKind == JsonValueKind.String
                ? noteValue.GetString() ?? string.Empty
                : string.Empty;
            if (string.IsNullOrWhiteSpace(noteText) || noteText.Length > 4_000)
            {
                await WriteErrorAsync(
                    id,
                    -32602,
                    "Invalid Reader note text",
                    cancellationToken).ConfigureAwait(false);
                return;
            }
            JsonObject notePayload = new()
            {
                ["fn"] = "_nativeReaderCreateNote",
                ["args"] = new JsonArray
                {
                    new JsonObject { ["text"] = noteText },
                },
            };
            await SendReaderOutputAsync(
                id,
                "client-action",
                notePayload,
                cancellationToken).ConfigureAwait(false);
            return;
        }
        if (
            toolName == UndoLastToolName
            && _sendOutputAsync is not null
        )
        {
            await HandleUndoLastToolCallAsync(
                id,
                arguments,
                cancellationToken).ConfigureAwait(false);
            return;
        }
        if (
            toolName == AnkiDraftToolName
            && _sendOutputAsync is not null
        )
        {
            await HandleExactSourceOutputToolCallAsync(
                id,
                arguments,
                "anki-draft",
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
        await AttachOutputAccessAsync(payload, cancellationToken)
            .ConfigureAwait(false);
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

    private async Task HandleExactSourceOutputToolCallAsync(
        JsonNode id,
        JsonElement arguments,
        string kind,
        CancellationToken cancellationToken)
    {
        if (!TryReadExactSourceOutput(arguments, kind, out JsonNode payload))
        {
            await WriteErrorAsync(
                id,
                -32602,
                kind == "anki-draft"
                    ? "Invalid Reader Anki draft"
                    : "Invalid Reader exact-text highlight",
                cancellationToken).ConfigureAwait(false);
            return;
        }
        if (kind == "highlight-text")
        {
            await TryLoadLatestAsync(cancellationToken).ConfigureAwait(false);
            JsonObject current = BuildToolPayload();
            if (current["currentPage"]?["highlightSource"] is JsonObject)
            {
                await WriteReaderOutputToolErrorAsync(
                    id,
                    "BW_READER_HIGHLIGHT_RANGE_REQUIRED",
                    "当前书页已提供 marker 范围；旧全文反查高亮已拒绝。请重新读取 Reader 上下文并调用 reader_highlight_range。",
                    cancellationToken).ConfigureAwait(false);
                return;
            }
        }
        await SendReaderOutputAsync(
            id,
            kind,
            payload,
            cancellationToken).ConfigureAwait(false);
    }

    private async Task HandleHighlightRangeToolCallAsync(
        JsonNode id,
        JsonElement arguments,
        CancellationToken cancellationToken)
    {
        if (!TryReadHighlightRangeOutput(
            arguments,
            out JsonNode payload,
            out JsonObject rangeRef))
        {
            await WriteErrorAsync(
                id,
                -32602,
                "Invalid Reader marker-range highlight",
                cancellationToken).ConfigureAwait(false);
            return;
        }
        await SendReaderOutputAsync(
            id,
            "highlight-range",
            payload,
            cancellationToken,
            rangeRef).ConfigureAwait(false);
    }

    private async Task HandleUndoLastToolCallAsync(
        JsonNode id,
        JsonElement arguments,
        CancellationToken cancellationToken)
    {
        if (!HasNoArguments(arguments))
        {
            await WriteErrorAsync(
                id,
                -32602,
                "Invalid Reader undo request",
                cancellationToken).ConfigureAwait(false);
            return;
        }
        string operationId =
            "rundo_" + Guid.NewGuid().ToString("N")[..24];
        JsonNode payload = ReaderRealtimeOutputProtocol.ValidatePayload(
            "client-action",
            new JsonObject
            {
                ["fn"] = "_nativeReaderUndoLast",
                ["args"] = new JsonArray(operationId),
            });
        await SendReaderOutputAsync(
            id,
            "client-action",
            payload,
            cancellationToken).ConfigureAwait(false);
    }

    // 查询的失败必须能被区分。「没有高亮」和「这本书还没就绪」在助手那里会导出
    // 完全不同的话；把两者都答成一个空列表，它就会替用户断定「你没有划过」。
    private async Task RunReaderQueryAsync(
        JsonNode id,
        string query,
        JsonObject parameters,
        CancellationToken cancellationToken)
    {
        JsonObject snapshot = BuildToolPayload();
        ReaderQueryRequest? request = BuildQueryRequest(
            snapshot,
            query,
            parameters);
        if (request is null)
        {
            await WriteReaderOutputToolErrorAsync(
                id,
                "BW_READER_QUERY_SOURCE_NOT_READY",
                "当前快照没有已就绪的 PDF/EPUB 阅读来源。请先打开书或重新读取 Reader 上下文。",
                cancellationToken).ConfigureAwait(false);
            return;
        }
        ReaderQueryResponse response;
        try
        {
            response = await _queryReaderAsync!(request, cancellationToken)
                .ConfigureAwait(false);
        }
        catch (ReaderQueryException exception)
        {
            await WriteReaderOutputToolErrorAsync(
                id,
                exception.Code,
                exception.Message,
                cancellationToken).ConfigureAwait(false);
            return;
        }
        if (response.Status != "ok")
        {
            await WriteReaderOutputToolErrorAsync(
                id,
                response.Status == "unsupported"
                    ? "BW_READER_QUERY_UNSUPPORTED"
                    : "BW_READER_QUERY_UNAVAILABLE",
                response.Status == "unsupported"
                    ? "当前阅读界面不支持该查询。"
                    : "Reader 暂时无法回答该查询。",
                cancellationToken).ConfigureAwait(false);
            return;
        }
        JsonObject result = new()
        {
            ["query"] = query,
            ["file"] = request.File,
            ["revision"] = request.SnapshotRevision,
            // 截断状态永远显式出现，即使是 false。缺省它就等于让读到的人
            // 自行假设「全都在这儿了」—— 那正是静默截断最有害的地方。
            ["truncated"] = response.Truncated,
            ["result"] = JsonNode.Parse(response.Result.GetRawText()),
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
                        ["text"] = result.ToJsonString(
                            DirectBridgeContract.JsonOptions),
                    },
                },
            },
            cancellationToken).ConfigureAwait(false);
    }

    private async Task SendReaderOutputAsync(
        JsonNode id,
        string kind,
        JsonNode payload,
        CancellationToken cancellationToken,
        JsonObject? expectedRangeRef = null)
    {
        await TryLoadLatestAsync(cancellationToken).ConfigureAwait(false);
        JsonObject current = BuildToolPayload();
        if (
            expectedRangeRef is not null
            && !HighlightRangeMatchesSnapshot(
                current,
                expectedRangeRef,
                _utcNow())
        )
        {
            await WriteReaderOutputToolErrorAsync(
                id,
                "BW_READER_HIGHLIGHT_RANGE_STALE",
                "高亮 marker 范围已过期、顺序无效或不再属于当前书页。请重新读取 Reader 上下文；不会回退全文搜索。",
                cancellationToken).ConfigureAwait(false);
            return;
        }
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

        if (_probeOutputSourceAsync is not null)
        {
            ReaderRealtimeOutputSourceStatus status;
            try
            {
                status = await _probeOutputSourceAsync(
                    request.SourceInstanceId,
                    cancellationToken).ConfigureAwait(false);
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
            if (!status.Online)
            {
                await WriteReaderOutputToolErrorAsync(
                    id,
                    "BW_READER_REALTIME_OUTPUT_SOURCE_OFFLINE",
                    "当前 Reader 页面仍可读取缓存，但实时来源已离线；请重新打开或唤醒该页面后再试。",
                    cancellationToken).ConfigureAwait(false);
                return;
            }
        }

        if (request.Kind == "anki-draft")
        {
            try
            {
                await _localAnkiRegistry.RegisterDraftAsync(
                    request,
                    cancellationToken).ConfigureAwait(false);
            }
            catch (ReaderLocalAnkiException exception)
            {
                await WriteReaderOutputToolErrorAsync(
                    id,
                    exception.Code,
                    exception.Message,
                    cancellationToken).ConfigureAwait(false);
                return;
            }
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
                            ["status"] = request.Kind switch
                            {
                                "anki-draft" => "draft_delivered",
                                "highlight-text" => "highlight_saved",
                                "highlight-range" => "highlight_saved",
                                _ => "delivered",
                            },
                            ["anki_written"] = request.Kind == "anki-draft"
                                ? false
                                : null,
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

    internal static bool HighlightRangeMatchesSnapshot(
        JsonObject snapshot,
        JsonObject rangeRef,
        DateTimeOffset now)
    {
        if (
            StringValue(snapshot["contextStatus"]) != "ready"
            || snapshot["activeReading"] is not JsonObject active
            || active["fresh"]?.GetValue<bool?>() != true
            || snapshot["currentPage"] is not JsonObject page
            || page["stable"]?.GetValue<bool?>() != true
            || page["highlightSource"] is not JsonObject source
            || source["markers"] is not JsonArray markers
            || LongValue(source["expiresAt"]) is not long expiresAt
            || expiresAt <= now.ToUnixTimeMilliseconds()
            || StringValue(active["sourceInstanceId"])
                is not string activeSource
            || !string.Equals(
                activeSource,
                StringValue(page["sourceInstanceId"]),
                StringComparison.Ordinal)
            || StringValue(page["file"]) is not string file
            || !string.Equals(
                file,
                StringValue(active["file"]),
                StringComparison.Ordinal)
            || !string.Equals(
                file,
                StringValue(source["documentId"]),
                StringComparison.Ordinal)
            || !string.Equals(
                file,
                StringValue(rangeRef["documentId"]),
                StringComparison.Ordinal)
        )
        {
            return false;
        }
        foreach (string field in new[]
            {
                "snapshotId",
                "documentId",
                "sourceDigest",
                "revision",
            })
        {
            if (!string.Equals(
                StringValue(source[field]),
                StringValue(rangeRef[field]),
                StringComparison.Ordinal))
            {
                return false;
            }
        }
        if (
            StringValue(rangeRef["contract"])
                != "reader-source-range/1"
            || StringValue(source["contract"])
                != "reader-highlight-source/1"
            || !JsonNode.DeepEquals(
                source["target"],
                rangeRef["target"])
            || !HighlightRangeTargetMatchesPage(
                rangeRef["target"] as JsonObject,
                page)
        )
        {
            return false;
        }
        string? start = StringValue(rangeRef["startMarker"]);
        string? end = StringValue(rangeRef["endMarker"]);
        if (start is null || end is null || start == end)
        {
            return false;
        }
        int startIndex = -1;
        int endIndex = -1;
        for (int index = 0; index < markers.Count; index += 1)
        {
            if (markers[index] is not JsonObject marker)
            {
                return false;
            }
            string? markerId = StringValue(marker["marker"]);
            if (markerId == start)
            {
                startIndex = index;
            }
            if (markerId == end)
            {
                endIndex = index;
            }
        }
        return startIndex >= 0 && endIndex > startIndex;
    }

    private static bool HighlightRangeTargetMatchesPage(
        JsonObject? target,
        JsonObject page)
    {
        if (
            target is null
            || StringValue(page["kind"]) is not string kind
            || LongValue(page["page"]) is not long current
            || StringValue(target["kind"]) != kind
        )
        {
            return false;
        }
        return kind switch
        {
            "pdf" => LongValue(target["page"]) == current,
            "epub" => LongValue(target["section"]) == current,
            _ => false,
        };
    }

    private static bool TryReadExactSourceOutput(
        JsonElement arguments,
        string kind,
        out JsonNode payload)
    {
        payload = new JsonObject();
        if (
            arguments.ValueKind != JsonValueKind.Object
            || kind is not ("highlight-text" or "anki-draft")
        )
        {
            return false;
        }
        try
        {
            DirectJsonValidation.RequireNoDuplicateKeys(arguments);
            HashSet<string> actual = arguments.EnumerateObject()
                .Select(property => property.Name)
                .ToHashSet(StringComparer.Ordinal);
            HashSet<string> exactExpected = kind == "highlight-text"
                ? new HashSet<string>(
                    ["file", "target", "text", "color", "note"],
                    StringComparer.Ordinal)
                : new HashSet<string>(
                    ["file", "target", "sourceText", "cards"],
                    StringComparer.Ordinal);
            bool genericAnki = kind == "anki-draft"
                && actual.SetEquals(new[] { "cards" });
            if (!actual.SetEquals(exactExpected) && !genericAnki)
            {
                return false;
            }
            JsonObject normalized = JsonNode.Parse(arguments.GetRawText())
                as JsonObject
                ?? throw new JsonException("Reader exact source is empty");
            if (kind == "highlight-text")
            {
                normalized["mutationId"] =
                    "c_" + Guid.NewGuid().ToString("N");
            }
            else
            {
                normalized["draftId"] =
                    "draft-" + Guid.NewGuid().ToString("N");
            }
            payload = ReaderRealtimeOutputProtocol.ValidatePayload(
                kind,
                normalized);
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

    private static bool TryReadHighlightRangeOutput(
        JsonElement arguments,
        out JsonNode payload,
        out JsonObject rangeRef)
    {
        payload = new JsonObject();
        rangeRef = new JsonObject();
        if (arguments.ValueKind != JsonValueKind.Object)
        {
            return false;
        }
        try
        {
            DirectJsonValidation.RequireNoDuplicateKeys(arguments);
            JsonProperty[] fields = arguments.EnumerateObject().ToArray();
            if (
                fields.Length != 3
                || !fields.Select(field => field.Name).ToHashSet(
                    StringComparer.Ordinal).SetEquals(
                        new[] { "rangeRef", "color", "note" })
                || arguments.GetProperty("rangeRef").ValueKind
                    != JsonValueKind.Object
            )
            {
                return false;
            }
            JsonObject normalized = JsonNode.Parse(arguments.GetRawText())
                as JsonObject
                ?? throw new JsonException("Reader range source is empty");
            normalized["mutationId"] =
                "c_" + Guid.NewGuid().ToString("N");
            payload = ReaderRealtimeOutputProtocol.ValidatePayload(
                "highlight-range",
                normalized);
            rangeRef = payload["rangeRef"]?.DeepClone() as JsonObject
                ?? throw new JsonException("Reader range reference is empty");
            return true;
        }
        catch (Exception exception) when (
            exception is JsonException
            or DirectProtocolException
            or ReaderRealtimeOutputException)
        {
            payload = new JsonObject();
            rangeRef = new JsonObject();
            return false;
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

    // 查询面向书，不面向网页：本机高亮库属于 PDF/EPUB 阅读器，普通网页没有它。
    // 这跟 browser-control 只认 web 正好互补，两者都不该去猜对方的场景。
    internal static ReaderQueryRequest? BuildQueryRequest(
        JsonObject payload,
        string query,
        JsonObject parameters)
    {
        if (
            !ReaderQueryProtocol.IsQuery(query)
            || LongValue(payload["revision"]) is not long revision
            || revision < 0
            || payload["contextStatus"]?.GetValue<string>() != "ready"
            || payload["activeReading"] is not JsonObject active
            || StringValue(active["kind"]) is not string kind
            || !ReaderQueryProtocol.IsQueryForSurface(query, kind)
            || StringValue(active["sourceInstanceId"])
                is not string activeSource
            || !DirectBridgeContract.IsSafeId(activeSource)
            || StringValue(active["file"]) is not string file
            || string.IsNullOrWhiteSpace(file)
        )
        {
            return null;
        }
        return new ReaderQueryRequest(
            "query-" + Guid.NewGuid().ToString("N"),
            activeSource,
            revision,
            file,
            query,
            parameters);
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

    private async Task AttachOutputAccessAsync(
        JsonObject payload,
        CancellationToken cancellationToken)
    {
        ReaderVisualDeliveryRequest? identity = BuildVisualRequest(
            payload,
            "viewport-context",
            null);
        JsonObject access = new()
        {
            ["configured"] = _sendOutputAsync is not null,
            ["available"] = false,
            ["verified"] = false,
            ["sourceInstanceId"] = identity?.SourceInstanceId,
            ["reason"] = identity is null
                ? "snapshot-not-actionable"
                : "status-probe-unavailable",
        };
        payload["outputAccess"] = access;
        if (
            _sendOutputAsync is null
            || identity is null
            || _probeOutputSourceAsync is null
        )
        {
            return;
        }
        try
        {
            ReaderRealtimeOutputSourceStatus status =
                await _probeOutputSourceAsync(
                    identity.SourceInstanceId,
                    cancellationToken).ConfigureAwait(false);
            access["available"] = status.Online;
            access["verified"] = true;
            access["reason"] = status.Online
                ? null
                : "source-offline";
        }
        catch (ReaderRealtimeOutputException exception)
        {
            access["available"] = false;
            access["verified"] = true;
            access["reason"] = exception.Code;
        }
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
