using System.Diagnostics;
using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace BwReader.ComputerVoiceAudio;

internal enum DirectProtocolPhase
{
    AwaitingAuthentication,
    AwaitingStart,
    ContextOnly,
    Starting,
    Active,
}

internal sealed record DirectCodexVoiceState(
    string Status,
    bool? Active,
    string? Source);

internal sealed record DirectCodexVoiceSetResult(
    DirectCodexVoiceState State,
    bool ShortcutSent);

internal interface IDirectCodexVoiceControl
{
    bool KeepActive { get; }

    DirectCodexVoiceState ReadState();

    Task<DirectCodexVoiceSetResult> SetActiveAsync(
        bool active,
        CancellationToken cancellationToken);

    Task<DirectCodexVoiceSetResult> SetKeepActiveAsync(
        bool enabled,
        CancellationToken cancellationToken);
}

/// <summary>
/// Non-voice ReaderPC service control. It deliberately owns no keepalive
/// monitor, Windows capability probe, F24 sender, or automatic recovery task.
/// </summary>
internal sealed class DirectDisabledCodexVoiceControl :
    IDirectCodexVoiceControl
{
    internal const string DisabledSource = "readerpc-voice-disabled";

    public bool KeepActive => false;

    public DirectCodexVoiceState ReadState() => new(
        "unavailable",
        Active: null,
        DisabledSource);

    public Task<DirectCodexVoiceSetResult> SetActiveAsync(
        bool active,
        CancellationToken cancellationToken) =>
        Task.FromException<DirectCodexVoiceSetResult>(Disabled());

    public Task<DirectCodexVoiceSetResult> SetKeepActiveAsync(
        bool enabled,
        CancellationToken cancellationToken) =>
        Task.FromException<DirectCodexVoiceSetResult>(Disabled());

    public ValueTask DisposeAsync() => ValueTask.CompletedTask;

    private static DirectProtocolException Disabled() => new(
        "BW_COMPUTER_VOICE_DIRECT_VOICE_DISABLED",
        "ReaderPC 语音功能已关闭；快照与其它非语音工具仍可用。");
}

