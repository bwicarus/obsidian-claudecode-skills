using System.Diagnostics;
using System.Runtime.InteropServices;

namespace BwReader.ComputerVoiceAudio;

internal sealed record CodexAppTarget(
    uint RootProcessId,
    IReadOnlySet<uint> ProcessTree,
    nint WindowHandle);

internal sealed record CodexAppProbeState(
    int RootCount,
    int WindowCount,
    CodexAppTarget? ReadyTarget);

internal enum VoiceShortcutInputBatch
{
    Activation,
    ReleasePressedKeys,
}

internal readonly record struct VoiceShortcutKeyEvent(
    ushort VirtualKey,
    bool KeyUp);

internal static class WindowsCodexAppProbe
{
    // The packaged app currently exposes only the observed START shortcut to
    // this bridge.  There is no locally verified, ownership-safe application
    // voice stop primitive.  Bridge STOP must not guess that sending the same
    // shortcut a second time is a safe toggle.
    internal static bool SupportsOwnedVoiceStop => false;

    private const uint SnapshotProcesses = 0x00000002;
    private const uint KeyEventKeyUp = 0x0002;
    private const ushort VirtualKeyControl = 0x11;
    private const ushort VirtualKeyShift = 0x10;
    private const ushort VirtualKeyC = 0x43;
    private const int InputKeyboard = 1;
    private const int ShowRestore = 9;

