using System.Runtime.InteropServices;
using BwReader.ComputerVoiceAudio.Interop;

namespace BwReader.ComputerVoiceAudio;

internal sealed record DirectMicrophoneEndpoint(
    string EndpointId,
    string FriendlyName);

internal static class DirectMicrophoneDiscovery
{
    private const uint StorageRead = 0;
    private const ushort VariantTypeWideString = 31;
    private static readonly PropertyKey DeviceFriendlyName = new(
        new Guid("A45C254E-DF1C-4EFD-8020-67D146A850E0"),
        14);
    private static readonly PropertyKey DeviceDescription = new(
        new Guid("A45C254E-DF1C-4EFD-8020-67D146A850E0"),
        2);

    internal static IReadOnlyList<DirectMicrophoneEndpoint>
        EnumerateActive()
    {
        if (!OperatingSystem.IsWindows())
        {
            throw new PlatformNotSupportedException(
                "BW_COMPUTER_VOICE_AUDIO_WINDOWS_REQUIRED");
        }

        object? enumeratorObject = null;
        IMMDeviceCollectionForSelection? collection = null;
        List<DirectMicrophoneEndpoint> endpoints = [];
        using ComMtaLease apartment = ComMtaLease.Enter();
        try
        {
            Type enumeratorType = Type.GetTypeFromCLSID(
                ExplicitMicrophoneInterop.ClsidMmDeviceEnumerator,
                throwOnError: true)
                ?? throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_AUDIO_MMDEVICE_TYPE_MISSING");
            enumeratorObject = Activator.CreateInstance(enumeratorType);
            if (enumeratorObject
                is not IMMDeviceEnumeratorForSelection enumerator)
            {
                throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_AUDIO_MMDEVICE_ENUMERATOR_INVALID");
            }

            RequireSucceeded(enumerator.EnumAudioEndpoints(
                AudioDataFlow.Capture,
                DeviceState.Active,
                out collection));
            RequireSucceeded(collection.GetCount(out uint count));
            if (count > 64)
            {
                throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_AUDIO_MIC_DEVICE_COUNT_INVALID");
            }

            HashSet<string> seen = new(StringComparer.Ordinal);
            for (uint index = 0; index < count; index += 1)
            {
                IMMDevice? endpoint = null;
                nint endpointIdPointer = 0;
                try
                {
                    RequireSucceeded(collection.Item(index, out endpoint));
                    RequireSucceeded(endpoint.GetId(out endpointIdPointer));
                    string endpointId =
                        Marshal.PtrToStringUni(endpointIdPointer)
                        ?? throw new InvalidOperationException(
                            "BW_COMPUTER_VOICE_AUDIO_MIC_ID_MISSING");
                    _ = MicCaptureRequest.Create(endpointId);
                    if (!seen.Add(endpointId))
                    {
                        throw new InvalidOperationException(
                            "BW_COMPUTER_VOICE_AUDIO_MIC_ID_DUPLICATE");
                    }
                    string friendlyName =
                        ReadStringProperty(endpoint, DeviceFriendlyName)
                        ?? ReadStringProperty(endpoint, DeviceDescription)
                        ?? endpointId;
                    endpoints.Add(new DirectMicrophoneEndpoint(
                        endpointId,
                        friendlyName));
                }
                finally
                {
                    if (endpointIdPointer != 0)
                    {
                        Marshal.FreeCoTaskMem(endpointIdPointer);
                    }
                    ReleaseComObject(endpoint);
                }
            }
        }
        finally
        {
            ReleaseComObject(collection);
            ReleaseComObject(enumeratorObject);
        }

        endpoints.Sort(static (left, right) =>
            StringComparer.CurrentCultureIgnoreCase.Compare(
                left.FriendlyName,
                right.FriendlyName));
        return endpoints;
    }

    private static string? ReadStringProperty(
        IMMDevice endpoint,
        PropertyKey key)
    {
        object? propertyStoreObject = null;
        DiscoveryPropVariant value = default;
        try
        {
            RequireSucceeded(endpoint.OpenPropertyStore(
                StorageRead,
                out propertyStoreObject));
            if (propertyStoreObject is not IPropertyStoreForSelection store)
            {
                return null;
            }
            RequireSucceeded(store.GetValue(ref key, out value));
            if (
                value.VariantType != VariantTypeWideString
                || value.PointerValue == 0
            )
            {
                return null;
            }
            string? text = Marshal.PtrToStringUni(value.PointerValue);
            return string.IsNullOrWhiteSpace(text) ? null : text;
        }
        catch (COMException)
        {
            return null;
        }
        finally
        {
            _ = PropVariantClear(ref value);
            ReleaseComObject(propertyStoreObject);
        }
    }

    private static void RequireSucceeded(int result)
    {
        if (result < 0)
        {
            Marshal.ThrowExceptionForHR(result);
        }
    }

    private static void ReleaseComObject(object? value)
    {
        if (
            OperatingSystem.IsWindows()
            && value is not null
            && Marshal.IsComObject(value)
        )
        {
            Marshal.FinalReleaseComObject(value);
        }
    }

    [DllImport("ole32.dll", ExactSpelling = true)]
    private static extern int PropVariantClear(
        ref DiscoveryPropVariant value);
}

[ComImport]
[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IMMDeviceEnumeratorForSelection
{
    [PreserveSig]
    int EnumAudioEndpoints(
        AudioDataFlow dataFlow,
        DeviceState stateMask,
        out IMMDeviceCollectionForSelection devices);

    [PreserveSig]
    [Obsolete(
        "Microphone discovery must enumerate active endpoints; default-device selection is forbidden.",
        error: true)]
    int GetDefaultAudioEndpoint(
        AudioDataFlow dataFlow,
        AudioRole role,
        out IMMDevice endpoint);

    [PreserveSig]
    int GetDevice(
        [MarshalAs(UnmanagedType.LPWStr)] string endpointId,
        out IMMDevice endpoint);

    [PreserveSig]
    int RegisterEndpointNotificationCallback(nint client);

    [PreserveSig]
    int UnregisterEndpointNotificationCallback(nint client);
}

[ComImport]
[Guid("0BD7A1BE-7A1A-44DB-8397-CC5392387B5E")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IMMDeviceCollectionForSelection
{
    [PreserveSig]
    int GetCount(out uint count);

    [PreserveSig]
    int Item(uint index, out IMMDevice endpoint);
}

[ComImport]
[Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IPropertyStoreForSelection
{
    [PreserveSig]
    int GetCount(out uint propertyCount);

    [PreserveSig]
    int GetAt(uint propertyIndex, out PropertyKey key);

    [PreserveSig]
    int GetValue(
        ref PropertyKey key,
        out DiscoveryPropVariant value);

    [PreserveSig]
    int SetValue(
        ref PropertyKey key,
        ref DiscoveryPropVariant value);

    [PreserveSig]
    int Commit();
}

[StructLayout(LayoutKind.Sequential)]
internal struct PropertyKey
{
    internal Guid FormatId;
    internal uint PropertyId;

    internal PropertyKey(Guid formatId, uint propertyId)
    {
        FormatId = formatId;
        PropertyId = propertyId;
    }
}

[StructLayout(LayoutKind.Explicit, Size = 24)]
internal struct DiscoveryPropVariant
{
    [FieldOffset(0)]
    internal ushort VariantType;

    [FieldOffset(8)]
    internal nint PointerValue;
}
