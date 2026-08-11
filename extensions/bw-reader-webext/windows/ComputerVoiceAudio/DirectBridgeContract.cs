using System.Text.Json;

namespace BwReader.ComputerVoiceAudio;

internal static class DirectBridgeContract
{
    internal const string Contract = "reader-computer-voice-direct/1";
    internal const string ConfigContract =
        "reader-computer-voice-direct-config/5";
    internal const string FixedAudioBusConfigContract =
        "reader-computer-voice-direct-config/6";
    internal const string LegacyConfigContract =
        "reader-computer-voice-direct-config/4";
    internal const string RuntimeStatusContract =
        "reader-computer-voice-direct-runtime-status/2";
    internal const string ListenHost = "127.0.0.1";
    internal const int DefaultListenPort = 43128;
    internal const int MaximumMessageBytes = 64 * 1024;
    internal const int PcmFrameHeaderBytes = 36;
    internal const int PcmPayloadBytes =
        Pcm48kMonoFramer.BytesPerChunk;
    internal const int PcmFrameBytes =
        PcmFrameHeaderBytes + PcmPayloadBytes;
    internal const int PcmQueueLimitMilliseconds = 400;
    internal const int UplinkPcmQueueLimitMilliseconds =
        BoundedUplinkPcmQueue.MaximumBufferedMilliseconds;
    internal const int AuthenticationTimeoutMilliseconds = 10_000;
    internal const int StartTimeoutMilliseconds = 30_000;
    internal const int ClientHeartbeatIntervalMilliseconds = 5_000;
    internal const int ClientHeartbeatTimeoutMilliseconds = 15_000;
    internal const string CodexAppUserModelId =
        "OpenAI.CodexBeta_2p2nqsd0c76g0!App";
    internal const string ChatGptClassicAppUserModelId =
        "OpenAI.ChatGPT-Desktop_2p2nqsd0c76g0!ChatGPT";
    internal static readonly TimeSpan RuntimeStatusHeartbeatInterval =
        TimeSpan.FromSeconds(5);

    internal static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        WriteIndented = false,
    };

    internal static bool IsSafeId(string value) =>
        value.Length is >= 1 and <= 160
        && value.All(character =>
            character is >= 'A' and <= 'Z'
            or >= 'a' and <= 'z'
            or >= '0' and <= '9'
            or '.' or '_' or ':' or '-');

    internal static bool IsServiceInstanceId(string value) =>
        value.Length == 32
        && value.All(character =>
            character is >= '0' and <= '9'
            or >= 'a' and <= 'f');
}

internal static class DirectContextDeliveryMode
{
    internal const string LegacyInject = "legacy-inject";
    internal const string SnapshotMcp = "snapshot-mcp";

    internal static bool IsSupported(string value) =>
        value is LegacyInject or SnapshotMcp;
}

internal sealed class DirectProtocolException : Exception
{
    internal DirectProtocolException(
        string code,
        string message,
        bool retryable = false,
        Exception? innerException = null)
        : base(message, innerException)
    {
        Code = code;
        Retryable = retryable;
    }

    internal string Code { get; }

    internal bool Retryable { get; }
}

internal static class DirectBase64Url
{
    internal static string Encode(ReadOnlySpan<byte> value) =>
        Convert.ToBase64String(value)
            .TrimEnd('=')
            .Replace('+', '-')
            .Replace('/', '_');

    internal static byte[] Decode(
        string value,
        int maximumBytes,
        string errorCode)
    {
        if (
            string.IsNullOrEmpty(value)
            || value.Length > ((maximumBytes + 2) / 3) * 4
            || value.Any(character =>
                !(character is >= 'A' and <= 'Z')
                && !(character is >= 'a' and <= 'z')
                && !(character is >= '0' and <= '9')
                && character is not '-' and not '_')
        )
        {
            throw new DirectProtocolException(
                errorCode,
                "base64url 字段无效");
        }

        string padded = value.Replace('-', '+').Replace('_', '/');
        padded += (padded.Length % 4) switch
        {
            0 => "",
            2 => "==",
            3 => "=",
            _ => throw new DirectProtocolException(
                errorCode,
                "base64url 字段无效"),
        };
        try
        {
            byte[] decoded = Convert.FromBase64String(padded);
            if (decoded.Length > maximumBytes
                || !string.Equals(
                    Encode(decoded),
                    value,
                    StringComparison.Ordinal))
            {
                throw new DirectProtocolException(
                    errorCode,
                    "base64url 字段无效");
            }
            return decoded;
        }
        catch (FormatException exception)
        {
            throw new DirectProtocolException(
                errorCode,
                "base64url 字段无效",
                retryable: false,
                innerException: exception);
        }
    }
}
