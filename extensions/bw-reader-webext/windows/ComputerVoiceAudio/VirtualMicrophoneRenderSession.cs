using System.Buffers;
using System.Runtime.InteropServices;
using BwReader.ComputerVoiceAudio.Interop;

namespace BwReader.ComputerVoiceAudio;

internal sealed record VirtualMicrophoneRenderRequest
{
    internal const int MaximumEndpointIdLength = 1024;

    private VirtualMicrophoneRenderRequest(string endpointId)
    {
        EndpointId = endpointId;
    }

    internal string EndpointId { get; }

    internal static VirtualMicrophoneRenderRequest Create(
        string? endpointId)
    {
        if (string.IsNullOrWhiteSpace(endpointId))
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_VIRTUAL_MIC_RENDER_REQUIRED",
                "尚未配置虚拟麦克风播放端点");
        }
        if (
            endpointId.Length > MaximumEndpointIdLength
            || endpointId.Any(char.IsControl)
        )
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_VIRTUAL_MIC_RENDER_INVALID",
                "虚拟麦克风播放端点无效");
        }
        return new VirtualMicrophoneRenderRequest(endpointId);
    }
}

internal sealed class BoundedUplinkPcmQueue
{
    internal const int FrameDurationMilliseconds = 20;
    // 400 ms（原 200）。队列只在上游停顿后的突发里才会满；满了怎么丢决定"打断的
    // 开头"活不活（2026-09-06 审计 C06）：盲丢最旧帧，网络抖 300 ms 之后用户那句
    // "等一下"的前半截就没了，Codex 的自动暂停靠的正是这前半截。
    internal const int MaximumBufferedMilliseconds = 400;
    internal const int MaximumFrames =
        MaximumBufferedMilliseconds / FrameDurationMilliseconds;
    /// 判"这一帧是静音"的 s16 RMS 门限：低于它的帧先让路。
    internal const double SilentFrameRms = 200;

    private readonly object _gate = new();
    private readonly Queue<byte[]> _frames = new();
    private int _headOffset;
    private long _droppedFrames;
    private bool _stopped;

    internal int BufferedFrames
    {
        get
        {
            lock (_gate)
            {
                return _frames.Count;
            }
        }
    }

    internal long DroppedFrames
    {
        get
        {
            lock (_gate)
            {
                return _droppedFrames;
            }
        }
    }

    internal void Push(ReadOnlyMemory<byte> pcmS16Le)
    {
        if (pcmS16Le.Length != DirectBridgeContract.PcmPayloadBytes)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_UPLINK_FRAME_INVALID",
                "浏览器麦克风 PCM 帧大小无效");
        }
        lock (_gate)
        {
            if (_stopped)
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_UPLINK_NOT_ACTIVE",
                    "浏览器麦克风上行尚未启动");
            }
            if (_frames.Count >= MaximumFrames)
            {
                // The uplink sequence has already been validated before this
                // jitter queue. Keep latency bounded by dropping stale audio;
                // unlike the downlink START gate, this cannot create a later
                // protocol sequence gap.
                //
                // 先丢**静音**帧，没有静音才丢最旧的人声帧：延迟照样收敛（停顿
                // 里的静音被吃掉），而用户刚开口的那几帧留下来。
                DropOneFramePreferringSilence();
            }
            _frames.Enqueue(pcmS16Le.ToArray());
        }
    }

    /// 调用方持有 _gate。丢掉队列里最旧的一帧静音；一帧静音都没有就丢最旧的那帧。
    /// 队头正在被 Read 消费时（_headOffset > 0）不动它，从第二帧起找。
    private void DropOneFramePreferringSilence()
    {
        byte[][] frames = _frames.ToArray();
        int start = _headOffset > 0 ? 1 : 0;
        int victim = -1;
        for (int index = start; index < frames.Length; index++)
        {
            if (UplinkSpeechEndDetector.Rms(frames[index]) < SilentFrameRms)
            {
                victim = index;
                break;
            }
        }
        if (victim < 0)
        {
            victim = start < frames.Length ? start : 0;
        }
        _frames.Clear();
        for (int index = 0; index < frames.Length; index++)
        {
            if (index == victim)
            {
                Array.Clear(frames[index]);
                continue;
            }
            _frames.Enqueue(frames[index]);
        }
        if (victim == 0)
        {
            _headOffset = 0;
        }
        _droppedFrames += 1;
    }

    internal int Read(Span<byte> destination)
    {
        int written = 0;
        lock (_gate)
        {
            while (written < destination.Length && _frames.Count != 0)
            {
                byte[] head = _frames.Peek();
                int available = head.Length - _headOffset;
                int copy = Math.Min(
                    available,
                    destination.Length - written);
                head.AsSpan(_headOffset, copy).CopyTo(
                    destination.Slice(written, copy));
                written += copy;
                _headOffset += copy;
                if (_headOffset == head.Length)
                {
                    Array.Clear(head);
                    _frames.Dequeue();
                    _headOffset = 0;
                }
            }
        }
        return written;
    }

    internal void StopAndClear()
    {
        lock (_gate)
        {
            _stopped = true;
            while (_frames.TryDequeue(out byte[]? frame))
            {
                Array.Clear(frame);
            }
            _headOffset = 0;
        }
    }
}

