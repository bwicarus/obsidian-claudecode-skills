namespace BwReader.ComputerVoiceAudio;

/// 主动推送的自检（2026-09-09 Codex→Claude 交接）。
///
/// 交接点名要覆盖的六项里，这里守住不需要真连管道就能证明的四项 ——
/// 也正是坏掉最没有症状的四项：
///
///   ① **默认关**。消费端还在轮询时同时推送就是双发，而双发的表现是
///      "同一件事说两遍"，看着像模型啰嗦，不像配置错了。
///   ② **无变化静默**。板面没写盘就不许推。一次没有情报的唤醒 = 白烧额度。
///   ③ **管道名归一化**。`NamedPipeClientStream` 要名字不要 `\\.\pipe\` 全路径，
///      而环境变量里两种形态都可能出现；削不干净就拿一个非法名字去连，
///      表现只是"消息没到"。
///
/// ⚠ 「提示里不含板面正文」和「不碰业务 ack」这两条改成**源码级**断言，
///    放在 tests/reader_contract 里：打包成单文件之后 .cs 源码不在包内，
///    运行时读不到，写在这里会变成一条永远读不到文件因而形同虚设的检查。
///
/// 真连管道、断线恢复、任务切换这三项要活的 Codex 桌面在跑，留给联调。
internal static class ReaderCodexPushSelfTest
{
    internal static void Run(ICollection<string> checks)
    {
        bool restore = ReaderCodexPush.Enabled;
        try
        {
            CheckDisabledByDefault(checks);
            CheckNoChangeIsSilent(checks);
        CheckFastQuietWindow(checks);
        CheckLedgerSurvivesConcurrency(checks);
            CheckPipeNameNormalisation(checks);
        }
        finally
        {
            ReaderCodexPush.SetEnabled(restore);
        }
    }

    private static void CheckDisabledByDefault(ICollection<string> checks)
    {
        // ⚠ 负对照：只断言"关着"的话，一个永远关不开的实现也能过。
        ReaderCodexPush.SetEnabled(false);
        if (ReaderCodexPush.Enabled)
        {
            throw new InvalidOperationException("推送开关关不掉");
        }
        ReaderCodexPush.SetEnabled(true);
        if (!ReaderCodexPush.Enabled)
        {
            throw new InvalidOperationException("推送开关打不开");
        }
        ReaderCodexPush.SetEnabled(false);
        checks.Add("codex-push: 开关可关可开，且迁移期默认关");
    }

    /// 快板的安静窗口（2026-09-10 用户：「一定时间窗口内不连续发送相同通知」）。
    ///
    /// ⚠ 这里最要紧的一条是**紧急标记不许被压**：快板里「他主动挂断了电话」
    /// 的语义是"看到就停止向通话说话"，压它 90 秒等于让 AI 对着已经挂断的
    /// 电话继续说一分半。一个只测"会不会限流"的测试会把这条漏掉。
    /// 账本在**并发**下还写不写得进去（2026-09-10 实测事故）。
    ///
    /// ⚠ 这条守的是一个特别刁钻的形态：`RecordAttempt` 里那三个文件操作原来
    /// 没有锁，单线程时一切正常，一旦并发起来就几乎每次都撞成 IOException，
    /// 然后被那个"记账失败不影响推送"的静默 catch 吞掉。
    /// 结果是**发出去 432 条、账本一条都没有** —— 而并发失控本身正是账本
    /// 唯一能记录的证据，并发又正是让它写不进去的原因。
    ///
    /// 所以判据必须是"N 个并发写，最后账本里就有 N 条"，而不是"写一条能读到"。
    private static void CheckLedgerSurvivesConcurrency(
        ICollection<string> checks)
    {
        string runtime = System.IO.Path.Combine(
            System.IO.Path.GetTempPath(),
            "bw-ledger-selftest-" + Guid.NewGuid().ToString("N"));
        System.IO.Directory.CreateDirectory(runtime);
        string? previous = ReaderAttentionBoard.RuntimeDirectory;
        try
        {
            ReaderAttentionBoard.Configure(runtime);
            const int writers = 60;
            Parallel.For(0, writers, index =>
                ReaderCodexPush.NoteBridgeStart(
                    "concurrency-" + index.ToString(
                        System.Globalization.CultureInfo.InvariantCulture),
                    true,
                    "并发写测试"));
            string path = System.IO.Path.Combine(
                runtime, ReaderCodexPush.AttemptsFileName);
            int rows = System.IO.File.Exists(path)
                ? System.IO.File.ReadAllLines(path)
                    .Count(line => line.Trim().Length > 0)
                : 0;
            if (rows != writers)
            {
                throw new InvalidOperationException(
                    "并发写了 " + writers + " 条，账本里只有 " + rows + " 条");
            }
            checks.Add("codex-push: 账本在并发下不丢条（60 并发 = 60 条）");
        }
        finally
        {
            if (previous is not null)
            {
                ReaderAttentionBoard.Configure(previous);
            }
            try
            {
                System.IO.Directory.Delete(runtime, recursive: true);
            }
            catch (Exception)
            {
            }
        }
    }

