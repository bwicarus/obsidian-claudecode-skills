using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace BwReader.ComputerVoiceAudio;

internal sealed record DirectRuntimeError(
    string FailureId,
    string Code,
    string Stage,
    string? Hresult,
    DateTimeOffset AtUtc,
    // 异常的**类型名**，不是它的 message（2026-08-18）。
    //
    // 起因：INTERNAL_FAILURE 这种通配码不带任何附加信息，runtime-status 和
    // failures.jsonl 都说不出发生了什么。我本来想把异常 message 带出来 ——
    // 那是错的：message **不是被顺手丢掉的，是被刻意挡住的**。
    // 自测 direct-status-endpoint-failure-is-sanitized-and-not-ready 里那个异常的
    // message 就写着 "secret-endpoint-id-must-never-be-serialized" —— 设备/端点
    // 标识会出现在 message 里，而这个文件要被显示、被读取、可能被同步。
    //
    // 类型名则是编译期常量，永远不含用户数据，同时又足以把"哪一类失败"说清楚。
    // 拿得到确定性又安全的那部分，拿不到的就老实不拿。
    string? ExceptionType = null)
{
    internal static DirectRuntimeError FromException(
        Exception exception,
        string fallbackStage,
        DateTimeOffset? atUtc = null)
    {
        ArgumentNullException.ThrowIfNull(exception);
        AudioCaptureStageException? audioStage =
            FindAudioStageFailure(exception);
        string code = exception is DirectProtocolException protocol
            && DirectBridgeContract.IsSafeId(protocol.Code)
                ? protocol.Code
                : "BW_COMPUTER_VOICE_DIRECT_INTERNAL_FAILURE";
        string stage = audioStage?.Stage ?? fallbackStage;
        if (
            string.IsNullOrWhiteSpace(stage)
            || stage.Length > 80
            || stage.Any(character =>
                !(character is >= 'a' and <= 'z')
                && character is not '-' and not '.')
        )
        {
            stage = "unknown";
        }
        // 只有类型名 —— 见上面 ExceptionType 的说明：message 可能带设备/端点标识。
        // 通配码 INTERNAL_FAILURE 时它是唯一的线索，所以对通配码才带；
        // 有专属 code 的失败本身已经说明了是什么，不必再加一层实现细节。
        string? exceptionType =
            code == "BW_COMPUTER_VOICE_DIRECT_INTERNAL_FAILURE"
                ? exception.GetType().Name
                : null;
        return new DirectRuntimeError(
            "failure-" + DirectBase64Url.Encode(
                RandomNumberGenerator.GetBytes(12)),
            code,
            stage,
            audioStage is null
                ? null
                : $"0x{unchecked((uint)audioStage.Result):X8}",
            (atUtc ?? DateTimeOffset.UtcNow).ToUniversalTime(),
            exceptionType);
    }

    private static AudioCaptureStageException? FindAudioStageFailure(
        Exception exception)
    {
        if (exception is AudioCaptureStageException stage)
        {
            return stage;
        }
        if (exception is AggregateException aggregate)
        {
            foreach (Exception inner in aggregate.Flatten().InnerExceptions)
            {
                AudioCaptureStageException? found =
                    FindAudioStageFailure(inner);
                if (found is not null)
                {
                    return found;
                }
            }
        }
        return exception.InnerException is null
            ? null
            : FindAudioStageFailure(exception.InnerException);
    }
}

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
        await WriteAsync(
            state,
            readerConnected,
            captureActive,
            lastError: null,
            cancellationToken).ConfigureAwait(false);
    }

    internal async Task WriteAsync(
        string state,
        bool readerConnected,
        bool captureActive,
        DirectRuntimeError? lastError,
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
                lastError,
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
