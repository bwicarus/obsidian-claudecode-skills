using System.Text.Json;

namespace BwReader.ComputerVoiceAudio;

internal static class Program
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        WriteIndented = true,
    };

    public static async Task<int> Main(string[] args)
    {
        try
        {
            if (args is ["--describe"])
            {
                return WriteJson(AudioBridgeContract.Describe());
            }
            if (args is ["--self-test"])
            {
                return WriteJson(ContractSelfTest.Run());
            }
            if (args is ["--list-direct-microphones"])
            {
                return WriteJson(new
                {
                    contract =
                        "reader-computer-voice-microphones/1",
                    ok = true,
                    captureStarted = false,
                    devices =
                        DirectMicrophoneDiscovery.EnumerateActive(),
                });
            }
            if (args is ["--list-direct-render-endpoints"])
            {
                return WriteJson(new
                {
                    contract =
                        "reader-computer-voice-render-endpoints/1",
                    ok = true,
                    captureStarted = false,
                    devices = DirectMicrophoneDiscovery
                        .EnumerateActiveRenderEndpoints(),
                });
            }
            if (
                args.Length == 3
                && args[0] == "--probe-direct-output-route"
                && args[1] == "--config"
            )
            {
                if (!Path.IsPathFullyQualified(args[2]))
                {
                    throw new DirectProtocolException(
                        "BW_COMPUTER_VOICE_DIRECT_CONFIG_PATH_INVALID",
                        "直连配置必须使用绝对路径");
                }
                DirectBridgeConfigStore configStore = new(
                    Path.GetFullPath(args[2]));
                return WriteJson(DirectOutputRouteProbe.Run(
                    configStore.Load()));
            }
            if (
                args.Length == 3
                && args[0] == "--diagnose-direct-audio-no-start"
                && args[1] == "--config"
            )
            {
                if (!Path.IsPathFullyQualified(args[2]))
                {
                    throw new DirectProtocolException(
                        "BW_COMPUTER_VOICE_DIRECT_CONFIG_PATH_INVALID",
                        "直连配置必须使用绝对路径");
                }
                DirectBridgeConfigStore configStore = new(
                    Path.GetFullPath(args[2]));
                return WriteJson(await DirectAudioDiagnostics.RunAsync(
                    configStore.Load(),
                    CancellationToken.None).ConfigureAwait(false));
            }
            if (
                args.Length == 3
                && args[0] == "--direct-serve"
                && args[1] == "--config"
            )
            {
                if (!Path.IsPathFullyQualified(args[2]))
                {
                    throw new DirectProtocolException(
                        "BW_COMPUTER_VOICE_DIRECT_CONFIG_PATH_INVALID",
                        "直连配置必须使用绝对路径");
                }
                string configPath = Path.GetFullPath(args[2]);
                DirectBridgeConfigStore configStore = new(configPath);
                _ = configStore.Load();
                await using DirectBridgeServer server = new(
                    configStore,
                    new WindowsDirectAppLauncher(),
                    new WindowsDirectMediaAdapter(
                        configStore.InstallationRoot),
                    new NamedPipeDirectContextAdapter(),
                    new FileDirectSnapshotContextAdapter(
                        Path.Combine(
                            configStore.InstallationRoot,
                            "runtime",
                            FileDirectSnapshotContextAdapter
                                .SnapshotFileName)));
                return await server.RunAsync(CancellationToken.None)
                    .ConfigureAwait(false);
            }
            if (
                args.Length == 3
                && args[0] == "--reader-context-mcp"
                && args[1] == "--state"
            )
            {
                if (!Path.IsPathFullyQualified(args[2]))
                {
                    throw new DirectProtocolException(
                        "BW_READER_CONTEXT_SNAPSHOT_PATH_INVALID",
                        "Reader 本地快照必须使用绝对路径");
                }
                Console.InputEncoding = new System.Text.UTF8Encoding(
                    encoderShouldEmitUTF8Identifier: false);
                Console.OutputEncoding = new System.Text.UTF8Encoding(
                    encoderShouldEmitUTF8Identifier: false);
                ReaderContextMcpServer server = new(
                    Path.GetFullPath(args[2]),
                    Console.In,
                    Console.Out);
                return await server.RunAsync(CancellationToken.None)
                    .ConfigureAwait(false);
            }
            if (IsNativeMessagingInvocation(args))
            {
                NativeHostConfig config = NativeHostConfig.Load(
                    AppContext.BaseDirectory);
                config.RequireOrigin(args[0]);
                await using NativeMessagingHost host = new(
                    Console.OpenStandardInput(),
                    Console.OpenStandardOutput(),
                    config);
                return await host.RunAsync(CancellationToken.None)
                    .ConfigureAwait(false);
            }
            return RejectUnknownCommand();
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine(JsonSerializer.Serialize(new
            {
                contract = AudioBridgeContract.Contract,
                ok = false,
                error = "BW_COMPUTER_VOICE_AUDIO_SELF_TEST_FAILED",
                detail = exception.Message,
            }, JsonOptions));
            return 1;
        }
    }

    private static bool IsNativeMessagingInvocation(string[] args)
    {
        if (
            args.Length is < 1 or > 2
            || !args[0].StartsWith(
                "chrome-extension://",
                StringComparison.Ordinal)
            || !args[0].EndsWith("/", StringComparison.Ordinal)
        )
        {
            return false;
        }
        return args.Length == 1
            || (
                args[1].StartsWith(
                    "--parent-window=",
                    StringComparison.Ordinal)
                && uint.TryParse(
                    args[1]["--parent-window=".Length..],
                    out _)
            );
    }

    private static int WriteJson(object value)
    {
        Console.WriteLine(JsonSerializer.Serialize(value, JsonOptions));
        return 0;
    }

    private static int RejectUnknownCommand()
    {
        Console.Error.WriteLine(JsonSerializer.Serialize(new
        {
            contract = AudioBridgeContract.Contract,
            ok = false,
            error = "BW_COMPUTER_VOICE_AUDIO_COMMAND_NOT_ALLOWED",
            allowed = new[] {
                "--describe",
                "--self-test",
                "--list-direct-microphones",
                "--list-direct-render-endpoints",
                "--probe-direct-output-route --config <absolute-path>",
                "--diagnose-direct-audio-no-start --config <absolute-path>",
                "--direct-serve --config <absolute-path>",
                "--reader-context-mcp --state <absolute-path>",
                "Chrome Native Messaging origin (registered host only)",
            },
        }, JsonOptions));
        return 64;
    }
}
