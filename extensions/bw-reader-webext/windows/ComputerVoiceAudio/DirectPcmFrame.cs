using System.Buffers.Binary;
using System.Text;

namespace BwReader.ComputerVoiceAudio;

internal enum DirectPcmTrack : byte
{
    AppOutput = 1,
    UserMicrophone = 2,
}

internal sealed record DirectPcmFrame(
    DirectPcmTrack Track,
    uint Sequence,
    ulong TimestampMicroseconds,
    ReadOnlyMemory<byte> PcmS16Le);

internal static class DirectPcmFrameCodec
{
    private static readonly byte[] Magic = Encoding.ASCII.GetBytes("BWCV");

    internal static byte[] Encode(
        string sessionId,
        DirectPcmFrame frame)
    {
        byte[] sessionBytes = ParseSessionId(sessionId);
        if (
            frame.Track is not (
                DirectPcmTrack.AppOutput
                or DirectPcmTrack.UserMicrophone)
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
    private const int FramesPerTrack =
        DirectBridgeContract.PcmQueueLimitMilliseconds / 20;
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

        while (true)
        {
            DirectPcmFrame? frame;
            lock (_gate)
            {
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
