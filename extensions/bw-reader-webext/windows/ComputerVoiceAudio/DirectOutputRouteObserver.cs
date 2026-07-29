using System.Runtime.InteropServices;
using BwReader.ComputerVoiceAudio.Interop;

namespace BwReader.ComputerVoiceAudio;

internal enum DirectAudioSessionState
{
    Inactive = 0,
    Active = 1,
    Expired = 2,
}

internal readonly record struct DirectOutputRouteSession(
    uint ProcessId,
    DirectAudioSessionState State);

internal sealed class DirectOutputRouteEvidenceTracker
{
    private readonly object _gate = new();
    private readonly Dictionary<
        uint,
        (DirectAudioSessionState State, long Version)> _notifications = [];
    private HashSet<uint> _targetProcessTree = [];
    private Dictionary<uint, DirectAudioSessionState> _sessions = [];
    private uint _targetRootProcessId;
    private long _notificationVersion;

    internal DirectOutputRouteEvidenceTracker(CodexAppTarget target)
    {
        SetTarget(target);
    }

    internal bool Verified
    {
        get
        {
            lock (_gate)
            {
                return _sessions.Any(item =>
                    _targetProcessTree.Contains(item.Key)
                    && IsVerifiedState(item.Value));
            }
        }
    }

    internal long BeginEnumeration()
    {
        lock (_gate)
        {
            return _notificationVersion;
        }
    }

    internal void CompleteEnumeration(
        long notificationVersionAtStart,
        IEnumerable<DirectOutputRouteSession> sessions)
    {
        ArgumentNullException.ThrowIfNull(sessions);
        DirectOutputRouteSession[] snapshot = sessions.ToArray();

        lock (_gate)
        {
            Dictionary<uint, DirectAudioSessionState> enumerated = [];
            foreach (DirectOutputRouteSession session in snapshot)
            {
                if (
                    _targetProcessTree.Contains(session.ProcessId)
                    && IsVerifiedState(session.State)
                )
                {
                    enumerated[session.ProcessId] = session.State;
                }
            }
            foreach (
                (
                    uint processId,
                    (DirectAudioSessionState state, long version)
                ) in _notifications
            )
            {
                if (
                    version <= notificationVersionAtStart
                    || !_targetProcessTree.Contains(processId)
                )
                {
                    continue;
                }
                if (IsVerifiedState(state))
                {
                    enumerated[processId] = state;
                }
                else
                {
                    enumerated.Remove(processId);
                }
            }
            _sessions = enumerated;
        }
    }

    internal void ObserveNotification(DirectOutputRouteSession session)
    {
        lock (_gate)
        {
            if (!_targetProcessTree.Contains(session.ProcessId))
            {
                return;
            }
            long version = checked(++_notificationVersion);
            _notifications[session.ProcessId] =
                (session.State, version);
            if (IsVerifiedState(session.State))
            {
                _sessions[session.ProcessId] = session.State;
            }
            else
            {
                _sessions.Remove(session.ProcessId);
            }
        }
    }

    internal void SetTarget(CodexAppTarget target)
    {
        ArgumentNullException.ThrowIfNull(target);
        if (
            target.RootProcessId == 0
            || !target.ProcessTree.Contains(target.RootProcessId)
        )
        {
            throw new ArgumentException(
                "Codex target process tree is invalid",
                nameof(target));
        }
        lock (_gate)
        {
            if (
                _targetRootProcessId == target.RootProcessId
                && _targetProcessTree.SetEquals(target.ProcessTree)
            )
            {
                return;
            }
            _targetRootProcessId = target.RootProcessId;
            _targetProcessTree =
                new HashSet<uint>(target.ProcessTree);
            ClearEvidenceUnderGate();
        }
    }

    internal void Clear()
    {
        lock (_gate)
        {
            _targetRootProcessId = 0;
            _targetProcessTree = [];
            ClearEvidenceUnderGate();
        }
    }

    private void ClearEvidenceUnderGate()
    {
        _sessions = [];
        _notifications.Clear();
        _notificationVersion = 0;
    }

    private static bool IsVerifiedState(
        DirectAudioSessionState state) =>
        state == DirectAudioSessionState.Active;
}