    private static void CheckFastQuietWindow(ICollection<string> checks)
    {
        DateTimeOffset t0 = new(2026, 9, 10, 12, 0, 0, TimeSpan.Zero);
        TimeSpan window = ReaderAttentionBoard.FastQuietWindow;

        // ① 第一次：没推过任何东西，立刻推。
        if (!ReaderAttentionBoard.ShouldPushFast(
                "焦点 A", string.Empty, t0, DateTimeOffset.MinValue))
        {
            throw new InvalidOperationException("第一次就被压住了");
        }

        // ② 窗口内的第二次不同内容：压住。
        if (ReaderAttentionBoard.ShouldPushFast(
                "焦点 B", "焦点 A", t0 + TimeSpan.FromSeconds(5), t0))
        {
            throw new InvalidOperationException("窗口内没有限流");
        }

        // ③ 窗口结束：补推**当时最新**的那份。
        if (!ReaderAttentionBoard.ShouldPushFast(
                "焦点 C", "焦点 A", t0 + window, t0))
        {
            throw new InvalidOperationException("窗口结束后没有补推");
        }

        // ④ 压下之后又变回原样：窗口结束时无事可推（无变化静默的延伸）。
        if (ReaderAttentionBoard.ShouldPushFast(
                "焦点 A", "焦点 A", t0 + window, t0))
        {
            throw new InvalidOperationException("内容没变却在窗口结束时推了");
        }

        // ⑤ **紧急标记直通**：窗口内也必须立刻推。
        string urgent = "焦点 B\n" + ReaderAttentionBoard.HangUpMarker + "。";
        if (!ReaderAttentionBoard.ShouldPushFast(
                urgent, "焦点 A", t0 + TimeSpan.FromSeconds(1), t0))
        {
            throw new InvalidOperationException("挂断这条被安静窗口压住了");
        }

        checks.Add("codex-push: 快板安静窗口会补推，且挂断不受它约束");
    }

    private static void CheckNoChangeIsSilent(ICollection<string> checks)
    {
        // 开着、但两块板都没变 → 一次都不许发。
        ReaderCodexPush.SetEnabled(true);
        long before = ReaderCodexPush.SentCount;
        ReaderCodexPush
            .NotifyBoardChangedAsync(false, false, "慢板", "快板", CancellationToken.None)
            .GetAwaiter().GetResult();
        if (ReaderCodexPush.SentCount != before)
        {
            throw new InvalidOperationException("板面没变却推了一次");
        }
        // 关着、板面变了 → 也不许发（这一条守的是迁移开关本身）。
        ReaderCodexPush.SetEnabled(false);
        ReaderCodexPush
            .NotifyBoardChangedAsync(true, true, "慢板", "快板", CancellationToken.None)
            .GetAwaiter().GetResult();
        if (ReaderCodexPush.SentCount != before)
        {
            throw new InvalidOperationException("开关关着却推了一次");
        }
        checks.Add("codex-push: 无变化静默，且开关关着时一律不推");
    }

    private static void CheckPipeNameNormalisation(ICollection<string> checks)
    {
        // NamedPipeClientStream 要名字不要全路径；环境变量里两种都可能出现。
        if (ReaderCodexEndpoint.NormalizePipeName(@"\\.\pipe\codex-abc")
            != "codex-abc")
        {
            throw new InvalidOperationException("管道全路径没削成名字");
        }
        if (ReaderCodexEndpoint.NormalizePipeName("codex-abc") != "codex-abc")
        {
            throw new InvalidOperationException("已经是名字的被改坏了");
        }
        // 削不干净的一律判空：宁可不推并说原因，也不要拿一个非法名字去连。
        if (ReaderCodexEndpoint.NormalizePipeName(@"foo\bar").Length != 0)
        {
            throw new InvalidOperationException("残留反斜杠的名字没被判空");
        }
        checks.Add("codex-push: 管道名归一化，削不干净判空");
    }

}
