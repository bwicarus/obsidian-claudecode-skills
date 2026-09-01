using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace BwReader.ComputerVoiceAudio;

/// 卡片图片的 Windows 留底（用户 2026-08-30 拍板的架构）。
///
/// > 反正这些内容都要在 Windows 上留底，为何不在 Windows 上用浏览器
/// > 获取后保存，然后传输到 App。
///
/// 建图片卡的请求本来就物理经过这台机器（语音 AI 在这跑），所以抓取
/// 发生在**创建那一刻、这台机器上**：浏览器级 UA、正常的 DNS/IPv6 ——
/// 设备端直连公网取图的整类故障（2026-08-30 全部卡图排查一天的那类）
/// 从此不存在。App 只从本桥取字节（Tailscale 内网），外链烂掉也不影响
/// 已建的卡。
///
/// ## 纪律
///
/// - **id = sha256(URL) 前 16 位**：同 URL 天然去重，不需要锁注册表
///   来发号；registry.json 只是元数据（原始 URL 的留底就在这里）。
/// - **下载失败不改写**：卡片保留原 URL，走设备端代理的旧路兜底 ——
///   宁可退回旧行为，也不要指向一个不存在的本地资产。
/// - 只收 7 种图片格式、≤16MiB、https、无凭据 —— 跟 App 端代理同一套
///   约束，两边谁先拦到都一样。
internal static class ReaderCardAssetStore
{
    internal const string RoutePrefix = "/reader-card-asset/";
    private const int MaximumBytes = 16 * 1_024 * 1_024;

    private static readonly HttpClient Client = CreateClient();

    private static readonly Dictionary<string, string> ExtensionByType =
        new(StringComparer.Ordinal)
        {
            ["image/avif"] = ".avif",
            ["image/bmp"] = ".bmp",
            ["image/gif"] = ".gif",
            ["image/jpeg"] = ".jpg",
            ["image/png"] = ".png",
            ["image/svg+xml"] = ".svg",
            ["image/webp"] = ".webp",
        };

    private static HttpClient CreateClient()
    {
        HttpClient client = new(new SocketsHttpHandler
        {
            AllowAutoRedirect = true,
            MaxAutomaticRedirections = 5,
        })
        {
            Timeout = TimeSpan.FromSeconds(12),
        };
        // ⚠ 浏览器级 UA —— 正是这套架构的卖点之一。设备端代理自报
        // "native image proxy" 会被部分源站按非浏览器一刀切（实测
        // food.fnr.sndimg.com 对它 403）。
        client.DefaultRequestHeaders.UserAgent.ParseAdd(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            + "AppleWebKit/537.36 (KHTML, like Gecko) "
            + "Chrome/127.0 Safari/537.36");
        client.DefaultRequestHeaders.Accept.ParseAdd(
            "image/avif,image/webp,image/png,image/jpeg,"
            + "image/gif,image/svg+xml;q=0.8,*/*;q=0.1");
        return client;
    }

    internal static string StoreDirectory =>
        Path.Combine(
            Environment.GetFolderPath(
                Environment.SpecialFolder.LocalApplicationData),
            "BWReader",
            "card-assets");

    internal static string AssetID(string url) =>
        Convert.ToHexString(
            SHA256.HashData(Encoding.UTF8.GetBytes(url)))[..16]
            .ToLowerInvariant();

    internal static bool IsValidAssetID(string value) =>
        value.Length == 16
        && value.All(static c => c is >= '0' and <= '9' or >= 'a' and <= 'f');

    /// 找已存的资产文件（id + 任一已知扩展名）。没有 = null。
    internal static string? FindStoredFile(string assetID)
    {
        if (!IsValidAssetID(assetID))
        {
            return null;
        }
        foreach (string extension in ExtensionByType.Values)
        {
            string path = Path.Combine(
                StoreDirectory, assetID + extension);
            if (File.Exists(path))
            {
                return path;
            }
        }
        return null;
    }

    internal static string ContentTypeForFile(string path)
    {
        string extension = Path.GetExtension(path).ToLowerInvariant();
        foreach (KeyValuePair<string, string> pair in ExtensionByType)
        {
            if (pair.Value == extension)
            {
                return pair.Key;
            }
        }
        return "application/octet-stream";
    }

