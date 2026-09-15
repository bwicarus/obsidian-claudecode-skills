using System.Net;
using System.Net.Sockets;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace BwReader.ComputerVoiceAudio;

// # App 档音频直连（2026-09-15 用户拍板）
//
// 原来这一路要绕两次虚拟声卡：App 的 PCM 帧 → 桥 → WASAPI 写进 CABLE Input →
// 语音核心运行器用 sounddevice 从 CABLE Output 抓回来；下行反过来再绕一次。
// 两端时钟各走各的，靠预填和缓冲硬凑，代价是持续的断音/underrun、40–80 ms 额外
// 延迟，以及两个会坏还不出声的活动件（设备被改名、被别的程序独占）。
//
// 直连模式下桥不开任何音频设备：PCM 帧用本地 UDP 直接和运行器对流。
//
// **帧格式两侧必须逐字节一致**（改一边的结果就是哑掉，而且哑得没有任何提示）：
//   8 字节头 = "BWA1" + uint32 小端序号，负载 = 20 ms 单声道 48 kHz s16 = 1920 字节。
// 走回环 UDP、丢包按静音补 —— 这是实时音频，重传比丢一帧更糟。
// 对应的 Python 端在 computer-voice-desktop/voice_cli_runner.py 的
// PipeMicTrack / PipeSpeaker（常量 PIPE_MAGIC / PIPE_HEADER / PIPE_PAYLOAD）。
internal sealed record DirectAudioPipeConfig(int UplinkPort, int DownlinkPort);

internal static class DirectAudioPipe
{
    /// <summary>运行器在 runtime 目录里放下这个文件 = 这次通话走直连管道。
    /// 跟 voice-backend-external.json 同一套做法：状态的主人是运行器，桥只读。</summary>
    internal const string FileName = "voice-audio-pipe.json";

    internal const int FrameBytes = 1920;      // 20 ms / 48 kHz / 单声道 / s16
    internal const int HeaderBytes = 8;
    internal const int FrameSamples = 960;
    internal const uint SampleRate = 48_000;

    private static readonly byte[] Magic = "BWA1"u8.ToArray();

    /// <summary>直连的 PCM 格式，写死：运行器只认这一种，协商只会带来"某天变成 44.1k 然后全链路变调"。</summary>
    internal static PcmAudioFormat Format { get; } = new(
        PcmSampleEncoding.IntegerPcm,
        PcmAudioFormat.WaveFormatPcm,
        Channels: 1,
        SamplesPerSecond: SampleRate,
        AverageBytesPerSecond: SampleRate * 2,
        BlockAlign: 2,
        BitsPerSample: 16,
        ValidBitsPerSample: 16,
        ExtraSize: 0,
        ChannelMask: 0,
        // 整数 PCM 必须配 SubtypePcm，PcmAudioFormat.Validate 会当场拒（自检抓到过一次）
        SubFormat: PcmAudioFormat.SubtypePcm);

    internal static void WriteHeader(Span<byte> destination, uint sequence)
    {
        Magic.CopyTo(destination);
        BitConverter.TryWriteBytes(destination[4..], sequence);
    }

    internal static bool TryReadPayload(
        ReadOnlySpan<byte> datagram,
        out ReadOnlySpan<byte> payload)
    {
        payload = default;
        if (
            datagram.Length != HeaderBytes + FrameBytes
            || !datagram[..4].SequenceEqual(Magic)
        )
        {
            return false;
        }
        payload = datagram[HeaderBytes..];
        return true;
    }

    /// <summary>读运行器放下的直连标记。没有文件 = 走原来的虚拟声卡那条路。
    /// 文件坏了/端口不合法**也当没有**，但要让调用方能说出原因 —— 静默降级到
    /// 另一条路而不出声，正是这一带最贵的那类故障。</summary>
    internal static DirectAudioPipeConfig? Read(out string? fault)
    {
        fault = null;
        string? runtime = ReaderAttentionBoard.RuntimeDirectory;
        if (string.IsNullOrEmpty(runtime))
        {
            return null;
        }
        string path = Path.Combine(runtime, FileName);
        if (!File.Exists(path))
        {
            return null;
        }
        try
        {
            JsonNode? node = JsonNode.Parse(File.ReadAllText(path));
            if (node is not JsonObject body)
            {
                fault = "直连标记不是 JSON 对象";
                return null;
            }
            int uplink = body["uplinkPort"]?.GetValue<int>() ?? 0;
            int downlink = body["downlinkPort"]?.GetValue<int>() ?? 0;
            if (
                uplink is < 1 or > 65535
                || downlink is < 1 or > 65535
                || uplink == downlink
            )
            {
                fault = "直连端口不合法：" + uplink + "/" + downlink;
                return null;
            }
            return new DirectAudioPipeConfig(uplink, downlink);
        }
        catch (Exception exception)
            when (exception is IOException
                or JsonException
                or FormatException
                or InvalidOperationException
                or UnauthorizedAccessException)
        {
            fault = "直连标记读不了：" + exception.GetType().Name;
            return null;
        }
    }
}

