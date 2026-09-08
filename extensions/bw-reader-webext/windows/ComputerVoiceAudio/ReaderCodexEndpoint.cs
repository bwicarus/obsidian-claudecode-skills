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
/// - **带 TTL，过期即失效。** 一个陈旧绑定会让板面事件一直推进一个已经
///   死掉的任务，而那种失败完全无声：接口照样可能返回成功。
/// - **地址不做任何猜测。** 不枚举命名管道、不去读 Codex 的安装目录 ——
///   交接里明说安装路径带版本号会变。拿不到就不推，并说出原因。
/// - **注册只是"能推"，不等于"该推"。** 推不推由 `ReaderCodexPush.Enabled`
///   决定，而它默认关：消费端还在轮询时两条都开就是双发。
internal static class ReaderCodexEndpoint
{
    internal const string RoutePath = "/reader-codex-endpoint/v1";
    private const string StoreFileName = "codex-push-binding.json";
    private const int MaxBodyBytes = 4 * 1024;
    /// 绑定活多久。比一次语音会话长一些，但短到不会跨到下一次 ——
    /// 用户中途换任务时，旧绑定最迟这么久之后自己失效。
    internal static readonly TimeSpan Lifetime = TimeSpan.FromHours(6);

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

    /// 当前可用的绑定。没注册过、读不出来、或已过期都返回 null。
    internal static Binding? Current()
    {
        try
        {
            string path = StorePath;
            if (!File.Exists(path)) return null;
            JsonNode? parsed = JsonNode.Parse(File.ReadAllText(path));
            if (parsed is not JsonObject value) return null;
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

        long now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        JsonObject record = new()
        {
            ["contract"] = "reader-codex-push-binding/1",
            ["pipeName"] = pipeName,
            ["threadId"] = threadId,
            ["registeredAtMs"] = now,
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
        await Ok(context, new JsonObject
        {
            ["ok"] = true,
            ["threadId"] = threadId,
            ["expiresAtMs"] = now + (long)Lifetime.TotalMilliseconds,
            ["pushEnabled"] = ReaderCodexPush.Enabled,
            ["lastNote"] = ReaderCodexPush.LastNote,
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
