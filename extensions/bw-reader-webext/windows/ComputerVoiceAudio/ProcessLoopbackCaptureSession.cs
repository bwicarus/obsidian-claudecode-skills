using System.Runtime.InteropServices;
using BwReader.ComputerVoiceAudio.Interop;

namespace BwReader.ComputerVoiceAudio;

internal enum CaptureSessionState
{
    Prepared,
    Starting,
    Running,
    Stopping,
    Stopped,
    Faulted,
    Disposed,
}

internal interface IProcessLoopbackCaptureRuntimeFactory
{
    IProcessLoopbackCaptureRuntime Create(
        uint targetProcessId,
        TimeSpan activationTimeout,
        CancellationToken cancellationToken);
}

internal interface IProcessLoopbackCaptureRuntime : IDisposable
{
    PcmAudioFormat Initialize(EventWaitHandle audioReadyEvent);

    void Start();

    int Drain(
        IBoundedPcmSink sink,
        CaptureSessionOptions options);

    void Stop();
}

internal sealed class ProcessLoopbackCaptureSession :
    IDisposable,
    IAsyncDisposable
{
    private readonly object _gate = new();
    private readonly IBoundedPcmSink _sink;
    private readonly CaptureSessionOptions _options;
    private readonly IProcessLoopbackCaptureRuntimeFactory _runtimeFactory;
    private readonly CancellationTokenSource _lifetime = new();
    private readonly EventWaitHandle _audioReady =
        new(false, EventResetMode.AutoReset);
    private readonly TaskCompletionSource _started =
        new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly TaskCompletionSource _completed =
        new(TaskCreationOptions.RunContinuationsAsynchronously);
    private Thread? _captureThread;
    private CaptureSessionState _state = CaptureSessionState.Prepared;
    private PcmAudioFormat? _format;
    private int _disposed;

    private ProcessLoopbackCaptureSession(
        uint targetProcessId,
        IBoundedPcmSink sink,
        CaptureSessionOptions options,
        IProcessLoopbackCaptureRuntimeFactory runtimeFactory)
    {
        _ = ProcessLoopbackActivation.BuildParameters(targetProcessId);
        ArgumentNullException.ThrowIfNull(sink);
        ArgumentNullException.ThrowIfNull(runtimeFactory);
        options.Validate();

        TargetProcessId = targetProcessId;
        _sink = sink;
        _options = options;
        _runtimeFactory = runtimeFactory;
    }

    internal uint TargetProcessId { get; }

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

    internal static ProcessLoopbackCaptureSession Prepare(
        uint targetProcessId,
        IBoundedPcmSink sink,
        CaptureSessionOptions? options = null) =>
        new(
            targetProcessId,
            sink,
            options ?? CaptureSessionOptions.Default,
            new NativeProcessLoopbackCaptureRuntimeFactory());

    internal static ProcessLoopbackCaptureSession PrepareForTest(
        uint targetProcessId,
        IBoundedPcmSink sink,
        IProcessLoopbackCaptureRuntimeFactory runtimeFactory,
        CaptureSessionOptions? options = null) =>
        new(
            targetProcessId,
            sink,
            options ?? CaptureSessionOptions.Default,
            runtimeFactory);

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
                Name = $"BW process audio {TargetProcessId}",
            };
            _captureThread = captureThread;
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
            runtime = _runtimeFactory.Create(
                TargetProcessId,
                _options.ActivationTimeout,
                _lifetime.Token);
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

internal sealed class NativeProcessLoopbackCaptureRuntimeFactory :
    IProcessLoopbackCaptureRuntimeFactory
{
    public IProcessLoopbackCaptureRuntime Create(
        uint targetProcessId,
        TimeSpan activationTimeout,
        CancellationToken cancellationToken)
    {
        ActivatedProcessAudioClient activated =
            ProcessLoopbackActivation.ActivateAsync(
                    targetProcessId,
                    activationTimeout,
                    cancellationToken)
                .GetAwaiter()
                .GetResult();

        try
        {
            return new NativeProcessLoopbackCaptureRuntime(activated);
        }
        catch
        {
            activated.Dispose();
            throw;
        }
    }
}

