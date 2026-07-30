using System.Text.Json;

namespace BwReader.ComputerVoiceAudio;

internal static class LocalBookPageResolverSelfTest
{
    internal static void Run(ICollection<string> checks)
    {
        string root = Path.Combine(
            Path.GetTempPath(),
            "bw-local-page-self-test-"
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
                    "bw-local-page-self-test-",
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

        int extractionCount = 0;
        LocalBookPageResolver resolver = new(
            new LocalBookPageResolverOptions(
                libraryRoot,
                pythonPath),
            (path, page, _) =>
            {
                extractionCount++;
                return Task.FromResult(
                    new LocalBookPageResolver.ExtractedPage(
                        true,
                        $"{Path.GetFileName(path)} page {page}",
                        false,
                        null));
            });
        DirectActiveReading active = Reading(
            "books/sample.pdf",
            2);
        LocalBookPageResolution first =
            await resolver.ResolveAsync(
                active,
                CancellationToken.None).ConfigureAwait(false);
        LocalBookPageResolution cached =
            await resolver.ResolveAsync(
                active,
                CancellationToken.None).ConfigureAwait(false);
        Require(
            first.Success
            && cached.Success
            && extractionCount == 1
            && first.CurrentPage?["page"]?.GetValue<int>() == 2
            && first.CurrentPage?["text"]?.GetValue<string>()
                == "sample.pdf page 2"
            && first.CurrentPage?["textSource"]?.GetValue<string>()
                == "windows-local-pdf",
            "local-page-resolver-uses-reader-one-based-page-and-cache",
            checks);

        await File.AppendAllTextAsync(bookPath, "changed")
            .ConfigureAwait(false);
        LocalBookPageResolution changed =
            await resolver.ResolveAsync(
                active,
                CancellationToken.None).ConfigureAwait(false);
        Require(
            changed.Success && extractionCount == 2,
            "local-page-resolver-invalidates-cache-on-file-change",
            checks);

        bool rejectsTraversal = !LocalBookPageResolver
            .TryResolvePdfPath(
                libraryRoot,
                "../sample.pdf",
                out _,
                out _);
        bool rejectsAbsolute = !LocalBookPageResolver
            .TryResolvePdfPath(
                libraryRoot,
                bookPath,
                out _,
                out _);
        bool rejectsNonPdf = !LocalBookPageResolver
            .TryResolvePdfPath(
                libraryRoot,
                "books/sample.epub",
                out _,
                out _);
        Require(
            rejectsTraversal && rejectsAbsolute && rejectsNonPdf,
            "local-page-resolver-fails-closed-on-untrusted-paths",
            checks);

        LocalBookPageResolution invalidPage =
            await resolver.ResolveAsync(
                Reading("books/sample.pdf", 0),
                CancellationToken.None).ConfigureAwait(false);
        Require(
            invalidPage.Applicable
            && !invalidPage.Success
            && invalidPage.FailureReason
                == "local-pdf-page-invalid",
            "local-page-resolver-rejects-non-one-based-page",
            checks);

        Require(
            LocalBookPageResolverOptions.DefaultLibraryRoot
                == @"C:\obsidian"
            && Path.IsPathFullyQualified(
                LocalBookPageResolverOptions
                    .DefaultPythonExecutable)
            && LocalBookPageResolverOptions
                .DefaultPythonExecutable.EndsWith(
                    @"Python313\python.exe",
                    StringComparison.OrdinalIgnoreCase),
            "local-page-resolver-has-explicit-configurable-defaults",
            checks);
    }

    private static DirectActiveReading Reading(
        string file,
        int page) =>
        new(
            "pdf",
            file,
            "Sample",
            JsonSerializer.SerializeToElement(page),
            "unknown",
            null,
            1_750_000_000_000);

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
}
