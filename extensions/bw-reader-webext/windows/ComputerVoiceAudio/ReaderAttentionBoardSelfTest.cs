namespace BwReader.ComputerVoiceAudio;

/// 提示板的自检。它守的是**三条支点规矩**，每一条坏掉都没有症状：
///
///   ① 状态没变 → 输出一个字节都不许变
///      （对面"变了就读"，一次没有情报的变化 = 白花它一次读取和一次判断）
///   ② 已经说过的待办不再重复
///      （待办的 state 一直是 pending，等用户说"扔了"才完成）
///   ③ 板子必须很小
///      （"尽量减少信息量免得他每次都过度思考"）
///
/// ⚠ 每条都配负对照：只证明"不变/不重复"的话，一个永远返回常量的实现
/// 也能全过。
internal static class ReaderAttentionBoardSelfTest
{
    internal static void Run(ICollection<string> checks)
    {
        string dir = Path.Combine(
            Path.GetTempPath(), "bw-attention-selftest-" + Guid.NewGuid());
        Directory.CreateDirectory(dir);
        try
        {
            RunWith(dir, checks);
        }
        finally
        {
            ReaderAttentionBoard.ResetForSelfTest();
            try { Directory.Delete(dir, recursive: true); } catch { }
        }
    }

    private static void WriteTodos(string dir, params string[] titles)
    {
        var items = new List<string>();
        for (int index = 0; index < titles.Length; index++)
        {
            items.Add(
                "{\"id\":\"ntf-" + index + "\",\"kind\":\"user-todo\","
                + "\"title\":\"" + titles[index] + "\",\"state\":\"pending\","
                + "\"place\":{\"name\":\"家\"}}");
        }
        File.WriteAllText(
            Path.Combine(dir, "notifications-user.json"),
            "{\"contract\":\"reader-notifications/1\",\"items\":["
            + string.Join(",", items) + "]}");
    }

    private static void RunWith(string dir, ICollection<string> checks)
    {
        DateTimeOffset t0 = DateTimeOffset.UnixEpoch;
        ReaderAttentionBoard.ResetForSelfTest(dir);
        WriteTodos(dir);

        // ① 空板子必须**很小**，而且读两遍完全一样。
        string idle = ReaderAttentionBoard.RenderForSelfTest();
        if (!string.Equals(
            idle,
            ReaderAttentionBoard.RenderForSelfTest(),
            StringComparison.Ordinal))
        {
            throw new InvalidOperationException(
                "同样的状态两次渲染不一样 —— 每一次这样的变化都白花对面一次读取");
        }
        if (idle.Length > 200)
        {
            throw new InvalidOperationException(
                "空板子已经 " + idle.Length + " 字，超出 200 —— "
                + "不变的规则该放进 skill，不该每次重发一遍：" + idle);
        }
        checks.Add("attention-board-idle-is-small:" + idle.Length);

        // ② 有待办 → 出现在「开口」里。
        WriteTodos(dir, "垃圾投放提醒");
        string withTodo = ReaderAttentionBoard.RenderForSelfTest();
        if (!withTodo.Contains("垃圾投放提醒", StringComparison.Ordinal))
        {
            throw new InvalidOperationException("待办没有出现在板子上");
        }
        checks.Add("attention-board-shows-pending-todo");

        // ③ 说过一次之后不再重复 —— 待办的 state 会一直是 pending，
        //    没有这条就会每次都当成新消息重报。
        ReaderAttentionBoard.MarkDeliveredForSelfTest();
        string afterSaid = ReaderAttentionBoard.RenderForSelfTest();
        if (afterSaid.Contains("开口：\n- 垃圾投放提醒", StringComparison.Ordinal))
        {
            throw new InvalidOperationException(
                "说过的待办又进了「开口」—— 事实一直成立就会一直吵");
        }
        if (!afterSaid.Contains("待办", StringComparison.Ordinal))
        {
            throw new InvalidOperationException(
                "说过之后待办彻底消失了 —— 它还没完成，"
                + "该留在状态里让它知道，只是不要再说");
        }
        checks.Add("attention-board-does-not-repeat-said-todo");

        // ④ 负对照：**内容变了**就是新消息，必须重新说。
        WriteTodos(dir, "垃圾投放提醒（今天是可燃）");
        if (!ReaderAttentionBoard.RenderForSelfTest()
            .Contains("开口：", StringComparison.Ordinal))
        {
            throw new InvalidOperationException(
                "待办内容变了却没重新说 —— 去重把真消息也吃掉了");
        }
        checks.Add("attention-board-changed-todo-speaks-again");

        // ⑤ 停留门槛：没待够不算换了地方，待够了必须算。
        ReaderAttentionBoard.ResetForSelfTest(dir);
        WriteTodos(dir);
        ReaderAttentionBoard.NoteLocation("doc-a", "书 A", t0);
        ReaderAttentionBoard.NoteLocation("doc-a", "书 A", t0);
        ReaderAttentionBoard.NoteLocation(
            "doc-b", "书 B", t0 + TimeSpan.FromSeconds(2));
        ReaderAttentionBoard.NoteLocation(
            "doc-b", "书 B", t0 + TimeSpan.FromSeconds(5));
        if (ReaderAttentionBoard.RenderForSelfTest()
            .Contains("书 B", StringComparison.Ordinal))
        {
            throw new InvalidOperationException(
                "只待了几秒就算成注意力转移 —— 翻页会让板子一直抖");
        }
        ReaderAttentionBoard.NoteLocation(
            "doc-b", "书 B", t0 + TimeSpan.FromMinutes(3));
        if (!ReaderAttentionBoard.RenderForSelfTest()
            .Contains("书 B", StringComparison.Ordinal))
        {
            throw new InvalidOperationException(
                "待够了却没算成转移 —— 门槛把真信号也挡掉了");
        }
        checks.Add("attention-board-dwell-threshold");

        // ⑥ 待办文件坏掉/缺失，板子仍要能端出位置 ——
        //    一个来源出问题不该让整块板子变哑。
        File.WriteAllText(
            Path.Combine(dir, "notifications-user.json"), "{ 这不是 json");
        string degraded = ReaderAttentionBoard.RenderForSelfTest();
        if (!degraded.Contains("书 B", StringComparison.Ordinal))
        {
            throw new InvalidOperationException(
                "待办文件坏了就连位置也不端了 —— 一个来源不该拖垮整块板子");
        }
        checks.Add("attention-board-survives-broken-source");

        // 把一份典型的板子带进结果：格式的活文档，也让"悄悄变啰嗦"看得见。
        ReaderAttentionBoard.ResetForSelfTest(dir);
        WriteTodos(dir, "垃圾投放提醒");
        ReaderAttentionBoard.NoteLocation("bk", "食文化の本 p.26", t0);
        ReaderAttentionBoard.NoteLocation(
            "bk", "食文化の本 p.26", t0 + TimeSpan.FromMinutes(2));
        checks.Add(
            "attention-board-sample>>>"
            + ReaderAttentionBoard.RenderForSelfTest());
    }
}
