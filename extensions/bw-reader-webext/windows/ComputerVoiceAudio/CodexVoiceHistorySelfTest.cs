using System.Text;
using System.Text.Json;

namespace BwReader.ComputerVoiceAudio;

internal static class CodexVoiceHistorySelfTest
{
    private const string ThreadId =
        "11111111-2222-3333-4444-555555555555";

    internal static void Run(ICollection<string> checks)
    {
        RunAsync(checks).GetAwaiter().GetResult();
    }

    private static async Task RunAsync(ICollection<string> checks)
    {
        await CheckValidProjectionAsync(checks).ConfigureAwait(false);
        await CheckContinuityFailureRetainsLastGoodAsync(checks)
            .ConfigureAwait(false);
        await CheckBindingFailureRetainsLastGoodAsync(checks)
            .ConfigureAwait(false);
        await CheckCleanUnboundClearsHistoryAsync(checks)
            .ConfigureAwait(false);
        await CheckCodexFailureRetainsLastGoodAsync(checks)
            .ConfigureAwait(false);
        await CheckCodexUnavailableIsExplicitAsync(checks)
            .ConfigureAwait(false);
        await CheckReadOnlyPersistentProtocolAsync(checks)
            .ConfigureAwait(false);
    }

    private static async Task CheckValidProjectionAsync(
        ICollection<string> checks)
    {
        FakeHistoryFileSource source = new(
            [GlobalState(ThreadId, 7)],
            [Continuity(ThreadId)]);
        FakeThreadHistoryClient client = new(
            [ThreadResult(ThreadId)]);
        await using CodexVoiceHistoryReader reader = new(
            source,
            client);

        CodexVoiceHistorySnapshot snapshot =
            await reader.PollAsync(CancellationToken.None)
                .ConfigureAwait(false);

        Require(
            snapshot.BindingStatus
                == CodexVoiceHistoryBindingStatus.Bound
            && snapshot.ThreadId == ThreadId
            && snapshot.BindingVersion == 7
            && snapshot.VoiceRecentStatus
                == CodexVoiceHistoryDataStatus.Available
            && snapshot.VoiceRecent.SequenceEqual(
                [
                    new("user", "voice user"),
                    new("assistant", "voice answer"),
                ])
            && snapshot.CodexHistoryStatus
                == CodexVoiceHistoryDataStatus.Available
            && snapshot.CodexHistory.SequenceEqual(
                [
                    new("user", "typed user"),
                    new("assistant", "final answer"),
                ])
            && !snapshot.Gap
            && client.ThreadIds.SequenceEqual([ThreadId]),
            "codex-voice-history-strict-user-final-projection",
            checks);
    }

    private static async Task
        CheckContinuityFailureRetainsLastGoodAsync(
            ICollection<string> checks)
    {
        FakeHistoryFileSource source = new(
            [
                GlobalState(ThreadId, 1),
                GlobalState(ThreadId, 2),
            ],
            [
                Continuity(ThreadId),
                InvalidContinuityWithUnknownItemField(ThreadId),
            ]);
        await using CodexVoiceHistoryReader reader = new(source);

        CodexVoiceHistorySnapshot first =
            await reader.PollAsync(CancellationToken.None)
                .ConfigureAwait(false);
        CodexVoiceHistorySnapshot second =
            await reader.PollAsync(CancellationToken.None)
                .ConfigureAwait(false);

        Require(
            first.VoiceRecentStatus
                == CodexVoiceHistoryDataStatus.Available
            && second.BindingStatus
                == CodexVoiceHistoryBindingStatus.Bound
            && second.VoiceRecentStatus
                == CodexVoiceHistoryDataStatus.Stale
            && second.VoiceRecent.SequenceEqual(first.VoiceRecent)
            && second.Gap,
            "codex-voice-history-continuity-last-good-on-gap",
            checks);
    }

