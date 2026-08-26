using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace BwReader.ComputerVoiceAudio;

// 两节点复制（references/reader-two-node-replication.md）的命令接收端。
//
// 信封 replication-command/1 的**权威定义在 Python**
// （computer-voice-desktop/replication_command_ledger.py 的
// validate_command_envelope）；这里是同一约定的 C# 闸副本 ——
// 多副本登记见 reader-specs/contract-sites.json 的 replication-command-envelope，
// 改任何字段两处必须同步。
//
// 职责边界：这里只做"验形状 + 持久落 spool + ack"。
//   · 幂等/游标/冲突在 Python 账本入账时判（mutationId + 整信封摘要）；
//   · 端点白名单由执行端把守，这里刻意不再抄一份副本；
//   · ack 只有 accepted / rejected 两值 —— accepted 的语义是"已 fsync 落盘"，
//     不是"已应用"。说"applied"就是撒谎：应用发生在账本分发之后。
internal static partial class ReplicationCommandProtocol
{
    internal const string CommandType = "replication-command";
    internal const string ChunkType = "replication-command-chunk";
    internal const string DigestQueryType = "replication-digest-query";
    internal const string NotificationsQueryType =
        "replication-notifications-query";
    internal const string EnvelopeContract = "replication-command/1";
    internal const string DigestsFileName = "replication-digests.json";
    internal const string DigestsViewContract = "replication-digests-view/1";

    // 信封上限是**账本层**的（最大单命令：真实页 blocks 4MB + 转义余量），
    // 与 Python 权威的 MAX_ENVELOPE_BYTES 同值。传输层的 256KiB 单帧硬限
    // 由分片协议（ChunkType，base64 片 + 会话级聚合重组）解决。
    internal const int MaxEnvelopeBytes = 6 * 1024 * 1024;
    internal const int MaxChunkCount = 64;
    internal const int MaxChunkPartChars = 200 * 1024;

    [GeneratedRegex("^(?:native-app|pwa-install|server-node)-v1-[a-f0-9]{32}$")]
    private static partial Regex DeviceIdPattern();

    [GeneratedRegex("^repbook-[a-f0-9]{32}$")]
    private static partial Regex ReplicationBookIdPattern();

    [GeneratedRegex("^mut-v2-[a-f0-9]{32}$")]
    private static partial Regex MutationIdPattern();

    private static readonly string[] EnvelopeKeys =
        ["contract", "deviceId", "replicationBookId", "actor", "op"];

    private static readonly string[] OpKeys =
        ["mutationId", "url", "method", "body"];

    internal static ReplicationCommandEnvelope ValidateEnvelope(
        JsonElement envelope)
    {
        RequireExact(envelope, EnvelopeKeys, "信封");
        if (String(envelope, "contract", 64) != EnvelopeContract)
        {
            throw Invalid("信封 contract 不符");
        }
        string deviceId = String(envelope, "deviceId", 64);
        if (!DeviceIdPattern().IsMatch(deviceId))
        {
            throw Invalid("deviceId 必须是设备族格式（不要用 sourceInstanceId）");
        }
        string bookId = String(envelope, "replicationBookId", 64);
        if (!ReplicationBookIdPattern().IsMatch(bookId))
        {
            throw Invalid("replicationBookId 形状非法");
        }
        string actor = String(envelope, "actor", 16);
        if (actor is not ("user" or "ai" or "system"))
        {
            throw Invalid("actor 必须是 user/ai/system");
        }
        JsonElement op = envelope.GetProperty("op");
        RequireExact(op, OpKeys, "op");
        string mutationId = String(op, "mutationId", 64);
        if (!MutationIdPattern().IsMatch(mutationId))
        {
            throw Invalid("mutationId 形状非法");
        }
        string url = String(op, "url", 512);
        if (
            !url.StartsWith('/')
            || url.StartsWith("//", StringComparison.Ordinal)
            || url.Contains("..", StringComparison.Ordinal)
            || url.Contains('#')
            || url.Any(ch => ch <= ' ' || ch >= (char)0x7F)
        )
        {
            throw Invalid("op.url 必须是无 .. 的站内路径");
        }
        string method = String(op, "method", 8);
        if (method is not ("POST" or "PATCH" or "DELETE"))
        {
            throw Invalid("op.method 必须是 POST/PATCH/DELETE");
        }
        if (op.GetProperty("body").ValueKind != JsonValueKind.Object)
        {
            throw Invalid("op.body 必须是 JSON 对象");
        }
        string raw = envelope.GetRawText();
        if (Encoding.UTF8.GetByteCount(raw) > MaxEnvelopeBytes)
        {
            throw Invalid($"信封超过 {MaxEnvelopeBytes} 字节");
        }
        return new ReplicationCommandEnvelope(
            deviceId,
            bookId,
            actor,
            mutationId,
            url,
            method,
            raw);
    }

