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
