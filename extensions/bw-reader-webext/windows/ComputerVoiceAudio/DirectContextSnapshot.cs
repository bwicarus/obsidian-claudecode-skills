using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Globalization;

namespace BwReader.ComputerVoiceAudio;

internal sealed record DirectActiveReading(
    string Kind,
    string File,
    string? Title,
    JsonElement Page,
    string SelectionState,
    string? Selection,
    long ObservedAtEpochMilliseconds,
    string? ViewFile = null,
    JsonElement? ViewPage = null,
    string? SourceInstanceId = null,
    JsonElement? SelectionRegions = null,
    JsonElement? HighlightSource = null,
    // 选中附近的原文与它的来历(pdf-sentence / epub-paragraph / web-block)。
    // 可选:旧版前端不发,缺席就是今天的行为。selection 本体保持字符串不动 ——
    // 链上有多处按 typeof selection === "string" 把关,改它的形状会让旧版
    // 前端的选中被整条静默清空。
    string? SelectionContext = null,
    string? SelectionContextSource = null,
    // 复习模式投影。可选:缺席 = 未进入复习模式,旧版前端不发这个字段,
    // 缺席就是今天的行为。在场时形状由 ValidateReviewState 把守。
    JsonElement? Review = null);

internal sealed record DirectViewportContext(
    string SourceInstanceId,
    string DocumentKey,
    string Url,
    string? Title,
    string BeforeText,
    string VisibleText,
    string AfterText,
    string SelectionState,
    string? Selection,
    long ObservedAtEpochMilliseconds,
    string? ControlCorrelation = null);

internal sealed record DirectSnapshotForwardResult(
    string Outcome,
    long Revision);

internal interface IDirectSnapshotContextAdapter
{
    Task<DirectSnapshotForwardResult> ForwardJournalAsync(
        string requestId,
        string sessionId,
        DirectContextEvent contextEvent,
        CancellationToken cancellationToken);

    Task<DirectSnapshotForwardResult> ForwardActiveReadingAsync(
        string requestId,
        string sessionId,
        DirectActiveReading activeReading,
        CancellationToken cancellationToken);

    Task<DirectSnapshotForwardResult> ForwardViewportAsync(
        string requestId,
        string sessionId,
        DirectViewportContext viewport,
        CancellationToken cancellationToken);

    Task<DirectSnapshotForwardResult> ClearAsync(
        string requestId,
        string sessionId,
        CancellationToken cancellationToken);
}

internal sealed class UnwiredDirectSnapshotContextAdapter :
    IDirectSnapshotContextAdapter
{
    public Task<DirectSnapshotForwardResult> ForwardJournalAsync(
        string requestId,
        string sessionId,
        DirectContextEvent contextEvent,
        CancellationToken cancellationToken) =>
        Task.FromException<DirectSnapshotForwardResult>(Unavailable());

    public Task<DirectSnapshotForwardResult> ForwardActiveReadingAsync(
        string requestId,
        string sessionId,
        DirectActiveReading activeReading,
        CancellationToken cancellationToken) =>
        Task.FromException<DirectSnapshotForwardResult>(Unavailable());

    public Task<DirectSnapshotForwardResult> ForwardViewportAsync(
        string requestId,
        string sessionId,
        DirectViewportContext viewport,
        CancellationToken cancellationToken) =>
        Task.FromException<DirectSnapshotForwardResult>(Unavailable());

    public Task<DirectSnapshotForwardResult> ClearAsync(
        string requestId,
        string sessionId,
        CancellationToken cancellationToken) =>
        Task.FromException<DirectSnapshotForwardResult>(Unavailable());

    private static DirectProtocolException Unavailable() =>
        new(
            "BW_READER_CONTEXT_SNAPSHOT_UNAVAILABLE",
            "Windows 本地 Reader 快照存储尚未接线",
            retryable: true);
}

