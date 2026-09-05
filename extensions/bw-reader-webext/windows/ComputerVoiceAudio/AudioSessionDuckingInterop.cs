using System.Runtime.InteropServices;
using BwReader.ComputerVoiceAudio.Interop;

namespace BwReader.ComputerVoiceAudio;

/// <summary>
/// IAudioSessionControl2（bfb7ff88-7239-4fc9-8fa2-07c950be9c6d）。
/// vtable = IAudioSessionControl 的 9 个方法 + 5 个扩展方法，顺序不能动。
/// 与 DirectOutputRouteObserver.cs 里的 IAudioSessionControlForRoute 方法表相同，
/// 但那一个声明的是基接口 GUID；要调 SetDuckingPreference 必须按 Control2 的 IID 去 QI，
/// 拿基接口指针去调第 14 个槽只是"通常碰巧能用"。
/// </summary>
[ComImport]
[Guid("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IAudioSessionControl2Ducking
{
    [PreserveSig]
    int GetState(out DirectAudioSessionState state);

    [PreserveSig]
    int GetDisplayName(out nint displayName);

    [PreserveSig]
    int SetDisplayName(
        [MarshalAs(UnmanagedType.LPWStr)] string displayName,
        ref Guid eventContext);

    [PreserveSig]
    int GetIconPath(out nint iconPath);

    [PreserveSig]
    int SetIconPath(
        [MarshalAs(UnmanagedType.LPWStr)] string iconPath,
        ref Guid eventContext);

    [PreserveSig]
    int GetGroupingParam(out Guid groupingId);

    [PreserveSig]
    int SetGroupingParam(
        ref Guid groupingId,
        ref Guid eventContext);

    [PreserveSig]
    int RegisterAudioSessionNotification(nint client);

    [PreserveSig]
    int UnregisterAudioSessionNotification(nint client);

    [PreserveSig]
    int GetSessionIdentifier(out nint sessionIdentifier);

    [PreserveSig]
    int GetSessionInstanceIdentifier(out nint sessionInstanceIdentifier);

    [PreserveSig]
    int GetProcessId(out uint processId);

    [PreserveSig]
    int IsSystemSoundsSession();

    [PreserveSig]
    int SetDuckingPreference(
        [MarshalAs(UnmanagedType.Bool)] bool optOut);
}

/// <summary>
/// Windows 通讯闪避（2026-09-05）。
///
/// 系统检测到"通讯活动"（有应用在默认通讯设备上开了通讯流）时，默认把**其它所有**音频会话
/// 降低 80%（HKCU\Software\Microsoft\Multimedia\Audio\UserDuckingPreference 缺省即此）。
/// 桥往虚拟麦克风端点渲染的是**用户的声音**，是喂给 Codex 的输入 —— 它不是"其它声音"，
/// 但对系统来说它就是一个普通会话。Codex 出声时若它的播放流带通讯角色，这一路就会被压 80%，
/// 表现恰好是用户 2026-09-05 描述的「AI 在说话时听不到我」。
///
/// 所以渲染会话一建好就显式退出闪避。退出失败不抛：老系统或虚拟线缆不支持时上行照常走，
/// 只是没有豁免；结果字符串进双工诊断，人看得见。
/// </summary>
internal static class AudioSessionDucking
{
    internal static readonly Guid IidIAudioSessionControl =
        new("F4B1A599-7266-4319-A8CA-E70ACB11E8CD");

    /// 最近一次为虚拟麦克风渲染会话退出闪避的结果（"opted-out" 或失败原因）。诊断用。
    internal static volatile string LastVirtualMicrophoneOptOut = "not-attempted";

    internal static string TryOptOut(IAudioClient audioClient)
    {
        nint sessionPointer = 0;
        try
        {
            Guid sessionControlId = IidIAudioSessionControl;
            int result = audioClient.GetService(
                ref sessionControlId,
                out sessionPointer);
            if (result < 0 || sessionPointer == 0)
            {
                return "get-session-control:0x"
                    + unchecked((uint)result).ToString("X8");
            }
            object sessionObject = Marshal.GetObjectForIUnknown(sessionPointer);
            if (sessionObject is not IAudioSessionControl2Ducking control)
            {
                return "session-control2-unavailable";
            }
            result = control.SetDuckingPreference(optOut: true);
            return result < 0
                ? "set-ducking-preference:0x" + unchecked((uint)result).ToString("X8")
                : "opted-out";
        }
        catch (Exception exception)
        {
            return "ducking-opt-out-failed:" + exception.GetType().Name;
        }
        finally
        {
            if (sessionPointer != 0)
            {
                _ = Marshal.Release(sessionPointer);
            }
        }
    }
}
