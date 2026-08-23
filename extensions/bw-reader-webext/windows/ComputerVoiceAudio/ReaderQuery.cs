using System.Text.Json;
using System.Text.Json.Nodes;

namespace BwReader.ComputerVoiceAudio;

// 助手向 Reader 提问并等一个答案。
//
// 已有的三条通道都答不了这类问题：visual 传的是图，browser-control 的响应形状
// 是滚动位置，realtime-output 是单向下发。读取书里的东西 —— 高亮有哪些、便签
// 写了什么 —— 需要一次真正的往返。
//
// 塞进快照是个诱人的捷径，但站不住：桥接只保留最新一份快照，用户翻到第 200 页
// 之后，第 50 页的高亮就不在里面了；要覆盖就得每次都推全书，体积随书增长。
// 按需问一次，问什么答什么。
//
// 与 client-action 的语义正好相反，这一点决定了两边的错误处理：那边是写，结果
// 未知时不能重试（会写出第二条）；这边是读，超时重试是安全的，也应该重试。
// 所以异常带 Retryable，且这里的超时明确标成可重试。
//
// 结果放不下时标 Truncated 并如实上报。静默截断读起来就像"全部在这儿了"，
// 助手会据此下结论 —— 那比没有答案更糟。
internal sealed record ReaderQueryRequest(
    string Correlation,
    string SourceInstanceId,
    long SnapshotRevision,
    string File,
    string Query,
    JsonNode Parameters);

internal sealed record ReaderQueryResponse(
    string SessionId,
    string Correlation,
    string SourceInstanceId,
    long SnapshotRevision,
    string File,
    string Query,
    string Status,
    JsonElement Result,
    bool Truncated);

