namespace BwReader.ComputerVoiceAudio;

internal sealed record DirectAppTarget(
    uint RootProcessId,
    string AppKind,
    string AppUserModelId);

internal interface IDirectAppLauncher
{
    bool IsWired { get; }

    Task EnsureRunningAsync(
        string appKind,
        string appUserModelId,
        CancellationToken cancellationToken);

    Task<DirectAppTarget> WaitForUniqueReadyAsync(
        string appKind,
        string appUserModelId,
        TimeSpan timeout,
        CancellationToken cancellationToken);
}

internal sealed class UnwiredDirectAppLauncher : IDirectAppLauncher
{
    public bool IsWired => false;

    public Task EnsureRunningAsync(
        string appKind,
        string appUserModelId,
        CancellationToken cancellationToken) =>
        Task.FromException(
            new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_APP_LAUNCHER_NOT_WIRED",
                "Windows 直连的 Codex 启动适配器尚未接线"));

    public Task<DirectAppTarget> WaitForUniqueReadyAsync(
        string appKind,
        string appUserModelId,
        TimeSpan timeout,
        CancellationToken cancellationToken) =>
        Task.FromException<DirectAppTarget>(
            new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_APP_LAUNCHER_NOT_WIRED",
                "Windows 直连的 Codex 启动适配器尚未接线"));
}

internal sealed record DirectMediaStartRequest(
    string SessionId,
    uint RootProcessId,
    string AppKind,
    string AppUserModelId,
    string MicrophoneEndpointId);

internal sealed record DirectMediaStartResult(
    bool HostReady,
    bool CaptureActive);

internal interface IDirectMediaAdapter : IAsyncDisposable
{
    bool IsWired { get; }

    bool CaptureActive { get; }

    Task<DirectProtocolException?> Completion { get; }

    Task<DirectMediaStartResult> StartAsync(
        DirectMediaStartRequest request,
        Func<DirectPcmFrame, CancellationToken, Task> sendFrameAsync,
        CancellationToken cancellationToken);

    Task StopAsync(CancellationToken cancellationToken);
}

internal sealed class UnwiredDirectMediaAdapter : IDirectMediaAdapter
{
    public bool IsWired => false;

    public bool CaptureActive => false;

    public Task<DirectProtocolException?> Completion =>
        Task.FromResult<DirectProtocolException?>(null);

    public Task<DirectMediaStartResult> StartAsync(
        DirectMediaStartRequest request,
        Func<DirectPcmFrame, CancellationToken, Task> sendFrameAsync,
        CancellationToken cancellationToken) =>
        Task.FromException<DirectMediaStartResult>(
            new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_MEDIA_NOT_WIRED",
                "Windows 直连媒体适配器尚未接线"));

    public Task StopAsync(CancellationToken cancellationToken) =>
        Task.CompletedTask;

    public ValueTask DisposeAsync() => ValueTask.CompletedTask;
}

internal sealed class DirectBridgeCoordinator : IAsyncDisposable
{
    private static readonly TimeSpan AppReadyTimeout = TimeSpan.FromSeconds(20);
    private readonly DirectBridgeConfigStore _configStore;
    private readonly IDirectAppLauncher _appLauncher;
    private readonly IDirectMediaAdapter _mediaAdapter;
    private readonly Func<long> _monotonicMilliseconds;
    private readonly SemaphoreSlim _stateGate = new(1, 1);
    private readonly object _heartbeatGate = new();
    private string? _activeConnectionId;
    private string? _activeSessionId;
    private long? _heartbeatDeadlineMilliseconds;
    private uint _heartbeatSequence;
    private bool _disposed;

    internal DirectBridgeCoordinator(
        DirectBridgeConfigStore configStore,
        IDirectAppLauncher appLauncher,
        IDirectMediaAdapter mediaAdapter,
        Func<long>? monotonicMilliseconds = null)
    {
        _configStore = configStore;
        _appLauncher = appLauncher;
        _mediaAdapter = mediaAdapter;
        _monotonicMilliseconds =
            monotonicMilliseconds ?? (() => Environment.TickCount64);
    }

