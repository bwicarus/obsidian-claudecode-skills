using System.Runtime.InteropServices;
using BwReader.ComputerVoiceAudio.Interop;

namespace BwReader.ComputerVoiceAudio;

internal sealed class AudioCaptureStageException : Exception
{
    internal AudioCaptureStageException(
        string stage,
        int result,
        Exception? innerException = null)
        : base(
            $"BW_COMPUTER_VOICE_AUDIO_STAGE_FAILED:{stage}:"
                + $"0x{unchecked((uint)result):X8}",
            innerException)
    {
        if (
            string.IsNullOrWhiteSpace(stage)
            || stage.Length > 80
            || stage.Any(character =>
                !(character is >= 'a' and <= 'z')
                && character is not '-' and not '.')
        )
        {
            throw new ArgumentException(
                "audio diagnostic stage is invalid",
                nameof(stage));
        }
        Stage = stage;
        Result = result;
        HResult = result;
    }

    internal string Stage { get; }

    internal int Result { get; }

    internal string PublicDetail =>
        $"{Stage} / HRESULT 0x{unchecked((uint)Result):X8}";

    internal static AudioCaptureStageException From(
        string stage,
        Exception exception) =>
        exception as AudioCaptureStageException
        ?? new AudioCaptureStageException(
            stage,
            exception.HResult,
            exception);
}

internal interface INativeAudioClientLease : IDisposable
{
    IAudioClient AudioClient { get; }
}

// Both the target-process output path and the explicitly selected microphone
// path use this one native stream implementation. The virtual process-loopback
// client receives a caller-supplied PCM format because GetMixFormat is not
// implemented there; the microphone keeps its exact device mix format. Packet,
// backpressure and rollback rules remain deliberately shared.
internal sealed class SharedEventDrivenPcmRuntime : IDisposable
{
    private readonly CaptureThreadAffinity _threadAffinity = new();
    private readonly AudioClientStreamFlags _streamFlags;
    private readonly string _stagePrefix;
    private readonly WaveFormatEx? _fixedCaptureFormat;
    private INativeAudioClientLease? _lease;
    private NativeCapturePacketSource? _packetSource;
    private bool _initialized;
    private bool _startSucceeded;
    private bool _stopAttempted;

    internal SharedEventDrivenPcmRuntime(
        INativeAudioClientLease lease,
        AudioClientStreamFlags streamFlags,
        string stagePrefix,
        WaveFormatEx? fixedCaptureFormat = null)
    {
        ArgumentNullException.ThrowIfNull(lease);
        if (
            stagePrefix is not ("app-output" or "microphone")
        )
        {
            throw new ArgumentOutOfRangeException(nameof(stagePrefix));
        }
        if ((streamFlags & AudioClientStreamFlags.EventCallback) == 0
            || (streamFlags & ~(
                AudioClientStreamFlags.EventCallback
                | AudioClientStreamFlags.Loopback
                | AudioClientStreamFlags.AutoConvertPcm)) != 0)
        {
            throw new ArgumentOutOfRangeException(
                nameof(streamFlags),
                "BW_COMPUTER_VOICE_AUDIO_STREAM_FLAGS_INVALID");
        }

        bool processLoopback =
            (streamFlags & AudioClientStreamFlags.Loopback) != 0;
        bool autoConvertPcm =
            (streamFlags & AudioClientStreamFlags.AutoConvertPcm) != 0;
        if (
            processLoopback != fixedCaptureFormat.HasValue
            || processLoopback != autoConvertPcm
            || (processLoopback && stagePrefix != "app-output")
            || (!processLoopback && stagePrefix != "microphone")
        )
        {
            throw new ArgumentException(
                "BW_COMPUTER_VOICE_AUDIO_FORMAT_SOURCE_INVALID",
                nameof(fixedCaptureFormat));
        }

        _lease = lease;
        _streamFlags = streamFlags;
        _stagePrefix = stagePrefix;
        _fixedCaptureFormat = fixedCaptureFormat;
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
            if (_fixedCaptureFormat is WaveFormatEx fixedCaptureFormat)
            {
                try
                {
                    formatPointer =
                        Marshal.AllocCoTaskMem(Marshal.SizeOf<WaveFormatEx>());
                    Marshal.StructureToPtr(
                        fixedCaptureFormat,
                        formatPointer,
                        false);
                }
                catch (Exception exception)
                {
                    if (formatPointer != 0)
                    {
                        Marshal.FreeCoTaskMem(formatPointer);
                        formatPointer = 0;
                    }
                    throw AudioCaptureStageException.From(
                        Stage("marshal-fixed-format"),
                        exception);
                }
            }
            else
            {
                RequireSucceeded(
                    AudioClient.GetMixFormat(out formatPointer),
                    "get-mix-format");
            }
            PcmAudioFormat format;
            try
            {
                format = PcmAudioFormat.FromNative(formatPointer);
            }
            catch (Exception exception)
            {
                throw AudioCaptureStageException.From(
                    Stage(
                        _fixedCaptureFormat.HasValue
                            ? "parse-fixed-format"
                            : "parse-mix-format"),
                    exception);
            }

            RequireSucceeded(
                AudioClient.Initialize(
                    AudioClientShareMode.Shared,
                    (uint)_streamFlags,
                    bufferDuration: 0,
                    periodicity: 0,
                    format: formatPointer,
                    audioSessionGuid: 0),
                "initialize");
            _initialized = true;

            RequireSucceeded(
                AudioClient.GetBufferSize(out uint maximumFrameCount),
                "get-buffer-size");
            if (maximumFrameCount == 0)
            {
                throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_AUDIO_BUFFER_SIZE_INVALID");
            }

            RequireSucceeded(
                AudioClient.SetEventHandle(
                    audioReadyEvent.SafeWaitHandle.DangerousGetHandle()),
                "set-event-handle");

            Guid captureClientId =
                ProcessLoopbackInterop.IidIAudioCaptureClient;
            nint servicePointer = 0;
            try
            {
                int serviceResult = AudioClient.GetService(
                    ref captureClientId,
                    out servicePointer);
                RequireSucceeded(serviceResult, "get-capture-service");
                try
                {
                    captureObject =
                        Marshal.GetObjectForIUnknown(servicePointer);
                }
                catch (Exception exception)
                {
                    throw AudioCaptureStageException.From(
                        Stage("marshal-capture-service"),
                        exception);
                }
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
                throw new AudioCaptureStageException(
                    Stage("validate-capture-service"),
                    unchecked((int)0x80004002));
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

        RequireSucceeded(AudioClient.Start(), "start");
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

    private string Stage(string operation) =>
        $"{_stagePrefix}.{operation}";

    private void RequireSucceeded(int result, string operation)
    {
        if (result != ProcessLoopbackInterop.Succeeded)
        {
            throw new AudioCaptureStageException(
                Stage(operation),
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
