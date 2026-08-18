// 独立探针：枚举 WASAPI 音频会话，看 Codex 的语音能不能被稳定观测到。
//
// 为什么要先做这个：语音链路重做的整个方案都压在一句假设上 ——
// "Codex 的进程在音频端点上会有一个 Active 的会话，开关语音时它会翻转"。
// 现有的确认信号（注册表麦克风台账）已经被实测证明是引用计数的、分不清通话与探测、
// 还会留上次运行的残值。换一个同样没验过的信号毫无意义，所以先量一遍。
//
// 只读：不发按键、不改路由、不碰任何状态。
using System.Diagnostics;
using System.Runtime.InteropServices;

internal static class Program
{
    private static int Main(string[] args)
    {
        if (args.Length > 0 && args[0] == "--timing")
        {
            return Timing.Run(
                args.Length > 1 && int.TryParse(args[1], out int t) ? t : 180);
        }
        if (args.Length > 0 && args[0] == "--watch")
        {
            return SignalDiff.Watch(
                args.Length > 1 && int.TryParse(args[1], out int w) ? w : 120);
        }
        if (args.Length > 0 && args[0] == "--diff")
        {
            return SignalDiff.Run(press: args.Length < 2 || args[1] != "nopress");
        }
        if (args.Length > 0 && args[0] == "--hook")
        {
            return HookProbe.Run();
        }
        int seconds = args.Length > 0 && int.TryParse(args[0], out int s) ? s : 20;
        string? filter = args.Length > 1 ? args[1] : null;

        Console.OutputEncoding = System.Text.Encoding.UTF8;
        Console.WriteLine($"观察 {seconds} 秒；过滤进程名包含: {filter ?? "(全部)"}");

        var previous = new Dictionary<string, string>();
        var seen = new HashSet<string>();
        DateTime deadline = DateTime.UtcNow.AddSeconds(seconds);
        bool first = true;

        while (DateTime.UtcNow < deadline)
        {
            var now = Snapshot(filter);
            foreach (var (key, value) in now)
            {
                if (!previous.TryGetValue(key, out string? old))
                {
                    Console.WriteLine($"[{DateTime.Now:HH:mm:ss.fff}] + {key} => {value}");
                    seen.Add(key);
                }
                else if (old != value)
                {
                    Console.WriteLine($"[{DateTime.Now:HH:mm:ss.fff}] ~ {key} : {old} -> {value}");
                }
            }
            foreach (var key in previous.Keys.Where(k => !now.ContainsKey(k)).ToList())
            {
                Console.WriteLine($"[{DateTime.Now:HH:mm:ss.fff}] - {key} (会话消失)");
            }
            previous = now;
            if (first)
            {
                first = false;
                Console.WriteLine("--- 以上为初始快照；以下只打印变化 ---");
            }
            Thread.Sleep(400);
        }

        Console.WriteLine($"--- 结束。共见到 {seen.Count} 个会话键 ---");
        return 0;
    }

    private static Dictionary<string, string> Snapshot(string? filter)
    {
        var result = new Dictionary<string, string>();
        var enumeratorType = Type.GetTypeFromCLSID(new Guid("BCDE0395-E52F-467C-8E3D-C4579291692E"))!;
        var deviceEnumerator = (IMMDeviceEnumerator)Activator.CreateInstance(enumeratorType)!;

        foreach (EDataFlow flow in new[] { EDataFlow.eCapture, EDataFlow.eRender })
        {
            if (deviceEnumerator.EnumAudioEndpoints(flow, DeviceStateActive, out IMMDeviceCollection collection) != 0)
            {
                continue;
            }
            collection.GetCount(out int count);
            for (int i = 0; i < count; i++)
            {
                if (collection.Item(i, out IMMDevice device) != 0)
                {
                    continue;
                }
                device.GetId(out string deviceId);
                string deviceName = FriendlyName(device) ?? deviceId;
                Guid managerIid = typeof(IAudioSessionManager2).GUID;
                if (device.Activate(ref managerIid, ClsCtxAll, IntPtr.Zero, out object managerObject) != 0)
                {
                    continue;
                }
                var manager = (IAudioSessionManager2)managerObject;
                if (manager.GetSessionEnumerator(out IAudioSessionEnumerator sessions) != 0)
                {
                    continue;
                }
                sessions.GetCount(out int sessionCount);
                for (int j = 0; j < sessionCount; j++)
                {
                    if (sessions.GetSession(j, out IAudioSessionControl control) != 0)
                    {
                        continue;
                    }
                    var control2 = (IAudioSessionControl2)control;
                    control2.GetProcessId(out uint pid);
                    control2.GetState(out int state);
                    string process = ProcessName(pid);
                    if (filter is not null &&
                        process.IndexOf(filter, StringComparison.OrdinalIgnoreCase) < 0)
                    {
                        continue;
                    }
                    string key = $"{(flow == EDataFlow.eCapture ? "采集" : "渲染")} | {deviceName} | pid {pid} {process}";
                    result[key] = state switch
                    {
                        0 => "Inactive",
                        1 => "Active",
                        2 => "Expired",
                        _ => $"?{state}",
                    };
                }
            }
        }
        return result;
    }

    private static string ProcessName(uint pid)
    {
        if (pid == 0)
        {
            return "(系统混音)";
        }
        try
        {
            return Process.GetProcessById((int)pid).ProcessName;
        }
        catch
        {
            return "(已退出)";
        }
    }

    private static string? FriendlyName(IMMDevice device)
    {
        try
        {
            if (device.OpenPropertyStore(StgmRead, out IPropertyStore store) != 0)
            {
                return null;
            }
            var key = new PropertyKey
            {
                fmtid = new Guid("A45C254E-DF1C-4EFD-8020-67D146A850E0"),
                pid = 14,
            };
            if (store.GetValue(ref key, out PropVariant value) != 0)
            {
                return null;
            }
            return value.vt == 31 ? Marshal.PtrToStringUni(value.pointerValue) : null;
        }
        catch
        {
            return null;
        }
    }

