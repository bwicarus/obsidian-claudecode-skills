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
        // 就绪与否由 ReadyTarget 说了算，不再由"恰好一个可见窗口"说了算。
        // Probe 已经按 app 类型决定要不要窗口（走全局快捷键的 Codex 不要求，
        // 靠 UIA 点按钮的 ChatGPT Classic 仍然要求）—— 这里再卡一次
        // WindowCount != 1，等于把那条放宽整个抵消掉：Codex 常驻托盘时
        // 可见窗口恒为 0，于是每次都抛 APP_WINDOW_AMBIGUOUS。
        if (state.ReadyTarget is null)
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
            // 走**全局快捷键**的目标（Codex）不要求有可见主窗口（2026-08-18）。
            //
            // 2026-08-18 本机实测：Codex 有 12 个进程、16 个顶层窗口，
            // 其中**可见的是 0 个** —— 它常驻托盘，而 .NET 的 MainWindowHandle
            // 只认可见窗口，于是这个判据恒不成立，WaitForUniqueReadyAsync 每次都熬到
            // 20 秒超时抛 TimeoutException，keepalive 因此永远起不来。
            // 用户报的"语音总是出问题 / 有时无法启动"，最硬的那一份出处就在这里。
            //
            // 而这个句柄在这条路上**根本不用来定位** —— F24 是 keybd_event 全局盲发的，
            // 句柄只是快捷键请求签名里的一个字段（做幂等去重），而签名里已经有
            // rootProcessId + startTime 足以唯一。也就是说这道门守的是一件它不需要的事。
            //
            // ⚠ ChatGPT Classic 那条路不同：它靠 UIA 点真实按钮
            // （ChatGptClassicVoiceAutomation 要 target.WindowHandle != 0），
            // 所以**只对 UsesCodexGlobalShortcut 放宽**，其余照旧要求恰好一个窗口。
            nint windowHandle;
            if (profile.UsesCodexGlobalShortcut)
            {
                // 有唯一可见窗口就带上（签名更具体），没有就带 0。
                windowHandle = windows.Length == 1 ? windows[0] : 0;
            }
            else if (windows.Length != 1)
            {
                return new CodexAppProbeState(
                    RootCount: 1,
                    windows.Length,
                    ReadyTarget: null);
            }
            else
            {
                windowHandle = windows[0];
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
                windows.Length,
                new CodexAppTarget(
                    root,
                    rootProcessStartFileTimeUtc,
                    tree,
                    windowHandle,
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

/// <summary>
/// 此刻有没有一个**能接住注入按键**的桌面。
/// </summary>
/// <remarks>
/// 2026-09-11 定案：F24 走 keybd_event 全局盲发，会话锁屏/断开时没有任何
/// 前台窗口，按键就凭空消失。当时的表现是连按 10 轮（跨 9.5 分钟）全部
/// `not-confirmed`，而 Codex 主窗口一直在 —— 于是这件事被一路记成
/// "按了但 Codex 没接住"，账本里成片的 not-confirmed 从来没人看出是锁屏。
///
/// 放在这个文件是因为按键本身就在这里发（SendInput / keybd_event）——
/// **守卫跟动作同处一侧**，才不会出现"某条路绕过了判据"。
/// </remarks>
internal static class WindowsInputDesktop
{
    private const uint DesktopSwitchDesktop = 0x0100;
    private const int UoiName = 2;

    /// <summary>能接住按键就返回真。</summary>
    /// <remarks>
    /// ⚠ **两个信号都说不行才算不行**。`OpenInputDesktop` 单独失败还可能是
    /// 权限之类的原因，只凭它就拦下按键，等于给"打不开语音"新增一个失败源；
    /// 而锁屏时两个信号必然同时成立（实测 OpenInputDesktop 打不开 +
    /// GetForegroundWindow() == 0）。宁可偶尔白按一次，也不要误拦。
    ///
    /// ⚠ 任何异常都当"可用"：判据的价值是省一次注定落空的按键，不是变成新的门。
    /// </remarks>
    internal static bool Usable()
    {
        try
        {
            return ProbeRaw();
        }
        catch (Exception)
        {
            return true;
        }
    }

    /// <summary>同一判据，但**不吞异常**。给自检用。</summary>
    /// <remarks>
    /// ⚠ 为什么要有这一份：<see cref="Usable"/> 把任何异常都当"可用"（那是对的，
    /// 判据坏掉不该让语音开不了）—— 可这也意味着**一个签名写错的 P/Invoke
    /// 会让整个闸门永远不触发，而且一点痕迹都不留**：调用抛
    /// EntryPointNotFoundException、被吞、返回 true、按键照发，
    /// 表现跟"没装这个闸门"完全一样。
    /// 项目里那份"静默失败十处清单"讲的就是这种：出了状况就悄悄什么都不做。
    /// 所以留一条不设防的入口，让自检能真的验到这几个 P/Invoke 是通的。
    /// </remarks>
    internal static bool ProbeRaw()
    {
        // ⚠ **决定性的信号是"有没有窗口拿着焦点"，不是桌面名**
        //（2026-09-11 第三版，前两版都被桥自己的观测推翻了）。
        //
        // 第一版："OpenInputDesktop 打得开就算可用"。我自己的进程在那个状态下
        // 确实打不开，判据在实验里成立 —— 可装进桥之后**一次都没触发**。
        // 第二版：改认输入桌面的名字（锁屏应是 Winlogon）。桥记下来的是
        //   `input=Default fg=0`
        // 也就是说桥打得开、名字还是 Default，所以这两版都判"可用"，照样按键、
        // 照样 not-confirmed。
        //
        // 真实状态是 `query session` 说的那个：会话 1 = **Disc（已断开）**，
        // 控制台在会话 2。断开的会话桌面还在、名字还是 Default，但**没有任何
        // 窗口拿着焦点** —— 于是注入的按键没有收件人。
        //
        // 教训写在这儿：前两版都是"我这个进程观察到的现象"直接当成判据，
        // 而执行按键的是**另一个进程**。判据必须由执行方自己量，
        // 这也是为什么这条链上每次判断都要落盘。
        nint desktop = OpenInputDesktop(0, false, DesktopSwitchDesktop);
        if (desktop != 0)
        {
            try
            {
                string name = NameOf(desktop);
                if (name.Length > 0
                    && !string.Equals(
                        name, "Default", StringComparison.OrdinalIgnoreCase))
                {
                    return false;       // Winlogon 等安全桌面：确定接不住
                }
            }
            finally
            {
                CloseDesktop(desktop);
            }
        }
        if (GetForegroundWindow() != 0)
        {
            return true;
        }
        // ⚠ 前台为 0 也可能只是窗口切换中的一瞬。再看一次才下"接不住"的结论 ——
        // 误拦的代价是语音开不了，比白按一次贵得多。
        System.Threading.Thread.Sleep(150);
        return GetForegroundWindow() != 0;
    }

    /// <summary>把此刻看到的信号原样说出来，给账本用。</summary>
    /// <remarks>
    /// ⚠ 这条存在的理由：`voice-start-attempts.jsonl` 攒了 155 条样本，
    /// **一条都回答不了"为什么没成"** —— 因为当时的世界状态一个字都没记。
    /// 折成一个布尔之前先把原始值留下，是那份"静默失败"清单里的第二条规矩。
    /// </remarks>
    internal static string Describe()
    {
        try
        {
            nint desktop = OpenInputDesktop(0, false, DesktopSwitchDesktop);
            string name;
            if (desktop == 0)
            {
                name = "open-failed";
            }
            else
            {
                try
                {
                    name = NameOf(desktop);
                    if (name.Length == 0)
                    {
                        name = "no-name";
                    }
                }
                finally
                {
                    CloseDesktop(desktop);
                }
            }
            // 两次前台取样都留下 —— 判据就是靠"连着两次都是 0"下的结论，
            // 只记一次事后就分不清"真的没人拿焦点"和"刚好撞上切换那一瞬"。
            string first = GetForegroundWindow() != 0 ? "1" : "0";
            System.Threading.Thread.Sleep(150);
            string second = GetForegroundWindow() != 0 ? "1" : "0";
            return "input=" + name + " fg=" + first + second;
        }
        catch (Exception exception)
        {
            return "probe-threw=" + exception.GetType().Name;
        }
    }

    private static string NameOf(nint desktop)
    {
        System.Text.StringBuilder buffer = new(256);
        if (!GetUserObjectInformationW(
                desktop, UoiName, buffer, buffer.Capacity * 2, out _))
        {
            return string.Empty;
        }
        return buffer.ToString();
    }

    [DllImport("user32.dll", SetLastError = true)]
    private static extern nint OpenInputDesktop(
        uint flags,
        [MarshalAs(UnmanagedType.Bool)] bool inherit,
        uint desiredAccess);

    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CloseDesktop(nint desktop);

    [DllImport("user32.dll")]
    private static extern nint GetForegroundWindow();

    [DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetUserObjectInformationW(
        nint handle,
        int index,
        System.Text.StringBuilder buffer,
        int length,
        out int lengthNeeded);
}
