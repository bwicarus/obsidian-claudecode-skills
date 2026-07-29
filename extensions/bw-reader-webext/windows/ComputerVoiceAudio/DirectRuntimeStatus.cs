using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace BwReader.ComputerVoiceAudio;

internal sealed class DirectRuntimeStatusWriter
{
    private static readonly IReadOnlySet<string> AllowedStates =
        new HashSet<string>(StringComparer.Ordinal)
        {
            "starting",
            "idle",
            "reader-connected",
            "starting-app",
            "waiting-app-ready",
            "starting-capture",
            "active",
            "faulted",
            "stopping",
            "stopped",
        };

    private readonly string _path;
    private readonly string _serviceInstanceId;
    private readonly SemaphoreSlim _writeGate = new(1, 1);

    internal DirectRuntimeStatusWriter(
        string path,
        string serviceInstanceId)
    {
        if (
            !System.IO.Path.IsPathFullyQualified(path)
            || !DirectBridgeContract.IsServiceInstanceId(
                serviceInstanceId)
        )
        {
            throw new ArgumentException(
                "runtime status path or instance ID is invalid");
        }
        _path = System.IO.Path.GetFullPath(path);
        _serviceInstanceId = serviceInstanceId;
    }

    internal async Task WriteAsync(
        string state,
        bool readerConnected,
        bool captureActive,
        CancellationToken cancellationToken)
    {
        if (!AllowedStates.Contains(state))
        {
            throw new ArgumentOutOfRangeException(nameof(state));
        }
        await _writeGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            string? directory = System.IO.Path.GetDirectoryName(_path);
            if (string.IsNullOrEmpty(directory))
            {
                throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_DIRECT_STATUS_PATH_INVALID");
            }
            Directory.CreateDirectory(directory);
            string temporaryPath = System.IO.Path.Combine(
                directory,
                $".{System.IO.Path.GetFileName(_path)}."
                    + $"{Convert.ToHexString(
                        RandomNumberGenerator.GetBytes(8))}.tmp");
            string json = JsonSerializer.Serialize(new
            {
                contract =
                    DirectBridgeContract.RuntimeStatusContract,
                serviceInstanceId = _serviceInstanceId,
                pid = Environment.ProcessId,
                state,
                readerConnected,
                captureActive,
                updatedAtUtc = DateTimeOffset.UtcNow,
            }, new JsonSerializerOptions(
                DirectBridgeContract.JsonOptions)
            {
                WriteIndented = true,
            });
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
        finally
        {
            _writeGate.Release();
        }
    }
}

internal static class DirectSecurityLog
{
    internal static void Write(
        string serviceInstanceId,
        string eventName,
        string code,
        bool ok)
    {
        // Deliberately log only fixed event/code values and the local service
        // instance. Pair codes, public keys, signatures, origins, control
        // messages and PCM never enter logs.
        Console.Error.WriteLine(JsonSerializer.Serialize(new
        {
            contract = "reader-computer-voice-direct-security-log/1",
            atUtc = DateTimeOffset.UtcNow,
            serviceInstanceId,
            @event = eventName,
            code,
            ok,
        }, DirectBridgeContract.JsonOptions));
    }
}
