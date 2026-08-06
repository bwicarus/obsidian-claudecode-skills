using System.Diagnostics;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Text.Json;
using BwReader.ComputerVoiceAudio.Interop;

namespace BwReader.ComputerVoiceAudio;

internal sealed class WindowsDirectAppLauncher : IDirectAppLauncher
{
    public bool IsWired => true;

    public Task EnsureRunningAsync(
        string appKind,
        string appUserModelId,
        CancellationToken cancellationToken)
    {
        DirectAppTargetProfile profile =
            ValidateTarget(appKind, appUserModelId);
        cancellationToken.ThrowIfCancellationRequested();
        if (!OperatingSystem.IsWindows())
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_WINDOWS_REQUIRED",
                "Codex packaged app 只能在 Windows 启动");
        }
        CodexAppProbeState current =
            WindowsCodexAppProbe.Probe(profile.AppKind);
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
                profile.AppUserModelId,
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
        DirectAppTargetProfile profile =
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
            CodexAppProbeState state =
                WindowsCodexAppProbe.Probe(profile.AppKind);
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
                    target.RootProcessStartFileTimeUtc,
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

    internal static DirectAppTargetProfile ValidateTarget(
        string appKind,
        string appUserModelId) =>
        DirectAppTargets.Require(appKind, appUserModelId);

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

    internal Task<DirectTypistLease?> EnsureRunningAsync(
        CancellationToken cancellationToken) =>
        EnsureRunningAsync(
            DirectAppTargets.CodexDesktop,
            cancellationToken);

    internal async Task<DirectTypistLease?> EnsureRunningAsync(
        string appKind,
        CancellationToken cancellationToken)
    {
        _ = DirectAppTargets.Require(appKind);
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
                    appKind,
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

internal interface IDirectCaptureEndpointMuteBackend
{
    bool ReadMuted(string endpointId);

    void WriteMuted(string endpointId, bool muted);
}

internal sealed class DirectCaptureEndpointMuteLease
{
    private readonly IDirectCaptureEndpointMuteBackend _backend;
    private readonly string _endpointId;
    private readonly bool _restoreRequired;
    private bool _restored;

    private DirectCaptureEndpointMuteLease(
        IDirectCaptureEndpointMuteBackend backend,
        string endpointId,
        bool restoreRequired)
    {
        _backend = backend;
        _endpointId = endpointId;
        _restoreRequired = restoreRequired;
    }

    internal static DirectCaptureEndpointMuteLease Acquire(
        IDirectCaptureEndpointMuteBackend backend,
        string endpointId)
    {
        ArgumentNullException.ThrowIfNull(backend);
        if (string.IsNullOrWhiteSpace(endpointId))
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_MIC_ENDPOINT_MISSING",
                "虚拟麦克风采集端点未配置");
        }

        bool originallyMuted = false;
        bool originalStateRead = false;
        try
        {
            originallyMuted = backend.ReadMuted(endpointId);
            originalStateRead = true;
            if (originallyMuted)
            {
                backend.WriteMuted(endpointId, muted: false);
            }
            if (backend.ReadMuted(endpointId))
            {
                throw new InvalidOperationException(
                    "capture endpoint remained muted after unmute");
            }
            return new DirectCaptureEndpointMuteLease(
                backend,
                endpointId,
                restoreRequired: originallyMuted);
        }
        catch (Exception exception)
        {
            Exception failure = exception;
            if (originalStateRead && originallyMuted)
            {
                try
                {
                    backend.WriteMuted(endpointId, muted: true);
                }
                catch (Exception restoreException)
                {
                    failure = new AggregateException(
                        exception,
                        restoreException);
                }
            }
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_MIC_ENDPOINT_UNMUTE_FAILED",
                "Windows 虚拟麦克风采集端点无法解除静音",
                retryable: true,
                innerException: failure);
        }
    }

    internal void RequireUnmuted()
    {
        if (_restored)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_MIC_MUTE_LEASE_RESTORED",
                "虚拟麦克风静音状态租约已释放");
        }
        try
        {
            if (_backend.ReadMuted(_endpointId))
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_MIC_ENDPOINT_MUTED",
                    "Windows 虚拟麦克风采集端点已被重新静音",
                    retryable: true);
            }
        }
        catch (DirectProtocolException)
        {
            throw;
        }
        catch (Exception exception)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_MIC_ENDPOINT_STATE_FAILED",
                "无法确认 Windows 虚拟麦克风静音状态",
                retryable: true,
                innerException: exception);
        }
    }

    internal void Restore()
    {
        if (_restored)
        {
            return;
        }
        if (!_restoreRequired)
        {
            _restored = true;
            return;
        }
        try
        {
            if (!_backend.ReadMuted(_endpointId))
            {
                _backend.WriteMuted(_endpointId, muted: true);
            }
            if (!_backend.ReadMuted(_endpointId))
            {
                throw new InvalidOperationException(
                    "capture endpoint mute state was not restored");
            }
            _restored = true;
        }
        catch (Exception exception)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_MIC_ENDPOINT_RESTORE_FAILED",
                "Windows 虚拟麦克风静音状态恢复失败",
                retryable: true,
                innerException: exception);
        }
    }
}

