using System.Diagnostics;
using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.AspNetCore.Http;

namespace BwReader.ComputerVoiceAudio;

internal static class DirectSnapshotMarkdown
{
    internal const string ReaderOrigin =
        "https://bwicarus.taile44d0c.ts.net";

    private static readonly UTF8Encoding Utf8WithoutBom = new(
        encoderShouldEmitUTF8Identifier: false);

    internal static string PathFor(string statePath) =>
        System.IO.Path.Combine(
            System.IO.Path.GetDirectoryName(statePath)
                ?? throw new ArgumentException(
                    "snapshot state path has no directory",
                    nameof(statePath)),
            FileDirectSnapshotContextAdapter.MarkdownFileName);

    internal static async Task WriteBestEffortAsync(
        JsonObject snapshot,
        string statePath,
        CancellationToken cancellationToken)
    {
        string path = PathFor(statePath);
        string temporaryPath = path + ".tmp";
        try
        {
            await File.WriteAllTextAsync(
                temporaryPath,
                Render(snapshot),
                Utf8WithoutBom,
                cancellationToken).ConfigureAwait(false);
            File.Move(temporaryPath, path, overwrite: true);
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (Exception exception) when (
            exception is IOException
            or UnauthorizedAccessException
            or ArgumentException
            or InvalidOperationException)
        {
            // The JSON file is the authoritative MCP state. A presentation
            // write failure must not make Reader context disappear from AI.
            try
            {
                File.Delete(temporaryPath);
            }
            catch
            {
            }
        }
    }

    internal static string Render(JsonObject snapshot)
    {
        StringBuilder output = new();
        output.AppendLine("# Reader 实时快照");
        output.AppendLine();
        output.AppendLine(
            "> Windows 本地只读投影；JSON 快照仍是 MCP 唯一真值。"
            + " 文件随 Reader 事件原子更新。");
        output.AppendLine();
        AppendField(output, "状态", Text(snapshot["contextStatus"]));
        AppendField(output, "修订", Text(snapshot["revision"]));
        AppendField(output, "更新时间", Text(snapshot["updatedAtUtc"]));

        JsonObject? active = snapshot["activeReading"] as JsonObject;
        output.AppendLine();
        output.AppendLine("## 当前阅读位置");
        output.AppendLine();
        if (active is null)
        {
            output.AppendLine("_尚未收到活动书页。_");
        }
        else
        {
            AppendField(output, "书名", Text(active["title"]));
            AppendField(output, "文件", Text(active["file"]));
            AppendField(output, "页/章节", Text(active["page"]));
            AppendField(output, "类型", Text(active["kind"]));
            AppendField(output, "新鲜", Text(active["fresh"]));
            AppendField(output, "年龄（秒）", Text(active["ageSec"]));
        }

        JsonObject? selection = snapshot["selection"] as JsonObject;
        output.AppendLine();
        output.AppendLine("## 当前选区");
        output.AppendLine();
        AppendField(output, "状态", Text(selection?["state"]));
        string? selectedText = Text(selection?["text"]);
        output.AppendLine(string.IsNullOrEmpty(selectedText)
            ? "_当前没有可用选区正文。_"
            : Fenced(selectedText));

        JsonObject? page = snapshot["currentPage"] as JsonObject;
        output.AppendLine();
        output.AppendLine("## 当前页正文");
        output.AppendLine();
        if (page is null)
        {
            output.AppendLine("_尚未收到稳定页正文。_");
        }
        else
        {
            DirectSnapshotTerminal.ReaderTextProjection projection =
                DirectSnapshotTerminal.ParseAnnotatedReaderText(
                    Text(page["text"]) ?? "");
            AppendField(output, "稳定", Text(page["stable"]));
            AppendField(
                output,
                "正文可用",
                Text(page["textAvailable"]));
            AppendField(output, "来源", Text(page["textSource"]));
            AppendField(
                output,
                "降级原因",
                Text(page["fallbackReason"]));
            AppendField(output, "已截断", Text(page["truncated"]));
            output.AppendLine();
            output.AppendLine(Fenced(
                DirectSnapshotTerminal.ReadableReaderText(
                    projection.PlainText)));
            AppendVisual(output, page["visual"] as JsonObject);
            AppendEmbeds(
                output,
                page["embeds"] as JsonObject,
                projection);
            AppendViewport(output, page["viewport"] as JsonObject);
        }

        output.AppendLine();
        output.AppendLine("## 最近阅读器事件");
        output.AppendLine();
        JsonObject? latest = snapshot["latestEvent"] as JsonObject;
        if (latest is null)
        {
            output.AppendLine("_当前没有阅读器事件。_");
        }
        else
        {
            AppendField(output, "事件", Text(
                latest["event"] ?? latest["type"]));
            AppendField(output, "事件 ID", Text(latest["id"]));
            AppendField(output, "状态", Text(latest["state"]));
            AppendField(output, "原因", Text(latest["reason"]));
            AppendField(output, "时间", Text(latest["ts"]));
        }
        return output.ToString();
    }

    private static void AppendVisual(
        StringBuilder output,
        JsonObject? visual)
    {
        output.AppendLine();
        output.AppendLine("## 绘图与图片");
        output.AppendLine();
        if (visual is null)
        {
            output.AppendLine("_当前页没有视觉引用。_");
            return;
        }
        AppendField(output, "有墨迹", Text(visual["has_ink"]));
        string? image = Text(visual["page_image"]);
        if (!string.IsNullOrWhiteSpace(image))
        {
            string absolute =
                DirectSnapshotTerminal.AbsoluteImageUrl(image);
            output.AppendLine(
                $"- 页图：[在 Reader 中打开原图](<{absolute}>)");
            output.AppendLine(
                "- 说明：页图接口受 Reader 登录保护；本地 Markdown "
                + "预览不会携带跨站登录 Cookie，因此不伪装成可内嵌图片。");
        }
        JsonObject? drawing = visual["drawing"] as JsonObject;
        output.AppendLine();
        output.AppendLine("### 绘图状态");
        output.AppendLine();
        if (drawing is null)
        {
            output.AppendLine("_当前没有绘图状态。_");
            return;
        }
        AppendField(output, "新鲜度", Text(drawing["freshness"]));
        AppendField(output, "正在绘制", Text(drawing["inProgress"]));
        AppendField(output, "稳定", Text(drawing["stable"]));
        AppendField(
            output,
            "绘图修订",
            Text(drawing["drawingRevision"]));
    }

    private static void AppendEmbeds(
        StringBuilder output,
        JsonObject? embeds,
        DirectSnapshotTerminal.ReaderTextProjection projection)
    {
        output.AppendLine();
        output.AppendLine("## 高亮与嵌入内容");
        output.AppendLine();
        AppendField(
            output,
            "已锚定高亮",
            Text(embeds?["highlights"]));
        AppendField(output, "附属块", Text(embeds?["blocks"]));

        output.AppendLine();
        output.AppendLine("### 协议高亮正文");
        output.AppendLine();
        if (projection.Highlights.Count == 0)
        {
            output.AppendLine("_无。_");
        }
        else
        {
            int index = 1;
            foreach (
                DirectSnapshotTerminal.ReaderMarkedContent highlight
                in projection.Highlights)
            {
                output.Append(index++);
                output.Append(". ");
                output.AppendLine(EscapeInline(highlight.Text));
                if (!string.IsNullOrEmpty(highlight.Attributes))
                {
                    output.AppendLine(
                        "   - 属性："
                        + EscapeInline(highlight.Attributes));
                }
            }
        }

        output.AppendLine();
        output.AppendLine("### 协议卡片与便签");
        output.AppendLine();
        if (projection.Cards.Count == 0)
        {
            output.AppendLine("_无。_");
        }
        else
        {
            int index = 1;
            foreach (
                DirectSnapshotTerminal.ReaderMarkedContent card
                in projection.Cards)
            {
                output.Append(index++);
                output.Append(". ");
                output.AppendLine(EscapeInline(card.Text));
                if (!string.IsNullOrEmpty(card.Attributes))
                {
                    output.AppendLine(
                        "   - 属性："
                        + EscapeInline(card.Attributes));
                }
            }
        }

        JsonArray? unanchored = embeds?["unanchored"] as JsonArray;
        if (unanchored is not { Count: > 0 })
        {
            return;
        }
        output.AppendLine();
        output.AppendLine("### 未锚定内容");
        output.AppendLine();
        foreach (JsonNode? node in unanchored)
        {
            JsonObject? item = node as JsonObject;
            output.Append("- ");
            output.Append(EscapeInline(
                Text(item?["text"])
                ?? Text(item?["id"])
                ?? "未命名"));
            output.Append("（");
            output.Append(EscapeInline(
                Text(item?["_reason"]) ?? "unknown"));
            output.AppendLine("）");
        }
    }

    private static void AppendViewport(
        StringBuilder output,
        JsonObject? viewport)
    {
        output.AppendLine();
        output.AppendLine("## EPUB 视口");
        output.AppendLine();
        if (viewport is null)
        {
            output.AppendLine("_当前不是 EPUB 视口快照。_");
            return;
        }
        AppendField(output, "中心段", Text(viewport["center"]));
        AppendField(output, "范围起点", Text(viewport["from"]));
        AppendField(output, "范围终点（不含）", Text(viewport["to"]));
        AppendField(output, "总段数", Text(viewport["total"]));
        AppendField(output, "前后窗口", Text(viewport["pad"]));
    }

    private static void AppendField(
        StringBuilder output,
        string label,
        string? value) =>
        output.AppendLine(
            $"- {label}：{EscapeInline(value ?? "—")}");

    private static string Fenced(
        string value,
        string language = "text")
    {
        int longest = 0;
        int current = 0;
        foreach (char character in value)
        {
            if (character == '`')
            {
                current++;
                longest = Math.Max(longest, current);
            }
            else
            {
                current = 0;
            }
        }
        string fence = new(
            '`',
            Math.Max(3, longest + 1));
        return $"{fence}{language}\n{value}\n{fence}";
    }

    private static string EscapeInline(string value) =>
        value.Replace("\\", "\\\\", StringComparison.Ordinal)
            .Replace("`", "\\`", StringComparison.Ordinal)
            .Replace("\r", " ", StringComparison.Ordinal)
            .Replace("\n", " ", StringComparison.Ordinal);

    internal static string? Text(JsonNode? node)
    {
        if (node is null)
        {
            return null;
        }
        try
        {
            return node.GetValue<string>();
        }
        catch (InvalidOperationException)
        {
            return node.ToJsonString();
        }
    }
}

internal static class DirectSnapshotTerminal
{
    private const int MaximumPresentationBytes = 2 * 1024 * 1024;
    private static readonly TimeSpan RefreshInterval =
        TimeSpan.FromMilliseconds(500);
    private static readonly TimeSpan HistoryRefreshInterval =
        TimeSpan.FromSeconds(2);
    private static readonly TimeSpan HistoryPollTimeout =
        TimeSpan.FromSeconds(5);
    private const int MaximumDisplayedHistoryMessages = 40;