/// <summary>上行（App 的麦克风）：把 PCM 帧发给运行器，不写任何渲染设备。
/// 实现的是渲染运行时接口，所以整条上行链路（解帧、序号闸、有界队列）一行都不用改。</summary>
internal sealed class UdpUplinkRenderRuntime : IVirtualMicrophoneRenderRuntime
{
    private readonly IPEndPoint _target;
    private readonly Socket _socket = new(
        AddressFamily.InterNetwork,
        SocketType.Dgram,
        ProtocolType.Udp);
    private readonly byte[] _datagram =
        new byte[DirectAudioPipe.HeaderBytes + DirectAudioPipe.FrameBytes];
    private readonly byte[] _carry = new byte[DirectAudioPipe.FrameBytes];
    private int _carryLength;
    private uint _sequence;
    private EventWaitHandle? _ready;
    private Timer? _pacer;
    private bool _disposed;

    internal UdpUplinkRenderRuntime(DirectAudioPipeConfig pipe)
    {
        _target = new IPEndPoint(IPAddress.Loopback, pipe.UplinkPort);
    }

    internal long SentFrames { get; private set; }

    public void Initialize(EventWaitHandle audioReadyEvent)
    {
        ArgumentNullException.ThrowIfNull(audioReadyEvent);
        _ready = audioReadyEvent;
    }

    public void Prime()
    {
    }

    public void Start()
    {
        // 渲染线程靠这个事件醒。原来是声卡每 20 ms 要数据；直连没有声卡，
        // 就自己当那口钟 —— 不打这个拍子，只能靠循环里 100 ms 的兜底唤醒，
        // 一次吐五帧，抖动直接搬到对面。
        _pacer ??= new Timer(
            _ =>
            {
                try
                {
                    _ready?.Set();
                }
                catch (ObjectDisposedException)
                {
                }
            },
            null,
            TimeSpan.Zero,
            TimeSpan.FromMilliseconds(20));
    }

    public void Render(BoundedUplinkPcmQueue source)
    {
        ArgumentNullException.ThrowIfNull(source);
        while (true)
        {
            int written = source.Read(
                _carry.AsSpan(_carryLength));
            _carryLength += written;
            if (_carryLength < DirectAudioPipe.FrameBytes)
            {
                // 不够一整帧就留着：半帧发出去会让对面把帧界对错，之后每一帧都错位。
                return;
            }
            DirectAudioPipe.WriteHeader(_datagram, unchecked(++_sequence));
            _carry.AsSpan(0, DirectAudioPipe.FrameBytes)
                .CopyTo(_datagram.AsSpan(DirectAudioPipe.HeaderBytes));
            _carryLength = 0;
            try
            {
                _socket.SendTo(_datagram, SocketFlags.None, _target);
                SentFrames += 1;
            }
            catch (SocketException)
            {
                // 运行器还没起来/刚重启：丢掉这一帧就好，实时音频没有重传的余地。
                return;
            }
            catch (ObjectDisposedException)
            {
                return;
            }
            if (written == 0)
            {
                return;
            }
        }
    }

    public void Stop()
    {
        _pacer?.Change(Timeout.Infinite, Timeout.Infinite);
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }
        _disposed = true;
        _pacer?.Dispose();
        _pacer = null;
        _socket.Dispose();
    }
}

internal sealed class UdpUplinkRenderRuntimeFactory :
    IVirtualMicrophoneRenderRuntimeFactory
{
    private readonly DirectAudioPipeConfig _pipe;

    internal UdpUplinkRenderRuntimeFactory(DirectAudioPipeConfig pipe)
    {
        _pipe = pipe;
    }

    public IVirtualMicrophoneRenderRuntime Create(
        VirtualMicrophoneRenderRequest request,
        CancellationToken cancellationToken) =>
        new UdpUplinkRenderRuntime(_pipe);
}

