using System.Globalization;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.AspNetCore.Http;

namespace BwReader.ComputerVoiceAudio;

/// 展示板：给「一段时间里要反复看状态」的任务一块分了区的板子。
///
/// ## 用户原话（2026-09-04，逐条不打折）
///
/// > 主要针对的是那些一定时间内多次获取状态的任务，虽然实际发生变化时已经做了
/// > 丰富的通知功能，但是对于每日特定方面的新闻、某个产品的发布、还有一些任务的
/// > 实际运行情况我还是希望能在某个小组件上看到。这个作为一个 ai 可选的通知功能
/// > 就好，它可以申请一个展示板并把内容放入。对于这个组件来说其实就只是一个展示
/// > 内容的分了区的板子，但是 ai 那边需要有明确的说明和格式要求等。这个展示板的
/// > 使用是用户在创建任务时明确说明需要开启的，所以不需要 ai 自行判断是否开启。
/// > 还有就是因为 codex 使用时可能会使用固定的程序，所以我希望这个小组件的接口
/// > 可以为使用方提供方便的查询和更改渠道，比如第一次注册后获得唯一编码然后利用
/// > 该编码就可以更新内容，删除等功能，当然还有可选的自动消除的条件，以及提供
/// > 手动操作的可能。
///
/// 逐条落到实现上：
///
/// - **分区**：`sections[]`，每区一个标题 + 若干行。板子不解释内容、不排版智能，
///   它就是块板子 —— 复杂的判断留给写板子的人。
/// - **唯一编码**：`register` 一次拿 `code`，之后所有改动只认 code。
///   **对同一个 `slug` 幂等** —— 固定程序每次跑都可以重新 register，
///   拿回的是同一个 code，不必自己存（存了就会丢，丢了就没法更新）。
/// - **自动消除**：`autoClear`。两种：`afterHours`（某区多久没更新就撤掉那一区）、
///   `dailyAtLocal`（每天到某个墙钟时刻整块清空，适合"每日新闻"）。
/// - **手动操作**：同一套 op，App 直接调 —— 用户能看、能停、能删。
/// - **开不开由用户说**：`enabled` 是**用户的开关**。AI 不许调 `enable`
///   （能力说明里明写）。桥这边不可能核实任务原文，所以不假装有闸门，
///   改为让它**可见可撤**：谁建的、为什么建（`note`）都在板子上，App 里一眼看到。
///
/// ## 边界（都踩过）
///
/// - 所有尺寸都有上限。没上限的话，一次跑飞的循环能把板子写成几十 MB，
///   而表现会是**别的功能**开始出错（磁盘/序列化），排查指向完全错误的方向。
/// - 自动消除在**每次加载**时结算，不靠定时器。定时器只在进程活着时才跑，
///   而这份数据的读者（小组件）根本不在这个进程里。
/// - 读不到/坏了 → 抛错，**不折成空板子**。空板子会被下游当权威，
///   于是"桥挂了"看起来像"AI 什么都没写"。这是通知视图那条教训的同一形态。
internal static class ReaderDisplayBoard
{
    internal const string BoardPath = "/reader-board/v1";
    /// 渲好的卡片图。**内容寻址**：sha 就是卡片 html 的指纹，
    /// 所以同一个 URL 的内容永不变 → 设备端可以长缓存，改了内容自然换 URL。
    internal const string CardImagePath = "/reader-board/card.png";
    internal const string StoreContract = "reader-display-boards/1";
    private const string StoreFileName = "reader-display-boards.json";

    // 上限。改这里之前先想清楚"超了会怎样"——超了必须是拒绝，不是截断到看不出。
    private const int MaxBoards = 24;
    private const int MaxSections = 12;
    private const int MaxLines = 24;
    private const int MaxLineChars = 200;
    // 卡片(2026-09-05 用户改版):一张卡 = 一个方块 = 一条信息。
    // 12 张已经超过最大号小组件能放下的量;再多只是把 payload 撑大。
    private const int MaxCards = 12;
    private const int MaxCardHtmlChars = 4000;
    private const int MaxCardIdChars = 64;
    private const int MaxCardAltChars = 200;
    private const int MaxTitleChars = 60;
    private const int MaxSlugChars = 64;
    private const int MaxNoteChars = 200;
    private const long MaxStoreBytes = 512 * 1024;
    // 小组件那块地方很小：多给也显示不出来，只会让 payload 变大。
    private const int WidgetSections = 6;
    private const int WidgetLines = 8;

    private static readonly object Gate = new();
    private static string _storeDirectory = string.Empty;

    /// 存到 %LOCALAPPDATA%\BWReader —— 跟 replication_notifications.py 的
    /// default_root 同一处。**不放桥的安装目录**：那里会被整目录原子替换。
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

    private static long NowMs =>
        DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();

    // ── 存储 ────────────────────────────────────────────────────────────

