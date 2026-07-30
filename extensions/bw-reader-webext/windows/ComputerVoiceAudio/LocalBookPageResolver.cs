using System.Diagnostics;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace BwReader.ComputerVoiceAudio;

internal sealed record LocalBookPageResolverOptions(
    string LibraryRoot,
    string PythonExecutable)
{
    internal const string LibraryRootEnvironmentVariable =
        "BW_READER_LIBRARY_ROOT";
    internal const string PythonEnvironmentVariable =
        "BW_READER_PYTHON";
    internal const string DefaultLibraryRoot = @"C:\obsidian";
    internal static readonly string DefaultPythonExecutable =
        Path.Combine(
            Environment.GetFolderPath(
                Environment.SpecialFolder.UserProfile),
            "AppData",
            "Local",
            "Programs",
            "Python",
            "Python313",
            "python.exe");

    internal static LocalBookPageResolverOptions FromEnvironment() =>
        new(
            Environment.GetEnvironmentVariable(
                LibraryRootEnvironmentVariable)
                ?? DefaultLibraryRoot,
            Environment.GetEnvironmentVariable(
                PythonEnvironmentVariable)
                ?? DefaultPythonExecutable);
}

internal sealed record LocalBookPageResolution(
    bool Applicable,
    bool Success,
    JsonObject? CurrentPage,
    string? FailureReason)
{
    internal static LocalBookPageResolution NotApplicable() =>
        new(false, false, null, null);

    internal static LocalBookPageResolution Failed(string reason) =>
        new(true, false, null, reason);

    internal static LocalBookPageResolution Ready(JsonObject page) =>
        new(true, true, page, null);
}

internal interface ILocalBookPageResolver
{
    Task<LocalBookPageResolution> ResolveAsync(
        DirectActiveReading activeReading,
        CancellationToken cancellationToken);
}

internal sealed class LocalBookPageResolver : ILocalBookPageResolver
{
    private const int MaximumCachedPages = 32;
    private const int MaximumExtractedCharacters = 96 * 1024;
    private static readonly TimeSpan ExtractionTimeout =
        TimeSpan.FromSeconds(10);
    private const string ExtractionScript =
        """
        import json
        import sys
        import fitz

        path = sys.argv[1]
        page_number = int(sys.argv[2])
        limit = int(sys.argv[3])
        try:
            with fitz.open(path) as document:
                if not document.is_pdf:
                    raise ValueError("not-pdf")
                if page_number < 1 or page_number > document.page_count:
                    raise IndexError("page-out-of-range")
                text = document.load_page(page_number - 1).get_text("text")
                truncated = len(text) > limit
                if truncated:
                    text = text[:limit]
                print(json.dumps({
                    "ok": True,
                    "text": text,
                    "truncated": truncated,
                }, ensure_ascii=False))
        except Exception as error:
            print(json.dumps({
                "ok": False,
                "error": type(error).__name__,
            }))
        """;

    private readonly string _libraryRoot;
    private readonly string _pythonExecutable;
    private readonly Func<
        string,
        int,
        CancellationToken,
        Task<ExtractedPage>> _extract;
    private readonly object _cacheGate = new();
    private readonly Dictionary<PageCacheKey, JsonObject> _cache = [];
    private readonly Queue<PageCacheKey> _cacheOrder = new();

    internal sealed record ExtractedPage(
        bool Success,
        string Text,
        bool Truncated,
        string? FailureReason);

    private sealed record PageCacheKey(
        string CanonicalPath,
        long Size,
        long LastWriteUtcTicks,
        int Page);

    internal LocalBookPageResolver(
        LocalBookPageResolverOptions options)
        : this(options, null)
    {
    }

    internal LocalBookPageResolver(
        LocalBookPageResolverOptions options,
        Func<
            string,
            int,
            CancellationToken,
            Task<ExtractedPage>>? extract)
    {
        ArgumentNullException.ThrowIfNull(options);
        _libraryRoot = RequireAbsolutePath(
            options.LibraryRoot,
            nameof(options.LibraryRoot));
        _pythonExecutable = RequireAbsolutePath(
            options.PythonExecutable,
            nameof(options.PythonExecutable));
        _extract = extract ?? ExtractWithPythonAsync;
    }

