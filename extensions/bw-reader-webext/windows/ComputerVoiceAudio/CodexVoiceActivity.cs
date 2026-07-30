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

internal interface ICodexVoiceShortcutSender
{
    void Send(CodexAppTarget target);
}

internal sealed class WindowsCodexVoiceShortcutSender
    : ICodexVoiceShortcutSender
{
    public void Send(CodexAppTarget target) =>
        WindowsCodexAppProbe.SendVoiceShortcutOrThrow(target);
}

internal sealed class WindowsRegistryCodexVoiceActivitySource
    : ICodexVoiceActivitySource
{
    // This is a capability-use ledger only. It contains two timestamps and
    // does not expose conversation text, transcript, or audio samples.
    internal const string RegistryPath =
        @"Software\Microsoft\Windows\CurrentVersion\"
        + @"CapabilityAccessManager\ConsentStore\microphone\"
        + "OpenAI.Codex_2p2nqsd0c76g0";

    private const string StartValueName = "LastUsedTimeStart";
    private const string StopValueName = "LastUsedTimeStop";

    public CodexVoiceActivitySnapshot Read()
    {
        if (!OperatingSystem.IsWindows())
        {
            return CodexVoiceActivitySnapshot.Unavailable();
        }

        try
        {
            using RegistryKey? key = Registry.CurrentUser.OpenSubKey(
                RegistryPath,
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

internal sealed record CodexVoiceStartConfirmation(
    CodexVoiceActivitySnapshot Snapshot,
    bool StartedByBridge)
{
    internal bool OwnsVoice => StartedByBridge;
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
    internal static readonly TimeSpan TransitionTimeout =
        TimeSpan.FromSeconds(5);
    internal static readonly TimeSpan MonitorInterval =
        TimeSpan.FromMilliseconds(250);

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

    internal CodexVoiceActivityController()
        : this(
            new WindowsRegistryCodexVoiceActivitySource(),
            new SystemCodexVoiceActivityClock())
    {
    }

    internal CodexVoiceActivityController(
        ICodexVoiceActivitySource source,
        ICodexVoiceActivityClock clock)
    {
        ArgumentNullException.ThrowIfNull(source);
        ArgumentNullException.ThrowIfNull(clock);
        _source = source;
        _clock = clock;
    }

    internal CodexVoiceStartBaseline CaptureStartBaseline() =>
        new(ReadRequired());

    internal CodexVoiceActivitySnapshot ReadCurrent() =>
        ReadRequired();

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
                StartedByBridge: false);
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
                return new CodexVoiceStartConfirmation(
                    current,
                    StartedByBridge: true);
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
            while (true)
            {
                cancellationToken.ThrowIfCancellationRequested();
                CodexVoiceActivitySnapshot current = ReadRequired();
                if (
                    !current.Active
                    || current.LastUsedTimeStart
                        != confirmation.Snapshot.LastUsedTimeStart
                )
                {
                    return new DirectProtocolException(
                        ClosedLocallyCode,
                        "Codex 本地语音会话已关闭或已被另一代会话替换");
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
