using System.Security.Cryptography;
using System.Text.Json;
using Microsoft.AspNetCore.Http;

namespace BwReader.ComputerVoiceAudio;

/// 书库：设备把书传到这台服务器，也从这里取回。
///
/// ## 为什么在这里，而不是 Pi
///
/// CLAUDE.md 2026-08-25：「新的服务端能力一律放 Windows（ReaderPC/桥）——
/// AI 就跑在 Windows，贴数据零传输；不要再给 Pi 建新的数据链路。」
/// 用户 2026-08-28 再次确认，并说明理由：**那边的 AI 要看同一批文件**。
///
/// ## ⚠ 它服务的那条规矩
///
/// > **本地的书必须先上传服务器才能开始使用。**（用户 2026-08-28 拍板）
///
/// 它买到的是一个不变量：**任何一本能用的书，两边都有**。
/// 从服务器下载的书天然满足；本地导入的上传之后也满足 —— 两条路收敛成
/// 同一个状态，那个「两批书各是各的」问题从根上消失，而不是靠事后同步补。
///
/// 所以这个端点的**可靠性直接决定书能不能打开**。它失败时必须说清是什么
/// 失败了，而不是给一个笼统的错误 —— 否则表现就是「书打不开而且不知道
/// 为什么」，而那正是这个项目反复吃亏的形态。
///
/// ## ⚠ 不做的事
///
/// - **不覆盖同名文件**：同名不同内容的书是两本，覆盖等于悄悄毁掉一本。
/// - **不删除任何东西**：这个端点只增不减。删书是另一件事，要另外的确认。
/// - **不解析书的内容**：它只负责存字节。解析是阅读器的事。
internal static class ReaderLibraryStore
{
    /// 书存哪。⚠ 放在 AI 能直接读到的地方 —— 那是把书放在这台机器上的
    /// 全部理由（用户：「windows 上的 ai 需要查看和处理一样的文件」）。
    internal static string RootDirectory
    {
        get
        {
            string local = Environment.GetFolderPath(
                Environment.SpecialFolder.LocalApplicationData);
            return Path.Combine(local, "BWReader", "books");
        }
    }

    /// 单本书的上限。⚠ 不设上限的话，一次坏掉的请求能把磁盘写满，
    /// 而磁盘写满的表现是**别的功能**开始出错 —— 排查会指向完全错误的方向。
    private const long MaximumBytes = 2L * 1024 * 1024 * 1024;

    private static readonly HashSet<string> AllowedExtensions =
        new(StringComparer.OrdinalIgnoreCase)
        {
            ".pdf", ".epub", ".txt", ".md", ".html", ".htm",
        };

    /// 文件名里绝不接受的东西。**这一条是安全边界，不是整洁**：
    /// 放行 `..` 或绝对路径等于让请求方指定往哪写。
    internal static bool IsSafeName(string name)
    {
        if (string.IsNullOrWhiteSpace(name) || name.Length > 180) return false;
        if (name.Contains("..", StringComparison.Ordinal)) return false;
        if (name.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0) return false;
        if (Path.IsPathRooted(name)) return false;
        // 文件名与它自己的裸名必须相等 —— 任何目录分隔都不接受。
        return Path.GetFileName(name) == name
            && AllowedExtensions.Contains(Path.GetExtension(name));
    }

    internal readonly record struct Entry(
        string Name, long Bytes, string Sha256, DateTimeOffset ModifiedUtc);

    internal static IReadOnlyList<Entry> List()
    {
        var root = new DirectoryInfo(RootDirectory);
        if (!root.Exists) return Array.Empty<Entry>();
        var entries = new List<Entry>();
        foreach (FileInfo file in root.EnumerateFiles())
        {
            if (!AllowedExtensions.Contains(file.Extension)) continue;
            entries.Add(new Entry(
                file.Name, file.Length, ShortHash(file.FullName),
                new DateTimeOffset(file.LastWriteTimeUtc, TimeSpan.Zero)));
        }
        entries.Sort((left, right) =>
            string.CompareOrdinal(left.Name, right.Name));
        return entries;
    }

    /// 内容指纹。用来回答「这本我已经有了吗」。
    ///
    /// ⚠ 按**内容**而不是文件名去重：同一本书在两台设备上常常叫不同名字，
    /// 而按名字判断会让同一本书存两遍、或者更糟 —— 把不同的书当成同一本。
    internal static string ShortHash(string path)
    {
        using FileStream stream = File.OpenRead(path);
        using var sha = SHA256.Create();
        return Convert.ToHexString(sha.ComputeHash(stream)).ToLowerInvariant();
    }

