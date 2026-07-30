using System.Diagnostics;
using System.Text;
using System.Text.Json;

namespace BwReader.ComputerVoiceAudio;

internal enum CodexVoiceHistoryBindingStatus
{
    Bound,
    Stale,
    Unbound,
}

internal enum CodexVoiceHistoryDataStatus
{
    Available,
    Stale,
    Unavailable,
}

internal sealed record CodexVoiceHistoryMessage(
    string Role,
    string Text);

internal sealed record CodexVoiceHistorySnapshot(
    CodexVoiceHistoryBindingStatus BindingStatus,
    string? ThreadId,
    long? BindingVersion,
    CodexVoiceHistoryDataStatus VoiceRecentStatus,
    IReadOnlyList<CodexVoiceHistoryMessage> VoiceRecent,
    CodexVoiceHistoryDataStatus CodexHistoryStatus,
    IReadOnlyList<CodexVoiceHistoryMessage> CodexHistory,
    bool Gap);

internal interface ICodexVoiceHistoryFileSource
{
    ValueTask<string> ReadGlobalStateAsync(
        CancellationToken cancellationToken);

    ValueTask<string> ReadContinuityAsync(
        CancellationToken cancellationToken);
}

internal sealed class FileCodexVoiceHistorySource
    : ICodexVoiceHistoryFileSource
{
    private readonly string _globalStatePath;
    private readonly string _continuityPath;

    internal FileCodexVoiceHistorySource(
        string globalStatePath,
        string continuityPath)
    {
        _globalStatePath = RequireAbsolutePath(
            globalStatePath,
            nameof(globalStatePath));
        _continuityPath = RequireAbsolutePath(
            continuityPath,
            nameof(continuityPath));
    }

    public ValueTask<string> ReadGlobalStateAsync(
        CancellationToken cancellationToken) =>
        ReadSharedTextAsync(_globalStatePath, cancellationToken);

    public ValueTask<string> ReadContinuityAsync(
        CancellationToken cancellationToken) =>
        ReadSharedTextAsync(_continuityPath, cancellationToken);

    private static async ValueTask<string> ReadSharedTextAsync(
        string path,
        CancellationToken cancellationToken)
    {
        await using FileStream stream = new(
            path,
            FileMode.Open,
            FileAccess.Read,
            FileShare.ReadWrite | FileShare.Delete,
            bufferSize: 16 * 1024,
            FileOptions.Asynchronous | FileOptions.SequentialScan);
        using StreamReader reader = new(
            stream,
            new UTF8Encoding(
                encoderShouldEmitUTF8Identifier: false,
                throwOnInvalidBytes: true),
            detectEncodingFromByteOrderMarks: true);
        return await reader.ReadToEndAsync(cancellationToken)
            .ConfigureAwait(false);
    }

    private static string RequireAbsolutePath(
        string path,
        string parameterName)
    {
        if (
            string.IsNullOrWhiteSpace(path)
            || !Path.IsPathFullyQualified(path)
        )
        {
            throw new ArgumentException(
                "BW_CODEX_VOICE_HISTORY_PATH_MUST_BE_ABSOLUTE",
                parameterName);
        }
        return Path.GetFullPath(path);
    }
}

internal interface ICodexThreadHistoryClient : IAsyncDisposable
{
    Task<string> ReadThreadAsync(
        string threadId,
        CancellationToken cancellationToken);
}

internal interface ICodexAppServerTransportFactory
{
    ValueTask<ICodexAppServerTransport> StartAsync(
        CancellationToken cancellationToken);
}

internal interface ICodexAppServerTransport : IAsyncDisposable
{
    TextReader Reader { get; }

    TextWriter Writer { get; }

    bool IsAlive { get; }
}

