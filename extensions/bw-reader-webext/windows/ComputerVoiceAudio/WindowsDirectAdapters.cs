using System.Diagnostics;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Text.Json;

namespace BwReader.ComputerVoiceAudio;

internal sealed class WindowsDirectAppLauncher : IDirectAppLauncher
{
    public bool IsWired => true;

    public Task EnsureRunningAsync(
        string appKind,
        string appUserModelId,
        CancellationToken cancellationToken)
    {
        ValidateTarget(appKind, appUserModelId);
        cancellationToken.ThrowIfCancellationRequested();
        if (!OperatingSystem.IsWindows())
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_WINDOWS_REQUIRED",
                "Codex packaged app 只能在 Windows 启动");
        }
        CodexAppProbeState current = WindowsCodexAppProbe.Probe();
        if (current.ReadyTarget is not null || current.RootCount == 1)
        {
            return Task.CompletedTask;
        }
        if (current.RootCount > 1)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_APP_AMBIGUOUS",
                "检测到多个 Codex 进程树，拒绝自动启动");
        }

        IApplicationActivationManager manager =
            (IApplicationActivationManager)(object)
                new ApplicationActivationManager();
        try
        {
            int result = manager.ActivateApplication(
                DirectBridgeContract.CodexAppUserModelId,
                arguments: null,
                ActivateOptions.None,
                out uint processId);
            if (result < 0 || processId == 0)
            {
                Marshal.ThrowExceptionForHR(result);
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_APP_START_FAILED",
                    "Codex packaged app 启动失败",
                    retryable: true);
            }
        }
        catch (COMException exception)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_APP_START_FAILED",
                "Codex packaged app 启动失败",
                retryable: true,
                innerException: exception);
        }
        finally
        {
            if (Marshal.IsComObject(manager))
            {
                Marshal.FinalReleaseComObject(manager);
            }
        }
        cancellationToken.ThrowIfCancellationRequested();
        return Task.CompletedTask;
    }

    public async Task<DirectAppTarget> WaitForUniqueReadyAsync(
        string appKind,
        string appUserModelId,
        TimeSpan timeout,
        CancellationToken cancellationToken)
    {
        ValidateTarget(appKind, appUserModelId);
        if (timeout <= TimeSpan.Zero)
        {
            throw new ArgumentOutOfRangeException(nameof(timeout));
        }
        long deadline = Stopwatch.GetTimestamp()
            + checked((long)(timeout.TotalSeconds
                * Stopwatch.Frequency));
        while (true)
        {
            cancellationToken.ThrowIfCancellationRequested();
            CodexAppProbeState state = WindowsCodexAppProbe.Probe();
            if (state.RootCount > 1)
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_APP_AMBIGUOUS",
                    "检测到多个 Codex 进程树");
            }
            if (state.ReadyTarget is CodexAppTarget target)
            {
                return new DirectAppTarget(
                    target.RootProcessId,
                    appKind,
                    appUserModelId);
            }
            if (Stopwatch.GetTimestamp() >= deadline)
            {
                throw new TimeoutException(
                    "BW_COMPUTER_VOICE_DIRECT_APP_READY_TIMEOUT");
            }
            await Task.Delay(
                TimeSpan.FromMilliseconds(200),
                cancellationToken).ConfigureAwait(false);
        }
    }

    internal static void ValidateTarget(
        string appKind,
        string appUserModelId)
    {
        if (
            appKind != "codex-desktop"
            || appUserModelId
                != DirectBridgeContract.CodexAppUserModelId
        )
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_APP_TARGET_INVALID",
                "应用目标不在本机固定白名单");
        }
    }

    [Flags]
    private enum ActivateOptions
    {
        None = 0,
    }

    [ComImport]
    [Guid("45BA127D-10A8-46EA-8AB7-56EA9078943C")]
    private sealed class ApplicationActivationManager
    {
    }

    [ComImport]
    [Guid("2e941141-7f97-4756-ba1d-9decde894a3d")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IApplicationActivationManager
    {
        [PreserveSig]
        int ActivateApplication(
            [MarshalAs(UnmanagedType.LPWStr)] string appUserModelId,
            [MarshalAs(UnmanagedType.LPWStr)] string? arguments,
            ActivateOptions options,
            out uint processId);

        [PreserveSig]
        int ActivateForFile(
            nint appUserModelId,
            nint itemArray,
            nint verb,
            out uint processId);

        [PreserveSig]
        int ActivateForProtocol(
            nint appUserModelId,
            nint itemArray,
            out uint processId);
    }
}

internal sealed record DirectTypistLease(
    int ProcessId,
    long ProcessStartFileTimeUtc);

internal sealed record DirectTypistHelperResult(
    int ExitCode,
    string StandardOutput,
    string StandardError);

internal sealed class WindowsDirectTypistLeaseController
{
    private readonly string _typistHelper;
    private readonly Func<(int ProcessId, long StartFileTimeUtc)>
        _ownerGenerationProvider;
    private readonly Func<
        IReadOnlyList<string>,
        CancellationToken,
        Task<DirectTypistHelperResult>> _invokeHelperAsync;

    internal WindowsDirectTypistLeaseController(string installationRoot)
    {
        _typistHelper = System.IO.Path.Combine(
            installationRoot,
            "bw_computer_voice_typist_helper.py");
        _invokeHelperAsync = InvokeHelperAsync;
        _ownerGenerationProvider = CurrentProcessGeneration;
    }

