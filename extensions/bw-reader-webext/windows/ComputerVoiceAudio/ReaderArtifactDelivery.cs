using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;

namespace BwReader.ComputerVoiceAudio;

// Shared by ordinary MCP sends and Jev's direct resend. It stores the last
// generated payload, not a new card entity; resends only display that payload.
internal sealed partial class ReaderRealtimeOutputBroker
{
    private sealed class ArtifactFlight
    {
        internal readonly TaskCompletionSource<ReaderRealtimeOutputAck> Completion =
            new(TaskCreationOptions.RunContinuationsAsynchronously);
        internal long Finished;
        internal bool Unknown;
    }
    private readonly object _artifactGate = new();
    private readonly Dictionary<string, ArtifactFlight> _artifactFlights = new();
    private readonly Dictionary<string, JsonObject> _resendJobs = new();
    private ReaderRealtimeOutputRequest? _latestArtifact;
    private string? _artifactPath;
    internal Func<long> ArtifactClock { get; set; } = () => Environment.TickCount64;

    private void InitializeArtifactDelivery(string? outboxPath)
    {
        if (string.IsNullOrWhiteSpace(outboxPath)) return;
        _artifactPath = Path.Combine(Path.GetDirectoryName(outboxPath)!, "reader-latest-artifact.json");
        try
        {
            using var doc = System.Text.Json.JsonDocument.Parse(File.ReadAllText(_artifactPath));
            var saved = ReaderRealtimeOutputRpcProtocol.ValidateRequest(doc.RootElement);
            if (saved.Kind is "card" or "anki-draft") _latestArtifact = saved;
        }
        catch (Exception error) when (error is IOException or System.Text.Json.JsonException
            or ReaderRealtimeOutputException or UnauthorizedAccessException) { }
    }

    private static JsonNode Canonical(JsonNode node) => node switch
    {
        JsonObject obj => new JsonObject(obj.OrderBy(x => x.Key, StringComparer.Ordinal)
            .Select(x => KeyValuePair.Create(x.Key, x.Value is null ? null : Canonical(x.Value)))),
        JsonArray arr => new JsonArray(arr.Select(x => x is null ? null : Canonical(x)).ToArray()),
        _ => node.DeepClone()
    };

    internal static JsonNode DisplayPayload(ReaderRealtimeOutputRequest request)
    {
        var payload = request.Payload.DeepClone();
        // A resend is a display operation, never another placement mutation.
        if (request.Kind == "card" && payload["card"] is JsonObject card) card.Remove("bind");
        return payload;
    }

    private static string ArtifactFingerprint(ReaderRealtimeOutputRequest request) =>
        request.SourceInstanceId + ":" + request.Kind + ":" + Convert.ToHexString(
            SHA256.HashData(Encoding.UTF8.GetBytes(
                (request.Payload["card"]?["bind"] is not null ? request.File + ":" + request.Page.ToJsonString() : "")
                + Canonical(request.Payload).ToJsonString())));

    private static string ArtifactTitle(ReaderRealtimeOutputRequest request) =>
        request.Kind == "card" ? request.Payload["card"]?["title"]?.GetValue<string>() ?? "生成物"
        : "Anki 草稿";

    private static bool DeliveryUnknown(Exception error) => error is OperationCanceledException
        || error is ReaderRealtimeOutputException e && (e.Code.Contains("TIMEOUT", StringComparison.Ordinal)
            || e.Message.Contains("输出期间", StringComparison.Ordinal)
            || e.InnerException is ReaderVisualDeliveryException);

    internal JsonObject ArtifactState(string? requestKey = null)
    {
        lock (_artifactGate)
        {
            if (!string.IsNullOrEmpty(requestKey)) return _resendJobs.TryGetValue(requestKey, out var job)
                ? (JsonObject)job.DeepClone() : new JsonObject { ["ok"] = false, ["status"] = "not-found" };
            return new JsonObject { ["ok"] = true, ["latest"] = _latestArtifact is null ? null :
                new JsonObject { ["id"] = _latestArtifact.Correlation, ["title"] = ArtifactTitle(_latestArtifact),
                    ["kind"] = _latestArtifact.Kind } };
        }
    }

