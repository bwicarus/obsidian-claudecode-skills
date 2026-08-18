using Microsoft.Win32;
using System.Buffers.Binary;
using System.Security;

namespace BwReader.ComputerVoiceAudio;

internal enum CodexVoiceActivityReadStatus
{
    Available,
    Unavailable,
    Error,
}

internal sealed record CodexVoiceActivitySnapshot(
    CodexVoiceActivityReadStatus Status,
    long LastUsedTimeStart,
    long LastUsedTimeStop)
{
    internal bool Active =>
        Status == CodexVoiceActivityReadStatus.Available
        && LastUsedTimeStart > 0
        && (
            LastUsedTimeStop == 0
            || LastUsedTimeStart > LastUsedTimeStop
        );

    internal static CodexVoiceActivitySnapshot Available(
        long lastUsedTimeStart,
        long lastUsedTimeStop) =>
        new(
            CodexVoiceActivityReadStatus.Available,
            lastUsedTimeStart,
            lastUsedTimeStop);

    internal static CodexVoiceActivitySnapshot Unavailable() =>
        new(CodexVoiceActivityReadStatus.Unavailable, 0, 0);

    internal static CodexVoiceActivitySnapshot Error() =>
        new(CodexVoiceActivityReadStatus.Error, 0, 0);
}

internal interface ICodexVoiceActivitySource
{
    CodexVoiceActivitySnapshot Read();
}

internal enum DirectVoiceCommand
{
    Start,
    Stop,
}

internal interface ICodexVoiceShortcutSender
{
    void Send(CodexAppTarget target, DirectVoiceCommand command);
}

internal sealed class WindowsCodexVoiceShortcutSender
    : ICodexVoiceShortcutSender
{
    private readonly ICodexVoiceShortcutBrokerTransport _transport;

    internal WindowsCodexVoiceShortcutSender()
        : this(new NamedPipeCodexVoiceShortcutBrokerTransport())
    {
    }

    internal WindowsCodexVoiceShortcutSender(
        ICodexVoiceShortcutBrokerTransport transport)
    {
        _transport = transport
            ?? throw new ArgumentNullException(nameof(transport));
    }

    public void Send(
        CodexAppTarget target,
        DirectVoiceCommand command)
    {
        ArgumentNullException.ThrowIfNull(target);
        if (target.AppKind != DirectAppTargets.CodexDesktop)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_SHORTCUT_TARGET_INVALID",
                "Codex 全局快捷键不能用于其他应用目标");
        }
        // Recheck the same root generation immediately before asking the
        // interactive-user broker to emit F24.  The broker is deliberately
        // only a one-shot input executor, not an app-discovery authority.
        WindowsCodexAppProbe.RequireCurrentReadyTarget(target);
        WindowsCodexAppProbe.RequireExpectedGlobalVoiceShortcut();

        string requestId = CodexVoiceShortcutBrokerContract.NewRequestId();
        string request = CodexVoiceShortcutBrokerContract.SerializeRequest(
            requestId,
            target);
        string receipt = _transport.Exchange(request);
        CodexVoiceShortcutBrokerContract.RequireSuccessfulReceipt(
            receipt,
            requestId);
    }
}

