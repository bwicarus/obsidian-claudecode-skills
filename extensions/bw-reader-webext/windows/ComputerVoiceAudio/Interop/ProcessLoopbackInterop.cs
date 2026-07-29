using System.Runtime.InteropServices;

namespace BwReader.ComputerVoiceAudio.Interop;

// Definitions mirror audioclientactivationparams.h and mmdeviceapi.h. They are
// intentionally internal so callers cannot select default/system loopback.
internal static class ProcessLoopbackInterop
{
    internal const string VirtualAudioDeviceProcessLoopback =
        "VAD\\Process_Loopback";

    internal static readonly Guid IidIAudioClient =
        new("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2");

    internal static readonly Guid IidIAudioCaptureClient =
        new("C8ADBD64-E71E-48A0-A4DE-185C395CD317");

    internal static readonly Guid IidIAudioRenderClient =
        new("F294ACFC-3146-4483-A7BF-ADDCA7C260E2");

    internal const ushort VariantTypeBlob = 65; // VT_BLOB

    internal const int Succeeded = 0;

    // _AUDCLNT_SUCCESS(1). GetBuffer did not lease a packet in this case, so
    // ReleaseBuffer must not be called.
    internal const int AudioClientBufferEmpty = 0x0889_0001;

    [DllImport(
        "Mmdevapi.dll",
        EntryPoint = "ActivateAudioInterfaceAsync",
        ExactSpelling = true,
        CharSet = CharSet.Unicode)]
    internal static extern int ActivateAudioInterfaceAsync(
        [MarshalAs(UnmanagedType.LPWStr)] string deviceInterfacePath,
        ref Guid riid,
        nint activationParams,
        [MarshalAs(UnmanagedType.Interface)]
        IActivateAudioInterfaceCompletionHandler completionHandler,
        [MarshalAs(UnmanagedType.Interface)]
        out IActivateAudioInterfaceAsyncOperation activationOperation);
}

internal enum AudioClientActivationType : int
{
    Default = 0,
    ProcessLoopback = 1,
}

internal enum ProcessLoopbackMode : int
{
    IncludeTargetProcessTree = 0,
    ExcludeTargetProcessTree = 1,
}

[StructLayout(LayoutKind.Sequential)]
internal struct AudioClientProcessLoopbackParams
{
    internal uint TargetProcessId;
    internal ProcessLoopbackMode ProcessLoopbackMode;
}

[StructLayout(LayoutKind.Sequential)]
internal struct AudioClientActivationParams
{
    internal AudioClientActivationType ActivationType;

    // The native field is a union. Its only currently defined member is
    // AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS, so sequential layout is identical.
    internal AudioClientProcessLoopbackParams ProcessLoopbackParams;
}

[StructLayout(LayoutKind.Sequential)]
internal struct Blob
{
    internal uint Size;
    internal nint Data;
}

[StructLayout(LayoutKind.Explicit)]
internal struct PropVariant
{
    [FieldOffset(0)]
    internal ushort VariantType;

    [FieldOffset(2)]
    internal ushort Reserved1;

    [FieldOffset(4)]
    internal ushort Reserved2;

    [FieldOffset(6)]
    internal ushort Reserved3;

    [FieldOffset(8)]
    internal Blob Blob;
}

[ComImport]
[Guid("72A22D78-CDE4-431D-B8CC-843A71199B6D")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IActivateAudioInterfaceAsyncOperation
{
    [PreserveSig]
    int GetActivateResult(
        out int activateResult,
        [MarshalAs(UnmanagedType.IUnknown)] out object activatedInterface);
}

[ComVisible(true)]
[Guid("41D949AB-9862-444A-80F6-C261334DA5EB")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IActivateAudioInterfaceCompletionHandler
{
    [PreserveSig]
    int ActivateCompleted(IActivateAudioInterfaceAsyncOperation operation);
}

// Exposing IAgileObject on the managed completion handler allows Windows to
// invoke it from the MTA worker used by ActivateAudioInterfaceAsync.
[ComVisible(true)]
[Guid("94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IAgileObject
{
}

internal enum AudioClientShareMode : int
{
    Shared = 0,
    Exclusive = 1,
}

[Flags]
internal enum AudioClientStreamFlags : uint
{
    Loopback = 0x0002_0000,
    EventCallback = 0x0004_0000,
    SrcDefaultQuality = 0x0800_0000,
    AutoConvertPcm = 0x8000_0000,
}

[Flags]
internal enum AudioClientBufferFlags : uint
{
    None = 0,
    DataDiscontinuity = 0x1,
    Silent = 0x2,
    TimestampError = 0x4,
}

[StructLayout(LayoutKind.Sequential, Pack = 1)]
internal struct WaveFormatEx
{
    internal ushort FormatTag;
    internal ushort Channels;
    internal uint SamplesPerSecond;
    internal uint AverageBytesPerSecond;
    internal ushort BlockAlign;
    internal ushort BitsPerSample;
    internal ushort ExtraSize;
}

[StructLayout(LayoutKind.Sequential, Pack = 1)]
internal struct WaveFormatExtensible
{
    internal WaveFormatEx Format;
    internal ushort ValidBitsPerSample;
    internal uint ChannelMask;
    internal Guid SubFormat;
}

// Keep all 12 IAudioClient methods in native vtable order. The process-loopback
// activation result is IAudioClient (not IAudioClient2/3).
[ComImport]
[Guid("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IAudioClient
{
    [PreserveSig]
    int Initialize(
        AudioClientShareMode shareMode,
        uint streamFlags,
        long bufferDuration,
        long periodicity,
        nint format,
        nint audioSessionGuid);

    [PreserveSig]
    int GetBufferSize(out uint bufferFrameCount);

    [PreserveSig]
    int GetStreamLatency(out long latency);

    [PreserveSig]
    int GetCurrentPadding(out uint currentPadding);

    [PreserveSig]
    int IsFormatSupported(
        AudioClientShareMode shareMode,
        nint format,
        out nint closestMatch);

    [PreserveSig]
    int GetMixFormat(out nint deviceFormat);

    [PreserveSig]
    int GetDevicePeriod(out long defaultPeriod, out long minimumPeriod);

    [PreserveSig]
    int Start();

    [PreserveSig]
    int Stop();

    [PreserveSig]
    int Reset();

    [PreserveSig]
    int SetEventHandle(nint eventHandle);

    [PreserveSig]
    int GetService(ref Guid interfaceId, out nint service);
}

// Future capture seam. GetBuffer and ReleaseBuffer must be paired on the same
// dedicated capture thread; ProcessLoopbackCaptureSession owns that guard.
[ComImport]
[Guid("C8ADBD64-E71E-48A0-A4DE-185C395CD317")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IAudioCaptureClient
{
    [PreserveSig]
    int GetBuffer(
        out nint data,
        out uint framesToRead,
        out uint flags,
        out ulong devicePosition,
        out ulong qpcPosition);

    [PreserveSig]
    int ReleaseBuffer(uint framesRead);

    [PreserveSig]
    int GetNextPacketSize(out uint nextPacketSize);
}

// GetBuffer and ReleaseBuffer are paired on the dedicated virtual-microphone
// render thread.  Keeping this vtable here beside IAudioClient avoids a NuGet
// audio wrapper and makes the exact endpoint/thread ownership auditable.
[ComImport]
[Guid("F294ACFC-3146-4483-A7BF-ADDCA7C260E2")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IAudioRenderClient
{
    [PreserveSig]
    int GetBuffer(uint requestedFrames, out nint data);

    [PreserveSig]
    int ReleaseBuffer(uint writtenFrames, uint flags);
}
