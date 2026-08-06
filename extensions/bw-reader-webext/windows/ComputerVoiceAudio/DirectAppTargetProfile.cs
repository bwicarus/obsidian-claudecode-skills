namespace BwReader.ComputerVoiceAudio;

internal sealed record DirectAppTargetProfile(
    string AppKind,
    string AppUserModelId,
    string PackagePathMarker,
    string MicrophoneConsentPackageKey,
    bool UsesCodexGlobalShortcut,
    // 本机实测:Codex 的进程/映像名是 ChatGPT / ChatGPT.exe,GPT Classic 的是
    // ChatGPT Classic / ChatGPT Classic.exe —— 两者并不同名。探测器过去硬编码
    // "ChatGPT",会把 Classic 的全部进程滤掉,导致它的窗口永远不被发现。
    string ProcessName,
    string ExecutableSuffix);

internal static class DirectAppTargets
{
    internal const string CodexDesktop = "codex-desktop";
    internal const string ChatGptClassic = "chatgpt-classic";

    private static readonly DirectAppTargetProfile Codex = new(
        CodexDesktop,
        DirectBridgeContract.CodexAppUserModelId,
        @"\WindowsApps\OpenAI.Codex_",
        "OpenAI.Codex_2p2nqsd0c76g0",
        UsesCodexGlobalShortcut: true,
        ProcessName: "ChatGPT",
        ExecutableSuffix: @"\ChatGPT.exe");

    private static readonly DirectAppTargetProfile Classic = new(
        ChatGptClassic,
        DirectBridgeContract.ChatGptClassicAppUserModelId,
        @"\WindowsApps\OpenAI.ChatGPT-Desktop_",
        "OpenAI.ChatGPT-Desktop_2p2nqsd0c76g0",
        UsesCodexGlobalShortcut: false,
        ProcessName: "ChatGPT Classic",
        ExecutableSuffix: @"\ChatGPT Classic.exe");

    internal static bool IsSupported(string appKind) =>
        appKind is CodexDesktop or ChatGptClassic;

    internal static DirectAppTargetProfile Require(string appKind) =>
        appKind switch
        {
            CodexDesktop => Codex,
            ChatGptClassic => Classic,
            _ => throw InvalidTarget(),
        };

    internal static DirectAppTargetProfile Require(
        string appKind,
        string appUserModelId)
    {
        DirectAppTargetProfile profile = Require(appKind);
        if (!string.Equals(
            profile.AppUserModelId,
            appUserModelId,
            StringComparison.Ordinal))
        {
            throw InvalidTarget();
        }
        return profile;
    }

    private static DirectProtocolException InvalidTarget() =>
        new(
            "BW_COMPUTER_VOICE_DIRECT_APP_TARGET_INVALID",
            "应用目标不在本机固定白名单");
}
