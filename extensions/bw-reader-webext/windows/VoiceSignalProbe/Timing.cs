// 高分辨率计时：量"按下 → 语音真正就绪"到底要多久。
//
// 用户提供的关键线索：**语音加载完成时会叮咚一声**。那声提示音是 Codex 在渲染侧放的，
// 所以"初始化完成"这一刻是可观测的 —— 渲染会话的峰值电平会出现一个尖峰。
// 在此之前我一直以为这件事没法计时（因为我拿不到"语音开没开"的可靠信号），
// 而那个判断已经被用户手动开关一次证伪了：台账、会话状态、峰值三路都可靠。
//
// 采样 100ms 一次，只打印变化：台账翻转、会话状态翻转、渲染侧首次出现声音。
using System.Diagnostics;
using System.Runtime.InteropServices;
using Microsoft.Win32;

internal static class Timing
{
    internal static int Run(int seconds)
    {
        Console.OutputEncoding = System.Text.Encoding.UTF8;
        Console.WriteLine($"高分辨率计时 {seconds} 秒（100ms 采样，只打印变化）");
        var stopwatch = Stopwatch.StartNew();
        bool? lastLedger = null;
        string lastStates = "";
        bool renderQuiet = true;
        DateTime deadline = DateTime.UtcNow.AddSeconds(seconds);
        while (DateTime.UtcNow < deadline)
        {
            bool ledger = LedgerActive();
            var (states, renderPeak, capturePeak) = Sessions();
            if (lastLedger != ledger)
            {
                Console.WriteLine(
                    $"[{DateTime.Now:HH:mm:ss.fff}] 台账 active = {ledger}");
                lastLedger = ledger;
            }
            if (states != lastStates)
            {
                Console.WriteLine(
                    $"[{DateTime.Now:HH:mm:ss.fff}] 会话 {states}");
                lastStates = states;
            }
            // 渲染侧从静默变成有声 = Codex 发出了声音（「叮咚」就在这里）
            if (renderQuiet && renderPeak > 0.02f)
            {
                Console.WriteLine(
                    $"[{DateTime.Now:HH:mm:ss.fff}] ♪ 渲染侧出声 peak={renderPeak:F3}（就绪提示音）");
                renderQuiet = false;
            }
            else if (!renderQuiet && renderPeak <= 0.005f)
            {
                renderQuiet = true;
            }
            Thread.Sleep(100);
        }
        Console.WriteLine($"--- 结束（{stopwatch.Elapsed.TotalSeconds:F0}s）---");
        return 0;
    }

    private static bool LedgerActive()
    {
        try
        {
            using RegistryKey? key = Registry.CurrentUser.OpenSubKey(
                @"SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone\OpenAI.CodexBeta_2p2nqsd0c76g0");
            long start = Convert.ToInt64(key?.GetValue("LastUsedTimeStart") ?? 0L);
            long stop = Convert.ToInt64(key?.GetValue("LastUsedTimeStop") ?? 0L);
            return start > 0 && (stop == 0 || start > stop);
        }
        catch
        {
            return false;
        }
    }

    private static (string States, float RenderPeak, float CapturePeak) Sessions()
    {
        var pids = new HashSet<uint>();
        foreach (Process process in Process.GetProcessesByName("ChatGPT (Beta)"))
        {
            pids.Add((uint)process.Id);
            process.Dispose();
        }
        string states = "";
        float render = 0, capture = 0;
        try
        {
            var enumeratorType = Type.GetTypeFromCLSID(
                new Guid("BCDE0395-E52F-467C-8E3D-C4579291692E"))!;
            var deviceEnumerator =
                (IMMDeviceEnumerator)Activator.CreateInstance(enumeratorType)!;
            foreach (int flow in new[] { 1, 0 })
            {
                if (deviceEnumerator.EnumAudioEndpoints(
                        flow, 1, out IMMDeviceCollection devices) != 0)
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
                        states += (flow == 1 ? "采集=" : "渲染=") +
                            (state == 1 ? "Active " : state == 0 ? "Inactive " : "? ");
                        if (control is IAudioMeterInformation meter &&
                            meter.GetPeakValue(out float peak) == 0)
                        {
                            if (flow == 0) render = Math.Max(render, peak);
                            else capture = Math.Max(capture, peak);
                        }
                    }
                }
            }
        }
        catch
        {
        }
        return (states.Trim(), render, capture);
    }

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
