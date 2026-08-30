using System.Text.Json;

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

    /// 一条 pending + 一条 acknowledged 混在一起。
    ///
    /// 登记表核对要的就是这种状态：只有这样「开口：」和「- 待办｜N 条
    /// 已确认」才会**同时**出现在慢板上。用纯 pending 的夹具时，
    /// 「已确认待办」那条登记项永远缺席 —— 2026-08-30 我就是这么被
    /// 自检抓住的（注释里还写着"夹具里有"，其实没有）。
    private static void WriteMixedTodos(string dir)
    {
        File.WriteAllText(
            Path.Combine(dir, "notifications.json"),
            "{\"contract\":\"reader-notifications/1\",\"items\":["
            + "{\"id\":\"ntf-p\",\"kind\":\"user-todo\","
            + "\"title\":\"登记表核对用\",\"state\":\"pending\","
            + "\"audience\":\"user\",\"place\":{\"name\":\"家\"}},"
            + "{\"id\":\"ntf-a\",\"kind\":\"user-todo\","
            + "\"title\":\"已经确认过的\",\"state\":\"acknowledged\","
            + "\"audience\":\"user\"}]}");
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
        // ⚠ 断言的是**行为**不是措辞：ack 过的不该再被说成"还没跟他说过"，
        // 但必须仍以某种形式留在板上。措辞改过好几版（分节标题 →
        // 每行自包含），断言字面量的话每次改文案都要跟着改，而那不是契约。
        if (afterSaid.Contains("还没跟他说过", StringComparison.Ordinal))
        {
            throw new InvalidOperationException(
                "已 ack 的待办又被当成没说过 —— 事实一直成立就会一直吵");
        }
        if (!afterSaid.Contains("待办", StringComparison.Ordinal))
        {
            throw new InvalidOperationException(
                "ack 之后待办彻底消失了 —— 它还没完成，"
                + "该留在板上让它知道，只是不要再说");
        }
        checks.Add("attention-board-does-not-repeat-acked-todo");

        // ④ 负对照：还是 pending 的**必须**被说成要说，别被 ③ 一起吃掉。
        WriteTodos(dir, "垃圾投放提醒（今天是可燃）");
        if (!ReaderAttentionBoard.RenderForSelfTest()
            .Contains("还没跟他说过", StringComparison.Ordinal))
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
            .Contains("正在画或刚画过", StringComparison.Ordinal))
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

        // ⑨ 拆板（用户 2026-08-30）：慢板要稳，快板要及时。
        //
        // 这一组的核心是**负控制**那条 —— 「慢板不因绘图而变」。没有它，
        // 拆板等于没拆：两块板照样一起抖，而表面上看一切正常。
        ReaderAttentionBoard.ResetForSelfTest(dir);
        WriteTodos(dir, "垃圾投放提醒");
        ReaderAttentionBoard.NoteLocation("bk", "食文化の本 p.26", t0);
        DateTimeOffset t1 = t0 + TimeSpan.FromMinutes(2);
        ReaderAttentionBoard.NoteLocation("bk", "食文化の本 p.26", t1);
        string slowBefore = ReaderAttentionBoard.RenderSlowForSelfTest(t1);
        ReaderAttentionBoard.NoteDrawing(t1);
        string slowAfter = ReaderAttentionBoard.RenderSlowForSelfTest(t1);
        if (!string.Equals(slowBefore, slowAfter, StringComparison.Ordinal))
        {
            throw new InvalidOperationException(
                "开始绘图把慢板也改了 —— 拆板就是为了让待办不跟着抖");
        }
        checks.Add("attention-board-slow-ignores-ink");

        string fastNow = ReaderAttentionBoard.RenderFastForSelfTest(t1);
        if (!fastNow.Contains("正在画或刚画过", StringComparison.Ordinal))
        {
            throw new InvalidOperationException("快板上没有笔画");
        }
        if (slowAfter.Contains("正在画或刚画过", StringComparison.Ordinal))
        {
            throw new InvalidOperationException("笔画漏进了慢板");
        }
        if (!slowAfter.Contains("垃圾投放提醒", StringComparison.Ordinal))
        {
            throw new InvalidOperationException("待办没在慢板上");
        }
        if (fastNow.Contains("垃圾投放提醒", StringComparison.Ordinal))
        {
            throw new InvalidOperationException("待办漏进了快板");
        }
        // ⚠ 用户 2026-08-30 收窄：「位置应该只保留在慢的上面」。
        // 这推翻了拆板时那条"基础内容两边都有" —— 地点和焦点都是慢信号，
        // 放进快板只会让它们跟着绘图一起抖。
        if (!slowAfter.Contains("食文化の本", StringComparison.Ordinal))
        {
            throw new InvalidOperationException("注意力焦点没在慢板上");
        }
        if (fastNow.Contains("食文化の本", StringComparison.Ordinal)
            || fastNow.Contains("现在地点：", StringComparison.Ordinal))
        {
            throw new InvalidOperationException(
                "地点/焦点漏进了快板 —— 慢信号放快板会跟着绘图一起抖");
        }
        checks.Add("attention-board-split-routes-each-signal");

        // 快板到点就退场，**不用等别的变化搭车**（那条规矩只对慢板成立）。
        DateTimeOffset t2 = t1 + TimeSpan.FromMinutes(3);
        if (ReaderAttentionBoard.RenderFastForSelfTest(t2)
            .Contains("正在画或刚画过", StringComparison.Ordinal))
        {
            throw new InvalidOperationException(
                "停笔够久了快板还挂着笔画 —— 快板要的是及时，不是稳定");
        }
        checks.Add("attention-board-fast-retires-on-time");

        // ⑩ 登记表**双向**跟渲染对齐（用户 2026-08-30 要的那个清单）。
        //
        // 清单类的东西错了没有任何症状 —— 它会信誓旦旦地报一个不存在的
        // 种类，或者漏报一个真在用的。所以两个方向都要锁：
        //   正向：登记的标识，在"全都有值"时必须真的出现在那块板上
        //   反向：板上出现的标识，必须都在登记表里
        // 只做正向的话，新加一个信号忘了登记，自检照样是绿的。
        ReaderAttentionBoard.ResetForSelfTest(dir);
        WriteMixedTodos(dir);
        DateTimeOffset t3 = t0 + TimeSpan.FromMinutes(2);
        // 把能立的旗都立起来：位置（要满足停留门槛）、快照失效、绘图。
        ReaderAttentionBoard.NoteLocation("aa", "第一处", t0);
        ReaderAttentionBoard.NoteLocation("aa", "第一处", t3);
        ReaderAttentionBoard.NoteLocation("bb", "第二处", t3);
        ReaderAttentionBoard.NoteLocation(
            "bb", "第二处", t3 + TimeSpan.FromMinutes(2));
        DateTimeOffset t4 = t3 + TimeSpan.FromMinutes(2);
        ReaderAttentionBoard.NoteDrawing(t4);
        string slowFull = ReaderAttentionBoard.RenderSlowForSelfTest(t4);
        string fastFull = ReaderAttentionBoard.RenderFastForSelfTest(t4);
        foreach ((string board, string body) in new[]
        {
            ("slow", slowFull), ("fast", fastFull),
        })
        {
            var registered = new HashSet<string>(
                ReaderAttentionBoard.MarkersForSelfTest(board),
                StringComparer.Ordinal);
            foreach (string marker in ReaderAttentionBoard.MarkersInBody(body))
            {
                if (!registered.Contains(marker))
                {
                    throw new InvalidOperationException(
                        $"{board} 板上有「{marker}」但登记表里没有 —— "
                        + "新信号忘了登记，清单在撒谎");
                }
            }
        }
        checks.Add("attention-registry-covers-everything-on-the-boards");

        // 正向那一半：登记的每一条在这个"全都有值"的状态下都必须出现。
        // 夹具是 WriteMixedTodos（pending + acknowledged 各一条），
        // 「已确认待办」那条登记项要靠 acknowledged 那条才会出现。
        string registryJson =
            ReaderAttentionBoard.RenderRegistryForSelfTest(t4);
        foreach (string missing in new[] { "\"present\": false" })
        {
            if (registryJson.Contains(missing, StringComparison.Ordinal))
            {
                throw new InvalidOperationException(
                    "「全都有值」的状态下仍有登记项没出现在板上：登记表里有"
                    + "渲染逻辑并不会产出的种类。原始登记表 JSON：\n"
                    + registryJson);
            }
        }
        checks.Add("attention-registry-has-nothing-imaginary");

        // ⑪ 空状态下 present 必须**全是 false**。
        //
        // 上面两条只验证了"全都有值"的那一头，而错法恰恰出在另一头：
        // 我最初用 body.Contains("开口：") 判 present，而空板子写的是
        // 「开口：无」—— 含那个子串，于是一条待办都没有时登记表报
        // 「待办 · 现在在板上」。两条正向检查全绿，清单照样在撒谎。
        ReaderAttentionBoard.ResetForSelfTest(dir);
        File.WriteAllText(
            Path.Combine(dir, "notifications.json"),
            "{\"contract\":\"reader-notifications/1\",\"items\":[]}");
        string emptyRegistry =
            ReaderAttentionBoard.RenderRegistryForSelfTest(t0);
        // ⚠ 不能笼统断言"全 false"：「位置」那一行是**恒在**的（不知道时
        // 写「位置｜未知」），它报 true 是事实而非缺陷。第一版就是这么写
        // 的，于是断言逼着我去改一个本来正确的行为 —— 太粗的断言会把人
        // 引向错误的修复。所以按项断言。
        using (JsonDocument parsed = JsonDocument.Parse(emptyRegistry))
        {
            foreach (JsonElement board in
                parsed.RootElement.GetProperty("boards").EnumerateArray())
            {
                foreach (JsonElement item in
                    board.GetProperty("items").EnumerateArray())
                {
                    string kind = item.GetProperty("kind").GetString() ?? "";
                    if (kind == "地理位置" || kind == "注意力焦点")
                    {
                        continue;   // 这两行恒在（不知道时写「不知道」/「未知」）
                    }
                    if (item.GetProperty("present").GetBoolean())
                    {
                        throw new InvalidOperationException(
                            $"空状态下「{kind}」却报在板上 —— 清单在撒谎。"
                            + "登记表：\n" + emptyRegistry);
                    }
                }
            }
        }
        checks.Add("attention-registry-empty-means-empty");

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