    internal sealed record ReaderMarkedContent(
        string Attributes,
        string Text);

    internal sealed record ReaderTextProjection(
        string PlainText,
        IReadOnlyList<ReaderMarkedContent> Highlights,
        IReadOnlyList<ReaderMarkedContent> Cards);

    internal static async Task<int> RunAsync(
        string statePath,
        CancellationToken cancellationToken)
    {
        Console.InputEncoding = new UTF8Encoding(false);
        Console.OutputEncoding = new UTF8Encoding(false);
        Console.Title = "Reader 实时上下文";

        string fullPath = System.IO.Path.GetFullPath(statePath);
        using Mutex singleton = new(
            initiallyOwned: true,
            name: ViewerMutexName(fullPath),
            createdNew: out bool createdNew);
        if (!createdNew)
        {
            return 0;
        }

        using CancellationTokenSource lifetime =
            CancellationTokenSource.CreateLinkedTokenSource(
                cancellationToken);
        ConsoleCancelEventHandler cancel = (_, args) =>
        {
            args.Cancel = true;
            lifetime.Cancel();
        };
        Console.CancelKeyPress += cancel;
        CodexVoiceHistoryReader historyReader =
            CreateVoiceHistoryReader();
        Task<CodexVoiceHistorySnapshot>? historyPoll = null;
        CodexVoiceHistorySnapshot? history = null;
        DateTimeOffset nextHistoryPoll = DateTimeOffset.MinValue;
        try
        {
            string? previousProjection = null;
            while (!lifetime.IsCancellationRequested)
            {
                if (
                    historyPoll is null
                    && DateTimeOffset.UtcNow >= nextHistoryPoll
                )
                {
                    historyPoll = PollHistoryWithTimeoutAsync(
                        historyReader,
                        lifetime.Token);
                }
                if (historyPoll is { IsCompleted: true })
                {
                    try
                    {
                        history = await historyPoll.ConfigureAwait(false);
                    }
                    catch (OperationCanceledException)
                        when (!lifetime.IsCancellationRequested)
                    {
                        // Preserve the last completed projection. The reader
                        // itself marks bad file/app-server reads stale/gap.
                    }
                    catch (
                        Exception exception
                    ) when (
                        exception is IOException
                        or UnauthorizedAccessException
                        or JsonException
                        or InvalidDataException
                        or InvalidOperationException
                        or System.ComponentModel.Win32Exception
                    )
                    {
                        // History is supplemental. Reader snapshot display
                        // remains available if Codex history is unavailable.
                    }
                    historyPoll = null;
                    nextHistoryPoll =
                        DateTimeOffset.UtcNow
                        + HistoryRefreshInterval;
                }

                string projection;
                try
                {
                    projection = RenderFromFile(fullPath, history);
                }
                catch (Exception exception) when (
                    exception is IOException
                    or UnauthorizedAccessException
                    or JsonException
                    or InvalidOperationException)
                {
                    projection =
                        "Reader 实时上下文\n\n"
                        + "快照暂时不可读："
                        + exception.Message
                        + "\n\n等待下一次 Reader 更新……";
                }
                if (!string.Equals(
                    projection,
                    previousProjection,
                    StringComparison.Ordinal))
                {
                    ClearScreen();
                    Console.Write(projection);
                    Console.WriteLine();
                    Console.WriteLine();
                    Console.WriteLine(
                        "此窗口只读并自动刷新；Ctrl+C 关闭。");
                    previousProjection = projection;
                }
                await Task.Delay(
                    RefreshInterval,
                    lifetime.Token).ConfigureAwait(false);
            }
            return 0;
        }
        catch (OperationCanceledException)
            when (lifetime.IsCancellationRequested)
        {
            return 0;
        }
        finally
        {
            lifetime.Cancel();
            if (historyPoll is not null)
            {
                try
                {
                    _ = await historyPoll.ConfigureAwait(false);
                }
                catch
                {
                }
            }
            await historyReader.DisposeAsync().ConfigureAwait(false);
            Console.CancelKeyPress -= cancel;
            singleton.ReleaseMutex();
        }
    }

    internal static string Render(
        JsonObject snapshot,
        CodexVoiceHistorySnapshot? history = null)
    {
        StringBuilder output = new();
        string status =
            DirectSnapshotMarkdown.Text(snapshot["contextStatus"])
            ?? "pending";
        output.AppendLine("Reader 实时上下文");
        output.AppendLine(
            $"状态 {status}  |  修订 "
            + (DirectSnapshotMarkdown.Text(snapshot["revision"]) ?? "0")
            + "  |  "
            + (DirectSnapshotMarkdown.Text(snapshot["updatedAtUtc"]) ?? "—"));

        Section(output, "当前阅读位置");
        JsonObject? active = snapshot["activeReading"] as JsonObject;
        if (active is null)
        {
            output.AppendLine("尚未收到活动书页。");
        }
        else
        {
            Field(output, "书名", active["title"]);
            Field(output, "文件", active["file"]);
            Field(output, "页/章节", active["page"]);
            Field(output, "类型", active["kind"]);
            Field(output, "新鲜", active["fresh"]);
            Field(output, "年龄（秒）", active["ageSec"]);
        }

        Section(output, "当前选区");
        JsonObject? selection = snapshot["selection"] as JsonObject;
        Field(output, "状态", selection?["state"]);
        string selectionText =
            DirectSnapshotMarkdown.Text(selection?["text"]) ?? "";
        output.AppendLine(string.IsNullOrWhiteSpace(selectionText)
            ? "（无可用选区）"
            : selectionText);

        JsonObject? page = snapshot["currentPage"] as JsonObject;
        string annotatedText =
            DirectSnapshotMarkdown.Text(page?["text"]) ?? "";
        ReaderTextProjection projection =
            ParseAnnotatedReaderText(annotatedText);
        Section(output, "当前页正文");
        if (page is null)
        {
            output.AppendLine("尚未收到稳定页正文。");
        }
        else
        {
            Field(output, "稳定", page["stable"]);
            Field(output, "正文可用", page["textAvailable"]);
            Field(output, "来源", page["textSource"]);
            Field(output, "降级原因", page["fallbackReason"]);
            Field(output, "已截断", page["truncated"]);
            output.AppendLine();
            output.AppendLine(string.IsNullOrEmpty(
                projection.PlainText)
                ? "（当前页无文字层）"
                : ReadableReaderText(projection.PlainText));
        }

        AppendEmbeds(output, page, projection);
        AppendVisual(output, page?["visual"] as JsonObject);
        AppendViewport(output, page?["viewport"] as JsonObject);
        AppendLatestEvent(
            output,
            snapshot["latestEvent"] as JsonObject);
        AppendVoiceHistory(output, history);
        return output.ToString();
    }

