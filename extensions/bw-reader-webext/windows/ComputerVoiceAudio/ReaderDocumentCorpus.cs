using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace BwReader.ComputerVoiceAudio;

internal sealed record ReaderDocumentCorpusEntry(
    string SourceInstanceId,
    string DocumentKey,
    string Url,
    string? Title,
    string ContentRevision,
    string Text,
    bool Truncated,
    long ObservedAtEpochMilliseconds);

internal sealed class ReaderDocumentCorpusStore
{
    internal const string DocumentContract = "reader-document/1";
    internal const string CorpusContract = "reader-document-corpus/1";
    internal const string CorpusFileName = "reader-document-corpus.json";
    internal const int MaximumTextCharacters = 256 * 1024;

    // System.Text.Json may escape non-ASCII characters, so a valid 256 Ki
    // character CJK document needs more than its raw UTF-8 byte count on disk.
    private const int MaximumCorpusBytes = 2 * 1024 * 1024;
    private static readonly UTF8Encoding Utf8WithoutBom = new(
        encoderShouldEmitUTF8Identifier: false);

    private readonly string _path;
    private readonly Func<DateTimeOffset> _utcNow;
    private readonly SemaphoreSlim _gate = new(1, 1);

    internal ReaderDocumentCorpusStore(
        string path,
        Func<DateTimeOffset>? utcNow = null)
    {
        if (!Path.IsPathFullyQualified(path))
        {
            throw new ArgumentException(
                "Reader document corpus path must be absolute",
                nameof(path));
        }
        _path = Path.GetFullPath(path);
        _utcNow = utcNow ?? (() => DateTimeOffset.UtcNow);
    }

    internal string PathValue => _path;

