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
    string? ExceptionType = null,
    // 一段**受控**的补充说明（2026-09-10）。
    //
    // 上面那条规矩不变：异常 message 永远不进来。这个字段专收"代码自己拼出来
    // 的、由常量与 id 组成"的线索 —— 例如媒体停止原因
    // `fault:<stage>:<TypeName>` / `takeover-by-new-start:<connectionId>`，
    // 与 Stage、ExceptionType 是同一类东西，只是拼在一起。
    //
    // 为什么需要它：DirectBridgeServer 报 MEDIA_STOPPED_UNEXPECTEDLY 时特意把
    // LastMediaStopReason 拼进了异常消息（那儿的注释写着"2026-09-05 查了一小
    // 时"），可账本与状态文件都不收消息 —— 于是那条线索**一次也没落过盘**，
    // 2026-09-10 又照原样查不出来一次。修在消息里等于没修。
    //
    // ⚠ 只能由调用方显式传入，且必须过 SanitizeDetail；FromException 永远不
    // 填它 —— 异常消息因此没有任何路径能流到这里来。
    string? SafeDetail = null)
{
    /// <summary>
    /// 字符白名单 + 长度上限。不合规就整段丢弃，**不做替换** ——
    /// 半个被改写过的字符串比没有更难判读，而且会让人以为自己看到了全部。
    /// </summary>
    internal static string? SanitizeDetail(string? detail)
    {
        if (string.IsNullOrWhiteSpace(detail) || detail.Length > 200)
        {
            return null;
        }
        foreach (char character in detail)
        {
            bool allowed =
                character is >= 'a' and <= 'z'
                || character is >= 'A' and <= 'Z'
                || character is >= '0' and <= '9'
                || character is '-' or '_' or '.' or ':';
            if (!allowed)
            {
                return null;
            }
        }
        return detail;
    }

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
