using System.Runtime.InteropServices;
using BwReader.ComputerVoiceAudio.Interop;

namespace BwReader.ComputerVoiceAudio;

internal sealed record MicCaptureRequest
{
    internal const int MaximumEndpointIdLength = 1024;

    private MicCaptureRequest(string endpointId)
    {
        EndpointId = endpointId;
    }

    internal string EndpointId { get; }

    internal static MicCaptureRequest Create(string? endpointId)
    {
        if (string.IsNullOrWhiteSpace(endpointId))
        {
            throw new ArgumentException(
                "BW_COMPUTER_VOICE_AUDIO_MIC_ENDPOINT_ID_REQUIRED",
                nameof(endpointId));
        }

        if (endpointId.Length > MaximumEndpointIdLength)
        {
            throw new ArgumentException(
                "BW_COMPUTER_VOICE_AUDIO_MIC_ENDPOINT_ID_TOO_LONG",
                nameof(endpointId));
        }

        if (endpointId.Any(char.IsControl))
        {
            throw new ArgumentException(
                "BW_COMPUTER_VOICE_AUDIO_MIC_ENDPOINT_ID_CONTROL_CHARACTER",
                nameof(endpointId));
        }

        // Preserve the exact endpoint ID. Trimming or case folding would turn
        // an explicit user selection into a different implicit lookup.
        return new MicCaptureRequest(endpointId);
    }
}

internal interface IExplicitMicrophoneAudioClientLeaseFactory
{
    INativeAudioClientLease OpenExact(MicCaptureRequest request);
}

internal sealed class NativeExplicitMicrophoneAudioClientLeaseFactory :
    IExplicitMicrophoneAudioClientLeaseFactory
{
    public INativeAudioClientLease OpenExact(MicCaptureRequest request)
    {
        ArgumentNullException.ThrowIfNull(request);
        if (!OperatingSystem.IsWindows())
        {
            throw new PlatformNotSupportedException(
                "BW_COMPUTER_VOICE_AUDIO_WINDOWS_REQUIRED");
        }

        object? enumeratorObject = null;
        IMMDevice? endpoint = null;
        object? audioClientObject = null;
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
                enumerator.GetDevice(request.EndpointId, out endpoint),
                "microphone.get-explicit-device");
            if (endpoint is null)
            {
                throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_AUDIO_MIC_DEVICE_MISSING");
            }

            Guid audioClientId = ExplicitMicrophoneInterop.IidIAudioClient;
            RequireSucceeded(
                endpoint.Activate(
                    ref audioClientId,
                    ComClassContext.All,
                    activationParameters: 0,
                    out audioClientObject),
                "microphone.activate-audio-client");
            if (audioClientObject is not IAudioClient audioClient)
            {
                throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_AUDIO_MIC_AUDIO_CLIENT_INVALID");
            }

            ExplicitMicrophoneAudioClientLease lease = new(
                request.EndpointId,
                enumerator,
                endpoint,
                audioClient);
            enumeratorObject = null;
            endpoint = null;
            audioClientObject = null;
            return lease;
        }
        catch
        {
            ReleaseComObject(audioClientObject);
            ReleaseComObject(endpoint);
            ReleaseComObject(enumeratorObject);
            throw;
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
        if (OperatingSystem.IsWindows()
            && value is not null
            && Marshal.IsComObject(value))
        {
            Marshal.FinalReleaseComObject(value);
        }
    }
}

internal sealed class ExplicitMicrophoneAudioClientLease :
    INativeAudioClientLease
{
    private IMMDeviceEnumerator? _enumerator;
    private IMMDevice? _endpoint;
    private IAudioClient? _audioClient;

    internal ExplicitMicrophoneAudioClientLease(
        string endpointId,
        IMMDeviceEnumerator enumerator,
        IMMDevice endpoint,
        IAudioClient audioClient)
    {
        EndpointId = endpointId;
        _enumerator = enumerator;
        _endpoint = endpoint;
        _audioClient = audioClient;
    }

    internal string EndpointId { get; }

    public IAudioClient AudioClient =>
        _audioClient
        ?? throw new ObjectDisposedException(
            nameof(ExplicitMicrophoneAudioClientLease));

    public void Dispose()
    {
        ReleaseComObject(Interlocked.Exchange(ref _audioClient, null));
        ReleaseComObject(Interlocked.Exchange(ref _endpoint, null));
        ReleaseComObject(Interlocked.Exchange(ref _enumerator, null));
    }

    private static void ReleaseComObject(object? value)
    {
        if (OperatingSystem.IsWindows()
            && value is not null
            && Marshal.IsComObject(value))
        {
            Marshal.FinalReleaseComObject(value);
        }
    }
}