internal sealed class NativeDirectCaptureEndpointMuteBackend :
    IDirectCaptureEndpointMuteBackend
{
    private static readonly Guid EndpointVolumeInterfaceId =
        new("5CDF2C82-841E-4546-9722-0CF74078229A");
    private static readonly Guid EventContext =
        new("2C138E74-1C55-4B65-A371-39C44C6D93B6");

    public bool ReadMuted(string endpointId) =>
        WithEndpointVolume(
            endpointId,
            volume =>
            {
                RequireSucceeded(
                    volume.GetMute(out bool muted),
                    "virtual-microphone.get-mute");
                return muted;
            });

    public void WriteMuted(string endpointId, bool muted)
    {
        _ = WithEndpointVolume(
            endpointId,
            volume =>
            {
                Guid eventContext = EventContext;
                RequireSucceeded(
                    volume.SetMute(muted, ref eventContext),
                    "virtual-microphone.set-mute");
                return true;
            });
    }

    private static T WithEndpointVolume<T>(
        string endpointId,
        Func<IAudioEndpointVolumeForBridge, T> action)
    {
        if (!OperatingSystem.IsWindows())
        {
            throw new PlatformNotSupportedException(
                "BW_COMPUTER_VOICE_AUDIO_WINDOWS_REQUIRED");
        }
        object? enumeratorObject = null;
        IMMDevice? endpoint = null;
        object? endpointVolumeObject = null;
        using ComMtaLease apartment = ComMtaLease.Enter();
        try
        {
            Type enumeratorType = Type.GetTypeFromCLSID(
                ExplicitMicrophoneInterop.ClsidMmDeviceEnumerator,
                throwOnError: true)
                ?? throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_AUDIO_MMDEVICE_TYPE_MISSING");
            enumeratorObject = Activator.CreateInstance(enumeratorType);
            if (enumeratorObject is not IMMDeviceEnumerator enumerator)
            {
                throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_AUDIO_MMDEVICE_ENUMERATOR_INVALID");
            }
            RequireSucceeded(
                enumerator.GetDevice(endpointId, out endpoint),
                "virtual-microphone.get-explicit-capture-device");
            if (endpoint is null)
            {
                throw new AudioCaptureStageException(
                    "virtual-microphone.get-explicit-capture-device",
                    unchecked((int)0x80070490));
            }
            RequireSucceeded(
                endpoint.GetState(out DeviceState state),
                "virtual-microphone.get-capture-device-state");
            if ((state & DeviceState.Active) == 0)
            {
                throw new AudioCaptureStageException(
                    "virtual-microphone.capture-device-inactive",
                    unchecked((int)0x88890004));
            }
            if (endpoint is not IMMEndpoint direction)
            {
                throw new AudioCaptureStageException(
                    "virtual-microphone.query-capture-data-flow",
                    unchecked((int)0x80004002));
            }
            RequireSucceeded(
                direction.GetDataFlow(out AudioDataFlow dataFlow),
                "virtual-microphone.get-capture-data-flow");
            if (dataFlow != AudioDataFlow.Capture)
            {
                throw new AudioCaptureStageException(
                    "virtual-microphone.capture-data-flow-mismatch",
                    unchecked((int)0x80070057));
            }
            Guid interfaceId = EndpointVolumeInterfaceId;
            RequireSucceeded(
                endpoint.Activate(
                    ref interfaceId,
                    ComClassContext.All,
                    nint.Zero,
                    out endpointVolumeObject),
                "virtual-microphone.activate-endpoint-volume");
            if (
                endpointVolumeObject
                    is not IAudioEndpointVolumeForBridge endpointVolume
            )
            {
                throw new AudioCaptureStageException(
                    "virtual-microphone.query-endpoint-volume",
                    unchecked((int)0x80004002));
            }
            return action(endpointVolume);
        }
        finally
        {
            ReleaseComObject(endpointVolumeObject);
            ReleaseComObject(endpoint);
            ReleaseComObject(enumeratorObject);
        }
    }

    private static void RequireSucceeded(int result, string operation)
    {
        if (result != ProcessLoopbackInterop.Succeeded)
        {
            throw new AudioCaptureStageException(
                operation,
                result,
                result < 0
                    ? Marshal.GetExceptionForHR(result)
                    : null);
        }
    }

    private static void ReleaseComObject(object? value)
    {
        if (
            OperatingSystem.IsWindows()
            && value is not null
            && Marshal.IsComObject(value)
        )
        {
            Marshal.FinalReleaseComObject(value);
        }
    }
}

