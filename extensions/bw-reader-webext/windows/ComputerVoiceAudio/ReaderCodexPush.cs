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

    /// <summary>进程启动时，从盘上的绑定把「推不推」接回来。</summary>
    /// <remarks>
    /// ⚠ 不这么做的表现是**静默停摆**：绑定还在、通话还在、板面照常更新，
    /// 就是没人推 —— 而账本一行都不会有（`if (!Enabled) return;` 在最前面）。
    /// 用户 2026-09-12 报的正是这个。
    ///
    /// 只在盘上明确写着 true 时才打开：读不到、没这个字段、或写着 false，
    /// 一律保持默认的关 —— 那个默认值是有意选的，不该被「恢复」顺手改掉。
    /// </remarks>
    internal static void RestoreEnabledFromBinding()
    {
        if (ReaderCodexEndpoint.EnabledOnDisk())
        {
            SetEnabled(true);
        }
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

    /// <summary>拿到可用绑定；没有就**自己派生**，而不是这一轮不推。</summary>
    /// <remarks>
    /// ⚠ 原来这里是"没绑定就记一笔、返回"，等 AI 那侧的钩子来登记。
    /// 用户 2026-09-11 点破：登记之后传过去的东西，和我们直接通知进去的
    /// **本质上是同一件事**（两条路最后都是 `send_message_to_thread`），
    /// 差别只在那两个参数从哪来。既然 `codex_channel.py` 自己就能发现并自证
    /// 管道、目标对话也能观测得到，"等对方登记"就不该是前置条件。
    ///
    /// 派生失败仍然照旧出声 —— 只是从"这一轮不推"变成"试过派生也不行"，
    /// 排查时这两句话指向完全不同的地方。
    /// </remarks>
    private static async Task<ReaderCodexEndpoint.Binding?>
        ResolveBindingAsync(
            string purpose,
            CancellationToken cancellationToken)
    {
        ReaderCodexEndpoint.Binding? binding = ReaderCodexEndpoint.Current();
        if (binding is not null) return binding;
        bool derived = await DirectBridgeProtocolSession
            .EnsureBindingAsync(cancellationToken).ConfigureAwait(false);
        binding = ReaderCodexEndpoint.Current();
        if (binding is not null) return binding;
        string why = ReaderCodexEndpoint.InvalidReason();
        NoteAttempt(
            purpose, "unbound", false,
            (why.Length > 0
                ? "绑定已被判失效：" + why
                : "没有可用的 Codex 绑定")
            + (derived
                ? "；自己派生过一次仍然没有"
                : "；派生这次被节流或没成"));
        return null;
    }

    /// 推送尝试的账本文件名。
    ///
    /// ⚠ **进程外必须看得到**（2026-09-10 用户点出来的：「主动推送没有办法确认
    /// 是否推送成功，一开始的绑定对话也无法判断是否成功」）。他说得对：`Note`
    /// 以前只把**最后一句**留在内存里，进程外一个字都读不到,于是"推送发没发到"
    /// 这件事只能靠猜 —— 而这条链本来就没有界面,猜错的代价是整条链看起来
    /// "什么都没发生"。
    internal const string AttemptsFileName = "codex-push-attempts.jsonl";
    /// 账本保留条数。
    ///
    /// ⚠ 2026-09-10 从 300 提到 1200：这本账现在也记 START（见
    /// RequestVoiceEntryIfNobodyElseWill 顶部那段）。一次刷屏能在半小时里
    /// 写进几百条，300 的话**刷屏本身会把它的前因后果一起挤掉** ——
    /// 而那正是要查的东西。1200 行 ≈ 240 KB，代价可以忽略。
    private const int MaxAttemptsKept = 1200;

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
        // ⚠ **必须串行**（2026-09-10 实测事故）。
        //
        // 这里原来没有锁：AppendAllText / ReadAllLines / WriteAllLines 三个
        // 无同步的文件操作。单线程时没事，可这条链一旦并发起来（那天有 191 个
        // 入口任务同时在跑），几乎每一次写都撞成 IOException —— 然后被下面那个
        // 静默的 catch 吞掉。表现是：**发出去 432 条，账本一条都没有**。
        //
        // 也就是说，账本恰好在最需要它的那一刻是哑的：并发失控本身就是它
        // 唯一能记录的证据，而并发正是让它写不进去的原因。
        lock (LedgerGate)
        {
            RecordAttemptWithinLock(runtime, purpose, requestId, ok, detail);
        }
    }

    private static readonly object LedgerGate = new();

    private static void RecordAttemptWithinLock(
        string runtime,
        string purpose,
        string requestId,
        bool ok,
        string detail)
    {
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

    /// <summary>
    /// 记一条**关于挂断的决定**（2026-09-10）。
    /// </summary>
    /// <remarks>
    /// 包括"决定不挂"。挂断这条链上一半的动作是**选择不动手**（意图为假但不是
    /// 这一代开的、台账读不到、F24 兜底关着），而这些在外面全都长成"什么都没
    /// 发生"。跟起语音落进同一本账：排查的人只该有一个地方要看。
    /// </remarks>
    internal static void NoteHangUpDecision(
        string requestId, bool ok, string detail) =>
        NoteAttempt("voice-hangup-decision", requestId, ok, detail);

    /// 记一条尝试：内存里留最后一句给现有调用方，账本里留全量给排查的人。
    /// 下一次入口推送跳过「最近那条语音对话」这个目标。
    /// 由入口循环在「对面没有收下这条消息」之后设。
    private static bool _skipEntryTargetOverride;

    internal static void ClearVoiceEntryTargetOverride() =>
        _skipEntryTargetOverride = true;

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
        // ⚠ **起语音途中的那次临时绑定不发板子**（2026-09-11 实测重复）。
        //
        // 一次按键现在要登记两回：先 ensure 绑一条能投递的（冷启动时只能是
        // 某条旧对话），语音起来后再 lock 到真正那条。每次登记都触发这条
        // 「接上时的全量提醒」，于是对面**同一次按键收到两遍**：
        //   02:17:30 → 01a088fd（临时那条）
        //   02:17:41 → 01a08c51（通话那条）
        // 用户截图里那两条一模一样的运维/状态消息就是这么来的。
        //
        // 那条临时绑定是**投递指令用的中转**，不是"有新 AI 接上了"。
        // 判据：起语音正在进行中、而且绑的还不是通话那条 —— 那就等它锁定。
        if (DirectBridgeProtocolSession.VoiceEntryInFlight)
        {
            string live = DirectCodexVoiceControl.InCallThreadIdIfActive();
            if (live.Length == 0
                || !string.Equals(binding.ThreadId, live,
                                  StringComparison.Ordinal))
            {
                NoteAttempt(
                    "board-push", "connect", true,
                    "起语音途中的临时绑定，接上提醒先不发（等锁定到通话那条）");
                return;
            }
        }
        // 接上这一条同样**直接带正文**（用户 2026-09-09：「不是说了直接推送
        // 快慢板内容么怎么现在还是这种提醒」）。变化推送已经改了，这一条
        // 当时漏了 —— 两条走同一条运输却一条给内容一条给指路，说不通。
        //
        // 与变化推送的区别只有一个：这一条给**两块都给**，因为对面手上
        // 什么都还没有；之后才是只给变了的那块。
        (string slowNow, string fastNow) = ReaderAttentionBoard.CurrentBoards();
        var connect = new StringBuilder();
        connect.Append(BoardSilenceLine);
        connect.Append("提示板已接上主动推送，下面是当前两块板的全部内容")
               .Append("（其中可能有你登记之前就已经存在的待办）。")
               .Append("之后只有内容变化时才会再推，且只推变了的那块。\n");
        connect.Append("\n【快板】\n").Append(Trim(fastNow));
        connect.Append("\n【慢板】\n").Append(Trim(slowNow));
        string prompt = connect.ToString();
        using IDisposable _whyConnect = Because("通道刚登记，补一次全量板");
        // ⚠ **提示板要发给正在通话的那条对话**（2026-09-11 用户点出来的）：
        //
        //   「建立通道后再让 ai 运行快捷键开启语音，可能会造成开启语音的
        //     对话和建立通道的对话不是同一对话」
        //
        // 说得对，而且我们这条链让它更容易发生：冷启动时先建通道（那会儿
        // 语音会话还不存在，只能绑到某条旧对话），语音一起来 Codex 新开一条
        // voice_chat 会话。于是板子推给了 A，人在跟 B 说话 —— 板上的焦点、
        // 待办、挂断提示，通话里那位一条都看不到，而两边都不报错。
        // references/codex-notification-channel.md 里已记过一次实测：
        // 绑定 01a0847a、通话 01a08560。
        //
        // ⚠ 挂断与状态查询**早就在用**这个机制（threadIdOverride），
        // 只有提示板漏了 —— 同一条道理的两份实现只改了一份，又一次。
        //
        // 管道来自绑定（那是传输），目标线程按现状选：在通话就跟着通话，
        // 不在通话（或读不到）就仍用绑定里那条 —— 设置页指定的那条。
        string boardTarget = DirectCodexVoiceControl.InCallThreadIdIfActive();
        // ⚠ **顺手把绑定纠正过来**（2026-09-11 实测）。
        //
        // 锁定那一步用的是侧栏同步的 lastGood，而它**跟得比通话慢**：
        //   02:48:47 通话起来了，通道已锁到它 → 01a08c64（上一通那条）
        //   02:49:56 已推送（快板）          → 01a08c6f（真正在通话的那条）
        // 一次性地在语音刚起来时锁一把，正好锁在它还没跟上的那一刻。
        //
        // 板面推送每次都会重算"现在在跟谁通话"，所以让它顺带发现并纠正 ——
        // 判据是观测到的事实（两者不同），不是又一个猜出来的等待时长。
        if (boardTarget.Length > 0
            && !string.Equals(boardTarget, binding.ThreadId,
                              StringComparison.Ordinal))
        {
            DirectBridgeProtocolSession.RequestChannelRelock(boardTarget);
        }
        try
        {
            await SendAsync(
                binding, prompt, cancellationToken,
                threadIdOverride: boardTarget.Length > 0 ? boardTarget : null)
                .ConfigureAwait(false);
            lock (Gate)
            {
                _sentCount++;
                _consecutiveFailures = 0;
            }
            // ⚠ 落盘，不只是留在内存（2026-09-10 用户问「你刚才为何连发
            // 两次」时才发现）：板面这两条推送原来只调 Note()，进程外一
            // 个字都读不到，于是「推了几次、什么时候推的、间隔多久」根本
            // 查不出来 —— 而这正是关于推送最常被问到的一类问题。语音那
            // 几条早就走 NoteAttempt 落盘了，这两条被漏下了。
            // ⚠ 这一条**就代表了两块板**：告诉板子它们已送达，紧随其后的
            // 板面推送因此不会把同样的内容再送一遍（用户点名的"复数通知"，
            // 最常见的就是这一对）。
            ReaderAttentionBoard.NoteFastBoardDelivered(fastNow);
            // ⚠ 记下**发给了哪条线程**：绑定那条与通话那条不是一回事
            // （Codex 每起一次语音就新建一条 voice_chat），不记就没法
            // 回答"板子到底进了谁的对话" —— 而那正是 2026-09-11 用户
            // 问的问题，当时我答不上来。
            NoteAttempt(
                "board-push", "connect", true,
                "已推送（接上时的全量提醒）→ " + BoardTargetNote(boardTarget, binding));
        }
        catch (Exception exception)
        {
            // 这一条失败**不判绑定失效**：刚登记完就判死太急，而且下一次
            // 真实变化会再试一次。只把原因留下。
            NoteAttempt(
                "board-push", "connect", false,
                "接上提醒没发出去：" + exception.Message);
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
        ReaderCodexEndpoint.Binding? binding =
            await ResolveBindingAsync("board-push", cancellationToken)
                .ConfigureAwait(false);
        if (binding is null) return;
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
        body.Append(BoardSilenceLine);
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
        // ⚠ 原因只说"为什么发"；"发给谁"由出站咽喉自己记（它那时才知道）。
        using IDisposable _why = Because("板面变了（" + which + "）");
        // ⚠ **提示板要发给正在通话的那条对话**（2026-09-11 用户点出来的）：
        //
        //   「建立通道后再让 ai 运行快捷键开启语音，可能会造成开启语音的
        //     对话和建立通道的对话不是同一对话」
        //
        // 说得对，而且我们这条链让它更容易发生：冷启动时先建通道（那会儿
        // 语音会话还不存在，只能绑到某条旧对话），语音一起来 Codex 新开一条
        // voice_chat 会话。于是板子推给了 A，人在跟 B 说话 —— 板上的焦点、
        // 待办、挂断提示，通话里那位一条都看不到，而两边都不报错。
        // references/codex-notification-channel.md 里已记过一次实测：
        // 绑定 01a0847a、通话 01a08560。
        //
        // ⚠ 挂断与状态查询**早就在用**这个机制（threadIdOverride），
        // 只有提示板漏了 —— 同一条道理的两份实现只改了一份，又一次。
        //
        // 管道来自绑定（那是传输），目标线程按现状选：在通话就跟着通话，
        // 不在通话（或读不到）就仍用绑定里那条 —— 设置页指定的那条。
        string boardTarget = DirectCodexVoiceControl.InCallThreadIdIfActive();
        // ⚠ **顺手把绑定纠正过来**（2026-09-11 实测）。
        //
        // 锁定那一步用的是侧栏同步的 lastGood，而它**跟得比通话慢**：
        //   02:48:47 通话起来了，通道已锁到它 → 01a08c64（上一通那条）
        //   02:49:56 已推送（快板）          → 01a08c6f（真正在通话的那条）
        // 一次性地在语音刚起来时锁一把，正好锁在它还没跟上的那一刻。
        //
        // 板面推送每次都会重算"现在在跟谁通话"，所以让它顺带发现并纠正 ——
        // 判据是观测到的事实（两者不同），不是又一个猜出来的等待时长。
        if (boardTarget.Length > 0
            && !string.Equals(boardTarget, binding.ThreadId,
                              StringComparison.Ordinal))
        {
            DirectBridgeProtocolSession.RequestChannelRelock(boardTarget);
        }
        try
        {
            await SendAsync(
                binding, prompt, cancellationToken,
                threadIdOverride: boardTarget.Length > 0 ? boardTarget : null)
                .ConfigureAwait(false);
            lock (Gate)
            {
                _sentCount++;
                _consecutiveFailures = 0;   // 成功一次就把计数清零
            }
            // 同上：板面推送也要落盘。带上是哪块板 —— 「为什么连推两次」
            // 的答案通常就是"两次变化各推一次"，而没有账本就只能靠猜。
            // 送达才算数：**判去重要用"真送到的"而不是"我们试过的"**。
            // 通道断着的时候每一次尝试都会失败，若那时就记成已推，
            // 通道恢复后这份内容就再也不会被送出去了。
            if (fastChanged) ReaderAttentionBoard
                .NoteFastBoardDelivered(fastText);
            NoteAttempt(
                "board-push", which, true,
                "已推送（" + which + "）→ "
                + BoardTargetNote(boardTarget, binding));
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
                NoteAttempt(
                    "board-push", "invalidated", false,
                    "连续 " + failures + " 次失败，已判绑定失效，等重新登记："
                     + exception.Message);
                return;
            }
            // 失败**尤其**要落盘：只记成功的账本回答不了"为什么没到"。
            NoteAttempt(
                "board-push", "failure", false,
                "推送失败（连续第 " + failures + " 次）：" + exception.Message);
        }
    }

    /// <summary>
    /// 板面推送的**开口纪律**（2026-09-10 用户：「除了明确通知的都应该静默」）。
    /// </summary>
    /// <remarks>
    /// 板子自己的合同一直是「陈述句就是资料，祈使句才是要你做的事」
    /// （用户 2026-08-30 定的形状），登记表里也写着待办才是「该开口说的事」。
    /// **但那份合同从来没被送到对面** —— 它只存在于
    /// `reader-attention-registry.json`，而没有任何东西要求它去读那个文件。
    /// 对面收到的原文只有「提示板更新（快板）。焦点从…转移到…。」，
    /// 于是它按对话处理，把每一次焦点转移都念了出来（用户 2026-09-10 实录）。
    ///
    /// ⚠ 纪律必须**长在消息里**：推送是唯一到达对面的东西，写在别处等于没写。
    /// 这正是 silent-failure-lessons.md 那条「面向 AI 的说明写反比没写更糟」
    /// 的邻居 —— 这里是压根没写。
    /// </remarks>
    internal const string BoardSilenceLine =
        "这是状态更新，不是对话：陈述句只用来更新你的理解，"
        + "**不要在通话里念出来、也不要回应这条消息**；"
        + "只有祈使句（待办）才是要你开口告诉他的事。\n";

    /// <summary>
    /// 运维指令的**开口纪律**（同上）。
    /// </summary>
    /// <remarks>
    /// 挂断 / 状态查询 / 指定操作三条都是运维动作，用户不需要听见任何一句。
    /// 尤其「指定操作」原来字面写着「并回报它打印的 confirmed 值」——
    /// 在通话里"回报"就是说出来，等于我们自己要求了那句噪音。
    ///
    /// ⚠ 这条纪律有**第二份实现**：Python 侧 codex_thread_notify 的
    /// OPERATION_SILENCE_LINE（app-server 那条兜底路径）。措辞必须一致，
    /// 有测试钉住 —— 两边都"有纪律"但要求不同，是更难发现的那种不一致。
    /// </remarks>
    internal const string OperationSilenceLine =
        "这是后台运维指令，不是对话：照做即可，"
        + "**全程不要在通话里提这件事，也不要念出任何结果**。\n";

    /// 板面正文进消息前的收口。板子本来就短，这里只防病态输入 ——
    /// 一块板长到几十 KB 说明渲染出了别的问题，那时截断比让对面吞下整块好。
    /// 这一条板子发去了哪条线程，以及那是不是通话那条。
    private static string BoardTargetNote(
        string boardTarget,
        ReaderCodexEndpoint.Binding binding)
    {
        string target = boardTarget.Length > 0
            ? boardTarget : binding.ThreadId;
        string head = target.Length > 13 ? target[..13] : target;
        return head + (boardTarget.Length > 0
            ? "（通话中的那条）" : "（绑定那条，此刻没有通话）");
    }

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
        ReaderCodexEndpoint.Binding? binding =
            await ResolveBindingAsync("reader-voice-hangup", cancellationToken)
                .ConfigureAwait(false);
        if (binding is null) return false;
        string prompt =
            OperationSilenceLine
            + "用户预先设定的自动关闭规则触发了：" + Trim(reason) + "。\n"
            + "这条规则是用户本人事先在设置里定下的，触发即等同于他此刻明确"
            + "要求结束语音通话。\n"
            + "请调用 end_realtime_voice_call 结束当前语音通话，不要只回复文字。\n"
            + "请求编号：" + Trim(requestId)
            + "（同一编号再次出现表示上一次没有生效）。";
        using IDisposable _whyHangUp = Because(
            "自动关闭触发：" + Trim(reason));
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
            // 取消也要留痕：静默返回让账本看起来像"一次都没试过"。
            NoteAttempt("reader-voice-hangup", requestId, false,
                "请求被取消（多半是预算耗尽或服务在停）");
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
        ReaderCodexEndpoint.Binding? binding =
            await ResolveBindingAsync("reader-voice-status", cancellationToken)
                .ConfigureAwait(false);
        if (binding is null) return false;
        string script = Path.Combine(
            Environment.GetFolderPath(
                Environment.SpecialFolder.LocalApplicationData),
            "BWReader",
            "voice_status_receipt.py");
        string prompt =
            OperationSilenceLine
            + "状态查询（requestId: " + Trim(requestId)
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
            // 取消也要留痕：静默返回让账本看起来像"一次都没试过"。
            NoteAttempt("reader-voice-status", requestId, false,
                "请求被取消（多半是预算耗尽或服务在停）");
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
        // ⚠ 这一条最要紧：入口指令原来"没绑定就不发"，于是冷启动时
        // 整条链卡在等 AI 登记（账本里那句「通道没能建立（Codex 可能还没
        // 加载完）」）。现在没绑定就自己派生一个再发。
        ReaderCodexEndpoint.Binding? binding =
            await ResolveBindingAsync("reader-voice-entry", cancellationToken)
                .ConfigureAwait(false);
        if (binding is null) return false;
        string step = Path.Combine(
            Environment.GetFolderPath(
                Environment.SpecialFolder.LocalApplicationData),
            "BWReader",
            "voice_start_step.py");
        // voice_start_failed.py 仍然随包分发，但现在由 voice_start_step.py
        // --report-failure 在进程内调用，不再让 AI 另起一次运行。
        // 指定操作：只说要跑什么、怎么判、跑几次。
        //
        // ⚠ **不叙述用途**（用户 2026-09-09 与 09-10 两次点明）：这是一条运维
        // 指令，不是一段说明文。脚本自己带守卫（已在通话中不动作、台账读不到
        // 不动作、冷却期内不动作），所以对面不需要先判断什么，照着跑就行。
        string prompt =
            OperationSilenceLine
            + "指定操作（requestId: " + Trim(requestId) + "）。\n"
            // ⚠ 只给**一行**，而且要说清"等它自己结束"（2026-09-13）。
            //   会话记录里这条链原来要 3–6 个模型回合：跑一次 → exec 到点先
            //   回来 → AI 每 5 秒空写 stdin 看结束没（170 次、中位 5 s）→
            //   false 再跑 → 还不行跑第三个脚本。重试与上报现在都在脚本里。
            + "请运行下面这一行**一次**，它自己会重试并在放弃时上报，"
            + "你不用再做别的：\n"
            + "    python \"" + step + "\" --attempts 2 --report-failure\n"
            + "它最多要跑一分钟：exec_command 的 yield_time_ms 给 120000，"
            + "等它自己结束；**不要**用 write_stdin 反复查看，"
            + "也不要再运行第二次。\n"
            + "同一编号再次出现表示上一次没有生效。";
        try
        {
            // ⚠ **发给最近那条语音对话，不是绑定那条**（2026-09-11 实测）。
            //
            // 用户：「之前测试时是可以打开指定对话的语音的，而且本身软件的
            // 设计也是语音快捷键按下时默认打开最近的语音对话，为何现在变成
            // 打开新的对话了，是你的脚本问题吧」——是。
            //
            // 「指定操作」这条推送是**送进某条对话**的，Codex 在那条里跑脚本、
            // 触发 F24。送进**最近那条语音对话** → 它续上；送进一条别的
            // （比如按标题绑到的昨天那条）→ 它新开一条。
            //
            // 实录对照：
            //   16:52 / 16:56 / 19:38  绑定 = 01a08a2f（当时正在用的那条）
            //                          → 三通全部复用同一条对话
            //   09-11 那五次           绑定 = 01a088fd（昨天 10:45 的旧对话，
            //                          因为新建的 voice_chat 都没有标题，
            //                          mode:title 只能落在旧的上）
            //                          → 每一次都新开
            //
            // 我之前把这条写成"必须发给绑定那条"，理由是"发它时还没有通话"。
            // 前半句对、后半句错：没有**正在进行**的通话，但**最近那条**一直在，
            // 而那正是 F24 会续上的那条。
            //
            // ⚠ 这里用不加通话守卫的 InCallThreadId()（lastGood）——要的就是
            // "最后一条好的"，散场之后仍然是它。这跟提示板那边**故意相反**：
            // 板子要发给活着的通话，入口要发给将要被续上的那条。
            // 上一轮若是「对面没有收下」（目标线程已死），这一轮别再发给它。
            string entryTarget = _skipEntryTargetOverride
                ? string.Empty
                : DirectCodexVoiceControl.InCallThreadId();
            _skipEntryTargetOverride = false;
            await SendAsync(
                binding,
                prompt,
                cancellationToken,
                threadIdOverride: entryTarget.Length > 0 ? entryTarget : null,
                purpose: "reader-voice-entry").ConfigureAwait(false);
            NoteAttempt("reader-voice-entry", requestId, true,
                "已请求开语音（" + Trim(requestId) + "）→ "
                + (entryTarget.Length > 0
                    ? entryTarget[..Math.Min(13, entryTarget.Length)]
                      + "（最近那条语音对话）"
                    : "绑定那条（还没有过语音对话）"));
            return true;
        }
        catch (OperationCanceledException)
        {
            // 取消也要留痕：静默返回让账本看起来像"一次都没试过"，
            // 而那正是 2026-09-10 那一晚查不动的原因之一。
            NoteAttempt("reader-voice-entry", requestId, false,
                "语音入口请求被取消（多半是预算耗尽）");
            return false;
        }
        catch (Exception exception)
        {
            NoteAttempt("reader-voice-entry", requestId, false,
                "语音入口请求发送失败：" + exception.Message);
            return false;
        }
    }

    /// <summary>
    /// 出站闸：**同一时刻只发一条，且两条之间留出间隔**
    /// （2026-09-10 用户：「重试后不希望通知积压在通道连通后输出复数通知」）。
    /// </summary>
    /// <remarks>
    /// 对面每收到一条消息就跑一整轮（实测 ≈11 秒 + 额度）。通道断开期间
    /// 各条链各自在等：入口重试每 10 秒试一次、板面变化各自等窗口、接上时
    /// 还要补一条全量板 —— 通道一恢复，它们会在同一秒里全部成功，于是对面
    /// 连着跑好几轮，而每一轮都是刚才那些消息的**过期版本**。
    ///
    /// ⚠ 闸只保证**间隔**，不保证顺序公平，也不排队积压：等在闸上的调用
    /// 拿到的是各自当时的正文，谁先进谁先发。真正防积压的是各条链自己的
    /// 去重（板面比"上一次真送到的内容"、入口比台账），闸只负责别让它们
    /// 挤在同一秒。
    /// </remarks>
    internal static readonly TimeSpan OutboundMinimumGap =
        TimeSpan.FromSeconds(6);

    private static readonly SemaphoreSlim OutboundGate = new(1, 1);
    private static DateTime _lastOutboundAtUtc = DateTime.MinValue;

    /// <summary>
    /// 这一条是**为什么**发出去的。由调用方在发之前设，出站咽喉照抄进账本。
    /// </summary>
    /// <remarks>
    /// ⚠ 用户 2026-09-11：「每个动作都该带上触发的原因和记录，我们不记录
    /// 无法分析多次发送指令的原因」。
    ///
    /// 那一夜最难堪的一次就是这个：02:19–02:28 对面收到 6 条入口指令，而账本
    /// 里**一行都没有** —— 我只能说"不知道是谁发的"。原因是记账挂在各个
    /// 包装函数上，绕过包装的路子就不留痕。
    ///
    /// 现在挪到唯一的出站咽喉：**任何消息出去都留一行，带上 purpose、目标
    /// 线程、和这个 cause**。谁发的、为什么发、发给谁，一次全在。
    ///
    /// ⚠ AsyncLocal 而不是参数：cause 要跨越"入口循环 → 请求函数 → 发送"
    /// 好几层，一路当参数传会让每个中间层都有机会忘掉它 ——
    /// 而忘掉的那条正是将来要查的那条。
    /// </remarks>
    private static readonly System.Threading.AsyncLocal<string?> _cause = new();

    internal static IDisposable Because(string cause)
    {
        string? previous = _cause.Value;
        _cause.Value = cause;
        return new CauseScope(previous);
    }

    private sealed class CauseScope(string? previous) : IDisposable
    {
        public void Dispose() => _cause.Value = previous;
    }

    /// <summary>把用户**打字说的话**送进正在通话的那条对话。</summary>
    /// <remarks>
    /// 用户 2026-09-11：「电脑语音模式时的输入框其实一直都没有设计和利用起来过，
    /// 现在既然已经有了稳定的注入内容的途径，就可以把这个输入框利用起来了」。
    ///
    /// ⚠ **这条不带静默纪律**，跟这个文件里其它五条外发正好相反。
    /// 板面和运维指令都要求"不要在通话里念出来、不要回应"，因为那是状态同步；
    /// 而这一条**是他在说话**，要的就是对面像他开口一样正常回答。
    /// 复用错前缀的后果是最难查的那种：送到了，对面却按纪律故意不吭声。
    ///
    /// 前缀用最短的「来自用户：」（他定的）：不写指令、不解释、不加条件 ——
    /// 多一句话就多一分被当成"运维指令"对待的可能。
    ///
    /// ⚠ 目标是**正在通话的那条**，不是绑定那条：绑定可能还停在上一条
    /// （今天实测过这种偏差）。读不到在通话哪条时才退回绑定。
    /// </remarks>
    internal static async Task<bool> SendTypedAsync(
        string text,
        string requestId,
        CancellationToken cancellationToken)
    {
        string body = (text ?? string.Empty).Trim();
        if (body.Length == 0)
        {
            NoteAttempt("reader-user-typed", requestId, false, "空文本，不发");
            return false;
        }
        ReaderCodexEndpoint.Binding? binding =
            await ResolveBindingAsync("reader-user-typed", cancellationToken)
                .ConfigureAwait(false);
        if (binding is null) return false;
        string target = DirectCodexVoiceControl.InCallThreadIdIfActive();
        using IDisposable _why = Because("用户在输入框里打字");
        try
        {
            await SendAsync(
                binding,
                "来自用户：" + body,
                cancellationToken,
                threadIdOverride: target.Length > 0 ? target : null,
                purpose: "reader-user-typed").ConfigureAwait(false);
            NoteAttempt(
                "reader-user-typed", requestId, true,
                "已把打字内容送进通话（" + body.Length + " 字）→ "
                + (target.Length > 0
                    ? BoardTargetNote(target, binding)
                    : "绑定那条（读不到通话在哪条）"));
            return true;
        }
        catch (Exception error)
        {
            NoteAttempt(
                "reader-user-typed", requestId, false,
                "打字内容没送出去：" + error.Message);
            return false;
        }
    }

    /// 出站口封死了没有。**只在 --self-test 进程里为真。**
    private static volatile bool _outboundSealed;

    /// <summary>把这个进程的出站口封死。只给自检用。</summary>
    /// <remarks>
    /// ⚠ 2026-09-12 的事故：跑了 5 轮 `--self-test`，用户对话里就多了四条
    /// 「开语音」的运维指令 —— 他直接来问「你现在在测试什么东西么」。
    ///
    /// 病根是**测试和生产共用一个出站口**：`ReaderCodexPush` 是静态的，
    /// 绑定读的是 `LocalAppData\BWReader` 里那份真的，于是自检里任何一条
    /// 走到语音入口逻辑的路径，发的都是真消息。
    ///
    /// 所以封的是**口**，不是某几条 check。逐条绕开的话，将来新写的 check
    /// 默认仍然是会外发的 —— 而这次出事的恰恰是一条我没意识到会走到那里的
    /// 路径。默认必须是"不发"，要发得先解释自己不是自检。
    /// </remarks>
    internal static void SealOutboundForSelfTest()
    {
        _outboundSealed = true;
    }

    /// <summary>自检进程里出站被封死时抛的东西。</summary>
    internal sealed class OutboundSealedException : InvalidOperationException
    {
        internal OutboundSealedException()
            : base("自检进程：出站口已封死，这条没有发出去")
        {
        }
    }

    private static async Task SendAsync(
        ReaderCodexEndpoint.Binding binding,
        string prompt,
        CancellationToken cancellationToken,
        string? threadIdOverride = null,
        string purpose = "reader-board-push")
    {
        string target = string.IsNullOrEmpty(threadIdOverride)
            ? binding.ThreadId
            : threadIdOverride!;
        string why = _cause.Value ?? "（调用方没说为什么 —— 这本身是个 bug）";
        if (_outboundSealed)
        {
            // **抛，而不是悄悄返回**：静默成功会在账本里写下"已推送"，
            // 那是一句假话；而调用方的 catch 本来就会把失败如实记下来。
            OutboundSealedException sealed_ = new();
            NoteOutbound(purpose, target, why, false, sealed_.Message);
            throw sealed_;
        }
        try
        {
            await SendTracedAsync(
                binding, prompt, cancellationToken,
                threadIdOverride, purpose).ConfigureAwait(false);
            NoteOutbound(purpose, target, why, true, "");
        }
        catch (Exception exception)
        {
            NoteOutbound(purpose, target, why, false,
                         exception.GetType().Name + "：" + exception.Message);
            throw;
        }
    }

    /// 出站流水：**每一条消息一行**，与各个包装函数的"结果账"分开。
    /// 前者回答"谁发了什么、为什么"，后者回答"这次动作成没成"。
    private static void NoteOutbound(
        string purpose, string target, string why, bool ok, string detail)
    {
        string head = target.Length > 13 ? target[..13] : target;
        NoteAttempt(
            "outbound/" + purpose,
            head,
            ok,
            "因为「" + why + "」→ " + head
            + (detail.Length > 0 ? "；" + detail : ""));
    }

    private static async Task SendTracedAsync(
        ReaderCodexEndpoint.Binding binding,
        string prompt,
        CancellationToken cancellationToken,
        string? threadIdOverride,
        string purpose)
    {
        // ⚠ **时间敏感的那几条连队都不排**（2026-09-11 第二轮修）。
        //
        // 上一版只免了"间隔"，没免"排队"：板面推送握着这把闸做完整趟管道
        // I/O（连接 4s + 请求最多 12s），起语音只能等在后面。实测 02:15 那次
        //   02:15:15 通道重建 → 02:15:21 全量板（占闸）→ 02:15:26 才轮到起语音
        // 29 秒总时长里，光排队就吃掉 11 秒。
        //
        // 闸的本意是"别让几条挤在同一秒送到对面"，那说的是板面。起语音/挂断/
        // 状态查询各自开自己的管道连接，彼此不冲突，也没有"挤成一堆"的问题 ——
        // 有人在等结果的动作不该给不急的让路。
        bool paced = purpose.StartsWith(
            "reader-board", StringComparison.Ordinal);
        if (!paced)
        {
            await SendWithinGateAsync(
                binding, prompt, cancellationToken,
                threadIdOverride, purpose).ConfigureAwait(false);
            _lastOutboundAtUtc = DateTime.UtcNow;
            return;
        }
        await OutboundGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            // ⚠ **时间敏感的那几条不排队等间隔**（2026-09-11 实测）。
            //
            // 间隔是为了防"通道恢复时几条挤成一堆"，那说的是**板面**推送。
            // 可它一视同仁之后就卡在了起语音的关键路径上：01:52 那次实测
            //   01:52:16 通道重建 → 01:52:22 接上时的全量板（+6s）
            //   → 01:52:28 才轮到起语音（又 +6s）
            // 30 秒总时长里 **12 秒是这个间隔**，而用户的原话是"好像在那里
            // 等待了很多秒"。
            //
            // 起语音、挂断、状态查询都是**有人在等结果**的动作；板面推送不是。
            // 所以只有板面排队。
            TimeSpan since = DateTime.UtcNow - _lastOutboundAtUtc;
            if (since < OutboundMinimumGap)
            {
                await Task.Delay(
                    OutboundMinimumGap - since,
                    cancellationToken).ConfigureAwait(false);
            }
            await SendWithinGateAsync(
                binding,
                prompt,
                cancellationToken,
                threadIdOverride,
                purpose).ConfigureAwait(false);
        }
        finally
        {
            // ⚠ 成败都记：一次失败的发送同样占用了对面的管道与我们的时间，
            // 紧接着再发一条并不会更成功，只会更挤。
            _lastOutboundAtUtc = DateTime.UtcNow;
            OutboundGate.Release();
        }
    }

    private static async Task SendWithinGateAsync(
        ReaderCodexEndpoint.Binding binding,
        string prompt,
        CancellationToken cancellationToken,
        string? threadIdOverride,
        string purpose)
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
        catch (OperationCanceledException)
            when (!cancellationToken.IsCancellationRequested)
        {
            // ⚠ **连接超时才是"管道不在"的真信号**（2026-09-10 实测）。
            //
            // 我原本以为管道不存在会抛 FileNotFoundException，于是把判失效挂在
            // 那上面 —— 而 NamedPipeClientStream.ConnectAsync **不会立刻抛**，
            // 它会一直等到有实例可用或超时。所以那条判失效永远不触发，
            // 账本里看到的是八次"被取消"，每次干等满 4 秒。
            //
            // 这里用 `when (!cancellationToken.IsCancellationRequested)` 把
            // **我们的连接超时**与**调用方取消**分开：前者是确证（那个会话没了），
            // 后者是我们自己在收摊，两者处置完全不同。
            ReaderCodexEndpoint.Invalidate(
                "推送管道连不上（" + ConnectTimeoutMs + " 毫秒内没有可用实例）");
            throw new IOException(
                "推送管道连不上：" + binding.PipeName);
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
