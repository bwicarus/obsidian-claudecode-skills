namespace BwReader.ComputerVoiceAudio;

internal sealed record DirectAppTarget(
    uint RootProcessId,
    long RootProcessStartFileTimeUtc,
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

    Task<DirectAppTarget> RestartAsync(
        string appKind,
        string appUserModelId,
        DirectAppTarget expected,
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

    public Task<DirectAppTarget> RestartAsync(
        string appKind,
        string appUserModelId,
        DirectAppTarget expected,
        TimeSpan timeout,
        CancellationToken cancellationToken) =>
        Task.FromException<DirectAppTarget>(
            new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_APP_LAUNCHER_NOT_WIRED",
                "Windows 直连的 Codex 重启适配器尚未接线"));
}

internal sealed record DirectMediaStartRequest(
    string SessionId,
    uint RootProcessId,
    long RootProcessStartFileTimeUtc,
    string AppKind,
    string AppUserModelId,
    string VirtualMicrophoneRenderEndpointId,
    string VirtualMicrophoneCaptureEndpointId,
    string VirtualSpeakerRenderEndpointId,
    bool StartTypist = true,
    bool AutomatePerAppAudioRoute = false,
    string VirtualSpeakerCaptureEndpointId = "",
    bool FixedVirtualAudioBus = false);

internal sealed record DirectMediaStartResult(
    bool HostReady,
    bool CaptureActive);

internal interface IDirectMediaAdapter : IAsyncDisposable
{
    bool IsWired { get; }

    bool CaptureActive { get; }

    bool CleanupPending { get; }

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