    internal WindowsDirectTypistLeaseController(
        Func<
            IReadOnlyList<string>,
            CancellationToken,
            Task<DirectTypistHelperResult>> invokeHelperAsync,
        Func<(int ProcessId, long StartFileTimeUtc)>?
            ownerGenerationProvider = null)
    {
        _typistHelper = "";
        _invokeHelperAsync = invokeHelperAsync;
        _ownerGenerationProvider =
            ownerGenerationProvider ?? CurrentProcessGeneration;
    }

    internal async Task<DirectTypistLease?> EnsureRunningAsync(
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        // Once the fixed launcher has been invoked, let it finish and return
        // the exact PID result even if the browser disconnects.  The caller
        // can then release an owned lease instead of losing ownership during
        // a cancellation race.
        (
            int ownerProcessId,
            long ownerStartFileTimeUtc
        ) = _ownerGenerationProvider();
        if (ownerProcessId <= 0 || ownerStartFileTimeUtc <= 0)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_TYPIST_OWNER_INVALID",
                "bridge owner 进程代次无效");
        }
        DirectTypistHelperResult completed =
            await _invokeHelperAsync(
                new[]
                {
                    "--ensure-running",
                    ownerProcessId.ToString(
                        CultureInfo.InvariantCulture),
                    ownerStartFileTimeUtc.ToString(
                        CultureInfo.InvariantCulture),
                },
                CancellationToken.None).ConfigureAwait(false);
        if (completed.ExitCode != 0)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_TYPIST_START_FAILED",
                "voice-typist 启动失败");
        }
        return ParseEnsureResult(completed.StandardOutput);
    }

    internal async Task ReleaseAsync(
        DirectTypistLease lease,
        CancellationToken cancellationToken)
    {
        if (
            lease.ProcessId <= 0
            || lease.ProcessStartFileTimeUtc <= 0
        )
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_TYPIST_LEASE_INVALID",
                "voice-typist lease 无效");
        }
        DirectTypistHelperResult completed =
            await _invokeHelperAsync(
                new[]
                {
                    "--stop-if-owned",
                    lease.ProcessId.ToString(
                        CultureInfo.InvariantCulture),
                    lease.ProcessStartFileTimeUtc.ToString(
                        CultureInfo.InvariantCulture),
                },
                cancellationToken).ConfigureAwait(false);
        if (completed.ExitCode != 0)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_TYPIST_STOP_FAILED",
                "voice-typist owned lease 释放失败");
        }
        RequireReleaseResult(completed.StandardOutput);
    }

    internal static DirectTypistLease? ParseEnsureResult(
        string output)
    {
        try
        {
            using JsonDocument result = JsonDocument.Parse(output);
            JsonElement root = result.RootElement;
            if (
                !root.TryGetProperty("ok", out JsonElement ok)
                || ok.ValueKind != JsonValueKind.True
                || !root.TryGetProperty(
                    "running",
                    out JsonElement running)
                || running.ValueKind != JsonValueKind.True
                || !root.TryGetProperty("pid", out JsonElement pid)
                || !pid.TryGetInt32(out int processId)
                || processId <= 0
                || !root.TryGetProperty(
                    "processStartFileTimeUtc",
                    out JsonElement processStart)
                || !processStart.TryGetInt64(
                    out long processStartFileTimeUtc)
                || processStartFileTimeUtc <= 0
                || !root.TryGetProperty(
                    "result",
                    out JsonElement outcome)
                || outcome.ValueKind != JsonValueKind.String
            )
            {
                throw InvalidStartResult();
            }
            return outcome.GetString() switch
            {
                "started" => new DirectTypistLease(
                    processId,
                    processStartFileTimeUtc),
                "already-running" or "raced-running" => null,
                _ => throw InvalidStartResult(),
            };
        }
        catch (JsonException exception)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_TYPIST_START_FAILED",
                "voice-typist 返回无效状态",
                retryable: false,
                innerException: exception);
        }
    }

    private static void RequireReleaseResult(string output)
    {
        try
        {
            using JsonDocument result = JsonDocument.Parse(output);
            JsonElement root = result.RootElement;
            if (
                !root.TryGetProperty("ok", out JsonElement ok)
                || ok.ValueKind != JsonValueKind.True
                || !root.TryGetProperty(
                    "running",
                    out JsonElement running)
                || running.ValueKind != JsonValueKind.False
                || !root.TryGetProperty(
                    "result",
                    out JsonElement outcome)
                || outcome.ValueKind != JsonValueKind.String
                || outcome.GetString()
                    is not ("stopped" or "already-stopped")
            )
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_TYPIST_STOP_FAILED",
                    "voice-typist Stop 后置条件无效");
            }
        }
        catch (JsonException exception)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_TYPIST_STOP_FAILED",
                "voice-typist Stop 返回无效状态",
                retryable: false,
                innerException: exception);
        }
    }

    private async Task<DirectTypistHelperResult> InvokeHelperAsync(
        IReadOnlyList<string> arguments,
        CancellationToken cancellationToken)
    {
        string python = PythonExecutable();
        if (!File.Exists(python) || !File.Exists(_typistHelper))
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_TYPIST_UNAVAILABLE",
                "voice-typist helper 不可用");
        }
        ProcessStartInfo start = new()
        {
            FileName = python,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
        };
        start.ArgumentList.Add(_typistHelper);
        foreach (string argument in arguments)
        {
            start.ArgumentList.Add(argument);
        }
        cancellationToken.ThrowIfCancellationRequested();
        using Process process = Process.Start(start)
            ?? throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_TYPIST_HELPER_FAILED",
                "voice-typist helper 启动失败");
        string output = await process.StandardOutput.ReadToEndAsync(
            cancellationToken).ConfigureAwait(false);
        string error = await process.StandardError.ReadToEndAsync(
            cancellationToken).ConfigureAwait(false);
        await process.WaitForExitAsync(cancellationToken)
            .ConfigureAwait(false);
        return new DirectTypistHelperResult(
            process.ExitCode,
            output,
            error);
    }

    private static DirectProtocolException InvalidStartResult() =>
        new(
            "BW_COMPUTER_VOICE_DIRECT_TYPIST_START_FAILED",
            "voice-typist 未确认运行");

    private static (int ProcessId, long StartFileTimeUtc)
        CurrentProcessGeneration()
    {
        using Process process = Process.GetCurrentProcess();
        return (
            process.Id,
            process.StartTime.ToUniversalTime().ToFileTimeUtc()
        );
    }

    private static string PythonExecutable() => System.IO.Path.Combine(
        Environment.GetFolderPath(
            Environment.SpecialFolder.UserProfile),
        "AppData",
        "Local",
        "Programs",
        "Python",
        "Python313",
        "python.exe");
}