internal sealed class ProcessCodexAppServerTransportFactory
    : ICodexAppServerTransportFactory
{
    private readonly string _executablePath;
    private readonly IReadOnlyList<string> _arguments;

    internal ProcessCodexAppServerTransportFactory(
        string executablePath,
        IReadOnlyList<string>? arguments = null)
    {
        if (
            string.IsNullOrWhiteSpace(executablePath)
            || !Path.IsPathFullyQualified(executablePath)
        )
        {
            throw new ArgumentException(
                "BW_CODEX_APP_SERVER_EXECUTABLE_MUST_BE_ABSOLUTE",
                nameof(executablePath));
        }
        _executablePath = Path.GetFullPath(executablePath);
        _arguments = arguments ?? ["app-server", "--stdio"];
    }

    public ValueTask<ICodexAppServerTransport> StartAsync(
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        if (!File.Exists(_executablePath))
        {
            throw new FileNotFoundException(
                "BW_CODEX_APP_SERVER_EXECUTABLE_NOT_FOUND",
                _executablePath);
        }
        ProcessStartInfo start = new()
        {
            FileName = _executablePath,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardInputEncoding = new UTF8Encoding(false),
            StandardOutputEncoding = new UTF8Encoding(false),
            StandardErrorEncoding = new UTF8Encoding(false),
        };
        foreach (string argument in _arguments)
        {
            start.ArgumentList.Add(argument);
        }
        Process process = new()
        {
            StartInfo = start,
            EnableRaisingEvents = true,
        };
        if (!process.Start())
        {
            process.Dispose();
            throw new InvalidOperationException(
                "BW_CODEX_APP_SERVER_START_FAILED");
        }
        return ValueTask.FromResult<ICodexAppServerTransport>(
            new ProcessCodexAppServerTransport(process));
    }
}

internal sealed class ProcessCodexAppServerTransport
    : ICodexAppServerTransport
{
    private readonly Process _process;
    private readonly Task _stderrDrain;
    private bool _disposed;

    internal ProcessCodexAppServerTransport(Process process)
    {
        _process = process;
        _stderrDrain = DrainAsync(process.StandardError);
    }

    public TextReader Reader => _process.StandardOutput;

    public TextWriter Writer => _process.StandardInput;

    public bool IsAlive => !_disposed && !_process.HasExited;

    public async ValueTask DisposeAsync()
    {
        if (_disposed)
        {
            return;
        }
        _disposed = true;
        try
        {
            _process.StandardInput.Close();
            if (!_process.HasExited)
            {
                using CancellationTokenSource wait = new(
                    TimeSpan.FromSeconds(2));
                try
                {
                    await _process.WaitForExitAsync(wait.Token)
                        .ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                    if (!_process.HasExited)
                    {
                        _process.Kill(entireProcessTree: true);
                    }
                }
            }
            await _stderrDrain.ConfigureAwait(false);
        }
        finally
        {
            _process.Dispose();
        }
    }

    private static async Task DrainAsync(TextReader error)
    {
        while (
            await error.ReadLineAsync().ConfigureAwait(false)
                is not null
        )
        {
            // Deliberately discard stderr: it may contain private thread data.
        }
    }
}

