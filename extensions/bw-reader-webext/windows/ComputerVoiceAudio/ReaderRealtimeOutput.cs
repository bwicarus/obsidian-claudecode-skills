using System.Net;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace BwReader.ComputerVoiceAudio;

internal sealed record ReaderRealtimeOutputRequest(
    string Correlation,
    string SourceInstanceId,
    long SnapshotRevision,
    string File,
    JsonNode Page,
    string Kind,
    JsonNode Payload);

internal sealed record ReaderRealtimeOutputAck(
    string SessionId,
    string Correlation,
    string SourceInstanceId,
    string Outcome,
    string? Error,
    // 卡片钉在正文上没有、没钉上是为什么。
    // ⚠ 不能复用 Error：下面那条不变式要求 Error 当且仅当 outcome=='rejected'
    //   时存在，而「退回浮层」是 applied —— 卡确实送到了，只是没钉上。
    //   这个 record 是**容器闸**：不给它开槽，前后两处 new 就无处可搬。
    string? BindOutcome,
    string? BindReason);

internal sealed class ReaderRealtimeOutputException : Exception
{
    internal ReaderRealtimeOutputException(
        string code,
        string message,
        bool retryable,
        Exception? innerException = null)
        : base(message, innerException)
    {
        Code = code;
        Retryable = retryable;
    }

    internal string Code { get; }
    internal bool Retryable { get; }
}

internal static class ReaderRealtimeOutputProtocol
{
    internal const string OutputContract = "reader-realtime-output/1";
    internal const string EventName = "reader-realtime-output";
    internal const string AckType = "reader-realtime-output-ack";
    internal const int MaximumTextCharacters = 8_000;
    internal const int MaximumPayloadBytes = 32 * 1024;
    internal const int MaximumPageCardContentCharacters = 100_000;

    internal static bool IsKind(string value) => value is
        "assistant-turn"
        or "tool-status"
        or "card"
        or "navigate"
        or "highlight"
        or "highlight-text"
        or "highlight-range"
        or "anki-draft"
        or "client-action";

    // Only mutations with a stable replay identity and a document-independent
    // receiver may enter the durable outbox. A bound result card uses
    // correlation as its stable uid; learning-card edits carry their own
    // validated mutationId. Page-card edits still depend on the live page
    // projection/number revision, so they remain live-only until their payload
    // carries an independently addressable target page. Navigation,
    // highlights, free notes and ordinary floating cards also remain live-only
    // because replaying them after an unknown result could duplicate or target
    // stale content.
    internal static bool IsDurableMutation(
        ReaderRealtimeOutputRequest request)
    {
        if (IsPageCharsCardMutation(request))
        {
            return true;
        }
        if (request.Kind != "client-action"
            || request.Payload is not JsonObject action
            || action["fn"]?.GetValue<string>() is not string fn
            || fn != "_nativeReaderLearningCardMutate"
            || action["args"] is not JsonArray { Count: 1 } args
            || args[0] is not JsonObject mutation
            || mutation["mutationId"]?.GetValue<string>() is not string id)
        {
            return false;
        }
        return DirectBridgeContract.IsSafeId(id);
    }

    internal static bool IsPageCharsCardMutation(
        ReaderRealtimeOutputRequest request) =>
        request.Kind == "card"
            && request.Payload is JsonObject cardPayload
            && cardPayload["card"] is JsonObject card
            && card["bind"] is JsonObject bind
            && string.Equals(
                bind["kind"]?.GetValue<string>(),
                "page-chars",
                StringComparison.Ordinal);

    internal static object Event(ReaderRealtimeOutputRequest request) =>
        new
        {
            contract = DirectBridgeContract.Contract,
            type = "event",
            @event = EventName,
            payload = new
            {
                contract = OutputContract,
                commandKind = "realtime-output",
                correlation = request.Correlation,
                sourceInstanceId = request.SourceInstanceId,
                snapshotRevision = request.SnapshotRevision,
                file = request.File,
                page = request.Page,
                kind = request.Kind,
                payload = request.Payload,
            },
        };

    internal static ReaderRealtimeOutputRequest Create(
        string correlation,
        string sourceInstanceId,
        long snapshotRevision,
        string file,
        JsonNode page,
        string kind,
        JsonNode payload)
    {
        if (
            !DirectBridgeContract.IsSafeId(correlation)
            || !DirectBridgeContract.IsSafeId(sourceInstanceId)
            || snapshotRevision < 0
            || string.IsNullOrWhiteSpace(file)
            || file.Length > 4096
            || ContainsC0OrC1(file)
            || !IsKind(kind)
        )
        {
            throw Invalid("Reader 输出身份或类型无效");
        }
        using JsonDocument pageDocument = JsonDocument.Parse(
            page.ToJsonString());
        JsonElement pageElement = pageDocument.RootElement;
        if (!ReaderVisualDeliveryProtocol.PageEquivalent(page, pageElement))
        {
            throw Invalid("Reader 输出页身份无效");
        }
        JsonNode normalized = ValidatePayload(kind, payload);
        return new ReaderRealtimeOutputRequest(
            correlation,
            sourceInstanceId,
            snapshotRevision,
            file,
            page.DeepClone(),
            kind,
            normalized);
    }

    internal static JsonNode ValidatePayload(string kind, JsonNode payload)
    {
        if (!IsKind(kind) || payload is not JsonObject)
        {
            throw Invalid("Reader 输出 payload 无效");
        }
        byte[] bytes = Encoding.UTF8.GetBytes(
            payload.ToJsonString(DirectBridgeContract.JsonOptions));
        int maximumPayloadBytes = IsPageCardMutation(kind, payload)
            ? DirectBridgeContract.MaximumMessageBytes
            : MaximumPayloadBytes;
        if (bytes.Length > maximumPayloadBytes)
        {
            throw Invalid("Reader 输出 payload 超过大小上限");
        }
        using JsonDocument document = JsonDocument.Parse(
            bytes,
            new JsonDocumentOptions
            {
                AllowTrailingCommas = false,
                CommentHandling = JsonCommentHandling.Disallow,
                MaxDepth = 12,
            });
        JsonElement root = document.RootElement;
        DirectJsonValidation.RequireNoDuplicateKeys(root);
        switch (kind)
        {
            case "assistant-turn":
                Exact(root, "threadId", "user", "assistant");
                NullableSafeId(root, "threadId");
                Text(root, "user", MaximumTextCharacters);
                Text(root, "assistant", MaximumTextCharacters);
                break;
            case "tool-status":
                Exact(root, "status", "tool", "label", "detail");
                string status = Text(root, "status", 16);
                if (status is not ("running" or "done" or "error" or "aborted"))
                {
                    throw Invalid("Reader 工具状态无效");
                }
                Text(root, "tool", 160);
                Text(root, "label", 320);
                NullableText(root, "detail", 6_000);
                break;
            case "card":
                Exact(root, "card");
                ValidateCard(root.GetProperty("card"));
                break;
            case "navigate":
                Exact(root, "action", "target", "selectionId");
                ValidateNavigation(root);
                break;
            case "highlight":
                Exact(root, "color", "note");
                string color = Text(root, "color", 16);
                if (color is not ("yellow" or "green" or "blue" or "pink"))
                {
                    throw Invalid("Reader 高亮颜色无效");
                }
                NullableText(root, "note", 2_000);
                break;
            case "highlight-text":
                Exact(
                    root,
                    "mutationId",
                    "file",
                    "target",
                    "text",
                    "color",
                    "note");
                ValidateClientMutationId(root, "mutationId");
                FileText(root, "file", 4_096);
                ValidateDocumentTarget(root.GetProperty("target"));
                Text(root, "text", 2_000);
                string exactColor = Text(root, "color", 16);
                if (exactColor is not (
                    "yellow" or "green" or "blue" or "pink"))
                {
                    throw Invalid("Reader 高亮颜色无效");
                }
                NullableText(root, "note", 2_000);
                break;
            case "highlight-range":
                Exact(
                    root,
                    "mutationId",
                    "rangeRef",
                    "color",
                    "note");
                ValidateClientMutationId(root, "mutationId");
                ValidateHighlightRangeReference(
                    root.GetProperty("rangeRef"));
                string rangeColor = Text(root, "color", 16);
                if (rangeColor is not (
                    "yellow" or "green" or "blue" or "pink"))
                {
                    throw Invalid("Reader 高亮颜色无效");
                }
                NullableText(root, "note", 2_000);
                break;
            case "anki-draft":
                bool hasFile = root.TryGetProperty("file", out _);
                bool hasTarget = root.TryGetProperty("target", out _);
                bool hasSourceText = root.TryGetProperty(
                    "sourceText",
                    out _);
                bool exactSource = hasFile && hasTarget && hasSourceText;
                if ((hasFile || hasTarget || hasSourceText) && !exactSource)
                {
                    throw Invalid(
                        "Reader Anki 引用来源必须同时提供 file/target/sourceText");
                }
                if (exactSource)
                {
                    Exact(
                        root,
                        "draftId",
                        "file",
                        "target",
                        "sourceText",
                        "cards");
                }
                else
                {
                    Exact(root, "draftId", "cards");
                }
                ValidateAnkiDraftId(root, "draftId");
                if (exactSource)
                {
                    FileText(root, "file", 4_096);
                    ValidateDocumentTarget(root.GetProperty("target"));
                    Text(root, "sourceText", 2_000);
                }
                ValidateAnkiDraftCards(root.GetProperty("cards"));
                break;
            case "client-action":
                Exact(root, "fn", "args");
                ValidateClientAction(root);
                break;
            default:
                throw Invalid("Reader 输出类型无效");
        }
        return JsonNode.Parse(root.GetRawText())
            ?? throw Invalid("Reader 输出 payload 无效");
    }