    internal static string AbsoluteImageUrl(string value)
    {
        if (value.StartsWith("/", StringComparison.Ordinal))
        {
            return DirectSnapshotMarkdown.ReaderOrigin + value;
        }
        return value;
    }

    private static string RenderFromFile(
        string statePath,
        CodexVoiceHistorySnapshot? history)
    {
        if (!File.Exists(statePath))
        {
            return "Reader 实时上下文\n\n等待首个 Reader 快照……";
        }
        FileInfo info = new(statePath);
        if (info.Length is <= 0 or > MaximumPresentationBytes)
        {
            throw new IOException("快照文件大小无效");
        }
        JsonObject snapshot = JsonNode.Parse(
            File.ReadAllText(statePath, Encoding.UTF8)) as JsonObject
            ?? throw new JsonException("快照根节点不是对象");
        return Render(snapshot, history);
    }

    private static async Task<CodexVoiceHistorySnapshot>
        PollHistoryWithTimeoutAsync(
            CodexVoiceHistoryReader reader,
            CancellationToken cancellationToken)
    {
        using CancellationTokenSource timeout =
            CancellationTokenSource.CreateLinkedTokenSource(
                cancellationToken);
        timeout.CancelAfter(HistoryPollTimeout);
        return await reader.PollAsync(timeout.Token)
            .ConfigureAwait(false);
    }

    private static CodexVoiceHistoryReader CreateVoiceHistoryReader()
    {
        string userProfile = Environment.GetFolderPath(
            Environment.SpecialFolder.UserProfile);
        string codexHome = System.IO.Path.Combine(
            userProfile,
            ".codex");
        FileCodexVoiceHistorySource source = new(
            System.IO.Path.Combine(
                codexHome,
                ".codex-global-state.json"),
            System.IO.Path.Combine(
                codexHome,
                "realtime-voice-continuity.json"));
        string? executable = FindCodexExecutable();
        return new CodexVoiceHistoryReader(
            source,
            executable is null
                ? null
                : new CodexAppServerReadOnlyHistoryClient(
                    new ProcessCodexAppServerTransportFactory(
                        executable)));
    }

    private static string? FindCodexExecutable()
    {
        string appData = Environment.GetFolderPath(
            Environment.SpecialFolder.ApplicationData);
        string packageRoot = System.IO.Path.Combine(
            appData,
            "npm",
            "node_modules",
            "@openai",
            "codex",
            "node_modules");
        string[] candidates =
        [
            System.IO.Path.Combine(
                packageRoot,
                "@openai",
                "codex-win32-x64",
                "vendor",
                "x86_64-pc-windows-msvc",
                "bin",
                "codex.exe"),
            System.IO.Path.Combine(
                packageRoot,
                "@openai",
                "codex-win32-arm64",
                "vendor",
                "aarch64-pc-windows-msvc",
                "bin",
                "codex.exe"),
        ];
        return candidates.FirstOrDefault(File.Exists);
    }

    private static void AppendEmbeds(
        StringBuilder output,
        JsonObject? page,
        ReaderTextProjection projection)
    {
        Section(output, "高亮与嵌入内容");
        JsonObject? embeds = page?["embeds"] as JsonObject;
        Field(output, "已锚定高亮", embeds?["highlights"]);
        Field(output, "附属块", embeds?["blocks"]);

        if (projection.Highlights.Count == 0)
        {
            output.AppendLine("高亮正文：（无）");
        }
        else
        {
            output.AppendLine("高亮正文：");
            int index = 1;
            foreach (
                ReaderMarkedContent highlight
                in projection.Highlights)
            {
                output.Append("  ");
                output.Append(index++);
                output.Append(". ");
                output.AppendLine(highlight.Text);
                if (!string.IsNullOrEmpty(highlight.Attributes))
                {
                    output.AppendLine(
                        "     " + highlight.Attributes);
                }
            }
        }

        if (projection.Cards.Count > 0)
        {
            output.AppendLine("附属卡片/便签：");
            int index = 1;
            foreach (ReaderMarkedContent card in projection.Cards)
            {
                output.Append("  ");
                output.Append(index++);
                output.Append(". ");
                if (!string.IsNullOrEmpty(card.Attributes))
                {
                    output.Append('[');
                    output.Append(card.Attributes);
                    output.Append("] ");
                }
                output.AppendLine(card.Text);
            }
        }

        JsonArray? unanchored = embeds?["unanchored"] as JsonArray;
        if (unanchored is { Count: > 0 })
        {
            output.AppendLine("未锚定内容：");
            foreach (JsonNode? node in unanchored)
            {
                JsonObject? item = node as JsonObject;
                output.Append("  - ");
                output.Append(
                    DirectSnapshotMarkdown.Text(item?["text"])
                    ?? DirectSnapshotMarkdown.Text(item?["id"])
                    ?? "未命名");
                output.Append("（");
                output.Append(
                    DirectSnapshotMarkdown.Text(item?["_reason"])
                    ?? "unknown");
                output.AppendLine("）");
            }
        }
    }

    private static void AppendVisual(
        StringBuilder output,
        JsonObject? visual)
    {
        Section(output, "绘图与图片");
        if (visual is null)
        {
            output.AppendLine("当前页没有视觉引用。");
            return;
        }
        Field(output, "有墨迹", visual["has_ink"]);
        JsonObject? drawing = visual["drawing"] as JsonObject;
        Field(output, "绘图新鲜度", drawing?["freshness"]);
        Field(output, "正在绘制", drawing?["inProgress"]);
        Field(output, "稳定版本", drawing?["drawingRevision"]);

        string? image = DirectSnapshotMarkdown.Text(
            visual["page_image"]);
        if (string.IsNullOrWhiteSpace(image))
        {
            output.AppendLine("图片链接：（无）");
            return;
        }
        string url = AbsoluteImageUrl(image);
        output.Append("图片链接：");
        output.AppendLine(Hyperlink(url, "点击打开当前页图片"));
        output.AppendLine(url);
    }

    private static void AppendViewport(
        StringBuilder output,
        JsonObject? viewport)
    {
        Section(output, "EPUB 视口");
        if (viewport is null)
        {
            output.AppendLine("（当前不是 EPUB 视口快照）");
            return;
        }
        Field(output, "中心段", viewport["center"]);
        Field(output, "范围起点", viewport["from"]);
        Field(output, "范围终点（不含）", viewport["to"]);
        Field(output, "总段数", viewport["total"]);
        Field(output, "前后窗口", viewport["pad"]);
    }

    private static void AppendLatestEvent(
        StringBuilder output,
        JsonObject? latest)
    {
        Section(output, "最近阅读器事件");
        if (latest is null)
        {
            output.AppendLine("（无）");
            return;
        }
        Field(output, "事件", latest["event"] ?? latest["type"]);
        Field(output, "事件 ID", latest["id"]);
        Field(output, "状态", latest["state"]);
        Field(output, "原因", latest["reason"]);
        Field(output, "时间", latest["ts"]);
    }

    private static void AppendVoiceHistory(
        StringBuilder output,
        CodexVoiceHistorySnapshot? history)
    {
        Section(output, "语音层最近对话");
        if (history is null)
        {
            output.AppendLine("（正在连接本地语音历史）");
        }
        else
        {
            output.AppendLine(
                "绑定："
                + history.BindingStatus.ToString().ToLowerInvariant()
                + "  |  数据："
                + history.VoiceRecentStatus
                    .ToString().ToLowerInvariant()
                + (history.Gap ? "  |  有缺口/陈旧" : ""));
            AppendConversation(
                output,
                history.VoiceRecent,
                emptyMessage: "（当前线程没有可用的语音层记录）");
        }

        Section(output, "Codex 线程历史");
        if (history is null)
        {
            output.AppendLine("（正在启动只读历史连接）");
            return;
        }
        output.AppendLine(
            "数据："
            + history.CodexHistoryStatus
                .ToString().ToLowerInvariant()
            + "  |  线程："
            + ShortThreadId(history.ThreadId));
        AppendConversation(
            output,
            history.CodexHistory,
            emptyMessage: "（当前没有可显示的完整线程历史）");
    }

