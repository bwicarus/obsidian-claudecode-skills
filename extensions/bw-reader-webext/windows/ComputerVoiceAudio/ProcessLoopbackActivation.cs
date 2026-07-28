using System.Runtime.InteropServices;
using BwReader.ComputerVoiceAudio.Interop;

namespace BwReader.ComputerVoiceAudio;

internal static class ProcessLoopbackActivation
{
    internal static AudioClientActivationParams BuildParameters(uint targetProcessId)
    {
        if (targetProcessId == 0)
        {
            throw new ArgumentOutOfRangeException(
                nameof(targetProcessId),
                "BW_COMPUTER_VOICE_AUDIO_TARGET_PID_REQUIRED");
        }

        return new AudioClientActivationParams
        {
            ActivationType = AudioClientActivationType.ProcessLoopback,
            ProcessLoopbackParams = new AudioClientProcessLoopbackParams
            {
                TargetProcessId = targetProcessId,
                ProcessLoopbackMode = ProcessLoopbackMode.IncludeTargetProcessTree,
            },
        };
    }

    // This is the native activation seam for the future capture transport.
    // No current CLI command calls it: --describe and --self-test stay pure.
    internal static async Task<ActivatedProcessAudioClient> ActivateAsync(
        uint targetProcessId,
        TimeSpan timeout,
        CancellationToken cancellationToken)
    {
        EnsureSupportedWindows();

        if (timeout <= TimeSpan.Zero)
        {
            throw new ArgumentOutOfRangeException(nameof(timeout));
        }

        PendingActivation pending = new(BuildParameters(targetProcessId));
        pending.Start();

        // A caller timeout/cancellation only stops this wait. PendingActivation
        // remains rooted by the native callback and does not free the
        // PROPVARIANT/BLOB or operation RCW until that callback completes.
        try
        {
            IAudioClient audioClient = await pending.Result
                .WaitAsync(timeout, cancellationToken)
                .ConfigureAwait(false);
            return new ActivatedProcessAudioClient(targetProcessId, audioClient);
        }
        catch (TimeoutException)
        {
            pending.Abandon();
            throw;
        }
        catch (OperationCanceledException)
        {
            pending.Abandon();
            throw;
        }
    }

    private static void EnsureSupportedWindows()
    {
        if (!OperatingSystem.IsWindows())
        {
            throw new PlatformNotSupportedException(
                "BW_COMPUTER_VOICE_AUDIO_WINDOWS_REQUIRED");
        }

        if (Environment.OSVersion.Version.Build < AudioBridgeContract.MinimumWindowsBuild)
        {
            throw new PlatformNotSupportedException(
                "BW_COMPUTER_VOICE_AUDIO_PROCESS_LOOPBACK_UNSUPPORTED");
        }
    }

    private sealed class ActivationBuffers : IDisposable
    {
        private nint _activationPointer;
        private nint _propVariantPointer;

        internal ActivationBuffers(AudioClientActivationParams activation)
        {
            _activationPointer = Marshal.AllocCoTaskMem(
                Marshal.SizeOf<AudioClientActivationParams>());
            Marshal.StructureToPtr(activation, _activationPointer, false);

            PropVariant propVariant = new()
            {
                VariantType = ProcessLoopbackInterop.VariantTypeBlob,
                Blob = new Blob
                {
                    Size = checked((uint)Marshal.SizeOf<AudioClientActivationParams>()),
                    Data = _activationPointer,
                },
            };

            _propVariantPointer = Marshal.AllocCoTaskMem(
                Marshal.SizeOf<PropVariant>());
            Marshal.StructureToPtr(propVariant, _propVariantPointer, false);
        }

        internal nint PropVariantPointer =>
            _propVariantPointer != 0
                ? _propVariantPointer
                : throw new ObjectDisposedException(nameof(ActivationBuffers));

        public void Dispose()
        {
            if (_propVariantPointer != 0)
            {
                Marshal.FreeCoTaskMem(_propVariantPointer);
                _propVariantPointer = 0;
            }

            if (_activationPointer != 0)
            {
                Marshal.FreeCoTaskMem(_activationPointer);
                _activationPointer = 0;
            }
        }
    }

    private sealed class PendingActivation
    {
        private readonly ActivationBuffers _buffers;
        private readonly ActivationCompletionHandler _completionHandler;
        private readonly ActivationCleanupGate _cleanupGate;
        private IActivateAudioInterfaceAsyncOperation? _operation;
        private int _abandoned;

        internal PendingActivation(AudioClientActivationParams activation)
        {
            _buffers = new ActivationBuffers(activation);
            _completionHandler = new ActivationCompletionHandler();
            _cleanupGate = new ActivationCleanupGate(CleanupNativeResources);
            _ = FinalizeNativeCompletionAsync();
        }

        internal Task<IAudioClient> Result => _completionHandler.Result;

        internal void Start()
        {
            Guid iid = ProcessLoopbackInterop.IidIAudioClient;
            int callResult;

            try
            {
                callResult = ProcessLoopbackInterop.ActivateAudioInterfaceAsync(
                    ProcessLoopbackInterop.VirtualAudioDeviceProcessLoopback,
                    ref iid,
                    _buffers.PropVariantPointer,
                    _completionHandler,
                    out IActivateAudioInterfaceAsyncOperation operation);
                _operation = operation;
            }
            catch (Exception exception)
            {
                _completionHandler.FailSynchronously(exception);
                _cleanupGate.MarkSynchronousFailure();
                throw;
            }

            if (callResult < 0)
            {
                Exception failure =
                    Marshal.GetExceptionForHR(callResult)
                    ?? new COMException(
                        "BW_COMPUTER_VOICE_AUDIO_ACTIVATION_FAILED",
                        callResult);
                _completionHandler.FailSynchronously(failure);
                _cleanupGate.MarkSynchronousFailure();
                throw failure;
            }

            _cleanupGate.MarkCallReturned();
        }

