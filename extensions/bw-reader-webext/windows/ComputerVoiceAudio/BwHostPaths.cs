namespace BwReader.ComputerVoiceAudio;

/// <summary>
/// 桥要拉起的外部程序（Python / Codex CLI / 浏览器）在本机的位置。
/// </summary>
/// <remarks>
/// 2026-09-25 迁移到 Mac：这几处原来各自写死 Windows 路径（Python313\python.exe、
/// npm 下的 codex-win32-x64、Program Files 里的 msedge.exe），在 Mac 上全都"找不到"。
/// 收成一处：**Windows 上返回的路径与原来逐字相同**，Mac 上按下面的约定找；
/// 任何一项都可以用环境变量直接指定（BW_PYTHON / BW_CODEX / BW_BROWSER），
/// 服务的启动配置里写死一次，就不用猜。
/// </remarks>
internal static class BwHostPaths
{
    private static string Home =>
        Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);

    private static string? FromEnvironment(string name)
    {
        string? value = Environment.GetEnvironmentVariable(name);
        return string.IsNullOrWhiteSpace(value) ? null : value.Trim();
    }

    /// <summary>跑项目 Python 脚本用的解释器。</summary>
    internal static string Python()
    {
        string? configured = FromEnvironment("BW_PYTHON");
        if (configured is not null)
        {
            return configured;
        }
        if (OperatingSystem.IsWindows())
        {
            return Path.Combine(
                Environment.GetFolderPath(
                    Environment.SpecialFolder.LocalApplicationData),
                "Programs", "Python", "Python313", "python.exe");
        }
        // Mac 服务器：服务专用的 venv（依赖与 Windows 那份 Python 逐包对齐）。
        return Path.Combine(Home, "BW", "venv", "server", "bin", "python");
    }

    /// <summary>
    /// 非 Windows 上的 Codex CLI。Windows 调用方保留原来那份候选列表，这里返回 null。
    /// </summary>
    /// <remarks>
    /// 优先 npm 包里的原生二进制（不经 node 启动壳，launchd 的 PATH 里不必有 node）；
    /// 找不到再退回 npm 的 bin 壳。
    /// </remarks>
    internal static string? CodexOnUnix()
    {
        if (OperatingSystem.IsWindows())
        {
            return null;
        }
        string? configured = FromEnvironment("BW_CODEX");
        if (configured is not null)
        {
            return configured;
        }
        string nodeRoot = Path.Combine(Home, ".local", "opt", "node");
        string package = Path.Combine(
            nodeRoot, "lib", "node_modules", "@openai", "codex", "node_modules", "@openai");
        string[] candidates =
        [
            Path.Combine(package, "codex-darwin-arm64", "vendor",
                "aarch64-apple-darwin", "bin", "codex"),
            Path.Combine(package, "codex-darwin-x64", "vendor",
                "x86_64-apple-darwin", "bin", "codex"),
            Path.Combine(nodeRoot, "bin", "codex"),
            "/opt/homebrew/bin/codex",
            "/usr/local/bin/codex",
        ];
        return candidates.FirstOrDefault(File.Exists);
    }

    /// <summary>
    /// 非 Windows 上做无头截图用的 Chromium 系浏览器。Windows 调用方保留原来找 Edge 的逻辑。
    /// </summary>
    internal static string? BrowserOnUnix()
    {
        if (OperatingSystem.IsWindows())
        {
            return null;
        }
        string? configured = FromEnvironment("BW_BROWSER");
        if (configured is not null)
        {
            return configured;
        }
        string[] candidates =
        [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            Path.Combine(Home, "Applications", "Google Chrome.app", "Contents", "MacOS", "Google Chrome"),
        ];
        return candidates.FirstOrDefault(File.Exists);
    }
}