internal sealed class WindowsDirectMediaAdapter : IDirectMediaAdapter
{
    private readonly WindowsDirectTypistLeaseController _typist;
    private readonly IDirectOutputRouteObserverFactory
        _outputRouteObserverFactory;
    private readonly SemaphoreSlim _stateGate = new(1, 1);
    private ProcessLoopbackCaptureSession? _outputSession;
    private VirtualMicrophoneRenderSession? _renderSession;
    private IDirectOutputRouteObserver? _outputRouteObserver;
    private CancellationTokenSource? _captureLifetime;
    private Task? _outputPump;
    private Task? _renderMonitor;
    private TaskCompletionSource<DirectProtocolException?>?
        _completionSource;
    private Task<DirectProtocolException?> _completion =
        Task.FromResult<DirectProtocolException?>(null);
    private DirectTypistLease? _ownedTypistLease;
    private DirectProtocolException? _terminalMediaFailure;
    private volatile bool _captureActive;
    private bool _disposed;

    internal WindowsDirectMediaAdapter(string installationRoot)
        : this(
            new WindowsDirectTypistLeaseController(installationRoot),
            new NativeDirectOutputRouteObserverFactory())
    {
    }

    internal WindowsDirectMediaAdapter(
        WindowsDirectTypistLeaseController typist,
        IDirectOutputRouteObserverFactory? outputRouteObserverFactory = null)
    {
        _typist = typist;
        _outputRouteObserverFactory =
            outputRouteObserverFactory
            ?? new NativeDirectOutputRouteObserverFactory();
    }

    public bool IsWired => true;

    public bool CaptureActive => _captureActive;

    public bool IsOutputRouteVerified(DirectBridgeConfig config)
    {
        ArgumentNullException.ThrowIfNull(config);
        IDirectOutputRouteObserver? active =
            Volatile.Read(ref _outputRouteObserver);
        if (
            active is not null
            && string.Equals(
                active.EndpointId,
                config.VirtualSpeakerRenderEndpointId,
                StringComparison.Ordinal)
        )
        {
            return active.Verified;
        }
        // STATUS is read-only and may be polled repeatedly. Do not create a
        // new COM observer thread for every idle refresh. Positive evidence
        // belongs to the observer owned by the current START generation; the
        // explicit CLI probe remains available for one-shot diagnostics.
        return false;
    }

    public Task<DirectProtocolException?> Completion => _completion;