    private static JsonObject LoadStore()
    {
        string path = StorePath;
        FileInfo info = new(path);
        if (!info.Exists)
        {
            return new JsonObject
            {
                ["contract"] = StoreContract,
                ["boards"] = new JsonArray(),
            };
        }
        if (info.Length > MaxStoreBytes)
        {
            throw new DirectProtocolException(
                "BW_BOARD_STORE_CORRUPT",
                "展示板存储超过上限：" + info.Length + " 字节",
                retryable: false);
        }
        JsonNode? parsed;
        try
        {
            parsed = JsonNode.Parse(File.ReadAllText(path));
        }
        catch (Exception exception) when (
            exception is JsonException or IOException
                or UnauthorizedAccessException)
        {
            throw new DirectProtocolException(
                "BW_BOARD_STORE_CORRUPT",
                "展示板存储读取失败：" + exception.Message,
                retryable: true);
        }
        if (parsed is not JsonObject root
            || root["contract"]?.GetValue<string>() != StoreContract
            || root["boards"] is not JsonArray)
        {
            throw new DirectProtocolException(
                "BW_BOARD_STORE_CORRUPT",
                "展示板存储形状不符合 " + StoreContract,
                retryable: false);
        }
        return root;
    }

    private static void SaveStore(JsonObject root)
    {
        Configure();
        string path = StorePath;
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        // ⚠ 不要传自己 new 的 JsonSerializerOptions：这个 exe 的序列化配置里
        //   反射默认是关的，一个没设 TypeInfoResolver 的 options 会让
        //   `ToJsonString(options)` 抛
        //   "JsonSerializerOptions instance must specify a TypeInfoResolver"
        //   —— 而在 HTTP 上的表现是一个**空 500**（2026-09-05 实锤：
        //   section/update 全挂，register 却好的，因为它走的是别的路）。
        //   要缩进就用 Utf8JsonWriter：JsonNode.WriteTo 不需要 resolver。
        string serialized;
        using (MemoryStream buffer = new())
        {
            using (Utf8JsonWriter writer = new(
                buffer, new JsonWriterOptions { Indented = true }))
            {
                root.WriteTo(writer);
            }
            serialized = System.Text.Encoding.UTF8.GetString(buffer.ToArray());
        }
        if (serialized.Length > MaxStoreBytes)
        {
            throw new DirectProtocolException(
                "BW_BOARD_TOO_LARGE",
                "写入后会超过存储上限，已拒绝",
                retryable: false);
        }
        string temporary = path + ".part-" + Environment.ProcessId;
        File.WriteAllText(temporary, serialized);
        File.Move(temporary, path, overwrite: true);
    }

    // ── 自动消除：每次加载结算，不靠定时器 ────────────────────────────

    /// 返回是否改动过（改过就得落盘，否则下次读又要重算一遍）。
    private static bool ApplyAutoClear(JsonArray boards, long nowMs)
    {
        bool changed = false;
        foreach (JsonNode? node in boards)
        {
            if (node is not JsonObject board) continue;
            JsonObject? rule = board["autoClear"] as JsonObject;
            string kind = rule?["kind"]?.GetValue<string>() ?? "never";
            if (kind == "afterHours")
            {
                double hours = ReadNumber(rule?["hours"]) ?? 0;
                if (hours <= 0) continue;
                long cutoff = nowMs - (long)(hours * 3600_000);
                // 卡片模型:按卡过期(一张卡多久没更新就撤那一张)。
                if (board["cards"] is JsonArray cardList)
                {
                    for (int index = cardList.Count - 1; index >= 0; index--)
                    {
                        long cardAt = (cardList[index] as JsonObject)
                            ?["updatedAtMs"]?.GetValue<long>() ?? 0;
                        if (cardAt > 0 && cardAt < cutoff)
                        {
                            cardList.RemoveAt(index);
                            changed = true;
                        }
                    }
                    if (changed) board["updatedAtMs"] = nowMs;
                }
                if (board["sections"] is not JsonArray sections) continue;
                for (int index = sections.Count - 1; index >= 0; index--)
                {
                    long at = (sections[index] as JsonObject)?["updatedAtMs"]
                        ?.GetValue<long>() ?? 0;
                    if (at > 0 && at < cutoff)
                    {
                        sections.RemoveAt(index);
                        changed = true;
                    }
                }
                if (changed) board["updatedAtMs"] = nowMs;
            }
            else if (kind == "dailyAtLocal")
            {
                string hhmm = rule?["hhmm"]?.GetValue<string>() ?? "";
                long boundary = MostRecentLocalBoundaryMs(hhmm, nowMs);
                if (boundary <= 0) continue;
                long clearedAt = board["clearedAtMs"]?.GetValue<long>() ?? 0;
                if (clearedAt >= boundary) continue;
                if (board["sections"] is JsonArray sections
                    && sections.Count > 0)
                {
                    sections.Clear();
                    changed = true;
                }
                if (board["cards"] is JsonArray dailyCards
                    && dailyCards.Count > 0)
                {
                    dailyCards.Clear();
                    changed = true;
                }
                board["clearedAtMs"] = nowMs;
                board["updatedAtMs"] = nowMs;
                changed = true;
            }
        }
        return changed;
    }

    /// `hhmm` 最近一次经过的**本地**墙钟时刻（毫秒 epoch）；格式不对回 0。
    /// ⚠ 用本地时区：用户说"每天 4 点清"说的是他家的 4 点。
    private static long MostRecentLocalBoundaryMs(string hhmm, long nowMs)
    {
        if (hhmm.Length != 5 || hhmm[2] != ':') return 0;
        if (!int.TryParse(
                hhmm.AsSpan(0, 2), NumberStyles.None,
                CultureInfo.InvariantCulture, out int hour)
            || !int.TryParse(
                hhmm.AsSpan(3, 2), NumberStyles.None,
                CultureInfo.InvariantCulture, out int minute)
            || hour > 23 || minute > 59)
        {
            return 0;
        }
        DateTimeOffset now = DateTimeOffset.FromUnixTimeMilliseconds(nowMs)
            .ToLocalTime();
        DateTimeOffset today = new(
            now.Year, now.Month, now.Day, hour, minute, 0, now.Offset);
        DateTimeOffset boundary = today <= now ? today : today.AddDays(-1);
        return boundary.ToUnixTimeMilliseconds();
    }

