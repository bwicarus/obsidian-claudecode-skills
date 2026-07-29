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
    string VirtualMicrophoneRenderEndpointId,
    string VirtualSpeakerRenderEndpointId);

internal sealed record DirectMediaStartResult(
    bool HostReady,
    bool CaptureActive);

internal interface IDirectMediaAdapter : IAsyncDisposable
{
    bool IsWired { get; }

    bool CaptureActive { get; }

    bool IsOutputRouteVerified(DirectBridgeConfig config);

    Task<DirectProtocolException?> Completion { get; }

    Task<DirectMediaStartResult> StartAsync(
        DirectMediaStartRequest request,
        Func<DirectPcmFrame, CancellationToken, Task> sendFrameAsync,
        CancellationToken cancellationToken);

    Task PushUplinkFrameAsync(
        DirectPcmFrame frame,
        CancellationToken cancellationToken);

    Task StopAsync(CancellationToken cancellationToken);
}

internal sealed class UnwiredDirectMediaAdapter : IDirectMediaAdapter
{
    public bool IsWired => false;

    public bool CaptureActive => false;

    public bool IsOutputRouteVerified(DirectBridgeConfig config) => false;

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

    public Task PushUplinkFrameAsync(
        DirectPcmFrame frame,
        CancellationToken cancellationToken) =>
        Task.FromException(
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
    private readonly IDirectContextAdapter _contextAdapter;
    private readonly Func<long> _monotonicMilliseconds;
    private readonly Func<DirectBridgeConfig, DirectProtocolException?>
        _renderEndpointProbe;
    private readonly SemaphoreSlim _stateGate = new(1, 1);
    private readonly SemaphoreSlim _disposeGate = new(1, 1);
    private readonly object _heartbeatGate = new();
    private readonly object _runtimeErrorGate = new();
    private string? _activeConnectionId;
    private string? _activeSessionId;
    private long? _heartbeatDeadlineMilliseconds;
    private uint _heartbeatSequence;
    private bool _disposed;
    private bool _disposeCompleted;

    internal DirectBridgeCoordinator(
        DirectBridgeConfigStore configStore,
        IDirectAppLauncher appLauncher,
        IDirectMediaAdapter mediaAdapter,
        Func<long>? monotonicMilliseconds = null,
        Func<DirectBridgeConfig, DirectProtocolException?>?
            renderEndpointProbe = null,
        IDirectContextAdapter? contextAdapter = null)
    {
        _configStore = configStore;
        _appLauncher = appLauncher;
        _mediaAdapter = mediaAdapter;
        _contextAdapter =
            contextAdapter ?? new UnwiredDirectContextAdapter();
        _monotonicMilliseconds =
            monotonicMilliseconds ?? (() => Environment.TickCount64);
        _renderEndpointProbe =
            renderEndpointProbe ?? ProbeConfiguredRenderEndpoints;
    }

    internal bool CaptureActive => _mediaAdapter.CaptureActive;

    internal bool AppLauncherReady => _appLauncher.IsWired;

    internal bool MediaHostReady => _mediaAdapter.IsWired;

    internal bool OutputRouteVerified(DirectBridgeConfig config)
    {
        try
        {
            return _mediaAdapter.IsOutputRouteVerified(config);
        }
        catch
        {
            return false;
        }
    }

    internal bool ConfiguredRenderEndpointsReady(
        DirectBridgeConfig config,
        out string? reason)
    {
        DirectProtocolException? failure = _renderEndpointProbe(config);
        if (failure is null)
        {
            reason = null;
            return true;
        }
        _ = RecordFailure(failure, "render-endpoint-probe");
        reason = failure.Code;
        return false;
    }

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

    internal DirectRuntimeError? LastError
    {
        get
        {
            lock (_runtimeErrorGate)
            {
                return _lastError;
            }
        }
    }

    private DirectRuntimeError? _lastError;

    internal DirectRuntimeError RecordFailure(
        Exception exception,
        string fallbackStage)
    {
        DirectRuntimeError failure = DirectRuntimeError.FromException(
            exception,
            fallbackStage);
        lock (_runtimeErrorGate)
        {
            _lastError = failure;
        }
        return failure;
    }

    private void ClearLastError()
    {
        lock (_runtimeErrorGate)
        {
            _lastError = null;
        }
    }

    private static DirectProtocolException?
        ProbeConfiguredRenderEndpoints(DirectBridgeConfig config)
    {
        try
        {
            VirtualRenderEndpointProbe.ValidateExactActiveRender(
                config.VirtualMicrophoneRenderEndpointId,
                "virtual-microphone");
            VirtualRenderEndpointProbe.ValidateExactActiveRender(
                config.VirtualSpeakerRenderEndpointId,
                "virtual-speaker");
            return null;
        }
        catch (Exception exception)
        {
            return new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_RENDER_ENDPOINT_UNAVAILABLE",
                "虚拟音频播放端点未就绪",
                retryable: true,
                innerException: exception);
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
                    ClearLastError();
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
                        config.VirtualMicrophoneRenderEndpointId,
                        config.VirtualSpeakerRenderEndpointId),
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
            ClearLastError();
            return started;
        }
        catch (Exception exception)
        {
            _ = RecordFailure(exception, "start");
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
            // Once ownership is confirmed, teardown belongs to the bridge.
            // A peer abort may cancel its request token, but must not cancel
            // capture/typist cleanup or erase the only active-session lease
            // before the media adapter has settled Completion.
            await _mediaAdapter.StopAsync(CancellationToken.None)
                .ConfigureAwait(false);
            Task<DirectProtocolException?> completion =
                _mediaAdapter.Completion;
            if (!completion.IsCompleted)
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_MEDIA_STOP_UNCONFIRMED",
                    "媒体适配器没有确认停止完成");
            }
            DirectProtocolException? failure =
                await completion.ConfigureAwait(false);
            ClearActiveSession();
            if (failure is not null)
            {
                throw failure;
            }
        }
        finally
        {
            _stateGate.Release();
        }
    }

    internal async Task PushUplinkFrameAsync(
        string connectionId,
        string sessionId,
        DirectPcmFrame frame,
        CancellationToken cancellationToken)
    {
        await _stateGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            ThrowIfDisposed();
            RequireActiveOwner(connectionId, sessionId);
            if (
                !_mediaAdapter.CaptureActive
                || frame.Track != DirectPcmTrack.BrowserMicrophone
            )
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_UPLINK_NOT_ACTIVE",
                    "浏览器麦克风上行尚未启动");
            }
            await _mediaAdapter.PushUplinkFrameAsync(
                frame,
                cancellationToken).ConfigureAwait(false);
        }
        finally
        {
            _stateGate.Release();
        }
    }

    internal async Task<DirectContextForwardResult> ForwardContextAsync(
        string connectionId,
        string requestId,
        string sessionId,
        string contextContract,
        DirectContextEvent contextEvent,
        CancellationToken cancellationToken)
    {
        await _stateGate.WaitAsync(cancellationToken)
            .ConfigureAwait(false);
        try
        {
            ThrowIfDisposed();
            RequireActiveOwner(connectionId, sessionId);
            if (!_mediaAdapter.CaptureActive)
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_CONTEXT_NOT_ACTIVE",
                    "Reader context 只允许发送到当前活动通话");
            }
        }
        finally
        {
            _stateGate.Release();
        }

        // IPC is deliberately outside the media state lock. A 3 second typist
        // timeout must not block microphone uplink, heartbeats, or STOP.
        return await _contextAdapter.ForwardAsync(
            requestId,
            sessionId,
            contextContract,
            contextEvent,
            cancellationToken).ConfigureAwait(false);
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
            try
            {
                await _mediaAdapter.StopAsync(CancellationToken.None)
                    .ConfigureAwait(false);
            }
            catch
            {
                // There is no peer left to receive a teardown error.  The
                // media adapter preserves its terminal failure in Completion;
                // connection cleanup must still release the session lease.
            }
            finally
            {
                ClearActiveSession();
            }
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
        await _disposeGate.WaitAsync().ConfigureAwait(false);
        try
        {
            if (_disposeCompleted)
            {
                return;
            }
            // Enter the terminal service state immediately so failed teardown
            // attempts cannot reopen normal operations.  Dispose itself stays
            // retryable until the owned media adapter confirms full release.
            _disposed = true;
            bool stopSettled = false;
            await _stateGate.WaitAsync().ConfigureAwait(false);
            try
            {
                try
                {
                    await _mediaAdapter.StopAsync(CancellationToken.None)
                        .ConfigureAwait(false);
                    Task<DirectProtocolException?> completion =
                        _mediaAdapter.Completion;
                    if (completion.IsCompleted)
                    {
                        _ = await completion.ConfigureAwait(false);
                        stopSettled = true;
                    }
                }
                catch
                {
                    // DisposeAsync below is the final owner-aware retry.  Do
                    // not discard the active-session lease unless either Stop
                    // settled or media disposal ultimately succeeds.
                }
                if (stopSettled)
                {
                    ClearActiveSession();
                }
            }
            finally
            {
                _stateGate.Release();
            }

            await _mediaAdapter.DisposeAsync().ConfigureAwait(false);

            if (!stopSettled)
            {
                await _stateGate.WaitAsync().ConfigureAwait(false);
                try
                {
                    ClearActiveSession();
                }
                finally
                {
                    _stateGate.Release();
                }
            }
            _stateGate.Dispose();
            _disposeCompleted = true;
        }
        finally
        {
            _disposeGate.Release();
        }
    }
}
