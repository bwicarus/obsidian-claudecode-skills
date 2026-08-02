namespace BwReader.ComputerVoiceAudio;

internal static class AudioBridgeContract
{
    internal const string Contract = "reader-computer-voice-audio/1";
    internal const string CaptureScope = "process-only";
    internal const string LoopbackMode = "include-target-process-tree";
    internal const string VirtualAudioDevice = "VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK";
    internal const int MinimumWindowsBuild = 20348;

    internal static object Describe() => new
    {
        contract = Contract,
        targetFramework = "net8.0",
        captureScope = CaptureScope,
        loopbackMode = LoopbackMode,
        activationDevice = VirtualAudioDevice,
        legacyNativeMicrophoneSelection =
            "explicit-capture-endpoint-id-only",
        directMicrophoneUplink =
            "browser-pcm-to-explicit-virtual-render-endpoint",
        directSpeakerReadiness =
            "explicit-render-endpoint-plus-core-audio-session-evidence",
        defaultMicrophoneFallback = false,
        automaticMicrophoneCapture = false,
        minimumWindowsBuild = MinimumWindowsBuild,
        systemOutputFallback = false,
        captureState =
            "direct-v3-start-gated;legacy-native-messaging-retained",
        captureThreadModel = "dedicated-mta-single-thread",
        captureCliExposed = false,
        automaticCapture = false,
        sinkPolicy = "bounded-fail-closed",
        requiresExplicitStart = true,
        safeCommands = new[] {
            "--describe",
            "--self-test",
            "--list-direct-microphones",
            "--list-direct-render-endpoints",
            "--probe-codex-app-audio-route --config <absolute-path>",
            "--probe-direct-output-route --config <absolute-path>",
            "--diagnose-direct-audio-no-start --config <absolute-path>",
        },
        nativeMessagingOriginAllowlist = true,
        localOptInRequired = true,
        directServer = new
        {
            command = "--direct-serve --config <absolute-path>",
            bind = "127.0.0.1-only",
            defaultPort = DirectBridgeContract.DefaultListenPort,
            transport = "tailnet-wss-fixed-pcm-and-context",
            automaticCapture = false,
            appLaunch = new
            {
                selectionOrStatus = false,
                authenticatedStart = true,
                target = DirectBridgeContract.CodexAppUserModelId,
            },
            productionAdapterState = "wired-authenticated-start-gated",
        },
    };
}