internal sealed class DirectCodexVoiceControl :
    IDirectCodexVoiceControl,
    IAsyncDisposable
{
    internal const string StateSource =
        "windows-microphone-capability-ledger";
    internal static readonly TimeSpan RestartReadySettleDelay =
        TimeSpan.FromSeconds(5);
    internal static readonly TimeSpan DisposeStopTimeout =
        TimeSpan.FromSeconds(8);
    /// <summary>
    /// 上一次按键的确认还没走完之前，不许再按。
    /// </summary>
    /// <remarks>
    /// F24 是**切换**不是"打开"。Codex 收到后要几秒初始化，这期间信号还没翻转 ——
    /// 旧行为是确认等不到就重试**再按一次**，正好把刚开起来的关掉。用户 2026-08-18
    /// 亲眼看到：「刚好看到你打开语音，但是在他初始化完全前就又被关闭了，
    /// 可我手动打开的语音在初始化结束后留在了那里」—— 手动开的能留下，
    /// 恰恰因为没有第二次按键去撤销它。
    ///
    /// **由确认窗口推导，不是拍的数字**（用户：「既然已经有了明确的确定信号，
    /// 每次都按照这个信号来不好么，这样就算加载时间长也能用」）。
    /// 真正判"开没开"的始终是信号本身（麦克风台账 / 音频会话，均已实测验证可靠）；
    /// 这个冷却只负责"确认走完之前不许再按"，所以它天然等于一整轮确认的长度，
    /// 而不该是另一个独立猜出来的常数 —— 确认窗口调整时它自动跟着走。
    ///
    /// ⚠ 对开和关**一视同仁**：关的那一次同样可能落在刚开起来的初始化中间。
    /// </remarks>
    internal static readonly TimeSpan ShortcutCooldown =
        CodexVoiceActivityController.StartObservationTimeout
        + CodexVoiceActivityController.StartUsableSettleDelay;

    internal static readonly TimeSpan AutomaticRecoveryRetryDelay =
        TimeSpan.FromSeconds(20);

    /// <summary>
    /// 自动恢复的退避上限。**没有"放弃"这一档**（2026-08-18 重做）。
    /// </summary>
    /// <remarks>
    /// 旧设计是每个意图代际只给 2 次预算，用尽就 _automaticRecoveryBlocked = 1
    /// 永久放弃 —— 而解封只有 keepalive 真跃迁一条路，于是用户唯一的出口变成
    /// "去把 Codex 重启一下"（用户原话）。吸收态在一个本来就靠猜的链路上尤其危险：
    /// 猜错一次的后果是永久的。
    ///
    /// 现在改成有界指数退避、但一直重试：20s → 40s → 80s → … 封顶 5 分钟。
    /// 既不会 hammering（这是当初设上限的正当理由），也不会把"暂时不行"
    /// 变成"从此不行"。
    /// </remarks>
    internal static readonly TimeSpan AutomaticRecoveryMaximumRetryDelay =
        TimeSpan.FromMinutes(5);
    internal const int MaximumAutomaticRecoveryFailuresPerIntent = 2;

    internal static DirectCodexVoiceControl Shared { get; } =
        CreateProduction();

    private readonly Func<CodexVoiceActivitySnapshot> _readSnapshot;
    private readonly Func<
        bool,
        CodexVoiceActivitySnapshot,
        CancellationToken,
        Task<CodexVoiceActivitySnapshot>> _transitionAsync;
    private readonly SemaphoreSlim _transitionGate;
    private readonly string? _keepActivePath;
    private readonly TimeSpan _keepActivePollInterval;
    private readonly CancellationTokenSource? _keepActiveLifetime;
    private readonly Task? _keepActiveMonitor;
    private readonly Func<
        CodexVoiceActivitySnapshot?,
        CancellationToken,
        Task>?
        _prepareStartAsync;
    private readonly Func<CancellationToken, Task>?
        _recoverStartFailureAsync;
    private readonly Action<bool>? _keepActiveChanged;
    private readonly Action<Exception>? _automaticRecoveryFailed;
    private readonly Action? _automaticRecoverySucceeded;
    private readonly Func<TimeSpan, CancellationToken, Task>
        _automaticRecoveryDelayAsync;
    private readonly object _keepActiveIntentGate = new();
    private readonly object _automaticRecoveryTaskGate = new();
    private readonly List<CancellationTokenSource>
        _retiredIntentLifetimes = [];
    private CancellationTokenSource _intentLifetime = new();
    private Task? _automaticRecoveryTask;
    private long _intentGeneration;
    private int _keepActive;
    private int _automaticRecoveryFailureCount;
    private int _automaticRecoveryBlocked;
    private long _lastShortcutSentTicksUtc;
    private readonly TimeSpan _shortcutCooldown;
    private int _disposeStarted;

    internal DirectCodexVoiceControl(
        Func<CodexVoiceActivitySnapshot> readSnapshot,
        Func<
            bool,
            CodexVoiceActivitySnapshot,
            CancellationToken,
            Task<CodexVoiceActivitySnapshot>> transitionAsync,
        SemaphoreSlim? transitionGate = null,
        string? keepActivePath = null,
        TimeSpan? keepActivePollInterval = null,
        Func<
            CodexVoiceActivitySnapshot?,
            CancellationToken,
            Task>? prepareStartAsync = null,
        Func<CancellationToken, Task>? recoverStartFailureAsync = null,
        Action<bool>? keepActiveChanged = null,
        Action<Exception>? automaticRecoveryFailed = null,
        Action? automaticRecoverySucceeded = null,
        TimeSpan? shortcutCooldown = null,
        Func<TimeSpan, CancellationToken, Task>?
            automaticRecoveryDelayAsync = null)
    {
        _readSnapshot = readSnapshot
            ?? throw new ArgumentNullException(nameof(readSnapshot));
        _transitionAsync = transitionAsync
            ?? throw new ArgumentNullException(nameof(transitionAsync));
        _transitionGate = transitionGate ?? new SemaphoreSlim(1, 1);
        _keepActivePath = string.IsNullOrWhiteSpace(keepActivePath)
            ? null
            : System.IO.Path.GetFullPath(keepActivePath);
        _keepActivePollInterval = keepActivePollInterval
            ?? TimeSpan.FromSeconds(5);
        _prepareStartAsync = prepareStartAsync;
        _recoverStartFailureAsync = recoverStartFailureAsync;
        _keepActiveChanged = keepActiveChanged;
        _automaticRecoveryFailed = automaticRecoveryFailed;
        _automaticRecoverySucceeded = automaticRecoverySucceeded;
        _shortcutCooldown = shortcutCooldown ?? ShortcutCooldown;
        _automaticRecoveryDelayAsync = automaticRecoveryDelayAsync
            ?? ((delay, cancellationToken) =>
                Task.Delay(delay, cancellationToken));
        if (_keepActivePollInterval < TimeSpan.FromSeconds(1))
        {
            throw new ArgumentOutOfRangeException(
                nameof(keepActivePollInterval));
        }
        _keepActive = LoadKeepActive(_keepActivePath) ? 1 : 0;
        _intentGeneration = _keepActive == 1 ? 1 : 0;
        if (_keepActivePath is not null)
        {
            _keepActiveLifetime = new CancellationTokenSource();
            _keepActiveMonitor = MonitorKeepActiveAsync(
                _keepActiveLifetime.Token);
        }
    }

    public bool KeepActive => Volatile.Read(ref _keepActive) == 1;

    public DirectCodexVoiceState ReadState()
    {
        try
        {
            return ToState(_readSnapshot());
        }
        catch
        {
            // STATUS must remain a side-effect-free diagnostic even if the
            // Windows capability ledger is temporarily unreadable.
            return new DirectCodexVoiceState(
                "error",
                Active: null,
                StateSource);
        }
    }

    public async Task<DirectCodexVoiceSetResult> SetActiveAsync(
        bool active,
        CancellationToken cancellationToken)
    {
        return await SetActiveSerializedAsync(
            active,
            cancellationToken).ConfigureAwait(false);
    }

    private async Task<DirectCodexVoiceSetResult> SetActiveSerializedAsync(
        bool active,
        CancellationToken cancellationToken)
    {
        await _transitionGate.WaitAsync(cancellationToken)
            .ConfigureAwait(false);
        try
        {
            return await SetActiveWithinGateAsync(
                active,
                cancellationToken).ConfigureAwait(false);
        }
        finally
        {
            _transitionGate.Release();
        }
    }

    public async Task<DirectCodexVoiceSetResult> SetKeepActiveAsync(
        bool enabled,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        SaveKeepActive(_keepActivePath, enabled);
        bool changed = ApplyKeepActiveIntent(
            enabled,
            out long generation,
            out CancellationToken intentToken);
        using CancellationTokenSource transitionLifetime =
            CancellationTokenSource.CreateLinkedTokenSource(
                intentToken,
                cancellationToken);
        CancellationToken transitionToken = transitionLifetime.Token;
        if (enabled && !changed)
        {
            return new DirectCodexVoiceSetResult(
                ReadState(),
                ShortcutSent: false);
        }

        try
        {
            if (!enabled)
            {
                DirectCodexVoiceState current = ReadState();
                if (current.Status == "available" && current.Active == true)
                {
                    return await SetActiveSerializedAsync(
                        active: false,
                        transitionToken).ConfigureAwait(false);
                }
                return new DirectCodexVoiceSetResult(
                    current,
                    ShortcutSent: false);
            }
            DirectCodexVoiceSetResult result =
                await SetActiveSerializedAsync(
                active: true,
                transitionToken).ConfigureAwait(false);
            MarkAutomaticRecoverySucceeded(generation);
            return result;
        }
        catch (OperationCanceledException) when (
            intentToken.IsCancellationRequested
            && !cancellationToken.IsCancellationRequested)
        {
            return new DirectCodexVoiceSetResult(
                ReadState(),
                ShortcutSent: false);
        }
        catch (OperationCanceledException) when (
            cancellationToken.IsCancellationRequested)
        {
            // A canceled caller is not evidence that the service-owned intent
            // is unhealthy. Let the monitor reconcile it without consuming
            // the bounded automatic recovery budget.
            throw;
        }
        catch (Exception exception) when (enabled)
        {
            bool shouldRetry = RegisterAutomaticRecoveryFailure(
                generation,
                exception);
            if (shouldRetry)
            {
                StartAutomaticRecoveryIfNeeded(
                    _keepActiveLifetime?.Token
                        ?? CancellationToken.None);
            }
            throw;
        }
    }

    private async Task<DirectCodexVoiceSetResult> SetActiveWithinGateAsync(
        bool active,
        CancellationToken cancellationToken)
    {
        if (active && _prepareStartAsync is not null)
        {
            CodexVoiceActivitySnapshot? initial = null;
            try
            {
                initial = _readSnapshot();
                if (
                    initial.Status == CodexVoiceActivityReadStatus.Available
                    && initial.Active
                )
                {
                    return new DirectCodexVoiceSetResult(
                        ToState(initial),
                        ShortcutSent: false);
                }
            }
            catch
            {
                // Starting the packaged app is the recovery path when its
                // microphone ledger does not exist yet. The authoritative
                // read below still fails closed if it remains unavailable.
            }
            await _prepareStartAsync(initial, cancellationToken)
                .ConfigureAwait(false);
            cancellationToken.ThrowIfCancellationRequested();
        }
        CodexVoiceActivitySnapshot before = ReadRequired();
        cancellationToken.ThrowIfCancellationRequested();
        if (before.Active == active)
        {
            return new DirectCodexVoiceSetResult(
                ToState(before),
                ShortcutSent: false);
        }

        // 冷却期内一律不按（见 ShortcutCooldown）：上一次按键可能还在初始化，
        // 这时再按就是把它撤销。返回当前状态、标明没发按键，让调用方稍后再看 ——
        // 而不是把"我们选择不按"伪装成一次失败。
        if (WithinShortcutCooldown())
        {
            return new DirectCodexVoiceSetResult(
                ToState(before),
                ShortcutSent: false);
        }

        CodexVoiceActivitySnapshot confirmed;
        try
        {
            MarkShortcutSent();
            confirmed = await TransitionOnceAsync(
                active,
                before,
                cancellationToken).ConfigureAwait(false);
        }
        catch (DirectProtocolException exception) when (
            active
            && exception.Code
                == CodexVoiceActivityController.StartNotConfirmedCode
            && _recoverStartFailureAsync is not null)
        {
            // The first F24 is allowed to finish its bounded observation. Only
            // that explicit failure authorizes one restart of the same intent
            // generation. The intent token cancels the restart/settle before a
            // stale generation can send its second F24.
            await _recoverStartFailureAsync(cancellationToken)
                .ConfigureAwait(false);
            cancellationToken.ThrowIfCancellationRequested();
            CodexVoiceActivitySnapshot afterRestart = ReadRequired();
            cancellationToken.ThrowIfCancellationRequested();
            confirmed = afterRestart.Active
                ? afterRestart
                : await TransitionOnceAfterRestartAsync(
                    afterRestart,
                    cancellationToken).ConfigureAwait(false);
        }
        return new DirectCodexVoiceSetResult(
            ToState(confirmed),
            ShortcutSent: true);
    }

    private bool WithinShortcutCooldown()
    {
        long last = Interlocked.Read(ref _lastShortcutSentTicksUtc);
        if (last == 0)
        {
            return false;
        }
        return DateTime.UtcNow - new DateTime(last, DateTimeKind.Utc)
            < _shortcutCooldown;
    }

    private void MarkShortcutSent() =>
        Interlocked.Exchange(
            ref _lastShortcutSentTicksUtc,
            DateTime.UtcNow.Ticks);

    /// <summary>重启补救后的那一次按键：同样要记时间戳，否则冷却形同虚设。</summary>
    private Task<CodexVoiceActivitySnapshot> TransitionOnceAfterRestartAsync(
        CodexVoiceActivitySnapshot afterRestart,
        CancellationToken cancellationToken)
    {
        MarkShortcutSent();
        return TransitionOnceAsync(
            active: true,
            afterRestart,
            cancellationToken);
    }

    private async Task<CodexVoiceActivitySnapshot> TransitionOnceAsync(
        bool active,
        CodexVoiceActivitySnapshot before,
        CancellationToken cancellationToken)
    {
        CodexVoiceActivitySnapshot confirmed =
            await _transitionAsync(
                active,
                before,
                cancellationToken).ConfigureAwait(false);
        cancellationToken.ThrowIfCancellationRequested();
        RequireAvailable(confirmed);
        if (confirmed.Active != active)
        {
            throw new DirectProtocolException(
                active
                    ? CodexVoiceActivityController.StartNotConfirmedCode
                    : CodexVoiceActivityController.StopNotConfirmedCode,
                active
                    ? "未确认 Codex 语音已开启"
                    : "未确认 Codex 语音已关闭",
                retryable: true);
        }
        return confirmed;
    }

    private CodexVoiceActivitySnapshot ReadRequired()
    {
        try
        {
            CodexVoiceActivitySnapshot snapshot = _readSnapshot();
            RequireAvailable(snapshot);
            return snapshot;
        }
        catch (DirectProtocolException)
        {
            throw;
        }
        catch (Exception exception)
        {
            throw new DirectProtocolException(
                CodexVoiceActivityController.ActivityReadFailedCode,
                "读取 Codex 语音状态失败",
                retryable: true,
                innerException: exception);
        }
    }

    private static void RequireAvailable(
        CodexVoiceActivitySnapshot snapshot)
    {
        if (snapshot.Status == CodexVoiceActivityReadStatus.Unavailable)
        {
            throw new DirectProtocolException(
                CodexVoiceActivityController.ActivityUnavailableCode,
                "Codex 语音状态当前不可用",
                retryable: true);
        }
        if (snapshot.Status == CodexVoiceActivityReadStatus.Error)
        {
            throw new DirectProtocolException(
                CodexVoiceActivityController.ActivityReadFailedCode,
                "读取 Codex 语音状态失败",
                retryable: true);
        }
    }

    private static DirectCodexVoiceState ToState(
        CodexVoiceActivitySnapshot snapshot) =>
        snapshot.Status switch
        {
            CodexVoiceActivityReadStatus.Available => new(
                "available",
                snapshot.Active,
                StateSource),
            CodexVoiceActivityReadStatus.Unavailable => new(
                "unavailable",
                Active: null,
                StateSource),
            _ => new(
                "error",
                Active: null,
                StateSource),
        };

    internal static DirectCodexVoiceControl CreateProduction(
        string? keepActivePath = null,
        IDirectAppLauncher? appLauncher = null,
        Action<bool>? keepActiveChanged = null,
        Action<Exception>? automaticRecoveryFailed = null,
        Action? automaticRecoverySucceeded = null,
        TimeSpan? shortcutCooldown = null)
    {
        WindowsRegistryCodexVoiceActivitySource source = new(
            DirectAppTargets.CodexDesktop);
        CodexVoiceActivityController controller = new(
            source,
            new SystemCodexVoiceActivityClock());
        WindowsCodexVoiceShortcutSender shortcutSender = new();
        IDirectAppLauncher launcher = appLauncher
            ?? new WindowsDirectAppLauncher();
        return new DirectCodexVoiceControl(
            source.Read,
            async (active, before, cancellationToken) =>
            {
                if (!active)
                {
                    return await HangUpAsync(
                        controller,
                        shortcutSender,
                        before,
                        cancellationToken).ConfigureAwait(false);
                }
                CodexAppTarget target = RequireCodexTarget();
                {
                    CodexVoiceStartBaseline baseline = new(before);
                    shortcutSender.Send(target, DirectVoiceCommand.Start);
                    CodexVoiceShortcutReceipt receipt =
                        controller.RecordShortcutSent(baseline, target);
                    CodexVoiceStartConfirmation confirmation;
                    try
                    {
                        confirmation = await controller.ConfirmStartedAsync(
                            baseline,
                            receipt,
                            CodexVoiceActivityController.StartObservationTimeout,
                            CodexVoiceActivityController.MonitorInterval,
                            cancellationToken).ConfigureAwait(false);
                    }
                    catch (DirectProtocolException exception) when (
                        exception.Code == CodexVoiceActivityController
                            .StartNotConfirmedCode)
                    {
                        // ## 死按重试（2026-08-30，用户：「每次都会出现一次
                        // 已发送快捷键但失败，然后等很久才真的启动成功」）
                        //
                        // 冷启动时窗口句柄出现得比全局热键注册早：沉降 5 秒
                        // 后的第一按常常**落空**。落空的代价原来是一整轮
                        // 「记失败 → 20 秒自动恢复 → 第二按成功」≈ 35 秒。
                        //
                        // 整个观察窗（10s）过去台账**一点没动** = 按键没被
                        // 接住。此时不存在"正在初始化的会话"可被第二按
                        // 撤销 —— 翻转必在观察窗内发生，正是那 10 秒的定义
                        // （2026-08-18 误杀的前提是确认没走完就按，这里
                        // 确认已经走完，外层冷却的本义也因此满足）。
                        // 同一次尝试内立刻补按一次，只补这一次。
                        //
                        // ⚠ 只在台账**仍未激活**时才补按：若 Active 已翻转
                        // 只是时间戳没对上确认条件，再按就是把开着的关掉 ——
                        // 那种情况原样抛，交给外层如实报。
                        CodexVoiceActivitySnapshot? fresh = source.Read();
                        if (fresh is null || fresh.Active)
                        {
                            throw;
                        }
                        CodexVoiceStartBaseline second = new(fresh);
                        shortcutSender.Send(target, DirectVoiceCommand.Start);
                        receipt = controller.RecordShortcutSent(
                            second, target);
                        confirmation = await controller.ConfirmStartedAsync(
                            second,
                            receipt,
                            CodexVoiceActivityController.StartObservationTimeout,
                            CodexVoiceActivityController.MonitorInterval,
                            cancellationToken).ConfigureAwait(false);
                    }
                    confirmation = await controller.ConfirmUsableAsync(
                        confirmation,
                        CodexVoiceActivityController.StartUsableSettleDelay,
                        cancellationToken).ConfigureAwait(false);
                    return confirmation.Snapshot;
                }
            },
            keepActivePath: keepActivePath,
            prepareStartAsync: (_, cancellationToken) =>
                PrepareInitialStartAsync(
                    launcher,
                    static (delay, token) => Task.Delay(delay, token),
                    cancellationToken),
            // recoverStartFailureAsync 有意不接线(2026-08-17 用户实测拍板):
            // "恢复=重启 Codex App"会反复杀掉用户正在使用的会话(今晚实录:20 分钟
            // 内多次),且重启窗口里新旧两代并存又制造 APP_AMBIGUOUS——自己造病
            // 自己治。语音开不成就如实报失败,交给失败预算(每代 2 次、20s 间隔)
            // 温和重试;绝不动用户开着的 App。
            recoverStartFailureAsync: null,
            keepActiveChanged: keepActiveChanged,
            automaticRecoveryFailed: automaticRecoveryFailed,
            automaticRecoverySucceeded: automaticRecoverySucceeded,
            shortcutCooldown: shortcutCooldown);
    }

    /// <summary>
    /// 挂断当前通话。**先走通知通道，F24 只是兜底**（2026-09-10 用户拍板：
    /// 「挂断走通知更稳定不要再用 f24」）。
    /// </summary>
    /// <remarks>
    /// 理由与 <see cref="ReaderCodexPush.RequestVoiceHangUpAsync"/> 那段注释
    /// 同源：F24 是**切换**，按它之前必须先知道当前状态，而状态只能从有秒级
    /// 延迟的麦克风台账读 —— 读错就做反（以为已挂其实在通话＝挂掉用户的电话；
    /// 以为在通话其实已挂＝**反向开一通并开始计费**）。
    /// <c>end_realtime_voice_call</c> 是**有方向**的：对面不在通话时它只空转，
    /// 判断错的代价从"做反"降级成"白做一次"。
    ///
    /// ⚠ 那段道理 2026-09-09 就写下来了，但**只有 ReaderPC 的策略环照做**；
    /// 收敛环（本方法的调用方）一直在直接按键。2026-09-10 17:01 就是这么
    /// 挂掉一通正在进行的通话的。同一条道理有两个实现、只改了一个 ——
    /// 这正是 CLAUDE.md 里"先数清楚有几份副本"那条。现在按用途收到一处：
    /// **收敛环与策略环共用这条挂断路径**，F24 留在
    /// <c>hangUpVoiceFallback</c> 那个显式兜底 op 里。
    ///
    /// ⚠ 兜底按键受 <see cref="ShortcutFallbackEnabled"/> 管（用户
    /// 2026-09-10：「把 f24 兜底作为一个可选开关」）。关着时不按，**如实报
    /// 失败而不是假装挂掉了** —— 上层据此决定要不要提示用户手动挂。
    /// </remarks>
    private static async Task<CodexVoiceActivitySnapshot> HangUpAsync(
        CodexVoiceActivityController controller,
        WindowsCodexVoiceShortcutSender shortcutSender,
        CodexVoiceActivitySnapshot before,
        CancellationToken cancellationToken)
    {
        string requestId = "hangup-"
            + DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()
                .ToString(CultureInfo.InvariantCulture);
        string threadId = InCallThreadId();
        bool requested = false;
        if (threadId.Length > 0)
        {
            requested = await ReaderCodexPush.RequestVoiceHangUpAsync(
                threadId,
                "桥端收敛：语音保活意图已撤销，这一通该结束了",
                requestId,
                cancellationToken).ConfigureAwait(false);
        }
        else
        {
            // 出声：拿不到通话线程与"送出去了但对面没动"是两件不同的事，
            // 折成一个"没挂掉"会让排查的人查错方向。
            ReaderCodexPush.NoteHangUpDecision(
                requestId, false,
                "拿不到正在通话的线程 id（"
                + InCallThreadSource()
                + "），挂断请求未发送");
        }

        if (requested)
        {
            // 通道这条路要等的是：推送送达 → 对面跑完一轮 → 通话真的拆掉。
            // 比按键那条长，所以用它自己的上界（见 StopViaChannelTimeout）。
            return await ConfirmHangUpAsync(
                controller,
                before,
                CodexVoiceActivityController.StopViaChannelTimeout,
                requestId,
                "通道",
                cancellationToken).ConfigureAwait(false);
        }

        if (!ShortcutFallbackEnabled())
        {
            ReaderCodexPush.NoteHangUpDecision(
                requestId, false,
                "挂断请求没送出去，且 F24 兜底在设置里是关的 —— 没有按任何键");
            throw new DirectProtocolException(
                CodexVoiceActivityController.StopNotConfirmedCode,
                "挂断请求没能经通知通道送出，而 F24 兜底已关闭；本次没有挂断",
                retryable: true);
        }

        CodexAppTarget target = RequireCodexTarget();
        shortcutSender.Send(target, DirectVoiceCommand.Stop);
        ReaderCodexPush.NoteHangUpDecision(
            requestId, true,
            "通道没送出去，已按 F24 兜底");
        return await ConfirmHangUpAsync(
            controller,
            before,
            CodexVoiceActivityController.StopTransitionTimeout,
            requestId,
            "F24",
            cancellationToken).ConfigureAwait(false);
    }

    /// <summary>
    /// 等台账转为未通话，并把**实际耗时**记下来。
    /// </summary>
    /// <remarks>
    /// 记耗时是为了以后调 <c>StopTransitionTimeout</c> /
    /// <c>StopViaChannelTimeout</c> 时**有数据可依**。2026-09-10 只有一个
    /// 样本（按下到台账释放 ≈ 22 秒，而当时的上界是 5 秒），拿一个样本拍
    /// 常数正是这两个数字最初就拍错的原因。
    /// </remarks>
    private static async Task<CodexVoiceActivitySnapshot> ConfirmHangUpAsync(
        CodexVoiceActivityController controller,
        CodexVoiceActivitySnapshot before,
        TimeSpan timeout,
        string requestId,
        string via,
        CancellationToken cancellationToken)
    {
        long startedAt = Stopwatch.GetTimestamp();
        try
        {
            CodexVoiceActivitySnapshot after = await controller
                .ConfirmStoppedAsync(
                    before,
                    timeout,
                    CodexVoiceActivityController.MonitorInterval,
                    cancellationToken).ConfigureAwait(false);
            ReaderCodexPush.NoteHangUpDecision(
                requestId, true,
                string.Format(
                    CultureInfo.InvariantCulture,
                    "已挂断（经{0}），台账 {1:0.0} 秒后转为未通话（上界 {2:0} 秒）",
                    via,
                    Stopwatch.GetElapsedTime(startedAt).TotalSeconds,
                    timeout.TotalSeconds));
            return after;
        }
        catch (OperationCanceledException)
            when (cancellationToken.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception exception)
        {
            // ⚠ "没确认到"不等于"没挂掉"：2026-09-10 那次按键其实生效了，只是
            // 台账 22 秒后才翻，而上界是 5 秒。所以这里要把**等了多久**写进去，
            // 否则下一个人只看到一条"未确认"，仍然不知道该把上界调到多少。
            ReaderCodexPush.NoteHangUpDecision(
                requestId, false,
                string.Format(
                    CultureInfo.InvariantCulture,
                    "已请求挂断（经{0}），但 {1:0.0} 秒内没等到台账转为未通话"
                    + "（上界 {2:0} 秒）：{3}",
                    via,
                    Stopwatch.GetElapsedTime(startedAt).TotalSeconds,
                    timeout.TotalSeconds,
                    exception.Message));
            throw;
        }
    }

    /// <summary>正在通话的那条线程。</summary>
    /// <remarks>
    /// ⚠ **不能用推送绑定里的 threadId**：那是提示板推送的目标，通常不是通话
    /// 那条（2026-09-09 实测绑定 01a0847a 而通话 01a08560）。侧栏同步一直跟着
    /// 通话线程走，读它的 lastGood 即可。
    ///
    /// ⚠ 文件在 <c>~/.codex/</c>，不在桥 runtime 也不在 ReaderPC 的 local_root。
    /// Python 侧 <c>voice_autoclose.in_call_thread_id</c> 是同一份知识的第二个
    /// 实现，且原本指错了目录（2026-09-10 一起修）——改一处必须改两处。
    /// </remarks>
    internal static string InCallThreadId()
    {
        try
        {
            string path = InCallThreadSource();
            if (!File.Exists(path))
            {
                return string.Empty;
            }
            if (JsonNode.Parse(File.ReadAllText(path)) is not JsonObject root)
            {
                return string.Empty;
            }
            if (root["lastGood"] is not JsonObject lastGood)
            {
                return string.Empty;
            }
            string? threadId = (string?)lastGood["threadId"];
            return string.IsNullOrWhiteSpace(threadId)
                ? string.Empty
                : threadId;
        }
        catch (Exception)
        {
            return string.Empty;
        }
    }

    internal static string InCallThreadSource() => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
        ".codex",
        "voice-history-sidebar-sync-state.json");

    /// <summary>
    /// F24 兜底开关（2026-09-10 用户：「把 f24 兜底作为一个可选开关」）。
    /// 读不到一律当**开** —— 一个坏掉/缺失的偏好不该让语音开不了。
    /// </summary>
    /// <remarks>
    /// ⚠ 只此一份：起语音（<c>StartVoiceFromBridge</c>）、收敛挂断
    /// （<see cref="HangUpAsync"/>）、显式兜底 op（<c>hangUpVoiceFallback</c>）
    /// 三处都调这里。原来这个判断长在 DirectBridgeProtocolSession 里、只管起
    /// 语音那一处，于是"关掉开关之后挂断仍然按 F24"。
    /// </remarks>
    internal static bool ShortcutFallbackEnabled()
    {
        string? runtime = ReaderAttentionBoard.RuntimeDirectory;
        if (string.IsNullOrEmpty(runtime)) return true;
        try
        {
            string path = Path.Combine(
                runtime, "voice-shortcut-fallback.json");
            if (!File.Exists(path)) return true;
            if (JsonNode.Parse(File.ReadAllText(path)) is not JsonObject value)
            {
                return true;
            }
            if ((string?)value["contract"]
                != "reader-voice-shortcut-fallback/1")
            {
                return true;
            }
            return value["enabled"] is not JsonValue flag
                || !flag.TryGetValue(out bool enabled) || enabled;
        }
        catch (Exception)
        {
            return true;
        }
    }

    internal static async Task PrepareInitialStartAsync(
        IDirectAppLauncher launcher,
        Func<TimeSpan, CancellationToken, Task> delayAsync,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(launcher);
        ArgumentNullException.ThrowIfNull(delayAsync);
        DirectAppTargetProfile profile = DirectAppTargets.Require(
            DirectAppTargets.CodexDesktop);
        // The capability ledger may remain Available/Inactive after the app has
        // exited, so it cannot decide whether this is a cold launch. The
        // launcher reports that fact directly; readiness is always confirmed.
        bool started = await launcher.EnsureRunningAsync(
            profile.AppKind,
            profile.AppUserModelId,
            cancellationToken).ConfigureAwait(false);
        DirectAppTarget ready = await launcher.WaitForUniqueReadyAsync(
            profile.AppKind,
            profile.AppUserModelId,
            TimeSpan.FromSeconds(20),
            cancellationToken).ConfigureAwait(false);
        // 沉降按**这个 App 起来多久了**算，而不是按"是不是我们启动的"（2026-08-18 重做）。
        //
        // 旧写法是 `if (started)`：用户自己刚点开 Codex 时 started == false，
        // 于是一秒都不等 —— 而窗口句柄出现得比语音 UI 就绪早得多，F24 就落在
        // 一个还接不住它的窗口上。用户原话：「codex 刚启动时大概无法立刻打开语音，
        // 需要等待几秒」。谁启动的跟它准备好没有毫无关系，那是我们的视角，不是它的状态。
        TimeSpan upFor = UptimeOf(ready);
        TimeSpan settle = started
            ? RestartReadySettleDelay
            : RestartReadySettleDelay - upFor;
        if (settle > TimeSpan.Zero)
        {
            await delayAsync(settle, cancellationToken).ConfigureAwait(false);
        }
    }

    /// <summary>目标 App 已经运行了多久；读不出启动时间就按"已经很久"处理。</summary>
    /// <remarks>
    /// 读不出时**不等**：多等几秒的代价只是慢，而这里若因为解析不出时间就一律等，
    /// 每一次 START 都会平白多花 5 秒。真正需要等的是刚起来那一小段，
    /// 而那一段的启动时间一定是读得到的（进程就在眼前）。
    /// </remarks>
    internal static TimeSpan UptimeOf(DirectAppTarget target)
    {
        ArgumentNullException.ThrowIfNull(target);
        if (target.RootProcessStartFileTimeUtc <= 0)
        {
            return TimeSpan.MaxValue;
        }
        try
        {
            DateTime started = DateTime.FromFileTimeUtc(
                target.RootProcessStartFileTimeUtc);
            TimeSpan elapsed = DateTime.UtcNow - started;
            return elapsed < TimeSpan.Zero ? TimeSpan.Zero : elapsed;
        }
        catch (ArgumentOutOfRangeException)
        {
            return TimeSpan.MaxValue;
        }
    }

    private async Task MonitorKeepActiveAsync(
        CancellationToken cancellationToken)
    {
        using PeriodicTimer timer = new(_keepActivePollInterval);
        try
        {
            ReconcileKeepActive(initialReconcile: true, cancellationToken);
            while (await timer.WaitForNextTickAsync(cancellationToken)
                .ConfigureAwait(false))
            {
                ReconcileKeepActive(
                    initialReconcile: false,
                    cancellationToken);
            }
        }
        catch (OperationCanceledException)
            when (cancellationToken.IsCancellationRequested)
        {
        }
    }

    /// <summary>
    /// 把磁盘上的保活意图收敛到内存。
    /// </summary>
    /// <remarks>
    /// 2026-09-10 起是**纯同步**的：意图为假时这里不再挂断通话（见下），
    /// 于是没有任何需要 await 的动作。名字去掉 Async 是为了让"这一步不会
    /// 跑很久、也不会碰外部世界"在调用处一眼看得到。
    /// </remarks>
    private void ReconcileKeepActive(
        bool initialReconcile,
        CancellationToken serviceToken)
    {
        bool intentChanged = RefreshKeepActiveFromDisk(
            out bool enabled,
            out _,
            out _);
        if (!enabled)
        {
            // ## 收敛环**永不挂断**（2026-09-10 用户拍板重构）
            //
            // 用户原话：「把语音线路的连接和 ai 语音的在线解除绑定关系……即使
            // app 上的语音暂时断开，电脑上也不做出任何反应，除非是满足了智能
            // 开启设置的那些选项才使用自动关闭的功能」。
            //
            // 也就是说**挂断只有一个合法触发源：智能关闭**。音频线路怎么样、
            // 桥换了几代、App 断没断，都与"那通电话该不该继续"无关。
            //
            // 原来是 `intentChanged || initialReconcile` → 挂断。那是"保活=语音
            // 总开关"年代的写法：意图为假就等于语音该关着。**一次性启动方式
            // 落地后这个前提没了** —— 一次性方式下保活意图全程为假
            // （should_keep_alive 返回 False），它的含义是"别自动再开"，
            // 不是"把正在打的电话挂掉"。于是每一次桥换代都杀掉一通。
            //
            // 实录 2026-09-10：
            //   17:01:07.18  旧桥 media-fault，进程没了
            //   17:01:07.92  ReaderPC 保活拉起新一代（意图=假，一次性方式）
            //   17:01:08.18  新一代 service-start → 初次收敛 → 挂断
            //   17:01:30     通话真的结束（用户还在打）
            //
            // ⚠ 这个布尔**分不出**三件事：用户关掉了语音功能、一次性方式的常
            // 态、自动关闭刚撤的意图。分不出就不该动手 —— 真要挂断的那两条路
            // 都有各自的显式入口（ReaderPC 的 close_voice / hangUpVoiceFallback
            // op），信息在那儿是齐的。
            //
            // 代价：语音功能关着时，遗留的一通不会被自动收摊。那一通用户自己
            // 能挂，而挂错的那一通他挂不回来。
            if (intentChanged || initialReconcile)
            {
                try
                {
                    DirectCodexVoiceState state = ReadState();
                    if (state.Status == "available" && state.Active == true)
                    {
                        // 出声：**"我们选择不挂断"与"这一轮什么都没发生"在外面
                        // 长得一样**，而前者是新规则、后者是故障。
                        ReaderCodexPush.NoteHangUpDecision(
                            initialReconcile
                                ? "keepalive-initial-reconcile"
                                : "keepalive-intent-cleared",
                            true,
                            "台账显示在通话中，但保活意图为假不等于要挂断"
                            + "（音频线路与通话已解绑）；不动手");
                    }
                }
                catch (Exception)
                {
                    // 记不下来绝不能影响收敛本身。
                }
            }
            return;
        }
        StartAutomaticRecoveryIfNeeded(serviceToken);
    }

    private void StartAutomaticRecoveryIfNeeded(
        CancellationToken serviceToken)
    {
        if (
            Volatile.Read(ref _automaticRecoveryBlocked) != 0
            || !TryCaptureActiveIntent(
                out long generation,
                out CancellationToken intentToken)
        )
        {
            return;
        }
        lock (_automaticRecoveryTaskGate)
        {
            if (_automaticRecoveryTask is { IsCompleted: false })
            {
                return;
            }
            _automaticRecoveryTask = RunAutomaticRecoveryAsync(
                generation,
                intentToken,
                serviceToken);
        }
    }

    private async Task RunAutomaticRecoveryAsync(
        long generation,
        CancellationToken intentToken,
        CancellationToken serviceToken)
    {
        using CancellationTokenSource lifetime =
            CancellationTokenSource.CreateLinkedTokenSource(
                intentToken,
                serviceToken);
        while (!lifetime.IsCancellationRequested)
        {
            if (!TryCaptureAutomaticRecoveryAttempt(
                generation,
                out int priorFailureCount))
            {
                return;
            }
            try
            {
                if (priorFailureCount > 0)
                {
                    await _automaticRecoveryDelayAsync(
                        BackoffFor(priorFailureCount),
                        lifetime.Token).ConfigureAwait(false);
                    if (!TryCaptureAutomaticRecoveryAttempt(
                        generation,
                        out _))
                    {
                        return;
                    }
                }
                DirectCodexVoiceState state = ReadState();
                if (state.Status == "available" && state.Active == true)
                {
                    MarkAutomaticRecoverySucceeded(generation);
                    return;
                }
                _ = await SetActiveSerializedAsync(
                    active: true,
                    lifetime.Token).ConfigureAwait(false);
                MarkAutomaticRecoverySucceeded(generation);
                return;
            }
            catch (OperationCanceledException)
                when (lifetime.IsCancellationRequested)
            {
                return;
            }
            catch (Exception exception)
            {
                if (!RegisterAutomaticRecoveryFailure(
                    generation,
                    exception))
                {
                    return;
                }
            }
        }
    }

    private bool RefreshKeepActiveFromDisk(
        out bool enabled,
        out long generation,
        out CancellationToken intentToken)
    {
        if (
            _keepActivePath is null
            || !TryLoadKeepActive(_keepActivePath, out enabled)
        )
        {
            // An unreadable or partially replaced file must not invent a new
            // user intent. Explicit writers use atomic replacement; the last
            // fully validated value remains authoritative until the next poll.
            return CaptureCurrentIntent(
                out enabled,
                out generation,
                out intentToken,
                changed: false);
        }
        return ApplyKeepActiveIntent(
            enabled,
            out generation,
            out intentToken);
    }

    private bool ApplyKeepActiveIntent(
        bool enabled,
        out long generation,
        out CancellationToken intentToken)
    {
        CancellationTokenSource? previousLifetime = null;
        lock (_keepActiveIntentGate)
        {
            bool previous = _keepActive == 1;
            if (previous == enabled)
            {
                generation = _intentGeneration;
                intentToken = _intentLifetime.Token;
                return false;
            }
            previousLifetime = _intentLifetime;
            _retiredIntentLifetimes.Add(previousLifetime);
            _intentLifetime = new CancellationTokenSource();
            generation = ++_intentGeneration;
            intentToken = _intentLifetime.Token;
            Volatile.Write(ref _keepActive, enabled ? 1 : 0);
            _automaticRecoveryFailureCount = 0;
            Volatile.Write(
                ref _automaticRecoveryBlocked,
                enabled ? 0 : 1);
        }
        previousLifetime.Cancel();
        NotifyKeepActiveChanged(enabled);
        return true;
    }

    private bool CaptureCurrentIntent(
        out bool enabled,
        out long generation,
        out CancellationToken intentToken,
        bool changed)
    {
        lock (_keepActiveIntentGate)
        {
            enabled = _keepActive == 1;
            generation = _intentGeneration;
            intentToken = _intentLifetime.Token;
            return changed;
        }
    }

    private bool TryCaptureActiveIntent(
        out long generation,
        out CancellationToken intentToken)
    {
        lock (_keepActiveIntentGate)
        {
            generation = _intentGeneration;
            intentToken = _intentLifetime.Token;
            return _keepActive == 1;
        }
    }

    private bool TryCaptureAutomaticRecoveryAttempt(
        long generation,
        out int priorFailureCount)
    {
        lock (_keepActiveIntentGate)
        {
            priorFailureCount = _automaticRecoveryFailureCount;
            return _keepActive == 1
                && _intentGeneration == generation
                && _automaticRecoveryBlocked == 0;
        }
    }

    private void MarkAutomaticRecoverySucceeded(long generation)
    {
        lock (_keepActiveIntentGate)
        {
            if (
                _keepActive != 1
                || _intentGeneration != generation
            )
            {
                return;
            }
            _automaticRecoveryFailureCount = 0;
            Volatile.Write(ref _automaticRecoveryBlocked, 0);
        }
        // 恢复成功要**把上一次的失败销掉**。lastError 过去只写不清（清除只发生在
        // App 侧 START 成功路径），保活自愈成功不碰它 —— 于是界面上那个码可能是
        // 几分钟前某次尝试留下的旧账，排障时会把人带偏。
        try
        {
            _automaticRecoverySucceeded?.Invoke();
        }
        catch
        {
            // 通知失败不该影响"已经恢复"这个事实。
        }
    }

    /// <summary>连败 n 次后该等多久：前三次固定 20s，之后翻倍，封顶 5 分钟。</summary>
    /// <remarks>
    /// ⚠ 前三次**不翻倍**（2026-08-30 实测定的）：正常冷启动里热键在启动后
    /// 45-60 秒才接得住，第一按（+5s）注定落空 —— 指数退避在恰好要成的
    /// 那个窗口跨大步错过，用户看到的"等很久"一半是它贡献的。
    /// 前三次平铺让按键落在 ~40s/~62s/~84s，盖住正常就绪窗口；
    /// 之后仍翻倍 —— 连败到第四次说明不是"还没就绪"而是卡死
    /// （实测：崩溃页/僵死的 App 按到天亮也没用），该退避并交给
    /// 健康通知去喊人，而不是继续密集敲一扇死门。
    /// </remarks>
    internal static TimeSpan BackoffFor(int priorFailureCount)
    {
        if (priorFailureCount <= 0)
        {
            return TimeSpan.Zero;
        }
        double seconds = priorFailureCount <= 3
            ? AutomaticRecoveryRetryDelay.TotalSeconds
            : AutomaticRecoveryRetryDelay.TotalSeconds
                * Math.Pow(2, Math.Min(priorFailureCount - 3, 8));
        double capped = Math.Min(
            seconds,
            AutomaticRecoveryMaximumRetryDelay.TotalSeconds);
        return TimeSpan.FromSeconds(capped);
    }

    private bool RegisterAutomaticRecoveryFailure(
        long generation,
        Exception exception)
    {
        bool shouldRetry;
        lock (_keepActiveIntentGate)
        {
            if (
                _keepActive != 1
                || _intentGeneration != generation
            )
            {
                return false;
            }
            _automaticRecoveryFailureCount++;
            // 只要用户的意图还在（keepActive 且同代），就继续试 —— 失败次数只用来
            // 决定下一次等多久（见 AutomaticRecoveryMaximumRetryDelay）。
            // 这里过去会在预算用尽或失败"看着不像暂时的"时上闩；而判定是否暂时
            // 本身就不可靠（例如读 keybindings.json 失败被归成非暂时），
            // 一次误判换来的是永久熄火。
            shouldRetry = true;
            Volatile.Write(ref _automaticRecoveryBlocked, 0);
        }
        NotifyAutomaticRecoveryFailed(exception);
        return shouldRetry;
    }

    private static bool IsTransientAutomaticRecoveryFailure(
        Exception exception)
    {
        return exception is TimeoutException
            || exception is DirectProtocolException protocol
            && (
                protocol.Retryable
                || protocol.Code is
                    "BW_COMPUTER_VOICE_DIRECT_APP_AMBIGUOUS"
                    or "BW_COMPUTER_VOICE_APP_TREE_AMBIGUOUS"
                    or "BW_COMPUTER_VOICE_APP_WINDOW_AMBIGUOUS"
            );
    }

    private void NotifyAutomaticRecoveryFailed(Exception exception)
    {
        try
        {
            _automaticRecoveryFailed?.Invoke(exception);
        }
        catch
        {
            // Diagnostics must never create a second recovery loop.
        }
    }

    private void NotifyKeepActiveChanged(bool enabled)
    {
        try
        {
            _keepActiveChanged?.Invoke(enabled);
        }
        catch
        {
            // The persisted intent remains authoritative even if its optional
            // presentation callback cannot update immediately.
        }
    }

    private static bool LoadKeepActive(string? path)
    {
        return path is not null
            && TryLoadKeepActive(path, out bool enabled)
            && enabled;
    }

    private static bool TryLoadKeepActive(
        string path,
        out bool enabled)
    {
        enabled = false;
        if (!File.Exists(path))
        {
            return false;
        }
        try
        {
            using JsonDocument document = JsonDocument.Parse(
                File.ReadAllText(path, Encoding.UTF8));
            JsonElement root = document.RootElement;
            if (
                root.ValueKind != JsonValueKind.Object
                || root.GetRawText().Length > 1024
                || root.EnumerateObject().Count() != 2
                || !root.TryGetProperty("contract", out JsonElement contract)
                || contract.GetString()
                    != "reader-codex-voice-keepalive/1"
                || !root.TryGetProperty(
                    "enabled",
                    out JsonElement enabledElement)
                || enabledElement.ValueKind is not (
                    JsonValueKind.True or JsonValueKind.False)
            )
            {
                return false;
            }
            enabled = enabledElement.GetBoolean();
            return true;
        }
        catch
        {
            return false;
        }
    }

    private static void SaveKeepActive(string? path, bool enabled)
    {
        if (path is null)
        {
            return;
        }
        string directory = System.IO.Path.GetDirectoryName(path)
            ?? throw new InvalidOperationException(
                "Codex 语音持续运行配置目录无效");
        Directory.CreateDirectory(directory);
        string temporary = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            File.WriteAllText(
                temporary,
                JsonSerializer.Serialize(new
                {
                    contract = "reader-codex-voice-keepalive/1",
                    enabled,
                }),
                new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
            File.Move(temporary, path, overwrite: true);
        }
        finally
        {
            try { File.Delete(temporary); } catch { }
        }
    }

    public async ValueTask DisposeAsync()
    {
        if (Interlocked.Exchange(ref _disposeStarted, 1) != 0)
        {
            return;
        }

        List<Exception>? failures = null;
        // Freeze disk reconciliation before publishing the terminal false
        // intent. Otherwise a poll already in flight can re-apply stale true
        // while shutdown is trying to confirm the one allowed stop shortcut.
        _keepActiveLifetime?.Cancel();
        try
        {
            if (_keepActiveMonitor is not null)
            {
                await _keepActiveMonitor.ConfigureAwait(false);
            }
        }
        catch (OperationCanceledException)
        {
        }
        catch (Exception exception)
        {
            failures ??= [];
            failures.Add(exception);
        }

        // ## 退出**不再挂断通话**（2026-09-10 用户拍板重构）
        //
        // 原来这里 SetKeepActiveAsync(false)，而那条路在通话中会按停并要求确认，
        // 确认不到就记一条 DISPOSE_STOP_UNCONFIRMED/TIMEOUT。
        //
        // 问题是**桥退出与"那通电话该不该继续"无关**。桥每天要换好几代（装新版、
        // ReaderPC 接管、保活重拉），每一代退出都把用户正在打的电话按掉，下一代
        // 起来又不知道该不该开回去。用户：「即使 app 上的语音暂时断开，电脑上
        // 也不做出任何反应」—— 桥自己收摊更是如此。
        //
        // 现在只做**放弃意图**这一件事：把意图落成 false，让下一代不会误以为
        // 要自动开；通话留给它自己的生命周期（用户挂、或智能关闭挂）。
        try
        {
            SaveKeepActive(_keepActivePath, false);
            _ = ApplyKeepActiveIntent(false, out _, out _);
            DirectCodexVoiceState state = ReadState();
            if (state.Status == "available" && state.Active == true)
            {
                // 出声：这一代桥退出时通话还在，是**有意**留着的。
                ReaderCodexPush.NoteHangUpDecision(
                    "dispose-leaves-call-running",
                    true,
                    "桥退出，通话仍在进行 —— 按解绑规则不挂断");
            }
        }
        catch (Exception exception)
        {
            failures ??= [];
            failures.Add(exception);
        }

        Task? automaticRecovery;
        lock (_automaticRecoveryTaskGate)
        {
            automaticRecovery = _automaticRecoveryTask;
        }
        if (automaticRecovery is not null)
        {
            try
            {
                await automaticRecovery.ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
            }
            catch (Exception exception)
            {
                failures ??= [];
                failures.Add(exception);
            }
        }
        CancellationTokenSource currentIntent;
        lock (_keepActiveIntentGate)
        {
            currentIntent = _intentLifetime;
        }
        currentIntent.Cancel();
        _keepActiveLifetime?.Dispose();
        lock (_keepActiveIntentGate)
        {
            _intentLifetime.Dispose();
            foreach (CancellationTokenSource retired in
                _retiredIntentLifetimes)
            {
                retired.Dispose();
            }
            _retiredIntentLifetimes.Clear();
        }
        if (failures is { Count: 1 })
        {
            throw failures[0];
        }
        if (failures is { Count: > 1 })
        {
            throw new AggregateException(failures);
        }
    }

    private static CodexAppTarget RequireCodexTarget()
    {
        try
        {
            return WindowsCodexAppProbe.RequireReady(
                DirectAppTargets.CodexDesktop);
        }
        catch (DirectProtocolException)
        {
            throw;
        }
        catch (Exception exception)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_SHORTCUT_TARGET_UNAVAILABLE",
                "无法确认唯一的 Codex 快捷键目标",
                retryable: true,
                innerException: exception);
        }
    }
}

