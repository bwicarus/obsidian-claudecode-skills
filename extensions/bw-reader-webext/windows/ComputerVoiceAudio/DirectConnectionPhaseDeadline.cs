namespace BwReader.ComputerVoiceAudio;

internal sealed class DirectConnectionPhaseDeadline
{
    private readonly Func<long> _monotonicMilliseconds;
    private readonly long _authenticationDeadlineMilliseconds;
    private long? _startDeadlineMilliseconds;

    internal DirectConnectionPhaseDeadline(
        Func<long>? monotonicMilliseconds = null)
    {
        _monotonicMilliseconds =
            monotonicMilliseconds ?? (() => Environment.TickCount64);
        _authenticationDeadlineMilliseconds = checked(
            _monotonicMilliseconds()
            + DirectBridgeContract
                .AuthenticationTimeoutMilliseconds);
    }

    internal void Observe(DirectProtocolPhase phase)
    {
        if (
            phase == DirectProtocolPhase.AwaitingStart
            && _startDeadlineMilliseconds is null
        )
        {
            _startDeadlineMilliseconds = checked(
                _monotonicMilliseconds()
                + DirectBridgeContract.StartTimeoutMilliseconds);
        }
    }

    internal int? GetRemainingMilliseconds(DirectProtocolPhase phase)
    {
        long? deadline = phase switch
        {
            DirectProtocolPhase.AwaitingAuthentication =>
                _authenticationDeadlineMilliseconds,
            DirectProtocolPhase.AwaitingStart =>
                _startDeadlineMilliseconds
                ?? throw new InvalidOperationException(
                    "authenticated phase was not observed"),
            DirectProtocolPhase.Starting
                or DirectProtocolPhase.Active
                or DirectProtocolPhase.ContextOnly => null,
            _ => throw new InvalidOperationException(
                "unknown direct protocol phase"),
        };
        if (deadline is null)
        {
            return null;
        }
        long remaining = deadline.Value - _monotonicMilliseconds();
        if (remaining <= 0)
        {
            return 0;
        }
        return (int)Math.Min(remaining, int.MaxValue);
    }

    internal bool IsExpired(DirectProtocolPhase phase) =>
        GetRemainingMilliseconds(phase) == 0;

    internal bool IsAuthenticationExpired() =>
        _authenticationDeadlineMilliseconds
            - _monotonicMilliseconds() <= 0;

    internal static DirectProtocolException TimeoutFailure(
        DirectProtocolPhase phase) =>
        phase switch
        {
            DirectProtocolPhase.AwaitingAuthentication =>
                new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_AUTH_TIMEOUT",
                    "电脑语音连接认证超时"),
            DirectProtocolPhase.AwaitingStart =>
                new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_START_TIMEOUT",
                    "电脑语音等待启动超时"),
            _ => throw new InvalidOperationException(
                "当前协议阶段没有连接期限"),
        };
}
