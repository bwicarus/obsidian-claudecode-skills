using System.Net.Http;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace BwReader.ComputerVoiceAudio;

// KJ 页级分析（2026-09-07 用户拍板：页是最小单位，读到即分析）。
//
// 桥是语音助手读页的表面：reader_context_snapshot 与 reader_page_text 的结果都附一个
// kjPage 块——未分析页给"先答后交"的指示与 YOLO 框，已分析页给页标注、节点掌握度、
// 公式 LaTeX、图描述。判断与内容全在 Windows 本机 Flask（/kj/api/page/*，
// scripts/kj/pages.py），这里只搬运：所有给出整页内容的表面都调同一个后端函数，
// 免得"每层一份"漂移（CLAUDE.md 2026-08-19）。拿不到时把 error 写进块里，绝不静默
// （references/silent-failure-lessons.md）。
internal static class KjPageClient
{
    // 提示文本里让模型调用的提交工具名：桥这一面叫 kj_page_submit。
    internal const string SubmitToolLabel = "kj_page_submit";

    private static readonly HttpClient Http = new()
    {
        Timeout = TimeSpan.FromSeconds(8),
    };

    private static readonly object TokenLock = new();
    private static bool _tokenLoaded;
    private static string? _token;

    private static string BaseUrl
    {
        get
        {
            string? env = Environment.GetEnvironmentVariable("BW_KJ_WEBAPP_BASE");
            return string.IsNullOrWhiteSpace(env)
                ? "http://127.0.0.1:5000"
                : env.Trim().TrimEnd('/');
        }
    }

    // 与 _server_deploy/mcp_server.py 同一把令牌：env MCP_WEBAPP_TOKEN，
    // 否则 ~/.config/mcp-webapp-token（webapp app.db 的 api_tokens）。
    private static string? Token()
    {
        lock (TokenLock)
        {
            if (_tokenLoaded)
            {
                return _token;
            }
            _tokenLoaded = true;
            string? env = Environment.GetEnvironmentVariable("MCP_WEBAPP_TOKEN");
            if (!string.IsNullOrWhiteSpace(env))
            {
                _token = env.Trim();
                return _token;
            }
            try
            {
                string path = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                    ".config",
                    "mcp-webapp-token");
                if (File.Exists(path))
                {
                    string text = File.ReadAllText(path).Trim();
                    _token = text.Length == 0 ? null : text;
                }
            }
            catch (Exception)
            {
                _token = null;
            }
            return _token;
        }
    }

    // 只给测试用：换令牌 / 换地址后重读。
    internal static void ResetTokenCacheForTests()
    {
        lock (TokenLock)
        {
            _tokenLoaded = false;
            _token = null;
        }
    }

    internal static async Task<JsonObject> BlockAsync(
        string file,
        long page,
        CancellationToken cancellationToken)
    {
        string url = BaseUrl
            + "/kj/api/page/block?file=" + Uri.EscapeDataString(file)
            + "&page=" + page.ToString(System.Globalization.CultureInfo.InvariantCulture)
            + "&tool=" + Uri.EscapeDataString(SubmitToolLabel);
        using HttpRequestMessage request = new(HttpMethod.Get, url);
        return await SendAsync(request, cancellationToken).ConfigureAwait(false);
    }

    internal static async Task<JsonObject> SubmitAsync(
        JsonObject body,
        CancellationToken cancellationToken)
    {
        using HttpRequestMessage request = new(HttpMethod.Post, BaseUrl + "/kj/api/page/submit");
        request.Content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json");
        return await SendAsync(request, cancellationToken).ConfigureAwait(false);
    }

    // 快照有就绪的 PDF/EPUB 来源且有页号 → 附 kjPage。快照里的文字未必是整页，
    // 所以未分析页额外提醒：要按整页提交，先 reader_page_text 取全文。
    internal static async Task AttachToSnapshotAsync(
        JsonObject payload,
        CancellationToken cancellationToken)
    {
        if (payload["contextStatus"] is not JsonValue statusValue
            || !statusValue.TryGetValue<string>(out string? status)
            || status != "ready"
            || payload["activeReading"] is not JsonObject active)
        {
            return;
        }
        string? kind = Str(active["kind"]);
        if (kind is not ("pdf" or "epub"))
        {
            return;
        }
        string? file = Str(active["file"]);
        long? page = Long(active["page"])
            ?? Long((payload["currentPage"] as JsonObject)?["page"]);
        if (string.IsNullOrWhiteSpace(file) || page is not long pageNo || pageNo < 1)
        {
            return;
        }
        JsonObject block = await BlockAsync(file, pageNo, cancellationToken).ConfigureAwait(false);
        if (Str(block["status"]) == "unanalyzed")
        {
            block["note"] = "快照里的文字未必是整页；要按整页提交分析，先 reader_page_text(page) 取全文再交。";
        }
        payload["kjPage"] = block;
    }

    internal static string? Str(JsonNode? node) =>
        node is JsonValue value && value.TryGetValue<string>(out string? text) ? text : null;

    internal static long? Long(JsonNode? node)
    {
        if (node is not JsonValue value)
        {
            return null;
        }
        if (value.TryGetValue<long>(out long number))
        {
            return number;
        }
        if (value.TryGetValue<double>(out double real) && Math.Abs(real - Math.Round(real)) < 1e-9)
        {
            return (long)Math.Round(real);
        }
        if (value.TryGetValue<string>(out string? text)
            && long.TryParse(text, System.Globalization.NumberStyles.Integer,
                System.Globalization.CultureInfo.InvariantCulture, out long parsed))
        {
            return parsed;
        }
        return null;
    }

    private static async Task<JsonObject> SendAsync(
        HttpRequestMessage request,
        CancellationToken cancellationToken)
    {
        string? token = Token();
        if (token is null)
        {
            return Failure(
                "BW_KJ_NO_TOKEN",
                "没有 webapp 令牌（MCP_WEBAPP_TOKEN 或 ~/.config/mcp-webapp-token），KJ 页块不可用");
        }
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        try
        {
            using HttpResponseMessage response = await Http
                .SendAsync(request, cancellationToken)
                .ConfigureAwait(false);
            string text = await response.Content
                .ReadAsStringAsync(cancellationToken)
                .ConfigureAwait(false);
            JsonNode? node = null;
            try
            {
                node = JsonNode.Parse(text);
            }
            catch (JsonException)
            {
                node = null;
            }
            if (node is not JsonObject obj)
            {
                return Failure(
                    "BW_KJ_BAD_RESPONSE",
                    "本机 Flask 返回的不是 JSON（HTTP " + ((int)response.StatusCode) + "）");
            }
            if (!response.IsSuccessStatusCode && obj["code"] is null)
            {
                obj["code"] = "HTTP_" + ((int)response.StatusCode);
            }
            if (!response.IsSuccessStatusCode && obj["error"] is null)
            {
                obj["error"] = "本机 Flask 返回 HTTP " + ((int)response.StatusCode);
            }
            obj.Remove("ok");   // 给模型的块里 ok 没信息量；失败时 error / code 已在
            return obj;
        }
        catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
        {
            return Failure("BW_KJ_TIMEOUT", "本机 Flask 超时，KJ 页块暂不可用");
        }
        catch (HttpRequestException exception)
        {
            return Failure("BW_KJ_UNREACHABLE", "本机 Flask 不可达：" + exception.Message);
        }
    }

    private static JsonObject Failure(string code, string message) => new()
    {
        ["error"] = message,
        ["code"] = code,
    };
}
