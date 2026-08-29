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
                // ⚠ 真值库里两种 audience 混在一起，板子只该拿 user 的。
                + "\"audience\":\"user\","
                + "\"place\":{\"name\":\"家\"}}");
        }
        File.WriteAllText(
            Path.Combine(dir, "notifications.json"),
            "{\"contract\":\"reader-notifications/1\",\"items\":["
            + string.Join(",", items) + "]}");
    }

    /// 同一批待办，但 state 是 acknowledged（助手已经 ack 过）。
    private static void WriteAcknowledged(string dir, params string[] titles)
    {
        var items = new List<string>();
        for (int index = 0; index < titles.Length; index++)
        {
            items.Add(
                "{\"id\":\"ntf-" + index + "\",\"kind\":\"user-todo\","
                + "\"title\":\"" + titles[index] + "\","
                + "\"state\":\"acknowledged\",\"audience\":\"user\"}");
        }
        File.WriteAllText(
            Path.Combine(dir, "notifications.json"),
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

        // ③ 助手 ack 过之后不再重复。
        //
        // ⚠ 判据是**真值库的状态机**（pending → acknowledged），不是
        // "板子被读过"。2026-08-29 实测板子每秒被读一次，读一次就消费的话，
        // 新待办会在一秒内被轮询吃掉，而没有任何人听见。
        WriteAcknowledged(dir, "垃圾投放提醒");
        string afterSaid = ReaderAttentionBoard.RenderForSelfTest();
        if (afterSaid.Contains("开口：\n- 垃圾投放提醒", StringComparison.Ordinal))
        {
            throw new InvalidOperationException(
                "已 ack 的待办又进了「开口」—— 事实一直成立就会一直吵");
        }
        if (!afterSaid.Contains("待办", StringComparison.Ordinal))
        {
            throw new InvalidOperationException(
                "ack 之后待办彻底消失了 —— 它还没完成，"
                + "该留在状态里让它知道，只是不要再说");
        }
        checks.Add("attention-board-does-not-repeat-acked-todo");

        // ④ 负对照：还是 pending 的**必须**进开口，别被 ③ 一起吃掉。
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
            Path.Combine(dir, "notifications.json"), "{ 这不是 json");
        string degraded = ReaderAttentionBoard.RenderForSelfTest();
        if (!degraded.Contains("书 B", StringComparison.Ordinal))
        {
            throw new InvalidOperationException(
                "待办文件坏了就连位置也不端了 —— 一个来源不该拖垮整块板子");
        }
        checks.Add("attention-board-survives-broken-source");

        // ⑦ 负对照：audience=ai 的条目**不该**上板子（那是助手自己的原料，
        //    不是"要告诉用户的事"）。直接读真值库之后这个过滤归我们做，
        //    漏掉的话板子会开始念系统内部消息。
        ReaderAttentionBoard.ResetForSelfTest(dir);
        File.WriteAllText(
            Path.Combine(dir, "notifications.json"),
            "{\"contract\":\"reader-notifications/1\",\"items\":["
            + "{\"id\":\"ntf-a\",\"title\":\"给助手的原料\","
            + "\"state\":\"pending\",\"audience\":\"ai\"}]}");
        if (ReaderAttentionBoard.RenderForSelfTest()
            .Contains("给助手的原料", StringComparison.Ordinal))
        {
            throw new InvalidOperationException(
                "audience=ai 的条目上了板子 —— 板子会开始念系统内部消息");
        }
        checks.Add("attention-board-skips-ai-audience");

        // ⑧ 笔画：有动作就立旗（不等稳定）；**持续画 → 一个字都不改**。
        ReaderAttentionBoard.ResetForSelfTest(dir);
        WriteTodos(dir);
        ReaderAttentionBoard.NoteDrawing(t0);
        // ⚠ 渲染要传**同一条时间轴**上的时刻。不传的话默认用真实时钟，
        // 而夹具的 t0 是 1970 —— 一渲染就被判成"停笔十几万小时"，
        // 笔画那行当场退场。第一版就栽在这里，报的却是"没上板子"。
        if (!ReaderAttentionBoard.RenderForSelfTest(t0)
            .Contains("有笔画", StringComparison.Ordinal))
        {
            throw new InvalidOperationException("笔画稳定了却没上板子");
        }
        long afterInk = ReaderAttentionBoard.Health().Sequence;
        // ⚠ 关键那条：用户 2026-08-29「即使我持续在绘图，这个信息也不需要
        // 被更新」。对面是"变了就读"，多一次变化就是白花它一次读取。
        for (int i = 1; i <= 5; i++)
        {
            ReaderAttentionBoard.NoteDrawing(
                t0 + TimeSpan.FromSeconds(10 * i));
        }
        if (ReaderAttentionBoard.Health().Sequence != afterInk)
        {
            throw new InvalidOperationException(
                "持续绘图把板子刷新了 —— 每一次都白花对面一次读取");
        }
        checks.Add("attention-board-ink-does-not-churn");

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
