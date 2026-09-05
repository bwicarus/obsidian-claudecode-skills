using System.Runtime.ExceptionServices;
using System.Runtime.InteropServices;
using BwReader.ComputerVoiceAudio.Interop;

namespace BwReader.ComputerVoiceAudio;

internal sealed record CaptureSessionOptions
{
    internal static CaptureSessionOptions Default { get; } = new();

    internal TimeSpan ActivationTimeout { get; init; } = TimeSpan.FromSeconds(10);

    internal int MaximumPacketBytes { get; init; } = 1 * 1024 * 1024;

    internal int MaximumPacketsPerWake { get; init; } = 256;

    internal void Validate()
    {
        if (ActivationTimeout <= TimeSpan.Zero)
        {
            throw new ArgumentOutOfRangeException(
                nameof(ActivationTimeout),
                "BW_COMPUTER_VOICE_AUDIO_ACTIVATION_TIMEOUT_INVALID");
        }

        if (MaximumPacketBytes <= 0)
        {
            throw new ArgumentOutOfRangeException(
                nameof(MaximumPacketBytes),
                "BW_COMPUTER_VOICE_AUDIO_PACKET_LIMIT_INVALID");
        }

        if (MaximumPacketsPerWake <= 0)
        {
            throw new ArgumentOutOfRangeException(
                nameof(MaximumPacketsPerWake),
                "BW_COMPUTER_VOICE_AUDIO_DRAIN_LIMIT_INVALID");
        }
    }
}

internal enum PcmSampleEncoding
{
    IntegerPcm,
    IeeeFloat,
}

internal readonly record struct PcmAudioFormat(
    PcmSampleEncoding Encoding,
    ushort FormatTag,
    ushort Channels,
    uint SamplesPerSecond,
    uint AverageBytesPerSecond,
    ushort BlockAlign,
    ushort BitsPerSample,
    ushort ValidBitsPerSample,
    ushort ExtraSize,
    uint ChannelMask,
    Guid SubFormat)
{
    internal const ushort WaveFormatPcm = 0x0001;
    internal const ushort WaveFormatIeeeFloat = 0x0003;
    internal const ushort WaveFormatExtensible = 0xfffe;

    internal static readonly Guid SubtypePcm =
        new("00000001-0000-0010-8000-00aa00389b71");

    internal static readonly Guid SubtypeIeeeFloat =
        new("00000003-0000-0010-8000-00aa00389b71");

    internal static PcmAudioFormat FromNative(nint formatPointer)
    {
        if (formatPointer == 0)
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_AUDIO_MIX_FORMAT_MISSING");
        }

        WaveFormatEx native = Marshal.PtrToStructure<WaveFormatEx>(formatPointer);
        PcmAudioFormat format;
        if (native.FormatTag == WaveFormatPcm)
        {
            format = new(
                PcmSampleEncoding.IntegerPcm,
                native.FormatTag,
                native.Channels,
                native.SamplesPerSecond,
                native.AverageBytesPerSecond,
                native.BlockAlign,
                native.BitsPerSample,
                native.BitsPerSample,
                native.ExtraSize,
                ChannelMask: 0,
                SubtypePcm);
        }
        else if (native.FormatTag == WaveFormatIeeeFloat)
        {
            format = new(
                PcmSampleEncoding.IeeeFloat,
                native.FormatTag,
                native.Channels,
                native.SamplesPerSecond,
                native.AverageBytesPerSecond,
                native.BlockAlign,
                native.BitsPerSample,
                native.BitsPerSample,
                native.ExtraSize,
                ChannelMask: 0,
                SubtypeIeeeFloat);
        }
        else if (native.FormatTag == WaveFormatExtensible
            && native.ExtraSize >= 22)
        {
            WaveFormatExtensible extensible =
                Marshal.PtrToStructure<WaveFormatExtensible>(formatPointer);
            PcmSampleEncoding encoding = extensible.SubFormat switch
            {
                Guid value when value == SubtypePcm =>
                    PcmSampleEncoding.IntegerPcm,
                Guid value when value == SubtypeIeeeFloat =>
                    PcmSampleEncoding.IeeeFloat,
                _ => throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_AUDIO_MIX_SUBFORMAT_UNSUPPORTED"),
            };
            format = new(
                encoding,
                native.FormatTag,
                native.Channels,
                native.SamplesPerSecond,
                native.AverageBytesPerSecond,
                native.BlockAlign,
                native.BitsPerSample,
                extensible.ValidBitsPerSample,
                native.ExtraSize,
                extensible.ChannelMask,
                extensible.SubFormat);
        }
        else
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_AUDIO_MIX_FORMAT_UNSUPPORTED");
        }

        format.Validate();
        return format;
    }

    internal void Validate()
    {
        if (FormatTag is not (
                WaveFormatPcm
                or WaveFormatIeeeFloat
                or WaveFormatExtensible)
            || Channels == 0
            || Channels > 32
            || SamplesPerSecond == 0
            || SamplesPerSecond > 768000
            || AverageBytesPerSecond == 0
            || BlockAlign == 0
            || BitsPerSample == 0
            || BitsPerSample > 64
            || BitsPerSample % 8 != 0
            || ValidBitsPerSample == 0
            || ValidBitsPerSample > BitsPerSample)
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_AUDIO_MIX_FORMAT_INVALID");
        }

        uint expectedBlockAlign = checked(
            (uint)Channels * ((uint)BitsPerSample / 8u));
        uint expectedBytesPerSecond = checked(
            SamplesPerSecond * BlockAlign);
        if (BlockAlign != expectedBlockAlign
            || AverageBytesPerSecond != expectedBytesPerSecond)
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_AUDIO_MIX_FORMAT_RATE_INVALID");
        }

        if ((Encoding == PcmSampleEncoding.IntegerPcm
                && SubFormat != SubtypePcm)
            || (Encoding == PcmSampleEncoding.IeeeFloat
                && SubFormat != SubtypeIeeeFloat)
            || (Encoding == PcmSampleEncoding.IeeeFloat
                && BitsPerSample != 32)
            || (FormatTag == WaveFormatExtensible && ExtraSize < 22))
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_AUDIO_MIX_SUBFORMAT_UNSUPPORTED");
        }
    }
}