    private static void AppendConversation(
        StringBuilder output,
        IReadOnlyList<CodexVoiceHistoryMessage> messages,
        string emptyMessage)
    {
        if (messages.Count == 0)
        {
            output.AppendLine(emptyMessage);
            return;
        }
        int omitted = Math.Max(
            0,
            messages.Count - MaximumDisplayedHistoryMessages);
        if (omitted > 0)
        {
            output.AppendLine($"（更早 {omitted} 条已在此视图折叠）");
        }
        foreach (
            CodexVoiceHistoryMessage message
            in messages.Skip(omitted)
        )
        {
            output.AppendLine();
            output.Append(
                message.Role == "user"
                    ? "用户："
                    : "助手：");
            output.AppendLine(SafeTerminalText(message.Text));
        }
    }

    private static string ShortThreadId(string? threadId)
    {
        if (string.IsNullOrEmpty(threadId))
        {
            return "—";
        }
        return threadId.Length <= 13
            ? threadId
            : threadId[..8] + "…" + threadId[^4..];
    }

    private static string SafeTerminalText(string value)
    {
        StringBuilder safe = new(value.Length);
        foreach (char character in value)
        {
            if (
                character is '\r' or '\n' or '\t'
                || !char.IsControl(character)
            )
            {
                safe.Append(character);
            }
            else
            {
                safe.Append('�');
            }
        }
        return safe.ToString();
    }

    private static void Section(
        StringBuilder output,
        string title)
    {
        output.AppendLine();
        output.AppendLine(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
        output.AppendLine("【" + title + "】");
    }

    private static void Field(
        StringBuilder output,
        string label,
        JsonNode? value)
    {
        output.Append(label);
        output.Append("：");
        output.AppendLine(
            DirectSnapshotMarkdown.Text(value) ?? "—");
    }

    private static string Hyperlink(
        string url,
        string label)
    {
        if (
            Console.IsOutputRedirected
            || !Uri.TryCreate(url, UriKind.Absolute, out _)
            || url.Any(character =>
                character is '\u001b' or '\a'
                || char.IsControl(character))
        )
        {
            return label;
        }
        return "\u001b]8;;"
            + url
            + "\u001b\\"
            + label
            + "\u001b]8;;\u001b\\";
    }

    internal static string PlainReaderText(string value) =>
        ParseAnnotatedReaderText(value).PlainText;

    internal static string ReadableReaderText(string value) =>
        NormalizePresentationLayout(value);

    internal static string UnescapeReaderText(string value) =>
        DecodeEscapedReaderText(value);

    internal static ReaderTextProjection ParseAnnotatedReaderText(
        string value)
    {
        StringBuilder plain = new(value.Length);
        List<ReaderMarkedContent> highlights = [];
        List<ReaderMarkedContent> cards = [];
        StringBuilder? markedText = null;
        string? markedAttributes = null;
        string? markedKind = null;
        for (int index = 0; index < value.Length;)
        {
            char current = value[index];
            if (current == '\\')
            {
                if (index + 1 >= value.Length)
                {
                    throw ReaderTextInvalid();
                }
                char next = value[index + 1];
                if (next is '\\' or '⟦' or '⟧')
                {
                    AppendReaderCharacter(
                        plain,
                        markedText,
                        next);
                }
                else
                {
                    // Unknown escape sequences are ordinary Reader text.
                    // Preserve both characters exactly, matching the Pi
                    // contract's single left-to-right scan.
                    AppendReaderCharacter(
                        plain,
                        markedText,
                        current);
                    AppendReaderCharacter(
                        plain,
                        markedText,
                        next);
                }
                index += 2;
                continue;
            }
            if (current == '⟦')
            {
                int close = value.IndexOf('⟧', index + 1);
                if (close < 0)
                {
                    throw ReaderTextInvalid();
                }
                ReadOnlySpan<char> marker = value.AsSpan(
                    index + 1,
                    close - index - 1);
                if (StartsMarker(marker, "HIGHLIGHT"))
                {
                    if (markedKind is not null)
                    {
                        throw ReaderTextInvalid();
                    }
                    markedKind = "highlight";
                    markedAttributes =
                        marker["HIGHLIGHT".Length..]
                            .ToString()
                            .Trim();
                    markedText = new StringBuilder();
                }
                else if (marker.SequenceEqual("/HIGHLIGHT"))
                {
                    if (
                        markedKind != "highlight"
                        || markedText is null
                    )
                    {
                        throw ReaderTextInvalid();
                    }
                    highlights.Add(new ReaderMarkedContent(
                        markedAttributes ?? "",
                        markedText.ToString()));
                    markedKind = null;
                    markedAttributes = null;
                    markedText = null;
                }
                else if (StartsMarker(marker, "CARD_START"))
                {
                    if (markedKind is not null)
                    {
                        throw ReaderTextInvalid();
                    }
                    markedKind = "card";
                    markedAttributes =
                        marker["CARD_START".Length..]
                            .ToString()
                            .Trim();
                    markedText = new StringBuilder();
                }
                else if (marker.SequenceEqual("CARD_END"))
                {
                    if (
                        markedKind != "card"
                        || markedText is null
                    )
                    {
                        throw ReaderTextInvalid();
                    }
                    cards.Add(new ReaderMarkedContent(
                        markedAttributes ?? "",
                        markedText.ToString()));
                    markedKind = null;
                    markedAttributes = null;
                    markedText = null;
                }
                else
                {
                    // Original brackets are always backslash-escaped by the
                    // producer. An unescaped unknown marker is malformed, so
                    // displaying a guessed projection would be unsafe.
                    throw ReaderTextInvalid();
                }
                index = close + 1;
                continue;
            }
            if (current == '⟧')
            {
                throw ReaderTextInvalid();
            }
            AppendReaderCharacter(
                plain,
                markedText,
                current);
            index++;
        }
        if (markedKind is not null)
        {
            throw ReaderTextInvalid();
        }
        return new ReaderTextProjection(
            plain.ToString(),
            highlights,
            cards);
    }

    private static string DecodeEscapedReaderText(string value)
    {
        StringBuilder output = new(value.Length);
        for (int index = 0; index < value.Length;)
        {
            char current = value[index];
            if (current != '\\')
            {
                output.Append(current);
                index++;
                continue;
            }
            if (index + 1 >= value.Length)
            {
                throw ReaderTextInvalid();
            }
            char next = value[index + 1];
            if (next is '\\' or '⟦' or '⟧')
            {
                output.Append(next);
            }
            else
            {
                output.Append(current);
                output.Append(next);
            }
            index += 2;
        }
        return output.ToString();
    }

    private static string NormalizePresentationLayout(string value)
    {
        // This is deliberately presentation-only. The JSON/MCP snapshot keeps
        // the exact character layer. We remove only narrowly recognizable PDF
        // extraction debris and reconstruct glyph-stacked headings.
        string normalized = value
            .Replace("\r\n", "\n", StringComparison.Ordinal)
            .Replace('\r', '\n');
        string[] lines = normalized.Split('\n');
        List<string> presented = new(lines.Length);
        for (int index = 0; index < lines.Length;)
        {
            string line = lines[index];
            if (
                IsRepeatedPipeArtifact(line)
                || IsBracketScannerArtifact(line)
            )
            {
                index++;
                continue;
            }

            int followingGlyphCount = CountGlyphStack(
                lines,
                index + 1);
            if (
                followingGlyphCount >= 3
                && TryDetachTrailingAsciiGlyph(
                    line,
                    out string prefix,
                    out string trailingGlyph)
            )
            {
                List<string> glyphs =
                [
                    trailingGlyph,
                    .. lines
                        .Skip(index + 1)
                        .Take(followingGlyphCount)
                        .Select(item => item.Trim()),
                ];
                if (IsLikelyScannerGlyphStack(glyphs))
                {
                    string joined = JoinScannerGlyphs(glyphs);
                    presented.Add(
                        string.IsNullOrWhiteSpace(prefix)
                            ? joined
                            : prefix.TrimEnd() + " " + joined);
                    index += followingGlyphCount + 1;
                    continue;
                }
            }

            int glyphCount = CountGlyphStack(lines, index);
            if (glyphCount >= 4)
            {
                List<string> glyphs = lines
                    .Skip(index)
                    .Take(glyphCount)
                    .Select(item => item.Trim())
                    .ToList();
                if (IsLikelyScannerGlyphStack(glyphs))
                {
                    presented.Add(JoinScannerGlyphs(glyphs));
                    index += glyphCount;
                    continue;
                }
            }

            presented.Add(line);
            index++;
        }

        // Scanner artifacts can leave a visual hole. Keep intentional
        // paragraph breaks, but never emit more than one empty separator.
        List<string> compacted = new(presented.Count);
        foreach (string line in presented)
        {
            if (
                line.Length == 0
                && compacted.Count > 0
                && compacted[^1].Length == 0
            )
            {
                continue;
            }
            compacted.Add(line);
        }
        return string.Join('\n', compacted);
    }

    private static int CountGlyphStack(
        IReadOnlyList<string> lines,
        int start)
    {
        int count = 0;
        while (
            start + count < lines.Count
            && IsSingleScannerGlyph(lines[start + count])
        )
        {
            count++;
        }
        return count;
    }

    private static bool IsSingleScannerGlyph(string value)
    {
        string trimmed = value.Trim();
        if (
            trimmed.Length == 0
            || StringInfo.ParseCombiningCharacters(trimmed).Length != 1
        )
        {
            return false;
        }
        Rune rune = trimmed.EnumerateRunes().First();
        return Rune.IsLetterOrDigit(rune)
            || IsCjkRune(rune);
    }

    private static bool IsLikelyScannerGlyphStack(
        IReadOnlyList<string> glyphs)
    {
        int cjk = 0;
        int uppercaseAscii = 0;
        int asciiDigits = 0;
        foreach (string glyph in glyphs)
        {
            Rune rune = glyph.EnumerateRunes().First();
            if (IsCjkRune(rune))
            {
                cjk++;
            }
            else if (
                rune.Value is >= 'A' and <= 'Z'
            )
            {
                uppercaseAscii++;
            }
            else if (rune.Value is >= '0' and <= '9')
            {
                asciiDigits++;
            }
        }
        // Lower-case one-symbol lines are common in source code and formulas.
        // An ASCII-only stack is reconstructed only when it also contains a
        // heading number; this deliberately leaves X/Y/Z-style formulas alone.
        return cjk >= 2
            || (uppercaseAscii >= 3 && asciiDigits >= 1);
    }

    private static string JoinScannerGlyphs(
        IReadOnlyList<string> glyphs)
    {
        StringBuilder output = new();
        bool previousCjk = false;
        bool previousAscii = false;
        foreach (string glyph in glyphs)
        {
            Rune rune = glyph.EnumerateRunes().First();
            bool currentCjk = IsCjkRune(rune);
            bool currentAscii =
                rune.Value <= 0x7f
                && Rune.IsLetterOrDigit(rune);
            if (
                output.Length > 0
                && (
                    (previousCjk && currentAscii)
                    || (previousAscii && currentCjk)
                )
            )
            {
                output.Append(' ');
            }
            output.Append(glyph);
            previousCjk = currentCjk;
            previousAscii = currentAscii;
        }
        return output.ToString();
    }

    private static bool TryDetachTrailingAsciiGlyph(
        string line,
        out string prefix,
        out string glyph)
    {
        prefix = line;
        glyph = "";
        int last = line.Length - 1;
        while (last >= 0 && char.IsWhiteSpace(line[last]))
        {
            last--;
        }
        if (
            last <= 0
            || line[last] > 0x7f
            || !char.IsLetterOrDigit(line[last])
            || !char.IsWhiteSpace(line[last - 1])
        )
        {
            return false;
        }
        glyph = line[last].ToString();
        prefix = line[..last];
        return true;
    }

    private static bool IsRepeatedPipeArtifact(string value)
    {
        string trimmed = value.Trim();
        return trimmed.Length >= 4
            && trimmed.All(character => character == '|');
    }

    private static bool IsBracketScannerArtifact(string value)
    {
        string trimmed = value.Trim();
        if (trimmed.Length < 12)
        {
            return false;
        }
        int brackets = 0;
        int alphanumeric = 0;
        foreach (char character in trimmed)
        {
            if (character is '[' or ']')
            {
                brackets++;
                continue;
            }
            if (char.IsLetterOrDigit(character))
            {
                alphanumeric++;
                continue;
            }
            if (
                char.IsWhiteSpace(character)
                || character is '/' or '\\' or '|'
            )
            {
                continue;
            }
            return false;
        }
        return brackets >= 8 && alphanumeric <= 6;
    }

    private static bool IsCjkRune(Rune rune) =>
        rune.Value is
            >= 0x2E80 and <= 0x2FFF
            or >= 0x3005 and <= 0x3007
            or >= 0x3040 and <= 0x30FF
            or >= 0x31F0 and <= 0x31FF
            or >= 0x3400 and <= 0x4DBF
            or >= 0x4E00 and <= 0x9FFF
            or >= 0xF900 and <= 0xFAFF
            or >= 0x20000 and <= 0x2FA1F;

    private static void AppendReaderCharacter(
        StringBuilder plain,
        StringBuilder? marked,
        char value)
    {
        plain.Append(value);
        marked?.Append(value);
    }

    private static bool StartsMarker(
        ReadOnlySpan<char> marker,
        string name) =>
        marker.StartsWith(name, StringComparison.Ordinal)
        && (
            marker.Length == name.Length
            || marker[name.Length] is
                ' ' or '\t' or '\r' or '\n' or '\v' or '\f'
        );

    private static InvalidOperationException ReaderTextInvalid() =>
        new(
            "BW_READER_CONTEXT_MARK_ESCAPE_INVALID: "
            + "Reader 正文标记或反转义序列无效");

    private static void ClearScreen()
    {
        if (Console.IsOutputRedirected)
        {
            return;
        }
        Console.Write("\u001b[2J\u001b[H");
    }

    private static string ViewerMutexName(string statePath)
    {
        byte[] digest = SHA256.HashData(
            Encoding.UTF8.GetBytes(
                statePath.ToUpperInvariant()));
        return @"Local\BWReaderContextViewer_"
            + Convert.ToHexString(digest.AsSpan(0, 12));
    }
}

internal sealed class DirectSnapshotViewer : IDisposable
{
    private const string ViewerWindowTitle = "Reader 实时快照";
    private const int MaximumPresentationBytes = 2 * 1024 * 1024;
    internal const string ViewerPath = "/reader-context-view";
    internal const string SnapshotPath =
        "/reader-context-snapshot.json";
    internal const string MarkdownPath =
        "/reader-context-live.md";

