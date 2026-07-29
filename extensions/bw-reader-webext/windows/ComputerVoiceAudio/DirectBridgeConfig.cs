using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace BwReader.ComputerVoiceAudio;

internal sealed record DirectBridgeConfig(
    string Path,
    bool LocalOptIn,
    string MicrophoneEndpointId,
    string ListenHost,
    int ListenPort,
    IReadOnlySet<string> AllowedOrigins,
    string AllowedTailscaleUserLogin,
    bool ExperimentalSingleUserMode,
    string PairingCodeHash,
    DateTimeOffset? PairingExpiresAtUtc,
    string PairedClientPublicKeySpki,
    string PairedClientFingerprintSha256,
    string OutputScope,
    string AppKind,
    string RuntimeStatusPath)
{
    internal bool HasPairingCode =>
        PairingCodeHash.Length != 0 && PairingExpiresAtUtc.HasValue;

    internal bool HasPairedClient =>
        PairedClientPublicKeySpki.Length != 0
        && PairedClientFingerprintSha256.Length != 0;
}

internal sealed class DirectBridgeConfigStore
{
    private const int MaximumConfigBytes = 64 * 1024;
    private readonly string _path;
    private readonly SemaphoreSlim _writeGate = new(1, 1);

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
            bool hasExperimentalSingleUserMode =
                root.TryGetProperty(
                    "experimentalSingleUserMode",
                    out _);
            if (hasExperimentalSingleUserMode)
            {
                RequireExactKeys(
                    root,
                    "contract",
                    "localOptIn",
                    "microphoneEndpointId",
                    "listenHost",
                    "listenPort",
                    "allowedOrigins",
                    "allowedTailscaleUserLogin",
                    "experimentalSingleUserMode",
                    "pairingCodeHash",
                    "pairingExpiresAtUtc",
                    "pairedClientPublicKeySpki",
                    "pairedClientFingerprintSha256",
                    "outputScope",
                    "appKind",
                    "runtimeStatusPath");
            }
            else
            {
                RequireExactKeys(
                    root,
                    "contract",
                    "localOptIn",
                    "microphoneEndpointId",
                    "listenHost",
                    "listenPort",
                    "allowedOrigins",
                    "allowedTailscaleUserLogin",
                    "pairingCodeHash",
                    "pairingExpiresAtUtc",
                    "pairedClientPublicKeySpki",
                    "pairedClientFingerprintSha256",
                    "outputScope",
                    "appKind",
                    "runtimeStatusPath");
            }

            if (RequireString(root, "contract", 128)
                    != DirectBridgeContract.ConfigContract)
            {
                throw ConfigInvalid();
            }
            bool localOptIn = RequireBoolean(root, "localOptIn");
            string microphoneEndpointId = RequireString(
                root,
                "microphoneEndpointId",
                MicCaptureRequest.MaximumEndpointIdLength,
                allowEmpty: true);
            if (
                microphoneEndpointId.Any(char.IsControl)
                || (localOptIn && string.IsNullOrWhiteSpace(
                    microphoneEndpointId))
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
            bool experimentalSingleUserMode =
                hasExperimentalSingleUserMode
                    ? RequireBoolean(
                        root,
                        "experimentalSingleUserMode")
                    : true;
            if (
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
            string pairingCodeHash = RequireString(
                root,
                "pairingCodeHash",
                64,
                allowEmpty: true);
            DateTimeOffset? pairingExpiresAtUtc = ParseNullableUtc(
                root.GetProperty("pairingExpiresAtUtc"));
            ValidatePairing(pairingCodeHash, pairingExpiresAtUtc);

            string clientSpki = RequireString(
                root,
                "pairedClientPublicKeySpki",
                512,
                allowEmpty: true);
            string clientFingerprint = RequireString(
                root,
                "pairedClientFingerprintSha256",
                64,
                allowEmpty: true);
            ValidatePairedClient(clientSpki, clientFingerprint);

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
                microphoneEndpointId,
                listenHost,
                listenPort,
                allowedOrigins,
                allowedTailscaleUserLogin,
                experimentalSingleUserMode,
                pairingCodeHash,
                pairingExpiresAtUtc,
                clientSpki,
                clientFingerprint,
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
            or CryptographicException
        )
        {
            throw ConfigInvalid(exception);
        }
    }