internal sealed class CodexAppServerReadOnlyHistoryClient
    : ICodexThreadHistoryClient
{
    private const int MaximumResponseLineLength = 32 * 1024 * 1024;
    private readonly ICodexAppServerTransportFactory _factory;
    private readonly SemaphoreSlim _gate = new(1, 1);
    private ICodexAppServerTransport? _transport;
    private long _nextRequestId;
    private bool _initialized;
    private bool _disposed;

    internal CodexAppServerReadOnlyHistoryClient(
        ICodexAppServerTransportFactory factory)
    {
        _factory = factory;
    }

    public async Task<string> ReadThreadAsync(
        string threadId,
        CancellationToken cancellationToken)
    {
        string normalizedThreadId = RequireThreadId(threadId);
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            ThrowIfDisposed();
            try
            {
                await EnsureInitializedAsync(cancellationToken)
                    .ConfigureAwait(false);
                long requestId = NextRequestId();
                await WriteAsync(
                    new
                    {
                        method = "thread/read",
                        id = requestId,
                        @params = new
                        {
                            threadId = normalizedThreadId,
                            includeTurns = true,
                        },
                    },
                    cancellationToken).ConfigureAwait(false);
                return await ReadResultAsync(
                    requestId,
                    cancellationToken).ConfigureAwait(false);
            }
            catch
            {
                await ResetTransportAsync().ConfigureAwait(false);
                throw;
            }
        }
        finally
        {
            _gate.Release();
        }
    }

    public async ValueTask DisposeAsync()
    {
        await _gate.WaitAsync().ConfigureAwait(false);
        try
        {
            if (_disposed)
            {
                return;
            }
            _disposed = true;
            await ResetTransportAsync().ConfigureAwait(false);
        }
        finally
        {
            _gate.Release();
            _gate.Dispose();
        }
    }

    private async Task EnsureInitializedAsync(
        CancellationToken cancellationToken)
    {
        if (
            _initialized
            && _transport is { IsAlive: true }
        )
        {
            return;
        }
        await ResetTransportAsync().ConfigureAwait(false);
        _transport = await _factory.StartAsync(cancellationToken)
            .ConfigureAwait(false);
        long requestId = NextRequestId();
        await WriteAsync(
            new
            {
                method = "initialize",
                id = requestId,
                @params = new
                {
                    clientInfo = new
                    {
                        name = "bw_reader_voice_history",
                        title = "BW Reader Voice History",
                        version = "1.0.0",
                    },
                },
            },
            cancellationToken).ConfigureAwait(false);
        _ = await ReadResultAsync(
            requestId,
            cancellationToken).ConfigureAwait(false);
        await WriteAsync(
            new
            {
                method = "initialized",
            },
            cancellationToken).ConfigureAwait(false);
        _initialized = true;
    }

    private async Task WriteAsync(
        object request,
        CancellationToken cancellationToken)
    {
        ICodexAppServerTransport transport = RequireTransport();
        string line = JsonSerializer.Serialize(request);
        await transport.Writer.WriteLineAsync(
            line.AsMemory(),
            cancellationToken).ConfigureAwait(false);
        await transport.Writer.FlushAsync(cancellationToken)
            .ConfigureAwait(false);
    }

    private async Task<string> ReadResultAsync(
        long expectedRequestId,
        CancellationToken cancellationToken)
    {
        ICodexAppServerTransport transport = RequireTransport();
        while (true)
        {
            string? line = await transport.Reader.ReadLineAsync(
                cancellationToken).ConfigureAwait(false);
            if (line is null)
            {
                throw new EndOfStreamException(
                    "BW_CODEX_APP_SERVER_CLOSED");
            }
            if (
                line.Length == 0
                || line.Length > MaximumResponseLineLength
            )
            {
                throw new InvalidDataException(
                    "BW_CODEX_APP_SERVER_RESPONSE_INVALID_LENGTH");
            }
            using JsonDocument document = ParseJson(line);
            JsonElement root = document.RootElement;
            if (
                root.ValueKind != JsonValueKind.Object
                || !root.TryGetProperty(
                    "id",
                    out JsonElement idElement)
            )
            {
                // Notifications are irrelevant to this read-only client.
                continue;
            }
            if (
                !idElement.TryGetInt64(out long responseId)
                || responseId != expectedRequestId
            )
            {
                throw new InvalidDataException(
                    "BW_CODEX_APP_SERVER_RESPONSE_ID_MISMATCH");
            }
            if (root.TryGetProperty("error", out _))
            {
                throw new InvalidOperationException(
                    "BW_CODEX_APP_SERVER_REQUEST_FAILED");
            }
            if (
                !root.TryGetProperty(
                    "result",
                    out JsonElement result)
            )
            {
                throw new InvalidDataException(
                    "BW_CODEX_APP_SERVER_RESULT_MISSING");
            }
            return result.GetRawText();
        }
    }

    private async ValueTask ResetTransportAsync()
    {
        _initialized = false;
        if (_transport is null)
        {
            return;
        }
        ICodexAppServerTransport transport = _transport;
        _transport = null;
        await transport.DisposeAsync().ConfigureAwait(false);
    }

    private ICodexAppServerTransport RequireTransport() =>
        _transport is { IsAlive: true } transport
            ? transport
            : throw new EndOfStreamException(
                "BW_CODEX_APP_SERVER_NOT_RUNNING");

    private long NextRequestId() =>
        Interlocked.Increment(ref _nextRequestId);

    private void ThrowIfDisposed()
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
    }

    private static string RequireThreadId(string threadId)
    {
        if (
            !Guid.TryParse(threadId, out Guid parsed)
            || parsed == Guid.Empty
        )
        {
            throw new ArgumentException(
                "BW_CODEX_VOICE_HISTORY_THREAD_ID_INVALID",
                nameof(threadId));
        }
        return parsed.ToString();
    }

    private static JsonDocument ParseJson(string value) =>
        JsonDocument.Parse(
            value,
            new JsonDocumentOptions
            {
                AllowTrailingCommas = false,
                CommentHandling = JsonCommentHandling.Disallow,
                MaxDepth = 128,
            });
}