internal interface IDirectOutputRouteObserver : IDisposable
{
    string EndpointId { get; }

    uint TargetRootProcessId { get; }

    bool Verified { get; }
}

internal interface IDirectOutputRouteObserverFactory
{
    IDirectOutputRouteObserver Create(
        string endpointId,
        CodexAppTarget target);
}

internal sealed class UnverifiedDirectOutputRouteObserver :
    IDirectOutputRouteObserver
{
    internal UnverifiedDirectOutputRouteObserver(
        string endpointId,
        uint targetRootProcessId)
    {
        EndpointId = endpointId;
        TargetRootProcessId = targetRootProcessId;
    }

    public string EndpointId { get; }

    public uint TargetRootProcessId { get; }

    public bool Verified => false;

    public void Dispose()
    {
    }
}

internal sealed class NativeDirectOutputRouteObserverFactory :
    IDirectOutputRouteObserverFactory
{
    public IDirectOutputRouteObserver Create(
        string endpointId,
        CodexAppTarget target)
    {
        try
        {
            return NativeDirectOutputRouteObserver.Start(
                endpointId,
                target,
                WindowsCodexAppProbe.Probe);
        }
        catch
        {
            // Route evidence is fail closed, while media START is intentionally
            // independent of whether a pre-shortcut audio session exists.
            return new UnverifiedDirectOutputRouteObserver(
                endpointId,
                target.RootProcessId);
        }
    }
}

