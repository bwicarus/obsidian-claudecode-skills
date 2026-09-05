namespace BwReader.ComputerVoiceAudio;

internal sealed record DirectAppTargetProfile(
    string AppKind,
    string AppUserModelId,
    string PackagePathMarker,
    string MicrophoneConsentPackageKey,
    bool UsesCodexGlobalShortcut,
    // 本机实测（2026-09-06 换到正式版）:Codex 正式版包 OpenAI.Codex_… 的进程/映像名是
    // ChatGPT / ChatGPT.exe（包目录里另有 Codex.exe，但跑起来的主进程是 ChatGPT.exe）；
    // Beta 包是 ChatGPT (Beta) / ChatGPT (Beta).exe；GPT Classic 是 ChatGPT Classic /
    // ChatGPT Classic.exe —— 三者并不同名，必须与固定包身份（路径标记）一并精确匹配。
    // 路径标记 "\OpenAI.Codex_" 带下划线，不会前缀命中 "OpenAI.CodexBeta_"。
    string ProcessName,
    string ExecutableSuffix);

internal static class DirectAppTargets
{
    internal const string CodexDesktop = "codex-desktop";
    /// Beta 包保留为一个可选目标，只给回滚用；默认一律正式版（用户 2026-09-06 拍板）。
    internal const string CodexDesktopBeta = "codex-desktop-beta";
    internal const string ChatGptClassic = "chatgpt-classic";

    private static readonly DirectAppTargetProfile Codex = new(
        CodexDesktop,
        DirectBridgeContract.CodexAppUserModelId,
        @"\WindowsApps\OpenAI.Codex_",
        "OpenAI.Codex_2p2nqsd0c76g0",
        UsesCodexGlobalShortcut: true,
        ProcessName: "ChatGPT",
        ExecutableSuffix: @"\ChatGPT.exe");

    private static readonly DirectAppTargetProfile CodexBeta = new(
        CodexDesktopBeta,
        DirectBridgeContract.CodexBetaAppUserModelId,
        @"\WindowsApps\OpenAI.CodexBeta_",
        "OpenAI.CodexBeta_2p2nqsd0c76g0",
        UsesCodexGlobalShortcut: true,
        ProcessName: "ChatGPT (Beta)",
        ExecutableSuffix: @"\ChatGPT (Beta).exe");

    private static readonly DirectAppTargetProfile Classic = new(
        ChatGptClassic,
        DirectBridgeContract.ChatGptClassicAppUserModelId,
        @"\WindowsApps\OpenAI.ChatGPT-Desktop_",
        "OpenAI.ChatGPT-Desktop_2p2nqsd0c76g0",
        UsesCodexGlobalShortcut: false,
        ProcessName: "ChatGPT Classic",
        ExecutableSuffix: @"\ChatGPT Classic.exe");

    internal static bool IsSupported(string appKind) =>
        appKind is CodexDesktop or CodexDesktopBeta or ChatGptClassic;

    internal static DirectAppTargetProfile Require(string appKind) =>
        appKind switch
        {
            CodexDesktop => Codex,
            CodexDesktopBeta => CodexBeta,
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
