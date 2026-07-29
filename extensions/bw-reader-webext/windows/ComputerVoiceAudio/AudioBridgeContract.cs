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
        microphoneSelection = "explicit-endpoint-id-only",
        defaultMicrophoneFallback = false,
        automaticMicrophoneCapture = false,
        minimumWindowsBuild = MinimumWindowsBuild,
        systemOutputFallback = false,
        captureState =
            "native-messaging-gated;direct-authenticated-start-gated",
        captureThreadModel = "dedicated-mta-single-thread",
        captureCliExposed = false,
        automaticCapture = false,
        sinkPolicy = "bounded-fail-closed",
        requiresExplicitStart = true,
        safeCommands = new[] { "--describe", "--self-test" },
        nativeMessagingOriginAllowlist = true,
        localOptInRequired = true,
        directServer = new
        {
            command = "--direct-serve --config <absolute-path>",
            bind = "127.0.0.1-only",
            defaultPort = DirectBridgeContract.DefaultListenPort,
            transport = "authenticated-websocket-fixed-pcm",
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