internal sealed class NativeProcessLoopbackCaptureRuntime :
    IProcessLoopbackCaptureRuntime
{
    private static readonly AudioClientStreamFlags ProcessLoopbackStreamFlags =
        AudioClientStreamFlags.Loopback
        | AudioClientStreamFlags.EventCallback
        | AudioClientStreamFlags.AutoConvertPcm;

    private readonly SharedEventDrivenPcmRuntime _inner;

    internal NativeProcessLoopbackCaptureRuntime(
        ActivatedProcessAudioClient activated)
    {
        _inner = new SharedEventDrivenPcmRuntime(
            activated,
            ProcessLoopbackStreamFlags,
            "app-output",
            CreateProcessLoopbackCaptureFormat());
    }

    internal static AudioClientStreamFlags StreamFlagsForTest =>
        ProcessLoopbackStreamFlags;

    internal static WaveFormatEx CaptureFormatForTest =>
        CreateProcessLoopbackCaptureFormat();

    private static WaveFormatEx CreateProcessLoopbackCaptureFormat() =>
        new()
        {
            FormatTag = PcmAudioFormat.WaveFormatPcm,
            Channels = 2,
            SamplesPerSecond = Pcm48kMonoFramer.SampleRate,
            AverageBytesPerSecond =
                Pcm48kMonoFramer.SampleRate * 2u * sizeof(short),
            BlockAlign = 2 * sizeof(short),
            BitsPerSample = sizeof(short) * 8,
            ExtraSize = 0,
        };

    public PcmAudioFormat Initialize(EventWaitHandle audioReadyEvent)
        => _inner.Initialize(audioReadyEvent);

    public void Start()
        => _inner.Start();

    public int Drain(
        IBoundedPcmSink sink,
        CaptureSessionOptions options)
        => _inner.Drain(sink, options);

    public void Stop()
        => _inner.Stop();

    public void Dispose()
        => _inner.Dispose();
}

internal sealed class NativeCapturePacketSource : ICapturePacketSource
{
    private IAudioCaptureClient? _captureClient;
    private readonly CaptureThreadAffinity _threadAffinity;

    internal NativeCapturePacketSource(
        IAudioCaptureClient captureClient,
        CaptureThreadAffinity threadAffinity,
        uint maximumFrameCount)
    {
        if (maximumFrameCount == 0)
        {
            throw new ArgumentOutOfRangeException(nameof(maximumFrameCount));
        }

        _captureClient = captureClient;
        _threadAffinity = threadAffinity;
        MaximumFrameCount = maximumFrameCount;
    }

    internal PcmAudioFormat? Format { get; set; }

    public uint MaximumFrameCount { get; }

    public uint GetNextPacketSize()
    {
        _threadAffinity.BindOrAssertCurrentThread();
        RequireSucceeded(
            CaptureClient.GetNextPacketSize(out uint packetSize),
            "GET_NEXT_PACKET_SIZE");
        return packetSize;
    }

    public bool TryGetBuffer(out NativeCapturePacket packet)
    {
        _threadAffinity.BindOrAssertCurrentThread();
        int result = CaptureClient.GetBuffer(
            out nint data,
            out uint frames,
            out uint flags,
            out ulong devicePosition,
            out ulong qpcPosition);
        if (result == ProcessLoopbackInterop.AudioClientBufferEmpty)
        {
            packet = default;
            return false;
        }

        if (result != ProcessLoopbackInterop.Succeeded)
        {
            if (result < 0)
            {
                Marshal.ThrowExceptionForHR(result);
            }

            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_AUDIO_GET_BUFFER_RESULT_UNEXPECTED");
        }

        packet = new NativeCapturePacket(
            data,
            frames,
            (AudioClientBufferFlags)flags,
            devicePosition,
            qpcPosition);
        return true;
    }

    public void ReleaseBuffer(uint frameCount)
    {
        _threadAffinity.BindOrAssertCurrentThread();
        RequireSucceeded(
            CaptureClient.ReleaseBuffer(frameCount),
            "RELEASE_BUFFER");
    }

    public void Dispose()
    {
        _threadAffinity.BindOrAssertCurrentThread();
        IAudioCaptureClient? captureClient = Interlocked.Exchange(
            ref _captureClient,
            null);
        if (OperatingSystem.IsWindows()
            && captureClient is not null
            && Marshal.IsComObject(captureClient))
        {
            Marshal.FinalReleaseComObject(captureClient);
        }
    }

    private IAudioCaptureClient CaptureClient =>
        _captureClient
        ?? throw new ObjectDisposedException(nameof(NativeCapturePacketSource));

    private static void RequireSucceeded(int result, string operation)
    {
        if (result < 0)
        {
            Marshal.ThrowExceptionForHR(result);
        }

        if (result != ProcessLoopbackInterop.Succeeded)
        {
            throw new InvalidOperationException(
                $"BW_COMPUTER_VOICE_AUDIO_{operation}_RESULT_UNEXPECTED");
        }
    }
}

internal sealed class CaptureThreadAffinity
{
    private int _threadId;

    internal int ThreadId => Volatile.Read(ref _threadId);

    internal void BindOrAssertCurrentThread() =>
        BindOrAssert(Environment.CurrentManagedThreadId);

    internal void BindOrAssert(int threadId)
    {
        if (threadId <= 0)
        {
            throw new ArgumentOutOfRangeException(nameof(threadId));
        }

        int existing = Interlocked.CompareExchange(
            ref _threadId,
            threadId,
            comparand: 0);
        if (existing != 0 && existing != threadId)
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_AUDIO_CAPTURE_THREAD_MISMATCH");
        }
    }
}