internal sealed class CodexVoiceHistoryReader : IAsyncDisposable
{
    private const int MaximumContinuityItems = 20;
    private const int MaximumContinuityTextLength = 4_000;
    private const int MaximumCodexHistoryItems = 200;
    private const int MaximumCodexHistoryTextLength = 32_000;
    private const string RecentThreadKey =
        "realtime-voice-most-recent-thread";
    private readonly ICodexVoiceHistoryFileSource _source;
    private readonly ICodexThreadHistoryClient? _codexClient;
    private CodexVoiceThreadBinding? _lastBinding;
    private IReadOnlyList<CodexVoiceHistoryMessage> _lastVoiceRecent =
        Array.Empty<CodexVoiceHistoryMessage>();
    private IReadOnlyList<CodexVoiceHistoryMessage> _lastCodexHistory =
        Array.Empty<CodexVoiceHistoryMessage>();

    internal CodexVoiceHistoryReader(
        ICodexVoiceHistoryFileSource source,
        ICodexThreadHistoryClient? codexClient = null)
    {
        _source = source;
        _codexClient = codexClient;
    }

    internal async Task<CodexVoiceHistorySnapshot> PollAsync(
        CancellationToken cancellationToken)
    {
        BindingReadResult bindingRead;
        try
        {
            string globalState = await _source.ReadGlobalStateAsync(
                cancellationToken).ConfigureAwait(false);
            bindingRead = ParseBinding(globalState);
        }
        catch (OperationCanceledException)
            when (cancellationToken.IsCancellationRequested)
        {
            throw;
        }
        catch
        {
            bindingRead = BindingReadResult.Invalid();
        }

        bool gap = false;
        CodexVoiceHistoryBindingStatus bindingStatus;
        CodexVoiceThreadBinding? binding;
        if (bindingRead.Status == BindingReadStatus.Bound)
        {
            binding = bindingRead.Binding;
            if (
                _lastBinding is null
                || !string.Equals(
                    _lastBinding.ThreadId,
                    binding!.ThreadId,
                    StringComparison.Ordinal)
            )
            {
                _lastVoiceRecent =
                    Array.Empty<CodexVoiceHistoryMessage>();
                _lastCodexHistory =
                    Array.Empty<CodexVoiceHistoryMessage>();
            }
            _lastBinding = binding;
            bindingStatus = CodexVoiceHistoryBindingStatus.Bound;
        }
        else if (bindingRead.Status == BindingReadStatus.Unbound)
        {
            _lastBinding = null;
            _lastVoiceRecent =
                Array.Empty<CodexVoiceHistoryMessage>();
            _lastCodexHistory =
                Array.Empty<CodexVoiceHistoryMessage>();
            return new CodexVoiceHistorySnapshot(
                CodexVoiceHistoryBindingStatus.Unbound,
                ThreadId: null,
                BindingVersion: null,
                CodexVoiceHistoryDataStatus.Unavailable,
                _lastVoiceRecent,
                CodexVoiceHistoryDataStatus.Unavailable,
                _lastCodexHistory,
                Gap: false);
        }
        else if (_lastBinding is not null)
        {
            binding = _lastBinding;
            bindingStatus = CodexVoiceHistoryBindingStatus.Stale;
            gap = true;
        }
        else
        {
            return new CodexVoiceHistorySnapshot(
                CodexVoiceHistoryBindingStatus.Unbound,
                ThreadId: null,
                BindingVersion: null,
                CodexVoiceHistoryDataStatus.Unavailable,
                _lastVoiceRecent,
                CodexVoiceHistoryDataStatus.Unavailable,
                _lastCodexHistory,
                Gap: true);
        }

        CodexVoiceHistoryDataStatus voiceStatus;
        try
        {
            string continuity = await _source.ReadContinuityAsync(
                cancellationToken).ConfigureAwait(false);
            _lastVoiceRecent = ParseContinuity(
                continuity,
                binding!.ThreadId);
            voiceStatus = CodexVoiceHistoryDataStatus.Available;
        }
        catch (OperationCanceledException)
            when (cancellationToken.IsCancellationRequested)
        {
            throw;
        }
        catch
        {
            voiceStatus = _lastVoiceRecent.Count > 0
                ? CodexVoiceHistoryDataStatus.Stale
                : CodexVoiceHistoryDataStatus.Unavailable;
            gap = true;
        }

        CodexVoiceHistoryDataStatus codexStatus;
        if (_codexClient is null)
        {
            codexStatus = CodexVoiceHistoryDataStatus.Unavailable;
        }
        else if (
            bindingStatus
            != CodexVoiceHistoryBindingStatus.Bound
        )
        {
            codexStatus = _lastCodexHistory.Count > 0
                ? CodexVoiceHistoryDataStatus.Stale
                : CodexVoiceHistoryDataStatus.Unavailable;
        }
        else
        {
            try
            {
                string result = await _codexClient.ReadThreadAsync(
                    binding!.ThreadId,
                    cancellationToken).ConfigureAwait(false);
                _lastCodexHistory = ParseCodexHistory(
                    result,
                    binding.ThreadId);
                codexStatus =
                    CodexVoiceHistoryDataStatus.Available;
            }
            catch (OperationCanceledException)
                when (cancellationToken.IsCancellationRequested)
            {
                throw;
            }
            catch
            {
                codexStatus = _lastCodexHistory.Count > 0
                    ? CodexVoiceHistoryDataStatus.Stale
                    : CodexVoiceHistoryDataStatus.Unavailable;
                gap = true;
            }
        }

        return new CodexVoiceHistorySnapshot(
            bindingStatus,
            binding!.ThreadId,
            binding.Version,
            voiceStatus,
            _lastVoiceRecent,
            codexStatus,
            _lastCodexHistory,
            gap);
    }