        internal void Abandon()
        {
            if (Interlocked.Exchange(ref _abandoned, 1) != 0)
            {
                return;
            }

            // No timeout here by design. The native callback owns the
            // activation lifetime. If Windows never calls it, retaining this
            // one bounded activation is safer than freeing memory it may use.
            _ = AbandonedResultFinalizer.ObserveAsync(
                Result,
                ReleaseComObject);
        }

        private async Task FinalizeNativeCompletionAsync()
        {
            try
            {
                _ = await Result.ConfigureAwait(false);
            }
            catch
            {
                // The caller and abandoned-result observer own result errors.
            }

            // Run buffer cleanup from a later asynchronous turn, never inline
            // in TrySetResult. This proves the callback has finished reading
            // the activation parameters. It does not prove that this
            // continuation runs after ActivateCompleted has returned, so the
            // operation RCW is deliberately not force-released here.
            await Task.Yield();
            _cleanupGate.MarkCallbackFinished();
        }

        private void CleanupNativeResources()
        {
            _buffers.Dispose();

            // The callback parameter may share this RCW. A thread-pool
            // continuation can run before ActivateCompleted returns, so only
            // drop our reference and let the callback/native reference plus
            // the CLR own its final COM lifetime.
            _ = Interlocked.Exchange(ref _operation, null);
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

    [ComVisible(true)]
    [ClassInterface(ClassInterfaceType.None)]
    private sealed class ActivationCompletionHandler :
        IActivateAudioInterfaceCompletionHandler,
        IAgileObject
    {
        private readonly TaskCompletionSource<IAudioClient> _completion =
            new(TaskCreationOptions.RunContinuationsAsynchronously);

        internal Task<IAudioClient> Result => _completion.Task;

        internal void FailSynchronously(Exception exception) =>
            _completion.TrySetException(exception);

        public int ActivateCompleted(IActivateAudioInterfaceAsyncOperation operation)
        {
            object? audioClient = null;
            bool transferred = false;
            try
            {
                int operationResult = operation.GetActivateResult(
                    out int activationResult,
                    out audioClient);
                Marshal.ThrowExceptionForHR(operationResult);
                Marshal.ThrowExceptionForHR(activationResult);

                if (audioClient is not IAudioClient typedAudioClient)
                {
                    throw new InvalidOperationException(
                        "BW_COMPUTER_VOICE_AUDIO_INVALID_CLIENT");
                }

                transferred = _completion.TrySetResult(typedAudioClient);
            }
            catch (Exception exception)
            {
                _completion.TrySetException(exception);
            }
            finally
            {
                if (!transferred
                    && OperatingSystem.IsWindows()
                    && audioClient is not null
                    && Marshal.IsComObject(audioClient))
                {
                    _ = Marshal.ReleaseComObject(audioClient);
                }
            }
            // Callback delivery succeeded. Activation failures are carried by
            // Result and must not be hidden by a second callback HRESULT.
            return 0;
        }
    }
}

internal static class AbandonedResultFinalizer
{
    internal static async Task ObserveAsync<T>(
        Task<T> result,
        Action<T> release)
    {
        try
        {
            T unclaimedResult = await result.ConfigureAwait(false);
            release(unclaimedResult);
        }
        catch
        {
            // Result failures are observed here so an abandoned activation
            // cannot produce an unobserved task exception.
        }
    }
}

internal sealed class ActivationCleanupGate
{
    private readonly object _gate = new();
    private readonly Action _cleanup;
    private bool _callReturned;
    private bool _callbackFinished;
    private bool _cleaned;

    internal ActivationCleanupGate(Action cleanup)
    {
        _cleanup = cleanup;
    }

    internal void MarkCallReturned()
    {
        lock (_gate)
        {
            _callReturned = true;
        }

        TryCleanup();
    }

    internal void MarkCallbackFinished()
    {
        lock (_gate)
        {
            _callbackFinished = true;
        }

        TryCleanup();
    }

    internal void MarkSynchronousFailure()
    {
        lock (_gate)
        {
            _callReturned = true;
            _callbackFinished = true;
        }

        TryCleanup();
    }

    private void TryCleanup()
    {
        bool shouldCleanup;
        lock (_gate)
        {
            shouldCleanup = _callReturned && _callbackFinished && !_cleaned;
            if (shouldCleanup)
            {
                _cleaned = true;
            }
        }

        if (shouldCleanup)
        {
            _cleanup();
        }
    }
}

internal sealed class ActivatedProcessAudioClient : INativeAudioClientLease
{
    private IAudioClient? _audioClient;

    internal ActivatedProcessAudioClient(
        uint targetProcessId,
        IAudioClient audioClient)
    {
        TargetProcessId = targetProcessId;
        _audioClient = audioClient;
    }

    internal uint TargetProcessId { get; }

    public IAudioClient AudioClient =>
        _audioClient
        ?? throw new ObjectDisposedException(nameof(ActivatedProcessAudioClient));

    public void Dispose()
    {
        IAudioClient? audioClient = Interlocked.Exchange(
            ref _audioClient,
            null);
        if (OperatingSystem.IsWindows()
            && audioClient is not null
            && Marshal.IsComObject(audioClient))
        {
            Marshal.FinalReleaseComObject(audioClient);
        }
    }
}