internal sealed class NativeDirectOutputRouteObserver :
    IDirectOutputRouteObserver
{
    private static readonly TimeSpan InitializationTimeout =
        TimeSpan.FromSeconds(3);
    private static readonly TimeSpan RefreshInterval =
        TimeSpan.FromMilliseconds(250);

    private readonly DirectOutputRouteEvidenceTracker _tracker;
    private readonly Func<CodexAppProbeState> _targetProbe;
    private readonly object _sessionControlsGate = new();
    private readonly Dictionary<
        nint,
        IAudioSessionControl2ForRoute> _sessionControls = [];
    private readonly ManualResetEventSlim _initialized = new(false);
    private readonly AutoResetEvent _refreshRequested = new(false);
    private readonly ManualResetEvent _stopRequested = new(false);
    private readonly Thread _thread;
    private Exception? _initializationFailure;
    private int _disposeRequested;
    private int _handlesDisposed;

    private NativeDirectOutputRouteObserver(
        string endpointId,
        CodexAppTarget target,
        Func<CodexAppProbeState> targetProbe)
    {
        if (
            string.IsNullOrWhiteSpace(endpointId)
            || endpointId.Length
                > VirtualMicrophoneRenderRequest.MaximumEndpointIdLength
            || endpointId.Any(char.IsControl)
        )
        {
            throw new ArgumentException(
                "Output route endpoint is invalid",
                nameof(endpointId));
        }
        ArgumentNullException.ThrowIfNull(target);
        ArgumentNullException.ThrowIfNull(targetProbe);
        EndpointId = endpointId;
        TargetRootProcessId = target.RootProcessId;
        _tracker = new DirectOutputRouteEvidenceTracker(target);
        _targetProbe = targetProbe;
        _thread = new Thread(Run)
        {
            IsBackground = true,
            Name = "BW direct output-route observer",
        };
    }

    public string EndpointId { get; }

    public uint TargetRootProcessId { get; }

    public bool Verified =>
        Volatile.Read(ref _disposeRequested) == 0
        && _initializationFailure is null
        && _tracker.Verified;

    internal static NativeDirectOutputRouteObserver Start(
        string endpointId,
        CodexAppTarget target,
        Func<CodexAppProbeState> targetProbe)
    {
        NativeDirectOutputRouteObserver observer = new(
            endpointId,
            target,
            targetProbe);
        observer._thread.Start();
        if (!observer._initialized.Wait(InitializationTimeout))
        {
            observer.Dispose();
            throw new TimeoutException(
                "BW_COMPUTER_VOICE_DIRECT_OUTPUT_ROUTE_PROBE_TIMEOUT");
        }
        if (observer._initializationFailure is not null)
        {
            Exception failure = observer._initializationFailure;
            observer.Dispose();
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_OUTPUT_ROUTE_UNVERIFIED",
                "无法验证 Codex 到虚拟扬声器的 Windows 音频会话",
                retryable: true,
                innerException: failure);
        }
        return observer;
    }

    private void Run()
    {
        object? enumeratorObject = null;
        IMMDevice? endpoint = null;
        object? sessionManagerObject = null;
        IAudioSessionManager2ForRoute? sessionManager = null;
        DirectAudioSessionNotification? notification = null;
        ComMtaLease? apartment = null;
        bool registered = false;
        try
        {
            if (!OperatingSystem.IsWindows())
            {
                throw new PlatformNotSupportedException(
                    "BW_COMPUTER_VOICE_AUDIO_WINDOWS_REQUIRED");
            }
            apartment = ComMtaLease.Enter();
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
                enumerator.GetDevice(EndpointId, out endpoint),
                "output-route.get-explicit-render-device");
            if (endpoint is null)
            {
                throw new AudioCaptureStageException(
                    "output-route.get-explicit-render-device",
                    unchecked((int)0x80070490));
            }
            RequireSucceeded(
                endpoint.GetState(out DeviceState deviceState),
                "output-route.get-render-device-state");
            if ((deviceState & DeviceState.Active) == 0)
            {
                throw new AudioCaptureStageException(
                    "output-route.render-device-inactive",
                    unchecked((int)0x88890004));
            }
            if (endpoint is not IMMEndpoint direction)
            {
                throw new AudioCaptureStageException(
                    "output-route.query-render-data-flow",
                    unchecked((int)0x80004002));
            }
            RequireSucceeded(
                direction.GetDataFlow(out AudioDataFlow dataFlow),
                "output-route.get-render-data-flow");
            if (dataFlow != AudioDataFlow.Render)
            {
                throw new AudioCaptureStageException(
                    "output-route.render-data-flow-mismatch",
                    unchecked((int)0x80070057));
            }

            Guid sessionManagerId =
                DirectOutputRouteInterop.IidIAudioSessionManager2;
            RequireSucceeded(
                endpoint.Activate(
                    ref sessionManagerId,
                    ComClassContext.All,
                    activationParameters: 0,
                    out sessionManagerObject),
                "output-route.activate-session-manager");
            sessionManager = sessionManagerObject
                as IAudioSessionManager2ForRoute
                ?? throw new AudioCaptureStageException(
                    "output-route.query-session-manager",
                    unchecked((int)0x80004002));
            notification = new DirectAudioSessionNotification(
                ObserveCreatedSession,
                _refreshRequested);

            // Microsoft requires registration before the initial enumeration
            // so a session created in the hand-off cannot be lost.
            RequireSucceeded(
                sessionManager.RegisterSessionNotification(notification),
                "output-route.register-session-notification");
            registered = true;
            EnumerateExistingSessions(sessionManager);
            Refresh();
            _initialized.Set();

            WaitHandle[] waits =
            [
                _stopRequested,
                _refreshRequested,
            ];
            while (
                WaitHandle.WaitAny(
                    waits,
                    RefreshInterval)
                    != 0
            )
            {
                Refresh();
            }
        }
        catch (Exception exception)
        {
            _initializationFailure ??= exception;
            _tracker.Clear();
            _initialized.Set();
        }
        finally
        {
            try
            {
                if (registered && sessionManager is not null
                    && notification is not null)
                {
                    try
                    {
                        _ = sessionManager.UnregisterSessionNotification(
                            notification);
                    }
                    catch
                    {
                    }
                }
            }
            finally
            {
                try
                {
                    ReleaseComObject(sessionManagerObject);
                }
                catch
                {
                }
                try
                {
                    ReleaseComObject(endpoint);
                }
                catch
                {
                }
                try
                {
                    ReleaseComObject(enumeratorObject);
                }
                catch
                {
                }
                try
                {
                    ReleaseRetainedSessionControls();
                }
                catch
                {
                }
                _tracker.Clear();
                // Notification cleanup and every COM release above must run
                // before this observer thread leaves its MTA apartment.
                apartment?.Dispose();
                _initialized.Set();
            }
        }
    }

    private void EnumerateExistingSessions(
        IAudioSessionManager2ForRoute sessionManager)
    {
        IAudioSessionEnumeratorForRoute? sessionEnumerator = null;
        try
        {
            RequireSucceeded(
                sessionManager.GetSessionEnumerator(
                    out sessionEnumerator),
                "output-route.get-session-enumerator");
            RequireSucceeded(
                sessionEnumerator.GetCount(out int count),
                "output-route.get-session-count");
            if (count < 0 || count > 16_384)
            {
                throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_DIRECT_OUTPUT_ROUTE_SESSION_COUNT_INVALID");
            }
            for (int index = 0; index < count; index++)
            {
                IAudioSessionControlForRoute? control = null;
                try
                {
                    RequireSucceeded(
                        sessionEnumerator.GetSession(
                            index,
                            out control),
                        "output-route.get-session");
                    RetainSessionControl(control);
                }
                finally
                {
                    // RetainSessionControl owns an explicit IUnknown reference.
                    // The callback/enumerator RCW itself is left to normal RCW
                    // lifetime management so a shared RCW is never force
                    // released underneath the retained observer entry.
                }
            }
        }
        finally
        {
            ReleaseComObject(sessionEnumerator);
        }
    }

    private void Refresh()
    {
        RefreshTarget();
        long notificationVersion = _tracker.BeginEnumeration();
        List<DirectOutputRouteSession> sessions = [];
        KeyValuePair<nint, IAudioSessionControl2ForRoute>[] controls;
        lock (_sessionControlsGate)
        {
            controls = _sessionControls.ToArray();
        }
        foreach (
            KeyValuePair<nint, IAudioSessionControl2ForRoute> item
            in controls
        )
        {
            try
            {
                if (
                    TryReadSession(
                        item.Value,
                        out DirectOutputRouteSession session)
                    && session.State
                        != DirectAudioSessionState.Expired
                )
                {
                    sessions.Add(session);
                    continue;
                }
            }
            catch
            {
            }
            RemoveRetainedSessionControl(item.Key);
        }
        _tracker.CompleteEnumeration(
            notificationVersion,
            sessions);
    }

    private void RefreshTarget()
    {
        CodexAppProbeState state;
        try
        {
            state = _targetProbe();
        }
        catch
        {
            _tracker.Clear();
            return;
        }
        if (
            state.ReadyTarget is CodexAppTarget target
            && target.RootProcessId == TargetRootProcessId
        )
        {
            _tracker.SetTarget(target);
            return;
        }
        _tracker.Clear();
    }

    private void ObserveCreatedSession(
        IAudioSessionControlForRoute control)
    {
        try
        {
            if (
                TryReadSession(
                    control,
                    out DirectOutputRouteSession session)
            )
            {
                RetainSessionControl(control);
                _tracker.ObserveNotification(session);
            }
        }
        catch
        {
            _tracker.Clear();
        }
    }

    private void RetainSessionControl(
        IAudioSessionControlForRoute? control)
    {
        if (
            control is not IAudioSessionControl2ForRoute control2
            || !OperatingSystem.IsWindows()
        )
        {
            return;
        }
        nint identity = Marshal.GetIUnknownForObject(control2);
        bool retained = false;
        lock (_sessionControlsGate)
        {
            if (
                Volatile.Read(ref _disposeRequested) == 0
                && !_sessionControls.ContainsKey(identity)
                && _sessionControls.Count < 16_384
            )
            {
                _sessionControls.Add(identity, control2);
                retained = true;
            }
        }
        if (!retained)
        {
            _ = Marshal.Release(identity);
        }
    }

    private void RemoveRetainedSessionControl(nint identity)
    {
        bool removed;
        lock (_sessionControlsGate)
        {
            removed = _sessionControls.Remove(identity);
        }
        if (removed)
        {
            _ = Marshal.Release(identity);
        }
    }

    private void ReleaseRetainedSessionControls()
    {
        nint[] identities;
        lock (_sessionControlsGate)
        {
            identities = _sessionControls.Keys.ToArray();
            _sessionControls.Clear();
        }
        foreach (nint identity in identities)
        {
            _ = Marshal.Release(identity);
        }
    }

    private static bool TryReadSession(
        object? control,
        out DirectOutputRouteSession session)
    {
        session = default;
        if (control is not IAudioSessionControl2ForRoute control2)
        {
            return false;
        }
        RequireSucceeded(
            control2.GetProcessId(out uint processId),
            "output-route.get-session-process-id");
        RequireSucceeded(
            control2.GetState(out DirectAudioSessionState state),
            "output-route.get-session-state");
        if (
            processId == 0
            || state is < DirectAudioSessionState.Inactive
                or > DirectAudioSessionState.Expired
        )
        {
            return false;
        }
        session = new DirectOutputRouteSession(processId, state);
        return true;
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

    public void Dispose()
    {
        if (
            Interlocked.Exchange(ref _disposeRequested, 1) == 0
        )
        {
            _tracker.Clear();
            _stopRequested.Set();
            _refreshRequested.Set();
        }
        bool joined = !_thread.IsAlive;
        if (
            _thread.IsAlive
            && Thread.CurrentThread != _thread
        )
        {
            joined = _thread.Join(TimeSpan.FromSeconds(5));
        }
        if (!joined)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_OUTPUT_ROUTE_OBSERVER_STOP_TIMEOUT",
                "Windows 音频路由观察器停止超时",
                retryable: true);
        }
        if (Interlocked.Exchange(ref _handlesDisposed, 1) != 0)
        {
            return;
        }
        _initialized.Dispose();
        _refreshRequested.Dispose();
        _stopRequested.Dispose();
    }
}

