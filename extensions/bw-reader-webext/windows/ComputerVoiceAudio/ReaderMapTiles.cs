using System.Text.Json;

namespace BwReader.ComputerVoiceAudio;

/// 谷歌地图瓦片代取（2026-08-26 用户拍板：内嵌地图要用谷歌的样式）。
///
/// 为什么由桥代取而不是设备直连：官方 Map Tiles API 的每个瓦片请求都要
/// 带 `session` **和** `key`。让设备端拼这个 URL 就等于把密钥发到每个
/// 表面、写进每条瓦片请求。桥代取之后，设备端只看到
/// `/map/tile?z=&x=&y=`，凭据一步都不出这台机器。
///
/// ⚠ 不用那个人人都在抄的 `mt0.google.com/vt/...` 端点：它是未公开的，
/// 直接用违反服务条款。官方路径就是先 createSession 再取 2dtiles。
///
/// 降级：拿不到密钥/建不了会话/取图失败时返回 503（**不是**空图片），
/// 前端据此退回 OpenStreetMap —— 地图变个样子，但不会变成一片空白。
internal sealed class ReaderMapTiles
{
    private static readonly TimeSpan SessionSafetyMargin =
        TimeSpan.FromHours(6);
    private static readonly HttpClient Http = new()
    {
        Timeout = TimeSpan.FromSeconds(12),
    };

    private readonly object _gate = new();
    private string? _session;
    private DateTimeOffset _sessionExpiry = DateTimeOffset.MinValue;
    private string? _lastError;

    /// 最近一次失败原因（进日志用；没有失败时为 null）。
    internal string? LastError
    {
        get { lock (_gate) { return _lastError; } }
    }

    private static string? ReadApiKey()
    {
        try
        {
            string path = System.IO.Path.Combine(
                Environment.GetFolderPath(
                    Environment.SpecialFolder.UserProfile),
                ".config",
                "gcp-vision-key");
            if (!File.Exists(path))
            {
                return null;
            }
            string key = File.ReadAllText(path).Trim();
            return key.Length is > 20 and < 200 ? key : null;
        }
        catch (Exception exception) when (
            exception is IOException or UnauthorizedAccessException)
        {
            return null;
        }
    }

    private async Task<string?> SessionAsync(
        string apiKey,
        CancellationToken cancellationToken)
    {
        lock (_gate)
        {
            if (_session is not null
                && DateTimeOffset.UtcNow < _sessionExpiry)
            {
                return _session;
            }
        }
        using HttpRequestMessage request = new(
            HttpMethod.Post,
            "https://tile.googleapis.com/v1/createSession?key=" + apiKey)
        {
            Content = new StringContent(
                "{\"mapType\":\"roadmap\",\"language\":\"ja-JP\","
                    + "\"region\":\"JP\"}",
                System.Text.Encoding.UTF8,
                "application/json"),
        };
        using HttpResponseMessage response = await Http
            .SendAsync(request, cancellationToken).ConfigureAwait(false);
        if (!response.IsSuccessStatusCode)
        {
            lock (_gate)
            {
                _lastError = "createSession HTTP "
                    + (int)response.StatusCode;
            }
            return null;
        }
        string body = await response.Content
            .ReadAsStringAsync(cancellationToken).ConfigureAwait(false);
        using JsonDocument parsed = JsonDocument.Parse(body);
        if (!parsed.RootElement.TryGetProperty(
                "session", out JsonElement sessionValue)
            || sessionValue.GetString() is not string session
            || session.Length == 0)
        {
            lock (_gate) { _lastError = "createSession 响应无 session"; }
            return null;
        }
        // 官方给的是 Unix 秒的过期时刻；提前 6 小时换新，别等它当场失效。
        DateTimeOffset expiry = DateTimeOffset.UtcNow.AddDays(7);
        if (parsed.RootElement.TryGetProperty(
                "expiry", out JsonElement expiryValue)
            && long.TryParse(expiryValue.GetString(), out long expirySeconds))
        {
            expiry = DateTimeOffset.FromUnixTimeSeconds(expirySeconds)
                - SessionSafetyMargin;
        }
        lock (_gate)
        {
            _session = session;
            _sessionExpiry = expiry;
            _lastError = null;
        }
        return session;
    }

    /// 取一张瓦片。失败返回 null —— 调用方要回 503 让前端退回 OSM。
    internal async Task<byte[]?> TileAsync(
        int zoom,
        int x,
        int y,
        CancellationToken cancellationToken)
    {
        if (zoom is < 0 or > 22) { return null; }
        int span = 1 << zoom;
        if (x < 0 || x >= span || y < 0 || y >= span) { return null; }
        string? apiKey = ReadApiKey();
        if (apiKey is null)
        {
            lock (_gate) { _lastError = "找不到 gcp-vision-key"; }
            return null;
        }
        string? session;
        try
        {
            session = await SessionAsync(apiKey, cancellationToken)
                .ConfigureAwait(false);
        }
        catch (Exception exception) when (
            exception is HttpRequestException or TaskCanceledException
                or JsonException)
        {
            lock (_gate) { _lastError = exception.GetType().Name; }
            return null;
        }
        if (session is null) { return null; }
        string url = "https://tile.googleapis.com/v1/2dtiles/"
            + zoom + "/" + x + "/" + y
            + "?session=" + session + "&key=" + apiKey;
        try
        {
            using HttpResponseMessage response = await Http
                .GetAsync(url, cancellationToken).ConfigureAwait(false);
            if (!response.IsSuccessStatusCode)
            {
                lock (_gate)
                {
                    // 会话过期时官方回 4xx —— 清掉本地缓存，下一张自动重建。
                    if ((int)response.StatusCode is 401 or 403 or 404)
                    {
                        _session = null;
                    }
                    _lastError = "tile HTTP " + (int)response.StatusCode;
                }
                return null;
            }
            return await response.Content
                .ReadAsByteArrayAsync(cancellationToken)
                .ConfigureAwait(false);
        }
        catch (Exception exception) when (
            exception is HttpRequestException or TaskCanceledException)
        {
            lock (_gate) { _lastError = exception.GetType().Name; }
            return null;
        }
    }
}