internal interface IVirtualMicrophoneRenderRuntime : IDisposable
{
    void Initialize(EventWaitHandle audioReadyEvent);

    void Prime();

    void Start();

    void Render(BoundedUplinkPcmQueue source);

    void Stop();
}

internal interface IVirtualMicrophoneRenderRuntimeFactory
{
    IVirtualMicrophoneRenderRuntime Create(
        VirtualMicrophoneRenderRequest request,
        CancellationToken cancellationToken);
}

internal static class VirtualRenderEndpointProbe
{
    internal static void ValidateExactActiveRender(
        string endpointId,
        string stagePrefix)
    {
        if (
            string.IsNullOrWhiteSpace(endpointId)
            || endpointId.Length
                > VirtualMicrophoneRenderRequest.MaximumEndpointIdLength
            || endpointId.Any(char.IsControl)
        )
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_RENDER_ENDPOINT_INVALID",
                "虚拟音频播放端点无效");
        }
        if (
            stagePrefix is not (
                "virtual-microphone"
                or "virtual-speaker")
        )
        {
            throw new ArgumentOutOfRangeException(nameof(stagePrefix));
        }
        if (!OperatingSystem.IsWindows())
        {
            throw new PlatformNotSupportedException(
                "BW_COMPUTER_VOICE_AUDIO_WINDOWS_REQUIRED");
        }

        object? enumeratorObject = null;
        IMMDevice? endpoint = null;
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
                stagePrefix + ".get-explicit-render-device");
            if (endpoint is null)
            {
                throw new AudioCaptureStageException(
                    stagePrefix + ".get-explicit-render-device",
                    unchecked((int)0x80070490));
            }
            RequireSucceeded(
                endpoint.GetState(out DeviceState state),
                stagePrefix + ".get-render-device-state");
            if ((state & DeviceState.Active) == 0)
            {
                throw new AudioCaptureStageException(
                    stagePrefix + ".render-device-inactive",
                    unchecked((int)0x88890004));
            }
            if (endpoint is not IMMEndpoint direction)
            {
                throw new AudioCaptureStageException(
                    stagePrefix + ".query-render-data-flow",
                    unchecked((int)0x80004002));
            }
            RequireSucceeded(
                direction.GetDataFlow(out AudioDataFlow dataFlow),
                stagePrefix + ".get-render-data-flow");
            if (dataFlow != AudioDataFlow.Render)
            {
                throw new AudioCaptureStageException(
                    stagePrefix + ".render-data-flow-mismatch",
                    unchecked((int)0x80070057));
            }
        }
        finally
        {
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

internal sealed class NativeVirtualMicrophoneRenderRuntimeFactory :
    IVirtualMicrophoneRenderRuntimeFactory
{
    public IVirtualMicrophoneRenderRuntime Create(
        VirtualMicrophoneRenderRequest request,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        return new NativeVirtualMicrophoneRenderRuntime(request);
    }
}

internal sealed class NativeVirtualMicrophoneRenderRuntime :
    IVirtualMicrophoneRenderRuntime
{
    private const long SharedBufferDuration100Nanoseconds = 2_000_000;
    private static readonly AudioClientStreamFlags StreamFlags =
        AudioClientStreamFlags.EventCallback
        | AudioClientStreamFlags.AutoConvertPcm
        | AudioClientStreamFlags.SrcDefaultQuality;

    private readonly VirtualMicrophoneRenderRequest _request;
    private object? _enumeratorObject;
    private IMMDevice? _endpoint;
    private object? _audioClientObject;
    private IAudioClient? _audioClient;
    private object? _renderClientObject;
    private IAudioRenderClient? _renderClient;
    private uint _bufferFrameCount;
    private bool _initialized;
    private bool _primed;
    private bool _started;
    private int _disposed;

    internal NativeVirtualMicrophoneRenderRuntime(
        VirtualMicrophoneRenderRequest request)
    {
        ArgumentNullException.ThrowIfNull(request);
        _request = request;
    }

    internal static AudioClientStreamFlags StreamFlagsForTest =>
        StreamFlags;

    internal static WaveFormatEx RenderFormatForTest =>
        CreateRenderFormat();

    public void Initialize(EventWaitHandle audioReadyEvent)
    {
        ObjectDisposedException.ThrowIf(
            Volatile.Read(ref _disposed) != 0,
            this);
        ArgumentNullException.ThrowIfNull(audioReadyEvent);
        if (_initialized)
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_DIRECT_RENDER_ALREADY_INITIALIZED");
        }
        if (!OperatingSystem.IsWindows())
        {
            throw new PlatformNotSupportedException(
                "BW_COMPUTER_VOICE_AUDIO_WINDOWS_REQUIRED");
        }

        nint formatPointer = 0;
        nint servicePointer = 0;
        try
        {
            Type enumeratorType = Type.GetTypeFromCLSID(
                ExplicitMicrophoneInterop.ClsidMmDeviceEnumerator,
                throwOnError: true)
                ?? throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_AUDIO_MMDEVICE_TYPE_MISSING");
            _enumeratorObject = Activator.CreateInstance(enumeratorType);
            if (_enumeratorObject is not IMMDeviceEnumerator enumerator)
            {
                throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_AUDIO_MMDEVICE_ENUMERATOR_INVALID");
            }
            RequireSucceeded(
                enumerator.GetDevice(
                    _request.EndpointId,
                    out IMMDevice endpoint),
                "virtual-microphone.get-explicit-render-device");
            _endpoint = endpoint
                ?? throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_DIRECT_RENDER_DEVICE_MISSING");
            RequireSucceeded(
                endpoint.GetState(out DeviceState state),
                "virtual-microphone.get-render-device-state");
            if ((state & DeviceState.Active) == 0)
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_VIRTUAL_MIC_RENDER_INACTIVE",
                    "虚拟麦克风播放端点未激活");
            }
            if (
                endpoint is not IMMEndpoint endpointDirection
                || RequireDataFlow(endpointDirection) != AudioDataFlow.Render
            )
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_VIRTUAL_MIC_RENDER_WRONG_FLOW",
                    "配置的虚拟麦克风端点不是播放端点");
            }

            Guid audioClientId = ExplicitMicrophoneInterop.IidIAudioClient;
            RequireSucceeded(
                endpoint.Activate(
                    ref audioClientId,
                    ComClassContext.All,
                    activationParameters: 0,
                    out object audioClientObject),
                "virtual-microphone.activate-audio-client");
            _audioClientObject = audioClientObject;
            _audioClient = audioClientObject as IAudioClient
                ?? throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_DIRECT_RENDER_AUDIO_CLIENT_INVALID");

            WaveFormatEx format = CreateRenderFormat();
            formatPointer = Marshal.AllocCoTaskMem(
                Marshal.SizeOf<WaveFormatEx>());
            Marshal.StructureToPtr(format, formatPointer, fDeleteOld: false);
            RequireSucceeded(
                _audioClient.Initialize(
                    AudioClientShareMode.Shared,
                    (uint)StreamFlags,
                    SharedBufferDuration100Nanoseconds,
                    periodicity: 0,
                    formatPointer,
                    audioSessionGuid: 0),
                "virtual-microphone.initialize-render");
            _initialized = true;
            RequireSucceeded(
                _audioClient.GetBufferSize(out _bufferFrameCount),
                "virtual-microphone.get-render-buffer-size");
            if (_bufferFrameCount == 0)
            {
                throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_DIRECT_RENDER_BUFFER_SIZE_INVALID");
            }
            RequireSucceeded(
                _audioClient.SetEventHandle(
                    audioReadyEvent.SafeWaitHandle.DangerousGetHandle()),
                "virtual-microphone.set-render-event");

            Guid renderClientId =
                ProcessLoopbackInterop.IidIAudioRenderClient;
            RequireSucceeded(
                _audioClient.GetService(
                    ref renderClientId,
                    out servicePointer),
                "virtual-microphone.get-render-service");
            _renderClientObject =
                Marshal.GetObjectForIUnknown(servicePointer);
            _renderClient = _renderClientObject as IAudioRenderClient
                ?? throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_DIRECT_RENDER_SERVICE_INVALID");
            // 这一路渲染的是用户的声音（Codex 的输入），Windows 通讯闪避默认会在
            // "通讯活动"期间把它当"其它声音"压低 80% —— 见 AudioSessionDucking。
            // 退出失败不抛：上行照走，只是没有豁免；结果进双工诊断。
            AudioSessionDucking.LastVirtualMicrophoneOptOut =
                AudioSessionDucking.TryOptOut(_audioClient);
        }
        catch
        {
            Dispose();
            throw;
        }
        finally
        {
            if (servicePointer != 0)
            {
                _ = Marshal.Release(servicePointer);
            }
            if (formatPointer != 0)
            {
                Marshal.FreeCoTaskMem(formatPointer);
            }
        }
    }

    public void Prime()
    {
        _ = RequireInitializedAudioClient();
        IAudioRenderClient renderClient = _renderClient
            ?? throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_DIRECT_RENDER_SERVICE_MISSING");
        if (_primed || _started)
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_DIRECT_RENDER_ALREADY_PRIMED");
        }

        nint destination = 0;
        bool released = false;
        try
        {
            RequireSucceeded(
                renderClient.GetBuffer(
                    _bufferFrameCount,
                    out destination),
                "virtual-microphone.get-prime-render-buffer");
            RequireSucceeded(
                renderClient.ReleaseBuffer(
                    _bufferFrameCount,
                    (uint)AudioClientBufferFlags.Silent),
                "virtual-microphone.release-prime-render-buffer");
            released = true;
            _primed = true;
        }
        catch (Exception exception)
        {
            if (destination != 0 && !released)
            {
                try
                {
                    RequireSucceeded(
                        renderClient.ReleaseBuffer(
                            _bufferFrameCount,
                            (uint)AudioClientBufferFlags.Silent),
                        "virtual-microphone.rollback-prime-render-buffer");
                }
                catch (Exception rollback)
                {
                    throw new AggregateException(exception, rollback);
                }
            }
            throw;
        }
    }

    public void Start()
    {
        IAudioClient audioClient = RequireInitializedAudioClient();
        if (!_primed)
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_DIRECT_RENDER_NOT_PRIMED");
        }
        RequireSucceeded(
            audioClient.Start(),
            "virtual-microphone.start-render");
        _started = true;
    }

    public void Render(BoundedUplinkPcmQueue source)
    {
        ArgumentNullException.ThrowIfNull(source);
        IAudioClient audioClient = RequireInitializedAudioClient();
        IAudioRenderClient renderClient = _renderClient
            ?? throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_DIRECT_RENDER_SERVICE_MISSING");
        RequireSucceeded(
            audioClient.GetCurrentPadding(out uint padding),
            "virtual-microphone.get-render-padding");
        if (padding > _bufferFrameCount)
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_DIRECT_RENDER_PADDING_INVALID");
        }
        uint writableFrames = _bufferFrameCount - padding;
        if (writableFrames == 0)
        {
            return;
        }

        int writableBytes = checked((int)(
            writableFrames * sizeof(short)));
        byte[] rented = ArrayPool<byte>.Shared.Rent(writableBytes);
        nint destination = 0;
        bool released = false;
        try
        {
            RequireSucceeded(
                renderClient.GetBuffer(
                    writableFrames,
                    out destination),
                "virtual-microphone.get-render-buffer");
            int copied = source.Read(
                rented.AsSpan(0, writableBytes));
            if (copied == 0)
            {
                RequireSucceeded(
                    renderClient.ReleaseBuffer(
                        writableFrames,
                        (uint)AudioClientBufferFlags.Silent),
                    "virtual-microphone.release-silent-render-buffer");
                released = true;
                return;
            }
            if (copied < writableBytes)
            {
                Array.Clear(rented, copied, writableBytes - copied);
            }
            Marshal.Copy(rented, 0, destination, writableBytes);
            RequireSucceeded(
                renderClient.ReleaseBuffer(
                    writableFrames,
                    (uint)AudioClientBufferFlags.None),
                "virtual-microphone.release-render-buffer");
            released = true;
        }
        catch (Exception exception)
        {
            if (destination != 0 && !released)
            {
                try
                {
                    RequireSucceeded(
                        renderClient.ReleaseBuffer(
                            writableFrames,
                            (uint)AudioClientBufferFlags.Silent),
                        "virtual-microphone.rollback-render-buffer");
                }
                catch (Exception rollback)
                {
                    throw new AggregateException(exception, rollback);
                }
            }
            throw;
        }
        finally
        {
            ArrayPool<byte>.Shared.Return(rented, clearArray: true);
        }
    }

    public void Stop()
    {
        IAudioClient? audioClient = _audioClient;
        if (audioClient is null || !_initialized)
        {
            return;
        }
        Exception? failure = null;
        if (_started)
        {
            try
            {
                RequireSucceeded(
                    audioClient.Stop(),
                    "virtual-microphone.stop-render");
            }
            catch (Exception exception)
            {
                failure = exception;
            }
            _started = false;
        }
        try
        {
            RequireSucceeded(
                audioClient.Reset(),
                "virtual-microphone.reset-render");
            _primed = false;
        }
        catch (Exception exception)
        {
            failure = failure is null
                ? exception
                : new AggregateException(failure, exception);
        }
        if (failure is not null)
        {
            throw failure;
        }
    }

    public void Dispose()
    {
        if (Interlocked.Exchange(ref _disposed, 1) != 0)
        {
            return;
        }
        ReleaseComObject(
            Interlocked.Exchange(ref _renderClientObject, null));
        _renderClient = null;
        ReleaseComObject(
            Interlocked.Exchange(ref _audioClientObject, null));
        _audioClient = null;
        ReleaseComObject(
            Interlocked.Exchange(ref _endpoint, null));
        ReleaseComObject(
            Interlocked.Exchange(ref _enumeratorObject, null));
    }

    private IAudioClient RequireInitializedAudioClient() =>
        _initialized && _audioClient is not null
            ? _audioClient
            : throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_DIRECT_RENDER_NOT_INITIALIZED");

    private static AudioDataFlow RequireDataFlow(
        IMMEndpoint endpoint)
    {
        RequireSucceeded(
            endpoint.GetDataFlow(out AudioDataFlow dataFlow),
            "virtual-microphone.get-render-data-flow");
        return dataFlow;
    }

    private static WaveFormatEx CreateRenderFormat() =>
        new()
        {
            FormatTag = PcmAudioFormat.WaveFormatPcm,
            Channels = 1,
            SamplesPerSecond = Pcm48kMonoFramer.SampleRate,
            AverageBytesPerSecond =
                Pcm48kMonoFramer.SampleRate * sizeof(short),
            BlockAlign = sizeof(short),
            BitsPerSample = sizeof(short) * 8,
            ExtraSize = 0,
        };

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