[ComVisible(true)]
[ClassInterface(ClassInterfaceType.None)]
internal sealed class DirectAudioSessionNotification :
    IAudioSessionNotificationForRoute
{
    private readonly Action<IAudioSessionControlForRoute> _observe;
    private readonly EventWaitHandle _refreshRequested;

    internal DirectAudioSessionNotification(
        Action<IAudioSessionControlForRoute> observe,
        EventWaitHandle refreshRequested)
    {
        _observe = observe;
        _refreshRequested = refreshRequested;
    }

    public int OnSessionCreated(
        IAudioSessionControlForRoute newSession)
    {
        try
        {
            _observe(newSession);
            _refreshRequested.Set();
        }
        catch
        {
        }
        return ProcessLoopbackInterop.Succeeded;
    }
}

internal static class DirectOutputRouteProbe
{
    internal const string Contract =
        "reader-computer-voice-direct-output-route-probe/1";
    internal const string UnverifiedReason =
        "BW_COMPUTER_VOICE_DIRECT_OUTPUT_ROUTE_UNVERIFIED";

    internal static object Run(
        DirectBridgeConfig config,
        IDirectOutputRouteObserverFactory? observerFactory = null,
        Func<CodexAppProbeState>? targetProbe = null)
    {
        ArgumentNullException.ThrowIfNull(config);
        observerFactory ??=
            new NativeDirectOutputRouteObserverFactory();
        targetProbe ??= WindowsCodexAppProbe.Probe;
        bool verified = false;
        try
        {
            CodexAppProbeState state = targetProbe();
            if (state.ReadyTarget is CodexAppTarget target)
            {
                using IDirectOutputRouteObserver observer =
                    observerFactory.Create(
                        config.VirtualSpeakerRenderEndpointId,
                        target);
                verified = observer.Verified;
            }
        }
        catch
        {
            verified = false;
        }
        return new
        {
            contract = Contract,
            ok = true,
            verified,
            reason = verified ? null : UnverifiedReason,
            captureStarted = false,
            shortcutSent = false,
            appLaunched = false,
        };
    }
}

