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
            MoveWithRetries(temporaryPath);
        }
        finally
        {
            if (File.Exists(temporaryPath))
            {
                File.Delete(temporaryPath);
            }
        }
    }

    /// 覆盖目标文件时**别人正开着它**是常态，不是故障。
    ///
    /// ⚠ 2026-09-11 实测：这一步抛 UnauthorizedAccessException 会一路冒到
    /// `DirectBridgeServer.RunAsync`，**把整条语音桥杀掉**，然后被守护拉起；
    /// 拉起之后队列里那条入口指令又被执行一次 —— 于是"挂断几秒后语音自己
    /// 又回来了"、"连上之后又跑一次开语音流程"。当天日志里这样的
    /// heartbeat 失败刷了几百行，服务反复 start。
    ///
    /// 租约是**记账**：它回答"现在是哪个进程在服务"。丢一次的代价是这个问题
    /// 暂时没答案；而为它杀掉一条正在服务的语音链，代价是用户正在打的电话。
    /// 两者不成比例，所以这里**重试几次，仍不行就放弃并继续跑**。
    ///
    /// Windows 上 File.Move(overwrite) 需要删掉目标，而任何一个没带
    /// FILE_SHARE_DELETE 打开它的读者都会让这一步失败 —— 包括随手
    /// `cat` 一下这个文件的人。短暂重试正好覆盖这种一闪而过的占用。
    private void MoveWithRetries(string temporaryPath)
    {
        const int attempts = 5;
        for (int attempt = 1; ; attempt++)
        {
            try
            {
                File.Move(temporaryPath, _path, overwrite: true);
                return;
            }
            catch (Exception error) when (
                (error is UnauthorizedAccessException or IOException)
                && attempt < attempts)
            {
                Thread.Sleep(60 * attempt);
            }
            catch (Exception error) when (
                error is UnauthorizedAccessException or IOException)
            {
                // ⚠ 放弃也要**出声**：静默地不写租约，会让"现在谁在服务"
                // 这个问题在排查时变成一个说不清的空白。
                Console.Error.WriteLine(
                    "[lease] 写不进租约（重试 " + attempts + " 次仍被占用）："
                    + error.GetType().Name + "；服务继续运行");
                return;
            }
        }
    }
}
