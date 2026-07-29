using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace BwReader.ComputerVoiceAudio;

internal sealed class DirectServiceLease
{
    internal const string Contract =
        "reader-computer-voice-desktop-service/1";

    private readonly string _path;
    private readonly int _pid;
    private readonly string _executable;
    private readonly string _configPath;
    private readonly DateTimeOffset _startedAtUtc;
    private readonly SemaphoreSlim _gate = new(1, 1);

    internal DirectServiceLease(
        string installationRoot,
        string configPath)
        : this(
            System.IO.Path.Combine(
                installationRoot,
                "runtime",
                "computer-voice-direct.service.json"),
            Environment.ProcessId,
            Environment.ProcessPath
                ?? throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_DIRECT_PROCESS_PATH_UNKNOWN"),
            configPath,
            DateTimeOffset.UtcNow)
    {
    }

    internal DirectServiceLease(
        string path,
        int pid,
        string executable,
        string configPath,
        DateTimeOffset startedAtUtc)
    {
        if (
            !System.IO.Path.IsPathFullyQualified(path)
            || pid <= 0
            || !System.IO.Path.IsPathFullyQualified(executable)
            || !System.IO.Path.IsPathFullyQualified(configPath)
            || startedAtUtc.Offset != TimeSpan.Zero
        )
        {
            throw new ArgumentException(
                "service lease values are invalid");
        }
        _path = System.IO.Path.GetFullPath(path);
        _pid = pid;
        _executable = System.IO.Path.GetFullPath(executable);
        _configPath = System.IO.Path.GetFullPath(configPath);
        _startedAtUtc = startedAtUtc;
    }

    internal string Path => _path;

    internal async Task WriteAsync(
        CancellationToken cancellationToken)
    {
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            string json = JsonSerializer.Serialize(new
            {
                contract = Contract,
                pid = _pid,
                executable = _executable,
                configPath = _configPath,
                startedAtUtc = _startedAtUtc,
            }, new JsonSerializerOptions(
                DirectBridgeContract.JsonOptions)
            {
                WriteIndented = true,
            });
            await AtomicWriteAsync(
                json,
                cancellationToken).ConfigureAwait(false);
        }
        finally
        {
            _gate.Release();
        }
    }

    internal async Task ClearIfOwnedAsync()
    {
        await _gate.WaitAsync().ConfigureAwait(false);
        try
        {
            if (!File.Exists(_path))
            {
                return;
            }
            FileInfo info = new(_path);
            if (info.Length is <= 0 or > 16 * 1024)
            {
                return;
            }
            using JsonDocument document = JsonDocument.Parse(
                await File.ReadAllTextAsync(_path).ConfigureAwait(false));
            JsonElement root = document.RootElement;
            if (
                root.ValueKind == JsonValueKind.Object
                && root.TryGetProperty(
                    "contract",
                    out JsonElement contract)
                && contract.GetString() == Contract
                && root.TryGetProperty("pid", out JsonElement pid)
                && pid.TryGetInt32(out int ownerPid)
                && ownerPid == _pid
                && root.TryGetProperty(
                    "executable",
                    out JsonElement executable)
                && string.Equals(
                    executable.GetString(),
                    _executable,
                    StringComparison.OrdinalIgnoreCase)
                && root.TryGetProperty(
                    "configPath",
                    out JsonElement configPath)
                && string.Equals(
                    configPath.GetString(),
                    _configPath,
                    StringComparison.OrdinalIgnoreCase)
            )
            {
                File.Delete(_path);
            }
        }
        catch (
            Exception exception
        ) when (
            exception is IOException
            or UnauthorizedAccessException
            or JsonException
        )
        {
            // A newer owner or a temporarily locked lease must be preserved.
        }
        finally
        {
            _gate.Release();
        }
    }

    private async Task AtomicWriteAsync(
        string json,
        CancellationToken cancellationToken)
    {
        string? directory = System.IO.Path.GetDirectoryName(_path);
        if (string.IsNullOrEmpty(directory))
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_DIRECT_SERVICE_LEASE_PATH_INVALID");
        }
        Directory.CreateDirectory(directory);
        string temporaryPath = System.IO.Path.Combine(
            directory,
            $".{System.IO.Path.GetFileName(_path)}."
                + $"{Convert.ToHexString(
                    RandomNumberGenerator.GetBytes(8))}.tmp");
        try
        {
            await File.WriteAllTextAsync(
                temporaryPath,
                json,
                new UTF8Encoding(encoderShouldEmitUTF8Identifier: false),
                cancellationToken).ConfigureAwait(false);
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
}