    private static void ValidateHighlightRangeReference(JsonElement value)
    {
        Exact(
            value,
            "contract",
            "snapshotId",
            "documentId",
            "target",
            "sourceDigest",
            "revision",
            "startMarker",
            "endMarker");
        if (Text(value, "contract", 64) != "reader-source-range/1")
        {
            throw Invalid("Reader 范围高亮合同无效");
        }
        string snapshotId = SafeId(value, "snapshotId");
        if (!IsHighlightSourceSnapshotId(snapshotId))
        {
            throw Invalid("Reader 高亮来源 snapshotId 无效");
        }
        FileText(value, "documentId", 4_096);
        ValidateDocumentTarget(value.GetProperty("target"));
        string digest = Text(value, "sourceDigest", 30);
        if (!IsHighlightSourceDigest(digest))
        {
            throw Invalid("Reader 高亮来源摘要无效");
        }
        string revision = Text(value, "revision", 160);
        if (
            revision.Any(character =>
                char.IsControl(character)
                || char.IsWhiteSpace(character))
        )
        {
            throw Invalid("Reader 高亮来源 revision 无效");
        }
        string start = SafeId(value, "startMarker");
        string end = SafeId(value, "endMarker");
        if (
            !IsHighlightSourceMarker(start)
            || !IsHighlightSourceMarker(end)
            || string.Equals(start, end, StringComparison.Ordinal)
        )
        {
            throw Invalid("Reader 范围高亮不能为空");
        }
    }

    internal static bool IsHighlightSourceSnapshotId(string value) =>
        value.Length == 28
        && value.StartsWith("hrs_", StringComparison.Ordinal)
        && value[4..].All(character => character is
            >= '0' and <= '9' or >= 'a' and <= 'f');

    internal static bool IsHighlightSourceDigest(string value) =>
        value.Length == 30
        && value.StartsWith("rsd1_", StringComparison.Ordinal)
        && value[13] == '_'
        && value[5..13].All(character => character is
            >= '0' and <= '9' or >= 'a' and <= 'f')
        && value[14..].All(character => character is
            >= '0' and <= '9' or >= 'a' and <= 'f');

    internal static bool IsHighlightSourceMarker(string value) =>
        value.Length is >= 3 and <= 6
        && value.StartsWith("m_", StringComparison.Ordinal)
        && value[2..].All(character => character is
            >= '0' and <= '9' or >= 'a' and <= 'z');