    // ── 校验 ────────────────────────────────────────────────────────────

    private static string RequireText(
        JsonObject body, string field, int limit, bool required)
    {
        JsonNode? node = body[field];
        if (node is null)
        {
            if (!required) return string.Empty;
            throw new DirectProtocolException(
                "BW_BOARD_FIELD_MISSING", "缺少字段 " + field, retryable: false);
        }
        string value;
        try
        {
            value = node.GetValue<string>();
        }
        catch (Exception)
        {
            throw new DirectProtocolException(
                "BW_BOARD_FIELD_INVALID", field + " 必须是字符串",
                retryable: false);
        }
        value = value.Replace("\r", string.Empty).Trim();
        if (required && value.Length == 0)
        {
            throw new DirectProtocolException(
                "BW_BOARD_FIELD_MISSING", field + " 不能为空", retryable: false);
        }
        if (value.Length > limit)
        {
            throw new DirectProtocolException(
                "BW_BOARD_FIELD_INVALID",
                field + " 超过 " + limit + " 字（收到 " + value.Length + "）",
                retryable: false);
        }
        return value;
    }

    /// JsonNode → double。`GetValue<double>()` 对非数字**抛异常**，
    /// 而这里的输入来自使用方，写错类型是常事：折成 null 由调用方给出
    /// 说得清的错误，比让 ASP.NET 变成一个没线索的 500 好。
    private static double? ReadNumber(JsonNode? node)
    {
        if (node is not JsonValue value) return null;
        if (value.TryGetValue(out double asDouble)) return asDouble;
        if (value.TryGetValue(out long asLong)) return asLong;
        return null;
    }

    /// AI 写的 HTML 入库前先洗一遍。
    ///
    /// ⚠ 渲染端(无头 Chromium)已经关了 JS 和网络,但**不能只靠那一层**:
    ///   同一段 HTML 还会进 App 的 WebView 显示。任何一处把它当可信输入,
    ///   这条链就有一处能执行别人写的字。
    ///   这里只做"结构性"删除(整段扔掉),不做属性级美化 —— 半清洗最危险:
    ///   看起来干净了,实际留了一条路。
    private static readonly string[] ForbiddenTags =
    {
        "script", "iframe", "object", "embed", "link", "meta", "base",
        "form", "input", "button", "textarea", "svg", "math", "video",
        "audio", "source", "track", "canvas", "portal",
    };

    private static string SanitizeHtml(string html)
    {
        string value = html.Replace("\r", string.Empty);
        foreach (string tag in ForbiddenTags)
        {
            value = System.Text.RegularExpressions.Regex.Replace(
                value,
                "<" + tag + "\\b[\\s\\S]*?</" + tag + "\\s*>",
                string.Empty,
                System.Text.RegularExpressions.RegexOptions.IgnoreCase);
            value = System.Text.RegularExpressions.Regex.Replace(
                value,
                "<\\s*/?" + tag + "\\b[^>]*>",
                string.Empty,
                System.Text.RegularExpressions.RegexOptions.IgnoreCase);
        }
        // on* 事件属性(onclick=…, onerror=…):带引号和不带引号两种写法都去掉。
        value = System.Text.RegularExpressions.Regex.Replace(
            value,
            "\\son[a-zA-Z]+\\s*=\\s*(\"[^\"]*\"|'[^']*'|[^\\s>]+)",
            " ",
            System.Text.RegularExpressions.RegexOptions.IgnoreCase);
        // 外链与危险协议:板子是离线渲染的,任何 http(s)/javascript/data 资源都不该出现。
        value = System.Text.RegularExpressions.Regex.Replace(
            value,
            "\\s(src|href|xlink:href|background|poster)\\s*=\\s*(\"[^\"]*\"|'[^']*'|[^\\s>]+)",
            " ",
            System.Text.RegularExpressions.RegexOptions.IgnoreCase);
        value = System.Text.RegularExpressions.Regex.Replace(
            value, "javascript:", "blocked:",
            System.Text.RegularExpressions.RegexOptions.IgnoreCase);
        value = System.Text.RegularExpressions.Regex.Replace(
            value, "@import", "blocked-import",
            System.Text.RegularExpressions.RegexOptions.IgnoreCase);
        return value.Trim();
    }

    private static string NewCardId()
    {
        Span<byte> raw = stackalloc byte[6];
        RandomNumberGenerator.Fill(raw);
        return "c_" + Convert.ToHexString(raw).ToLowerInvariant();
    }

    /// 卡片内容的指纹：渲染端据此判断"这张卡变了没有"，没变就不重渲。
    private static string CardSha(string html)
    {
        byte[] digest = SHA256.HashData(
            System.Text.Encoding.UTF8.GetBytes(html));
        return Convert.ToHexString(digest, 0, 8).ToLowerInvariant();
    }