    /// 抓取并留底。成功回资产 id；任何失败回 null（调用方**不得改写**卡）。
    ///
    /// ⚠ 失败也要留痕：写进 registry 的 lastError —— 回填或重试时能看到
    /// "这个源上次为什么没下来"，而不是每次从零猜。
    // ── 同站节流（2026-09-01 实锤：Codex 批量建卡几十张并发抓
    //   wikimedia，把本机 IP 撞进 429，整波留底失败）。同一 host 串行
    //   且间隔 ≥1.5s；收到 429 该 host 冷却 10 分钟内直接快速失败 ——
    //   持续撞只会让封禁续期。
    private static readonly SemaphoreSlim FetchGate = new(1, 1);
    private static readonly Dictionary<string, DateTime> HostCooldown =
        new(StringComparer.OrdinalIgnoreCase);
    private static readonly Dictionary<string, DateTime> HostLastFetch =
        new(StringComparer.OrdinalIgnoreCase);
    private static readonly object ThrottleGate = new();

    internal static async Task<string?> EnsureAsync(
        string url,
        CancellationToken cancellationToken)
    {
        if (!IsFetchableURL(url, out Uri? parsed) || parsed is null)
        {
            return null;
        }
        string id = AssetID(url);
        if (FindStoredFile(id) is not null)
        {
            return id;
        }
        string host = parsed.Host.ToLowerInvariant();
        lock (ThrottleGate)
        {
            if (HostCooldown.TryGetValue(host, out DateTime coolUntil))
            {
                if (coolUntil > DateTime.UtcNow)
                {
                    RecordFailure(id, url, "host-cooldown");
                    return null;
                }
                HostCooldown.Remove(host);
            }
        }
        await FetchGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            TimeSpan wait = TimeSpan.Zero;
            lock (ThrottleGate)
            {
                if (HostLastFetch.TryGetValue(host, out DateTime last))
                {
                    TimeSpan since = DateTime.UtcNow - last;
                    if (since < TimeSpan.FromMilliseconds(1500))
                    {
                        wait = TimeSpan.FromMilliseconds(1500) - since;
                    }
                }
            }
            if (wait > TimeSpan.Zero)
            {
                await Task.Delay(wait, cancellationToken)
                    .ConfigureAwait(false);
            }
            lock (ThrottleGate)
            {
                HostLastFetch[host] = DateTime.UtcNow;
            }
            return await FetchOnceAsync(url, parsed, id, host, cancellationToken)
                .ConfigureAwait(false);
        }
        finally
        {
            FetchGate.Release();
        }
    }

    private static async Task<string?> FetchOnceAsync(
        string url,
        Uri parsed,
        string id,
        string host,
        CancellationToken cancellationToken)
    {
        try
        {
            using HttpResponseMessage response = await Client.GetAsync(
                parsed,
                HttpCompletionOption.ResponseHeadersRead,
                cancellationToken).ConfigureAwait(false);
            if ((int)response.StatusCode != 200)
            {
                if ((int)response.StatusCode == 429)
                {
                    lock (ThrottleGate)
                    {
                        HostCooldown[host] =
                            DateTime.UtcNow + TimeSpan.FromMinutes(10);
                    }
                }
                RecordFailure(id, url, "http-" + (int)response.StatusCode);
                return null;
            }
            string contentType = (response.Content.Headers.ContentType
                ?.MediaType ?? "").ToLowerInvariant();
            if (!ExtensionByType.TryGetValue(
                contentType, out string? extension))
            {
                RecordFailure(id, url, "type-" + contentType);
                return null;
            }
            byte[] body;
            await using (Stream stream = await response.Content
                .ReadAsStreamAsync(cancellationToken).ConfigureAwait(false))
            using (MemoryStream buffer = new())
            {
                byte[] chunk = new byte[64 * 1024];
                while (true)
                {
                    int read = await stream.ReadAsync(
                        chunk, cancellationToken).ConfigureAwait(false);
                    if (read <= 0)
                    {
                        break;
                    }
                    if (buffer.Length + read > MaximumBytes)
                    {
                        RecordFailure(id, url, "too-large");
                        return null;
                    }
                    buffer.Write(chunk, 0, read);
                }
                body = buffer.ToArray();
            }
            if (body.Length < 100)
            {
                RecordFailure(id, url, "empty");
                return null;
            }
            Directory.CreateDirectory(StoreDirectory);
            string target = Path.Combine(StoreDirectory, id + extension);
            string temporary = target + ".tmp-" + Environment.ProcessId;
            await File.WriteAllBytesAsync(
                temporary, body, cancellationToken).ConfigureAwait(false);
            File.Move(temporary, target, overwrite: true);
            RecordSuccess(id, url, contentType, body.Length);
            return id;
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (Exception exception)
        {
            RecordFailure(
                id, url,
                "transport-" + exception.GetType().Name);
            return null;
        }
    }

    /// 抓取范围：公网 https、无凭据、host 不指向本机/内网名。
    /// URL 来自本机的 AI（可信度尚可），这里挡的是**顺手写错**而不是攻击。
    private static bool IsFetchableURL(string url, out Uri? parsed)
    {
        parsed = null;
        if (string.IsNullOrWhiteSpace(url) || url.Length > 4096)
        {
            return false;
        }
        if (!Uri.TryCreate(url, UriKind.Absolute, out Uri? uri)
            || uri.Scheme != Uri.UriSchemeHttps
            || !string.IsNullOrEmpty(uri.UserInfo))
        {
            return false;
        }
        string host = uri.Host.ToLowerInvariant().TrimEnd('.');
        if (host.Length == 0
            || host == "localhost"
            || host.EndsWith(".local", StringComparison.Ordinal)
            || host.EndsWith(".internal", StringComparison.Ordinal)
            || host.EndsWith(".ts.net", StringComparison.Ordinal)
            || System.Net.IPAddress.TryParse(host, out _))
        {
            // 字面 IP 一律不抓（公网图片没有理由用裸 IP）；.ts.net 不抓 ——
            // 抓自己等于把内网服务的字节变成"图"。
            return false;
        }
        parsed = uri;
        return true;
    }

    private static readonly object RegistryGate = new();

    private static void RecordSuccess(
        string id, string url, string contentType, int bytes) =>
        UpdateRegistry(id, entry =>
        {
            entry["url"] = url;
            entry["contentType"] = contentType;
            entry["bytes"] = bytes;
            entry["fetchedAtUtcMs"] =
                DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
            entry.Remove("lastError");
        });

    private static void RecordFailure(string id, string url, string code) =>
        UpdateRegistry(id, entry =>
        {
            entry["url"] = url;
            entry["lastError"] = code;
            entry["lastErrorAtUtcMs"] =
                DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        });

    private static void UpdateRegistry(
        string id,
        Action<Dictionary<string, object>> mutate)
    {
        try
        {
            lock (RegistryGate)
            {
                string path = Path.Combine(StoreDirectory, "registry.json");
                Dictionary<string, Dictionary<string, object>> registry = new(
                    StringComparer.Ordinal);
                if (File.Exists(path))
                {
                    using JsonDocument document = JsonDocument.Parse(
                        File.ReadAllText(path));
                    foreach (JsonProperty one in
                        document.RootElement.EnumerateObject())
                    {
                        Dictionary<string, object> entry = new(
                            StringComparer.Ordinal);
                        foreach (JsonProperty field in
                            one.Value.EnumerateObject())
                        {
                            entry[field.Name] = field.Value.ValueKind switch
                            {
                                JsonValueKind.Number =>
                                    field.Value.GetInt64(),
                                _ => field.Value.ToString(),
                            };
                        }
                        registry[one.Name] = entry;
                    }
                }
                if (!registry.TryGetValue(
                    id, out Dictionary<string, object>? target))
                {
                    target = new Dictionary<string, object>(
                        StringComparer.Ordinal);
                    registry[id] = target;
                }
                mutate(target);
                Directory.CreateDirectory(StoreDirectory);
                string temporary =
                    path + ".tmp-" + Environment.ProcessId;
                File.WriteAllText(
                    temporary,
                    JsonSerializer.Serialize(registry));
                File.Move(temporary, path, overwrite: true);
            }
        }
        catch (Exception)
        {
            // 注册表只是元数据留底，坏了不该拦住图片本身的链路。
        }
    }
}
