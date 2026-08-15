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
    internal const int MaximumResultDepth = 8;
    internal const int MaximumQueryTextCharacters = 256;

    // 每个名字都对应执行侧一个显式分支。这里不做前缀匹配也不接受通配：
    // 名单之外的名字只可能是错误或攻击，两种都该当场拒绝。
    internal static bool IsQuery(string value) =>
        value is "highlights" or "notes" or "search" or "toc"
            or "page-text" or "lookup";

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
            "search" or "page-text" => kind is "pdf" or "epub",
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
        RequireBoundedJson(result);
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

    internal static void RequireBoundedJson(JsonElement value)
    {
        int bytes = System.Text.Encoding.UTF8.GetByteCount(
            value.GetRawText());
        if (bytes > MaximumResultBytes)
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

    internal async Task<ReaderQueryResponse> RequestAsync(
        ReaderQueryRequest request,
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
                "BW_READER_QUERY_SOURCE_OFFLINE",
                "快照指定的 Reader 页面来源当前不在线（"
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