    private static readonly byte[] ViewerDocument =
        Encoding.UTF8.GetBytes(
            """
            <!doctype html>
            <html lang="zh-CN">
            <head>
              <meta charset="utf-8">
              <meta name="viewport" content="width=device-width,initial-scale=1">
              <title>Reader 实时快照</title>
              <style>
                :root {
                  color-scheme: dark;
                  font-family: "Segoe UI", "Microsoft YaHei UI", sans-serif;
                  background: #0b1020;
                  color: #e8edf8;
                }
                * { box-sizing: border-box; }
                body { margin: 0; min-height: 100vh; background:
                  radial-gradient(circle at top right, #173464 0, transparent 36rem),
                  #0b1020; }
                header {
                  position: sticky; top: 0; z-index: 2;
                  display: flex; gap: 1rem; align-items: center;
                  justify-content: space-between;
                  padding: .9rem 1.25rem;
                  background: rgba(9, 14, 29, .92);
                  border-bottom: 1px solid #263655;
                  backdrop-filter: blur(12px);
                }
                h1 { margin: 0; font-size: 1.1rem; }
                a { color: #8ec5ff; }
                .status { display: flex; gap: .55rem; align-items: center;
                  flex-wrap: wrap; justify-content: flex-end; }
                .pill { border-radius: 999px; padding: .24rem .65rem;
                  background: #293755; color: #dce8ff; font-size: .82rem; }
                .pill.ready { background: #0e614c; }
                .pill.stale { background: #7a5010; }
                .pill.error { background: #7b2431; }
                main { width: min(1180px, 100%); margin: 0 auto;
                  padding: 1rem; display: grid; gap: 1rem;
                  grid-template-columns: repeat(2, minmax(0, 1fr)); }
                section { min-width: 0; border: 1px solid #263655;
                  border-radius: 14px; padding: 1rem;
                  background: rgba(18, 26, 47, .92);
                  box-shadow: 0 12px 36px rgba(0,0,0,.24); }
                section.wide { grid-column: 1 / -1; }
                h2 { margin: 0 0 .75rem; color: #bad6ff;
                  font-size: 1rem; }
                pre { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere;
                  font: 14px/1.65 "Cascadia Mono", "Microsoft YaHei UI", monospace; }
                .body { min-height: 10rem; max-height: 54vh; overflow: auto;
                  padding: .85rem; border-radius: 10px; background: #080d19;
                  border: 1px solid #1f2a42; }
                .muted { color: #9aa9c2; }
                .warning { color: #ffd37d; }
                ul { margin: .4rem 0 0; padding-left: 1.4rem; }
                li { margin: .35rem 0; white-space: pre-wrap;
                  overflow-wrap: anywhere; }
                img { display: none; margin-top: .8rem; max-width: 100%;
                  max-height: 52vh; object-fit: contain; border-radius: 10px;
                  border: 1px solid #31456a; background: #050811; }
                @media (max-width: 820px) {
                  main { grid-template-columns: 1fr; }
                  section.wide { grid-column: auto; }
                }
              </style>
            </head>
            <body>
              <header>
                <h1>Reader 实时快照</h1>
                <div class="status">
                  <span id="status" class="pill">等待快照</span>
                  <span id="revision" class="pill">revision —</span>
                  <a href="/reader-context-live.md" target="_blank"
                     rel="noreferrer">Markdown</a>
                </div>
              </header>
              <main>
                <section>
                  <h2>当前阅读位置</h2>
                  <pre id="active" class="muted">尚未收到活动书页。</pre>
                </section>
                <section>
                  <h2>当前选区</h2>
                  <pre id="selection" class="muted">当前没有可用选区。</pre>
                </section>
                <section class="wide">
                  <h2>当前页正文</h2>
                  <pre id="pageMeta" class="muted"></pre>
                  <pre id="pageBody" class="body">尚未收到稳定页正文。</pre>
                </section>
                <section>
                  <h2>高亮、卡片与未锚定内容</h2>
                  <ul id="embeds"></ul>
                </section>
                <section>
                  <h2>EPUB 视口</h2>
                  <pre id="viewport" class="muted">当前没有 EPUB 视口。</pre>
                </section>
                <section class="wide">
                  <h2>绘图与页图</h2>
                  <pre id="drawing" class="muted">当前页没有视觉引用。</pre>
                  <p id="imageNote" class="warning" hidden></p>
                  <a id="imageLink" target="_blank" rel="noreferrer" hidden>
                    在 Reader 中打开原图（可能需要登录）
                  </a>
                  <img id="pageImage" alt="当前页图（加载失败时请使用上方链接）"
                       referrerpolicy="no-referrer">
                </section>
                <section class="wide">
                  <h2>最近阅读器事件</h2>
                  <pre id="latest" class="muted">暂无事件。</pre>
                </section>
              </main>
              <script>
                "use strict";
                const byId = id => document.getElementById(id);
                const status = byId("status");
                const revision = byId("revision");
                const active = byId("active");
                const selection = byId("selection");
                const pageMeta = byId("pageMeta");
                const pageBody = byId("pageBody");
                const embeds = byId("embeds");
                const viewport = byId("viewport");
                const drawing = byId("drawing");
                const imageNote = byId("imageNote");
                const imageLink = byId("imageLink");
                const pageImage = byId("pageImage");
                const latest = byId("latest");

                const valueText = value =>
                  value === null || value === undefined
                    ? "—"
                    : typeof value === "string"
                      ? value
                      : JSON.stringify(value);

                function startsMarker(marker, name) {
                  return marker === name
                    || (marker.startsWith(name)
                      && [" ", "\t", "\r", "\n", "\v", "\f"]
                        .includes(marker.charAt(name.length)));
                }

                function parseReaderText(input) {
                  const value = String(input ?? "");
                  let plain = "";
                  let marked = "";
                  let markedKind = null;
                  let markedAttributes = "";
                  const highlights = [];
                  const cards = [];
                  const append = value => {
                    plain += value;
                    if (markedKind !== null) marked += value;
                  };
                  for (let index = 0; index < value.length;) {
                    const current = value.charAt(index);
                    if (current === "\\") {
                      if (index + 1 >= value.length) {
                        throw new Error(
                          "BW_READER_CONTEXT_MARK_ESCAPE_INVALID");
                      }
                      const next = value.charAt(index + 1);
                      if (next === "\\" || next === "⟦" || next === "⟧") {
                        append(next);
                      } else {
                        append(current);
                        append(next);
                      }
                      index += 2;
                      continue;
                    }
                    if (current === "⟦") {
                      const close = value.indexOf("⟧", index + 1);
                      if (close < 0) {
                        throw new Error(
                          "BW_READER_CONTEXT_MARK_ESCAPE_INVALID");
                      }
                      const marker = value.slice(index + 1, close);
                      if (startsMarker(marker, "HIGHLIGHT")) {
                        if (markedKind !== null) {
                          throw new Error(
                            "BW_READER_CONTEXT_MARK_ESCAPE_INVALID");
                        }
                        markedKind = "highlight";
                        markedAttributes =
                          marker.slice("HIGHLIGHT".length).trim();
                        marked = "";
                      } else if (marker === "/HIGHLIGHT") {
                        if (markedKind !== "highlight") {
                          throw new Error(
                            "BW_READER_CONTEXT_MARK_ESCAPE_INVALID");
                        }
                        highlights.push({
                          attributes: markedAttributes,
                          text: marked
                        });
                        markedKind = null;
                        markedAttributes = "";
                        marked = "";
                      } else if (startsMarker(marker, "CARD_START")) {
                        if (markedKind !== null) {
                          throw new Error(
                            "BW_READER_CONTEXT_MARK_ESCAPE_INVALID");
                        }
                        markedKind = "card";
                        markedAttributes =
                          marker.slice("CARD_START".length).trim();
                        marked = "";
                      } else if (marker === "CARD_END") {
                        if (markedKind !== "card") {
                          throw new Error(
                            "BW_READER_CONTEXT_MARK_ESCAPE_INVALID");
                        }
                        cards.push({
                          attributes: markedAttributes,
                          text: marked
                        });
                        markedKind = null;
                        markedAttributes = "";
                        marked = "";
                      } else {
                        throw new Error(
                          "BW_READER_CONTEXT_MARK_ESCAPE_INVALID");
                      }
                      index = close + 1;
                      continue;
                    }
                    if (current === "⟧") {
                      throw new Error(
                        "BW_READER_CONTEXT_MARK_ESCAPE_INVALID");
                    }
                    append(current);
                    index += 1;
                  }
                  if (markedKind !== null) {
                    throw new Error(
                      "BW_READER_CONTEXT_MARK_ESCAPE_INVALID");
                  }
                  return { plain, highlights, cards };
                }

                function resetImage() {
                  pageImage.removeAttribute("src");
                  pageImage.style.display = "none";
                  imageLink.hidden = true;
                  imageLink.removeAttribute("href");
                  imageNote.hidden = true;
                  imageNote.textContent = "";
                }

                function clearProjection(message) {
                  status.textContent = "快照不可用";
                  status.className = "pill error";
                  revision.textContent = "revision —";
                  active.textContent = message;
                  selection.textContent = "当前没有可用选区。";
                  pageMeta.textContent = "";
                  pageBody.textContent = "没有可安全显示的正文。";
                  embeds.replaceChildren();
                  viewport.textContent = "当前没有 EPUB 视口。";
                  drawing.textContent = "当前页没有视觉引用。";
                  latest.textContent = "暂无事件。";
                  resetImage();
                }

                function addEmbed(prefix, entry) {
                  const item = document.createElement("li");
                  item.textContent = prefix
                    + (entry.attributes ? ` [${entry.attributes}]` : "")
                    + ` ${entry.text}`;
                  embeds.append(item);
                }

                function render(snapshot) {
                  const contextStatus = valueText(
                    snapshot.contextStatus || "pending");
                  status.textContent = contextStatus;
                  status.className = "pill "
                    + (contextStatus === "ready"
                      ? "ready"
                      : contextStatus === "stale"
                        ? "stale"
                        : "");
                  revision.textContent =
                    `revision ${valueText(snapshot.revision)}`;

                  const reading = snapshot.activeReading;
                  active.textContent = reading
                    ? [
                        `书名：${valueText(reading.title)}`,
                        `文件：${valueText(reading.file)}`,
                        `页/章节：${valueText(reading.page)}`,
                        `类型：${valueText(reading.kind)}`,
                        `新鲜：${valueText(reading.fresh)}`,
                        `年龄（秒）：${valueText(reading.ageSec)}`
                      ].join("\n")
                    : "尚未收到活动书页。";

                  const selected = snapshot.selection;
                  selection.textContent =
                    selected?.state === "active"
                    && typeof selected.text === "string"
                    && selected.text.length > 0
                      ? selected.text
                      : `状态：${valueText(selected?.state)}\n`
                        + "当前没有可用选区。";

                  const page = snapshot.currentPage;
                  let projection = {
                    plain: "",
                    highlights: [],
                    cards: []
                  };
                  if (page) {
                    projection = parseReaderText(page.text);
                    pageMeta.textContent = [
                      `稳定：${valueText(page.stable)}`,
                      `正文可用：${valueText(page.textAvailable)}`,
                      `来源：${valueText(page.textSource)}`,
                      `降级原因：${valueText(page.fallbackReason)}`,
                      `已截断：${valueText(page.truncated)}`
                    ].join("  |  ");
                    pageBody.textContent = projection.plain
                      || "（当前页无文字层）";
                  } else {
                    pageMeta.textContent = "";
                    pageBody.textContent = "尚未收到稳定页正文。";
                  }

                  embeds.replaceChildren();
                  for (const item of projection.highlights) {
                    addEmbed("高亮：", item);
                  }
                  for (const item of projection.cards) {
                    addEmbed("卡片：", item);
                  }
                  const unanchored = page?.embeds?.unanchored;
                  if (Array.isArray(unanchored)) {
                    for (const entry of unanchored) {
                      const item = document.createElement("li");
                      item.textContent = "未锚定："
                        + valueText(entry.text ?? entry.id)
                        + `（${valueText(entry._reason)}）`;
                      embeds.append(item);
                    }
                  }
                  if (embeds.children.length === 0) {
                    const item = document.createElement("li");
                    item.textContent = "当前没有高亮或附属内容。";
                    embeds.append(item);
                  }

                  viewport.textContent = page?.viewport
                    ? JSON.stringify(page.viewport, null, 2)
                    : "当前没有 EPUB 视口。";
                  drawing.textContent = page?.visual
                    ? JSON.stringify({
                        has_ink: page.visual.has_ink,
                        drawing: page.visual.drawing ?? null
                      }, null, 2)
                    : "当前页没有视觉引用。";
                  latest.textContent = snapshot.latestEvent
                    ? JSON.stringify(snapshot.latestEvent, null, 2)
                    : "暂无事件。";

                  resetImage();
                  const image = page?.visual?.page_image;
                  if (typeof image === "string" && image.length > 0) {
                    let url;
                    try {
                      url = new URL(
                        image,
                        "https://bwicarus.taile44d0c.ts.net");
                    } catch {
                      throw new Error("页图 URL 无效");
                    }
                    if (url.protocol !== "https:"
                        || url.origin
                          !== "https://bwicarus.taile44d0c.ts.net") {
                      throw new Error("页图 URL 来源无效");
                    }
                    imageLink.href = url.href;
                    imageLink.hidden = false;
                    imageNote.hidden = false;
                    imageNote.textContent =
                      "页图接口受 Reader 登录保护。"
                      + "本地查看器的跨站内嵌请求可能没有登录 Cookie；"
                      + "失败时请使用原图链接，并在 Reader 中登录。";
                    pageImage.onload = () => {
                      pageImage.style.display = "block";
                    };
                    pageImage.onerror = () => {
                      pageImage.style.display = "none";
                    };
                    pageImage.src = url.href;
                  }
                }

                async function refresh() {
                  try {
                    const response = await fetch(
                      "/reader-context-snapshot.json",
                      { cache: "no-store", credentials: "same-origin" });
                    if (!response.ok) {
                      throw new Error(`HTTP ${response.status}`);
                    }
                    render(await response.json());
                  } catch (error) {
                    clearProjection(
                      `快照暂时不可读：${error.message}\n`
                      + "等待下一次 Reader 更新……");
                  }
                }

                clearProjection("等待首个 Reader 快照……");
                void refresh();
                setInterval(() => void refresh(), 750);
              </script>
            </body>
            </html>
            """);

