using System.Buffers.Binary;
using System.Text;

namespace BwReader.ComputerVoiceAudio;

internal enum DirectPcmTrack : byte
{
    AppOutput = 1,
    LegacyWindowsMicrophone = 2,
    BrowserMicrophone = 3,
}

internal sealed record DirectPcmFrame(
    DirectPcmTrack Track,
    uint Sequence,
    ulong TimestampMicroseconds,
    ReadOnlyMemory<byte> PcmS16Le);

internal sealed record DirectDecodedPcmFrame(
    string SessionId,
    DirectPcmFrame Frame);

internal static class DirectPcmFrameCodec
{
    private static readonly byte[] Magic = Encoding.ASCII.GetBytes("BWCV");

    internal static byte[] Encode(
        string sessionId,
        DirectPcmFrame frame)
    {
        byte[] sessionBytes = ParseSessionId(sessionId);
        if (
            frame.Track != DirectPcmTrack.AppOutput
            || frame.PcmS16Le.Length
                != DirectBridgeContract.PcmPayloadBytes
        )
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_PCM_FRAME_INVALID",
                "PCM 帧合同无效");
        }

        byte[] result = new byte[DirectBridgeContract.PcmFrameBytes];
        Magic.CopyTo(result, 0);
        result[4] = 1;
        result[5] = (byte)frame.Track;
        BinaryPrimitives.WriteUInt16LittleEndian(
            result.AsSpan(6, 2),
            0);
        sessionBytes.CopyTo(result, 8);
        BinaryPrimitives.WriteUInt32LittleEndian(
            result.AsSpan(24, 4),
            frame.Sequence);
        BinaryPrimitives.WriteUInt64LittleEndian(
            result.AsSpan(28, 8),
            frame.TimestampMicroseconds);
        frame.PcmS16Le.Span.CopyTo(
            result.AsSpan(DirectBridgeContract.PcmFrameHeaderBytes));
        return result;
    }

    internal static DirectDecodedPcmFrame DecodeUplink(
        ReadOnlySpan<byte> encoded)
    {
        if (
            encoded.Length != DirectBridgeContract.PcmFrameBytes
            || !encoded[..4].SequenceEqual(Magic)
            || encoded[4] != 1
            || encoded[5] != (byte)DirectPcmTrack.BrowserMicrophone
            || BinaryPrimitives.ReadUInt16LittleEndian(
                encoded.Slice(6, 2)) != 0
        )
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_UPLINK_FRAME_INVALID",
                "浏览器麦克风 binary 帧合同无效");
        }

        byte[] sessionBytes = encoded.Slice(8, 16).ToArray();
        string sessionId =
            "session-" + DirectBase64Url.Encode(sessionBytes);
        _ = ParseSessionId(sessionId);
        DirectPcmFrame frame = new(
            DirectPcmTrack.BrowserMicrophone,
            BinaryPrimitives.ReadUInt32LittleEndian(
                encoded.Slice(24, 4)),
            BinaryPrimitives.ReadUInt64LittleEndian(
                encoded.Slice(28, 8)),
            encoded[DirectBridgeContract.PcmFrameHeaderBytes..]
                .ToArray());
        return new DirectDecodedPcmFrame(sessionId, frame);
    }

    internal static byte[] ParseSessionId(string sessionId)
    {
        const string prefix = "session-";
        if (
            !sessionId.StartsWith(prefix, StringComparison.Ordinal)
            || sessionId.Length != prefix.Length + 22
        )
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_SESSION_ID_INVALID",
                "sessionId 必须绑定 16 字节随机值");
        }
        byte[] value = DirectBase64Url.Decode(
            sessionId[prefix.Length..],
            16,
            "BW_COMPUTER_VOICE_DIRECT_SESSION_ID_INVALID");
        if (value.Length != 16)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_SESSION_ID_INVALID",
                "sessionId 必须绑定 16 字节随机值");
        }
        return value;
    }
}

internal sealed class DirectPcmStartGate
{
    // START cannot release PCM until Codex Voice has been observed through
    // the Windows capability ledger and the JSON success reply is on the
    // wire.  That confirmation has a five-second timeout, so reusing the
    // 400 ms live-playback horizon here can tear down an otherwise healthy
    // start before its reply.  Keep this bootstrap buffer separately bounded;
    // the Reader still enforces the advertised 400 ms playback horizon.
    internal const int BootstrapBufferMilliseconds = 6000;
    private const int FramesPerTrack =
        BootstrapBufferMilliseconds / 20;
    private readonly object _gate = new();
    private readonly Queue<DirectPcmFrame> _buffer = new();
    private readonly Dictionary<DirectPcmTrack, int> _trackCounts = [];
    private readonly Func<DirectPcmFrame, CancellationToken, Task> _sender;
    private int _state;

    internal DirectPcmStartGate(
        Func<DirectPcmFrame, CancellationToken, Task> sender)
    {
        _sender = sender;
    }

    internal Task SendAsync(
        DirectPcmFrame frame,
        CancellationToken cancellationToken)
    {
        lock (_gate)
        {
            if (_state == 3)
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_PCM_START_ABORTED",
                    "START 未完成，PCM 帧已拒绝");
            }
            if (_state < 2)
            {
                if (frame.Track != DirectPcmTrack.AppOutput)
                {
                    throw new DirectProtocolException(
                        "BW_COMPUTER_VOICE_DIRECT_PCM_FRAME_INVALID",
                        "服务端下行只允许应用输出轨道");
                }
                int count = _trackCounts.GetValueOrDefault(frame.Track);
                if (
                    count >= FramesPerTrack
                    || frame.PcmS16Le.Length
                        != DirectBridgeContract.PcmPayloadBytes
                )
                {
                    throw new DirectProtocolException(
                        "BW_COMPUTER_VOICE_DIRECT_PCM_START_GATE_FULL",
                        "START 回执前的 PCM 缓冲已满");
                }
                _trackCounts[frame.Track] = count + 1;
                _buffer.Enqueue(frame with
                {
                    PcmS16Le = frame.PcmS16Le.ToArray(),
                });
                return Task.CompletedTask;
            }
        }
        return _sender(frame, cancellationToken);
    }

    internal async Task ReleaseAsync(
        CancellationToken cancellationToken)
    {
        lock (_gate)
        {
            if (_state != 0)
            {
                throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_DIRECT_PCM_START_GATE_STATE");
            }
            _state = 1;
        }

        try
        {
            while (true)
            {
                DirectPcmFrame? frame;
                lock (_gate)
                {
                    if (_state == 3)
                    {
                        throw new DirectProtocolException(
                            "BW_COMPUTER_VOICE_DIRECT_PCM_START_ABORTED",
                            "START 未完成，PCM 帧已拒绝");
                    }
                    if (_buffer.Count == 0)
                    {
                        _state = 2;
                        return;
                    }
                    frame = _buffer.Dequeue();
                    _trackCounts[frame.Track]--;
                }
                await _sender(frame, cancellationToken)
                    .ConfigureAwait(false);
            }
        }
        catch
        {
            Abort();
            throw;
        }
    }

    internal void Abort()
    {
        lock (_gate)
        {
            _state = 3;
            _buffer.Clear();
            _trackCounts.Clear();
        }
    }
}