    internal JsonObject StartArtifactResend(string requestKey, string artifactId,
        string sourceInstanceId, long revision, string file, JsonNode page, CancellationToken serviceToken)
    {
        lock (_artifactGate)
        {
            if (_resendJobs.TryGetValue(requestKey, out var existing)) return (JsonObject)existing.DeepClone();
            var original = _latestArtifact;
            if (original is null || original.Correlation != artifactId)
                return new JsonObject { ["ok"] = false, ["status"] = "not-started", ["error"] = "latest-artifact-changed-or-missing" };
            var request = ReaderRealtimeOutputProtocol.Create("resend-" + Guid.NewGuid().ToString("N"),
                sourceInstanceId, revision, file, page, original.Kind, DisplayPayload(original));
            var job = new JsonObject { ["ok"] = true, ["status"] = "attempted", ["requestKey"] = requestKey,
                ["artifactId"] = original.Correlation, ["title"] = ArtifactTitle(original),
                ["correlation"] = request.Correlation };
            _resendJobs[requestKey] = job;
            // Reserve in the shared guard synchronously, before returning the attempt notice.
            var delivery = SendArtifactGuardedAsync(request, serviceToken, remember: false,
                original with { SourceInstanceId = sourceInstanceId });
            _ = FinishResendAsync(job, delivery);
            foreach (var key in _resendJobs.Keys.ToArray())
                if (_resendJobs.Count > 128 && _resendJobs[key]["status"]?.GetValue<string>() != "attempted")
                    _resendJobs.Remove(key);
            return (JsonObject)job.DeepClone();
        }
    }

    private async Task FinishResendAsync(JsonObject job, Task<ReaderRealtimeOutputAck> delivery)
    {
        try
        {
            var ack = await delivery.ConfigureAwait(false);
            lock (_artifactGate) { job["status"] = ack.Outcome == "queued" ? "queued" : "delivered"; }
        }
        catch (Exception error)
        {
            lock (_artifactGate)
            {
                job["status"] = DeliveryUnknown(error)
                    ? "unknown" : "failed";
                job["error"] = error.Message;
            }
        }
    }

    private Task<ReaderRealtimeOutputAck> SendArtifactGuardedAsync(ReaderRealtimeOutputRequest request,
        CancellationToken token, bool remember = true, ReaderRealtimeOutputRequest? original = null)
    {
        if (request.Kind is not ("card" or "anki-draft")) return SendAsync(request, token, alreadyQueued: false);
        string key = ArtifactFingerprint(original ?? request);
        string displayKey = ArtifactFingerprint(request);
        lock (_artifactGate)
        {
            long now = ArtifactClock();
            foreach (var old in _artifactFlights.Keys.ToArray())
            {
                var value = _artifactFlights[old];
                if (value.Completion.Task.IsCompleted && !value.Unknown && now - value.Finished >= 4000)
                    _artifactFlights.Remove(old);
            }
            if (_artifactFlights.TryGetValue(key, out var prior) || _artifactFlights.TryGetValue(displayKey, out prior))
                return ReuseArtifactReceiptAsync(prior.Completion.Task, request);
            var flight = new ArtifactFlight();
            _artifactFlights[key] = flight;
            if (original is not null) _artifactFlights[displayKey] = flight;
            if (remember)
            {
                _latestArtifact = request;
                if (_artifactPath is not null)
                {
                    try
                    {
                        Directory.CreateDirectory(Path.GetDirectoryName(_artifactPath)!);
                        File.WriteAllText(_artifactPath + ".tmp", ReaderRealtimeOutputRpcProtocol.Request(request).ToJsonString());
                        File.Move(_artifactPath + ".tmp", _artifactPath, overwrite: true);
                    }
                    catch (IOException error) { Console.Error.WriteLine("[artifact-history] " + error.GetType().Name); }
                }
            }
            _ = CompleteArtifactFlightAsync(key, flight, request, token);
            return flight.Completion.Task;
        }
    }

    private static async Task<ReaderRealtimeOutputAck> ReuseArtifactReceiptAsync(Task<ReaderRealtimeOutputAck> task,
        ReaderRealtimeOutputRequest request)
    {
        var ack = await task.ConfigureAwait(false);
        // The duplicate caller keeps its own transport identity, but receives
        // the actual outcome of the first operation (never invented success).
        return ack with { Correlation = request.Correlation, SourceInstanceId = request.SourceInstanceId };
    }

    private async Task CompleteArtifactFlightAsync(string key, ArtifactFlight flight,
        ReaderRealtimeOutputRequest request, CancellationToken token)
    {
        try { flight.Completion.TrySetResult(await SendAsync(request, token, alreadyQueued: false).ConfigureAwait(false)); }
        catch (Exception error)
        {
            lock (_artifactGate)
            {
                flight.Unknown = DeliveryUnknown(error);
                if (!flight.Unknown)
                    foreach (var alias in _artifactFlights.Where(x => ReferenceEquals(x.Value, flight)).Select(x => x.Key).ToArray())
                        _artifactFlights.Remove(alias); // confirmed failure allows retry
            }
            flight.Completion.TrySetException(error);
        }
        finally { lock (_artifactGate) { flight.Finished = ArtifactClock(); } }
    }
}