    // 桥接以这种输出承载 Reader 本机的受信语义入口。助手高亮的卡片条由本地落库
    // 成功分支直接产生，不走跨进程动态函数通道。这里只允许名单内的入口，且每个
    // 入口的参数各自校验；normalizer 与执行侧还会分别再卡一次，任何时候都不能
    // 把任意 window 函数暴露给桥接。
    private static void ValidateClientAction(JsonElement root)
    {
        string fn = Text(root, "fn", 64);
        if (fn is not (
            "_nativeReaderUndoLast"
            or "_nativeReaderPageCardMutate"
            or "_nativeReaderLearningCardMutate"
            or "_nativeReaderCreateNote"
            or "_nativeReaderEditNote"
            or "_nativeReaderMakeNote"
            or "_nativeReaderMarkVocabulary"
            or "_bwWebHighlightByText"
            or "_bwWebNoteCreate"
            or "__upStartTask"))
        {
            throw Invalid("Reader 客户端动作不在白名单内");
        }
        JsonElement args = root.GetProperty("args");
        if (args.ValueKind != JsonValueKind.Array)
        {
            throw Invalid("Reader 客户端动作参数必须是数组");
        }
        if (fn is "__upStartTask")
        {
            // 交互练习纸:一个 spec 对象遥控 PDF 阅读器起 free 任务纸(乐观建页 →
            // run-start → 服务端布局)。内容级容错单源在 Pi 的任务链;这里只卡
            // 结构与上限,坚持"名单内入口 + 逐入口校验",不放任意字段过桥。
            if (args.GetArrayLength() != 1
                || args[0].ValueKind != JsonValueKind.Object)
            {
                throw Invalid("Reader 练习纸需要一个任务对象");
            }
            JsonElement paperSpec = args[0];
            DirectJsonValidation.RequireNoDuplicateKeys(paperSpec);
            Exact(paperSpec, "kind", "title", "paper", "params");
            if (Text(paperSpec, "kind", 16) != "free")
            {
                throw Invalid("Reader 练习纸只允许 free 任务");
            }
            _ = Text(paperSpec, "title", 120);
            _ = Text(paperSpec, "paper", 16);
            JsonElement paperParams = paperSpec.GetProperty("params");
            if (paperParams.ValueKind != JsonValueKind.Object)
            {
                throw Invalid("Reader 练习纸参数必须是对象");
            }
            DirectJsonValidation.RequireNoDuplicateKeys(paperParams);
            Exact(paperParams, "blocks", "paper", "title");
            JsonElement paperBlocks = paperParams.GetProperty("blocks");
            if (paperBlocks.ValueKind != JsonValueKind.Array
                || paperBlocks.GetArrayLength() < 1
                || paperBlocks.GetArrayLength() > 48)
            {
                throw Invalid("Reader 练习纸元素必须是 1..48 个");
            }
            foreach (JsonElement paperBlock in paperBlocks.EnumerateArray())
            {
                if (paperBlock.ValueKind != JsonValueKind.Object)
                {
                    throw Invalid("Reader 练习纸元素必须是对象");
                }
            }
            return;
        }
        if (fn is "_nativeReaderPageCardMutate")
        {
            if (args.GetArrayLength() != 1
                || args[0].ValueKind != JsonValueKind.Object)
            {
                throw Invalid("Reader 页面卡片修改需要一个对象");
            }
            JsonElement mutation = args[0];
            DirectJsonValidation.RequireNoDuplicateKeys(mutation);
            string operation = Text(mutation, "operation", 16);
            bool hasNumber = mutation.TryGetProperty(
                "number",
                out JsonElement numberValue);
            if (operation == "edit")
            {
                if (hasNumber)
                {
                    Exact(
                        mutation,
                        "operation",
                        "operationId",
                        "number",
                        "expectedId",
                        "expectedRevision",
                        "replacement");
                }
                else
                {
                    Exact(
                        mutation,
                        "operation",
                        "operationId",
                        "expectedId",
                        "expectedRevision",
                        "replacement");
                }
            }
            else if (operation == "delete")
            {
                if (hasNumber)
                {
                    Exact(
                        mutation,
                        "operation",
                        "operationId",
                        "number",
                        "expectedId",
                        "expectedRevision");
                }
                else
                {
                    Exact(
                        mutation,
                        "operation",
                        "operationId",
                        "expectedId",
                        "expectedRevision");
                }
            }
            else
            {
                throw Invalid("Reader 页面卡片操作无效");
            }
            string pageCardOperationId = Text(mutation, "operationId", 30);
            const string operationPrefix = "pcard_";
            if (pageCardOperationId.Length != operationPrefix.Length + 24
                || !pageCardOperationId.StartsWith(
                    operationPrefix,
                    StringComparison.Ordinal)
                || pageCardOperationId[operationPrefix.Length..].Any(character =>
                    character is not (
                        >= '0' and <= '9' or >= 'a' and <= 'f')))
            {
                throw Invalid("Reader 页面卡片操作编号无效");
            }
            if (hasNumber
                && (numberValue.ValueKind != JsonValueKind.Number
                    || !numberValue.TryGetInt32(out int number)
                    || number < 1))
            {
                throw Invalid("Reader 页面卡片当前序号无效");
            }
            string expectedId = Text(mutation, "expectedId", 96);
            if (expectedId.Length < 2
                || expectedId.Any(character => character is not (
                    >= 'A' and <= 'Z'
                    or >= 'a' and <= 'z'
                    or >= '0' and <= '9'
                    or '_' or '-')))
            {
                throw Invalid("Reader 页面卡片稳定编号无效");
            }
            if (!mutation.TryGetProperty(
                    "expectedRevision",
                    out JsonElement revisionValue)
                || revisionValue.ValueKind != JsonValueKind.Number
                || !revisionValue.TryGetInt64(out long expectedRevision)
                || expectedRevision < 0
                || expectedRevision > 9_007_199_254_740_991L)
            {
                throw Invalid("Reader 页面卡片版本无效");
            }
            if (operation == "edit")
            {
                JsonElement replacement = mutation.GetProperty("replacement");
                if (replacement.ValueKind != JsonValueKind.Object)
                {
                    throw Invalid("Reader 页面卡片替换内容无效");
                }
                DirectJsonValidation.RequireNoDuplicateKeys(replacement);
                bool hasContent = replacement.TryGetProperty("content", out _);
                bool hasCards = replacement.TryGetProperty("cards", out _);
                if (hasContent == hasCards)
                {
                    throw Invalid("Reader 页面卡片只能选择一种替换内容");
                }
                if (hasContent)
                {
                    Exact(replacement, "content");
                    string content = Text(
                        replacement,
                        "content",
                        MaximumPageCardContentCharacters);
                    if (string.IsNullOrWhiteSpace(content))
                    {
                        throw Invalid("Reader 页面卡片内容为空");
                    }
                }
                else
                {
                    Exact(replacement, "cards");
                    ValidatePageCardReplacementCards(
                        replacement.GetProperty("cards"));
                }
            }
            return;
        }
        if (fn is "_nativeReaderLearningCardMutate")
        {
            if (args.GetArrayLength() != 1
                || args[0].ValueKind != JsonValueKind.Object)
            {
                throw Invalid("Reader 学习卡修改需要一个对象");
            }
            JsonElement mutation = args[0];
            DirectJsonValidation.RequireNoDuplicateKeys(mutation);
            string operation = Text(mutation, "operation", 16);
            if (operation == "edit")
            {
                ExactWithOptional(
                    mutation,
                    [
                        "operation",
                        "mutationId",
                        "id",
                        "cardIndex",
                        "expectedEntityRev",
                        "externalPolicy",
                    ],
                    ["card", "source"]);
            }
            else if (operation == "delete")
            {
                Exact(
                    mutation,
                    "operation",
                    "mutationId",
                    "id",
                    "cardIndex",
                    "expectedStateRev",
                    "externalPolicy");
            }
            else
            {
                throw Invalid("Reader 学习卡操作无效");
            }
            string mutationId = Text(mutation, "mutationId", 32);
            if (mutationId.Length != 30
                || !mutationId.StartsWith("lcard_", StringComparison.Ordinal)
                || mutationId[6..].Any(character => character is not (
                    >= '0' and <= '9' or >= 'a' and <= 'f')))
            {
                throw Invalid("Reader 学习卡 mutationId 无效");
            }
            string cardId = Text(mutation, "id", 69);
            if (cardId.Length is < 9 or > 69
                || !cardId.StartsWith("card_", StringComparison.Ordinal)
                || cardId[5..].Any(character => character is not (
                    >= '0' and <= '9' or >= 'a' and <= 'f')))
            {
                throw Invalid("Reader 学习卡 id 无效");
            }
            if (!mutation.TryGetProperty(
                    "cardIndex",
                    out JsonElement indexValue)
                || indexValue.ValueKind != JsonValueKind.Number
                || !indexValue.TryGetInt32(out int cardIndex)
                || cardIndex is < 0 or > 255)
            {
                throw Invalid("Reader 学习卡 cardIndex 无效");
            }
            string externalPolicy = Text(
                mutation,
                "externalPolicy",
                32);
            if (externalPolicy is not ("sync-if-projected" or "reader-only"))
            {
                throw Invalid("Reader 学习卡外部策略无效");
            }
            string revisionName = operation == "edit"
                ? "expectedEntityRev"
                : "expectedStateRev";
            if (!mutation.TryGetProperty(
                    revisionName,
                    out JsonElement revisionValue)
                || revisionValue.ValueKind != JsonValueKind.Number
                || !revisionValue.TryGetInt64(out long revision)
                || revision is < 0 or > 9_007_199_254_740_991L)
            {
                throw Invalid("Reader 学习卡版本无效");
            }
            JsonElement card = default;
            JsonElement source = default;
            bool hasCard = operation == "edit"
                && mutation.TryGetProperty("card", out card);
            bool hasSource = operation == "edit"
                && mutation.TryGetProperty("source", out source);
            if (operation == "edit"
                && (!hasCard && !hasSource
                    || hasCard
                        && !ReaderContextMcpServer.ValidateLearningCardContent(
                            card)
                    || hasSource
                        && !ReaderContextMcpServer.ValidateLearningCardSource(
                            source)))
            {
                throw Invalid("Reader 学习卡内容或出处无效");
            }
            return;
        }
        if (fn is "_bwWebNoteCreate")
        {
            // 只给内容。位置由页面用与用户点「新建便签」相同的落点逻辑决定 ——
            // 助手没有指针，让它传坐标就是把它没有的事实写进协议。
            if (args.GetArrayLength() != 1
                || args[0].ValueKind != JsonValueKind.Object)
            {
                throw Invalid("Reader 网页便签需要一个内容对象");
            }
            JsonElement webNote = args[0];
            DirectJsonValidation.RequireNoDuplicateKeys(webNote);
            Exact(webNote, "text");
            string webNoteText = Text(webNote, "text", 4_000);
            if (string.IsNullOrWhiteSpace(webNoteText))
            {
                throw Invalid("Reader 网页便签内容为空");
            }
            return;
        }
        if (fn is "_bwWebHighlightByText")
        {
            // 只给文字与上下文，不给坐标：助手看到的是快照里的正文，
            // 它指得出"哪一句"，指不出 DOM 位置。页面自己用 exact +
            // prefix/suffix 打分定位，重复句子才能选对那一处。
            if (args.GetArrayLength() != 1
                || args[0].ValueKind != JsonValueKind.Object)
            {
                throw Invalid("Reader 网页高亮需要一个对象");
            }
            JsonElement web = args[0];
            DirectJsonValidation.RequireNoDuplicateKeys(web);
            Exact(web, "exact", "prefix", "suffix", "color", "note");
            string webExact = Text(web, "exact", 2_000);
            if (string.IsNullOrWhiteSpace(webExact))
            {
                throw Invalid("Reader 网页高亮文字为空");
            }
            // 这四个允许为空：上下文可以没有（短句不需要消歧），
            // 颜色与备注本就是可选。默认的 Text() 会把空串当无效，
            // 那样助手每次都得编一个 prefix 才能画高亮。
            _ = Text(web, "prefix", 200, allowEmpty: true);
            _ = Text(web, "suffix", 200, allowEmpty: true);
            _ = Text(web, "color", 32, allowEmpty: true);
            _ = Text(web, "note", 2_000, allowEmpty: true);
            return;
        }
        if (fn is "_nativeReaderMarkVocabulary")
        {
            if (args.GetArrayLength() != 1
                || args[0].ValueKind != JsonValueKind.Object)
            {
                throw Invalid("Reader 生词标记需要一个对象");
            }
            JsonElement vocabulary = args[0];
            DirectJsonValidation.RequireNoDuplicateKeys(vocabulary);
            Exact(vocabulary, "word", "mark");
            string vocabularyWord = Text(vocabulary, "word", 128);
            if (string.IsNullOrWhiteSpace(vocabularyWord))
            {
                throw Invalid("Reader 生词为空");
            }
            string vocabularyMark = Text(vocabulary, "mark", 16);
            if (vocabularyMark is not ("known" or "unknown"))
            {
                throw Invalid("Reader 生词标记取值无效");
            }
            return;
        }
        if (fn is "_nativeReaderMakeNote")
        {
            // 标题可选：给了就用，没给由 App 按书名和时间生成。让桥接编一个，
            // 出来的会是它以为用户在读的那本书。
            if (args.GetArrayLength() != 1
                || args[0].ValueKind != JsonValueKind.Object)
            {
                throw Invalid("Reader 笔记需要一个内容对象");
            }
            JsonElement made = args[0];
            DirectJsonValidation.RequireNoDuplicateKeys(made);
            Exact(made, "title", "text");
            string madeText = Text(made, "text", 240_000);
            if (string.IsNullOrWhiteSpace(madeText))
            {
                throw Invalid("Reader 笔记内容为空");
            }
            if (made.GetProperty("title").ValueKind != JsonValueKind.String
                || (made.GetProperty("title").GetString() ?? string.Empty)
                    .Length > 240)
            {
                throw Invalid("Reader 笔记标题无效");
            }
            return;
        }
        if (fn is "_nativeReaderEditNote")
        {
            // 只改内容。位置、颜色、尺寸是用户对页面的布置，助手改写一段文字
            // 不该顺手把便签挪走 —— 所以协议里根本没有这些字段可填。
            if (args.GetArrayLength() != 1
                || args[0].ValueKind != JsonValueKind.Object)
            {
                throw Invalid("Reader 便签修改需要一个对象");
            }
            JsonElement edit = args[0];
            DirectJsonValidation.RequireNoDuplicateKeys(edit);
            Exact(edit, "id", "text");
            string editId = Text(edit, "id", 64);
            if (editId.Length == 0
                || editId.Any(character =>
                    character is not (
                        >= '0' and <= '9'
                        or >= 'a' and <= 'z'
                        or >= 'A' and <= 'Z'
                        or '_' or '-')))
            {
                throw Invalid("Reader 便签编号无效");
            }
            string editText = Text(edit, "text", 4_000);
            if (string.IsNullOrWhiteSpace(editText))
            {
                throw Invalid("Reader 便签内容为空");
            }
            return;
        }
        if (fn is "_nativeReaderCreateNote")
        {
            // 只传内容。位置由 App 用受信的当前界面与页码自己填 ——
            // 桥接不知道此刻在哪一页，让它给坐标等于把它没有的事实写进协议。
            if (args.GetArrayLength() != 1
                || args[0].ValueKind != JsonValueKind.Object)
            {
                throw Invalid("Reader 便签需要一个内容对象");
            }
            JsonElement note = args[0];
            DirectJsonValidation.RequireNoDuplicateKeys(note);
            Exact(note, "text");
            string noteText = Text(note, "text", 4_000);
            if (string.IsNullOrWhiteSpace(noteText))
            {
                throw Invalid("Reader 便签内容为空");
            }
            return;
        }
        if (args.GetArrayLength() != 1
            || args[0].ValueKind != JsonValueKind.String)
        {
            throw Invalid("Reader 撤销需要一个操作编号");
        }
        string operationId = args[0].GetString() ?? string.Empty;
        const string prefix = "rundo_";
        if (operationId.Length != prefix.Length + 24
            || !operationId.StartsWith(prefix, StringComparison.Ordinal)
            || operationId[prefix.Length..].Any(character =>
                character is not (>= '0' and <= '9' or >= 'a' and <= 'f')))
        {
            throw Invalid("Reader 撤销操作编号无效");
        }
    }