internal interface IExplicitMicrophoneCaptureRuntimeFactory
{
    IProcessLoopbackCaptureRuntime Create(
        MicCaptureRequest request,
        CancellationToken cancellationToken);
}

internal sealed class NativeExplicitMicrophoneCaptureRuntimeFactory :
    IExplicitMicrophoneCaptureRuntimeFactory
{
    private readonly IExplicitMicrophoneAudioClientLeaseFactory _leaseFactory;

    internal NativeExplicitMicrophoneCaptureRuntimeFactory()
        : this(new NativeExplicitMicrophoneAudioClientLeaseFactory())
    {
    }

    internal NativeExplicitMicrophoneCaptureRuntimeFactory(
        IExplicitMicrophoneAudioClientLeaseFactory leaseFactory)
    {
        ArgumentNullException.ThrowIfNull(leaseFactory);
        _leaseFactory = leaseFactory;
    }

    public IProcessLoopbackCaptureRuntime Create(
        MicCaptureRequest request,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        INativeAudioClientLease lease = _leaseFactory.OpenExact(request);
        try
        {
            cancellationToken.ThrowIfCancellationRequested();
            return new ExplicitMicrophoneCaptureRuntime(lease);
        }
        catch
        {
            lease.Dispose();
            throw;
        }
    }
}

internal sealed class ExplicitMicrophoneCaptureRuntime :
    IProcessLoopbackCaptureRuntime
{
    private readonly SharedEventDrivenPcmRuntime _inner;

    internal ExplicitMicrophoneCaptureRuntime(
        INativeAudioClientLease lease)
    {
        _inner = new SharedEventDrivenPcmRuntime(
            lease,
            AudioClientStreamFlags.EventCallback,
            "microphone");
    }

    public PcmAudioFormat Initialize(EventWaitHandle audioReadyEvent) =>
        _inner.Initialize(audioReadyEvent);

    public void Start() => _inner.Start();

    public int Drain(
        IBoundedPcmSink sink,
        CaptureSessionOptions options) =>
        _inner.Drain(sink, options);

    public void Stop() => _inner.Stop();

    public void Dispose() => _inner.Dispose();
}

internal sealed class ExplicitMicrophoneCaptureSession :
    IDisposable,
    IAsyncDisposable
{
    private readonly DedicatedMicrophoneCaptureSession _inner;

    private ExplicitMicrophoneCaptureSession(
        MicCaptureRequest request,
        IBoundedPcmSink sink,
        CaptureSessionOptions options,
        IExplicitMicrophoneCaptureRuntimeFactory runtimeFactory)
    {
        ArgumentNullException.ThrowIfNull(request);
        _inner = new DedicatedMicrophoneCaptureSession(
            request,
            sink,
            options,
            runtimeFactory);
    }

    internal MicCaptureRequest Request => _inner.Request;

    internal CaptureSessionState State => _inner.State;

    internal Task Completion => _inner.Completion;

    internal PcmAudioFormat? Format => _inner.Format;

    internal static ExplicitMicrophoneCaptureSession Prepare(
        MicCaptureRequest request,
        IBoundedPcmSink sink,
        CaptureSessionOptions? options = null) =>
        new(
            request,
            sink,
            options ?? CaptureSessionOptions.Default,
            new NativeExplicitMicrophoneCaptureRuntimeFactory());

    internal static ExplicitMicrophoneCaptureSession PrepareForTest(
        MicCaptureRequest request,
        IBoundedPcmSink sink,
        IExplicitMicrophoneCaptureRuntimeFactory runtimeFactory,
        CaptureSessionOptions? options = null) =>
        new(
            request,
            sink,
            options ?? CaptureSessionOptions.Default,
            runtimeFactory);

    internal Task StartAsync(
        CancellationToken cancellationToken = default) =>
        _inner.StartAsync(cancellationToken);

    internal Task StopAsync(
        CancellationToken cancellationToken = default) =>
        _inner.StopAsync(cancellationToken);

    public void Dispose() => _inner.Dispose();

    public ValueTask DisposeAsync() => _inner.DisposeAsync();
}

