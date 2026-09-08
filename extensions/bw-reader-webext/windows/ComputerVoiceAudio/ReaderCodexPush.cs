using System.Buffers.Binary;
using System.IO.Pipes;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace BwReader.ComputerVoiceAudio;

/// 把「板面变了」主动推给 Codex 的当前任务（2026-09-09 Codex→Claude 交接）。
///
/// ## 为什么放在生产程序里，而不是另写一个读板转发的常驻程序
///
/// 板面渲染这一处**已经**算好了两件事：内容有没有变（`WriteIfChangedAsync`），
/// 以及慢板这一轮值不值得唤醒（`DecideSlowFlush` 的「祈使句立刻 / 纯上下文攒
/// ContextBatchSize 次」）。从这里推送，这两条政策白白继承；另写一个读板的
/// 程序就得把它们各自重新推导一遍，而两份实现迟早不一致 ——
/// 到那时的表现是推送和板子各说各话，且两边都不报错。
///
/// 顺带解决背压：板面是**整块内容**而不是增量，所以一阵突发最多产生一次
/// 落盘、一次推送。不需要另做合并。
///
/// ## 纪律（每条都对应交接里点名的一个坑）
///
/// - **只做运输，绝不碰业务状态。** `success=true` 只代表接口收下了，
///   不代表任务处理了、更不代表用户听到了。所以这里**永远不 ack** 任何
///   业务通知 —— 业务 ack/resolve 留在原处。提前 ack 的后果是通知消失而
///   人根本没被告知，且没有一处会报错。
/// - **板面文件仍是权威。** 推送只是让对面早知道；它拿到通知后照样去读
///   文件。所以推送挂在落盘**之后**，且推送失败不影响落盘。
/// - **默认关。** 消费端还在轮询，两条都开就是双发。开关翻开之前先改消费端。
/// - **地址和任务绑定必须动态。** 管道地址、Codex 安装路径、目标任务 id
///   一律不写死：那两个环境变量只有 Codex 亲自启动的进程才有，独立启动的
///   ReaderPC 拿不到（2026-09-09 实测）。所以由 AI 自己注册，见
///   `ReaderCodexEndpoint`。绑定死没死**由连续推送失败判定**，不由时钟 ——
///   时钟答不出目标活着与否，还会在长会话中途把活绑定杀掉。
/// - **不做重试。** 一次推送失败就算了：下一轮板面若仍与对面不同，
///   `WriteIfChangedAsync` 会再写一次并再推一次。自己攒重试队列会把
///   "同一件事说两遍"变成常态。
internal static class ReaderCodexPush
{
    /// 帧头是 4 字节小端长度，随后 UTF-8 的 JSON-RPC 2.0 正文。
    private const int MaxFrameBytes = 16 * 1024 * 1024;
    private const int ConnectTimeoutMs = 4000;
    private const int RequestTimeoutMs = 12000;
    /// 这条通道要传的工具名。找不到就放弃 —— 不猜别的名字。
    private const string ToolName = "send_message_to_thread";

    private static readonly object Gate = new();
    private static bool _enabled;
    private static string _lastNote = "尚未推送";
    private static long _sentCount;
    private static int _consecutiveFailures;

    /// 连续失败多少次就判这个绑定死了。
    ///
    /// ⚠ 判"目标还活着吗"用的是**这个**，不是时钟（2026-09-09 用户当场问了
    /// 那个 6 小时 TTL 的设计）：推失败了就是死了，这是实测；时钟只是猜。
    /// 不取 1 是因为要容一次抖动 —— Codex 重启那几秒里推送本来就会失败，
    /// 一次就判死会让它刚回来就被拒之门外。
    internal const int ConsecutiveFailureLimit = 5;

    /// 开关。**默认关**：消费端还在轮询时同时推送就是双发。
    internal static bool Enabled
    {
        get { lock (Gate) { return _enabled; } }
    }

