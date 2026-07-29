using System.Runtime.InteropServices;

namespace BwReader.ComputerVoiceAudio;

internal static class DirectAudioDiagnostics
{
    private const string Contract =
        "reader-computer-voice-audio-diagnostic/2";

    internal static async Task<object> RunAsync(
        DirectBridgeConfig config,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(config);
        if (!OperatingSystem.IsWindows())
        {
            throw new PlatformNotSupportedException(
                "BW_COMPUTER_VOICE_AUDIO_WINDOWS_REQUIRED");
        }
        if (!config.LocalOptIn)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_DISABLED",
                "Windows 电脑语音未启用");
        }

        CodexAppTarget target = WindowsCodexAppProbe.RequireReady();
        StreamDiagnostic output = await ProbeOnMtaAsync(
            "app-output",
            () => new NativeProcessLoopbackCaptureRuntimeFactory().Create(
                target.RootProcessId,
                CaptureSessionOptions.Default.ActivationTimeout,
                cancellationToken),
            cancellationToken).ConfigureAwait(false);
        StreamDiagnostic virtualMicrophoneRender =
            await ProbeRenderOnMtaAsync(
                config.VirtualMicrophoneRenderEndpointId,
                cancellationToken).ConfigureAwait(false);
        StreamDiagnostic virtualSpeakerEndpoint =
            await ProbeRenderEndpointOnMtaAsync(
                config.VirtualSpeakerRenderEndpointId,
                "virtual-speaker",
                cancellationToken).ConfigureAwait(false);

        return new
        {
            contract = Contract,
            ok = output.Ok
                && virtualMicrophoneRender.Ok
                && virtualSpeakerEndpoint.Ok,
            captureStarted = false,
            shortcutSent = false,
            output,
            virtualMicrophoneRender,
            virtualSpeakerEndpoint,
        };
    }

    private static Task<StreamDiagnostic> ProbeRenderOnMtaAsync(
        string endpointId,
        CancellationToken cancellationToken)
    {
        TaskCompletionSource<StreamDiagnostic> completion =
            new(TaskCreationOptions.RunContinuationsAsynchronously);
        Thread thread = new(() =>
        {
            IVirtualMicrophoneRenderRuntime? runtime = null;
            EventWaitHandle? audioReady = null;
            ComMtaLease? apartment = null;
            try
            {
                cancellationToken.ThrowIfCancellationRequested();
                apartment = ComMtaLease.Enter();
                audioReady = new EventWaitHandle(
                    false,
                    EventResetMode.AutoReset);
                runtime =
                    new NativeVirtualMicrophoneRenderRuntimeFactory()
                        .Create(
                            VirtualMicrophoneRenderRequest.Create(
                                endpointId),
                            cancellationToken);
                runtime.Initialize(audioReady);
                // Deliberately do not call Start().
                runtime.Stop();
                completion.TrySetResult(SuccessfulRenderDiagnostic());
            }
            catch (OperationCanceledException)
                when (cancellationToken.IsCancellationRequested)
            {
                completion.TrySetCanceled(cancellationToken);
            }
            catch (Exception exception)
            {
                completion.TrySetResult(FailedDiagnostic(
                    "virtual-microphone",
                    exception));
            }
            finally
            {
                try
                {
                    runtime?.Dispose();
                }
                catch
                {
                }
                audioReady?.Dispose();
                apartment?.Dispose();
            }
        })
        {
            IsBackground = true,
            Name = "BW audio diagnostic virtual-microphone",
        };
        if (OperatingSystem.IsWindows())
        {
            thread.SetApartmentState(ApartmentState.MTA);
        }
        thread.Start();
        return completion.Task;
    }

    private static Task<StreamDiagnostic>
        ProbeRenderEndpointOnMtaAsync(
            string endpointId,
            string stagePrefix,
            CancellationToken cancellationToken)
    {
        TaskCompletionSource<StreamDiagnostic> completion =
            new(TaskCreationOptions.RunContinuationsAsynchronously);
        Thread thread = new(() =>
        {
            try
            {
                cancellationToken.ThrowIfCancellationRequested();
                VirtualRenderEndpointProbe.ValidateExactActiveRender(
                    endpointId,
                    stagePrefix);
                completion.TrySetResult(new StreamDiagnostic(
                    Ok: true,
                    Stage: "virtual-speaker-endpoint-ready",
                    HResult: "0x00000000",
                    SampleRate: 0,
                    Channels: 0,
                    BitsPerSample: 0));
            }
            catch (OperationCanceledException)
                when (cancellationToken.IsCancellationRequested)
            {
                completion.TrySetCanceled(cancellationToken);
            }
            catch (Exception exception)
            {
                completion.TrySetResult(FailedDiagnostic(
                    stagePrefix,
                    exception));
            }
        })
        {
            IsBackground = true,
            Name = $"BW audio diagnostic {stagePrefix}",
        };
        if (OperatingSystem.IsWindows())
        {
            thread.SetApartmentState(ApartmentState.MTA);
        }
        thread.Start();
        return completion.Task;
    }

    private static StreamDiagnostic SuccessfulRenderDiagnostic() =>
        new(
            Ok: true,
            Stage: "initialized-without-start",
            HResult: "0x00000000",
            SampleRate: Pcm48kMonoFramer.SampleRate,
            Channels: 1,
            BitsPerSample: 16);

    private static StreamDiagnostic FailedDiagnostic(
        string stream,
        Exception exception)
    {
        AudioCaptureStageException failure =
            FindAudioStageFailure(exception)
            ?? AudioCaptureStageException.From(
                $"{stream}.diagnostic",
                exception);
        return new StreamDiagnostic(
            Ok: false,
            Stage: failure.Stage,
            HResult: $"0x{unchecked((uint)failure.Result):X8}",
            SampleRate: 0,
            Channels: 0,
            BitsPerSample: 0);
    }

    private static Task<StreamDiagnostic> ProbeOnMtaAsync(
        string stream,
        Func<IProcessLoopbackCaptureRuntime> createRuntime,
        CancellationToken cancellationToken)
    {
        TaskCompletionSource<StreamDiagnostic> completion =
            new(TaskCreationOptions.RunContinuationsAsynchronously);
        Thread thread = new(() =>
        {
            IProcessLoopbackCaptureRuntime? runtime = null;
            EventWaitHandle? audioReady = null;
            ComMtaLease? apartment = null;
            try
            {
                cancellationToken.ThrowIfCancellationRequested();
                apartment = ComMtaLease.Enter();
                audioReady = new EventWaitHandle(
                    false,
                    EventResetMode.AutoReset);
                runtime = createRuntime();
                PcmAudioFormat format = runtime.Initialize(audioReady);

                // Deliberately never call runtime.Start(). Stop only resets the
                // initialized IAudioClient and makes the diagnostic reversible.
                runtime.Stop();
                completion.TrySetResult(new StreamDiagnostic(
                    Ok: true,
                    Stage: "initialized-without-start",
                    HResult: "0x00000000",
                    SampleRate: format.SamplesPerSecond,
                    Channels: format.Channels,
                    BitsPerSample: format.BitsPerSample));
            }
            catch (OperationCanceledException)
                when (cancellationToken.IsCancellationRequested)
            {
                completion.TrySetCanceled(cancellationToken);
            }
            catch (Exception exception)
            {
                AudioCaptureStageException failure =
                    FindAudioStageFailure(exception)
                    ?? AudioCaptureStageException.From(
                        $"{stream}.diagnostic",
                        exception);
                completion.TrySetResult(new StreamDiagnostic(
                    Ok: false,
                    Stage: failure.Stage,
                    HResult:
                        $"0x{unchecked((uint)failure.Result):X8}",
                    SampleRate: 0,
                    Channels: 0,
                    BitsPerSample: 0));
            }
            finally
            {
                if (runtime is not null)
                {
                    try
                    {
                        runtime.Dispose();
                    }
                    catch
                    {
                    }
                }
                audioReady?.Dispose();
                apartment?.Dispose();
            }
        })
        {
            IsBackground = true,
            Name = $"BW audio diagnostic {stream}",
        };
        if (OperatingSystem.IsWindows())
        {
            thread.SetApartmentState(ApartmentState.MTA);
        }
        thread.Start();
        return completion.Task;
    }

    private static AudioCaptureStageException? FindAudioStageFailure(
        Exception exception)
    {
        if (exception is AudioCaptureStageException stage)
        {
            return stage;
        }
        if (exception is AggregateException aggregate)
        {
            foreach (Exception inner in aggregate.Flatten().InnerExceptions)
            {
                AudioCaptureStageException? found =
                    FindAudioStageFailure(inner);
                if (found is not null)
                {
                    return found;
                }
            }
        }
        return exception.InnerException is null
            ? null
            : FindAudioStageFailure(exception.InnerException);
    }

    private sealed record StreamDiagnostic(
        bool Ok,
        string Stage,
        string HResult,
        uint SampleRate,
        ushort Channels,
        ushort BitsPerSample);
}

internal sealed class ComMtaLease : IDisposable
{
    private const uint CoInitMultithreaded = 0;
    private bool _mustUninitialize;

    private ComMtaLease(bool mustUninitialize)
    {
        _mustUninitialize = mustUninitialize;
    }

    internal static ComMtaLease Enter()
    {
        int result = CoInitializeEx(0, CoInitMultithreaded);
        if (result < 0)
        {
            throw new AudioCaptureStageException(
                "com.initialize-mta",
                result,
                Marshal.GetExceptionForHR(result));
        }
        return new ComMtaLease(mustUninitialize: true);
    }

    public void Dispose()
    {
        if (!_mustUninitialize)
        {
            return;
        }
        _mustUninitialize = false;
        CoUninitialize();
    }

    [DllImport("ole32.dll", ExactSpelling = true)]
    private static extern int CoInitializeEx(
        nint reserved,
        uint coInit);

    [DllImport("ole32.dll", ExactSpelling = true)]
    private static extern void CoUninitialize();
}
