// 低层键盘钩子：判断注入的 F24 到底有没有进入系统输入流。
//
// 为什么需要它：配置里 realtimeVoice 绑的就是 F24，我们也确实发了 keybd_event，
// 但 Codex 毫无反应（麦克风台账不动、音频会话保持 Inactive）。
// 这时只有两种可能，而它们的修法完全相反：
//   · 注入被挡住了（UIPI/权限）→ 是我们这侧的问题；
//   · 注入到了、Codex 收到却不理 → 不是我们能修的，得换触发方式。
// 猜是没用的，装个钩子看一眼即可。
//
// 只读：钩子不吞键（一律 CallNextHookEx 放行），自己发一次 F24 然后看能不能收到。
using System.Diagnostics;
using System.Runtime.InteropServices;

internal static class HookProbe
{
    private const int WhKeyboardLl = 13;
    private const int WmKeyDown = 0x0100;
    private const int WmKeyUp = 0x0101;
    private const int WmSysKeyDown = 0x0104;
    private const byte VkF24 = 0x87;

    private static IntPtr _hook = IntPtr.Zero;
    private static HookProc? _proc;
    private static int _seen;

    internal static int Run()
    {
        Console.OutputEncoding = System.Text.Encoding.UTF8;
        _proc = Callback;
        using Process current = Process.GetCurrentProcess();
        using ProcessModule module = current.MainModule!;
        _hook = SetWindowsHookEx(
            WhKeyboardLl,
            _proc,
            GetModuleHandle(module.ModuleName),
            0);
        if (_hook == IntPtr.Zero)
        {
            Console.WriteLine($"装钩子失败 err={Marshal.GetLastWin32Error()}");
            return 2;
        }
        Console.WriteLine("钩子已装；1 秒后注入 F24…");

        var pump = new Thread(() =>
        {
            Thread.Sleep(1000);
            keybd_event(VkF24, 0, 0, UIntPtr.Zero);
            Thread.Sleep(60);
            keybd_event(VkF24, 0, 2, UIntPtr.Zero);
            Console.WriteLine("已注入 F24（按下+抬起）");
        });
        pump.IsBackground = true;
        pump.Start();

        // 低层钩子必须有消息循环才会被回调。
        int ticks = 0;
        while (ticks < 400)
        {
            if (PeekMessage(out MSG msg, IntPtr.Zero, 0, 0, 1))
            {
                TranslateMessage(ref msg);
                DispatchMessage(ref msg);
            }
            Thread.Sleep(10);
            ticks++;
        }
        UnhookWindowsHookEx(_hook);
        Console.WriteLine(_seen > 0
            ? $"结论：注入**到达了**系统输入流（收到 {_seen} 个 F24 事件）→ 键没被挡，是对方没响应"
            : "结论：注入**没有到达**系统输入流 → 被 UIPI/权限挡住了，是我们这侧的问题");
        return 0;
    }

    private static IntPtr Callback(int code, IntPtr wParam, IntPtr lParam)
    {
        if (code >= 0)
        {
            int message = wParam.ToInt32();
            if (message is WmKeyDown or WmKeyUp or WmSysKeyDown)
            {
                var data = Marshal.PtrToStructure<KbdllHookStruct>(lParam);
                if (data.vkCode == (uint)VkF24)
                {
                    _seen++;
                    bool injected = (data.flags & 0x10) != 0;
                    Console.WriteLine(
                        $"  收到 F24  msg=0x{message:X4} injected={injected} flags=0x{data.flags:X}");
                }
            }
        }
        return CallNextHookEx(_hook, code, wParam, lParam);
    }

    private delegate IntPtr HookProc(int code, IntPtr wParam, IntPtr lParam);

    [StructLayout(LayoutKind.Sequential)]
    private struct KbdllHookStruct
    {
        public uint vkCode;
        public uint scanCode;
        public uint flags;
        public uint time;
        public IntPtr dwExtraInfo;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct MSG
    {
        public IntPtr hwnd;
        public uint message;
        public IntPtr wParam;
        public IntPtr lParam;
        public uint time;
        public int ptX;
        public int ptY;
    }

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr SetWindowsHookEx(
        int idHook, HookProc lpfn, IntPtr hMod, uint dwThreadId);

    [DllImport("user32.dll")]
    private static extern bool UnhookWindowsHookEx(IntPtr hhk);

    [DllImport("user32.dll")]
    private static extern IntPtr CallNextHookEx(
        IntPtr hhk, int nCode, IntPtr wParam, IntPtr lParam);

    [DllImport("kernel32.dll")]
    private static extern IntPtr GetModuleHandle(string lpModuleName);

    [DllImport("user32.dll")]
    private static extern void keybd_event(
        byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);

    [DllImport("user32.dll")]
    private static extern bool PeekMessage(
        out MSG lpMsg, IntPtr hWnd, uint filterMin, uint filterMax, uint remove);

    [DllImport("user32.dll")]
    private static extern bool TranslateMessage(ref MSG lpMsg);

    [DllImport("user32.dll")]
    private static extern IntPtr DispatchMessage(ref MSG lpMsg);
}