    internal static void SetEnabled(bool value)
    {
        lock (Gate) { _enabled = value; }
    }

    /// 换了推送目标、或从失效里恢复时清零。不清的话上一个死绑定攒下的
    /// 失败次数会记在新目标头上，新目标可能一上来就被判死。
    internal static void ResetFailures()
    {
        lock (Gate) { _consecutiveFailures = 0; }
    }

    /// 最近一次尝试的结果。这条链没有界面，出问题只能靠它。
    internal static string LastNote
    {
        get { lock (Gate) { return _lastNote; } }
    }

    internal static long SentCount
    {
        get { lock (Gate) { return _sentCount; } }
    }

    private static void Note(string text)
    {
        lock (Gate)
        {
            _lastNote = DateTimeOffset.Now.ToString("HH:mm:ss") + " " + text;
        }
    }

    /// 刚接上时推一次**全量提醒**（2026-09-09 用户点出来的缺口）。
    ///
    /// > 现在既然已经变成了主动推送，ai 也就不会轮询快慢板内容，那现在板子上
    /// > 留着的比如现在通知之类的信息说白了也就没有机会送到 ai 那里，
    /// > 应该是每次连上时先把快慢板内容主动传输一次
    ///
    /// 说的对：推送只在**变化时**触发，而"接上之前就已经摆在板上的东西"
    /// 不构成变化。不补这一下，一条在他登记之前就建好的待办会一直躺着，
    /// 而两边都不会觉得有问题 —— 板上明明写着，推送也从没出错。
    ///
    /// ⚠ 与变化推送共用同一条运输和同一套失败判定；只有措辞不同：
    ///   这一条明说"这是接上时的一次，板上现有内容请整个读一遍"，
    ///   否则对面会以为只有增量。
    /// ⚠ 目标由**调用方**传进来，不在这里读全局绑定：这一条是异步发的，
    /// 中间全局绑定可能已被另一段对话覆盖，那样提醒就发到别人那里去了
    /// （2026-09-09 Codex 明确点出这一条）。
    internal static async Task NotifyConnectedAsync(
        ReaderCodexEndpoint.Binding binding,
        CancellationToken cancellationToken)
    {
        if (!Enabled) return;
        string prompt =
            "提示板已接上主动推送。"
            + "这是接上时的一次全量提醒：**把快板和慢板都完整读一遍**，"
            + "板上可能有你登记之前就已经存在的待办。"
            + "之后只有内容变化时才会再推。"
            + "板面文件是权威；业务 ack/resolve 仍按原契约，"
            + "本条不代表任何通知已交付用户。";
        try
        {
            await SendAsync(binding, prompt, cancellationToken)
                .ConfigureAwait(false);
            lock (Gate)
            {
                _sentCount++;
                _consecutiveFailures = 0;
            }
            Note("已推送（接上时的全量提醒）");
        }
        catch (Exception exception)
        {
            // 这一条失败**不判绑定失效**：刚登记完就判死太急，而且下一次
            // 真实变化会再试一次。只把原因留下。
            Note("接上提醒没发出去：" + exception.Message);
        }
    }