    public async Task<DirectMediaStartResult> StartAsync(
        DirectMediaStartRequest request,
        Func<DirectPcmFrame, CancellationToken, Task> sendFrameAsync,
        CancellationToken cancellationToken)
    {
        DirectTypistLease? pendingTypistLease = null;
        IDirectOutputRouteObserver? pendingOutputRouteObserver = null;
        Exception? startFailure = null;
        await _stateGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            ObjectDisposedException.ThrowIf(_disposed, this);
            if (_captureActive)
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_MEDIA_BUSY",
                    "Windows 音频捕获已在运行",
                    retryable: true);
            }
            if (HasOwnedCleanupResources)
            {
                DirectProtocolException? cleanupFailure =
                    await StopOwnedResourcesUnderGateAsync()
                        .ConfigureAwait(false);
                if (
                    cleanupFailure is not null
                    || HasOwnedCleanupResources
                )
                {
                    throw cleanupFailure
                        ?? new DirectProtocolException(
                            "BW_COMPUTER_VOICE_DIRECT_MEDIA_CLEANUP_PENDING",
                            "上一次 Windows 音频清理尚未完成",
                            retryable: true);
                }
            }
            WindowsDirectAppLauncher.ValidateTarget(
                request.AppKind,
                request.AppUserModelId);
            _ = DirectPcmFrameCodec.ParseSessionId(request.SessionId);
            VirtualMicrophoneRenderRequest virtualMicrophone =
                VirtualMicrophoneRenderRequest.Create(
                    request.VirtualMicrophoneRenderEndpointId);
            if (
                string.Equals(
                    request.VirtualMicrophoneRenderEndpointId,
                    request.VirtualSpeakerRenderEndpointId,
                    StringComparison.Ordinal)
            )
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_RENDER_ENDPOINTS_NOT_DISTINCT",
                    "虚拟麦克风与虚拟扬声器必须使用不同播放端点");
            }
            // This verifies only that the separately selected Codex-output
            // endpoint exists as an active eRender endpoint.  It does not
            // claim or alter the Windows per-app route.
            VirtualRenderEndpointProbe.ValidateExactActiveRender(
                request.VirtualSpeakerRenderEndpointId,
                "virtual-speaker");
            CodexAppTarget target = WindowsCodexAppProbe.RequireReady();
            if (target.RootProcessId != request.RootProcessId)
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_APP_TARGET_CHANGED",
                    "Codex 目标进程已变化");
            }
            // The installed Codex command is an OS-global hotkey. Validate
            // the single-user local binding before typist or either audio
            // session starts, then revalidate again at the shortcut boundary.
            WindowsCodexAppProbe.RequireExpectedGlobalVoiceShortcut();
            BoundedPcmPacketQueue outputQueue = new(
                32,
                2 * 1024 * 1024);
            ProcessLoopbackCaptureSession outputSession =
                ProcessLoopbackCaptureSession.Prepare(
                    request.RootProcessId,
                    outputQueue);
            VirtualMicrophoneRenderSession renderSession =
                VirtualMicrophoneRenderSession.Prepare(
                    virtualMicrophone);
            CancellationTokenSource lifetime =
                CancellationTokenSource.CreateLinkedTokenSource(
                    cancellationToken);
            pendingOutputRouteObserver =
                CreateOutputRouteObserverWithoutBlockingStart(
                    _outputRouteObserverFactory,
                    request.VirtualSpeakerRenderEndpointId,
                    target);
            try
            {
                // The bounded process-loopback queue has no consumer until
                // the atomic shortcut commit below. Start the already-approved
                // typist first so its launcher checks cannot fill that queue
                // with silent engine packets before the pump is owned.
                if (request.StartTypist)
                {
                    await EnsureTypistThenStartPreparedMediaAsync(
                            _typist.EnsureRunningAsync,
                            lease => pendingTypistLease = lease,
                            renderSession.StartAsync,
                            outputSession.StartAsync,
                            lifetime.Token)
                        .ConfigureAwait(false);
                }
                else
                {
                    // Snapshot-MCP mode keeps the already-validated media and
                    // shortcut path, but must never acquire a Voice Typist
                    // lease: proactive client text injection and MCP snapshot
                    // delivery are mutually exclusive.
                    await renderSession.StartAsync(lifetime.Token)
                        .ConfigureAwait(false);
                    await outputSession.StartAsync(lifetime.Token)
                        .ConfigureAwait(false);
                }
                Pcm48kMonoFramer outputFramer = new(
                    outputSession.Format
                    ?? throw new DirectProtocolException(
                        "BW_COMPUTER_VOICE_DIRECT_AUDIO_FORMAT_MISSING",
                        "应用输出音频格式不存在"));
                TaskCompletionSource<DirectProtocolException?> completion =
                    new(TaskCreationOptions.RunContinuationsAsynchronously);
                SendShortcutAtAtomicCommitBoundary(
                    () =>
                    {
                        // RDP attach/detach can replace endpoint visibility
                        // after preparation. Revalidate B at the last safe
                        // boundary and require both owned audio sessions to
                        // still be running before the shortcut side effect.
                        VirtualRenderEndpointProbe.ValidateExactActiveRender(
                            request.VirtualSpeakerRenderEndpointId,
                            "virtual-speaker");
                        RequirePreparedMediaRunning(
                            outputSession.State,
                            outputSession.Completion.IsCompleted,
                            renderSession.State,
                            renderSession.Completion.IsCompleted);
                    },
                    () =>
                    {
                        WindowsCodexAppProbe.SendVoiceShortcutOrThrow(
                            target);
                        return true;
                    },
                    () =>
                    {
                        // There must be no cancellation observation between a
                        // successful shortcut and committing every bridge-owned
                        // resource.  A peer close can then wait on _stateGate
                        // and deterministically tear capture/typist down.
                        _outputSession = outputSession;
                        _renderSession = renderSession;
                        _outputRouteObserver =
                            pendingOutputRouteObserver;
                        pendingOutputRouteObserver = null;
                        _captureLifetime = lifetime;
                        _completionSource = completion;
                        _completion = completion.Task;
                        Interlocked.Exchange(
                            ref _terminalMediaFailure,
                            null);
                        _ownedTypistLease = pendingTypistLease;
                        pendingTypistLease = null;
                        _outputPump = PumpAsync(
                            DirectPcmTrack.AppOutput,
                            outputQueue,
                            outputFramer,
                            sendFrameAsync,
                            completion,
                            lifetime);
                        _renderMonitor = MonitorRenderAsync(
                            renderSession,
                            completion,
                            lifetime);
                        _captureActive = true;
                    },
                    cancellationToken);
                return new DirectMediaStartResult(
                    HostReady: true,
                    CaptureActive: true);
            }
            catch (Exception startException)
            {
                Exception? cleanupFailure =
                    await RunBestEffortCleanupAsync(
                        () =>
                        {
                            lifetime.Cancel();
                            return Task.CompletedTask;
                        },
                        () => StopPreparedAsync(
                            renderSession,
                            outputSession),
                        () =>
                        {
                            pendingOutputRouteObserver?.Dispose();
                            pendingOutputRouteObserver = null;
                            return Task.CompletedTask;
                        },
                        () =>
                        {
                            lifetime.Dispose();
                            return Task.CompletedTask;
                        }).ConfigureAwait(false);
                if (cleanupFailure is not null)
                {
                    throw CombineStartAndCleanupFailures(
                        startException,
                        cleanupFailure);
                }
                throw;
            }
        }
        catch (DirectProtocolException exception)
        {
            startFailure = exception;
            throw;
        }
        catch (OperationCanceledException exception)
        {
            startFailure = exception;
            throw;
        }
        catch (Exception exception)
        {
            AudioCaptureStageException? stageFailure =
                FindAudioStageFailure(exception);
            DirectProtocolException wrapped = new(
                "BW_COMPUTER_VOICE_DIRECT_MEDIA_START_FAILED",
                stageFailure is null
                    ? "Windows 音频捕获启动失败"
                    : "Windows 音频捕获启动失败（"
                        + stageFailure.PublicDetail
                        + "）",
                retryable: false,
                innerException: exception);
            startFailure = wrapped;
            throw wrapped;
        }
        finally
        {
            try
            {
                if (pendingTypistLease is not null)
                {
                    await ReleasePendingTypistAfterStartFailureAsync(
                            pendingTypistLease,
                            startFailure)
                        .ConfigureAwait(false);
                }
            }
            finally
            {
                _stateGate.Release();
            }
        }
    }

    private static AudioCaptureStageException? FindAudioStageFailure(
        Exception exception)
    {
        if (exception is AudioCaptureStageException stage)
        {
            return stage;
        }
        if (exception is AggregateException aggregate)
        {
            foreach (Exception inner in aggregate.Flatten().InnerExceptions)
            {
                AudioCaptureStageException? found =
                    FindAudioStageFailure(inner);
                if (found is not null)
                {
                    return found;
                }
            }
        }
        return exception.InnerException is null
            ? null
            : FindAudioStageFailure(exception.InnerException);
    }

    internal static void SendShortcutAtAtomicCommitBoundary(
        Action validatePreparedMedia,
        Func<bool> sendShortcut,
        Action commitOwnedResources,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        validatePreparedMedia();
        if (!sendShortcut())
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_SHORTCUT_FAILED",
                "Codex 语音快捷键发送失败");
        }

        // Intentionally do not observe cancellation here.  Once SendInput
        // reports success, bridge resource ownership must be committed before
        // peer-close cleanup is allowed to proceed.
        commitOwnedResources();
    }

    internal static async Task EnsureTypistThenStartPreparedMediaAsync(
        Func<CancellationToken, Task<DirectTypistLease?>>
            ensureTypistAsync,
        Action<DirectTypistLease?> rememberTypistLease,
        Func<CancellationToken, Task> startRenderAsync,
        Func<CancellationToken, Task> startOutputAsync,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(ensureTypistAsync);
        ArgumentNullException.ThrowIfNull(rememberTypistLease);
        ArgumentNullException.ThrowIfNull(startRenderAsync);
        ArgumentNullException.ThrowIfNull(startOutputAsync);

        cancellationToken.ThrowIfCancellationRequested();
        DirectTypistLease? lease =
            await ensureTypistAsync(cancellationToken)
                .ConfigureAwait(false);
        // Ownership must be visible before either audio Start can fail so the
        // caller's existing cleanup path can release only this exact lease.
        rememberTypistLease(lease);
        await startRenderAsync(cancellationToken).ConfigureAwait(false);
        await startOutputAsync(cancellationToken).ConfigureAwait(false);
    }

    internal static IDirectOutputRouteObserver
        CreateOutputRouteObserverWithoutBlockingStart(
            IDirectOutputRouteObserverFactory factory,
            string endpointId,
            CodexAppTarget target)
    {
        ArgumentNullException.ThrowIfNull(factory);
        ArgumentNullException.ThrowIfNull(target);
        try
        {
            return factory.Create(endpointId, target);
        }
        catch
        {
            return new UnverifiedDirectOutputRouteObserver(
                endpointId,
                target.RootProcessId);
        }
    }

    internal static void RequirePreparedMediaRunning(
        CaptureSessionState outputState,
        bool outputCompleted,
        CaptureSessionState renderState,
        bool renderCompleted)
    {
        if (
            outputState != CaptureSessionState.Running
            || outputCompleted
            || renderState != CaptureSessionState.Running
            || renderCompleted
        )
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_MEDIA_START_UNCONFIRMED",
                "快捷键发送前音频端点已失效",
                retryable: true);
        }
    }

    public async Task StopAsync(CancellationToken cancellationToken)
    {
        await _stateGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            _ = await StopOwnedResourcesUnderGateAsync()
                .ConfigureAwait(false);
        }
        finally
        {
            _stateGate.Release();
        }
    }

    public async Task PushUplinkFrameAsync(
        DirectPcmFrame frame,
        CancellationToken cancellationToken)
    {
        await _stateGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            ObjectDisposedException.ThrowIf(_disposed, this);
            VirtualMicrophoneRenderSession render =
                _renderSession
                ?? throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_UPLINK_NOT_ACTIVE",
                    "浏览器麦克风上行尚未启动");
            if (!_captureActive)
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_UPLINK_NOT_ACTIVE",
                    "浏览器麦克风上行尚未启动");
            }
            render.Push(frame);
        }
        finally
        {
            _stateGate.Release();
        }
    }

    private bool HasOwnedCleanupResources =>
        _captureLifetime is not null
        || _outputSession is not null
        || _renderSession is not null
        || _outputRouteObserver is not null
        || _outputPump is not null
        || _renderMonitor is not null
        || _ownedTypistLease is not null;

    private async Task<DirectProtocolException?>
        StopOwnedResourcesUnderGateAsync()
    {
        CancellationTokenSource? lifetime = _captureLifetime;
        ProcessLoopbackCaptureSession? output = _outputSession;
        VirtualMicrophoneRenderSession? render = _renderSession;
        IDirectOutputRouteObserver? outputRoute =
            _outputRouteObserver;
        Task? outputPump = _outputPump;
        Task? renderMonitor = _renderMonitor;
        DirectTypistLease? typistLease = _ownedTypistLease;
        bool hadOwnedResources = HasOwnedCleanupResources;
        _captureActive = false;

        if (!hadOwnedResources)
        {
            return Volatile.Read(ref _terminalMediaFailure);
        }

        TaskCompletionSource<DirectProtocolException?> completion =
            new(TaskCreationOptions.RunContinuationsAsynchronously);
        _completionSource = completion;
        _completion = completion.Task;

        Exception? cleanupFailure = await RunBestEffortCleanupAsync(
            () =>
            {
                lifetime?.Cancel();
                return Task.CompletedTask;
            },
            () => render is null
                ? Task.CompletedTask
                : render.StopAsync(CancellationToken.None),
            () => output is null
                ? Task.CompletedTask
                : output.StopAsync(CancellationToken.None),
            async () =>
            {
                if (render is null)
                {
                    return;
                }
                try
                {
                    await render.DisposeAsync().ConfigureAwait(false);
                }
                finally
                {
                    if (
                        render.State == CaptureSessionState.Disposed
                        && ReferenceEquals(
                            _renderSession,
                            render)
                    )
                    {
                        _renderSession = null;
                    }
                }
            },
            () =>
            {
                if (outputRoute is not null)
                {
                    outputRoute.Dispose();
                    if (ReferenceEquals(
                        _outputRouteObserver,
                        outputRoute))
                    {
                        _outputRouteObserver = null;
                    }
                }
                return Task.CompletedTask;
            },
            async () =>
            {
                if (output is null)
                {
                    return;
                }
                try
                {
                    await output.DisposeAsync().ConfigureAwait(false);
                }
                finally
                {
                    if (
                        output.State == CaptureSessionState.Disposed
                        && ReferenceEquals(_outputSession, output)
                    )
                    {
                        _outputSession = null;
                    }
                }
            },
            async () =>
            {
                if (outputPump is null)
                {
                    return;
                }
                try
                {
                    await outputPump.ConfigureAwait(false);
                }
                finally
                {
                    if (
                        outputPump.IsCompleted
                        && ReferenceEquals(_outputPump, outputPump)
                    )
                    {
                        _outputPump = null;
                    }
                }
            },
            async () =>
            {
                if (renderMonitor is null)
                {
                    return;
                }
                try
                {
                    await renderMonitor.ConfigureAwait(false);
                }
                finally
                {
                    if (ReferenceEquals(
                        _renderMonitor,
                        renderMonitor)
                        && renderMonitor.IsCompleted)
                    {
                        _renderMonitor = null;
                    }
                }
            },
            () =>
            {
                if (lifetime is not null)
                {
                    lifetime.Dispose();
                    if (ReferenceEquals(_captureLifetime, lifetime))
                    {
                        _captureLifetime = null;
                    }
                }
                return Task.CompletedTask;
            },
            async () =>
            {
                if (typistLease is null)
                {
                    return;
                }
                try
                {
                    await ReleaseOwnershipAfterSuccessAsync(
                        () => ReleaseTypistLeaseAsync(typistLease),
                        () =>
                        {
                            if (ReferenceEquals(
                                _ownedTypistLease,
                                typistLease))
                            {
                                _ownedTypistLease = null;
                            }
                        }).ConfigureAwait(false);
                }
                catch (Exception exception)
                {
                    throw TypistReleaseFailure(exception);
                }
            }).ConfigureAwait(false);

        // This settles bridge-owned capture, pumps and typist only.  There is
        // no verified application-side stop primitive, so STOP deliberately
        // does not guess that Ctrl+Shift+C is an ownership-safe toggle.
        DirectProtocolException? stopFailure = CombineStopFailures(
            Volatile.Read(ref _terminalMediaFailure),
            cleanupFailure);
        completion.TrySetResult(stopFailure);
        return stopFailure;
    }

    private async Task PumpAsync(
        DirectPcmTrack track,
        BoundedPcmPacketQueue queue,
        Pcm48kMonoFramer framer,
        Func<DirectPcmFrame, CancellationToken, Task> sendFrameAsync,
        TaskCompletionSource<DirectProtocolException?> completion,
        CancellationTokenSource ownerLifetime)
    {
        CancellationToken cancellationToken = ownerLifetime.Token;
        try
        {
            while (!cancellationToken.IsCancellationRequested)
            {
                bool progressed = false;
                while (queue.TryRead(out PcmPacket packet))
                {
                    progressed = true;
                    framer.Push(packet);
                    while (framer.TryRead(out PcmFrameChunk chunk))
                    {
                        if (
                            chunk.Sequence is < 0 or > uint.MaxValue
                            || chunk.TimestampUs < 0
                        )
                        {
                            throw new DirectProtocolException(
                                "BW_COMPUTER_VOICE_DIRECT_PCM_FRAME_INVALID",
                                "PCM 序列或时间戳无效");
                        }
                        await sendFrameAsync(
                            new DirectPcmFrame(
                                track,
                                checked((uint)chunk.Sequence),
                                checked((ulong)chunk.TimestampUs),
                                chunk.Data),
                            cancellationToken).ConfigureAwait(false);
                    }
                }
                if (
                    queue.IsCompleted
                    && queue.CompletionError is not null
                )
                {
                    throw queue.CompletionError;
                }
                if (!progressed)
                {
                    await Task.Delay(2, cancellationToken)
                        .ConfigureAwait(false);
                }
            }
        }
        catch (OperationCanceledException)
            when (cancellationToken.IsCancellationRequested)
        {
        }
        catch (Exception exception)
        {
            DirectProtocolException failure =
                exception as DirectProtocolException
                ?? new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_MEDIA_PUMP_FAILED",
                    "Windows PCM 传输中断",
                    retryable: true,
                    innerException: exception);
            _ = Interlocked.CompareExchange(
                ref _terminalMediaFailure,
                failure,
                null);
            completion.TrySetResult(failure);
            if (ReferenceEquals(
                Volatile.Read(ref _captureLifetime),
                ownerLifetime))
            {
                _captureActive = false;
                ownerLifetime.Cancel();
            }
            ScheduleOwnedFailureCleanup(ownerLifetime);
        }
    }

    private async Task MonitorRenderAsync(
        VirtualMicrophoneRenderSession renderSession,
        TaskCompletionSource<DirectProtocolException?> completion,
        CancellationTokenSource ownerLifetime)
    {
        CancellationToken cancellationToken = ownerLifetime.Token;
        DirectProtocolException? failure = null;
        try
        {
            await renderSession.Completion.ConfigureAwait(false);
            if (!cancellationToken.IsCancellationRequested)
            {
                failure = new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_RENDER_STOPPED_UNEXPECTEDLY",
                    "虚拟麦克风播放端点意外停止",
                    retryable: true);
            }
        }
        catch (OperationCanceledException)
            when (cancellationToken.IsCancellationRequested)
        {
        }
        catch (Exception exception)
        {
            AudioCaptureStageException? stageFailure =
                FindAudioStageFailure(exception);
            failure = new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_RENDER_FAILED",
                stageFailure is null
                    ? "虚拟麦克风播放失败"
                    : "虚拟麦克风播放失败（"
                        + stageFailure.PublicDetail
                        + "）",
                retryable: true,
                innerException: exception);
        }

        if (failure is null)
        {
            return;
        }
        _ = Interlocked.CompareExchange(
            ref _terminalMediaFailure,
            failure,
            null);
        completion.TrySetResult(failure);
        if (ReferenceEquals(
            Volatile.Read(ref _captureLifetime),
            ownerLifetime))
        {
            _captureActive = false;
            ownerLifetime.Cancel();
        }
        ScheduleOwnedFailureCleanup(ownerLifetime);
    }

    private void ScheduleOwnedFailureCleanup(
        CancellationTokenSource expectedLifetime)
    {
        _ = Task.Run(async () =>
        {
            await _stateGate.WaitAsync().ConfigureAwait(false);
            try
            {
                await StopIfCurrentGenerationAsync(
                    _captureLifetime,
                    expectedLifetime,
                    async () =>
                    {
                        _ = await StopOwnedResourcesUnderGateAsync()
                            .ConfigureAwait(false);
                    }).ConfigureAwait(false);
            }
            catch
            {
            }
            finally
            {
                _stateGate.Release();
            }
        });
    }

    internal static Task StopIfCurrentGenerationAsync(
        object? currentGeneration,
        object expectedGeneration,
        Func<Task> stopCurrentAsync) =>
        ReferenceEquals(currentGeneration, expectedGeneration)
            ? stopCurrentAsync()
            : Task.CompletedTask;

    private Task ReleaseTypistLeaseAsync(
        DirectTypistLease lease)
        => _typist.ReleaseAsync(
            lease,
            CancellationToken.None);

    internal async Task ReleasePendingTypistAfterStartFailureAsync(
        DirectTypistLease pendingLease,
        Exception? startFailure)
    {
        try
        {
            await ReleaseTypistLeaseAsync(pendingLease)
                .ConfigureAwait(false);
        }
        catch (Exception releaseFailure)
        {
            // StartAsync still holds _stateGate here.  Preserve the exact PID
            // lease before unwinding so the next START or DisposeAsync owns
            // and retries the same helper process instead of orphaning it.
            if (_ownedTypistLease is not null)
            {
                throw CombineStartAndTypistReleaseFailures(
                    startFailure,
                    new AggregateException(
                        releaseFailure,
                        new InvalidOperationException(
                            "BW_COMPUTER_VOICE_DIRECT_TYPIST_OWNERSHIP_CONFLICT")));
            }
            _ownedTypistLease = pendingLease;
            throw CombineStartAndTypistReleaseFailures(
                startFailure,
                releaseFailure);
        }
    }

    internal static DirectProtocolException
        CombineStartAndTypistReleaseFailures(
            Exception? startFailure,
            Exception releaseFailure)
        => CombineStartAndCleanupFailures(
            startFailure,
            releaseFailure,
            "Windows 音频捕获启动失败，且 voice-typist lease 释放失败");

    internal static DirectProtocolException
        CombineStartAndCleanupFailures(
            Exception? startFailure,
            Exception cleanupFailure,
            string fallbackMessage = "Windows 音频捕获启动失败，且清理失败")
    {
        if (startFailure is DirectProtocolException protocol)
        {
            return new DirectProtocolException(
                protocol.Code,
                protocol.Message,
                protocol.Retryable,
                new AggregateException(
                    startFailure,
                    cleanupFailure));
        }
        return new DirectProtocolException(
            "BW_COMPUTER_VOICE_DIRECT_MEDIA_START_FAILED",
            fallbackMessage,
            retryable: false,
            innerException: startFailure is null
                ? cleanupFailure
                : new AggregateException(
                    startFailure,
                    cleanupFailure));
    }

    internal static async Task<Exception?> RunBestEffortCleanupAsync(
        params Func<Task>[] operations)
    {
        List<Exception>? failures = null;
        foreach (Func<Task> operation in operations)
        {
            try
            {
                await operation().ConfigureAwait(false);
            }
            catch (Exception exception)
            {
                failures ??= [];
                failures.Add(exception);
            }
        }
        return failures?.Count switch
        {
            null or 0 => null,
            1 => failures[0],
            _ => new AggregateException(failures),
        };
    }

    internal static async Task ReleaseOwnershipAfterSuccessAsync(
        Func<Task> releaseAsync,
        Action clearOwnership)
    {
        await releaseAsync().ConfigureAwait(false);
        clearOwnership();
    }

    private static DirectProtocolException? CombineStopFailures(
        DirectProtocolException? terminalMediaFailure,
        Exception? cleanupFailure)
    {
        if (cleanupFailure is null)
        {
            return terminalMediaFailure;
        }
        if (
            terminalMediaFailure is null
            && cleanupFailure is DirectProtocolException protocol
        )
        {
            return protocol;
        }
        return new DirectProtocolException(
            terminalMediaFailure?.Code
                ?? "BW_COMPUTER_VOICE_DIRECT_MEDIA_STOP_FAILED",
            terminalMediaFailure?.Message
                ?? "Windows 音频捕获清理失败",
            terminalMediaFailure?.Retryable ?? false,
            terminalMediaFailure is null
                ? cleanupFailure
                : new AggregateException(
                    terminalMediaFailure,
                    cleanupFailure));
    }

    private static DirectProtocolException TypistReleaseFailure(
        Exception exception) =>
        exception as DirectProtocolException
        ?? new DirectProtocolException(
            "BW_COMPUTER_VOICE_DIRECT_TYPIST_STOP_FAILED",
            "voice-typist owned lease 释放失败",
            retryable: false,
            innerException: exception);

    private static async Task StopPreparedAsync(
        VirtualMicrophoneRenderSession? render,
        ProcessLoopbackCaptureSession? output)
    {
        Exception? failure = await RunBestEffortCleanupAsync(
            () => render is null
                ? Task.CompletedTask
                : render.StopAsync(CancellationToken.None),
            () => output is null
                ? Task.CompletedTask
                : output.StopAsync(CancellationToken.None),
            () => render is null
                ? Task.CompletedTask
                : render.DisposeAsync().AsTask(),
            () => output is null
                ? Task.CompletedTask
                : output.DisposeAsync().AsTask())
            .ConfigureAwait(false);
        if (failure is not null)
        {
            throw failure is DirectProtocolException protocol
                ? protocol
                : new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_MEDIA_STOP_FAILED",
                    "Windows 音频捕获清理失败",
                    retryable: false,
                    innerException: failure);
        }
    }

    public async ValueTask DisposeAsync()
    {
        if (_disposed && !HasOwnedCleanupResources)
        {
            return;
        }
        _disposed = true;
        DirectProtocolException? teardownFailure = null;
        bool cleanupPending = false;
        try
        {
            // A retained exact-PID typist lease is safe to retry once.  The
            // first failed release remains observable through Completion; a
            // successful second attempt replaces it with a settled success.
            for (int attempt = 0; attempt < 2; attempt++)
            {
                await StopAsync(CancellationToken.None)
                    .ConfigureAwait(false);
                Task<DirectProtocolException?> completion = Completion;
                teardownFailure = completion.IsCompleted
                    ? await completion.ConfigureAwait(false)
                    : new DirectProtocolException(
                        "BW_COMPUTER_VOICE_DIRECT_MEDIA_STOP_UNCONFIRMED",
                        "媒体适配器没有确认停止完成");
                if (!HasOwnedCleanupResources)
                {
                    break;
                }
            }
            if (HasOwnedCleanupResources)
            {
                cleanupPending = true;
                teardownFailure ??= new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_MEDIA_CLEANUP_PENDING",
                    "Windows 音频清理仍持有未释放资源",
                    retryable: true);
            }
        }
        finally
        {
            cleanupPending = HasOwnedCleanupResources;
            if (!cleanupPending)
            {
                _stateGate.Dispose();
            }
        }
        if (teardownFailure is not null)
        {
            throw teardownFailure;
        }
    }
}