internal static class DirectOutputRouteInterop
{
    internal static readonly Guid IidIAudioSessionManager2 =
        new("BFA971F1-4D5E-40BB-935E-967039BFBEA4");
}

[ComImport]
[Guid("BFA971F1-4D5E-40BB-935E-967039BFBEA4")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IAudioSessionManager2ForRoute
{
    [PreserveSig]
    int GetAudioSessionControl(
        nint sessionGuid,
        uint streamFlags,
        [MarshalAs(UnmanagedType.Interface)] out object sessionControl);

    [PreserveSig]
    int GetSimpleAudioVolume(
        nint sessionGuid,
        uint streamFlags,
        [MarshalAs(UnmanagedType.Interface)] out object audioVolume);

    [PreserveSig]
    int GetSessionEnumerator(
        [MarshalAs(UnmanagedType.Interface)]
        out IAudioSessionEnumeratorForRoute sessionEnumerator);

    [PreserveSig]
    int RegisterSessionNotification(
        [MarshalAs(UnmanagedType.Interface)]
        IAudioSessionNotificationForRoute sessionNotification);

    [PreserveSig]
    int UnregisterSessionNotification(
        [MarshalAs(UnmanagedType.Interface)]
        IAudioSessionNotificationForRoute sessionNotification);

    [PreserveSig]
    int RegisterDuckNotification(
        [MarshalAs(UnmanagedType.LPWStr)] string sessionId,
        nint duckNotification);

    [PreserveSig]
    int UnregisterDuckNotification(nint duckNotification);
}

[ComImport]
[Guid("E2F5BB11-0570-40CA-ACDD-3AA01277DEE8")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IAudioSessionEnumeratorForRoute
{
    [PreserveSig]
    int GetCount(out int sessionCount);

    [PreserveSig]
    int GetSession(
        int sessionIndex,
        [MarshalAs(UnmanagedType.Interface)]
        out IAudioSessionControlForRoute sessionControl);
}

[ComImport]
[Guid("F4B1A599-7266-4319-A8CA-E70ACB11E8CD")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IAudioSessionControlForRoute
{
    [PreserveSig]
    int GetState(out DirectAudioSessionState state);

    [PreserveSig]
    int GetDisplayName(out nint displayName);

    [PreserveSig]
    int SetDisplayName(
        [MarshalAs(UnmanagedType.LPWStr)] string displayName,
        ref Guid eventContext);

    [PreserveSig]
    int GetIconPath(out nint iconPath);

    [PreserveSig]
    int SetIconPath(
        [MarshalAs(UnmanagedType.LPWStr)] string iconPath,
        ref Guid eventContext);

    [PreserveSig]
    int GetGroupingParam(out Guid groupingId);

    [PreserveSig]
    int SetGroupingParam(
        ref Guid groupingId,
        ref Guid eventContext);

    [PreserveSig]
    int RegisterAudioSessionNotification(nint client);

    [PreserveSig]
    int UnregisterAudioSessionNotification(nint client);
}

[ComImport]
[Guid("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IAudioSessionControl2ForRoute
{
    [PreserveSig]
    int GetState(out DirectAudioSessionState state);

    [PreserveSig]
    int GetDisplayName(out nint displayName);

    [PreserveSig]
    int SetDisplayName(
        [MarshalAs(UnmanagedType.LPWStr)] string displayName,
        ref Guid eventContext);

    [PreserveSig]
    int GetIconPath(out nint iconPath);

    [PreserveSig]
    int SetIconPath(
        [MarshalAs(UnmanagedType.LPWStr)] string iconPath,
        ref Guid eventContext);

    [PreserveSig]
    int GetGroupingParam(out Guid groupingId);

    [PreserveSig]
    int SetGroupingParam(
        ref Guid groupingId,
        ref Guid eventContext);

    [PreserveSig]
    int RegisterAudioSessionNotification(nint client);

    [PreserveSig]
    int UnregisterAudioSessionNotification(nint client);

    [PreserveSig]
    int GetSessionIdentifier(out nint sessionIdentifier);

    [PreserveSig]
    int GetSessionInstanceIdentifier(out nint sessionInstanceIdentifier);

    [PreserveSig]
    int GetProcessId(out uint processId);

    [PreserveSig]
    int IsSystemSoundsSession();

    [PreserveSig]
    int SetDuckingPreference(
        [MarshalAs(UnmanagedType.Bool)] bool optOut);
}

[ComImport]
[Guid("641DD20B-4D41-49CC-ABA3-174B9477BB08")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IAudioSessionNotificationForRoute
{
    [PreserveSig]
    int OnSessionCreated(
        [MarshalAs(UnmanagedType.Interface)]
        IAudioSessionControlForRoute newSession);
}