    private readonly string _snapshotPath;
    private readonly int _listenPort;
    private readonly string _viewerUrl;
    private readonly string _profilePath;
    private readonly object _gate = new();
    private Process? _viewerProcess;
    private string? _viewerOwnerId;
    private bool _disposed;

    internal DirectSnapshotViewer(string snapshotPath) : this(
        snapshotPath,
        DirectBridgeContract.DefaultListenPort)
    {
    }

    internal DirectSnapshotViewer(
        string snapshotPath,
        int listenPort)
    {
        _snapshotPath = System.IO.Path.GetFullPath(snapshotPath);
        _listenPort = listenPort;
        _viewerUrl =
            $"http://{DirectBridgeContract.ListenHost}:{listenPort}"
            + ViewerPath;
        _profilePath = System.IO.Path.Combine(
            System.IO.Path.GetDirectoryName(_snapshotPath)
                ?? throw new ArgumentException(
                    "snapshot state path has no directory",
                    nameof(snapshotPath)),
            "reader-context-viewer-profile");
    }

    internal Task HandleViewerAsync(HttpContext context)
    {
        if (!PrepareLocalResponse(context, "text/html; charset=utf-8"))
        {
            return Task.CompletedTask;
        }
        return WriteBytesAsync(
            context,
            ViewerDocument,
            StatusCodes.Status200OK);
    }