    private static void RequireExact(
        JsonElement value,
        string[] fields,
        string label)
    {
        if (value.ValueKind != JsonValueKind.Object)
        {
            throw Invalid($"{label}必须是对象");
        }
        DirectJsonValidation.RequireNoDuplicateKeys(value);
        HashSet<string> actual = value.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        if (!actual.SetEquals(fields))
        {
            throw Invalid($"{label}字段不符合 replication-command/1");
        }
    }

    private static string String(JsonElement root, string name, int max)
    {
        JsonElement value = root.GetProperty(name);
        if (
            value.ValueKind != JsonValueKind.String
            || value.GetString() is not string text
            || text.Length == 0
            || text.Length > max
            || text.Any(char.IsControl)
        )
        {
            throw Invalid($"信封 {name} 无效");
        }
        return text;
    }

    internal static bool IsReplicationBookId(string value) =>
        ReplicationBookIdPattern().IsMatch(value);

    internal static bool IsMutationId(string value) =>
        MutationIdPattern().IsMatch(value);

    // 对账查询（规格 §6）：读 Python 摄取线程每轮导出的摘要文件，
    // 只回被问的那本书。文件缺失 = 还没跑过一轮 → 空视图（不是错误）；
    // 文件损坏必须出声 —— 静默回空会让 App 以为"两端都空、一致"。
    // 通知 tab(2026-08-26 用户拍板:侧边栏通知 tab,Windows 通知经此
    // 下发到 App/扩展)。真值仍是 ReaderPC 的通知表;这里只端 runtime 的
    // open 投影 —— 桥不管状态机,ack/resolve 走复制命令流回 Windows。
    internal static object ReadNotificationsView(string notificationsPath)
    {
        // ⚠ 读不到**绝不能**折成空列表（2026-08-26 对抗式复核抓到的最严重
        // 一条）：这份视图现在驱动的是**破坏性撤销** —— App 与 widget 都
        // 按它算 stale 并 removePendingNotificationRequests / EKReminder
        // remove / AlarmManager.cancel。一次瞬时 IO 错误、或对账循环还没
        // 跑过第一轮，就会把三条"互不重叠"的提醒通道一起抹掉，而回执还是
        // 全绿。姊妹函数 ReadDigestsView 对同样的失败一直是抛错的，这里
        // 曾与自己头顶的注释相反。
        string json;
        try
        {
            FileInfo info = new(notificationsPath);
            if (!info.Exists)
            {
                throw new DirectProtocolException(
                    "BW_REPLICATION_NOTIFICATIONS_UNAVAILABLE",
                    "通知导出尚未生成（对账循环还没跑过一轮）",
                    retryable: true);
            }
            if (info.Length is <= 0 or > 256 * 1024)
            {
                throw new DirectProtocolException(
                    "BW_REPLICATION_NOTIFICATIONS_UNAVAILABLE",
                    "通知导出大小异常：" + info.Length + " 字节",
                    retryable: true);
            }
            json = File.ReadAllText(notificationsPath);
        }
        catch (Exception exception) when (
            exception is IOException or UnauthorizedAccessException)
        {
            throw new DirectProtocolException(
                "BW_REPLICATION_NOTIFICATIONS_UNAVAILABLE",
                "通知导出读取失败：" + exception.Message,
                retryable: true);
        }
        JsonDocument parsed;
        try
        {
            parsed = JsonDocument.Parse(json);
        }
        catch (JsonException exception)
        {
            throw new DirectProtocolException(
                "BW_REPLICATION_NOTIFICATIONS_CORRUPT",
                "通知导出 JSON 损坏：" + exception.Message,
                retryable: true);
        }
        using (parsed)
        {
        return ProjectNotifications(parsed);
        }
    }

