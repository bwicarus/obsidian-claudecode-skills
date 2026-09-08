using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.AspNetCore.Http;

namespace BwReader.ComputerVoiceAudio;

/// Codex 主动推送的**绑定登记处**（2026-09-09 Codex→Claude 交接）。
///
/// ## 为什么必须由 AI 自己来注册
///
/// 推送要两样东西：命名管道地址，和目标任务 id。两样都只存在于
/// **Codex 亲自启动的进程**的环境里（`CODEX_APP_TOOLS_PIPE_PATH` /
/// `CODEX_THREAD_ID`）。独立启动的 ReaderPC 拿不到 —— 2026-09-09 实测确认。
///
/// 于是设计只剩一种：语音会话开始时，由 AI 调一次这个端点把自己的地址和
/// 任务 id 报上来。这也顺带解决了「绑对任务」这件事 —— 只有它知道自己是谁。
///
/// ## 纪律
///
/// - **失效由实测判定，不由时钟。** 一个陈旧绑定会让板面事件一直推进一个
///   已经死掉的任务。判它死没死的**不是时间**，是推送本身连续失败
///   （见 `Invalidate` 和 `ReaderCodexPush` 的失败计数）；`Lifetime` 只是
///   防"注册完再没人管过"的远期兜底。2026-09-09 从 6 小时的硬 TTL 改过来，
///   因为那种设计会在一段长会话中途把活绑定杀掉，制造出它本要防的静默停摆。
/// - **地址不做任何猜测。** 不枚举命名管道、不去读 Codex 的安装目录 ——
///   交接里明说安装路径带版本号会变。拿不到就不推，并说出原因。
/// - **注册只是"能推"，不等于"该推"。** 推不推由 `ReaderCodexPush.Enabled`
///   决定，而它默认关：消费端还在轮询时两条都开就是双发。
internal static class ReaderCodexEndpoint
{
    internal const string RoutePath = "/reader-codex-endpoint/v1";
    private const string StoreFileName = "codex-push-binding.json";
    private const int MaxBodyBytes = 4 * 1024;
    /// 绑定的**远期兜底**期限。
    ///
    /// ⚠ 2026-09-09 从 6 小时放宽到 72 小时，因为用时钟判"目标还活着吗"
    /// 是错的方向（用户当场问了这个设计）：
    ///   · 时钟答不出目标死没死，而**推送本身答得出** —— 推失败了就是死了；
    ///   · 6 小时会在一段长会话**中途**过期，推送悄悄停掉，
    ///     那正是这个机制本来要防的失败，只不过改由我们的定时器制造；
    ///   · 两种错的代价不对称：过期太早=静默停摆（很糟），
    ///     过期太晚=往死目标推一次并失败（会记在 lastNote 里，便宜）。
    /// 所以真正判失效的是**连续推送失败**（见 `Invalidate`），
    /// 这个时钟只是防"注册完就再没人管过"的兜底。
    internal static readonly TimeSpan Lifetime = TimeSpan.FromHours(72);

    internal sealed record Binding(string PipeName, string ThreadId);

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

