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

    // ── 快照路径上不许有网络等待（2026-09-09 用户：「不要因为获取 kj 信息导致
    //    查看快照变慢」）
    //
    // 原来 reader_context_snapshot 直接 await 这边的 HTTP。实测的代价不是
    // "KJ 算得慢"，而是**等一个没在跑的服务**：Flask 由 ReaderPC 托管，
    // ReaderPC 一关它就没了，而 Windows 上连一个拒连的端口要约 2 秒
    // （2026-09-09 连测三次都是 2050 ms 上下）。于是每一次带书页的快照都白等 2 秒。
    //
    // 所以快照改成**只读缓存**：有就附上，没有就附一句"后台在取"，
    // 网络那一跳挪到后台。熔断另加一层，免得服务不在时后台每次都去撞 2 秒。
    private sealed record Cached(JsonObject Block, DateTimeOffset At);

    private static readonly object CacheLock = new();
    private static readonly Dictionary<string, Cached> Blocks = new(StringComparer.Ordinal);
    private static readonly HashSet<string> InFlight = new(StringComparer.Ordinal);
    private static DateTimeOffset _coolUntil = DateTimeOffset.MinValue;
    private static string _coolReason = string.Empty;

    /// 缓存多久算新鲜。页分析不常变；真正要紧的"未分析→已分析"那一跳由
    /// `SubmitAsync` 成功后直接失效，不靠等 TTL。
    internal static readonly TimeSpan CacheTtl = TimeSpan.FromSeconds(90);
    /// 取不到时冷却多久再试。没有这层的话，服务不在时每次快照都会在后台
    /// 撞一次 2 秒的拒连 —— 快照本身不慢了，但机器被白白占着。
    internal static readonly TimeSpan FailureCooldown = TimeSpan.FromSeconds(60);

    private static string CacheKey(string book, long page) =>
        book + "#" + page.ToString(System.Globalization.CultureInfo.InvariantCulture);

    /// 只给测试用。
    internal static void ResetCacheForTests()
    {
        lock (CacheLock)
        {
            Blocks.Clear();
            InFlight.Clear();
            _coolUntil = DateTimeOffset.MinValue;
            _coolReason = string.Empty;
        }
    }

    private static JsonObject? TryCached(string book, long page)
    {
        lock (CacheLock)
        {
            if (!Blocks.TryGetValue(CacheKey(book, page), out Cached? hit))
            {
                return null;
            }
            if (DateTimeOffset.UtcNow - hit.At > CacheTtl)
            {
                return null;
            }
            // 复制一份：调用方会往块里塞 note，别让它改到缓存。
            return JsonNode.Parse(hit.Block.ToJsonString()) as JsonObject;
        }
    }

    private static void Remember(string book, long page, JsonObject block)
    {
        lock (CacheLock)
        {
            Blocks[CacheKey(book, page)] =
                new Cached(block, DateTimeOffset.UtcNow);
            // 缓存不设上限会随翻页无限长。一本书几百页 × 几本 = 几千条小对象,
            // 不算多,但没有理由让它无界。
            if (Blocks.Count > 400)
            {
                string[] oldest = Blocks
                    .OrderBy(one => one.Value.At)
                    .Take(100)
                    .Select(one => one.Key)
                    .ToArray();
                foreach (string key in oldest) Blocks.Remove(key);
            }
        }
    }

    private static void Forget(string book, long page)
    {
        lock (CacheLock)
        {
            Blocks.Remove(CacheKey(book, page));
        }
    }

    private static bool Cooling(out string reason)
    {
        lock (CacheLock)
        {
            reason = _coolReason;
            return DateTimeOffset.UtcNow < _coolUntil;
        }
    }

    private static void StartCooling(string reason)
    {
        lock (CacheLock)
        {
            _coolUntil = DateTimeOffset.UtcNow + FailureCooldown;
            _coolReason = reason;
        }
    }

    private static void StopCooling()
    {
        lock (CacheLock)
        {
            _coolUntil = DateTimeOffset.MinValue;
            _coolReason = string.Empty;
        }
    }

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

    /// 取一页的块。**缓存优先**；服务不在（熔断中）就立刻返回失败而不是再撞一次
    /// 拒连。reader_page_text 走这条：那一刻模型正要交分析，值得等真实的耗时，
    /// 但不值得等一个已知不在的服务。
    internal static async Task<JsonObject> BlockAsync(
        string file,
        long page,
        CancellationToken cancellationToken)
    {
        JsonObject? cached = TryCached(file, page);
        if (cached is not null)
        {
            return cached;
        }
        if (Cooling(out string why))
        {
            return Failure("BW_KJ_COOLING", "KJ 页块暂时取不到：" + why);
        }
        return await FetchAsync(file, page, cancellationToken).ConfigureAwait(false);
    }

    private static async Task<JsonObject> FetchAsync(
        string file,
        long page,
        CancellationToken cancellationToken)
    {
        string url = BaseUrl
            + "/kj/api/page/block?file=" + Uri.EscapeDataString(file)
            + "&page=" + page.ToString(System.Globalization.CultureInfo.InvariantCulture)
            + "&tool=" + Uri.EscapeDataString(SubmitToolLabel);
        using HttpRequestMessage request = new(HttpMethod.Get, url);
        JsonObject block = await SendAsync(request, cancellationToken)
            .ConfigureAwait(false);
        // 失败的块不缓存：缓存住一次失败等于把一次抖动变成 90 秒的空白。
        // 改成开熔断 —— 冷却期内立刻失败，冷却过了再试一次真请求。
        if (block["error"] is not null || block["ok"] is JsonValue okValue
            && okValue.TryGetValue(out bool ok) && !ok)
        {
            StartCooling(Str(block["error"]) ?? "取 KJ 页块失败");
            return block;
        }
        StopCooling();
        Remember(file, page, block);
        return block;
    }

    internal static async Task<JsonObject> SubmitAsync(
        JsonObject body,
        CancellationToken cancellationToken)
    {
        using HttpRequestMessage request = new(HttpMethod.Post, BaseUrl + "/kj/api/page/submit");
        request.Content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json");
        JsonObject reply = await SendAsync(request, cancellationToken)
            .ConfigureAwait(false);
        // ⚠ 交上去之后这一页从"未分析"变成"已分析" —— 缓存里那份旧块必须当场
        //   作废，不能等 TTL。否则模型刚交完分析，下一次快照还告诉它"这页
        //   没分析过，去交一份"，它就会重复交。
        string? book = Str(body["book"]);
        long? page = Long(body["page"]);
        if (!string.IsNullOrWhiteSpace(book) && page is long pageNo)
        {
            Forget(book, pageNo);
        }
        return reply;
    }

    // 快照有就绪的 PDF/EPUB 来源且有页号 → 附 kjPage。快照里的文字未必是整页，
    // 所以未分析页额外提醒：要按整页提交，先 reader_page_text 取全文。
    //
    // ⚠ **同步、无 await、无网络**（2026-09-09 用户：「不要因为获取 kj 信息导致
    //   查看快照变慢」）。刻意不写成 async Task：签名里没有 Task，调用点就
    //   没有 await 可写，以后想"顺手等一下"得先改签名 —— 那一步足够让人停下来想。
    internal static void AttachToSnapshot(JsonObject payload)
    {
        if (payload["contextStatus"] is not JsonValue statusValue
            || !statusValue.TryGetValue<string>(out string? status)
            || status != "ready"
            || payload["activeReading"] is not JsonObject active)
        {
            return;
        }
        string? kind = Str(active["kind"]);
        if (kind is not ("pdf" or "epub" or "web"))
        {
            return;
        }
        string? file = Str(active["file"]);
        // 网页=单文档，页恒 1；在不在"网页分析范围"由 Flask 按规则表判，out_of_scope 就不附块
        long? page = kind == "web"
            ? 1
            : Long(active["page"]) ?? Long((payload["currentPage"] as JsonObject)?["page"]);
        if (string.IsNullOrWhiteSpace(file) || page is not long pageNo || pageNo < 1)
        {
            return;
        }
        // ⚠ 快照这条路径**只读缓存，不等网络**（2026-09-09 用户定的硬要求）。
        //   没有缓存就说一句"后台在取"，并把那一跳丢到后台。
        string bookKey = BookKeyFor(kind, file);
        JsonObject? block = TryCached(bookKey, pageNo);
        if (block is null)
        {
            RefreshInBackground(bookKey, pageNo);
            if (Cooling(out string why))
            {
                // 服务不在时也要**出声**：静默缺块会让模型以为这页没有 KJ 数据，
                // 而那跟"取不到"是两件事。
                payload["kjPage"] = new JsonObject
                {
                    ["status"] = "unavailable",
                    ["book"] = bookKey,
                    ["page"] = pageNo,
                    ["error"] = why,
                };
            }
            else
            {
                payload["kjPage"] = new JsonObject
                {
                    ["status"] = "pending",
                    ["book"] = bookKey,
                    ["page"] = pageNo,
                    // 措辞刻意不邀请轮询：让它继续做手上的事，别为等这块反复查快照。
                    ["note"] = "本页 KJ 块正在后台取，下一次快照就会带上；不必为此重复查快照。",
                };
            }
            return;
        }
        if (Str(block["status"]) == "out_of_scope")
        {
            return;
        }
        if (Str(block["status"]) == "unanalyzed")
        {
            block["note"] = "快照里的文字未必是整页；要按整页提交分析，先 reader_page_text(page) 取全文再交。";
        }
        payload["kjPage"] = block;
    }

    /// 后台补一次，绝不让调用方等。
    ///
    /// ⚠ 用 `CancellationToken.None` 而不是请求的 token：快照那一刻就返回了，
    ///   拿请求的 token 会让后台取数当场被取消 —— 于是缓存永远填不上，
    ///   而表现只是"快照里那块永远是 pending"，没有一处会报错。
    private static void RefreshInBackground(string book, long page)
    {
        if (Cooling(out _)) return;
        string key = CacheKey(book, page);
        lock (CacheLock)
        {
            // 同一页别并发取好几次：翻页快的时候会叠出一堆同样的请求。
            if (!InFlight.Add(key)) return;
        }
        _ = Task.Run(async () =>
        {
            try
            {
                await FetchAsync(book, page, CancellationToken.None)
                    .ConfigureAwait(false);
            }
            catch (Exception exception)
            {
                StartCooling(exception.Message);
            }
            finally
            {
                lock (CacheLock) { InFlight.Remove(key); }
            }
        });
    }

    // 书键口径与 Flask/侧栏一致：网页（kind=web，或 file 本身是 http(s) URL）→ "web:" + URL；书 → 原样。
    internal static string BookKeyFor(string? kind, string file)
    {
        if (file.StartsWith("web:", StringComparison.Ordinal))
        {
            return file;
        }
        bool isUrl = file.StartsWith("http://", StringComparison.OrdinalIgnoreCase)
            || file.StartsWith("https://", StringComparison.OrdinalIgnoreCase);
        return kind == "web" || isUrl ? "web:" + file : file;
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