internal readonly record struct PcmPacket(
    ReadOnlyMemory<byte> Data,
    uint FrameCount,
    bool Silent,
    bool Discontinuous,
    bool TimestampError,
    ulong DevicePosition,
    ulong QpcPosition);

internal interface IBoundedPcmSink
{
    bool TryWrite(PcmPacket packet);

    void Complete(Exception? error);
}

// The queue is deliberately bounded by both packet count and byte count. A
// full queue never blocks the dedicated WASAPI thread: TryWrite returns false
// and the capture session fails closed instead of growing memory or silently
// dropping audio.
internal sealed class BoundedPcmPacketQueue : IBoundedPcmSink
{
    private readonly object _gate = new();
    private readonly Queue<PcmPacket> _packets = new();
    private readonly int _maximumPackets;
    private readonly int _maximumBytes;
    private readonly bool _dropOldestWhenFull;
    private int _queuedBytes;
    private long _droppedPackets;
    private bool _completed;
    private Exception? _completionError;

    /// dropOldestWhenFull（2026-09-06 审计 C04）：AI 声音的下行队列满了不再 fail-closed。
    /// 原策略是"满了 = 整条媒体拆掉"，而它满的时刻恰恰是 AI 正在出声、App 那边收得慢
    /// （网络抖一下）—— 一次 320 ms 的下行卡顿就把用户的上行也一起拆了。AI 的声音
    /// 少几个包只是听到一小格空白；用户的麦克风没了才是事故。麦克风上行队列另有
    /// 自己的策略（BoundedUplinkPcmQueue），不走这里。
    internal BoundedPcmPacketQueue(
        int maximumPackets,
        int maximumBytes,
        bool dropOldestWhenFull = false)
    {
        if (maximumPackets <= 0)
        {
            throw new ArgumentOutOfRangeException(nameof(maximumPackets));
        }

        if (maximumBytes <= 0)
        {
            throw new ArgumentOutOfRangeException(nameof(maximumBytes));
        }

        _maximumPackets = maximumPackets;
        _maximumBytes = maximumBytes;
        _dropOldestWhenFull = dropOldestWhenFull;
    }

    internal long DroppedPackets
    {
        get
        {
            lock (_gate)
            {
                return _droppedPackets;
            }
        }
    }

    internal int Count
    {
        get
        {
            lock (_gate)
            {
                return _packets.Count;
            }
        }
    }

    internal int QueuedBytes
    {
        get
        {
            lock (_gate)
            {
                return _queuedBytes;
            }
        }
    }

    internal bool IsCompleted
    {
        get
        {
            lock (_gate)
            {
                return _completed;
            }
        }
    }

    internal Exception? CompletionError
    {
        get
        {
            lock (_gate)
            {
                return _completionError;
            }
        }
    }

    public bool TryWrite(PcmPacket packet)
    {
        int packetBytes = packet.Data.Length;
        lock (_gate)
        {
            if (_completed)
            {
                return false;
            }
            if (_dropOldestWhenFull)
            {
                // 单个包本身就超过整个上限的，谁也救不了；照旧拒收。
                if (packetBytes > _maximumBytes)
                {
                    return false;
                }
                while (
                    _packets.Count != 0
                    && (
                        _packets.Count >= _maximumPackets
                        || packetBytes > _maximumBytes - _queuedBytes
                    )
                )
                {
                    PcmPacket stale = _packets.Dequeue();
                    _queuedBytes -= stale.Data.Length;
                    _droppedPackets += 1;
                }
            }
            else if (
                _packets.Count >= _maximumPackets
                || packetBytes > _maximumBytes - _queuedBytes)
            {
                return false;
            }

            _packets.Enqueue(packet);
            _queuedBytes += packetBytes;
            return true;
        }
    }