    internal static ReaderRealtimeOutputAck ValidateAck(JsonElement message)
    {
        // Exact 是 SetEquals：多一个少一个都拒。bindOutcome/bindReason 是可选的，
        // 所以走 ExactWithOptional（同一个类里已有，2026-08-19 为 card.bind 加的）。
        ExactWithOptional(
            message,
            new[]
            {
                "contract",
                "type",
                "requestId",
                "sessionId",
                "correlation",
                "sourceInstanceId",
                "outcome",
                "error",
            },
            new[] { "bindOutcome", "bindReason" });
        if (
            Text(message, "contract", 128) != DirectBridgeContract.Contract
            || Text(message, "type", 64) != AckType
        )
        {
            throw Invalid("Reader 输出回执合同无效");
        }
        string sessionId = SafeId(message, "sessionId");
        _ = DirectPcmFrameCodec.ParseSessionId(sessionId);
        string correlation = SafeId(message, "correlation");
        string sourceInstanceId = SafeId(message, "sourceInstanceId");
        string outcome = Text(message, "outcome", 16);
        if (outcome is not ("applied" or "replay" or "rejected"))
        {
            throw Invalid("Reader 输出回执 outcome 无效");
        }
        string? error = NullableText(message, "error", 500);
        if ((outcome == "rejected") != (error is not null))
        {
            throw Invalid("Reader 输出拒绝回执必须且只能携带 error");
        }
        // 过闸之后立刻重建成 record —— JsonElement 上的其它字段在这一行之后
        // 就不存在了。放行不等于搬运，这一处必须显式取。
        string? bindOutcome = message.TryGetProperty("bindOutcome", out _)
            ? NullableText(message, "bindOutcome", 32)
            : null;
        if (bindOutcome is not (null or "none" or "bound" or "floating" or "unknown"))
        {
            throw Invalid("Reader 输出回执 bindOutcome 无效");
        }
        string? bindReason = message.TryGetProperty("bindReason", out _)
            ? NullableText(message, "bindReason", 120)
            : null;
        return new ReaderRealtimeOutputAck(
            sessionId,
            correlation,
            sourceInstanceId,
            outcome,
            error,
            bindOutcome,
            bindReason);
    }

    private static void ValidateCard(JsonElement card)
    {
        // bind 是**可选**的，所以不能用 Exact（它是 SetEquals 全等，多一个字段就拒）。
        // 2026-08-19：此前正是这一道把 AI 传来的 bind 挡在外面 —— 就算 schema 放行了，
        // 到这里仍会被判"字段不匹配"。
        ExactWithOptional(card, new[] { "kind", "title", "data" }, new[] { "bind" });
        if (card.TryGetProperty("bind", out JsonElement bind)
            && bind.ValueKind != JsonValueKind.Null)
        {
            ValidateCardBind(bind);
        }
        string kind = Text(card, "kind", 32);
        if (kind is not (
            "weather" or "news" or "images" or "videos" or "fact" or
            "general"))
        {
            throw Invalid("Reader 卡片类型无效");
        }
        NullableText(card, "title", 320);
        JsonElement data = card.GetProperty("data");
        if (data.ValueKind != JsonValueKind.Object)
        {
            throw Invalid("Reader 卡片 data 无效");
        }
        switch (kind)
        {
            case "weather":
                Shape(
                    data,
                    ["lo", "hi", "cond"],
                    ["loc", "date", "precip", "tip"]);
                CardScalar(data, "lo");
                CardScalar(data, "hi");
                CardText(data, "cond");
                OptionalCardText(data, "loc");
                OptionalCardText(data, "date");
                OptionalCardScalar(data, "precip");
                OptionalCardText(data, "tip");
                break;
            case "news":
                CardItems(
                    data,
                    ["t"],
                    ["s", "src"],
                    static item =>
                    {
                        CardText(item, "t");
                        OptionalCardText(item, "s");
                        OptionalCardText(item, "src");
                    });
                break;
            case "images":
                CardItems(
                    data,
                    ["url"],
                    ["title", "aid", "src"],
                    static item =>
                    {
                        CardUrl(item, "url");
                        OptionalCardText(item, "title");
                        OptionalCardText(item, "aid");
                        OptionalCardText(item, "src");
                    });
                break;
            case "videos":
                CardItems(
                    data,
                    ["title"],
                    ["thumb", "url", "channel", "src"],
                    static item =>
                    {
                        CardText(item, "title");
                        OptionalCardUrl(item, "thumb");
                        OptionalCardUrl(item, "url");
                        OptionalCardText(item, "channel");
                        OptionalCardText(item, "src");
                    });
                break;
            case "fact":
                Shape(data, ["answer"], ["detail"]);
                CardText(data, "answer");
                OptionalCardText(data, "detail");
                break;
            case "general":
                Shape(data, [], ["text"]);
                OptionalCardText(data, "text");
                break;
        }
    }

    private static void CardItems(
        JsonElement data,
        string[] required,
        string[] optional,
        Action<JsonElement> validate)
    {
        Shape(data, ["items"], []);
        JsonElement items = data.GetProperty("items");
        if (
            items.ValueKind != JsonValueKind.Array
            || items.GetArrayLength() is < 1 or > 20
        )
        {
            throw Invalid("Reader 卡片 items 数量无效");
        }
        foreach (JsonElement item in items.EnumerateArray())
        {
            Shape(item, required, optional);
            validate(item);
        }
    }

    private static void Shape(
        JsonElement value,
        string[] required,
        string[] optional)
    {
        if (value.ValueKind != JsonValueKind.Object)
        {
            throw Invalid("Reader 卡片字段无效");
        }
        DirectJsonValidation.RequireNoDuplicateKeys(value);
        HashSet<string> actual = value.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        if (
            required.Any(field => !actual.Contains(field))
            || actual.Any(field =>
                !required.Contains(field, StringComparer.Ordinal)
                && !optional.Contains(field, StringComparer.Ordinal))
        )
        {
            throw Invalid("Reader 卡片字段不匹配");
        }
    }