internal sealed class WindowsRegistryCodexVoiceActivitySource
    : ICodexVoiceActivitySource
{
    // This is a capability-use ledger only. It contains two timestamps and
    // does not expose conversation text, transcript, or audio samples.
    internal const string RegistryPath =
        @"Software\Microsoft\Windows\CurrentVersion\"
        + @"CapabilityAccessManager\ConsentStore\microphone\"
        + "OpenAI.CodexBeta_2p2nqsd0c76g0";

    private const string StartValueName = "LastUsedTimeStart";
    private const string StopValueName = "LastUsedTimeStop";
    private readonly string _registryPath;

    internal WindowsRegistryCodexVoiceActivitySource()
        : this(DirectAppTargets.CodexDesktop)
    {
    }

    internal WindowsRegistryCodexVoiceActivitySource(string appKind)
    {
        DirectAppTargetProfile profile = DirectAppTargets.Require(appKind);
        _registryPath =
            @"Software\Microsoft\Windows\CurrentVersion\"
            + @"CapabilityAccessManager\ConsentStore\microphone\"
            + profile.MicrophoneConsentPackageKey;
    }

    public CodexVoiceActivitySnapshot Read()
    {
        if (!OperatingSystem.IsWindows())
        {
            return CodexVoiceActivitySnapshot.Unavailable();
        }

        try
        {
            using RegistryKey? key = Registry.CurrentUser.OpenSubKey(
                _registryPath,
                writable: false);
            if (key is null)
            {
                return CodexVoiceActivitySnapshot.Unavailable();
            }

            object? startValue = key.GetValue(
                StartValueName,
                defaultValue: null,
                RegistryValueOptions.DoNotExpandEnvironmentNames);
            object? stopValue = key.GetValue(
                StopValueName,
                defaultValue: null,
                RegistryValueOptions.DoNotExpandEnvironmentNames);
            if (startValue is null || stopValue is null)
            {
                return CodexVoiceActivitySnapshot.Unavailable();
            }
            if (
                !TryReadNonNegativeFileTime(startValue, out long start)
                || !TryReadNonNegativeFileTime(stopValue, out long stop)
            )
            {
                return CodexVoiceActivitySnapshot.Error();
            }
            return CodexVoiceActivitySnapshot.Available(start, stop);
        }
        catch (Exception exception) when (
            exception is IOException
            or UnauthorizedAccessException
            or SecurityException
            or ObjectDisposedException
        )
        {
            return CodexVoiceActivitySnapshot.Error();
        }
    }

    private static bool TryReadNonNegativeFileTime(
        object value,
        out long fileTime)
    {
        fileTime = value switch
        {
            long number => number,
            int number => number,
            byte[] bytes when bytes.Length == sizeof(long) =>
                BinaryPrimitives.ReadInt64LittleEndian(bytes),
            _ => -1,
        };
        return fileTime >= 0;
    }
}

internal interface ICodexVoiceActivityClock
{
    DateTimeOffset UtcNow { get; }

    Task DelayAsync(
        TimeSpan delay,
        CancellationToken cancellationToken);
}

internal sealed class SystemCodexVoiceActivityClock
    : ICodexVoiceActivityClock
{
    public DateTimeOffset UtcNow => DateTimeOffset.UtcNow;

    public Task DelayAsync(
        TimeSpan delay,
        CancellationToken cancellationToken) =>
        Task.Delay(delay, cancellationToken);
}

internal sealed record CodexVoiceStartBaseline(
    CodexVoiceActivitySnapshot Snapshot)
{
    internal bool ShortcutRequired => !Snapshot.Active;
}

internal sealed record CodexVoiceShortcutReceipt(
    Guid AttemptId,
    uint RootProcessId,
    long RootProcessStartFileTimeUtc,
    long SentAtFileTimeUtc);

internal sealed record CodexVoiceOwnershipToken(
    Guid AttemptId,
    uint RootProcessId,
    long RootProcessStartFileTimeUtc,
    long VoiceStartFileTimeUtc);

internal interface ICodexVoiceOwnershipAttestor
{
    CodexVoiceOwnershipToken? TryAttest(
        CodexVoiceShortcutReceipt receipt,
        CodexVoiceActivitySnapshot observedTransition);
}

internal sealed class FailClosedCodexVoiceOwnershipAttestor
    : ICodexVoiceOwnershipAttestor
{
    public CodexVoiceOwnershipToken? TryAttest(
        CodexVoiceShortcutReceipt receipt,
        CodexVoiceActivitySnapshot observedTransition) =>
        null;
}

internal sealed class ExactTargetVoiceOwnershipAttestor
    : ICodexVoiceOwnershipAttestor
{
    public CodexVoiceOwnershipToken? TryAttest(
        CodexVoiceShortcutReceipt receipt,
        CodexVoiceActivitySnapshot observedTransition) =>
        new(
            receipt.AttemptId,
            receipt.RootProcessId,
            receipt.RootProcessStartFileTimeUtc,
            observedTransition.LastUsedTimeStart);
}

internal sealed record CodexVoiceStartConfirmation(
    CodexVoiceActivitySnapshot Snapshot,
    bool ObservedAfterShortcut,
    CodexVoiceOwnershipToken? OwnershipToken)
{
    internal bool StartedByBridge => OwnershipToken is not null;

    internal bool OwnsVoice => OwnershipToken is not null;
}