    public async Task<LocalBookPageResolution> ResolveAsync(
        DirectActiveReading activeReading,
        CancellationToken cancellationToken)
    {
        if (activeReading.Kind != "pdf")
        {
            return LocalBookPageResolution.NotApplicable();
        }
        if (
            activeReading.Page.ValueKind != JsonValueKind.Number
            || !activeReading.Page.TryGetInt32(out int page)
            || page < 1
        )
        {
            return LocalBookPageResolution.Failed(
                "local-pdf-page-invalid");
        }
        if (
            !TryResolvePdfPath(
                _libraryRoot,
                activeReading.File,
                out string? path,
                out string? pathFailure)
        )
        {
            return LocalBookPageResolution.Failed(
                pathFailure ?? "local-pdf-path-invalid");
        }
        string resolvedPath = path
            ?? throw new InvalidOperationException(
                "validated local PDF path is missing");

        FileInfo info;
        try
        {
            info = new FileInfo(resolvedPath);
            if (!info.Exists || info.Length <= 0)
            {
                return LocalBookPageResolution.Failed(
                    "local-pdf-file-missing");
            }
        }
        catch (
            Exception exception
        ) when (
            exception is IOException
            or UnauthorizedAccessException
            or ArgumentException)
        {
            return LocalBookPageResolution.Failed(
                "local-pdf-file-unreadable");
        }

        PageCacheKey key = new(
            resolvedPath,
            info.Length,
            info.LastWriteTimeUtc.Ticks,
            page);
        lock (_cacheGate)
        {
            if (_cache.TryGetValue(key, out JsonObject? cached))
            {
                return LocalBookPageResolution.Ready(
                    cached.DeepClone() as JsonObject
                        ?? throw new InvalidOperationException(
                            "local page cache clone failed"));
            }
        }

        ExtractedPage extracted;
        try
        {
            extracted = await _extract(
                resolvedPath,
                page,
                cancellationToken).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch
        {
            return LocalBookPageResolution.Failed(
                "local-pdf-extraction-failed");
        }
        if (!extracted.Success)
        {
            return LocalBookPageResolution.Failed(
                extracted.FailureReason
                    ?? "local-pdf-extraction-failed");
        }

        JsonObject currentPage = new()
        {
            ["kind"] = "pdf",
            ["file"] = activeReading.File,
            ["title"] = activeReading.Title,
            ["page"] = page,
            ["stable"] = true,
            ["reason"] = "windows-local-pdf",
            ["text"] = extracted.Text,
            ["textAvailable"] =
                !string.IsNullOrWhiteSpace(extracted.Text),
            ["textSource"] = "windows-local-pdf",
            ["fallbackReason"] = null,
            ["truncated"] = extracted.Truncated,
        };
        Remember(key, currentPage);
        return LocalBookPageResolution.Ready(currentPage);
    }

    internal static bool TryResolvePdfPath(
        string libraryRoot,
        string readerFile,
        out string? resolvedPath,
        out string? failureReason)
    {
        resolvedPath = null;
        failureReason = null;
        try
        {
            string root = RequireAbsolutePath(
                libraryRoot,
                nameof(libraryRoot));
            if (
                string.IsNullOrWhiteSpace(readerFile)
                || Path.IsPathFullyQualified(readerFile)
                || readerFile.StartsWith(
                    Path.DirectorySeparatorChar)
                || readerFile.StartsWith(
                    Path.AltDirectorySeparatorChar)
                || readerFile.Any(char.IsControl)
            )
            {
                failureReason = "local-pdf-path-invalid";
                return false;
            }
            string normalized = readerFile.Replace(
                Path.AltDirectorySeparatorChar,
                Path.DirectorySeparatorChar);
            string[] segments = normalized.Split(
                Path.DirectorySeparatorChar,
                StringSplitOptions.RemoveEmptyEntries);
            if (
                segments.Length == 0
                || segments.Any(segment =>
                    segment is "." or ".."
                    || segment.Contains(':'))
            )
            {
                failureReason = "local-pdf-path-traversal";
                return false;
            }
            string candidate = Path.GetFullPath(
                Path.Combine(root, Path.Combine(segments)));
            string relative = Path.GetRelativePath(root, candidate);
            if (
                Path.IsPathFullyQualified(relative)
                || relative == ".."
                || relative.StartsWith(
                    ".." + Path.DirectorySeparatorChar,
                    StringComparison.Ordinal)
                || !string.Equals(
                    Path.GetExtension(candidate),
                    ".pdf",
                    StringComparison.OrdinalIgnoreCase)
            )
            {
                failureReason = "local-pdf-path-traversal";
                return false;
            }
            if (!Directory.Exists(root))
            {
                failureReason = "local-library-root-missing";
                return false;
            }
            RejectReparsePoint(root);
            string cursor = root;
            foreach (string segment in segments)
            {
                cursor = Path.Combine(cursor, segment);
                if (File.Exists(cursor) || Directory.Exists(cursor))
                {
                    RejectReparsePoint(cursor);
                }
            }
            resolvedPath = candidate;
            return true;
        }
        catch (
            Exception exception
        ) when (
            exception is IOException
            or UnauthorizedAccessException
            or ArgumentException
            or NotSupportedException)
        {
            failureReason = "local-pdf-path-invalid";
            return false;
        }
    }

    private async Task<ExtractedPage> ExtractWithPythonAsync(
        string path,
        int page,
        CancellationToken cancellationToken)
    {
        if (
            !File.Exists(_pythonExecutable)
            || (
                File.GetAttributes(_pythonExecutable)
                & FileAttributes.ReparsePoint
            ) != 0
        )
        {
            return new ExtractedPage(
                false,
                "",
                false,
                "local-pdf-python-unavailable");
        }
        ProcessStartInfo startInfo = new()
        {
            FileName = _pythonExecutable,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = new UTF8Encoding(false),
            StandardErrorEncoding = new UTF8Encoding(false),
        };
        startInfo.ArgumentList.Add("-I");
        startInfo.ArgumentList.Add("-c");
        startInfo.ArgumentList.Add(ExtractionScript);
        startInfo.ArgumentList.Add(path);
        startInfo.ArgumentList.Add(page.ToString(
            System.Globalization.CultureInfo.InvariantCulture));
        startInfo.ArgumentList.Add(MaximumExtractedCharacters.ToString(
            System.Globalization.CultureInfo.InvariantCulture));

        using Process process = new() { StartInfo = startInfo };
        try
        {
            if (!process.Start())
            {
                return new ExtractedPage(
                    false,
                    "",
                    false,
                    "local-pdf-python-start-failed");
            }
            Task<string> stdoutTask =
                process.StandardOutput.ReadToEndAsync(
                    cancellationToken);
            Task<string> stderrTask =
                process.StandardError.ReadToEndAsync(
                    cancellationToken);
            using CancellationTokenSource timeout =
                CancellationTokenSource.CreateLinkedTokenSource(
                    cancellationToken);
            timeout.CancelAfter(ExtractionTimeout);
            await process.WaitForExitAsync(timeout.Token)
                .ConfigureAwait(false);
            string stdout = await stdoutTask.ConfigureAwait(false);
            _ = await stderrTask.ConfigureAwait(false);
            if (process.ExitCode != 0)
            {
                return new ExtractedPage(
                    false,
                    "",
                    false,
                    "local-pdf-python-failed");
            }
            using JsonDocument result = JsonDocument.Parse(stdout);
            JsonElement root = result.RootElement;
            if (
                !root.TryGetProperty("ok", out JsonElement ok)
                || ok.ValueKind is not (
                    JsonValueKind.True or JsonValueKind.False)
                || !ok.GetBoolean()
            )
            {
                return new ExtractedPage(
                    false,
                    "",
                    false,
                    "local-pdf-extraction-failed");
            }
            string text = root.GetProperty("text").GetString() ?? "";
            bool truncated =
                root.GetProperty("truncated").GetBoolean();
            return new ExtractedPage(
                true,
                text,
                truncated,
                null);
        }
        catch (OperationCanceledException)
        {
            try
            {
                process.Kill(entireProcessTree: true);
            }
            catch
            {
            }
            if (cancellationToken.IsCancellationRequested)
            {
                throw;
            }
            return new ExtractedPage(
                false,
                "",
                false,
                "local-pdf-extraction-timeout");
        }
        catch (
            Exception exception
        ) when (
            exception is IOException
            or UnauthorizedAccessException
            or InvalidOperationException
            or JsonException)
        {
            return new ExtractedPage(
                false,
                "",
                false,
                "local-pdf-extraction-failed");
        }
    }

    private void Remember(PageCacheKey key, JsonObject page)
    {
        lock (_cacheGate)
        {
            if (_cache.ContainsKey(key))
            {
                return;
            }
            _cache[key] = page.DeepClone() as JsonObject
                ?? throw new InvalidOperationException(
                    "local page cache clone failed");
            _cacheOrder.Enqueue(key);
            while (_cacheOrder.Count > MaximumCachedPages)
            {
                _cache.Remove(_cacheOrder.Dequeue());
            }
        }
    }

    private static string RequireAbsolutePath(
        string value,
        string parameterName)
    {
        if (
            string.IsNullOrWhiteSpace(value)
            || !Path.IsPathFullyQualified(value)
            || value.Any(char.IsControl)
        )
        {
            throw new ArgumentException(
                "path must be absolute",
                parameterName);
        }
        return Path.GetFullPath(value);
    }

    private static void RejectReparsePoint(string path)
    {
        if (
            (File.GetAttributes(path) & FileAttributes.ReparsePoint)
            != 0
        )
        {
            throw new IOException("reparse points are not allowed");
        }
    }
}