/// <summary>下行（说给 App 的声音）：从运行器收 PCM 帧，不开任何采集设备。
/// 实现的是采集运行时接口，所以帧器、有界队列、回发 App 那一段一行都不用改。</summary>
internal sealed class UdpDownlinkCaptureRuntime : IProcessLoopbackCaptureRuntime
{
    private readonly int _port;
    private readonly Queue<byte[]> _packets = new();
    private readonly object _gate = new();
    private Socket? _socket;
    private EventWaitHandle? _ready;
    private Thread? _receiver;
    private volatile bool _stopped;
    private bool _disposed;

    internal UdpDownlinkCaptureRuntime(DirectAudioPipeConfig pipe)
    {
        _port = pipe.DownlinkPort;
    }

    internal long ReceivedFrames { get; private set; }

    internal long DroppedFrames { get; private set; }

    public PcmAudioFormat Initialize(EventWaitHandle audioReadyEvent)
    {
        ArgumentNullException.ThrowIfNull(audioReadyEvent);
        _ready = audioReadyEvent;
        Socket socket = new(
            AddressFamily.InterNetwork,
            SocketType.Dgram,
            ProtocolType.Udp);
        socket.Bind(new IPEndPoint(IPAddress.Loopback, _port));
        socket.ReceiveTimeout = 500;
        _socket = socket;
        return DirectAudioPipe.Format;
    }

    public void Start()
    {
        if (_receiver is not null)
        {
            return;
        }
        _receiver = new Thread(ReceiveLoop)
        {
            IsBackground = true,
            Name = "bw-direct-audio-pipe-downlink",
        };
        _receiver.Start();
    }

    private void ReceiveLoop()
    {
        byte[] buffer =
            new byte[DirectAudioPipe.HeaderBytes + DirectAudioPipe.FrameBytes];
        while (!_stopped)
        {
            int read;
            try
            {
                read = _socket!.Receive(buffer);
            }
            catch (SocketException)
            {
                continue;   // 超时（没人说话）也走这里：接着等
            }
            catch (ObjectDisposedException)
            {
                return;
            }
            if (!DirectAudioPipe.TryReadPayload(
                buffer.AsSpan(0, read),
                out ReadOnlySpan<byte> payload))
            {
                continue;
            }
            byte[] frame = payload.ToArray();
            lock (_gate)
            {
                // 攒多了丢最旧的：管道上的积压只会变成延迟，不会变成音质
                // （跟下行有界队列同一条规矩）。
                while (_packets.Count >= 32)
                {
                    _packets.Dequeue();
                    DroppedFrames += 1;
                }
                _packets.Enqueue(frame);
                ReceivedFrames += 1;
            }
            try
            {
                _ready?.Set();
            }
            catch (ObjectDisposedException)
            {
                return;
            }
        }
    }

    public int Drain(IBoundedPcmSink sink, CaptureSessionOptions options)
    {
        ArgumentNullException.ThrowIfNull(sink);
        int drained = 0;
        while (true)
        {
            byte[] frame;
            lock (_gate)
            {
                if (_packets.Count == 0)
                {
                    return drained;
                }
                frame = _packets.Dequeue();
            }
            if (!sink.TryWrite(new PcmPacket(
                frame,
                DirectAudioPipe.FrameSamples,
                Silent: false,
                Discontinuous: false,
                TimestampError: false,
                DevicePosition: 0,
                QpcPosition: 0)))
            {
                return drained;
            }
            drained += 1;
        }
    }

    public void Stop()
    {
        _stopped = true;
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }
        _disposed = true;
        _stopped = true;
        _socket?.Dispose();
        _socket = null;
        lock (_gate)
        {
            _packets.Clear();
        }
    }
}

internal sealed class UdpDownlinkCaptureRuntimeFactory :
    IProcessLoopbackCaptureRuntimeFactory
{
    private readonly DirectAudioPipeConfig _pipe;

    internal UdpDownlinkCaptureRuntimeFactory(DirectAudioPipeConfig pipe)
    {
        _pipe = pipe;
    }

    public IProcessLoopbackCaptureRuntime Create(
        uint targetProcessId,
        TimeSpan activationTimeout,
        CancellationToken cancellationToken) =>
        new UdpDownlinkCaptureRuntime(_pipe);
}
