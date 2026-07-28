using System.Buffers.Binary;

namespace BwReader.ComputerVoiceAudio;

internal readonly record struct PcmFrameChunk(
    long Sequence,
    long TimestampUs,
    byte[] Data);

/// <summary>
/// Converts a verified 48 kHz WASAPI mix format into fixed 20 ms mono s16
/// chunks. Unsupported sample rates fail closed instead of silently invoking
/// an implicit device or lossy platform resampler.
/// </summary>
internal sealed class Pcm48kMonoFramer
{
    internal const int SampleRate = 48_000;
    internal const int FramesPerChunk = 960;
    internal const int BytesPerChunk = FramesPerChunk * sizeof(short);
    private const int MaximumQueuedChunks = 16;

    private readonly PcmAudioFormat _format;
    private readonly Queue<PcmFrameChunk> _chunks = new();
    private readonly short[] _pending = new short[FramesPerChunk * 2];
    private int _pendingCount;
    private long _sequence;

    internal Pcm48kMonoFramer(PcmAudioFormat format)
    {
        format.Validate();
        if (format.SamplesPerSecond != SampleRate)
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_AUDIO_SAMPLE_RATE_UNSUPPORTED");
        }

        bool supported = format.Encoding switch
        {
            PcmSampleEncoding.IeeeFloat =>
                format.BitsPerSample == 32,
            PcmSampleEncoding.IntegerPcm =>
                format.BitsPerSample is 16 or 24 or 32,
            _ => false,
        };
        if (!supported)
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_AUDIO_SAMPLE_FORMAT_UNSUPPORTED");
        }

        _format = format;
    }

    internal int QueuedChunks => _chunks.Count;

    internal void Push(PcmPacket packet)
    {
        int expectedBytes = checked(
            (int)packet.FrameCount * _format.BlockAlign);
        if (packet.Data.Length != expectedBytes)
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_AUDIO_PACKET_FORMAT_MISMATCH");
        }
        if (packet.TimestampError)
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_AUDIO_TIMESTAMP_INVALID");
        }
        if (packet.Discontinuous)
        {
            _pendingCount = 0;
        }

        ReadOnlySpan<byte> bytes = packet.Data.Span;
        int bytesPerSample = _format.BitsPerSample / 8;
        int frameCount = checked((int)packet.FrameCount);
        for (int frame = 0; frame < frameCount; frame++)
        {
            double mixed = 0;
            for (int channel = 0; channel < _format.Channels; channel++)
            {
                int offset = checked(
                    frame * _format.BlockAlign + channel * bytesPerSample);
                mixed += packet.Silent
                    ? 0
                    : DecodeSample(bytes.Slice(offset, bytesPerSample));
            }
            mixed /= _format.Channels;
            short sample = (short)Math.Clamp(
                (int)Math.Round(mixed * short.MaxValue),
                short.MinValue,
                short.MaxValue);
            Append(sample);
        }
    }

    internal bool TryRead(out PcmFrameChunk chunk) =>
        _chunks.TryDequeue(out chunk);

    private double DecodeSample(ReadOnlySpan<byte> value)
    {
        if (_format.Encoding == PcmSampleEncoding.IeeeFloat)
        {
            int bits = BinaryPrimitives.ReadInt32LittleEndian(value);
            float sample = BitConverter.Int32BitsToSingle(bits);
            if (!float.IsFinite(sample))
            {
                throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_AUDIO_NONFINITE_SAMPLE");
            }
            return Math.Clamp((double)sample, -1.0, 1.0);
        }

        return _format.BitsPerSample switch
        {
            16 => BinaryPrimitives.ReadInt16LittleEndian(value) / 32768.0,
            24 => DecodePcm24(value) / 8388608.0,
            32 => BinaryPrimitives.ReadInt32LittleEndian(value) / 2147483648.0,
            _ => throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_AUDIO_SAMPLE_FORMAT_UNSUPPORTED"),
        };
    }

    private static int DecodePcm24(ReadOnlySpan<byte> value)
    {
        int sample = value[0] | value[1] << 8 | value[2] << 16;
        if ((sample & 0x00800000) != 0)
        {
            sample |= unchecked((int)0xff000000);
        }
        return sample;
    }

    private void Append(short sample)
    {
        _pending[_pendingCount++] = sample;
        if (_pendingCount < FramesPerChunk)
        {
            return;
        }
        if (_chunks.Count >= MaximumQueuedChunks)
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_AUDIO_FRAME_BACKPRESSURE");
        }

        byte[] data = new byte[BytesPerChunk];
        for (int index = 0; index < FramesPerChunk; index++)
        {
            BinaryPrimitives.WriteInt16LittleEndian(
                data.AsSpan(index * sizeof(short), sizeof(short)),
                _pending[index]);
        }
        _chunks.Enqueue(new PcmFrameChunk(
            Sequence: _sequence,
            TimestampUs: checked(_sequence * 20_000),
            Data: data));
        _sequence++;

        int remaining = _pendingCount - FramesPerChunk;
        if (remaining > 0)
        {
            Array.Copy(
                _pending,
                FramesPerChunk,
                _pending,
                0,
                remaining);
        }
        _pendingCount = remaining;
    }
}
