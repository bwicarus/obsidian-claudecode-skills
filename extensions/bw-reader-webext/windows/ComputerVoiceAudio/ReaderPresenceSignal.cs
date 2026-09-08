using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.AspNetCore.Http;

namespace BwReader.ComputerVoiceAudio;

/// 在场信号的落点（2026-09-08 用户：「还可以加入是否带耳机的判断」）。
///
/// App 把**此刻**的音频输出走向和前台状态 POST 过来，Windows 落一份小 JSON，
/// `situation_signals.py` 把它折成 `audio_route` / `headphones` / `app_foreground`
/// 三个信号，供 AI 判断和 `situation_triggers.py` 的规则使用。
///
/// 为什么值得有：位置能告诉我们他在不在家，但"能不能出声"还差一半 ——
/// 在外面戴着耳机和在外面不戴耳机，对语音播放是两个完全不同的场合。
///
/// ⚠ **App 侧的自动静音不等这个端点**。静音必须在设备本地即时发生
/// （耳机一拔就得静），绕一趟 Windows 再回来早就来不及了。这里收的是
/// 给**AI 判断和规则触发**用的副本，不是控制回路的一环。
///
/// 存到 %LOCALAPPDATA%\BWReader，与 ReaderSleepSignal 同一处 ——
/// **不放桥的安装目录**，那里会被整目录原子替换。
internal static class ReaderPresenceSignal
{
    internal const string RoutePath = "/reader-presence/v1";
    private const string StoreFileName = "presence-signal.json";
    private const int MaxBodyBytes = 4 * 1024;

    /// 认得的音频走向。**表外的值折成 other 而不是拒绝**：
    /// 拒绝会让新系统版本上的 App 静默地一次都报不成功，而这条链没有界面。
    private static readonly HashSet<string> KnownRoutes = new(
        StringComparer.OrdinalIgnoreCase)
    {
        "speaker", "receiver", "headphones", "bluetooth", "airplay", "usb",
        "carplay", "other",
    };

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

        string route = Route(body["audioRoute"]);
        if (route.Length == 0)
        {
            await Fail(context, "audioRoute 是必需的").ConfigureAwait(false);
            return;
        }
        bool? foreground = Flag(body["foreground"]);
        if (foreground is null)
        {
            // 缺 foreground 就整条拒绝：补一个默认值等于替 App 编一个状态，
            // 而 Python 侧会拿它当真去判断"要不要现在出声"。
            await Fail(context, "foreground 必须是布尔值").ConfigureAwait(false);
            return;
        }

        long now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        JsonObject record = new()
        {
            ["contract"] = "reader-presence-signal/1",
            ["audioRoute"] = route,
            ["foreground"] = foreground.Value,
            ["device"] = Text(body["device"], 40) is { Length: > 0 } device
                ? device
                : "unknown",
            // ⚠ atMs 用**服务端时钟**：新鲜度问的是"App 多久前告诉我们的"，
            // 拿设备时钟算这个，设备时钟一歪就会让耳机状态永远显得很新或很旧。
            // 设备自己的时刻另存一份，只作参考。
            ["atMs"] = now,
            ["reportedAtMs"] = Millis(body["atMs"]) is var reported && reported > 0
                ? reported
                : null,
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

        JsonObject reply = new()
        {
            ["ok"] = true,
            ["audioRoute"] = route,
        };
        // 顺路把「语音区」带回去：App 靠它在**本机**判断在不在家。
        // 为什么搭这趟车而不另开一个端点：App 需要这份判据的时机，正好
        // 就是它在报在场状态的时候；另开端点等于多一条要各自保活的链。
        JsonNode? zones = ReadVoiceZones();
        if (zones is not null) reply["voiceZones"] = zones;
        context.Response.StatusCode = StatusCodes.Status200OK;
        context.Response.ContentType = "application/json; charset=utf-8";
        await context.Response.WriteAsync(
            reply.ToJsonString(), cancellationToken).ConfigureAwait(false);
    }

    /// 读 Python 导出的语音区。读不到就**不带这个字段** ——
    /// 空数组会被 App 理解成"一个已命名的地点都没有"，于是它认为自己
    /// 永远在外面，一出声就静音。缺字段才是"这次没拿到，用你缓存的那份"。
    private static JsonNode? ReadVoiceZones()
    {
        try
        {
            Configure();
            string path;
            lock (Gate)
            {
                path = Path.Combine(_storeDirectory, "voice-zones.json");
            }
            if (!File.Exists(path)) return null;
            JsonNode? parsed = JsonNode.Parse(File.ReadAllText(path));
            return parsed as JsonObject;
        }
        catch (Exception)
        {
            return null;
        }
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

    /// 归一化音频走向。不认识的一律 other —— 而 other 在 Python 侧算
    /// **戴着耳机**（即"别自动静音"）：认错成扬声器会把人的语音静掉，
    /// 认错成耳机只是少静一次，两种代价不对等。
    private static string Route(JsonNode? node)
    {
        string text = Text(node, 20);
        if (text.Length == 0) return string.Empty;
        return KnownRoutes.Contains(text) ? text.ToLowerInvariant() : "other";
    }

    private static bool? Flag(JsonNode? node) =>
        node is JsonValue value && value.TryGetValue(out bool flag) ? flag : null;

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

    private static string Text(JsonNode? node, int limit)
    {
        if (node is not JsonValue value
            || !value.TryGetValue(out string? text))
        {
            return string.Empty;
        }
        string trimmed = (text ?? string.Empty).Trim();
        return trimmed[..Math.Min(trimmed.Length, limit)];
    }

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