internal sealed record CodexVoiceStopPlan(
    CodexVoiceActivitySnapshot Snapshot,
    bool OwnedByBridge,
    bool VoiceGenerationMatches)
{
    internal bool ShortcutRequired =>
        OwnedByBridge
        && VoiceGenerationMatches
        && Snapshot.Active;
}

internal sealed class CodexVoiceActivityController
{
    internal static readonly TimeSpan StartObservationTimeout =
        TimeSpan.FromSeconds(20);
    internal static readonly TimeSpan StartUsableSettleDelay =
        TimeSpan.FromSeconds(8);
    internal static readonly TimeSpan StopTransitionTimeout =
        TimeSpan.FromSeconds(5);
    internal static readonly TimeSpan MonitorInterval =
        TimeSpan.FromMilliseconds(250);

    /// <summary>
    /// 判定「通话已在本地关闭」需要连续命中的帧数。
    /// </summary>
    /// <remarks>
    /// 单帧定罪会把撕裂读和同通话内的麦克风重取误判成挂断，代价是挂掉用户的电话；
    /// 多等 <c>MonitorInterval</c> × (N-1) 的代价只是晚一点收摊。按 250ms 一帧，
    /// 4 帧 ≈ 1 秒 —— 对"用户主动挂断"这件事来说察觉得足够快。
    /// </remarks>
    internal const int LocalCloseConfirmations = 4;

    internal const string ActivityUnavailableCode =
        "BW_COMPUTER_VOICE_DIRECT_ACTIVITY_UNAVAILABLE";
    internal const string ActivityReadFailedCode =
        "BW_COMPUTER_VOICE_DIRECT_ACTIVITY_READ_FAILED";
    internal const string StartNotConfirmedCode =
        "BW_COMPUTER_VOICE_DIRECT_VOICE_START_NOT_CONFIRMED";
    internal const string StopNotConfirmedCode =
        "BW_COMPUTER_VOICE_DIRECT_VOICE_STOP_NOT_CONFIRMED";
    internal const string ClosedLocallyCode =
        "BW_COMPUTER_VOICE_DIRECT_VOICE_CLOSED_LOCALLY";

    private readonly ICodexVoiceActivitySource _source;
    private readonly ICodexVoiceActivityClock _clock;
    private readonly ICodexVoiceOwnershipAttestor _ownershipAttestor;

    internal CodexVoiceActivityController()
        : this(
            new WindowsRegistryCodexVoiceActivitySource(),
            new SystemCodexVoiceActivityClock(),
            new FailClosedCodexVoiceOwnershipAttestor())
    {
    }

    internal CodexVoiceActivityController(
        ICodexVoiceActivitySource source,
        ICodexVoiceActivityClock clock,
        ICodexVoiceOwnershipAttestor? ownershipAttestor = null)
    {
        ArgumentNullException.ThrowIfNull(source);
        ArgumentNullException.ThrowIfNull(clock);
        _source = source;
        _clock = clock;
        _ownershipAttestor = ownershipAttestor
            ?? new FailClosedCodexVoiceOwnershipAttestor();
    }

    internal CodexVoiceStartBaseline CaptureStartBaseline() =>
        new(ReadRequired());

    internal CodexVoiceActivitySnapshot ReadCurrent() =>
        ReadRequired();

