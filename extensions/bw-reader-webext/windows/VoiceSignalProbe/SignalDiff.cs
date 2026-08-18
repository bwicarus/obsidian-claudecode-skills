// 一次把所有候选信号记下来 → 按一次 F24 → 再记一次 → 看**谁动了**。
//
// 为什么要这么做：我先后拿"麦克风台账"和"WASAPI 会话状态"当过"语音开没开"的
// 判据，两个都被实测打脸（而且第二次是在我已经写完报告之后才被用户当场纠正）。
// 一个一个试、试一个错一个，是因为每次只看一个信号 —— 那就没法知道"是这个信号
// 不行"还是"根本没发生变化"。把候选信号并排记下来再触发，才分得清。
//
// 候选：
//   1. WASAPI 会话状态（已知弱，留作对照）
//   2. 会话**峰值电平**（区分"流开着"和"真有声音"）—— 之前没试过
//   3. Codex 进程的 TCP 连接（实时语音必然有长连接，平时没有）
//   4. 麦克风台账（注册表两个时间戳）
//   5. 进程线程数（弱信号，但免费）
using System.Diagnostics;
using System.Runtime.InteropServices;
using Microsoft.Win32;

internal static class SignalDiff
{
    internal static int Run(bool press)
    {
        Console.OutputEncoding = System.Text.Encoding.UTF8;
        Console.WriteLine("=== 触发前 ===");
        var before = Capture();
        Print(before);

        if (press)
        {
            Console.WriteLine("\n--- 按 F24 ---");
            keybd_event(0x87, 0, 0, UIntPtr.Zero);
            Thread.Sleep(60);
            keybd_event(0x87, 0, 2, UIntPtr.Zero);
            Console.WriteLine("等 12 秒让它初始化…");
            Thread.Sleep(12000);
        }
        else
        {
            Console.WriteLine("\n--- 不按键，等 12 秒（对照） ---");
            Thread.Sleep(12000);
        }

        Console.WriteLine("\n=== 触发后 ===");
        var after = Capture();
        Print(after);

        Console.WriteLine("\n=== 变化的信号 ===");
        bool any = false;
        foreach (var key in before.Keys.Union(after.Keys).OrderBy(k => k))
        {
            before.TryGetValue(key, out string? b);
            after.TryGetValue(key, out string? a);
            if (b != a)
            {
                any = true;
                Console.WriteLine($"  ★ {key}: {b ?? "(无)"} -> {a ?? "(无)"}");
            }
        }
        if (!any)
        {
            Console.WriteLine("  （没有任何候选信号发生变化）");
        }
        return 0;
    }

    /// <summary>
    /// 连续采样所有候选信号，只在**变化**时打印。
    /// </summary>
    /// <remarks>
    /// 单靠我自己按键验证不了任何信号：按下去要是什么都没动，我分不清是
    /// "信号瞎了"还是"这次根本没打开"。要判定一个探测器好不好用，必须先有一个
    /// **已知为真**的状态变化 —— 那只能由用户手动开关语音来提供。
    /// 这个模式就是为了配合那次已知变化：他动手，我记录，然后看谁跟着动了。
    /// </remarks>
    internal static int Watch(int seconds)
    {
        Console.OutputEncoding = System.Text.Encoding.UTF8;
        Console.WriteLine($"记录 {seconds} 秒；只打印变化。请在这期间手动开/关一次 Codex 语音。");
        var previous = Capture();
        foreach (var (key, value) in previous.OrderBy(x => x.Key, StringComparer.Ordinal))
        {
            Console.WriteLine($"  基线 {key} = {value}");
        }
        Console.WriteLine("--- 以下只打印变化 ---");
        DateTime deadline = DateTime.UtcNow.AddSeconds(seconds);
        while (DateTime.UtcNow < deadline)
        {
            var current = Capture();
            foreach (var key in previous.Keys.Union(current.Keys).OrderBy(k => k))
            {
                previous.TryGetValue(key, out string? before);
                current.TryGetValue(key, out string? after);
                if (before != after)
                {
                    Console.WriteLine(
                        $"[{DateTime.Now:HH:mm:ss}] ★ {key}: {before ?? "(无)"} -> {after ?? "(无)"}");
                }
            }
            previous = current;
        }
        Console.WriteLine("--- 记录结束 ---");
        return 0;
    }

    private static Dictionary<string, string> Capture()
    {
        var result = new Dictionary<string, string>(StringComparer.Ordinal);
        var pids = new HashSet<uint>();
        int threads = 0;
        foreach (Process process in Process.GetProcesses())
        {
            try
            {
                if (!process.ProcessName.StartsWith("ChatGPT", StringComparison.OrdinalIgnoreCase))
                {
                    continue;
                }
                pids.Add((uint)process.Id);
                threads += process.Threads.Count;
            }
            catch
            {
            }
            finally
            {
                process.Dispose();
            }
        }
        result["进程数"] = pids.Count.ToString();
        result["线程总数"] = threads.ToString();

        // 麦克风台账
        try
        {
            using RegistryKey? key = Registry.CurrentUser.OpenSubKey(
                @"SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone\OpenAI.CodexBeta_2p2nqsd0c76g0");
            long start = Convert.ToInt64(key?.GetValue("LastUsedTimeStart") ?? 0L);
            long stop = Convert.ToInt64(key?.GetValue("LastUsedTimeStop") ?? 0L);
            result["台账"] = $"start={start} stop={stop} active={(start > 0 && (stop == 0 || start > stop))}";
        }
        catch (Exception exception)
        {
            result["台账"] = "读失败 " + exception.GetType().Name;
        }

        // TCP 连接（远端去重后的数量与端口分布）
        try
        {
            var remotes = TcpRemotesFor(pids);
            result["TCP 连接数"] = remotes.Count.ToString();
            result["TCP 远端"] = string.Join(",", remotes.OrderBy(x => x).Take(12));
        }
        catch (Exception exception)
        {
            result["TCP"] = "读失败 " + exception.GetType().Name;
        }

        // WASAPI：会话状态 + 峰值电平（峰值取 1.5 秒内最大，避免采到静音瞬间）
        try
        {
            foreach (var (name, state, peak) in AudioSessions(pids))
            {
                result[$"会话 {name} 状态"] = state;
                result[$"会话 {name} 峰值"] = peak.ToString("F4");
            }
        }
        catch (Exception exception)
        {
            result["WASAPI"] = "读失败 " + exception.GetType().Name;
        }
        return result;
    }

