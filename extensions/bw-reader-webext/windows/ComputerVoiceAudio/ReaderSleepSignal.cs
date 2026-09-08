using System.Text.Json;
using System.Text.Json.Nodes;

namespace BwReader.ComputerVoiceAudio;

/// 睡眠信号的落点（2026-09-08 用户要做手表功能）。
///
/// App 从健康库读到「昨晚几点睡、今早几点醒」之后 POST 到这里，Windows 落一份小 JSON，
/// `replication_notifications.wake_time_today_ms()` 优先读它 —— 读不到才回落到
/// 「命令账本里那段睡眠缺口」的推断。两级的关系是**更准的替换更粗的**，不是二选一：
/// 设备活动只能看出「他开始碰这套系统了」，健康数据才知道他其实七点就醒了、只是先用了别的软件。
///
/// 存到 %LOCALAPPDATA%\BWReader，与 ReaderDisplayBoard 和 replication_notifications.py 的
/// root 同一处 —— **不放桥的安装目录**，那里会被整目录原子替换。
///
/// ⚠ 这个端点只收数据、不做判断。什么时候算「起床」、要不要因此提醒，全在 Python 侧；
/// 桥这边多一层判断只会变成第二个真值源。
internal static class ReaderSleepSignal
{
    internal const string RoutePath = "/reader-sleep/v1";
    private const string StoreFileName = "sleep-signal.json";
    private const int MaxBodyBytes = 4 * 1024;

    private static readonly object Gate = new();
    private static string _storeDirectory = string.Empty;

    internal static void Configure()
    {
        lock (Gate)
        {
            if (_storeDirectory.Length > 0) return;
            _storeDirectory = Path.Combine(
                Environment.GetFolderPath(
                    Environment.SpecialFolder.LocalApplicationData),
                "BWReader");
        }
    }

    private static string StorePath
    {
        get
        {
            Configure();
            lock (Gate)
            {
                return Path.Combine(_storeDirectory, StoreFileName);
            }
        }
    }

    internal static async Task WriteResponseAsync(
        HttpContext context,
        CancellationToken cancellationToken)
    {
        JsonObject? body = await ReadBodyAsync(context, cancellationToken)
            .ConfigureAwait(false);
        if (body is null)
        {
            await Fail(context, "请求不是 JSON 对象，或超过 4 KiB").ConfigureAwait(false);
            return;
        }

        long woke = Millis(body["wokeAtMs"]);
        long slept = Millis(body["sleptAtMs"]);
        if (woke <= 0)
        {
            await Fail(context, "wokeAtMs 必须是正的毫秒时间戳").ConfigureAwait(false);
            return;
        }
        long now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        // 未来时刻一律拒绝：设备时钟错乱时写进去会让「起床点」永远在前方，提醒再也不出声。
        if (woke > now + 60_000 || (slept > 0 && slept > now + 60_000))
        {
            await Fail(context, "时间戳在未来，拒绝写入").ConfigureAwait(false);
            return;
        }

        JsonObject record = new()
        {
            ["contract"] = "reader-sleep-signal/1",
            ["wokeAtMs"] = woke,
            ["sleptAtMs"] = slept > 0 ? slept : null,
            ["asleepMinutes"] = Millis(body["asleepMinutes"]) is var minutes && minutes > 0
                ? minutes
                : null,
            ["source"] = Text(body["source"]) is { Length: > 0 } source ? source : "unknown",
            ["receivedAtMs"] = now,
        };

        try
        {
            Configure();
            Directory.CreateDirectory(_storeDirectory);
            string path = StorePath;
            string temporary = path + ".tmp-" + Environment.ProcessId;
            File.WriteAllText(temporary, record.ToJsonString(
                new JsonSerializerOptions { WriteIndented = true }));
            File.Move(temporary, path, overwrite: true);
        }
        catch (Exception exception)
        {
            await Fail(context, "写入失败：" + exception.Message).ConfigureAwait(false);
            return;
        }

        context.Response.StatusCode = StatusCodes.Status200OK;
        context.Response.ContentType = "application/json; charset=utf-8";
        await context.Response.WriteAsync(
            new JsonObject { ["ok"] = true, ["wokeAtMs"] = woke }.ToJsonString(),
            cancellationToken).ConfigureAwait(false);
    }

    private static async Task<JsonObject?> ReadBodyAsync(
        HttpContext context,
        CancellationToken cancellationToken)
    {
        try
        {
            using MemoryStream buffer = new();
            await context.Request.Body
                .CopyToAsync(buffer, cancellationToken)
                .ConfigureAwait(false);
            if (buffer.Length is 0 or > MaxBodyBytes) return null;
            return JsonNode.Parse(buffer.ToArray()) as JsonObject;
        }
        catch (Exception)
        {
            return null;
        }
    }

    private static long Millis(JsonNode? node)
    {
        if (node is not JsonValue value) return 0;
        if (value.TryGetValue(out long number)) return number;
        if (value.TryGetValue(out double real) && double.IsFinite(real))
        {
            return (long)Math.Round(real);
        }
        return 0;
    }

    private static string Text(JsonNode? node) =>
        node is JsonValue value && value.TryGetValue(out string? text)
            ? (text ?? string.Empty).Trim()[..Math.Min((text ?? string.Empty).Trim().Length, 40)]
            : string.Empty;

    private static async Task Fail(HttpContext context, string detail)
    {
        context.Response.StatusCode = StatusCodes.Status400BadRequest;
        context.Response.ContentType = "application/json; charset=utf-8";
        // detail 原样端出去：折成"操作失败"等于把唯一有用的一句话丢掉。
        await context.Response.WriteAsync(
            new JsonObject { ["ok"] = false, ["detail"] = detail }.ToJsonString())
            .ConfigureAwait(false);
    }
}