internal sealed class DirectBridgeProtocolSession
{
    private readonly string _connectionId;
    private readonly DirectBridgeConfigStore _configStore;
    private readonly DirectBridgeCoordinator _coordinator;
    private readonly IDirectCodexVoiceControl _codexVoiceControl;
    private readonly IReaderDictionaryFallback? _dictionaryFallback;
    private readonly IReaderLocalAnkiWriter? _localAnkiWriter;
    private readonly Func<string, ReaderContextSourceLease>
        _registerReaderSource;
    private readonly Func<
        ReaderVisualDeliveryChunk,
        ReaderVisualDeliveryAck> _acceptReaderVisual;
    private readonly Action<ReaderBrowserControlResponse>
        _acceptReaderBrowserControl;
    private readonly Action<ReaderQueryResponse>
        _acceptReaderQuery;
    private readonly Action<ReaderRealtimeOutputAck>
        _acceptReaderRealtimeOutput;
    private readonly Func<
        ReplicationCommandEnvelope,
        CancellationToken,
        Task<ReplicationCommandIntakeReceipt>> _acceptReplicationCommand;
    private readonly Func<string, object> _queryReplicationDigests;
    private readonly Func<object> _queryReplicationNotifications;
    private readonly ReplicationChunkAssembler _replicationChunkAssembler = new();
    private readonly Action<string> _contextDeliveryModeChanged;
    private readonly Func<DateTimeOffset> _utcNow;
    private readonly bool _bridgeOnlyMode;
    private readonly bool _voiceEnabled;
    private readonly Action<string?, bool?>? _writeServiceModeIntent;
    private bool _helloSeen;
    private bool _authenticated;
    private string? _contextDeliveryMode;
    private string? _contextOnlySessionId;
    private string? _activeVoiceSessionId;
    private string? _activeVoiceAppKind;
    private string? _registeredSourceInstanceId;
    private DirectProtocolPhase _phase =
        DirectProtocolPhase.AwaitingAuthentication;

    internal DirectBridgeProtocolSession(
        string connectionId,
        string origin,
        DirectBridgeConfigStore configStore,
        DirectBridgeCoordinator coordinator,
        Func<DateTimeOffset>? utcNow = null,
        IDirectCodexVoiceControl? codexVoiceControl = null,
        Func<string, ReaderContextSourceLease>?
            registerReaderSource = null,
        Func<
            ReaderVisualDeliveryChunk,
            ReaderVisualDeliveryAck>? acceptReaderVisual = null,
        Action<ReaderBrowserControlResponse>?
            acceptReaderBrowserControl = null,
        Action<ReaderQueryResponse>?
            acceptReaderQuery = null,
        Action<ReaderRealtimeOutputAck>?
            acceptReaderRealtimeOutput = null,
        Func<
            ReplicationCommandEnvelope,
            CancellationToken,
            Task<ReplicationCommandIntakeReceipt>>?
            acceptReplicationCommand = null,
        Func<string, object>? queryReplicationDigests = null,
        Func<object>? queryReplicationNotifications = null,
        Action<string>? contextDeliveryModeChanged = null,
        IReaderDictionaryFallback? dictionaryFallback = null,
        IReaderLocalAnkiWriter? localAnkiWriter = null,
        bool bridgeOnlyMode = false,
        bool voiceEnabled = true,
        Action<string?, bool?>? writeServiceModeIntent = null)
    {
        if (!DirectBridgeContract.IsSafeId(connectionId))
        {
            throw new ArgumentException(
                "connectionId must be a safe identifier",
                nameof(connectionId));
        }
        _connectionId = connectionId;
        _configStore = configStore;
        _coordinator = coordinator;
        _codexVoiceControl = codexVoiceControl
            ?? DirectCodexVoiceControl.Shared;
        _dictionaryFallback = dictionaryFallback;
        _localAnkiWriter = localAnkiWriter;
        _registerReaderSource = registerReaderSource
            ?? (_ => throw new DirectProtocolException(
                "BW_READER_VISUAL_UNAVAILABLE",
                "Reader 视觉来源路由尚未接线",
                retryable: true));
        _acceptReaderVisual = acceptReaderVisual
            ?? (_ => throw new DirectProtocolException(
                "BW_READER_VISUAL_UNAVAILABLE",
                "Reader 视觉接收器尚未接线",
                retryable: true));
        _acceptReaderBrowserControl = acceptReaderBrowserControl
            ?? (_ => throw new DirectProtocolException(
                "BW_READER_BROWSER_CONTROL_UNAVAILABLE",
                "Reader 浏览控制接收器尚未接线",
                retryable: true));
        _acceptReaderQuery = acceptReaderQuery
            ?? (_ => throw new DirectProtocolException(
                "BW_READER_QUERY_UNAVAILABLE",
                "Reader 查询接收器尚未接线",
                retryable: true));
        _acceptReaderRealtimeOutput = acceptReaderRealtimeOutput
            ?? (_ => throw new DirectProtocolException(
                "BW_READER_REALTIME_OUTPUT_UNAVAILABLE",
                "Reader 输出接收器尚未接线",
                retryable: true));
        _acceptReplicationCommand = acceptReplicationCommand
            ?? ((_, _) => throw new DirectProtocolException(
                "BW_REPLICATION_COMMAND_UNAVAILABLE",
                "复制命令接收器尚未接线",
                retryable: true));
        _queryReplicationDigests = queryReplicationDigests
            ?? (_ => throw new DirectProtocolException(
                "BW_REPLICATION_DIGESTS_UNAVAILABLE",
                "复制摘要查询尚未接线",
                retryable: true));
        _queryReplicationNotifications = queryReplicationNotifications
            ?? (static object () => throw new DirectProtocolException(
                "BW_REPLICATION_NOTIFICATIONS_UNAVAILABLE",
                "通知查询尚未接线",
                retryable: true));
        _contextDeliveryModeChanged = contextDeliveryModeChanged
            ?? (_ => { });
        _utcNow = utcNow ?? (() => DateTimeOffset.UtcNow);
        _bridgeOnlyMode = bridgeOnlyMode;
        _voiceEnabled = voiceEnabled;
        _writeServiceModeIntent = writeServiceModeIntent;
    }