internal sealed class VirtualMicrophoneRenderSession :
    IDisposable,
    IAsyncDisposable
{
    internal const int RenderWakeFallbackMilliseconds = 100;

    private readonly object _gate = new();
    private readonly VirtualMicrophoneRenderRequest _request;
    private readonly IVirtualMicrophoneRenderRuntimeFactory _runtimeFactory;
    private readonly BoundedUplinkPcmQueue _queue = new();
    private readonly CancellationTokenSource _lifetime = new();
    private readonly EventWaitHandle _audioReady =
        new(false, EventResetMode.AutoReset);
    private readonly TaskCompletionSource _started =
        new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly TaskCompletionSource _completed =
        new(TaskCreationOptions.RunContinuationsAsynchronously);
    private CaptureSessionState _state = CaptureSessionState.Prepared;
    private int _disposed;

    private VirtualMicrophoneRenderSession(
        VirtualMicrophoneRenderRequest request,
        IVirtualMicrophoneRenderRuntimeFactory runtimeFactory)
    {
        ArgumentNullException.ThrowIfNull(request);
        ArgumentNullException.ThrowIfNull(runtimeFactory);
        _request = request;
        _runtimeFactory = runtimeFactory;
    }

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

    internal int BufferedFrames => _queue.BufferedFrames;

    internal long DroppedFrames => _queue.DroppedFrames;

    internal static VirtualMicrophoneRenderSession Prepare(
        VirtualMicrophoneRenderRequest request) =>
        new(request, new NativeVirtualMicrophoneRenderRuntimeFactory());

    internal static VirtualMicrophoneRenderSession PrepareForTest(
        VirtualMicrophoneRenderRequest request,
        IVirtualMicrophoneRenderRuntimeFactory runtimeFactory) =>
        new(request, runtimeFactory);

    internal async Task StartAsync(
        CancellationToken cancellationToken = default)
    {
        ThrowIfDisposed();
        Thread renderThread;
        lock (_gate)
        {
            if (_state != CaptureSessionState.Prepared)
            {
                throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_DIRECT_RENDER_ALREADY_STARTED");
            }
            _state = CaptureSessionState.Starting;
            renderThread = new Thread(RenderThreadMain)
            {
                IsBackground = true,
                Name = "BW browser microphone render",
            };
        }
        if (OperatingSystem.IsWindows())
        {
            renderThread.SetApartmentState(ApartmentState.MTA);
        }
        renderThread.Start();
        try
        {
            await _started.Task.WaitAsync(cancellationToken)
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
            }
            throw;
        }
    }

    internal void Push(DirectPcmFrame frame)
    {
        if (frame.Track != DirectPcmTrack.BrowserMicrophone)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_UPLINK_TRACK_INVALID",
                "客户端 binary 只允许浏览器麦克风轨道");
        }
        lock (_gate)
        {
            if (_state != CaptureSessionState.Running)
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_UPLINK_NOT_ACTIVE",
                    "浏览器麦克风上行尚未启动");
            }
        }
        _queue.Push(frame.PcmS16Le);
    }

    internal async Task StopAsync(
        CancellationToken cancellationToken = default)
    {
        bool withoutThread = false;
        lock (_gate)
        {
            if (_state == CaptureSessionState.Prepared)
            {
                _state = CaptureSessionState.Stopped;
                withoutThread = true;
            }
            else if (_state is CaptureSessionState.Starting
                or CaptureSessionState.Running)
            {
                _state = CaptureSessionState.Stopping;
            }
        }
        _queue.StopAndClear();
        if (withoutThread)
        {
            _started.TrySetCanceled();
            _completed.TrySetResult();
            return;
        }
        RequestStop();
        await _completed.Task.WaitAsync(cancellationToken)
            .ConfigureAwait(false);
    }

    private void RenderThreadMain()
    {
        IVirtualMicrophoneRenderRuntime? runtime = null;
        Exception? terminalError = null;
        bool initialized = false;
        try
        {
            using ComMtaLease apartment = ComMtaLease.Enter();
            runtime = _runtimeFactory.Create(
                _request,
                _lifetime.Token);
            _lifetime.Token.ThrowIfCancellationRequested();
            runtime.Initialize(_audioReady);
            initialized = true;
            _lifetime.Token.ThrowIfCancellationRequested();
            runtime.Prime();
            _lifetime.Token.ThrowIfCancellationRequested();
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
                    static value => ((EventWaitHandle)value!).Set(),
                    _audioReady);
            while (!_lifetime.IsCancellationRequested)
            {
                _audioReady.WaitOne(RenderWakeFallbackMilliseconds);
                if (_lifetime.IsCancellationRequested)
                {
                    break;
                }
                runtime.Render(_queue);
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
                catch (Exception exception)
                {
                    terminalError = Combine(terminalError, exception);
                }
            }
            if (runtime is not null)
            {
                try
                {
                    runtime.Dispose();
                }
                catch (Exception exception)
                {
                    terminalError = Combine(terminalError, exception);
                }
            }
            _queue.StopAndClear();
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

    private static Exception Combine(
        Exception? first,
        Exception second) =>
        first is null
            ? second
            : new AggregateException(first, second);

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