    private static JsonObject BuildCard(JsonObject item, long nowMs)
    {
        string html = SanitizeHtml(
            RequireText(item, "html", MaxCardHtmlChars, required: true));
        if (html.Length == 0)
        {
            throw new DirectProtocolException(
                "BW_BOARD_FIELD_INVALID",
                "html 洗掉之后是空的（是不是整段都被禁用标签包着？）",
                retryable: false);
        }
        string id = RequireText(item, "id", MaxCardIdChars, required: false);
        return new JsonObject
        {
            ["id"] = id.Length > 0 ? id : NewCardId(),
            ["html"] = html,
            ["alt"] = RequireText(item, "alt", MaxCardAltChars, required: false),
            ["sha"] = CardSha(html),
            ["updatedAtMs"] = nowMs,
        };
    }

    /// 旧的「分区+行」读进来转成卡片，那块示例板不会因为改版变空。
    private static JsonArray CardsOf(JsonObject board, long nowMs)
    {
        if (board["cards"] is JsonArray cards) return cards;
        JsonArray converted = new();
        foreach (JsonObject section in
            (board["sections"] as JsonArray ?? new JsonArray())
                .OfType<JsonObject>())
        {
            string title = section["title"]?.GetValue<string>() ?? "";
            string body = string.Join("<br>",
                (section["lines"] as JsonArray ?? new JsonArray())
                    .Select(line => line?.GetValue<string>() ?? string.Empty)
                    .Where(line => line.Length > 0));
            converted.Add(new JsonObject
            {
                ["id"] = NewCardId(),
                ["html"] =
                    "<div style=\"font:600 15px system-ui;margin-bottom:6px\">"
                    + title + "</div><div style=\"font:13px system-ui;"
                    + "line-height:1.5;opacity:.85\">" + body + "</div>",
                ["alt"] = title,
                ["sha"] = CardSha(title + body),
                ["updatedAtMs"] =
                    section["updatedAtMs"]?.GetValue<long>() ?? nowMs,
            });
        }
        board["cards"] = converted;
        return converted;
    }

    private static string NewCode()
    {
        Span<byte> raw = stackalloc byte[8];
        RandomNumberGenerator.Fill(raw);
        return "bd_" + Convert.ToHexString(raw).ToLowerInvariant();
    }

    private static JsonObject? FindByCode(JsonArray boards, string code) =>
        boards.OfType<JsonObject>().FirstOrDefault(
            board => board["code"]?.GetValue<string>() == code);

    private static JsonObject RequireByCode(JsonArray boards, string code)
    {
        JsonObject? board = FindByCode(boards, code);
        if (board is null)
        {
            throw new DirectProtocolException(
                "BW_BOARD_UNKNOWN_CODE",
                "没有这个编码的展示板：" + code
                + "（先 register，或用 list 看现有的）",
                retryable: false);
        }
        return board;
    }

    private static JsonObject? ParseAutoClear(JsonObject body)
    {
        if (body["autoClear"] is not JsonObject rule) return null;
        string kind = rule["kind"]?.GetValue<string>() ?? "";
        if (kind == "never")
        {
            return new JsonObject { ["kind"] = "never" };
        }
        if (kind == "afterHours")
        {
            double hours = ReadNumber(rule["hours"]) ?? 0;
            if (hours is <= 0 or > 24 * 30)
            {
                throw new DirectProtocolException(
                    "BW_BOARD_FIELD_INVALID",
                    "autoClear.hours 必须在 0 与 720 之间", retryable: false);
            }
            return new JsonObject
            {
                ["kind"] = "afterHours",
                ["hours"] = hours,
            };
        }
        if (kind == "dailyAtLocal")
        {
            string hhmm = rule["hhmm"]?.GetValue<string>() ?? "";
            if (MostRecentLocalBoundaryMs(hhmm, NowMs) <= 0)
            {
                throw new DirectProtocolException(
                    "BW_BOARD_FIELD_INVALID",
                    "autoClear.hhmm 必须是 HH:MM（24 小时制本地时间）",
                    retryable: false);
            }
            return new JsonObject
            {
                ["kind"] = "dailyAtLocal",
                ["hhmm"] = hhmm,
            };
        }
        throw new DirectProtocolException(
            "BW_BOARD_FIELD_INVALID",
            "autoClear.kind 只能是 never / afterHours / dailyAtLocal",
            retryable: false);
    }

    private static JsonArray ParseSections(JsonObject body, long nowMs)
    {
        if (body["sections"] is not JsonArray incoming)
        {
            throw new DirectProtocolException(
                "BW_BOARD_FIELD_MISSING",
                "缺少 sections（分区数组）", retryable: false);
        }
        if (incoming.Count > MaxSections)
        {
            throw new DirectProtocolException(
                "BW_BOARD_FIELD_INVALID",
                "最多 " + MaxSections + " 个分区（收到 " + incoming.Count + "）",
                retryable: false);
        }
        JsonArray sections = new();
        foreach (JsonNode? node in incoming)
        {
            if (node is not JsonObject item)
            {
                throw new DirectProtocolException(
                    "BW_BOARD_FIELD_INVALID", "分区必须是对象",
                    retryable: false);
            }
            sections.Add(BuildSection(item, nowMs));
        }
        return sections;
    }

