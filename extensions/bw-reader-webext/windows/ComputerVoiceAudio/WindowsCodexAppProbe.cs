using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;

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

internal sealed record VoiceShortcutSendResult(
    bool Sent,
    string? FailureCode,
    string? FailureDetail,
    uint InsertedInputCount,
    int Win32Error);

internal readonly record struct VoiceShortcutInteropLayout(
    int PointerSize,
    int InputSize,
    int UnionSize,
    int KeyboardSize,
    int MouseSize,
    int HardwareSize,
    int UnionOffset);

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
    private const uint InputKeyboard = 1;
    private const string RealtimeVoiceCommand = "realtimeVoice";
    private const string RealtimeVoiceShortcut = "Ctrl+Shift+C";
    private const int MaximumKeybindingsBytes = 64 * 1024;

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
        return SendVoiceShortcutDetailed(expected).Sent;
    }

    internal static void RequireExpectedGlobalVoiceShortcut()
    {
        if (!TryReadExpectedGlobalVoiceShortcut())
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_SHORTCUT_CONFIG_INVALID",
                "Codex 全局语音快捷键必须唯一配置为 Ctrl+Shift+C");
        }
    }

    internal static void SendVoiceShortcutOrThrow(
        CodexAppTarget expected)
    {
        VoiceShortcutSendResult result =
            SendVoiceShortcutDetailed(expected);
        if (result.Sent)
        {
            return;
        }
        Exception? innerException = result.Win32Error == 0
            ? null
            : new Win32Exception(result.Win32Error);
        throw new DirectProtocolException(
            result.FailureCode
                ?? "BW_COMPUTER_VOICE_DIRECT_SHORTCUT_FAILED",
            result.FailureDetail
                ?? "Codex 全局语音快捷键发送失败",
            retryable:
                result.FailureCode
                    != "BW_COMPUTER_VOICE_DIRECT_SHORTCUT_CONFIG_INVALID",
            innerException);
    }

    internal static VoiceShortcutSendResult
        SendVoiceShortcutDetailed(CodexAppTarget expected)
    {
        CodexAppTarget current;
        try
        {
            current = RequireReady();
        }
        catch
        {
            return ShortcutFailure(
                "BW_COMPUTER_VOICE_DIRECT_SHORTCUT_TARGET_UNAVAILABLE",
                "发送快捷键前无法确认唯一 Codex 目标");
        }
        bool shortcutConfigured =
            TryReadExpectedGlobalVoiceShortcut();
        return SendValidatedGlobalVoiceShortcut(
            expected,
            current,
            shortcutConfigured,
            batch =>
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
            },
            Marshal.GetLastPInvokeError);
    }

    internal static VoiceShortcutSendResult
        SendValidatedGlobalVoiceShortcut(
            CodexAppTarget expected,
            CodexAppTarget current,
            bool shortcutConfigured,
            Func<VoiceShortcutInputBatch, uint> sendBatch,
            Func<int> getLastError)
    {
        ArgumentNullException.ThrowIfNull(expected);
        ArgumentNullException.ThrowIfNull(current);
        ArgumentNullException.ThrowIfNull(sendBatch);
        ArgumentNullException.ThrowIfNull(getLastError);

        // Electron child processes are dynamic.  The process-loopback target
        // and the global-hotkey owner are both anchored to the stable packaged
        // app root, so unrelated child churn must not reject START.
        if (current.RootProcessId != expected.RootProcessId)
        {
            return ShortcutFailure(
                "BW_COMPUTER_VOICE_DIRECT_SHORTCUT_TARGET_CHANGED",
                "Codex 目标进程在发送快捷键前发生变化");
        }
        if (!shortcutConfigured)
        {
            return ShortcutFailure(
                "BW_COMPUTER_VOICE_DIRECT_SHORTCUT_CONFIG_INVALID",
                "Codex 全局语音快捷键必须唯一配置为 Ctrl+Shift+C");
        }

        uint insertedInputCount = 0;
        int win32Error = 0;
        bool sent = SendVoiceShortcutInputSequence(batch =>
        {
            uint inserted = sendBatch(batch);
            if (batch == VoiceShortcutInputBatch.Activation)
            {
                insertedInputCount = inserted;
                win32Error = getLastError();
            }
            return inserted;
        });
        return sent
            ? new VoiceShortcutSendResult(
                Sent: true,
                FailureCode: null,
                FailureDetail: null,
                InsertedInputCount: insertedInputCount,
                Win32Error: 0)
            : ShortcutFailure(
                "BW_COMPUTER_VOICE_DIRECT_SHORTCUT_INPUT_FAILED",
                "Windows 未完整发送 Codex 全局语音快捷键",
                insertedInputCount,
                win32Error);
    }

    internal static bool IsExpectedGlobalVoiceShortcutConfig(
        string json)
    {
        try
        {
            if (
                string.IsNullOrWhiteSpace(json)
                || Encoding.UTF8.GetByteCount(json)
                    > MaximumKeybindingsBytes
            )
            {
                return false;
            }
            using JsonDocument document = JsonDocument.Parse(
                json,
                new JsonDocumentOptions
                {
                    AllowTrailingCommas = true,
                    CommentHandling = JsonCommentHandling.Skip,
                });
            if (document.RootElement.ValueKind
                != JsonValueKind.Array)
            {
                return false;
            }
            int commandCount = 0;
            int shortcutCount = 0;
            bool exactBinding = false;
            foreach (JsonElement item in
                document.RootElement.EnumerateArray())
            {
                if (
                    item.ValueKind != JsonValueKind.Object
                )
                {
                    continue;
                }
                string command =
                    item.TryGetProperty(
                        "command",
                        out JsonElement commandElement)
                    && commandElement.ValueKind
                        == JsonValueKind.String
                        ? commandElement.GetString() ?? ""
                        : "";
                string key =
                    item.TryGetProperty(
                        "key",
                        out JsonElement keyElement)
                    && keyElement.ValueKind == JsonValueKind.String
                        ? keyElement.GetString() ?? ""
                        : "";
                if (command == RealtimeVoiceCommand)
                {
                    commandCount++;
                    exactBinding |= key == RealtimeVoiceShortcut;
                }
                if (key == RealtimeVoiceShortcut)
                {
                    shortcutCount++;
                }
            }
            return commandCount == 1
                && shortcutCount == 1
                && exactBinding;
        }
        catch (JsonException)
        {
            return false;
        }
    }

    private static bool TryReadExpectedGlobalVoiceShortcut()
    {
        try
        {
            string userProfile = Environment.GetFolderPath(
                Environment.SpecialFolder.UserProfile);
            if (string.IsNullOrWhiteSpace(userProfile))
            {
                return false;
            }
            string path = Path.Combine(
                userProfile,
                ".codex",
                "keybindings.json");
            if (!File.Exists(path))
            {
                return false;
            }
            FileInfo info = new(path);
            return info.Length is > 0 and <= MaximumKeybindingsBytes
                && IsExpectedGlobalVoiceShortcutConfig(
                    File.ReadAllText(path));
        }
        catch
        {
            return false;
        }
    }

    private static VoiceShortcutSendResult ShortcutFailure(
        string code,
        string detail,
        uint insertedInputCount = 0,
        int win32Error = 0) =>
        new(
            Sent: false,
            FailureCode: code,
            FailureDetail: detail,
            InsertedInputCount: insertedInputCount,
            Win32Error: win32Error);

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

    internal static VoiceShortcutInteropLayout
        GetVoiceShortcutInteropLayout() =>
        new(
            PointerSize: IntPtr.Size,
            InputSize: Marshal.SizeOf<INPUT>(),
            UnionSize: Marshal.SizeOf<INPUTUNION>(),
            KeyboardSize: Marshal.SizeOf<KEYBDINPUT>(),
            MouseSize: Marshal.SizeOf<MOUSEINPUT>(),
            HardwareSize: Marshal.SizeOf<HARDWAREINPUT>(),
            UnionOffset: Marshal.OffsetOf<INPUT>(nameof(INPUT.Union))
                .ToInt32());

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
        internal uint Type;
        internal INPUTUNION Union;
    }

    [StructLayout(LayoutKind.Explicit)]
    private struct INPUTUNION
    {
        [FieldOffset(0)]
        internal MOUSEINPUT Mouse;

        [FieldOffset(0)]
        internal KEYBDINPUT Keyboard;

        [FieldOffset(0)]
        internal HARDWAREINPUT Hardware;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct MOUSEINPUT
    {
        internal int X;
        internal int Y;
        internal uint MouseData;
        internal uint Flags;
        internal uint Time;
        internal nuint ExtraInfo;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct KEYBDINPUT
    {
        internal ushort VirtualKey;
        internal ushort Scan;
        internal uint Flags;
        internal uint Time;
        internal nuint ExtraInfo;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct HARDWAREINPUT
    {
        internal uint Message;
        internal ushort ParameterLow;
        internal ushort ParameterHigh;
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

    [DllImport("user32.dll", SetLastError = true)]
    private static extern uint SendInput(
        uint inputCount,
        INPUT[] inputs,
        int inputSize);
}
