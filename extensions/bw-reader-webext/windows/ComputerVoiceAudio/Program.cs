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
                "Chrome Native Messaging origin (registered host only)",
            },
        }, JsonOptions));
        return 64;
    }
}
