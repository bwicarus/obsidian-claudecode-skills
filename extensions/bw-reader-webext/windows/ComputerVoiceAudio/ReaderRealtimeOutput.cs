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
        or "highlight-range"
        or "anki-draft"
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
            or "_nativeReaderCreateNote"
            or "_nativeReaderEditNote"
            or "_nativeReaderMakeNote"
            or "_nativeReaderMarkVocabulary"
            or "_bwWebHighlightByText"))
        {
            throw Invalid("Reader 客户端动作不在白名单内");
        }
        JsonElement args = root.GetProperty("args");
        if (args.ValueKind != JsonValueKind.Array)
        {
            throw Invalid("Reader 客户端动作参数必须是数组");
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
