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

internal sealed record DirectTypistLease(int ProcessId);

internal sealed record DirectTypistHelperResult(
    int ExitCode,
    string StandardOutput,
    string StandardError);

internal sealed class WindowsDirectTypistLeaseController
{
    private readonly string _typistHelper;
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
    }

    internal WindowsDirectTypistLeaseController(
        Func<
            IReadOnlyList<string>,
            CancellationToken,
            Task<DirectTypistHelperResult>> invokeHelperAsync)
    {
        _typistHelper = "";
        _invokeHelperAsync = invokeHelperAsync;
    }

    internal async Task<DirectTypistLease?> EnsureRunningAsync(
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        // Once the fixed launcher has been invoked, let it finish and return
        // the exact PID result even if the browser disconnects.  The caller
        // can then release an owned lease instead of losing ownership during
        // a cancellation race.
        DirectTypistHelperResult completed =
            await _invokeHelperAsync(
                new[] { "--ensure-running" },
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
        if (lease.ProcessId <= 0)
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
                    "result",
                    out JsonElement outcome)
                || outcome.ValueKind != JsonValueKind.String
            )
            {
                throw InvalidStartResult();
            }
            return outcome.GetString() switch
            {
                "started" => new DirectTypistLease(processId),
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
    private readonly SemaphoreSlim _stateGate = new(1, 1);
    private ProcessLoopbackCaptureSession? _outputSession;
    private ExplicitMicrophoneCaptureSession? _microphoneSession;
    private CancellationTokenSource? _captureLifetime;
    private Task? _outputPump;
    private Task? _microphonePump;
    private TaskCompletionSource<DirectProtocolException?>?
        _completionSource;
    private Task<DirectProtocolException?> _completion =
        Task.FromResult<DirectProtocolException?>(null);
    private DirectTypistLease? _ownedTypistLease;
    private DirectProtocolException? _terminalMediaFailure;
    private volatile bool _captureActive;
    private bool _disposed;

    internal WindowsDirectMediaAdapter(string installationRoot)
        : this(new WindowsDirectTypistLeaseController(installationRoot))
    {
    }

    internal WindowsDirectMediaAdapter(
        WindowsDirectTypistLeaseController typist)
    {
        _typist = typist;
    }

    public bool IsWired => true;

    public bool CaptureActive => _captureActive;

    public Task<DirectProtocolException?> Completion => _completion;

    public async Task<DirectMediaStartResult> StartAsync(
        DirectMediaStartRequest request,
        Func<DirectPcmFrame, CancellationToken, Task> sendFrameAsync,
        CancellationToken cancellationToken)
    {
        DirectTypistLease? pendingTypistLease = null;
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
            MicCaptureRequest microphone =
                MicCaptureRequest.Create(
                    request.MicrophoneEndpointId);
            CodexAppTarget target = WindowsCodexAppProbe.RequireReady();
            if (target.RootProcessId != request.RootProcessId)
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_APP_TARGET_CHANGED",
                    "Codex 目标进程已变化");
            }

            pendingTypistLease = await _typist.EnsureRunningAsync(
                    cancellationToken)
                .ConfigureAwait(false);
            BoundedPcmPacketQueue outputQueue = new(
                32,
                2 * 1024 * 1024);
            BoundedPcmPacketQueue microphoneQueue = new(
                32,
                2 * 1024 * 1024);
            ProcessLoopbackCaptureSession outputSession =
                ProcessLoopbackCaptureSession.Prepare(
                    request.RootProcessId,
                    outputQueue);
            ExplicitMicrophoneCaptureSession microphoneSession =
                ExplicitMicrophoneCaptureSession.Prepare(
                    microphone,
                    microphoneQueue);
            CancellationTokenSource lifetime =
                CancellationTokenSource.CreateLinkedTokenSource(
                    cancellationToken);
            try
            {
                await outputSession.StartAsync(lifetime.Token)
                    .ConfigureAwait(false);
                await microphoneSession.StartAsync(lifetime.Token)
                    .ConfigureAwait(false);
                Pcm48kMonoFramer outputFramer = new(
                    outputSession.Format
                    ?? throw new DirectProtocolException(
                        "BW_COMPUTER_VOICE_DIRECT_AUDIO_FORMAT_MISSING",
                        "应用输出音频格式不存在"));
                Pcm48kMonoFramer microphoneFramer = new(
                    microphoneSession.Format
                    ?? throw new DirectProtocolException(
                        "BW_COMPUTER_VOICE_DIRECT_AUDIO_FORMAT_MISSING",
                        "麦克风音频格式不存在"));

                TaskCompletionSource<DirectProtocolException?> completion =
                    new(TaskCreationOptions.RunContinuationsAsynchronously);
                SendShortcutAtAtomicCommitBoundary(
                    () => WindowsCodexAppProbe.SendVoiceShortcut(target),
                    () =>
                    {
                        // There must be no cancellation observation between a
                        // successful shortcut and committing every bridge-owned
                        // resource.  A peer close can then wait on _stateGate
                        // and deterministically tear capture/typist down.
                        _outputSession = outputSession;
                        _microphoneSession = microphoneSession;
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
                            lifetime.Token);
                        _microphonePump = PumpAsync(
                            DirectPcmTrack.UserMicrophone,
                            microphoneQueue,
                            microphoneFramer,
                            sendFrameAsync,
                            completion,
                            lifetime.Token);
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
                            microphoneSession,
                            outputSession),
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
        Func<bool> sendShortcut,
        Action commitOwnedResources,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
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

    private bool HasOwnedCleanupResources =>
        _captureLifetime is not null
        || _outputSession is not null
        || _microphoneSession is not null
        || _outputPump is not null
        || _microphonePump is not null
        || _ownedTypistLease is not null;

    private async Task<DirectProtocolException?>
        StopOwnedResourcesUnderGateAsync()
    {
        CancellationTokenSource? lifetime = _captureLifetime;
        ProcessLoopbackCaptureSession? output = _outputSession;
        ExplicitMicrophoneCaptureSession? microphone =
            _microphoneSession;
        Task? outputPump = _outputPump;
        Task? microphonePump = _microphonePump;
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
            () => microphone is null
                ? Task.CompletedTask
                : microphone.StopAsync(CancellationToken.None),
            () => output is null
                ? Task.CompletedTask
                : output.StopAsync(CancellationToken.None),
            async () =>
            {
                if (microphone is null)
                {
                    return;
                }
                try
                {
                    await microphone.DisposeAsync().ConfigureAwait(false);
                }
                finally
                {
                    if (
                        microphone.State == CaptureSessionState.Disposed
                        && ReferenceEquals(
                            _microphoneSession,
                            microphone)
                    )
                    {
                        _microphoneSession = null;
                    }
                }
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
                if (microphonePump is null)
                {
                    return;
                }
                try
                {
                    await microphonePump.ConfigureAwait(false);
                }
                finally
                {
                    if (ReferenceEquals(
                        _microphonePump,
                        microphonePump)
                        && microphonePump.IsCompleted)
                    {
                        _microphonePump = null;
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
        CancellationToken cancellationToken)
    {
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
            _captureActive = false;
            completion.TrySetResult(failure);
            _captureLifetime?.Cancel();
            _ = Task.Run(async () =>
            {
                try
                {
                    await StopAsync(CancellationToken.None)
                        .ConfigureAwait(false);
                }
                catch
                {
                }
            });
        }
    }

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
        ExplicitMicrophoneCaptureSession? microphone,
        ProcessLoopbackCaptureSession? output)
    {
        Exception? failure = await RunBestEffortCleanupAsync(
            () => microphone is null
                ? Task.CompletedTask
                : microphone.StopAsync(CancellationToken.None),
            () => output is null
                ? Task.CompletedTask
                : output.StopAsync(CancellationToken.None),
            () => microphone is null
                ? Task.CompletedTask
                : microphone.DisposeAsync().AsTask(),
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
