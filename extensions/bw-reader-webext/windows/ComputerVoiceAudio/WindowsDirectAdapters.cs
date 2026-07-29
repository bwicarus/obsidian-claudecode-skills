using System.Diagnostics;
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

internal sealed class WindowsDirectMediaAdapter : IDirectMediaAdapter
{
    private readonly string _typistHelper;
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
    private volatile bool _captureActive;
    private bool _disposed;

    internal WindowsDirectMediaAdapter(string installationRoot)
    {
        _typistHelper = System.IO.Path.Combine(
            installationRoot,
            "bw_computer_voice_typist_helper.py");
    }

    public bool IsWired => true;

    public bool CaptureActive => _captureActive;

    public Task<DirectProtocolException?> Completion => _completion;

    public async Task<DirectMediaStartResult> StartAsync(
        DirectMediaStartRequest request,
        Func<DirectPcmFrame, CancellationToken, Task> sendFrameAsync,
        CancellationToken cancellationToken)
    {
        await _stateGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            ObjectDisposedException.ThrowIf(_disposed, this);
            if (_captureActive
                || _outputSession is not null
                || _microphoneSession is not null)
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_MEDIA_BUSY",
                    "Windows 音频捕获已在运行",
                    retryable: true);
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

            await EnsureTypistRunningAsync(cancellationToken)
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

                cancellationToken.ThrowIfCancellationRequested();
                if (!WindowsCodexAppProbe.SendVoiceShortcut(target))
                {
                    throw new DirectProtocolException(
                        "BW_COMPUTER_VOICE_DIRECT_SHORTCUT_FAILED",
                        "Codex 语音快捷键发送失败");
                }
                cancellationToken.ThrowIfCancellationRequested();

                _outputSession = outputSession;
                _microphoneSession = microphoneSession;
                _captureLifetime = lifetime;
                TaskCompletionSource<DirectProtocolException?> completion =
                    new(TaskCreationOptions.RunContinuationsAsynchronously);
                _completionSource = completion;
                _completion = completion.Task;
                _captureActive = true;
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
                return new DirectMediaStartResult(
                    HostReady: true,
                    CaptureActive: true);
            }
            catch
            {
                lifetime.Cancel();
                await StopPreparedAsync(
                    microphoneSession,
                    outputSession).ConfigureAwait(false);
                lifetime.Dispose();
                throw;
            }
        }
        catch (DirectProtocolException)
        {
            throw;
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (Exception exception)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_MEDIA_START_FAILED",
                "Windows 音频捕获启动失败",
                retryable: false,
                innerException: exception);
        }
        finally
        {
            _stateGate.Release();
        }
    }

    public async Task StopAsync(CancellationToken cancellationToken)
    {
        await _stateGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            CancellationTokenSource? lifetime = _captureLifetime;
            ProcessLoopbackCaptureSession? output = _outputSession;
            ExplicitMicrophoneCaptureSession? microphone =
                _microphoneSession;
            Task? outputPump = _outputPump;
            Task? microphonePump = _microphonePump;
            TaskCompletionSource<DirectProtocolException?>? completion =
                _completionSource;
            _captureLifetime = null;
            _outputSession = null;
            _microphoneSession = null;
            _outputPump = null;
            _microphonePump = null;
            _completionSource = null;
            _captureActive = false;
            lifetime?.Cancel();
            completion?.TrySetResult(null);
            await StopPreparedAsync(microphone, output)
                .ConfigureAwait(false);
            if (outputPump is not null || microphonePump is not null)
            {
                try
                {
                    await Task.WhenAll(
                        outputPump ?? Task.CompletedTask,
                        microphonePump ?? Task.CompletedTask)
                        .ConfigureAwait(false);
                }
                catch
                {
                }
            }
            lifetime?.Dispose();
        }
        finally
        {
            _stateGate.Release();
        }
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

    private async Task EnsureTypistRunningAsync(
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
        start.ArgumentList.Add("--ensure-running");
        cancellationToken.ThrowIfCancellationRequested();
        using Process process = Process.Start(start)
            ?? throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_TYPIST_START_FAILED",
                "voice-typist 启动失败");
        string output = await process.StandardOutput.ReadToEndAsync(
            cancellationToken).ConfigureAwait(false);
        _ = await process.StandardError.ReadToEndAsync(
            cancellationToken).ConfigureAwait(false);
        await process.WaitForExitAsync(cancellationToken)
            .ConfigureAwait(false);
        if (process.ExitCode != 0)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_TYPIST_START_FAILED",
                "voice-typist 启动失败");
        }
        try
        {
            using JsonDocument result = JsonDocument.Parse(output);
            if (
                !result.RootElement.TryGetProperty(
                    "ok",
                    out JsonElement ok)
                || ok.ValueKind != JsonValueKind.True
                || !result.RootElement.TryGetProperty(
                    "running",
                    out JsonElement running)
                || running.ValueKind != JsonValueKind.True
            )
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_TYPIST_START_FAILED",
                    "voice-typist 未确认运行");
            }
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

    private static async Task StopPreparedAsync(
        ExplicitMicrophoneCaptureSession? microphone,
        ProcessLoopbackCaptureSession? output)
    {
        if (microphone is not null)
        {
            try
            {
                await microphone.StopAsync(CancellationToken.None)
                    .ConfigureAwait(false);
            }
            catch
            {
            }
        }
        if (output is not null)
        {
            try
            {
                await output.StopAsync(CancellationToken.None)
                    .ConfigureAwait(false);
            }
            catch
            {
            }
        }
        if (microphone is not null)
        {
            await microphone.DisposeAsync().ConfigureAwait(false);
        }
        if (output is not null)
        {
            await output.DisposeAsync().ConfigureAwait(false);
        }
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

    public async ValueTask DisposeAsync()
    {
        if (_disposed)
        {
            return;
        }
        _disposed = true;
        await StopAsync(CancellationToken.None).ConfigureAwait(false);
        _stateGate.Dispose();
    }
}