    private static async Task
        CheckBindingFailureRetainsLastGoodAsync(
            ICollection<string> checks)
    {
        FakeHistoryFileSource source = new(
            [
                GlobalState(ThreadId, 5),
                """
                {"electron-persisted-atom-state":{"realtime-voice-most-recent-thread":{"conversationId":"bad","hostId":"local","version":6}}}
                """,
            ],
            [
                Continuity(ThreadId),
                Continuity(ThreadId),
            ]);
        await using CodexVoiceHistoryReader reader = new(source);

        _ = await reader.PollAsync(CancellationToken.None)
            .ConfigureAwait(false);
        CodexVoiceHistorySnapshot second =
            await reader.PollAsync(CancellationToken.None)
                .ConfigureAwait(false);

        Require(
            second.BindingStatus
                == CodexVoiceHistoryBindingStatus.Stale
            && second.ThreadId == ThreadId
            && second.BindingVersion == 5
            && second.VoiceRecentStatus
                == CodexVoiceHistoryDataStatus.Available
            && second.Gap,
            "codex-voice-history-binding-last-good-on-gap",
            checks);
    }

    private static async Task CheckCleanUnboundClearsHistoryAsync(
        ICollection<string> checks)
    {
        FakeHistoryFileSource source = new(
            [
                GlobalState(ThreadId, 1),
                """{"electron-persisted-atom-state":{}}""",
            ],
            [Continuity(ThreadId)]);
        FakeThreadHistoryClient client = new(
            [ThreadResult(ThreadId)]);
        await using CodexVoiceHistoryReader reader = new(
            source,
            client);

        _ = await reader.PollAsync(CancellationToken.None)
            .ConfigureAwait(false);
        CodexVoiceHistorySnapshot second =
            await reader.PollAsync(CancellationToken.None)
                .ConfigureAwait(false);

        Require(
            second.BindingStatus
                == CodexVoiceHistoryBindingStatus.Unbound
            && second.ThreadId is null
            && second.VoiceRecent.Count == 0
            && second.CodexHistory.Count == 0
            && second.VoiceRecentStatus
                == CodexVoiceHistoryDataStatus.Unavailable
            && second.CodexHistoryStatus
                == CodexVoiceHistoryDataStatus.Unavailable
            && !second.Gap
            && source.ContinuityReadCount == 1
            && client.ThreadIds.Count == 1,
            "codex-voice-history-clean-unbound-clears-cache",
            checks);
    }

    private static async Task
        CheckCodexFailureRetainsLastGoodAsync(
            ICollection<string> checks)
    {
        FakeHistoryFileSource source = new(
            [
                GlobalState(ThreadId, 1),
                GlobalState(ThreadId, 2),
            ],
            [
                Continuity(ThreadId),
                Continuity(ThreadId),
            ]);
        FakeThreadHistoryClient client = new(
            [
                ThreadResult(ThreadId),
                new IOException("fake"),
            ]);
        await using CodexVoiceHistoryReader reader = new(
            source,
            client);

        CodexVoiceHistorySnapshot first =
            await reader.PollAsync(CancellationToken.None)
                .ConfigureAwait(false);
        CodexVoiceHistorySnapshot second =
            await reader.PollAsync(CancellationToken.None)
                .ConfigureAwait(false);

        Require(
            first.CodexHistoryStatus
                == CodexVoiceHistoryDataStatus.Available
            && second.CodexHistoryStatus
                == CodexVoiceHistoryDataStatus.Stale
            && second.CodexHistory.SequenceEqual(
                first.CodexHistory)
            && second.Gap,
            "codex-voice-history-thread-last-good-on-gap",
            checks);
    }

    private static async Task CheckCodexUnavailableIsExplicitAsync(
        ICollection<string> checks)
    {
        FakeHistoryFileSource source = new(
            [GlobalState(ThreadId, 1)],
            [Continuity(ThreadId)]);
        await using CodexVoiceHistoryReader reader = new(source);

        CodexVoiceHistorySnapshot snapshot =
            await reader.PollAsync(CancellationToken.None)
                .ConfigureAwait(false);

        Require(
            snapshot.BindingStatus
                == CodexVoiceHistoryBindingStatus.Bound
            && snapshot.VoiceRecentStatus
                == CodexVoiceHistoryDataStatus.Available
            && snapshot.CodexHistoryStatus
                == CodexVoiceHistoryDataStatus.Unavailable
            && snapshot.CodexHistory.Count == 0
            && !snapshot.Gap,
            "codex-voice-history-full-history-explicitly-unavailable",
            checks);
    }