    /// 板面变了。哪块变了决定措辞，但**内容不在这条消息里** ——
    /// 对面照样去读板面文件，那才是权威。
    ///
    /// ⚠ 消息里只放"去看哪块板"，不放板面正文：正文里有书页标题和内容，
    /// 那是资料不是指令，塞进一条送给模型的提示里等于把资料升级成命令。
    internal static async Task NotifyBoardChangedAsync(
        bool slowChanged,
        bool fastChanged,
        string slowText,
        string fastText,
        CancellationToken cancellationToken)
    {
        if (!Enabled) return;
        if (!slowChanged && !fastChanged) return;
        ReaderCodexEndpoint.Binding? binding = ReaderCodexEndpoint.Current();
        if (binding is null)
        {
            string why = ReaderCodexEndpoint.InvalidReason();
            Note(why.Length > 0
                ? "绑定已被判失效，等重新登记：" + why
                : "没有可用的 Codex 绑定（未注册或已过兜底期限），这一轮不推");
            return;
        }
        // 直接把**变了那块板的全文**带过去（2026-09-09 用户：
        // 「直接把快慢板内容发过去就好，只是快板有变化就发快板的全部内容，
        //   慢板同理」）。
        //
        // 原来只发一句"有更新，去读文件"，于是对面每次还要再读一趟 ——
        // 而板面本来就短，那一趟纯属多余。文件留着当权威和回退面，
        // 但不必每次都回头读。
        //
        // ⚠ 只带**变了的那块**：没变的那块对面手上已经有了，
        // 再发一遍是同一件事说两遍。
        string which = slowChanged && fastChanged
            ? "快板和慢板"
            : (fastChanged ? "快板" : "慢板");
        var body = new StringBuilder();
        body.Append("提示板更新（").Append(which).Append("）。\n");
        if (fastChanged)
        {
            body.Append("\n【快板】\n").Append(Trim(fastText));
        }
        if (slowChanged)
        {
            body.Append("\n【慢板】\n").Append(Trim(slowText));
        }
        string prompt = body.ToString();
        try
        {
            await SendAsync(binding, prompt, cancellationToken)
                .ConfigureAwait(false);
            lock (Gate)
            {
                _sentCount++;
                _consecutiveFailures = 0;   // 成功一次就把计数清零
            }
            Note("已推送（" + which + "）");
        }
        catch (OperationCanceledException)
        {
            // 收摊，不算失败。
        }
        catch (Exception exception)
        {
            // 失败**要留下原因**，但不重试：下一轮板面若仍不同会再写再推。
            int failures;
            lock (Gate) { failures = ++_consecutiveFailures; }
            if (failures >= ConsecutiveFailureLimit)
            {
                // 连着这么多次都不成，那不是抖动，是目标没了。判绑定失效并
                // **把原因写进绑定文件** —— 这条链没有界面，原因丢了就等于
                // 没发生过，下次问"为什么不推了"会完全没有答案。
                ReaderCodexEndpoint.Invalidate(
                    "连续 " + failures + " 次推送失败：" + exception.Message);
                lock (Gate) { _consecutiveFailures = 0; }
                Note("连续 " + failures + " 次失败，已判绑定失效，等重新登记："
                     + exception.Message);
                return;
            }
            Note("推送失败（连续第 " + failures + " 次）：" + exception.Message);
        }
    }

    /// 板面正文进消息前的收口。板子本来就短，这里只防病态输入 ——
    /// 一块板长到几十 KB 说明渲染出了别的问题，那时截断比让对面吞下整块好。
    private static string Trim(string text)
    {
        string value = (text ?? string.Empty).TrimEnd();
        const int limit = 8000;
        return value.Length <= limit
            ? value
            : value[..limit] + "\n…（板面过长已截断，完整内容见板面文件）";
    }

