using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace BwReader.ComputerVoiceAudio;

/// 书籍用户状态包的 Windows 枢纽存储（用户 2026-09-01 拍板：跨设备同步
/// 经 Windows，不再指望已出局的 Pi）。
///
/// 第一期语义（整包快照，last-writer-wins）：每台设备把一本书（按内容
/// sha256 寻址 —— 同一 PDF 在不同设备各自导入也会得到同一个键）的
/// user-state **全量包**推上来；每设备各存最新一份，读取时给出**全局
/// 最新**的那份并注明来源设备 —— 拉取方据 deviceId/at 自判要不要导入。
/// 冲突合并（per-collection 向量）留给下一期；单人多设备先后使用的
/// 场景这一期就正确。
internal static class ReaderUserStateStore
{
    internal const long MaximumPackageBytes = 48L * 1024 * 1024;

    private static readonly object Gate = new();

    private static string Directory()
    {
        string root = Path.Combine(
            Environment.GetFolderPath(
                Environment.SpecialFolder.LocalApplicationData),
            "BWReader",
            "library-user-state");
        System.IO.Directory.CreateDirectory(root);
        return root;
    }

    internal static bool IsValidContentSha(string value) =>
        value.Length == 64
        && value.All(static ch => ch is >= '0' and <= '9' or >= 'a' and <= 'f');

    internal static bool IsValidDeviceId(string value) =>
        value.Length is >= 8 and <= 80
        && value.All(static ch =>
            ch is >= '0' and <= '9' or >= 'a' and <= 'z'
                or >= 'A' and <= 'Z' or '-' or '_');

    /// 存一台设备的最新包。返回 (ok, code, message)。
    internal static (bool Ok, string Code, string Message) Save(
        string contentSha,
        string deviceId,
        long atMs,
        JsonObject package)
    {
        if (!IsValidContentSha(contentSha))
        {
            return (false, "BW_USER_STATE_SHA", "contentSha256 无效");
        }
        if (!IsValidDeviceId(deviceId))
        {
            return (false, "BW_USER_STATE_DEVICE", "deviceId 无效");
        }
        if (atMs <= 0)
        {
            return (false, "BW_USER_STATE_AT", "at 无效");
        }
        string body = package.ToJsonString();
        if (body.Length > MaximumPackageBytes)
        {
            return (false, "BW_USER_STATE_TOO_LARGE", "状态包超过 48MiB");
        }
        string path = Path.Combine(
            Directory(), contentSha + "." + deviceId + ".json");
        lock (Gate)
        {
            // 时钟回拨/乱序投递不允许把新包覆盖成旧包。
            if (File.Exists(path))
            {
                try
                {
                    JsonNode? existing = JsonNode.Parse(
                        File.ReadAllText(path));
                    long existingAt =
                        existing?["at"]?.GetValue<long>() ?? 0;
                    if (existingAt >= atMs)
                    {
                        return (true, "BW_USER_STATE_STALE_IGNORED",
                            "已有更新的包，忽略这份旧的");
                    }
                }
                catch (JsonException)
                {
                }
            }
            string temp = path + ".tmp";
            File.WriteAllText(temp, body);
            File.Move(temp, path, overwrite: true);
        }
        return (true, "BW_USER_STATE_SAVED", "已保存");
    }

    /// 全局最新的一份（含来源设备与时间），没有任何包时返回 null。
    internal static JsonObject? Latest(string contentSha)
    {
        if (!IsValidContentSha(contentSha)) return null;
        lock (Gate)
        {
            JsonObject? best = null;
            long bestAt = -1;
            foreach (string path in System.IO.Directory.EnumerateFiles(
                Directory(), contentSha + ".*.json"))
            {
                try
                {
                    if (JsonNode.Parse(File.ReadAllText(path))
                        is not JsonObject one)
                    {
                        continue;
                    }
                    long at = one["at"]?.GetValue<long>() ?? 0;
                    if (at > bestAt)
                    {
                        bestAt = at;
                        best = one;
                    }
                }
                catch (Exception error) when (
                    error is IOException or JsonException)
                {
                }
            }
            return best;
        }
    }
}
