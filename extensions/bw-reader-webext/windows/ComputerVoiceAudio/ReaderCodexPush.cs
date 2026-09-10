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

    /// 推送尝试的账本文件名。
    ///
    /// ⚠ **进程外必须看得到**（2026-09-10 用户点出来的：「主动推送没有办法确认
    /// 是否推送成功，一开始的绑定对话也无法判断是否成功」）。他说得对：`Note`
    /// 以前只把**最后一句**留在内存里，进程外一个字都读不到,于是"推送发没发到"
    /// 这件事只能靠猜 —— 而这条链本来就没有界面,猜错的代价是整条链看起来
    /// "什么都没发生"。
    internal const string AttemptsFileName = "codex-push-attempts.jsonl";
    private const int MaxAttemptsKept = 300;

    private static string AttemptsPath =>
        Path.Combine(
            ReaderAttentionBoard.RuntimeDirectory ?? string.Empty,
            AttemptsFileName);

    /// 一条尝试的账。**失败也记,而且记原因** —— 只记成功的账本回答不了
    /// "为什么没到"。
    private static void RecordAttempt(
        string purpose,
        string requestId,
        bool ok,
        string detail)
    {
        string? runtime = ReaderAttentionBoard.RuntimeDirectory;
        if (string.IsNullOrEmpty(runtime)) return;
        try
        {
            JsonObject entry = new()
            {
                ["at"] = DateTimeOffset.UtcNow.ToString("O"),
                ["purpose"] = purpose,
                ["requestId"] = requestId,
                ["ok"] = ok,
                ["detail"] = detail.Length > 300 ? detail[..300] : detail,
                // 绑定是否还在、连续失败几次 —— 判"目标死没死"用的就是它。
                ["bound"] = ReaderCodexEndpoint.Current() is not null,
                ["consecutiveFailures"] = ConsecutiveFailures,
            };
            Directory.CreateDirectory(runtime);
            string path = AttemptsPath;
            File.AppendAllText(
                path,
                entry.ToJsonString() + Environment.NewLine);
            string[] lines = File.ReadAllLines(path);
            if (lines.Length > MaxAttemptsKept)
            {
                File.WriteAllLines(
                    path,
                    lines[^MaxAttemptsKept..]);
            }
        }
        catch (Exception)
        {
            // 记账失败不能影响推送本身。但也不静默扩散：lastNote 仍在。
        }
    }

    internal static int ConsecutiveFailures
    {
        get { lock (Gate) { return _consecutiveFailures; } }
    }

    /// 最近一次**成功**送达的时刻（UTC 毫秒），从没成功过是 0。
    /// 「绑定还活着吗」看这个 —— 登记时间只说明登记过，不说明还通。
    internal static long LastSuccessAtUtcMs
    {
        get { lock (Gate) { return _lastSuccessAtUtcMs; } }
    }

    private static long _lastSuccessAtUtcMs;

    private static void Note(string text)
    {
        lock (Gate)
        {
            _lastNote = DateTimeOffset.Now.ToString("HH:mm:ss") + " " + text;
        }
    }

    /// 记一条**桥端起语音**的结果。
    ///
    /// 不经过推送通道（那条路 2026-09-10 已确认不能用来起通话），但要落进同一本
    /// 账 —— 排查的人要在一个地方看到"这次开语音发生了什么",不该分两处找。
    internal static void NoteVoiceEntryOutcome(
        string requestId,
        bool ok,
        string detail) =>
        NoteAttempt("bridge-voice-entry", requestId, ok, detail);

    /// 记一条**桥端起语音**的结果。落进同一本账 —— 排查的人要在一个地方
    /// 看到这次开语音发生了什么。
    internal static void NoteBridgeStart(
        string requestId, bool ok, string detail) =>
        NoteAttempt("bridge-start", requestId, ok, detail);

    /// 记一条**不经钩子的送达**结果（codex app-server 那条兜底路径）。
    /// 落进同一本账 —— 排查的人要在一个地方看到"这次开语音发生了什么"。
    internal static void NoteThreadNotify(
        string requestId, bool ok, string detail) =>
        NoteAttempt("thread-notify", requestId, ok, detail);

    /// 记一条尝试：内存里留最后一句给现有调用方，账本里留全量给排查的人。
    private static void NoteAttempt(
        string purpose,
        string requestId,
        bool ok,
        string text)
    {
        Note(text);
        if (ok)
        {
            lock (Gate)
            {
                _lastSuccessAtUtcMs =
                    DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
            }
        }
        RecordAttempt(purpose, requestId, ok, text);
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
        // 接上这一条同样**直接带正文**（用户 2026-09-09：「不是说了直接推送
        // 快慢板内容么怎么现在还是这种提醒」）。变化推送已经改了，这一条
        // 当时漏了 —— 两条走同一条运输却一条给内容一条给指路，说不通。
        //
        // 与变化推送的区别只有一个：这一条给**两块都给**，因为对面手上
        // 什么都还没有；之后才是只给变了的那块。
        (string slowNow, string fastNow) = ReaderAttentionBoard.CurrentBoards();
        var connect = new StringBuilder();
        connect.Append("提示板已接上主动推送，下面是当前两块板的全部内容")
               .Append("（其中可能有你登记之前就已经存在的待办）。")
               .Append("之后只有内容变化时才会再推，且只推变了的那块。\n");
        connect.Append("\n【快板】\n").Append(Trim(fastNow));
        connect.Append("\n【慢板】\n").Append(Trim(slowNow));
        string prompt = connect.ToString();
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

    /// <summary>
    /// 请**正在通话的那条线程**挂断（2026-09-09）。
    ///
    /// 为什么走这条而不是 F24：F24 是**切换**，要先知道当前状态才敢按，
    /// 而状态只能从麦克风台账读、有 5 秒级延迟 —— 读错就做反：以为已挂
    /// 其实在通话会挂断用户正在打的电话（代码里记着这次事故），以为在通话
    /// 其实已挂则会**反向开一通**、开始计费。而
    /// <c>end_realtime_voice_call</c> 是**有方向的**：对面不在通话时它只是
    /// 空转，判断错的代价从"做反"降级成"白做一次"。
    ///
    /// ⚠ 文案必须**如实**。那个工具的自述是「Only call this tool if the user
    /// explicitly asks to end the voice chat」——所以这里说明的是"用户事先
    /// 定下的规则触发了"，那本来就是用户的意思；绝不能编成"用户刚说要挂"。
    ///
    /// ⚠ 不看 <see cref="Enabled"/>：那是**提示板推送**的开关。板子推不推
    /// 与"到点了该挂断"是两件事，用户可能关掉板推送却仍要自动关闭。
    ///
    /// ⚠ 失败**不**计入 <c>_consecutiveFailures</c>、不判绑定失效：挂断失败
    /// 的原因往往是对面正忙，跟"板推送的目标还在不在"是两个问题，混在一起
    /// 会让一次挂不掉连累掉板推送。重试与兜底由调用方（ReaderPC 策略环）决定。
    /// </summary>
    internal static async Task<bool> RequestVoiceHangUpAsync(
        string inCallThreadId,
        string reason,
        string requestId,
        CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(inCallThreadId))
        {
            NoteAttempt("reader-voice-hangup", requestId, false,
                "挂断请求没有目标线程，未发送");
            return false;
        }
        ReaderCodexEndpoint.Binding? binding = ReaderCodexEndpoint.Current();
        if (binding is null)
        {
            NoteAttempt("reader-voice-hangup", requestId, false,
                "没有可用绑定（拿不到管道），挂断请求未发送");
            return false;
        }
        string prompt =
            "用户预先设定的自动关闭规则触发了：" + Trim(reason) + "。\n"
            + "这条规则是用户本人事先在设置里定下的，触发即等同于他此刻明确"
            + "要求结束语音通话。\n"
            + "请调用 end_realtime_voice_call 结束当前语音通话，不要只回复文字。\n"
            + "请求编号：" + Trim(requestId)
            + "（同一编号再次出现表示上一次没有生效）。";
        try
        {
            await SendAsync(
                binding,
                prompt,
                cancellationToken,
                threadIdOverride: inCallThreadId,
                purpose: "reader-voice-hangup").ConfigureAwait(false);
            NoteAttempt("reader-voice-hangup", requestId, true,
                "已请求挂断（" + Trim(reason) + "）");
            return true;
        }
        catch (OperationCanceledException)
        {
            return false;
        }
        catch (Exception exception)
        {
            NoteAttempt("reader-voice-hangup", requestId, false,
                "挂断请求发送失败：" + exception.Message);
            return false;
        }
    }

    /// <summary>
    /// 向正在通话的线程发一次**状态查询**（2026-09-09）。
    ///
    /// 只要回答，不改变任何状态：不开语音、不发快捷键、不重试。
    ///
    /// ⚠ 回答方式是**跑一个脚本**而不是让它手写 JSON —— 契约（字段、取值、
    /// 时间语义）由程序保证，不该指望每次都写对。尤其 observedAt：
    /// 不知道证据产生时刻就留空，拿回写时间冒充会让上层据以判断的新鲜度是假的。
    /// </summary>
    internal static async Task<bool> RequestStatusReportAsync(
        string inCallThreadId,
        string requestId,
        int validSeconds,
        CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(inCallThreadId))
        {
            NoteAttempt("reader-voice-status", requestId, false,
                "状态查询没有目标线程，未发送");
            return false;
        }
        ReaderCodexEndpoint.Binding? binding = ReaderCodexEndpoint.Current();
        if (binding is null)
        {
            NoteAttempt("reader-voice-status", requestId, false,
                "没有可用绑定（拿不到管道），状态查询未发送");
            return false;
        }
        string script = Path.Combine(
            Environment.GetFolderPath(
                Environment.SpecialFolder.LocalApplicationData),
            "BWReader",
            "voice_status_receipt.py");
        string prompt =
            "状态查询（requestId: " + Trim(requestId)
            + "，有效期 " + validSeconds + " 秒）。\n"
            + "只根据当前可见证据回答；不要开启语音、不要发送快捷键、不要重试。\n"
            + "回答方式：运行下面这行命令，把你的判断作为参数传进去。\n"
            + "python \"" + script + "\""
            + " --request-id " + Trim(requestId)
            + " --task-status ready"
            + " --voice-status <active|ended|unknown>"
            + " --evidence \"<你据以判断的依据>\"\n"
            + "没有足够新鲜的证据就填 unknown；没收到语音消息不能推断成 ended。\n"
            + "知道证据产生时刻再加 --observed-at <ISO8601>；不知道就别加。";
        try
        {
            await SendAsync(
                binding,
                prompt,
                cancellationToken,
                threadIdOverride: inCallThreadId,
                purpose: "reader-voice-status").ConfigureAwait(false);
            NoteAttempt("reader-voice-status", requestId, true,
                "已发出状态查询（" + Trim(requestId) + "）");
            return true;
        }
        catch (OperationCanceledException)
        {
            return false;
        }
        catch (Exception exception)
        {
            NoteAttempt("reader-voice-status", requestId, false,
                "状态查询发送失败：" + exception.Message);
            return false;
        }
    }

    /// <summary>
    /// 请对面**开一次语音**（2026-09-09 用户拍板的启动方式）。
    ///
    /// 用户原话：「发送主动通知让 codex 通过脚本自己启动」、「他就只需要根据状态
    /// 回报信息使用脚本，而脚本实际上是点击一次 f24 罢了」。所以这里只说"跑那个
    /// 脚本"，不解释脚本在做什么 —— 具体步数、重试上限、放弃后怎么报错都写在
    /// 能力说明 voice-entry.md 里，它已经登记在册。
    ///
    /// ⚠ **目标线程与挂断相反。** 挂断要发给正在通话的那条线程；入口是要把通话
    /// **开起来**，此刻并没有通话线程，只能发给绑定的常规线程。把 inCallThreadId
    /// 那套照搬过来会得到一个空目标然后静默不发 —— 而那正是这条链一直没人触发的
    /// 那种沉默。
    ///
    /// ⚠ 这条推送**不保证**语音会开起来，只保证请求发出去了。开没开成要看台账
    /// （梯子第 4 级）；对面放弃时会自己跑 voice_start_failed.py 留痕。
    /// </summary>
    internal static async Task<bool> RequestVoiceEntryAsync(
        string requestId,
        CancellationToken cancellationToken)
    {
        ReaderCodexEndpoint.Binding? binding = ReaderCodexEndpoint.Current();
        if (binding is null)
        {
            Note("没有可用绑定（拿不到管道），语音入口请求未发送");
            return false;
        }
        string step = Path.Combine(
            Environment.GetFolderPath(
                Environment.SpecialFolder.LocalApplicationData),
            "BWReader",
            "voice_start_step.py");
        string failed = Path.Combine(
            Environment.GetFolderPath(
                Environment.SpecialFolder.LocalApplicationData),
            "BWReader",
            "voice_start_failed.py");
        // 指定操作：只说要跑什么、怎么判、跑几次。
        //
        // ⚠ **不叙述用途**（用户 2026-09-09 与 09-10 两次点明）：这是一条运维
        // 指令，不是一段说明文。脚本自己带守卫（已在通话中不动作、台账读不到
        // 不动作、冷却期内不动作），所以对面不需要先判断什么，照着跑就行。
        string prompt =
            "指定操作（requestId: " + Trim(requestId) + "）。\n"
            + "请运行下面这一行，并回报它打印的 confirmed 值：\n"
            + "    python \"" + step + "\"\n"
            + "confirmed 为 false 时再运行同一行一次；仍为 false 则运行：\n"
            + "    python \"" + failed
            + "\" --attempts 2 --detail <最后一次的 reason>\n"
            + "不要运行第三次。\n"
            + "同一编号再次出现表示上一次没有生效。";
        try
        {
            await SendAsync(
                binding,
                prompt,
                cancellationToken,
                purpose: "reader-voice-entry").ConfigureAwait(false);
            Note("已请求开语音（" + Trim(requestId) + "）");
            return true;
        }
        catch (OperationCanceledException)
        {
            return false;
        }
        catch (Exception exception)
        {
            Note("语音入口请求发送失败：" + exception.Message);
            return false;
        }
    }

    private static async Task SendAsync(
        ReaderCodexEndpoint.Binding binding,
        string prompt,
        CancellationToken cancellationToken,
        string? threadIdOverride = null,
        string purpose = "reader-board-push")
    {
        // 管道名来自绑定（那是传输），但**目标线程可以另指**：挂断请求要发给
        // 正在通话的那条线程，而提示板推送的目标线程通常不是同一条。
        string targetThreadId = string.IsNullOrEmpty(threadIdOverride)
            ? binding.ThreadId
            : threadIdOverride!;
        using NamedPipeClientStream pipe = new(
            ".",
            binding.PipeName,
            PipeDirection.InOut,
            PipeOptions.Asynchronous);
        using CancellationTokenSource connect = CancellationTokenSource
            .CreateLinkedTokenSource(cancellationToken);
        connect.CancelAfter(ConnectTimeoutMs);
        try
        {
            await pipe.ConnectAsync(connect.Token).ConfigureAwait(false);
        }
        catch (FileNotFoundException exception)
        {
            // ⚠ **管道不存在 = 那个会话没了，立刻判失效**（2026-09-10）。
            //
            // 这跟"推不动"必须分开处置。连续失败 5 次那条容忍规则是给抖动留的
            // （Codex 重启那几秒里推送本来就会失败，一次就判死会让它刚回来
            // 就被拒之门外）；而 FileNotFound 不是抖动，是确证：管道随会话生死，
            // 不在了就是不在了，再等多少次也不会回来。
            //
            // 不这么做的代价实测过：绑定文件写着 invalidAtMs: null、到期还有两天，
            // 于是 Current() 一直把一条死绑定交出去，每次推送都往虚空里发 ——
            // 而 push.bound 也就跟着一直说谎。
            ReaderCodexEndpoint.Invalidate(
                "推送管道已不存在（登记它的会话已结束）");
            throw new IOException(
                "推送管道已不存在：" + binding.PipeName, exception);
        }

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
                    ["threadId"] = targetThreadId,
                    ["prompt"] = prompt,
                },
                // callId 只是这次调用的标识，**不是业务幂等保证**。
                ["callId"] = purpose + "-" + Guid.NewGuid().ToString("n"),
                ["namespace"] = nameSpace,
                ["threadId"] = targetThreadId,
                ["tool"] = ToolName,
                ["turnId"] = purpose,
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