    private static string CardText(JsonElement value, string name) =>
        Text(value, name, 2_000);

    private static void OptionalCardText(JsonElement value, string name)
    {
        if (value.TryGetProperty(name, out _))
        {
            _ = CardText(value, name);
        }
    }

    private static void CardScalar(JsonElement value, string name)
    {
        JsonElement field = value.GetProperty(name);
        if (
            field.ValueKind == JsonValueKind.Number
            && field.TryGetDouble(out double number)
            && double.IsFinite(number)
        )
        {
            return;
        }
        _ = CardText(value, name);
    }

    private static void OptionalCardScalar(JsonElement value, string name)
    {
        if (value.TryGetProperty(name, out _))
        {
            CardScalar(value, name);
        }
    }

    private static void CardUrl(JsonElement value, string name)
    {
        string text = CardText(value, name);
        if (
            !Uri.TryCreate(text, UriKind.Absolute, out Uri? parsed)
            || parsed.Scheme != Uri.UriSchemeHttps
            || string.IsNullOrWhiteSpace(parsed.Host)
            || !string.IsNullOrEmpty(parsed.UserInfo)
            || text != text.Trim()
            || text.Contains('\\')
            || IsPrivateCardHost(parsed)
        )
        {
            throw Invalid("Reader 卡片 URL 无效");
        }
    }

    // 卡片图片和缩略图由 App 直接去请求。一张卡片没有任何理由指向本机或内网 ——
    // 而助手读的是网页和书里的内容，被注入的内容诱导它产出这样一张卡，就成了一次
    // 借 App 之手的内网探测。
    //
    // 这道闸只拦得住 IP 字面量和几个已知的本地后缀。内部 DNS 名（比如
    // https://intranet.example/）解析出来才知道指向哪，这里看不出来，因此**不是**
    // 完整的 SSRF 防护，只是把最直接的一类拿掉。真要做全，得在 App 发起请求前按
    // 解析结果再判一次。
    private static bool IsPrivateCardHost(Uri parsed)
    {
        string host = parsed.Host.Trim('[', ']');
        if (IPAddress.TryParse(host, out IPAddress? address))
        {
            if (IPAddress.IsLoopback(address)
                || address.IsIPv6LinkLocal
                || address.IsIPv6SiteLocal
                || address.IsIPv6UniqueLocal
                || address.Equals(IPAddress.Any)
                || address.Equals(IPAddress.IPv6Any))
            {
                return true;
            }
            if (address.AddressFamily
                == System.Net.Sockets.AddressFamily.InterNetwork)
            {
                byte[] octets = address.GetAddressBytes();
                return octets[0] switch
                {
                    0 or 10 or 127 => true,
                    // CGNAT 段，也就是 Tailscale 用的 100.64.0.0/10。卡片指向
                    // tailnet 只可能是异常；真要显示自建服务上的图，该走
                    // /pdf/api/img-proxy 或本机路由，而不是让卡片直连。
                    100 => octets[1] >= 64 && octets[1] <= 127,
                    169 => octets[1] == 254,
                    172 => octets[1] >= 16 && octets[1] <= 31,
                    192 => octets[1] == 168,
                    _ => false,
                };
            }
            return false;
        }
        string name = host.TrimEnd('.').ToLowerInvariant();
        return name == "localhost"
            || name.EndsWith(".localhost", StringComparison.Ordinal)
            || name.EndsWith(".local", StringComparison.Ordinal)
            || name.EndsWith(".internal", StringComparison.Ordinal)
            || name.EndsWith(".ts.net", StringComparison.Ordinal);
    }

    private static void OptionalCardUrl(JsonElement value, string name)
    {
        if (value.TryGetProperty(name, out _))
        {
            CardUrl(value, name);
        }
    }

    private static void ValidateNavigation(JsonElement root)
    {
        string action = Text(root, "action", 32);
        JsonElement target = root.GetProperty("target");
        JsonElement selection = root.GetProperty("selectionId");
        switch (action)
        {
            case "next-viewport":
            case "previous-viewport":
                RequireNull(target, "target");
                RequireNull(selection, "selectionId");
                return;
            case "scroll-to-text":
            case "scroll-to-heading":
                Text(root, "target", 320);
                RequireNull(selection, "selectionId");
                return;
            case "scroll-to-selection":
                RequireNull(target, "target");
                NullableSafeId(root, "selectionId", requireValue: true);
                return;
            case "go-to-page":
            case "go-to-section":
                if (
                    target.ValueKind != JsonValueKind.Number
                    || !target.TryGetInt64(out long location)
                    || location < 0
                    || location > 10_000_000
                )
                {
                    throw Invalid("Reader 跳转位置无效");
                }
                RequireNull(selection, "selectionId");
                return;
            default:
                throw Invalid("Reader 导航动作无效");
        }
    }

    private static void ValidateDocumentTarget(JsonElement target)
    {
        if (target.ValueKind != JsonValueKind.Object)
        {
            throw Invalid("Reader 文档目标无效");
        }
        DirectJsonValidation.RequireNoDuplicateKeys(target);
        if (!target.TryGetProperty("kind", out JsonElement kindValue))
        {
            throw Invalid("Reader 文档目标无效");
        }
        string kind = Text(target, "kind", 16);
        string locationName;
        long minimum;
        switch (kind)
        {
            case "pdf":
                Exact(target, "kind", "page");
                locationName = "page";
                minimum = 1;
                break;
            case "epub":
                Exact(target, "kind", "section");
                locationName = "section";
                minimum = 0;
                break;
            default:
                throw Invalid("Reader 文档目标类型无效");
        }
        JsonElement location = target.GetProperty(locationName);
        if (
            location.ValueKind != JsonValueKind.Number
            || !location.TryGetInt64(out long value)
            || value < minimum
            || value > 10_000_000
        )
        {
            throw Invalid("Reader 文档目标位置无效");
        }
    }

    private static void ValidateClientMutationId(
        JsonElement root,
        string name)
    {
        string value = Text(root, name, 34);
        if (
            !value.StartsWith("c_", StringComparison.Ordinal)
            || value.Length is < 10 or > 34
            || value[2..].Any(character =>
                !char.IsAsciiHexDigit(character)
                || char.IsUpper(character))
        )
        {
            throw Invalid($"Reader 输出 {name} 无效");
        }
    }

    private static void ValidateAnkiDraftId(
        JsonElement root,
        string name)
    {
        string value = Text(root, name, 38);
        if (
            value.Length != 38
            || !value.StartsWith("draft-", StringComparison.Ordinal)
            || value[6..].Any(character =>
                character is not (>= '0' and <= '9'
                    or >= 'a' and <= 'f'))
        )
        {
            throw Invalid($"Reader 输出 {name} 无效");
        }
    }

    private static void ValidateAnkiDraftCards(JsonElement cards)
    {
        if (
            cards.ValueKind != JsonValueKind.Array
            || cards.GetArrayLength() is < 1 or > 12
        )
        {
            throw Invalid("Reader Anki 草稿卡片数量无效");
        }
        foreach (JsonElement card in cards.EnumerateArray())
        {
            if (card.ValueKind != JsonValueKind.Object)
            {
                throw Invalid("Reader Anki 草稿卡片无效");
            }
            DirectJsonValidation.RequireNoDuplicateKeys(card);
            string type = Text(card, "type", 16);
            switch (type)
            {
                case "basic":
                    Exact(card, "type", "front", "back");
                    Text(card, "front", 8_000);
                    Text(card, "back", 8_000, allowEmpty: true);
                    break;
                case "cloze":
                    Exact(card, "type", "cloze");
                    Text(card, "cloze", 8_000);
                    break;
                default:
                    throw Invalid("Reader Anki 草稿卡片类型无效");
            }
        }
    }