    private static async Task SendAsync(
        ReaderCodexEndpoint.Binding binding,
        string prompt,
        CancellationToken cancellationToken)
    {
        using NamedPipeClientStream pipe = new(
            ".",
            binding.PipeName,
            PipeDirection.InOut,
            PipeOptions.Asynchronous);
        using CancellationTokenSource connect = CancellationTokenSource
            .CreateLinkedTokenSource(cancellationToken);
        connect.CancelAfter(ConnectTimeoutMs);
        await pipe.ConnectAsync(connect.Token).ConfigureAwait(false);

        // 先问一次工具表，用它**返回的 namespace**。猜 namespace 会在对面
        // 改分组时静默失效，而失效的表现只是"消息没到"。
        JsonNode catalog = await RequestAsync(
            pipe, 1, "tools/list",
            new JsonObject { ["threadStartKind"] = "all" },
            cancellationToken).ConfigureAwait(false);
        string? nameSpace = null;
        if (catalog["tools"] is JsonArray tools)
        {
            foreach (JsonNode? item in tools)
            {
                if (item is not JsonObject tool) continue;
                if ((string?)tool["name"] != ToolName) continue;
                nameSpace = (string?)tool["namespace"];
                break;
            }
        }
        if (string.IsNullOrEmpty(nameSpace))
        {
            throw new InvalidOperationException(
                ToolName + " 没有暴露给外部连接");
        }

        JsonNode result = await RequestAsync(
            pipe, 2, "tools/call",
            new JsonObject
            {
                ["arguments"] = new JsonObject
                {
                    ["threadId"] = binding.ThreadId,
                    ["prompt"] = prompt,
                },
                // callId 只是这次调用的标识，**不是业务幂等保证**。
                ["callId"] = "reader-board-" + Guid.NewGuid().ToString("n"),
                ["namespace"] = nameSpace,
                ["threadId"] = binding.ThreadId,
                ["tool"] = ToolName,
                ["turnId"] = "reader-board-push",
            },
            cancellationToken).ConfigureAwait(false);
        if (result["success"] is not JsonValue success
            || !success.TryGetValue(out bool accepted) || !accepted)
        {
            throw new InvalidOperationException("对面没有收下这条消息");
        }
    }

    private static async Task<JsonNode> RequestAsync(
        NamedPipeClientStream pipe,
        int id,
        string method,
        JsonObject parameters,
        CancellationToken cancellationToken)
    {
        JsonObject envelope = new()
        {
            ["jsonrpc"] = "2.0",
            ["id"] = id,
            ["method"] = method,
            ["params"] = parameters,
        };
        byte[] body = Encoding.UTF8.GetBytes(envelope.ToJsonString());
        byte[] frame = new byte[4 + body.Length];
        BinaryPrimitives.WriteUInt32LittleEndian(frame, (uint)body.Length);
        body.CopyTo(frame, 4);
        using CancellationTokenSource deadline = CancellationTokenSource
            .CreateLinkedTokenSource(cancellationToken);
        deadline.CancelAfter(RequestTimeoutMs);
        await pipe.WriteAsync(frame, deadline.Token).ConfigureAwait(false);
        await pipe.FlushAsync(deadline.Token).ConfigureAwait(false);

        // 按 id 配对，并且处理分包粘包：一次读到的可能是半帧，也可能是
        // 好几帧连在一起。只按"读一次得一帧"写会在真机上偶发解析失败。
        List<byte> buffer = new();
        byte[] chunk = new byte[8192];
        while (true)
        {
            int read = await pipe
                .ReadAsync(chunk, deadline.Token)
                .ConfigureAwait(false);
            if (read <= 0)
            {
                throw new InvalidOperationException("连接在收到回应前断开");
            }
            buffer.AddRange(chunk.AsSpan(0, read).ToArray());
            while (buffer.Count >= 4)
            {
                uint size = BinaryPrimitives.ReadUInt32LittleEndian(
                    buffer.GetRange(0, 4).ToArray());
                if (size > MaxFrameBytes)
                {
                    throw new InvalidOperationException("帧长超过上限");
                }
                if (buffer.Count < size + 4) break;
                byte[] payload = buffer.GetRange(4, (int)size).ToArray();
                buffer.RemoveRange(0, (int)size + 4);
                JsonNode? message = JsonNode.Parse(payload);
                if (message is not JsonObject reply) continue;
                if ((int?)reply["id"] != id) continue;   // 不是我等的那条
                if (reply["error"] is not null)
                {
                    throw new InvalidOperationException(
                        "对面返回错误：" + reply["error"]!.ToJsonString());
                }
                return reply["result"]
                    ?? throw new InvalidOperationException("回应里没有 result");
            }
        }
    }
}
