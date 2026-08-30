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
                CodexAppTarget target = RequireCodexTarget();
                if (active)
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

                shortcutSender.Send(target, DirectVoiceCommand.Stop);
                return await controller.ConfirmStoppedAsync(
                    before,
                    CodexVoiceActivityController.StopTransitionTimeout,
                    CodexVoiceActivityController.MonitorInterval,
                    cancellationToken).ConfigureAwait(false);
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
            await ReconcileKeepActiveAsync(
                initialReconcile: true,
                cancellationToken)
                .ConfigureAwait(false);
            while (await timer.WaitForNextTickAsync(cancellationToken)
                .ConfigureAwait(false))
            {
                await ReconcileKeepActiveAsync(
                    initialReconcile: false,
                    cancellationToken)
                    .ConfigureAwait(false);
            }
        }
        catch (OperationCanceledException)
            when (cancellationToken.IsCancellationRequested)
        {
        }
    }

    private async Task ReconcileKeepActiveAsync(
        bool initialReconcile,
        CancellationToken cancellationToken)
    {
        bool intentChanged = RefreshKeepActiveFromDisk(
            out bool enabled,
            out _,
            out CancellationToken intentToken);
        if (!enabled)
        {
            if (intentChanged || initialReconcile)
            {
                try
                {
                    DirectCodexVoiceState state = ReadState();
                    if (state.Status == "available" && state.Active == true)
                    {
                        _ = await SetActiveSerializedAsync(
                            active: false,
                            intentToken).ConfigureAwait(false);
                    }
                }
                catch (OperationCanceledException) when (
                    intentToken.IsCancellationRequested
                    || cancellationToken.IsCancellationRequested)
                {
                }
                catch (Exception exception)
                {
                    NotifyAutomaticRecoveryFailed(exception);
                }
            }
            return;
        }
        StartAutomaticRecoveryIfNeeded(cancellationToken);
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

    /// <summary>连败 n 次后该等多久：20s 起，每次翻倍，封顶 5 分钟。</summary>
    internal static TimeSpan BackoffFor(int priorFailureCount)
    {
        if (priorFailureCount <= 0)
        {
            return TimeSpan.Zero;
        }
        double seconds = AutomaticRecoveryRetryDelay.TotalSeconds
            * Math.Pow(2, Math.Min(priorFailureCount - 1, 8));
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

        try
        {
            using CancellationTokenSource stopLifetime = new(
                DisposeStopTimeout);
            DirectCodexVoiceSetResult stopped =
                await SetKeepActiveAsync(
                    enabled: false,
                    stopLifetime.Token).WaitAsync(
                        DisposeStopTimeout).ConfigureAwait(false);
            if (
                stopped.State.Status != "available"
                || stopped.State.Active != false
            )
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_DISPOSE_STOP_UNCONFIRMED",
                    "Direct 退出前未能确认 Codex 语音已停止");
            }
        }
        catch (Exception exception) when (
            exception is OperationCanceledException
            or TimeoutException)
        {
            failures ??= [];
            failures.Add(new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_DISPOSE_STOP_TIMEOUT",
                "Direct 退出前等待 Codex 语音停止超时",
                retryable: false,
                innerException: exception));
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
        if (hasProjection)
        {
            RequireExactKeys(
                message,
                "contract",
                "type",
                "requestId",
                "sessionId",
                "sourceInstanceId",
                "draftId",
                "cardIndex",
                "aid",
                "card",
                "projection");
        }
        else
        {
            // Rolling upgrade: Direct is installed before WebExt. The previous
            // extension sent canonical Markdown only as `card`; use that same
            // Markdown-shaped value as the fallback projection until WebExt
            // starts sending the separately rendered `projection` field.
            RequireExactKeys(
                message,
                "contract",
                "type",
                "requestId",
                "sessionId",
                "sourceInstanceId",
                "draftId",
                "cardIndex",
                "aid",
                "card");
        }
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
        string sourceInstanceId = RequireSafeId(
            message,
            "sourceInstanceId");
        string draftId = RequireString(message, "draftId", 64);
        string aid = RequireString(message, "aid", 64);
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
            ReaderLocalAnkiWriteOutcome outcome =
                await _localAnkiWriter.AddAsync(
                    sourceInstanceId,
                    draftId,
                    cardIndex,
                    aid,
                    card,
                    projection,
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
        };

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