internal sealed class FileDirectSnapshotContextAdapter :
    IDirectSnapshotContextAdapter
{
    internal const string SnapshotContract = "reader-context-snapshot/1";
    internal const string ActiveReadingContract =
        "reader-active-reading/1";
    internal const string SnapshotFileName =
        "reader-context-snapshot.json";
    internal const string MarkdownFileName =
        "reader-context-live.md";

    internal const int MaximumSnapshotBytes = 512 * 1024;
    private const int RecentEventLimit = 256;
    private static readonly UTF8Encoding Utf8WithoutBom = new(
        encoderShouldEmitUTF8Identifier: false);

    private readonly string _statePath;
    private readonly Func<DateTimeOffset> _utcNow;
    private readonly string _producerInstanceId =
        Guid.NewGuid().ToString("N");
    private readonly SemaphoreSlim _gate = new(1, 1);
    private readonly Queue<string> _recentEventOrder = new();
    private readonly HashSet<string> _recentEventIds =
        new(StringComparer.Ordinal);
    private long _revision;
    private JsonObject? _stablePage;
    private JsonObject? _activeReading;
    private JsonObject _selection = UnknownSelection(
        "snapshot-not-received");
    private JsonObject _focus = UnknownFocus(
        "snapshot-not-received");
    private JsonObject? _latestEvent;

    // 「用户刚做了什么」——不同于 _latestEvent(内部记账,装的是折叠出来的系统
    // 事件类型,readerpc.recovering 那种)。这里只在几处明确知道是**真实用户
    // 动作**的地方追加(翻页/画完一笔),≤5 条、30 秒窗、按当前书清空——不做
    // 事件全覆盖,宁可少而准。参见 references/local-first-data-architecture.md
    // 第 16 条。跟 _latestEvent 一样不落盘:重启后没有"最近"可言,清空是对的。
    private readonly List<JsonObject> _recentActions = new();
    private string? _recentActionsFile;
    private const int MaximumRecentActions = 5;
    private static readonly TimeSpan RecentActionsWindow =
        TimeSpan.FromSeconds(30);

    private sealed record AdapterState(
        long Revision,
        JsonObject? StablePage,
        JsonObject? ActiveReading,
        JsonObject Selection,
        JsonObject Focus,
        JsonObject? LatestEvent,
        IReadOnlyList<string> RecentEventOrder,
        IReadOnlyList<JsonObject> RecentActions,
        string? RecentActionsFile);

    private sealed record FocusFoldResult(
        JsonObject Focus,
        JsonObject? Selection);

    internal FileDirectSnapshotContextAdapter(
        string statePath,
        Func<DateTimeOffset>? utcNow = null)
    {
        if (!System.IO.Path.IsPathFullyQualified(statePath))
        {
            throw new ArgumentException(
                "snapshot state path must be absolute",
                nameof(statePath));
        }
        _statePath = System.IO.Path.GetFullPath(statePath);
        _utcNow = utcNow ?? (() => DateTimeOffset.UtcNow);
        LoadExistingState();
    }

    internal string StatePath => _statePath;

    public async Task<DirectSnapshotForwardResult> ForwardJournalAsync(
        string requestId,
        string sessionId,
        DirectContextEvent contextEvent,
        CancellationToken cancellationToken)
    {
        ValidateRequestIdentity(requestId, sessionId);
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            if (_recentEventIds.Contains(contextEvent.EventId))
            {
                return new DirectSnapshotForwardResult(
                    "duplicate",
                    _revision);
            }
            AdapterState before = CaptureState();
            try
            {
                FoldJournal(contextEvent);
                RememberEvent(contextEvent.EventId);
                await PersistAsync(cancellationToken)
                    .ConfigureAwait(false);
            }
            catch
            {
                RestoreState(before);
                throw;
            }
            return new DirectSnapshotForwardResult("accepted", _revision);
        }
        finally
        {
            _gate.Release();
        }
    }

    public async Task<DirectSnapshotForwardResult>
        ForwardActiveReadingAsync(
            string requestId,
            string sessionId,
            DirectActiveReading activeReading,
            CancellationToken cancellationToken)
    {
        ValidateRequestIdentity(requestId, sessionId);
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            AdapterState before = CaptureState();
            try
            {
                long receivedAt = _utcNow().ToUnixTimeMilliseconds();
                if (
                    activeReading.HighlightSource
                        is JsonElement receivedSource
                    && (
                        !receivedSource.TryGetProperty(
                            "expiresAt",
                            out JsonElement expiry)
                        || !expiry.TryGetInt64(out long expiresAt)
                        || expiresAt <= receivedAt
                        || expiresAt > receivedAt + 300_000
                    )
                )
                {
                    throw ActiveReadingInvalid();
                }
                JsonObject next = new()
                {
                    ["kind"] = activeReading.Kind,
                    ["file"] = activeReading.File,
                    ["title"] = activeReading.Title,
                    ["page"] = JsonNode.Parse(
                        activeReading.Page.GetRawText()),
                    ["fresh"] = true,
                    ["ageSec"] = 0,
                    ["observedAtEpochMs"] =
                        activeReading.ObservedAtEpochMilliseconds,
                    ["receivedAtEpochMs"] =
                        receivedAt,
                };
                if (
                    activeReading.ViewFile is not null
                    && activeReading.ViewPage is JsonElement viewPage
                )
                {
                    next["viewFile"] = activeReading.ViewFile;
                    next["viewPage"] = JsonNode.Parse(
                        viewPage.GetRawText());
                }
                if (activeReading.SourceInstanceId is not null)
                {
                    next["sourceInstanceId"] =
                        activeReading.SourceInstanceId;
                }
                if (activeReading.SelectionRegions is JsonElement regions)
                {
                    next["selectionRegions"] = JsonNode.Parse(
                        regions.GetRawText());
                }
                if (activeReading.HighlightSource is JsonElement source)
                {
                    next["highlightSource"] = JsonNode.Parse(
                        source.GetRawText());
                }
                // 复习投影跟着本次上报走:缺席就是缺席(= 已退出复习模式),
                // 不进 PreserveActiveReadingContinuity 的保留名单 ——
                // 一个"复活"的旧 review 会让 AI 以为用户还停在复习里。
                if (activeReading.Review is JsonElement reviewState)
                {
                    next["review"] = JsonNode.Parse(
                        reviewState.GetRawText());
                }
                if (_activeReading is JsonObject priorActive)
                {
                    PreserveActiveReadingContinuity(
                        priorActive,
                        next,
                        preserveHighlightSource: false);
                }
                bool changedPage = _activeReading is not null
                    && !SameActiveReadingIdentity(_activeReading, next);
                _activeReading = next;
                if (changedPage)
                {
                    RecordAction(
                        "page-turn",
                        next,
                        activeReading.ObservedAtEpochMilliseconds);
                }
                if (activeReading.SelectionState == "active")
                {
                    _selection = ActiveSelection(
                        activeReading.Selection!,
                        next,
                        activeReading.SelectionContext,
                        activeReading.SelectionContextSource);
                    RecordAction(
                        "selection",
                        next,
                        activeReading.ObservedAtEpochMilliseconds);
                }
                else if (activeReading.SelectionState == "cleared")
                {
                    _selection = ClearedSelection(
                        "active-reading-cleared");
                }
                else if (
                    changedPage
                    || (
                        _selection["ref"] is JsonObject selectionRef
                        && !SameLogicalPage(next, selectionRef, next)
                    )
                )
                {
                    _selection = UnknownSelection(
                        "active-page-changed");
                }
                if (changedPage)
                {
                    _focus = UnknownFocus("active-page-changed");
                }
                _revision = checked(_revision + 1);
                _latestEvent = new JsonObject
                {
                    ["source"] = "active-reading",
                    ["seq"] = null,
                    ["id"] = requestId,
                    ["type"] = "active.reading",
                    ["ts"] =
                        activeReading.ObservedAtEpochMilliseconds,
                };
                await PersistAsync(cancellationToken)
                    .ConfigureAwait(false);
            }
            catch
            {
                RestoreState(before);
                throw;
            }
            return new DirectSnapshotForwardResult("accepted", _revision);
        }
        finally
        {
            _gate.Release();
        }
    }

    public async Task<DirectSnapshotForwardResult> ForwardViewportAsync(
        string requestId,
        string sessionId,
        DirectViewportContext viewport,
        CancellationToken cancellationToken)
    {
        ValidateRequestIdentity(requestId, sessionId);
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            AdapterState before = CaptureState();
            try
            {
                if (
                    _activeReading is not JsonObject active
                    || !string.Equals(
                        StringValue(active["sourceInstanceId"]),
                        viewport.SourceInstanceId,
                        StringComparison.Ordinal)
                    || !string.Equals(
                        StringValue(active["file"]),
                        viewport.Url,
                        StringComparison.Ordinal)
                    || _stablePage is not JsonObject stable
                    || !string.Equals(
                        StringValue(stable["file"]),
                        viewport.Url,
                        StringComparison.Ordinal)
                )
                {
                    throw new DirectProtocolException(
                        "BW_READER_VIEWPORT_IDENTITY_MISMATCH",
                        "Reader 当前视口与活动页面身份不一致",
                        retryable: true);
                }
                JsonObject viewportJson = new()
                {
                    ["contract"] = "reader-viewport/1",
                    ["sourceInstanceId"] = viewport.SourceInstanceId,
                    ["documentKey"] = viewport.DocumentKey,
                    ["url"] = viewport.Url,
                    ["title"] = viewport.Title,
                    ["beforeText"] = viewport.BeforeText,
                    ["visibleText"] = viewport.VisibleText,
                    ["afterText"] = viewport.AfterText,
                    ["selectionState"] = viewport.SelectionState,
                    ["selection"] = viewport.Selection,
                    ["observedAtEpochMs"] =
                        viewport.ObservedAtEpochMilliseconds,
                    ["receivedAtEpochMs"] =
                        _utcNow().ToUnixTimeMilliseconds(),
                };
                if (viewport.ControlCorrelation is not null)
                {
                    viewportJson["controlCorrelation"] =
                        viewport.ControlCorrelation;
                }
                stable["sourceInstanceId"] = viewport.SourceInstanceId;
                stable["documentKey"] = viewport.DocumentKey;
                stable["readingWindow"] = viewportJson;
                // 网页正文 = 前文 + ⟦VIEWPORT⟧视口⟦/VIEWPORT⟧ + 后文。
                // 用户拍板(2026-08-16):跟阅读器「当前页」同一个模型——只给视口
                // 等于把页面剪成一条缝。前后文本来就在 readingWindow 里一路传到
                // 这儿(扩展端各截 2400 字),只是从没被组装进 text。标记沿用 ⟦⟧
                // 家族;真虚拟页编号不做——网页在配对协议里 page 恒为 0,
                // 编号会震到 (file,page) 配对。
                // ⚠ 网页正文必须先转义再拼进注解格式。
                //
                //   这个格式（⟦…⟧ 标记族 + 反斜杠转义）有一条不变式：**正文里
                //   出现的 ⟦ ⟧ \ 一律是转义过的**，所以解析器可以放心把未转义的
                //   ⟦…⟧ 当成协议标记。PDF 那条路由 escapeLocalLayoutText 遵守它
                //   （rc-computer-voice.js::escapeLocalLayoutText）。
                //
                //   网页这条路一直没有转义，而网页内容是**不可信输入**：
                //   · 页面里出现一个裸 ⟧（数学记号页面完全可能）→ 解析器抛
                //     ReaderTextInvalid → 整份 Markdown 投影 503；
                //   · visibleText 是按 12000 字**硬切**的（content.js MAX_TEXT），
                //     切在反斜杠上 → 末尾孤立转义 → 同样抛（自检 danglingEscapeRejected
                //     明确要求这条必须抛，那是有意的：孤立转义说明上游格式坏了）；
                //   · 更糟的是页面可以自带 ⟦CARD_START …⟧ 伪造标记。
                //
                //   所以修法不是放宽解析器，而是让这条生产端也遵守格式 ——
                //   在**唯一入口**这里转义。桥自己加的 ⟦VIEWPORT⟧ 在转义之后拼，
                //   所以它仍然是真标记。
                string beforePart = EscapeAnnotatedReaderText(viewport.BeforeText ?? "");
                string afterPart = EscapeAnnotatedReaderText(viewport.AfterText ?? "");
                string visiblePart = EscapeAnnotatedReaderText(viewport.VisibleText ?? "");
                bool hasAround =
                    !string.IsNullOrWhiteSpace(beforePart)
                    || !string.IsNullOrWhiteSpace(afterPart);
                stable["text"] = hasAround
                    ? (string.IsNullOrWhiteSpace(beforePart)
                          ? ""
                          : beforePart + "\n")
                      + "⟦VIEWPORT⟧\n"
                      + visiblePart
                      + "\n⟦/VIEWPORT⟧"
                      + (string.IsNullOrWhiteSpace(afterPart)
                          ? ""
                          : "\n" + afterPart)
                    : visiblePart;
                stable["textAvailable"] =
                    !string.IsNullOrWhiteSpace(viewport.VisibleText);
                _revision = checked(_revision + 1);
                _latestEvent = new JsonObject
                {
                    ["source"] = "viewport",
                    ["seq"] = null,
                    ["id"] = requestId,
                    ["type"] = "viewport.context",
                    ["ts"] = viewport.ObservedAtEpochMilliseconds,
                };
                await PersistAsync(cancellationToken)
                    .ConfigureAwait(false);
            }
            catch
            {
                RestoreState(before);
                throw;
            }
            return new DirectSnapshotForwardResult("accepted", _revision);
        }
        finally
        {
            _gate.Release();
        }
    }

    public async Task<DirectSnapshotForwardResult> ClearAsync(
        string requestId,
        string sessionId,
        CancellationToken cancellationToken)
    {
        ValidateRequestIdentity(requestId, sessionId);
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            AdapterState before = CaptureState();
            try
            {
                _revision = checked(_revision + 1);
                _stablePage = null;
                _activeReading = null;
                _selection = UnknownSelection("snapshot-cleared");
                _focus = UnknownFocus("snapshot-cleared");
                // 没有"当前书"了,留着上一本书的动作记录只会显得像是这本新
                // 上下文里发生的事。
                _recentActions.Clear();
                _recentActionsFile = null;
                _latestEvent = new JsonObject
                {
                    ["source"] = "context-control",
                    ["seq"] = null,
                    ["id"] = requestId,
                    ["type"] = "context.clear",
                    ["ts"] = _utcNow().ToUnixTimeMilliseconds(),
                };
                await PersistAsync(cancellationToken)
                    .ConfigureAwait(false);
            }
            catch
            {
                RestoreState(before);
                throw;
            }
            return new DirectSnapshotForwardResult(
                "accepted",
                _revision);
        }
        finally
        {
            _gate.Release();
        }
    }

    internal static DirectActiveReading ValidateActiveReading(
        JsonElement value)
    {
        if (value.ValueKind != JsonValueKind.Object)
        {
            throw ActiveReadingInvalid();
        }
        try
        {
            DirectJsonValidation.RequireNoDuplicateKeys(value);
        }
        catch (DirectProtocolException exception)
        {
            throw ActiveReadingInvalid(exception);
        }
        HashSet<string> keys = value.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        string[] requiredKeys =
        {
            "kind",
            "file",
            "title",
            "page",
            "selectionState",
            "selection",
            "observedAtEpochMs",
        };
        HashSet<string> allowedKeys = requiredKeys
            .Append("viewFile")
            .Append("viewPage")
            .Append("sourceInstanceId")
            .Append("selectionRegions")
            .Append("highlightSource")
            .Append("selectionContext")
            .Append("selectionContextSource")
            .Append("review")
            .ToHashSet(StringComparer.Ordinal);
        if (
            requiredKeys.Any(key => !keys.Contains(key))
            || keys.Any(key => !allowedKeys.Contains(key))
            || keys.Contains("viewFile") != keys.Contains("viewPage")
        )
        {
            throw ActiveReadingInvalid();
        }
        if (
            value.GetProperty("kind").ValueKind != JsonValueKind.String
            || value.GetProperty("kind").GetString() is not string kind
            || kind is not ("pdf" or "epub" or "html" or "web")
            || value.GetProperty("file").ValueKind != JsonValueKind.String
            || value.GetProperty("file").GetString() is not string file
            || file.Length is < 1 or > 4096
            || file.Any(char.IsControl)
            || file.StartsWith("vbook:", StringComparison.Ordinal)
        )
        {
            throw ActiveReadingInvalid();
        }
        JsonElement titleValue = value.GetProperty("title");
        string? title = titleValue.ValueKind switch
        {
            JsonValueKind.Null => null,
            JsonValueKind.String => titleValue.GetString(),
            _ => throw ActiveReadingInvalid(),
        };
        if (
            title is { Length: > 1024 }
            || (title is not null && title.Any(char.IsControl))
        )
        {
            throw ActiveReadingInvalid();
        }
        JsonElement page = value.GetProperty("page");
        bool pageValid = page.ValueKind switch
        {
            JsonValueKind.Null => true,
            JsonValueKind.Number =>
                page.TryGetInt64(out long number)
                && number is >= 0 and <= 9_007_199_254_740_991,
            JsonValueKind.String =>
                page.GetString() is string text
                && text.Length is >= 1 and <= 256
                && !text.Any(char.IsControl),
            _ => false,
        };
        if (
            !pageValid
            || value.GetProperty("selectionState").ValueKind
                != JsonValueKind.String
            || value.GetProperty("selectionState").GetString()
                is not string selectionState
            || selectionState is not (
                "active" or "cleared" or "unknown")
            || !value.GetProperty("observedAtEpochMs")
                .TryGetInt64(out long observedAt)
            || observedAt is < 1 or > 9_007_199_254_740_991
        )
        {
            throw ActiveReadingInvalid();
        }
        string? viewFile = null;
        JsonElement? viewPage = null;
        if (keys.Contains("viewFile"))
        {
            JsonElement viewFileValue = value.GetProperty("viewFile");
            JsonElement viewPageValue = value.GetProperty("viewPage");
            if (
                kind == "web"
                || viewFileValue.ValueKind != JsonValueKind.String
                || viewFileValue.GetString() is not string candidateViewFile
                || !candidateViewFile.StartsWith(
                    "vbook:",
                    StringComparison.Ordinal)
                || candidateViewFile.Length is < 7 or > 4096
                || candidateViewFile.Any(char.IsControl)
                || !ValidPageIdentifier(
                    JsonNode.Parse(viewPageValue.GetRawText()),
                    allowNull: false)
            )
            {
                throw ActiveReadingInvalid();
            }
            viewFile = candidateViewFile;
            viewPage = viewPageValue.Clone();
        }
        string? sourceInstanceId = null;
        if (keys.Contains("sourceInstanceId"))
        {
            JsonElement sourceValue = value.GetProperty(
                "sourceInstanceId");
            if (
                sourceValue.ValueKind != JsonValueKind.String
                || sourceValue.GetString() is not string candidateSource
                || candidateSource.Length is < 1 or > 160
                || !DirectBridgeContract.IsSafeId(candidateSource)
            )
            {
                throw ActiveReadingInvalid();
            }
            sourceInstanceId = candidateSource;
        }
        JsonElement? selectionRegions = null;
        if (keys.Contains("selectionRegions"))
        {
            selectionRegions = ValidateSelectionRegions(
                value.GetProperty("selectionRegions"));
        }
        JsonElement? highlightSource = null;
        if (keys.Contains("highlightSource"))
        {
            highlightSource = ValidateHighlightSource(
                value.GetProperty("highlightSource"),
                kind,
                file,
                page,
                observedAt);
        }
        JsonElement selectionValue = value.GetProperty("selection");
        string? selection = selectionValue.ValueKind switch
        {
            JsonValueKind.Null => null,
            JsonValueKind.String => selectionValue.GetString(),
            _ => throw ActiveReadingInvalid(),
        };
        if (
            selection is { Length: > 400 }
            || (
                selectionState == "active"
                && string.IsNullOrWhiteSpace(selection)
            )
            || (
                selectionState != "active"
                && selection is not null
            )
        )
        {
            throw ActiveReadingInvalid();
        }
        // 选中附近的原文。约束跟着 selection 走:没有活动选中就不许有上下文,
        // 有来历标签就必须有上下文本体 —— 一个孤零零的 "pdf-sentence" 标签
        // 什么也不说明。null 等同缺席(前端清空选中时整组一起消失)。
        string? selectionContext = OptionalActiveText(
            value,
            "selectionContext",
            1200);
        string? selectionContextSource = OptionalActiveText(
            value,
            "selectionContextSource",
            40);
        if (
            (
                selectionState != "active"
                && (selectionContext is not null
                    || selectionContextSource is not null)
            )
            || (
                selectionContextSource is not null
                && selectionContext is null
            )
        )
        {
            throw ActiveReadingInvalid();
        }
        JsonElement? review = null;
        if (keys.Contains("review"))
        {
            review = ValidateReviewState(value.GetProperty("review"));
        }
        return new DirectActiveReading(
            kind,
            file,
            title,
            page.Clone(),
            selectionState,
            selection,
            observedAt,
            viewFile,
            viewPage,
            sourceInstanceId,
            selectionRegions,
            highlightSource,
            selectionContext,
            selectionContextSource,
            review);
    }

    // 复习模式投影:在场必须整形合规,否则整条 active-reading 拒绝 ——
    // 与 selectionRegions/highlightSource 同一条纪律。字段缺席表示
    // 未进入复习模式,所以这里永远不产出"空的 review"。
    private static JsonElement ValidateReviewState(JsonElement value)
    {
        if (value.ValueKind != JsonValueKind.Object)
        {
            throw ActiveReadingInvalid();
        }
        try
        {
            DirectJsonValidation.RequireNoDuplicateKeys(value);
        }
        catch (DirectProtocolException exception)
        {
            throw ActiveReadingInvalid(exception);
        }
        HashSet<string> keys = value.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        string[] requiredKeys =
        {
            "dueTotal",
            "index",
            "queueIds",
            "showingAnswer",
        };
        HashSet<string> allowedKeys = requiredKeys
            .Append("current")
            .ToHashSet(StringComparer.Ordinal);
        if (
            requiredKeys.Any(key => !keys.Contains(key))
            || keys.Any(key => !allowedKeys.Contains(key))
            || !value.GetProperty("dueTotal").TryGetInt64(
                out long dueTotal)
            || dueTotal is < 0 or > 100_000
            || !value.GetProperty("index").TryGetInt64(out long index)
            || index is < 0 or > 100_000
            || value.GetProperty("showingAnswer").ValueKind
                is not (JsonValueKind.True or JsonValueKind.False)
            || value.GetProperty("queueIds").ValueKind
                != JsonValueKind.Array
        )
        {
            throw ActiveReadingInvalid();
        }
        JsonElement queueIds = value.GetProperty("queueIds");
        int queueCount = 0;
        foreach (JsonElement entry in queueIds.EnumerateArray())
        {
            queueCount += 1;
            if (
                queueCount > 200
                || entry.ValueKind != JsonValueKind.String
                || entry.GetString() is not string entryId
                || entryId.Length is < 1 or > 120
                || !DirectBridgeContract.IsSafeId(entryId)
            )
            {
                throw ActiveReadingInvalid();
            }
        }
        if (keys.Contains("current"))
        {
            JsonElement current = value.GetProperty("current");
            if (current.ValueKind != JsonValueKind.Object)
            {
                throw ActiveReadingInvalid();
            }
            try
            {
                DirectJsonValidation.RequireNoDuplicateKeys(current);
            }
            catch (DirectProtocolException exception)
            {
                throw ActiveReadingInvalid(exception);
            }
            HashSet<string> currentKeys = current.EnumerateObject()
                .Select(property => property.Name)
                .ToHashSet(StringComparer.Ordinal);
            string[] currentRequired = { "id", "front", "back" };
            if (
                currentKeys.Count != currentRequired.Length
                || currentRequired.Any(key => !currentKeys.Contains(key))
                || current.GetProperty("id").ValueKind
                    != JsonValueKind.String
                || current.GetProperty("id").GetString()
                    is not string currentId
                || currentId.Length is < 1 or > 120
                || !DirectBridgeContract.IsSafeId(currentId)
            )
            {
                throw ActiveReadingInvalid();
            }
            foreach (string face in new[] { "front", "back" })
            {
                if (
                    current.GetProperty(face).ValueKind
                        != JsonValueKind.String
                    || current.GetProperty(face).GetString()
                        is not string text
                    || text.Length > 2000
                    || text.Any(character =>
                        char.IsControl(character)
                        && character is not ('\n' or '\t'))
                )
                {
                    throw ActiveReadingInvalid();
                }
            }
        }
        return value.Clone();
    }

    // active 里的可选文本字段:缺席/null → null;字符串超限或含控制字符、
    // 或者是别的类型 → 整条拒绝。宽进(可缺)严出(在场必须合规),
    // 与 selection 本体同一条纪律 —— 一条严一条松,校验就形同虚设。
    private static string? OptionalActiveText(
        JsonElement value,
        string name,
        int maximumLength)
    {
        if (!value.TryGetProperty(name, out JsonElement property))
        {
            return null;
        }
        if (property.ValueKind == JsonValueKind.Null)
        {
            return null;
        }
        if (
            property.ValueKind != JsonValueKind.String
            || property.GetString() is not string text
            || text.Length > maximumLength
            || string.IsNullOrWhiteSpace(text)
            || text.Any(character =>
                char.IsControl(character)
                && character is not ('\n' or '\t'))
        )
        {
            throw ActiveReadingInvalid();
        }
        return text;
    }

    private static JsonElement ValidateHighlightSource(
        JsonElement value,
        string kind,
        string file,
        JsonElement page,
        long observedAt)
    {
        if (value.ValueKind != JsonValueKind.Object)
        {
            throw ActiveReadingInvalid();
        }
        try
        {
            DirectJsonValidation.RequireNoDuplicateKeys(value);
        }
        catch (DirectProtocolException exception)
        {
            throw ActiveReadingInvalid(exception);
        }
        HashSet<string> fields = value.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        if (!fields.SetEquals(new[]
            {
                "contract",
                "snapshotId",
                "documentId",
                "target",
                "sourceDigest",
                "revision",
                "expiresAt",
                "markers",
            }))
        {
            throw ActiveReadingInvalid();
        }
        if (
            value.GetProperty("contract").ValueKind
                != JsonValueKind.String
            || value.GetProperty("snapshotId").ValueKind
                != JsonValueKind.String
            || value.GetProperty("documentId").ValueKind
                != JsonValueKind.String
            || value.GetProperty("sourceDigest").ValueKind
                != JsonValueKind.String
            || value.GetProperty("revision").ValueKind
                != JsonValueKind.String
        )
        {
            throw ActiveReadingInvalid();
        }
        string? contract = value.GetProperty("contract").GetString();
        string? snapshotId = value.GetProperty("snapshotId").GetString();
        string? documentId = value.GetProperty("documentId").GetString();
        string? digest = value.GetProperty("sourceDigest").GetString();
        string? revision = value.GetProperty("revision").GetString();
        if (
            contract != "reader-highlight-source/1"
            || snapshotId is null
            || !ReaderRealtimeOutputProtocol
                .IsHighlightSourceSnapshotId(snapshotId)
            || documentId != file
            || documentId.Length is < 1 or > 4096
            || documentId.Any(char.IsControl)
            || digest is null
            || !ReaderRealtimeOutputProtocol
                .IsHighlightSourceDigest(digest)
            || string.IsNullOrEmpty(revision)
            || revision.Length > 160
            || revision.Any(character =>
                char.IsControl(character)
                || char.IsWhiteSpace(character))
            || !value.GetProperty("expiresAt")
                .TryGetInt64(out long expiresAt)
            || expiresAt <= observedAt
            || expiresAt > observedAt + 300_000
            || !HighlightTargetMatches(
                value.GetProperty("target"),
                kind,
                page)
        )
        {
            throw ActiveReadingInvalid();
        }
        JsonElement markers = value.GetProperty("markers");
        if (markers.ValueKind != JsonValueKind.Array)
        {
            throw ActiveReadingInvalid();
        }
        int count = markers.GetArrayLength();
        if (count is < 2 or > 2048)
        {
            throw ActiveReadingInvalid();
        }
        HashSet<string> ids = new(StringComparer.Ordinal);
        int totalText = 0;
        for (int index = 0; index < count; index += 1)
        {
            JsonElement marker = markers[index];
            if (marker.ValueKind != JsonValueKind.Object)
            {
                throw ActiveReadingInvalid();
            }
            try
            {
                DirectJsonValidation.RequireNoDuplicateKeys(marker);
            }
            catch (DirectProtocolException exception)
            {
                throw ActiveReadingInvalid(exception);
            }
            HashSet<string> markerFields = marker.EnumerateObject()
                .Select(property => property.Name)
                .ToHashSet(StringComparer.Ordinal);
            string? id = marker.TryGetProperty(
                "marker",
                out JsonElement markerValue)
                && markerValue.ValueKind == JsonValueKind.String
                    ? markerValue.GetString()
                    : null;
            string? text = marker.TryGetProperty(
                "text",
                out JsonElement textValue)
                && textValue.ValueKind == JsonValueKind.String
                    ? textValue.GetString()
                    : null;
            bool final = index == count - 1;
            if (
                !markerFields.SetEquals(new[] { "marker", "text" })
                || id is null
                || !ReaderRealtimeOutputProtocol
                    .IsHighlightSourceMarker(id)
                || !ids.Add(id)
                || text is null
                || text.Length > 512
                || text.Any(character =>
                    char.IsControl(character)
                    && character is not ('\r' or '\n' or '\t'))
                || (final ? text.Length != 0 : text.Length == 0)
            )
            {
                throw ActiveReadingInvalid();
            }
            totalText = checked(totalText + text.Length);
        }
        if (totalText is < 1 or > 16_384)
        {
            throw ActiveReadingInvalid();
        }
        return value.Clone();
    }

    private static bool HighlightTargetMatches(
        JsonElement target,
        string kind,
        JsonElement page)
    {
        if (
            target.ValueKind != JsonValueKind.Object
            || page.ValueKind != JsonValueKind.Number
            || !page.TryGetInt64(out long current)
        )
        {
            return false;
        }
        try
        {
            DirectJsonValidation.RequireNoDuplicateKeys(target);
        }
        catch (DirectProtocolException)
        {
            return false;
        }
        HashSet<string> fields = target.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        if (
            !target.TryGetProperty("kind", out JsonElement targetKind)
            || targetKind.ValueKind != JsonValueKind.String
        )
        {
            return false;
        }
        if (kind == "pdf")
        {
            return fields.SetEquals(new[] { "kind", "page" })
                && targetKind.GetString() == "pdf"
                && target.TryGetProperty("page", out JsonElement location)
                && location.TryGetInt64(out long value)
                && value >= 1
                && value == current;
        }
        if (kind == "epub")
        {
            return fields.SetEquals(new[] { "kind", "section" })
                && targetKind.GetString() == "epub"
                && target.TryGetProperty(
                    "section",
                    out JsonElement location)
                && location.TryGetInt64(out long value)
                && value >= 0
                && value == current;
        }
        return false;
    }

    private static JsonElement ValidateSelectionRegions(
        JsonElement value)
    {
        if (value.ValueKind != JsonValueKind.Object)
        {
            throw ActiveReadingInvalid();
        }
        try
        {
            DirectJsonValidation.RequireNoDuplicateKeys(value);
        }
        catch (DirectProtocolException exception)
        {
            throw ActiveReadingInvalid(exception);
        }
        HashSet<string> fields = value.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        if (
            !fields.SetEquals(new[]
            {
                "contract", "total", "truncated", "items",
            })
            || value.GetProperty("contract").ValueKind
                != JsonValueKind.String
            || value.GetProperty("contract").GetString()
                != "reader-selection-regions/1"
            || !value.GetProperty("total").TryGetInt32(out int total)
            || total is < 0 or > 1_000_000
            || value.GetProperty("truncated").ValueKind
                is not (JsonValueKind.True or JsonValueKind.False)
            || value.GetProperty("items").ValueKind
                != JsonValueKind.Array
        )
        {
            throw ActiveReadingInvalid();
        }
        bool truncated = value.GetProperty("truncated").GetBoolean();
        JsonElement items = value.GetProperty("items");
        int count = items.GetArrayLength();
        if (
            count > 128
            || truncated != (total > count)
            || (!truncated && total != count)
        )
        {
            throw ActiveReadingInvalid();
        }
        HashSet<string> ids = new(StringComparer.Ordinal);
        int priorOrdinal = 0;
        foreach (JsonElement item in items.EnumerateArray())
        {
            if (item.ValueKind != JsonValueKind.Object)
            {
                throw ActiveReadingInvalid();
            }
            try
            {
                DirectJsonValidation.RequireNoDuplicateKeys(item);
            }
            catch (DirectProtocolException exception)
            {
                throw ActiveReadingInvalid(exception);
            }
            HashSet<string> itemFields = item.EnumerateObject()
                .Select(property => property.Name)
                .ToHashSet(StringComparer.Ordinal);
            if (
                !itemFields.SetEquals(new[]
                {
                    "selectionId", "label", "ordinal",
                    "createdAtEpochMs",
                })
                || item.GetProperty("selectionId").ValueKind
                    != JsonValueKind.String
                || item.GetProperty("selectionId").GetString()
                    is not string selectionId
                || selectionId.Length is < 1 or > 160
                || !DirectBridgeContract.IsSafeId(selectionId)
                || !ids.Add(selectionId)
                || item.GetProperty("label").ValueKind
                    != JsonValueKind.String
                || item.GetProperty("label").GetString()
                    is not string label
                || label.Length is < 1 or > 80
                || label.Any(char.IsControl)
                || !item.GetProperty("ordinal").TryGetInt32(
                    out int ordinal)
                || ordinal <= priorOrdinal
                || ordinal < 1
                || !label.StartsWith(
                    "#" + ordinal.ToString(
                        CultureInfo.InvariantCulture) + " ",
                    StringComparison.Ordinal)
                || !item.GetProperty("createdAtEpochMs").TryGetInt64(
                    out long createdAt)
                || createdAt is < 0 or > 9_007_199_254_740_991
            )
            {
                throw ActiveReadingInvalid();
            }
            priorOrdinal = ordinal;
        }
        return value.Clone();
    }

    internal static bool SelectionRegionExists(
        JsonNode? source,
        string? selectionId)
    {
        if (
            source is null
            || string.IsNullOrEmpty(selectionId)
            || selectionId.Length > 160
            || !DirectBridgeContract.IsSafeId(selectionId)
        )
        {
            return false;
        }
        try
        {
            using JsonDocument document = JsonDocument.Parse(
                source.ToJsonString(DirectBridgeContract.JsonOptions));
            JsonElement validated = ValidateSelectionRegions(
                document.RootElement);
            return validated.GetProperty("items")
                .EnumerateArray()
                .Any(item => string.Equals(
                    item.GetProperty("selectionId").GetString(),
                    selectionId,
                    StringComparison.Ordinal));
        }
        catch (Exception exception) when (
            exception is DirectProtocolException
            or JsonException
            or InvalidOperationException
        )
        {
            return false;
        }
    }

    internal static DirectViewportContext ValidateViewport(
        JsonElement value)
    {
        if (value.ValueKind != JsonValueKind.Object)
        {
            throw ViewportInvalid();
        }
        try
        {
            DirectJsonValidation.RequireNoDuplicateKeys(value);
        }
        catch (DirectProtocolException exception)
        {
            throw ViewportInvalid(exception);
        }
        string[] requiredFields =
        {
            "contract",
            "sourceInstanceId",
            "documentKey",
            "url",
            "title",
            "beforeText",
            "visibleText",
            "afterText",
            "selectionState",
            "selection",
            "observedAtEpochMs",
        };
        HashSet<string> actual = value.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        HashSet<string> allowed = requiredFields
            .Append("controlCorrelation")
            .ToHashSet(StringComparer.Ordinal);
        if (
            requiredFields.Any(field => !actual.Contains(field))
            || actual.Any(field => !allowed.Contains(field))
        )
        {
            throw ViewportInvalid();
        }
        string sourceInstanceId = RequiredSafeViewportId(
            value,
            "sourceInstanceId",
            160);
        string documentKey = RequiredViewportUrl(
            value,
            "documentKey");
        if (
            value.GetProperty("contract").ValueKind
                != JsonValueKind.String
            || value.GetProperty("contract").GetString()
                != "reader-viewport/1"
            || value.GetProperty("url").ValueKind
                != JsonValueKind.String
            || value.GetProperty("url").GetString() is not string url
            || url.Length is < 1 or > 4096
            || url.Any(char.IsControl)
            || !Uri.TryCreate(url, UriKind.Absolute, out Uri? uri)
            || uri.Scheme is not ("http" or "https")
            || value.GetProperty("selectionState").ValueKind
                != JsonValueKind.String
            || value.GetProperty("selectionState").GetString()
                is not string selectionState
            || selectionState is not (
                "active" or "cleared" or "unknown")
            || !value.GetProperty("observedAtEpochMs")
                .TryGetInt64(out long observedAt)
            || observedAt is < 1 or > 9_007_199_254_740_991
        )
        {
            throw ViewportInvalid();
        }
        string beforeText = RequiredViewportText(
            value,
            "beforeText",
            12_000);
        string visibleText = RequiredViewportText(
            value,
            "visibleText",
            24_000);
        string afterText = RequiredViewportText(
            value,
            "afterText",
            12_000);
        JsonElement titleValue = value.GetProperty("title");
        string? title = titleValue.ValueKind switch
        {
            JsonValueKind.Null => null,
            JsonValueKind.String => titleValue.GetString(),
            _ => throw ViewportInvalid(),
        };
        if (
            title is { Length: > 1024 }
            || (title is not null && title.Any(char.IsControl))
        )
        {
            throw ViewportInvalid();
        }
        JsonElement selectionValue = value.GetProperty("selection");
        string? selection = selectionValue.ValueKind switch
        {
            JsonValueKind.Null => null,
            JsonValueKind.String => selectionValue.GetString(),
            _ => throw ViewportInvalid(),
        };
        if (
            selection is { Length: > 400 }
            || (
                selectionState == "active"
                && string.IsNullOrWhiteSpace(selection)
            )
            || (selectionState != "active" && selection is not null)
        )
        {
            throw ViewportInvalid();
        }
        string? controlCorrelation = null;
        if (actual.Contains("controlCorrelation"))
        {
            controlCorrelation = RequiredSafeViewportId(
                value,
                "controlCorrelation",
                160);
        }
        return new DirectViewportContext(
            sourceInstanceId,
            documentKey,
            url,
            title,
            beforeText,
            visibleText,
            afterText,
            selectionState,
            selection,
            observedAt,
            controlCorrelation);
    }

    private static string RequiredSafeViewportId(
        JsonElement value,
        string field,
        int maximumLength)
    {
        if (
            !value.TryGetProperty(field, out JsonElement property)
            || property.ValueKind != JsonValueKind.String
            || property.GetString() is not string result
            || result.Length is < 1
            || result.Length > maximumLength
            || !DirectBridgeContract.IsSafeId(result)
        )
        {
            throw ViewportInvalid();
        }
        return result;
    }

    private static string RequiredViewportUrl(
        JsonElement value,
        string field)
    {
        if (
            !value.TryGetProperty(field, out JsonElement property)
            || property.ValueKind != JsonValueKind.String
            || property.GetString() is not string result
            || result.Length is < 1 or > 4096
            || result.Any(char.IsControl)
            || !Uri.TryCreate(result, UriKind.Absolute, out Uri? uri)
            || uri.Scheme is not ("http" or "https")
        )
        {
            throw ViewportInvalid();
        }
        return result;
    }

    /// 把不可信正文（网页抓来的）转义成注解文本格式。
    ///
    /// ⚠ 必须是 DirectSnapshotTerminal 里 DecodeEscapedReaderText 的**严格逆运算**：
    ///   那边只认 \\ 、 \⟦ 、 \⟧ 三种转义（其它 \x 原样保留两个字符），
    ///   所以这里也只转这三个字符。多转或少转都会让往返对不上 ——
    ///   少转 = 正文里的 ⟧ 被当成协议标记（轻则错渲，重则整份投影被拒）；
    ///   多转 = 用户看到莫名其妙的反斜杠。
    ///
    /// 反斜杠必须**第一个**替换，否则后面插进去的反斜杠会被二次转义。
    private static string EscapeAnnotatedReaderText(string value) =>
        (value ?? string.Empty)
            .Replace("\\", "\\\\", StringComparison.Ordinal)
            .Replace("⟦", "\\⟦", StringComparison.Ordinal)
            .Replace("⟧", "\\⟧", StringComparison.Ordinal);

    private static string RequiredViewportText(
        JsonElement value,
        string field,
        int maximumLength)
    {
        if (
            !value.TryGetProperty(field, out JsonElement property)
            || property.ValueKind != JsonValueKind.String
            || property.GetString() is not string result
            || result.Length > maximumLength
            || result.Any(character =>
                char.IsControl(character)
                && character is not ('\r' or '\n' or '\t'))
        )
        {
            throw ViewportInvalid();
        }
        return result.Replace("\r\n", "\n", StringComparison.Ordinal)
            .Replace('\r', '\n')
            .Trim();
    }

    // 记一条"用户刚做了什么"。只在明确知道是真实用户动作的地方调用——
    // 翻页(真的换了页,不是重复上报同一页)、画完一笔(从未稳定到稳定的那一刻)。
    // 不追求覆盖面:宁可少几种动作类型,也不要把系统内部状态转换错认成用户动作。
    private void RecordAction(string kind, JsonObject pageIdentity, long atMs)
    {
        string? file = StringValue(pageIdentity["file"]);
        if (string.IsNullOrEmpty(file))
        {
            return;
        }
        // 换书清空:上一本书翻到第几页,跟这本书完全无关,留着只会误导。
        if (!string.Equals(_recentActionsFile, file, StringComparison.Ordinal))
        {
            _recentActions.Clear();
            _recentActionsFile = file;
        }
        _recentActions.Add(new JsonObject
        {
            ["kind"] = kind,
            ["page"] = pageIdentity["page"]?.DeepClone(),
            ["atMs"] = atMs,
        });
        PruneRecentActions();
    }

    private void PruneRecentActions()
    {
        long cutoffMs = _utcNow().ToUnixTimeMilliseconds()
            - (long)RecentActionsWindow.TotalMilliseconds;
        while (
            _recentActions.Count > 0
            && (_recentActions[0]["atMs"]?.GetValue<long?>() ?? 0) < cutoffMs
        )
        {
            _recentActions.RemoveAt(0);
        }
        while (_recentActions.Count > MaximumRecentActions)
        {
            _recentActions.RemoveAt(0);
        }
    }

    // 给模型的那份要带"多少秒前",而不是一个原始时间戳——模型不该自己去算
    // 现在减去 atMs。每次读的时候重新剪一遍:条目可能是几十秒前写入的,
    // 单靠写入时剪过一次不够,窗口要在**读**的这一刻仍然成立。
    private JsonArray BuildRecentActions()
    {
        PruneRecentActions();
        long nowMs = _utcNow().ToUnixTimeMilliseconds();
        JsonArray array = [];
        foreach (JsonObject entry in _recentActions)
        {
            long atMs = entry["atMs"]?.GetValue<long?>() ?? nowMs;
            array.Add(new JsonObject
            {
                ["kind"] = entry["kind"]?.DeepClone(),
                ["page"] = entry["page"]?.DeepClone(),
                ["secondsAgo"] = (int)Math.Max(
                    0,
                    (nowMs - atMs) / 1000),
            });
        }
        return array;
    }

    // 「用户此刻选中/聚焦着什么」,合并两个此前互相独立的单槽状态:_selection
    // (纯文字,来自 window.getSelection)与 _focus(卡片/图片/画布区域/高亮,
    // 来自点选)。两者**已经可能同时非空**——BuildFocus 只在 kind=="text"
    // 时才会覆盖 _selection,聚焦一张卡片不会清掉之前选中的文字——所以合并
    // 不需要新的前端手势,只是把两份已经存在的信号并到一处给模型看。
    //
    // 不做的事:真正的"同时选中好几个东西"(比如同时选两条高亮)需要全新的
    // 前端交互设计,现在完全没有;items 里最多两条(文字 + 聚焦对象),
    // 不是一个开放式的多选列表。
    private JsonArray BuildSelectionItems()
    {
        JsonArray items = [];
        if (
            StringValue(_selection["state"]) == "active"
            && StringValue(_selection["text"]) is string selectedText
        )
        {
            JsonObject textItem = new()
            {
                ["kind"] = "text",
                ["text"] = selectedText,
            };
            // 选中附近的原文,让模型不必回整页正文里找这句话的前后文。
            // 只在前端真的算出来时出现。
            if (StringValue(_selection["context"]) is string context)
            {
                textItem["context"] = context;
                if (StringValue(_selection["contextSource"])
                    is string contextSource)
                {
                    textItem["contextSource"] = contextSource;
                }
            }
            items.Add(textItem);
        }
        // kind=="text" 的 focus 只是 _selection 的影子(同一次选中在两处
        // 各存一份),在这里再放一条会让模型以为用户选中了两样东西。
        if (
            StringValue(_focus["state"]) == "active"
            && StringValue(_focus["kind"]) is string focusKind
            && focusKind != "text"
            && _focus["ref"] is JsonObject focusRef
        )
        {
            JsonObject item = new() { ["kind"] = focusKind };
            if (StringValue(focusRef["text"]) is string focusText)
            {
                item["text"] = focusText;
            }
            if (StringValue(focusRef["brief"]) is string brief)
            {
                item["text"] ??= brief;
            }
            // cid 是卡片批次的全局编号,批内单卡没有自己的号——只能给到
            // "这一批"的粒度,不能假装能定位到批内第几张。
            if (StringValue(focusRef["cid"]) is string cid)
            {
                item["ref"] = cid;
            }
            else if (StringValue(focusRef["id"]) is string id)
            {
                item["ref"] = id;
            }
            if (StringValue(focusRef["color"]) is string color)
            {
                item["color"] = color;
            }
            items.Add(item);
        }
        return items;
    }

    private void FoldJournal(DirectContextEvent contextEvent)
    {
        JsonObject value = JsonNode.Parse(
            contextEvent.Payload.GetRawText()) as JsonObject
            ?? throw JournalInvalid();
        JsonObject latestEvent = new()
        {
            ["source"] = "outgoing-context",
            ["seq"] = contextEvent.Sequence,
            ["id"] = contextEvent.EventId,
            ["type"] = contextEvent.Type,
            ["ts"] = value["ts"]?.DeepClone(),
        };

        if (contextEvent.Type == "page.context")
        {
            (JsonObject stablePage, JsonObject activeReading) =
                BuildPageContext(value);
            bool changedPage = _stablePage is not null
                && !SamePage(_stablePage, stablePage);
            if (
                _activeReading is JsonObject priorActive
                && SamePageEquivalent(priorActive, activeReading)
            )
            {
                PreserveActiveReadingContinuity(
                    priorActive,
                    activeReading,
                    preserveHighlightSource: true);
            }
            _stablePage = stablePage;
            _activeReading = activeReading;
            if (changedPage)
            {
                _focus = UnknownFocus("stable-page-changed");
                RecordAction(
                    "page-turn",
                    activeReading,
                    activeReading["observedAtEpochMs"]
                        ?.GetValue<long?>() ?? 0);
            }
            // Pi 一直在写这个字段(pdf_reader.py:3290 的 ctx["selection"]),
            // 这里此前从没读过——服务器托管书籍的选区经这条链路整段丢失,
            // 只有 App 直连的 WSS 活跃阅读上报能把选区带到模型面前。
            // 这个事件每次都代表"此刻的真实状态"(reason 由 Pi 按当时有没有
            // 选区二选一),所以缺字段要当作"现在没有选区"处理,不是"这条
            // 事件没提所以维持原样"——否则旧选区会在用户已经点掉之后继续
            // 挂在快照里。
            JsonObject? pageContext = value["page_context"] as JsonObject;
            string? reportedSelection = StringValue(
                pageContext?["selection"]);
            // 400 字符与控制符校验跟 WSS 活跃阅读那条路的 selection 字段
            // 同一套约束(line ~653) —— 两条路径最终写进同一个 _selection,
            // 不能一条严格一条放任,否则校验形同虚设。
            if (
                reportedSelection is { Length: > 400 }
                || (
                    reportedSelection is not null
                    && reportedSelection.Any(char.IsControl)
                )
            )
            {
                throw JournalInvalid();
            }
            // **字段缺席 ≠ 没有选区。** Pi 只在 has_sel 时才写 ctx["selection"]
            // (pdf_reader.py:3289 `if has_sel:`),所以"这条事件没提选区"是
            // 常态,不代表用户刚点掉了选中。把缺席当清空会抹掉 WSS 活跃阅读
            // 那条路刚送来的真实选区 —— 自检 direct-snapshot-events-
            // atomically-fold-latest 就是这么红的:同一页先来 active-reading
            // 带 "selected words",随后的 page.context 不带该键,选区被清掉。
            // 只有键**在场且为空**才是"用户取消了选中"这个明确信号。
            bool selectionReported = pageContext is not null
                && pageContext.ContainsKey("selection");
            if (selectionReported)
            {
                _selection = !string.IsNullOrEmpty(reportedSelection)
                    ? ActiveSelection(reportedSelection, activeReading)
                    : ClearedSelection(
                        changedPage
                            ? "stable-page-changed"
                            : "page-context-no-selection");
            }
            else if (changedPage)
            {
                // 翻页仍然清:上一页的选区挂在新页面上是错的。
                _selection = ClearedSelection("stable-page-changed");
            }
        }
        else if (contextEvent.Type == "focus")
        {
            FocusFoldResult folded = BuildFocus(
                value,
                _stablePage,
                _activeReading);
            _focus = folded.Focus;
            if (folded.Selection is not null)
            {
                _selection = folded.Selection;
            }
        }
        else if (contextEvent.Type == "drawing")
        {
            // 落笔前后各读一次 stable:FoldDrawingEvent 页不对就原样返回,
            // 这里必须拿折叠后真正生效的那份来判断"是不是这一刻刚稳定",
            // 不能拿传入的 value 猜 —— 传入 value 页不对时折叠根本没发生。
            bool wasStable = _stablePage?["visual"] is JsonObject beforeVisual
                && beforeVisual["drawing"] is JsonObject beforeDrawing
                && beforeDrawing["stable"]?.GetValueKind()
                    == JsonValueKind.True;
            JsonObject? folded = FoldDrawingEvent(value, _stablePage);
            bool nowStable = folded?["visual"] is JsonObject afterVisual
                && afterVisual["drawing"] is JsonObject afterDrawing
                && afterDrawing["stable"]?.GetValueKind()
                    == JsonValueKind.True;
            _stablePage = folded;
            if (!wasStable && nowStable && folded is not null)
            {
                // lastEditedAt 是**秒**(Python time.time() 风格,freshWindowS
                // 那个 S 后缀就是同一单位的提示),不是毫秒 —— 当成毫秒读会让
                // 每一次画图动作都显得发生在 1970 年,进 RecordAction 时立刻
                // 被 30 秒窗剪掉,画图这个动作就永远不会出现在 recentActions 里。
                double? lastEditedAtSeconds =
                    NumericValue(folded["visual"]?["drawing"]?["lastEditedAt"]);
                RecordAction(
                    "drawing",
                    folded,
                    lastEditedAtSeconds is double seconds
                        ? (long)(seconds * 1000)
                        : 0);
            }
        }
        else if (contextEvent.Type == "command-failed")
        {
            AddCommandFailure(latestEvent, value);
        }
        _revision = checked(_revision + 1);
        _latestEvent = latestEvent;
    }

    private (JsonObject StablePage, JsonObject ActiveReading)
        BuildPageContext(JsonObject value)
    {
        string? file = StringValue(value["file"])
            ?? StringValue(value["book_id"]);
        string? kind = StringValue(value["kind"]);
        JsonNode? page = value["page"]?.DeepClone();
        JsonObject? pageContext = value["page_context"] as JsonObject;
        bool stable = BooleanValue(
            value["stable"],
            out bool stableValue)
            && stableValue;
        if (
            string.IsNullOrWhiteSpace(file)
            || file.Length > 4096
            || file.Any(char.IsControl)
            || kind is not ("pdf" or "epub" or "html" or "web")
            || !ValidPageIdentifier(page, allowNull: false)
            || pageContext is null
            || !stable
        )
        {
            throw JournalInvalid();
        }
        JsonNode safePage = page
            ?? throw JournalInvalid();
        JsonObject next = new()
        {
            ["kind"] = kind,
            ["file"] = file,
            ["title"] = StringValue(value["title"]),
            ["page"] = safePage.DeepClone(),
            ["stable"] = true,
            ["reason"] = StringValue(pageContext["reason"]),
            ["text"] = StringValue(pageContext["text"]) ?? "",
            ["textAvailable"] =
                pageContext["text_available"]?.GetValue<bool?>(),
            ["textSource"] =
                StringValue(pageContext["text_source"]),
            ["fallbackReason"] =
                StringValue(pageContext["fallback_reason"]),
            ["truncated"] =
                pageContext["truncated"]?.GetValue<bool?>(),
        };
        JsonObject? visual = CopyVisual(
            pageContext["visual"],
            file,
            safePage);
        if (visual is not null)
        {
            next["visual"] = visual;
        }
        JsonObject? embeds = CopyEmbeds(pageContext["embeds"]);
        if (embeds is not null)
        {
            next["embeds"] = embeds;
        }
        JsonObject? viewport = CopyViewport(
            pageContext["viewport"],
            kind);
        if (viewport is not null)
        {
            next["viewport"] = viewport;
        }
        JsonObject? knowledge = CopyKnowledge(pageContext["knowledge"]);
        if (knowledge is not null)
        {
            next["knowledge"] = knowledge;
        }
        JsonObject activeReading = new()
        {
            ["kind"] = kind,
            ["file"] = file,
            ["title"] = StringValue(value["title"]),
            ["page"] = safePage.DeepClone(),
            ["fresh"] = true,
            ["ageSec"] = 0,
            ["observedAtEpochMs"] =
                EpochSecondsToMilliseconds(value["ts"]),
            ["receivedAtEpochMs"] =
                _utcNow().ToUnixTimeMilliseconds(),
        };
        return (next, activeReading);
    }

    private static FocusFoldResult BuildFocus(
        JsonObject value,
        JsonObject? stablePage,
        JsonObject? activeReading)
    {
        string? action = StringValue(value["action"]);
        if (action == "cancel")
        {
            JsonObject? cancelled =
                value["cancelledObject"] as JsonObject;
            string? kind = StringValue(cancelled?["kind"]);
            JsonObject? reference = cancelled?["ref"] as JsonObject;
            if (
                !ValidFocusKind(kind)
                || reference is null
            )
            {
                throw JournalInvalid();
            }
            JsonObject safeReference = CopyFocusReference(
                kind!,
                reference);
            return new FocusFoldResult(
                new JsonObject
                {
                    ["state"] = "cleared",
                    ["kind"] = kind,
                    ["ref"] = safeReference,
                    ["reason"] = "explicit-cancel",
                },
                kind == "text"
                    ? ClearedSelection("explicit-focus-cancel")
                    : null);
        }
        if (action is not ("set" or "replace"))
        {
            throw JournalInvalid();
        }
        string? focusKind = StringValue(value["kind"]);
        JsonObject? focusReference = value["ref"] as JsonObject;
        if (
            !ValidFocusKind(focusKind)
            || focusReference is null
        )
        {
            throw JournalInvalid();
        }
        JsonObject safe = CopyFocusReference(
            focusKind!,
            focusReference);
        JsonObject focus = new()
        {
            ["state"] = "active",
            ["kind"] = focusKind,
            ["ref"] = safe.DeepClone(),
            ["reason"] = null,
        };
        if (focusKind != "text")
        {
            return new FocusFoldResult(focus, null);
        }
        string? text = StringValue(safe["text"]);
        bool samePage =
            stablePage is not null
            && SameLogicalPage(stablePage, safe, activeReading);
        if (!samePage)
        {
            return new FocusFoldResult(
                focus,
                UnknownSelection("focus-page-mismatch"));
        }
        return new FocusFoldResult(
            focus,
            new JsonObject
            {
                ["state"] = "active",
                ["text"] = text,
                ["ref"] = new JsonObject
                {
                    ["file"] = safe["file"]?.DeepClone(),
                    ["page"] = safe["page"]?.DeepClone(),
                },
                ["reason"] = null,
            });
    }

    private static bool ValidFocusKind(string? kind) =>
        kind is "text" or "image" or "card"
            or "drawing" or "region" or "highlight";

    private static JsonObject CopyFocusReference(
        string kind,
        JsonObject reference)
    {
        JsonObject safe = new();
        if (kind == "text")
        {
            string file = RequiredFocusString(
                reference,
                "file",
                4096);
            JsonNode? page = reference["page"]?.DeepClone();
            string text = RequiredFocusString(
                reference,
                "text",
                4000,
                multiline: true);
            if (!ValidPageIdentifier(page, allowNull: false))
            {
                throw JournalInvalid();
            }
            safe["file"] = file;
            safe["page"] = page;
            safe["text"] = text;
            return safe;
        }

        if (kind == "drawing")
        {
            string file = RequiredFocusString(
                reference,
                "file",
                4096);
            JsonNode? page = reference["page"]?.DeepClone();
            string revision = RequiredFocusString(
                reference,
                "drawingRevision",
                64);
            if (
                !ValidPageIdentifier(page, allowNull: false)
                || !IsDrawingRevision(revision)
            )
            {
                throw JournalInvalid();
            }
            safe["file"] = file;
            safe["page"] = page;
            safe["drawingRevision"] = revision;
            safe["region"] = CopyFocusRegion(reference["region"]);
            return safe;
        }

        foreach (string field in new[]
        {
            "id",
            "cid",
            "label",
            "brief",
            "url",
            "src",
            "alt",
            "text",
            "file",
            // 高亮的颜色是它在正文里唯一的视觉身份(跟 ⟦HIGHLIGHT color=…⟧
            // 用的是同一个属性)——没有它,模型分不清用户选中的是哪一条高亮。
            "color",
        })
        {
            string? value = OptionalFocusString(
                reference,
                field,
                field is "brief" or "text" ? 4000 : 4096,
                multiline: field is "brief" or "text");
            if (value is not null)
            {
                safe[field] = value;
            }
        }
        JsonNode? anchoredPage = reference["page"]?.DeepClone();
        if (anchoredPage is not null)
        {
            if (!ValidPageIdentifier(anchoredPage, allowNull: false))
            {
                throw JournalInvalid();
            }
            safe["page"] = anchoredPage;
        }
        if (reference.ContainsKey("region"))
        {
            safe["region"] = CopyFocusRegion(reference["region"]);
        }
        bool hasLocator =
            safe["id"] is not null
            || safe["cid"] is not null
            || safe["url"] is not null
            || (
                safe["file"] is not null
                && safe["page"] is not null
            );
        if (!hasLocator)
        {
            throw JournalInvalid();
        }
        return safe;
    }

    private static JsonNode? CopyFocusRegion(JsonNode? node)
    {
        if (node is null || node.GetValueKind() == JsonValueKind.Null)
        {
            return null;
        }
        if (node is not JsonObject region)
        {
            throw JournalInvalid();
        }
        JsonObject safe = new();
        foreach (string field in new[]
        {
            "x",
            "y",
            "w",
            "h",
            "left",
            "top",
            "right",
            "bottom",
            "width",
            "height",
        })
        {
            if (!region.ContainsKey(field))
            {
                continue;
            }
            double? value = NumericValue(region[field]);
            if (value is null)
            {
                throw JournalInvalid();
            }
            safe[field] = value.Value;
        }
        if (safe.Count == 0 && region.Count != 0)
        {
            throw JournalInvalid();
        }
        return safe;
    }

    private static string RequiredFocusString(
        JsonObject source,
        string field,
        int maximumLength,
        bool multiline = false)
    {
        string? value = OptionalFocusString(
            source,
            field,
            maximumLength,
            multiline);
        if (string.IsNullOrWhiteSpace(value))
        {
            throw JournalInvalid();
        }
        return value;
    }

    private static string? OptionalFocusString(
        JsonObject source,
        string field,
        int maximumLength,
        bool multiline = false)
    {
        string? value = StringValue(source[field]);
        if (value is null)
        {
            return null;
        }
        if (
            value.Length > maximumLength
            || (
                multiline
                    ? value.Any(character =>
                        char.IsControl(character)
                        && character is not ('\r' or '\n' or '\t'))
                    : value.Any(char.IsControl)
            )
        )
        {
            throw JournalInvalid();
        }
        return multiline
            ? NormalizeEmbedText(value)
            : value;
    }

    private static void AddCommandFailure(
        JsonObject latestEvent,
        JsonObject value)
    {
        string correlation = RequiredFocusString(
            value,
            "correlation",
            80);
        string commandId = RequiredFocusString(
            value,
            "commandId",
            80);
        string taskId = RequiredFocusString(
            value,
            "taskId",
            80);
        string error = RequiredFocusString(
            value,
            "error",
            2000,
            multiline: true);
        if (
            !NonNegativeInteger(value["step"], out long step)
            || !BooleanValue(
                value["retryable"],
                out bool retryable)
        )
        {
            throw JournalInvalid();
        }
        latestEvent["correlation"] = correlation;
        latestEvent["commandId"] = commandId;
        latestEvent["taskId"] = taskId;
        latestEvent["step"] = step;
        latestEvent["retryable"] = retryable;
        latestEvent["error"] = error;
    }

    private static JsonObject? FoldDrawingEvent(
        JsonObject value,
        JsonObject? stablePage)
    {
        if (stablePage is null)
        {
            return null;
        }
        string? file = StringValue(value["file"]);
        JsonNode? page = value["page"];
        string? state = StringValue(value["state"]);
        if (
            string.IsNullOrWhiteSpace(file)
            || page is null
            || state is not ("pending" or "stable")
        )
        {
            throw JournalInvalid();
        }
        if (
            !string.Equals(
                StringValue(stablePage["file"]),
                file,
                StringComparison.Ordinal)
            || !PageEquivalent(stablePage["page"], page)
        )
        {
            return stablePage;
        }

        JsonObject next = stablePage.DeepClone() as JsonObject
            ?? throw JournalInvalid();
        JsonObject visual = next["visual"] as JsonObject
            ?? new JsonObject
            {
                ["page_image"] = null,
                ["has_ink"] = true,
            };
        JsonObject existingDrawing =
            visual["drawing"] as JsonObject
            ?? new JsonObject();
        double eventSeconds = NumericValue(value["ts"]) ?? 0;
        double lastEditedAt =
            NumericValue(existingDrawing["lastEditedAt"])
            ?? eventSeconds;
        double freshWindow =
            NumericValue(existingDrawing["freshWindowS"])
            ?? 120.0;
        if (state == "pending")
        {
            visual["drawing"] = new JsonObject
            {
                ["contract"] = "reader-outgoing-context/1",
                ["file"] = file,
                ["page"] = stablePage["page"]?.DeepClone(),
                ["freshness"] = "recent",
                ["lastEditedAt"] = eventSeconds,
                ["freshWindowS"] = freshWindow,
                ["inProgress"] = true,
                ["stable"] = false,
                ["drawingRevision"] = null,
                ["pendingSince"] = 0.0,
                ["ref"] = null,
                ["empty"] = false,
            };
        }
        else
        {
            string? revision = StringValue(value["drawingRevision"]);
            JsonObject? reference = value["ref"] as JsonObject;
            if (
                !IsDrawingRevision(revision)
                || reference is null
                || StringValue(reference["kind"]) != "drawing"
                || !string.Equals(
                    StringValue(reference["file"]),
                    file,
                    StringComparison.Ordinal)
                || !PageEquivalent(reference["page"], page)
                || StringValue(reference["revision"]) != revision
            )
            {
                throw JournalInvalid();
            }
            visual["drawing"] = new JsonObject
            {
                ["contract"] = "reader-outgoing-context/1",
                ["file"] = file,
                ["page"] = stablePage["page"]?.DeepClone(),
                ["freshness"] =
                    DrawingFreshness(existingDrawing["freshness"]),
                ["lastEditedAt"] = lastEditedAt,
                ["freshWindowS"] = freshWindow,
                ["inProgress"] = false,
                ["stable"] = true,
                ["drawingRevision"] = revision,
                ["pendingSince"] = null,
                ["ref"] = new JsonObject
                {
                    ["kind"] = "drawing",
                    ["file"] = file,
                    ["page"] = stablePage["page"]?.DeepClone(),
                    ["revision"] = revision,
                },
                ["empty"] = false,
            };
        }
        visual["has_ink"] = true;
        next["visual"] = visual;
        return next;
    }

    private static JsonObject? CopyVisual(
        JsonNode? node,
        string file,
        JsonNode page)
    {
        if (node is null)
        {
            return null;
        }
        if (node is not JsonObject value)
        {
            throw JournalInvalid();
        }
        JsonNode? imageNode = value["page_image"];
        string? image = StringValue(imageNode);
        if (
            imageNode is not null
            && imageNode.GetValueKind() != JsonValueKind.Null
            && (
                image is null
                || !image.StartsWith(
                    "/pdf/api/page-image?",
                    StringComparison.Ordinal)
                || image.Length > 8192
                || image.Any(char.IsControl)
            )
        )
        {
            throw JournalInvalid();
        }
        if (!BooleanValue(value["has_ink"], out bool hasInk))
        {
            throw JournalInvalid();
        }
        JsonObject? drawing = CopyDrawing(
            value["drawing"],
            file,
            page);
        if (
            (drawing is null && hasInk)
            || (
                drawing is not null
                && drawing["empty"]?.GetValue<bool?>()
                    == hasInk
            )
        )
        {
            throw JournalInvalid();
        }
        return new JsonObject
        {
            ["page_image"] = image,
            ["has_ink"] = hasInk,
            ["drawing"] = drawing,
        };
    }

    private static JsonObject? CopyDrawing(
        JsonNode? node,
        string file,
        JsonNode page)
    {
        if (node is null || node.GetValueKind() == JsonValueKind.Null)
        {
            return null;
        }
        if (node is not JsonObject value)
        {
            throw JournalInvalid();
        }
        string? contract = StringValue(value["contract"]);
        string? drawingFile = StringValue(value["file"]);
        JsonNode? drawingPage = value["page"];
        string? freshness = StringValue(value["freshness"]);
        string? revision = StringValue(value["drawingRevision"]);
        if (
            contract != "reader-outgoing-context/1"
            || !string.Equals(
                drawingFile,
                file,
                StringComparison.Ordinal)
            || drawingPage is null
            || !PageEquivalent(page, drawingPage)
            || freshness is not ("none" or "recent" or "stale")
            || !BooleanValue(value["inProgress"], out bool inProgress)
            || !BooleanValue(value["stable"], out bool stable)
            || !BooleanValue(value["empty"], out bool empty)
        )
        {
            throw JournalInvalid();
        }
        double? lastEditedAt = NullableNumericValue(
            value["lastEditedAt"]);
        double? freshWindow = NumericValue(value["freshWindowS"]);
        double? pendingSince = NullableNumericValue(
            value["pendingSince"]);
        if (
            freshWindow is null
            || freshWindow is <= 0 or > 3600
            || (
                lastEditedAt is not null
                && (
                    !double.IsFinite(lastEditedAt.Value)
                    || lastEditedAt <= 0
                )
            )
            || (
                pendingSince is not null
                && (
                    !double.IsFinite(pendingSince.Value)
                    || pendingSince < 0
                )
            )
        )
        {
            throw JournalInvalid();
        }

        JsonObject? reference = null;
        if (value["ref"] is JsonObject sourceReference)
        {
            if (
                StringValue(sourceReference["kind"]) != "drawing"
                || !string.Equals(
                    StringValue(sourceReference["file"]),
                    file,
                    StringComparison.Ordinal)
                || sourceReference["page"] is not JsonNode refPage
                || !PageEquivalent(page, refPage)
                || StringValue(sourceReference["revision"])
                    != revision
            )
            {
                throw JournalInvalid();
            }
            reference = new JsonObject
            {
                ["kind"] = "drawing",
                ["file"] = file,
                ["page"] = page.DeepClone(),
                ["revision"] = revision,
            };
        }
        else if (
            value["ref"] is not null
            && value["ref"]!.GetValueKind() != JsonValueKind.Null
        )
        {
            throw JournalInvalid();
        }

        if (
            empty != (freshness == "none")
            || (empty && (
                inProgress
                || stable
                || revision is not null
                || reference is not null
                || lastEditedAt is not null
                || pendingSince is not null
            ))
            || (inProgress && (empty || stable))
            || (stable && (
                !IsDrawingRevision(revision)
                || reference is null
                || inProgress
                || pendingSince is not null
            ))
            || (!stable && (
                revision is not null
                || reference is not null
            ))
            || (!empty && !stable && pendingSince is null)
            || (!empty && lastEditedAt is null)
        )
        {
            throw JournalInvalid();
        }
        return new JsonObject
        {
            ["contract"] = contract,
            ["file"] = file,
            ["page"] = page.DeepClone(),
            ["freshness"] = freshness,
            ["lastEditedAt"] = lastEditedAt,
            ["freshWindowS"] = freshWindow,
            ["inProgress"] = inProgress,
            ["stable"] = stable,
            ["drawingRevision"] = revision,
            ["pendingSince"] = pendingSince,
            ["ref"] = reference,
            ["empty"] = empty,
        };
    }

    // 这一页在图谱上对应哪些知识点。正文说的是"这页写了什么字",
    // 这里说的是"这页在讲什么" —— 助手最常需要再问一轮的东西。
    //
    // 与 CopyVisual/CopyEmbeds 不同,格式不合时**不丢整条**,只把这个字段换成
    // 一句说明。正文是主线,知识点是增强;为一个增强字段丢掉整页正文,
    // 用户付出的代价远大于收益。但也不静默:换上的说明会一路带到助手那里。
    private const int KnowledgeNameLimit = 200;
    private const int KnowledgeSummaryLimit = 400;
    private const int KnowledgeConceptLimit = 8;

    private static JsonObject KnowledgeUnavailable(string reason) =>
        new()
        {
            ["available"] = false,
            ["reason"] = reason,
            ["book"] = null,
            ["section"] = null,
            ["concepts"] = new JsonArray(),
        };

    private static JsonObject? CopyKnowledge(JsonNode? node)
    {
        // 没有这个字段 = 对面还是旧版 Pi。不是错误,也不必解释。
        if (node is null)
        {
            return null;
        }
        if (node is not JsonObject value)
        {
            return KnowledgeUnavailable("知识图谱字段格式不正确");
        }
        bool available =
            value["available"]?.GetValueKind() == JsonValueKind.True;
        JsonObject result = new()
        {
            ["available"] = available,
            ["reason"] = SafeKnowledgeText(
                value["reason"], KnowledgeSummaryLimit),
            ["book"] = SafeKnowledgeText(value["book"], KnowledgeNameLimit),
        };
        if (value["section"] is JsonObject section)
        {
            JsonObject? safeSection = CopyKnowledgeNode(section);
            if (safeSection is null)
            {
                return KnowledgeUnavailable("知识图谱节点格式不正确");
            }
            result["section"] = safeSection;
        }
        else
        {
            result["section"] = null;
        }
        JsonArray concepts = [];
        if (value["concepts"] is JsonArray raw)
        {
            if (raw.Count > KnowledgeConceptLimit)
            {
                return KnowledgeUnavailable("知识图谱节点过多");
            }
            foreach (JsonNode? item in raw)
            {
                if (item is not JsonObject concept)
                {
                    return KnowledgeUnavailable("知识图谱节点格式不正确");
                }
                JsonObject? safe = CopyKnowledgeNode(concept);
                if (safe is null)
                {
                    return KnowledgeUnavailable("知识图谱节点格式不正确");
                }
                concepts.Add(safe);
            }
        }
        result["concepts"] = concepts;
        if (
            NonNegativeInteger(
                value["concepts_truncated"],
                out long dropped)
            && dropped > 0
        )
        {
            result["conceptsTruncated"] = dropped;
        }
        // 摘要是建图时写的概括,不是书上的字。不把这句带过去,助手会拿它当原文
        // 引用,而用户翻到那页会发现书上没有这句话。
        string? note = SafeKnowledgeText(
            value["note"], KnowledgeSummaryLimit);
        if (!string.IsNullOrEmpty(note))
        {
            result["note"] = note;
        }
        return result;
    }

    private static JsonObject? CopyKnowledgeNode(JsonObject node)
    {
        string? name = SafeKnowledgeText(node["name"], KnowledgeNameLimit);
        if (string.IsNullOrWhiteSpace(name))
        {
            return null;
        }
        JsonObject safe = new() { ["name"] = name };
        foreach ((string field, int limit) in new[]
        {
            ("id", KnowledgeNameLimit),
            ("type", KnowledgeNameLimit),
            ("summary", KnowledgeSummaryLimit),
        })
        {
            if (node[field] is null)
            {
                continue;
            }
            string? text = SafeKnowledgeText(node[field], limit);
            if (text is null)
            {
                return null;
            }
            safe[field] = text;
        }
        if (node["summary_truncated"]?.GetValueKind() == JsonValueKind.True)
        {
            safe["summaryTruncated"] = true;
        }
        return safe;
    }

    private static string? SafeKnowledgeText(JsonNode? node, int limit)
    {
        if (node is null || node.GetValueKind() == JsonValueKind.Null)
        {
            return null;
        }
        string? text = StringValue(node);
        if (text is null || text.Length > limit || text.Any(char.IsControl))
        {
            return null;
        }
        return text;
    }

    private static JsonObject? CopyEmbeds(JsonNode? node)
    {
        if (node is null)
        {
            return null;
        }
        if (
            node is not JsonObject value
            || !NonNegativeInteger(
                value["highlights"],
                out long highlights)
            || !NonNegativeInteger(
                value["blocks"],
                out long blocks)
            || value["unanchored"] is not JsonArray unanchored
            || unanchored.Count > 64
        )
        {
            throw JournalInvalid();
        }
        JsonArray safeUnanchored = [];
        foreach (JsonNode? itemNode in unanchored)
        {
            if (itemNode is not JsonObject item)
            {
                throw JournalInvalid();
            }
            string? reason = StringValue(item["_reason"]);
            if (reason is not (
                "empty_text"
                or "no_text"
                or "not_found_in_page_text"
                or "whitespace_mismatch"
                or "overlaps_earlier_highlight"))
            {
                throw JournalInvalid();
            }
            JsonObject safe = new()
            {
                ["_reason"] = reason,
            };
            foreach (string field in new[]
            {
                "id",
                "text",
                "color",
                "note",
                "kind",
            })
            {
                JsonNode? source = item[field];
                if (source is null)
                {
                    continue;
                }
                string? text = StringValue(source);
                bool multiline = field is "text" or "note";
                if (
                    text is null
                    || text.Length > 4000
                    || (
                        !multiline
                        && text.Any(char.IsControl)
                    )
                    || (
                        multiline
                        && text.Any(character =>
                            char.IsControl(character)
                            && character is not (
                                '\r' or '\n' or '\t'))
                    )
                )
                {
                    throw JournalInvalid();
                }
                safe[field] = multiline
                    ? NormalizeEmbedText(text)
                    : text;
            }
            safeUnanchored.Add(safe);
        }
        JsonObject copied = new()
        {
            ["highlights"] = highlights,
            ["blocks"] = blocks,
            ["unanchored"] = safeUnanchored,
        };
        // 标注读取失败的原因过去止步于此:Pi 会写 embeds.error,而这里只重建
        // 三个键,于是诊断在跨机边界上蒸发。零计数和读不出来在下游长得一样,
        // 用户看不到自己的高亮时也就无从查起。
        // 键名是 sidecarError 而不是通用的 error:自检
        // direct-snapshot-folds-whitelisted-metadata 会喂一个
        // error="diagnostic-not-contract" 来验证"合同外字段一律剥掉"这条规则,
        // 沿用 error 等于把那条规则挖个洞。专属键名让诊断和白名单纪律共存。
        if (value["sidecarError"] is not null)
        {
            string? reason = StringValue(value["sidecarError"]);
            if (
                reason is null
                || reason.Length > 240
                || reason.Any(char.IsControl)
            )
            {
                throw JournalInvalid();
            }
            copied["sidecarError"] = reason;
        }
        return copied;
    }

    private static JsonObject? CopyViewport(
        JsonNode? node,
        string kind)
    {
        if (node is null)
        {
            return null;
        }
        if (
            kind != "epub"
            || node is not JsonObject value
            || !NonNegativeInteger(value["center"], out long center)
            || !NonNegativeInteger(value["from"], out long from)
            || !NonNegativeInteger(value["to"], out long to)
            || !NonNegativeInteger(value["total"], out long total)
            || !NonNegativeInteger(value["pad"], out long pad)
            || from > center
            || center >= to
            || to > total
            || from != Math.Max(0, center - pad)
            || to != Math.Min(total, center + pad + 1)
        )
        {
            throw JournalInvalid();
        }
        return new JsonObject
        {
            ["center"] = center,
            ["from"] = from,
            ["to"] = to,
            ["total"] = total,
            ["pad"] = pad,
        };
    }

    private static JsonObject CopyReadingWindow(
        JsonNode? node,
        string sourceInstanceId,
        string documentKey,
        string file)
    {
        if (node is not JsonObject value)
        {
            throw JournalInvalid();
        }
        string? contract = StringValue(value["contract"]);
        string? source = StringValue(value["sourceInstanceId"]);
        string? key = StringValue(value["documentKey"]);
        string? url = StringValue(value["url"]);
        string? title = StringValue(value["title"]);
        string? before = StringValue(value["beforeText"]);
        string? visible = StringValue(value["visibleText"]);
        string? after = StringValue(value["afterText"]);
        string? selectionState = StringValue(
            value["selectionState"]);
        string? selection = StringValue(value["selection"]);
        long? observedAt = value["observedAtEpochMs"]
            ?.GetValue<long?>();
        long? receivedAt = value["receivedAtEpochMs"]
            ?.GetValue<long?>();
        bool hasControlCorrelation = value.ContainsKey(
            "controlCorrelation");
        string? controlCorrelation = StringValue(
            value["controlCorrelation"]);
        if (
            contract != "reader-viewport/1"
            || !string.Equals(
                source,
                sourceInstanceId,
                StringComparison.Ordinal)
            || !string.Equals(
                key,
                documentKey,
                StringComparison.Ordinal)
            || !string.Equals(url, file, StringComparison.Ordinal)
            || title is { Length: > 1024 }
            || before is null or { Length: > 12_000 }
            || visible is null or { Length: > 24_000 }
            || after is null or { Length: > 12_000 }
            || selectionState is not (
                "active" or "cleared" or "unknown")
            || selection is { Length: > 400 }
            || (selectionState == "active")
                != !string.IsNullOrWhiteSpace(selection)
            || observedAt is null or < 1
            || receivedAt is null or < 1
            || (
                hasControlCorrelation
                && (
                    string.IsNullOrEmpty(controlCorrelation)
                    || controlCorrelation.Length > 160
                    || !DirectBridgeContract.IsSafeId(
                        controlCorrelation)
                )
            )
        )
        {
            throw JournalInvalid();
        }
        JsonObject restored = new()
        {
            ["contract"] = contract,
            ["sourceInstanceId"] = source,
            ["documentKey"] = key,
            ["url"] = url,
            ["title"] = title,
            ["beforeText"] = before,
            ["visibleText"] = visible,
            ["afterText"] = after,
            ["selectionState"] = selectionState,
            ["selection"] = selectionState == "active"
                ? selection
                : null,
            ["observedAtEpochMs"] = observedAt,
            ["receivedAtEpochMs"] = receivedAt,
        };
        if (controlCorrelation is not null)
        {
            restored["controlCorrelation"] = controlCorrelation;
        }
        return restored;
    }

    private static bool BooleanValue(
        JsonNode? node,
        out bool value)
    {
        if (
            node is JsonValue jsonValue
            && jsonValue.TryGetValue(out value)
        )
        {
            return true;
        }
        value = false;
        return false;
    }

    private static bool NonNegativeInteger(
        JsonNode? node,
        out long value)
    {
        if (
            node is JsonValue jsonValue
            && jsonValue.TryGetValue(out value)
            && value >= 0
        )
        {
            return true;
        }
        value = 0;
        return false;
    }

    private static bool ValidPageIdentifier(
        JsonNode? node,
        bool allowNull)
    {
        if (node is null)
        {
            return allowNull;
        }
        if (node is not JsonValue value)
        {
            return false;
        }
        if (value.TryGetValue(out long number))
        {
            return number is >= 0 and <= 9_007_199_254_740_991;
        }
        return value.TryGetValue(out string? text)
            && text is { Length: >= 1 and <= 256 }
            && !text.Any(char.IsControl);
    }

    private static string NormalizeEmbedText(string value) =>
        value.Replace("\r\n", "\n", StringComparison.Ordinal)
            .Replace("\r", "\n", StringComparison.Ordinal)
            .Replace("\t", "    ", StringComparison.Ordinal);

    private static double? NumericValue(JsonNode? node)
    {
        if (node is not JsonValue value)
        {
            return null;
        }
        if (
            value.TryGetValue(out double number)
            && double.IsFinite(number)
        )
        {
            return number;
        }
        return null;
    }

    private static double? NullableNumericValue(JsonNode? node)
    {
        if (node is null || node.GetValueKind() == JsonValueKind.Null)
        {
            return null;
        }
        double? value = NumericValue(node);
        return value ?? throw JournalInvalid();
    }

    private static bool PageEquivalent(
        JsonNode? left,
        JsonNode? right)
    {
        if (JsonNode.DeepEquals(left, right))
        {
            return true;
        }
        return left is not null
            && right is not null
            && string.Equals(
                left.ToJsonString().Trim('"'),
                right.ToJsonString().Trim('"'),
                StringComparison.Ordinal);
    }

    private static bool IsDrawingRevision(string? value) =>
        value is { Length: 19 }
        && value.StartsWith("dr_", StringComparison.Ordinal)
        && value[3..].All(character =>
            character is >= '0' and <= '9'
            or >= 'a' and <= 'f');

    private static string DrawingFreshness(JsonNode? node)
    {
        string? value = StringValue(node);
        return value is "recent" or "stale"
            ? value
            : "recent";
    }

    private JsonObject BuildSnapshot()
    {
        string contextStatus = _stablePage is null
            ? "pending"
            : "ready";
        JsonObject? effectivePage = _stablePage?.DeepClone()
            as JsonObject;
        if (_activeReading is not null)
        {
            if (
                _stablePage is null
                || !SamePage(_stablePage, _activeReading)
                || WebSourceDiffers(_stablePage, _activeReading)
            )
            {
                contextStatus = "pending";
                effectivePage = PendingPage(_activeReading);
            }
        }
        if (
            contextStatus == "ready"
            && effectivePage is not null
            && _activeReading is not null
            && SamePageEquivalent(effectivePage, _activeReading)
        )
        {
            // page.context carries content, while active-reading carries the
            // live source identity used by on-demand visual capture.  A local
            // PDF/EPUB page does not send a separate viewport packet, so the
            // two facts must be joined here once their canonical page matches.
            // Without this, the snapshot is ready and has ink, but visual
            // tools reject it because currentPage has no sourceInstanceId.
            if (
                StringValue(_activeReading["sourceInstanceId"])
                    is string sourceInstanceId
            )
            {
                effectivePage["sourceInstanceId"] = sourceInstanceId;
            }
            if (
                _activeReading["selectionRegions"]
                    is JsonObject regions
            )
            {
                effectivePage["selectionRegions"] = regions.DeepClone();
            }
            if (
                _activeReading["highlightSource"]
                    is JsonObject highlightSource
            )
            {
                effectivePage["highlightSource"] =
                    highlightSource.DeepClone();
            }
        }
        JsonObject? publicActiveReading = _activeReading?.DeepClone()
            as JsonObject;
        // The range source belongs to currentPage. Keep the internal copy for
        // continuity/joining, but do not serialize the same bounded source a
        // second time under activeReading.
        publicActiveReading?.Remove("highlightSource");
        return new JsonObject
        {
            ["schema"] = SnapshotContract,
            // revision is monotonic only for one running writer.  A stable
            // producer identity lets long-lived MCP clients distinguish a
            // service restart from an out-of-order write.
            ["producerInstanceId"] = _producerInstanceId,
            ["revision"] = _revision,
            ["updatedAtUtc"] = _utcNow()
                .ToString("O"),
            ["latestEvent"] = _latestEvent?.DeepClone(),
            ["recentActions"] = BuildRecentActions(),
            ["activeReading"] = publicActiveReading,
            ["contextStatus"] = contextStatus,
            ["currentPage"] = effectivePage,
            ["selection"] = _selection.DeepClone(),
            ["focus"] = _focus.DeepClone(),
            ["selectedItems"] = BuildSelectionItems(),
        };
    }

    private async Task PersistAsync(CancellationToken cancellationToken)
    {
        JsonObject snapshot = BuildSnapshot();
        byte[] payload = JsonSerializer.SerializeToUtf8Bytes(
            snapshot,
            DirectBridgeContract.JsonOptions);
        if (payload.Length is < 1 or > MaximumSnapshotBytes)
        {
            throw new DirectProtocolException(
                "BW_READER_CONTEXT_SNAPSHOT_TOO_LARGE",
                "Windows 本地 Reader 快照超过大小上限");
        }
        string? directory = System.IO.Path.GetDirectoryName(_statePath);
        if (directory is null)
        {
            throw SnapshotWriteFailed();
        }
        string temporaryPath = _statePath + ".tmp";
        try
        {
            Directory.CreateDirectory(directory);
            await File.WriteAllTextAsync(
                temporaryPath,
                Utf8WithoutBom.GetString(payload),
                Utf8WithoutBom,
                cancellationToken).ConfigureAwait(false);
            File.Move(temporaryPath, _statePath, overwrite: true);
            await DirectSnapshotMarkdown.WriteBestEffortAsync(
                snapshot,
                _statePath,
                CancellationToken.None).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (Exception exception) when (
            exception is IOException
            or UnauthorizedAccessException
            or ArgumentException
        )
        {
            throw SnapshotWriteFailed(exception);
        }
        finally
        {
            Array.Clear(payload);
            try
            {
                File.Delete(temporaryPath);
            }
            catch
            {
            }
        }
    }

    private AdapterState CaptureState() =>
        new(
            _revision,
            _stablePage?.DeepClone() as JsonObject,
            _activeReading?.DeepClone() as JsonObject,
            _selection.DeepClone() as JsonObject
                ?? UnknownSelection("snapshot-checkpoint-failed"),
            _focus.DeepClone() as JsonObject
                ?? UnknownFocus("snapshot-checkpoint-failed"),
            _latestEvent?.DeepClone() as JsonObject,
            _recentEventOrder.ToArray(),
            _recentActions
                .Select(entry => entry.DeepClone() as JsonObject
                    ?? throw JournalInvalid())
                .ToArray(),
            _recentActionsFile);

    private void RestoreState(AdapterState state)
    {
        _revision = state.Revision;
        _stablePage = state.StablePage?.DeepClone() as JsonObject;
        _activeReading =
            state.ActiveReading?.DeepClone() as JsonObject;
        _selection = state.Selection.DeepClone() as JsonObject
            ?? UnknownSelection("snapshot-rollback-failed");
        _focus = state.Focus.DeepClone() as JsonObject
            ?? UnknownFocus("snapshot-rollback-failed");
        _latestEvent =
            state.LatestEvent?.DeepClone() as JsonObject;
        _recentEventOrder.Clear();
        _recentEventIds.Clear();
        foreach (string eventId in state.RecentEventOrder)
        {
            _recentEventOrder.Enqueue(eventId);
            _recentEventIds.Add(eventId);
        }
        _recentActions.Clear();
        foreach (JsonObject entry in state.RecentActions)
        {
            _recentActions.Add(entry.DeepClone() as JsonObject
                ?? throw JournalInvalid());
        }
        _recentActionsFile = state.RecentActionsFile;
    }

    private void LoadExistingState()
    {
        try
        {
            if (!File.Exists(_statePath))
            {
                return;
            }
            FileInfo info = new(_statePath);
            if (info.Length is <= 0 or > MaximumSnapshotBytes)
            {
                return;
            }
            JsonObject? root = JsonNode.Parse(
                File.ReadAllText(_statePath, Encoding.UTF8)) as JsonObject;
            if (
                root?["schema"]?.GetValue<string>() != SnapshotContract
                || root["revision"]?.GetValue<long?>() is not long revision
                || revision < 0
            )
            {
                return;
            }
            _revision = revision;
            _activeReading = RestoreActiveReading(
                root["activeReading"] as JsonObject);
            JsonObject? currentPage = root["currentPage"]
                as JsonObject;
            if (
                _activeReading is JsonObject restoredActive
                && currentPage?["highlightSource"] is JsonNode source
            )
            {
                JsonObject? restoredSource = RestoreHighlightSource(
                    source,
                    StringValue(restoredActive["kind"])
                        ?? throw JournalInvalid(),
                    StringValue(restoredActive["file"])
                        ?? throw JournalInvalid(),
                    restoredActive["page"]?.DeepClone(),
                    restoredActive["observedAtEpochMs"]
                        ?.GetValue<long?>());
                if (restoredSource is not null)
                {
                    restoredActive["highlightSource"] = restoredSource;
                }
            }
            if (
                currentPage?["stable"]?.GetValue<bool?>() == true
            )
            {
                _stablePage = RestoreStablePage(
                    currentPage,
                    _activeReading);
            }
            _selection = RestoreSelection(
                root["selection"] as JsonObject);
            _focus = RestoreFocus(
                root["focus"] as JsonObject);
            _latestEvent = root["latestEvent"]?.DeepClone()
                as JsonObject;
        }
        catch
        {
            // A damaged prior cache is not authoritative. Keep it untouched
            // until the first validated update atomically replaces it.
            _revision = 0;
            _stablePage = null;
            _activeReading = null;
            _selection = UnknownSelection("snapshot-invalid");
            _focus = UnknownFocus("snapshot-invalid");
            _latestEvent = null;
        }
    }

    private void RememberEvent(string eventId)
    {
        if (!_recentEventIds.Add(eventId))
        {
            return;
        }
        _recentEventOrder.Enqueue(eventId);
        while (_recentEventOrder.Count > RecentEventLimit)
        {
            _recentEventIds.Remove(_recentEventOrder.Dequeue());
        }
    }

    private static JsonObject PendingPage(JsonObject active) =>
        new()
        {
            ["kind"] = active["kind"]?.DeepClone(),
            ["file"] = active["file"]?.DeepClone(),
            ["title"] = active["title"]?.DeepClone(),
            ["page"] = active["page"]?.DeepClone(),
            ["stable"] = false,
            ["text"] = "",
            ["textAvailable"] = false,
        };

    private static JsonObject? RestoreActiveReading(
        JsonObject? source)
    {
        if (source is null)
        {
            return null;
        }
        string? kind = StringValue(source["kind"]);
        string? file = StringValue(source["file"]);
        JsonNode? page = source["page"]?.DeepClone();
        if (
            kind is not ("pdf" or "epub" or "html" or "web")
            || string.IsNullOrWhiteSpace(file)
            || !source.ContainsKey("page")
            || !ValidPageIdentifier(page, allowNull: true)
        )
        {
            throw JournalInvalid();
        }
        JsonObject restored = new()
        {
            ["kind"] = kind,
            ["file"] = file,
            ["title"] = StringValue(source["title"]),
            ["page"] = page,
            ["fresh"] = source["fresh"]?.GetValue<bool?>(),
            ["ageSec"] = source["ageSec"]?.GetValue<long?>(),
            ["observedAtEpochMs"] =
                source["observedAtEpochMs"]?.GetValue<long?>(),
            ["receivedAtEpochMs"] =
                source["receivedAtEpochMs"]?.GetValue<long?>(),
        };
        string? sourceInstanceId = StringValue(
            source["sourceInstanceId"]);
        if (sourceInstanceId is not null)
        {
            if (
                sourceInstanceId.Length > 160
                || !DirectBridgeContract.IsSafeId(sourceInstanceId)
            )
            {
                throw JournalInvalid();
            }
            restored["sourceInstanceId"] = sourceInstanceId;
        }
        bool hasViewFile = source.ContainsKey("viewFile");
        bool hasViewPage = source.ContainsKey("viewPage");
        if (hasViewFile != hasViewPage)
        {
            throw JournalInvalid();
        }
        if (hasViewFile)
        {
            string? viewFile = StringValue(source["viewFile"]);
            JsonNode? viewPage = source["viewPage"]?.DeepClone();
            if (
                kind == "web"
                || viewFile is null
                || !viewFile.StartsWith("vbook:", StringComparison.Ordinal)
                || viewFile.Length is < 7 or > 4096
                || viewFile.Any(char.IsControl)
                || !ValidPageIdentifier(viewPage, allowNull: false)
            )
            {
                throw JournalInvalid();
            }
            restored["viewFile"] = viewFile;
            restored["viewPage"] = viewPage;
        }
        JsonObject? selectionRegions = RestoreSelectionRegions(
            source["selectionRegions"]);
        if (selectionRegions is not null)
        {
            restored["selectionRegions"] = selectionRegions;
        }
        JsonObject? highlightSource = RestoreHighlightSource(
            source["highlightSource"],
            kind,
            file,
            page,
            restored["observedAtEpochMs"]?.GetValue<long?>());
        if (highlightSource is not null)
        {
            restored["highlightSource"] = highlightSource;
        }
        return restored;
    }

    private static JsonObject? RestoreHighlightSource(
        JsonNode? source,
        string kind,
        string file,
        JsonNode? page,
        long? observedAt)
    {
        if (source is null)
        {
            return null;
        }
        if (page is null || observedAt is null)
        {
            throw JournalInvalid();
        }
        try
        {
            using JsonDocument sourceDocument = JsonDocument.Parse(
                source.ToJsonString(DirectBridgeContract.JsonOptions));
            using JsonDocument pageDocument = JsonDocument.Parse(
                page.ToJsonString(DirectBridgeContract.JsonOptions));
            JsonElement validated = ValidateHighlightSource(
                sourceDocument.RootElement,
                kind,
                file,
                pageDocument.RootElement,
                observedAt.Value);
            return JsonNode.Parse(validated.GetRawText()) as JsonObject
                ?? throw JournalInvalid();
        }
        catch (DirectProtocolException)
        {
            throw JournalInvalid();
        }
        catch (JsonException)
        {
            throw JournalInvalid();
        }
    }

    private static JsonObject? RestoreSelectionRegions(JsonNode? source)
    {
        if (source is null)
        {
            return null;
        }
        try
        {
            using JsonDocument document = JsonDocument.Parse(
                source.ToJsonString(DirectBridgeContract.JsonOptions));
            JsonElement validated = ValidateSelectionRegions(
                document.RootElement);
            return JsonNode.Parse(validated.GetRawText()) as JsonObject
                ?? throw JournalInvalid();
        }
        catch (DirectProtocolException)
        {
            throw JournalInvalid();
        }
        catch (JsonException)
        {
            throw JournalInvalid();
        }
    }

    private static JsonObject RestoreStablePage(
        JsonObject source,
        JsonObject? active)
    {
        string? file = StringValue(source["file"]);
        string? kind = StringValue(source["kind"])
            ?? StringValue(active?["kind"])
            ?? InferKind(file);
        JsonNode? page = source["page"]?.DeepClone();
        if (
            string.IsNullOrWhiteSpace(file)
            || kind is not ("pdf" or "epub" or "html" or "web")
            || !ValidPageIdentifier(page, allowNull: false)
            || source["stable"]?.GetValue<bool?>() != true
        )
        {
            throw JournalInvalid();
        }
        JsonObject restored = new()
        {
            ["kind"] = kind,
            ["file"] = file,
            ["title"] = StringValue(source["title"]),
            ["page"] = page,
            ["stable"] = true,
            ["reason"] = StringValue(source["reason"]),
            ["text"] = StringValue(source["text"]) ?? "",
            ["textAvailable"] =
                source["textAvailable"]?.GetValue<bool?>(),
            ["textSource"] = StringValue(source["textSource"]),
            ["fallbackReason"] =
                StringValue(source["fallbackReason"]),
            ["truncated"] =
                source["truncated"]?.GetValue<bool?>(),
        };
        JsonObject? visual = CopyVisual(
            source["visual"],
            file,
            page!);
        if (visual is not null)
        {
            restored["visual"] = visual;
        }
        JsonObject? embeds = CopyEmbeds(source["embeds"]);
        if (embeds is not null)
        {
            restored["embeds"] = embeds;
        }
        JsonObject? viewport = CopyViewport(
            source["viewport"],
            kind);
        if (viewport is not null)
        {
            restored["viewport"] = viewport;
        }
        string? sourceInstanceId = StringValue(
            source["sourceInstanceId"]);
        string? documentKey = StringValue(source["documentKey"]);
        if (sourceInstanceId is not null || documentKey is not null)
        {
            if (
                string.IsNullOrEmpty(sourceInstanceId)
                || sourceInstanceId.Length > 160
                || !DirectBridgeContract.IsSafeId(sourceInstanceId)
                || string.IsNullOrEmpty(documentKey)
                || documentKey.Length > 4096
                || documentKey.Any(char.IsControl)
                || !Uri.TryCreate(
                    documentKey,
                    UriKind.Absolute,
                    out Uri? documentUri)
                || documentUri.Scheme is not ("http" or "https")
            )
            {
                throw JournalInvalid();
            }
            restored["sourceInstanceId"] = sourceInstanceId;
            restored["documentKey"] = documentKey;
            restored["readingWindow"] = CopyReadingWindow(
                source["readingWindow"],
                sourceInstanceId,
                documentKey,
                file);
        }
        return restored;
    }

    private static JsonObject RestoreSelection(JsonObject? source)
    {
        string? state = StringValue(source?["state"]);
        if (state is not ("active" or "cleared" or "unknown"))
        {
            return UnknownSelection("snapshot-invalid-selection");
        }
        string? text = StringValue(source?["text"]);
        if (
            state == "active"
            && string.IsNullOrWhiteSpace(text)
        )
        {
            return UnknownSelection("snapshot-invalid-selection");
        }
        JsonObject restored = new()
        {
            ["state"] = state,
            ["text"] = state == "active" ? text : null,
            ["ref"] = source?["ref"]?.DeepClone(),
            ["reason"] = StringValue(source?["reason"]),
        };
        // 上下文要在重启后活下来:这里逐键重建,不补这两行的话,进程存活期间
        // 一切正常、服务一重启 context 就无声消失 —— 又一个只在特定时机
        // 出现的静默丢弃。纪律与入口一致:只在 active 时接受,超限即弃。
        if (
            state == "active"
            && StringValue(source?["context"]) is string context
            && context.Length <= 1200
            && !string.IsNullOrWhiteSpace(context)
        )
        {
            restored["context"] = context;
            if (
                StringValue(source?["contextSource"]) is string contextSource
                && contextSource.Length <= 40
                && !string.IsNullOrWhiteSpace(contextSource)
            )
            {
                restored["contextSource"] = contextSource;
            }
        }
        return restored;
    }

    private static JsonObject RestoreFocus(JsonObject? source)
    {
        string? state = StringValue(source?["state"]);
        if (state == "unknown")
        {
            return UnknownFocus(
                StringValue(source?["reason"])
                    ?? "snapshot-invalid-focus");
        }
        if (state is not ("active" or "cleared"))
        {
            return UnknownFocus("snapshot-invalid-focus");
        }
        string? kind = StringValue(source?["kind"]);
        if (
            !ValidFocusKind(kind)
            || source?["ref"] is not JsonObject reference
        )
        {
            return UnknownFocus("snapshot-invalid-focus");
        }
        try
        {
            return new JsonObject
            {
                ["state"] = state,
                ["kind"] = kind,
                ["ref"] = CopyFocusReference(kind!, reference),
                ["reason"] = StringValue(source?["reason"]),
            };
        }
        catch (DirectProtocolException)
        {
            return UnknownFocus("snapshot-invalid-focus");
        }
    }

    private static string? InferKind(string? file)
    {
        string extension = System.IO.Path.GetExtension(file ?? "");
        return extension.ToLowerInvariant() switch
        {
            ".pdf" => "pdf",
            ".epub" => "epub",
            ".html" or ".htm" or ".md" => "html",
            _ => null,
        };
    }

    private static JsonObject ClearedSelection(string reason) =>
        new()
        {
            ["state"] = "cleared",
            ["text"] = null,
            ["ref"] = null,
            ["reason"] = reason,
        };

    private static JsonObject UnknownSelection(string reason) =>
        new()
        {
            ["state"] = "unknown",
            ["text"] = null,
            ["ref"] = null,
            ["reason"] = reason,
        };

    private static JsonObject UnknownFocus(string reason) =>
        new()
        {
            ["state"] = "unknown",
            ["kind"] = null,
            ["ref"] = null,
            ["reason"] = reason,
        };

    private static JsonObject ActiveSelection(
        string text,
        JsonObject active,
        string? context = null,
        string? contextSource = null)
    {
        JsonObject selection = new()
        {
            ["state"] = "active",
            ["text"] = text,
            ["ref"] = new JsonObject
            {
                ["file"] = active["file"]?.DeepClone(),
                ["page"] = active["page"]?.DeepClone(),
            },
            ["reason"] = null,
        };
        // 上下文只在有的时候出现:缺席(旧版前端、journal 折叠路径)时不放一个
        // null 占位 —— 「没有上下文」和「上下文为空」是两句不同的话,
        // null 占位会把前者伪装成后者。
        if (context is not null)
        {
            selection["context"] = context;
            if (contextSource is not null)
            {
                selection["contextSource"] = contextSource;
            }
        }
        return selection;
    }

    private static bool SamePage(JsonObject left, JsonObject right) =>
        string.Equals(
            StringValue(left["file"]),
            StringValue(right["file"]),
            StringComparison.Ordinal)
        && JsonNode.DeepEquals(left["page"], right["page"]);

    // Same page by equivalence rather than by raw JSON shape.
    //
    // SamePage uses DeepEquals, so 12 and "12" read as different pages even
    // though the readers treat them as one. PageEquivalent already knows the
    // difference; these comparisons need it too.
    private static bool SamePageEquivalent(
        JsonObject left,
        JsonObject right) =>
        string.Equals(
            StringValue(left["file"]),
            StringValue(right["file"]),
            StringComparison.Ordinal)
        && PageEquivalent(left["page"], right["page"]);

    // Content and location updates are allowed to omit live-source metadata.
    // On the same canonical page, omission means "unchanged", not "clear".
    // Dropping the source here leaves a readable snapshot with no route for
    // highlighter, browser-control, or on-demand visual commands.
    private static void PreserveActiveReadingContinuity(
        JsonObject prior,
        JsonObject next,
        bool preserveHighlightSource)
    {
        if (!SamePageEquivalent(prior, next))
        {
            return;
        }
        string? priorSource = StringValue(
            prior["sourceInstanceId"]);
        string? nextSource = StringValue(
            next["sourceInstanceId"]);
        if (
            nextSource is not null
            && !string.Equals(
                nextSource,
                priorSource,
                StringComparison.Ordinal)
        )
        {
            return;
        }
        if (!HasViewBinding(next) && HasViewBinding(prior))
        {
            next["viewFile"] = prior["viewFile"]?.DeepClone();
            next["viewPage"] = prior["viewPage"]?.DeepClone();
        }
        if (nextSource is null && priorSource is not null)
        {
            next["sourceInstanceId"] = priorSource;
        }
        if (
            next["selectionRegions"] is null
            && prior["selectionRegions"] is JsonObject priorRegions
        )
        {
            next["selectionRegions"] = priorRegions.DeepClone();
        }
        if (
            preserveHighlightSource
            && next["highlightSource"] is null
            && prior["highlightSource"]
                is JsonObject priorHighlightSource
        )
        {
            next["highlightSource"] = priorHighlightSource.DeepClone();
        }
    }

    private static bool WebSourceDiffers(
        JsonObject stablePage,
        JsonObject activeReading)
    {
        if (
            !string.Equals(
                StringValue(activeReading["kind"]),
                "web",
                StringComparison.Ordinal)
            || StringValue(activeReading["sourceInstanceId"])
                is not string activeSource
        )
        {
            return false;
        }
        return !string.Equals(
            StringValue(stablePage["sourceInstanceId"]),
            activeSource,
            StringComparison.Ordinal);
    }

    // Whether this reading carries a usable volume view.
    //
    // Both halves must be present and well formed. A half-written binding is
    // worse than none: it would claim a mapping the other half cannot honour.
    private static bool HasViewBinding(JsonObject value) =>
        StringValue(value["viewFile"]) is string viewFile
        && viewFile.StartsWith("vbook:", StringComparison.Ordinal)
        && ValidPageIdentifier(value["viewPage"], allowNull: false);

    // Identity of an active reading, view binding included.
    //
    // Two readings on the same canonical page are still different readings if
    // one is being viewed through a volume and the other is not, or through a
    // different volume page. Comparing only the canonical page would call them
    // equal and the selection would survive a move it should not have.
    private static bool SameActiveReadingIdentity(
        JsonObject left,
        JsonObject right)
    {
        if (!SamePageEquivalent(left, right))
        {
            return false;
        }
        bool leftHasView = HasViewBinding(left);
        bool rightHasView = HasViewBinding(right);
        if (leftHasView != rightHasView)
        {
            return false;
        }
        return !leftHasView
            || (
                string.Equals(
                    StringValue(left["viewFile"]),
                    StringValue(right["viewFile"]),
                    StringComparison.Ordinal)
                && PageEquivalent(
                    left["viewPage"],
                    right["viewPage"])
            );
    }

    // Whether a reference names the same page as the canonical one, allowing
    // for the reference having been written in volume coordinates.
    //
    // A selection made while reading a merged volume records the volume's file
    // and page. Compared directly against the canonical member it never
    // matches, and the selection gets discarded on every context event. The
    // active reading holds the mapping between the two, so it is consulted
    // before declaring a mismatch.
    private static bool SameLogicalPage(
        JsonObject canonicalPage,
        JsonObject reference,
        JsonObject? activeReading)
    {
        if (SamePageEquivalent(canonicalPage, reference))
        {
            return true;
        }
        return activeReading is not null
            && HasViewBinding(activeReading)
            && SamePageEquivalent(canonicalPage, activeReading)
            && string.Equals(
                StringValue(activeReading["viewFile"]),
                StringValue(reference["file"]),
                StringComparison.Ordinal)
            && PageEquivalent(
                activeReading["viewPage"],
                reference["page"]);
    }

    private static string? StringValue(JsonNode? value)
    {
        if (value is null)
        {
            return null;
        }
        try
        {
            return value.GetValue<string>();
        }
        catch (InvalidOperationException)
        {
            return null;
        }
    }

    private static long? EpochSecondsToMilliseconds(JsonNode? value)
    {
        try
        {
            return checked(value?.GetValue<long>() * 1000);
        }
        catch (Exception exception) when (
            exception is InvalidOperationException or OverflowException
        )
        {
            return null;
        }
    }

    private static void ValidateRequestIdentity(
        string requestId,
        string sessionId)
    {
        if (
            !DirectBridgeContract.IsSafeId(requestId)
            || DirectPcmFrameCodec.ParseSessionId(sessionId).Length != 16
        )
        {
            throw new DirectProtocolException(
                "BW_READER_CONTEXT_SNAPSHOT_SCHEMA_INVALID",
                "Windows 本地 Reader 快照请求标识无效");
        }
    }

    private static DirectProtocolException ActiveReadingInvalid(
        Exception? inner = null) =>
        new(
            "BW_READER_ACTIVE_READING_SCHEMA_INVALID",
            "Reader active-reading 更新无效",
            retryable: false,
            innerException: inner);

    private static DirectProtocolException ViewportInvalid(
        Exception? inner = null) =>
        new(
            "BW_READER_VIEWPORT_SCHEMA_INVALID",
            "Reader 当前视口更新无效",
            retryable: false,
            innerException: inner);

    private static DirectProtocolException JournalInvalid() =>
        new(
            "BW_READER_CONTEXT_SNAPSHOT_EVENT_INVALID",
            "Reader outgoing context 无法折叠为本地快照");

    private static DirectProtocolException SnapshotWriteFailed(
        Exception? inner = null) =>
        new(
            "BW_READER_CONTEXT_SNAPSHOT_WRITE_FAILED",
            "Windows 本地 Reader 快照写入失败",
            retryable: true,
            innerException: inner);
}