    private static JsonObject BuildSection(JsonObject item, long nowMs)
    {
        string title = RequireText(item, "title", MaxTitleChars, required: true);
        if (item["lines"] is not JsonArray rawLines)
        {
            throw new DirectProtocolException(
                "BW_BOARD_FIELD_MISSING",
                "分区「" + title + "」缺少 lines", retryable: false);
        }
        if (rawLines.Count > MaxLines)
        {
            throw new DirectProtocolException(
                "BW_BOARD_FIELD_INVALID",
                "分区「" + title + "」最多 " + MaxLines + " 行（收到 "
                + rawLines.Count + "）", retryable: false);
        }
        JsonArray lines = new();
        foreach (JsonNode? line in rawLines)
        {
            string text;
            try
            {
                text = line?.GetValue<string>() ?? string.Empty;
            }
            catch (Exception)
            {
                throw new DirectProtocolException(
                    "BW_BOARD_FIELD_INVALID",
                    "分区「" + title + "」的 lines 只能是字符串",
                    retryable: false);
            }
            text = text.Replace("\r", string.Empty)
                .Replace("\n", " ").Trim();
            if (text.Length == 0) continue;
            if (text.Length > MaxLineChars)
            {
                throw new DirectProtocolException(
                    "BW_BOARD_FIELD_INVALID",
                    "分区「" + title + "」有一行超过 " + MaxLineChars + " 字",
                    retryable: false);
            }
            lines.Add(text);
        }
        return new JsonObject
        {
            ["title"] = title,
            ["lines"] = lines,
            ["updatedAtMs"] = nowMs,
        };
    }

    // ── 操作 ────────────────────────────────────────────────────────────

    /// 一个入口一个 `op`。分成多条路径要在两份路径白名单里各加一条，
    /// 而这些 op 共享同一套校验与落盘 —— 分开只会让"少放行一条"多一次机会。
    internal static JsonObject Execute(JsonObject body)
    {
        string op = RequireText(body, "op", 24, required: true);
        long nowMs = NowMs;
        lock (Gate)
        {
            JsonObject root = LoadStore();
            JsonArray boards = (JsonArray)root["boards"]!;
            bool dirty = ApplyAutoClear(boards, nowMs);
            JsonObject result;
            switch (op)
            {
                case "register":
                    result = OpRegister(boards, body, nowMs, ref dirty);
                    break;
                case "update":
                    result = OpUpdate(boards, body, nowMs, ref dirty);
                    break;
                case "section":
                    result = OpSection(boards, body, nowMs, ref dirty);
                    break;
                case "card":
                    result = OpCard(boards, body, nowMs, ref dirty);
                    break;
                case "cards":
                    result = OpCards(boards, body, nowMs, ref dirty);
                    break;
                case "cardDelete":
                    result = OpCardDelete(boards, body, nowMs, ref dirty);
                    break;
                case "clear":
                    result = OpClear(boards, body, nowMs, ref dirty);
                    break;
                case "delete":
                    result = OpDelete(boards, body, ref dirty);
                    break;
                case "enable":
                    result = OpEnable(boards, body, nowMs, ref dirty);
                    break;
                case "get":
                    result = new JsonObject
                    {
                        ["ok"] = true,
                        ["board"] = RequireByCode(
                            boards,
                            RequireText(body, "code", 40, required: true))
                            .DeepClone(),
                    };
                    break;
                case "list":
                    result = OpList(boards);
                    break;
                default:
                    throw new DirectProtocolException(
                        "BW_BOARD_UNKNOWN_OP",
                        "不认识的 op：" + op
                        + "（register/card/cards/cardDelete/clear/delete"
                        + "/enable/get/list；update/section 是旧的分区式写法）",
                        retryable: false);
            }
            if (dirty) SaveStore(root);
            return result;
        }
    }

    private static JsonObject OpRegister(
        JsonArray boards, JsonObject body, long nowMs, ref bool dirty)
    {
        string slug = RequireText(body, "slug", MaxSlugChars, required: true);
        JsonObject? existing = boards.OfType<JsonObject>().FirstOrDefault(
            board => board["slug"]?.GetValue<string>() == slug);
        if (existing is not null)
        {
            // 幂等：固定程序每次跑都可以 register，拿回同一个 code。
            // 顺便允许它把标题/规则改新（同一块板子换了口径是常事）。
            string retitle = RequireText(
                body, "title", MaxTitleChars, required: false);
            if (retitle.Length > 0
                && existing["title"]?.GetValue<string>() != retitle)
            {
                existing["title"] = retitle;
                existing["updatedAtMs"] = nowMs;
                dirty = true;
            }
            JsonObject? rule = ParseAutoClear(body);
            if (rule is not null)
            {
                existing["autoClear"] = rule;
                existing["updatedAtMs"] = nowMs;
                dirty = true;
            }
            return new JsonObject
            {
                ["ok"] = true,
                ["code"] = existing["code"]?.GetValue<string>(),
                ["created"] = false,
                ["enabled"] = existing["enabled"]?.GetValue<bool>() ?? true,
            };
        }
        if (boards.Count >= MaxBoards)
        {
            throw new DirectProtocolException(
                "BW_BOARD_LIMIT",
                "展示板已达上限 " + MaxBoards + " 块；先删掉不用的",
                retryable: false);
        }
        string code = NewCode();
        JsonObject board = new()
        {
            ["code"] = code,
            ["slug"] = slug,
            ["title"] = RequireText(body, "title", MaxTitleChars, required: true),
            ["note"] = RequireText(body, "note", MaxNoteChars, required: false),
            ["enabled"] = true,
            ["createdAtMs"] = nowMs,
            ["updatedAtMs"] = nowMs,
            ["clearedAtMs"] = 0,
            ["autoClear"] = ParseAutoClear(body)
                ?? new JsonObject { ["kind"] = "never" },
            ["sections"] = new JsonArray(),
        };
        boards.Add(board);
        dirty = true;
        return new JsonObject
        {
            ["ok"] = true,
            ["code"] = code,
            ["created"] = true,
            ["enabled"] = true,
        };
    }