    internal async Task SaveAsync(
        JsonElement value,
        CancellationToken cancellationToken)
    {
        ReaderDocumentCorpusEntry entry = Validate(value);
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            string? directory = Path.GetDirectoryName(_path);
            if (string.IsNullOrEmpty(directory))
            {
                throw Invalid("Reader 全文缓存目录无效");
            }
            Directory.CreateDirectory(directory);
            JsonObject root = new()
            {
                ["contract"] = CorpusContract,
                ["updatedAtUtc"] = _utcNow().ToString("O"),
                ["document"] = ToJson(entry),
            };
            byte[] bytes = Utf8WithoutBom.GetBytes(
                root.ToJsonString(DirectBridgeContract.JsonOptions));
            if (bytes.Length is <= 0 or > MaximumCorpusBytes)
            {
                throw Invalid("Reader 全文缓存超过大小上限");
            }

            string temporaryPath = _path + ".tmp-"
                + Guid.NewGuid().ToString("N");
            try
            {
                await using (FileStream stream = new(
                    temporaryPath,
                    FileMode.CreateNew,
                    FileAccess.Write,
                    FileShare.None,
                    bufferSize: 16 * 1024,
                    options:
                        FileOptions.Asynchronous
                        | FileOptions.WriteThrough))
                {
                    await stream.WriteAsync(bytes, cancellationToken)
                        .ConfigureAwait(false);
                    await stream.FlushAsync(cancellationToken)
                        .ConfigureAwait(false);
                    stream.Flush(flushToDisk: true);
                }
                _ = await ReadFileAsync(
                    temporaryPath,
                    cancellationToken).ConfigureAwait(false)
                    ?? throw Invalid("Reader 全文缓存写后校验失败");
                if (File.Exists(_path))
                {
                    File.Replace(
                        temporaryPath,
                        _path,
                        destinationBackupFileName: null,
                        ignoreMetadataErrors: true);
                }
                else
                {
                    File.Move(temporaryPath, _path);
                }
            }
            finally
            {
                try
                {
                    File.Delete(temporaryPath);
                }
                catch
                {
                }
            }
        }
        finally
        {
            _gate.Release();
        }
    }

    internal async Task<ReaderDocumentCorpusEntry?> ReadAsync(
        CancellationToken cancellationToken)
    {
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            return await ReadFileAsync(_path, cancellationToken)
                .ConfigureAwait(false);
        }
        finally
        {
            _gate.Release();
        }
    }

    internal static ReaderDocumentCorpusEntry Validate(JsonElement value)
    {
        if (value.ValueKind != JsonValueKind.Object)
        {
            throw Invalid("Reader 全文必须是对象");
        }
        DirectJsonValidation.RequireNoDuplicateKeys(value);
        string[] fields =
        {
            "contract",
            "sourceInstanceId",
            "documentKey",
            "url",
            "title",
            "contentRevision",
            "text",
            "truncated",
            "observedAtEpochMs",
        };
        HashSet<string> actual = value.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        if (!actual.SetEquals(fields))
        {
            throw Invalid("Reader 全文字段不匹配");
        }
        if (
            value.GetProperty("contract").ValueKind
                != JsonValueKind.String
            || value.GetProperty("contract").GetString()
                != DocumentContract
            || !TrySafeId(
                value.GetProperty("sourceInstanceId"),
                160,
                out string sourceInstanceId)
            || !TryHttpUrl(
                value.GetProperty("documentKey"),
                out string documentKey)
            || !TryHttpUrl(value.GetProperty("url"), out string url)
            || !TryRevision(
                value.GetProperty("contentRevision"),
                out string contentRevision)
            || value.GetProperty("text").ValueKind
                != JsonValueKind.String
            || value.GetProperty("text").GetString() is not string text
            || text.Length > MaximumTextCharacters
            || text.Any(character =>
                char.IsControl(character)
                && character is not ('\r' or '\n' or '\t'))
            || value.GetProperty("truncated").ValueKind
                is not (JsonValueKind.True or JsonValueKind.False)
            || !value.GetProperty("observedAtEpochMs")
                .TryGetInt64(out long observedAt)
            || observedAt is < 1 or > 9_007_199_254_740_991
        )
        {
            throw Invalid("Reader 全文内容无效");
        }
        JsonElement titleValue = value.GetProperty("title");
        string? title = titleValue.ValueKind switch
        {
            JsonValueKind.Null => null,
            JsonValueKind.String => titleValue.GetString(),
            _ => throw Invalid("Reader 全文标题无效"),
        };
        if (
            title is { Length: > 1024 }
            || (title is not null && title.Any(char.IsControl))
        )
        {
            throw Invalid("Reader 全文标题无效");
        }
        string normalized = NormalizeText(text);
        bool truncated = value.GetProperty("truncated").GetBoolean();
        if (
            !truncated
            && !string.Equals(
                ComputeRevision(normalized),
                contentRevision,
                StringComparison.Ordinal)
        )
        {
            throw Invalid("Reader 全文摘要与正文不一致");
        }
        return new ReaderDocumentCorpusEntry(
            sourceInstanceId,
            documentKey,
            url,
            title,
            contentRevision,
            normalized,
            truncated,
            observedAt);
    }

    internal static string ComputeRevision(string text)
    {
        byte[] digest = SHA256.HashData(
            Utf8WithoutBom.GetBytes(NormalizeText(text)));
        return Convert.ToHexString(digest).ToLowerInvariant();
    }

    private async Task<ReaderDocumentCorpusEntry?> ReadFileAsync(
        string path,
        CancellationToken cancellationToken)
    {
        FileInfo info = new(path);
        if (!info.Exists)
        {
            return null;
        }
        if (info.Length is <= 0 or > MaximumCorpusBytes)
        {
            throw Invalid("Reader 全文缓存文件无效");
        }
        await using FileStream stream = new(
            path,
            FileMode.Open,
            FileAccess.Read,
            FileShare.ReadWrite | FileShare.Delete,
            bufferSize: 16 * 1024,
            options:
                FileOptions.Asynchronous
                | FileOptions.SequentialScan);
        using JsonDocument document = await JsonDocument.ParseAsync(
            stream,
            new JsonDocumentOptions
            {
                AllowTrailingCommas = false,
                CommentHandling = JsonCommentHandling.Disallow,
                MaxDepth = 16,
            },
            cancellationToken).ConfigureAwait(false);
        JsonElement root = document.RootElement;
        if (
            root.ValueKind != JsonValueKind.Object
            || root.GetProperty("contract").GetString()
                != CorpusContract
            || !root.TryGetProperty("document", out JsonElement value)
        )
        {
            throw Invalid("Reader 全文缓存合同无效");
        }
        return Validate(value);
    }

    private static JsonObject ToJson(ReaderDocumentCorpusEntry entry) =>
        new()
        {
            ["contract"] = DocumentContract,
            ["sourceInstanceId"] = entry.SourceInstanceId,
            ["documentKey"] = entry.DocumentKey,
            ["url"] = entry.Url,
            ["title"] = entry.Title,
            ["contentRevision"] = entry.ContentRevision,
            ["text"] = entry.Text,
            ["truncated"] = entry.Truncated,
            ["observedAtEpochMs"] =
                entry.ObservedAtEpochMilliseconds,
        };

    private static bool TrySafeId(
        JsonElement value,
        int maximumLength,
        out string result)
    {
        result = string.Empty;
        if (
            value.ValueKind != JsonValueKind.String
            || value.GetString() is not string text
            || text.Length is < 1
            || text.Length > maximumLength
            || !DirectBridgeContract.IsSafeId(text)
        )
        {
            return false;
        }
        result = text;
        return true;
    }

    private static bool TryHttpUrl(
        JsonElement value,
        out string result)
    {
        result = string.Empty;
        if (
            value.ValueKind != JsonValueKind.String
            || value.GetString() is not string text
            || text.Length is < 1 or > 4096
            || text.Any(char.IsControl)
            || !Uri.TryCreate(text, UriKind.Absolute, out Uri? uri)
            || uri.Scheme is not ("http" or "https")
        )
        {
            return false;
        }
        result = text;
        return true;
    }

    private static bool TryRevision(
        JsonElement value,
        out string result)
    {
        result = string.Empty;
        if (
            value.ValueKind != JsonValueKind.String
            || value.GetString() is not string text
            || text.Length != 64
            || text.Any(character =>
                character is not (
                    >= '0' and <= '9'
                    or >= 'a' and <= 'f'))
        )
        {
            return false;
        }
        result = text;
        return true;
    }

    private static string NormalizeText(string value) =>
        value.Replace("\r\n", "\n", StringComparison.Ordinal)
            .Replace('\r', '\n')
            .Trim();

    private static DirectProtocolException Invalid(string message) =>
        new(
            "BW_READER_DOCUMENT_INVALID",
            message,
            retryable: false);
}