    /// 通知条目上的地点绑定 → 值类型投影（不留 JsonElement 引用）。
    /// 字段不全就整条返回 null：半个坐标比没有更糟。
    private static object? ReadPlace(JsonElement item)
    {
        if (!item.TryGetProperty("place", out JsonElement place)
            || place.ValueKind != JsonValueKind.Object
            || !place.TryGetProperty("name", out JsonElement nameValue)
            || nameValue.GetString() is not string name
            || name.Length == 0
            || !place.TryGetProperty("lat", out JsonElement latValue)
            || latValue.ValueKind != JsonValueKind.Number
            || !latValue.TryGetDouble(out double latitude)
            || !place.TryGetProperty("lon", out JsonElement lonValue)
            || lonValue.ValueKind != JsonValueKind.Number
            || !lonValue.TryGetDouble(out double longitude))
        {
            return null;
        }
        string proximity =
            place.TryGetProperty("proximity", out JsonElement proximityValue)
            && proximityValue.GetString() == "leave" ? "leave" : "enter";
        double radius =
            place.TryGetProperty("radiusMeters", out JsonElement radiusValue)
            && radiusValue.ValueKind == JsonValueKind.Number
            && radiusValue.TryGetDouble(out double parsedRadius)
                ? parsedRadius : 200;
        return new
        {
            name,
            lat = latitude,
            lon = longitude,
            proximity,
            radiusMeters = radius,
        };
    }