    private static void Print(Dictionary<string, string> snapshot)
    {
        foreach (var (key, value) in snapshot.OrderBy(x => x.Key, StringComparer.Ordinal))
        {
            Console.WriteLine($"  {key} = {value}");
        }
    }

    private static List<string> TcpRemotesFor(HashSet<uint> pids)
    {
        var remotes = new List<string>();
        int size = 0;
        _ = GetExtendedTcpTable(IntPtr.Zero, ref size, false, 2, 5, 0);
        IntPtr buffer = Marshal.AllocHGlobal(size);
        try
        {
            if (GetExtendedTcpTable(buffer, ref size, false, 2, 5, 0) != 0)
            {
                return remotes;
            }
            int count = Marshal.ReadInt32(buffer);
            IntPtr row = buffer + 4;
            int rowSize = Marshal.SizeOf<TcpRowOwnerPid>();
            for (int i = 0; i < count; i++)
            {
                var entry = Marshal.PtrToStructure<TcpRowOwnerPid>(row);
                if (pids.Contains(entry.owningPid) && entry.state == 5)
                {
                    uint address = entry.remoteAddr;
                    int port = (int)(((entry.remotePort & 0xFF) << 8) | ((entry.remotePort >> 8) & 0xFF));
                    remotes.Add(
                        $"{address & 0xFF}.{(address >> 8) & 0xFF}.{(address >> 16) & 0xFF}.{(address >> 24) & 0xFF}:{port}");
                }
                row += rowSize;
            }
        }
        finally
        {
            Marshal.FreeHGlobal(buffer);
        }
        return remotes.Distinct().ToList();
    }

    private static List<(string Name, string State, float Peak)> AudioSessions(
        HashSet<uint> pids)
    {
        var found = new List<(string, string, float)>();
        var enumeratorType = Type.GetTypeFromCLSID(
            new Guid("BCDE0395-E52F-467C-8E3D-C4579291692E"))!;
        var deviceEnumerator = (IMMDeviceEnumerator)Activator.CreateInstance(enumeratorType)!;
        foreach (int flow in new[] { 1, 0 })
        {
            if (deviceEnumerator.EnumAudioEndpoints(flow, 1, out IMMDeviceCollection devices) != 0)
            {
                continue;
            }
            devices.GetCount(out int deviceCount);
            for (int d = 0; d < deviceCount; d++)
            {
                if (devices.Item(d, out IMMDevice device) != 0)
                {
                    continue;
                }
                device.GetId(out string deviceId);
                Guid iid = typeof(IAudioSessionManager2).GUID;
                if (device.Activate(ref iid, 23, IntPtr.Zero, out object manager) != 0)
                {
                    continue;
                }
                if (((IAudioSessionManager2)manager).GetSessionEnumerator(
                        out IAudioSessionEnumerator sessions) != 0)
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
                    if (!pids.Contains(pid))
                    {
                        continue;
                    }
                    control2.GetState(out int state);
                    float peak = 0;
                    if (control is IAudioMeterInformation meter)
                    {
                        for (int t = 0; t < 15; t++)
                        {
                            if (meter.GetPeakValue(out float value) == 0)
                            {
                                peak = Math.Max(peak, value);
                            }
                            Thread.Sleep(100);
                        }
                    }
                    string label = $"{(flow == 1 ? "采集" : "渲染")}/{deviceId[^12..]}/pid{pid}";
                    found.Add((label, state switch
                    {
                        0 => "Inactive",
                        1 => "Active",
                        2 => "Expired",
                        _ => "?",
                    }, peak));
                }
            }
        }
        return found;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct TcpRowOwnerPid
    {
        public uint state;
        public uint localAddr;
        public uint localPort;
        public uint remoteAddr;
        public uint remotePort;
        public uint owningPid;
    }

    [DllImport("iphlpapi.dll", SetLastError = true)]
    private static extern uint GetExtendedTcpTable(
        IntPtr table, ref int size, bool order, int af, int tableClass, uint reserved);

    [DllImport("user32.dll")]
    private static extern void keybd_event(
        byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);

    [ComImport, Guid("C02216F6-8C67-4B5B-9D00-D008E73E0064"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IAudioMeterInformation
    {
        [PreserveSig] int GetPeakValue(out float peak);
    }

    [ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IMMDeviceEnumerator
    {
        [PreserveSig] int EnumAudioEndpoints(int dataFlow, int stateMask, out IMMDeviceCollection devices);
        [PreserveSig] int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice device);
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
        [PreserveSig] int OpenPropertyStore(int access, out IntPtr properties);
        [PreserveSig] int GetId([MarshalAs(UnmanagedType.LPWStr)] out string id);
        [PreserveSig] int GetState(out int state);
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
