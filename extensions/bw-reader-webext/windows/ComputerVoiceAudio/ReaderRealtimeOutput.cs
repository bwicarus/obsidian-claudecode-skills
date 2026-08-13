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
    string? Error);

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

    internal static bool IsKind(string value) => value is
        "assistant-turn"
        or "tool-status"
        or "card"
        or "navigate"
        or "highlight"
        or "highlight-text"
        or "anki-draft"
        or "practice-page"
        or "client-action";

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
        if (bytes.Length > MaximumPayloadBytes)
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
            case "practice-page":
                ValidatePracticePage(root);
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

    // 可见反馈通道。
    //
    // 迁移前，AI 的改动经 Pi 返回 client_actions，前端 runActions 把 {fn,args} 分派到
    // window[fn]，于是高亮完会出现那张「原文｜↗跳转｜↩撤销⇄↪重做」卡片条。桥接语音
    // 绕开 Pi 之后，本地操作照样落库，却没有任何东西告诉界面「发生了什么」——用户看到的
    // 就是"功能没了"。这里让桥接产出与 Pi 逐字段同形的 {fn,args}，前端渲染器一行不改。
    //
    // fn 必须白名单：runActions 是 window[fn].apply(...) 的动态分派。Pi 路径下由服务端
    // 可信性兜底，换到桥接就必须显式限制，否则等于把「远程调用页面任意函数」开给桥接侧。
    // 接收端（rc-computer-voice 的 normalizer）应当再卡一次，两端都卡，不靠单边。
    private static void ValidateClientAction(JsonElement root)
    {
        string fn = Text(root, "fn", 64);
        if (fn is not (
            "_assistEdit"
            or "notesReload"
            or "jumpWithBack"
            or "_nativePDFUndoLast"))
        {
            throw Invalid("Reader 客户端动作不在白名单内");
        }
        JsonElement args = root.GetProperty("args");
        if (args.ValueKind != JsonValueKind.Array)
        {
            throw Invalid("Reader 客户端动作参数必须是数组");
        }
        if (fn is "notesReload")
        {
            if (args.GetArrayLength() != 0)
            {
                throw Invalid("notesReload 不接受参数");
            }
            return;
        }
        // 撤销复用 App 本机已有的撤销栈（native-local-runtime 里已实现 highlight-create /
        // note-create / note-edit 三类，含修订冲突检测），桥接只负责发这条指令。
        // 参数是一次性可信操作编号，本机据此拒绝重放；格式与 Pi 的 undo_last 下发一致。
        if (fn is "_nativePDFUndoLast")
        {
            if (args.GetArrayLength() != 1
                || args[0].ValueKind != JsonValueKind.String)
            {
                throw Invalid("_nativePDFUndoLast 需要一个操作编号");
            }
            // 与本文件既有的 id 校验同一写法，不额外引入正则依赖。
            // 形状须与 native-local-runtime 的 npdf_[0-9a-f]{24} 完全一致。
            string operationId = args[0].GetString() ?? string.Empty;
            if (operationId.Length != 29
                || !operationId.StartsWith("npdf_", StringComparison.Ordinal)
                || operationId[5..].Any(character =>
                    character is not (>= '0' and <= '9' or >= 'a' and <= 'f')))
            {
                throw Invalid("Reader 撤销操作编号无效");
            }
            return;
        }
        if (fn is "jumpWithBack")
        {
            if (args.GetArrayLength() != 1
                || args[0].ValueKind != JsonValueKind.Number
                || !args[0].TryGetInt32(out int page)
                || page < 1
                || page > 100_000)
            {
                throw Invalid("jumpWithBack 需要一个合法页码");
            }
            return;
        }
        if (args.GetArrayLength() != 1
            || args[0].ValueKind != JsonValueKind.Object)
        {
            throw Invalid("_assistEdit 需要一个对象参数");
        }
        JsonElement edit = args[0];
        DirectJsonValidation.RequireNoDuplicateKeys(edit);
        Exact(edit, "type", "file", "items");
        // 目前只放行高亮：便签走各自的既有通道，未经验证的类型不应从桥接侧引入。
        if (Text(edit, "type", 16) is not "highlight")
        {
            throw Invalid("Reader 客户端动作类型无效");
        }
        FileText(edit, "file", 4_096);
        ValidateClientActionItems(edit.GetProperty("items"));
    }

    // 卡片条逐条渲染这些字段（rc-turncard 的 _hlCardEl 消费 text/color/pdf_page/
    // disp_page，撤销与重做则要靠 id 找回那条高亮）。字段集固定，避免桥接侧
    // 塞进渲染器根本不认识的内容。
    private static void ValidateClientActionItems(JsonElement items)
    {
        if (items.ValueKind != JsonValueKind.Array)
        {
            throw Invalid("Reader 客户端动作条目必须是数组");
        }
        int count = items.GetArrayLength();
        if (count is 0 or > 64)
        {
            throw Invalid("Reader 客户端动作条目数量无效");
        }
        foreach (JsonElement item in items.EnumerateArray())
        {
            if (item.ValueKind != JsonValueKind.Object)
            {
                throw Invalid("Reader 客户端动作条目必须是对象");
            }
            DirectJsonValidation.RequireNoDuplicateKeys(item);
            Exact(item, "id", "text", "color", "pdf_page", "disp_page");
            Text(item, "id", 128);
            Text(item, "text", 2_000);
            string color = Text(item, "color", 16);
            if (color is not ("yellow" or "green" or "blue" or "pink"))
            {
                throw Invalid("Reader 客户端动作条目颜色无效");
            }
            foreach (string key in new[] { "pdf_page", "disp_page" })
            {
                JsonElement page = item.GetProperty(key);
                if (page.ValueKind != JsonValueKind.Number
                    || !page.TryGetInt32(out int value)
                    || value < 1
                    || value > 100_000)
                {
                    throw Invalid("Reader 客户端动作条目页码无效");
                }
            }
        }
    }

    internal static ReaderRealtimeOutputAck ValidateAck(JsonElement message)
    {
        Exact(
            message,
            "contract",
            "type",
            "requestId",
            "sessionId",
            "correlation",
            "sourceInstanceId",
            "outcome",
            "error");
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
        return new ReaderRealtimeOutputAck(
            sessionId,
            correlation,
            sourceInstanceId,
            outcome,
            error);
    }

    private static void ValidateCard(JsonElement card)
    {
        Exact(card, "kind", "title", "data");
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
        )
        {
            throw Invalid("Reader 卡片 URL 无效");
        }
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

    private static void ValidatePracticePage(JsonElement root)
    {
        Exact(root, "mutationId", "title", "paper", "blocks");
        ValidateClientMutationId(root, "mutationId");
        Text(root, "title", 120);
        string paper = Text(root, "paper", 16);
        if (paper is not ("dictation" or "exam" or "math" or "draw" or "note"))
        {
            throw Invalid("Reader 练习页纸张类型无效");
        }

        JsonElement blocks = root.GetProperty("blocks");
        if (
            blocks.ValueKind != JsonValueKind.Array
            || blocks.GetArrayLength() is < 1 or > 80
        )
        {
            throw Invalid("Reader 练习页元素数量无效");
        }

        HashSet<string> ids = new(StringComparer.Ordinal);
        int answerBlocks = 0;
        int checkButtons = 0;
        foreach (JsonElement block in blocks.EnumerateArray())
        {
            if (block.ValueKind != JsonValueKind.Object)
            {
                throw Invalid("Reader 练习页元素无效");
            }
            DirectJsonValidation.RequireNoDuplicateKeys(block);
            string id = SafeId(block, "id");
            if (!ids.Add(id))
            {
                throw Invalid("Reader 练习页元素 id 重复");
            }
            string kind = Text(block, "kind", 16);
            switch (kind)
            {
                case "text":
                    Exact(block, "id", "kind", "text");
                    Text(block, "text", 4_000);
                    break;
                case "blank":
                    Exact(block, "id", "kind", "label", "answer");
                    Text(block, "label", 320);
                    NullableText(block, "answer", 2_000);
                    answerBlocks += 1;
                    break;
                case "choice":
                    Exact(
                        block,
                        "id",
                        "kind",
                        "text",
                        "options",
                        "answer");
                    Text(block, "text", 4_000);
                    JsonElement options = block.GetProperty("options");
                    if (
                        options.ValueKind != JsonValueKind.Array
                        || options.GetArrayLength() is < 2 or > 6
                    )
                    {
                        throw Invalid("Reader 练习页选择项数量无效");
                    }
                    HashSet<string> optionTexts = new(StringComparer.Ordinal);
                    foreach (JsonElement option in options.EnumerateArray())
                    {
                        if (
                            option.ValueKind != JsonValueKind.String
                            || option.GetString() is not string optionText
                            || string.IsNullOrWhiteSpace(optionText)
                            || optionText.Length > 2_000
                            || optionText.Any(character => character == '\0')
                            || !optionTexts.Add(optionText)
                        )
                        {
                            throw Invalid("Reader 练习页选择项无效");
                        }
                    }
                    string answer = Text(block, "answer", 1);
                    if (
                        answer[0] is < 'A' or > 'F'
                        || answer[0] - 'A' >= options.GetArrayLength()
                    )
                    {
                        throw Invalid("Reader 练习页选择题答案无效");
                    }
                    answerBlocks += 1;
                    break;
                case "button":
                    Exact(block, "id", "kind", "label", "event");
                    Text(block, "label", 320);
                    if (Text(block, "event", 16) != "check")
                    {
                        throw Invalid("Reader 练习页按钮事件无效");
                    }
                    checkButtons += 1;
                    break;
                default:
                    throw Invalid("Reader 练习页元素类型无效");
            }
        }
        if (answerBlocks < 1 || checkButtons != 1)
        {
            throw Invalid("Reader 练习页必须包含作答区和一个检查按钮");
        }
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
    // 插入真实 PDF 页后还要等本机 PDF 写入、页号重排和 blocks 持久化。
    // 这条仍然有界，但不能套用精确高亮的 20 秒预算；否则 Reader 可能已经
    // 落盘而 MCP 先报告未知，诱发用户或模型重复建页。
    private static readonly TimeSpan PracticePageDeliveryTimeout =
        TimeSpan.FromSeconds(50);
    private const int MaximumPendingOutputs = 16;

    private sealed record PendingOutput(
        ReaderRealtimeOutputRequest Request,
        ReaderContextSourceLease Lease,
        TaskCompletionSource<ReaderRealtimeOutputAck> Completion);

    private readonly ReaderContextSourceRouter _router;
    private readonly object _gate = new();
    private readonly Dictionary<string, PendingOutput> _pending =
        new(StringComparer.Ordinal);

    internal ReaderRealtimeOutputBroker(ReaderContextSourceRouter router)
    {
        _router = router;
    }

    internal ReaderRealtimeOutputSourceStatus GetSourceStatus(
        string sourceInstanceId) => new(
            sourceInstanceId,
            _router.TryGetLease(sourceInstanceId, out _));

    internal async Task<ReaderRealtimeOutputAck> SendAsync(
        ReaderRealtimeOutputRequest request,
        CancellationToken cancellationToken)
    {
        if (
            !_router.TryGetLease(
                request.SourceInstanceId,
                out ReaderContextSourceLease? lease)
            || lease is null
        )
        {
            throw Failure(
                "BW_READER_REALTIME_OUTPUT_SOURCE_OFFLINE",
                "指定 Reader 页面来源当前不在线",
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
            TimeSpan deliveryTimeout = request.Kind == "practice-page"
                ? PracticePageDeliveryTimeout
                : DeliveryTimeout;
            Task winner = await Task.WhenAny(
                pending.Completion.Task,
                lease.LeaseRetired,
                Task.Delay(deliveryTimeout, cancellationToken))
                .ConfigureAwait(false);
            cancellationToken.ThrowIfCancellationRequested();
            if (winner == pending.Completion.Task)
            {
                ReaderRealtimeOutputAck ack =
                    await pending.Completion.Task.ConfigureAwait(false);
                if (ack.Outcome == "rejected")
                {
                    throw Failure(
                        "BW_READER_REALTIME_OUTPUT_REJECTED",
                        ack.Error ?? "Reader 拒绝了输出",
                        retryable: false);
                }
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

    private static ReaderRealtimeOutputException Failure(
        string code,
        string message,
        bool retryable,
        Exception? innerException = null) =>
        new(code, message, retryable, innerException);
}