    public async ValueTask DisposeAsync()
    {
        if (_codexClient is not null)
        {
            await _codexClient.DisposeAsync().ConfigureAwait(false);
        }
    }

    private static BindingReadResult ParseBinding(string value)
    {
        using JsonDocument document = ParseJson(value);
        JsonElement root = document.RootElement;
        if (
            root.ValueKind != JsonValueKind.Object
            || !root.TryGetProperty(
                "electron-persisted-atom-state",
                out JsonElement persisted)
            || persisted.ValueKind != JsonValueKind.Object
            || !persisted.TryGetProperty(
                RecentThreadKey,
                out JsonElement recent)
            || recent.ValueKind == JsonValueKind.Null
        )
        {
            return BindingReadResult.Unbound();
        }
        if (recent.ValueKind != JsonValueKind.Object)
        {
            return BindingReadResult.Invalid();
        }
        RequireOnlyProperties(
            recent,
            ["conversationId", "hostId", "version"]);
        if (
            !recent.TryGetProperty(
                "conversationId",
                out JsonElement conversationId)
            || conversationId.ValueKind != JsonValueKind.String
            || !Guid.TryParse(
                conversationId.GetString(),
                out Guid threadId)
            || threadId == Guid.Empty
            || !recent.TryGetProperty(
                "hostId",
                out JsonElement hostId)
            || hostId.ValueKind != JsonValueKind.String
            || !recent.TryGetProperty(
                "version",
                out JsonElement version)
            || !version.TryGetInt64(out long parsedVersion)
            || parsedVersion < 0
        )
        {
            return BindingReadResult.Invalid();
        }
        if (
            !string.Equals(
                hostId.GetString(),
                "local",
                StringComparison.Ordinal)
        )
        {
            return BindingReadResult.Unbound();
        }
        return BindingReadResult.Bound(
            new CodexVoiceThreadBinding(
                threadId.ToString(),
                parsedVersion));
    }