    // 桥接模式:语音留在电脑本机(Codex 保活照常),只拒 START——那是把音频
    // 路由到虚拟设备、PCM 隧道到 App 的动作。codex-voice-set/keepalive-set
    // 不闸:远程开关电脑本机的语音不涉及音频路由。
    private void RequireVoiceAllowed()
    {
        if (!_voiceEnabled)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_VOICE_DISABLED",
                "ReaderPC 语音功能已关闭；快照与其它非语音工具仍可用。");
        }
        if (_bridgeOnlyMode)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_BRIDGE_ONLY",
                "桥接模式:语音在电脑本机运行,通话不接到 App。要把通话接过来,请切回完整模式。");
        }
    }

    internal bool Authenticated => _authenticated;

    internal bool IsAuthenticated => _authenticated;

    internal DirectProtocolPhase Phase => _phase;

    internal async Task<DirectProtocolReply> HandleAsync(
        string json,
        Func<string, string, Task> reportStatusAsync,
        Func<string, DirectPcmFrame, CancellationToken, Task>
            sendPcmFrameAsync,
        CancellationToken cancellationToken)
    {
        string requestId = "invalid";
        string action = "unknown";
        try
        {
            if (
                Encoding.UTF8.GetByteCount(json)
                    > DirectBridgeContract.MaximumMessageBytes
            )
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_MESSAGE_TOO_LARGE",
                    "消息超过大小上限");
            }
            using JsonDocument document = JsonDocument.Parse(
                json,
                new JsonDocumentOptions
                {
                    AllowTrailingCommas = false,
                    CommentHandling = JsonCommentHandling.Disallow,
                    MaxDepth = 16,
                });
            JsonElement message = document.RootElement;
            RequireObject(message);
            DirectJsonValidation.RequireNoDuplicateKeys(message);
            requestId = RequireSafeId(message, "requestId");
            if (RequireString(message, "contract", 128)
                != DirectBridgeContract.Contract)
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_CONTRACT_INVALID",
                    "直连消息合同不匹配");
            }
            action = RequireString(message, "type", 32);
            object payload;
            Func<CancellationToken, Task>? afterSend = null;
            switch (action)
            {
                case "hello":
                    payload = HandleHello(message);
                    break;
                case "status":
                    payload = HandleStatus(message);
                    break;
                case "codex-voice-set":
                    payload = await HandleCodexVoiceSetAsync(
                        message,
                        cancellationToken).ConfigureAwait(false);
                    break;
                case "codex-voice-keepalive-set":
                    payload = await HandleCodexVoiceKeepAliveSetAsync(
                        message,
                        cancellationToken).ConfigureAwait(false);
                    break;
                case "dictionary-lookup":
                    payload = await HandleDictionaryLookupAsync(
                        message,
                        cancellationToken).ConfigureAwait(false);
                    break;
                case "anki-add-cards-local":
                    payload = await HandleLocalAnkiAddAsync(
                        message,
                        cancellationToken).ConfigureAwait(false);
                    break;
                case "anki-card-operation-local":
                    payload = await HandleLocalAnkiOperationAsync(
                        message,
                        cancellationToken).ConfigureAwait(false);
                    break;
                case "context-mode":
                    payload = HandleContextMode(message);
                    break;
                case "context-mode-set":
                    payload = HandleContextModeSet(message);
                    break;
                case "service-mode-set":
                    payload = HandleServiceModeSet(message);
                    break;
                case "context-open":
                    payload = HandleContextOpen(message);
                    break;
                case ReaderVisualDeliveryProtocol.RegisterType:
                    payload = HandleVisualRegister(message);
                    break;
                case ReaderVisualDeliveryProtocol.ChunkType:
                    payload = HandleReaderVisual(message);
                    break;
                case ReaderBrowserControlProtocol.ResponseType:
                    payload = HandleReaderBrowserControl(message);
                    break;
                case ReaderQueryProtocol.ResponseType:
                    payload = HandleReaderQuery(message);
                    break;
                case ReaderRealtimeOutputProtocol.AckType:
                    payload = HandleReaderRealtimeOutput(message);
                    break;
                case ReplicationCommandProtocol.CommandType:
                    payload = await HandleReplicationCommandAsync(
                        message,
                        cancellationToken).ConfigureAwait(false);
                    break;
                case ReplicationCommandProtocol.DigestQueryType:
                    payload = HandleReplicationDigestQuery(message);
                    break;
                case ReplicationCommandProtocol.NotificationsQueryType:
                    payload = HandleReplicationNotificationsQuery(message);
                    break;
                case ReplicationCommandProtocol.ChunkType:
                    payload = await HandleReplicationChunkAsync(
                        message,
                        cancellationToken).ConfigureAwait(false);
                    break;
                case "start":
                    DirectStartActionResult start =
                        await HandleStartAsync(
                            message,
                            reportStatusAsync,
                            sendPcmFrameAsync,
                            cancellationToken).ConfigureAwait(false);
                    payload = start.Payload;
                    afterSend = start.AfterSendAsync;
                    break;
                case "heartbeat":
                    payload = await HandleHeartbeatAsync(
                        message,
                        cancellationToken).ConfigureAwait(false);
                    break;
                case "context":
                    payload = await HandleContextAsync(
                        message,
                        cancellationToken).ConfigureAwait(false);
                    break;
                case "active-reading":
                    payload = await HandleActiveReadingAsync(
                        message,
                        cancellationToken).ConfigureAwait(false);
                    break;
                case "context-clear":
                    payload = await HandleContextClearAsync(
                        message,
                        cancellationToken).ConfigureAwait(false);
                    break;
                case "log":
                    payload = await HandleExtensionLogAsync(
                        message,
                        cancellationToken).ConfigureAwait(false);
                    break;
                case "stop":
                    payload = await HandleStopAsync(
                        message,
                        cancellationToken).ConfigureAwait(false);
                    break;
                default:
                    throw new DirectProtocolException(
                        "BW_COMPUTER_VOICE_DIRECT_ACTION_INVALID",
                        "不支持的直连操作");
            }
            return new DirectProtocolReply(
                Success(requestId, action, payload),
                afterSend);
        }
        catch (DirectProtocolException exception)
        {
            return new DirectProtocolReply(
                Failure(
                    requestId,
                    action,
                    exception.Code,
                    exception.Message,
                    exception.Retryable),
                AfterSendAsync: null);
        }
        catch (
            Exception exception
        ) when (
            exception is JsonException
            or FormatException
            or InvalidOperationException
            or ArgumentException
        )
        {
            return new DirectProtocolReply(
                Failure(
                    requestId,
                    action,
                    "BW_COMPUTER_VOICE_DIRECT_MESSAGE_INVALID",
                    "直连消息无效",
                    retryable: false),
                AfterSendAsync: null);
        }
    }

    private object HandleHello(JsonElement message)
    {
        if (_helloSeen || _authenticated)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_HELLO_REPEATED",
                "每条连接只能发送一次 hello");
        }
        _helloSeen = true;
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId",
            "protocolVersion");
        JsonElement protocolVersion = message.GetProperty(
            "protocolVersion");
        if (
            protocolVersion.ValueKind != JsonValueKind.Number
            || !protocolVersion.TryGetInt32(out int version)
            || version != 3
        )
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_PROTOCOL_VERSION_INVALID",
                "直连协议版本不受支持");
        }
        DirectBridgeConfig config = _configStore.Load();
        if (!config.ExperimentalSingleUserMode)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_CONFIG_INVALID",
                "v3 仅支持固定单用户实验模式");
        }

        _authenticated = true;
        _contextDeliveryMode = config.ContextDeliveryMode;
        _phase = DirectProtocolPhase.AwaitingStart;
        return new
        {
            protocolVersion = 3,
            limits = new
            {
                maxMessageBytes =
                    DirectBridgeContract.MaximumMessageBytes,
                pcmFrameBytes = DirectBridgeContract.PcmFrameBytes,
                pcmQueueLimitMs =
                    DirectBridgeContract.PcmQueueLimitMilliseconds,
                uplinkTrack =
                    (byte)DirectPcmTrack.BrowserMicrophone,
                uplinkQueueLimitMs =
                    DirectBridgeContract
                        .UplinkPcmQueueLimitMilliseconds,
                heartbeatIntervalMs =
                    DirectBridgeContract
                        .ClientHeartbeatIntervalMilliseconds,
                heartbeatTimeoutMs =
                    DirectBridgeContract
                        .ClientHeartbeatTimeoutMilliseconds,
            },
        };
    }

    private object HandleContextMode(JsonElement message)
    {
        // serviceMode 按请求自愿升级:客户端带 wantServiceMode:true 才回新字段。
        // 无条件加字段会炸旧客户端的 exactObject 校验(已装 App 的 bundle 改不了);
        // 旧客户端不发该键 → 回执与历史逐字节同形。每次快照连接与前台验活都会
        // 重新查 context-mode,桥接/完整状态天然保鲜,图标据此分支。
        bool wantServiceMode = message.TryGetProperty(
            "wantServiceMode",
            out JsonElement wantValue)
            && wantValue.ValueKind == JsonValueKind.True;
        bool wantVoiceEnabled = message.TryGetProperty(
            "wantVoiceEnabled",
            out JsonElement wantVoiceValue)
            && wantVoiceValue.ValueKind == JsonValueKind.True;
        if (wantVoiceEnabled && !wantServiceMode)
        {
            throw new DirectProtocolException(
                "BW_READERPC_SERVICE_MODE_INVALID",
                "voiceEnabled 状态只能和 serviceMode 一起读取");
        }
        if (wantVoiceEnabled)
        {
            RequireExactKeys(
                message,
                "contract",
                "type",
                "requestId",
                "wantServiceMode",
                "wantVoiceEnabled");
        }
        else if (wantServiceMode)
        {
            RequireExactKeys(
                message,
                "contract",
                "type",
                "requestId",
                "wantServiceMode");
        }
        else
        {
            RequireExactKeys(
                message,
                "contract",
                "type",
                "requestId");
        }
        RequireAuthenticated();
        if (wantVoiceEnabled)
        {
            return new
            {
                mode = RequireContextDeliveryMode(),
                serviceMode = _bridgeOnlyMode ? "bridge-only" : "full",
                voiceEnabled = _voiceEnabled,
            };
        }
        if (wantServiceMode)
        {
            return new
            {
                mode = RequireContextDeliveryMode(),
                serviceMode = _bridgeOnlyMode ? "bridge-only" : "full",
            };
        }
        return new
        {
            mode = RequireContextDeliveryMode(),
        };
    }

    private object HandleServiceModeSet(JsonElement message)
    {
        // App 设置面板遥控 ReaderPC 模式:这里只写意图文件;真正的停旧代际→按新
        // 模式重启由 ReaderPC 的收敛循环执行(它是唯一的服务生命周期所有者)。
        bool hasMode = message.TryGetProperty("mode", out _);
        bool hasVoiceEnabled = message.TryGetProperty(
            "voiceEnabled",
            out _);
        if (!hasMode && !hasVoiceEnabled)
        {
            throw new DirectProtocolException(
                "BW_READERPC_SERVICE_MODE_INVALID",
                "至少需要指定 serviceMode 或 voiceEnabled");
        }
        if (hasMode && hasVoiceEnabled)
        {
            RequireExactKeys(
                message,
                "contract",
                "type",
                "requestId",
                "mode",
                "voiceEnabled");
        }
        else if (hasMode)
        {
            RequireExactKeys(
                message,
                "contract",
                "type",
                "requestId",
                "mode");
        }
        else
        {
            RequireExactKeys(
                message,
                "contract",
                "type",
                "requestId",
                "voiceEnabled");
        }
        RequireAuthenticated();
        string? mode = hasMode
            ? RequireString(message, "mode", 16)
            : null;
        if (mode is not null
            && mode is not ("full" or "bridge-only"))
        {
            throw new DirectProtocolException(
                "BW_READERPC_SERVICE_MODE_INVALID",
                "服务模式只能是 full 或 bridge-only");
        }
        if (_writeServiceModeIntent is null)
        {
            throw new DirectProtocolException(
                "BW_READERPC_SERVICE_MODE_UNAVAILABLE",
                "服务模式意图写入尚未接线");
        }
        bool? voiceEnabled = hasVoiceEnabled
            ? RequireBoolean(message, "voiceEnabled")
            : null;
        _writeServiceModeIntent(mode, voiceEnabled);
        if (hasMode && hasVoiceEnabled)
        {
            return new
            {
                serviceMode = mode,
                voiceEnabled,
                applied = "pending-restart",
            };
        }
        if (hasVoiceEnabled)
        {
            return new
            {
                voiceEnabled,
                applied = "pending-restart",
            };
        }
        return new
        {
            serviceMode = mode,
            applied = "pending-restart",
        };
    }

    private object HandleContextModeSet(JsonElement message)
    {
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId",
            "mode",
            "sessionId");
        RequireAuthenticated();
        string sessionId = RequireSafeId(message, "sessionId");
        _ = DirectPcmFrameCodec.ParseSessionId(sessionId);
        string mode = RequireString(message, "mode", 32);
        if (!DirectContextDeliveryMode.IsSupported(mode))
        {
            throw new DirectProtocolException(
                "BW_READER_CONTEXT_DELIVERY_MODE_INVALID",
                "Reader 上下文交付模式无效");
        }
        if (
            _phase != DirectProtocolPhase.AwaitingStart
            || _contextOnlySessionId is not null
            || _coordinator.ActiveSessionId is not null
            || _coordinator.CaptureActive
            || _coordinator.CleanupPending
        )
        {
            throw new DirectProtocolException(
                "BW_READER_CONTEXT_DELIVERY_MODE_BUSY",
                "请先结束电脑语音并清理旧上下文链路",
                retryable: true);
        }

        string previousMode =
            _configStore.SetContextDeliveryMode(mode);
        _contextDeliveryMode = mode;
        _contextDeliveryModeChanged(mode);
        return new
        {
            mode,
            previousMode,
        };
    }

    private object HandleContextOpen(JsonElement message)
    {
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId",
            "sessionId");
        RequireAuthenticated();
        if (
            RequireContextDeliveryMode()
                != DirectContextDeliveryMode.SnapshotMcp
        )
        {
            throw new DirectProtocolException(
                "BW_READER_CONTEXT_SNAPSHOT_MODE_REQUIRED",
                "Windows 未启用 Reader 快照 MCP 实验模式");
        }
        if (_phase != DirectProtocolPhase.AwaitingStart)
        {
            throw new DirectProtocolException(
                "BW_READER_CONTEXT_SNAPSHOT_PHASE_INVALID",
                "当前连接不能切换为纯上下文连接");
        }
        string sessionId = RequireSafeId(message, "sessionId");
        _ = DirectPcmFrameCodec.ParseSessionId(sessionId);
        _contextOnlySessionId = sessionId;
        _phase = DirectProtocolPhase.ContextOnly;
        return new
        {
            sessionId,
            state = "context-only",
            mode = DirectContextDeliveryMode.SnapshotMcp,
        };
    }

    private object HandleVisualRegister(JsonElement message)
    {
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId",
            "sessionId",
            "sourceInstanceId");
        RequireAuthenticated();
        if (
            RequireContextDeliveryMode()
                != DirectContextDeliveryMode.SnapshotMcp
            || _phase != DirectProtocolPhase.ContextOnly
        )
        {
            throw new DirectProtocolException(
                "BW_READER_VISUAL_CONTEXT_ONLY_REQUIRED",
                "Reader 视觉来源只允许在纯上下文连接中注册");
        }
        string sessionId = RequireSafeId(message, "sessionId");
        _ = DirectPcmFrameCodec.ParseSessionId(sessionId);
        RequireContextOnlySession(sessionId);
        if (_registeredSourceInstanceId is not null)
        {
            throw new DirectProtocolException(
                "BW_READER_VISUAL_SOURCE_REPEATED",
                "每条 Reader 上下文连接只能注册一次视觉来源");
        }
        string sourceInstanceId = RequireSafeId(
            message,
            "sourceInstanceId");
        _ = _registerReaderSource(sourceInstanceId);
        _registeredSourceInstanceId = sourceInstanceId;
        return new
        {
            sessionId,
            sourceInstanceId,
            state = "registered",
        };
    }

    private object HandleReaderVisual(JsonElement message)
    {
        RequireAuthenticated();
        if (
            RequireContextDeliveryMode()
                != DirectContextDeliveryMode.SnapshotMcp
            || _phase != DirectProtocolPhase.ContextOnly
        )
        {
            throw new DirectProtocolException(
                "BW_READER_VISUAL_CONTEXT_ONLY_REQUIRED",
                "Reader 视觉只允许在纯上下文连接中回传");
        }
        ReaderVisualDeliveryChunk chunk =
            ReaderVisualDeliveryProtocol.ValidateChunk(message);
        RequireContextOnlySession(chunk.SessionId);
        if (
            _registeredSourceInstanceId is null
            || !string.Equals(
                _registeredSourceInstanceId,
                chunk.SourceInstanceId,
                StringComparison.Ordinal)
        )
        {
            throw new DirectProtocolException(
                "BW_READER_VISUAL_SOURCE_MISMATCH",
                "Reader 视觉回传来源与当前连接不匹配");
        }
        ReaderVisualDeliveryAck ack = _acceptReaderVisual(chunk);
        return new
        {
            correlation = ack.Correlation,
            chunkIndex = ack.ChunkIndex,
            accepted = ack.Accepted,
            complete = ack.Complete,
        };
    }

    private object HandleReaderBrowserControl(JsonElement message)
    {
        RequireAuthenticated();
        if (
            RequireContextDeliveryMode()
                != DirectContextDeliveryMode.SnapshotMcp
            || _phase != DirectProtocolPhase.ContextOnly
        )
        {
            throw new DirectProtocolException(
                "BW_READER_BROWSER_CONTROL_CONTEXT_ONLY_REQUIRED",
                "Reader 浏览控制只允许在纯上下文连接中回传");
        }
        ReaderBrowserControlResponse response =
            ReaderBrowserControlProtocol.ValidateResponse(message);
        RequireContextOnlySession(response.SessionId);
        if (
            _registeredSourceInstanceId is null
            || !string.Equals(
                _registeredSourceInstanceId,
                response.SourceInstanceId,
                StringComparison.Ordinal)
        )
        {
            throw new DirectProtocolException(
                "BW_READER_BROWSER_CONTROL_SOURCE_MISMATCH",
                "Reader 浏览控制回传来源与当前连接不匹配");
        }
        _acceptReaderBrowserControl(response);
        return new
        {
            correlation = response.Correlation,
            accepted = true,
        };
    }

    // 与浏览控制同样的三道守卫：必须是快照模式、必须是纯上下文连接、来源必须
    // 就是本连接注册的那一个。少任何一道，另一个页面就能替这本书回答。
    private object HandleReaderQuery(JsonElement message)
    {
        if (
            RequireContextDeliveryMode()
                != DirectContextDeliveryMode.SnapshotMcp
            || _phase != DirectProtocolPhase.ContextOnly
        )
        {
            throw new DirectProtocolException(
                "BW_READER_QUERY_CONTEXT_ONLY_REQUIRED",
                "Reader 查询只允许在纯上下文连接中回传");
        }
        ReaderQueryResponse response =
            ReaderQueryProtocol.ValidateResponse(message);
        RequireContextOnlySession(response.SessionId);
        if (
            _registeredSourceInstanceId is null
            || !string.Equals(
                _registeredSourceInstanceId,
                response.SourceInstanceId,
                StringComparison.Ordinal)
        )
        {
            throw new DirectProtocolException(
                "BW_READER_QUERY_SOURCE_MISMATCH",
                "Reader 查询回传来源与当前连接不匹配");
        }
        _acceptReaderQuery(response);
        return new
        {
            correlation = response.Correlation,
            accepted = true,
        };
    }

    private object HandleReaderRealtimeOutput(JsonElement message)
    {
        RequireAuthenticated();
        if (
            RequireContextDeliveryMode()
                != DirectContextDeliveryMode.SnapshotMcp
            || _phase != DirectProtocolPhase.ContextOnly
        )
        {
            throw new DirectProtocolException(
                "BW_READER_REALTIME_OUTPUT_CONTEXT_ONLY_REQUIRED",
                "Reader 输出回执只允许在纯上下文连接中回传");
        }
        ReaderRealtimeOutputAck ack;
        try
        {
            ack = ReaderRealtimeOutputProtocol.ValidateAck(message);
        }
        catch (ReaderRealtimeOutputException exception)
        {
            throw new DirectProtocolException(
                exception.Code,
                exception.Message,
                exception.Retryable);
        }
        RequireContextOnlySession(ack.SessionId);
        if (
            _registeredSourceInstanceId is null
            || !string.Equals(
                _registeredSourceInstanceId,
                ack.SourceInstanceId,
                StringComparison.Ordinal)
        )
        {
            throw new DirectProtocolException(
                "BW_READER_REALTIME_OUTPUT_SOURCE_MISMATCH",
                "Reader 输出回执来源与当前连接不匹配");
        }
        try
        {
            _acceptReaderRealtimeOutput(ack);
        }
        catch (ReaderRealtimeOutputException exception)
        {
            throw new DirectProtocolException(
                exception.Code,
                exception.Message,
                exception.Retryable);
        }
        return new
        {
            correlation = ack.Correlation,
            outcome = ack.Outcome,
            matched = true,
        };
    }

    // 两节点复制的命令入口（App→服务端方向）。只走纯上下文连接 ——
    // 命令是数据面，跟随 reader 源，与语音会话无关。
    // ack=accepted 的含义是"已 fsync 落 spool"，不是"已应用"；
    // 幂等/游标/冲突由 Python 账本入账时判。
    private async Task<object> HandleReplicationCommandAsync(
        JsonElement message,
        CancellationToken cancellationToken)
    {
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId",
            "sessionId",
            "envelope");
        RequireAuthenticated();
        if (
            RequireContextDeliveryMode()
                != DirectContextDeliveryMode.SnapshotMcp
            || _phase != DirectProtocolPhase.ContextOnly
        )
        {
            throw new DirectProtocolException(
                "BW_REPLICATION_COMMAND_CONTEXT_ONLY_REQUIRED",
                "复制命令只允许在纯上下文连接中投递");
        }
        string sessionId = RequireSafeId(message, "sessionId");
        RequireContextOnlySession(sessionId);
        ReplicationCommandEnvelope envelope =
            ReplicationCommandProtocol.ValidateEnvelope(
                message.GetProperty("envelope"));
        ReplicationCommandIntakeReceipt receipt =
            await _acceptReplicationCommand(envelope, cancellationToken)
                .ConfigureAwait(false);
        return new
        {
            contract = ReplicationCommandProtocol.EnvelopeContract,
            mutationId = envelope.MutationId,
            outcome = receipt.Outcome,
        };
    }

    // 超帧命令的分片入口：与单帧命令同一 context-only 闸；重组后走
    // **完全相同**的 ValidateEnvelope + spool 流程。中间片 ack partial
    // （不是 accepted —— accepted 的语义是已 fsync 落盘，中间片没有）。
    private async Task<object> HandleReplicationChunkAsync(
        JsonElement message,
        CancellationToken cancellationToken)
    {
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId",
            "sessionId",
            "chunk");
        RequireAuthenticated();
        if (
            RequireContextDeliveryMode()
                != DirectContextDeliveryMode.SnapshotMcp
            || _phase != DirectProtocolPhase.ContextOnly
        )
        {
            throw new DirectProtocolException(
                "BW_REPLICATION_COMMAND_CONTEXT_ONLY_REQUIRED",
                "复制命令分片只允许在纯上下文连接中投递");
        }
        RequireContextOnlySession(RequireSafeId(message, "sessionId"));
        JsonElement chunk = message.GetProperty("chunk");
        DirectJsonValidation.RequireNoDuplicateKeys(chunk);
        HashSet<string> chunkKeys = chunk.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        if (!chunkKeys.SetEquals(["mutationId", "seq", "total", "part"]))
        {
            throw new DirectProtocolException(
                "BW_REPLICATION_COMMAND_INVALID",
                "分片字段不符");
        }
        string mutationId = RequireString(chunk, "mutationId", 64);
        if (!ReplicationCommandProtocol.IsMutationId(mutationId))
        {
            throw new DirectProtocolException(
                "BW_REPLICATION_COMMAND_INVALID",
                "分片 mutationId 形状非法");
        }
        if (
            !chunk.GetProperty("seq").TryGetInt32(out int seq)
            || !chunk.GetProperty("total").TryGetInt32(out int total)
            || chunk.GetProperty("part").GetString() is not string part
        )
        {
            throw new DirectProtocolException(
                "BW_REPLICATION_COMMAND_INVALID",
                "分片参数非法");
        }
        (string? envelopeJson, int received) =
            _replicationChunkAssembler.Accept(mutationId, seq, total, part);
        if (envelopeJson is null)
        {
            return new
            {
                contract = ReplicationCommandProtocol.EnvelopeContract,
                mutationId,
                outcome = "partial",
                received,
            };
        }
        ReplicationCommandEnvelope envelope;
        using (JsonDocument document = JsonDocument.Parse(envelopeJson))
        {
            envelope = ReplicationCommandProtocol.ValidateEnvelope(
                document.RootElement);
        }
        if (envelope.MutationId != mutationId)
        {
            // 片头的 mutationId 决定聚合分组；信封若报另一个 id，
            // 幂等与重投判定会互相错认 —— 拒收整组。
            throw new DirectProtocolException(
                "BW_REPLICATION_COMMAND_INVALID",
                "分片 mutationId 与重组信封不一致");
        }
        ReplicationCommandIntakeReceipt receipt =
            await _acceptReplicationCommand(envelope, cancellationToken)
                .ConfigureAwait(false);
        return new
        {
            contract = ReplicationCommandProtocol.EnvelopeContract,
            mutationId,
            outcome = receipt.Outcome,
        };
    }

    // 对账查询（规格 §6）：回 Windows 端每域摘要视图，App 与本端物化摘要
    // 比对，不一致触发整域重同步。与命令入口同一 context-only 闸。
    private object HandleReplicationDigestQuery(JsonElement message)
    {
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId",
            "sessionId",
            "replicationBookId");
        RequireAuthenticated();
        if (
            RequireContextDeliveryMode()
                != DirectContextDeliveryMode.SnapshotMcp
            || _phase != DirectProtocolPhase.ContextOnly
        )
        {
            throw new DirectProtocolException(
                "BW_REPLICATION_COMMAND_CONTEXT_ONLY_REQUIRED",
                "复制摘要查询只允许在纯上下文连接中进行");
        }
        RequireContextOnlySession(RequireSafeId(message, "sessionId"));
        string replicationBookId = RequireString(
            message,
            "replicationBookId",
            64);
        if (!ReplicationCommandProtocol.IsReplicationBookId(replicationBookId))
        {
            throw new DirectProtocolException(
                "BW_REPLICATION_COMMAND_INVALID",
                "replicationBookId 形状非法");
        }
        return _queryReplicationDigests(replicationBookId);
    }

    private object HandleReplicationNotificationsQuery(JsonElement message)
    {
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId",
            "sessionId");
        RequireAuthenticated();
        if (
            RequireContextDeliveryMode()
                != DirectContextDeliveryMode.SnapshotMcp
            || _phase != DirectProtocolPhase.ContextOnly
        )
        {
            throw new DirectProtocolException(
                "BW_REPLICATION_COMMAND_CONTEXT_ONLY_REQUIRED",
                "通知查询只允许在纯上下文连接中进行");
        }
        RequireContextOnlySession(RequireSafeId(message, "sessionId"));
        return _queryReplicationNotifications();
    }

    private object HandleStatus(JsonElement message)
    {
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId");
        RequireAuthenticated();
        DirectBridgeConfig config = _configStore.Load();
        bool captureActive = _coordinator.CaptureActive;
        bool outputRouteVerified =
            _coordinator.OutputRouteVerified(config);
        string state;
        string? reason;
        bool ready;
        if (captureActive)
        {
            state = "active";
            reason = outputRouteVerified
                ? null
                : DirectOutputRouteProbe.UnverifiedReason;
            ready = outputRouteVerified;
        }
        else if (_coordinator.CleanupPending)
        {
            state = "faulted";
            reason = _coordinator.LastError?.Code
                ?? "BW_COMPUTER_VOICE_DIRECT_MEDIA_CLEANUP_PENDING";
            ready = false;
        }
        else if (!config.LocalOptIn)
        {
            state = "unavailable";
            reason =
                "BW_COMPUTER_VOICE_DIRECT_LOCAL_OPT_IN_REQUIRED";
            ready = false;
        }
        else if (!_voiceEnabled)
        {
            // ReaderPC's non-voice foundation is independently useful.  It
            // must report ready without probing an App launcher, media host,
            // virtual routes, or Codex Voice when the optional voice layer is
            // disabled.
            state = "idle";
            reason = null;
            ready = true;
        }
        else if (!_coordinator.AppLauncherReady)
        {
            state = "unavailable";
            reason =
                "BW_COMPUTER_VOICE_DIRECT_APP_LAUNCHER_NOT_WIRED";
            ready = false;
        }
        else if (!_coordinator.MediaHostReady)
        {
            state = "unavailable";
            reason = "BW_COMPUTER_VOICE_DIRECT_MEDIA_NOT_WIRED";
            ready = false;
        }
        else if (
            !_coordinator.ConfiguredRenderEndpointsReady(
                config,
                out reason)
        )
        {
            state = "unavailable";
            ready = false;
        }
        else
        {
            state = "idle";
            reason = outputRouteVerified
                ? null
                : DirectOutputRouteProbe.UnverifiedReason;
            ready = outputRouteVerified;
        }
        return new
        {
            ready,
            state,
            reason,
            localOptIn = config.LocalOptIn,
            lastError = _coordinator.LastError,
            media = new
            {
                hostReady = _coordinator.MediaHostReady,
                captureActive,
            },
            codexVoice = CodexVoicePayload(
                _codexVoiceControl.ReadState(),
                shortcutSent: false),
        };
    }

    private async Task<object> HandleCodexVoiceSetAsync(
        JsonElement message,
        CancellationToken cancellationToken)
    {
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId",
            "active");
        RequireAuthenticated();
        RequireVoiceAllowed();
        if (_phase is not (
            DirectProtocolPhase.AwaitingStart
            or DirectProtocolPhase.ContextOnly
            or DirectProtocolPhase.Active))
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_PHASE_INVALID",
                "当前连接阶段不能远程控制 Codex 语音");
        }
        DirectCodexVoiceSetResult result =
            await _codexVoiceControl.SetActiveAsync(
                RequireBoolean(message, "active"),
                cancellationToken).ConfigureAwait(false);
        return CodexVoicePayload(
            result.State,
            result.ShortcutSent);
    }

    private async Task<object> HandleCodexVoiceKeepAliveSetAsync(
        JsonElement message,
        CancellationToken cancellationToken)
    {
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId",
            "enabled");
        RequireAuthenticated();
        RequireVoiceAllowed();
        if (_phase is not (
            DirectProtocolPhase.AwaitingStart
            or DirectProtocolPhase.ContextOnly
            or DirectProtocolPhase.Active))
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_PHASE_INVALID",
                "当前连接阶段不能设置 Codex 语音持续运行");
        }
        DirectCodexVoiceSetResult result =
            await _codexVoiceControl.SetKeepActiveAsync(
                RequireBoolean(message, "enabled"),
                cancellationToken).ConfigureAwait(false);
        return CodexVoicePayload(
            result.State,
            result.ShortcutSent);
    }

    private async Task<object> HandleDictionaryLookupAsync(
        JsonElement message,
        CancellationToken cancellationToken)
    {
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId",
            "mode",
            "term",
            "context",
            "reading",
            "english");
        RequireAuthenticated();
        if (_phase is not (
            DirectProtocolPhase.AwaitingStart
            or DirectProtocolPhase.ContextOnly
            or DirectProtocolPhase.Active))
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_PHASE_INVALID",
                "当前连接阶段不能执行本机词义分析");
        }
        if (_dictionaryFallback is null)
        {
            throw new DirectProtocolException(
                "BW_READER_DICTIONARY_CLI_UNAVAILABLE",
                "ReaderPC 本机词义分析尚未接线",
                retryable: true);
        }
        ReaderDictionaryFallbackRequest request = new(
            RequireString(message, "mode", 16),
            RequireString(message, "term", 256),
            RequireBoundedString(message, "context", 1200),
            RequireBoundedString(message, "reading", 256),
            RequireBoundedString(message, "english", 1200));
        try
        {
            ReaderDictionaryFallbackResult result =
                await _dictionaryFallback.LookupAsync(
                    request,
                    cancellationToken).ConfigureAwait(false);
            return new
            {
                term = result.Term,
                mode = result.Mode,
                language = result.Language,
                text = result.Text,
                source = result.Source,
                cached = result.Cached,
            };
        }
        catch (ReaderDictionaryFallbackException exception)
        {
            throw new DirectProtocolException(
                exception.Code,
                exception.Message,
                exception.Retryable,
                exception);
        }
    }

    private async Task<object> HandleLocalAnkiAddAsync(
        JsonElement message,
        CancellationToken cancellationToken)
    {
        bool hasProjection = message.TryGetProperty(
            "projection",
            out JsonElement projectionValue);
        // track 可选(滚动升级:桥先装、App 后出构建)。加进精确字段集就成了**必需**,
        // 旧版 App 不发就被拒 —— 2026-09-08 自检当场抓到过一次。
        bool hasTrack = message.TryGetProperty("track", out _);
        // 两种身份二选一(2026-09-09):
        //   草稿路 = sourceInstanceId + draftId —— AI 刚交付草稿时用，
        //            身份绑在那个页面模块实例上，**页面一重载就失效**。
        //   实体路 = entityId + cards —— 身份来自卡库实体(card_*)，跨会话
        //            稳定，所以"当时没发出去、过几天再补"这件事才成立。
        //   实测四张 2026-08-13 的卡就死在只有草稿路这一点上。
        bool hasEntity = message.TryGetProperty("entityId", out _);
        // 出处三件套要么齐、要么全无(与草稿登记同一条规则)。
        bool hasFile = message.TryGetProperty("file", out _);
        bool hasTarget = message.TryGetProperty("target", out _);
        bool hasSourceText = message.TryGetProperty("sourceText", out _);
        List<string> expected =
        [
            "contract", "type", "requestId", "sessionId",
            "cardIndex", "aid", "card", "nodeIds",
        ];
        if (hasEntity)
        {
            expected.Add("entityId");
            expected.Add("cards");
            if (hasFile || hasTarget || hasSourceText)
            {
                expected.Add("file");
                expected.Add("target");
                expected.Add("sourceText");
            }
        }
        else
        {
            // Rolling upgrade: Direct is installed before WebExt. The previous
            // extension sent canonical Markdown only as `card`; use that same
            // Markdown-shaped value as the fallback projection until WebExt
            // starts sending the separately rendered `projection` field.
            // nodeIds（KJ 知识节点）2026-09-06 起必填：制卡必须带归属。
            // 2026-09-08 起归属二选一，track 承担单词/语法卡的归属。
            expected.Add("sourceInstanceId");
            expected.Add("draftId");
        }
        if (hasProjection)
        {
            expected.Add("projection");
        }
        if (hasTrack)
        {
            expected.Add("track");
        }
        RequireExactKeys(message, expected.ToArray());
        if (Encoding.UTF8.GetByteCount(message.GetRawText()) > 192 * 1024)
        {
            throw new DirectProtocolException(
                "BW_READER_ANKI_REQUEST_TOO_LARGE",
                "Reader 本地 Anki 请求超过 192 KiB 安全上限");
        }
        RequireAuthenticated();
        if (_phase != DirectProtocolPhase.ContextOnly)
        {
            throw new DirectProtocolException(
                "BW_READER_ANKI_CONTEXT_ONLY_REQUIRED",
                "Reader 本地 Anki 写入只允许在纯上下文连接中执行");
        }
        string sessionId = RequireSafeId(message, "sessionId");
        _ = DirectPcmFrameCodec.ParseSessionId(sessionId);
        RequireContextOnlySession(sessionId);
        if (_localAnkiWriter is null)
        {
            throw new DirectProtocolException(
                "BW_READER_ANKI_LOCAL_UNAVAILABLE",
                "ReaderPC 本地 Anki 写入尚未接线",
                retryable: true);
        }
        string sourceInstanceId = hasEntity
            ? ""
            : RequireSafeId(message, "sourceInstanceId");
        string draftId = hasEntity
            ? ""
            : RequireString(message, "draftId", 64);
        string entityId = hasEntity
            ? RequireString(message, "entityId", 80)
            : "";
        string aid = RequireString(message, "aid", 64);
        string track = RequireKjCardTrack(message);
        string[] nodeIds = RequireKjNodeIds(message, track);
        if (!message.TryGetProperty("cardIndex", out JsonElement indexValue)
            || indexValue.ValueKind != JsonValueKind.Number
            || !indexValue.TryGetInt32(out int cardIndex)
            || cardIndex is < 0 or >= 20
            || !message.TryGetProperty("card", out JsonElement cardValue)
            || cardValue.ValueKind != JsonValueKind.Object
            || (hasProjection
                && projectionValue.ValueKind != JsonValueKind.Object))
        {
            throw new DirectProtocolException(
                "BW_READER_ANKI_REQUEST_INVALID",
                "Reader 本地 Anki 卡片请求无效");
        }
        try
        {
            JsonObject card = JsonNode.Parse(cardValue.GetRawText())
                as JsonObject
                ?? throw new JsonException("card is empty");
            JsonObject projection = JsonNode.Parse(
                (hasProjection ? projectionValue : cardValue).GetRawText())
                as JsonObject
                ?? throw new JsonException("projection is empty");
            ReaderLocalAnkiWriteOutcome outcome = hasEntity
                ? await _localAnkiWriter.AddFromEntityAsync(
                    entityId,
                    cardIndex,
                    aid,
                    card,
                    projection,
                    hasFile ? RequireString(message, "file", 4096) : "",
                    hasTarget
                        ? JsonNode.Parse(
                            message.GetProperty("target").GetRawText())
                            as JsonObject
                            ?? throw new JsonException("target is empty")
                        : new JsonObject(),
                    hasSourceText
                        ? RequireString(message, "sourceText", 8000)
                        : "",
                    RequireEntityCards(message),
                    nodeIds,
                    track,
                    cancellationToken).ConfigureAwait(false)
                : await _localAnkiWriter.AddAsync(
                    sourceInstanceId,
                    draftId,
                    cardIndex,
                    aid,
                    card,
                    projection,
                    nodeIds,
                    track,
                    cancellationToken).ConfigureAwait(false);
            return outcome.Result.ToPayload(outcome.Dedup);
        }
        catch (ReaderLocalAnkiException exception)
        {
            throw new DirectProtocolException(
                exception.Code,
                exception.Message,
                exception.Retryable,
                exception);
        }
    }

    private async Task<object> HandleLocalAnkiOperationAsync(
        JsonElement message,
        CancellationToken cancellationToken)
    {
        RequireAuthenticated();
        if (_phase != DirectProtocolPhase.ContextOnly)
        {
            throw new DirectProtocolException(
                "BW_READER_ANKI_CONTEXT_ONLY_REQUIRED",
                "Reader 本地 Anki 操作只允许在纯上下文连接中执行");
        }
        string sessionId = RequireSafeId(message, "sessionId");
        _ = DirectPcmFrameCodec.ParseSessionId(sessionId);
        RequireContextOnlySession(sessionId);
        if (_localAnkiWriter is null)
        {
            throw new DirectProtocolException(
                "BW_READER_ANKI_LOCAL_UNAVAILABLE",
                "ReaderPC 本地 Anki 操作尚未接线",
                retryable: true);
        }
        string operation = RequireString(message, "operation", 32);
        ReaderLocalAnkiOperationRequest request;
        switch (operation)
        {
            case "read-notes":
                RequireExactKeys(
                    message,
                    "contract",
                    "type",
                    "requestId",
                    "sessionId",
                    "operation",
                    "noteIds");
                request = new ReaderLocalAnkiOperationRequest(
                    operation,
                    MutationId: null,
                    NoteIds: RequirePositiveIds(message, "noteIds", 20),
                    CardIds: [],
                    Fields: null,
                    Answers: [],
                    SyncMode: null);
                break;
            case "read-cards":
                RequireExactKeys(
                    message,
                    "contract",
                    "type",
                    "requestId",
                    "sessionId",
                    "operation",
                    "cardIds");
                request = new ReaderLocalAnkiOperationRequest(
                    operation,
                    MutationId: null,
                    NoteIds: [],
                    CardIds: RequirePositiveIds(message, "cardIds", 20),
                    Fields: null,
                    Answers: [],
                    SyncMode: null);
                break;
            case "update-note-fields":
                RequireExactKeys(
                    message,
                    "contract",
                    "type",
                    "requestId",
                    "sessionId",
                    "operation",
                    "mutationId",
                    "noteId",
                    "fields",
                    "syncMode");
                request = new ReaderLocalAnkiOperationRequest(
                    operation,
                    RequireSafeId(message, "mutationId"),
                    [RequirePositiveId(message, "noteId")],
                    CardIds: [],
                    Fields: RequireJsonObject(message, "fields"),
                    Answers: [],
                    SyncMode: RequireString(message, "syncMode", 16));
                break;
            case "delete-notes":
                RequireExactKeys(
                    message,
                    "contract",
                    "type",
                    "requestId",
                    "sessionId",
                    "operation",
                    "mutationId",
                    "noteIds",
                    "syncMode");
                request = new ReaderLocalAnkiOperationRequest(
                    operation,
                    RequireSafeId(message, "mutationId"),
                    RequirePositiveIds(message, "noteIds", 20),
                    CardIds: [],
                    Fields: null,
                    Answers: [],
                    SyncMode: RequireString(message, "syncMode", 16));
                break;
            case "answer-cards":
                RequireExactKeys(
                    message,
                    "contract",
                    "type",
                    "requestId",
                    "sessionId",
                    "operation",
                    "mutationId",
                    "answers",
                    "syncMode");
                request = new ReaderLocalAnkiOperationRequest(
                    operation,
                    RequireSafeId(message, "mutationId"),
                    NoteIds: [],
                    CardIds: [],
                    Fields: null,
                    Answers: RequireAnkiAnswers(message),
                    SyncMode: RequireString(message, "syncMode", 16));
                break;
            case "sync":
                RequireExactKeys(
                    message,
                    "contract",
                    "type",
                    "requestId",
                    "sessionId",
                    "operation",
                    "mutationId");
                request = new ReaderLocalAnkiOperationRequest(
                    operation,
                    RequireSafeId(message, "mutationId"),
                    NoteIds: [],
                    CardIds: [],
                    Fields: null,
                    Answers: [],
                    SyncMode: null);
                break;
            default:
                throw new DirectProtocolException(
                    "BW_READER_ANKI_REQUEST_INVALID",
                    "Reader 本地 Anki 操作类型无效");
        }
        try
        {
            return await _localAnkiWriter.OperateAsync(
                request,
                cancellationToken).ConfigureAwait(false);
        }
        catch (ReaderLocalAnkiException exception)
        {
            throw new DirectProtocolException(
                exception.Code,
                exception.Message,
                exception.Retryable,
                exception);
        }
    }

    private static long RequirePositiveId(
        JsonElement message,
        string name)
    {
        if (!message.TryGetProperty(name, out JsonElement value)
            || value.ValueKind != JsonValueKind.Number
            || !value.TryGetInt64(out long id)
            || id <= 0)
        {
            throw new DirectProtocolException(
                "BW_READER_ANKI_REQUEST_INVALID",
                $"Reader 本地 Anki {name} 无效");
        }
        return id;
    }

    /// 学习轨道（单词/语法卡的归属，2026-09-08）。缺失=空串，表示这张卡走概念节点那条路。
    private static string RequireKjCardTrack(JsonElement message)
    {
        if (!message.TryGetProperty("track", out JsonElement value))
        {
            return "";
        }
        string track = value.ValueKind == JsonValueKind.String
            ? value.GetString() ?? ""
            : "";
        if (track.Length == 0)
        {
            return "";
        }
        if (!ReaderRealtimeOutputProtocol.KjCardTracks.IsValid(track))
        {
            throw new DirectProtocolException(
                "BW_READER_ANKI_NODE_REQUIRED",
                "Reader 制卡 track 不在白名单内");
        }
        return track;
    }

    /// 制卡必须有归属（2026-09-06 用户拍板）。缺失/无效直接拒绝，绝不静默放行。
    /// 2026-09-08：track 已承担归属时 nodeIds 必须为空；否则仍要 1~8 个合法节点。
    private static string[] RequireKjNodeIds(JsonElement message, string track)
    {
        if (track.Length > 0)
        {
            if (message.TryGetProperty("nodeIds", out JsonElement bound)
                && (bound.ValueKind != JsonValueKind.Array
                    || bound.GetArrayLength() != 0))
            {
                throw new DirectProtocolException(
                    "BW_READER_ANKI_NODE_REQUIRED",
                    "Reader 制卡按 track 归属时不应再带 nodeIds");
            }
            return Array.Empty<string>();
        }
        if (
            !message.TryGetProperty("nodeIds", out JsonElement value)
            || value.ValueKind != JsonValueKind.Array
            || value.GetArrayLength() is < 1
                or > ReaderRealtimeOutputProtocol.KjNodeIdRules.Maximum)
        {
            throw new DirectProtocolException(
                "BW_READER_ANKI_NODE_REQUIRED",
                "Reader 制卡必须给出归属：单词/语法卡传 track，概念卡绑 1~8 个知识节点（nodeIds）");
        }
        HashSet<string> seen = new(StringComparer.Ordinal);
        List<string> ids = new();
        foreach (JsonElement item in value.EnumerateArray())
        {
            string? id = item.ValueKind == JsonValueKind.String
                ? item.GetString()
                : null;
            if (
                !ReaderRealtimeOutputProtocol.KjNodeIdRules.IsValid(id)
                || !seen.Add(id!))
            {
                throw new DirectProtocolException(
                    "BW_READER_ANKI_NODE_INVALID",
                    "Reader 知识节点编号无效或重复");
            }
            ids.Add(id!);
        }
        return ids.ToArray();
    }

    private static long[] RequirePositiveIds(
        JsonElement message,
        string name,
        int maximum)
    {
        if (!message.TryGetProperty(name, out JsonElement value)
            || value.ValueKind != JsonValueKind.Array
            || value.GetArrayLength() is < 1
            || value.GetArrayLength() > maximum)
        {
            throw new DirectProtocolException(
                "BW_READER_ANKI_REQUEST_INVALID",
                $"Reader 本地 Anki {name} 无效");
        }
        List<long> result = [];
        foreach (JsonElement item in value.EnumerateArray())
        {
            if (item.ValueKind != JsonValueKind.Number
                || !item.TryGetInt64(out long id)
                || id <= 0
                || result.Contains(id))
            {
                throw new DirectProtocolException(
                    "BW_READER_ANKI_REQUEST_INVALID",
                    $"Reader 本地 Anki {name} 无效");
            }
            result.Add(id);
        }
        return result.ToArray();
    }

    private static JsonObject RequireJsonObject(
        JsonElement message,
        string name)
    {
        if (!message.TryGetProperty(name, out JsonElement value)
            || value.ValueKind != JsonValueKind.Object)
        {
            throw new DirectProtocolException(
                "BW_READER_ANKI_REQUEST_INVALID",
                $"Reader 本地 Anki {name} 无效");
        }
        return JsonNode.Parse(value.GetRawText()) as JsonObject
            ?? throw new DirectProtocolException(
                "BW_READER_ANKI_REQUEST_INVALID",
                $"Reader 本地 Anki {name} 无效");
    }

    private static ReaderLocalAnkiAnswer[] RequireAnkiAnswers(
        JsonElement message)
    {
        if (!message.TryGetProperty("answers", out JsonElement value)
            || value.ValueKind != JsonValueKind.Array
            || value.GetArrayLength() is < 1 or > 20)
        {
            throw new DirectProtocolException(
                "BW_READER_ANKI_REQUEST_INVALID",
                "Reader 本地 Anki answers 无效");
        }
        List<ReaderLocalAnkiAnswer> result = [];
        foreach (JsonElement answer in value.EnumerateArray())
        {
            RequireExactKeys(answer, "cardId", "ease");
            long cardId = RequirePositiveId(answer, "cardId");
            if (result.Any(item => item.CardId == cardId)
                || !answer.TryGetProperty(
                    "ease",
                    out JsonElement easeValue)
                || easeValue.ValueKind != JsonValueKind.Number
                || !easeValue.TryGetInt32(out int ease)
                || ease is < 1 or > 4)
            {
                throw new DirectProtocolException(
                    "BW_READER_ANKI_REQUEST_INVALID",
                    "Reader 本地 Anki ease 无效");
            }
            result.Add(new ReaderLocalAnkiAnswer(cardId, ease));
        }
        return result.ToArray();
    }

    private object CodexVoicePayload(
        DirectCodexVoiceState state,
        bool shortcutSent) =>
        new
        {
            status = state.Status,
            active = state.Active,
            source = state.Source,
            shortcutSent,
            keepActive = _codexVoiceControl.KeepActive,
            // 语音入口梯子（2026-09-09）。ReaderPC 每 30 秒算一次写在这里，
            // 桥只是**捎带**给界面 —— 界面据此显示"卡在第几级"，而不是
            // 一直转圈。读不到就是 null：不知道要如实说，不能编一个"就绪"。
            ladder = ReadVoiceLadder(),
            // 「通知送得到 Codex 吗」（2026-09-10 用户点出来的盲区：「主动推送
            // 没有办法确认是否推送成功，一开始的绑定对话也无法判断是否成功」）。
            // 这三项让界面说得出"卡住是因为对面收不到"，而不是一直干闪。
            // ⚠ bound 只说明**登记过**，不说明还通 —— 通不通看
            // lastSuccessAtUtcMs 与 consecutiveFailures。
            push = new
            {
                bound = ReaderCodexEndpoint.Current() is not null,
                lastSuccessAtUtcMs = ReaderCodexPush.LastSuccessAtUtcMs,
                consecutiveFailures = ReaderCodexPush.ConsecutiveFailures,
                lastNote = ReaderCodexPush.LastNote,
            },
        };

    /// ReaderPC 每 30 秒发布一次梯子；超过 3 个周期就当没有。
    /// 宁可界面什么都不显示，也不能显示一份**过期到会撒谎**的状态。
    private const long VoiceLadderMaxAgeMs = 90_000;

    /// <summary>
    /// 读 ReaderPC 发布的语音梯子状态。任何读失败**或过期**都返回 null（不知道）。
    /// </summary>
    private static object? ReadVoiceLadder()
    {
        string? runtime = ReaderAttentionBoard.RuntimeDirectory;
        if (string.IsNullOrEmpty(runtime))
        {
            return null;
        }
        try
        {
            string path = Path.Combine(runtime, "voice-ladder-status.json");
            FileInfo info = new(path);
            if (!info.Exists || info.Length is <= 0 or > 64 * 1024)
            {
                return null;
            }
            using JsonDocument document = JsonDocument.Parse(
                File.ReadAllText(path));
            JsonElement root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
            {
                return null;
            }
            // ⚠ **过期的梯子会撒谎**（2026-09-09 实测抓到）：ReaderPC 在通话中
            // 写下「语音已连接」，随后通话结束、服务停止、ReaderPC 退出，而
            // 那份文件原样留着 —— 界面照着显示"已连接"，而实际上语音是关的。
            // ReaderPC 每 30 秒发布一次，所以超过 3 个周期就当没有：
            // 不知道要如实说 null，不能拿旧快照冒充现状。
            if (!root.TryGetProperty("atUtcMs", out JsonElement at)
                || !at.TryGetInt64(out long publishedAt)
                || DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() - publishedAt
                    > VoiceLadderMaxAgeMs)
            {
                return null;
            }
            // 只捎带界面要用的那几项：整份原样透传会让这条状态消息随
            // ReaderPC 那边改字段而悄悄变形。
            return new
            {
                reached = root.TryGetProperty("reached", out JsonElement r)
                    && r.TryGetInt32(out int reached) ? reached : 0,
                total = root.TryGetProperty("total", out JsonElement to)
                    && to.TryGetInt32(out int total) ? total : 0,
                label = root.TryGetProperty("label", out JsonElement la)
                    && la.ValueKind == JsonValueKind.String
                    ? la.GetString() ?? "" : "",
                blockedAt =
                    root.TryGetProperty("blockedAt", out JsonElement bl)
                    && bl.ValueKind == JsonValueKind.String
                    ? bl.GetString() : null,
                reachable =
                    root.TryGetProperty("reachable", out JsonElement re)
                    && re.ValueKind is JsonValueKind.True
                        or JsonValueKind.False
                    ? re.GetBoolean() : true,
                // 对面**放弃**了(跑过 voice_start_failed.py)。必须捎给界面:
                // 不报错的放弃跟"还在试"在界面上长得一模一样 —— 按钮一直闪、
                // 人一直等,而其实早就不会成了。那个脚本存在的全部理由就是留下
                // 这个痕迹,而痕迹到不了显示它的那一层,等于没留(2026-09-10)。
                startGaveUp =
                    root.TryGetProperty("startGaveUp", out JsonElement gu)
                    && gu.ValueKind == JsonValueKind.Object,
            };
        }
        catch (Exception)
        {
            return null;
        }
    }

    private async Task<DirectStartActionResult> HandleStartAsync(
        JsonElement message,
        Func<string, string, Task> reportStatusAsync,
        Func<string, DirectPcmFrame, CancellationToken, Task>
            sendPcmFrameAsync,
        CancellationToken cancellationToken)
    {
        bool hasAppKind = message.TryGetProperty(
            "appKind",
            out _);
        bool hasTakeover = message.TryGetProperty(
            "takeover",
            out _);
        List<string> expectedKeys =
        [
            "contract",
            "type",
            "requestId",
            "sessionId",
        ];
        if (hasAppKind)
        {
            expectedKeys.Add("appKind");
        }
        if (hasTakeover)
        {
            expectedKeys.Add("takeover");
        }
        RequireExactKeys(message, [.. expectedKeys]);
        RequireAuthenticated();
        RequireVoiceAllowed();
        string sessionId = RequireSafeId(message, "sessionId");
        _ = DirectPcmFrameCodec.ParseSessionId(sessionId);
        string appKind = hasAppKind
            ? RequireString(message, "appKind", 32)
            : DirectAppTargets.CodexDesktop;
        bool takeover = hasTakeover
            && RequireBoolean(message, "takeover");
        _ = DirectAppTargets.Require(appKind);
        if (_phase is not (
            DirectProtocolPhase.AwaitingStart
            or DirectProtocolPhase.Active))
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_PHASE_INVALID",
                "当前连接阶段不接受 START");
        }
        if (
            _phase == DirectProtocolPhase.Active
            && (
                !string.Equals(
                    _activeVoiceSessionId,
                    sessionId,
                    StringComparison.Ordinal)
                || !string.Equals(
                    _activeVoiceAppKind,
                    appKind,
                    StringComparison.Ordinal)
            )
        )
        {
            // Replacing a session on the same transport would also require
            // resetting both PCM sequence guards.  Keep takeover scoped to a
            // second AwaitingStart connection; an active transport may only
            // repeat its exact START idempotently.
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_SESSION_MISMATCH",
                "活动连接上的 START 与当前会话不匹配");
        }
        DirectPcmStartGate pcmGate = new(
            (frame, token) => sendPcmFrameAsync(
                sessionId,
                frame,
                token));
        DirectProtocolPhase previousPhase = _phase;
        _phase = DirectProtocolPhase.Starting;
        try
        {
            DirectMediaStartResult started =
                await _coordinator.StartAsync(
                    _connectionId,
                    sessionId,
                    appKind,
                    RequireContextDeliveryMode(),
                    takeover,
                    reportStatusAsync,
                    pcmGate.SendAsync,
                    cancellationToken).ConfigureAwait(false);
            object payload = new
            {
                sessionId,
                state = "active",
                media = new
                {
                    hostReady = started.HostReady,
                    captureActive = started.CaptureActive,
                },
            };
            _phase = DirectProtocolPhase.Active;
            _activeVoiceSessionId = sessionId;
            _activeVoiceAppKind = appKind;
            RequestVoiceEntryIfNobodyElseWill(appKind, sessionId);
            return new DirectStartActionResult(
                payload,
                pcmGate.ReleaseAsync);
        }
        catch
        {
            _phase = previousPhase;
            pcmGate.Abort();
            throw;
        }
    }

    /// 同一条入口请求的最小间隔。活动连接上的 START 允许幂等重复，
    /// 不设这个门就会对同一次"开语音"反复催对面。
    ///
    /// ⚠ **按会话计，不是按时钟计**（2026-09-10 用户实测：「我再次点击后…
    /// 并没有发送内容到 codex 让他启动语音」）。原来是一个全局时间戳，于是用户
    /// 第一次按失败、隔十几秒再按时被这个门当成"重复的幂等 START"挡掉 ——
    /// 而那是一次**新的用户意图**，恰恰最该发。幂等重复的特征是 sessionId 相同；
    /// 换了 sessionId 就是新按了一次。
    private static readonly TimeSpan VoiceEntryRequestCooldown =
        TimeSpan.FromSeconds(45);
    private static readonly object VoiceEntryGate = new();
    private static long _lastVoiceEntryRequestTicksUtc;
    private static string _lastVoiceEntrySessionId = string.Empty;

    /// <summary>
    /// 同一时刻只允许**一个**入口任务在跑（2026-09-10 实测事故）。
    /// </summary>
    /// <remarks>
    /// 上面那个冷却是**按 sessionId 算**的（2026-09-10 早些时候改的，因为
    /// 全局时间戳会把用户真正的第二次点击当成幂等重复挡掉）。那个改动是对的，
    /// 但它顺手把唯一的全局刹车也拆了：**换个 sessionId 就绕过一切**。
    ///
    /// 实录：19:47–20:23 的 35 分钟里，对面收到 **432 条**「指定操作」，
    /// 来自 **191 个不同的 requestId** —— 平均每 11 秒诞生一个新任务。
    /// 每个 START 都带一个新 sessionId，而 App 那阵在反复重连。
    ///
    /// 这个闸不看时间也不看会话，只问一句"上一个还在跑吗"：在跑就不再开第二个。
    /// 正在跑的那个每一轮都读台账，语音一起来它自己收手 —— 多开一个不会更快，
    /// 只会让对面多跑一轮。
    /// </remarks>
    private static int _voiceEntryInFlight;

    /// 请求发出后还要盯多久。**Codex 往前几秒才刚被我们拉起来**
    /// （同一次 START 里 EnsureRunningAsync 干的），它的推送绑定要等自己的会话
    /// 钩子跑完才登记 —— 在那之前管道对面没人。只发一次正好落在最差的时刻：
    /// 请求失败，而失败原因只写进 lastNote，用户看到的是"什么都没发生"。
    private static readonly TimeSpan VoiceEntryRetryWindow =
        TimeSpan.FromSeconds(90);
    private static readonly TimeSpan VoiceEntryRetryInterval =
        TimeSpan.FromSeconds(10);

    /// <summary>
    /// 送达之后再等多久才认为"这一次没生效"（2026-09-10 用户：「在 app 中开启
    /// 语音后服务器如果没有连接语音则需要**积极的**去开启语音」）。
    /// </summary>
    /// <remarks>
    /// 原来是 `if (sent) return;` —— **送出去就收工**。可是"接口收下了"从来
    /// 不等于"任务处理了"，这条纪律本文件顶部就写着，偏偏在这里没守住：
    /// 推送成功但对面没跑成脚本时，整条链就此静默，用户看到的是按钮闪一下
    /// 然后什么都没有。
    ///
    /// 30 秒的来历：实测一次成功的链路是 19:38:04 送出 → 19:38:09 台账翻转，
    /// 约 5 秒；对面跑一轮约 11 秒。30 秒 ≈ 3 倍余量，超过它基本可以断定
    /// 这一次没落地。
    /// </remarks>
    private static readonly TimeSpan VoiceEntrySentGrace =
        TimeSpan.FromSeconds(30);

    /// <summary>同一次开语音里最多送几遍。</summary>
    /// <remarks>
    /// ⚠ 不是越多越好：**每一次送达都让对面跑一整轮**（实测 11 秒 + 额度）。
    /// 而重发是安全的 —— 指令正文里写着「同一编号再次出现表示上一次没有生效」，
    /// 脚本自己也带守卫（已在通话中不动作、冷却期内不动作）。
    /// 取 2 = 一次 + 一次补发；再不行就交给桥端兜底，那条不烧对面的额度。
    /// </remarks>
    private const int VoiceEntrySendBudget = 2;

    /// <summary>
    /// 音频通道刚通，但语音会话没起来 —— 且**没有别人会去起它**时，请对面开一次。
    /// </summary>
    /// <remarks>
    /// 用户 2026-09-10 实测：「直接就变绿显示联通但是实际上 codex 语音没起来」。
    /// 根因不在显示层：一次性启动方式下保活收敛是**故意**关着的，而那条
    /// 「语音入口」推送**一个发送方都没有** —— 脚本、失败上报、梯子、端点、
    /// 能力说明全建好了，就是没人触发。链上任何一环缺了都表现成"什么都没发生"。
    ///
    /// 判据三条，缺一条都会做错事：
    ///   · 台账已 active → 已经在通话，催它只会多按一次 F24（那是**挂断**）；
    ///   · keepActive 为真 → 保活收敛正在负责起它，再推一遍就是两个人同时按；
    ///   · 只对 Codex 桌面端 → 别的 appKind 没有这条入口链。
    /// 台账读不到时**照发**：脚本那侧的守卫才是权威（它会先把 Codex 拉起来，
    /// 仍读不到就失败关闭），在这里替它判断等于把"不知道"折成"不必开"。
    ///
    /// 整段是 fire-and-forget：推送慢或管道不通绝不能拖住/弄失败一次已经成功的
    /// START。发没发成写在 ReaderCodexPush 的 lastNote 里。
    /// </remarks>
    private void RequestVoiceEntryIfNobodyElseWill(
        string appKind,
        string sessionId)
    {
        // ## 每一次 START 都留一条（2026-09-10）
        //
        // 起因：对面被「指定操作」刷屏，205 个不同的 requestId 横跨三个多小时，
        // 其中 120 个间隔小于 200 毫秒（成对出现 = 两条连接同时 START）。
        // 想知道"是谁在反复 START"时才发现 —— **桥从来没记录过 START**：
        // 安全日志只记 reader-connect（那一段里只有两次），账本只记推送。
        // 于是能看到结果、看不到起因。
        //
        // 这是同一天里第三次撞上"查不出来是因为压根没记"（媒体为什么停、
        // 推送发没发、现在这条）。所以先记录，别再猜。
        //
        // ⚠ 记的是**每一次调用**，包括被闸挡掉的 —— 被挡掉的次数正是
        // "上游有多吵"的度量，而那恰恰是要回答的问题。
        ReaderCodexPush.NoteVoiceEntryOutcome(
            "start:" + _connectionId,
            true,
            "收到 START（session " + (sessionId.Length > 12
                ? sessionId[..12] : sessionId) + "），准备判断要不要请求入口");
        if (!string.Equals(
                appKind,
                DirectAppTargets.CodexDesktop,
                StringComparison.Ordinal))
        {
            return;
        }
        if (_codexVoiceControl.KeepActive)
        {
            return;
        }
        try
        {
            if (_codexVoiceControl.ReadState().Active == true)
            {
                return;
            }
        }
        catch (Exception)
        {
            // 读不到就当"不知道" —— 继续发。见 remarks。
        }
        long now = DateTime.UtcNow.Ticks;
        lock (VoiceEntryGate)
        {
            bool sameSession = string.Equals(
                _lastVoiceEntrySessionId,
                sessionId,
                StringComparison.Ordinal);
            long previous = _lastVoiceEntryRequestTicksUtc;
            // 只挡"同一次开语音里重复的幂等 START"。换了 sessionId 说明用户
            // 又按了一次 —— 那是新意图，必须放过去。
            if (
                sameSession
                && previous != 0
                && now - previous < VoiceEntryRequestCooldown.Ticks
            )
            {
                return;
            }
            _lastVoiceEntryRequestTicksUtc = now;
            _lastVoiceEntrySessionId = sessionId;
        }
        string requestId = "voice-entry-"
            + DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()
                .ToString(System.Globalization.CultureInfo.InvariantCulture);
        IDirectCodexVoiceControl control = _codexVoiceControl;
        // 已经有一个在跑就不再开 —— 见 _voiceEntryInFlight 的说明。
        if (Interlocked.Exchange(ref _voiceEntryInFlight, 1) == 1)
        {
            ReaderCodexPush.NoteVoiceEntryOutcome(
                requestId, true,
                "已有一个入口任务在跑，这一次不另开（防重连风暴）");
            return;
        }
        _ = Task.Run(async () =>
        {
          try
          {
            using CancellationTokenSource lifetime = new(
                VoiceEntryRetryWindow + VoiceEntryRetryInterval);
            DateTime deadline = DateTime.UtcNow + VoiceEntryRetryWindow;
            // 自愈周期 30 秒，给它一轮多一点。
            DateTime healWindow = DateTime.UtcNow + TimeSpan.FromSeconds(40);
            // 送出去几次、上一次是什么时候 —— 用来决定这一轮该不该再送。
            int sentCount = 0;
            DateTime lastSentAt = DateTime.MinValue;
            // 连着几次整条链都没起来，先重启一次 Codex 再谈（见
            // RestartCodexIfWedgedAsync；它自己判在不在通话、自己清零）。
            await RestartCodexIfWedgedAsync(requestId, lifetime.Token)
                .ConfigureAwait(false);
            while (true)
            {
                try
                {
                    // 中途语音自己起来了(或别人起了)就收手 —— 再催一遍会让对面
                    // 多按一次 F24,而那是**挂断**。
                    if (control.ReadState().Active == true)
                    {
                        // 起来了 = 这一串失败到此为止。
                        WriteVoiceEntryStreak(0);
                        return;
                    }
                }
                catch (Exception)
                {
                    // 读不到就当不知道,继续按原计划催。
                }
                // ⚠ **没有绑定时先自己去建通道**（2026-09-10 用户点出的顺序）：
                //
                //   「顺序必须是冷启动后尝试刷新列表，等刷新成功时就证明 codex
                //     加载成功，然后选择记录中的那个对话然后建立通道」
                //
                // 原来这里只会干等 —— 等 ReaderPC 那个 30 秒的自愈 tick，或者
                // 等 Codex 自己的会话钩子登记。可**冷启动时两者都还没发生**：
                // 钩子要等会话建起来，而会话要等语音起来，正是那个闭环。
                // 于是按钮按下、Codex 被拉起来了，通道却始终是空的。
                //
                // ensure_channel 那四步（枚举管道 → tools/list 自证 →
                // list_threads → 按记录选 → 登记）任何一步不成就整体失败，
                // 所以"重试到成功"天然等价于"等 Codex 真的加载完"。
                // ⚠ 它比窗口句柄可靠：句柄出现得比 app-tools 管道早得多，
                // 而我们要的是后者。
                if (ReaderCodexEndpoint.Current() is null)
                {
                    await TryEnsureChannelAsync(requestId, lifetime.Token)
                        .ConfigureAwait(false);
                }
                // 这一轮该不该送：
                //   · 一次都没送成 → 一直试（送不出去不烧对面的额度）
                //   · 送成过 → 等满宽限期，且还有补发预算才再送一次
                bool maySend =
                    sentCount == 0
                    || (sentCount < VoiceEntrySendBudget
                        && DateTime.UtcNow - lastSentAt >= VoiceEntrySentGrace);
                if (maySend)
                {
                    try
                    {
                        if (await ReaderCodexPush.RequestVoiceEntryAsync(
                                requestId,
                                lifetime.Token).ConfigureAwait(false))
                        {
                            sentCount++;
                            lastSentAt = DateTime.UtcNow;
                        }
                    }
                    catch (Exception)
                    {
                        // RequestVoiceEntryAsync 自己已经记过原因。
                    }
                }
                // ⚠ 这里**没有** `if (sent) return;`（2026-09-10 删掉的）。
                // 送达只说明消息进了管道；判"起来了没有"的始终是循环顶部那次
                // 台账读取。收工的唯一理由是语音真的起来了。
                // 绑定已判死就别再等了 —— 重试救不回一条不存在的管道，
                // 而每一轮都要干等满一个连接超时。用户看到的是按钮白闪 90 秒，
                // 然后才轮到兜底（2026-09-10 实测：八次×14 秒）。
                //
                // ⚠ 但**给自愈留一点时间**：ReaderPC 每 30 秒会去重新发现管道
                // 并登记。实测撞到过一次 48 秒之差 —— 按钮按下时通道刚好还没
                // 重连上，于是走了兜底 F24，而 48 秒后通道就自己好了。
                // 所以头一轮不立刻放弃，等一个自愈周期再看。
                if (ReaderCodexEndpoint.Current() is null
                    && DateTime.UtcNow >= healWindow)
                {
                    WriteVoiceEntryStreak(ReadVoiceEntryStreak() + 1);
                    StartVoiceFromBridge(control, requestId);
                    return;
                }
                if (DateTime.UtcNow >= deadline)
                {
                    // 推送这条路走不通时，**桥自己把语音开起来**。
                    //
                    // 2026-09-10 与 Codex 核对后定的分工：它明确表示"通过模拟
                    // 快捷键控制桌面应用这条操作路线目前不能执行"，并建议把桥端
                    // 启动与它能做的（状态回报、处理通知、授权挂断）分开设计。
                    // 那就分开 —— 起通话走桥自己那条已验证的链（拉起 Codex →
                    // 等就绪 → 沉降 → 按一次 → 用台账确认），实测 3.7~5.8 秒。
                    //
                    // ⚠ 守卫全在 SetActiveAsync 里：已在通话不按（再按是挂断）、
                    // 台账读不到失败关闭、冷却期内不按。
                    //
                    // ⚠ 记一次失败：连够 VoiceEntryRestartAfterFailures 次，
                    // 下一次入口会先重启一次 Codex。
                    WriteVoiceEntryStreak(ReadVoiceEntryStreak() + 1);
                    StartVoiceFromBridge(control, requestId);
                    return;
                }
                try
                {
                    await Task.Delay(
                        VoiceEntryRetryInterval,
                        lifetime.Token).ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                    return;
                }
            }
          }
          finally
          {
              // ⚠ 无论怎么退出都要放闸 —— 漏放一次就是
              // 「从此再也起不了语音」，而那种失效没有任何提示。
              Interlocked.Exchange(ref _voiceEntryInFlight, 0);
          }
        });
    }

    /// <summary>连着几次整条入口都没把语音开起来，就重启一次 Codex。</summary>
    /// <remarks>
    /// 用户 2026-09-10：「多次重试失败时可能需要一次 codex 重启」。
    ///
    /// ⚠ **这条路 2026-08-17 被用户实测否掉过一次**，当时它接在
    /// `recoverStartFailureAsync` 上 —— 那个钩子在 SetActiveAsync **每一次**
    /// 起语音失败时都会触发，于是"恢复=重启 App"在 20 分钟里反复杀掉用户
    /// 正在用的会话，而且重启窗口里新旧两代并存又制造 APP_AMBIGUOUS。
    /// 所以那个钩子至今仍然接的是 null，别把它接回去。
    ///
    /// 现在的形状不同，差别就是当初出事的那一点：
    ///   · 判据是**整条入口链**失败（推送 ×2 + 桥端兜底都没起来），不是单次按键；
    ///   · 要连着 <see cref="VoiceEntryRestartAfterFailures"/> 次；
    ///   · 重启前先读台账，**在通话中一律不重启**（那才是"杀掉他正在用的会话"）；
    ///   · 重启后立刻清零，所以最多重启一次，不会变成重启风暴。
    /// </remarks>
    private const int VoiceEntryRestartAfterFailures = 3;

    private const string VoiceEntryStreakFileName =
        "voice-entry-failure-streak.json";

    private static string? VoiceEntryStreakPath()
    {
        string? runtime = ReaderAttentionBoard.RuntimeDirectory;
        return string.IsNullOrEmpty(runtime)
            ? null
            : Path.Combine(runtime, VoiceEntryStreakFileName);
    }

    /// 连败次数。读不到当 0 —— 一个坏掉的计数器不该触发重启。
    private static int ReadVoiceEntryStreak()
    {
        try
        {
            string? path = VoiceEntryStreakPath();
            if (path is null || !File.Exists(path)) return 0;
            if (JsonNode.Parse(File.ReadAllText(path)) is not JsonObject value)
            {
                return 0;
            }
            return value["failures"] is JsonValue count
                && count.TryGetValue(out int failures)
                && failures is >= 0 and <= 1000
                ? failures
                : 0;
        }
        catch (Exception)
        {
            return 0;
        }
    }

    private static void WriteVoiceEntryStreak(int failures)
    {
        try
        {
            string? path = VoiceEntryStreakPath();
            if (path is null) return;
            string temporary = path + ".tmp-" + Environment.ProcessId;
            File.WriteAllText(
                temporary,
                new JsonObject
                {
                    ["contract"] = "reader-voice-entry-streak/1",
                    ["failures"] = failures,
                    ["atUtcMs"] =
                        DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
                }.ToJsonString());
            File.Move(temporary, path, overwrite: true);
        }
        catch (Exception)
        {
            // 记不下来只是少一次重启，不该反过来弄坏起语音。
        }
    }

    /// <summary>连败够了就重启一次 Codex。返回是否真的重启了。</summary>
    private static async Task<bool> RestartCodexIfWedgedAsync(
        string requestId,
        CancellationToken cancellationToken)
    {
        int streak = ReadVoiceEntryStreak();
        if (streak < VoiceEntryRestartAfterFailures) return false;
        // ⚠ 在通话中绝不重启 —— 那正是 2026-08-17 出事的形态。
        try
        {
            if (new WindowsRegistryCodexVoiceActivitySource(
                    DirectAppTargets.CodexDesktop).Read().Active)
            {
                ReaderCodexPush.NoteVoiceEntryOutcome(
                    requestId, false,
                    "连败 " + streak + " 次，但台账显示在通话中 —— 不重启 Codex");
                return false;
            }
        }
        catch (Exception)
        {
            // 读不到就是"不知道在不在通话"，而不知道时不该动用户的 App。
            return false;
        }
        try
        {
            // 与 PrepareInitialStartAsync 同一套取法：profile 给名字，
            // WaitForUniqueReadyAsync 给"现在这一代是谁"。
            // ⚠ 不用探针那个 CodexAppTarget —— 启动器要的是 DirectAppTarget，
            // 两个是不同的记录，混用编译期就会拦下来（刚才就拦了一次）。
            DirectAppTargetProfile profile = DirectAppTargets.Require(
                DirectAppTargets.CodexDesktop);
            WindowsDirectAppLauncher launcher = new();
            DirectAppTarget current = await launcher
                .WaitForUniqueReadyAsync(
                    profile.AppKind,
                    profile.AppUserModelId,
                    TimeSpan.FromSeconds(10),
                    cancellationToken).ConfigureAwait(false);
            await launcher.RestartAsync(
                profile.AppKind,
                profile.AppUserModelId,
                current,
                AppRestartTimeout,
                cancellationToken).ConfigureAwait(false);
            // 立刻清零：这一次已经用掉了，不许连着重启。
            WriteVoiceEntryStreak(0);
            ReaderCodexPush.NoteVoiceEntryOutcome(
                requestId, true,
                "连败 " + streak + " 次，已重启 Codex 一次再试");
            return true;
        }
        catch (Exception exception)
        {
            WriteVoiceEntryStreak(0);
            ReaderCodexPush.NoteVoiceEntryOutcome(
                requestId, false,
                "连败 " + streak + " 次，重启 Codex 失败："
                + exception.GetType().Name);
            return false;
        }
    }

    private static readonly TimeSpan AppRestartTimeout =
        TimeSpan.FromSeconds(45);

    /// <summary>跑一次 codex_channel --ensure，把通道建起来。</summary>
    /// <remarks>
    /// ⚠ 走脚本而不是在 C# 里重写一遍：枚举命名管道、逐条 tools/list 自证、
    /// list_threads、按名字/活跃度挑对话、登记 —— 这一整套已经在
    /// `codex_channel.py` 里，而且那份还带着六个实测踩坑的处理
    /// （管道名每次重启都变、同时存在的管道只有一条是活的、信封要真实 threadId、
    /// updatedAt 混着秒和毫秒…）。抄第二份的下场是两边迟早不一致。
    ///
    /// ⚠ 失败是**常态**而不是异常：Codex 还没加载完时它必然失败，那正是我们
    /// 据以判断"还没就绪"的信号。所以失败只记账不抛。
    /// </remarks>
    private static async Task<bool> TryEnsureChannelAsync(
        string requestId,
        CancellationToken cancellationToken)
    {
        string script = Path.Combine(
            Environment.GetFolderPath(
                Environment.SpecialFolder.LocalApplicationData),
            "BWReader",
            "codex_channel.py");
        if (!File.Exists(script)) return false;
        try
        {
            ProcessStartInfo info = new()
            {
                FileName = PythonExecutable(),
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
            };
            info.ArgumentList.Add(script);
            info.ArgumentList.Add("--ensure");
            using Process? child = Process.Start(info);
            if (child is null) return false;
            using CancellationTokenSource budget =
                CancellationTokenSource.CreateLinkedTokenSource(
                    cancellationToken);
            budget.CancelAfter(EnsureChannelTimeout);
            string output = await child.StandardOutput
                .ReadToEndAsync(budget.Token).ConfigureAwait(false);
            await child.WaitForExitAsync(budget.Token).ConfigureAwait(false);
            bool ok = child.ExitCode == 0;
            ReaderCodexPush.NoteVoiceEntryOutcome(
                requestId, ok,
                (ok ? "通道已建立：" : "通道还建不起来（多半是 Codex 还没加载完）：")
                + (output.Length > 160 ? output[..160] : output).Trim());
            return ok;
        }
        catch (OperationCanceledException)
            when (cancellationToken.IsCancellationRequested)
        {
            return false;
        }
        catch (Exception exception)
        {
            ReaderCodexPush.NoteVoiceEntryOutcome(
                requestId, false,
                "建通道脚本跑不起来：" + exception.GetType().Name);
            return false;
        }
    }

    /// 一次建通道的上界。它要连管道、问 tools/list、列对话 —— 正常一两秒，
    /// 卡住多半是管道那头没人。
    private static readonly TimeSpan EnsureChannelTimeout =
        TimeSpan.FromSeconds(20);

    /// 与 NativeMessagingHost 用同一个解释器路径。
    private static string PythonExecutable() => Path.Combine(
        Environment.GetFolderPath(
            Environment.SpecialFolder.LocalApplicationData),
        "Programs", "Python", "Python313", "python.exe");

    /// <summary>桥自己把语音开起来。</summary>
    /// <remarks>
    /// 走的是与保活收敛**同一条**链，所以守卫也是同一套 —— 不另拼一条按键链
    /// （2026-09-09 那次就是抄漏了"拉起 Codex"这一步）。
    ///
    /// 整段 fire-and-forget 且**不重试**：SetActiveAsync 内部已有观察窗与冷却，
    /// 在外面再按一次的含义是不确定的（可能补上一次失败，也可能把刚起来的
    /// 通话按掉）。成没成如实记进账本，由 App 决定要不要让用户再点。
    /// </remarks>
    /// F24 兜底开关。实现只在 <see cref="DirectCodexVoiceControl"/> 一处
    /// （2026-09-10 收拢）：原来这份私有副本只管起语音，于是"把开关关掉之后
    /// 挂断仍然按 F24"。
    private static bool ShortcutFallbackEnabled() =>
        DirectCodexVoiceControl.ShortcutFallbackEnabled();

    private static void StartVoiceFromBridge(
        IDirectCodexVoiceControl control,
        string requestId)
    {
        if (!ShortcutFallbackEnabled())
        {
            // ⚠ 不按也要留痕：不然"通道不通"与"通道不通且我们选择不兜底"
            // 在外面看长得一样，而后者是用户自己设的，不该被当成故障查。
            ReaderCodexPush.NoteBridgeStart(
                requestId, false,
                "推送没送到，且 F24 兜底在设置里是关的 —— 没有按任何键");
            return;
        }
        _ = Task.Run(async () =>
        {
            using CancellationTokenSource lifetime = new(VoiceEntryBudget);
            try
            {
                _ = await control
                    .SetActiveAsync(active: true, lifetime.Token)
                    .ConfigureAwait(false);
            }
            catch (Exception exception)
            {
                ReaderCodexPush.NoteBridgeStart(
                    requestId, false, "桥端起语音失败：" + exception.Message);
                return;
            }
            string detail;
            try
            {
                detail = control.ReadState().Active == true
                    ? "桥端已起语音"
                    : "桥端按过了，但台账未显示在通话";
            }
            catch (Exception exception)
            {
                detail = "桥端按过了，读不到台账：" + exception.Message;
            }
            ReaderCodexPush.NoteBridgeStart(requestId, true, detail);
        });
    }

    /// 桥端起一次语音的总预算。含冷启动那一段：拉起 Codex、等窗口就绪（20 秒）、
    /// 等音频服务子进程（20 秒）、沉降、观察窗（10 秒）。宁可给足 ——
    /// 不够的表现是把本来会成的那次判成失败。
    private static readonly TimeSpan VoiceEntryBudget =
        TimeSpan.FromSeconds(120);

    private async Task<object> HandleStopAsync(
        JsonElement message,
        CancellationToken cancellationToken)
    {
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId",
            "sessionId");
        RequireAuthenticated();
        RequireVoiceAllowed();
        string sessionId = RequireSafeId(message, "sessionId");
        await _coordinator.StopAsync(
            _connectionId,
            sessionId,
            cancellationToken).ConfigureAwait(false);
        _phase = DirectProtocolPhase.AwaitingStart;
        _activeVoiceSessionId = null;
        _activeVoiceAppKind = null;
        return new
        {
            sessionId,
            state = "idle",
        };
    }

    private async Task<object> HandleHeartbeatAsync(
        JsonElement message,
        CancellationToken cancellationToken)
    {
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId",
            "sessionId",
            "sequence");
        RequireAuthenticated();
        string sessionId = RequireSafeId(message, "sessionId");
        _ = DirectPcmFrameCodec.ParseSessionId(sessionId);
        uint sequence = RequireUInt32(message, "sequence");
        if (sequence == 0)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_HEARTBEAT_SEQUENCE_INVALID",
                "电脑语音心跳序号必须从 1 开始");
        }
        await _coordinator.RenewHeartbeatAsync(
            _connectionId,
            sessionId,
            sequence,
            cancellationToken).ConfigureAwait(false);
        return new
        {
            sessionId,
            sequence,
            state = "active",
        };
    }

    private async Task<object> HandleContextAsync(
        JsonElement message,
        CancellationToken cancellationToken)
    {
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId",
            "sessionId",
            "contextContract",
            "event");
        RequireAuthenticated();
        string mode = RequireContextDeliveryMode();
        bool activeSession = _phase == DirectProtocolPhase.Active;
        bool contextOnly =
            _phase == DirectProtocolPhase.ContextOnly;
        if (
            mode == DirectContextDeliveryMode.LegacyInject
            && !activeSession
        )
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_CONTEXT_NOT_ACTIVE",
                "Reader context 只允许发送到当前活动通话");
        }
        if (
            mode == DirectContextDeliveryMode.SnapshotMcp
            && !activeSession
            && !contextOnly
        )
        {
            throw new DirectProtocolException(
                "BW_READER_CONTEXT_SNAPSHOT_NOT_OPEN",
                "Reader 本地快照连接尚未打开");
        }
        string sessionId = RequireSafeId(message, "sessionId");
        _ = DirectPcmFrameCodec.ParseSessionId(sessionId);
        if (contextOnly)
        {
            RequireContextOnlySession(sessionId);
        }
        string contextContract = RequireString(
            message,
            "contextContract",
            128);
        if (
            contextContract
                != NamedPipeDirectContextAdapter.ContextContract
        )
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_CONTEXT_SCHEMA_INVALID",
                "Reader outgoing context 合同无效");
        }
        DirectContextEvent contextEvent =
            NamedPipeDirectContextAdapter.ValidateEvent(
                message.GetProperty("event"));
        string requestId = RequireSafeId(message, "requestId");
        string outcome;
        if (mode == DirectContextDeliveryMode.LegacyInject)
        {
            DirectContextForwardResult forwarded =
                await _coordinator.ForwardLegacyContextAsync(
                    _connectionId,
                    requestId,
                    sessionId,
                    contextContract,
                    contextEvent,
                    cancellationToken).ConfigureAwait(false);
            outcome = forwarded.Outcome;
        }
        else
        {
            DirectSnapshotForwardResult forwarded =
                await _coordinator.ForwardSnapshotContextAsync(
                    _connectionId,
                    requestId,
                    sessionId,
                    contextEvent,
                    requireActiveOwner: activeSession,
                    cancellationToken).ConfigureAwait(false);
            outcome = forwarded.Outcome;
        }
        return new
        {
            sessionId,
            eventId = contextEvent.EventId,
            seq = contextEvent.Sequence,
            outcome,
        };
    }

    private async Task<object> HandleActiveReadingAsync(
        JsonElement message,
        CancellationToken cancellationToken)
    {
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId",
            "sessionId",
            "activeContract",
            "active");
        RequireAuthenticated();
        if (
            RequireContextDeliveryMode()
                != DirectContextDeliveryMode.SnapshotMcp
        )
        {
            throw new DirectProtocolException(
                "BW_READER_CONTEXT_SNAPSHOT_MODE_REQUIRED",
                "Windows 未启用 Reader 快照 MCP 实验模式");
        }
        bool activeSession = _phase == DirectProtocolPhase.Active;
        if (
            !activeSession
            && _phase != DirectProtocolPhase.ContextOnly
        )
        {
            throw new DirectProtocolException(
                "BW_READER_CONTEXT_SNAPSHOT_NOT_OPEN",
                "Reader 本地快照连接尚未打开");
        }
        if (
            RequireString(message, "activeContract", 128)
                != FileDirectSnapshotContextAdapter
                    .ActiveReadingContract
        )
        {
            throw new DirectProtocolException(
                "BW_READER_ACTIVE_READING_SCHEMA_INVALID",
                "Reader active-reading 合同无效");
        }
        string requestId = RequireSafeId(message, "requestId");
        string sessionId = RequireSafeId(message, "sessionId");
        _ = DirectPcmFrameCodec.ParseSessionId(sessionId);
        if (!activeSession)
        {
            RequireContextOnlySession(sessionId);
        }
        DirectActiveReading activeReading =
            FileDirectSnapshotContextAdapter.ValidateActiveReading(
                message.GetProperty("active"));
        DirectSnapshotForwardResult forwarded =
            await _coordinator.ForwardActiveReadingAsync(
                _connectionId,
                requestId,
                sessionId,
                activeReading,
                requireActiveOwner: activeSession,
                cancellationToken).ConfigureAwait(false);
        // 提示板的焦点（2026-09-09）。
        //
        // ⚠⚠ `ForwardActiveReadingAsync` 有**两个**调用点，而我上一版只补了
        //   另一个（DirectBridgeServer 里那条 HTTP POST）。那条是**浏览器扩展**
        //   走的；**App 走的是这条直连通道**。于是表现正好是用户报的：
        //   "扩展里打开新网页板子更新了，但 app 书换页、绘图没变化"。
        //   改这类东西之前先数清有几个调用点 —— 这是 CLAUDE.md 里那条
        //   "先数清楚它有几份副本"的又一次实证。
        //
        // 身份取书+页；「在新页上划选」立刻确认，不必等满 45 秒
        // （SelectionState 三种取值里只有 active 表示他真的划了）。
        // 与 HTTP 那条保持逐字一致，两边分叉的表现会是"某些设备上焦点不动"。
        string focusPage = activeReading.Page.ValueKind == JsonValueKind.Number
            ? activeReading.Page.ToString()
            : string.Empty;
        string focusTitle = string.IsNullOrWhiteSpace(activeReading.Title)
            ? activeReading.File
            : activeReading.Title;
        ReaderAttentionBoard.NoteLocation(
            focusPage.Length == 0
                ? activeReading.File
                : activeReading.File + "#" + focusPage,
            focusPage.Length == 0
                ? focusTitle
                : focusTitle + " 第 " + focusPage + " 页",
            DateTimeOffset.UtcNow,
            source: activeReading.SourceInstanceId,
            interacted: string.Equals(
                activeReading.SelectionState, "active",
                StringComparison.Ordinal));
        return new
        {
            sessionId,
            revision = forwarded.Revision,
            outcome = forwarded.Outcome,
        };
    }

    private async Task<object> HandleContextClearAsync(
        JsonElement message,
        CancellationToken cancellationToken)
    {
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId",
            "sessionId");
        RequireAuthenticated();
        string mode = RequireContextDeliveryMode();
        bool activeSession = _phase == DirectProtocolPhase.Active;
        bool contextOnly =
            _phase == DirectProtocolPhase.ContextOnly;
        bool legacyTransition =
            mode == DirectContextDeliveryMode.LegacyInject
            && _phase == DirectProtocolPhase.AwaitingStart;
        if (!activeSession && !contextOnly && !legacyTransition)
        {
            throw new DirectProtocolException(
                "BW_READER_CONTEXT_SNAPSHOT_CLEAR_PHASE_INVALID",
                "当前连接不能清空 Reader 本地快照");
        }
        string requestId = RequireSafeId(message, "requestId");
        string sessionId = RequireSafeId(message, "sessionId");
        _ = DirectPcmFrameCodec.ParseSessionId(sessionId);
        if (contextOnly)
        {
            RequireContextOnlySession(sessionId);
        }
        DirectSnapshotForwardResult forwarded =
            await _coordinator.ClearSnapshotContextAsync(
                _connectionId,
                requestId,
                sessionId,
                requireActiveOwner: activeSession,
                cancellationToken).ConfigureAwait(false);
        return new
        {
            sessionId,
            revision = forwarded.Revision,
            outcome = forwarded.Outcome,
        };
    }

    private async Task<object> HandleExtensionLogAsync(
        JsonElement message,
        CancellationToken cancellationToken)
    {
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId",
            "sessionId",
            "entries");
        RequireAuthenticated();
        if (_phase is not (
            DirectProtocolPhase.AwaitingStart
            or DirectProtocolPhase.ContextOnly
            or DirectProtocolPhase.Active))
        {
            throw new DirectProtocolException(
                "BW_READER_EXTENSION_LOG_PHASE_INVALID",
                "当前连接阶段不接受扩展日志");
        }
        string sessionId = RequireSafeId(message, "sessionId");
        _ = DirectPcmFrameCodec.ParseSessionId(sessionId);
        if (_phase == DirectProtocolPhase.ContextOnly)
        {
            RequireContextOnlySession(sessionId);
        }
        else if (
            _phase == DirectProtocolPhase.Active
            && !string.Equals(
                _activeVoiceSessionId,
                sessionId,
                StringComparison.Ordinal)
        )
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_SESSION_MISMATCH",
                "扩展日志 sessionId 与当前语音会话不匹配");
        }
        if (
            !message.TryGetProperty("entries", out JsonElement entriesValue)
            || entriesValue.ValueKind != JsonValueKind.Array
            || entriesValue.GetArrayLength() is < 1 or > 50
        )
        {
            throw new DirectProtocolException(
                "BW_READER_EXTENSION_LOG_ENTRIES_INVALID",
                "扩展日志每批必须包含 1 至 50 条记录");
        }

        List<DirectExtensionLogEntry> entries = [];
        foreach (JsonElement entryValue in entriesValue.EnumerateArray())
        {
            RequireExactKeys(
                entryValue,
                "at",
                "source",
                "stage",
                "detail");
            string at = RequireString(entryValue, "at", 64);
            string source = RequireString(entryValue, "source", 32);
            if (source is not (
                "extension-page"
                or "content-script"
                or "call-page"))
            {
                throw new DirectProtocolException(
                    "BW_READER_EXTENSION_LOG_SOURCE_INVALID",
                    "扩展日志 source 无效");
            }
            string stage = RequireString(entryValue, "stage", 64);
            if (!DirectBridgeContract.IsSafeId(stage))
            {
                throw new DirectProtocolException(
                    "BW_READER_EXTENSION_LOG_STAGE_INVALID",
                    "扩展日志 stage 无效");
            }
            string detail = RequireString(entryValue, "detail", 500);
            entries.Add(new DirectExtensionLogEntry(
                at,
                source,
                stage,
                detail));
        }

        try
        {
            int accepted = await DirectExtensionLogStore.AppendAsync(
                _configStore.InstallationRoot,
                _connectionId,
                sessionId,
                entries,
                _utcNow(),
                cancellationToken).ConfigureAwait(false);
            return new
            {
                ok = true,
                accepted,
            };
        }
        catch (
            Exception exception
        ) when (
            exception is IOException
            or UnauthorizedAccessException
            or NotSupportedException
        )
        {
            throw new DirectProtocolException(
                "BW_READER_EXTENSION_LOG_WRITE_FAILED",
                "Windows 无法写入扩展诊断日志",
                retryable: true,
                innerException: exception);
        }
    }

    internal static object StatusEvent(string state, string reason) =>
        new
        {
            contract = DirectBridgeContract.Contract,
            type = "event",
            @event = "status",
            payload = new
            {
                state,
                reason,
            },
        };

    private void RequireAuthenticated()
    {
        if (!_authenticated)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_AUTH_REQUIRED",
                "当前连接尚未认证");
        }
    }

    private string RequireContextDeliveryMode() =>
        _contextDeliveryMode
        ?? throw new DirectProtocolException(
            "BW_COMPUTER_VOICE_DIRECT_AUTH_REQUIRED",
            "当前连接尚未认证");

    private void RequireContextOnlySession(string sessionId)
    {
        if (
            _contextOnlySessionId is null
            || !string.Equals(
                _contextOnlySessionId,
                sessionId,
                StringComparison.Ordinal)
        )
        {
            throw new DirectProtocolException(
                "BW_READER_CONTEXT_SNAPSHOT_SESSION_MISMATCH",
                "Reader 本地快照 sessionId 与当前连接不匹配");
        }
    }

    private static object Success(
        string requestId,
        string action,
        object payload) =>
        new
        {
            contract = DirectBridgeContract.Contract,
            type = "result",
            requestId,
            ok = true,
            action,
            payload,
        };

    private static object Failure(
        string requestId,
        string action,
        string code,
        string message,
        bool retryable) =>
        new
        {
            contract = DirectBridgeContract.Contract,
            type = "result",
            requestId,
            ok = false,
            action,
            error = new
            {
                code,
                message,
                retryable,
            },
        };

    private static string RequireSafeId(
        JsonElement message,
        string name)
    {
        string result = RequireString(message, name, 160);
        if (!DirectBridgeContract.IsSafeId(result))
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_ID_INVALID",
                $"{name} 无效");
        }
        return result;
    }

    private static string RequireString(
        JsonElement message,
        string name,
        int maximumLength)
    {
        if (
            !message.TryGetProperty(name, out JsonElement value)
            || value.ValueKind != JsonValueKind.String
            || value.GetString() is not string result
            || result.Length is < 1
            || result.Length > maximumLength
        )
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_MESSAGE_INVALID",
                $"{name} 字段无效");
        }
        return result;
    }

    private static string RequireBoundedString(
        JsonElement message,
        string name,
        int maximumLength)
    {
        if (
            !message.TryGetProperty(name, out JsonElement value)
            || value.ValueKind != JsonValueKind.String
            || value.GetString() is not string result
            || result.Length > maximumLength
        )
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_MESSAGE_INVALID",
                $"{name} 字段无效");
        }
        return result;
    }

    private static uint RequireUInt32(
        JsonElement message,
        string name)
    {
        if (
            !message.TryGetProperty(name, out JsonElement value)
            || value.ValueKind != JsonValueKind.Number
            || !value.TryGetUInt32(out uint result)
        )
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_MESSAGE_INVALID",
                $"{name} 字段无效");
        }
        return result;
    }

    private static bool RequireBoolean(
        JsonElement message,
        string name)
    {
        if (
            !message.TryGetProperty(name, out JsonElement value)
            || value.ValueKind is not (
                JsonValueKind.True or JsonValueKind.False)
        )
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_MESSAGE_INVALID",
                $"{name} 字段无效");
        }
        return value.GetBoolean();
    }

    private static void RequireObject(JsonElement value)
    {
        if (value.ValueKind != JsonValueKind.Object)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_MESSAGE_INVALID",
                "直连消息必须是对象");
        }
    }

    /// <summary>
    /// 实体路必须带上整批卡面：登记表按 draftId 存一整批，
    /// <c>ResolveCardAsync</c> 还要用 cardIndex 在这批里定位。
    /// </summary>
    private static JsonArray RequireEntityCards(JsonElement message)
    {
        if (!message.TryGetProperty("cards", out JsonElement value)
            || value.ValueKind != JsonValueKind.Array)
        {
            throw new DirectProtocolException(
                "BW_READER_ANKI_REQUEST_INVALID",
                "Reader 本地 Anki 实体导出必须带上整批卡面");
        }
        JsonArray cards = JsonNode.Parse(value.GetRawText()) as JsonArray
            ?? throw new DirectProtocolException(
                "BW_READER_ANKI_REQUEST_INVALID",
                "Reader 本地 Anki 实体卡面无效");
        if (cards.Count is < 1 or > 20
            || cards.Any(node => node is not JsonObject))
        {
            throw new DirectProtocolException(
                "BW_READER_ANKI_REQUEST_INVALID",
                "Reader 本地 Anki 实体卡面无效");
        }
        return cards;
    }

    private static void RequireExactKeys(
        JsonElement value,
        params string[] expected)
    {
        RequireObject(value);
        HashSet<string> actual = value.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        if (!actual.SetEquals(expected))
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_MESSAGE_INVALID",
                "直连消息字段不匹配");
        }
    }
}