    internal async Task HandleSnapshotAsync(HttpContext context)
    {
        if (!PrepareLocalResponse(
            context,
            "application/json; charset=utf-8"))
        {
            return;
        }
        JsonObject? snapshot = await ReadFreshSnapshotAsync(
            context.RequestAborted).ConfigureAwait(false);
        if (snapshot is null)
        {
            await WriteBytesAsync(
                context,
                Encoding.UTF8.GetBytes(
                    """{"contextStatus":"pending","error":"snapshot-unavailable"}"""),
                StatusCodes.Status503ServiceUnavailable)
                .ConfigureAwait(false);
            return;
        }
        await WriteBytesAsync(
            context,
            JsonSerializer.SerializeToUtf8Bytes(
                snapshot,
                DirectBridgeContract.JsonOptions),
            StatusCodes.Status200OK).ConfigureAwait(false);
    }

    internal async Task HandleMarkdownAsync(HttpContext context)
    {
        if (!PrepareLocalResponse(
            context,
            "text/markdown; charset=utf-8"))
        {
            return;
        }
        JsonObject? snapshot = await ReadFreshSnapshotAsync(
            context.RequestAborted).ConfigureAwait(false);
        if (snapshot is null)
        {
            await WriteBytesAsync(
                context,
                Encoding.UTF8.GetBytes(
                    "# Reader 实时快照\n\n"
                    + "_快照暂时不可读，等待下一次 Reader 更新。_\n"),
                StatusCodes.Status503ServiceUnavailable)
                .ConfigureAwait(false);
            return;
        }
        try
        {
            await WriteBytesAsync(
                context,
                Encoding.UTF8.GetBytes(
                    DirectSnapshotMarkdown.Render(snapshot)),
                StatusCodes.Status200OK).ConfigureAwait(false);
        }
        catch (InvalidOperationException)
        {
            await WriteBytesAsync(
                context,
                Encoding.UTF8.GetBytes(
                    "# Reader 实时快照\n\n"
                    + "_正文标记无效，投影已拒绝；JSON 快照仍可供 MCP "
                    + "按合同读取。_\n"),
                StatusCodes.Status503ServiceUnavailable)
                .ConfigureAwait(false);
        }
    }