    private static object ProjectNotifications(JsonDocument parsed)
    {
        if (
            parsed.RootElement.ValueKind != JsonValueKind.Object
            || parsed.RootElement.TryGetProperty(
                "contract", out JsonElement contract) is false
            || contract.GetString() != "reader-notifications/1"
            || !parsed.RootElement.TryGetProperty(
                "items", out JsonElement items)
            || items.ValueKind != JsonValueKind.Array
        )
        {
            throw new DirectProtocolException(
                "BW_REPLICATION_NOTIFICATIONS_CORRUPT",
                "通知导出形状不符合 reader-notifications/1",
                retryable: true);
        }
        // 数据时刻：显式搬（重建不是透传）。消费端靠它区分"桥在但对账
        // 循环已经死了"——否则 widget 会拿"拉取成功时刻"冒充数据新鲜度，
        // 亮着绿灯显示一份不再更新的数据。
        long exportedAtUtcMs =
            parsed.RootElement.TryGetProperty(
                "exportedAtUtcMs", out JsonElement exportedAt)
            && exportedAt.ValueKind == JsonValueKind.Number
            && exportedAt.TryGetInt64(out long exportedMs) ? exportedMs : 0;
        List<(long? Due, int Order, object Payload)> candidates = new();
        int order = 0;
        List<object> projected = new();
        foreach (JsonElement item in items.EnumerateArray())
        {
            if (item.ValueKind != JsonValueKind.Object)
            {
                continue;
            }
            string? id = item.TryGetProperty("id", out JsonElement idValue)
                ? idValue.GetString() : null;
            string? title = item.TryGetProperty(
                "title", out JsonElement titleValue)
                ? titleValue.GetString() : null;
            if (id is null || title is null)
            {
                continue;
            }
            // 到期时刻（行程场景）：投影成设备侧的到点通知与闹钟。
            // ⚠ TryGetInt64 对 Null 元素**抛异常**而不是返回 false，
            // 而我们对"没有到点时刻"的条目导出的正是 JSON null —— 少了
            // 这个 ValueKind 判断，整个端点会 500（实测）。
            long? due = item.TryGetProperty(
                "dueAtUtcMs", out JsonElement itemDue)
                && itemDue.ValueKind == JsonValueKind.Number
                && itemDue.TryGetInt64(out long dueAt)
                ? (long?)dueAt : null;
            candidates.Add((due, order++, new
            {
                id,
                kind = item.TryGetProperty("kind", out JsonElement k)
                    ? (k.GetString() ?? "") : "",
                title,
                body = item.TryGetProperty("body", out JsonElement b)
                    ? (b.GetString() ?? "") : "",
                state = item.TryGetProperty("state", out JsonElement s)
                    ? (s.GetString() ?? "pending") : "pending",
                createdAtUtcMs = item.TryGetProperty(
                    "createdAtUtcMs", out JsonElement c)
                    && c.ValueKind == JsonValueKind.Number
                    && c.TryGetInt64(out long created) ? created : 0,
                dueAtUtcMs = due,
                // 地点绑定：**显式重建成基本类型**。原来这里塞的是
                // Dictionary<string, JsonElement>，而那些 JsonElement 背靠
                // 的 JsonDocument 在序列化之前就已经 Dispose —— 实测直接
                // 500。序列化发生在方法返回之后这件事很容易忘，凡是从
                // JsonDocument 里取出来要往外传的，都得先落成值类型。
                place = ReadPlace(item),
            }));
        }
        // 上限按**紧急度**取，不按文件顺序（文件顺序=创建顺序，原来的
        // `break` 永远丢最新建的那条 —— 恰恰是刚排好的到点提醒）。
        // 生产端 MAX_OPEN=50，这里放到同一水位，正常情况一条都不丢。
        foreach ((long? Due, int Order, object Payload) one in candidates
            .OrderBy(x => x.Due.HasValue ? 0 : 1)
            .ThenBy(x => x.Due ?? long.MaxValue)
            .ThenBy(x => x.Order)
            .Take(50))
        {
            projected.Add(one.Payload);
        }
        // review 摘要（到期/新卡数）搭通知视图的车下发：App 用它喂 iOS
        // 小组件（2026-08-27）。缺失时为 null —— 消费端显示"暂无数据"。
        object? review = null;
        if (
            parsed.RootElement.TryGetProperty(
                "review", out JsonElement reviewValue)
            && reviewValue.ValueKind == JsonValueKind.Object
            && reviewValue.TryGetProperty("due", out JsonElement dueValue)
            && dueValue.ValueKind == JsonValueKind.Number
            && dueValue.TryGetInt64(out long dueCount)
            && reviewValue.TryGetProperty("new", out JsonElement newValue)
            && newValue.ValueKind == JsonValueKind.Number
            && newValue.TryGetInt64(out long newCount)
        )
        {
            review = new
            {
                due = dueCount,
                @new = newCount,
                atMs = reviewValue.TryGetProperty("atMs", out JsonElement at)
                    && at.ValueKind == JsonValueKind.Number
                    && at.TryGetInt64(out long atMs) ? atMs : 0,
            };
        }
        return new
        {
            contract = "reader-notifications/1",
            items = projected.ToArray(),
            review,
            exportedAtUtcMs,
            // 被上限丢掉的条数。静默截断会让"创建成功但设备上没有"
            // 无从查起；宁可多一个恒为 0 的字段。
            dropped = Math.Max(0, candidates.Count - projected.Count),
        };
    }