// 通知投影(2026-08-25 用户拍板:通知与复习提醒放入快照,AI 读取快照即知
// 待办)。真值在 ReaderPC 的通知表;它每轮对账把 open 通知导出到 runtime
// 目录,这里在快照**读取投影**时合并 —— 快照真值文件本身不含通知,
// 两个真值各归各家,读的人看到合体。白名单重建;任何失败折成"无通知",
// 通知是增强,坏了不拦快照本体。
internal static class ReaderNotificationsProjection
{
    internal const string FileName = "notifications-open.json";
    private const int MaximumBytes = 256 * 1024;

    internal static void Apply(JsonObject snapshot, string directory)
    {
        try
        {
            snapshot.Remove("notifications");
            string path = System.IO.Path.Combine(directory, FileName);
            FileInfo info = new(path);
            if (!info.Exists || info.Length is <= 0 or > MaximumBytes)
            {
                return;
            }
            JsonObject? parsed = JsonNode.Parse(
                File.ReadAllText(path)) as JsonObject;
            if (
                parsed?["contract"]?.GetValue<string>()
                    != "reader-notifications/1"
                || parsed["items"] is not JsonArray items
            )
            {
                return;
            }
            JsonArray projected = new();
            foreach (JsonNode? node in items)
            {
                if (node is not JsonObject item)
                {
                    continue;
                }
                string? id = item["id"]?.GetValue<string>();
                string? title = item["title"]?.GetValue<string>();
                string? state = item["state"]?.GetValue<string>();
                if (
                    id is null || title is null
                    || state is not ("pending" or "acknowledged")
                )
                {
                    continue;
                }
                projected.Add(new JsonObject
                {
                    ["id"] = id,
                    ["kind"] = item["kind"]?.GetValue<string>() ?? "",
                    ["title"] = title,
                    ["body"] = item["body"]?.GetValue<string>() ?? "",
                    ["state"] = state,
                    ["createdAtUtcMs"] =
                        item["createdAtUtcMs"]?.GetValue<long>() ?? 0,
                });
                if (projected.Count >= 20)
                {
                    break;
                }
            }
            if (projected.Count > 0)
            {
                snapshot["notifications"] = projected;
            }
        }
        catch
        {
        }
    }
}