    internal async Task<JsonObject?> ReadFreshSnapshotAsync(
        CancellationToken cancellationToken)
    {
        try
        {
            await using FileStream stream = new(
                _snapshotPath,
                FileMode.Open,
                FileAccess.Read,
                FileShare.ReadWrite | FileShare.Delete,
                bufferSize: 4096,
                options:
                    FileOptions.Asynchronous
                    | FileOptions.SequentialScan);
            if (
                stream.Length is <= 0
                or > MaximumPresentationBytes
            )
            {
                return null;
            }
            JsonObject? snapshot = await JsonNode.ParseAsync(
                stream,
                new JsonNodeOptions
                {
                    PropertyNameCaseInsensitive = false,
                },
                new JsonDocumentOptions
                {
                    AllowTrailingCommas = false,
                    CommentHandling =
                        JsonCommentHandling.Disallow,
                    MaxDepth = 32,
                },
                cancellationToken).ConfigureAwait(false)
                as JsonObject;
            if (
                snapshot?["schema"]?.GetValue<string>()
                    != FileDirectSnapshotContextAdapter
                        .SnapshotContract
                || !TryNonNegativeRevision(
                    snapshot["revision"],
                    out _)
            )
            {
                return null;
            }
            ReaderContextMcpServer.ApplyFreshness(
                snapshot,
                DateTimeOffset.UtcNow);
            return snapshot;
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (Exception exception) when (
            exception is IOException
            or UnauthorizedAccessException
            or JsonException
            or InvalidOperationException)
        {
            return null;
        }
    }

    internal void OpenIfSnapshotMode(
        string contextDeliveryMode,
        string ownerId)
    {
        if (
            contextDeliveryMode != DirectContextDeliveryMode.SnapshotMcp
            || string.IsNullOrWhiteSpace(ownerId)
            || _disposed)
        {
            return;
        }
        lock (_gate)
        {
            if (_disposed)
            {
                return;
            }
            if (
                _viewerProcess is { HasExited: false }
                && string.Equals(
                    _viewerOwnerId,
                    ownerId,
                    StringComparison.Ordinal)
            )
            {
                return;
            }
            StopViewerProcessBestEffort();
            CloseStaleViewerWindowsBestEffort();

            try
            {
                string executable = FindEdgeExecutable()
                    ?? throw new InvalidOperationException(
                        "BW_READER_CONTEXT_VIEWER_EDGE_NOT_FOUND");
                ProcessStartInfo start = CreateEdgeStartInfo(
                    executable,
                    _viewerUrl,
                    _profilePath);
                _viewerProcess = Process.Start(start);
                _viewerOwnerId = _viewerProcess is null
                    ? null
                    : ownerId;
            }
            catch (Exception exception) when (
                exception is IOException
                or UnauthorizedAccessException
                or InvalidOperationException
                or System.ComponentModel.Win32Exception)
            {
                // Presentation is best-effort. Voice and MCP continue even
                // if Windows cannot create the isolated app-mode viewer.
                StopViewerProcessBestEffort();
            }
        }
    }

    internal void CloseForConnection(string ownerId)
    {
        if (string.IsNullOrWhiteSpace(ownerId))
        {
            return;
        }
        lock (_gate)
        {
            if (!string.Equals(
                _viewerOwnerId,
                ownerId,
                StringComparison.Ordinal))
            {
                return;
            }
            StopViewerProcessBestEffort();
            CloseStaleViewerWindowsBestEffort();
        }
    }

    internal static ProcessStartInfo CreateEdgeStartInfo(
        string edgeExecutable,
        string viewerUrl,
        string profilePath)
    {
        if (
            string.IsNullOrWhiteSpace(edgeExecutable)
            || string.IsNullOrWhiteSpace(viewerUrl)
            || string.IsNullOrWhiteSpace(profilePath)
        )
        {
            throw new ArgumentException(
                "viewer launch arguments must not be empty");
        }
        ProcessStartInfo start = new()
        {
            FileName = edgeExecutable,
            WorkingDirectory =
                System.IO.Path.GetDirectoryName(edgeExecutable)
                ?? AppContext.BaseDirectory,
            UseShellExecute = false,
            WindowStyle = ProcessWindowStyle.Normal,
        };
        start.ArgumentList.Add($"--app={viewerUrl}");
        start.ArgumentList.Add($"--user-data-dir={profilePath}");
        start.ArgumentList.Add("--no-first-run");
        start.ArgumentList.Add("--no-default-browser-check");
        start.ArgumentList.Add("--disable-extensions");
        start.ArgumentList.Add("--disable-sync");
        return start;
    }

    public void Dispose()
    {
        lock (_gate)
        {
            _disposed = true;
            StopViewerProcessBestEffort();
            CloseStaleViewerWindowsBestEffort();
        }
    }

    private void StopViewerProcessBestEffort()
    {
        Process? process = _viewerProcess;
        _viewerProcess = null;
        _viewerOwnerId = null;
        if (process is null)
        {
            return;
        }
        try
        {
            if (!process.HasExited)
            {
                process.Kill(entireProcessTree: true);
            }
        }
        catch (Exception exception) when (
            exception is InvalidOperationException
            or NotSupportedException
            or System.ComponentModel.Win32Exception)
        {
        }
        finally
        {
            process.Dispose();
        }
    }

    private static void CloseStaleViewerWindowsBestEffort()
    {
        foreach (Process process in Process.GetProcessesByName("msedge"))
        {
            try
            {
                if (
                    string.Equals(
                        process.MainWindowTitle,
                        ViewerWindowTitle,
                        StringComparison.Ordinal)
                    && !process.HasExited
                )
                {
                    process.Kill(entireProcessTree: true);
                }
            }
            catch (Exception exception) when (
                exception is InvalidOperationException
                or NotSupportedException
                or System.ComponentModel.Win32Exception)
            {
            }
            finally
            {
                process.Dispose();
            }
        }
    }

    private bool PrepareLocalResponse(
        HttpContext context,
        string contentType)
    {
        if (!HttpMethods.IsGet(context.Request.Method))
        {
            context.Response.StatusCode =
                StatusCodes.Status405MethodNotAllowed;
            context.Response.Headers.Allow = HttpMethods.Get;
            return false;
        }
        System.Net.IPAddress? remote =
            context.Connection.RemoteIpAddress;
        if (
            remote is null
            || !System.Net.IPAddress.IsLoopback(
                remote.IsIPv4MappedToIPv6
                    ? remote.MapToIPv4()
                    : remote)
            || !string.Equals(
                context.Request.Host.Host,
                DirectBridgeContract.ListenHost,
                StringComparison.Ordinal)
            || context.Request.Host.Port != _listenPort
            || HasForwardingHeaders(context)
        )
        {
            context.Response.StatusCode =
                StatusCodes.Status403Forbidden;
            return false;
        }

        context.Response.ContentType = contentType;
        context.Response.Headers.CacheControl =
            "no-store, max-age=0";
        context.Response.Headers.Pragma = "no-cache";
        context.Response.Headers["Content-Security-Policy"] =
            "default-src 'none'; connect-src 'self'; "
            + "img-src 'self' "
            + DirectSnapshotMarkdown.ReaderOrigin
            + "; style-src 'unsafe-inline'; "
            + "script-src 'unsafe-inline'; base-uri 'none'; "
            + "form-action 'none'; frame-ancestors 'none'";
        context.Response.Headers["Cross-Origin-Resource-Policy"] =
            "same-origin";
        context.Response.Headers["Referrer-Policy"] =
            "no-referrer";
        context.Response.Headers["X-Content-Type-Options"] =
            "nosniff";
        context.Response.Headers["X-Frame-Options"] = "DENY";
        return true;
    }

    private static bool HasForwardingHeaders(HttpContext context) =>
        context.Request.Headers.ContainsKey("Forwarded")
        || context.Request.Headers.ContainsKey("X-Forwarded-For")
        || context.Request.Headers.ContainsKey("X-Forwarded-Host")
        || context.Request.Headers.ContainsKey("X-Forwarded-Proto")
        || context.Request.Headers.ContainsKey("X-Real-IP");

    private static async Task WriteBytesAsync(
        HttpContext context,
        byte[] value,
        int statusCode)
    {
        context.Response.StatusCode = statusCode;
        context.Response.ContentLength = value.Length;
        await context.Response.Body.WriteAsync(
            value,
            context.RequestAborted).ConfigureAwait(false);
    }

    private static bool TryNonNegativeRevision(
        JsonNode? node,
        out long revision)
    {
        if (
            node is JsonValue value
            && value.TryGetValue(out revision)
            && revision >= 0
        )
        {
            return true;
        }
        revision = 0;
        return false;
    }

    private static string? FindEdgeExecutable()
    {
        string[] candidates =
        [
            System.IO.Path.Combine(
                Environment.GetFolderPath(
                    Environment.SpecialFolder.ProgramFilesX86),
                "Microsoft",
                "Edge",
                "Application",
                "msedge.exe"),
            System.IO.Path.Combine(
                Environment.GetFolderPath(
                    Environment.SpecialFolder.ProgramFiles),
                "Microsoft",
                "Edge",
                "Application",
                "msedge.exe"),
            System.IO.Path.Combine(
                Environment.GetFolderPath(
                    Environment.SpecialFolder.LocalApplicationData),
                "Microsoft",
                "Edge",
                "Application",
                "msedge.exe"),
        ];
        return candidates.FirstOrDefault(File.Exists);
    }
}