    internal async Task<CodexVoiceActivitySnapshot>
        WaitForAvailableAsync(
            TimeSpan timeout,
            TimeSpan pollInterval,
            CancellationToken cancellationToken)
    {
        ValidatePolling(timeout, pollInterval);
        DateTimeOffset deadline = _clock.UtcNow + timeout;
        while (true)
        {
            cancellationToken.ThrowIfCancellationRequested();
            CodexVoiceActivitySnapshot snapshot = _source.Read();
            if (
                snapshot.Status
                == CodexVoiceActivityReadStatus.Available
            )
            {
                return snapshot;
            }
            if (snapshot.Status == CodexVoiceActivityReadStatus.Error)
            {
                throw new DirectProtocolException(
                    ActivityReadFailedCode,
                    "读取目标应用麦克风活动状态失败",
                    retryable: true);
            }

            TimeSpan remaining = deadline - _clock.UtcNow;
            if (remaining <= TimeSpan.Zero)
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_VOICE_READY_TIMEOUT",
                    "等待目标应用语音子系统就绪超时",
                    retryable: true);
            }
            await _clock.DelayAsync(
                Min(pollInterval, remaining),
                cancellationToken).ConfigureAwait(false);
        }
    }

    internal CodexVoiceShortcutReceipt RecordShortcutSent(
        CodexVoiceStartBaseline baseline,
        CodexAppTarget target)
    {
        ArgumentNullException.ThrowIfNull(baseline);
        ArgumentNullException.ThrowIfNull(target);
        if (
            !baseline.ShortcutRequired
            || target.RootProcessId == 0
            || target.RootProcessStartFileTimeUtc <= 0
        )
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_VOICE_SHORTCUT_RECEIPT_INVALID",
                "Codex 语音快捷键回执无效");
        }
        return new CodexVoiceShortcutReceipt(
            Guid.NewGuid(),
            target.RootProcessId,
            target.RootProcessStartFileTimeUtc,
            _clock.UtcNow.UtcDateTime.ToFileTimeUtc());
    }

    internal CodexVoiceStopPlan PrepareStop(
        CodexVoiceStartConfirmation confirmation)
    {
        ArgumentNullException.ThrowIfNull(confirmation);
        CodexVoiceActivitySnapshot current = ReadRequired();
        return new CodexVoiceStopPlan(
            current,
            confirmation.OwnsVoice,
            current.Active
            && current.LastUsedTimeStart
                == confirmation.Snapshot.LastUsedTimeStart);
    }

    internal async Task<CodexVoiceStartConfirmation>
        ConfirmStartedAsync(
            CodexVoiceStartBaseline baseline,
            CodexVoiceShortcutReceipt? shortcutReceipt,
            TimeSpan timeout,
            TimeSpan pollInterval,
            CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(baseline);
        RequireAvailable(baseline.Snapshot);
        ValidatePolling(timeout, pollInterval);

        if (baseline.Snapshot.Active)
        {
            return new CodexVoiceStartConfirmation(
                baseline.Snapshot,
                ObservedAfterShortcut: false,
                OwnershipToken: null);
        }

        DateTimeOffset deadline = _clock.UtcNow + timeout;
        while (true)
        {
            cancellationToken.ThrowIfCancellationRequested();
            CodexVoiceActivitySnapshot current = ReadRequired();
            if (
                current.Active
                && current.LastUsedTimeStart
                    > Math.Max(
                        baseline.Snapshot.LastUsedTimeStart,
                        baseline.Snapshot.LastUsedTimeStop)
            )
            {
                CodexVoiceOwnershipToken? ownershipToken =
                    shortcutReceipt is null
                        ? null
                        : ValidateOwnershipToken(
                            shortcutReceipt,
                            current,
                            _ownershipAttestor.TryAttest(
                                shortcutReceipt,
                                current));
                return new CodexVoiceStartConfirmation(
                    current,
                    ObservedAfterShortcut: true,
                    ownershipToken);
            }

            TimeSpan remaining = deadline - _clock.UtcNow;
            if (remaining <= TimeSpan.Zero)
            {
                throw new DirectProtocolException(
                    StartNotConfirmedCode,
                    "Codex 语音快捷键已发送，但未确认新的语音会话已开启",
                    retryable: true);
            }
            await _clock.DelayAsync(
                Min(pollInterval, remaining),
                cancellationToken).ConfigureAwait(false);
        }
    }

    internal async Task<CodexVoiceStartConfirmation>
        ConfirmUsableForStartAsync(
            CodexVoiceStartConfirmation confirmation,
            CodexVoiceShortcutReceipt? shortcutReceipt,
            TimeSpan settleDelay,
            CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(confirmation);
        RequireAvailable(confirmation.Snapshot);

        // ReaderPC now owns Codex Voice keepalive independently from an App
        // audio START.  When this START sent no shortcut and the voice was
        // already active, a fresh read of the same generation is sufficient:
        // the media and route resources have already been committed at the
        // caller's atomic boundary.  Do not impose the legacy eight-second
        // post-shortcut settle delay on that pre-warmed path.
        if (
            shortcutReceipt is null
            && !confirmation.ObservedAfterShortcut
            && confirmation.Snapshot.Active
        )
        {
            CodexVoiceActivitySnapshot current = _source.Read();
            if (
                current.Status
                    == CodexVoiceActivityReadStatus.Available
                && current.Active
                && current.LastUsedTimeStart
                    == confirmation.Snapshot.LastUsedTimeStart
            )
            {
                return confirmation with { Snapshot = current };
            }
        }

        // Cold activation, a changed/closed generation, unavailable activity
        // telemetry, or an actual shortcut receipt retains the bounded settle
        // confirmation and its fail-closed result.
        return await ConfirmUsableAsync(
            confirmation,
            settleDelay,
            cancellationToken).ConfigureAwait(false);
    }

    internal async Task<CodexVoiceStartConfirmation>
        ConfirmUsableAsync(
            CodexVoiceStartConfirmation confirmation,
            TimeSpan settleDelay,
            CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(confirmation);
        RequireAvailable(confirmation.Snapshot);
        if (!confirmation.Snapshot.Active)
        {
            throw new ArgumentException(
                "BW_COMPUTER_VOICE_ACTIVITY_SETTLE_REQUIRES_ACTIVE",
                nameof(confirmation));
        }
        if (settleDelay <= TimeSpan.Zero)
        {
            throw new ArgumentOutOfRangeException(nameof(settleDelay));
        }

        await _clock.DelayAsync(settleDelay, cancellationToken)
            .ConfigureAwait(false);
        CodexVoiceActivitySnapshot current = ReadRequired();
        if (
            !current.Active
            || current.LastUsedTimeStart
                != confirmation.Snapshot.LastUsedTimeStart
        )
        {
            throw new DirectProtocolException(
                StartNotConfirmedCode,
                "Codex 语音浮标已出现，但等待后未保持为同一可用语音会话",
                retryable: true);
        }
        return confirmation with { Snapshot = current };
    }

    internal async Task<CodexVoiceActivitySnapshot>
        ConfirmStoppedAsync(
            CodexVoiceActivitySnapshot beforeShortcut,
            TimeSpan timeout,
            TimeSpan pollInterval,
            CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(beforeShortcut);
        RequireAvailable(beforeShortcut);
        if (!beforeShortcut.Active)
        {
            throw new ArgumentException(
                "BW_COMPUTER_VOICE_ACTIVITY_STOP_REQUIRES_ACTIVE",
                nameof(beforeShortcut));
        }
        ValidatePolling(timeout, pollInterval);

        DateTimeOffset deadline = _clock.UtcNow + timeout;
        while (true)
        {
            cancellationToken.ThrowIfCancellationRequested();
            CodexVoiceActivitySnapshot current = ReadRequired();
            if (
                !current.Active
                && current.LastUsedTimeStart
                    == beforeShortcut.LastUsedTimeStart
                && current.LastUsedTimeStop
                    > beforeShortcut.LastUsedTimeStop
                && current.LastUsedTimeStop
                    >= beforeShortcut.LastUsedTimeStart
            )
            {
                return current;
            }

            TimeSpan remaining = deadline - _clock.UtcNow;
            if (remaining <= TimeSpan.Zero)
            {
                throw new DirectProtocolException(
                    StopNotConfirmedCode,
                    "Codex 语音关闭快捷键已发送，但未确认语音会话已关闭",
                    retryable: true);
            }
            await _clock.DelayAsync(
                Min(pollInterval, remaining),
                cancellationToken).ConfigureAwait(false);
        }
    }

    internal async Task<DirectProtocolException?>
        MonitorForLocalCloseAsync(
            CodexVoiceStartConfirmation confirmation,
            TimeSpan pollInterval,
            CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(confirmation);
        RequireAvailable(confirmation.Snapshot);
        if (!confirmation.Snapshot.Active)
        {
            throw new ArgumentException(
                "BW_COMPUTER_VOICE_ACTIVITY_MONITOR_REQUIRES_ACTIVE",
                nameof(confirmation));
        }
        if (pollInterval <= TimeSpan.Zero)
        {
            throw new ArgumentOutOfRangeException(
                nameof(pollInterval),
                "BW_COMPUTER_VOICE_ACTIVITY_POLL_INTERVAL_INVALID");
        }

        try
        {
            // 「通话结束」必须**连续多帧**都成立才算数（2026-08-18 重做）。
            //
            // 旧行为是单帧定罪：250ms 读一次注册表，任何一帧看着不对就直接判定
            // 通话已关闭，然后把整条链拆掉 —— 用户感知就是"说着说着自己没了"。
            // 而这个数据源恰恰经不起单帧：两个时间戳是**分两次** GetValue 读的，
            // 没有原子快照，撕裂读会把活着的通话读成已结束；同一通通话里麦克风
            // 重新获取也会让 start 变一次。
            //
            // 一次误读的代价是挂断用户的电话，一次漏判的代价只是晚一秒收摊 ——
            // 两边不对称，所以宁可慢。
            int consecutiveClosed = 0;
            while (true)
            {
                cancellationToken.ThrowIfCancellationRequested();
                CodexVoiceActivitySnapshot current = ReadRequired();
                bool looksClosed =
                    !current.Active
                    || current.LastUsedTimeStart
                        != confirmation.Snapshot.LastUsedTimeStart;
                if (looksClosed)
                {
                    consecutiveClosed++;
                    if (consecutiveClosed >= LocalCloseConfirmations)
                    {
                        return new DirectProtocolException(
                            ClosedLocallyCode,
                            "Codex 本地语音会话已关闭或已被另一代会话替换");
                    }
                }
                else
                {
                    consecutiveClosed = 0;
                }
                await _clock.DelayAsync(
                    pollInterval,
                    cancellationToken).ConfigureAwait(false);
            }
        }
        catch (OperationCanceledException)
            when (cancellationToken.IsCancellationRequested)
        {
            return null;
        }
        catch (DirectProtocolException exception)
        {
            return exception;
        }
        catch (Exception exception)
        {
            return new DirectProtocolException(
                ActivityReadFailedCode,
                "无法继续确认 Codex 语音状态",
                retryable: true,
                innerException: exception);
        }
    }

    private CodexVoiceActivitySnapshot ReadRequired()
    {
        CodexVoiceActivitySnapshot snapshot = _source.Read();
        RequireAvailable(snapshot);
        return snapshot;
    }

    private static CodexVoiceOwnershipToken? ValidateOwnershipToken(
        CodexVoiceShortcutReceipt receipt,
        CodexVoiceActivitySnapshot transition,
        CodexVoiceOwnershipToken? token)
    {
        if (token is null)
        {
            return null;
        }
        if (
            token.AttemptId != receipt.AttemptId
            || token.RootProcessId != receipt.RootProcessId
            || token.RootProcessStartFileTimeUtc
                != receipt.RootProcessStartFileTimeUtc
            || token.VoiceStartFileTimeUtc
                != transition.LastUsedTimeStart
        )
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_VOICE_OWNERSHIP_ATTESTATION_INVALID",
                "Codex 语音所有权证明与本次快捷键不匹配",
                retryable: true);
        }
        return token;
    }

    private static void RequireAvailable(
        CodexVoiceActivitySnapshot snapshot)
    {
        ArgumentNullException.ThrowIfNull(snapshot);
        if (snapshot.Status == CodexVoiceActivityReadStatus.Available)
        {
            return;
        }
        if (snapshot.Status == CodexVoiceActivityReadStatus.Unavailable)
        {
            throw new DirectProtocolException(
                ActivityUnavailableCode,
                "当前 Windows 会话没有可用的 Codex 麦克风活动状态",
                retryable: true);
        }
        throw new DirectProtocolException(
            ActivityReadFailedCode,
            "读取 Codex 麦克风活动状态失败",
            retryable: true);
    }

    private static void ValidatePolling(
        TimeSpan timeout,
        TimeSpan pollInterval)
    {
        if (timeout <= TimeSpan.Zero)
        {
            throw new ArgumentOutOfRangeException(
                nameof(timeout),
                "BW_COMPUTER_VOICE_ACTIVITY_TIMEOUT_INVALID");
        }
        if (pollInterval <= TimeSpan.Zero)
        {
            throw new ArgumentOutOfRangeException(
                nameof(pollInterval),
                "BW_COMPUTER_VOICE_ACTIVITY_POLL_INTERVAL_INVALID");
        }
    }

    private static TimeSpan Min(TimeSpan left, TimeSpan right) =>
        left <= right ? left : right;
}