// 「最近操作」的账本化替代(2026-08-25 用户拍板:用记录功能的信息代替
// 快照里的 recentActions)。旧实现是内存态 ≤5 条/30s 窗,重启即空、
// 覆盖面窄;账本派生的最近条目落盘可靠、带条目号可追。留一个兜底:
// 近 30 分钟账本无条目时保留桥内原值(翻页/画笔那几类不进账本)。
internal static class ReaderRecentActivityProjection
{
    private const int WindowMinutes = 30;
    private const int MaximumItems = 8;
    private static readonly (string Method, string Url, string Label)[]
        Labels =
    {
        ("POST", "/pdf/api/highlights", "新建高亮"),
        ("PATCH", "/pdf/api/highlights", "修改高亮"),
        ("DELETE", "/pdf/api/highlights", "删除高亮"),
        ("POST", "/pdf/api/epub-highlights", "新建高亮"),
        ("PATCH", "/pdf/api/epub-highlights", "修改高亮"),
        ("DELETE", "/pdf/api/epub-highlights", "删除高亮"),
        ("POST", "/pdf/api/notes", "新建便签/卡片"),
        ("PATCH", "/pdf/api/notes", "修改便签/卡片"),
        ("DELETE", "/pdf/api/notes", "删除便签/卡片"),
        ("POST", "/pdf/api/userpages", "新建用户页"),
        ("PATCH", "/pdf/api/userpages", "修改用户页"),
        ("DELETE", "/pdf/api/userpages", "删除用户页"),
        ("POST", "/pdf/api/ink", "手写墨迹"),
        ("POST", "/pdf/api/epub-ink", "手写墨迹"),
        ("POST", "/replication/activity", "阅读/复习活动"),
    };

