using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace BwReader.ComputerVoiceAudio;

internal sealed class ReaderContextReadLedger
{
    internal const string LedgerContract =
        "reader-context-read-ledger/1";
    internal const string LedgerFileName =
        "reader-context-read-ledger.json";

    private const int MaximumEntries = 2048;
    private const int MaximumLedgerBytes = 1024 * 1024;
    private const string MutexName =
        "Local\\BWReaderContextReadLedgerV1";
    private static readonly UTF8Encoding Utf8WithoutBom = new(
        encoderShouldEmitUTF8Identifier: false);

    private readonly string _path;
    private readonly Func<DateTimeOffset> _utcNow;

    private sealed record LedgerEntry(
        string ThreadId,
        string DocumentKeyHash,
        string ContentRevision,
        string DeliveredAtUtc);

    internal ReaderContextReadLedger(
        string path,
        Func<DateTimeOffset>? utcNow = null)
    {
        if (!Path.IsPathFullyQualified(path))
        {
            throw new ArgumentException(
                "Reader context ledger path must be absolute",
                nameof(path));
        }
        _path = Path.GetFullPath(path);
        _utcNow = utcNow ?? (() => DateTimeOffset.UtcNow);
    }

    internal Task<bool> HasReadAsync(
        string threadId,
        string documentKey,
        string contentRevision,
        CancellationToken cancellationToken) =>
        WithLockAsync(
            entries => Task.FromResult(entries.Any(entry =>
                string.Equals(
                    entry.ThreadId,
                    threadId,
                    StringComparison.Ordinal)
                && string.Equals(
                    entry.DocumentKeyHash,
                    HashDocumentKey(documentKey),
                    StringComparison.Ordinal)
                && string.Equals(
                    entry.ContentRevision,
                    contentRevision,
                    StringComparison.Ordinal))),
            write: false,
            cancellationToken);

    internal Task<bool> MarkReadAsync(
        string threadId,
        string documentKey,
        string contentRevision,
        CancellationToken cancellationToken) =>
        WithLockAsync(
            async entries =>
            {
                string documentKeyHash = HashDocumentKey(documentKey);
                if (entries.Any(entry =>
                    string.Equals(
                        entry.ThreadId,
                        threadId,
                        StringComparison.Ordinal)
                    && string.Equals(
                        entry.DocumentKeyHash,
                        documentKeyHash,
                        StringComparison.Ordinal)
                    && string.Equals(
                        entry.ContentRevision,
                        contentRevision,
                        StringComparison.Ordinal)))
                {
                    return true;
                }
                entries.Add(new LedgerEntry(
                    threadId,
                    documentKeyHash,
                    contentRevision,
                    _utcNow().ToString("O")));
                if (entries.Count > MaximumEntries)
                {
                    entries.RemoveRange(
                        0,
                        entries.Count - MaximumEntries);
                }
                await PersistAsync(entries, cancellationToken)
                    .ConfigureAwait(false);
                return true;
            },
            write: true,
            cancellationToken);

    internal static bool TryThreadId(
        JsonElement parameters,
        out string threadId)
    {
        threadId = string.Empty;
        if (
            parameters.ValueKind != JsonValueKind.Object
            || !parameters.TryGetProperty("_meta", out JsonElement meta)
            || meta.ValueKind != JsonValueKind.Object
            || !meta.TryGetProperty(
                "threadId",
                out JsonElement threadValue)
            || threadValue.ValueKind != JsonValueKind.String
            || threadValue.GetString() is not string candidate
            || !Guid.TryParseExact(candidate, "D", out Guid parsed)
        )
        {
            return false;
        }
        threadId = parsed.ToString("D");
        return true;
    }

    private Task<T> WithLockAsync<T>(
        Func<List<LedgerEntry>, Task<T>> action,
        bool write,
        CancellationToken cancellationToken)
    {
        _ = write;
        // A Windows Mutex is thread-affine. Keep acquisition, all awaited I/O
        // (blocked synchronously on this worker), and release on one worker
        // thread; releasing after an ordinary await may resume on another
        // thread and throws SynchronizationLockException.
        return Task.Run(() =>
        {
            using Mutex mutex = new(initiallyOwned: false, MutexName);
            bool acquired = false;
            try
            {
                cancellationToken.ThrowIfCancellationRequested();
                try
                {
                    acquired = mutex.WaitOne(TimeSpan.FromSeconds(3));
                }
                catch (AbandonedMutexException)
                {
                    acquired = true;
                }
                if (!acquired)
                {
                    throw new IOException(
                        "Reader context ledger lock timed out");
                }
                List<LedgerEntry> entries = LoadAsync(cancellationToken)
                    .GetAwaiter().GetResult();
                return action(entries).GetAwaiter().GetResult();
            }
            finally
            {
                if (acquired)
                {
                    mutex.ReleaseMutex();
                }
            }
        }, cancellationToken);
    }