    internal bool TryRead(out PcmPacket packet)
    {
        lock (_gate)
        {
            if (!_packets.TryDequeue(out packet))
            {
                return false;
            }

            _queuedBytes -= packet.Data.Length;
            return true;
        }
    }

    public void Complete(Exception? error)
    {
        lock (_gate)
        {
            if (_completed)
            {
                return;
            }

            _completed = true;
            _completionError = error;
        }
    }
}

internal readonly record struct NativeCapturePacket(
    nint Data,
    uint FrameCount,
    AudioClientBufferFlags Flags,
    ulong DevicePosition,
    ulong QpcPosition);

internal interface ICapturePacketSource : IDisposable
{
    uint MaximumFrameCount { get; }

    uint GetNextPacketSize();

    bool TryGetBuffer(out NativeCapturePacket packet);

    void ReleaseBuffer(uint frameCount);
}

internal static class PcmPacketPump
{
    internal static int DrainAvailable(
        ICapturePacketSource source,
        PcmAudioFormat format,
        IBoundedPcmSink sink,
        CaptureSessionOptions options)
    {
        ArgumentNullException.ThrowIfNull(source);
        ArgumentNullException.ThrowIfNull(sink);
        options.Validate();
        format.Validate();

        int packetsWritten = 0;
        while (source.GetNextPacketSize() != 0)
        {
            if (packetsWritten >= options.MaximumPacketsPerWake)
            {
                throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_AUDIO_DRAIN_LIMIT_EXCEEDED");
            }

            if (!source.TryGetBuffer(out NativeCapturePacket packet))
            {
                // AUDCLNT_S_BUFFER_EMPTY did not lease a packet. It is the
                // one successful GetBuffer result that must not be paired
                // with ReleaseBuffer.
                break;
            }

            Exception? packetError = null;
            try
            {
                if (packet.FrameCount > source.MaximumFrameCount)
                {
                    throw new InvalidOperationException(
                        "BW_COMPUTER_VOICE_AUDIO_FRAME_COUNT_EXCEEDED");
                }

                PcmPacket copied = CopyPacket(packet, format, options);
                if (!sink.TryWrite(copied))
                {
                    throw new InvalidOperationException(
                        "BW_COMPUTER_VOICE_AUDIO_SINK_BACKPRESSURE");
                }

                packetsWritten++;
            }
            catch (Exception exception)
            {
                packetError = exception;
            }

            try
            {
                // Every successful GetBuffer is paired exactly once, even if
                // copying or the bounded sink fails.
                source.ReleaseBuffer(packet.FrameCount);
            }
            catch (Exception releaseError)
            {
                if (packetError is not null)
                {
                    throw new AggregateException(packetError, releaseError);
                }

                throw;
            }

            if (packetError is not null)
            {
                ExceptionDispatchInfo.Capture(packetError).Throw();
            }
        }

        return packetsWritten;
    }

    private static PcmPacket CopyPacket(
        NativeCapturePacket packet,
        PcmAudioFormat format,
        CaptureSessionOptions options)
    {
        const AudioClientBufferFlags knownFlags =
            AudioClientBufferFlags.DataDiscontinuity
            | AudioClientBufferFlags.Silent
            | AudioClientBufferFlags.TimestampError;
        if ((packet.Flags & ~knownFlags) != 0)
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_AUDIO_PACKET_FLAGS_UNSUPPORTED");
        }

        int byteCount;
        try
        {
            byteCount = checked((int)(packet.FrameCount * format.BlockAlign));
        }
        catch (OverflowException exception)
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_AUDIO_PACKET_SIZE_OVERFLOW",
                exception);
        }

        if (byteCount > options.MaximumPacketBytes)
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_AUDIO_PACKET_TOO_LARGE");
        }

        bool silent = (packet.Flags & AudioClientBufferFlags.Silent) != 0;
        byte[] data = new byte[byteCount];
        if (!silent && byteCount != 0)
        {
            if (packet.Data == 0)
            {
                throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_AUDIO_PACKET_DATA_MISSING");
            }

            Marshal.Copy(packet.Data, data, 0, byteCount);
        }

        bool timestampError =
            (packet.Flags & AudioClientBufferFlags.TimestampError) != 0;
        return new PcmPacket(
            data,
            packet.FrameCount,
            silent,
            (packet.Flags & AudioClientBufferFlags.DataDiscontinuity) != 0,
            timestampError,
            timestampError ? 0 : packet.DevicePosition,
            timestampError ? 0 : packet.QpcPosition);
    }
}
