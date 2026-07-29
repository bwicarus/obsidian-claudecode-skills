using System.Text;
using System.Text.Json;

namespace BwReader.ComputerVoiceAudio;

internal sealed record DirectBridgeConfig(
    string Path,
    bool LocalOptIn,
    string VirtualMicrophoneRenderEndpointId,
    string VirtualSpeakerRenderEndpointId,
    string ListenHost,
    int ListenPort,
    IReadOnlySet<string> AllowedOrigins,
    string AllowedTailscaleUserLogin,
    bool ExperimentalSingleUserMode,
    string OutputScope,
    string AppKind,
    string RuntimeStatusPath);

internal sealed class DirectBridgeConfigStore
{
    private const int MaximumConfigBytes = 64 * 1024;
    private readonly string _path;

    internal DirectBridgeConfigStore(string path)
    {
        if (!System.IO.Path.IsPathFullyQualified(path))
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_CONFIG_PATH_INVALID",
                "直连配置必须使用绝对路径");
        }
        _path = System.IO.Path.GetFullPath(path);
    }

    internal string Path => _path;

    internal string InstallationRoot
    {
        get
        {
            string? configDirectory =
                System.IO.Path.GetDirectoryName(_path);
            DirectoryInfo? parent = configDirectory is null
                ? null
                : Directory.GetParent(configDirectory);
            return parent?.FullName
                ?? throw ConfigInvalid();
        }
    }

    internal DirectBridgeConfig Load()
    {
        FileInfo info = new(_path);
        if (!info.Exists || info.Length is <= 0 or > MaximumConfigBytes)
        {
            throw ConfigInvalid();
        }

        try
        {
            using JsonDocument document = JsonDocument.Parse(
                File.ReadAllText(_path, Encoding.UTF8),
                new JsonDocumentOptions
                {
                    AllowTrailingCommas = false,
                    CommentHandling = JsonCommentHandling.Disallow,
                    MaxDepth = 8,
                });
            JsonElement root = document.RootElement;
            RequireExactKeys(
                root,
                "contract",
                "localOptIn",
                "virtualMicrophoneRenderEndpointId",
                "virtualSpeakerRenderEndpointId",
                "listenHost",
                "listenPort",
                "allowedOrigins",
                "allowedTailscaleUserLogin",
                "experimentalSingleUserMode",
                "outputScope",
                "appKind",
                "runtimeStatusPath");

            if (RequireString(root, "contract", 128)
                    != DirectBridgeContract.ConfigContract)
            {
                throw ConfigInvalid();
            }
            bool localOptIn = RequireBoolean(root, "localOptIn");
            string virtualMicrophoneRenderEndpointId = RequireString(
                root,
                "virtualMicrophoneRenderEndpointId",
                VirtualMicrophoneRenderRequest.MaximumEndpointIdLength);
            string virtualSpeakerRenderEndpointId = RequireString(
                root,
                "virtualSpeakerRenderEndpointId",
                VirtualMicrophoneRenderRequest.MaximumEndpointIdLength);
            if (
                virtualMicrophoneRenderEndpointId.Any(char.IsControl)
                || virtualSpeakerRenderEndpointId.Any(char.IsControl)
                || string.IsNullOrWhiteSpace(
                    virtualMicrophoneRenderEndpointId)
                || string.IsNullOrWhiteSpace(
                    virtualSpeakerRenderEndpointId)
                || string.Equals(
                    virtualMicrophoneRenderEndpointId,
                    virtualSpeakerRenderEndpointId,
                    StringComparison.Ordinal)
            )
            {
                throw ConfigInvalid();
            }

            string listenHost = RequireString(root, "listenHost", 64);
            int listenPort = RequireInt32(root, "listenPort");
            if (
                listenHost != DirectBridgeContract.ListenHost
                || listenPort != DirectBridgeContract.DefaultListenPort
            )
            {
                throw ConfigInvalid();
            }

            HashSet<string> allowedOrigins = ParseOrigins(
                root.GetProperty("allowedOrigins"));
            string allowedTailscaleUserLogin = RequireString(
                root,
                "allowedTailscaleUserLogin",
                320);
            bool experimentalSingleUserMode = RequireBoolean(
                root,
                "experimentalSingleUserMode");
            if (
                !experimentalSingleUserMode
                ||
                !string.Equals(
                    allowedTailscaleUserLogin,
                    "bwicarus@gmail.com",
                    StringComparison.OrdinalIgnoreCase)
                || allowedTailscaleUserLogin.Any(character =>
                    character < '!' || character > '~')
            )
            {
                throw ConfigInvalid();
            }
            string outputScope = RequireString(root, "outputScope", 32);
            string appKind = RequireString(root, "appKind", 32);
            if (
                outputScope != AudioBridgeContract.CaptureScope
                || appKind != "codex-desktop"
            )
            {
                throw ConfigInvalid();
            }

            string runtimeStatusPath = RequireString(
                root,
                "runtimeStatusPath",
                4096);
            string expectedRuntimeStatusPath = System.IO.Path.Combine(
                InstallationRoot,
                "runtime",
                "computer-voice-direct.status.json");
            if (
                !System.IO.Path.IsPathFullyQualified(runtimeStatusPath)
                || !string.Equals(
                    System.IO.Path.GetExtension(runtimeStatusPath),
                    ".json",
                    StringComparison.OrdinalIgnoreCase)
                || string.Equals(
                    System.IO.Path.GetFullPath(runtimeStatusPath),
                    _path,
                    StringComparison.OrdinalIgnoreCase)
                || !string.Equals(
                    System.IO.Path.GetFullPath(runtimeStatusPath),
                    System.IO.Path.GetFullPath(
                        expectedRuntimeStatusPath),
                    StringComparison.OrdinalIgnoreCase)
            )
            {
                throw ConfigInvalid();
            }

            return new DirectBridgeConfig(
                _path,
                localOptIn,
                virtualMicrophoneRenderEndpointId,
                virtualSpeakerRenderEndpointId,
                listenHost,
                listenPort,
                allowedOrigins,
                allowedTailscaleUserLogin,
                experimentalSingleUserMode,
                outputScope,
                appKind,
                System.IO.Path.GetFullPath(runtimeStatusPath));
        }
        catch (DirectProtocolException)
        {
            throw;
        }
        catch (
            Exception exception
        ) when (
            exception is IOException
            or UnauthorizedAccessException
            or JsonException
            or ArgumentException
            or FormatException
        )
        {
            throw ConfigInvalid(exception);
        }
    }

    private static HashSet<string> ParseOrigins(JsonElement value)
    {
        if (value.ValueKind != JsonValueKind.Array)
        {
            throw ConfigInvalid();
        }
        string[] origins = value.EnumerateArray()
            .Select(item =>
            {
                if (
                    item.ValueKind != JsonValueKind.String
                    || item.GetString() is not string origin
                    || origin.Length is < 1 or > 512
                )
                {
                    throw ConfigInvalid();
                }
                return origin;
            })
            .ToArray();
        if (origins.Length is < 1 or > 8)
        {
            throw ConfigInvalid();
        }

        HashSet<string> result = new(StringComparer.Ordinal);
        foreach (string origin in origins)
        {
            if (
                !Uri.TryCreate(origin, UriKind.Absolute, out Uri? uri)
                || uri.Scheme != Uri.UriSchemeHttps
                || uri.UserInfo.Length != 0
                || uri.AbsolutePath != "/"
                || uri.Query.Length != 0
                || uri.Fragment.Length != 0
                || !string.Equals(
                    uri.GetLeftPart(UriPartial.Authority),
                    origin,
                    StringComparison.Ordinal)
                || !result.Add(origin)
            )
            {
                throw ConfigInvalid();
            }
        }
        return result;
    }

    private static string RequireString(
        JsonElement root,
        string name,
        int maximumLength,
        bool allowEmpty = false)
    {
        if (
            !root.TryGetProperty(name, out JsonElement value)
            || value.ValueKind != JsonValueKind.String
            || value.GetString() is not string result
            || result.Length > maximumLength
            || (!allowEmpty && result.Length == 0)
        )
        {
            throw ConfigInvalid();
        }
        return result;
    }

    private static bool RequireBoolean(JsonElement root, string name)
    {
        if (
            !root.TryGetProperty(name, out JsonElement value)
            || value.ValueKind is not (
                JsonValueKind.True or JsonValueKind.False)
        )
        {
            throw ConfigInvalid();
        }
        return value.GetBoolean();
    }

    private static int RequireInt32(JsonElement root, string name)
    {
        if (
            !root.TryGetProperty(name, out JsonElement value)
            || value.ValueKind != JsonValueKind.Number
            || !value.TryGetInt32(out int result)
        )
        {
            throw ConfigInvalid();
        }
        return result;
    }

    private static void RequireExactKeys(
        JsonElement value,
        params string[] expected)
    {
        if (value.ValueKind != JsonValueKind.Object)
        {
            throw ConfigInvalid();
        }
        HashSet<string> actual = value.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        if (!actual.SetEquals(expected))
        {
            throw ConfigInvalid();
        }
    }

    private static DirectProtocolException ConfigInvalid(
        Exception? inner = null) =>
        inner is null
            ? new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_CONFIG_INVALID",
                "直连配置无效")
            : new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_CONFIG_INVALID",
                "直连配置无效",
                retryable: false);
}