    private static void ValidatePageCardReplacementCards(JsonElement cards)
    {
        if (
            cards.ValueKind != JsonValueKind.Array
            || cards.GetArrayLength() is < 1 or > 12
        )
        {
            throw Invalid("Reader 页面卡片数量无效");
        }
        foreach (JsonElement card in cards.EnumerateArray())
        {
            if (card.ValueKind != JsonValueKind.Object)
            {
                throw Invalid("Reader 页面卡片无效");
            }
            DirectJsonValidation.RequireNoDuplicateKeys(card);
            string type = Text(card, "type", 16);
            if (type == "basic")
            {
                Exact(card, "type", "front", "back");
                _ = Text(
                    card,
                    "front",
                    MaximumPageCardContentCharacters);
                _ = Text(
                    card,
                    "back",
                    MaximumPageCardContentCharacters);
            }
            else if (type == "cloze")
            {
                Exact(card, "type", "cloze");
                string cloze = Text(
                    card,
                    "cloze",
                    MaximumPageCardContentCharacters);
                if (!ContainsPageCardClozeDeletion(cloze))
                {
                    throw Invalid(
                        "Reader 页面 cloze 卡至少需要一个 {{c1::...}} 挖空");
                }
            }
            else
            {
                throw Invalid("Reader 页面卡片类型无效");
            }
        }
    }

    private static bool ContainsPageCardClozeDeletion(string value)
    {
        int searchFrom = 0;
        while (searchFrom < value.Length)
        {
            int marker = value.IndexOf("{{c", searchFrom, StringComparison.Ordinal);
            if (marker < 0)
            {
                return false;
            }
            int cursor = marker + 3;
            if (cursor >= value.Length || value[cursor] is < '1' or > '9')
            {
                searchFrom = marker + 3;
                continue;
            }
            cursor += 1;
            while (cursor < value.Length && value[cursor] is >= '0' and <= '9')
            {
                cursor += 1;
            }
            if (cursor + 2 > value.Length
                || value[cursor] != ':'
                || value[cursor + 1] != ':')
            {
                searchFrom = marker + 3;
                continue;
            }
            int contentStart = cursor + 2;
            int close = value.IndexOf("}}", contentStart, StringComparison.Ordinal);
            if (close > contentStart)
            {
                return true;
            }
            searchFrom = marker + 3;
        }
        return false;
    }

    private static bool IsPageCardMutation(string kind, JsonNode payload)
    {
        if (kind != "client-action" || payload is not JsonObject action)
        {
            return false;
        }
        return action["fn"] is JsonValue functionValue
            && functionValue.TryGetValue(out string? functionName)
            && functionName is (
                "_nativeReaderPageCardMutate"
                or "_nativeReaderLearningCardMutate");
    }

    /// 必填字段全等 + 允许一组具名可选字段。
    ///
    /// Exact 是 SetEquals：多一个少一个都拒。那对"这条协议只有这些字段"是对的，
    /// 但表达不了"可选"。加这个而不是放宽 Exact —— 别的调用点仍然该严格。
    private static void ExactWithOptional(
        JsonElement value,
        string[] required,
        string[] optional)
    {
        if (value.ValueKind != JsonValueKind.Object)
        {
            throw Invalid("Reader 输出必须是对象");
        }
        DirectJsonValidation.RequireNoDuplicateKeys(value);
        HashSet<string> actual = value.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        foreach (string field in required)
        {
            if (!actual.Remove(field))
            {
                throw Invalid("Reader 输出字段不匹配");
            }
        }
        foreach (string field in optional)
        {
            actual.Remove(field);
        }
        if (actual.Count > 0)
        {
            throw Invalid("Reader 输出字段不匹配");
        }
    }

    /// 卡片的绑定目标。形状与服务端 reader_card_contract._norm_bind 一致 ——
    /// 那边形状不对是**整条丢掉**（卡片仍显示，只是退回浮层），这里是跨机信封，
    /// 按既有教义**拒收**：宁可报错也别悄悄少一半语义。
    private static void ValidateCardBind(JsonElement bind)
    {
        if (bind.ValueKind != JsonValueKind.Object)
        {
            throw Invalid("Reader 卡片 bind 无效");
        }
        string kind = Text(bind, "kind", 32);
        switch (kind)
        {
            case "upage-block":
                Exact(bind, "kind", "upage", "bid");
                _ = Text(bind, "upage", 200);
                _ = Text(bind, "bid", 200);
                break;
            case "page-chars":
                // 序号与原文二选一；block 可选，把"按文本找"限定在某一块里。
                // 形状与服务端 _norm_bind、阅读器 normalizeCardBind 一致。
                ExactWithOptional(
                    bind,
                    new[] { "kind", "page" },
                    new[] { "from", "to", "text", "rev", "block" });
                int page = CardInt(bind, "page");
                if (page < 1)
                {
                    throw Invalid("Reader 卡片 bind 的页码无效");
                }
                bool hasFrom = bind.TryGetProperty("from", out _);
                bool hasTo = bind.TryGetProperty("to", out _);
                if (hasFrom != hasTo)
                {
                    // 只给一半是发错了，不是"想按文本找"。
                    throw Invalid("Reader 卡片 bind 的 from/to 必须成对出现");
                }
                if (hasFrom)
                {
                    int from = CardInt(bind, "from");
                    int to = CardInt(bind, "to");
                    // 歪掉的区间不如没有：与其在页面上定出一个荒唐的位置，
                    // 不如让调用方立刻知道自己发错了。
                    if (from < 0 || to < from)
                    {
                        throw Invalid("Reader 卡片 bind 的字符区间无效");
                    }
                }
                // ⚠ OptionalCardText 返回 void，这里需要值 —— 用 CardText 取。
                string bindText = bind.TryGetProperty("text", out _)
                    ? CardText(bind, "text") : string.Empty;
                OptionalCardText(bind, "rev");
                if (bind.TryGetProperty("block", out JsonElement blockValue)
                    && blockValue.ValueKind != JsonValueKind.Null)
                {
                    int block = CardInt(bind, "block");
                    if (block < 1)
                    {
                        throw Invalid("Reader 卡片 bind 的块号无效");
                    }
                    if (string.IsNullOrEmpty(bindText))
                    {
                        // 块号必须配原文：只给块号不是一个位置。
                        throw Invalid(
                            "Reader 卡片 bind 的块号必须与 text 同时给出");
                    }
                }
                if (!hasFrom && string.IsNullOrEmpty(bindText))
                {
                    throw Invalid("Reader 卡片 bind 必须给出字符区间或原文");
                }
                break;
            default:
                throw Invalid("Reader 卡片 bind 类型无效");
        }
    }

    private static int CardInt(JsonElement value, string name)
    {
        if (!value.TryGetProperty(name, out JsonElement field)
            || field.ValueKind != JsonValueKind.Number
            || !field.TryGetInt32(out int result))
        {
            throw Invalid("Reader 卡片 bind 字段无效");
        }
        return result;
    }

    private static void Exact(JsonElement value, params string[] fields)
    {
        if (value.ValueKind != JsonValueKind.Object)
        {
            throw Invalid("Reader 输出必须是对象");
        }
        DirectJsonValidation.RequireNoDuplicateKeys(value);
        HashSet<string> actual = value.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        if (!actual.SetEquals(fields))
        {
            throw Invalid("Reader 输出字段不匹配");
        }
    }

    private static string SafeId(JsonElement root, string name)
    {
        string value = Text(root, name, 160);
        if (!DirectBridgeContract.IsSafeId(value))
        {
            throw Invalid($"Reader 输出 {name} 无效");
        }
        return value;
    }

    private static string Text(
        JsonElement root,
        string name,
        int maximum,
        bool allowEmpty = false)
    {
        JsonElement value = root.GetProperty(name);
        if (
            value.ValueKind != JsonValueKind.String
            || value.GetString() is not string text
            || (!allowEmpty && string.IsNullOrWhiteSpace(text))
            || text.Length > maximum
            || text.Any(character => character == '\0')
        )
        {
            throw Invalid($"Reader 输出 {name} 无效");
        }
        return text;
    }

    private static string FileText(
        JsonElement root,
        string name,
        int maximum)
    {
        string text = Text(root, name, maximum);
        if (ContainsC0OrC1(text))
        {
            throw Invalid($"Reader 输出 {name} 无效");
        }
        return text;
    }

    private static bool ContainsC0OrC1(string value) => value.Any(
        character => character is >= '\u0000' and <= '\u001f'
            or >= '\u007f' and <= '\u009f');

    private static string? NullableText(
        JsonElement root,
        string name,
        int maximum)
    {
        JsonElement value = root.GetProperty(name);
        return value.ValueKind switch
        {
            JsonValueKind.Null => null,
            JsonValueKind.String => Text(root, name, maximum, allowEmpty: true),
            _ => throw Invalid($"Reader 输出 {name} 无效"),
        };
    }

    private static string? NullableSafeId(
        JsonElement root,
        string name,
        bool requireValue = false)
    {
        string? value = NullableText(root, name, 160);
        if (
            (requireValue && value is null)
            || (value is not null && !DirectBridgeContract.IsSafeId(value))
        )
        {
            throw Invalid($"Reader 输出 {name} 无效");
        }
        return value;
    }