    public bool CleanupPending => false;

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
    private readonly IDirectSnapshotContextAdapter _snapshotContextAdapter;
    private readonly Func<long> _monotonicMilliseconds;
    private readonly Func<DirectBridgeConfig, DirectProtocolException?>
        _renderEndpointProbe;
    private readonly SemaphoreSlim _stateGate = new(1, 1);
    private readonly SemaphoreSlim _disposeGate = new(1, 1);
    private readonly object _heartbeatGate = new();
    private readonly object _runtimeErrorGate = new();
    private string? _activeConnectionId;
    private string? _activeSessionId;
    private string? _activeAppKind;
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
        IDirectContextAdapter? contextAdapter = null,
        IDirectSnapshotContextAdapter? snapshotContextAdapter = null)
    {
        _configStore = configStore;
        _appLauncher = appLauncher;
        _mediaAdapter = mediaAdapter;
        _contextAdapter =
            contextAdapter ?? new UnwiredDirectContextAdapter();
        _snapshotContextAdapter =
            snapshotContextAdapter
            ?? new UnwiredDirectSnapshotContextAdapter();
        _monotonicMilliseconds =
            monotonicMilliseconds ?? (() => Environment.TickCount64);
        _renderEndpointProbe =
            renderEndpointProbe ?? ProbeConfiguredRenderEndpoints;
    }

    internal bool CaptureActive => _mediaAdapter.CaptureActive;

    internal bool CleanupPending => _mediaAdapter.CleanupPending;

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
        AppendFailureRecord(failure);
        return failure;
    }

    // Keeps a failure readable after the fact.
    //
    // LastError lives only in memory and the status file is rewritten on every
    // update, so a supervisor restart erases the evidence -- three times in one
    // evening the only surviving record of a failure was a photograph of the
    // iPad screen. This appends instead of replacing, and sits beside the status
    // file rather than inside it, so nothing that rewrites state can take it.
    //
    // Best-effort by design: diagnostics must never be able to break the thing
    // they are diagnosing, so every failure here is swallowed.
    private void AppendFailureRecord(DirectRuntimeError failure)
    {
        try
        {
            DirectBridgeConfig config = _configStore.Load();
            string? directory = System.IO.Path.GetDirectoryName(
                config.RuntimeStatusPath);
            if (string.IsNullOrEmpty(directory))
            {
                return;
            }
            string path = System.IO.Path.Combine(
                directory,
                "computer-voice-direct.failures.jsonl");
            string line = System.Text.Json.JsonSerializer.Serialize(new
            {
                atUtc = failure.AtUtc.ToString("O"),
                failureId = failure.FailureId,
                code = failure.Code,
                stage = failure.Stage,
                hresult = failure.Hresult,
            });
            System.IO.File.AppendAllText(
                path,
                line + Environment.NewLine,
                new System.Text.UTF8Encoding(false));
            // Trimmed only when it has grown past what anyone would read, and
            // trimmed from the front: the newest failure is the one being
            // investigated.
            var info = new System.IO.FileInfo(path);
            if (info.Length > 256 * 1024)
            {
                string[] all = System.IO.File.ReadAllLines(path);
                System.IO.File.WriteAllLines(
                    path,
                    all.Skip(Math.Max(0, all.Length - 200)));
            }
        }
        catch
        {
        }
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
            if (config.PerAppAudioRouteAutomationEnabled)
            {
                VirtualCaptureEndpointProbe.ValidateExactActiveCapture(
                    config.VirtualMicrophoneCaptureEndpointId);
            }
            if (config.FixedVirtualAudioBusEnabled)
            {
                VirtualCaptureEndpointProbe.ValidateExactActiveCapture(
                    config.VirtualSpeakerCaptureEndpointId);
            }
            return null;
        }
        catch (Exception exception)
        {
            return new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_RENDER_ENDPOINT_UNAVAILABLE",
                "虚拟音频端点未就绪",
                retryable: true,
                innerException: exception);
        }
    }

    internal Task<DirectMediaStartResult> StartAsync(
        string connectionId,
        string sessionId,
        Func<string, string, Task> reportStatusAsync,
        Func<DirectPcmFrame, CancellationToken, Task> sendFrameAsync,
        CancellationToken cancellationToken) =>
        StartAsync(
            connectionId,
            sessionId,
            DirectAppTargets.CodexDesktop,
            DirectContextDeliveryMode.LegacyInject,
            takeover: false,
            reportStatusAsync,
            sendFrameAsync,
            cancellationToken);

    internal async Task<DirectMediaStartResult> StartAsync(
        string connectionId,
        string sessionId,
        string contextDeliveryMode,
        Func<string, string, Task> reportStatusAsync,
        Func<DirectPcmFrame, CancellationToken, Task> sendFrameAsync,
        CancellationToken cancellationToken)
        => await StartAsync(
            connectionId,
            sessionId,
            DirectAppTargets.CodexDesktop,
            contextDeliveryMode,
            takeover: false,
            reportStatusAsync,
            sendFrameAsync,
            cancellationToken).ConfigureAwait(false);

    internal async Task<DirectMediaStartResult> StartAsync(
        string connectionId,
        string sessionId,
        string appKind,
        string contextDeliveryMode,
        Func<string, string, Task> reportStatusAsync,
        Func<DirectPcmFrame, CancellationToken, Task> sendFrameAsync,
        CancellationToken cancellationToken)
        => await StartAsync(
            connectionId,
            sessionId,
            appKind,
            contextDeliveryMode,
            takeover: false,
            reportStatusAsync,
            sendFrameAsync,
            cancellationToken).ConfigureAwait(false);

    internal async Task<DirectMediaStartResult> StartAsync(
        string connectionId,
        string sessionId,
        string appKind,
        string contextDeliveryMode,
        bool takeover,
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
                    && _activeAppKind == appKind
                    && _mediaAdapter.CaptureActive
                )
                {
                    ClearLastError();
                    return new DirectMediaStartResult(
                        HostReady: true,
                        CaptureActive: true);
                }
                bool healthyOwner =
                    _mediaAdapter.CaptureActive
                    && !ActiveHeartbeatExpired();
                if (healthyOwner && !takeover)
                {
                    throw new DirectProtocolException(
                        "BW_COMPUTER_VOICE_DIRECT_BUSY",
                        "另一个电脑语音会话正在使用桥接器",
                        retryable: true);
                }
                else
                {
                    // A healthy owner is preserved unless the authenticated
                    // START explicitly asks to take over.  A heartbeat-expired
                    // or capture-inactive owner is already stale, so a normal
                    // START may recover it without inventing a second
                    // liveness clock. CleanupPending is intentionally not a
                    // health signal: live media owns cleanup resources too.
                    await _mediaAdapter.StopAsync(CancellationToken.None)
                        .ConfigureAwait(false);
                    Task<DirectProtocolException?> completion =
                        _mediaAdapter.Completion;
                    if (!completion.IsCompleted)
                    {
                        throw new DirectProtocolException(
                            "BW_COMPUTER_VOICE_DIRECT_MEDIA_STOP_UNCONFIRMED",
                            "旧电脑语音会话没有确认停止完成",
                            retryable: true);
                    }
                    DirectProtocolException? stopFailure =
                        await completion.ConfigureAwait(false);
                    if (_mediaAdapter.CleanupPending)
                    {
                        throw stopFailure
                            ?? new DirectProtocolException(
                                "BW_COMPUTER_VOICE_DIRECT_MEDIA_CLEANUP_PENDING",
                                "旧 Windows 音频清理仍持有未释放资源",
                                retryable: true);
                    }
                    ClearActiveSession();
                    if (stopFailure is not null)
                    {
                        throw stopFailure;
                    }
                }
            }

            DirectBridgeConfig config = _configStore.Load();
            DirectAppTargetProfile appProfile =
                DirectAppTargets.Require(appKind);
            if (
                !DirectContextDeliveryMode.IsSupported(
                    contextDeliveryMode)
                || config.ContextDeliveryMode != contextDeliveryMode
            )
            {
                throw new DirectProtocolException(
                    "BW_READER_CONTEXT_DELIVERY_MODE_CHANGED",
                    "上下文交付模式已变化，请重新连接",
                    retryable: true);
            }
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
                appKind,
                appProfile.AppUserModelId,
                cancellationToken).ConfigureAwait(false);

            await reportStatusAsync(
                "waiting-app-ready",
                "BW_COMPUTER_VOICE_DIRECT_WAITING_APP_READY")
                .ConfigureAwait(false);
            DirectAppTarget target;
            try
            {
                target = await _appLauncher.WaitForUniqueReadyAsync(
                    appKind,
                    appProfile.AppUserModelId,
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
                || target.RootProcessStartFileTimeUtc <= 0
                || target.AppKind != appKind
                || target.AppUserModelId
                    != appProfile.AppUserModelId
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
                        target.RootProcessStartFileTimeUtc,
                        appKind,
                        appProfile.AppUserModelId,
                        config.VirtualMicrophoneRenderEndpointId,
                        config.VirtualMicrophoneCaptureEndpointId,
                        config.VirtualSpeakerRenderEndpointId,
                        StartTypist:
                            contextDeliveryMode
                                == DirectContextDeliveryMode.LegacyInject,
                        AutomatePerAppAudioRoute:
                            config.PerAppAudioRouteAutomationEnabled,
                        VirtualSpeakerCaptureEndpointId:
                            config.VirtualSpeakerCaptureEndpointId,
                        FixedVirtualAudioBus:
                            config.FixedVirtualAudioBusEnabled),
                    sendFrameAsync,
                    cancellationToken).ConfigureAwait(false);
            if (!started.HostReady
                || !started.CaptureActive
                || !_mediaAdapter.CaptureActive
                || _mediaAdapter.Completion.IsCompleted)
            {
                // 这四项过去共用一个 code 且 runtime status 不落 message,失败时
                // 无法区分是宿主没就绪、捕获没激活,还是适配器已提前收摊。code 保持
                // 不变(self-test 断言依赖它),改用 stage 精确指出是哪一项——stage
                // 只允许小写字母/连字符/点,经 AudioCaptureStageException 进异常链。
                string failedStage =
                    !started.HostReady
                        ? "media-start.host-not-ready"
                        : !started.CaptureActive
                            ? "media-start.started-capture-inactive"
                            : !_mediaAdapter.CaptureActive
                                ? "media-start.adapter-capture-inactive"
                                : "media-start.adapter-completed";
                // Completion 的结果类型就是 DirectProtocolException?,终端媒体失败
                // 会经 TrySetResult 塞在里面。过去只看 IsCompleted、把 Result 丢掉,
                // 于是真正的错误码被一个空壳 MEDIA_START_UNCONFIRMED 盖住。真实异常
                // 存在时直接抛它,让根因浮到 runtime status 上。
                DirectProtocolException? terminal = null;
                if (_mediaAdapter.Completion.IsCompleted)
                {
                    try
                    {
                        terminal = _mediaAdapter.Completion.Result;
                    }
                    catch (Exception completionFailure)
                    {
                        terminal = new DirectProtocolException(
                            "BW_COMPUTER_VOICE_DIRECT_MEDIA_TERMINAL_FAULT",
                            "媒体适配器以异常结束",
                            innerException: completionFailure);
                    }
                }
                await _mediaAdapter.StopAsync(CancellationToken.None)
                    .ConfigureAwait(false);
                if (terminal is not null)
                {
                    throw terminal;
                }
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_MEDIA_START_UNCONFIRMED",
                    "媒体适配器没有确认捕获已启动",
                    innerException: new AudioCaptureStageException(
                        failedStage,
                        0));
            }

            lock (_heartbeatGate)
            {
                _activeConnectionId = connectionId;
                _activeSessionId = sessionId;
                _activeAppKind = appKind;
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
            // BUSY belongs to the competing connection, not the healthy
            // owner. Do not let an unowned STATUS/START attempt poison the
            // global runtime error surfaced to the active call.
            if (
                exception is not DirectProtocolException
                {
                    Code: "BW_COMPUTER_VOICE_DIRECT_BUSY"
                }
            )
            {
                _ = RecordFailure(exception, "start");
            }
            if (
                _activeSessionId is null
                && (
                    _mediaAdapter.CaptureActive
                    || _mediaAdapter.CleanupPending
                )
            )
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
            if (_mediaAdapter.CleanupPending)
            {
                throw failure
                    ?? new DirectProtocolException(
                        "BW_COMPUTER_VOICE_DIRECT_MEDIA_CLEANUP_PENDING",
                        "Windows 音频清理仍持有未释放资源",
                        retryable: true);
            }
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

    internal async Task<DirectContextForwardResult>
        ForwardLegacyContextAsync(
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

    internal async Task<DirectSnapshotForwardResult>
        ForwardSnapshotContextAsync(
            string connectionId,
            string requestId,
            string sessionId,
            DirectContextEvent contextEvent,
            bool requireActiveOwner,
            CancellationToken cancellationToken)
    {
        _ = await ValidateSnapshotOwnerAsync(
            connectionId,
            sessionId,
            requireActiveOwner,
            cancellationToken).ConfigureAwait(false);
        return await _snapshotContextAdapter.ForwardJournalAsync(
            requestId,
            sessionId,
            contextEvent,
            cancellationToken).ConfigureAwait(false);
    }

    internal async Task<DirectSnapshotForwardResult>
        ForwardActiveReadingAsync(
            string connectionId,
            string requestId,
            string sessionId,
            DirectActiveReading activeReading,
            bool requireActiveOwner,
            CancellationToken cancellationToken)
    {
        _ = await ValidateSnapshotOwnerAsync(
            connectionId,
            sessionId,
            requireActiveOwner,
            cancellationToken).ConfigureAwait(false);
        return await _snapshotContextAdapter.ForwardActiveReadingAsync(
            requestId,
            sessionId,
            activeReading,
            cancellationToken).ConfigureAwait(false);
    }

    internal async Task<DirectSnapshotForwardResult>
        ClearSnapshotContextAsync(
            string connectionId,
            string requestId,
            string sessionId,
            bool requireActiveOwner,
            CancellationToken cancellationToken)
    {
        _ = await ValidateSnapshotOwnerAsync(
            connectionId,
            sessionId,
            requireActiveOwner,
            cancellationToken).ConfigureAwait(false);
        return await _snapshotContextAdapter.ClearAsync(
            requestId,
            sessionId,
            cancellationToken).ConfigureAwait(false);
    }

    private async Task<string?> ValidateSnapshotOwnerAsync(
        string connectionId,
        string sessionId,
        bool requireActiveOwner,
        CancellationToken cancellationToken)
    {
        await _stateGate.WaitAsync(cancellationToken)
            .ConfigureAwait(false);
        try
        {
            ThrowIfDisposed();
            if (requireActiveOwner)
            {
                RequireActiveOwner(connectionId, sessionId);
                if (!_mediaAdapter.CaptureActive)
                {
                    throw new DirectProtocolException(
                        "BW_READER_CONTEXT_SNAPSHOT_NOT_ACTIVE",
                        "活动通话的本地快照租约已失效");
                }
                return _activeAppKind;
            }
            // Context-only writers are independent from the single audio
            // owner.  Their writes are serialized by the snapshot adapter;
            // the last completed write becomes the current snapshot.
            return null;
        }
        finally
        {
            _stateGate.Release();
        }
    }

    internal async Task<bool> StopForConnectionAsync(string connectionId)
    {
        await _stateGate.WaitAsync().ConfigureAwait(false);
        try
        {
            if (
                _activeConnectionId != connectionId
                || _activeSessionId is null
            )
            {
                return false;
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
                // retain the session lease whenever cleanup still owns
                // resources so a later retry can finish the same generation.
            }
            if (!_mediaAdapter.CleanupPending)
            {
                ClearActiveSession();
            }
            return true;
        }
        finally
        {
            _stateGate.Release();
        }
    }

    internal async Task<bool> FailAndStopForConnectionAsync(
        string connectionId,
        Exception failure,
        string stage)
    {
        await _stateGate.WaitAsync().ConfigureAwait(false);
        try
        {
            if (
                _activeConnectionId != connectionId
                || _activeSessionId is null
            )
            {
                return false;
            }
            // Record the failure while the same state gate still proves this
            // connection owns media.  Otherwise a retired transport could
            // wake on the old Completion task after a replacement START and
            // overwrite the new owner's clean runtime state.
            _ = RecordFailure(failure, stage);
            try
            {
                await _mediaAdapter.StopAsync(CancellationToken.None)
                    .ConfigureAwait(false);
            }
            catch
            {
                // Keep the failure already recorded above. Cleanup ownership
                // remains visible through CleanupPending/Completion.
            }
            if (!_mediaAdapter.CleanupPending)
            {
                ClearActiveSession();
            }
            return true;
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

    private bool ActiveHeartbeatExpired()
    {
        lock (_heartbeatGate)
        {
            return _activeSessionId is not null
                && _heartbeatDeadlineMilliseconds is long deadline
                && deadline <= _monotonicMilliseconds();
        }
    }

    private void ClearActiveSession()
    {
        lock (_heartbeatGate)
        {
            _activeConnectionId = null;
            _activeSessionId = null;
            _activeAppKind = null;
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
                        stopSettled = !_mediaAdapter.CleanupPending;
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
