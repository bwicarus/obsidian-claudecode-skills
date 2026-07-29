using System.Runtime.InteropServices;

namespace BwReader.ComputerVoiceAudio.Interop;

// IMMDeviceEnumerator and IMMDevice must retain their native vtable order.
// Default-device lookup and endpoint enumeration are present only as forbidden
// slots: marking them Obsolete(error: true) makes either selection path a
// compile-time error inside this assembly. Production code can only call
// GetDevice with the endpoint ID carried by MicCaptureRequest.
internal static class ExplicitMicrophoneInterop
{
    internal static readonly Guid ClsidMmDeviceEnumerator =
        new("BCDE0395-E52F-467C-8E3D-C4579291692E");

    internal static readonly Guid IidIAudioClient =
        ProcessLoopbackInterop.IidIAudioClient;
}

internal enum AudioDataFlow : int
{
    Render = 0,
    Capture = 1,
    All = 2,
}

internal enum AudioRole : int
{
    Console = 0,
    Multimedia = 1,
    Communications = 2,
}

[Flags]
internal enum DeviceState : uint
{
    Active = 0x0000_0001,
}

[Flags]
internal enum ComClassContext : uint
{
    InProcessServer = 0x0000_0001,
    InProcessHandler = 0x0000_0002,
    LocalServer = 0x0000_0004,
    RemoteServer = 0x0000_0010,
    All = InProcessServer | InProcessHandler | LocalServer | RemoteServer,
}

[ComImport]
[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IMMDeviceEnumerator
{
    [PreserveSig]
    [Obsolete(
        "BW microphone endpoint enumeration is forbidden; use explicit GetDevice.",
        error: true)]
    int ForbiddenEnumAudioEndpoints(
        AudioDataFlow dataFlow,
        DeviceState stateMask,
        [MarshalAs(UnmanagedType.Interface)] out object devices);

    [PreserveSig]
    [Obsolete(
        "BW default microphone selection is forbidden; use explicit GetDevice.",
        error: true)]
    int ForbiddenGetDefaultAudioEndpoint(
        AudioDataFlow dataFlow,
        AudioRole role,
        [MarshalAs(UnmanagedType.Interface)] out IMMDevice endpoint);

    [PreserveSig]
    int GetDevice(
        [MarshalAs(UnmanagedType.LPWStr)] string endpointId,
        [MarshalAs(UnmanagedType.Interface)] out IMMDevice endpoint);

    [PreserveSig]
    int RegisterEndpointNotificationCallback(nint client);

    [PreserveSig]
    int UnregisterEndpointNotificationCallback(nint client);
}

[ComImport]
[Guid("D666063F-1587-4E43-81F1-B948E807363F")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IMMDevice
{
    [PreserveSig]
    int Activate(
        ref Guid interfaceId,
        ComClassContext classContext,
        nint activationParameters,
        [MarshalAs(UnmanagedType.IUnknown)] out object activatedInterface);

    [PreserveSig]
    int OpenPropertyStore(
        uint storageAccessMode,
        [MarshalAs(UnmanagedType.Interface)] out object properties);

    [PreserveSig]
    int GetId(out nint endpointId);

    [PreserveSig]
    int GetState(out DeviceState state);
}

[ComImport]
[Guid("1BE09788-6894-4089-8586-9A2A6C265AC5")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IMMEndpoint
{
    [PreserveSig]
    int GetDataFlow(out AudioDataFlow dataFlow);
}