    private static JsonObject OpUpdate(
        JsonArray boards, JsonObject body, long nowMs, ref bool dirty)
    {
        JsonObject board = RequireByCode(
            boards, RequireText(body, "code", 40, required: true));
        string title = RequireText(body, "title", MaxTitleChars, required: false);
        if (title.Length > 0) board["title"] = title;
        JsonObject? rule = ParseAutoClear(body);
        if (rule is not null) board["autoClear"] = rule;
        board["sections"] = ParseSections(body, nowMs);
        board["updatedAtMs"] = nowMs;
        dirty = true;
        return new JsonObject
        {
            ["ok"] = true,
            ["code"] = board["code"]?.GetValue<string>(),
            ["sections"] = ((JsonArray)board["sections"]!).Count,
        };
    }

    private static JsonObject OpSection(
        JsonArray boards, JsonObject body, long nowMs, ref bool dirty)
    {
        JsonObject board = RequireByCode(
            boards, RequireText(body, "code", 40, required: true));
        JsonObject section = BuildSection(body, nowMs);
        string title = section["title"]!.GetValue<string>();
        JsonArray sections = (JsonArray)board["sections"]!;
        int at = -1;
        for (int index = 0; index < sections.Count; index++)
        {
            if ((sections[index] as JsonObject)?["title"]?.GetValue<string>()
                == title)
            {
                at = index;
                break;
            }
        }
        if (at >= 0)
        {
            sections[at] = section;
        }
        else
        {
            if (sections.Count >= MaxSections)
            {
                throw new DirectProtocolException(
                    "BW_BOARD_FIELD_INVALID",
                    "这块板子已有 " + MaxSections + " 个分区；改现有分区或先 clear",
                    retryable: false);
            }
            sections.Add(section);
        }
        board["updatedAtMs"] = nowMs;
        dirty = true;
        return new JsonObject
        {
            ["ok"] = true,
            ["code"] = board["code"]?.GetValue<string>(),
            ["section"] = title,
            ["replaced"] = at >= 0,
        };
    }

    /// 放一张卡（有 id 就替换那一张，没有就新增）。
    ///
    /// 反复刷同一张卡是常态用法：拿同一个 id 再 `card` 一次即可 ——
    /// 这跟旧的「同名分区整区替换」是同一个语义，只是换成了 id。
    private static JsonObject OpCard(
        JsonArray boards, JsonObject body, long nowMs, ref bool dirty)
    {
        JsonObject board = RequireByCode(
            boards, RequireText(body, "code", 40, required: true));
        JsonArray cards = CardsOf(board, nowMs);
        JsonObject card = BuildCard(body, nowMs);
        string id = card["id"]!.GetValue<string>();
        int at = -1;
        for (int index = 0; index < cards.Count; index++)
        {
            if ((cards[index] as JsonObject)?["id"]?.GetValue<string>() == id)
            {
                at = index;
                break;
            }
        }
        if (at >= 0)
        {
            cards[at] = card;
        }
        else
        {
            if (cards.Count >= MaxCards)
            {
                throw new DirectProtocolException(
                    "BW_BOARD_FIELD_INVALID",
                    "这块板子已有 " + MaxCards + " 张卡；改现有的（同 id 再发一次）"
                    + "或先 cardDelete / clear",
                    retryable: false);
            }
            cards.Add(card);
        }
        board["updatedAtMs"] = nowMs;
        dirty = true;
        return new JsonObject
        {
            ["ok"] = true,
            ["code"] = board["code"]?.GetValue<string>(),
            ["id"] = id,
            ["sha"] = card["sha"]?.GetValue<string>(),
            ["replaced"] = at >= 0,
        };
    }

    /// 整块换掉所有卡片。
    private static JsonObject OpCards(
        JsonArray boards, JsonObject body, long nowMs, ref bool dirty)
    {
        JsonObject board = RequireByCode(
            boards, RequireText(body, "code", 40, required: true));
        if (body["cards"] is not JsonArray incoming)
        {
            throw new DirectProtocolException(
                "BW_BOARD_FIELD_MISSING", "缺少 cards（卡片数组）",
                retryable: false);
        }
        if (incoming.Count > MaxCards)
        {
            throw new DirectProtocolException(
                "BW_BOARD_FIELD_INVALID",
                "最多 " + MaxCards + " 张卡（收到 " + incoming.Count + "）",
                retryable: false);
        }
        JsonArray cards = new();
        foreach (JsonNode? node in incoming)
        {
            if (node is not JsonObject item)
            {
                throw new DirectProtocolException(
                    "BW_BOARD_FIELD_INVALID", "卡片必须是对象",
                    retryable: false);
            }
            cards.Add(BuildCard(item, nowMs));
        }
        board["cards"] = cards;
        board.Remove("sections");   // 旧模型不再共存：两份内容会各画各的
        board["updatedAtMs"] = nowMs;
        dirty = true;
        return new JsonObject
        {
            ["ok"] = true,
            ["code"] = board["code"]?.GetValue<string>(),
            ["cards"] = cards.Count,
        };
    }