    private static IReadOnlyList<CodexVoiceHistoryMessage>
        ParseContinuity(
            string value,
            string threadId)
    {
        using JsonDocument document = ParseJson(value);
        JsonElement root = document.RootElement;
        if (root.ValueKind != JsonValueKind.Object)
        {
            throw new InvalidDataException(
                "BW_CODEX_VOICE_CONTINUITY_ROOT_INVALID");
        }
        RequireOnlyProperties(root, ["version", "threads"]);
        if (
            !root.TryGetProperty(
                "version",
                out JsonElement version)
            || !version.TryGetInt32(out int parsedVersion)
            || parsedVersion != 1
            || !root.TryGetProperty(
                "threads",
                out JsonElement threads)
            || threads.ValueKind != JsonValueKind.Object
        )
        {
            throw new InvalidDataException(
                "BW_CODEX_VOICE_CONTINUITY_CONTRACT_INVALID");
        }
        if (
            !threads.TryGetProperty(
                threadId,
                out JsonElement thread)
        )
        {
            return Array.Empty<CodexVoiceHistoryMessage>();
        }
        if (thread.ValueKind != JsonValueKind.Object)
        {
            throw new InvalidDataException(
                "BW_CODEX_VOICE_CONTINUITY_THREAD_INVALID");
        }
        RequireOnlyProperties(thread, ["items"]);
        if (
            !thread.TryGetProperty(
                "items",
                out JsonElement items)
            || items.ValueKind != JsonValueKind.Array
            || items.GetArrayLength() > MaximumContinuityItems
        )
        {
            throw new InvalidDataException(
                "BW_CODEX_VOICE_CONTINUITY_ITEMS_INVALID");
        }
        List<CodexVoiceHistoryMessage> parsed = [];
        foreach (JsonElement item in items.EnumerateArray())
        {
            if (item.ValueKind != JsonValueKind.Object)
            {
                throw new InvalidDataException(
                    "BW_CODEX_VOICE_CONTINUITY_ITEM_INVALID");
            }
            RequireOnlyProperties(item, ["role", "text"]);
            if (
                !item.TryGetProperty(
                    "role",
                    out JsonElement role)
                || role.ValueKind != JsonValueKind.String
                || !item.TryGetProperty(
                    "text",
                    out JsonElement text)
                || text.ValueKind != JsonValueKind.String
            )
            {
                throw new InvalidDataException(
                    "BW_CODEX_VOICE_CONTINUITY_ITEM_INVALID");
            }
            string roleValue = role.GetString() ?? "";
            string textValue = (text.GetString() ?? "").Trim();
            if (
                roleValue is not ("user" or "assistant")
                || textValue.Length == 0
                || textValue.Length > MaximumContinuityTextLength
                || textValue.Contains('\0')
            )
            {
                throw new InvalidDataException(
                    "BW_CODEX_VOICE_CONTINUITY_ITEM_INVALID");
            }
            parsed.Add(new CodexVoiceHistoryMessage(
                roleValue,
                textValue));
        }
        return parsed.AsReadOnly();
    }

    private static IReadOnlyList<CodexVoiceHistoryMessage>
        ParseCodexHistory(
            string value,
            string expectedThreadId)
    {
        using JsonDocument document = ParseJson(value);
        JsonElement result = document.RootElement;
        if (
            result.ValueKind != JsonValueKind.Object
            || !result.TryGetProperty(
                "thread",
                out JsonElement thread)
            || thread.ValueKind != JsonValueKind.Object
            || !thread.TryGetProperty(
                "id",
                out JsonElement threadId)
            || threadId.ValueKind != JsonValueKind.String
            || !string.Equals(
                threadId.GetString(),
                expectedThreadId,
                StringComparison.Ordinal)
            || !thread.TryGetProperty(
                "turns",
                out JsonElement turns)
            || turns.ValueKind != JsonValueKind.Array
        )
        {
            throw new InvalidDataException(
                "BW_CODEX_THREAD_HISTORY_RESULT_INVALID");
        }
        List<CodexVoiceHistoryMessage> parsed = [];
        foreach (JsonElement turn in turns.EnumerateArray())
        {
            if (
                turn.ValueKind != JsonValueKind.Object
                || !turn.TryGetProperty(
                    "items",
                    out JsonElement items)
                || items.ValueKind != JsonValueKind.Array
            )
            {
                continue;
            }
            foreach (JsonElement item in items.EnumerateArray())
            {
                if (
                    item.ValueKind != JsonValueKind.Object
                    || !item.TryGetProperty(
                        "type",
                        out JsonElement type)
                    || type.ValueKind != JsonValueKind.String
                )
                {
                    continue;
                }
                string? itemType = type.GetString();
                if (itemType == "userMessage")
                {
                    AddUserMessageContent(parsed, item);
                }
                else if (itemType == "agentMessage")
                {
                    AddFinalAgentMessage(parsed, item);
                }
            }
        }
        if (parsed.Count > MaximumCodexHistoryItems)
        {
            parsed = parsed
                .TakeLast(MaximumCodexHistoryItems)
                .ToList();
        }
        return parsed.AsReadOnly();
    }