    private static async Task CheckReadOnlyPersistentProtocolAsync(
        ICollection<string> checks)
    {
        string responses = string.Join(
            Environment.NewLine,
            """
            {"id":1,"result":{"userAgent":"fake"}}
            """,
            ReplaceThreadId(
                """
                {"id":2,"result":{"thread":{"id":"$THREAD$","turns":[]}}}
                """),
            ReplaceThreadId(
                """
                {"id":3,"result":{"thread":{"id":"$THREAD$","turns":[]}}}
                """),
            "");
        FakeAppServerTransport transport = new(responses);
        FakeAppServerTransportFactory factory = new(transport);
        CodexAppServerReadOnlyHistoryClient client = new(factory);

        _ = await client.ReadThreadAsync(
            ThreadId,
            CancellationToken.None).ConfigureAwait(false);
        _ = await client.ReadThreadAsync(
            ThreadId,
            CancellationToken.None).ConfigureAwait(false);

        string[] lines = transport.WrittenLines;
        string[] methods = lines
            .Select(ReadMethod)
            .ToArray();
        bool firstReadValid = ReadRequestIsValid(lines[2], 2);
        bool secondReadValid = ReadRequestIsValid(lines[3], 3);
        await client.DisposeAsync().ConfigureAwait(false);

        Require(
            factory.StartCount == 1
            && methods.SequenceEqual(
                [
                    "initialize",
                    "initialized",
                    "thread/read",
                    "thread/read",
                ])
            && firstReadValid
            && secondReadValid
            && transport.Disposed,
            "codex-voice-history-app-server-read-only-persistent",
            checks);
    }

    private static string ReadMethod(string line)
    {
        using JsonDocument document = JsonDocument.Parse(line);
        return document.RootElement.GetProperty("method").GetString()
            ?? "";
    }

    private static bool ReadRequestIsValid(
        string line,
        long expectedId)
    {
        using JsonDocument document = JsonDocument.Parse(line);
        JsonElement root = document.RootElement;
        if (
            !root.TryGetProperty("id", out JsonElement id)
            || !id.TryGetInt64(out long actualId)
            || actualId != expectedId
            || root.GetProperty("method").GetString()
                != "thread/read"
        )
        {
            return false;
        }
        JsonElement parameters = root.GetProperty("params");
        return parameters.GetProperty("threadId").GetString()
                == ThreadId
            && parameters.GetProperty("includeTurns").GetBoolean();
    }

    private static string GlobalState(
        string threadId,
        long version) =>
        """
        {"other":"allowed","electron-persisted-atom-state":{"other":"allowed","realtime-voice-most-recent-thread":{"conversationId":"$THREAD$","hostId":"local","version":$VERSION$}}}
        """
            .Replace("$THREAD$", threadId, StringComparison.Ordinal)
            .Replace(
                "$VERSION$",
                version.ToString(
                    System.Globalization.CultureInfo.InvariantCulture),
                StringComparison.Ordinal);

    private static string Continuity(string threadId) =>
        """
        {"version":1,"threads":{"$THREAD$":{"items":[{"role":"user","text":" voice user "},{"role":"assistant","text":"voice answer"}]}}}
        """.Replace("$THREAD$", threadId, StringComparison.Ordinal);

    private static string InvalidContinuityWithUnknownItemField(
        string threadId) =>
        """
        {"version":1,"threads":{"$THREAD$":{"items":[{"role":"user","text":"voice user","unexpected":true}]}}}
        """.Replace("$THREAD$", threadId, StringComparison.Ordinal);

    private static string ThreadResult(string threadId) =>
        """
        {"thread":{"id":"$THREAD$","turns":[{"items":[{"type":"userMessage","content":[{"type":"text","text":" typed user "},{"type":"image","url":"private"},{"type":"text","text":""}]},{"type":"agentMessage","phase":"commentary","text":"hidden commentary"},{"type":"agentMessage","phase":"final_answer","text":" final answer "},{"type":"agentMessage","text":"hidden missing phase"},{"type":"reasoning","summary":"hidden reasoning"},{"type":"toolCall","name":"hidden tool"}]}]}}
        """.Replace("$THREAD$", threadId, StringComparison.Ordinal);