    internal bool CaptureActive => _mediaAdapter.CaptureActive;

    internal bool AppLauncherReady => _appLauncher.IsWired;

    internal bool MediaHostReady => _mediaAdapter.IsWired;

    internal Task<DirectProtocolException?> MediaCompletion =>
        _mediaAdapter.Completion;

    internal string? ActiveSessionId
    {
        get
        {
            lock (_heartbeatGate)
            {
                return _activeSessionId;
            }
        }
    }

    internal async Task<DirectMediaStartResult> StartAsync(
        string connectionId,
        string sessionId,
        Func<string, string, Task> reportStatusAsync,
        Func<DirectPcmFrame, CancellationToken, Task> sendFrameAsync,
        CancellationToken cancellationToken)
    {
        await _stateGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            ThrowIfDisposed();
            if (_activeSessionId is not null)
            {
                if (
                    _activeConnectionId == connectionId
                    && _activeSessionId == sessionId
                    && _mediaAdapter.CaptureActive
                )
                {
                    return new DirectMediaStartResult(
                        HostReady: true,
                        CaptureActive: true);
                }
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_BUSY",
                    "另一个电脑语音会话正在使用桥接器",
                    retryable: true);
            }

            DirectBridgeConfig config = _configStore.Load();
            if (!config.LocalOptIn)
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_LOCAL_OPT_IN_REQUIRED",
                    "Windows 本机尚未明确启用电脑语音");
            }

            await reportStatusAsync(
                "starting-app",
                "BW_COMPUTER_VOICE_DIRECT_STARTING_APP")
                .ConfigureAwait(false);
            await _appLauncher.EnsureRunningAsync(
                config.AppKind,
                DirectBridgeContract.CodexAppUserModelId,
                cancellationToken).ConfigureAwait(false);

            await reportStatusAsync(
                "waiting-app-ready",
                "BW_COMPUTER_VOICE_DIRECT_WAITING_APP_READY")
                .ConfigureAwait(false);
            DirectAppTarget target;
            try
            {
                target = await _appLauncher.WaitForUniqueReadyAsync(
                    config.AppKind,
                    DirectBridgeContract.CodexAppUserModelId,
                    AppReadyTimeout,
                    cancellationToken).ConfigureAwait(false);
            }
            catch (TimeoutException exception)
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_APP_READY_TIMEOUT",
                    "等待唯一 Codex 窗口就绪超时",
                    retryable: true,
                    innerException: exception);
            }

            if (
                target.RootProcessId == 0
                || target.AppKind != config.AppKind
                || target.AppUserModelId
                    != DirectBridgeContract.CodexAppUserModelId
            )
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_APP_TARGET_INVALID",
                    "Codex 目标进程校验失败");
            }

            await reportStatusAsync(
                "starting-capture",
                "BW_COMPUTER_VOICE_DIRECT_STARTING_CAPTURE")
                .ConfigureAwait(false);
            DirectMediaStartResult started =
                await _mediaAdapter.StartAsync(
                    new DirectMediaStartRequest(
                        sessionId,
                        target.RootProcessId,
                        config.AppKind,
                        DirectBridgeContract.CodexAppUserModelId,
                        config.MicrophoneEndpointId),
                    sendFrameAsync,
                    cancellationToken).ConfigureAwait(false);
            if (!started.HostReady
                || !started.CaptureActive
                || !_mediaAdapter.CaptureActive
                || _mediaAdapter.Completion.IsCompleted)
            {
                await _mediaAdapter.StopAsync(CancellationToken.None)
                    .ConfigureAwait(false);
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_MEDIA_START_UNCONFIRMED",
                    "媒体适配器没有确认捕获已启动");
            }

            lock (_heartbeatGate)
            {
                _activeConnectionId = connectionId;
                _activeSessionId = sessionId;
                _heartbeatSequence = 0;
                _heartbeatDeadlineMilliseconds = checked(
                    _monotonicMilliseconds()
                    + DirectBridgeContract
                        .ClientHeartbeatTimeoutMilliseconds);
            }
            return started;
        }
        catch
        {
            if (_activeSessionId is null && _mediaAdapter.CaptureActive)
            {
                await _mediaAdapter.StopAsync(CancellationToken.None)
                    .ConfigureAwait(false);
            }
            throw;
        }
        finally
        {
            _stateGate.Release();
        }
    }

    internal async Task StopAsync(
        string connectionId,
        string sessionId,
        CancellationToken cancellationToken)
    {
        await _stateGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            ThrowIfDisposed();
            if (_activeSessionId is null)
            {
                return;
            }
            RequireActiveOwner(connectionId, sessionId);
            await _mediaAdapter.StopAsync(cancellationToken)
                .ConfigureAwait(false);
            ClearActiveSession();
        }
        finally
        {
            _stateGate.Release();
        }
    }

    internal async Task StopForConnectionAsync(string connectionId)
    {
        await _stateGate.WaitAsync().ConfigureAwait(false);
        try
        {
            if (
                _activeConnectionId != connectionId
                || _activeSessionId is null
            )
            {
                return;
            }
            await _mediaAdapter.StopAsync(CancellationToken.None)
                .ConfigureAwait(false);
            ClearActiveSession();
        }
        finally
        {
            _stateGate.Release();
        }
    }

    internal async Task RenewHeartbeatAsync(
        string connectionId,
        string sessionId,
        uint sequence,
        CancellationToken cancellationToken)
    {
        await _stateGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            ThrowIfDisposed();
            RequireActiveOwner(connectionId, sessionId);
            lock (_heartbeatGate)
            {
                if (_heartbeatSequence == uint.MaxValue)
                {
                    throw new DirectProtocolException(
                        "BW_COMPUTER_VOICE_DIRECT_HEARTBEAT_SEQUENCE_INVALID",
                        "电脑语音心跳序号已耗尽");
                }
                uint expected = _heartbeatSequence + 1;
                if (sequence != expected)
                {
                    throw new DirectProtocolException(
                        "BW_COMPUTER_VOICE_DIRECT_HEARTBEAT_SEQUENCE_INVALID",
                        "电脑语音心跳序号无效");
                }
                _heartbeatSequence = sequence;
                _heartbeatDeadlineMilliseconds = checked(
                    _monotonicMilliseconds()
                    + DirectBridgeContract
                        .ClientHeartbeatTimeoutMilliseconds);
            }
        }
        finally
        {
            _stateGate.Release();
        }
    }

    internal int? GetHeartbeatRemainingMilliseconds(
        string connectionId)
    {
        lock (_heartbeatGate)
        {
            if (
                _activeConnectionId != connectionId
                || _activeSessionId is null
                || _heartbeatDeadlineMilliseconds is not long deadline
            )
            {
                return null;
            }
            long remaining = deadline - _monotonicMilliseconds();
            if (remaining <= 0)
            {
                return 0;
            }
            return (int)Math.Min(remaining, int.MaxValue);
        }
    }

    internal bool IsHeartbeatExpired(string connectionId) =>
        GetHeartbeatRemainingMilliseconds(connectionId) == 0;

    private void ClearActiveSession()
    {
        lock (_heartbeatGate)
        {
            _activeConnectionId = null;
            _activeSessionId = null;
            _heartbeatDeadlineMilliseconds = null;
            _heartbeatSequence = 0;
        }
    }

    private void RequireActiveOwner(
        string connectionId,
        string sessionId)
    {
        if (
            _activeConnectionId != connectionId
            || _activeSessionId != sessionId
        )
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_SESSION_MISMATCH",
                "电脑语音会话不匹配");
        }
    }

    private void ThrowIfDisposed()
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
    }

    public async ValueTask DisposeAsync()
    {
        if (_disposed)
        {
            return;
        }
        _disposed = true;
        await _stateGate.WaitAsync().ConfigureAwait(false);
        try
        {
            await _mediaAdapter.StopAsync(CancellationToken.None)
                .ConfigureAwait(false);
            ClearActiveSession();
        }
        finally
        {
            _stateGate.Release();
            _stateGate.Dispose();
        }
        await _mediaAdapter.DisposeAsync().ConfigureAwait(false);
    }
}