internal sealed record DirectProtocolReply(
    object Envelope,
    Func<CancellationToken, Task>? AfterSendAsync);

internal sealed record DirectStartActionResult(
    object Payload,
    Func<CancellationToken, Task> AfterSendAsync);

internal sealed record DirectExtensionLogEntry(
    string At,
    string Source,
    string Stage,
    string Detail);

internal static class DirectExtensionLogStore
{
    private const long MaximumLogBytes = 5L * 1024 * 1024;
    private const string LogContract = "reader-extension-runtime-log/1";
    private static readonly UTF8Encoding Utf8 = new(
        encoderShouldEmitUTF8Identifier: false,
        throwOnInvalidBytes: true);
    private static readonly SemaphoreSlim WriteGate = new(1, 1);

    internal static string GetLogPath(string installationRoot) =>
        Path.Combine(
            Path.GetFullPath(installationRoot),
            "runtime",
            "extension-log.jsonl");

    internal static async Task<int> AppendAsync(
        string installationRoot,
        string connectionId,
        string sessionId,
        IReadOnlyList<DirectExtensionLogEntry> entries,
        DateTimeOffset receivedAtUtc,
        CancellationToken cancellationToken)
    {
        if (entries.Count is < 1 or > 50)
        {
            throw new ArgumentOutOfRangeException(nameof(entries));
        }
        string path = GetLogPath(installationRoot);
        StringBuilder payload = new();
        foreach (DirectExtensionLogEntry entry in entries)
        {
            payload.Append(JsonSerializer.Serialize(new
            {
                contract = LogContract,
                receivedAtUtc,
                at = entry.At,
                source = entry.Source,
                stage = entry.Stage,
                detail = entry.Detail,
                connectionId,
                sessionId,
            }, DirectBridgeContract.JsonOptions));
            payload.Append('\n');
        }
        byte[] bytes = Utf8.GetBytes(payload.ToString());

        await WriteGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            string? directory = Path.GetDirectoryName(path);
            if (string.IsNullOrWhiteSpace(directory))
            {
                throw new InvalidOperationException(
                    "Extension log directory is unavailable");
            }
            Directory.CreateDirectory(directory);
            FileInfo current = new(path);
            if (
                current.Exists
                && current.Length + bytes.Length > MaximumLogBytes
            )
            {
                string previousPath = path + ".1";
                File.Move(path, previousPath, overwrite: true);
            }
            await using FileStream stream = new(
                path,
                FileMode.Append,
                FileAccess.Write,
                FileShare.Read,
                bufferSize: 4096,
                FileOptions.Asynchronous | FileOptions.WriteThrough);
            await stream.WriteAsync(bytes, cancellationToken)
                .ConfigureAwait(false);
            await stream.FlushAsync(cancellationToken).ConfigureAwait(false);
            return entries.Count;
        }
        finally
        {
            WriteGate.Release();
        }
    }
}
