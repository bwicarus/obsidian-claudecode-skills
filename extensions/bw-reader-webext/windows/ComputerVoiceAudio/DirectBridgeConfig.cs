using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace BwReader.ComputerVoiceAudio;

internal sealed record DirectBridgeConfig(
    string Path,
    bool LocalOptIn,
    string VirtualMicrophoneRenderEndpointId,
    string VirtualMicrophoneCaptureEndpointId,
    string VirtualSpeakerRenderEndpointId,
    string ListenHost,
    int ListenPort,
    IReadOnlySet<string> AllowedOrigins,
    string AllowedTailscaleUserLogin,
    bool ExperimentalSingleUserMode,
    string OutputScope,
    string AppKind,
    string RuntimeStatusPath,
    string ContextDeliveryMode,
    bool PerAppAudioRouteAutomationEnabled = false,
    string VirtualSpeakerCaptureEndpointId = "",
    bool FixedVirtualAudioBusEnabled = false);

internal sealed class DirectBridgeConfigStore
{
    private const int MaximumConfigBytes = 64 * 1024;
    private readonly string _path;
    private readonly object _writeGate = new();

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
            string contract = RequireString(root, "contract", 128);
            bool legacyV4 =
                contract == DirectBridgeContract.LegacyConfigContract;
            bool fixedAudioBus =
                contract
                == DirectBridgeContract.FixedAudioBusConfigContract;
            if (
                !legacyV4
                && contract != DirectBridgeContract.ConfigContract
                && !fixedAudioBus
            )
            {
                throw ConfigInvalid();
            }
            if (legacyV4)
            {
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
                    "runtimeStatusPath",
                    "contextDeliveryMode");
            }
            else if (!fixedAudioBus)
            {
                RequireExactKeys(
                    root,
                    "contract",
                    "localOptIn",
                    "virtualMicrophoneRenderEndpointId",
                    "virtualMicrophoneCaptureEndpointId",
                    "virtualSpeakerRenderEndpointId",
                    "listenHost",
                    "listenPort",
                    "allowedOrigins",
                    "allowedTailscaleUserLogin",
                    "experimentalSingleUserMode",
                    "outputScope",
                    "appKind",
                    "runtimeStatusPath",
                    "contextDeliveryMode");
            }
            else
            {
                RequireExactKeys(
                    root,
                    "contract",
                    "localOptIn",
                    "virtualMicrophoneRenderEndpointId",
                    "virtualMicrophoneCaptureEndpointId",
                    "virtualSpeakerRenderEndpointId",
                    "virtualSpeakerCaptureEndpointId",
                    "listenHost",
                    "listenPort",
                    "allowedOrigins",
                    "allowedTailscaleUserLogin",
                    "experimentalSingleUserMode",
                    "outputScope",
                    "appKind",
                    "runtimeStatusPath",
                    "contextDeliveryMode");
            }
            bool localOptIn = RequireBoolean(root, "localOptIn");
            string virtualMicrophoneRenderEndpointId = RequireString(
                root,
                "virtualMicrophoneRenderEndpointId",
                VirtualMicrophoneRenderRequest.MaximumEndpointIdLength);
            string virtualMicrophoneCaptureEndpointId = legacyV4
                ? ""
                : RequireString(
                    root,
                    "virtualMicrophoneCaptureEndpointId",
                    VirtualMicrophoneRenderRequest.MaximumEndpointIdLength);
            string virtualSpeakerRenderEndpointId = RequireString(
                root,
                "virtualSpeakerRenderEndpointId",
                VirtualMicrophoneRenderRequest.MaximumEndpointIdLength);
            string virtualSpeakerCaptureEndpointId = fixedAudioBus
                ? RequireString(
                    root,
                    "virtualSpeakerCaptureEndpointId",
                    VirtualMicrophoneRenderRequest.MaximumEndpointIdLength)
                : "";
            if (
                virtualMicrophoneRenderEndpointId.Any(char.IsControl)
                || (
                    !legacyV4
                    && virtualMicrophoneCaptureEndpointId.Any(
                        char.IsControl)
                )
                || virtualSpeakerRenderEndpointId.Any(char.IsControl)
                || (
                    fixedAudioBus
                    && virtualSpeakerCaptureEndpointId.Any(char.IsControl)
                )
                || string.IsNullOrWhiteSpace(
                    virtualMicrophoneRenderEndpointId)
                || (
                    !legacyV4
                    && string.IsNullOrWhiteSpace(
                        virtualMicrophoneCaptureEndpointId)
                )
                || string.IsNullOrWhiteSpace(
                    virtualSpeakerRenderEndpointId)
                || (
                    fixedAudioBus
                    && string.IsNullOrWhiteSpace(
                        virtualSpeakerCaptureEndpointId)
                )
                || string.Equals(
                    virtualMicrophoneRenderEndpointId,
                    virtualSpeakerRenderEndpointId,
                    StringComparison.OrdinalIgnoreCase)
            )
            {
                throw ConfigInvalid();
            }
            if (!legacyV4)
            {
                try
                {
                    AudioPolicyEndpointId.ValidateForFlow(
                        virtualMicrophoneRenderEndpointId,
                        PerAppAudioDataFlow.Render);
                    AudioPolicyEndpointId.ValidateForFlow(
                        virtualSpeakerRenderEndpointId,
                        PerAppAudioDataFlow.Render);
                    AudioPolicyEndpointId.ValidateForFlow(
                        virtualMicrophoneCaptureEndpointId,
                        PerAppAudioDataFlow.Capture);
                    if (fixedAudioBus)
                    {
                        AudioPolicyEndpointId.ValidateForFlow(
                            virtualSpeakerCaptureEndpointId,
                            PerAppAudioDataFlow.Capture);
                        if (string.Equals(
                            virtualMicrophoneCaptureEndpointId,
                            virtualSpeakerCaptureEndpointId,
                            StringComparison.OrdinalIgnoreCase))
                        {
                            throw ConfigInvalid();
                        }
                    }
                }
                catch (DirectProtocolException exception)
                {
                    throw ConfigInvalid(exception);
                }
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
                || !DirectAppTargets.IsSupported(appKind)
            )
            {
                throw ConfigInvalid();
            }
            string contextDeliveryMode = RequireString(
                root,
                "contextDeliveryMode",
                32);
            if (!DirectContextDeliveryMode.IsSupported(
                contextDeliveryMode))
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
                virtualMicrophoneCaptureEndpointId,
                virtualSpeakerRenderEndpointId,
                listenHost,
                listenPort,
                allowedOrigins,
                allowedTailscaleUserLogin,
                experimentalSingleUserMode,
                outputScope,
                appKind,
                System.IO.Path.GetFullPath(runtimeStatusPath),
                contextDeliveryMode,
                PerAppAudioRouteAutomationEnabled:
                    !legacyV4 && !fixedAudioBus,
                VirtualSpeakerCaptureEndpointId:
                    virtualSpeakerCaptureEndpointId,
                FixedVirtualAudioBusEnabled: fixedAudioBus);
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

    internal string SetContextDeliveryMode(string mode)
    {
        if (!DirectContextDeliveryMode.IsSupported(mode))
        {
            throw new DirectProtocolException(
                "BW_READER_CONTEXT_DELIVERY_MODE_INVALID",
                "Reader 上下文交付模式无效");
        }

        lock (_writeGate)
        {
            DirectBridgeConfig current = Load();
            string previousMode = current.ContextDeliveryMode;
            if (string.Equals(
                previousMode,
                mode,
                StringComparison.Ordinal))
            {
                return previousMode;
            }

            string temporaryPath = _path
                + ".tmp-"
                + Guid.NewGuid().ToString("N");
            try
            {
                JsonNode? parsed = JsonNode.Parse(
                    File.ReadAllText(_path, Encoding.UTF8),
                    nodeOptions: null,
                    documentOptions: new JsonDocumentOptions
                    {
                        AllowTrailingCommas = false,
                        CommentHandling = JsonCommentHandling.Disallow,
                        MaxDepth = 8,
                    });
                if (parsed is not JsonObject root)
                {
                    throw ConfigInvalid();
                }
                root["contextDeliveryMode"] = mode;
                string json = root.ToJsonString(
                    new JsonSerializerOptions(
                        DirectBridgeContract.JsonOptions)
                    {
                        WriteIndented = true,
                    });
                byte[] bytes = new UTF8Encoding(
                    encoderShouldEmitUTF8Identifier: false)
                    .GetBytes(json);
                if (bytes.Length is <= 0 or > MaximumConfigBytes)
                {
                    throw ConfigInvalid();
                }

                using (FileStream stream = new(
                    temporaryPath,
                    FileMode.CreateNew,
                    FileAccess.Write,
                    FileShare.None,
                    bufferSize: 4096,
                    FileOptions.WriteThrough))
                {
                    stream.Write(bytes);
                    stream.Flush(flushToDisk: true);
                }

                DirectBridgeConfig staged =
                    new DirectBridgeConfigStore(temporaryPath).Load();
                if (!string.Equals(
                    staged.ContextDeliveryMode,
                    mode,
                    StringComparison.Ordinal))
                {
                    throw ConfigInvalid();
                }

                File.Replace(
                    temporaryPath,
                    _path,
                    destinationBackupFileName: null,
                    ignoreMetadataErrors: true);
                DirectBridgeConfig persisted = Load();
                if (!string.Equals(
                    persisted.ContextDeliveryMode,
                    mode,
                    StringComparison.Ordinal))
                {
                    throw ConfigInvalid();
                }
                return previousMode;
            }
            catch (DirectProtocolException)
            {
                throw;
            }
            catch (Exception exception) when (
                exception is IOException
                or UnauthorizedAccessException
                or JsonException
                or ArgumentException
                or NotSupportedException)
            {
                throw new DirectProtocolException(
                    "BW_READER_CONTEXT_DELIVERY_MODE_WRITE_FAILED",
                    "Windows 上下文交付模式写入失败",
                    retryable: true,
                    innerException: exception);
            }
            finally
            {
                try
                {
                    File.Delete(temporaryPath);
                }
                catch
                {
                    // A stale same-directory temporary file is harmless and
                    // must not turn a successful atomic replacement into a
                    // failed mode switch.
                }
            }
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
