using System.Runtime.InteropServices;

namespace BwReader.ComputerVoiceAudio;

internal sealed record CodexAppAudioRouteProbeItem(
    string Flow,
    string Role,
    string Target,
    string State,
    string? EndpointId,
    bool Match,
    int? HResult,
    string? Stage);

internal static class CodexAppAudioRouteProbe
{
    internal const string Contract =
        "reader-computer-voice-codex-app-audio-route-probe/1";

    internal static object Run(
        DirectBridgeConfig config,
        Func<CodexAppTarget>? targetProvider = null,
        Func<IPerAppAudioPolicyBackend>? backendFactory = null,
        Func<CodexAppTarget, uint>? audioPolicyProcessProvider = null)
    {
        ArgumentNullException.ThrowIfNull(config);
        if (
            !config.PerAppAudioRouteAutomationEnabled
            || string.IsNullOrWhiteSpace(
                config.VirtualMicrophoneCaptureEndpointId)
        )
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_NOT_CONFIGURED",
                "当前配置未启用 Codex 按应用自动音频路由");
        }

        targetProvider ??= WindowsCodexAppProbe.RequireReady;
        CodexAppTarget target = targetProvider();
        if (target.RootProcessId == 0)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_APP_TREE_AMBIGUOUS",
                "无法确认唯一 Codex 目标进程");
        }

        IPerAppAudioPolicyBackend selectedBackend;
        if (backendFactory is null)
        {
            if (!OperatingSystem.IsWindows())
            {
                throw new PlatformNotSupportedException(
                    "Codex app audio routing requires Windows");
            }
            selectedBackend = new NativePerAppAudioPolicyBackend();
        }
        else
        {
            selectedBackend = backendFactory();
        }
        using IPerAppAudioPolicyBackend backend = selectedBackend;
        audioPolicyProcessProvider ??=
            WindowsCodexAppProbe.RequireAudioPolicyProcess;
        uint audioPolicyProcessId =
            audioPolicyProcessProvider(target);
        CodexAppAudioRouteProbeItem[] routes =
            PerAppAudioRouteKey.All.Select(key =>
            {
                string expected = key.Flow
                    == PerAppAudioDataFlow.Render
                    ? config.VirtualSpeakerRenderEndpointId
                    : config.VirtualMicrophoneCaptureEndpointId;
                PersistedAudioEndpoint current;
                try
                {
                    current = backend.Read(
                        audioPolicyProcessId,
                        key);
                }
                catch (Exception exception)
                {
                    current = PersistedAudioEndpoint.Error(
                        Marshal.GetHRForException(exception),
                        "audio-policy-probe-read");
                }
                bool match =
                    current.Kind
                        == PersistedAudioEndpointKind.Present
                    && string.Equals(
                        current.EndpointId,
                        expected,
                        StringComparison.OrdinalIgnoreCase);
                return new CodexAppAudioRouteProbeItem(
                    key.FlowName,
                    key.RoleName,
                    expected,
                    current.Kind.ToString(),
                    current.Kind
                        == PersistedAudioEndpointKind.Present
                        ? current.EndpointId
                        : null,
                    match,
                    current.Kind
                        == PersistedAudioEndpointKind.Error
                        ? current.HResult
                        : null,
                    current.Kind
                        == PersistedAudioEndpointKind.Error
                        ? current.Stage
                        : null);
            }).ToArray();

        return new
        {
            contract = Contract,
            ok = true,
            processId = audioPolicyProcessId,
            rootProcessId = target.RootProcessId,
            automationConfigured = true,
            allMatch = routes.All(item => item.Match),
            routes,
            audioRouteMutated = false,
            captureStarted = false,
            shortcutSent = false,
            appLaunched = false,
        };
    }
}