    internal static CodexAppTarget RequireReady()
    {
        CodexAppProbeState state = Probe();
        if (state.RootCount != 1)
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_APP_TREE_AMBIGUOUS");
        }
        if (state.WindowCount != 1 || state.ReadyTarget is null)
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_APP_WINDOW_AMBIGUOUS");
        }
        return state.ReadyTarget;
    }

    internal static CodexAppProbeState Probe()
    {
        if (!OperatingSystem.IsWindows())
        {
            throw new PlatformNotSupportedException(
                "BW_COMPUTER_VOICE_AUDIO_WINDOWS_REQUIRED");
        }

        int sessionId = Process.GetCurrentProcess().SessionId;
        Dictionary<uint, uint> parents = SnapshotParents();
        Dictionary<uint, Process> eligible = new();
        foreach (Process process in Process.GetProcessesByName("ChatGPT"))
        {
            try
            {
                if (process.SessionId != sessionId)
                {
                    process.Dispose();
                    continue;
                }
                string path = process.MainModule?.FileName ?? "";
                if (
                    !path.Contains(
                        @"\WindowsApps\OpenAI.Codex_",
                        StringComparison.OrdinalIgnoreCase)
                    || !path.EndsWith(
                        @"\ChatGPT.exe",
                        StringComparison.OrdinalIgnoreCase)
                )
                {
                    process.Dispose();
                    continue;
                }
                eligible[checked((uint)process.Id)] = process;
            }
            catch
            {
                process.Dispose();
            }
        }

        try
        {
            uint[] roots = eligible.Keys.Where(processId =>
                !parents.TryGetValue(processId, out uint parentId)
                || !eligible.ContainsKey(parentId)).ToArray();
            if (roots.Length != 1)
            {
                return new CodexAppProbeState(
                    roots.Length,
                    WindowCount: 0,
                    ReadyTarget: null);
            }
            uint root = roots[0];
            HashSet<uint> tree = [];
            Queue<uint> pending = new();
            pending.Enqueue(root);
            while (pending.TryDequeue(out uint current))
            {
                if (!tree.Add(current))
                {
                    continue;
                }
                foreach ((uint child, uint parent) in parents)
                {
                    if (parent == current && eligible.ContainsKey(child))
                    {
                        pending.Enqueue(child);
                    }
                }
            }
            nint[] windows = tree
                .Where(eligible.ContainsKey)
                .Select(processId =>
                {
                    try
                    {
                        eligible[processId].Refresh();
                        return eligible[processId].MainWindowHandle;
                    }
                    catch
                    {
                        return 0;
                    }
                })
                .Where(handle => handle != 0)
                .Distinct()
                .ToArray();
            if (windows.Length != 1)
            {
                return new CodexAppProbeState(
                    RootCount: 1,
                    windows.Length,
                    ReadyTarget: null);
            }
            return new CodexAppProbeState(
                RootCount: 1,
                WindowCount: 1,
                new CodexAppTarget(root, tree, windows[0]));
        }
        finally
        {
            foreach (Process process in eligible.Values)
            {
                process.Dispose();
            }
        }
    }

    internal static bool SendVoiceShortcut(CodexAppTarget expected)
    {
        CodexAppTarget current = RequireReady();
        if (current.RootProcessId != expected.RootProcessId
            || !current.ProcessTree.SetEquals(expected.ProcessTree))
        {
            return false;
        }
        _ = ShowWindow(current.WindowHandle, ShowRestore);
        if (!SetForegroundWindow(current.WindowHandle))
        {
            return false;
        }
        nint foreground = GetForegroundWindow();
        _ = GetWindowThreadProcessId(foreground, out uint foregroundPid);
        if (!current.ProcessTree.Contains(foregroundPid))
        {
            return false;
        }

        return SendVoiceShortcutInputSequence(batch =>
        {
            VoiceShortcutKeyEvent[] events =
                VoiceShortcutEvents(batch);
            INPUT[] inputs = new INPUT[events.Length];
            for (int index = 0; index < events.Length; index++)
            {
                inputs[index] = Key(
                    events[index].VirtualKey,
                    events[index].KeyUp);
            }
            return SendInput(
                checked((uint)inputs.Length),
                inputs,
                Marshal.SizeOf<INPUT>());
        });
    }

    internal static bool SendVoiceShortcutInputSequence(
        Func<VoiceShortcutInputBatch, uint> sendBatch)
    {
        uint inserted = sendBatch(VoiceShortcutInputBatch.Activation);
        if (inserted == 6)
        {
            return true;
        }
        if (inserted is >= 1 and <= 5)
        {
            // SendInput may have left Ctrl, Shift, or C down.  Release all
            // three in reverse order.  This cleanup is intentionally
            // best-effort and the activation remains failed regardless of
            // how many release events Windows accepts.
            try
            {
                _ = sendBatch(
                    VoiceShortcutInputBatch.ReleasePressedKeys);
            }
            catch
            {
            }
        }
        return false;
    }

    internal static VoiceShortcutKeyEvent[] VoiceShortcutEvents(
        VoiceShortcutInputBatch batch) =>
        batch switch
        {
            VoiceShortcutInputBatch.Activation =>
            [
                new(VirtualKeyControl, KeyUp: false),
                new(VirtualKeyShift, KeyUp: false),
                new(VirtualKeyC, KeyUp: false),
                new(VirtualKeyC, KeyUp: true),
                new(VirtualKeyShift, KeyUp: true),
                new(VirtualKeyControl, KeyUp: true),
            ],
            VoiceShortcutInputBatch.ReleasePressedKeys =>
            [
                new(VirtualKeyC, KeyUp: true),
                new(VirtualKeyShift, KeyUp: true),
                new(VirtualKeyControl, KeyUp: true),
            ],
            _ => throw new ArgumentOutOfRangeException(nameof(batch)),
        };

    private static Dictionary<uint, uint> SnapshotParents()
    {
        nint snapshot = CreateToolhelp32Snapshot(
            SnapshotProcesses,
            processId: 0);
        if (snapshot == -1)
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_PROCESS_SNAPSHOT_FAILED");
        }
        try
        {
            PROCESSENTRY32 entry = new()
            {
                Size = checked((uint)Marshal.SizeOf<PROCESSENTRY32>()),
            };
            Dictionary<uint, uint> result = [];
            if (!Process32First(snapshot, ref entry))
            {
                throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_PROCESS_SNAPSHOT_FAILED");
            }
            do
            {
                result[entry.ProcessId] = entry.ParentProcessId;
            }
            while (Process32Next(snapshot, ref entry));
            return result;
        }
        finally
        {
            _ = CloseHandle(snapshot);
        }
    }

    private static INPUT Key(ushort key, bool keyUp) => new()
    {
        Type = InputKeyboard,
        Union = new INPUTUNION
        {
            Keyboard = new KEYBDINPUT
            {
                VirtualKey = key,
                Flags = keyUp ? KeyEventKeyUp : 0,
            },
        },
    };

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct PROCESSENTRY32
    {
        internal uint Size;
        internal uint Usage;
        internal uint ProcessId;
        internal nint DefaultHeapId;
        internal uint ModuleId;
        internal uint Threads;
        internal uint ParentProcessId;
        internal int PriorityClassBase;
        internal uint Flags;

        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)]
        internal string? ExeFile;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct INPUT
    {
        internal int Type;
        internal INPUTUNION Union;
    }

    [StructLayout(LayoutKind.Explicit)]
    private struct INPUTUNION
    {
        [FieldOffset(0)]
        internal KEYBDINPUT Keyboard;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct KEYBDINPUT
    {
        internal ushort VirtualKey;
        internal ushort Scan;
        internal uint Flags;
        internal uint Time;
        internal nint ExtraInfo;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern nint CreateToolhelp32Snapshot(
        uint flags,
        uint processId);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool Process32First(
        nint snapshot,
        ref PROCESSENTRY32 entry);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool Process32Next(
        nint snapshot,
        ref PROCESSENTRY32 entry);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(nint handle);

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(nint window);

    [DllImport("user32.dll")]
    private static extern bool ShowWindow(nint window, int command);

    [DllImport("user32.dll")]
    private static extern nint GetForegroundWindow();

    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(
        nint window,
        out uint processId);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern uint SendInput(
        uint inputCount,
        INPUT[] inputs,
        int inputSize);
}
