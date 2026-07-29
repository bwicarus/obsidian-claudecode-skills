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

    internal static bool NativeCompletionHandlerIsAgileForTest()
    {
        NativeActivationCompletionHandler handler = new(static () => { });
        try
        {
            return handler.SupportsInterfaceForTest(
                    NativeActivationCompletionHandler.IUnknownId)
                && handler.SupportsInterfaceForTest(
                    NativeActivationCompletionHandler.CompletionHandlerId)
                && handler.SupportsInterfaceForTest(
                    NativeActivationCompletionHandler.AgileObjectId)
                && !handler.SupportsInterfaceForTest(Guid.Empty);
        }
        finally
        {
            handler.ReleaseOwnerReference();
        }
    }

    // Production capture and the explicit no-start diagnostic share this
    // activation seam. --describe and --self-test still never activate audio.
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
        try
        {
            pending.Start();
        }
        catch (Exception exception)
        {
            throw AudioCaptureStageException.From(
                "app-output.activate-call",
                exception);
        }

        // A caller timeout/cancellation only stops this wait. PendingActivation
        // remains rooted by the native callback and does not free the
        // PROPVARIANT/BLOB or operation pointer until that callback completes.
        try
        {
            IAudioClient audioClient = await pending.Result
                .WaitAsync(timeout, cancellationToken)
                .ConfigureAwait(false);
            return new ActivatedProcessAudioClient(targetProcessId, audioClient);
        }
        catch (TimeoutException exception)
        {
            pending.Abandon();
            throw AudioCaptureStageException.From(
                "app-output.activate-timeout",
                exception);
        }
        catch (OperationCanceledException)
        {
            pending.Abandon();
            throw;
        }
        catch (Exception exception)
        {
            throw AudioCaptureStageException.From(
                "app-output.activate-result",
                exception);
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
        private readonly NativeActivationCompletionHandler _completionHandler;
        private readonly ActivationCleanupGate _cleanupGate;
        private nint _operationPointer;
        private int _abandoned;

        internal PendingActivation(AudioClientActivationParams activation)
        {
            _buffers = new ActivationBuffers(activation);
            _cleanupGate = new ActivationCleanupGate(CleanupNativeResources);
            _completionHandler = new NativeActivationCompletionHandler(
                _cleanupGate.MarkCallbackFinished);
        }

        internal Task<IAudioClient> Result => _completionHandler.Result;

        internal void Start()
        {
            Guid iid = ProcessLoopbackInterop.IidIAudioClient;
            int callResult;

            try
            {
                callResult = NativeMethods.ActivateAudioInterfaceAsync(
                    ProcessLoopbackInterop.VirtualAudioDeviceProcessLoopback,
                    ref iid,
                    _buffers.PropVariantPointer,
                    _completionHandler.Pointer,
                    out nint operationPointer);
                _operationPointer = operationPointer;
            }
            catch (Exception exception)
            {
                _completionHandler.FailSynchronously(exception);
                _completionHandler.ReleaseOwnerReference();
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
                _completionHandler.ReleaseOwnerReference();
                _cleanupGate.MarkSynchronousFailure();
                throw failure;
            }

            // ActivateAudioInterfaceAsync owns a callback reference after a
            // successful return. Drop the creator reference so the native
            // operation, rather than a managed CCW, controls callback life.
            _completionHandler.ReleaseOwnerReference();
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

        private void CleanupNativeResources()
        {
            _buffers.Dispose();

            nint operationPointer = Interlocked.Exchange(
                ref _operationPointer,
                0);
            if (operationPointer != 0)
            {
                _ = Marshal.Release(operationPointer);
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

    // .NET's classic CCW can expose the IAgileObject IID without aggregating a
    // free-threaded marshaler. ActivateAudioInterfaceAsync rejects that shape
    // with E_ILLEGAL_METHOD_CALL. This tiny native COM object is genuinely
    // apartment-neutral: all mutable state is interlocked or owned by the
    // thread-safe TaskCompletionSource, and QI advertises IAgileObject.
    private sealed class NativeActivationCompletionHandler
    {
        private const int S_OK = 0;
        private const int E_NOINTERFACE = unchecked((int)0x80004002);
        private const int E_FAIL = unchecked((int)0x80004005);
        private const int InstancePointerCount = 2;

        private static readonly Guid IidIUnknown =
            new("00000000-0000-0000-C000-000000000046");
        private static readonly Guid IidCompletionHandler =
            new("41D949AB-9862-444A-80F6-C261334DA5EB");
        private static readonly Guid IidAgileObject =
            new("94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90");

        internal static Guid IUnknownId => IidIUnknown;

        internal static Guid CompletionHandlerId => IidCompletionHandler;

        internal static Guid AgileObjectId => IidAgileObject;

        private static readonly QueryInterfaceDelegate QueryInterfaceEntry =
            QueryInterface;
        private static readonly AddRefDelegate AddRefEntry = AddRef;
        private static readonly ReleaseDelegate ReleaseEntry = Release;
        private static readonly ActivateCompletedDelegate ActivateCompletedEntry =
            ActivateCompleted;
        private static readonly nint Vtable = BuildVtable();

        private readonly TaskCompletionSource<IAudioClient> _completion =
            new(TaskCreationOptions.RunContinuationsAsynchronously);
        private readonly Action _callbackFinished;
        private GCHandle _selfHandle;
        private nint _instancePointer;
        private int _nativeReferenceCount = 1;
        private int _ownerReferenceReleased;

        internal NativeActivationCompletionHandler(Action callbackFinished)
        {
            _callbackFinished = callbackFinished;
            _selfHandle = GCHandle.Alloc(this, GCHandleType.Normal);
            try
            {
                _instancePointer = Marshal.AllocHGlobal(
                    checked(InstancePointerCount * IntPtr.Size));
                Marshal.WriteIntPtr(_instancePointer, 0, Vtable);
                Marshal.WriteIntPtr(
                    _instancePointer,
                    IntPtr.Size,
                    GCHandle.ToIntPtr(_selfHandle));
            }
            catch
            {
                if (_instancePointer != 0)
                {
                    Marshal.FreeHGlobal(_instancePointer);
                    _instancePointer = 0;
                }
                if (_selfHandle.IsAllocated)
                {
                    _selfHandle.Free();
                }
                throw;
            }
        }

        internal Task<IAudioClient> Result => _completion.Task;

        internal nint Pointer =>
            _instancePointer != 0
                ? _instancePointer
                : throw new ObjectDisposedException(
                    nameof(NativeActivationCompletionHandler));

        internal void FailSynchronously(Exception exception) =>
            _completion.TrySetException(exception);

        internal void ReleaseOwnerReference()
        {
            if (Interlocked.Exchange(ref _ownerReferenceReleased, 1) == 0)
            {
                _ = ReleaseReference();
            }
        }

        internal bool SupportsInterfaceForTest(Guid interfaceId)
        {
            int result = QueryInterfaceEntry(
                Pointer,
                ref interfaceId,
                out nint value);
            if (result < 0)
            {
                return false;
            }
            if (value == 0)
            {
                throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_AUDIO_CALLBACK_QI_NULL");
            }
            _ = ReleaseEntry(value);
            return true;
        }

        private int Complete(nint operationPointer)
        {
            object? audioClient = null;
            nint audioClientPointer = 0;
            bool transferred = false;
            try
            {
                int operationResult = InvokeGetActivateResult(
                    operationPointer,
                    out int activationResult,
                    out audioClientPointer);
                Marshal.ThrowExceptionForHR(operationResult);
                Marshal.ThrowExceptionForHR(activationResult);

                if (audioClientPointer == 0)
                {
                    throw new InvalidOperationException(
                        "BW_COMPUTER_VOICE_AUDIO_INVALID_CLIENT");
                }

                if (!OperatingSystem.IsWindows())
                {
                    throw new PlatformNotSupportedException(
                        "BW_COMPUTER_VOICE_AUDIO_WINDOWS_REQUIRED");
                }
                audioClient = Marshal.GetObjectForIUnknown(audioClientPointer);
                _ = Marshal.Release(audioClientPointer);
                audioClientPointer = 0;
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
                if (audioClientPointer != 0)
                {
                    _ = Marshal.Release(audioClientPointer);
                }
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

        private int QueryInterfaceCore(in Guid interfaceId, out nint value)
        {
            if (interfaceId == IidIUnknown
                || interfaceId == IidCompletionHandler
                || interfaceId == IidAgileObject)
            {
                _ = Interlocked.Increment(ref _nativeReferenceCount);
                value = Pointer;
                return S_OK;
            }

            value = 0;
            return E_NOINTERFACE;
        }

        private uint AddReference() =>
            checked((uint)Interlocked.Increment(
                ref _nativeReferenceCount));

        private uint ReleaseReference()
        {
            int count = Interlocked.Decrement(ref _nativeReferenceCount);
            if (count < 0)
            {
                Environment.FailFast(
                    "BW_COMPUTER_VOICE_AUDIO_CALLBACK_REFCOUNT_UNDERFLOW");
            }
            if (count == 0)
            {
                nint instancePointer = Interlocked.Exchange(
                    ref _instancePointer,
                    0);
                if (instancePointer != 0)
                {
                    Marshal.FreeHGlobal(instancePointer);
                }
                if (_selfHandle.IsAllocated)
                {
                    _selfHandle.Free();
                }
            }
            return checked((uint)count);
        }

        private static nint BuildVtable()
        {
            nint vtable = Marshal.AllocHGlobal(
                checked(4 * IntPtr.Size));
            Marshal.WriteIntPtr(
                vtable,
                0 * IntPtr.Size,
                Marshal.GetFunctionPointerForDelegate(QueryInterfaceEntry));
            Marshal.WriteIntPtr(
                vtable,
                1 * IntPtr.Size,
                Marshal.GetFunctionPointerForDelegate(AddRefEntry));
            Marshal.WriteIntPtr(
                vtable,
                2 * IntPtr.Size,
                Marshal.GetFunctionPointerForDelegate(ReleaseEntry));
            Marshal.WriteIntPtr(
                vtable,
                3 * IntPtr.Size,
                Marshal.GetFunctionPointerForDelegate(ActivateCompletedEntry));
            return vtable;
        }

        private static NativeActivationCompletionHandler FromNative(
            nint thisPointer)
        {
            nint handlePointer = Marshal.ReadIntPtr(
                thisPointer,
                IntPtr.Size);
            GCHandle handle = GCHandle.FromIntPtr(handlePointer);
            return handle.Target as NativeActivationCompletionHandler
                ?? throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_AUDIO_CALLBACK_STATE_MISSING");
        }

        private static int QueryInterface(
            nint thisPointer,
            ref Guid interfaceId,
            out nint value)
        {
            value = 0;
            try
            {
                return FromNative(thisPointer).QueryInterfaceCore(
                    interfaceId,
                    out value);
            }
            catch
            {
                return E_FAIL;
            }
        }

        private static uint AddRef(nint thisPointer)
        {
            try
            {
                return FromNative(thisPointer).AddReference();
            }
            catch
            {
                return 0;
            }
        }

        private static uint Release(nint thisPointer)
        {
            try
            {
                return FromNative(thisPointer).ReleaseReference();
            }
            catch
            {
                return 0;
            }
        }

        private static int ActivateCompleted(
            nint thisPointer,
            nint operationPointer)
        {
            NativeActivationCompletionHandler? handler = null;
            int callbackResult;
            try
            {
                handler = FromNative(thisPointer);
                callbackResult = handler.Complete(operationPointer);
            }
            catch (Exception exception)
            {
                handler?._completion.TrySetException(exception);
                callbackResult = E_FAIL;
            }
            try
            {
                handler?._callbackFinished();
            }
            catch (Exception exception)
            {
                handler?._completion.TrySetException(exception);
                callbackResult = E_FAIL;
            }
            return callbackResult;
        }

        private static int InvokeGetActivateResult(
            nint operationPointer,
            out int activationResult,
            out nint activatedInterface)
        {
            nint operationVtable = Marshal.ReadIntPtr(operationPointer);
            nint getActivateResultPointer = Marshal.ReadIntPtr(
                operationVtable,
                3 * IntPtr.Size);
            GetActivateResultDelegate getActivateResult =
                Marshal.GetDelegateForFunctionPointer<GetActivateResultDelegate>(
                    getActivateResultPointer);
            return getActivateResult(
                operationPointer,
                out activationResult,
                out activatedInterface);
        }

        [UnmanagedFunctionPointer(CallingConvention.StdCall)]
        private delegate int QueryInterfaceDelegate(
            nint thisPointer,
            ref Guid interfaceId,
            out nint value);

        [UnmanagedFunctionPointer(CallingConvention.StdCall)]
        private delegate uint AddRefDelegate(nint thisPointer);

        [UnmanagedFunctionPointer(CallingConvention.StdCall)]
        private delegate uint ReleaseDelegate(nint thisPointer);

        [UnmanagedFunctionPointer(CallingConvention.StdCall)]
        private delegate int ActivateCompletedDelegate(
            nint thisPointer,
            nint operationPointer);

        [UnmanagedFunctionPointer(CallingConvention.StdCall)]
        private delegate int GetActivateResultDelegate(
            nint thisPointer,
            out int activationResult,
            out nint activatedInterface);
    }

    private static class NativeMethods
    {
        [DllImport(
            "Mmdevapi.dll",
            EntryPoint = "ActivateAudioInterfaceAsync",
            ExactSpelling = true,
            CharSet = CharSet.Unicode)]
        internal static extern int ActivateAudioInterfaceAsync(
            [MarshalAs(UnmanagedType.LPWStr)] string deviceInterfacePath,
            ref Guid riid,
            nint activationParams,
            nint completionHandler,
            out nint activationOperation);
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