// This session is deliberately microphone-specific so the source request
// cannot be confused with a process PID. It nevertheless shares the exact
// runtime, packet pump and capture-session state contract used by process
// loopback.
internal sealed class DedicatedMicrophoneCaptureSession :
    IDisposable,
    IAsyncDisposable
{
    private readonly object _gate = new();
    private readonly IBoundedPcmSink _sink;
    private readonly CaptureSessionOptions _options;
    private readonly IExplicitMicrophoneCaptureRuntimeFactory _runtimeFactory;
    private readonly CancellationTokenSource _lifetime = new();
    private readonly EventWaitHandle _audioReady =
        new(false, EventResetMode.AutoReset);
    private readonly TaskCompletionSource _started =
        new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly TaskCompletionSource _completed =
        new(TaskCreationOptions.RunContinuationsAsynchronously);
    private CaptureSessionState _state = CaptureSessionState.Prepared;
    private PcmAudioFormat? _format;
    private int _disposed;

    internal DedicatedMicrophoneCaptureSession(
        MicCaptureRequest request,
        IBoundedPcmSink sink,
        CaptureSessionOptions options,
        IExplicitMicrophoneCaptureRuntimeFactory runtimeFactory)
    {
        ArgumentNullException.ThrowIfNull(request);
        ArgumentNullException.ThrowIfNull(sink);
        ArgumentNullException.ThrowIfNull(runtimeFactory);
        options.Validate();

        Request = request;
        _sink = sink;
        _options = options;
        _runtimeFactory = runtimeFactory;
    }

    internal MicCaptureRequest Request { get; }

    internal CaptureSessionState State
    {
        get
        {
            lock (_gate)
            {
                return _state;
            }
        }
    }

    internal Task Completion => _completed.Task;

    internal PcmAudioFormat? Format
    {
        get
        {
            lock (_gate)
            {
                return _format;
            }
        }
    }

    internal async Task StartAsync(
        CancellationToken cancellationToken = default)
    {
        ThrowIfDisposed();

        Thread captureThread;
        lock (_gate)
        {
            if (_state != CaptureSessionState.Prepared)
            {
                throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_AUDIO_SESSION_ALREADY_STARTED");
            }

            _state = CaptureSessionState.Starting;
            captureThread = new Thread(CaptureThreadMain)
            {
                IsBackground = true,
                Name = "BW explicit microphone",
            };
        }

        try
        {
            if (OperatingSystem.IsWindows())
            {
                captureThread.SetApartmentState(ApartmentState.MTA);
            }

            captureThread.Start();
        }
        catch (Exception exception)
        {
            lock (_gate)
            {
                _state = CaptureSessionState.Faulted;
            }

            CompleteSinkAndTasks(exception);
            throw;
        }

        try
        {
            await _started.Task
                .WaitAsync(cancellationToken)
                .ConfigureAwait(false);
        }
        catch
        {
            RequestStop();
            try
            {
                await _completed.Task.ConfigureAwait(false);
            }
            catch
            {
                // Preserve the startup exception seen by the caller.
            }

            throw;
        }
    }

    internal async Task StopAsync(
        CancellationToken cancellationToken = default)
    {
        bool completeWithoutThread = false;
        lock (_gate)
        {
            if (_state == CaptureSessionState.Prepared)
            {
                _state = CaptureSessionState.Stopped;
                completeWithoutThread = true;
            }
            else if (_state is CaptureSessionState.Starting
                or CaptureSessionState.Running)
            {
                _state = CaptureSessionState.Stopping;
            }
        }

        if (completeWithoutThread)
        {
            CompleteSinkAndTasks(null);
            await _completed.Task
                .WaitAsync(cancellationToken)
                .ConfigureAwait(false);
            return;
        }

        RequestStop();
        await _completed.Task
            .WaitAsync(cancellationToken)
            .ConfigureAwait(false);
    }

    private void CaptureThreadMain()
    {
        IProcessLoopbackCaptureRuntime? runtime = null;
        Exception? terminalError = null;
        bool initialized = false;

        try
        {
            runtime = _runtimeFactory.Create(Request, _lifetime.Token);
            PcmAudioFormat format = runtime.Initialize(_audioReady);
            initialized = true;
            lock (_gate)
            {
                _format = format;
            }
            runtime.Start();

            if (_lifetime.IsCancellationRequested)
            {
                throw new OperationCanceledException(_lifetime.Token);
            }

            lock (_gate)
            {
                if (_state == CaptureSessionState.Starting)
                {
                    _state = CaptureSessionState.Running;
                }
            }

            _started.TrySetResult();
            using CancellationTokenRegistration wakeOnStop =
                _lifetime.Token.Register(
                    static state => ((EventWaitHandle)state!).Set(),
                    _audioReady);

            while (!_lifetime.IsCancellationRequested)
            {
                _audioReady.WaitOne();
                if (_lifetime.IsCancellationRequested)
                {
                    break;
                }

                _ = runtime.Drain(_sink, _options);
            }
        }
        catch (OperationCanceledException)
            when (_lifetime.IsCancellationRequested)
        {
            _started.TrySetCanceled(_lifetime.Token);
        }
        catch (Exception exception)
        {
            terminalError = exception;
            _started.TrySetException(exception);
        }
        finally
        {
            if (runtime is not null && initialized)
            {
                try
                {
                    runtime.Stop();
                }
                catch (Exception stopError)
                {
                    terminalError = CombineErrors(terminalError, stopError);
                }
            }

            if (runtime is not null)
            {
                try
                {
                    runtime.Dispose();
                }
                catch (Exception disposeError)
                {
                    terminalError = CombineErrors(terminalError, disposeError);
                }
            }

            try
            {
                _sink.Complete(terminalError);
            }
            catch (Exception sinkError)
            {
                terminalError = CombineErrors(terminalError, sinkError);
            }

            lock (_gate)
            {
                _state = terminalError is null
                    ? CaptureSessionState.Stopped
                    : CaptureSessionState.Faulted;
            }

            if (terminalError is null)
            {
                _completed.TrySetResult();
            }
            else
            {
                _completed.TrySetException(terminalError);
            }
        }
    }

    private static Exception CombineErrors(
        Exception? first,
        Exception second) =>
        first is null
            ? second
            : new AggregateException(first, second);

    private void CompleteSinkAndTasks(Exception? error)
    {
        Exception? terminalError = error;
        try
        {
            _sink.Complete(error);
        }
        catch (Exception sinkError)
        {
            terminalError = CombineErrors(terminalError, sinkError);
        }

        if (terminalError is not null)
        {
            lock (_gate)
            {
                if (_state != CaptureSessionState.Disposed)
                {
                    _state = CaptureSessionState.Faulted;
                }
            }
        }

        _started.TrySetCanceled();
        if (terminalError is null)
        {
            _completed.TrySetResult();
        }
        else
        {
            _completed.TrySetException(terminalError);
        }
    }

    private void RequestStop()
    {
        _lifetime.Cancel();
        _audioReady.Set();
    }

    public void Dispose()
    {
        if (Interlocked.Exchange(ref _disposed, 1) != 0)
        {
            return;
        }

        try
        {
            StopAsync().GetAwaiter().GetResult();
        }
        finally
        {
            _audioReady.Dispose();
            _lifetime.Dispose();
            lock (_gate)
            {
                _state = CaptureSessionState.Disposed;
            }
        }
    }

    public async ValueTask DisposeAsync()
    {
        if (Interlocked.Exchange(ref _disposed, 1) != 0)
        {
            return;
        }

        try
        {
            await StopAsync().ConfigureAwait(false);
        }
        finally
        {
            _audioReady.Dispose();
            _lifetime.Dispose();
            lock (_gate)
            {
                _state = CaptureSessionState.Disposed;
            }
        }
    }

    private void ThrowIfDisposed()
    {
        ObjectDisposedException.ThrowIf(
            Volatile.Read(ref _disposed) != 0,
            this);
    }
}