    private const int DeviceStateActive = 0x00000001;
    private const int ClsCtxAll = 23;
    private const int StgmRead = 0;

    private enum EDataFlow
    {
        eRender = 0,
        eCapture = 1,
        eAll = 2,
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct PropertyKey
    {
        public Guid fmtid;
        public int pid;
    }

    [StructLayout(LayoutKind.Explicit)]
    private struct PropVariant
    {
        [FieldOffset(0)] public short vt;
        [FieldOffset(8)] public IntPtr pointerValue;
    }

    [ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IMMDeviceEnumerator
    {
        [PreserveSig] int EnumAudioEndpoints(EDataFlow dataFlow, int stateMask, out IMMDeviceCollection devices);
        [PreserveSig] int GetDefaultAudioEndpoint(EDataFlow dataFlow, int role, out IMMDevice device);
        [PreserveSig] int GetDevice(string id, out IMMDevice device);
        [PreserveSig] int RegisterEndpointNotificationCallback(IntPtr client);
        [PreserveSig] int UnregisterEndpointNotificationCallback(IntPtr client);
    }

    [ComImport, Guid("0BD7A1BE-7A1A-44DB-8397-CC5392387B5E"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IMMDeviceCollection
    {
        [PreserveSig] int GetCount(out int count);
        [PreserveSig] int Item(int index, out IMMDevice device);
    }

    [ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IMMDevice
    {
        [PreserveSig] int Activate(ref Guid iid, int clsCtx, IntPtr activationParams,
            [MarshalAs(UnmanagedType.IUnknown)] out object instance);
        [PreserveSig] int OpenPropertyStore(int access, out IPropertyStore properties);
        [PreserveSig] int GetId([MarshalAs(UnmanagedType.LPWStr)] out string id);
        [PreserveSig] int GetState(out int state);
    }

    [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IPropertyStore
    {
        [PreserveSig] int GetCount(out int count);
        [PreserveSig] int GetAt(int index, out PropertyKey key);
        [PreserveSig] int GetValue(ref PropertyKey key, out PropVariant value);
        [PreserveSig] int SetValue(ref PropertyKey key, ref PropVariant value);
        [PreserveSig] int Commit();
    }

    [ComImport, Guid("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IAudioSessionManager2
    {
        [PreserveSig] int GetAudioSessionControl(IntPtr sessionGuid, int streamFlags, out IAudioSessionControl control);
        [PreserveSig] int GetSimpleAudioVolume(IntPtr sessionGuid, int streamFlags, out IntPtr volume);
        [PreserveSig] int GetSessionEnumerator(out IAudioSessionEnumerator sessions);
        [PreserveSig] int RegisterSessionNotification(IntPtr notification);
        [PreserveSig] int UnregisterSessionNotification(IntPtr notification);
        [PreserveSig] int RegisterDuckNotification(string sessionId, IntPtr duckNotification);
        [PreserveSig] int UnregisterDuckNotification(IntPtr duckNotification);
    }

    [ComImport, Guid("E2F5BB11-0570-40CA-ACDD-3AA01277DEE8"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IAudioSessionEnumerator
    {
        [PreserveSig] int GetCount(out int count);
        [PreserveSig] int GetSession(int index, out IAudioSessionControl session);
    }

    [ComImport, Guid("F4B1A599-7266-4319-A8CA-E70ACB11E8CD"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IAudioSessionControl
    {
        [PreserveSig] int GetState(out int state);
        [PreserveSig] int GetDisplayName([MarshalAs(UnmanagedType.LPWStr)] out string name);
        [PreserveSig] int SetDisplayName(string value, ref Guid eventContext);
        [PreserveSig] int GetIconPath([MarshalAs(UnmanagedType.LPWStr)] out string path);
        [PreserveSig] int SetIconPath(string value, ref Guid eventContext);
        [PreserveSig] int GetGroupingParam(out Guid groupingParam);
        [PreserveSig] int SetGroupingParam(ref Guid grouping, ref Guid eventContext);
        [PreserveSig] int RegisterAudioSessionNotification(IntPtr newNotifications);
        [PreserveSig] int UnregisterAudioSessionNotification(IntPtr newNotifications);
    }

    [ComImport, Guid("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IAudioSessionControl2
    {
        [PreserveSig] int GetState(out int state);
        [PreserveSig] int GetDisplayName([MarshalAs(UnmanagedType.LPWStr)] out string name);
        [PreserveSig] int SetDisplayName(string value, ref Guid eventContext);
        [PreserveSig] int GetIconPath([MarshalAs(UnmanagedType.LPWStr)] out string path);
        [PreserveSig] int SetIconPath(string value, ref Guid eventContext);
        [PreserveSig] int GetGroupingParam(out Guid groupingParam);
        [PreserveSig] int SetGroupingParam(ref Guid grouping, ref Guid eventContext);
        [PreserveSig] int RegisterAudioSessionNotification(IntPtr newNotifications);
        [PreserveSig] int UnregisterAudioSessionNotification(IntPtr newNotifications);
        [PreserveSig] int GetSessionIdentifier([MarshalAs(UnmanagedType.LPWStr)] out string id);
        [PreserveSig] int GetSessionInstanceIdentifier([MarshalAs(UnmanagedType.LPWStr)] out string id);
        [PreserveSig] int GetProcessId(out uint pid);
        [PreserveSig] int IsSystemSoundsSession();
        [PreserveSig] int SetDuckingPreference(bool optOut);
    }
}
