using System.Diagnostics;
using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace BwReader.ComputerVoiceAudio;

internal sealed record LocalSnapshotPageImageResult(
    bool Success,
    byte[]? PngBytes,
    string? CacheTag,
    string? FailureReason)
{
    internal static LocalSnapshotPageImageResult Ready(
        byte[] pngBytes,
        string cacheTag) =>
        new(true, pngBytes, cacheTag, null);

    internal static LocalSnapshotPageImageResult Failed(
        string reason) =>
        new(false, null, null, reason);
}

internal interface ILocalSnapshotPageImageRenderer
{
    Task<LocalSnapshotPageImageResult> RenderAsync(
        JsonObject snapshot,
        CancellationToken cancellationToken);
}

internal sealed class LocalSnapshotPageImageRenderer :
    ILocalSnapshotPageImageRenderer
{
    private const int MaximumCachedImages = 4;
    private const int MaximumImageBytes = 12 * 1024 * 1024;
    private const long MaximumCacheBytes = 32L * 1024 * 1024;
    private const int MaximumImageDimension = 1600;
    private static readonly TimeSpan RenderTimeout =
        TimeSpan.FromSeconds(12);
    private static readonly byte[] PngSignature =
        [137, 80, 78, 71, 13, 10, 26, 10];
    private const string RenderScript =
        """
        import json
        import pathlib
        import sys
        import fitz

        source = pathlib.Path(sys.argv[1])
        page_number = int(sys.argv[2])
        output = pathlib.Path(sys.argv[3])
        maximum_dimension = int(sys.argv[4])
        maximum_bytes = int(sys.argv[5])
        try:
            with fitz.open(source) as document:
                if not document.is_pdf:
                    raise ValueError("not-pdf")
                if page_number < 1 or page_number > document.page_count:
                    raise IndexError("page-out-of-range")
                page = document.load_page(page_number - 1)
                width = max(float(page.rect.width), 1.0)
                height = max(float(page.rect.height), 1.0)
                zoom = min(
                    maximum_dimension / width,
                    maximum_dimension / height,
                    4.0,
                )
                zoom = max(zoom, 0.25)
                pixmap = page.get_pixmap(
                    matrix=fitz.Matrix(zoom, zoom),
                    colorspace=fitz.csRGB,
                    alpha=False,
                )
                payload = pixmap.tobytes("png")
                if len(payload) <= 0 or len(payload) > maximum_bytes:
                    raise ValueError("image-size")
                with output.open("xb") as handle:
                    handle.write(payload)
                print(json.dumps({
                    "ok": True,
                    "bytes": len(payload),
                }))
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
        Task<RenderedPageImage>> _render;
    private readonly SemaphoreSlim _renderGate = new(1, 1);
    private readonly object _cacheGate = new();
    private readonly Dictionary<ImageCacheKey, CachedImage> _cache = [];
    private readonly Queue<ImageCacheKey> _cacheOrder = new();
    private long _cachedBytes;

    internal sealed record RenderedPageImage(
        bool Success,
        byte[]? PngBytes,
        string? FailureReason);

    private sealed record ImageCacheKey(
        string CanonicalPath,
        long Size,
        long LastWriteUtcTicks,
        int Page);

    private sealed record CachedImage(
        byte[] PngBytes,
        string CacheTag);

    internal int CachedImageCount
    {
        get
        {
            lock (_cacheGate)
            {
                return _cache.Count;
            }
        }
    }

    internal long CachedImageBytes
    {
        get
        {
            lock (_cacheGate)
            {
                return _cachedBytes;
            }
        }
    }

    internal LocalSnapshotPageImageRenderer(
        LocalBookPageResolverOptions options)
        : this(options, null)
    {
    }

    internal LocalSnapshotPageImageRenderer(
        LocalBookPageResolverOptions options,
        Func<
            string,
            int,
            CancellationToken,
            Task<RenderedPageImage>>? render)
    {
        ArgumentNullException.ThrowIfNull(options);
        _libraryRoot = Path.GetFullPath(options.LibraryRoot);
        _pythonExecutable = Path.GetFullPath(
            options.PythonExecutable);
        _render = render ?? RenderWithPythonAsync;
    }

    public async Task<LocalSnapshotPageImageResult> RenderAsync(
        JsonObject snapshot,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(snapshot);
        if (
            snapshot["currentPage"] is not JsonObject currentPage
            || currentPage["file"]?.GetValue<string>()
                is not string readerFile
            || string.IsNullOrWhiteSpace(readerFile)
            || currentPage["page"] is not JsonValue pageValue
            || !pageValue.TryGetValue(out int page)
            || page < 1
        )
        {
            return LocalSnapshotPageImageResult.Failed(
                "local-page-image-page-unavailable");
        }
        string? kind = currentPage["kind"]?.GetValue<string>();
        if (
            kind is null
            && snapshot["activeReading"] is JsonObject active
            && string.Equals(
                active["file"]?.GetValue<string>(),
                readerFile,
                StringComparison.Ordinal)
            && PageEquivalent(active["page"], currentPage["page"])
        )
        {
            kind = active["kind"]?.GetValue<string>();
        }
        if (
            kind is null
            && string.Equals(
                Path.GetExtension(readerFile),
                ".pdf",
                StringComparison.OrdinalIgnoreCase)
        )
        {
            kind = "pdf";
        }
        if (kind != "pdf")
        {
            return LocalSnapshotPageImageResult.Failed(
                "local-page-image-not-pdf");
        }
        if (
            !LocalBookPageResolver.TryResolvePdfPath(
                _libraryRoot,
                readerFile,
                out string? resolvedPath,
                out string? pathFailure)
        )
        {
            return LocalSnapshotPageImageResult.Failed(
                pathFailure ?? "local-page-image-path-invalid");
        }
        string path = resolvedPath
            ?? throw new InvalidOperationException(
                "validated local PDF path is missing");

        FileInfo info;
        try
        {
            info = new FileInfo(path);
            if (!info.Exists || info.Length <= 0)
            {
                return LocalSnapshotPageImageResult.Failed(
                    "local-page-image-file-missing");
            }
        }
        catch (Exception exception) when (
            exception is IOException
            or UnauthorizedAccessException
            or ArgumentException)
        {
            return LocalSnapshotPageImageResult.Failed(
                "local-page-image-file-unreadable");
        }

        ImageCacheKey key = new(
            path,
            info.Length,
            info.LastWriteTimeUtc.Ticks,
            page);
        LocalSnapshotPageImageResult? cached = FindCached(key);
        if (cached is not null)
        {
            return cached;
        }

        await _renderGate.WaitAsync(cancellationToken)
            .ConfigureAwait(false);
        try
        {
            cached = FindCached(key);
            if (cached is not null)
            {
                return cached;
            }
            RenderedPageImage rendered;
            try
            {
                rendered = await _render(
                    path,
                    page,
                    cancellationToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                throw;
            }
            catch
            {
                return LocalSnapshotPageImageResult.Failed(
                    "local-page-image-render-failed");
            }
            if (
                !rendered.Success
                || rendered.PngBytes is not { Length: >= 8 } png
                || png.Length > MaximumImageBytes
                || !png.AsSpan(0, PngSignature.Length)
                    .SequenceEqual(PngSignature)
            )
            {
                return LocalSnapshotPageImageResult.Failed(
                    rendered.FailureReason
                        ?? "local-page-image-render-failed");
            }
            string cacheTag = string.Create(
                CultureInfo.InvariantCulture,
                $"{info.LastWriteTimeUtc.Ticks:x}-{info.Length:x}-{page:x}");
            Remember(key, png, cacheTag);
            return LocalSnapshotPageImageResult.Ready(
                png,
                cacheTag);
        }
        finally
        {
            _renderGate.Release();
        }
    }

    private LocalSnapshotPageImageResult? FindCached(
        ImageCacheKey key)
    {
        lock (_cacheGate)
        {
            if (!_cache.TryGetValue(key, out CachedImage? cached))
            {
                return null;
            }
            return LocalSnapshotPageImageResult.Ready(
                cached.PngBytes,
                cached.CacheTag);
        }
    }

    private void Remember(
        ImageCacheKey key,
        byte[] pngBytes,
        string cacheTag)
    {
        lock (_cacheGate)
        {
            if (_cache.ContainsKey(key))
            {
                return;
            }
            _cache[key] = new CachedImage(pngBytes, cacheTag);
            _cacheOrder.Enqueue(key);
            _cachedBytes = checked(_cachedBytes + pngBytes.Length);
            while (
                _cacheOrder.Count > MaximumCachedImages
                || _cachedBytes > MaximumCacheBytes
            )
            {
                ImageCacheKey expired = _cacheOrder.Dequeue();
                if (_cache.Remove(expired, out CachedImage? removed))
                {
                    _cachedBytes -= removed.PngBytes.Length;
                }
            }
        }
    }

    private async Task<RenderedPageImage> RenderWithPythonAsync(
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
            return new RenderedPageImage(
                false,
                null,
                "local-page-image-python-unavailable");
        }

        string outputPath = Path.Combine(
            Path.GetTempPath(),
            "bw-reader-page-" + Guid.NewGuid().ToString("N") + ".png");
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
        startInfo.ArgumentList.Add(RenderScript);
        startInfo.ArgumentList.Add(path);
        startInfo.ArgumentList.Add(
            page.ToString(CultureInfo.InvariantCulture));
        startInfo.ArgumentList.Add(outputPath);
        startInfo.ArgumentList.Add(
            MaximumImageDimension.ToString(
                CultureInfo.InvariantCulture));
        startInfo.ArgumentList.Add(
            MaximumImageBytes.ToString(
                CultureInfo.InvariantCulture));

        using Process process = new() { StartInfo = startInfo };
        try
        {
            if (!process.Start())
            {
                return new RenderedPageImage(
                    false,
                    null,
                    "local-page-image-python-start-failed");
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
            timeout.CancelAfter(RenderTimeout);
            await process.WaitForExitAsync(timeout.Token)
                .ConfigureAwait(false);
            string stdout = await stdoutTask.ConfigureAwait(false);
            _ = await stderrTask.ConfigureAwait(false);
            if (process.ExitCode != 0)
            {
                return new RenderedPageImage(
                    false,
                    null,
                    "local-page-image-python-failed");
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
                return new RenderedPageImage(
                    false,
                    null,
                    "local-page-image-render-failed");
            }
            FileInfo output = new(outputPath);
            if (
                !output.Exists
                || output.Length is < 8 or > MaximumImageBytes
            )
            {
                return new RenderedPageImage(
                    false,
                    null,
                    "local-page-image-output-invalid");
            }
            byte[] payload = await File.ReadAllBytesAsync(
                outputPath,
                cancellationToken).ConfigureAwait(false);
            return new RenderedPageImage(true, payload, null);
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
            return new RenderedPageImage(
                false,
                null,
                "local-page-image-render-timeout");
        }
        catch (Exception exception) when (
            exception is IOException
            or UnauthorizedAccessException
            or InvalidOperationException
            or JsonException)
        {
            return new RenderedPageImage(
                false,
                null,
                "local-page-image-render-failed");
        }
        finally
        {
            try
            {
                File.Delete(outputPath);
            }
            catch
            {
            }
        }
    }

    private static bool PageEquivalent(
        JsonNode? left,
        JsonNode? right)
    {
        if (
            left is JsonValue leftValue
            && right is JsonValue rightValue
        )
        {
            if (
                leftValue.TryGetValue(out int leftNumber)
                && rightValue.TryGetValue(out int rightNumber)
            )
            {
                return leftNumber == rightNumber;
            }
            if (
                leftValue.TryGetValue(out string? leftText)
                && rightValue.TryGetValue(out string? rightText)
            )
            {
                return string.Equals(
                    leftText,
                    rightText,
                    StringComparison.Ordinal);
            }
        }
        return false;
    }
}