    internal static object ReadDigestsView(
        string digestsPath,
        string replicationBookId)
    {
        string? raw = null;
        try
        {
            if (File.Exists(digestsPath))
            {
                raw = File.ReadAllText(digestsPath);
            }
        }
        catch (Exception exception) when (
            exception is IOException or UnauthorizedAccessException)
        {
            throw new DirectProtocolException(
                "BW_REPLICATION_DIGESTS_UNAVAILABLE",
                "复制摘要文件读取失败：" + exception.Message,
                retryable: true);
        }
        long generatedAtUtcMs = 0;
        Dictionary<string, object> domains = new(StringComparer.Ordinal);
        if (raw is not null)
        {
            JsonElement root;
            try
            {
                using JsonDocument document = JsonDocument.Parse(raw);
                root = document.RootElement.Clone();
            }
            catch (JsonException exception)
            {
                throw new DirectProtocolException(
                    "BW_REPLICATION_DIGESTS_CORRUPT",
                    "复制摘要文件损坏：" + exception.Message,
                    retryable: true);
            }
            if (
                root.ValueKind != JsonValueKind.Object
                || root.GetProperty("contract").GetString()
                    != "replication-digests/1"
            )
            {
                throw new DirectProtocolException(
                    "BW_REPLICATION_DIGESTS_CORRUPT",
                    "复制摘要文件 contract 不符",
                    retryable: true);
            }
            generatedAtUtcMs = root.GetProperty("atUtcMs").GetInt64();
            if (
                root.TryGetProperty("books", out JsonElement books)
                && books.ValueKind == JsonValueKind.Object
                && books.TryGetProperty(replicationBookId, out JsonElement book)
                && book.ValueKind == JsonValueKind.Object
            )
            {
                foreach (JsonProperty domain in book.EnumerateObject())
                {
                    domains[domain.Name] = new
                    {
                        digest = domain.Value.GetProperty("digest").GetString(),
                        count = domain.Value.GetProperty("count").GetInt64(),
                    };
                }
            }
        }
        return new
        {
            contract = DigestsViewContract,
            replicationBookId,
            generatedAtUtcMs,
            domains,
        };
    }

    private static DirectProtocolException Invalid(string message) =>
        new("BW_REPLICATION_COMMAND_INVALID", message, retryable: false);
}

// 验过形状的信封。RawEnvelopeJson 是原文（不重排字段）——
// 落 spool 必须逐字节保真，账本摘要在 Python 侧按 canonical JSON 算。
internal sealed record ReplicationCommandEnvelope(
    string DeviceId,
    string ReplicationBookId,
    string Actor,
    string MutationId,
    string Url,
    string Method,
    string RawEnvelopeJson);

internal sealed record ReplicationCommandIntakeReceipt(
    string Outcome,
    string SpoolFileName);

// 持久交接点：C# 收到帧 → 验形状 → **fsync 落 spool → 才 ack**。
// 崩在 ack 之前，App 侧 outbox 会重投同 mutationId —— Python 账本按
// mutationId 幂等，重复行无害；崩在落盘之后 ack 之前，同理。
// 所以 spool 行允许重复，绝不允许"ack 了却没落盘"。
//
// 段文件按 UTC 日期滚动（inbox-<yyyyMMdd>.jsonl），C# 只追加；
// 段的回收由 Python 账本做（全部行入账且冷却后删除），两个进程
// 永不同时写同一文件。
// 超帧信封的分片聚合：会话级、一次一组（连接断即弃，App 按
// at-least-once 语义整组重投；重组后的信封走与单帧完全相同的
// 验证 + 落 spool 流程，幂等仍由账本按 mutationId 判）。
internal sealed class ReplicationChunkAssembler
{
    private string? _mutationId;
    private int _total;
    private string?[] _parts = [];
    private int _received;