    private static void RequireNull(JsonElement value, string name)
    {
        if (value.ValueKind != JsonValueKind.Null)
        {
            throw Invalid($"Reader 输出 {name} 必须为空");
        }
    }

    private static ReaderRealtimeOutputException Invalid(string message) =>
        new(
            "BW_READER_REALTIME_OUTPUT_INVALID",
            message,
            retryable: false);
}

internal sealed class ReaderRealtimeOutputBroker
{
    // An exact-text mutation may have to bring an off-screen PDF page into the
    // virtualized DOM and wait for its native text layer before it can produce
    // an applied/rejected receipt. Keep the broker deadline above the bounded
    // 9.6 second page wait plus persistence/ACK overhead. The page side remains
    // bounded, so this does not turn a lost receiver into an endless wait.
    private static readonly TimeSpan DeliveryTimeout = TimeSpan.FromSeconds(20);
    private const int MaximumPendingOutputs = 16;

    // WSS 连上与「这个 source 可以收东西」不是同一时刻:连接在
    // DirectBridgeServer 就建立了,能回传要等 DirectBridgeProtocol 的
    // visual-register/Attach。中间那段空窗里,快照是 ready、health 也说
    // contextConnected,写回却立刻报来源离线 —— 用户看到的就是这个。
    //
    // 所以这里等一小会儿,**只等注册,不重发**。区别要紧:此刻还没有任何东西
    // 发出去过,等的是能不能发;而一旦 SendAsync 抛了错或租约中途退休,
    // 写入结果就未知了,那时重试会写出第二条,绝不能等同处理。
    //
    // 上限刻意短。阅读器里用户是在等着的,让他为一次写便签站在那里数秒
    // 比失败更难受;重连退避第一档就在一秒上下,盖住它就够了。
    private static readonly TimeSpan SourceRegistrationWait =
        TimeSpan.FromMilliseconds(2_500);
    private static readonly TimeSpan SourceRegistrationPoll =
        TimeSpan.FromMilliseconds(50);

    private sealed record PendingOutput(
        ReaderRealtimeOutputRequest Request,
        ReaderContextSourceLease Lease,
        TaskCompletionSource<ReaderRealtimeOutputAck> Completion);

    private readonly ReaderContextSourceRouter _router;
    private readonly ReaderRealtimeOutputOutbox? _outbox;
    private readonly SemaphoreSlim _replayGate = new(1, 1);
    private int _replayRequested;
    private readonly object _gate = new();
    private readonly Dictionary<string, PendingOutput> _pending =
        new(StringComparer.Ordinal);

    internal ReaderRealtimeOutputBroker(
        ReaderContextSourceRouter router,
        string? outboxPath = null)
    {
        _router = router;
        _outbox = string.IsNullOrWhiteSpace(outboxPath)
            ? null
            : new ReaderRealtimeOutputOutbox(outboxPath);
        _router.SourceAttached += OnSourceAttached;
    }

    internal ReaderRealtimeOutputSourceStatus GetSourceStatus(
        string sourceInstanceId) => new(
            sourceInstanceId,
            _router.TryGetLease(sourceInstanceId, out _));

    /// 正常发送路径上的销账。
    ///
    /// ⚠ <paramref name="alreadyQueued"/> 为 true 时**什么都不做**：那是重放
    /// 路径，销账由重放循环自己按 bindOutcome 决定（绑定没落实的要留在队列里
    /// 继续等，不能在这里一律标成 applied）。在这里抢着销会把那条判断绕过去。
    private async Task SettleOutboxAsync(
        ReaderRealtimeOutputRequest request,
        bool durable,
        bool alreadyQueued)
    {
        if (!durable || alreadyQueued || _outbox is null)
        {
            return;
        }
        try
        {
            await _outbox.MarkAppliedAsync(
                request.Correlation,
                CancellationToken.None).ConfigureAwait(false);
        }
        catch (Exception exception)
        {
            // 销账失败不该把一次成功的写入变成失败；但也不能不出声 ——
            // 没销掉的记录会被重放，靠接收端的 cid 幂等兜住。
            Console.Error.WriteLine(
                "[outbox] 销账失败，该条将被重放（接收端按 cid 幂等）: "
                    + exception.GetType().Name);
        }
    }

    internal Task<ReaderRealtimeOutputAck> SendAsync(
        ReaderRealtimeOutputRequest request,
        CancellationToken cancellationToken) =>
        SendAsync(request, cancellationToken, alreadyQueued: false);

    /// <param name="alreadyQueued">
    /// true 表示这条已经在队列里（重放路径）。
    /// ⚠ 这个参数不是可选优化，是**防重入**：重放循环里就是
    /// <c>await SendAsync(entry.Request, ...)</c>，如果它再走一次入队，
    /// 就会在遍历队列的同时改写队列 —— 轻则语义混乱（刚销账又被加回来），
    /// 重则跟 outbox 的 _gate 撞成死锁（打包自检里表现为整体超时，
    /// 而不是任何一条断言失败，最难查）。
    /// </param>
    private async Task<ReaderRealtimeOutputAck> SendAsync(
        ReaderRealtimeOutputRequest request,
        CancellationToken cancellationToken,
        bool alreadyQueued)
    {
        // ── 先落地，再送达 ────────────────────────────────────────
        //   用户 2026-08-23：「就算当时没有连通也应该更新 windows 和 pi 本地的
        //   文件，在 app 联通时自动进行内容更新」。
        //
        //   原来的顺序是"先等租约，等不到才排队"，于是**拿到租约之后**的任何
        //   失败都直接丢：发送时租约没了、20 秒回执超时、页面回 rejected ——
        //   而"网络抖一下、连接刚断"恰恰发生在拿到租约之后，正好落在覆盖不到
        //   的洞里。
        //
        //   现在改成一律先入队：入队 = 这次写入已经不会丢了；随后照常尝试
        //   实时送达，成功再销账。代价是每次写多一次本地落盘（有界文件，
        //   MaximumEntries=64），换掉一整类"发出去就没了"。
        bool durable = _outbox is not null
            && ReaderRealtimeOutputProtocol.IsDurableMutation(request);
        if (durable && !alreadyQueued)
        {
            await _outbox!.EnqueueAsync(request, cancellationToken)
                .ConfigureAwait(false);
        }

        ReaderContextSourceLease? lease = await WaitForSourceAsync(
            request.SourceInstanceId,
            cancellationToken).ConfigureAwait(false);
        if (lease is null)
        {
            if (durable)
            {
                RequestReplay();
                return new ReaderRealtimeOutputAck(
                    string.Empty,
                    request.Correlation,
                    request.SourceInstanceId,
                    "queued",
                    null,
                    "unknown",
                    "source-offline-queued");
            }
            throw Failure(
                "BW_READER_REALTIME_OUTPUT_SOURCE_OFFLINE",
                "指定 Reader 页面来源当前不在线（已等待 "
                    + $"{SourceRegistrationWait.TotalSeconds:0.#} 秒仍未注册；"
                    + _router.DescribeRegisteredSources() + "）",
                retryable: true);
        }
        PendingOutput pending = new(
            request,
            lease,
            new TaskCompletionSource<ReaderRealtimeOutputAck>(
                TaskCreationOptions.RunContinuationsAsynchronously));
        lock (_gate)
        {
            if (_pending.Count >= MaximumPendingOutputs)
            {
                throw Failure(
                    "BW_READER_REALTIME_OUTPUT_CAPACITY",
                    "Reader 输出仍在处理中",
                    retryable: true);
            }
            if (!_pending.TryAdd(request.Correlation, pending))
            {
                throw Failure(
                    "BW_READER_REALTIME_OUTPUT_DUPLICATE_PENDING",
                    "相同 Reader 输出仍在处理中",
                    retryable: true);
            }
        }
        try
        {
            try
            {
                await _router.SendAsync(
                    lease,
                    ReaderRealtimeOutputProtocol.Event(request),
                    cancellationToken).ConfigureAwait(false);
            }
            catch (ReaderVisualDeliveryException exception)
            {
                throw Failure(
                    "BW_READER_REALTIME_OUTPUT_SOURCE_OFFLINE",
                    exception.Message,
                    retryable: true,
                    exception);
            }
            Task winner = await Task.WhenAny(
                pending.Completion.Task,
                lease.LeaseRetired,
                Task.Delay(DeliveryTimeout, cancellationToken))
                .ConfigureAwait(false);
            cancellationToken.ThrowIfCancellationRequested();
            if (winner == pending.Completion.Task)
            {
                ReaderRealtimeOutputAck ack =
                    await pending.Completion.Task.ConfigureAwait(false);
                if (ack.Outcome == "rejected")
                {
                    // 页面明确拒收 —— 不是"没送到"，是"送到了但对方不要"。
                    // 重放只会再被拒一次，销掉别留僵尸。
                    await SettleOutboxAsync(request, durable, alreadyQueued)
                        .ConfigureAwait(false);
                    throw Failure(
                        "BW_READER_REALTIME_OUTPUT_REJECTED",
                        ack.Error ?? "Reader 拒绝了输出",
                        retryable: false);
                }
                // ⚠ 送达成功必须销账。漏掉这一步，队列里会留着一条**已经生效**
                //   的记录，下次重放就真的多出一张卡 —— 正是 IsDurableMutation
                //   注释里担心的那个 duplicate。（接收端也按 cid 幂等，两道防线。）
                await SettleOutboxAsync(request, durable, alreadyQueued)
                    .ConfigureAwait(false);
                return ack;
            }
            if (winner == lease.LeaseRetired)
            {
                throw Failure(
                    "BW_READER_REALTIME_OUTPUT_SOURCE_OFFLINE",
                    "输出期间指定 Reader 页面来源已离线",
                    retryable: true);
            }
            throw Failure(
                "BW_READER_REALTIME_OUTPUT_TIMEOUT",
                "Reader 输出回执超时",
                retryable: true);
        }
        finally
        {
            lock (_gate)
            {
                if (
                    _pending.TryGetValue(
                        request.Correlation,
                        out PendingOutput? current)
                    && ReferenceEquals(current, pending)
                )
                {
                    _pending.Remove(request.Correlation);
                }
            }
        }
    }