    /// 删掉一张卡。小组件上每张卡固定的那个删除键走的就是这条。
    private static JsonObject OpCardDelete(
        JsonArray boards, JsonObject body, long nowMs, ref bool dirty)
    {
        JsonObject board = RequireByCode(
            boards, RequireText(body, "code", 40, required: true));
        string id = RequireText(body, "id", MaxCardIdChars, required: true);
        JsonArray cards = CardsOf(board, nowMs);
        for (int index = 0; index < cards.Count; index++)
        {
            if ((cards[index] as JsonObject)?["id"]?.GetValue<string>() == id)
            {
                cards.RemoveAt(index);
                board["updatedAtMs"] = nowMs;
                dirty = true;
                return new JsonObject
                {
                    ["ok"] = true,
                    ["code"] = board["code"]?.GetValue<string>(),
                    ["deleted"] = id,
                    ["cards"] = cards.Count,
                };
            }
        }
        throw new DirectProtocolException(
            "BW_BOARD_UNKNOWN_CARD",
            "这块板子上没有这张卡：" + id, retryable: false);
    }

    private static JsonObject OpClear(
        JsonArray boards, JsonObject body, long nowMs, ref bool dirty)
    {
        JsonObject board = RequireByCode(
            boards, RequireText(body, "code", 40, required: true));
        string only = RequireText(body, "section", MaxTitleChars, required: false);
        // 卡片模型:clear 就是把卡都清掉(不接受按标题清 —— 卡片按 id 删,用 cardDelete)。
        if (only.Length == 0 && board["cards"] is JsonArray cardList)
        {
            int cleared = cardList.Count;
            cardList.Clear();
            board["clearedAtMs"] = nowMs;
            board["updatedAtMs"] = nowMs;
            dirty = true;
            return new JsonObject
            {
                ["ok"] = true,
                ["code"] = board["code"]?.GetValue<string>(),
                ["removed"] = cleared,
            };
        }
        JsonArray sections = (board["sections"] as JsonArray) ?? new JsonArray();
        int removed = 0;
        for (int index = sections.Count - 1; index >= 0; index--)
        {
            if (only.Length > 0
                && (sections[index] as JsonObject)?["title"]?.GetValue<string>()
                    != only)
            {
                continue;
            }
            sections.RemoveAt(index);
            removed++;
        }
        board["clearedAtMs"] = nowMs;
        board["updatedAtMs"] = nowMs;
        dirty = true;
        return new JsonObject
        {
            ["ok"] = true,
            ["code"] = board["code"]?.GetValue<string>(),
            ["removed"] = removed,
        };
    }

    private static JsonObject OpDelete(
        JsonArray boards, JsonObject body, ref bool dirty)
    {
        string code = RequireText(body, "code", 40, required: true);
        for (int index = 0; index < boards.Count; index++)
        {
            if ((boards[index] as JsonObject)?["code"]?.GetValue<string>()
                == code)
            {
                boards.RemoveAt(index);
                dirty = true;
                return new JsonObject { ["ok"] = true, ["deleted"] = code };
            }
        }
        throw new DirectProtocolException(
            "BW_BOARD_UNKNOWN_CODE",
            "没有这个编码的展示板：" + code, retryable: false);
    }

    /// ⚠ 这是**用户的开关**。能力说明里明写 AI 不许调它 ——
    /// 「展示板的使用是用户在创建任务时明确说明需要开启的」。
    private static JsonObject OpEnable(
        JsonArray boards, JsonObject body, long nowMs, ref bool dirty)
    {
        JsonObject board = RequireByCode(
            boards, RequireText(body, "code", 40, required: true));
        bool wanted = body["enabled"] is JsonValue flag
            && flag.TryGetValue(out bool parsed) ? parsed : true;
        board["enabled"] = wanted;
        board["updatedAtMs"] = nowMs;
        dirty = true;
        return new JsonObject
        {
            ["ok"] = true,
            ["code"] = board["code"]?.GetValue<string>(),
            ["enabled"] = wanted,
        };
    }

    private static JsonObject OpList(JsonArray boards)
    {
        JsonArray items = new();
        foreach (JsonObject board in boards.OfType<JsonObject>())
        {
            items.Add(new JsonObject
            {
                ["code"] = board["code"]?.GetValue<string>(),
                ["slug"] = board["slug"]?.GetValue<string>(),
                ["title"] = board["title"]?.GetValue<string>(),
                ["note"] = board["note"]?.GetValue<string>(),
                ["enabled"] = board["enabled"]?.GetValue<bool>() ?? true,
                ["updatedAtMs"] = board["updatedAtMs"]?.GetValue<long>() ?? 0,
                ["autoClear"] = board["autoClear"]?.DeepClone(),
                ["sectionCount"] =
                    (board["sections"] as JsonArray)?.Count ?? 0,
            });
        }
        return new JsonObject { ["ok"] = true, ["boards"] = items };
    }

    // ── 小组件投影 ──────────────────────────────────────────────────────

