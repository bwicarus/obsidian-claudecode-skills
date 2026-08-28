namespace BwReader.ComputerVoiceAudio;

/// 验证提示板那条**支点规矩**：状态没变，输出一个字节都不许变。
///
/// ## 为什么这条值得一个自检
///
/// 对面是「文件一变就读取并判断一次」。所以一次没有情报的变化 =
/// 白花它一次读取和一次判断。而"多出一次变化"是**看不见的**故障：
/// 板子内容照样对，只是它读得比该读的次数多 —— 没有任何症状会提示你。
///
/// 这类错误最容易从两个地方溜进来，两个都被下面钉住了：
///   ① 往输出里加一个随时间走字的量（时钟、"已经 N 分钟"）
///   ② 让渲染带上副作用（读一次就推进一个状态），于是自己咬自己
///
/// ⚠ 这里的负对照同样重要：还要证明**该变的时候真的会变**。
/// 只证明"不变"的话，一个永远返回常量的实现也能通过。
internal static class ReaderAttentionBoardSelfTest
{
    internal static void Run(ICollection<string> checks)
    {
        DateTimeOffset t0 = DateTimeOffset.UnixEpoch;
        ReaderAttentionBoard.ResetForSelfTest();

        // ① 同样的状态读两遍必须完全一样（含"读了一遍"之后）。
        // ⚠ 两个时刻要在**每一个时间单位上都不同**（时/分/秒/毫秒）。
        // 第一版取的是整分钟偏移,于是"秒"这一位在两次渲染里都是 0 ——
        // 真有一个按秒走的时钟漏进输出,这条检查也照样通过。
        // 实测过:故意塞 now.Second 进去,那一版没抓到。
        string first = ReaderAttentionBoard.RenderForSelfTest(t0);
        string second = ReaderAttentionBoard.RenderForSelfTest(
            t0 + new TimeSpan(0, 3, 37, 13, 421));
        if (!string.Equals(first, second, StringComparison.Ordinal))
        {
            throw new InvalidOperationException(
                "提示板输出随时间变化 —— 每一次这样的变化都会白花对面一次读取");
        }
        checks.Add("attention-board-stable-across-time");

        // ⑦ 最常见的那一份必须**很小**。
        //
        // 用户 2026-08-29：「尽量减少信息量免得他每次都过度思考」。
        // 而板子会随着功能增加悄悄长胖 —— 长胖没有任何症状，
        // 只是它每次多想一点、多花一点。所以给它一个上限，
        // 越过就在这里停下，而不是等到某天发现它在读一篇文章。
        if (first.Length > 200)
        {
            throw new InvalidOperationException(
                "空板子已经 " + first.Length + " 字，超出 200 —— "
                + "不变的规则该放进 skill，不该每次重发一遍：" + first);
        }
        checks.Add("attention-board-idle-is-small:" + first.Length);

        // ② 重报同样的内容不算变化。
        ReaderAttentionBoard.Assert(
            "cards", "现在有 20 张到期", "问一句要不要现在复习",
            mustSpeak: true, TimeSpan.FromMinutes(30));
        long afterFirst = ReaderAttentionBoard.Health().Sequence;
        ReaderAttentionBoard.Assert(
            "cards", "现在有 20 张到期", "问一句要不要现在复习",
            mustSpeak: true, TimeSpan.FromMinutes(30));
        if (ReaderAttentionBoard.Health().Sequence != afterFirst)
        {
            throw new InvalidOperationException(
                "重报同样的内容被当成了变化 —— 续命会变成持续烧额度");
        }
        checks.Add("attention-board-reassert-is-not-a-change");

        // ③ 负对照：内容真变了，必须变。
        ReaderAttentionBoard.Assert(
            "cards", "现在有 31 张到期", "问一句要不要现在复习",
            mustSpeak: true, TimeSpan.FromMinutes(30));
        if (ReaderAttentionBoard.Health().Sequence == afterFirst)
        {
            throw new InvalidOperationException(
                "内容变了却没记成变化 —— 那样它永远不会来读新情报");
        }
        checks.Add("attention-board-real-change-is-a-change");

        // ④ 停留门槛：没待够就不算换了地方。
        ReaderAttentionBoard.ResetForSelfTest();
        ReaderAttentionBoard.NoteLocation("doc-a", "书 A", "", t0);
        ReaderAttentionBoard.NoteLocation("doc-a", "书 A", "", t0);
        long beforeDwell = ReaderAttentionBoard.Health().Sequence;
        ReaderAttentionBoard.NoteLocation(
            "doc-b", "书 B", "", t0 + TimeSpan.FromSeconds(2));
        ReaderAttentionBoard.NoteLocation(
            "doc-b", "书 B", "", t0 + TimeSpan.FromSeconds(5));
        if (ReaderAttentionBoard.Health().Sequence != beforeDwell)
        {
            throw new InvalidOperationException(
                "只待了几秒就算成注意力转移 —— 翻页会让板子一直抖");
        }
        ReaderAttentionBoard.NoteLocation(
            "doc-b", "书 B", "", t0 + TimeSpan.FromMinutes(3));
        if (ReaderAttentionBoard.Health().Sequence == beforeDwell)
        {
            throw new InvalidOperationException(
                "待够了却没算成转移 —— 门槛把真信号也挡掉了");
        }
        checks.Add("attention-board-dwell-threshold");

        // ⑤ 通知的寿命：读过一次 → 搭下一次状态变化的车离场。
        //
        // ⚠ 最容易做错的不是"有没有消失",是**消失花了几次变化**。
        // 立刻删掉的话:它看到通知(第 1 次变化) → 删除(第 2 次变化) →
        // 它再读一次,而这一次的新情报是零。所以离场必须并进
        // 一次本来就要发生的变化里,总账只能是一次。
        ReaderAttentionBoard.ResetForSelfTest();
        ReaderAttentionBoard.Assert(
            "cards", "现在有 20 张到期", "问要不要现在复习",
            mustSpeak: true, TimeSpan.FromMinutes(30));
        string withNotice = ReaderAttentionBoard.RenderForSelfTest(t0);
        if (!withNotice.Contains("20 张到期", StringComparison.Ordinal))
        {
            throw new InvalidOperationException("通知没出现在板子上");
        }
        // 「读过」由真正的读取路径登记。这里直接调它，避免测的是别的东西。
        ReaderAttentionBoard.MarkDeliveredForSelfTest();
        long afterRead = ReaderAttentionBoard.Health().Sequence;
        if (!string.Equals(
            withNotice,
            ReaderAttentionBoard.RenderForSelfTest(t0),
            StringComparison.Ordinal))
        {
            throw new InvalidOperationException(
                "登记已送达改变了输出 —— 那会白白再触发一次读取");
        }
        checks.Add("attention-board-delivery-is-not-a-change");

        // 来一次真实的状态变化：通知应当**在这同一次变化里**消失。
        ReaderAttentionBoard.NoteLocation("doc-x", "书 X", "", t0);
        ReaderAttentionBoard.NoteLocation(
            "doc-x", "书 X", "", t0 + TimeSpan.FromMinutes(2));
        long afterShift = ReaderAttentionBoard.Health().Sequence;
        string afterText = ReaderAttentionBoard.RenderForSelfTest(t0);
        if (afterText.Contains("20 张到期", StringComparison.Ordinal))
        {
            throw new InvalidOperationException("读过的通知没有离场");
        }
        if (afterShift - afterRead != 1)
        {
            throw new InvalidOperationException(
                "通知离场自成一次变化 —— 每条通知会花掉对面两次读取，"
                + "而第二次读到的新情报是零");
        }
        checks.Add("attention-board-retire-rides-along");

        // ⑥ 事实还成立时生产方会一直重报。同一件事不许反复通知；
        //    但内容真的变了（20→31）就是新消息，必须重新通知。
        ReaderAttentionBoard.Assert(
            "cards", "现在有 20 张到期", "问要不要现在复习",
            mustSpeak: true, TimeSpan.FromMinutes(30));
        if (ReaderAttentionBoard.RenderForSelfTest(t0)
            .Contains("20 张到期", StringComparison.Ordinal))
        {
            throw new InvalidOperationException(
                "重报同一件事又通知了一遍 —— 事实一直成立就会一直吵");
        }
        checks.Add("attention-board-reassert-does-not-renotify");
        ReaderAttentionBoard.Assert(
            "cards", "现在有 31 张到期", "问要不要现在复习",
            mustSpeak: true, TimeSpan.FromMinutes(30));
        if (!ReaderAttentionBoard.RenderForSelfTest(t0)
            .Contains("31 张到期", StringComparison.Ordinal))
        {
            throw new InvalidOperationException(
                "事实变了却没重新通知 —— 去重把真消息也吃掉了");
        }
        checks.Add("attention-board-changed-fact-renotifies");

        // 把一份**典型**的板子原样带进自检结果。它既是格式的活文档，
        // 也让"悄悄变啰嗦"看得见 —— 光看长度上限看不出文风退化。
        ReaderAttentionBoard.ResetForSelfTest();
        ReaderAttentionBoard.NoteLocation("bk", "食文化の本 p.26", "", t0);
        ReaderAttentionBoard.NoteLocation(
            "bk", "食文化の本 p.26", "", t0 + TimeSpan.FromMinutes(2));
        ReaderAttentionBoard.Assert(
            "home", "用户到家了", "提醒他倒垃圾",
            mustSpeak: true, TimeSpan.FromMinutes(30));
        ReaderAttentionBoard.Assert(
            "ink", "近期有新手绘图，问到再看", string.Empty,
            mustSpeak: false, TimeSpan.FromHours(1), once: true);
        ReaderAttentionBoard.Assert(
            "due", "当前页没有待复习目标", string.Empty,
            mustSpeak: false, TimeSpan.FromMinutes(30));
        checks.Add(
            "attention-board-sample>>>"
            + ReaderAttentionBoard.RenderForSelfTest(t0));

        ReaderAttentionBoard.ResetForSelfTest();
    }
}