    internal static void Apply(JsonObject snapshot, string directory)
    {
        try
        {
            string path = System.IO.Path.Combine(
                directory, "activity-report.json");
            FileInfo info = new(path);
            if (!info.Exists || info.Length is <= 0 or > 2 * 1024 * 1024)
            {
                return;
            }
            JsonObject? report = JsonNode.Parse(
                File.ReadAllText(path)) as JsonObject;
            if (report?["raw"]?["commands"] is not JsonArray commands)
            {
                return;
            }
            long cutoff = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()
                - WindowMinutes * 60_000L;
            JsonArray projected = new();
            foreach (JsonNode? node in commands)
            {
                if (node is not JsonObject command)
                {
                    continue;
                }
                long at = command["atUtcMs"]?.GetValue<long>() ?? 0;
                if (at < cutoff)
                {
                    continue;
                }
                string method = command["method"]?.GetValue<string>() ?? "";
                string url = command["url"]?.GetValue<string>() ?? "";
                string? label = null;
                foreach ((string m, string u, string l) in Labels)
                {
                    if (m == method && u == url)
                    {
                        label = l;
                        break;
                    }
                }
                if (label is null)
                {
                    continue;
                }
                projected.Add(new JsonObject
                {
                    ["kind"] = label,
                    ["atMs"] = at,
                    ["source"] = "ledger",
                });
                if (projected.Count >= MaximumItems)
                {
                    break;
                }
            }
            if (projected.Count > 0)
            {
                snapshot["recentActions"] = projected;
            }
        }
        catch
        {
        }
    }
}


