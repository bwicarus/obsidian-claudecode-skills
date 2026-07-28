using System.Text.Json;

namespace BwReader.ComputerVoiceAudio;

internal sealed record NativeHostConfig(
    bool LocalOptIn,
    string MicrophoneEndpointId,
    string AllowedExtensionId,
    string TypistHelper,
    string VoiceStartShortcut)
{
    internal const string Contract =
        "reader-computer-voice-native-host-config/1";

    internal static NativeHostConfig Load(string executableDirectory)
    {
        string path = Path.Combine(
            executableDirectory,
            "computer-voice-native.config.json");
        using JsonDocument document = JsonDocument.Parse(
            File.ReadAllText(path));
        JsonElement root = document.RootElement;
        HashSet<string> keys = root.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        HashSet<string> expected = new(StringComparer.Ordinal)
        {
            "contract",
            "localOptIn",
            "microphoneEndpointId",
            "allowedExtensionId",
            "typistHelper",
            "voiceStartShortcut",
            "outputScope",
            "appKind",
        };
        if (!keys.SetEquals(expected)
            || root.GetProperty("contract").GetString() != Contract
            || root.GetProperty("outputScope").GetString() != "process-only"
            || root.GetProperty("appKind").GetString() != "codex-desktop")
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_NATIVE_CONFIG_INVALID");
        }

        bool localOptIn = root.GetProperty("localOptIn").GetBoolean();
        string microphone = root.GetProperty("microphoneEndpointId")
            .GetString() ?? "";
        string extensionId = root.GetProperty("allowedExtensionId")
            .GetString() ?? "";
        string typistHelper = root.GetProperty("typistHelper")
            .GetString() ?? "";
        string shortcut = root.GetProperty("voiceStartShortcut")
            .GetString() ?? "";
        if (
            (microphone.Length != 0
                && (
                    microphone.Length
                        > MicCaptureRequest.MaximumEndpointIdLength
                    || microphone.Any(char.IsControl)
                ))
            || (
                extensionId.Length != 0
                && !extensionId.All(character =>
                    character is >= 'a' and <= 'p')
            )
            || extensionId.Length is not (0 or 32)
            || (
                typistHelper.Length != 0
                && (
                    !Path.IsPathFullyQualified(typistHelper)
                    || !string.Equals(
                        Path.GetExtension(typistHelper),
                        ".py",
                        StringComparison.OrdinalIgnoreCase)
                )
            )
            || shortcut != "Ctrl+Shift+C"
        )
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_NATIVE_CONFIG_INVALID");
        }
        if (localOptIn
            && (
                microphone.Length == 0
                || extensionId.Length != 32
                || typistHelper.Length == 0
            ))
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_NATIVE_CONFIG_INCOMPLETE");
        }

        return new NativeHostConfig(
            localOptIn,
            microphone,
            extensionId,
            typistHelper,
            shortcut);
    }

    internal void RequireOrigin(string origin)
    {
        string expected = $"chrome-extension://{AllowedExtensionId}/";
        if (AllowedExtensionId.Length != 32
            || !string.Equals(origin, expected, StringComparison.Ordinal))
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_NATIVE_ORIGIN_DENIED");
        }
    }
}
