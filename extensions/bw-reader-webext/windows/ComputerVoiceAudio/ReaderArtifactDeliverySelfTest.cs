using System.Text.Json;
using System.Text.Json.Nodes;

namespace BwReader.ComputerVoiceAudio;

internal static class ReaderArtifactDeliverySelfTest
{
    internal static async Task RunAsync(string root, ICollection<string> checks)
    {
        var router = new ReaderContextSourceRouter();
        var broker = new ReaderRealtimeOutputBroker(router, Path.Combine(root, "artifact-outbox.json"));
        long clock = 10000;
        broker.ArtifactClock = () => clock;
        var sent = new List<JsonObject>();
        var lease = router.Attach("artifact-source", "artifact-connection", (value, _) =>
        {
            sent.Add(JsonNode.Parse(JsonSerializer.Serialize(value))!["payload"]!.AsObject());
            return Task.CompletedTask;
        });
        void Assert(bool condition, string label)
        {
            if (!condition) throw new InvalidOperationException("artifact-self-test: " + label);
            checks.Add("artifact-" + label);
        }
        async Task WaitSent(int count)
        {
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(2));
            while (sent.Count < count) await Task.Delay(1, timeout.Token);
        }
        void Ack(JsonObject wire, bool rejected = false) => broker.Accept(lease,
            new ReaderRealtimeOutputAck("session-AAAAAAAAAAAAAAAAAAAAAA", wire["correlation"]!.GetValue<string>(),
                "artifact-source", rejected ? "rejected" : "applied", rejected ? "test-rejected" : null,
                wire["payload"]?["card"]?["bind"] is null ? null : "bound", null, "shown"));
        var original = ReaderRealtimeOutputProtocol.Create("artifact-original", "artifact-source", 1, "book.pdf",
            JsonValue.Create(3)!, "card", new JsonObject { ["card"] = new JsonObject {
                ["kind"] = "general", ["title"] = "原注解", ["data"] = new JsonObject { ["text"] = "原内容" },
                ["bind"] = new JsonObject { ["kind"] = "page-chars", ["page"] = 3, ["from"] = 1, ["to"] = 3, ["text"] = "原文" } } });
        var first = broker.SendAsync(original, CancellationToken.None);
        var second = broker.SendAsync(original with { Correlation = "artifact-duplicate" }, CancellationToken.None);
        await WaitSent(1);
        Assert(sent.Count == 1 && !first.IsCompleted && !second.IsCompleted, "in-flight-shares-single-send");
        Ack(sent[0]);
        await Task.WhenAll(first, second);
        Assert((await second).Correlation == "artifact-duplicate", "duplicate-has-own-rpc-identity");
        clock += 4001;
        var job = broker.StartArtifactResend("user-round", original.Correlation, "artifact-source", 2,
            "other-book.pdf", JsonValue.Create(8)!, CancellationToken.None);
        Assert(job["status"]!.GetValue<string>() == "attempted" && sent.Count == 2, "attempt-before-delivery");
        Assert(sent[1]["payload"]!["card"]!["bind"] is null && original.Payload["card"]!["bind"] is not null,
            "resend-displays-without-adding-placement");
        var concurrent = broker.SendAsync(original with { Correlation = "backend-racing-resend" }, CancellationToken.None);
        broker.StartArtifactResend("user-round", original.Correlation, "artifact-source", 2, "other-book.pdf", JsonValue.Create(8)!, CancellationToken.None);
        Assert(sent.Count == 2 && !concurrent.IsCompleted, "jev-and-backend-deduplicated");
        Ack(sent[1]); await concurrent;
        await broker.SendAsync(original with { Correlation = "within-cooldown" }, CancellationToken.None);
        Assert(sent.Count == 2, "four-second-cooldown");
        clock += 4001;
        var retry = broker.SendAsync(original with { Correlation = "rejected-send" }, CancellationToken.None);
        await WaitSent(3);
        Ack(sent[2], rejected: true);
        try { await retry; } catch (ReaderRealtimeOutputException) { }
        var recovered = broker.SendAsync(original with { Correlation = "after-failure" }, CancellationToken.None);
        await WaitSent(4);
        Assert(sent.Count == 4, "confirmed-failure-releases-guard");
        Ack(sent[3]); await recovered;
        var restored = new ReaderRealtimeOutputBroker(new ReaderContextSourceRouter(), Path.Combine(root, "artifact-outbox.json"));
        Assert(restored.ArtifactState()["latest"]?["id"]?.GetValue<string>() == "after-failure", "latest-survives-restart");
        router.Detach(lease);
    }
}