// 「当前位置」投影(2026-08-25 用户:位置信息现在就该进快照 —— AI 据此
// 判断通知该不该提醒:在公司就别提倒垃圾)。真值由 ReaderPC 从最近的
// 带坐标 dwell(≤30 分钟)派生,别名(place-aliases)优先于反解地名。
// 缺席 = 不知道在哪 —— 旧位置冒充当前比不知道更糟。
internal static class ReaderCurrentPlaceProjection
{
    internal const string FileName = "current-place.json";

    internal static void Apply(JsonObject snapshot, string directory)
    {
        try
        {
            snapshot.Remove("currentPlace");
            string path = System.IO.Path.Combine(directory, FileName);
            FileInfo info = new(path);
            if (!info.Exists || info.Length is <= 0 or > 16 * 1024)
            {
                return;
            }
            JsonObject? parsed = JsonNode.Parse(
                File.ReadAllText(path)) as JsonObject;
            if (
                parsed?["contract"]?.GetValue<string>()
                    != "reader-current-place/1"
            )
            {
                return;
            }
            long observed =
                parsed["observedAtUtcMs"]?.GetValue<long>() ?? 0;
            long age = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()
                - observed;
            if (age is < 0 or > 30 * 60_000L)
            {
                return;
            }
            string? alias = parsed["alias"]?.GetValue<string>();
            // ⚠ 快照只放**登记过的名字**（2026-08-27 用户拍板）：不放坐标、
            // 不放未登记的地理名。快照是判断用的整理视图，不是位置数据源 ——
            // 真机实锤 AI 为了拿一个地址去读整个实时快照（还连败两次），
            // 而正确来源本地 replication_places CLI 一直都在。这里少给，
            // 它就不会再把快照当位置接口。
            snapshot["currentPlace"] = new JsonObject
            {
                ["name"] = alias,
                ["named"] = alias is not null,
                ["ageMinutes"] = age / 60_000L,
            };
        }
        catch
        {
        }
    }
}