    /// `NamedPipeClientStream` 要的是**管道名**，不是 `\\.\pipe\` 全路径。
    /// 环境变量里两种形态都可能出现，所以这里统一削平。
    internal static string NormalizePipeName(string raw)
    {
        string text = (raw ?? string.Empty).Trim();
        const string prefix = @"\\.\pipe\";
        if (text.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
        {
            text = text[prefix.Length..];
        }
        // 反斜杠在管道名里非法（那是路径分隔符），出现就说明还没削干净。
        return text.Contains('\\') ? string.Empty : text;
    }

    /// 把绑定标成失效，并**记下原因**。由推送侧在连续失败到上限时调。
    ///
    /// 不直接删文件：删掉之后再问"为什么不推了"就没有答案了，
    /// 而这条链没有界面，原因丢了就等于没发生过。重新登记会清掉这个标记。
    internal static void Invalidate(string reason)
    {
        try
        {
            string path = StorePath;
            if (!File.Exists(path)) return;
            if (JsonNode.Parse(File.ReadAllText(path)) is not JsonObject value)
            {
                return;
            }
            value["invalidAtMs"] = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
            value["invalidReason"] = reason.Length > 200
                ? reason[..200] : reason;
            string temporary = path + ".tmp-" + Environment.ProcessId;
            File.WriteAllText(temporary, value.ToJsonString(
                new JsonSerializerOptions { WriteIndented = true }));
            File.Move(temporary, path, overwrite: true);
        }
        catch (Exception)
        {
            // 记不下就算了：这一步是为了留线索，不该反过来把推送弄坏。
        }
    }

    /// 绑定为什么失效了。没失效返回空串 —— 给状态查询用。
    internal static string InvalidReason()
    {
        try
        {
            string path = StorePath;
            if (!File.Exists(path)) return string.Empty;
            if (JsonNode.Parse(File.ReadAllText(path)) is not JsonObject value)
            {
                return string.Empty;
            }
            return (string?)value["invalidReason"] ?? string.Empty;
        }
        catch (Exception)
        {
            return string.Empty;
        }
    }

    /// 当前可用的绑定。没注册过、读不出来、被判失效、或超过兜底期限都返回 null。
    internal static Binding? Current()
    {
        try
        {
            string path = StorePath;
            if (!File.Exists(path)) return null;
            JsonNode? parsed = JsonNode.Parse(File.ReadAllText(path));
            if (parsed is not JsonObject value) return null;
            // 连续推失败判定的失效**优先于**时钟：它是实测的，时钟是猜的。
            if (!string.IsNullOrEmpty((string?)value["invalidReason"]))
            {
                return null;
            }
            long at = (long?)value["registeredAtMs"] ?? 0;
            if (at <= 0) return null;
            long now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
            if (now - at > (long)Lifetime.TotalMilliseconds) return null;
            string pipe = NormalizePipeName((string?)value["pipeName"] ?? "");
            string thread = ((string?)value["threadId"] ?? "").Trim();
            if (pipe.Length == 0 || thread.Length == 0) return null;
            return new Binding(pipe, thread);
        }
        catch (Exception)
        {
            return null;
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
            await Fail(context, "请求不是 JSON 对象，或超过 4 KiB")
                .ConfigureAwait(false);
            return;
        }

        // 注销：AI 结束值守时报一次，别等 TTL 慢慢过期。
        if (body["unregister"] is JsonValue flag
            && flag.TryGetValue(out bool wantsClear) && wantsClear)
        {
            try
            {
                File.Delete(StorePath);
            }
            catch (Exception exception)
            {
                await Fail(context, "清除失败：" + exception.Message)
                    .ConfigureAwait(false);
                return;
            }
            ReaderCodexPush.SetEnabled(false);
            await Ok(context, new JsonObject
            {
                ["ok"] = true,
                ["unregistered"] = true,
            }, cancellationToken).ConfigureAwait(false);
            return;
        }

        string pipeName = NormalizePipeName(Text(body["pipePath"], 200));
        string threadId = Text(body["threadId"], 100);
        if (pipeName.Length == 0)
        {
            await Fail(context, "pipePath 是必需的（可以是名字或 \\\\.\\pipe\\ 全路径）")
                .ConfigureAwait(false);
            return;
        }
        if (threadId.Length == 0)
        {
            await Fail(context, "threadId 是必需的").ConfigureAwait(false);
            return;
        }
        // enabled 缺省**不改**当前开关：注册和"要不要推"是两件事，
        // 一次注册顺手把推送打开会在消费端还在轮询时造成双发。
        bool? wantEnabled = body["enabled"] is JsonValue enabledValue
            && enabledValue.TryGetValue(out bool parsedEnabled)
            ? parsedEnabled
            : null;

        // 先记下上一次是不是被判死过，登记完在响应里回给 AI ——
        // 否则它永远不知道中间断过一段，也就想不到去问为什么。
        string previousInvalid = InvalidReason();
        long now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        JsonObject record = new()
        {
            ["contract"] = "reader-codex-push-binding/1",
            ["pipeName"] = pipeName,
            ["threadId"] = threadId,
            ["registeredAtMs"] = now,
            // 重新登记 = "我又活了"：把上一次的失效判定清掉。
            // 不清的话，一次网络抖动判死之后就再也起不来了。
            ["invalidAtMs"] = null,
            ["invalidReason"] = null,
            ["expiresAtMs"] = now + (long)Lifetime.TotalMilliseconds,
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
            await Fail(context, "写入失败：" + exception.Message)
                .ConfigureAwait(false);
            return;
        }
        if (wantEnabled is bool decided)
        {
            ReaderCodexPush.SetEnabled(decided);
        }
        // 接上就推一次全量（2026-09-09 用户：推送只在变化时触发，
        // 登记之前就摆在板上的待办永远送不出去）。
        //
        // ⚠ 不 await：登记这个请求不该等一次跨进程推送。而且它要用
        //   `CancellationToken.None` —— 拿这个请求的 token 的话，
        //   响应一返回推送就被取消，表现是"登记成功但全量提醒从来没到"，
        //   而没有一处会报错。KJ 那边刚踩过同一个形态。
        if (ReaderCodexPush.Enabled)
        {
            _ = ReaderCodexPush.NotifyConnectedAsync(CancellationToken.None);
        }
        await Ok(context, new JsonObject
        {
            ["ok"] = true,
            ["threadId"] = threadId,
            ["expiresAtMs"] = now + (long)Lifetime.TotalMilliseconds,
            ["pushEnabled"] = ReaderCodexPush.Enabled,
            ["lastNote"] = ReaderCodexPush.LastNote,
            ["previousInvalidReason"] = previousInvalid.Length == 0
                ? null : previousInvalid,
        }, cancellationToken).ConfigureAwait(false);
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

    private static async Task Ok(
        HttpContext context, JsonObject body, CancellationToken token)
    {
        context.Response.StatusCode = StatusCodes.Status200OK;
        context.Response.ContentType = "application/json; charset=utf-8";
        await context.Response
            .WriteAsync(body.ToJsonString(), token)
            .ConfigureAwait(false);
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
