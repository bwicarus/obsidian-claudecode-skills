using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;

namespace BwReader.ComputerVoiceAudio;

internal sealed record CodexAppTarget(
    uint RootProcessId,
    long RootProcessStartFileTimeUtc,
    IReadOnlySet<uint> ProcessTree,
    nint WindowHandle,
    string AppKind = DirectAppTargets.CodexDesktop);

internal sealed record CodexAppProbeState(
    int RootCount,
    int WindowCount,
    CodexAppTarget? ReadyTarget);

internal sealed record CodexAudioPolicyTarget(
    CodexAppTarget AppTarget,
    uint ProcessId);

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
    private const uint SnapshotProcesses = 0x00000002;
    private const uint ProcessQueryLimitedInformation = 0x00001000;
    private const int ProcessCommandLineInformation = 60;
    private const int MaximumCommandLineBytes = 64 * 1024;
    private const string ChromiumAudioServiceMarker =
        "--utility-sub-type=audio.mojom.AudioService";
    private const uint KeyEventKeyUp = 0x0002;
    private const ushort VirtualKeyF24 = 0x87;
    private const uint InputKeyboard = 1;
    private const string RealtimeVoiceCommand = "realtimeVoice";
    private const string RealtimeVoiceShortcut = "F24";
    private const int MaximumKeybindingsBytes = 64 * 1024;

    internal static CodexAppTarget RequireReady() =>
        RequireReady(DirectAppTargets.CodexDesktop);

    internal static CodexAppTarget RequireReady(string appKind)
    {
        CodexAppProbeState state = Probe(appKind);
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

    internal static uint RequireAudioPolicyProcess(
        CodexAppTarget target)
    {
        uint[] matches = FindAudioPolicyProcesses(target);
        if (matches.Length == 1)
        {
            return matches[0];
        }
        throw new DirectProtocolException(
            matches.Length == 0
                ? "BW_COMPUTER_VOICE_DIRECT_AUDIO_SERVICE_NOT_READY"
                : "BW_COMPUTER_VOICE_DIRECT_AUDIO_SERVICE_AMBIGUOUS",
            matches.Length == 0
                ? "Codex 音频服务进程尚未就绪"
                : "检测到多个 Codex 音频服务进程",
            retryable: true);
    }

    internal static uint[] FindAudioPolicyProcesses(
        CodexAppTarget target)
    {
        ArgumentNullException.ThrowIfNull(target);
        return target.ProcessTree
            .Where(processId =>
                TryReadCommandLine(processId, out string commandLine)
                && commandLine.Contains(
                    ChromiumAudioServiceMarker,
                    StringComparison.Ordinal))
            .Order()
            .ToArray();
    }

    internal static async Task<CodexAudioPolicyTarget>
        WaitForAudioPolicyProcessAsync(
            CodexAppTarget expected,
            TimeSpan timeout,
            CancellationToken cancellationToken,
            Func<CodexAppProbeState>? probe = null,
            Func<CodexAppTarget, IReadOnlyList<uint>>? candidates = null,
            Func<TimeSpan, CancellationToken, Task>? delay = null)
    {
        ArgumentNullException.ThrowIfNull(expected);
        if (timeout <= TimeSpan.Zero)
        {
            throw new ArgumentOutOfRangeException(nameof(timeout));
        }
        probe ??= () => Probe(expected.AppKind);
        candidates ??= FindAudioPolicyProcesses;
        delay ??= Task.Delay;
        long deadline = Stopwatch.GetTimestamp()
            + checked((long)(timeout.TotalSeconds
                * Stopwatch.Frequency));
        int lastCandidateCount = 0;
        uint stableProcessId = 0;
        int stableObservationCount = 0;
        while (true)
        {
            cancellationToken.ThrowIfCancellationRequested();
            CodexAppProbeState state = probe();
            if (state.RootCount > 1)
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_APP_TARGET_CHANGED",
                    "等待音频服务时 Codex 目标进程变得不唯一",
                    retryable: true);
            }
            if (state.ReadyTarget is CodexAppTarget current)
            {
                if (
                    current.RootProcessId != expected.RootProcessId
                    || current.RootProcessStartFileTimeUtc
                        != expected.RootProcessStartFileTimeUtc
                )
                {
                    throw new DirectProtocolException(
                        "BW_COMPUTER_VOICE_DIRECT_APP_TARGET_CHANGED",
                        "等待音频服务时 Codex 目标进程已变化",
                        retryable: true);
                }
                uint[] currentCandidates = candidates(current)
                    .Distinct()
                    .Order()
                    .ToArray();
                lastCandidateCount = currentCandidates.Length;
                if (currentCandidates.Length == 1)
                {
                    uint processId = currentCandidates[0];
                    if (stableProcessId == processId)
                    {
                        stableObservationCount++;
                    }
                    else
                    {
                        stableProcessId = processId;
                        stableObservationCount = 1;
                    }
                    if (stableObservationCount >= 2)
                    {
                        return new CodexAudioPolicyTarget(
                            current,
                            processId);
                    }
                }
                else
                {
                    stableProcessId = 0;
                    stableObservationCount = 0;
                }
            }
            if (Stopwatch.GetTimestamp() >= deadline)
            {
                throw new DirectProtocolException(
                    lastCandidateCount == 0
                        ? "BW_COMPUTER_VOICE_DIRECT_AUDIO_SERVICE_NOT_READY"
                        : "BW_COMPUTER_VOICE_DIRECT_AUDIO_SERVICE_AMBIGUOUS",
                    lastCandidateCount == 0
                        ? "等待 Codex 音频服务进程就绪超时"
                        : "等待多个 Codex 音频服务进程收敛超时",
                    retryable: true);
            }
            await delay(
                    TimeSpan.FromMilliseconds(150),
                    cancellationToken)
                .ConfigureAwait(false);
        }
    }

    private static bool TryReadCommandLine(
        uint processId,
        out string commandLine)
    {
        commandLine = "";
        nint process = OpenProcess(
            ProcessQueryLimitedInformation,
            inheritHandle: false,
            processId);
        if (process == 0)
        {
            return false;
        }
        nint buffer = 0;
        try
        {
            _ = NtQueryInformationProcess(
                process,
                ProcessCommandLineInformation,
                0,
                0,
                out int required);
            if (
                required <= Marshal.SizeOf<UNICODE_STRING>()
                || required > MaximumCommandLineBytes
            )
            {
                return false;
            }
            buffer = Marshal.AllocHGlobal(required);
            int status = NtQueryInformationProcess(
                process,
                ProcessCommandLineInformation,
                buffer,
                required,
                out int returned);
            if (status < 0 || returned > required)
            {
                return false;
            }
            UNICODE_STRING value =
                Marshal.PtrToStructure<UNICODE_STRING>(buffer);
            if (
                value.Buffer == 0
                || value.Length == 0
                || value.Length > value.MaximumLength
                || (value.Length & 1) != 0
            )
            {
                return false;
            }
            commandLine = Marshal.PtrToStringUni(
                value.Buffer,
                value.Length / sizeof(char)) ?? "";
            return commandLine.Length != 0;
        }
        catch
        {
            commandLine = "";
            return false;
        }
        finally
        {
            if (buffer != 0)
            {
                Marshal.FreeHGlobal(buffer);
            }
            _ = CloseHandle(process);
        }
    }

    internal static CodexAppProbeState Probe() =>
        Probe(DirectAppTargets.CodexDesktop);

    internal static CodexAppProbeState Probe(string appKind)
    {
        if (!OperatingSystem.IsWindows())
        {
            throw new PlatformNotSupportedException(
                "BW_COMPUTER_VOICE_AUDIO_WINDOWS_REQUIRED");
        }

        DirectAppTargetProfile profile = DirectAppTargets.Require(appKind);
        int sessionId = Process.GetCurrentProcess().SessionId;
        Dictionary<uint, uint> parents = SnapshotParents();
        Dictionary<uint, Process> eligible = new();
        // 进程名/映像名按目标 profile 取:Codex 是 ChatGPT(.exe),GPT Classic 是
        // ChatGPT Classic(.exe)。此处曾硬编码 "ChatGPT",Classic 的进程一个都进不来。
        foreach (
            Process process in Process.GetProcessesByName(profile.ProcessName))
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
                        profile.PackagePathMarker,
                        StringComparison.OrdinalIgnoreCase)
                    || !path.EndsWith(
                        profile.ExecutableSuffix,
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
            long rootProcessStartFileTimeUtc;
            try
            {
                eligible[root].Refresh();
                rootProcessStartFileTimeUtc = eligible[root]
                    .StartTime
                    .ToUniversalTime()
                    .ToFileTimeUtc();
            }
            catch
            {
                return new CodexAppProbeState(
                    RootCount: 1,
                    windows.Length,
                    ReadyTarget: null);
            }
            if (rootProcessStartFileTimeUtc <= 0)
            {
                return new CodexAppProbeState(
                    RootCount: 1,
                    windows.Length,
                    ReadyTarget: null);
            }
            return new CodexAppProbeState(
                RootCount: 1,
                WindowCount: 1,
                new CodexAppTarget(
                    root,
                    rootProcessStartFileTimeUtc,
                    tree,
                    windows[0],
                    profile.AppKind));
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
                "Codex 全局语音快捷键必须唯一配置为 F24");
        }
    }

    internal static void RequireCurrentReadyTarget(
        CodexAppTarget expected)
    {
        ArgumentNullException.ThrowIfNull(expected);
        CodexAppTarget current;
        try
        {
            current = RequireReady(expected.AppKind);
        }
        catch (Exception exception)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_SHORTCUT_TARGET_UNAVAILABLE",
                "发送快捷键前无法确认唯一 Codex 目标",
                retryable: true,
                innerException: exception);
        }
        if (
            current.RootProcessId != expected.RootProcessId
            || current.RootProcessStartFileTimeUtc
                != expected.RootProcessStartFileTimeUtc
            || current.AppKind != expected.AppKind
        )
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_SHORTCUT_TARGET_CHANGED",
                "Codex 目标进程在发送快捷键前发生变化",
                retryable: true);
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
        if (expected.AppKind != DirectAppTargets.CodexDesktop)
        {
            return ShortcutFailure(
                "BW_COMPUTER_VOICE_DIRECT_SHORTCUT_TARGET_INVALID",
                "Codex 全局快捷键不能用于其他应用目标");
        }
        CodexAppTarget current;
        try
        {
            current = RequireReady(expected.AppKind);
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
                uint inserted = 0;
                for (int index = 0; index < events.Length; index++)
                {
                    KeybdEvent(
                        checked((byte)events[index].VirtualKey),
                        scan: 0,
                        events[index].KeyUp ? KeyEventKeyUp : 0,
                        extraInfo: 0);
                    inserted++;
                }
                return inserted;
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
        if (
            expected.AppKind != DirectAppTargets.CodexDesktop
            || current.AppKind != DirectAppTargets.CodexDesktop
            || current.RootProcessId != expected.RootProcessId
            || current.RootProcessStartFileTimeUtc
                != expected.RootProcessStartFileTimeUtc
        )
        {
            return ShortcutFailure(
                "BW_COMPUTER_VOICE_DIRECT_SHORTCUT_TARGET_CHANGED",
                "Codex 目标进程在发送快捷键前发生变化");
        }
        if (!shortcutConfigured)
        {
            return ShortcutFailure(
                "BW_COMPUTER_VOICE_DIRECT_SHORTCUT_CONFIG_INVALID",
                "Codex 全局语音快捷键必须唯一配置为 F24");
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
        if (inserted == 2)
        {
            return true;
        }
        if (inserted == 1)
        {
            // SendInput may have left F24 down. This cleanup is
            // intentionally best-effort and the activation remains failed.
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
                new(VirtualKeyF24, KeyUp: false),
                new(VirtualKeyF24, KeyUp: true),
            ],
            VoiceShortcutInputBatch.ReleasePressedKeys =>
            [
                new(VirtualKeyF24, KeyUp: true),
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
    private struct UNICODE_STRING
    {
        internal ushort Length;
        internal ushort MaximumLength;
        internal nint Buffer;
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

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern nint OpenProcess(
        uint desiredAccess,
        bool inheritHandle,
        uint processId);

    [DllImport("ntdll.dll")]
    private static extern int NtQueryInformationProcess(
        nint process,
        int processInformationClass,
        nint processInformation,
        int processInformationLength,
        out int returnLength);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern uint SendInput(
        uint inputCount,
        INPUT[] inputs,
        int inputSize);

    [DllImport(
        "user32.dll",
        EntryPoint = "keybd_event",
        SetLastError = false)]
    private static extern void KeybdEvent(
        byte virtualKey,
        byte scan,
        uint flags,
        nuint extraInfo);

}