    /// 给 iOS 小组件的投影：只出 `enabled` 的板子，按最近更新排前面，
    /// 每块裁到小组件真能显示的量。**带上每块的数据时刻** ——
    /// 小组件那边靠它显示"这份是什么时候的"，否则会拿"拉取成功"
    /// 冒充新鲜度，亮着绿灯显示一份早就不动的板子。
    internal static JsonArray ProjectForWidget()
    {
        long nowMs = NowMs;
        lock (Gate)
        {
            JsonObject root = LoadStore();
            JsonArray boards = (JsonArray)root["boards"]!;
            if (ApplyAutoClear(boards, nowMs)) SaveStore(root);
            JsonArray projected = new();
            foreach (JsonObject board in boards.OfType<JsonObject>()
                .Where(board => board["enabled"]?.GetValue<bool>() ?? true)
                .OrderByDescending(
                    board => board["updatedAtMs"]?.GetValue<long>() ?? 0))
            {
                // 卡片:只出 id/alt/sha —— **不出 html**。
                // 小组件渲染不了 HTML(WidgetKit 只有 SwiftUI),它拿的是
                // Windows 渲好的 PNG;html 塞进来只会把每 15 分钟一次的
                // payload 撑大好几倍,而没有任何一个消费者用得上。
                JsonArray cards = new();
                foreach (JsonObject card in CardsOf(board, nowMs)
                    .OfType<JsonObject>().Take(MaxCards))
                {
                    cards.Add(new JsonObject
                    {
                        ["id"] = card["id"]?.GetValue<string>(),
                        ["alt"] = card["alt"]?.GetValue<string>(),
                        ["sha"] = card["sha"]?.GetValue<string>(),
                        ["updatedAtMs"] =
                            card["updatedAtMs"]?.GetValue<long>() ?? 0,
                    });
                }
                projected.Add(new JsonObject
                {
                    ["code"] = board["code"]?.GetValue<string>(),
                    ["slug"] = board["slug"]?.GetValue<string>(),
                    ["title"] = board["title"]?.GetValue<string>(),
                    ["updatedAtMs"] =
                        board["updatedAtMs"]?.GetValue<long>() ?? 0,
                    ["cards"] = cards,
                });
            }
            return projected;
        }
    }

    // ── HTTP ────────────────────────────────────────────────────────────

    /// 端出一张渲好的卡片图。渲染发生在 ReaderPC 那边（Playwright 无头 Chromium），
    /// 这里只负责把文件发出去 —— 桥不跑浏览器。
    ///
    /// 图还没渲出来时回 404 **而不是**占位图：占位图会被设备端长缓存下来，
    /// 于是"渲染慢了一拍"变成"这张卡永远是灰的"。
    internal static async Task WriteCardImageAsync(
        HttpContext context,
        CancellationToken cancellationToken)
    {
        string sha = context.Request.Query["sha"].ToString();
        if (sha.Length != 16 || !sha.All(
                ch => ch is >= '0' and <= '9' or >= 'a' and <= 'f'))
        {
            context.Response.StatusCode = StatusCodes.Status400BadRequest;
            return;
        }
        Configure();
        string path;
        lock (Gate)
        {
            path = Path.Combine(_storeDirectory, "board-cards", sha + ".png");
        }
        byte[] payload;
        try
        {
            payload = await File.ReadAllBytesAsync(path, cancellationToken)
                .ConfigureAwait(false);
        }
        catch (Exception)
        {
            context.Response.StatusCode = StatusCodes.Status404NotFound;
            return;
        }
        context.Response.ContentType = "image/png";
        // 内容寻址 → 永不失效。
        context.Response.Headers["Cache-Control"] =
            "public, max-age=31536000, immutable";
        await context.Response.Body
            .WriteAsync(payload, cancellationToken).ConfigureAwait(false);
    }

    internal static async Task WriteResponseAsync(
        HttpContext context,
        CancellationToken cancellationToken)
    {
        JsonObject? body;
        try
        {
            JsonNode? parsed = await JsonNode.ParseAsync(
                context.Request.Body,
                cancellationToken: cancellationToken).ConfigureAwait(false);
            body = parsed as JsonObject;
        }
        catch (Exception)
        {
            body = null;
        }
        if (body is null)
        {
            context.Response.StatusCode = StatusCodes.Status400BadRequest;
            await context.Response.WriteAsJsonAsync(
                new { ok = false, error = "BW_BOARD_BAD_BODY",
                      detail = "请求体必须是 JSON 对象，至少含 op" },
                cancellationToken).ConfigureAwait(false);
            return;
        }
        JsonObject result;
        try
        {
            result = Execute(body);
        }
        catch (DirectProtocolException exception)
        {
            // 说清是哪一条、错在哪 —— 使用方是程序或 AI，
            // 一句「失败」等于让它去猜（而它会猜错并重试同一个错）。
            context.Response.StatusCode = exception.Retryable
                ? StatusCodes.Status503ServiceUnavailable
                : StatusCodes.Status400BadRequest;
            await context.Response.WriteAsJsonAsync(
                new { ok = false, error = exception.Code,
                      detail = exception.Message },
                cancellationToken).ConfigureAwait(false);
            return;
        }
        catch (Exception exception)
        {
            // ⚠ 兜底必须留痕。没有这一段时 ASP.NET 把未处理异常变成一个
            //   **空 500**：使用方（程序/AI）看到的是"失败"两个字，
            //   而现场没有任何线索 —— 这正是本仓库反复吃到的静默失败形态
            //   （2026-09-05 首次真机调用 section/update 就是空 500，
            //   靠加了这段才看见真正的异常）。
            context.Response.StatusCode =
                StatusCodes.Status500InternalServerError;
            await context.Response.WriteAsJsonAsync(
                new { ok = false, error = "BW_BOARD_CRASH",
                      detail = exception.GetType().Name + ": "
                          + exception.Message },
                cancellationToken).ConfigureAwait(false);
            return;
        }
        context.Response.ContentType = "application/json; charset=utf-8";
        context.Response.Headers["Cache-Control"] = "no-store";
        await context.Response.WriteAsync(
            result.ToJsonString(), cancellationToken).ConfigureAwait(false);
    }
}