    internal sealed record SaveOutcome(
        bool Ok, string Code, string Message, string? Name = null,
        long Bytes = 0, bool Duplicate = false);

    /// 存一本书。
    ///
    /// ⚠ **先写临时文件再改名**：直接往目标文件写，中途断掉会留下一个
    /// 长度不对却看着存在的书 —— 而「存在但读不了」比「不存在」难查得多，
    /// 因为不变量（两边都有）看上去是满足的。
    internal static async Task<SaveOutcome> SaveAsync(
        string name, Stream source, CancellationToken cancellationToken)
    {
        if (!IsSafeName(name))
        {
            return new SaveOutcome(
                false, "BW_LIBRARY_NAME_INVALID",
                "书名不接受（只允许纯文件名，扩展名限 pdf/epub/txt/md/html）");
        }
        Directory.CreateDirectory(RootDirectory);
        string target = Path.Combine(RootDirectory, name);
        string temporary = target + ".part-" + Guid.NewGuid().ToString("N");

        long written = 0;
        try
        {
            await using (FileStream sink = File.Create(temporary))
            {
                byte[] buffer = new byte[128 * 1024];
                while (true)
                {
                    int read = await source
                        .ReadAsync(buffer, cancellationToken)
                        .ConfigureAwait(false);
                    if (read <= 0) break;
                    written += read;
                    if (written > MaximumBytes)
                    {
                        throw new InvalidOperationException("超出单本上限");
                    }
                    await sink.WriteAsync(
                        buffer.AsMemory(0, read), cancellationToken)
                        .ConfigureAwait(false);
                }
            }
            if (written == 0)
            {
                File.Delete(temporary);
                return new SaveOutcome(
                    false, "BW_LIBRARY_EMPTY", "上传内容是空的");
            }

            string incoming = ShortHash(temporary);
            // 已经有一本内容完全相同的 → 不重复存，也**不报错**：
            // 重传同一本书是正常操作（换设备、重装），把它当失败会让人
            // 以为坏了。
            foreach (Entry existing in List())
            {
                if (existing.Sha256 == incoming)
                {
                    File.Delete(temporary);
                    return new SaveOutcome(
                        true, "BW_LIBRARY_DUPLICATE", "这本已经在服务器上了",
                        existing.Name, existing.Bytes, Duplicate: true);
                }
            }
            if (File.Exists(target))
            {
                // 同名但内容不同 —— 是两本书。**绝不覆盖**：覆盖等于悄悄
                // 毁掉一本，而且不变量看上去还是满足的。
                File.Delete(temporary);
                return new SaveOutcome(
                    false, "BW_LIBRARY_NAME_TAKEN",
                    "服务器上已有同名但内容不同的书，请改名后再传",
                    name);
            }
            File.Move(temporary, target);
            return new SaveOutcome(
                true, "BW_LIBRARY_SAVED", "已保存", name, written);
        }
        catch (Exception error)
        {
            try { if (File.Exists(temporary)) File.Delete(temporary); }
            catch { /* 清临时文件失败不该盖住真正的错误 */ }
            return new SaveOutcome(
                false, "BW_LIBRARY_WRITE_FAILED",
                "写入失败：" + error.GetType().Name + "：" + error.Message);
        }
    }

    /// 把一次上传写成响应。
    internal static async Task WriteOutcomeAsync(
        HttpContext context, SaveOutcome outcome, CancellationToken token)
    {
        context.Response.StatusCode = outcome.Ok
            ? StatusCodes.Status200OK
            : StatusCodes.Status400BadRequest;
        await context.Response.WriteAsJsonAsync(
            new
            {
                ok = outcome.Ok,
                code = outcome.Code,
                message = outcome.Message,
                name = outcome.Name,
                bytes = outcome.Bytes,
                duplicate = outcome.Duplicate,
            },
            token).ConfigureAwait(false);
    }

    internal static async Task WriteListAsync(
        HttpContext context, CancellationToken token)
    {
        IReadOnlyList<Entry> entries = List();
        await context.Response.WriteAsJsonAsync(
            new
            {
                ok = true,
                root = RootDirectory,
                books = entries.Select(entry => new
                {
                    name = entry.Name,
                    bytes = entry.Bytes,
                    sha256 = entry.Sha256,
                    modifiedUtcMs = entry.ModifiedUtc.ToUnixTimeMilliseconds(),
                }).ToArray(),
            },
            token).ConfigureAwait(false);
    }
}