internal sealed class ReaderQueryException : Exception
{
    internal ReaderQueryException(
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

internal static class ReaderQueryProtocol
{
    internal const string QueryContract = "reader-query/1";
    internal const string EventName = "reader-query-request";
    internal const string ResponseType = "reader-query";
    internal const string PageCardDetailContract =
        "reader-page-card-detail/1";
    internal const string PageCardContentFormat =
        "application/vnd.bw-reader.card+json;version=1";
    internal const int MaximumPageCardChunkCodeUnits = 24 * 1024;
    internal const int MaximumPageCardChunkUtf8Bytes = 24 * 1024;

    // 两条约束，紧的是后一条：
    //
    //  · 传输：必须小于 rc-computer-voice.js 的 MAX_MESSAGE_BYTES
    //    （与 DirectBridgeContract.MaximumMessageBytes 同为 256 KiB）。
    //    否则两端各自看都合理、合起来永远发不出去 —— 最难查的那类失败。
    //  · **上下文**：结果整段进模型的上下文。40 KiB 已经约 12k token，
    //    再大就是拿用户的对话预算去换一份没人读完的列表。装不下时截断并
    //    标 Truncated，让助手知道该缩小范围重问，而不是以为自己看全了。
    //
    // 所以这个数字由第二条定，不是由帧限定。
    internal const int MaximumResultBytes = 40 * 1024;
    internal const int MaximumLearningCardResultBytes = 232 * 1024;
    internal const int MaximumResultDepth = 8;
    internal const int MaximumQueryTextCharacters = 256;

    // 每个名字都对应执行侧一个显式分支。这里不做前缀匹配也不接受通配：
    // 名单之外的名字只可能是错误或攻击，两种都该当场拒绝。
    internal static bool IsQuery(string value) =>
        value is "highlights" or "notes" or "search" or "toc"
            or "page-text" or "page-cards" or "page-card" or "lookup"
            or "learning-cards" or "learning-card" or "review-current";

    // 每个查询各自声明适用哪种阅读界面。一刀切放开会让助手在网页上问目录、
    // 在书里问网页锚点 —— 那些请求会走到执行侧才失败，错误信息也说不清缘由。
    // 在这里拒绝，助手拿到的是"这个界面不支持"，而不是一次无从解释的失败。
    //
    // web = 浏览器扩展所在的普通网页；pdf/epub = App 里打开的书。
    internal static bool IsQueryForSurface(string query, string kind) =>
        query switch
        {
            // 高亮两边都有：书里在 App 的本机库，网页在扩展的 webHighlightsV1
            "highlights" => kind is "pdf" or "epub" or "web",
            // 便签：书里在 App 本机库，网页在扩展的 __bwDocumentNotes
            // （scoped repository，同样不经 Pi）
            "notes" => kind is "pdf" or "epub" or "web",
            // 全书搜索、目录、按页取文，都以「书有页码结构」为前提
            // page-text 现在网页也有实现（扩展的 __bwWebPageText，字符层由
            // web-textlayer 提供）。search 仍只在书里 —— 网页没有全文索引，
            // 放行了也只会回 unsupported。
            "page-text" =>
                kind is "pdf" or "epub" or "web",
            "search" =>
                kind is "pdf" or "epub",
            "page-cards" => kind is "pdf",
            "page-card" => kind is "pdf",
            // The canonical learning-card repository is global to Reader, not
            // scoped to one page.  Manual/free and anchored cards therefore
            // remain addressable from every Reader surface.
            "learning-cards" or "learning-card" or "review-current" =>
                kind is "pdf" or "epub" or "web",
            "toc" => kind is "pdf",
            // 查词与界面无关
            "lookup" => kind is "pdf" or "epub" or "web",
            _ => false,
        };

    internal static object Event(ReaderQueryRequest request) =>
        new
        {
            contract = DirectBridgeContract.Contract,
            type = "event",
            @event = EventName,
            payload = new
            {
                contract = QueryContract,
                commandKind = "query",
                correlation = request.Correlation,
                sourceInstanceId = request.SourceInstanceId,
                snapshotRevision = request.SnapshotRevision,
                file = request.File,
                query = request.Query,
                @params = request.Parameters,
            },
        };

    internal static ReaderQueryResponse ValidateResponse(JsonElement message)
    {
        RequireExactFields(
            message,
            "contract",
            "type",
            "requestId",
            "sessionId",
            "correlation",
            "sourceInstanceId",
            "snapshotRevision",
            "file",
            "query",
            "status",
            "result",
            "truncated");
        if (
            RequiredString(message, "contract", 128)
                != DirectBridgeContract.Contract
            || RequiredString(message, "type", 64) != ResponseType
        )
        {
            throw Invalid("Reader 查询消息合同无效");
        }
        string sessionId = RequiredSafeId(message, "sessionId");
        _ = DirectPcmFrameCodec.ParseSessionId(sessionId);
        string correlation = RequiredSafeId(message, "correlation");
        string sourceInstanceId = RequiredSafeId(message, "sourceInstanceId");
        long snapshotRevision = RequiredInt64(message, "snapshotRevision");
        if (snapshotRevision < 0)
        {
            throw Invalid("Reader 查询 snapshotRevision 无效");
        }
        string file = RequiredString(message, "file", 4_096);
        string query = RequiredString(message, "query", 64);
        if (!IsQuery(query))
        {
            throw Invalid("Reader 查询名称不在名单内");
        }
        string status = RequiredString(message, "status", 64);
        if (status is not ("ok" or "unsupported" or "unavailable"))
        {
            throw Invalid("Reader 查询状态无效");
        }
        if (!message.TryGetProperty("truncated", out JsonElement truncated)
            || truncated.ValueKind
                is not (JsonValueKind.True or JsonValueKind.False))
        {
            throw Invalid("Reader 查询 truncated 无效");
        }
        if (!message.TryGetProperty("result", out JsonElement result)
            || result.ValueKind != JsonValueKind.Object)
        {
            throw Invalid("Reader 查询结果必须是对象");
        }
        // 执行侧构造结果，桥接不解释它的内容 —— 但必须限住它的体量与深度，
        // 否则一个页面就能让下游解析器陷进去。
        RequireBoundedJson(
            result,
            query is "learning-cards" or "learning-card" or "review-current"
                ? MaximumLearningCardResultBytes
                : MaximumResultBytes);
        if (query == "page-card" && status == "ok")
        {
            ValidatePageCardDetailResult(
                result,
                truncated.ValueKind == JsonValueKind.True);
        }
        return new ReaderQueryResponse(
            sessionId,
            correlation,
            sourceInstanceId,
            snapshotRevision,
            file,
            query,
            status,
            result.Clone(),
            truncated.ValueKind == JsonValueKind.True);
    }

    // page-card 是唯一把卡片源内容送进模型的查询，因此不能沿用“只限总大小、
    // 不解释结果”的宽松规则。这里同时钉住分块边界、序号/未绑定语义和内容格式；
    // 否则一个伪 next_offset 会让下一次读取跳过正文，或把自由卡伪装成第 N 张。
    internal static void ValidatePageCardDetailResult(
        JsonElement result,
        bool envelopeTruncated)
    {
        RequireExactFields(
            result,
            "contract",
            "page",
            "revision",
            "card",
            "content",
            "content_length",
            "offset",
            "next_offset",
            "truncated");
        if (RequiredString(result, "contract", 128) != PageCardDetailContract)
        {
            throw Invalid("Reader 单卡查询合同无效");
        }
        long page = RequiredSafeInteger(result, "page", 1, 10_000_000);
        _ = RequiredSafeInteger(result, "revision", 0, 9_007_199_254_740_991L);
        if (!result.TryGetProperty("card", out JsonElement card)
            || card.ValueKind != JsonValueKind.Object)
        {
            throw Invalid("Reader 单卡查询 card 无效");
        }
        ValidatePageCardIdentity(card, page);

        if (!result.TryGetProperty("content", out JsonElement contentValue)
            || contentValue.ValueKind != JsonValueKind.String)
        {
            throw Invalid("Reader 单卡查询 content 无效");
        }
        string content = contentValue.GetString() ?? string.Empty;
        if (content.Length > MaximumPageCardChunkCodeUnits
            || content.IndexOf('\0') >= 0
            || System.Text.Encoding.UTF8.GetByteCount(content)
                > MaximumPageCardChunkUtf8Bytes)
        {
            throw Invalid("Reader 单卡查询 content 超出分块上限");
        }
        long contentLength = RequiredSafeInteger(
            result,
            "content_length",
            0,
            9_007_199_254_740_991L);
        long offset = RequiredSafeInteger(
            result,
            "offset",
            0,
            9_007_199_254_740_991L);
        if (offset > contentLength)
        {
            throw Invalid("Reader 单卡查询 offset 越界");
        }
        long end = offset + content.Length;
        if (end > contentLength)
        {
            throw Invalid("Reader 单卡查询 content 越过总长度");
        }
        bool detailTruncated = RequiredBoolean(result, "truncated");
        if (detailTruncated != envelopeTruncated)
        {
            throw Invalid("Reader 单卡查询截断标志不一致");
        }
        if (!result.TryGetProperty("next_offset", out JsonElement nextValue))
        {
            throw Invalid("Reader 单卡查询缺 next_offset");
        }
        if (detailTruncated)
        {
            if (nextValue.ValueKind != JsonValueKind.Number
                || !nextValue.TryGetInt64(out long nextOffset)
                || nextOffset != end
                || nextOffset <= offset
                || nextOffset >= contentLength
                || nextOffset > 9_007_199_254_740_991L)
            {
                throw Invalid("Reader 单卡查询 next_offset 无效");
            }
        }
        else if (nextValue.ValueKind != JsonValueKind.Null || end != contentLength)
        {
            throw Invalid("Reader 单卡查询末块边界无效");
        }
    }

    private static void ValidatePageCardIdentity(JsonElement card, long page)
    {
        RequireExactFields(
            card,
            "id",
            "number",
            "kind",
            "type",
            "label",
            "bind",
            "unbound",
            "content_format");
        string id = RequiredString(card, "id", 96);
        if (id.Length < 2 || !id.All(character => character is
                >= 'A' and <= 'Z'
                or >= 'a' and <= 'z'
                or >= '0' and <= '9'
                or '_' or '-'))
        {
            throw Invalid("Reader 单卡查询 id 无效");
        }
        string kind = RequiredString(card, "kind", 16);
        string type = RequiredString(card, "type", 16);
        if (kind is not ("anki" or "card") || type != kind)
        {
            throw Invalid("Reader 单卡查询类型无效");
        }
        string label = RequiredString(card, "label", 120);
        if (label.IndexOf('\0') >= 0
            || RequiredString(card, "content_format", 128)
                != PageCardContentFormat)
        {
            throw Invalid("Reader 单卡查询标签或内容格式无效");
        }
        bool unbound = RequiredBoolean(card, "unbound");
        if (!card.TryGetProperty("number", out JsonElement number)
            || !card.TryGetProperty("bind", out JsonElement bind))
        {
            throw Invalid("Reader 单卡查询锚点字段缺失");
        }
        if (unbound)
        {
            if (number.ValueKind != JsonValueKind.Null
                || bind.ValueKind != JsonValueKind.Null)
            {
                throw Invalid("Reader 未绑定卡片不得伪造序号或锚点");
            }
            return;
        }
        if (number.ValueKind != JsonValueKind.Number
            || !number.TryGetInt64(out long visibleNumber)
            || visibleNumber is < 1 or > 1_000_000
            || bind.ValueKind != JsonValueKind.Object)
        {
            throw Invalid("Reader 已绑定卡片序号或锚点无效");
        }
        // 回执里的 bind：序号与原文二选一，可带 block。
        // 形状与 ReaderRealtimeOutput.ValidateCardBind 一致。
        // ⚠ 本文件的 RequireExactFields 是**全等**语义（没有可选版），
        //   所以这里就地判定：必需项必须在，其余项必须落在允许集里。
        RequireBindFields(
            bind,
            new[] { "kind", "page" },
            new[] { "from", "to", "text", "rev", "block" });
        if (RequiredString(bind, "kind", 32) != "page-chars"
            || RequiredSafeInteger(bind, "page", 1, 10_000_000) != page)
        {
            throw Invalid("Reader 单卡查询页锚不匹配");
        }
        long from = RequiredSafeInteger(bind, "from", 0, 1_000_000);
        long to = RequiredSafeInteger(bind, "to", 0, 1_000_000);
        if (to < from
            || !bind.TryGetProperty("text", out JsonElement text)
            || text.ValueKind != JsonValueKind.String
            || (text.GetString() ?? string.Empty).Length > 200
            || (text.GetString() ?? string.Empty).IndexOf('\0') >= 0)
        {
            throw Invalid("Reader 单卡查询字符锚无效");
        }
    }

    internal static bool PageCardResponseMatchesRequest(
        ReaderQueryRequest request,
        ReaderQueryResponse response)
    {
        if (request.Query != "page-card" || response.Status != "ok")
        {
            return true;
        }
        try
        {
            JsonElement result = response.Result;
            JsonElement card = result.GetProperty("card");
            JsonObject parameters = request.Parameters as JsonObject
                ?? throw Invalid("Reader 单卡查询参数无效");
            HashSet<string> allowed = new(
                ["page", "id", "number", "offset", "limit", "expectedRevision"],
                StringComparer.Ordinal);
            bool hasId = parameters.ContainsKey("id");
            bool hasNumber = parameters.ContainsKey("number");
            if (parameters.Any(pair => !allowed.Contains(pair.Key))
                || hasId == hasNumber
                || !TryNodeInt64(parameters["offset"], out long offset)
                || offset is < 0 or > 9_007_199_254_740_991L
                || !TryNodeInt64(parameters["limit"], out long limit)
                || limit is < 1 or > MaximumPageCardChunkCodeUnits
                || (offset > 0 && !parameters.ContainsKey("expectedRevision")))
            {
                throw Invalid("Reader 单卡查询参数无效");
            }
            if (parameters["expectedRevision"] is JsonNode expectedNode
                && (!TryNodeInt64(expectedNode, out long expectedRevision)
                    || expectedRevision is < 0 or > 9_007_199_254_740_991L
                    || result.GetProperty("revision").GetInt64()
                        != expectedRevision))
            {
                return false;
            }
            if (result.GetProperty("offset").GetInt64() != offset
                || (result.GetProperty("content").GetString() ?? string.Empty)
                    .Length > limit)
            {
                return false;
            }
            if (parameters["page"] is JsonNode requestedPage
                && (!TryNodeInt64(requestedPage, out long page)
                    || page is < 1 or > 10_000_000
                    || result.GetProperty("page").GetInt64() != page))
            {
                return false;
            }
            if (hasId)
            {
                string requestedId = parameters["id"]!.GetValue<string>();
                return requestedId.Length is >= 2 and <= 96
                    && requestedId.All(character => character is
                        >= 'A' and <= 'Z'
                        or >= 'a' and <= 'z'
                        or >= '0' and <= '9'
                        or '_' or '-')
                    && card.GetProperty("id").GetString() == requestedId;
            }
            return TryNodeInt64(parameters["number"], out long requestedNumber)
                && requestedNumber is >= 1 and <= 1_000_000
                && card.GetProperty("number").ValueKind == JsonValueKind.Number
                && card.GetProperty("number").GetInt64()
                    == requestedNumber;
        }
        catch (Exception exception) when (
            exception is InvalidOperationException
            or FormatException
            or KeyNotFoundException
            or DirectProtocolException)
        {
            return false;
        }
    }

    private static bool TryNodeInt64(JsonNode? node, out long value)
    {
        value = 0;
        if (node is not JsonValue scalar)
        {
            return false;
        }
        if (scalar.TryGetValue(out long longValue))
        {
            value = longValue;
            return true;
        }
        if (scalar.TryGetValue(out int intValue))
        {
            value = intValue;
            return true;
        }
        return false;
    }

    internal static void RequireBoundedJson(JsonElement value) =>
        RequireBoundedJson(value, MaximumResultBytes);

    internal static void RequireBoundedJson(
        JsonElement value,
        int maximumBytes)
    {
        int bytes = System.Text.Encoding.UTF8.GetByteCount(
            value.GetRawText());
        if (maximumBytes < 1 || maximumBytes > DirectBridgeContract.MaximumMessageBytes
            || bytes > maximumBytes)
        {
            throw Invalid("Reader 查询结果超出上限");
        }
        RequireDepth(value, 1);
    }

    private static void RequireDepth(JsonElement value, int depth)
    {
        if (depth > MaximumResultDepth)
        {
            throw Invalid("Reader 查询结果层级过深");
        }
        if (value.ValueKind == JsonValueKind.Object)
        {
            DirectJsonValidation.RequireNoDuplicateKeys(value);
            foreach (JsonProperty property in value.EnumerateObject())
            {
                RequireDepth(property.Value, depth + 1);
            }
        }
        else if (value.ValueKind == JsonValueKind.Array)
        {
            foreach (JsonElement item in value.EnumerateArray())
            {
                RequireDepth(item, depth + 1);
            }
        }
    }

    // 必需 + 可选两段式。RequireExactFields 是全等语义，对 bind 不适用 ——
    // bind 的 page-chars 分支里 from/to/text/rev/block 都是可选的。
    private static void RequireBindFields(
        JsonElement bind,
        string[] required,
        string[] optional)
    {
        if (bind.ValueKind != JsonValueKind.Object)
        {
            throw Invalid("Reader 卡片 bind 必须是对象");
        }
        DirectJsonValidation.RequireNoDuplicateKeys(bind);
        HashSet<string> allowed = new(required, StringComparer.Ordinal);
        foreach (string name in optional)
        {
            allowed.Add(name);
        }
        foreach (JsonProperty property in bind.EnumerateObject())
        {
            if (!allowed.Contains(property.Name))
            {
                throw Invalid("Reader 卡片 bind 含未知字段");
            }
        }
        foreach (string name in required)
        {
            if (!bind.TryGetProperty(name, out _))
            {
                throw Invalid("Reader 卡片 bind 缺少字段");
            }
        }
    }

    private static void RequireExactFields(
        JsonElement message,
        params string[] names)
    {
        if (message.ValueKind != JsonValueKind.Object)
        {
            throw Invalid("Reader 查询消息必须是对象");
        }
        DirectJsonValidation.RequireNoDuplicateKeys(message);
        HashSet<string> expected = new(names, StringComparer.Ordinal);
        int seen = 0;
        foreach (JsonProperty property in message.EnumerateObject())
        {
            if (!expected.Contains(property.Name))
            {
                throw Invalid("Reader 查询消息含未知字段");
            }
            seen += 1;
        }
        if (seen != expected.Count)
        {
            throw Invalid("Reader 查询消息缺少字段");
        }
    }

    private static string RequiredString(
        JsonElement message,
        string name,
        int maximumLength)
    {
        if (!message.TryGetProperty(name, out JsonElement value)
            || value.ValueKind != JsonValueKind.String)
        {
            throw Invalid($"Reader 查询 {name} 无效");
        }
        string text = value.GetString() ?? string.Empty;
        if (text.Length == 0 || text.Length > maximumLength)
        {
            throw Invalid($"Reader 查询 {name} 长度无效");
        }
        return text;
    }

    private static string RequiredSafeId(JsonElement message, string name)
    {
        string value = RequiredString(message, name, 128);
        if (!DirectBridgeContract.IsSafeId(value))
        {
            throw Invalid($"Reader 查询 {name} 无效");
        }
        return value;
    }

    private static long RequiredInt64(JsonElement message, string name)
    {
        if (!message.TryGetProperty(name, out JsonElement value)
            || value.ValueKind != JsonValueKind.Number
            || !value.TryGetInt64(out long parsed))
        {
            throw Invalid($"Reader 查询 {name} 无效");
        }
        return parsed;
    }

    private static long RequiredSafeInteger(
        JsonElement message,
        string name,
        long minimum,
        long maximum)
    {
        long value = RequiredInt64(message, name);
        if (value < minimum || value > maximum)
        {
            throw Invalid($"Reader 查询 {name} 范围无效");
        }
        return value;
    }

    private static bool RequiredBoolean(JsonElement message, string name)
    {
        if (!message.TryGetProperty(name, out JsonElement value)
            || value.ValueKind
                is not (JsonValueKind.True or JsonValueKind.False))
        {
            throw Invalid($"Reader 查询 {name} 无效");
        }
        return value.ValueKind == JsonValueKind.True;
    }

    private static DirectProtocolException Invalid(string message) =>
        new("BW_READER_QUERY_SCHEMA_INVALID", message, retryable: false);
}

internal sealed class ReaderQueryBroker
{
    private static readonly TimeSpan QueryTimeout = TimeSpan.FromSeconds(10);
    private const int MaximumPendingQueries = 4;

    private sealed class PendingQuery
    {
        internal PendingQuery(
            ReaderQueryRequest request,
            ReaderContextSourceLease lease)
        {
            Request = request;
            Lease = lease;
        }

        internal ReaderQueryRequest Request { get; }
        internal ReaderContextSourceLease Lease { get; }
        internal TaskCompletionSource<ReaderQueryResponse> Completion { get; }
            = new(TaskCreationOptions.RunContinuationsAsynchronously);
    }

    private readonly ReaderContextSourceRouter _router;
    private readonly object _gate = new();
    private readonly Dictionary<string, PendingQuery> _pending =
        new(StringComparer.Ordinal);

    internal ReaderQueryBroker(ReaderContextSourceRouter router)
    {
        _router = router;
    }

    /// 读操作也给一小段注册等待。
    ///
    /// ⚠ 在这之前读**比写更脆**：写有 2.5 秒注册窗口
    /// （ReaderRealtimeOutputBroker.SourceRegistrationWait）、取图也有，
    /// 唯独读是 TryGetLease 拿不到就立刻抛。于是 reader_page_text /
    /// reader_search / reader_toc 这些在网络抖一下的瞬间**必然失败**，
    /// 而它们恰恰是最常被调的。
    ///
    /// 读重试本来就是安全的（没有副作用），所以这里等一下纯赚。
    /// 上限跟写侧保持一致 —— 用户是站着等的，再长不如直接说失败。
    internal static readonly TimeSpan SourceRegistrationWait =
        TimeSpan.FromMilliseconds(2_500);

    private static readonly TimeSpan SourceRegistrationPoll =
        TimeSpan.FromMilliseconds(50);

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

    internal async Task<ReaderQueryResponse> RequestAsync(
        ReaderQueryRequest request,
        CancellationToken cancellationToken)
    {
        ReaderContextSourceLease? lease = await WaitForSourceAsync(
            request.SourceInstanceId,
            cancellationToken).ConfigureAwait(false);
        if (lease is null)
        {
            throw Failure(
                "BW_READER_QUERY_SOURCE_OFFLINE",
                "快照指定的 Reader 页面来源当前不在线（已等待 "
                    + $"{SourceRegistrationWait.TotalSeconds:0.#} 秒仍未注册；"
                    + _router.DescribeRegisteredSources() + "）",
                retryable: true);
        }
        PendingQuery pending = new(request, lease);
        lock (_gate)
        {
            if (_pending.Count >= MaximumPendingQueries)
            {
                throw Failure(
                    "BW_READER_QUERY_CAPACITY",
                    "Reader 查询仍在处理中",
                    retryable: true);
            }
            if (!_pending.TryAdd(request.Correlation, pending))
            {
                throw Failure(
                    "BW_READER_QUERY_DUPLICATE_PENDING",
                    "相同 Reader 查询仍在处理中",
                    retryable: true);
            }
        }
        try
        {
            try
            {
                await _router.SendAsync(
                    lease,
                    ReaderQueryProtocol.Event(request),
                    cancellationToken).ConfigureAwait(false);
            }
            catch (ReaderVisualDeliveryException exception)
            {
                throw Failure(
                    "BW_READER_QUERY_SOURCE_OFFLINE",
                    exception.Message,
                    retryable: true,
                    exception);
            }
            Task winner = await Task.WhenAny(
                pending.Completion.Task,
                lease.LeaseRetired,
                Task.Delay(QueryTimeout, cancellationToken))
                .ConfigureAwait(false);
            cancellationToken.ThrowIfCancellationRequested();
            if (winner == pending.Completion.Task)
            {
                return await pending.Completion.Task.ConfigureAwait(false);
            }
            if (winner == lease.LeaseRetired)
            {
                throw Failure(
                    "BW_READER_QUERY_SOURCE_OFFLINE",
                    "查询期间指定 Reader 页面来源已离线",
                    retryable: true);
            }
            // 读操作重试是安全的：再问一次不会改变书里的任何东西。
            // 这跟写入通道的"未知不重试"是相反的，两者不能共用一套语义。
            throw Failure(
                "BW_READER_QUERY_TIMEOUT",
                "Reader 查询超时",
                retryable: true);
        }
        finally
        {
            lock (_gate)
            {
                if (
                    _pending.TryGetValue(
                        request.Correlation,
                        out PendingQuery? current)
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
        ReaderQueryResponse response)
    {
        PendingQuery pending;
        lock (_gate)
        {
            if (
                !_pending.TryGetValue(response.Correlation, out pending!)
                || !ReferenceEquals(pending.Lease, lease)
            )
            {
                throw Failure(
                    "BW_READER_QUERY_NOT_PENDING",
                    "Reader 查询不存在或已过期",
                    retryable: false);
            }
            ReaderQueryRequest request = pending.Request;
            // 答案必须对得上问题。少了这一条，一个迟到的旧答复会被当成
            // 当前这本书的现状 —— 那正是"看起来有数据所以是对的"最坏的形态。
            if (
                request.SourceInstanceId != response.SourceInstanceId
                || request.SnapshotRevision != response.SnapshotRevision
                || request.File != response.File
                || request.Query != response.Query
            )
            {
                throw Failure(
                    "BW_READER_QUERY_IDENTITY_MISMATCH",
                    "Reader 查询来源、书目、版本或名称不匹配",
                    retryable: false);
            }
            if (!ReaderQueryProtocol.PageCardResponseMatchesRequest(
                    request,
                    response))
            {
                throw Failure(
                    "BW_READER_QUERY_RESULT_MISMATCH",
                    "Reader 单卡查询结果与页码、选择器或分块参数不匹配",
                    retryable: false);
            }
            _pending.Remove(response.Correlation);
            pending.Completion.TrySetResult(response);
        }
    }

    private static ReaderQueryException Failure(
        string code,
        string message,
        bool retryable,
        Exception? innerException = null) =>
        new(code, message, retryable, innerException);
}