    internal async Task<DirectBridgeConfig> PairClientAsync(
        string pairingCode,
        string clientPublicKeySpki,
        DateTimeOffset nowUtc,
        CancellationToken cancellationToken)
    {
        if (!DirectBridgeContract.IsValidPairingCode(pairingCode))
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_PAIRING_CODE_INVALID",
                "配对码格式无效");
        }
        (string canonicalSpki, string fingerprint) =
            ValidateClientSpki(clientPublicKeySpki);

        await _writeGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        byte[]? expectedHash = null;
        byte[]? actualHash = null;
        try
        {
            DirectBridgeConfig current = Load();
            if (current.HasPairedClient)
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_ALREADY_PAIRED",
                    "电脑客户端已经配对");
            }
            if (
                !current.HasPairingCode
                || current.PairingExpiresAtUtc <= nowUtc
            )
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_PAIRING_EXPIRED",
                    "一次性配对码不存在或已过期");
            }

            expectedHash = DirectBase64Url.Decode(
                current.PairingCodeHash,
                SHA256.HashSizeInBytes,
                "BW_COMPUTER_VOICE_DIRECT_CONFIG_INVALID");
            actualHash = SHA256.HashData(
                Encoding.UTF8.GetBytes(pairingCode));
            if (!CryptographicOperations.FixedTimeEquals(
                expectedHash,
                actualHash))
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_PAIRING_DENIED",
                    "一次性配对码不匹配");
            }

            DirectBridgeConfig paired = current with
            {
                PairingCodeHash = "",
                PairingExpiresAtUtc = null,
                PairedClientPublicKeySpki = canonicalSpki,
                PairedClientFingerprintSha256 = fingerprint,
            };
            await SaveAsync(paired, cancellationToken).ConfigureAwait(false);
            return paired;
        }
        finally
        {
            if (expectedHash is not null)
            {
                CryptographicOperations.ZeroMemory(expectedHash);
            }
            if (actualHash is not null)
            {
                CryptographicOperations.ZeroMemory(actualHash);
            }
            _writeGate.Release();
        }
    }

    private async Task SaveAsync(
        DirectBridgeConfig config,
        CancellationToken cancellationToken)
    {
        string? directory = System.IO.Path.GetDirectoryName(_path);
        if (string.IsNullOrEmpty(directory))
        {
            throw ConfigInvalid();
        }
        Directory.CreateDirectory(directory);
        string temporaryPath = System.IO.Path.Combine(
            directory,
            $".{System.IO.Path.GetFileName(_path)}."
                + $"{Convert.ToHexString(RandomNumberGenerator.GetBytes(8))}.tmp");
        string json = JsonSerializer.Serialize(new
        {
            contract = DirectBridgeContract.ConfigContract,
            localOptIn = config.LocalOptIn,
            microphoneEndpointId = config.MicrophoneEndpointId,
            listenHost = config.ListenHost,
            listenPort = config.ListenPort,
            allowedOrigins = config.AllowedOrigins
                .Order(StringComparer.Ordinal)
                .ToArray(),
            allowedTailscaleUserLogin =
                config.AllowedTailscaleUserLogin,
            experimentalSingleUserMode =
                config.ExperimentalSingleUserMode,
            pairingCodeHash = config.PairingCodeHash,
            pairingExpiresAtUtc = config.PairingExpiresAtUtc,
            pairedClientPublicKeySpki =
                config.PairedClientPublicKeySpki,
            pairedClientFingerprintSha256 =
                config.PairedClientFingerprintSha256,
            outputScope = config.OutputScope,
            appKind = config.AppKind,
            runtimeStatusPath = config.RuntimeStatusPath,
        }, new JsonSerializerOptions(DirectBridgeContract.JsonOptions)
        {
            WriteIndented = true,
        });

        try
        {
            await using (
                FileStream stream = new(
                    temporaryPath,
                    FileMode.CreateNew,
                    FileAccess.Write,
                    FileShare.None,
                    4096,
                    FileOptions.WriteThrough | FileOptions.Asynchronous)
            )
            {
                await stream.WriteAsync(
                    Encoding.UTF8.GetBytes(json),
                    cancellationToken).ConfigureAwait(false);
                await stream.FlushAsync(cancellationToken)
                    .ConfigureAwait(false);
            }
            File.Move(temporaryPath, _path, overwrite: true);
        }
        finally
        {
            if (File.Exists(temporaryPath))
            {
                File.Delete(temporaryPath);
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

    private static void ValidatePairing(
        string hash,
        DateTimeOffset? expiresAtUtc)
    {
        if ((hash.Length == 0) != !expiresAtUtc.HasValue)
        {
            throw ConfigInvalid();
        }
        if (hash.Length != 0)
        {
            byte[] value = DirectBase64Url.Decode(
                hash,
                SHA256.HashSizeInBytes,
                "BW_COMPUTER_VOICE_DIRECT_CONFIG_INVALID");
            if (
                value.Length != SHA256.HashSizeInBytes
                || expiresAtUtc!.Value.Offset != TimeSpan.Zero
            )
            {
                throw ConfigInvalid();
            }
        }
    }

    private static void ValidatePairedClient(
        string spki,
        string fingerprint)
    {
        if ((spki.Length == 0) != (fingerprint.Length == 0))
        {
            throw ConfigInvalid();
        }
        if (spki.Length == 0)
        {
            return;
        }

        (string canonicalSpki, string actualFingerprint) =
            ValidateClientSpki(spki);
        if (
            !string.Equals(spki, canonicalSpki, StringComparison.Ordinal)
            || !string.Equals(
                fingerprint,
                actualFingerprint,
                StringComparison.Ordinal)
        )
        {
            throw ConfigInvalid();
        }
    }

    internal static (
        string CanonicalSpki,
        string Fingerprint) ValidateClientSpki(string value)
    {
        byte[] spki = DirectBase64Url.Decode(
            value,
            256,
            "BW_COMPUTER_VOICE_DIRECT_CLIENT_KEY_INVALID");
        try
        {
            using ECDsa key = ECDsa.Create();
            key.ImportSubjectPublicKeyInfo(spki, out int bytesRead);
            if (bytesRead != spki.Length || key.KeySize != 256)
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_CLIENT_KEY_INVALID",
                    "客户端公钥必须是 ECDSA P-256 SPKI");
            }
            byte[] canonical = key.ExportSubjectPublicKeyInfo();
            return (
                DirectBase64Url.Encode(canonical),
                DirectBase64Url.Encode(SHA256.HashData(canonical)));
        }
        catch (CryptographicException exception)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_CLIENT_KEY_INVALID",
                "客户端公钥必须是 ECDSA P-256 SPKI",
                retryable: false,
                innerException: exception);
        }
        finally
        {
            CryptographicOperations.ZeroMemory(spki);
        }
    }

    private static DateTimeOffset? ParseNullableUtc(JsonElement value)
    {
        if (value.ValueKind == JsonValueKind.Null)
        {
            return null;
        }
        if (
            value.ValueKind != JsonValueKind.String
            || !DateTimeOffset.TryParse(
                value.GetString(),
                CultureInfo.InvariantCulture,
                DateTimeStyles.RoundtripKind,
                out DateTimeOffset parsed)
            || parsed.Offset != TimeSpan.Zero
        )
        {
            throw ConfigInvalid();
        }
        return parsed;
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
