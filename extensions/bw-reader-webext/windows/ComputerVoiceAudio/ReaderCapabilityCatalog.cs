using System.Text;
using System.Text.Json.Nodes;
using System.Reflection;

namespace BwReader.ComputerVoiceAudio;

internal sealed class ReaderCapabilityCatalog
{
    internal const string IndexUri = "reader://capabilities/index";
    private const int MaximumGuideBytes = 32 * 1024;

    private sealed record Entry(
        string Slug,
        string Title,
        string Description);

    private static readonly Entry[] Entries =
    [
        new("index", "Reader 能力索引", "仅在无法判断精确 topic 时用于发现"),
        new("get", "GET Reader 信息", "读取页文、窗口、选区、全文与合成图"),
        new("conversation", "对话同步", "Windows 电脑语音聊天同步语义"),
        new("cards", "卡片输出", "发送现有 Realtime 卡片"),
        new("navigation", "导航输出", "滚动、定位、跳页与跳章节"),
        new("highlight", "高亮输出", "保存当前稳定选区"),
        new("tool-status", "工具状态输出", "发送现有 Realtime 工具状态"),
        new("command-format", "统一命令格式", "命令外壳、回执、去重和失败规则"),
        new("task-routing", "Codex 原生任务路由", "按延迟和副作用选择直接工具或原生子代理"),
        new("research-task", "多步研究任务", "替代旧 CLI worker prompt 的原生研究合同"),
        new("interactive-paper", "交互练习纸", "原生编排出题、纸面元素和检查按钮"),
        new("check-report", "检查报告核实", "直接回答报告或按需查书核实"),
        new("saved-task", "已保存任务", "重生成型任务与机械回放的不同语义"),
        new("capability-matrix", "工具能力矩阵", "本机 MCP、服务 MCP、Skill 与子代理的职责"),
    ];

    internal static JsonArray TopicEnum() => new(
        Entries.Select(entry => (JsonNode)entry.Slug).ToArray());

    private const string ResourcePrefix =
        "BwReader.ComputerVoiceAudio.ReaderCapabilities.";
    private readonly string? _directory;

    internal ReaderCapabilityCatalog(string? baseDirectory = null)
    {
        if (baseDirectory is not null)
        {
            string root = Path.GetFullPath(baseDirectory);
            _directory = Path.GetFullPath(
                Path.Combine(root, "ReaderCapabilities"));
        }
    }

    internal JsonObject List() => new()
    {
        ["resources"] = new JsonArray(
            Entries.Select(entry => (JsonNode)new JsonObject
            {
                ["uri"] = Uri(entry),
                ["name"] = entry.Slug,
                ["title"] = entry.Title,
                ["description"] = entry.Description,
                ["mimeType"] = "text/markdown; charset=utf-8",
            }).ToArray()),
    };

    internal async Task<JsonObject> ReadAsync(
        string uri,
        CancellationToken cancellationToken)
    {
        Entry entry = Entries.SingleOrDefault(
            candidate => string.Equals(
                Uri(candidate),
                uri,
                StringComparison.Ordinal))
            ?? throw new KeyNotFoundException("Unknown Reader capability URI");
        string text = await ReadEntryTextAsync(entry, cancellationToken)
            .ConfigureAwait(false);
        return new JsonObject
        {
            ["contents"] = new JsonArray
            {
                new JsonObject
                {
                    ["uri"] = uri,
                    ["mimeType"] = "text/markdown; charset=utf-8",
                    ["text"] = text,
                },
            },
        };
    }

    internal async Task<(string Uri, string Text)> ReadTopicTextAsync(
        string topic,
        CancellationToken cancellationToken)
    {
        Entry entry = Entries.SingleOrDefault(candidate => string.Equals(
            candidate.Slug,
            topic,
            StringComparison.Ordinal))
            ?? throw new KeyNotFoundException(
                "Unknown Reader capability topic");
        return (
            Uri(entry),
            await ReadEntryTextAsync(entry, cancellationToken)
                .ConfigureAwait(false));
    }

    private async Task<string> ReadEntryTextAsync(
        Entry entry,
        CancellationToken cancellationToken)
    {
        string text = _directory is null
            ? await ReadEmbeddedAsync(entry, cancellationToken)
                .ConfigureAwait(false)
            : await ReadFileAsync(entry, cancellationToken)
                .ConfigureAwait(false);
        if (Encoding.UTF8.GetByteCount(text) > MaximumGuideBytes)
        {
            throw new InvalidDataException(
                "Reader capability guide exceeds size limit");
        }
        return text;
    }

    private static string Uri(Entry entry) =>
        "reader://capabilities/" + entry.Slug;

    private async Task<string> ReadFileAsync(
        Entry entry,
        CancellationToken cancellationToken)
    {
        string path = Path.GetFullPath(Path.Combine(
            _directory!,
            entry.Slug + ".md"));
        if (!string.Equals(
            Path.GetDirectoryName(path),
            _directory,
            StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidDataException(
                "Reader capability path escaped root");
        }
        FileInfo info = new(path);
        if (!info.Exists || info.Length is < 1 or > MaximumGuideBytes)
        {
            throw new FileNotFoundException(
                "Reader capability guide is unavailable",
                path);
        }
        return await File.ReadAllTextAsync(
            path,
            new UTF8Encoding(
                encoderShouldEmitUTF8Identifier: false,
                throwOnInvalidBytes: true),
            cancellationToken).ConfigureAwait(false);
    }

    private static async Task<string> ReadEmbeddedAsync(
        Entry entry,
        CancellationToken cancellationToken)
    {
        string name = ResourcePrefix + entry.Slug + ".md";
        await using Stream stream = Assembly.GetExecutingAssembly()
            .GetManifestResourceStream(name)
            ?? throw new FileNotFoundException(
                "Reader capability guide is unavailable",
                name);
        if (stream.CanSeek && stream.Length is < 1 or > MaximumGuideBytes)
        {
            throw new InvalidDataException(
                "Reader capability guide has an invalid size");
        }
        using StreamReader reader = new(
            stream,
            new UTF8Encoding(
                encoderShouldEmitUTF8Identifier: false,
                throwOnInvalidBytes: true),
            detectEncodingFromByteOrderMarks: false,
            bufferSize: 4096,
            leaveOpen: true);
        return await reader.ReadToEndAsync(cancellationToken)
            .ConfigureAwait(false);
    }
}