[ComImport]
[Guid("5CDF2C82-841E-4546-9722-0CF74078229A")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IAudioEndpointVolumeForBridge
{
    [PreserveSig]
    int RegisterControlChangeNotify(nint notify);

    [PreserveSig]
    int UnregisterControlChangeNotify(nint notify);

    [PreserveSig]
    int GetChannelCount(out uint channelCount);

    [PreserveSig]
    int SetMasterVolumeLevel(float levelDb, ref Guid eventContext);

    [PreserveSig]
    int SetMasterVolumeLevelScalar(float level, ref Guid eventContext);

    [PreserveSig]
    int GetMasterVolumeLevel(out float levelDb);

    [PreserveSig]
    int GetMasterVolumeLevelScalar(out float level);

    [PreserveSig]
    int SetChannelVolumeLevel(
        uint channel,
        float levelDb,
        ref Guid eventContext);

    [PreserveSig]
    int SetChannelVolumeLevelScalar(
        uint channel,
        float level,
        ref Guid eventContext);

    [PreserveSig]
    int GetChannelVolumeLevel(uint channel, out float levelDb);

    [PreserveSig]
    int GetChannelVolumeLevelScalar(uint channel, out float level);

    [PreserveSig]
    int SetMute(
        [MarshalAs(UnmanagedType.Bool)] bool muted,
        ref Guid eventContext);

    [PreserveSig]
    int GetMute([MarshalAs(UnmanagedType.Bool)] out bool muted);
}

internal sealed class WindowsDirectMediaAdapter : IDirectMediaAdapter
{
    private static readonly TimeSpan AudioPolicyProcessReadyTimeout =
        TimeSpan.FromSeconds(3);
    private static readonly TimeSpan VoiceReadyTimeout =
        TimeSpan.FromSeconds(12);

    private readonly WindowsDirectTypistLeaseController _typist;
    private readonly IDirectOutputRouteObserverFactory
        _outputRouteObserverFactory;
    private readonly Func<IPerAppAudioPolicyBackend>
        _audioPolicyBackendFactory;
    private readonly Func<IDirectCaptureEndpointMuteBackend>
        _captureEndpointMuteBackendFactory;
    private readonly string _audioRouteJournalPath;
    private readonly Func<string, CodexVoiceActivityController>
        _voiceActivityFactory;
    private readonly Func<string, ICodexVoiceShortcutSender>
        _voiceShortcutSenderFactory;
    private readonly SemaphoreSlim _stateGate = new(1, 1);
    private DirectOutputCaptureSession? _outputSession;
    private VirtualMicrophoneRenderSession? _renderSession;
    private IDirectOutputRouteObserver? _outputRouteObserver;
    private CancellationTokenSource? _captureLifetime;
    private Task? _outputPump;
    private Task? _renderMonitor;
    private CancellationTokenSource? _voiceMonitorLifetime;
    private Task? _voiceMonitor;
    private TaskCompletionSource<DirectProtocolException?>?
        _completionSource;
    private Task<DirectProtocolException?> _completion =
        Task.FromResult<DirectProtocolException?>(null);
    private DirectTypistLease? _ownedTypistLease;
    private IPerAppAudioPolicyBackend? _audioPolicyBackend;
    private PerAppAudioRouteLease? _audioRouteLease;
    private DirectCaptureEndpointMuteLease? _captureEndpointMuteLease;
    private CodexVoiceStartBaseline? _voiceStartBaseline;
    private CodexVoiceStartConfirmation? _voiceConfirmation;
    private CodexAppTarget? _voiceTarget;
    private CodexVoiceActivityController? _voiceActivity;
    private ICodexVoiceShortcutSender? _voiceShortcutSender;
    private string? _voiceAppKind;
    private DirectProtocolException? _terminalMediaFailure;
    private volatile bool _captureActive;
    private bool _disposed;

    internal WindowsDirectMediaAdapter(string installationRoot)
        : this(
            new WindowsDirectTypistLeaseController(installationRoot),
            new NativeDirectOutputRouteObserverFactory(),
            CreateNativeAudioPolicyBackend,
            System.IO.Path.Combine(
                installationRoot,
                "runtime",
                "computer-voice-audio-route.transaction.json"),
            CreateNativeCaptureEndpointMuteBackend)
    {
    }

    internal WindowsDirectMediaAdapter(
        WindowsDirectTypistLeaseController typist,
        IDirectOutputRouteObserverFactory? outputRouteObserverFactory = null,
        Func<IPerAppAudioPolicyBackend>? audioPolicyBackendFactory = null,
        string? audioRouteJournalPath = null,
        Func<IDirectCaptureEndpointMuteBackend>?
            captureEndpointMuteBackendFactory = null,
        CodexVoiceActivityController? voiceActivity = null,
        ICodexVoiceShortcutSender? voiceShortcutSender = null)
    {
        _typist = typist;
        _outputRouteObserverFactory =
            outputRouteObserverFactory
            ?? new NativeDirectOutputRouteObserverFactory();
        _audioPolicyBackendFactory =
            audioPolicyBackendFactory
            ?? (() => throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_AUDIO_POLICY_NOT_CONFIGURED",
                "按应用音频路由仅由正式 Windows 适配器创建"));
        _audioRouteJournalPath = audioRouteJournalPath
            ?? System.IO.Path.Combine(
                System.IO.Path.GetTempPath(),
                "bw-computer-voice-audio-route.self-test-disabled.json");
        _captureEndpointMuteBackendFactory =
            captureEndpointMuteBackendFactory
            ?? CreateNativeCaptureEndpointMuteBackend;
        _voiceActivityFactory = voiceActivity is null
            ? appKind => new CodexVoiceActivityController(
                new WindowsRegistryCodexVoiceActivitySource(appKind),
                new SystemCodexVoiceActivityClock(),
                CreateVoiceOwnershipAttestor(appKind))
            : _ => voiceActivity;
        _voiceShortcutSenderFactory = voiceShortcutSender is null
            ? appKind => appKind switch
            {
                DirectAppTargets.CodexDesktop =>
                    new WindowsCodexVoiceShortcutSender(),
                DirectAppTargets.ChatGptClassic =>
                    new WindowsChatGptClassicVoiceShortcutSender(),
                _ => throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_APP_TARGET_INVALID",
                    "应用目标不在本机固定白名单"),
            }
            : _ => voiceShortcutSender;
    }

    private static IPerAppAudioPolicyBackend
        CreateNativeAudioPolicyBackend()
    {
        if (!OperatingSystem.IsWindows())
        {
            throw new PlatformNotSupportedException(
                "Per-app audio routing requires Windows");
        }
        return new NativePerAppAudioPolicyBackend();
    }

    private static IDirectCaptureEndpointMuteBackend
        CreateNativeCaptureEndpointMuteBackend() =>
        new NativeDirectCaptureEndpointMuteBackend();

    internal static ICodexVoiceOwnershipAttestor
        CreateVoiceOwnershipAttestor(string appKind)
    {
        _ = DirectAppTargets.Require(appKind);
        // Ownership is granted only after this bridge has a successful
        // shortcut/UIA receipt and observes a newer activity generation for
        // the same process. Pre-existing, replaced, or restarted Voice
        // sessions still fail closed in ConfirmStarted/PrepareStop.
        return new ExactTargetVoiceOwnershipAttestor();
    }

    private CodexVoiceActivityController VoiceActivity =>
        _voiceActivity
        ?? throw new DirectProtocolException(
            "BW_COMPUTER_VOICE_DIRECT_VOICE_TARGET_MISSING",
            "语音状态目标尚未配置",
            retryable: true);

    private ICodexVoiceShortcutSender VoiceShortcutSender =>
        _voiceShortcutSender
        ?? throw new DirectProtocolException(
            "BW_COMPUTER_VOICE_DIRECT_VOICE_TARGET_MISSING",
            "语音控制目标尚未配置",
            retryable: true);

    private const int ClassicPrimeAttempts = 20;
    private const int ClassicPrimeDelayMilliseconds = 250;
    private static readonly TimeSpan ClassicPrimeSettleDelay =
        TimeSpan.FromMilliseconds(600);

    /// <summary>
    /// Give GPT Classic an audio session so its per-app route can be set.
    /// </summary>
    /// <remarks>
    /// Windows refuses a per-app route for a process that owns no audio
    /// session, answering E_INVALIDARG. Chromium creates that session lazily,
    /// only once the app actually uses audio, so a freshly launched Classic is
    /// absent from the volume mixer and cannot be routed at all -- waiting does
    /// not help because nothing will bring the session into being on its own.
    ///
    /// The entry is persisted though: it survives the audio stopping and only
    /// disappears when the app itself exits. So briefly starting and stopping
    /// voice mints the entry, after which the normal order (route, then start
    /// voice) works unchanged -- no audio can leak to the wrong device because
    /// the real call still begins only after the route is in place.
    ///
    /// Codex never needs this: its audio service stays resident once used.
    /// </remarks>
    private void PrimeClassicAudioSession(
        uint audioPolicyProcessId,
        CodexAppTarget target)
    {
        PerAppAudioRouteKey probeKey = PerAppAudioRouteKey.All[0];
        using IPerAppAudioPolicyBackend backend = _audioPolicyBackendFactory();

        static bool Readable(
            IPerAppAudioPolicyBackend policy,
            uint processId,
            PerAppAudioRouteKey key)
        {
            try
            {
                return policy.Read(processId, key).Kind
                    != PersistedAudioEndpointKind.Error;
            }
            catch
            {
                return false;
            }
        }

        if (Readable(backend, audioPolicyProcessId, probeKey))
        {
            // The app already holds a session; nothing to mint.
            return;
        }

        VoiceShortcutSender.Send(target, DirectVoiceCommand.Start);
        bool minted = false;
        for (int attempt = 0; attempt < ClassicPrimeAttempts; attempt++)
        {
            Thread.Sleep(ClassicPrimeDelayMilliseconds);
            if (Readable(backend, audioPolicyProcessId, probeKey))
            {
                minted = true;
                break;
            }
        }

        // Always hand the app back the way it was found. A priming failure must
        // not leave voice running, so the stop is attempted even when the entry
        // never appeared, and its own failure must not mask the real cause.
        try
        {
            VoiceShortcutSender.Send(target, DirectVoiceCommand.Stop);
        }
        catch (DirectProtocolException)
        {
        }
        Thread.Sleep(ClassicPrimeSettleDelay);

        if (!minted)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_AUDIO_SESSION_PRIME_FAILED",
                "无法让 GPT Classic 建立音频会话；其按应用音频路由不可设置",
                retryable: true,
                innerException: new AudioCaptureStageException(
                    "audio-route.prime-session",
                    0));
        }
    }

    private void ConfigureVoiceTarget(string appKind)
    {
        _ = DirectAppTargets.Require(appKind);
        if (
            _voiceActivity is not null
            && _voiceShortcutSender is not null
            && _voiceAppKind == appKind
        )
        {
            return;
        }
        if (HasOwnedCleanupResources)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_MEDIA_BUSY",
                "活动语音会话期间不能切换应用目标",
                retryable: true);
        }
        _voiceActivity = _voiceActivityFactory(appKind);
        _voiceShortcutSender = _voiceShortcutSenderFactory(appKind);
        _voiceAppKind = appKind;
    }

    public bool IsWired => true;

    public bool CaptureActive => _captureActive;

    public bool CleanupPending => HasOwnedCleanupResources;

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

    public async Task WaitForVoiceReadyAsync(
        TimeSpan timeout,
        CancellationToken cancellationToken)
    {
        _ = await _voiceActivity.WaitForAvailableAsync(
            timeout,
            CodexVoiceActivityController.MonitorInterval,
            cancellationToken).ConfigureAwait(false);
    }

    public async Task<DirectMediaStartResult> StartAsync(
        DirectMediaStartRequest request,
        Func<DirectPcmFrame, CancellationToken, Task> sendFrameAsync,
        CancellationToken cancellationToken)
    {
        DirectTypistLease? pendingTypistLease = null;
        IDirectOutputRouteObserver? pendingOutputRouteObserver = null;
        IPerAppAudioPolicyBackend? pendingAudioPolicyBackend = null;
        PerAppAudioRouteLease? pendingAudioRouteLease = null;
        DirectCaptureEndpointMuteLease?
            pendingCaptureEndpointMuteLease = null;
        CodexVoiceStartBaseline? initialVoiceBaseline = null;
        CodexVoiceStartBaseline? boundaryVoiceBaseline = null;
        CodexVoiceShortcutReceipt? shortcutReceipt = null;
        bool committedOwnedResources = false;
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
            DirectAppTargetProfile appProfile =
                WindowsDirectAppLauncher.ValidateTarget(
                request.AppKind,
                request.AppUserModelId);
            ConfigureVoiceTarget(request.AppKind);
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
            if (request.AutomatePerAppAudioRoute)
            {
                VirtualCaptureEndpointProbe.ValidateExactActiveCapture(
                    request.VirtualMicrophoneCaptureEndpointId);
            }
            if (request.FixedVirtualAudioBus)
            {
                VirtualCaptureEndpointProbe.ValidateExactActiveCapture(
                    request.VirtualSpeakerCaptureEndpointId);
            }
            CodexAppTarget target =
                WindowsCodexAppProbe.RequireReady(request.AppKind);
            if (
                target.RootProcessId != request.RootProcessId
                || target.AppKind != request.AppKind
            )
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_APP_TARGET_CHANGED",
                    "Codex 目标进程已变化");
            }
            if (
                target.RootProcessStartFileTimeUtc
                    != request.RootProcessStartFileTimeUtc
            )
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_APP_TARGET_CHANGED",
                    "Codex 目标进程代次已变化");
            }
            // The installed Codex command is an OS-global hotkey. Validate
            // the single-user local binding before typist or either audio
            // session starts, then revalidate again at the shortcut boundary.
            if (appProfile.UsesCodexGlobalShortcut)
            {
                WindowsCodexAppProbe
                    .RequireExpectedGlobalVoiceShortcut();
            }
            uint audioPolicyProcessId = target.RootProcessId;
            // Chromium 的 audio.mojom.AudioService 是 lazy-start:只有真正开始
            // 播放/录音才会被拉起。Codex 因常驻语音其音频服务一直在,可以先等到
            // 该进程再按它切路由;GPT Classic 冷启动时根本没有这个进程,等它出现
            // 与"点按钮才会出现"互为前置 → 必然 APP_READY_TIMEOUT,连按钮都按不到。
            // SetPersistedDefaultAudioEndpoint 是按应用身份持久化的,用根进程切
            // 效果等价,且路由仍在发送启动指令(下方 shortcut 边界)之前完成,
            // 音频服务起来时已经落在虚拟线上。Codex 分支行为保持不变。
            if (
                request.AutomatePerAppAudioRoute
                && appProfile.UsesCodexGlobalShortcut
            )
            {
                CodexAudioPolicyTarget audioPolicyTarget =
                    await WindowsCodexAppProbe
                        .WaitForAudioPolicyProcessAsync(
                            target,
                            AudioPolicyProcessReadyTimeout,
                            cancellationToken)
                        .ConfigureAwait(false);
                target = audioPolicyTarget.AppTarget;
                audioPolicyProcessId = audioPolicyTarget.ProcessId;
            }
            if (
                request.AutomatePerAppAudioRoute
                && appProfile.AppKind == DirectAppTargets.ChatGptClassic
            )
            {
                PrimeClassicAudioSession(audioPolicyProcessId, target);
            }
            _ = await VoiceActivity.WaitForAvailableAsync(
                VoiceReadyTimeout,
                CodexVoiceActivityController.MonitorInterval,
                cancellationToken).ConfigureAwait(false);
            initialVoiceBaseline =
                VoiceActivity.CaptureStartBaseline();
            BoundedPcmPacketQueue outputQueue = new(
                32,
                2 * 1024 * 1024);
            DirectOutputCaptureSession outputSession =
                DirectOutputCaptureSession.Prepare(
                    request,
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
                if (request.AutomatePerAppAudioRoute)
                {
                    pendingAudioPolicyBackend =
                        _audioPolicyBackendFactory();
                    PerAppAudioRouteController routeController = new(
                        pendingAudioPolicyBackend);
                    PerAppAudioRouteRestoreResult recovered =
                        routeController.RecoverPending(
                            audioPolicyProcessId,
                            _audioRouteJournalPath);
                    if (!recovered.Succeeded)
                    {
                        throw new DirectProtocolException(
                            "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_RECOVERY_INCOMPLETE",
                            "上一次 Codex 音频路由尚未恢复",
                            retryable: true);
                    }
                    // Windows refuses a per-app audio route for a process that
                    // owns no audio session: the policy store answers
                    // E_INVALIDARG (0x80070057), which is why a freshly started
                    // target fails here no matter how long we wait. Chromium
                    // only creates that session when the app actually starts
                    // using audio, which for GPT Classic means after its voice
                    // button is invoked -- the app is absent from the volume
                    // mixer until that moment.
                    //
                    // Codex is unaffected because its audio service stays
                    // resident once used, so its session already exists and the
                    // route is still applied before voice starts. Only Classic
                    // has to invert the order: start voice, then route. The
                    // brief window in between can let the first fragment of
                    // audio reach the default device, which is the unavoidable
                    // cost of the platform's ordering rule.
                    pendingAudioRouteLease = routeController.Acquire(
                        new PerAppAudioRouteRequest(
                            audioPolicyProcessId,
                            request.VirtualSpeakerRenderEndpointId,
                            request.VirtualMicrophoneCaptureEndpointId,
                            _audioRouteJournalPath));
                    // An already-running Codex voice is no longer an error.
                    //
                    // This refused the call whenever Codex was listening on its
                    // own devices, and told the user to close it first. That was
                    // coherent while the bridge opened Codex's voice itself: it
                    // expected to find it closed. Now the two are deliberately
                    // separate -- the user opens voice from its own switch, and
                    // this side only carries audio -- so finding it already open
                    // is the normal case, not a conflict.
                    //
                    // Nothing needs closing either. The lease above is
                    // per-process and has already been acquired by the time
                    // execution reaches here, so Codex's audio is on the virtual
                    // devices from this point on; the check was rejecting a
                    // state it had itself just made correct.
                    pendingCaptureEndpointMuteLease =
                        DirectCaptureEndpointMuteLease.Acquire(
                            _captureEndpointMuteBackendFactory(),
                            request
                                .VirtualMicrophoneCaptureEndpointId);
                }
                // The bounded process-loopback queue has no consumer until
                // the atomic shortcut commit below. Start the already-approved
                // typist first so its launcher checks cannot fill that queue
                // with silent engine packets before the pump is owned.
                if (request.StartTypist)
                {
                    await EnsureTypistThenStartPreparedMediaAsync(
                            token => _typist.EnsureRunningAsync(
                                request.AppKind,
                                token),
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
                        if (request.AutomatePerAppAudioRoute)
                        {
                            VirtualCaptureEndpointProbe
                                .ValidateExactActiveCapture(
                                    request
                                        .VirtualMicrophoneCaptureEndpointId);
                            (
                                pendingAudioRouteLease
                                ?? throw new DirectProtocolException(
                                    "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_LEASE_MISSING",
                                    "按应用音频路由租约不存在")
                            ).RequireStillApplied();
                            (
                                pendingCaptureEndpointMuteLease
                                ?? throw new DirectProtocolException(
                                    "BW_COMPUTER_VOICE_DIRECT_MIC_MUTE_LEASE_MISSING",
                                    "虚拟麦克风静音状态租约不存在")
                            ).RequireUnmuted();
                        }
                        RequirePreparedMediaRunning(
                            outputSession.State,
                            outputSession.Completion.IsCompleted,
                            renderSession.State,
                            renderSession.Completion.IsCompleted);
                        boundaryVoiceBaseline =
                            VoiceActivity.CaptureStartBaseline();
                    },
                    () =>
                    {
                        CodexVoiceStartBaseline baseline =
                            boundaryVoiceBaseline
                            ?? throw new DirectProtocolException(
                                "BW_COMPUTER_VOICE_DIRECT_VOICE_BASELINE_MISSING",
                                "Codex 语音状态基线不存在");
                        // Codex's own voice is no longer opened from here.
                        //
                        // Starting it was the source of every start-time
                        // failure this link has had: the shortcut landing but
                        // the session being stale, the session already running
                        // on another route, the priming toggle cancelling the
                        // real one, the recent-thread pointer aiming at a
                        // conversation that no longer exists. None of those are
                        // ours to control -- they are the internal state of an
                        // application that never agreed to be driven this way.
                        //
                        // So this side now only carries audio. Whether anyone
                        // is listening is a separate question with a separate
                        // switch, and a call placed to nobody is not a failure:
                        // it simply goes unanswered. The baseline is still
                        // captured, because reporting whether Codex is
                        // listening remains useful -- acting on it does not.
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
                        _audioPolicyBackend =
                            pendingAudioPolicyBackend;
                        pendingAudioPolicyBackend = null;
                        _audioRouteLease = pendingAudioRouteLease;
                        pendingAudioRouteLease = null;
                        _captureEndpointMuteLease =
                            pendingCaptureEndpointMuteLease;
                        pendingCaptureEndpointMuteLease = null;
                        _voiceStartBaseline =
                            boundaryVoiceBaseline;
                        _voiceConfirmation = null;
                        _voiceTarget = target;
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
                        committedOwnedResources = true;
                    },
                    cancellationToken);
                CodexVoiceStartConfirmation voiceConfirmation =
                    await VoiceActivity.ConfirmStartedAsync(
                        boundaryVoiceBaseline
                        ?? throw new DirectProtocolException(
                            "BW_COMPUTER_VOICE_DIRECT_VOICE_BASELINE_MISSING",
                            "Codex 语音状态基线不存在"),
                        shortcutReceipt,
                        CodexVoiceActivityController.TransitionTimeout,
                        CodexVoiceActivityController.MonitorInterval,
                        CancellationToken.None).ConfigureAwait(false);
                _voiceConfirmation = voiceConfirmation;
                _voiceStartBaseline = null;
                CancellationTokenSource voiceMonitorLifetime = new();
                _voiceMonitorLifetime = voiceMonitorLifetime;
                _voiceMonitor = MonitorVoiceAsync(
                    voiceConfirmation,
                    completion,
                    lifetime,
                    voiceMonitorLifetime);
                _captureActive = true;
                return new DirectMediaStartResult(
                    HostReady: true,
                    CaptureActive: true);
            }
            catch (Exception startException)
            {
                if (committedOwnedResources)
                {
                    DirectProtocolException failure =
                        startException as DirectProtocolException
                        ?? new DirectProtocolException(
                            "BW_COMPUTER_VOICE_DIRECT_VOICE_START_NOT_CONFIRMED",
                            // Says what actually happened. The old wording
                            // ("shortcut sent, no new session confirmed")
                            // described a step this side no longer performs,
                            // and sent every investigation after a keystroke
                            // that was never pressed.
                            "音频链路建立失败；未能确认通话就绪",
                            retryable: true,
                            innerException: startException);
                    _ = Interlocked.CompareExchange(
                        ref _terminalMediaFailure,
                        failure,
                        null);
                    _completionSource?.TrySetResult(failure);
                    _captureActive = false;
                    Exception? committedCleanupFailure =
                        await StopCommittedStartFailureAsync(failure)
                            .ConfigureAwait(false);
                    if (committedCleanupFailure is not null)
                    {
                        throw CombineStartAndCleanupFailures(
                            startException,
                            committedCleanupFailure);
                    }
                    throw;
                }
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
                            if (pendingCaptureEndpointMuteLease is null)
                            {
                                return Task.CompletedTask;
                            }
                            pendingCaptureEndpointMuteLease.Restore();
                            pendingCaptureEndpointMuteLease = null;
                            return Task.CompletedTask;
                        },
                        () =>
                        {
                            if (pendingAudioRouteLease is null)
                            {
                                return Task.CompletedTask;
                            }
                            PerAppAudioRouteRestoreResult result =
                                pendingAudioRouteLease.Restore();
                            if (!result.Succeeded)
                            {
                                throw AudioRouteRestoreFailure();
                            }
                            pendingAudioRouteLease = null;
                            return Task.CompletedTask;
                        },
                        () =>
                        {
                            pendingAudioRouteLease = null;
                            pendingAudioPolicyBackend?.Dispose();
                            pendingAudioPolicyBackend = null;
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
        // Validation can include native endpoint/route reads. Peer-close may
        // arrive while those checks run, so fence the irreversible shortcut
        // once more immediately before SendInput.
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

    internal static bool CanRestorePerAppAudioRoute(
        bool voiceSettled,
        bool captureLifetimeReleased,
        bool outputSessionReleased,
        bool renderSessionReleased,
        bool outputRouteObserverReleased,
        bool outputPumpReleased,
        bool renderMonitorReleased) =>
        voiceSettled
        && captureLifetimeReleased
        && outputSessionReleased
        && renderSessionReleased
        && outputRouteObserverReleased
        && outputPumpReleased
        && renderMonitorReleased;

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
        || _voiceMonitorLifetime is not null
        || _voiceMonitor is not null
        || _voiceStartBaseline is not null
        || _voiceConfirmation is not null
        || _voiceTarget is not null
        || _captureEndpointMuteLease is not null
        || _audioRouteLease is not null
        || _audioPolicyBackend is not null
        || _ownedTypistLease is not null;

    private async Task StopOwnedVoiceAsync(
        CodexVoiceStartBaseline? baseline,
        CodexVoiceStartConfirmation? confirmation,
        CodexAppTarget? target) =>
        await StopOwnedVoiceAsync(
            VoiceActivity,
            VoiceShortcutSender,
            () => WindowsCodexAppProbe.RequireReady(
                target?.AppKind
                ?? _voiceAppKind
                ?? DirectAppTargets.CodexDesktop),
            baseline,
            confirmation,
            target).ConfigureAwait(false);

    internal static async Task StopOwnedVoiceAsync(
        CodexVoiceActivityController voiceActivity,
        ICodexVoiceShortcutSender voiceShortcutSender,
        Func<CodexAppTarget> currentTargetProvider,
        CodexVoiceStartBaseline? baseline,
        CodexVoiceStartConfirmation? confirmation,
        CodexAppTarget? target)
    {
        ArgumentNullException.ThrowIfNull(voiceActivity);
        ArgumentNullException.ThrowIfNull(voiceShortcutSender);
        ArgumentNullException.ThrowIfNull(currentTargetProvider);
        if (confirmation is null)
        {
            if (baseline is null)
            {
                return;
            }
            CodexVoiceActivitySnapshot current =
                voiceActivity.ReadCurrent();
            if (!current.Active)
            {
                // Either SendInput never activated Voice, or the exact
                // provisional generation has already ended. In both cases a
                // second toggle would be unsafe and unnecessary.
                return;
            }
            // A later microphone ledger transition is not a causal receipt for
            // this SendInput attempt. It may belong to a user action or another
            // Codex microphone feature, so an unconfirmed START must never
            // synthesize bridge ownership and toggle it off.
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_VOICE_OWNERSHIP_UNCONFIRMED",
                "无法证明当前 Codex 语音由本次桥接快捷键开启；不会自动关闭",
                retryable: true);
        }

        CodexVoiceStopPlan plan =
            voiceActivity.PrepareStop(confirmation);
        if (!plan.Snapshot.Active)
        {
            return;
        }
        if (!plan.VoiceGenerationMatches)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_VOICE_REPLACED_CLEANUP_PENDING",
                "桥接器拥有的语音已被另一代会话替换；不会误关新会话",
                retryable: true);
        }
        if (!confirmation.OwnsVoice)
        {
            if (confirmation.ObservedAfterShortcut)
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_VOICE_OWNERSHIP_UNCONFIRMED",
                    "当前 Codex 语音已开启，但未取得本次快捷键的所有权证明；不会自动关闭",
                    retryable: true);
            }
            // A pre-existing session is observable but never bridge-owned.
            return;
        }
        if (target is null)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_VOICE_TARGET_MISSING",
                "Codex 语音目标不存在，无法安全关闭",
                retryable: true);
        }
        CodexAppTarget currentTarget =
            currentTargetProvider();
        if (
            currentTarget.RootProcessId != target.RootProcessId
            || currentTarget.RootProcessStartFileTimeUtc
                != target.RootProcessStartFileTimeUtc
            || currentTarget.AppKind != target.AppKind
        )
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_VOICE_TARGET_CHANGED",
                "Codex 进程代际已变化；不会向新进程发送关闭快捷键",
                retryable: true);
        }

        // Symmetric with start: this side does not close Codex's voice either.
        //
        // Hanging up here used to leave the two ends disagreeing -- the bridge
        // believed the call was over while Codex went on holding the route,
        // and the next dial was refused as ALREADY_ACTIVE_WRONG_ROUTE. Leaving
        // it alone costs nothing: the audio stops flowing the moment the pump
        // does, and the watchdog restores the route regardless.
        _ = voiceShortcutSender;
        _ = plan;
        _ = voiceActivity;
        await Task.CompletedTask.ConfigureAwait(false);
    }

    private async Task<DirectProtocolException?>
        StopOwnedResourcesUnderGateAsync()
    {
        CancellationTokenSource? lifetime = _captureLifetime;
        DirectOutputCaptureSession? output = _outputSession;
        VirtualMicrophoneRenderSession? render = _renderSession;
        IDirectOutputRouteObserver? outputRoute =
            _outputRouteObserver;
        Task? outputPump = _outputPump;
        Task? renderMonitor = _renderMonitor;
        CancellationTokenSource? voiceMonitorLifetime =
            _voiceMonitorLifetime;
        Task? voiceMonitor = _voiceMonitor;
        CodexVoiceStartBaseline? voiceStartBaseline =
            _voiceStartBaseline;
        CodexVoiceStartConfirmation? voiceConfirmation =
            _voiceConfirmation;
        CodexAppTarget? voiceTarget = _voiceTarget;
        DirectCaptureEndpointMuteLease? captureEndpointMuteLease =
            _captureEndpointMuteLease;
        bool captureEndpointMuteRestored =
            captureEndpointMuteLease is null;
        PerAppAudioRouteLease? audioRouteLease = _audioRouteLease;
        IPerAppAudioPolicyBackend? audioPolicyBackend =
            _audioPolicyBackend;
        bool audioRouteRestored = audioRouteLease is null;
        // Codex's own voice no longer gates the route being handed back.
        //
        // This waited for that voice to be closed before restoring, which held
        // while the bridge was the one closing it. Since START/STOP stopped
        // touching the F24 toggle, nothing on this side ever closes it -- so the
        // condition could never be met and the route was never returned. The
        // volume mixer kept every application pinned to the virtual devices long
        // after the call had ended.
        //
        // What actually has to settle is this side's own media: the pump, the
        // sessions, the observers. Those are checked below and are the real
        // reason a route cannot be pulled out from under a live stream. Codex
        // continuing to listen is not a reason to keep the machine rerouted.
        bool voiceSettled = true;
        _ = voiceStartBaseline;
        _ = voiceConfirmation;
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
                voiceMonitorLifetime?.Cancel();
                return Task.CompletedTask;
            },
            async () =>
            {
                if (voiceMonitor is null)
                {
                    return;
                }
                await voiceMonitor.ConfigureAwait(false);
                if (ReferenceEquals(_voiceMonitor, voiceMonitor))
                {
                    _voiceMonitor = null;
                }
            },
            () =>
            {
                if (voiceMonitorLifetime is not null)
                {
                    voiceMonitorLifetime.Dispose();
                    if (ReferenceEquals(
                        _voiceMonitorLifetime,
                        voiceMonitorLifetime))
                    {
                        _voiceMonitorLifetime = null;
                    }
                }
                return Task.CompletedTask;
            },
            async () =>
            {
                if (voiceSettled)
                {
                    return;
                }
                await StopOwnedVoiceAsync(
                    voiceStartBaseline,
                    voiceConfirmation,
                    voiceTarget).ConfigureAwait(false);
                voiceSettled = true;
                if (ReferenceEquals(
                    _voiceStartBaseline,
                    voiceStartBaseline))
                {
                    _voiceStartBaseline = null;
                }
                if (ReferenceEquals(
                    _voiceConfirmation,
                    voiceConfirmation))
                {
                    _voiceConfirmation = null;
                }
            },
            () =>
            {
                if (
                    voiceSettled
                    && ReferenceEquals(_voiceTarget, voiceTarget)
                )
                {
                    _voiceTarget = null;
                }
                return Task.CompletedTask;
            },
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
            () =>
            {
                if (captureEndpointMuteLease is null)
                {
                    captureEndpointMuteRestored = true;
                    return Task.CompletedTask;
                }
                bool mediaSettled = CanRestorePerAppAudioRoute(
                    voiceSettled,
                    _captureLifetime is null,
                    _outputSession is null,
                    _renderSession is null,
                    _outputRouteObserver is null,
                    _outputPump is null,
                    _renderMonitor is null);
                if (!mediaSettled)
                {
                    throw new DirectProtocolException(
                        "BW_COMPUTER_VOICE_DIRECT_MIC_MUTE_RESTORE_DEFERRED",
                        "Codex 语音或媒体尚未安全收口，虚拟麦克风静音状态保留等待重试",
                        retryable: true);
                }
                captureEndpointMuteLease.Restore();
                captureEndpointMuteRestored = true;
                if (ReferenceEquals(
                    _captureEndpointMuteLease,
                    captureEndpointMuteLease))
                {
                    _captureEndpointMuteLease = null;
                }
                return Task.CompletedTask;
            },
            () =>
            {
                if (audioRouteLease is null)
                {
                    audioRouteRestored = true;
                    return Task.CompletedTask;
                }
                bool mediaSettled = CanRestorePerAppAudioRoute(
                    voiceSettled,
                    _captureLifetime is null,
                    _outputSession is null,
                    _renderSession is null,
                    _outputRouteObserver is null,
                    _outputPump is null,
                    _renderMonitor is null);
                if (!mediaSettled)
                {
                    throw new DirectProtocolException(
                        "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_RESTORE_DEFERRED",
                        "Codex 语音或媒体尚未安全收口，音频路由保留等待重试",
                        retryable: true);
                }
                PerAppAudioRouteRestoreResult result =
                    audioRouteLease.Restore();
                if (!result.Succeeded)
                {
                    throw AudioRouteRestoreFailure();
                }
                audioRouteRestored = true;
                if (ReferenceEquals(
                    _audioRouteLease,
                    audioRouteLease))
                {
                    _audioRouteLease = null;
                }
                return Task.CompletedTask;
            },
            () =>
            {
                if (
                    audioPolicyBackend is null
                    || !audioRouteRestored
                    || !captureEndpointMuteRestored
                )
                {
                    return Task.CompletedTask;
                }
                audioPolicyBackend.Dispose();
                if (ReferenceEquals(
                    _audioPolicyBackend,
                    audioPolicyBackend))
                {
                    _audioPolicyBackend = null;
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

        // Cleanup first settles only the exact Voice generation confirmed as
        // bridge-owned, then stops media, restores the leased per-app routes,
        // and releases typist ownership. Pre-existing or replacement Voice
        // generations are never toggled.
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
                    try
                    {
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
                            try
                            {
                                await sendFrameAsync(
                                    new DirectPcmFrame(
                                        track,
                                        checked((uint)chunk.Sequence),
                                        checked((ulong)chunk.TimestampUs),
                                        chunk.Data),
                                    cancellationToken).ConfigureAwait(false);
                            }
                            catch (Exception exception) when (!(
                                exception is OperationCanceledException
                                && cancellationToken.IsCancellationRequested))
                            {
                                throw MediaPumpFailure(
                                    exception,
                                    "media-pump.websocket-send");
                            }
                        }
                    }
                    catch (Exception exception) when (
                        exception is not DirectProtocolException
                        && !(exception is OperationCanceledException
                            && cancellationToken.IsCancellationRequested))
                    {
                        throw MediaPumpFailure(
                            exception,
                            "media-pump.frame");
                    }
                }
                if (
                    queue.IsCompleted
                    && queue.CompletionError is not null
                )
                {
                    throw MediaPumpFailure(
                        queue.CompletionError,
                        "media-pump.capture-source");
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

    private static DirectProtocolException MediaPumpFailure(
        Exception exception,
        string stage) =>
        exception as DirectProtocolException
        ?? new DirectProtocolException(
            "BW_COMPUTER_VOICE_DIRECT_MEDIA_PUMP_FAILED",
            "Windows PCM 传输中断",
            retryable: true,
            innerException: AudioCaptureStageException.From(
                stage,
                exception));

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

    private async Task MonitorVoiceAsync(
        CodexVoiceStartConfirmation confirmation,
        TaskCompletionSource<DirectProtocolException?> completion,
        CancellationTokenSource ownerLifetime,
        CancellationTokenSource monitorLifetime)
    {
        DirectProtocolException? failure =
            await VoiceActivity.MonitorForLocalCloseAsync(
                confirmation,
                CodexVoiceActivityController.MonitorInterval,
                monitorLifetime.Token).ConfigureAwait(false);
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

    private async Task<Exception?> StopCommittedStartFailureAsync(
        DirectProtocolException startFailure)
    {
        // The START failure itself must not be mistaken for a teardown
        // failure. Temporarily remove it while the same complete owner-aware
        // STOP path settles Voice, media, per-app routes and typist.
        _ = Interlocked.Exchange(
            ref _terminalMediaFailure,
            null);
        DirectProtocolException? cleanupFailure;
        try
        {
            cleanupFailure =
                await StopOwnedResourcesUnderGateAsync()
                    .ConfigureAwait(false);
        }
        catch (Exception exception)
        {
            cleanupFailure = exception as DirectProtocolException
                ?? new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_MEDIA_CLEANUP_FAILED",
                    "Windows 音频清理发生未处理异常",
                    retryable: true,
                    exception);
        }
        DirectProtocolException completionFailure =
            cleanupFailure is null
                ? startFailure
                : CombineStartAndCleanupFailures(
                    startFailure,
                    cleanupFailure);
        _ = Interlocked.Exchange(
            ref _terminalMediaFailure,
            completionFailure);
        _completionSource = null;
        _completion = Task.FromResult<DirectProtocolException?>(
            completionFailure);
        return cleanupFailure;
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

    private static DirectProtocolException AudioRouteRestoreFailure() =>
        new(
            "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_RESTORE_INCOMPLETE",
            "Codex 原应用音频路由尚未完整恢复",
            retryable: true);

    private static async Task StopPreparedAsync(
        VirtualMicrophoneRenderSession? render,
        DirectOutputCaptureSession? output)
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