    internal (string? EnvelopeJson, int Received) Accept(
        string mutationId,
        int seq,
        int total,
        string part)
    {
        if (
            total < 1
            || total > ReplicationCommandProtocol.MaxChunkCount
            || seq < 0
            || seq >= total
            || part.Length == 0
            || part.Length > ReplicationCommandProtocol.MaxChunkPartChars
        )
        {
            throw new DirectProtocolException(
                "BW_REPLICATION_COMMAND_INVALID",
                "分片参数非法");
        }
        if (_mutationId != mutationId || _total != total)
        {
            // 新组开始：旧的未完组直接丢弃（发送端会整组重投）
            _mutationId = mutationId;
            _total = total;
            _parts = new string?[total];
            _received = 0;
        }
        if (_parts[seq] is null)
        {
            _received += 1;
        }
        _parts[seq] = part;
        if (_received < _total)
        {
            return (null, _received);
        }
        long totalChars = 0;
        foreach (string? piece in _parts)
        {
            totalChars += piece!.Length;
        }
        if (totalChars / 4 * 3 > ReplicationCommandProtocol.MaxEnvelopeBytes)
        {
            Reset();
            throw new DirectProtocolException(
                "BW_REPLICATION_COMMAND_INVALID",
                "分片重组超过信封上限");
        }
        string joined = string.Concat(_parts);
        int received = _received;
        Reset();
        byte[] decoded;
        try
        {
            decoded = Convert.FromBase64String(joined);
        }
        catch (FormatException)
        {
            throw new DirectProtocolException(
                "BW_REPLICATION_COMMAND_INVALID",
                "分片 base64 无效");
        }
        return (Encoding.UTF8.GetString(decoded), received);
    }

    private void Reset()
    {
        _mutationId = null;
        _total = 0;
        _parts = [];
        _received = 0;
    }
}

internal sealed class ReplicationCommandSpool
{
    internal const string DirectoryName = "replication-spool";

    private readonly string _directory;
    private readonly Func<DateTimeOffset> _utcNow;
    private readonly SemaphoreSlim _gate = new(1, 1);

    internal ReplicationCommandSpool(
        string directory,
        Func<DateTimeOffset>? utcNow = null)
    {
        _directory = directory;
        _utcNow = utcNow ?? (() => DateTimeOffset.UtcNow);
    }

    internal async Task<ReplicationCommandIntakeReceipt> AcceptAsync(
        ReplicationCommandEnvelope envelope,
        CancellationToken cancellationToken)
    {
        DateTimeOffset now = _utcNow();
        string fileName = $"inbox-{now:yyyyMMdd}.jsonl";
        string line = JsonSerializer.Serialize(new
        {
            contract = "replication-spool-line/1",
            receivedAtUtcMs = now.ToUnixTimeMilliseconds(),
            envelope = JsonSerializer.Deserialize<JsonElement>(
                envelope.RawEnvelopeJson),
        }) + "\n";
        byte[] encoded = Encoding.UTF8.GetBytes(line);
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            Directory.CreateDirectory(_directory);
            await using FileStream stream = new(
                Path.Combine(_directory, fileName),
                FileMode.Append,
                FileAccess.Write,
                FileShare.Read);
            await stream.WriteAsync(encoded, cancellationToken)
                .ConfigureAwait(false);
            // flushToDisk:true 是这个类存在的意义 —— ack 的含义是
            // "断电也不丢"。少了它整条通道退化成"大概率不丢"。
            stream.Flush(flushToDisk: true);
        }
        catch (Exception exception) when (
            exception is IOException or UnauthorizedAccessException)
        {
            throw new DirectProtocolException(
                "BW_REPLICATION_COMMAND_SPOOL_FAILED",
                "复制命令落盘失败：" + exception.Message,
                retryable: true);
        }
        finally
        {
            _gate.Release();
        }
        return new ReplicationCommandIntakeReceipt("accepted", fileName);
    }
}
