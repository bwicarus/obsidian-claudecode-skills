using System.Security.Cryptography;
using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.AspNetCore.Http;

namespace BwReader.ComputerVoiceAudio;

// Originals are immutable. A retry with the same id must carry the same bytes;
// a lost upload reply must never create a second attachment or overwrite one.
internal static class ReaderAssistantAttachmentStore
{
    internal const long MaximumBytes = 64L * 1024 * 1024;
    internal static string RootDirectory => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "BWReader", "assistant-attachments");
    private static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web);
    private static readonly SemaphoreSlim CommitGate = new(1, 1);
    internal sealed record Entry(string Id, string Name, string Mime, long Bytes, string Sha256, string StoredName);

    internal static bool ValidId(string? id) => id is not null && Regex.IsMatch(id, "\\A[a-f0-9]{32}\\z");
    private static string DirectoryFor(string id)
    {
        if (!ValidId(id)) throw new InvalidDataException("附件编号无效");
        return Path.Combine(RootDirectory, id);
    }

    internal static Entry Read(string id)
    {
        string directory = DirectoryFor(id);
        var entry = JsonSerializer.Deserialize<Entry>(File.ReadAllText(Path.Combine(directory, "metadata.json")), Json)
            ?? throw new InvalidDataException("附件记录无效");
        if (entry.Id != id || Path.GetFileName(entry.StoredName) != entry.StoredName ||
            !entry.StoredName.StartsWith("original", StringComparison.Ordinal) ||
            !File.Exists(Path.Combine(directory, entry.StoredName))) throw new InvalidDataException("附件记录不完整");
        return entry;
    }

    internal static string FilePath(Entry entry) => Path.Combine(DirectoryFor(entry.Id), entry.StoredName);
    internal static string PreviewPath(string id) => Path.Combine(DirectoryFor(id), "preview.jpg");
    internal static object Public(Entry entry) => new { entry.Id, entry.Name, entry.Mime, entry.Bytes, entry.Sha256,
        downloadPath = "/assistant-attachments/file/" + entry.Id };

    private static async Task<(long Bytes, string Sha)> CopyAsync(Stream source, string target, long limit, CancellationToken token)
    {
        using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        await using var output = new FileStream(target, FileMode.CreateNew, FileAccess.Write, FileShare.None, 65536, true);
        byte[] buffer = new byte[65536]; long total = 0;
        while (true)
        {
            int count = await source.ReadAsync(buffer, token).ConfigureAwait(false);
            if (count == 0) break;
            total += count;
            if (total > limit) throw new InvalidDataException("附件超过大小限制");
            hash.AppendData(buffer, 0, count);
            await output.WriteAsync(buffer.AsMemory(0, count), token).ConfigureAwait(false);
        }
        await output.FlushAsync(token).ConfigureAwait(false);
        return (total, Convert.ToHexString(hash.GetHashAndReset()).ToLowerInvariant());
    }

    internal static async Task<Entry> SaveAsync(string id, string name, string mime, Stream source, CancellationToken token)
    {
        string destination = DirectoryFor(id);
        if (string.IsNullOrWhiteSpace(name) || name.Length > 240 || name.Any(char.IsControl) ||
            name.Contains('/') || name.Contains('\\')) throw new InvalidDataException("附件文件名无效");
        if (mime.Length > 160 || !Regex.IsMatch(mime, "\\A[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+\\z"))
            mime = "application/octet-stream";
        string extension = Path.GetExtension(name).ToLowerInvariant();
        if (!Regex.IsMatch(extension, "\\A\\.[a-z0-9]{1,12}\\z")) extension = ".bin";
        Directory.CreateDirectory(RootDirectory);
        string temporary = Path.Combine(RootDirectory, ".upload-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(temporary);
        try
        {
            var copied = await CopyAsync(source, Path.Combine(temporary, "original" + extension), MaximumBytes, token).ConfigureAwait(false);
            var entry = new Entry(id, name, mime, copied.Bytes, copied.Sha, "original" + extension);
            await File.WriteAllTextAsync(Path.Combine(temporary, "metadata.json"), JsonSerializer.Serialize(entry, Json), token).ConfigureAwait(false);
            await CommitGate.WaitAsync(token).ConfigureAwait(false);
            try
            {
                if (Directory.Exists(destination))
                {
                    var existing = Read(id);
                    if (existing != entry) throw new InvalidDataException("同一附件编号对应了不同内容");
                    return existing;
                }
                Directory.Move(temporary, destination);
                return entry;
            }
            finally { CommitGate.Release(); }
        }
        finally { if (Directory.Exists(temporary)) Directory.Delete(temporary, true); }
    }

    internal static async Task SavePreviewAsync(string id, Stream source, CancellationToken token)
    {
        _ = Read(id);
        string destination = PreviewPath(id), temporary = destination + "." + Guid.NewGuid().ToString("N") + ".part";
        try
        {
            var copied = await CopyAsync(source, temporary, 10L * 1024 * 1024, token).ConfigureAwait(false);
            await using (var input = File.OpenRead(temporary))
            {
                byte[] signature = new byte[3];
                if (await input.ReadAsync(signature, token).ConfigureAwait(false) != 3 ||
                    signature[0] != 0xff || signature[1] != 0xd8 || signature[2] != 0xff)
                    throw new InvalidDataException("图片预览必须是 JPEG");
            }
            await CommitGate.WaitAsync(token).ConfigureAwait(false);
            try
            {
                if (File.Exists(destination))
                {
                    await using var existing = File.OpenRead(destination);
                    string sha = Convert.ToHexString(await SHA256.HashDataAsync(existing, token).ConfigureAwait(false)).ToLowerInvariant();
                    if (sha != copied.Sha) throw new InvalidDataException("图片预览内容冲突");
                    return;
                }
                File.Move(temporary, destination);
            }
            finally { CommitGate.Release(); }
        }
        finally { if (File.Exists(temporary)) File.Delete(temporary); }
    }

    internal static async Task HandleAsync(HttpContext context, CancellationToken serviceToken)
    {
        string id = context.Request.RouteValues["id"]?.ToString() ?? "";
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(serviceToken, context.RequestAborted);
        var token = linked.Token;
        try
        {
            if (HttpMethods.IsGet(context.Request.Method))
            {
                var entry = Read(id);
                context.Response.ContentType = "application/octet-stream";
                context.Response.Headers["X-Content-Type-Options"] = "nosniff";
                context.Response.Headers["Content-Disposition"] = "attachment; filename*=UTF-8''" + Uri.EscapeDataString(entry.Name);
                context.Response.ContentLength = entry.Bytes;
                await context.Response.SendFileAsync(FilePath(entry), token).ConfigureAwait(false);
                return;
            }
            var limit = context.Features.Get<Microsoft.AspNetCore.Http.Features.IHttpMaxRequestBodySizeFeature>();
            if (limit is { IsReadOnly: false }) limit.MaxRequestBodySize = MaximumBytes;
            if (context.Request.Path.StartsWithSegments("/assistant-attachments/preview"))
            {
                await SavePreviewAsync(id, context.Request.Body, token).ConfigureAwait(false);
                await context.Response.WriteAsJsonAsync(new { ok = true }, token).ConfigureAwait(false);
            }
            else
            {
                string name = Uri.UnescapeDataString(context.Request.Headers["X-BW-Attachment-Name"].FirstOrDefault() ?? "");
                string mime = context.Request.Headers["X-BW-Attachment-Type"].FirstOrDefault() ?? "application/octet-stream";
                var entry = await SaveAsync(id, name, mime, context.Request.Body, token).ConfigureAwait(false);
                await context.Response.WriteAsJsonAsync(new { ok = true, attachment = Public(entry) }, token).ConfigureAwait(false);
            }
        }
        catch (Exception error) when (error is InvalidDataException or IOException or UriFormatException or JsonException)
        {
            if (context.Response.HasStarted) throw;
            context.Response.StatusCode = error is FileNotFoundException or DirectoryNotFoundException ? 404 : 400;
            await context.Response.WriteAsJsonAsync(new { ok = false, message = error is InvalidDataException ? error.Message : "附件暂时无法读取或保存" }, token).ConfigureAwait(false);
        }
    }
}