    internal void Accept(
        ReaderContextSourceLease lease,
        ReaderRealtimeOutputAck ack)
    {
        PendingOutput pending;
        lock (_gate)
        {
            if (
                !_pending.TryGetValue(ack.Correlation, out pending!)
                || !ReferenceEquals(pending.Lease, lease)
            )
            {
                throw Failure(
                    "BW_READER_REALTIME_OUTPUT_NOT_PENDING",
                    "Reader 输出请求不存在或已过期",
                    retryable: false);
            }
            if (pending.Request.SourceInstanceId != ack.SourceInstanceId)
            {
                throw Failure(
                    "BW_READER_REALTIME_OUTPUT_IDENTITY_MISMATCH",
                    "Reader 输出来源不匹配",
                    retryable: false);
            }
            _pending.Remove(ack.Correlation);
            pending.Completion.TrySetResult(ack);
        }
    }

    internal Task<int> GetOutboxCountAsync(
        CancellationToken cancellationToken) =>
        _outbox is null
            ? Task.FromResult(0)
            : _outbox.CountAsync(cancellationToken);

    private void OnSourceAttached(ReaderContextSourceLease unusedLease)
    {
        _ = unusedLease;
        if (_outbox is not null)
        {
            RequestReplay();
        }
    }

    private void RequestReplay()
    {
        Interlocked.Exchange(ref _replayRequested, 1);
        _ = ReplayAvailableAsync();
    }

    private async Task ReplayAvailableAsync()
    {
        if (_outbox is null
            || !await _replayGate.WaitAsync(0).ConfigureAwait(false))
        {
            return;
        }
        try
        {
            // SourceAttached may arrive while an older source is still waiting
            // for its ACK. Keep one coalesced replay request bit so that event
            // cannot be lost merely because the non-blocking gate was busy.
            while (Interlocked.Exchange(ref _replayRequested, 0) == 1)
            {
                IReadOnlyList<ReaderRealtimeOutputOutboxEntry> entries =
                    await _outbox.ReplayableAsync(CancellationToken.None)
                        .ConfigureAwait(false);
                foreach (ReaderRealtimeOutputOutboxEntry entry in entries)
                {
                    bool complete = false;
                    foreach (ReaderContextSourceLease lease in
                        _router.CurrentLeases())
                    {
                        ReaderRealtimeOutputRequest replay =
                            entry.Request with
                            {
                                SourceInstanceId = lease.SourceInstanceId,
                                Page = entry.Request.Page.DeepClone(),
                                Payload = entry.Request.Payload.DeepClone(),
                            };
                        try
                        {
                            // ⚠ alreadyQueued: true —— 见 SendAsync 上的说明，
                            //   重放时再入队会在遍历队列的同时改写队列。
                            ReaderRealtimeOutputAck ack = await SendAsync(
                                replay,
                                CancellationToken.None,
                                alreadyQueued: true).ConfigureAwait(false);
                            if (ack.Outcome is "applied" or "replay")
                            {
                                if (ReaderRealtimeOutputProtocol
                                        .IsPageCharsCardMutation(entry.Request)
                                    && ack.BindOutcome != "bound")
                                {
                                    await _outbox.MarkDeferredAsync(
                                        entry.Request.Correlation,
                                        "BW_READER_REALTIME_OUTPUT_BIND_NOT_PERSISTED:"
                                            + (ack.BindReason ?? "unknown"),
                                        CancellationToken.None)
                                        .ConfigureAwait(false);
                                    continue;
                                }
                                await _outbox.MarkAppliedAsync(
                                    entry.Request.Correlation,
                                    CancellationToken.None).ConfigureAwait(false);
                                complete = true;
                                break;
                            }
                        }
                        catch (ReaderRealtimeOutputException exception)
                        {
                            if (ReplayMayWaitForAnotherSource(exception))
                            {
                                await _outbox.MarkDeferredAsync(
                                    entry.Request.Correlation,
                                    exception.Code + ":" + exception.Message,
                                    CancellationToken.None).ConfigureAwait(false);
                                continue;
                            }
                            await _outbox.MarkFailedAsync(
                                entry.Request.Correlation,
                                exception.Code + ":" + exception.Message,
                                CancellationToken.None).ConfigureAwait(false);
                            // Rejection belongs to this source/WebView.  Keep
                            // the durable failure visible, but do not let an
                            // old still-online WebView prevent another current
                            // healthy source from applying the same stable
                            // mutation during this replay cycle.
                            continue;
                        }
                        catch (Exception exception) when (
                            exception is IOException
                            or UnauthorizedAccessException
                            or JsonException)
                        {
                            await _outbox.MarkDeferredAsync(
                                entry.Request.Correlation,
                                exception.GetType().Name,
                                CancellationToken.None).ConfigureAwait(false);
                        }
                    }
                    if (complete)
                    {
                        continue;
                    }
                }
            }
        }
        catch
        {
            // The queue remains on disk.  A later source registration or tool
            // call retries it; background replay must never crash Direct.
        }
        finally
        {
            _replayGate.Release();
            if (Volatile.Read(ref _replayRequested) == 1)
            {
                _ = ReplayAvailableAsync();
            }
        }
    }

    private static bool ReplayMayWaitForAnotherSource(
        ReaderRealtimeOutputException exception)
    {
        if (exception.Code is
            "BW_READER_REALTIME_OUTPUT_SOURCE_OFFLINE"
            or "BW_READER_REALTIME_OUTPUT_TIMEOUT"
            or "BW_READER_REALTIME_OUTPUT_CAPACITY"
            or "BW_READER_REALTIME_OUTPUT_DUPLICATE_PENDING")
        {
            return true;
        }
        return exception.Code == "BW_READER_REALTIME_OUTPUT_REJECTED"
            && (exception.Message.Contains(
                    "STALE",
                    StringComparison.OrdinalIgnoreCase)
                || exception.Message.Contains(
                    "UNAVAILABLE",
                    StringComparison.OrdinalIgnoreCase));
    }

    // 等这个 source 完成注册。等到了给租约,等不到给 null。
    //
    // 轮询而不是让 router 发信号,是因为只有这一条路径需要等:为一个局部需求
    // 去改共享的 router 语义,代价比每 50 毫秒查一次字典大得多。
    private async Task<ReaderContextSourceLease?> WaitForSourceAsync(
        string sourceInstanceId,
        CancellationToken cancellationToken)
    {
        DateTimeOffset deadline = DateTimeOffset.UtcNow + SourceRegistrationWait;
        while (true)
        {
            if (
                _router.TryGetLease(
                    sourceInstanceId,
                    out ReaderContextSourceLease? lease)
                && lease is not null
            )
            {
                return lease;
            }
            if (DateTimeOffset.UtcNow >= deadline)
            {
                return null;
            }
            await Task.Delay(
                SourceRegistrationPoll,
                cancellationToken).ConfigureAwait(false);
        }
    }

    private static ReaderRealtimeOutputException Failure(
        string code,
        string message,
        bool retryable,
        Exception? innerException = null) =>
        new(code, message, retryable, innerException);
}