    private static string ReplaceThreadId(string value) =>
        value.Replace("$THREAD$", ThreadId, StringComparison.Ordinal);

    private static void Require(
        bool condition,
        string name,
        ICollection<string> checks)
    {
        if (!condition)
        {
            throw new InvalidOperationException(name);
        }
        checks.Add(name);
    }

    private sealed class FakeHistoryFileSource
        : ICodexVoiceHistoryFileSource
    {
        private readonly IReadOnlyList<string> _globalStates;
        private readonly IReadOnlyList<string> _continuities;
        private int _globalIndex;
        private int _continuityIndex;

        internal FakeHistoryFileSource(
            IReadOnlyList<string> globalStates,
            IReadOnlyList<string> continuities)
        {
            _globalStates = globalStates;
            _continuities = continuities;
        }

        internal int ContinuityReadCount => _continuityIndex;

        public ValueTask<string> ReadGlobalStateAsync(
            CancellationToken cancellationToken)
        {
            cancellationToken.ThrowIfCancellationRequested();
            return ValueTask.FromResult(
                Next(_globalStates, ref _globalIndex));
        }

        public ValueTask<string> ReadContinuityAsync(
            CancellationToken cancellationToken)
        {
            cancellationToken.ThrowIfCancellationRequested();
            return ValueTask.FromResult(
                Next(_continuities, ref _continuityIndex));
        }

        private static string Next(
            IReadOnlyList<string> values,
            ref int index)
        {
            if (values.Count == 0)
            {
                throw new IOException("fake-empty");
            }
            int selected = Math.Min(index, values.Count - 1);
            index++;
            return values[selected];
        }
    }

    private sealed class FakeThreadHistoryClient
        : ICodexThreadHistoryClient
    {
        private readonly Queue<object> _results;

        internal FakeThreadHistoryClient(
            IEnumerable<object> results)
        {
            _results = new Queue<object>(results);
        }

        internal List<string> ThreadIds { get; } = [];

        public Task<string> ReadThreadAsync(
            string threadId,
            CancellationToken cancellationToken)
        {
            cancellationToken.ThrowIfCancellationRequested();
            ThreadIds.Add(threadId);
            if (_results.Count == 0)
            {
                throw new IOException("fake-empty");
            }
            object result = _results.Dequeue();
            return result is Exception exception
                ? Task.FromException<string>(exception)
                : Task.FromResult((string)result);
        }

        public ValueTask DisposeAsync() => ValueTask.CompletedTask;
    }

    private sealed class FakeAppServerTransportFactory
        : ICodexAppServerTransportFactory
    {
        private readonly ICodexAppServerTransport _transport;

        internal FakeAppServerTransportFactory(
            ICodexAppServerTransport transport)
        {
            _transport = transport;
        }

        internal int StartCount { get; private set; }

        public ValueTask<ICodexAppServerTransport> StartAsync(
            CancellationToken cancellationToken)
        {
            cancellationToken.ThrowIfCancellationRequested();
            StartCount++;
            return ValueTask.FromResult(_transport);
        }
    }

    private sealed class FakeAppServerTransport
        : ICodexAppServerTransport
    {
        private readonly StringReader _reader;
        private readonly StringWriter _writer = new(
            new StringBuilder());

        internal FakeAppServerTransport(string responses)
        {
            _reader = new StringReader(responses);
        }

        public TextReader Reader => _reader;

        public TextWriter Writer => _writer;

        public bool IsAlive => !Disposed;

        internal bool Disposed { get; private set; }

        internal string[] WrittenLines =>
            _writer.ToString().Split(
                [Environment.NewLine],
                StringSplitOptions.RemoveEmptyEntries);

        public ValueTask DisposeAsync()
        {
            Disposed = true;
            _reader.Dispose();
            _writer.Dispose();
            return ValueTask.CompletedTask;
        }
    }
}