    private async Task<List<LedgerEntry>> LoadAsync(
        CancellationToken cancellationToken)
    {
        FileInfo info = new(_path);
        if (!info.Exists)
        {
            return new List<LedgerEntry>();
        }
        if (info.Length is <= 0 or > MaximumLedgerBytes)
        {
            throw new IOException("Reader context ledger is invalid");
        }
        await using FileStream stream = new(
            _path,
            FileMode.Open,
            FileAccess.Read,
            FileShare.Read,
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
            || !root.TryGetProperty("contract", out JsonElement contract)
            || contract.GetString() != LedgerContract
            || !root.TryGetProperty("entries", out JsonElement values)
            || values.ValueKind != JsonValueKind.Array
            || values.GetArrayLength() > MaximumEntries
        )
        {
            throw new IOException("Reader context ledger contract is invalid");
        }
        List<LedgerEntry> entries = new();
        foreach (JsonElement value in values.EnumerateArray())
        {
            if (
                value.ValueKind != JsonValueKind.Object
                || value.EnumerateObject().Count() != 4
                || !value.TryGetProperty(
                    "threadId",
                    out JsonElement thread)
                || thread.GetString() is not string threadId
                || !Guid.TryParseExact(threadId, "D", out _)
                || !value.TryGetProperty(
                    "documentKeyHash",
                    out JsonElement key)
                || key.GetString() is not string documentKeyHash
                || documentKeyHash.Length != 64
                || documentKeyHash.Any(character =>
                    character is not (
                        >= '0' and <= '9'
                        or >= 'a' and <= 'f'))
                || !value.TryGetProperty(
                    "contentRevision",
                    out JsonElement revision)
                || revision.GetString() is not string contentRevision
                || contentRevision.Length != 64
                || contentRevision.Any(character =>
                    character is not (
                        >= '0' and <= '9'
                        or >= 'a' and <= 'f'))
                || !value.TryGetProperty(
                    "deliveredAtUtc",
                    out JsonElement delivered)
                || delivered.GetString() is not string deliveredAt
                || !DateTimeOffset.TryParse(
                    deliveredAt,
                    System.Globalization.CultureInfo.InvariantCulture,
                    System.Globalization.DateTimeStyles.RoundtripKind,
                    out _)
            )
            {
                throw new IOException(
                    "Reader context ledger entry is invalid");
            }
            entries.Add(new LedgerEntry(
                threadId,
                documentKeyHash,
                contentRevision,
                deliveredAt));
        }
        return entries;
    }

    private async Task PersistAsync(
        IReadOnlyList<LedgerEntry> entries,
        CancellationToken cancellationToken)
    {
        string? directory = Path.GetDirectoryName(_path);
        if (string.IsNullOrEmpty(directory))
        {
            throw new IOException("Reader context ledger directory invalid");
        }
        Directory.CreateDirectory(directory);
        JsonArray values = new();
        foreach (LedgerEntry entry in entries)
        {
            values.Add(new JsonObject
            {
                ["threadId"] = entry.ThreadId,
                ["documentKeyHash"] = entry.DocumentKeyHash,
                ["contentRevision"] = entry.ContentRevision,
                ["deliveredAtUtc"] = entry.DeliveredAtUtc,
            });
        }
        JsonObject root = new()
        {
            ["contract"] = LedgerContract,
            ["entries"] = values,
        };
        byte[] bytes = Utf8WithoutBom.GetBytes(
            root.ToJsonString(DirectBridgeContract.JsonOptions));
        if (bytes.Length is <= 0 or > MaximumLedgerBytes)
        {
            throw new IOException("Reader context ledger too large");
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

    private static string HashDocumentKey(string value)
    {
        byte[] digest = System.Security.Cryptography.SHA256.HashData(
            Utf8WithoutBom.GetBytes(value));
        return Convert.ToHexString(digest).ToLowerInvariant();
    }
}
