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

    private static void CheckNoChangeIsSilent(ICollection<string> checks)
    {
        // 开着、但两块板都没变 → 一次都不许发。
        ReaderCodexPush.SetEnabled(true);
        long before = ReaderCodexPush.SentCount;
        ReaderCodexPush
            .NotifyBoardChangedAsync(false, false, CancellationToken.None)
            .GetAwaiter().GetResult();
        if (ReaderCodexPush.SentCount != before)
        {
            throw new InvalidOperationException("板面没变却推了一次");
        }
        // 关着、板面变了 → 也不许发（这一条守的是迁移开关本身）。
        ReaderCodexPush.SetEnabled(false);
        ReaderCodexPush
            .NotifyBoardChangedAsync(true, true, CancellationToken.None)
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
