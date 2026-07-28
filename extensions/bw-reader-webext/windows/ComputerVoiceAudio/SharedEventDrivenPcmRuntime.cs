using System.Runtime.InteropServices;
using BwReader.ComputerVoiceAudio.Interop;

namespace BwReader.ComputerVoiceAudio;

internal interface INativeAudioClientLease : IDisposable
{
    IAudioClient AudioClient { get; }
}

// Both the target-process output path and the explicitly selected microphone
// path use this one native stream implementation. The caller supplies the
// stream flags; every other format, packet, backpressure and rollback rule is
// deliberately shared.
internal sealed class SharedEventDrivenPcmRuntime : IDisposable
{
    private readonly CaptureThreadAffinity _threadAffinity = new();
    private readonly AudioClientStreamFlags _streamFlags;
    private INativeAudioClientLease? _lease;
    private NativeCapturePacketSource? _packetSource;
    private bool _initialized;
    private bool _startSucceeded;
    private bool _stopAttempted;

    internal SharedEventDrivenPcmRuntime(
        INativeAudioClientLease lease,
        AudioClientStreamFlags streamFlags)
    {
        ArgumentNullException.ThrowIfNull(lease);
        if ((streamFlags & AudioClientStreamFlags.EventCallback) == 0
            || (streamFlags & ~(
                AudioClientStreamFlags.EventCallback
                | AudioClientStreamFlags.Loopback)) != 0)
        {
            throw new ArgumentOutOfRangeException(
                nameof(streamFlags),
                "BW_COMPUTER_VOICE_AUDIO_STREAM_FLAGS_INVALID");
        }

        _lease = lease;
        _streamFlags = streamFlags;
    }

    private IAudioClient AudioClient =>
        _lease?.AudioClient
        ?? throw new ObjectDisposedException(
            nameof(SharedEventDrivenPcmRuntime));

    internal PcmAudioFormat Initialize(EventWaitHandle audioReadyEvent)
    {
        ArgumentNullException.ThrowIfNull(audioReadyEvent);
        if (!OperatingSystem.IsWindows())
        {
            throw new PlatformNotSupportedException(
                "BW_COMPUTER_VOICE_AUDIO_WINDOWS_REQUIRED");
        }

        _threadAffinity.BindOrAssertCurrentThread();
        if (_initialized)
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_AUDIO_RUNTIME_ALREADY_INITIALIZED");
        }

        nint formatPointer = 0;
        object? captureObject = null;
        try
        {
            RequireSucceeded(
                AudioClient.GetMixFormat(out formatPointer),
                "GET_MIX_FORMAT");
            PcmAudioFormat format = PcmAudioFormat.FromNative(formatPointer);

            RequireSucceeded(
                AudioClient.Initialize(
                    AudioClientShareMode.Shared,
                    (uint)_streamFlags,
                    bufferDuration: 0,
                    periodicity: 0,
                    format: formatPointer,
                    audioSessionGuid: 0),
                "INITIALIZE");
            _initialized = true;

            RequireSucceeded(
                AudioClient.GetBufferSize(out uint maximumFrameCount),
                "GET_BUFFER_SIZE");
            if (maximumFrameCount == 0)
            {
                throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_AUDIO_BUFFER_SIZE_INVALID");
            }

            RequireSucceeded(
                AudioClient.SetEventHandle(
                    audioReadyEvent.SafeWaitHandle.DangerousGetHandle()),
                "SET_EVENT_HANDLE");

            Guid captureClientId =
                ProcessLoopbackInterop.IidIAudioCaptureClient;
            nint servicePointer = 0;
            try
            {
                int serviceResult = AudioClient.GetService(
                    ref captureClientId,
                    out servicePointer);
                RequireSucceeded(serviceResult, "GET_CAPTURE_SERVICE");
                captureObject = Marshal.GetObjectForIUnknown(servicePointer);
            }
            finally
            {
                if (servicePointer != 0)
                {
                    _ = Marshal.Release(servicePointer);
                }
            }

            if (captureObject is not IAudioCaptureClient captureClient)
            {
                throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_AUDIO_CAPTURE_CLIENT_INVALID");
            }

            _packetSource = new NativeCapturePacketSource(
                captureClient,
                _threadAffinity,
                maximumFrameCount)
            {
                Format = format,
            };
            captureObject = null;
            return format;
        }
        catch (Exception exception)
        {
            ReleaseComObject(captureObject);
            Exception? rollbackError = RollbackInitializedStream();
            if (rollbackError is not null)
            {
                throw new AggregateException(exception, rollbackError);
            }

            throw;
        }
        finally
        {
            if (formatPointer != 0)
            {
                Marshal.FreeCoTaskMem(formatPointer);
            }
        }
    }

    internal void Start()
    {
        _threadAffinity.BindOrAssertCurrentThread();
        if (!_initialized || _packetSource is null)
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_AUDIO_RUNTIME_NOT_INITIALIZED");
        }

        if (_startSucceeded)
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_AUDIO_RUNTIME_ALREADY_STARTED");
        }

        RequireSucceeded(AudioClient.Start(), "START");
        _startSucceeded = true;
    }

    internal int Drain(
        IBoundedPcmSink sink,
        CaptureSessionOptions options)
    {
        _threadAffinity.BindOrAssertCurrentThread();
        NativeCapturePacketSource source =
            _packetSource
            ?? throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_AUDIO_RUNTIME_NOT_INITIALIZED");
        return PcmPacketPump.DrainAvailable(
            source,
            source.Format
                ?? throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_AUDIO_FORMAT_NOT_BOUND"),
            sink,
            options);
    }

    internal void Stop()
    {
        _threadAffinity.BindOrAssertCurrentThread();
        if (!_initialized || _stopAttempted)
        {
            return;
        }

        _stopAttempted = true;
        Exception? stopError = RollbackInitializedStream();
        if (stopError is not null)
        {
            throw stopError;
        }
    }

    private Exception? RollbackInitializedStream()
    {
        if (!_initialized)
        {
            return null;
        }

        Exception? rollbackError = null;
        if (_startSucceeded)
        {
            try
            {
                RequireSucceeded(AudioClient.Stop(), "STOP");
            }
            catch (Exception stopError)
            {
                rollbackError = stopError;
            }
        }

        try
        {
            RequireSucceeded(AudioClient.Reset(), "RESET");
        }
        catch (Exception resetError)
        {
            rollbackError = rollbackError is null
                ? resetError
                : new AggregateException(rollbackError, resetError);
        }

        _initialized = false;
        _startSucceeded = false;
        return rollbackError;
    }

    public void Dispose()
    {
        _threadAffinity.BindOrAssertCurrentThread();
        Exception? cleanupError = null;
        if (_initialized && !_stopAttempted)
        {
            _stopAttempted = true;
            cleanupError = RollbackInitializedStream();
        }

        try
        {
            _packetSource?.Dispose();
        }
        catch (Exception packetSourceError)
        {
            cleanupError = cleanupError is null
                ? packetSourceError
                : new AggregateException(cleanupError, packetSourceError);
        }

        _packetSource = null;
        try
        {
            _lease?.Dispose();
        }
        catch (Exception audioClientError)
        {
            cleanupError = cleanupError is null
                ? audioClientError
                : new AggregateException(cleanupError, audioClientError);
        }

        _lease = null;
        if (cleanupError is not null)
        {
            throw cleanupError;
        }
    }

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
