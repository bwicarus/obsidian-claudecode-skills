using System.Net;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.AspNetCore.Http;

namespace BwReader.ComputerVoiceAudio;

internal static class LocalSnapshotPageImageRendererSelfTest
{
    private static readonly byte[] TestPng =
        [137, 80, 78, 71, 13, 10, 26, 10, 1, 2, 3, 4];

    internal static void Run(ICollection<string> checks)
    {
        string root = Path.Combine(
            Path.GetTempPath(),
            "bw-local-page-image-self-test-"
                + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        try
        {
            RunAsync(root, checks).GetAwaiter().GetResult();
        }
        finally
        {
            string fullRoot = Path.GetFullPath(root);
            if (
                fullRoot.StartsWith(
                    Path.GetFullPath(Path.GetTempPath()),
                    StringComparison.OrdinalIgnoreCase)
                && Path.GetFileName(fullRoot).StartsWith(
                    "bw-local-page-image-self-test-",
                    StringComparison.Ordinal)
            )
            {
                Directory.Delete(fullRoot, recursive: true);
            }
        }
    }

    private static async Task RunAsync(
        string root,
        ICollection<string> checks)
    {
        string libraryRoot = Path.Combine(root, "library");
        string books = Path.Combine(libraryRoot, "books");
        Directory.CreateDirectory(books);
        string bookPath = Path.Combine(books, "sample.pdf");
        await File.WriteAllBytesAsync(bookPath, [1, 2, 3])
            .ConfigureAwait(false);
        string pythonPath = Path.Combine(root, "python.exe");
        await File.WriteAllBytesAsync(pythonPath, [1])
            .ConfigureAwait(false);

        int renderCount = 0;
        LocalSnapshotPageImageRenderer renderer = new(
            new LocalBookPageResolverOptions(
                libraryRoot,
                pythonPath),
            (_, _, _) =>
            {
                renderCount++;
                return Task.FromResult(
                    new LocalSnapshotPageImageRenderer
                        .RenderedPageImage(
                            true,
                            TestPng,
                            null));
            });
        JsonObject pageTwo = Snapshot("books/sample.pdf", 2, 7);
        LocalSnapshotPageImageResult first =
            await renderer.RenderAsync(
                pageTwo,
                CancellationToken.None).ConfigureAwait(false);
        LocalSnapshotPageImageResult cached =
            await renderer.RenderAsync(
                pageTwo,
                CancellationToken.None).ConfigureAwait(false);
        Require(
            first.Success
            && cached.Success
            && renderCount == 1
            && first.PngBytes is not null
            && first.PngBytes.SequenceEqual(TestPng),
            "local-page-image-renders-canonical-page-and-caches",
            checks);

        await File.AppendAllTextAsync(bookPath, "changed")
            .ConfigureAwait(false);
        LocalSnapshotPageImageResult changed =
            await renderer.RenderAsync(
                pageTwo,
                CancellationToken.None).ConfigureAwait(false);
        Require(
            changed.Success && renderCount == 2,
            "local-page-image-invalidates-cache-on-pdf-change",
            checks);

        for (int page = 3; page <= 8; page++)
        {
            LocalSnapshotPageImageResult additional =
                await renderer.RenderAsync(
                    Snapshot("books/sample.pdf", page, 7 + page),
                    CancellationToken.None).ConfigureAwait(false);
            Require(
                additional.Success,
                $"local-page-image-additional-page-{page}",
                checks);
        }
        Require(
            renderer.CachedImageCount <= 4
            && renderer.CachedImageBytes
                <= 32L * 1024 * 1024,
            "local-page-image-cache-is-bounded",
            checks);

        int beforeRejected = renderCount;
        LocalSnapshotPageImageResult traversal =
            await renderer.RenderAsync(
                Snapshot("../sample.pdf", 1, 20),
                CancellationToken.None).ConfigureAwait(false);
        LocalSnapshotPageImageResult nonPdf =
            await renderer.RenderAsync(
                Snapshot("books/sample.epub", 1, 21, "epub"),
                CancellationToken.None).ConfigureAwait(false);
        Require(
            !traversal.Success
            && traversal.FailureReason
                == "local-pdf-path-traversal"
            && !nonPdf.Success
            && nonPdf.FailureReason
                == "local-page-image-not-pdf"
            && renderCount == beforeRejected,
            "local-page-image-fails-closed-before-render",
            checks);

        await CheckLocalEndpointAsync(
            root,
            checks).ConfigureAwait(false);
    }

    private static async Task CheckLocalEndpointAsync(
        string root,
        ICollection<string> checks)
    {
        string statePath = Path.Combine(root, "snapshot.json");
        JsonObject snapshot = Snapshot(
            "books/sample.pdf",
            2,
            41);
        snapshot["updatedAtUtc"] =
            DateTimeOffset.UtcNow.ToString("O");
        JsonObject active =
            snapshot["activeReading"] as JsonObject
            ?? throw new InvalidOperationException(
                "test snapshot active reading missing");
        active["receivedAtEpochMs"] =
            DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        await File.WriteAllTextAsync(
            statePath,
            snapshot.ToJsonString(
                DirectBridgeContract.JsonOptions),
            new UTF8Encoding(false)).ConfigureAwait(false);

        CountingPageImageRenderer renderer = new(TestPng);
        using DirectSnapshotViewer viewer = new(
            statePath,
            DirectBridgeContract.DefaultListenPort,
            renderer);
        DefaultHttpContext imageContext = LocalContext(
            "?asset=current-page&revision=41");
        await viewer.HandleSnapshotAsync(imageContext)
            .ConfigureAwait(false);
        byte[] response = (
            (MemoryStream)imageContext.Response.Body
        ).ToArray();
        Require(
            imageContext.Response.StatusCode
                == StatusCodes.Status200OK
            && imageContext.Response.ContentType == "image/png"
            && response.SequenceEqual(TestPng)
            && renderer.RenderCount == 1,
            "local-page-image-endpoint-is-same-origin-png",
            checks);

        DefaultHttpContext staleRevision = LocalContext(
            "?asset=current-page&revision=40");
        await viewer.HandleSnapshotAsync(staleRevision)
            .ConfigureAwait(false);
        Require(
            staleRevision.Response.StatusCode
                == StatusCodes.Status409Conflict
            && renderer.RenderCount == 1,
            "local-page-image-endpoint-rejects-revision-race",
            checks);

        DefaultHttpContext viewerContext = LocalContext("");
        await viewer.HandleViewerAsync(viewerContext)
            .ConfigureAwait(false);
        string document = Encoding.UTF8.GetString(
            ((MemoryStream)viewerContext.Response.Body).ToArray());
        string csp = viewerContext.Response.Headers[
            "Content-Security-Policy"].ToString();
        Require(
            document.Contains(
                "url.searchParams.set(\"asset\", \"current-page\")",
                StringComparison.Ordinal)
            && document.Contains(
                "if (key === pageImageKey) return;",
                StringComparison.Ordinal)
            && !document.Contains(
                DirectSnapshotMarkdown.ReaderOrigin,
                StringComparison.Ordinal)
            && csp.Contains(
                "img-src 'self' blob:",
                StringComparison.Ordinal)
            && !csp.Contains(
                DirectSnapshotMarkdown.ReaderOrigin,
                StringComparison.Ordinal),
            "snapshot-viewer-never-fetches-protected-reader-image",
            checks);
    }

    private static JsonObject Snapshot(
        string file,
        int page,
        long revision,
        string kind = "pdf") =>
        new()
        {
            ["schema"] =
                FileDirectSnapshotContextAdapter.SnapshotContract,
            ["revision"] = revision,
            ["updatedAtUtc"] =
                DateTimeOffset.UtcNow.ToString("O"),
            ["latestEvent"] = null,
            ["activeReading"] = new JsonObject
            {
                ["kind"] = kind,
                ["file"] = file,
                ["title"] = "Sample",
                ["page"] = page,
                ["fresh"] = true,
                ["ageSec"] = 0,
                ["observedAtEpochMs"] =
                    DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
                ["receivedAtEpochMs"] =
                    DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
            },
            ["contextStatus"] = "ready",
            ["currentPage"] = new JsonObject
            {
                ["kind"] = kind,
                ["file"] = file,
                ["title"] = "Sample",
                ["page"] = page,
                ["stable"] = true,
                ["text"] = "body remains independent",
                ["textAvailable"] = true,
                ["textSource"] = "windows-local-pdf",
                ["fallbackReason"] = null,
                ["truncated"] = false,
            },
            ["selection"] = new JsonObject
            {
                ["state"] = "active",
                ["text"] = "selection remains independent",
                ["ref"] = null,
                ["reason"] = null,
            },
            ["focus"] = new JsonObject
            {
                ["state"] = "unknown",
                ["kind"] = null,
                ["ref"] = null,
                ["reason"] = "test",
            },
        };

    private static DefaultHttpContext LocalContext(string query)
    {
        DefaultHttpContext context = new();
        context.Request.Method = HttpMethods.Get;
        context.Request.Host = new HostString(
            DirectBridgeContract.ListenHost,
            DirectBridgeContract.DefaultListenPort);
        context.Request.QueryString = new QueryString(query);
        context.Connection.RemoteIpAddress = IPAddress.Loopback;
        context.Response.Body = new MemoryStream();
        return context;
    }

    private static void Require(
        bool condition,
        string name,
        ICollection<string> checks)
    {
        if (!condition)
        {
            throw new InvalidOperationException(
                $"self-test failed: {name}");
        }
        checks.Add(name);
    }

    private sealed class CountingPageImageRenderer :
        ILocalSnapshotPageImageRenderer
    {
        private readonly byte[] _png;

        internal CountingPageImageRenderer(byte[] png)
        {
            _png = png;
        }

        internal int RenderCount { get; private set; }

        public Task<LocalSnapshotPageImageResult> RenderAsync(
            JsonObject snapshot,
            CancellationToken cancellationToken)
        {
            _ = snapshot;
            cancellationToken.ThrowIfCancellationRequested();
            RenderCount++;
            return Task.FromResult(
                LocalSnapshotPageImageResult.Ready(
                    _png,
                    "test"));
        }
    }
}