    private static void AddUserMessageContent(
        ICollection<CodexVoiceHistoryMessage> destination,
        JsonElement item)
    {
        if (
            !item.TryGetProperty(
                "content",
                out JsonElement content)
            || content.ValueKind != JsonValueKind.Array
        )
        {
            return;
        }
        foreach (JsonElement part in content.EnumerateArray())
        {
            if (
                part.ValueKind != JsonValueKind.Object
                || !part.TryGetProperty(
                    "type",
                    out JsonElement type)
                || type.ValueKind != JsonValueKind.String
                || type.GetString() != "text"
                || !part.TryGetProperty(
                    "text",
                    out JsonElement text)
                || text.ValueKind != JsonValueKind.String
            )
            {
                continue;
            }
            AddBoundedMessage(
                destination,
                "user",
                text.GetString());
        }
    }

    private static void AddFinalAgentMessage(
        ICollection<CodexVoiceHistoryMessage> destination,
        JsonElement item)
    {
        if (
            !item.TryGetProperty(
                "phase",
                out JsonElement phase)
            || phase.ValueKind != JsonValueKind.String
            || phase.GetString() != "final_answer"
            || !item.TryGetProperty(
                "text",
                out JsonElement text)
            || text.ValueKind != JsonValueKind.String
        )
        {
            return;
        }
        AddBoundedMessage(
            destination,
            "assistant",
            text.GetString());
    }

    private static void AddBoundedMessage(
        ICollection<CodexVoiceHistoryMessage> destination,
        string role,
        string? text)
    {
        string value = (text ?? "").Trim();
        if (
            value.Length == 0
            || value.Length > MaximumCodexHistoryTextLength
            || value.Contains('\0')
        )
        {
            return;
        }
        destination.Add(new CodexVoiceHistoryMessage(role, value));
    }

    private static void RequireOnlyProperties(
        JsonElement value,
        string[] allowed)
    {
        foreach (JsonProperty property in value.EnumerateObject())
        {
            if (!allowed.Contains(property.Name))
            {
                throw new InvalidDataException(
                    "BW_CODEX_VOICE_HISTORY_UNKNOWN_FIELD");
            }
        }
    }

    private static JsonDocument ParseJson(string value) =>
        JsonDocument.Parse(
            value,
            new JsonDocumentOptions
            {
                AllowTrailingCommas = false,
                CommentHandling = JsonCommentHandling.Disallow,
                MaxDepth = 128,
            });

    private sealed record CodexVoiceThreadBinding(
        string ThreadId,
        long Version);

    private enum BindingReadStatus
    {
        Bound,
        Unbound,
        Invalid,
    }

    private sealed record BindingReadResult(
        BindingReadStatus Status,
        CodexVoiceThreadBinding? Binding)
    {
        internal static BindingReadResult Bound(
            CodexVoiceThreadBinding binding) =>
            new(BindingReadStatus.Bound, binding);

        internal static BindingReadResult Unbound() =>
            new(BindingReadStatus.Unbound, null);

        internal static BindingReadResult Invalid() =>
            new(BindingReadStatus.Invalid, null);
    }
}
