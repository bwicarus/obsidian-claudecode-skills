using System.Buffers.Binary;
using System.Diagnostics;
using System.Net;
using System.Net.WebSockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.AspNetCore.Http;

namespace BwReader.ComputerVoiceAudio;

internal static class DirectBridgeSelfTest
{
    internal static void Run(ICollection<string> checks)
    {
        string root = System.IO.Path.Combine(
            System.IO.Path.GetTempPath(),
            "bw-direct-self-test-"
                + Convert.ToHexString(RandomNumberGenerator.GetBytes(8)));
        Directory.CreateDirectory(root);
        try
        {
            RunAsync(root, checks).GetAwaiter().GetResult();
        }
        finally
        {
            string resolvedRoot = System.IO.Path.GetFullPath(root);
            string resolvedTemp = System.IO.Path.GetFullPath(
                System.IO.Path.GetTempPath());
            if (
                resolvedRoot.StartsWith(
                    resolvedTemp,
                    StringComparison.OrdinalIgnoreCase)
                && System.IO.Path.GetFileName(resolvedRoot).StartsWith(
                    "bw-direct-self-test-",
                    StringComparison.Ordinal)
            )
            {
                Directory.Delete(resolvedRoot, recursive: true);
            }
        }
    }

    private static async Task RunAsync(
        string root,
        ICollection<string> checks)
    {
        const string origin = "https://bwicarus.taile44d0c.ts.net";
        DateTimeOffset now = new(
            2026,
            7,
            29,
            5,
            0,
            0,
            TimeSpan.Zero);
        string installationRoot = System.IO.Path.Combine(root, "main");
        string configPath = System.IO.Path.Combine(
            installationRoot,
            "native-host",
            "direct.json");
        string statusPath = System.IO.Path.Combine(
            installationRoot,
            "runtime",
            "computer-voice-direct.status.json");
        await WriteConfigAsync(
            configPath,
            statusPath,
            origin,
            localOptIn: true).ConfigureAwait(false);

        DirectBridgeConfigStore store = new(configPath);
        DirectBridgeConfig initial = store.Load();
        Require(
            initial.ListenHost == "127.0.0.1"
            && initial.ListenPort == 43128
            && initial.AllowedOrigins.SetEquals(new[] { origin })
            && initial.AllowedTailscaleUserLogin
                == "bwicarus@gmail.com"
            && initial.ExperimentalSingleUserMode
            && initial.VirtualMicrophoneRenderEndpointId
                == "{0.0.0.00000000}."
                    + "{11111111-1111-1111-1111-111111111111}"
            && initial.VirtualMicrophoneCaptureEndpointId
                == "{0.0.1.00000000}."
                    + "{22222222-2222-2222-2222-222222222222}"
            && initial.VirtualSpeakerRenderEndpointId
                == "{0.0.0.00000000}."
                    + "{33333333-3333-3333-3333-333333333333}"
            && initial.PerAppAudioRouteAutomationEnabled
            && initial.ExperimentalSingleUserMode
            && initial.ContextDeliveryMode
                == DirectContextDeliveryMode.LegacyInject,
            "direct-config-v5-localhost-single-user-explicit-a-b-routes",
            checks);
        Require(
            DirectBridgeServer.TailscaleLoginMatches(
                initial,
                new Microsoft.Extensions.Primitives.StringValues(
                    "BWICARUS@GMAIL.COM"))
            && !DirectBridgeServer.TailscaleLoginMatches(
                initial,
                Microsoft.Extensions.Primitives.StringValues.Empty)
            && !DirectBridgeServer.TailscaleLoginMatches(
                initial,
                new Microsoft.Extensions.Primitives.StringValues(
                    new[]
                    {
                        "bwicarus@gmail.com",
                        "attacker@example.com",
                    }))
            && !DirectBridgeServer.TailscaleLoginMatches(
                initial,
                new Microsoft.Extensions.Primitives.StringValues(
                    "attacker@example.com")),
            "direct-tailscale-identity-header-is-single-exact-login",
            checks);
        DirectRuntimeStatusWriter statusWriter = new(
            statusPath,
            "0123456789abcdef0123456789abcdef");
        await statusWriter.WriteAsync(
            "idle",
            readerConnected: false,
            captureActive: false,
            CancellationToken.None).ConfigureAwait(false);
        using (JsonDocument statusDocument = JsonDocument.Parse(
            await File.ReadAllTextAsync(statusPath).ConfigureAwait(false)))
        {
            HashSet<string> statusKeys = statusDocument.RootElement
                .EnumerateObject()
                .Select(property => property.Name)
                .ToHashSet(StringComparer.Ordinal);
            Require(
                statusKeys.SetEquals(new[]
                {
                    "contract",
                    "serviceInstanceId",
                    "pid",
                    "state",
                    "readerConnected",
                    "captureActive",
                    "lastError",
                    "updatedAtUtc",
                })
                && statusDocument.RootElement.GetProperty("state")
                    .GetString() == "idle"
                && statusDocument.RootElement.GetProperty("lastError")
                    .ValueKind == JsonValueKind.Null
                && DirectBridgeContract.IsServiceInstanceId(
                    statusDocument.RootElement
                        .GetProperty("serviceInstanceId")
                        .GetString()!),
                "direct-runtime-status-is-atomic-bounded-schema",
                checks);
        }
        Require(
            DirectBridgeContract.RuntimeStatusHeartbeatInterval
                == TimeSpan.FromSeconds(5),
            "direct-runtime-status-heartbeat-is-five-seconds",
            checks);
        CheckDirectOutputRouteEvidence(initial, checks);
        await CheckServiceLeaseAsync(
            installationRoot,
            configPath,
            checks).ConfigureAwait(false);
        await CheckUnverifiedRouteStatusDoesNotGateStartAsync(
            store,
            checks).ConfigureAwait(false);

        FakeDirectAppLauncher appLauncher = new();
        FakeDirectMediaAdapter mediaAdapter = new()
        {
            EmitDuringStart = true,
        };
        FakeDirectContextAdapter contextAdapter = new();
        await using DirectBridgeCoordinator coordinator = new(
            store,
            appLauncher,
            mediaAdapter,
            renderEndpointProbe: _ => null,
            contextAdapter: contextAdapter);
        DirectBridgeProtocolSession directSession = new(
            "connection-v3-self-test",
            "https://extension-page.example",
            store,
            coordinator,
            () => now);
        List<object> directEvents = [];
        List<byte[]> directPcmFrames = [];
        JsonElement directHello = await SendAsync(
            directSession,
            new
            {
                contract = DirectBridgeContract.Contract,
                type = "hello",
                requestId = "request-v3-hello",
                protocolVersion = 3,
            },
            directEvents,
            directPcmFrames).ConfigureAwait(false);
        JsonElement directHelloPayload = RequireSuccess(
            directHello,
            "hello");
        Require(
            directHelloPayload.GetProperty("protocolVersion")
                .GetInt32() == 3
            && directHelloPayload.GetProperty("limits")
                .GetProperty("pcmFrameBytes").GetInt32() == 1956
            && directHelloPayload.GetProperty("limits")
                .GetProperty("uplinkTrack").GetInt32() == 3
            && directHelloPayload.GetProperty("limits")
                .GetProperty("uplinkQueueLimitMs").GetInt32() == 200
            && directSession.IsAuthenticated
            && directSession.Phase == DirectProtocolPhase.AwaitingStart
            && appLauncher.EnsureRunningCount == 0
            && mediaAdapter.StartCount == 0
            && directEvents.Count == 0
            && directPcmFrames.Count == 0,
            "direct-v3-hello-authenticates-without-side-effects",
            checks);

        DirectBridgeProtocolSession session = new(
            "connection-self-test",
            origin,
            store,
            coordinator,
            () => now);
        Require(
            !session.IsAuthenticated
            && session.Phase
                == DirectProtocolPhase.AwaitingAuthentication,
            "direct-new-connection-awaits-authentication",
            checks);
        List<object> events = [];
        List<byte[]> pcmFrames = [];

        JsonElement hello = await SendAsync(
            session,
            new
            {
                contract = DirectBridgeContract.Contract,
                type = "hello",
                requestId = "request-hello",
                protocolVersion = 3,
            },
            events,
            pcmFrames).ConfigureAwait(false);
        JsonElement helloPayload = RequireSuccess(
            hello,
            "hello");
        JsonElement limits = helloPayload.GetProperty("limits");
        Require(
            helloPayload.GetProperty("protocolVersion").GetInt32() == 3
            && limits.GetProperty("maxMessageBytes").GetInt32() == 65536
            && limits.GetProperty("pcmFrameBytes").GetInt32() == 1956
            && limits.GetProperty("pcmQueueLimitMs").GetInt32() == 400
            && limits.GetProperty("uplinkTrack").GetInt32() == 3
            && limits.GetProperty("uplinkQueueLimitMs").GetInt32() == 200
            && limits.GetProperty("heartbeatIntervalMs").GetInt32()
                == 5_000
            && limits.GetProperty("heartbeatTimeoutMs").GetInt32()
                == 15_000
            && appLauncher.EnsureRunningCount == 0
            && mediaAdapter.StartCount == 0
            && session.Authenticated
            && session.IsAuthenticated
            && session.Phase == DirectProtocolPhase.AwaitingStart,
            "direct-v3-hello-is-side-effect-free-and-authenticates",
            checks);

        JsonElement inactiveContext = await SendAsync(
            session,
            new
            {
                contract = DirectBridgeContract.Contract,
                type = "context",
                requestId = "request-context-inactive",
                sessionId = "session-" + DirectBase64Url.Encode(
                    Enumerable.Repeat((byte)0x11, 16).ToArray()),
                contextContract =
                    NamedPipeDirectContextAdapter.ContextContract,
                @event = new
                {
                    v = 1,
                    seq = 1,
                    type = "focus",
                    ts = 1_750_000_001,
                    id = "0000000000000001",
                },
            },
            events,
            pcmFrames).ConfigureAwait(false);
        Require(
            !inactiveContext.GetProperty("ok").GetBoolean()
            && inactiveContext.GetProperty("error")
                .GetProperty("code").GetString()
                == "BW_COMPUTER_VOICE_CONTEXT_NOT_ACTIVE"
            && contextAdapter.ForwardCount == 0,
            "direct-context-is-rejected-before-active-start",
            checks);

        JsonElement status = await SendAsync(
            session,
            new
            {
                contract = DirectBridgeContract.Contract,
                type = "status",
                requestId = "request-status",
            },
            events,
            pcmFrames).ConfigureAwait(false);
        JsonElement statusPayload = RequireSuccess(status, "status");
        Require(
            statusPayload.GetProperty("ready").GetBoolean()
            && statusPayload.GetProperty("state").GetString() == "idle"
            && HasExactStatusKeys(statusPayload)
            && appLauncher.EnsureRunningCount == 0
            && appLauncher.WaitReadyCount == 0
            && mediaAdapter.StartCount == 0
            && session.Phase == DirectProtocolPhase.AwaitingStart,
            "direct-status-and-model-selection-never-launch-app",
            checks);

        string sessionId = "session-" + DirectBase64Url.Encode(
            Enumerable.Range(0, 16)
                .Select(value => (byte)value)
                .ToArray());
        List<string> startWireOrder = [];
        JsonElement start = await SendAsync(
            session,
            new
            {
                contract = DirectBridgeContract.Contract,
                type = "start",
                requestId = "request-start",
                sessionId,
            },
            events,
            pcmFrames,
            startWireOrder).ConfigureAwait(false);
        JsonElement startPayload = RequireSuccess(start, "start");
        Require(
            startPayload.GetProperty("state").GetString() == "active"
            && appLauncher.EnsureRunningCount == 1
            && appLauncher.WaitReadyCount == 1
            && mediaAdapter.StartCount == 1
            && mediaAdapter.CaptureActive
            && events.Count == 3
            && session.Phase == DirectProtocolPhase.Active,
            "direct-start-is-only-app-and-capture-trigger",
            checks);

        JsonElement acceptedContext = await SendAsync(
            session,
            new
            {
                contract = DirectBridgeContract.Contract,
                type = "context",
                requestId = "request-context-accepted",
                sessionId,
                contextContract =
                    NamedPipeDirectContextAdapter.ContextContract,
                @event = new
                {
                    v = 1,
                    seq = 1,
                    type = "page.context",
                    ts = 1_750_000_001,
                    id = "0000000000000001",
                    file = "book.pdf",
                    page = 3,
                },
            },
            events,
            pcmFrames).ConfigureAwait(false);
        JsonElement acceptedContextPayload = RequireSuccess(
            acceptedContext,
            "context");
        Require(
            acceptedContextPayload.GetProperty("sessionId")
                .GetString() == sessionId
            && acceptedContextPayload.GetProperty("eventId")
                .GetString() == "0000000000000001"
            && acceptedContextPayload.GetProperty("seq").GetInt64() == 1
            && acceptedContextPayload.GetProperty("outcome")
                .GetString() == "accepted"
            && contextAdapter.ForwardCount == 1
            && contextAdapter.LastEvent?.Type == "page.context"
            && contextAdapter.LastEvent?.Payload
                .GetProperty("file").GetString() == "book.pdf",
            "direct-context-active-session-forwards-exact-event-after-ack",
            checks);

        contextAdapter.Failure = new DirectProtocolException(
            "BW_COMPUTER_VOICE_CONTEXT_IPC_TIMEOUT",
            "fake context timeout",
            retryable: true);
        JsonElement retryableContext = await SendAsync(
            session,
            new
            {
                contract = DirectBridgeContract.Contract,
                type = "context",
                requestId = "request-context-retryable",
                sessionId,
                contextContract =
                    NamedPipeDirectContextAdapter.ContextContract,
                @event = new
                {
                    v = 1,
                    seq = 2,
                    type = "command",
                    ts = 1_750_000_002,
                    id = "0000000000000002",
                    command = "nav.goto",
                },
            },
            events,
            pcmFrames).ConfigureAwait(false);
        Require(
            !retryableContext.GetProperty("ok").GetBoolean()
            && retryableContext.GetProperty("error")
                .GetProperty("code").GetString()
                == "BW_COMPUTER_VOICE_CONTEXT_IPC_TIMEOUT"
            && retryableContext.GetProperty("error")
                .GetProperty("retryable").GetBoolean()
            && mediaAdapter.CaptureActive
            && coordinator.ActiveSessionId == sessionId
            && session.Phase == DirectProtocolPhase.Active,
            "direct-context-ipc-error-is-retryable-without-stopping-audio",
            checks);
        contextAdapter.Failure = null;
        contextAdapter.Outcome = "duplicate";
        JsonElement duplicateContext = await SendAsync(
            session,
            new
            {
                contract = DirectBridgeContract.Contract,
                type = "context",
                requestId = "request-context-duplicate",
                sessionId,
                contextContract =
                    NamedPipeDirectContextAdapter.ContextContract,
                @event = new
                {
                    v = 1,
                    seq = 2,
                    type = "command",
                    ts = 1_750_000_002,
                    id = "0000000000000002",
                    command = "nav.goto",
                },
            },
            events,
            pcmFrames).ConfigureAwait(false);
        Require(
            RequireSuccess(duplicateContext, "context")
                .GetProperty("outcome").GetString() == "duplicate"
            && mediaAdapter.CaptureActive,
            "direct-context-duplicate-ack-is-success-with-audio-active",
            checks);
        Require(
            startWireOrder.SequenceEqual(new[] { "result", "pcm" }),
            "direct-start-result-precedes-buffered-pcm",
            checks);

        JsonElement repeatedStart = await SendAsync(
            session,
            new
            {
                contract = DirectBridgeContract.Contract,
                type = "start",
                requestId = "request-start-repeat",
                sessionId,
            },
            events,
            pcmFrames).ConfigureAwait(false);
        _ = RequireSuccess(repeatedStart, "start");
        Require(
            appLauncher.EnsureRunningCount == 1
            && appLauncher.WaitReadyCount == 1
            && mediaAdapter.StartCount == 1,
            "direct-repeat-start-is-idempotent",
            checks);

        JsonElement heartbeat = await SendAsync(
            session,
            new
            {
                contract = DirectBridgeContract.Contract,
                type = "heartbeat",
                requestId = "request-heartbeat-1",
                sessionId,
                sequence = 1,
            },
            events,
            pcmFrames).ConfigureAwait(false);
        JsonElement heartbeatPayload = RequireSuccess(
            heartbeat,
            "heartbeat");
        HashSet<string> heartbeatKeys = heartbeatPayload
            .EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        Require(
            heartbeatKeys.SetEquals(new[]
            {
                "sessionId",
                "sequence",
                "state",
            })
            && heartbeatPayload.GetProperty("sessionId").GetString()
                == sessionId
            && heartbeatPayload.GetProperty("sequence").GetUInt32() == 1
            && heartbeatPayload.GetProperty("state").GetString()
                == "active"
            && appLauncher.EnsureRunningCount == 1
            && mediaAdapter.StartCount == 1,
            "direct-heartbeat-result-is-strict-and-side-effect-free",
            checks);

        JsonElement repeatedHeartbeat = await SendAsync(
            session,
            new
            {
                contract = DirectBridgeContract.Contract,
                type = "heartbeat",
                requestId = "request-heartbeat-repeat",
                sessionId,
                sequence = 1,
            },
            events,
            pcmFrames).ConfigureAwait(false);
        Require(
            !repeatedHeartbeat.GetProperty("ok").GetBoolean()
            && repeatedHeartbeat.GetProperty("error")
                .GetProperty("code").GetString()
                == "BW_COMPUTER_VOICE_DIRECT_HEARTBEAT_SEQUENCE_INVALID",
            "direct-heartbeat-sequence-must-increase-by-one",
            checks);

        bool busyRejected = false;
        try
        {
            _ = await coordinator.StartAsync(
                "connection-other",
                "session-" + DirectBase64Url.Encode(
                    Enumerable.Repeat((byte)9, 16).ToArray()),
                (_, _) => Task.CompletedTask,
                (_, _) => Task.CompletedTask,
                CancellationToken.None).ConfigureAwait(false);
        }
        catch (DirectProtocolException exception)
        {
            busyRejected = exception.Code
                == "BW_COMPUTER_VOICE_DIRECT_BUSY";
        }
        Require(
            busyRejected
            && appLauncher.EnsureRunningCount == 1
            && mediaAdapter.StartCount == 1,
            "direct-single-session-busy-fails-closed",
            checks);

        byte[] encoded = pcmFrames.Single();
        Require(
            encoded.Length == 1956
            && encoded.AsSpan(0, 4).SequenceEqual("BWCV"u8)
            && encoded[4] == 1
            && encoded[5] == 1
            && BinaryPrimitives.ReadUInt16LittleEndian(
                encoded.AsSpan(6, 2)) == 0
            && encoded.AsSpan(8, 16).SequenceEqual(
                Enumerable.Range(0, 16)
                    .Select(value => (byte)value)
                    .ToArray())
            && BinaryPrimitives.ReadUInt32LittleEndian(
                encoded.AsSpan(24, 4)) == 0
            && BinaryPrimitives.ReadUInt64LittleEndian(
                encoded.AsSpan(28, 8)) == 1000,
            "direct-pcm-fixed-binary-frame-contract",
            checks);

        mediaAdapter.Fail(
            new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_FAKE_PUMP_FAILED",
                "fake pump failed"));
        DirectProtocolException? observedFault =
            await coordinator.MediaCompletion.ConfigureAwait(false);
        JsonElement faultEvent = JsonSerializer.SerializeToElement(
            DirectBridgeProtocolSession.StatusEvent(
                "error",
                observedFault!.Code),
            DirectBridgeContract.JsonOptions);
        Require(
            observedFault.Code
                == "BW_COMPUTER_VOICE_DIRECT_FAKE_PUMP_FAILED"
            && !mediaAdapter.CaptureActive
            && DirectBridgeServer.ShouldMonitorMedia(coordinator)
            && faultEvent.GetProperty("event").GetString() == "status"
            && faultEvent.GetProperty("payload")
                .GetProperty("state").GetString() == "error",
            "direct-media-pump-fault-is-observable-and-bounded",
            checks);

        JsonElement stop = await SendAsync(
            session,
            new
            {
                contract = DirectBridgeContract.Contract,
                type = "stop",
                requestId = "request-stop",
                sessionId,
            },
            events,
            pcmFrames).ConfigureAwait(false);
        Require(
            !stop.GetProperty("ok").GetBoolean()
            && stop.GetProperty("error").GetProperty("code")
                .GetString()
                == "BW_COMPUTER_VOICE_DIRECT_FAKE_PUMP_FAILED"
            && mediaAdapter.StopCount == 1
            && !mediaAdapter.CaptureActive
            && coordinator.ActiveSessionId is null
            && session.Phase == DirectProtocolPhase.Active,
            "direct-failed-stop-surfaces-media-failure-without-phase-change",
            checks);

        await CheckLocalOptInGateAsync(
            root,
            origin,
            checks).ConfigureAwait(false);
        await CheckAppTimeoutAsync(
            configPath,
            checks).ConfigureAwait(false);
        await CheckDisconnectCancellationAsync(
            configPath,
            checks).ConfigureAwait(false);
        await CheckConnectionTakeoverOwnershipAsync(checks)
            .ConfigureAwait(false);
        await CheckServerCloseCancelsBlockedStartAsync(
            configPath,
            checks).ConfigureAwait(false);
        await CheckServerPrefetchPreservesOrderAsync(
            checks).ConfigureAwait(false);
        await CheckEarlyBinaryCancelsStartAsync(
            checks).ConfigureAwait(false);
        await CheckHeartbeatDeadlineAsync(
            configPath,
            checks).ConfigureAwait(false);
        await CheckTypistLeaseLifecycleAsync(checks)
            .ConfigureAwait(false);
        await CheckAtomicShortcutAndBestEffortCleanupAsync(checks)
            .ConfigureAwait(false);
        await CheckPeerAbortDuringStopAsync(
            configPath,
            checks).ConfigureAwait(false);
        await CheckOuterDisposeRetryAsync(
            configPath,
            checks).ConfigureAwait(false);
        await CheckExplicitStopFailureAsync(
            configPath,
            checks).ConfigureAwait(false);
        await CheckRetainedCleanupOwnershipAsync(
            configPath,
            checks).ConfigureAwait(false);
        await CheckStaleStopPreservesActiveSessionAsync(
            configPath,
            checks).ConfigureAwait(false);
        CheckConnectionPhaseDeadlines(checks);
        CheckPcmSequenceGate(checks);
        CheckUplinkPcmContract(checks);
        await CheckPcmStartGateAbortRaceAsync(
            checks).ConfigureAwait(false);
        CheckProductionAdapterBoundaries(root, checks);
        CheckStrictOriginConfig(root, checks);
        await CheckRuntimeErrorLifecycleAsync(
            configPath,
            statusPath,
            checks).ConfigureAwait(false);
        await CheckContextIpcContractAsync(
            checks).ConfigureAwait(false);
        await CheckContextDeliveryModeSetAsync(
            root,
            origin,
            checks).ConfigureAwait(false);
        await CheckSnapshotMcpModeAsync(
            root,
            origin,
            checks).ConfigureAwait(false);
        await CheckSnapshotMetadataFoldAsync(
            root,
            checks).ConfigureAwait(false);
        await CheckReaderContextMcpProtocolAsync(
            root,
            checks).ConfigureAwait(false);
    }

    private static async Task CheckExplicitStopFailureAsync(
        string configPath,
        ICollection<string> checks)
    {
        FakeDirectMediaAdapter media = new()
        {
            StopFailure = new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_TYPIST_STOP_FAILED",
                "typist stop failed"),
        };
        await using DirectBridgeCoordinator coordinator = new(
            new DirectBridgeConfigStore(configPath),
            new FakeDirectAppLauncher(),
            media);
        DirectBridgeProtocolSession session = new(
            "connection-stop-failure",
            "https://extension-page.example",
            new DirectBridgeConfigStore(configPath),
            coordinator,
            () => DateTimeOffset.UtcNow);
        List<object> events = [];
        List<byte[]> frames = [];
        _ = RequireSuccess(
            await SendAsync(
                session,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "hello",
                    requestId = "request-stop-failure-hello",
                    protocolVersion = 3,
                },
                events,
                frames).ConfigureAwait(false),
            "hello");
        string sessionId = "session-" + DirectBase64Url.Encode(
            Enumerable.Range(32, 16)
                .Select(value => (byte)value)
                .ToArray());
        _ = RequireSuccess(
            await SendAsync(
                session,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "start",
                    requestId = "request-stop-failure-start",
                    sessionId,
                },
                events,
                frames).ConfigureAwait(false),
            "start");
        JsonElement stop = await SendAsync(
            session,
            new
            {
                contract = DirectBridgeContract.Contract,
                type = "stop",
                requestId = "request-stop-failure-stop",
                sessionId,
            },
            events,
            frames).ConfigureAwait(false);
        Require(
            !stop.GetProperty("ok").GetBoolean()
            && stop.GetProperty("error").GetProperty("code")
                .GetString()
                == "BW_COMPUTER_VOICE_DIRECT_TYPIST_STOP_FAILED"
            && coordinator.ActiveSessionId is null
            && session.Phase == DirectProtocolPhase.Active,
            "direct-explicit-stop-failure-preserves-active-phase",
            checks);
    }

    private static async Task CheckStaleStopPreservesActiveSessionAsync(
        string configPath,
        ICollection<string> checks)
    {
        FakeDirectMediaAdapter media = new();
        FakeDirectContextAdapter context = new();
        await using DirectBridgeCoordinator coordinator = new(
            new DirectBridgeConfigStore(configPath),
            new FakeDirectAppLauncher(),
            media,
            contextAdapter: context);
        DirectBridgeProtocolSession session = new(
            "connection-stale-stop",
            "https://extension-page.example",
            new DirectBridgeConfigStore(configPath),
            coordinator,
            () => DateTimeOffset.UtcNow);
        List<object> events = [];
        List<byte[]> frames = [];
        _ = RequireSuccess(
            await SendAsync(
                session,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "hello",
                    requestId = "request-stale-stop-hello",
                    protocolVersion = 3,
                },
                events,
                frames).ConfigureAwait(false),
            "hello");
        string oldSessionId = "session-" + DirectBase64Url.Encode(
            Enumerable.Repeat((byte)0x71, 16).ToArray());
        string currentSessionId = "session-" + DirectBase64Url.Encode(
            Enumerable.Repeat((byte)0x72, 16).ToArray());
        _ = RequireSuccess(
            await SendAsync(
                session,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "start",
                    requestId = "request-stale-stop-old-start",
                    sessionId = oldSessionId,
                },
                events,
                frames).ConfigureAwait(false),
            "start");
        _ = RequireSuccess(
            await SendAsync(
                session,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "stop",
                    requestId = "request-stale-stop-old-stop",
                    sessionId = oldSessionId,
                },
                events,
                frames).ConfigureAwait(false),
            "stop");
        _ = RequireSuccess(
            await SendAsync(
                session,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "start",
                    requestId = "request-stale-stop-current-start",
                    sessionId = currentSessionId,
                },
                events,
                frames).ConfigureAwait(false),
            "start");

        JsonElement staleStop = await SendAsync(
            session,
            new
            {
                contract = DirectBridgeContract.Contract,
                type = "stop",
                requestId = "request-stale-stop-late-old-stop",
                sessionId = oldSessionId,
            },
            events,
            frames).ConfigureAwait(false);
        bool staleStopPreservedCurrent =
            !staleStop.GetProperty("ok").GetBoolean()
            && staleStop.GetProperty("error").GetProperty("code")
                .GetString()
                == "BW_COMPUTER_VOICE_DIRECT_SESSION_MISMATCH"
            && session.Phase == DirectProtocolPhase.Active
            && coordinator.ActiveSessionId == currentSessionId
            && media.CaptureActive
            && media.StopCount == 1;
        Require(
            staleStopPreservedCurrent,
            "direct-stale-stop-preserves-current-active-session",
            checks);

        await media.EmitAsync(
            new DirectPcmFrame(
                DirectPcmTrack.AppOutput,
                Sequence: 7,
                TimestampMicroseconds: 7_000,
                PcmS16Le: new byte[
                    DirectBridgeContract.PcmPayloadBytes]))
            .ConfigureAwait(false);
        JsonElement heartbeat = await SendAsync(
            session,
            new
            {
                contract = DirectBridgeContract.Contract,
                type = "heartbeat",
                requestId = "request-stale-stop-current-heartbeat",
                sessionId = currentSessionId,
                sequence = 1,
            },
            events,
            frames).ConfigureAwait(false);
        JsonElement contextResult = await SendAsync(
            session,
            new
            {
                contract = DirectBridgeContract.Contract,
                type = "context",
                requestId = "request-stale-stop-current-context",
                sessionId = currentSessionId,
                contextContract =
                    NamedPipeDirectContextAdapter.ContextContract,
                @event = new
                {
                    v = 1,
                    seq = 1,
                    type = "focus",
                    ts = 1_750_000_010,
                    id = "0000000000000010",
                },
            },
            events,
            frames).ConfigureAwait(false);
        Require(
            RequireSuccess(heartbeat, "heartbeat")
                .GetProperty("sessionId").GetString()
                == currentSessionId
            && RequireSuccess(contextResult, "context")
                .GetProperty("sessionId").GetString()
                == currentSessionId
            && context.ForwardCount == 1
            && frames.Count == 1
            && frames[0].AsSpan(8, 16).SequenceEqual(
                DirectPcmFrameCodec.ParseSessionId(currentSessionId))
            && media.CaptureActive
            && coordinator.ActiveSessionId == currentSessionId
            && session.Phase == DirectProtocolPhase.Active,
            "direct-current-session-media-continues-after-stale-stop",
            checks);

        _ = RequireSuccess(
            await SendAsync(
                session,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "stop",
                    requestId = "request-stale-stop-current-stop",
                    sessionId = currentSessionId,
                },
                events,
                frames).ConfigureAwait(false),
            "stop");
        Require(
            !media.CaptureActive
            && coordinator.ActiveSessionId is null
            && session.Phase == DirectProtocolPhase.AwaitingStart
            && media.StopCount == 2,
            "direct-confirmed-current-stop-transitions-to-awaiting-start",
            checks);
    }

    private static async Task CheckRetainedCleanupOwnershipAsync(
        string configPath,
        ICollection<string> checks)
    {
        FakeDirectMediaAdapter media = new()
        {
            RetainCleanupOnStop = true,
        };
        await using DirectBridgeCoordinator coordinator = new(
            new DirectBridgeConfigStore(configPath),
            new FakeDirectAppLauncher(),
            media);
        DirectBridgeProtocolSession session = new(
            "connection-retained-cleanup",
            "https://extension-page.example",
            new DirectBridgeConfigStore(configPath),
            coordinator,
            () => DateTimeOffset.UtcNow);
        List<object> events = [];
        List<byte[]> frames = [];
        _ = RequireSuccess(
            await SendAsync(
                session,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "hello",
                    requestId = "request-retained-cleanup-hello",
                    protocolVersion = 3,
                },
                events,
                frames).ConfigureAwait(false),
            "hello");
        string sessionId = "session-" + DirectBase64Url.Encode(
            Enumerable.Repeat((byte)0x73, 16).ToArray());
        _ = RequireSuccess(
            await SendAsync(
                session,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "start",
                    requestId = "request-retained-cleanup-start",
                    sessionId,
                },
                events,
                frames).ConfigureAwait(false),
            "start");

        JsonElement stop = await SendAsync(
            session,
            new
            {
                contract = DirectBridgeContract.Contract,
                type = "stop",
                requestId = "request-retained-cleanup-stop",
                sessionId,
            },
            events,
            frames).ConfigureAwait(false);
        JsonElement status = RequireSuccess(
            await SendAsync(
                session,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "status",
                    requestId = "request-retained-cleanup-status",
                },
                events,
                frames).ConfigureAwait(false),
            "status");
        Require(
            !stop.GetProperty("ok").GetBoolean()
            && stop.GetProperty("error").GetProperty("code")
                .GetString()
                == "BW_COMPUTER_VOICE_DIRECT_MEDIA_CLEANUP_PENDING"
            && coordinator.ActiveSessionId == sessionId
            && media.CleanupPending
            && !media.CaptureActive
            && session.Phase == DirectProtocolPhase.Active
            && status.GetProperty("state").GetString() == "faulted"
            && !status.GetProperty("ready").GetBoolean()
            && status.GetProperty("reason").GetString()
                == "BW_COMPUTER_VOICE_DIRECT_MEDIA_CLEANUP_PENDING",
            "direct-stop-retains-owner-and-reports-faulted-while-cleanup-pending",
            checks);

        await coordinator.StopForConnectionAsync(
            "connection-retained-cleanup").ConfigureAwait(false);
        bool peerCloseRetainedOwner =
            coordinator.ActiveSessionId == sessionId
            && media.CleanupPending;
        media.RetainCleanupOnStop = false;
        await coordinator.StopForConnectionAsync(
            "connection-retained-cleanup").ConfigureAwait(false);
        Require(
            peerCloseRetainedOwner
            && coordinator.ActiveSessionId is null
            && !media.CleanupPending,
            "direct-peer-close-retries-retained-cleanup-before-releasing-owner",
            checks);
    }

    private static async Task CheckPeerAbortDuringStopAsync(
        string configPath,
        ICollection<string> checks)
    {
        FakeDirectMediaAdapter media = new()
        {
            BlockStopUntilReleased = true,
        };
        await using DirectBridgeCoordinator coordinator = new(
            new DirectBridgeConfigStore(configPath),
            new FakeDirectAppLauncher(),
            media);
        const string connectionId = "connection-peer-abort-stop";
        string sessionId = "session-" + DirectBase64Url.Encode(
            Enumerable.Range(96, 16)
                .Select(value => (byte)value)
                .ToArray());
        _ = await coordinator.StartAsync(
            connectionId,
            sessionId,
            (_, _) => Task.CompletedTask,
            (_, _) => Task.CompletedTask,
            CancellationToken.None).ConfigureAwait(false);

        using CancellationTokenSource peerAbort = new();
        Task stopTask = coordinator.StopAsync(
            connectionId,
            sessionId,
            peerAbort.Token);
        bool stillCleaningAfterPeerAbort;
        try
        {
            await media.StopEntered.WaitAsync(TimeSpan.FromSeconds(2))
                .ConfigureAwait(false);
            peerAbort.Cancel();
            await Task.Yield();
            stillCleaningAfterPeerAbort = !stopTask.IsCompleted;
        }
        finally
        {
            media.ReleaseBlockedStop();
        }
        await stopTask.WaitAsync(TimeSpan.FromSeconds(2))
            .ConfigureAwait(false);
        Require(
            stillCleaningAfterPeerAbort
            && media.StopCancellationCanBeCanceled == false
            && media.StopCount == 1
            && !media.CaptureActive
            && coordinator.ActiveSessionId is null,
            "direct-peer-abort-during-stop-cannot-cancel-owned-teardown",
            checks);
    }

    private static async Task CheckOuterDisposeRetryAsync(
        string configPath,
        ICollection<string> checks)
    {
        FakeDirectMediaAdapter coordinatorMedia = new()
        {
            DisposeFailuresRemaining = 1,
        };
        DirectBridgeCoordinator coordinator = new(
            new DirectBridgeConfigStore(configPath),
            new FakeDirectAppLauncher(),
            coordinatorMedia);
        const string connectionId = "connection-dispose-retry";
        string sessionId = "session-" + DirectBase64Url.Encode(
            Enumerable.Range(112, 16)
                .Select(value => (byte)value)
                .ToArray());
        _ = await coordinator.StartAsync(
            connectionId,
            sessionId,
            (_, _) => Task.CompletedTask,
            (_, _) => Task.CompletedTask,
            CancellationToken.None).ConfigureAwait(false);
        bool firstCoordinatorDisposeFailed = false;
        try
        {
            await coordinator.DisposeAsync().ConfigureAwait(false);
        }
        catch (DirectProtocolException exception)
        {
            firstCoordinatorDisposeFailed = exception.Code
                == "BW_COMPUTER_VOICE_DIRECT_FAKE_DISPOSE_FAILED";
        }
        bool coordinatorOwnershipRetained =
            coordinatorMedia.CleanupOwnership;
        await coordinator.DisposeAsync().ConfigureAwait(false);
        await coordinator.DisposeAsync().ConfigureAwait(false);
        Require(
            firstCoordinatorDisposeFailed
            && coordinatorOwnershipRetained
            && !coordinatorMedia.CleanupOwnership
            && coordinatorMedia.DisposeCount == 2
            && coordinator.ActiveSessionId is null,
            "direct-coordinator-dispose-retries-retained-media-owner",
            checks);

        FakeDirectMediaAdapter serverMedia = new()
        {
            CleanupOwnership = true,
            DisposeFailuresRemaining = 1,
        };
        DirectBridgeServer server = new(
            new DirectBridgeConfigStore(configPath),
            new FakeDirectAppLauncher(),
            serverMedia);
        await server.DisposeAsync().ConfigureAwait(false);
        await server.DisposeAsync().ConfigureAwait(false);
        Require(
            !serverMedia.CleanupOwnership
            && serverMedia.DisposeCount == 2,
            "direct-single-outer-dispose-consumes-owner-retry-budget",
            checks);

        FakeDirectMediaAdapter exhaustedMedia = new()
        {
            CleanupOwnership = true,
            DisposeFailuresRemaining = 2,
        };
        DirectBridgeServer exhaustedServer = new(
            new DirectBridgeConfigStore(configPath),
            new FakeDirectAppLauncher(),
            exhaustedMedia);
        bool boundedFailureObserved = false;
        try
        {
            await exhaustedServer.DisposeAsync().ConfigureAwait(false);
        }
        catch (DirectProtocolException exception)
        {
            boundedFailureObserved =
                exception.Code
                    == "BW_COMPUTER_VOICE_DIRECT_FAKE_DISPOSE_FAILED"
                && exception.InnerException is AggregateException;
        }
        bool retainedAfterBudget =
            exhaustedMedia.CleanupOwnership
            && exhaustedMedia.DisposeCount == 2;
        await exhaustedServer.DisposeAsync().ConfigureAwait(false);
        await exhaustedServer.DisposeAsync().ConfigureAwait(false);
        Require(
            boundedFailureObserved
            && retainedAfterBudget
            && !exhaustedMedia.CleanupOwnership
            && exhaustedMedia.DisposeCount == 3,
            "direct-outer-dispose-budget-is-bounded-and-remains-retryable",
            checks);
    }

    private static async Task CheckTypistLeaseLifecycleAsync(
        ICollection<string> checks)
    {
        List<string[]> calls = [];
        WindowsDirectTypistLeaseController ownedController = new(
            (arguments, _) =>
            {
                calls.Add(arguments.ToArray());
                DirectTypistHelperResult result =
                    arguments[0] == "--ensure-running"
                        ? new DirectTypistHelperResult(
                            0,
                            """
                            {"ok":true,"running":true,"pid":4512,"processStartFileTimeUtc":133700000000000000,"result":"started"}
                            """,
                            "")
                        : new DirectTypistHelperResult(
                            0,
                            """
                            {"ok":true,"running":false,"stopped":true,"result":"stopped","expectedPid":4512}
                            """,
                            "");
                return Task.FromResult(result);
            },
            () => (7001, 133600000000000000));
        DirectTypistLease? owned =
            await ownedController.EnsureRunningAsync(
                CancellationToken.None).ConfigureAwait(false);
        Require(
            owned?.ProcessId == 4512
            && owned.ProcessStartFileTimeUtc
                == 133700000000000000
            && calls.Count == 1
            && calls[0].SequenceEqual(
                new[]
                {
                    "--ensure-running",
                    "7001",
                    "133600000000000000",
                    DirectAppTargets.CodexDesktop,
                }),
            "direct-typist-started-result-creates-owned-lease",
            checks);
        await ownedController.ReleaseAsync(
            owned!,
            CancellationToken.None).ConfigureAwait(false);
        Require(
            calls.Count == 2
            && calls[1].SequenceEqual(
                new[]
                {
                    "--stop-if-owned",
                    "4512",
                    "133700000000000000",
                }),
            "direct-typist-owned-lease-releases-with-exact-generation",
            checks);
        DirectProtocolException original = new(
            "BW_COMPUTER_VOICE_DIRECT_SHORTCUT_FAILED",
            "shortcut failed");
        DirectProtocolException combined =
            WindowsDirectMediaAdapter
                .CombineStartAndTypistReleaseFailures(
                    original,
                    new InvalidOperationException("stop failed"));
        Require(
            combined.Code == original.Code
            && combined.InnerException is AggregateException aggregate
            && aggregate.InnerExceptions.Count == 2
            && ReferenceEquals(
                aggregate.InnerExceptions[0],
                original),
            "direct-typist-start-cleanup-preserves-and-aggregates-failure",
            checks);

        WindowsDirectTypistLeaseController failingRelease = new(
            (arguments, _) => Task.FromResult(
                new DirectTypistHelperResult(
                    arguments[0] == "--stop-if-owned" ? 1 : 0,
                    "",
                    "")));
        bool releaseRejected = false;
        try
        {
            await failingRelease.ReleaseAsync(
                new DirectTypistLease(
                    4512,
                    133700000000000000),
                CancellationToken.None).ConfigureAwait(false);
        }
        catch (DirectProtocolException exception)
        {
            releaseRejected =
                exception.Code
                    == "BW_COMPUTER_VOICE_DIRECT_TYPIST_STOP_FAILED";
        }
        Require(
            releaseRejected,
            "direct-typist-release-failure-is-not-silent",
            checks);

        List<string[]> retryCalls = [];
        int releaseAttempts = 0;
        WindowsDirectTypistLeaseController retryController = new(
            (arguments, _) =>
            {
                retryCalls.Add(arguments.ToArray());
                releaseAttempts++;
                return Task.FromResult(
                    releaseAttempts == 1
                        ? new DirectTypistHelperResult(1, "", "")
                        : new DirectTypistHelperResult(
                            0,
                            """
                            {"ok":true,"running":false,"stopped":true,"result":"stopped","expectedPid":4512}
                            """,
                            ""));
            });
        WindowsDirectMediaAdapter retryAdapter = new(retryController);
        bool startFailurePreserved = false;
        try
        {
            await retryAdapter
                .ReleasePendingTypistAfterStartFailureAsync(
                    new DirectTypistLease(
                        4512,
                        133700000000000000),
                    original)
                .ConfigureAwait(false);
        }
        catch (DirectProtocolException exception)
        {
            startFailurePreserved =
                exception.Code == original.Code
                && exception.InnerException is AggregateException;
        }
        await retryAdapter.DisposeAsync().ConfigureAwait(false);
        await retryAdapter.DisposeAsync().ConfigureAwait(false);
        Require(
            startFailurePreserved
            && retryCalls.Count == 2
            && retryCalls.All(arguments =>
                arguments.SequenceEqual(
                    new[]
                    {
                        "--stop-if-owned",
                        "4512",
                        "133700000000000000",
                    })),
            "direct-start-failed-typist-release-retains-and-retries-exact-generation",
            checks);

        foreach (string result in new[]
        {
            "already-running",
            "raced-running",
        })
        {
            int invocationCount = 0;
            WindowsDirectTypistLeaseController reusedController = new(
                (arguments, _) =>
                {
                    invocationCount++;
                    return Task.FromResult(
                        new DirectTypistHelperResult(
                            0,
                            $$"""
                            {"ok":true,"running":true,"pid":9002,"processStartFileTimeUtc":133700000000000000,"result":"{{result}}"}
                            """,
                            ""));
                });
            DirectTypistLease? reused =
                await reusedController.EnsureRunningAsync(
                    CancellationToken.None).ConfigureAwait(false);
            Require(
                reused is null && invocationCount == 1,
                "direct-typist-" + result
                    + "-does-not-create-stop-lease",
                checks);
        }

        List<string> startOrder = [];
        TaskCompletionSource<DirectTypistLease?> typistReady = new(
            TaskCreationOptions.RunContinuationsAsynchronously);
        Task orderedStart =
            WindowsDirectMediaAdapter
                .EnsureTypistThenStartPreparedMediaAsync(
                    async cancellationToken =>
                    {
                        startOrder.Add("typist");
                        return await typistReady.Task
                            .WaitAsync(cancellationToken)
                            .ConfigureAwait(false);
                    },
                    lease =>
                    {
                        if (lease is not null)
                        {
                            startOrder.Add("typist-owned");
                        }
                    },
                    _ =>
                    {
                        startOrder.Add("render");
                        return Task.CompletedTask;
                    },
                    _ =>
                    {
                        startOrder.Add("output");
                        return Task.CompletedTask;
                    },
                    CancellationToken.None);
        bool mediaWaitedForTypist =
            startOrder.SequenceEqual(new[] { "typist" })
            && !orderedStart.IsCompleted;
        typistReady.SetResult(new DirectTypistLease(
            4512,
            133700000000000000));
        await orderedStart.ConfigureAwait(false);
        Require(
            mediaWaitedForTypist
            && startOrder.SequenceEqual(new[]
            {
                "typist",
                "typist-owned",
                "render",
                "output",
            }),
            "direct-typist-completes-before-prepared-media-start",
            checks);
    }

    private static async Task
        CheckAtomicShortcutAndBestEffortCleanupAsync(
            ICollection<string> checks)
    {
        using CancellationTokenSource peerClose = new();
        int shortcutCount = 0;
        int commitCount = 0;
        WindowsDirectMediaAdapter.SendShortcutAtAtomicCommitBoundary(
            () => WindowsDirectMediaAdapter.RequirePreparedMediaRunning(
                CaptureSessionState.Running,
                outputCompleted: false,
                CaptureSessionState.Running,
                renderCompleted: false),
            () =>
            {
                shortcutCount++;
                peerClose.Cancel();
                return true;
            },
            () => commitCount++,
            peerClose.Token);
        Require(
            peerClose.IsCancellationRequested
            && shortcutCount == 1
            && commitCount == 1,
            "direct-shortcut-peer-close-still-commits-cleanup-ownership",
            checks);

        using CancellationTokenSource canceledBeforeShortcut = new();
        canceledBeforeShortcut.Cancel();
        bool preCanceled = false;
        try
        {
            WindowsDirectMediaAdapter.SendShortcutAtAtomicCommitBoundary(
                () => WindowsDirectMediaAdapter.RequirePreparedMediaRunning(
                    CaptureSessionState.Running,
                    outputCompleted: false,
                    CaptureSessionState.Running,
                    renderCompleted: false),
                () =>
                {
                    shortcutCount++;
                    return true;
                },
                () => commitCount++,
                canceledBeforeShortcut.Token);
        }
        catch (OperationCanceledException)
        {
            preCanceled = true;
        }
        Require(
            preCanceled
            && shortcutCount == 1
            && commitCount == 1,
            "direct-shortcut-pre-cancel-has-no-side-effect",
            checks);

        using CancellationTokenSource canceledDuringValidation = new();
        bool validationCanceled = false;
        try
        {
            WindowsDirectMediaAdapter.SendShortcutAtAtomicCommitBoundary(
                () => canceledDuringValidation.Cancel(),
                () =>
                {
                    shortcutCount++;
                    return true;
                },
                () => commitCount++,
                canceledDuringValidation.Token);
        }
        catch (OperationCanceledException)
        {
            validationCanceled = true;
        }
        Require(
            validationCanceled
            && shortcutCount == 1
            && commitCount == 1,
            "direct-shortcut-validation-race-has-no-side-effect",
            checks);

        int invalidEndpointShortcutCount = 0;
        bool invalidPreparedMediaRejected = false;
        try
        {
            WindowsDirectMediaAdapter.SendShortcutAtAtomicCommitBoundary(
                () => WindowsDirectMediaAdapter
                    .RequirePreparedMediaRunning(
                        CaptureSessionState.Running,
                        outputCompleted: false,
                        CaptureSessionState.Faulted,
                        renderCompleted: true),
                () =>
                {
                    invalidEndpointShortcutCount++;
                    return true;
                },
                () => commitCount++,
                CancellationToken.None);
        }
        catch (DirectProtocolException exception)
        {
            invalidPreparedMediaRejected = exception.Code
                == "BW_COMPUTER_VOICE_DIRECT_MEDIA_START_UNCONFIRMED";
        }
        Require(
            invalidPreparedMediaRejected
            && invalidEndpointShortcutCount == 0
            && commitCount == 1,
            "direct-shortcut-rechecks-prepared-audio-before-side-effect",
            checks);

        Require(
            WindowsDirectMediaAdapter.CanRestorePerAppAudioRoute(
                voiceSettled: true,
                captureLifetimeReleased: true,
                outputSessionReleased: true,
                renderSessionReleased: true,
                outputRouteObserverReleased: true,
                outputPumpReleased: true,
                renderMonitorReleased: true)
            && !WindowsDirectMediaAdapter.CanRestorePerAppAudioRoute(
                voiceSettled: false,
                captureLifetimeReleased: true,
                outputSessionReleased: true,
                renderSessionReleased: true,
                outputRouteObserverReleased: true,
                outputPumpReleased: true,
                renderMonitorReleased: true)
            && !WindowsDirectMediaAdapter.CanRestorePerAppAudioRoute(
                voiceSettled: true,
                captureLifetimeReleased: true,
                outputSessionReleased: false,
                renderSessionReleased: true,
                outputRouteObserverReleased: true,
                outputPumpReleased: true,
                renderMonitorReleased: true)
            && !WindowsDirectMediaAdapter.CanRestorePerAppAudioRoute(
                voiceSettled: true,
                captureLifetimeReleased: true,
                outputSessionReleased: true,
                renderSessionReleased: true,
                outputRouteObserverReleased: true,
                outputPumpReleased: false,
                renderMonitorReleased: true),
            "direct-route-restore-waits-for-voice-and-media-settlement",
            checks);

        object oldGeneration = new();
        object newGeneration = new();
        int staleCleanupCount = 0;
        await WindowsDirectMediaAdapter.StopIfCurrentGenerationAsync(
            newGeneration,
            oldGeneration,
            () =>
            {
                staleCleanupCount++;
                return Task.CompletedTask;
            }).ConfigureAwait(false);
        int currentCleanupCount = 0;
        await WindowsDirectMediaAdapter.StopIfCurrentGenerationAsync(
            oldGeneration,
            oldGeneration,
            () =>
            {
                currentCleanupCount++;
                return Task.CompletedTask;
            }).ConfigureAwait(false);
        Require(
            staleCleanupCount == 0 && currentCleanupCount == 1,
            "direct-stale-media-fault-cannot-stop-new-generation",
            checks);

        List<int> cleanupOrder = [];
        bool completionSettled = false;
        Exception? cleanupFailure =
            await WindowsDirectMediaAdapter.RunBestEffortCleanupAsync(
                () =>
                {
                    cleanupOrder.Add(1);
                    throw new InvalidOperationException("mic stop failed");
                },
                () =>
                {
                    cleanupOrder.Add(2);
                    return Task.CompletedTask;
                },
                () =>
                {
                    cleanupOrder.Add(3);
                    return Task.CompletedTask;
                },
                () =>
                {
                    cleanupOrder.Add(4);
                    throw new InvalidOperationException(
                        "output dispose failed");
                },
                () =>
                {
                    cleanupOrder.Add(5);
                    return Task.CompletedTask;
                },
                () =>
                {
                    cleanupOrder.Add(6);
                    return Task.CompletedTask;
                },
                () =>
                {
                    cleanupOrder.Add(7);
                    return Task.CompletedTask;
                }).ConfigureAwait(false);
        completionSettled = true;
        Require(
            cleanupOrder.SequenceEqual(
                new[] { 1, 2, 3, 4, 5, 6, 7 })
            && cleanupFailure is AggregateException aggregate
            && aggregate.InnerExceptions.Count == 2
            && completionSettled,
            "direct-stop-best-effort-runs-all-cleanup-and-aggregates",
            checks);

        bool leaseOwned = true;
        bool firstReleaseFailed = false;
        try
        {
            await WindowsDirectMediaAdapter
                .ReleaseOwnershipAfterSuccessAsync(
                    () => Task.FromException(
                        new InvalidOperationException(
                            "transient typist stop failure")),
                    () => leaseOwned = false).ConfigureAwait(false);
        }
        catch (InvalidOperationException)
        {
            firstReleaseFailed = true;
        }
        bool retainedAfterFailure = leaseOwned;
        await WindowsDirectMediaAdapter
            .ReleaseOwnershipAfterSuccessAsync(
                () => Task.CompletedTask,
                () => leaseOwned = false).ConfigureAwait(false);
        Require(
            firstReleaseFailed
            && retainedAfterFailure
            && !leaseOwned,
            "direct-typist-ownership-clears-only-after-successful-retry",
            checks);

        VoiceShortcutInteropLayout shortcutInteropLayout =
            WindowsCodexAppProbe.GetVoiceShortcutInteropLayout();
        bool shortcutInteropLayoutMatchesWin32 =
            shortcutInteropLayout.PointerSize switch
            {
                8 =>
                    shortcutInteropLayout.KeyboardSize == 24
                    && shortcutInteropLayout.MouseSize == 32
                    && shortcutInteropLayout.HardwareSize == 8
                    && shortcutInteropLayout.UnionSize == 32
                    && shortcutInteropLayout.InputSize == 40
                    && shortcutInteropLayout.UnionOffset == 8,
                4 =>
                    shortcutInteropLayout.KeyboardSize == 16
                    && shortcutInteropLayout.MouseSize == 24
                    && shortcutInteropLayout.HardwareSize == 8
                    && shortcutInteropLayout.UnionSize == 24
                    && shortcutInteropLayout.InputSize == 28
                    && shortcutInteropLayout.UnionOffset == 4,
                _ => false,
            };
        Require(
            shortcutInteropLayoutMatchesWin32,
            "direct-sendinput-abi-layout-matches-win32-input",
            checks);

        VoiceShortcutKeyEvent[] activationEvents =
            WindowsCodexAppProbe.VoiceShortcutEvents(
                VoiceShortcutInputBatch.Activation);
        VoiceShortcutKeyEvent[] releaseEvents =
            WindowsCodexAppProbe.VoiceShortcutEvents(
                VoiceShortcutInputBatch.ReleasePressedKeys);
        bool everyPartialReleased = true;
        for (uint inserted = 0; inserted <= 2; inserted++)
        {
            List<VoiceShortcutInputBatch> batches = [];
            bool accepted =
                WindowsCodexAppProbe.SendVoiceShortcutInputSequence(
                    batch =>
                    {
                        batches.Add(batch);
                        return batch
                            == VoiceShortcutInputBatch.Activation
                            ? inserted
                            : 1;
                    });
            bool expectedCleanup = inserted == 1;
            everyPartialReleased &= accepted == (inserted == 2)
                && batches.SequenceEqual(
                    expectedCleanup
                        ? new[]
                        {
                            VoiceShortcutInputBatch.Activation,
                            VoiceShortcutInputBatch.ReleasePressedKeys,
                        }
                        : new[]
                        {
                            VoiceShortcutInputBatch.Activation,
                        });
        }
        bool cleanupThrowRemainsFailed =
            !WindowsCodexAppProbe.SendVoiceShortcutInputSequence(
                batch => batch
                    == VoiceShortcutInputBatch.Activation
                    ? 1u
                    : throw new InvalidOperationException(
                        "release input failed"));
        Require(
            activationEvents.SequenceEqual(
                new[]
                {
                    new VoiceShortcutKeyEvent(0x87, KeyUp: false),
                    new VoiceShortcutKeyEvent(0x87, KeyUp: true),
                })
            && releaseEvents.SequenceEqual(
                new[]
                {
                    new VoiceShortcutKeyEvent(0x87, KeyUp: true),
                })
            && everyPartialReleased
            && cleanupThrowRemainsFailed,
            "direct-shortcut-partial-send-releases-keys-best-effort",
            checks);

        CodexAppTarget shortcutExpected = new(
            RootProcessId: 7001,
            RootProcessStartFileTimeUtc: 133700000000000000,
            ProcessTree: new HashSet<uint> { 7001, 7002 },
            WindowHandle: (nint)71);
        CodexAppTarget shortcutCurrentWithChildChurn = new(
            RootProcessId: 7001,
            RootProcessStartFileTimeUtc: 133700000000000000,
            ProcessTree: new HashSet<uint> { 7001, 7003, 7004 },
            WindowHandle: (nint)72);
        int audioPolicyProbeCount = 0;
        int audioPolicyCandidateCount = 0;
        CodexAudioPolicyTarget convergedAudioPolicy =
            await WindowsCodexAppProbe.WaitForAudioPolicyProcessAsync(
                shortcutExpected,
                TimeSpan.FromSeconds(1),
                CancellationToken.None,
                probe: () =>
                {
                    audioPolicyProbeCount++;
                    return new CodexAppProbeState(
                        RootCount: 1,
                        WindowCount: 1,
                        ReadyTarget: shortcutCurrentWithChildChurn);
                },
                candidates: _ =>
                {
                    audioPolicyCandidateCount++;
                    return audioPolicyCandidateCount == 1
                        ? new uint[] { 7003, 7004 }
                        : new uint[] { 7004 };
                },
                delay: (_, cancellationToken) =>
                {
                    cancellationToken.ThrowIfCancellationRequested();
                    return Task.CompletedTask;
                });
        Require(
            convergedAudioPolicy.ProcessId == 7004
            && ReferenceEquals(
                convergedAudioPolicy.AppTarget,
                shortcutCurrentWithChildChurn)
            && audioPolicyProbeCount == 3
            && audioPolicyCandidateCount == 3,
            "direct-audio-service-transient-ambiguity-converges",
            checks);
        List<VoiceShortcutInputBatch> globalBatches = [];
        VoiceShortcutSendResult globalShortcut =
            WindowsCodexAppProbe.SendValidatedGlobalVoiceShortcut(
                shortcutExpected,
                shortcutCurrentWithChildChurn,
                shortcutConfigured: true,
                batch =>
                {
                    globalBatches.Add(batch);
                    return batch == VoiceShortcutInputBatch.Activation
                        ? 2u
                        : 1u;
                },
                () => 0);
        VoiceShortcutSendResult changedRoot =
            WindowsCodexAppProbe.SendValidatedGlobalVoiceShortcut(
                shortcutExpected,
                shortcutCurrentWithChildChurn with
                {
                    RootProcessId = 8001,
                },
                shortcutConfigured: true,
                _ => throw new InvalidOperationException(
                    "changed target must not receive input"),
                () => 0);
        VoiceShortcutSendResult changedGeneration =
            WindowsCodexAppProbe.SendValidatedGlobalVoiceShortcut(
                shortcutExpected,
                shortcutCurrentWithChildChurn with
                {
                    RootProcessStartFileTimeUtc =
                        133700000000000001,
                },
                shortcutConfigured: true,
                _ => throw new InvalidOperationException(
                    "changed generation must not receive input"),
                () => 0);
        VoiceShortcutSendResult invalidBinding =
            WindowsCodexAppProbe.SendValidatedGlobalVoiceShortcut(
                shortcutExpected,
                shortcutCurrentWithChildChurn,
                shortcutConfigured: false,
                _ => throw new InvalidOperationException(
                    "invalid binding must not receive input"),
                () => 0);
        List<VoiceShortcutInputBatch> partialBatches = [];
        VoiceShortcutSendResult partialGlobalShortcut =
            WindowsCodexAppProbe.SendValidatedGlobalVoiceShortcut(
                shortcutExpected,
                shortcutCurrentWithChildChurn,
                shortcutConfigured: true,
                batch =>
                {
                    partialBatches.Add(batch);
                    return batch == VoiceShortcutInputBatch.Activation
                        ? 1u
                        : 1u;
                },
                () => 5);
        bool validGlobalShortcutConfig =
            WindowsCodexAppProbe.IsExpectedGlobalVoiceShortcutConfig(
                """
                [
                  {"command":"composer.submit","key":"Ctrl+Shift+L"},
                  {"command":"realtimeVoice","key":"F24"}
                ]
                """);
        bool rejectsDuplicateCommand =
            !WindowsCodexAppProbe.IsExpectedGlobalVoiceShortcutConfig(
                """
                [
                  {"command":"realtimeVoice","key":"F24"},
                  {"command":"realtimeVoice","key":"Ctrl+Shift+V"}
                ]
                """);
        bool rejectsShortcutCollision =
            !WindowsCodexAppProbe.IsExpectedGlobalVoiceShortcutConfig(
                """
                [
                  {"command":"realtimeVoice","key":"F24"},
                  {"command":"other","key":"F24"}
                ]
                """);
        bool rejectsMalformedBinding =
            !WindowsCodexAppProbe.IsExpectedGlobalVoiceShortcutConfig(
                """
                [
                  {"command":"realtimeVoice"},
                  {"command":"other","key":"F24"}
                ]
                """);
        Require(
            globalShortcut.Sent
            && globalShortcut.InsertedInputCount == 2
            && globalBatches.SequenceEqual(
                new[] { VoiceShortcutInputBatch.Activation })
            && changedRoot.FailureCode
                == "BW_COMPUTER_VOICE_DIRECT_SHORTCUT_TARGET_CHANGED"
            && changedGeneration.FailureCode
                == "BW_COMPUTER_VOICE_DIRECT_SHORTCUT_TARGET_CHANGED"
            && invalidBinding.FailureCode
                == "BW_COMPUTER_VOICE_DIRECT_SHORTCUT_CONFIG_INVALID"
            && partialGlobalShortcut.FailureCode
                == "BW_COMPUTER_VOICE_DIRECT_SHORTCUT_INPUT_FAILED"
            && partialGlobalShortcut.InsertedInputCount == 1
            && partialGlobalShortcut.Win32Error == 5
            && partialBatches.SequenceEqual(new[]
            {
                VoiceShortcutInputBatch.Activation,
                VoiceShortcutInputBatch.ReleasePressedKeys,
            })
            && validGlobalShortcutConfig
            && rejectsDuplicateCommand
            && rejectsShortcutCollision
            && rejectsMalformedBinding,
            "direct-os-global-shortcut-ignores-foreground-and-child-churn",
            checks);
    }

    private static async Task CheckHeartbeatDeadlineAsync(
        string configPath,
        ICollection<string> checks)
    {
        long monotonicMilliseconds = 1_000;
        FakeDirectAppLauncher launcher = new();
        FakeDirectMediaAdapter media = new();
        await using DirectBridgeCoordinator coordinator = new(
            new DirectBridgeConfigStore(configPath),
            launcher,
            media,
            () => monotonicMilliseconds);
        const string connectionId = "connection-heartbeat-timeout";
        string sessionId = "session-" + DirectBase64Url.Encode(
            Enumerable.Repeat((byte)6, 16).ToArray());
        _ = await coordinator.StartAsync(
            connectionId,
            sessionId,
            (_, _) => Task.CompletedTask,
            (_, _) => Task.CompletedTask,
            CancellationToken.None).ConfigureAwait(false);
        monotonicMilliseconds = 5_000;
        await coordinator.RenewHeartbeatAsync(
            connectionId,
            sessionId,
            sequence: 1,
            CancellationToken.None).ConfigureAwait(false);

        bool skippedSequenceRejected = false;
        monotonicMilliseconds = 6_000;
        try
        {
            await coordinator.RenewHeartbeatAsync(
                connectionId,
                sessionId,
                sequence: 3,
                CancellationToken.None).ConfigureAwait(false);
        }
        catch (DirectProtocolException exception)
        {
            skippedSequenceRejected = exception.Code
                == "BW_COMPUTER_VOICE_DIRECT_HEARTBEAT_SEQUENCE_INVALID";
        }

        monotonicMilliseconds = 19_999;
        bool aliveBeforeDeadline =
            !coordinator.IsHeartbeatExpired(connectionId)
            && coordinator.GetHeartbeatRemainingMilliseconds(
                connectionId) == 1;
        monotonicMilliseconds = 20_000;
        bool expiredAtDeadline =
            coordinator.IsHeartbeatExpired(connectionId);
        await coordinator.StopForConnectionAsync(connectionId)
            .ConfigureAwait(false);
        Require(
            skippedSequenceRejected
            && aliveBeforeDeadline
            && expiredAtDeadline
            && !media.CaptureActive
            && media.StopCount == 1
            && coordinator.GetHeartbeatRemainingMilliseconds(
                connectionId) is null,
            "direct-heartbeat-fake-clock-timeout-stops-capture",
            checks);
    }

    private static void CheckConnectionPhaseDeadlines(
        ICollection<string> checks)
    {
        long monotonicMilliseconds = 0;
        DirectConnectionPhaseDeadline noHello = new(
            () => monotonicMilliseconds);
        monotonicMilliseconds = 10_000;
        Require(
            noHello.IsExpired(
                DirectProtocolPhase.AwaitingAuthentication)
            && DirectConnectionPhaseDeadline.TimeoutFailure(
                DirectProtocolPhase.AwaitingAuthentication).Code
                == "BW_COMPUTER_VOICE_DIRECT_AUTH_TIMEOUT",
            "direct-no-hello-releases-slot-after-ten-seconds",
            checks);

        monotonicMilliseconds = 0;
        DirectConnectionPhaseDeadline helloOnly = new(
            () => monotonicMilliseconds);
        helloOnly.Observe(
            DirectProtocolPhase.AwaitingAuthentication);
        monotonicMilliseconds = 10_000;
        Require(
            helloOnly.IsExpired(
                DirectProtocolPhase.AwaitingAuthentication),
            "direct-hello-only-does-not-renew-auth-deadline",
            checks);

        monotonicMilliseconds = 0;
        DirectConnectionPhaseDeadline authenticated = new(
            () => monotonicMilliseconds);
        monotonicMilliseconds = 1_000;
        authenticated.Observe(DirectProtocolPhase.AwaitingStart);
        monotonicMilliseconds = 5_000;
        authenticated.Observe(DirectProtocolPhase.AwaitingStart);
        bool statusDidNotRenew =
            authenticated.GetRemainingMilliseconds(
                DirectProtocolPhase.AwaitingStart) == 26_000;
        monotonicMilliseconds = 31_000;
        Require(
            statusDidNotRenew
            && authenticated.IsExpired(
                DirectProtocolPhase.AwaitingStart)
            && DirectConnectionPhaseDeadline.TimeoutFailure(
                DirectProtocolPhase.AwaitingStart).Code
                == "BW_COMPUTER_VOICE_DIRECT_START_TIMEOUT",
            "direct-authenticated-status-does-not-renew-start-deadline",
            checks);

        monotonicMilliseconds = 0;
        DirectConnectionPhaseDeadline normalStart = new(
            () => monotonicMilliseconds);
        monotonicMilliseconds = 1_000;
        normalStart.Observe(DirectProtocolPhase.AwaitingStart);
        monotonicMilliseconds = 20_000;
        normalStart.Observe(DirectProtocolPhase.Active);
        monotonicMilliseconds = 100_000;
        Require(
            normalStart.GetRemainingMilliseconds(
                DirectProtocolPhase.Active) is null
            && !normalStart.IsExpired(DirectProtocolPhase.Active),
            "direct-normal-start-switches-to-heartbeat-only-deadline",
            checks);
    }

    private static async Task CheckLocalOptInGateAsync(
        string root,
        string origin,
        ICollection<string> checks)
    {
        string optoutRoot = System.IO.Path.Combine(root, "optout");
        string configPath = System.IO.Path.Combine(
            optoutRoot,
            "native-host",
            "direct.json");
        await WriteConfigAsync(
            configPath,
            System.IO.Path.Combine(
                optoutRoot,
                "runtime",
                "computer-voice-direct.status.json"),
            origin,
            localOptIn: false).ConfigureAwait(false);
        DirectBridgeConfigStore store = new(configPath);
        FakeDirectAppLauncher launcher = new();
        FakeDirectMediaAdapter media = new();
        await using DirectBridgeCoordinator coordinator = new(
            store,
            launcher,
            media);
        string sessionId = "session-" + DirectBase64Url.Encode(
            new byte[16]);
        bool denied = false;
        try
        {
            _ = await coordinator.StartAsync(
                "connection-optout",
                sessionId,
                (_, _) => Task.CompletedTask,
                (_, _) => Task.CompletedTask,
                CancellationToken.None).ConfigureAwait(false);
        }
        catch (DirectProtocolException exception)
        {
            denied = exception.Code
                == "BW_COMPUTER_VOICE_DIRECT_LOCAL_OPT_IN_REQUIRED";
        }
        Require(
            denied
            && launcher.EnsureRunningCount == 0
            && media.StartCount == 0,
            "direct-local-opt-in-precedes-all-side-effects",
            checks);
    }

    private static async Task CheckAppTimeoutAsync(
        string configPath,
        ICollection<string> checks)
    {
        DirectBridgeConfigStore store = new(configPath);
        FakeDirectAppLauncher launcher = new()
        {
            ThrowReadyTimeout = true,
        };
        FakeDirectMediaAdapter media = new();
        await using DirectBridgeCoordinator coordinator = new(
            store,
            launcher,
            media);
        bool timedOut = false;
        try
        {
            _ = await coordinator.StartAsync(
                "connection-timeout",
                "session-" + DirectBase64Url.Encode(
                    Enumerable.Repeat((byte)7, 16).ToArray()),
                (_, _) => Task.CompletedTask,
                (_, _) => Task.CompletedTask,
                CancellationToken.None).ConfigureAwait(false);
        }
        catch (DirectProtocolException exception)
        {
            timedOut = exception.Code
                == "BW_COMPUTER_VOICE_DIRECT_APP_READY_TIMEOUT";
        }
        Require(
            timedOut
            && launcher.EnsureRunningCount == 1
            && launcher.WaitReadyCount == 1
            && media.StartCount == 0,
            "direct-app-ready-timeout-is-explicit-and-no-capture",
            checks);
    }

    private static async Task CheckDisconnectCancellationAsync(
        string configPath,
        ICollection<string> checks)
    {
        DirectBridgeConfigStore store = new(configPath);
        FakeDirectAppLauncher launcher = new()
        {
            WaitUntilCanceled = true,
        };
        FakeDirectMediaAdapter media = new();
        await using DirectBridgeCoordinator coordinator = new(
            store,
            launcher,
            media);
        using CancellationTokenSource disconnected = new();
        Task<DirectMediaStartResult> start = coordinator.StartAsync(
            "connection-disconnect",
            "session-" + DirectBase64Url.Encode(
                Enumerable.Repeat((byte)8, 16).ToArray()),
            (_, _) => Task.CompletedTask,
            (_, _) => Task.CompletedTask,
            disconnected.Token);
        await launcher.WaitEntered.Task.WaitAsync(
            TimeSpan.FromSeconds(2)).ConfigureAwait(false);
        disconnected.Cancel();
        bool canceled = false;
        try
        {
            _ = await start.ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            canceled = true;
        }
        Require(
            canceled
            && launcher.CancellationObserved
            && media.StartCount == 0,
            "direct-disconnect-cancels-app-wait-before-capture",
            checks);
    }

    private static async Task CheckConnectionTakeoverOwnershipAsync(
        ICollection<string> checks)
    {
        await using DirectConnectionOwnership ownership = new();
        DirectConnectionClaim first = await ownership.ClaimAsync(
            "connection-takeover-first",
            CancellationToken.None,
            CancellationToken.None).ConfigureAwait(false);
        using FakeDirectWebSocket firstSocket = new();
        bool firstAttached = await ownership.TryAttachAsync(
            first.Current,
            firstSocket,
            CancellationToken.None).ConfigureAwait(false);

        DirectConnectionClaim second = await ownership.ClaimAsync(
            "connection-takeover-second",
            CancellationToken.None,
            CancellationToken.None).ConfigureAwait(false);
        using FakeDirectWebSocket secondSocket = new();
        bool secondAttached = await ownership.TryAttachAsync(
            second.Current,
            secondSocket,
            CancellationToken.None).ConfigureAwait(false);
        int staleCompletionWrites = 0;
        await ownership.CompleteAsync(
            first.Current,
            () =>
            {
                staleCompletionWrites++;
                return Task.CompletedTask;
            }).ConfigureAwait(false);
        bool secondRemainsCurrent = await ownership.IsCurrentAsync(
            second.Current,
            CancellationToken.None).ConfigureAwait(false);

        Task<DirectConnectionClaim> thirdTask = ownership.ClaimAsync(
            "connection-takeover-third",
            CancellationToken.None,
            CancellationToken.None);
        Task<DirectConnectionClaim> fourthTask = ownership.ClaimAsync(
            "connection-takeover-fourth",
            CancellationToken.None,
            CancellationToken.None);
        DirectConnectionClaim[] concurrent =
            await Task.WhenAll(thirdTask, fourthTask).ConfigureAwait(false);
        DirectConnectionClaim winner = concurrent.MaxBy(
            claim => claim.Current.Generation)!;
        DirectConnectionClaim loser = concurrent.MinBy(
            claim => claim.Current.Generation)!;
        using FakeDirectWebSocket loserSocket = new();
        using FakeDirectWebSocket winnerSocket = new();
        bool loserAttached = await ownership.TryAttachAsync(
            loser.Current,
            loserSocket,
            CancellationToken.None).ConfigureAwait(false);
        bool winnerAttached = await ownership.TryAttachAsync(
            winner.Current,
            winnerSocket,
            CancellationToken.None).ConfigureAwait(false);
        int loserCompletionWrites = 0;
        int winnerCompletionWrites = 0;
        await ownership.CompleteAsync(
            loser.Current,
            () =>
            {
                loserCompletionWrites++;
                return Task.CompletedTask;
            }).ConfigureAwait(false);
        await ownership.CompleteAsync(
            winner.Current,
            () =>
            {
                winnerCompletionWrites++;
                return Task.CompletedTask;
            }).ConfigureAwait(false);

        Require(
            firstAttached
            && secondAttached
            && ReferenceEquals(second.Previous, first.Current)
            && first.Current.Token.IsCancellationRequested
            && firstSocket.State == WebSocketState.Aborted
            && first.Current.Released.IsCompleted
            && staleCompletionWrites == 0
            && secondRemainsCurrent
            && second.Current.Token.IsCancellationRequested
            && secondSocket.State == WebSocketState.Aborted
            && winner.Current.Generation
                > loser.Current.Generation
            && loser.Current.Token.IsCancellationRequested
            && !loserAttached
            && winnerAttached
            && loserCompletionWrites == 0
            && winnerCompletionWrites == 1
            && loser.Current.Released.IsCompleted
            && winner.Current.Released.IsCompleted
            && winnerSocket.State == WebSocketState.Aborted,
            "direct-authenticated-connection-takeover-is-newest-wins",
            checks);
    }

    private static async Task CheckServerCloseCancelsBlockedStartAsync(
        string configPath,
        ICollection<string> checks)
    {
        DirectBridgeConfigStore store = new(configPath);
        FakeDirectAppLauncher launcher = new()
        {
            WaitUntilCanceled = true,
        };
        FakeDirectMediaAdapter media = new();
        await using DirectBridgeCoordinator coordinator = new(
            store,
            launcher,
            media);
        using FakeDirectWebSocket socket = new();
        const string connectionId = "connection-server-close";
        string sessionId = "session-" + DirectBase64Url.Encode(
            Enumerable.Repeat((byte)9, 16).ToArray());
        Task<DirectPeerMonitorOutcome<DirectMediaStartResult>> monitored =
            DirectBridgeServer.MonitorStartForPeerCloseAsync(
                socket,
                token => coordinator.StartAsync(
                    connectionId,
                    sessionId,
                    (_, _) => Task.CompletedTask,
                    (_, _) => Task.CompletedTask,
                    token),
                CancellationToken.None);

        await launcher.WaitEntered.Task.WaitAsync(
            TimeSpan.FromSeconds(2)).ConfigureAwait(false);
        await socket.ReceiveEntered.Task.WaitAsync(
            TimeSpan.FromSeconds(2)).ConfigureAwait(false);
        socket.QueueClose();
        DirectPeerMonitorOutcome<DirectMediaStartResult> outcome =
            await monitored.WaitAsync(
                TimeSpan.FromSeconds(2)).ConfigureAwait(false);
        await coordinator.StopForConnectionAsync(connectionId)
            .WaitAsync(TimeSpan.FromSeconds(2)).ConfigureAwait(false);

        Require(
            outcome.PeerClosed
            && outcome.Result is null
            && outcome.PrefetchedReceiveTask is null
            && launcher.CancellationObserved
            && media.StartCount == 0
            && !media.CaptureActive
            && coordinator.ActiveSessionId is null
            && socket.ReceiveCallCount == 1
            && socket.MaximumConcurrentReceives == 1,
            "direct-server-close-cancels-blocked-start-before-media",
            checks);
    }

    private static async Task CheckServerPrefetchPreservesOrderAsync(
        ICollection<string> checks)
    {
        using FakeDirectWebSocket socket = new();
        TaskCompletionSource<DirectMediaStartResult> release = new(
            TaskCreationOptions.RunContinuationsAsynchronously);
        Task<DirectPeerMonitorOutcome<DirectMediaStartResult>> monitored =
            DirectBridgeServer.MonitorStartForPeerCloseAsync(
                socket,
                _ => release.Task,
                CancellationToken.None);
        await socket.ReceiveEntered.Task.WaitAsync(
            TimeSpan.FromSeconds(2)).ConfigureAwait(false);
        const string nextMessage = "{\"type\":\"status\"}";
        socket.QueueText(nextMessage);
        await Task.Yield();
        bool waitedForStart = !monitored.IsCompleted;
        release.TrySetResult(
            new DirectMediaStartResult(
                HostReady: true,
                CaptureActive: true));
        DirectPeerMonitorOutcome<DirectMediaStartResult> outcome =
            await monitored.WaitAsync(
                TimeSpan.FromSeconds(2)).ConfigureAwait(false);
        DirectClientMessage? prefetchedMessage =
            outcome.PrefetchedReceiveTask is null
            ? null
            : await outcome.PrefetchedReceiveTask.ConfigureAwait(false);
        string? prefetched = prefetchedMessage?.Text;
        Require(
            waitedForStart
            && !outcome.PeerClosed
            && outcome.Result is not null
            && prefetched == nextMessage
            && socket.ReceiveCallCount == 1
            && socket.MaximumConcurrentReceives == 1,
            "direct-server-start-prefetch-is-single-bounded-and-ordered",
            checks);
    }

    private static async Task CheckEarlyBinaryCancelsStartAsync(
        ICollection<string> checks)
    {
        using FakeDirectWebSocket socket = new();
        TaskCompletionSource<bool> cancellationObserved = new(
            TaskCreationOptions.RunContinuationsAsynchronously);
        async Task<DirectMediaStartResult> BlockedStart(
            CancellationToken cancellationToken)
        {
            try
            {
                await Task.Delay(
                    Timeout.InfiniteTimeSpan,
                    cancellationToken).ConfigureAwait(false);
                throw new InvalidOperationException(
                    "blocked start unexpectedly completed");
            }
            catch (OperationCanceledException)
                when (cancellationToken.IsCancellationRequested)
            {
                cancellationObserved.TrySetResult(true);
                throw;
            }
        }

        Task<DirectPeerMonitorOutcome<DirectMediaStartResult>> monitored =
            DirectBridgeServer.MonitorStartForPeerCloseAsync(
                socket,
                BlockedStart,
                CancellationToken.None);
        await socket.ReceiveEntered.Task.WaitAsync(
            TimeSpan.FromSeconds(2)).ConfigureAwait(false);
        socket.QueueBinary(
            new byte[DirectBridgeContract.PcmFrameBytes]);
        DirectProtocolException? observed = null;
        try
        {
            _ = await monitored.WaitAsync(
                TimeSpan.FromSeconds(2)).ConfigureAwait(false);
        }
        catch (DirectProtocolException exception)
        {
            observed = exception;
        }
        Require(
            observed?.Code
                == "BW_COMPUTER_VOICE_DIRECT_UPLINK_NOT_ACTIVE"
            && await cancellationObserved.Task.WaitAsync(
                TimeSpan.FromSeconds(2)).ConfigureAwait(false)
            && socket.ReceiveCallCount == 1
            && socket.MaximumConcurrentReceives == 1,
            "direct-binary-before-start-cancels-start-and-fails-closed",
            checks);
    }

    private static void CheckPcmSequenceGate(
        ICollection<string> checks)
    {
        string sessionId = "session-" + DirectBase64Url.Encode(
            Enumerable.Repeat((byte)4, 16).ToArray());
        DirectPcmSequenceGuard concurrentGuard = new();
        Task output = Task.Run(() =>
        {
            for (uint sequence = 0; sequence < 100; sequence++)
            {
                concurrentGuard.Validate(
                    sessionId,
                    new DirectPcmFrame(
                        DirectPcmTrack.AppOutput,
                        sequence,
                        (sequence + 1) * 20_000UL,
                        new byte[Pcm48kMonoFramer.BytesPerChunk]));
            }
        });
        Task microphone = Task.Run(() =>
        {
            for (uint sequence = 0; sequence < 100; sequence++)
            {
                concurrentGuard.Validate(
                    sessionId,
                    new DirectPcmFrame(
                        DirectPcmTrack.BrowserMicrophone,
                        sequence,
                        (sequence + 1) * 20_000UL,
                        new byte[Pcm48kMonoFramer.BytesPerChunk]));
            }
        });
        Task.WhenAll(output, microphone).GetAwaiter().GetResult();
        Require(
            output.IsCompletedSuccessfully
            && microphone.IsCompletedSuccessfully,
            "direct-pcm-dual-track-sequence-guard-is-thread-safe",
            checks);

        DirectPcmSequenceGuard guard = new();
        DirectPcmFrame first = new(
            DirectPcmTrack.BrowserMicrophone,
            0,
            10,
            new byte[Pcm48kMonoFramer.BytesPerChunk]);
        guard.Validate(sessionId, first);
        guard.Validate(sessionId, first with
        {
            Sequence = 1,
            TimestampMicroseconds = 20,
        });
        bool rejected = false;
        try
        {
            guard.Validate(sessionId, first with
            {
                Sequence = 3,
                TimestampMicroseconds = 30,
            });
        }
        catch (DirectProtocolException exception)
        {
            rejected = exception.Code
                == "BW_COMPUTER_VOICE_DIRECT_PCM_SEQUENCE_INVALID";
        }
        Require(
            rejected,
            "direct-pcm-sequence-gap-fails-closed",
            checks);
    }

    private static void CheckUplinkPcmContract(
        ICollection<string> checks)
    {
        string sessionId = "session-" + DirectBase64Url.Encode(
            Enumerable.Range(80, 16)
                .Select(value => (byte)value)
                .ToArray());
        byte[] sessionBytes =
            DirectPcmFrameCodec.ParseSessionId(sessionId);
        byte[] encoded = new byte[DirectBridgeContract.PcmFrameBytes];
        Encoding.ASCII.GetBytes("BWCV").CopyTo(encoded, 0);
        encoded[4] = 1;
        encoded[5] = (byte)DirectPcmTrack.BrowserMicrophone;
        BinaryPrimitives.WriteUInt16LittleEndian(
            encoded.AsSpan(6, 2),
            0);
        sessionBytes.CopyTo(encoded, 8);
        BinaryPrimitives.WriteUInt32LittleEndian(
            encoded.AsSpan(24, 4),
            0);
        BinaryPrimitives.WriteUInt64LittleEndian(
            encoded.AsSpan(28, 8),
            20_000);
        for (int index = DirectBridgeContract.PcmFrameHeaderBytes;
            index < encoded.Length;
            index++)
        {
            encoded[index] = (byte)index;
        }

        DirectDecodedPcmFrame decoded =
            DirectPcmFrameCodec.DecodeUplink(encoded);
        byte firstOwned =
            decoded.Frame.PcmS16Le.Span[0];
        encoded[DirectBridgeContract.PcmFrameHeaderBytes] ^= 0xff;
        bool ownsPayload =
            decoded.Frame.PcmS16Le.Span[0] == firstOwned;
        Require(
            encoded.Length == 1956
            && decoded.SessionId == sessionId
            && decoded.Frame.Track
                == DirectPcmTrack.BrowserMicrophone
            && decoded.Frame.Sequence == 0
            && decoded.Frame.TimestampMicroseconds == 20_000
            && decoded.Frame.PcmS16Le.Length == 1920
            && ownsPayload,
            "direct-uplink-bwcv-v1-track3-frame-is-exact-and-owned",
            checks);

        int[] invalidOffsets = [0, 4, 5, 6];
        bool allHeaderMutationsRejected = invalidOffsets.All(offset =>
        {
            byte[] invalid = encoded.ToArray();
            invalid[offset] ^= 0x7f;
            try
            {
                _ = DirectPcmFrameCodec.DecodeUplink(invalid);
                return false;
            }
            catch (DirectProtocolException exception)
            {
                return exception.Code
                    == "BW_COMPUTER_VOICE_DIRECT_UPLINK_FRAME_INVALID";
            }
        });
        bool shortRejected;
        try
        {
            _ = DirectPcmFrameCodec.DecodeUplink(
                encoded.AsSpan(0, encoded.Length - 1));
            shortRejected = false;
        }
        catch (DirectProtocolException exception)
        {
            shortRejected = exception.Code
                == "BW_COMPUTER_VOICE_DIRECT_UPLINK_FRAME_INVALID";
        }
        Require(
            allHeaderMutationsRejected && shortRejected,
            "direct-uplink-rejects-length-magic-version-track-reserved",
            checks);

        DirectUplinkSequenceGuard guard = new();
        guard.Begin(sessionId);
        guard.Validate(sessionId, decoded);
        DirectDecodedPcmFrame second = decoded with
        {
            Frame = decoded.Frame with
            {
                Sequence = 1,
                TimestampMicroseconds = 40_000,
            },
        };
        guard.Validate(sessionId, second);
        bool gapRejected = false;
        try
        {
            guard.Validate(sessionId, second with
            {
                Frame = second.Frame with
                {
                    Sequence = 3,
                    TimestampMicroseconds = 60_000,
                },
            });
        }
        catch (DirectProtocolException exception)
        {
            gapRejected = exception.Code
                == "BW_COMPUTER_VOICE_DIRECT_UPLINK_SEQUENCE_INVALID";
        }
        guard.End();
        bool stoppedRejected = false;
        try
        {
            guard.Validate(sessionId, decoded);
        }
        catch (DirectProtocolException exception)
        {
            stoppedRejected = exception.Code
                == "BW_COMPUTER_VOICE_DIRECT_SESSION_MISMATCH";
        }
        string otherSessionId = "session-" + DirectBase64Url.Encode(
            Enumerable.Repeat((byte)0xa5, 16).ToArray());
        DirectUplinkSequenceGuard mismatchGuard = new();
        mismatchGuard.Begin(sessionId);
        bool mismatchRejected = false;
        try
        {
            mismatchGuard.Validate(
                sessionId,
                decoded with { SessionId = otherSessionId });
        }
        catch (DirectProtocolException exception)
        {
            mismatchRejected = exception.Code
                == "BW_COMPUTER_VOICE_DIRECT_SESSION_MISMATCH";
        }
        Require(
            gapRejected && stoppedRejected && mismatchRejected,
            "direct-uplink-guard-is-session-sequence-timestamp-gated",
            checks);
    }

    private static async Task CheckPcmStartGateAbortRaceAsync(
        ICollection<string> checks)
    {
        TaskCompletionSource<bool> senderEntered = new(
            TaskCreationOptions.RunContinuationsAsynchronously);
        TaskCompletionSource<bool> releaseSender = new(
            TaskCreationOptions.RunContinuationsAsynchronously);
        int sends = 0;
        DirectPcmStartGate gate = new(async (_, cancellationToken) =>
        {
            sends++;
            senderEntered.TrySetResult(true);
            await releaseSender.Task.WaitAsync(cancellationToken)
                .ConfigureAwait(false);
        });
        DirectPcmFrame frame = new(
            DirectPcmTrack.AppOutput,
            Sequence: 0,
            TimestampMicroseconds: 20_000,
            PcmS16Le: new byte[Pcm48kMonoFramer.BytesPerChunk]);
        await gate.SendAsync(frame, CancellationToken.None)
            .ConfigureAwait(false);
        Task release = gate.ReleaseAsync(CancellationToken.None);
        await senderEntered.Task.WaitAsync(
            TimeSpan.FromSeconds(2)).ConfigureAwait(false);
        gate.Abort();
        releaseSender.TrySetResult(true);
        bool releaseRejected = false;
        try
        {
            await release.ConfigureAwait(false);
        }
        catch (DirectProtocolException exception)
        {
            releaseRejected = exception.Code
                == "BW_COMPUTER_VOICE_DIRECT_PCM_START_ABORTED";
        }
        bool laterSendRejected = false;
        try
        {
            await gate.SendAsync(
                frame with { Sequence = 1 },
                CancellationToken.None).ConfigureAwait(false);
        }
        catch (DirectProtocolException exception)
        {
            laterSendRejected = exception.Code
                == "BW_COMPUTER_VOICE_DIRECT_PCM_START_ABORTED";
        }

        DirectPcmStartGate senderFailureGate = new(
            (_, _) => Task.FromException(
                new InvalidOperationException("fake sender failed")));
        await senderFailureGate.SendAsync(
            frame,
            CancellationToken.None).ConfigureAwait(false);
        bool senderFailureObserved = false;
        try
        {
            await senderFailureGate.ReleaseAsync(
                CancellationToken.None).ConfigureAwait(false);
        }
        catch (InvalidOperationException)
        {
            senderFailureObserved = true;
        }
        bool failedGateStayedClosed = false;
        try
        {
            await senderFailureGate.SendAsync(
                frame with { Sequence = 1 },
                CancellationToken.None).ConfigureAwait(false);
        }
        catch (DirectProtocolException exception)
        {
            failedGateStayedClosed = exception.Code
                == "BW_COMPUTER_VOICE_DIRECT_PCM_START_ABORTED";
        }
        Require(
            sends == 1
            && releaseRejected
            && laterSendRejected
            && senderFailureObserved
            && failedGateStayedClosed,
            "direct-pcm-start-gate-abort-and-sender-failure-stay-closed",
            checks);
    }

    private static void CheckDirectOutputRouteEvidence(
        DirectBridgeConfig config,
        ICollection<string> checks)
    {
        string[] managerMethods =
            typeof(IAudioSessionManager2ForRoute)
                .GetMethods()
                .Select(method => method.Name)
                .ToArray();
        string[] controlMethods =
            typeof(IAudioSessionControl2ForRoute)
                .GetMethods()
                .Select(method => method.Name)
                .ToArray();
        Require(
            typeof(IAudioSessionManager2ForRoute).GUID
                == DirectOutputRouteInterop.IidIAudioSessionManager2
            && managerMethods.SequenceEqual(new[]
            {
                "GetAudioSessionControl",
                "GetSimpleAudioVolume",
                "GetSessionEnumerator",
                "RegisterSessionNotification",
                "UnregisterSessionNotification",
                "RegisterDuckNotification",
                "UnregisterDuckNotification",
            })
            && controlMethods.SequenceEqual(new[]
            {
                "GetState",
                "GetDisplayName",
                "SetDisplayName",
                "GetIconPath",
                "SetIconPath",
                "GetGroupingParam",
                "SetGroupingParam",
                "RegisterAudioSessionNotification",
                "UnregisterAudioSessionNotification",
                "GetSessionIdentifier",
                "GetSessionInstanceIdentifier",
                "GetProcessId",
                "IsSystemSoundsSession",
                "SetDuckingPreference",
            })
            && typeof(IAudioSessionNotificationForRoute)
                .GetMethods()
                .Select(method => method.Name)
                .SequenceEqual(new[] { "OnSessionCreated" }),
            "direct-output-route-public-core-audio-vtables-are-exact",
            checks);

        CodexAppTarget firstTarget = new(
            RootProcessId: 4100,
            RootProcessStartFileTimeUtc: 133700000000000000,
            ProcessTree: new HashSet<uint> { 4100, 4101 },
            WindowHandle: 1);
        DirectOutputRouteEvidenceTracker tracker = new(firstTarget);
        long activeVersion = tracker.BeginEnumeration();
        tracker.CompleteEnumeration(
            activeVersion,
            new[]
            {
                new DirectOutputRouteSession(
                    4101,
                    DirectAudioSessionState.Active),
            });
        Require(
            tracker.Verified,
            "direct-output-route-active-session-is-verified",
            checks);

        long inactiveVersion = tracker.BeginEnumeration();
        tracker.CompleteEnumeration(
            inactiveVersion,
            new[]
            {
                new DirectOutputRouteSession(
                    4100,
                    DirectAudioSessionState.Inactive),
            });
        Require(
            !tracker.Verified,
            "direct-output-route-inactive-history-is-unverified",
            checks);

        long expiredVersion = tracker.BeginEnumeration();
        tracker.CompleteEnumeration(
            expiredVersion,
            new[]
            {
                new DirectOutputRouteSession(
                    4100,
                    DirectAudioSessionState.Expired),
            });
        Require(
            !tracker.Verified,
            "direct-output-route-expired-session-is-unverified",
            checks);

        tracker.ObserveNotification(
            new DirectOutputRouteSession(
                9999,
                DirectAudioSessionState.Active));
        Require(
            !tracker.Verified,
            "direct-output-route-foreign-process-is-unverified",
            checks);

        CodexAppTarget nextTarget = new(
            RootProcessId: 5100,
            RootProcessStartFileTimeUtc: 133700000000000001,
            ProcessTree: new HashSet<uint> { 5100, 5101 },
            WindowHandle: 2);
        tracker.SetTarget(nextTarget);
        tracker.ObserveNotification(
            new DirectOutputRouteSession(
                4101,
                DirectAudioSessionState.Active));
        Require(
            !tracker.Verified,
            "direct-output-route-old-process-tree-is-cleared",
            checks);

        long raceVersion = tracker.BeginEnumeration();
        tracker.ObserveNotification(
            new DirectOutputRouteSession(
                5101,
                DirectAudioSessionState.Active));
        tracker.CompleteEnumeration(
            raceVersion,
            Array.Empty<DirectOutputRouteSession>());
        Require(
            tracker.Verified,
            "direct-output-route-register-before-enumerate-race-is-closed",
            checks);
        tracker.Clear();
        Require(
            !tracker.Verified,
            "direct-output-route-clear-removes-evidence",
            checks);

        int targetProbeCount = 0;
        FakeDirectOutputRouteObserverFactory verifiedFactory = new(
            verified: true);
        object result = DirectOutputRouteProbe.Run(
            config,
            verifiedFactory,
            () =>
            {
                targetProbeCount++;
                return new CodexAppProbeState(
                    RootCount: 1,
                    WindowCount: 1,
                    ReadyTarget: firstTarget);
            });
        using JsonDocument document = JsonDocument.Parse(
            JsonSerializer.Serialize(
                result,
                DirectBridgeContract.JsonOptions));
        JsonElement root = document.RootElement;
        HashSet<string> keys = root.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        Require(
            keys.SetEquals(new[]
            {
                "contract",
                "ok",
                "verified",
                "reason",
                "captureStarted",
                "shortcutSent",
                "appLaunched",
            })
            && root.GetProperty("contract").GetString()
                == DirectOutputRouteProbe.Contract
            && root.GetProperty("ok").GetBoolean()
            && root.GetProperty("verified").GetBoolean()
            && root.GetProperty("reason").ValueKind
                == JsonValueKind.Null
            && !root.GetProperty("captureStarted").GetBoolean()
            && !root.GetProperty("shortcutSent").GetBoolean()
            && !root.GetProperty("appLaunched").GetBoolean()
            && targetProbeCount == 1
            && verifiedFactory.CreateCount == 1
            && verifiedFactory.DisposeCount == 1,
            "direct-output-route-cli-probe-is-strict-and-side-effect-free",
            checks);

        FakeDirectOutputRouteObserverFactory idleFactory = new(
            verified: true);
        WindowsDirectMediaAdapter idleAdapter = new(
            new WindowsDirectTypistLeaseController(
                (_, _) => throw new InvalidOperationException(
                    "idle STATUS must not invoke typist")),
            idleFactory);
        bool idleVerified = idleAdapter.IsOutputRouteVerified(config);
        idleAdapter.DisposeAsync().AsTask().GetAwaiter().GetResult();
        Require(
            !idleVerified
            && idleFactory.CreateCount == 0
            && idleFactory.DisposeCount == 0,
            "direct-output-route-idle-status-does-not-create-observer",
            checks);

        List<string> order = [];
        using IDirectOutputRouteObserver unverified =
            WindowsDirectMediaAdapter
                .CreateOutputRouteObserverWithoutBlockingStart(
                    new FakeDirectOutputRouteObserverFactory(
                        verified: false,
                        onCreate: () => order.Add("observer")),
                    config.VirtualSpeakerRenderEndpointId,
                    firstTarget);
        WindowsDirectMediaAdapter.SendShortcutAtAtomicCommitBoundary(
            validatePreparedMedia: () => order.Add("validate"),
            sendShortcut: () =>
            {
                order.Add("shortcut");
                return true;
            },
            commitOwnedResources: () => order.Add("commit"),
            CancellationToken.None);
        Require(
            !unverified.Verified
            && order.SequenceEqual(new[]
            {
                "observer",
                "validate",
                "shortcut",
                "commit",
            }),
            "direct-output-route-observer-precedes-shortcut-without-gating-start",
            checks);
    }

    private static async Task
        CheckUnverifiedRouteStatusDoesNotGateStartAsync(
            DirectBridgeConfigStore store,
            ICollection<string> checks)
    {
        FakeDirectAppLauncher app = new();
        FakeDirectMediaAdapter media = new()
        {
            OutputRouteVerified = false,
        };
        await using DirectBridgeCoordinator coordinator = new(
            store,
            app,
            media,
            renderEndpointProbe: _ => null);
        DirectBridgeProtocolSession session = new(
            "connection-route-unverified",
            "https://bwicarus.taile44d0c.ts.net",
            store,
            coordinator);
        List<object> events = [];
        List<byte[]> frames = [];
        _ = RequireSuccess(
            await SendAsync(
                session,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "hello",
                    requestId = "route-unverified-hello",
                    protocolVersion = 3,
                },
                events,
                frames).ConfigureAwait(false),
            "hello");
        JsonElement idle = RequireSuccess(
            await SendAsync(
                session,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "status",
                    requestId = "route-unverified-idle",
                },
                events,
                frames).ConfigureAwait(false),
            "status");
        Require(
            HasExactStatusKeys(idle)
            && !idle.GetProperty("ready").GetBoolean()
            && idle.GetProperty("state").GetString() == "idle"
            && idle.GetProperty("reason").GetString()
                == DirectOutputRouteProbe.UnverifiedReason
            && app.EnsureRunningCount == 0
            && app.WaitReadyCount == 0
            && media.StartCount == 0,
            "direct-output-route-unverified-idle-status-is-exact",
            checks);

        string sessionId = "session-" + DirectBase64Url.Encode(
            Enumerable.Repeat((byte)0x63, 16).ToArray());
        JsonElement start = RequireSuccess(
            await SendAsync(
                session,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "start",
                    requestId = "route-unverified-start",
                    sessionId,
                },
                events,
                frames).ConfigureAwait(false),
            "start");
        JsonElement activeUnverified = RequireSuccess(
            await SendAsync(
                session,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "status",
                    requestId = "route-unverified-active",
                },
                events,
                frames).ConfigureAwait(false),
            "status");
        media.OutputRouteVerified = true;
        JsonElement activeVerified = RequireSuccess(
            await SendAsync(
                session,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "status",
                    requestId = "route-verified-active",
                },
                events,
                frames).ConfigureAwait(false),
            "status");
        Require(
            start.GetProperty("state").GetString() == "active"
            && media.StartCount == 1
            && HasExactStatusKeys(activeUnverified)
            && activeUnverified.GetProperty("state").GetString()
                == "active"
            && !activeUnverified.GetProperty("ready").GetBoolean()
            && activeUnverified.GetProperty("reason").GetString()
                == DirectOutputRouteProbe.UnverifiedReason
            && HasExactStatusKeys(activeVerified)
            && activeVerified.GetProperty("state").GetString()
                == "active"
            && activeVerified.GetProperty("ready").GetBoolean()
            && activeVerified.GetProperty("reason").ValueKind
                == JsonValueKind.Null,
            "direct-output-route-start-is-not-gated-and-can-later-verify",
            checks);

        _ = RequireSuccess(
            await SendAsync(
                session,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "stop",
                    requestId = "route-unverified-stop",
                    sessionId,
                },
                events,
                frames).ConfigureAwait(false),
            "stop");
    }

    private static bool HasExactStatusKeys(JsonElement status)
    {
        HashSet<string> keys = status.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        if (!keys.SetEquals(new[]
        {
            "ready",
            "state",
            "reason",
            "localOptIn",
            "lastError",
            "media",
        }))
        {
            return false;
        }
        HashSet<string> mediaKeys = status.GetProperty("media")
            .EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        return mediaKeys.SetEquals(new[]
        {
            "hostReady",
            "captureActive",
        });
    }

    private static void CheckProductionAdapterBoundaries(
        string root,
        ICollection<string> checks)
    {
        UnwiredDirectAppLauncher launcher = new();
        bool appDenied = false;
        try
        {
            launcher.EnsureRunningAsync(
                "codex-desktop",
                DirectBridgeContract.CodexAppUserModelId,
                CancellationToken.None).GetAwaiter().GetResult();
        }
        catch (DirectProtocolException exception)
        {
            appDenied = exception.Code
                == "BW_COMPUTER_VOICE_DIRECT_APP_LAUNCHER_NOT_WIRED";
        }
        UnwiredDirectMediaAdapter media = new();
        bool mediaDenied = false;
        try
        {
            media.StartAsync(
                new DirectMediaStartRequest(
                    "session-" + DirectBase64Url.Encode(new byte[16]),
                    123,
                    133700000000000000,
                    "codex-desktop",
                    DirectBridgeContract.CodexAppUserModelId,
                    "virtual-mic-render",
                    "virtual-mic-capture",
                    "virtual-speaker-render"),
                (_, _) => Task.CompletedTask,
                CancellationToken.None).GetAwaiter().GetResult();
        }
        catch (DirectProtocolException exception)
        {
            mediaDenied = exception.Code
                == "BW_COMPUTER_VOICE_DIRECT_MEDIA_NOT_WIRED";
        }
        Require(
            appDenied && mediaDenied,
            "direct-unwired-adapter-seams-never-fake-readiness",
            checks);

        WindowsDirectAppLauncher productionLauncher = new();
        WindowsDirectMediaAdapter productionMedia = new(root);
        bool arbitraryTargetDenied = false;
        try
        {
            WindowsDirectAppLauncher.ValidateTarget(
                "codex-desktop",
                "arbitrary-app");
        }
        catch (DirectProtocolException exception)
        {
            arbitraryTargetDenied = exception.Code
                == "BW_COMPUTER_VOICE_DIRECT_APP_TARGET_INVALID";
        }
        productionMedia.DisposeAsync().AsTask().GetAwaiter().GetResult();
        Require(
            productionLauncher.IsWired
            && productionMedia.IsWired
            && arbitraryTargetDenied,
            "direct-production-adapters-use-fixed-local-targets",
            checks);
    }

    private static void CheckStrictOriginConfig(
        string root,
        ICollection<string> checks)
    {
        string badRoot = System.IO.Path.Combine(root, "bad-origin");
        string invalidPath = System.IO.Path.Combine(
            badRoot,
            "native-host",
            "direct.json");
        WriteConfigAsync(
            invalidPath,
            System.IO.Path.Combine(
                badRoot,
                "runtime",
                "computer-voice-direct.status.json"),
            "http://reader.example",
            localOptIn: false).GetAwaiter().GetResult();
        bool rejected = false;
        try
        {
            _ = new DirectBridgeConfigStore(invalidPath).Load();
        }
        catch (DirectProtocolException exception)
        {
            rejected = exception.Code
                == "BW_COMPUTER_VOICE_DIRECT_CONFIG_INVALID";
        }
        Require(
            rejected
            && CheckExperimentalAndStrictOrigins(root)
            && CheckStrictV5ConfigSchema(root),
            "direct-origin-policy-is-explicit-and-canonical",
            checks);
    }

    private static bool CheckStrictV5ConfigSchema(string root)
    {
        string schemaRoot = System.IO.Path.Combine(
            root,
            "strict-v5-schema");
        string configPath = System.IO.Path.Combine(
            schemaRoot,
            "native-host",
            "direct.json");
        WriteConfigAsync(
            configPath,
            System.IO.Path.Combine(
                schemaRoot,
                "runtime",
                "computer-voice-direct.status.json"),
            "https://bwicarus.taile44d0c.ts.net",
            localOptIn: false).GetAwaiter().GetResult();
        string validJson = File.ReadAllText(configPath);
        DirectBridgeConfig strict =
            new DirectBridgeConfigStore(configPath).Load();
        if (
            !strict.PerAppAudioRouteAutomationEnabled
            || string.IsNullOrEmpty(
                strict.VirtualMicrophoneCaptureEndpointId)
        )
        {
            return false;
        }

        List<Action<JsonObject>> mutations =
        [
            value => value["contract"] =
                "reader-computer-voice-direct-config/1",
            value => value["microphoneEndpointId"] = "legacy-mic",
            value => value["pairingCodeHash"] = "legacy-pair",
            value => value["pairingExpiresAtUtc"] =
                "2026-07-29T00:00:00Z",
            value => value["pairedClientPublicKeySpki"] = "legacy-key",
            value => value["pairedClientFingerprintSha256"] =
                "legacy-fingerprint",
            value => value["virtualMicrophoneRenderEndpointId"] = "",
            value => value["virtualMicrophoneCaptureEndpointId"] = "",
            value => value["virtualSpeakerRenderEndpointId"] = "",
            value => value["virtualSpeakerRenderEndpointId"] =
                value["virtualMicrophoneRenderEndpointId"]!.GetValue<string>(),
            value => value["virtualMicrophoneCaptureEndpointId"] =
                value["virtualMicrophoneRenderEndpointId"]!.GetValue<string>(),
            value => value["virtualMicrophoneRenderEndpointId"] =
                value["virtualMicrophoneCaptureEndpointId"]!.GetValue<string>(),
            value => value["virtualSpeakerRenderEndpointId"] =
                value["virtualMicrophoneCaptureEndpointId"]!.GetValue<string>(),
            value => value["contract"] =
                DirectBridgeContract.LegacyConfigContract,
            value => value["contract"] =
                "reader-computer-voice-direct-config/3",
            value => value["experimentalSingleUserMode"] = false,
        ];
        foreach (Action<JsonObject> mutate in mutations)
        {
            JsonObject candidate =
                JsonNode.Parse(validJson)?.AsObject()
                ?? throw new InvalidOperationException(
                    "self-test config JSON was not an object");
            mutate(candidate);
            File.WriteAllText(
                configPath,
                candidate.ToJsonString());
            try
            {
                _ = new DirectBridgeConfigStore(configPath).Load();
                return false;
            }
            catch (DirectProtocolException exception)
            {
                if (exception.Code
                    != "BW_COMPUTER_VOICE_DIRECT_CONFIG_INVALID")
                {
                    return false;
                }
            }
        }

        JsonObject legacy =
            JsonNode.Parse(validJson)?.AsObject()
            ?? throw new InvalidOperationException(
                "self-test config JSON was not an object");
        legacy["contract"] =
            DirectBridgeContract.LegacyConfigContract;
        legacy.Remove("virtualMicrophoneCaptureEndpointId");
        File.WriteAllText(configPath, legacy.ToJsonString());
        DirectBridgeConfig loadedLegacy =
            new DirectBridgeConfigStore(configPath).Load();
        return !loadedLegacy.PerAppAudioRouteAutomationEnabled
            && loadedLegacy.VirtualMicrophoneCaptureEndpointId == "";
    }

    private static bool CheckExperimentalAndStrictOrigins(string root)
    {
        string experimentalRoot = System.IO.Path.Combine(
            root,
            "experimental-origin");
        string experimentalPath = System.IO.Path.Combine(
            experimentalRoot,
            "native-host",
            "direct.json");
        WriteConfigAsync(
            experimentalPath,
            System.IO.Path.Combine(
                experimentalRoot,
                "runtime",
                "computer-voice-direct.status.json"),
            "https://extension-page.example",
            localOptIn: false,
            experimentalSingleUserMode: true).GetAwaiter().GetResult();
        DirectBridgeConfig experimental =
            new DirectBridgeConfigStore(experimentalPath).Load();
        string strictRoot = System.IO.Path.Combine(root, "strict-origin");
        string strictPath = System.IO.Path.Combine(
            strictRoot,
            "native-host",
            "direct.json");
        WriteConfigAsync(
            strictPath,
            System.IO.Path.Combine(
                strictRoot,
                "runtime",
                "computer-voice-direct.status.json"),
            "https://reader.example",
            localOptIn: false,
            experimentalSingleUserMode: false).GetAwaiter().GetResult();
        bool strictModeRejected = false;
        try
        {
            _ = new DirectBridgeConfigStore(strictPath).Load();
        }
        catch (DirectProtocolException exception)
        {
            strictModeRejected = exception.Code
                == "BW_COMPUTER_VOICE_DIRECT_CONFIG_INVALID";
        }

        const string chromeOrigin =
            "chrome-extension://jddhhakcblmihidgdobfkcejjinpigak";
        const string safariOrigin =
            "safari-web-extension://E8BEA491-9B80-45DB-8B20-3E586473BD47";
        return DirectBridgeServer.OriginMatchesAllowlist(
                experimental,
                "https://bwicarus.taile44d0c.ts.net")
            && DirectBridgeServer.OriginMatchesAllowlist(
                experimental,
                chromeOrigin)
            && DirectBridgeServer.OriginMatchesAllowlist(
                experimental,
                safariOrigin)
            && !DirectBridgeServer.OriginMatchesAllowlist(
                experimental,
                "https://extension-page.example")
            && !DirectBridgeServer.OriginMatchesAllowlist(
                experimental,
                "http://bwicarus.taile44d0c.ts.net")
            && !DirectBridgeServer.OriginMatchesAllowlist(
                experimental,
                "https://bwicarus.taile44d0c.ts.net/")
            && !DirectBridgeServer.OriginMatchesAllowlist(
                experimental,
                chromeOrigin + "/")
            && !DirectBridgeServer.OriginMatchesAllowlist(
                experimental,
                "chrome-extension://abcdefghijklmnopabcdefghijklmnop")
            && !DirectBridgeServer.OriginMatchesAllowlist(
                experimental,
                "chrome-extension://abcdefghijklmnopabcdefghijklmnop:443")
            && !DirectBridgeServer.OriginMatchesAllowlist(
                experimental,
                "chrome-extension://user@abcdefghijklmnopabcdefghijklmnop")
            && !DirectBridgeServer.OriginMatchesAllowlist(
                experimental,
                safariOrigin + "?forged=1")
            && !DirectBridgeServer.OriginMatchesAllowlist(
                experimental,
                "safari-web-extension://E8BEA491-9B80-45DB-8B20-3E586473BD4Z")
            && !DirectBridgeServer.OriginMatchesAllowlist(
                experimental,
                "file://extension-page.example")
            && !DirectBridgeServer.OriginMatchesAllowlist(
                experimental,
                "null")
            && strictModeRejected;
    }

    private static async Task CheckServiceLeaseAsync(
        string installationRoot,
        string configPath,
        ICollection<string> checks)
    {
        string leasePath = System.IO.Path.Combine(
            installationRoot,
            "runtime",
            "computer-voice-direct.service.json");
        DirectServiceLease lease = new(
            leasePath,
            pid: 4242,
            executable: System.IO.Path.Combine(
                installationRoot,
                "native-host",
                "bw-computer-voice-audio.exe"),
            configPath,
            startedAtUtc: new DateTimeOffset(
                2026,
                7,
                29,
                5,
                0,
                0,
                TimeSpan.Zero));
        await lease.WriteAsync(CancellationToken.None)
            .ConfigureAwait(false);
        using (JsonDocument document = JsonDocument.Parse(
            await File.ReadAllTextAsync(leasePath)
                .ConfigureAwait(false)))
        {
            HashSet<string> keys = document.RootElement
                .EnumerateObject()
                .Select(property => property.Name)
                .ToHashSet(StringComparer.Ordinal);
            Require(
                keys.SetEquals(new[]
                {
                    "contract",
                    "pid",
                    "executable",
                    "configPath",
                    "startedAtUtc",
                })
                && document.RootElement.GetProperty("contract")
                    .GetString() == DirectServiceLease.Contract
                && document.RootElement.GetProperty("pid")
                    .GetInt32() == 4242,
                "direct-service-lease-unifies-logon-and-gui-start",
                checks);
        }
        await lease.ClearIfOwnedAsync().ConfigureAwait(false);
        Require(
            !File.Exists(leasePath),
            "direct-service-lease-clears-only-owned-file",
            checks);
    }

    private static async Task CheckRuntimeErrorLifecycleAsync(
        string configPath,
        string statusPath,
        ICollection<string> checks)
    {
        const int endpointHresult = unchecked((int)0x88890004);
        DirectProtocolException endpointFailure = new(
            "BW_COMPUTER_VOICE_DIRECT_RENDER_ENDPOINT_UNAVAILABLE",
            "secret-endpoint-id-must-never-be-serialized",
            retryable: true,
            innerException: new AudioCaptureStageException(
                "virtual-microphone.get-render-device-state",
                endpointHresult));
        DirectBridgeConfigStore store = new(configPath);
        FakeDirectMediaAdapter media = new();
        await using DirectBridgeCoordinator coordinator = new(
            store,
            new FakeDirectAppLauncher(),
            media,
            renderEndpointProbe: _ => endpointFailure);
        DirectBridgeProtocolSession protocol = new(
            "connection-runtime-error",
            "https://extension-page.example",
            store,
            coordinator);
        List<object> events = [];
        List<byte[]> frames = [];
        _ = RequireSuccess(
            await SendAsync(
                protocol,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "hello",
                    requestId = "request-runtime-error-hello",
                    protocolVersion = 3,
                },
                events,
                frames).ConfigureAwait(false),
            "hello");
        JsonElement status = RequireSuccess(
            await SendAsync(
                protocol,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "status",
                    requestId = "request-runtime-error-status",
                },
                events,
                frames).ConfigureAwait(false),
            "status");
        JsonElement statusError = status.GetProperty("lastError");
        Require(
            !status.GetProperty("ready").GetBoolean()
            && status.GetProperty("state").GetString()
                == "unavailable"
            && status.GetProperty("reason").GetString()
                == endpointFailure.Code
            && statusError.GetProperty("code").GetString()
                == endpointFailure.Code
            && statusError.GetProperty("stage").GetString()
                == "virtual-microphone.get-render-device-state"
            && statusError.GetProperty("hresult").GetString()
                == "0x88890004"
            && statusError.EnumerateObject()
                .Select(property => property.Name)
                .ToHashSet(StringComparer.Ordinal)
                .SetEquals(new[]
                {
                    "failureId",
                    "code",
                    "stage",
                    "hresult",
                    "atUtc",
                })
            && !statusError.GetRawText().Contains(
                "secret-endpoint",
                StringComparison.Ordinal),
            "direct-status-endpoint-failure-is-sanitized-and-not-ready",
            checks);

        string sessionId = "session-" + DirectBase64Url.Encode(
            Enumerable.Repeat((byte)0xc3, 16).ToArray());
        _ = await coordinator.StartAsync(
            "connection-runtime-error",
            sessionId,
            (_, _) => Task.CompletedTask,
            (_, _) => Task.CompletedTask,
            CancellationToken.None).ConfigureAwait(false);
        Require(
            coordinator.LastError is null && media.CaptureActive,
            "direct-successful-start-clears-retained-last-error",
            checks);
        await coordinator.StopAsync(
            "connection-runtime-error",
            sessionId,
            CancellationToken.None).ConfigureAwait(false);

        DirectRuntimeError persisted =
            DirectRuntimeError.FromException(
                endpointFailure,
                "runtime-status",
                new DateTimeOffset(
                    2026,
                    7,
                    29,
                    6,
                    0,
                    0,
                    TimeSpan.Zero));
        DirectRuntimeStatusWriter writer = new(
            statusPath,
            "fedcba9876543210fedcba9876543210");
        await writer.WriteAsync(
            "faulted",
            readerConnected: true,
            captureActive: false,
            persisted,
            CancellationToken.None).ConfigureAwait(false);
        await writer.WriteAsync(
            "idle",
            readerConnected: false,
            captureActive: false,
            persisted,
            CancellationToken.None).ConfigureAwait(false);
        string persistedJson = await File.ReadAllTextAsync(statusPath)
            .ConfigureAwait(false);
        using JsonDocument document = JsonDocument.Parse(persistedJson);
        JsonElement root = document.RootElement;
        JsonElement retained = root.GetProperty("lastError");
        Require(
            root.GetProperty("contract").GetString()
                == DirectBridgeContract.RuntimeStatusContract
            && root.GetProperty("state").GetString() == "idle"
            && retained.GetProperty("failureId").GetString()
                == persisted.FailureId
            && retained.GetProperty("code").GetString()
                == endpointFailure.Code
            && retained.GetProperty("stage").GetString()
                == "virtual-microphone.get-render-device-state"
            && retained.GetProperty("hresult").GetString()
                == "0x88890004"
            && !persistedJson.Contains(
                "secret-endpoint",
                StringComparison.Ordinal),
            "direct-runtime-status-v2-retains-sanitized-error-across-idle",
            checks);
    }

    private static async Task CheckContextIpcContractAsync(
        ICollection<string> checks)
    {
        byte[] payload = Encoding.UTF8.GetBytes(
            """{"contract":"reader-voice-typist-ipc/1"}""");
        await using MemoryStream framed = new();
        await DirectContextIpcFraming.WriteAsync(
            framed,
            payload,
            CancellationToken.None).ConfigureAwait(false);
        byte[] wire = framed.ToArray();
        framed.Position = 0;
        byte[] roundTrip = await DirectContextIpcFraming.ReadAsync(
            framed,
            CancellationToken.None).ConfigureAwait(false);
        bool invalidUtf8Rejected = false;
        await using (MemoryStream invalidUtf8 = new(
            [1, 0, 0, 0, 0xff]))
        {
            try
            {
                _ = await DirectContextIpcFraming.ReadAsync(
                    invalidUtf8,
                    CancellationToken.None).ConfigureAwait(false);
            }
            catch (DirectProtocolException exception)
            {
                invalidUtf8Rejected = exception.Code
                    == "BW_COMPUTER_VOICE_CONTEXT_IPC_FRAME_INVALID"
                    && exception.Retryable;
            }
        }
        Require(
            BinaryPrimitives.ReadInt32LittleEndian(
                wire.AsSpan(0, sizeof(int))) == payload.Length
            && wire.AsSpan(sizeof(int)).SequenceEqual(payload)
            && roundTrip.SequenceEqual(payload)
            && invalidUtf8Rejected
            && NamedPipeDirectContextTransport.PipeName
                == "bw-reader-voice-typist-v1"
            && NamedPipeDirectContextTransport.PipePath
                == "\\\\.\\pipe\\bw-reader-voice-typist-v1"
            && NamedPipeDirectContextTransport.PipePath.Count(
                character => character == '\\') == 4
            && NamedPipeDirectContextTransport.MaximumServerInstances
                == 1
            && NamedPipeDirectContextTransport.RequiredPipeOptions
                == (
                    System.IO.Pipes.PipeOptions.Asynchronous
                    | System.IO.Pipes.PipeOptions.CurrentUserOnly)
            && NamedPipeDirectContextTransport.ExchangeTimeout
                == TimeSpan.FromSeconds(3)
            && NamedPipeDirectContextTransport.MaximumPayloadBytes
                == 65_536,
            "direct-context-pipe-is-current-user-single-framed-strict-utf8",
            checks);

        string sessionId = "session-" + DirectBase64Url.Encode(
            Enumerable.Repeat((byte)0xd4, 16).ToArray());
        const string ipcRequestId = "request-context-ipc-9";
        JsonElement eventValue = JsonSerializer.SerializeToElement(new
        {
            v = 1,
            seq = 9,
            type = "drawing",
            ts = 1_750_000_009,
            id = "0000000000000009",
            drawingRevision = "dr_abc",
        });
        DirectContextEvent contextEvent =
            NamedPipeDirectContextAdapter.ValidateEvent(eventValue);
        FakeDirectContextIpcTransport transport = new();
        transport.ReplyFactory = request =>
        {
            using JsonDocument requestDocument =
                JsonDocument.Parse(request);
            JsonElement root = requestDocument.RootElement;
            transport.RequestWasExact =
                root.EnumerateObject()
                    .Select(property => property.Name)
                    .ToHashSet(StringComparer.Ordinal)
                    .SetEquals(new[]
                    {
                        "contract",
                        "requestId",
                        "sessionId",
                        "action",
                        "event",
                    })
                && root.GetProperty("contract").GetString()
                    == NamedPipeDirectContextAdapter.IpcContract
                && root.GetProperty("requestId").GetString()
                    == ipcRequestId
                && root.GetProperty("action").GetString() == "context"
                && root.GetProperty("sessionId").GetString()
                    == sessionId
                && root.GetProperty("event")
                    .GetProperty("drawingRevision").GetString()
                    == "dr_abc";
            return JsonSerializer.SerializeToUtf8Bytes(new
            {
                contract = NamedPipeDirectContextAdapter.IpcContract,
                requestId = ipcRequestId,
                ok = true,
                action = "context",
                payload = new
                {
                    sessionId,
                    eventId = contextEvent.EventId,
                    seq = contextEvent.Sequence,
                    outcome = transport.Outcome,
                },
            });
        };
        NamedPipeDirectContextAdapter adapter = new(transport);
        DirectContextForwardResult accepted = await adapter.ForwardAsync(
            ipcRequestId,
            sessionId,
            NamedPipeDirectContextAdapter.ContextContract,
            contextEvent,
            CancellationToken.None).ConfigureAwait(false);
        transport.Outcome = "duplicate";
        DirectContextForwardResult duplicate = await adapter.ForwardAsync(
            ipcRequestId,
            sessionId,
            NamedPipeDirectContextAdapter.ContextContract,
            contextEvent,
            CancellationToken.None).ConfigureAwait(false);
        Require(
            accepted.Outcome == "accepted"
            && duplicate.Outcome == "duplicate"
            && transport.RequestWasExact
            && transport.ExchangeCount == 2,
            "direct-context-waits-for-exact-accepted-or-duplicate-ipc-ack",
            checks);

        transport.ReplyFactory = _ =>
            Encoding.UTF8.GetBytes(
                """
                {"contract":"reader-voice-typist-ipc/1","requestId":"request-context-ipc-9","ok":true,"action":"context","payload":{"sessionId":"wrong","eventId":"0000000000000009","seq":9,"outcome":"accepted"}}
                """);
        bool badAckRetryable = false;
        try
        {
            _ = await adapter.ForwardAsync(
                ipcRequestId,
                sessionId,
                NamedPipeDirectContextAdapter.ContextContract,
                contextEvent,
                CancellationToken.None).ConfigureAwait(false);
        }
        catch (DirectProtocolException exception)
        {
            badAckRetryable = exception.Code
                == "BW_COMPUTER_VOICE_CONTEXT_ACK_INVALID"
                && exception.Retryable;
        }
        transport.ReplyFactory = _ =>
            Encoding.UTF8.GetBytes(
                """
                {"contract":"reader-voice-typist-ipc/1","requestId":"request-context-ipc-9","ok":false,"error":{"code":"BW_TYPIST_IPC_PAYLOAD","message":"bad escaped context","retryable":false}}
                """);
        bool typistRejectionPreserved = false;
        try
        {
            _ = await adapter.ForwardAsync(
                ipcRequestId,
                sessionId,
                NamedPipeDirectContextAdapter.ContextContract,
                contextEvent,
                CancellationToken.None).ConfigureAwait(false);
        }
        catch (DirectProtocolException exception)
        {
            typistRejectionPreserved = exception.Code
                == "BW_TYPIST_IPC_PAYLOAD"
                && !exception.Retryable;
        }
        string[] allowedTypes =
        [
            "page.context",
            "focus",
            "drawing",
            "command",
            "command-failed",
        ];
        bool allowlistAccepted = allowedTypes.All(type =>
        {
            JsonElement candidate = JsonSerializer.SerializeToElement(
                new
                {
                    v = 1,
                    seq = 1,
                    type,
                    ts = 1,
                    id = "0000000000000001",
                });
            try
            {
                _ = NamedPipeDirectContextAdapter.ValidateEvent(
                    candidate);
                return true;
            }
            catch
            {
                return false;
            }
        });
        bool unknownRejected = false;
        try
        {
            _ = NamedPipeDirectContextAdapter.ValidateEvent(
                JsonSerializer.SerializeToElement(new
                {
                    v = 1,
                    seq = 1,
                    type = "unknown",
                    ts = 1,
                    id = "0000000000000001",
                }));
        }
        catch (DirectProtocolException exception)
        {
            unknownRejected = exception.Code
                == "BW_COMPUTER_VOICE_CONTEXT_SCHEMA_INVALID"
                && !exception.Retryable;
        }
        Require(
            badAckRetryable
            && typistRejectionPreserved
            && allowlistAccepted
            && unknownRejected,
            "direct-context-event-allowlist-and-ack-errors-fail-closed",
            checks);
    }

    private static async Task<JsonElement> SendAsync(
        DirectBridgeProtocolSession session,
        object message,
        ICollection<object> events,
        ICollection<byte[]> pcmFrames,
        ICollection<string>? wireOrder = null)
    {
        string json = JsonSerializer.Serialize(
            message,
            DirectBridgeContract.JsonOptions);
        DirectProtocolReply response = await session.HandleAsync(
            json,
            (state, reason) =>
            {
                events.Add(
                    DirectBridgeProtocolSession.StatusEvent(
                        state,
                        reason));
                return Task.CompletedTask;
            },
            (sessionId, frame, _) =>
            {
                wireOrder?.Add("pcm");
                pcmFrames.Add(
                    DirectPcmFrameCodec.Encode(sessionId, frame));
                return Task.CompletedTask;
            },
            CancellationToken.None).ConfigureAwait(false);
        wireOrder?.Add("result");
        if (response.AfterSendAsync is not null)
        {
            await response.AfterSendAsync(CancellationToken.None)
                .ConfigureAwait(false);
        }
        return JsonSerializer.SerializeToElement(
            response.Envelope,
            DirectBridgeContract.JsonOptions);
    }

    private static JsonElement RequireSuccess(
        JsonElement response,
        string action)
    {
        if (
            response.GetProperty("type").GetString() != "result"
            || !response.GetProperty("ok").GetBoolean()
            || response.GetProperty("action").GetString() != action
        )
        {
            throw new InvalidOperationException(
                $"direct self-test request failed: {action}: "
                + response.GetRawText());
        }
        return response.GetProperty("payload");
    }

    private static async Task CheckSnapshotMcpModeAsync(
        string root,
        string origin,
        ICollection<string> checks)
    {
        string installationRoot = System.IO.Path.Combine(
            root,
            "snapshot-mode");
        string configPath = System.IO.Path.Combine(
            installationRoot,
            "native-host",
            "direct.json");
        string statusPath = System.IO.Path.Combine(
            installationRoot,
            "runtime",
            "computer-voice-direct.status.json");
        string snapshotPath = System.IO.Path.Combine(
            installationRoot,
            "runtime",
            FileDirectSnapshotContextAdapter.SnapshotFileName);
        await WriteConfigAsync(
            configPath,
            statusPath,
            origin,
            localOptIn: true,
            contextDeliveryMode:
                DirectContextDeliveryMode.SnapshotMcp)
            .ConfigureAwait(false);

        DirectBridgeConfigStore store = new(configPath);
        FakeDirectAppLauncher launcher = new();
        FakeDirectMediaAdapter media = new();
        FakeDirectContextAdapter legacy = new();
        FileDirectSnapshotContextAdapter snapshot = new(
            snapshotPath,
            () => DateTimeOffset.FromUnixTimeMilliseconds(
                1_750_000_005_000));
        await using DirectBridgeCoordinator coordinator = new(
            store,
            launcher,
            media,
            renderEndpointProbe: _ => null,
            contextAdapter: legacy,
            snapshotContextAdapter: snapshot);
        List<object> events = [];
        List<byte[]> frames = [];
        DirectBridgeProtocolSession contextSession = new(
            "connection-snapshot-context",
            origin,
            store,
            coordinator,
            () => DateTimeOffset.UtcNow);
        _ = RequireSuccess(
            await SendAsync(
                contextSession,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "hello",
                    requestId = "request-snapshot-hello",
                    protocolVersion = 3,
                },
                events,
                frames).ConfigureAwait(false),
            "hello");
        JsonElement mode = RequireSuccess(
            await SendAsync(
                contextSession,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "context-mode",
                    requestId = "request-snapshot-mode",
                },
                events,
                frames).ConfigureAwait(false),
            "context-mode");
        string contextSessionId =
            "session-" + DirectBase64Url.Encode(
                Enumerable.Range(64, 16)
                    .Select(value => (byte)value)
                    .ToArray());
        JsonElement opened = RequireSuccess(
            await SendAsync(
                contextSession,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "context-open",
                    requestId = "request-snapshot-open",
                    sessionId = contextSessionId,
                },
                events,
                frames).ConfigureAwait(false),
            "context-open");
        Require(
            mode.GetProperty("mode").GetString()
                == DirectContextDeliveryMode.SnapshotMcp
            && opened.GetProperty("state").GetString()
                == "context-only"
            && contextSession.Phase == DirectProtocolPhase.ContextOnly
            && launcher.EnsureRunningCount == 0
            && launcher.WaitReadyCount == 0
            && media.StartCount == 0
            && legacy.ForwardCount == 0,
            "direct-snapshot-context-open-has-no-app-audio-or-typist-side-effect",
            checks);

        string wrongContextSessionId =
            "session-" + DirectBase64Url.Encode(
                Enumerable.Range(65, 16)
                    .Select(value => (byte)value)
                    .ToArray());
        JsonElement mismatchedSession = await SendAsync(
            contextSession,
            new
            {
                contract = DirectBridgeContract.Contract,
                type = "active-reading",
                requestId = "request-active-reading-wrong-session",
                sessionId = wrongContextSessionId,
                activeContract =
                    FileDirectSnapshotContextAdapter
                        .ActiveReadingContract,
                active = new
                {
                    kind = "pdf",
                    file = "book.pdf",
                    title = "Test Book",
                    page = 5,
                    selectionState = "unknown",
                    selection = (string?)null,
                    observedAtEpochMs = 1_750_000_000_000,
                },
            },
            events,
            frames).ConfigureAwait(false);
        Require(
            !mismatchedSession.GetProperty("ok").GetBoolean()
            && mismatchedSession.GetProperty("error")
                .GetProperty("code").GetString()
                == "BW_READER_CONTEXT_SNAPSHOT_SESSION_MISMATCH",
            "direct-snapshot-context-only-session-is-bound",
            checks);

        JsonElement activeAck = RequireSuccess(
            await SendAsync(
                contextSession,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "active-reading",
                    requestId = "request-active-reading-5",
                    sessionId = contextSessionId,
                    activeContract =
                        FileDirectSnapshotContextAdapter
                            .ActiveReadingContract,
                    active = new
                    {
                        kind = "pdf",
                        file = "book.pdf",
                        title = "Test Book",
                        page = 5,
                        selectionState = "active",
                        selection = "selected words",
                        observedAtEpochMs = 1_750_000_000_000,
                        viewFile = "vbook:g_test",
                        viewPage = 105,
                    },
                },
                events,
                frames).ConfigureAwait(false),
            "active-reading");
        DirectActiveReading aliasedActiveReading =
            FileDirectSnapshotContextAdapter.ValidateActiveReading(
                JsonSerializer.SerializeToElement(new
                {
                    kind = "pdf",
                    file = "books/part-2.pdf",
                    title = "Merged Book",
                    page = 7,
                    selectionState = "unknown",
                    selection = (string?)null,
                    observedAtEpochMs = 1_750_000_000_500,
                    viewFile = "vbook:g_book",
                    viewPage = 31,
                }));
        bool incompleteViewAliasRejected = false;
        try
        {
            _ = FileDirectSnapshotContextAdapter.ValidateActiveReading(
                JsonSerializer.SerializeToElement(new
                {
                    kind = "pdf",
                    file = "books/part-2.pdf",
                    title = "Merged Book",
                    page = 7,
                    selectionState = "unknown",
                    selection = (string?)null,
                    observedAtEpochMs = 1_750_000_000_500,
                    viewFile = "vbook:g_book",
                }));
        }
        catch (DirectProtocolException exception)
        {
            incompleteViewAliasRejected =
                exception.Code
                    == "BW_READER_ACTIVE_READING_SCHEMA_INVALID";
        }
        Require(
            aliasedActiveReading.File == "books/part-2.pdf"
            && aliasedActiveReading.Page.GetInt32() == 7
            && aliasedActiveReading.ViewFile == "vbook:g_book"
            && aliasedActiveReading.ViewPage?.GetInt32() == 31
            && incompleteViewAliasRejected,
            "direct-active-reading-view-alias-is-paired-and-canonical",
            checks);
        JsonElement contextAck = RequireSuccess(
            await SendAsync(
                contextSession,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "context",
                    requestId = "request-snapshot-page-5",
                    sessionId = contextSessionId,
                    contextContract =
                        NamedPipeDirectContextAdapter.ContextContract,
                    @event = new
                    {
                        v = 1,
                        seq = 5,
                        type = "page.context",
                        ts = 1_750_000_001,
                        id = "0000000000000005",
                        kind = "pdf",
                        file = "book.pdf",
                        title = "Test Book",
                        page = 5,
                        stable = true,
                        page_context = new
                        {
                            reason = "stable",
                            text = "Windows local snapshot text",
                            text_available = true,
                            text_source = "pdf-text",
                            truncated = false,
                        },
                    },
                },
                events,
                frames).ConfigureAwait(false),
            "context");
        JsonElement duplicateAck = RequireSuccess(
            await SendAsync(
                contextSession,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "context",
                    requestId = "request-snapshot-page-5-retry",
                    sessionId = contextSessionId,
                    contextContract =
                        NamedPipeDirectContextAdapter.ContextContract,
                    @event = new
                    {
                        v = 1,
                        seq = 5,
                        type = "page.context",
                        ts = 1_750_000_001,
                        id = "0000000000000005",
                        kind = "pdf",
                        file = "book.pdf",
                        title = "Test Book",
                        page = 5,
                        stable = true,
                        page_context = new
                        {
                            reason = "stable",
                            text = "Windows local snapshot text",
                            text_available = true,
                            text_source = "pdf-text",
                            truncated = false,
                        },
                    },
                },
                events,
                frames).ConfigureAwait(false),
            "context");
        using JsonDocument snapshotDocument = JsonDocument.Parse(
            await File.ReadAllTextAsync(snapshotPath)
                .ConfigureAwait(false));
        JsonElement snapshotRoot = snapshotDocument.RootElement;
        Require(
            activeAck.GetProperty("revision").GetInt64() == 1
            && contextAck.GetProperty("outcome").GetString()
                == "accepted"
            && duplicateAck.GetProperty("outcome").GetString()
                == "duplicate"
            && snapshotRoot.GetProperty("schema").GetString()
                == FileDirectSnapshotContextAdapter.SnapshotContract
            && snapshotRoot.GetProperty("revision").GetInt64() == 2
            && snapshotRoot.GetProperty("contextStatus").GetString()
                == "ready"
            && snapshotRoot.GetProperty("activeReading")
                .GetProperty("receivedAtEpochMs").GetInt64()
                == 1_750_000_005_000
            && snapshotRoot.GetProperty("activeReading")
                .GetProperty("file").GetString() == "book.pdf"
            && snapshotRoot.GetProperty("activeReading")
                .GetProperty("page").GetInt32() == 5
            && snapshotRoot.GetProperty("activeReading")
                .GetProperty("viewFile").GetString()
                == "vbook:g_test"
            && snapshotRoot.GetProperty("activeReading")
                .GetProperty("viewPage").GetInt32() == 105
            && snapshotRoot.GetProperty("currentPage")
                .GetProperty("text").GetString()
                == "Windows local snapshot text"
            && snapshotRoot.GetProperty("selection")
                .GetProperty("state").GetString() == "active"
            && snapshotRoot.GetProperty("selection")
                .GetProperty("text").GetString() == "selected words"
            && legacy.ForwardCount == 0,
            "direct-snapshot-events-atomically-fold-latest-without-legacy-injection",
            checks);

        JsonElement clearedAck = RequireSuccess(
            await SendAsync(
                contextSession,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "active-reading",
                    requestId = "request-active-reading-5-cleared",
                    sessionId = contextSessionId,
                    activeContract =
                        FileDirectSnapshotContextAdapter
                            .ActiveReadingContract,
                    active = new
                    {
                        kind = "pdf",
                        file = "book.pdf",
                        title = "Test Book",
                        page = 5,
                        selectionState = "cleared",
                        selection = (string?)null,
                        observedAtEpochMs = 1_750_000_002_000,
                    },
                },
                events,
                frames).ConfigureAwait(false),
            "active-reading");
        using JsonDocument clearedDocument = JsonDocument.Parse(
            await File.ReadAllTextAsync(snapshotPath)
                .ConfigureAwait(false));
        Require(
            clearedAck.GetProperty("revision").GetInt64() == 3
            && clearedDocument.RootElement.GetProperty("selection")
                .GetProperty("state").GetString() == "cleared"
            && clearedDocument.RootElement.GetProperty("selection")
                .GetProperty("text").ValueKind == JsonValueKind.Null,
            "direct-snapshot-active-reading-clears-stale-selection",
            checks);

        DirectBridgeProtocolSession voiceSession = new(
            "connection-snapshot-voice",
            origin,
            store,
            coordinator,
            () => DateTimeOffset.UtcNow);
        _ = RequireSuccess(
            await SendAsync(
                voiceSession,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "hello",
                    requestId = "request-snapshot-voice-hello",
                    protocolVersion = 3,
                },
                events,
                frames).ConfigureAwait(false),
            "hello");
        string voiceSessionId =
            "session-" + DirectBase64Url.Encode(
                Enumerable.Range(80, 16)
                    .Select(value => (byte)value)
                    .ToArray());
        _ = RequireSuccess(
            await SendAsync(
                voiceSession,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "start",
                    requestId = "request-snapshot-voice-start",
                    sessionId = voiceSessionId,
                },
                events,
                frames).ConfigureAwait(false),
            "start");
        Require(
            media.StartCount == 1
            && media.LastStartRequest?.StartTypist == false
            && legacy.ForwardCount == 0,
            "direct-snapshot-audio-start-explicitly-skips-voice-typist",
            checks);
        _ = RequireSuccess(
            await SendAsync(
                voiceSession,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "stop",
                    requestId = "request-snapshot-voice-stop",
                    sessionId = voiceSessionId,
                },
                events,
                frames).ConfigureAwait(false),
            "stop");

        JsonElement clearAck = RequireSuccess(
            await SendAsync(
                contextSession,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "context-clear",
                    requestId = "request-snapshot-clear",
                    sessionId = contextSessionId,
                },
                events,
                frames).ConfigureAwait(false),
            "context-clear");
        using JsonDocument clearedSnapshot = JsonDocument.Parse(
            await File.ReadAllTextAsync(snapshotPath)
                .ConfigureAwait(false));
        Require(
            clearAck.GetProperty("outcome").GetString() == "accepted"
            && clearedSnapshot.RootElement
                .GetProperty("contextStatus").GetString() == "pending"
            && clearedSnapshot.RootElement
                .GetProperty("currentPage").ValueKind
                == JsonValueKind.Null
            && clearedSnapshot.RootElement
                .GetProperty("selection")
                .GetProperty("state").GetString() == "unknown",
            "direct-snapshot-clear-removes-page-and-selection",
            checks);
    }

    private static async Task CheckContextDeliveryModeSetAsync(
        string root,
        string origin,
        ICollection<string> checks)
    {
        string installationRoot = System.IO.Path.Combine(
            root,
            "context-mode-set");
        string configPath = System.IO.Path.Combine(
            installationRoot,
            "native-host",
            "direct.json");
        string statusPath = System.IO.Path.Combine(
            installationRoot,
            "runtime",
            "computer-voice-direct.status.json");
        await WriteConfigAsync(
            configPath,
            statusPath,
            origin,
            localOptIn: true).ConfigureAwait(false);

        DirectBridgeConfigStore store = new(configPath);
        DirectBridgeConfig initial = store.Load();
        FakeDirectAppLauncher launcher = new();
        FakeDirectMediaAdapter media = new();
        await using DirectBridgeCoordinator coordinator = new(
            store,
            launcher,
            media,
            renderEndpointProbe: _ => null);
        List<object> events = [];
        List<byte[]> frames = [];
        string modeSessionId =
            "session-" + DirectBase64Url.Encode(
                Enumerable.Range(96, 16)
                    .Select(value => (byte)value)
                    .ToArray());

        DirectBridgeProtocolSession setter = new(
            "connection-context-mode-set",
            origin,
            store,
            coordinator);
        _ = RequireSuccess(
            await SendAsync(
                setter,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "hello",
                    requestId = "request-mode-set-hello",
                    protocolVersion = 3,
                },
                events,
                frames).ConfigureAwait(false),
            "hello");
        JsonElement changed = RequireSuccess(
            await SendAsync(
                setter,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "context-mode-set",
                    requestId = "request-mode-set-snapshot",
                    mode = DirectContextDeliveryMode.SnapshotMcp,
                    sessionId = modeSessionId,
                },
                events,
                frames).ConfigureAwait(false),
            "context-mode-set");
        DirectBridgeConfig snapshotConfig = store.Load();
        Require(
            changed.GetProperty("mode").GetString()
                == DirectContextDeliveryMode.SnapshotMcp
            && changed.GetProperty("previousMode").GetString()
                == DirectContextDeliveryMode.LegacyInject
            && snapshotConfig.ContextDeliveryMode
                == DirectContextDeliveryMode.SnapshotMcp
            && snapshotConfig.VirtualMicrophoneRenderEndpointId
                == initial.VirtualMicrophoneRenderEndpointId
            && snapshotConfig.VirtualSpeakerRenderEndpointId
                == initial.VirtualSpeakerRenderEndpointId
            && snapshotConfig.AllowedOrigins.SetEquals(
                initial.AllowedOrigins)
            && launcher.EnsureRunningCount == 0
            && launcher.WaitReadyCount == 0
            && media.StartCount == 0,
            "direct-context-mode-set-persists-atomically-without-side-effects",
            checks);

        DirectBridgeProtocolSession contextOnly = new(
            "connection-context-mode-open",
            origin,
            store,
            coordinator);
        _ = RequireSuccess(
            await SendAsync(
                contextOnly,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "hello",
                    requestId = "request-mode-open-hello",
                    protocolVersion = 3,
                },
                events,
                frames).ConfigureAwait(false),
            "hello");
        _ = RequireSuccess(
            await SendAsync(
                contextOnly,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "context-open",
                    requestId = "request-mode-open",
                    sessionId = modeSessionId,
                },
                events,
                frames).ConfigureAwait(false),
            "context-open");
        JsonElement busy = await SendAsync(
            contextOnly,
            new
            {
                contract = DirectBridgeContract.Contract,
                type = "context-mode-set",
                requestId = "request-mode-set-busy",
                mode = DirectContextDeliveryMode.LegacyInject,
                sessionId = modeSessionId,
            },
            events,
            frames).ConfigureAwait(false);
        Require(
            !busy.GetProperty("ok").GetBoolean()
            && busy.GetProperty("error").GetProperty("code")
                .GetString()
                == "BW_READER_CONTEXT_DELIVERY_MODE_BUSY"
            && store.Load().ContextDeliveryMode
                == DirectContextDeliveryMode.SnapshotMcp,
            "direct-context-mode-set-rejects-context-only-owner",
            checks);

        DirectBridgeProtocolSession rollback = new(
            "connection-context-mode-rollback",
            origin,
            store,
            coordinator);
        _ = RequireSuccess(
            await SendAsync(
                rollback,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "hello",
                    requestId = "request-mode-rollback-hello",
                    protocolVersion = 3,
                },
                events,
                frames).ConfigureAwait(false),
            "hello");
        JsonElement invalid = await SendAsync(
            rollback,
            new
            {
                contract = DirectBridgeContract.Contract,
                type = "context-mode-set",
                requestId = "request-mode-set-invalid",
                mode = "invalid-mode",
                sessionId = modeSessionId,
            },
            events,
            frames).ConfigureAwait(false);
        JsonElement restored = RequireSuccess(
            await SendAsync(
                rollback,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "context-mode-set",
                    requestId = "request-mode-set-legacy",
                    mode = DirectContextDeliveryMode.LegacyInject,
                    sessionId = modeSessionId,
                },
                events,
                frames).ConfigureAwait(false),
            "context-mode-set");
        DirectBridgeProtocolSession observer = new(
            "connection-context-mode-observer",
            origin,
            store,
            coordinator);
        _ = RequireSuccess(
            await SendAsync(
                observer,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "hello",
                    requestId = "request-mode-observer-hello",
                    protocolVersion = 3,
                },
                events,
                frames).ConfigureAwait(false),
            "hello");
        JsonElement observed = RequireSuccess(
            await SendAsync(
                observer,
                new
                {
                    contract = DirectBridgeContract.Contract,
                    type = "context-mode",
                    requestId = "request-mode-observer",
                },
                events,
                frames).ConfigureAwait(false),
            "context-mode");
        Require(
            !invalid.GetProperty("ok").GetBoolean()
            && invalid.GetProperty("error").GetProperty("code")
                .GetString()
                == "BW_READER_CONTEXT_DELIVERY_MODE_INVALID"
            && restored.GetProperty("mode").GetString()
                == DirectContextDeliveryMode.LegacyInject
            && restored.GetProperty("previousMode").GetString()
                == DirectContextDeliveryMode.SnapshotMcp
            && observed.GetProperty("mode").GetString()
                == DirectContextDeliveryMode.LegacyInject
            && store.Load().ContextDeliveryMode
                == DirectContextDeliveryMode.LegacyInject,
            "direct-context-mode-set-validates-and-persists-across-connections",
            checks);
    }

    private static async Task CheckReaderContextMcpProtocolAsync(
        string root,
        ICollection<string> checks)
    {
        string snapshotPath = System.IO.Path.Combine(
            root,
            "mcp-protocol",
            FileDirectSnapshotContextAdapter.SnapshotFileName);
        FileDirectSnapshotContextAdapter adapter = new(
            snapshotPath,
            () => DateTimeOffset.FromUnixTimeMilliseconds(
                1_750_000_000_500));
        JsonElement activeValue = JsonSerializer.SerializeToElement(new
        {
            kind = "pdf",
            file = "mcp-book.pdf",
            title = "MCP Book",
            page = 12,
            selectionState = "unknown",
            selection = (string?)null,
            observedAtEpochMs = 1_750_000_000_000,
        });
        _ = await adapter.ForwardActiveReadingAsync(
            "request-mcp-active",
            "session-" + DirectBase64Url.Encode(
                Enumerable.Range(96, 16)
                    .Select(value => (byte)value)
                    .ToArray()),
            FileDirectSnapshotContextAdapter.ValidateActiveReading(
                activeValue),
            CancellationToken.None).ConfigureAwait(false);

        string input = string.Join(
            "\n",
            JsonSerializer.Serialize(new
            {
                jsonrpc = "2.0",
                id = 1,
                method = "initialize",
                @params = new
                {
                    protocolVersion = "2025-06-18",
                    capabilities = new { },
                    clientInfo = new
                    {
                        name = "self-test",
                        version = "1",
                    },
                },
            }),
            JsonSerializer.Serialize(new
            {
                jsonrpc = "2.0",
                method = "notifications/initialized",
            }),
            JsonSerializer.Serialize(new
            {
                jsonrpc = "2.0",
                id = 2,
                method = "tools/list",
                @params = new { },
            }),
            JsonSerializer.Serialize(new
            {
                jsonrpc = "2.0",
                id = 3,
                method = "tools/call",
                @params = new
                {
                    name = ReaderContextMcpServer.ToolName,
                    arguments = new { },
                },
            }),
            JsonSerializer.Serialize(new
            {
                jsonrpc = "2.0",
                id = 4,
                method = "tools/call",
                @params = new
                {
                    name = ReaderContextMcpServer.ToolName,
                    arguments = new { },
                },
            }),
            "");
        StringWriter output = new();
        ReaderContextMcpServer server = new(
            snapshotPath,
            new StringReader(input),
            output,
            utcNow: () => DateTimeOffset.FromUnixTimeMilliseconds(
                1_750_000_010_000),
            instanceId: "mcp-self-test-instance");
        int exit = await server.RunAsync(CancellationToken.None)
            .ConfigureAwait(false);
        JsonDocument[] responses = output.ToString()
            .Split(
                new[] { "\r\n", "\n" },
                StringSplitOptions.RemoveEmptyEntries)
            .Select(value => JsonDocument.Parse(value))
            .ToArray();
        try
        {
            JsonElement tools = responses[1].RootElement
                .GetProperty("result")
                .GetProperty("tools");
            string firstText = responses[2].RootElement
                .GetProperty("result")
                .GetProperty("content")[0]
                .GetProperty("text")
                .GetString()!;
            string secondText = responses[3].RootElement
                .GetProperty("result")
                .GetProperty("content")[0]
                .GetProperty("text")
                .GetString()!;
            using JsonDocument first = JsonDocument.Parse(firstText);
            using JsonDocument second = JsonDocument.Parse(secondText);
            Require(
                exit == 0
                && responses.Length == 4
                && responses[0].RootElement.GetProperty("result")
                    .GetProperty("serverInfo")
                    .GetProperty("name").GetString()
                    == ReaderContextMcpServer.ServerName
                && tools.GetArrayLength() == 1
                && tools[0].GetProperty("name").GetString()
                    == ReaderContextMcpServer.ToolName
                && first.RootElement.GetProperty("schema").GetString()
                    == FileDirectSnapshotContextAdapter.SnapshotContract
                && first.RootElement.GetProperty("contextStatus")
                    .GetString() == "pending"
                && first.RootElement.GetProperty("mcp")
                    .GetProperty("instanceId").GetString()
                    == "mcp-self-test-instance"
                && second.RootElement.GetProperty("mcp")
                    .GetProperty("instanceId").GetString()
                    == "mcp-self-test-instance"
                && first.RootElement.GetProperty("mcp")
                    .GetProperty("callSequence").GetInt64() == 1
                && second.RootElement.GetProperty("mcp")
                    .GetProperty("callSequence").GetInt64() == 2,
                "direct-reader-context-mcp-is-persistent-read-only-single-tool",
                checks);
        }
        finally
        {
            foreach (JsonDocument response in responses)
            {
                response.Dispose();
            }
        }

        JsonObject stale = new()
        {
            ["activeReading"] = new JsonObject
            {
                ["file"] = "old.pdf",
                ["title"] = "Old",
                ["page"] = 3,
                ["observedAtEpochMs"] = long.MaxValue,
                ["receivedAtEpochMs"] = 1_000L,
                ["fresh"] = true,
            },
            ["contextStatus"] = "ready",
            ["currentPage"] = new JsonObject
            {
                ["file"] = "old.pdf",
                ["page"] = 3,
                ["stable"] = true,
                ["text"] = "must not leak",
                ["textAvailable"] = true,
            },
            ["selection"] = new JsonObject
            {
                ["state"] = "active",
                ["text"] = "stale selection",
            },
        };
        ReaderContextMcpServer.ApplyFreshness(
            stale,
            DateTimeOffset.FromUnixTimeMilliseconds(
                1_000
                + (long)ReaderContextMcpServer
                    .FreshnessWindow.TotalMilliseconds
                + 1));
        Require(
            stale["contextStatus"]?.GetValue<string>() == "stale"
            && stale["currentPage"]?["text"]?.GetValue<string>() == ""
            && stale["selection"]?["state"]?.GetValue<string>()
                == "unknown",
            "direct-reader-context-mcp-never-returns-stale-page-or-selection-text",
            checks);

        JsonObject drawingFreshness = new()
        {
            ["activeReading"] = new JsonObject
            {
                ["file"] = "drawing.pdf",
                ["page"] = 1,
                ["receivedAtEpochMs"] = 1_000_000L,
                ["fresh"] = true,
            },
            ["contextStatus"] = "ready",
            ["currentPage"] = new JsonObject
            {
                ["file"] = "drawing.pdf",
                ["page"] = 1,
                ["stable"] = true,
                ["text"] = "x",
                ["textAvailable"] = true,
                ["visual"] = new JsonObject
                {
                    ["drawing"] = new JsonObject
                    {
                        ["empty"] = false,
                        ["lastEditedAt"] = 1000.0,
                        ["freshWindowS"] = 120.0,
                        ["freshness"] = "recent",
                    },
                },
            },
        };
        ReaderContextMcpServer.ApplyFreshness(
            drawingFreshness,
            DateTimeOffset.FromUnixTimeMilliseconds(
                1_120_000));
        string? atBoundary = drawingFreshness["currentPage"]
            ?["visual"]?["drawing"]?["freshness"]
            ?.GetValue<string>();
        ReaderContextMcpServer.ApplyFreshness(
            drawingFreshness,
            DateTimeOffset.FromUnixTimeMilliseconds(
                1_120_001));
        Require(
            atBoundary == "recent"
            && drawingFreshness["currentPage"]
                ?["visual"]?["drawing"]?["freshness"]
                ?.GetValue<string>() == "stale",
            "direct-reader-context-mcp-recomputes-drawing-freshness",
            checks);
    }

    private static async Task CheckSnapshotMetadataFoldAsync(
        string root,
        ICollection<string> checks)
    {
        string snapshotPath = System.IO.Path.Combine(
            root,
            "snapshot-metadata",
            FileDirectSnapshotContextAdapter.SnapshotFileName);
        FileDirectSnapshotContextAdapter adapter = new(
            snapshotPath,
            () => DateTimeOffset.FromUnixTimeMilliseconds(
                1_750_000_005_000));
        string sessionId =
            "session-" + DirectBase64Url.Encode(
                Enumerable.Range(112, 16)
                    .Select(value => (byte)value)
                    .ToArray());
        JsonElement activeValue = JsonSerializer.SerializeToElement(new
        {
            kind = "epub",
            file = "book.epub",
            title = "Metadata Book",
            page = 3,
            selectionState = "unknown",
            selection = (string?)null,
            observedAtEpochMs = 1_750_000_000_000,
        });
        _ = await adapter.ForwardActiveReadingAsync(
            "request-metadata-active",
            sessionId,
            FileDirectSnapshotContextAdapter.ValidateActiveReading(
                activeValue),
            CancellationToken.None).ConfigureAwait(false);

        JsonElement pageValue = JsonSerializer.SerializeToElement(new
        {
            v = 1,
            seq = 1,
            type = "page.context",
            ts = 1_750_000_001,
            id = "0000000000000101",
            kind = "epub",
            file = "book.epub",
            title = "Metadata Book",
            page = 3,
            stable = true,
            page_context = new
            {
                reason = "dwell",
                text =
                    "日本の歴史と食文化 1 P\n"
                    + "A\nR\nT\n1\n食\n文\n化\n概\n論\n"
                    + "||||||\n"
                    + "4 縄文~平安時代\n"
                    + "f] l l l / A ] ] ] [ ] [ [ [ []]]]]] [[[[\n"
                    + "正文\\⟦HIGHLIGHT literal\\⟧"
                    + "⟦HIGHLIGHT color=\"yellow\""
                    + " note=\"重点\"⟧高亮内容⟦/HIGHLIGHT⟧"
                    + "和图像引用必须同时可见"
                    + "⟦CARD_START type=\"note\"⟧卡片内容"
                    + "⟦CARD_END⟧",
                text_available = true,
                text_source = "epub:viewport",
                fallback_reason = (string?)null,
                truncated = false,
                visual = new
                {
                    page_image =
                        "/pdf/api/page-image?file=book.epub&page=3",
                    has_ink = true,
                    unknownVisual = "drop-me",
                    drawing = new
                    {
                        contract = "reader-outgoing-context/1",
                        file = "book.epub",
                        page = "3",
                        freshness = "recent",
                        lastEditedAt = 1_750_000_000.0,
                        freshWindowS = 120.0,
                        inProgress = false,
                        stable = true,
                        drawingRevision = "dr_0123456789abcdef",
                        pendingSince = (double?)null,
                        unknownDrawing = "drop-me",
                        @ref = new
                        {
                            kind = "drawing",
                            file = "book.epub",
                            page = "3",
                            revision = "dr_0123456789abcdef",
                            secret = "drop-me",
                        },
                        empty = false,
                    },
                },
                embeds = new
                {
                    highlights = 1,
                    blocks = 2,
                    error = "diagnostic-not-contract",
                    unanchored = new[]
                    {
                        new
                        {
                            id = "h1",
                            text = "未锚定\r\n原文\t尾",
                            color = "yellow",
                            note = "note\rline\tend",
                            kind = "highlight",
                            _reason = "not_found_in_page_text",
                            privateGeometry = "drop-me",
                        },
                    },
                },
                viewport = new
                {
                    center = 7,
                    @from = 1,
                    to = 14,
                    total = 20,
                    pad = 6,
                    unknownViewport = "drop-me",
                },
            },
        });
        _ = await adapter.ForwardJournalAsync(
            "request-metadata-page",
            sessionId,
            new DirectContextEvent(
                1,
                "page.context",
                "0000000000000101",
                pageValue),
            CancellationToken.None).ConfigureAwait(false);

        using JsonDocument folded = JsonDocument.Parse(
            await File.ReadAllTextAsync(snapshotPath)
                .ConfigureAwait(false));
        JsonElement page = folded.RootElement.GetProperty(
            "currentPage");
        JsonElement visual = page.GetProperty("visual");
        JsonElement drawing = visual.GetProperty("drawing");
        JsonElement embeds = page.GetProperty("embeds");
        JsonElement viewport = page.GetProperty("viewport");
        string markdownPath = DirectSnapshotMarkdown.PathFor(
            snapshotPath);
        string markdown = await File.ReadAllTextAsync(markdownPath)
            .ConfigureAwait(false);
        JsonObject foldedObject = JsonNode.Parse(
            folded.RootElement.GetRawText()) as JsonObject
            ?? throw new InvalidOperationException(
                "snapshot metadata projection root invalid");
        string renderedMarkdown =
            DirectSnapshotMarkdown.Render(foldedObject);
        string terminal = DirectSnapshotTerminal.Render(
            foldedObject,
            new CodexVoiceHistorySnapshot(
                CodexVoiceHistoryBindingStatus.Bound,
                "11111111-2222-3333-4444-555555555555",
                BindingVersion: 9,
                CodexVoiceHistoryDataStatus.Available,
                [
                    new("user", "语音层问题"),
                    new("assistant", "语音层回答"),
                ],
                CodexVoiceHistoryDataStatus.Available,
                [
                    new("user", "完整线程问题"),
                    new("assistant", "完整线程最终回答"),
                ],
                Gap: false));
        DirectSnapshotTerminal.ReaderTextProjection markerProjection =
            DirectSnapshotTerminal.ParseAnnotatedReaderText(
                "A\\⟦HIGHLIGHT literal\\⟧"
                + "⟦HIGHLIGHT color=\"yellow\"⟧B"
                + "⟦/HIGHLIGHT⟧C"
                + "⟦CARD_START type=\"note\"⟧D"
                + "⟦CARD_END⟧E");
        string scannerProjection =
            DirectSnapshotTerminal.ReadableReaderText(
                "日本の歴史と食文化 1 P\n"
                + "A\nR\nT\n1\n食\n文\n化\n概\n論\n"
                + "||||||\n"
                + "4 縄文~平安時代\n"
                + "f] l l l / A ] ] ] [ ] [ [ [ []]]]]] [[[[\n"
                + "公式の変数は改行を保持する:\n"
                + "x\ny\nz\nw\n"
                + "X\nY\nZ\nW\n"
                + "f(x) = [a/b]\n"
                + "int[] values = [1, 2];");
        bool danglingEscapeRejected = false;
        try
        {
            _ = DirectSnapshotTerminal.PlainReaderText("bad\\");
        }
        catch (InvalidOperationException exception)
            when (exception.Message.Contains(
                "BW_READER_CONTEXT_MARK_ESCAPE_INVALID",
                StringComparison.Ordinal))
        {
            danglingEscapeRejected = true;
        }
        Require(
            scannerProjection.Contains(
                "日本の歴史と食文化 1 PART1 食文化概論",
                StringComparison.Ordinal)
            && !scannerProjection.Contains(
                "||||||",
                StringComparison.Ordinal)
            && !scannerProjection.Contains(
                "f] l l l / A",
                StringComparison.Ordinal)
            && scannerProjection.Contains(
                "x\ny\nz\nw",
                StringComparison.Ordinal)
            && scannerProjection.Contains(
                "X\nY\nZ\nW",
                StringComparison.Ordinal)
            && scannerProjection.Contains(
                "f(x) = [a/b]",
                StringComparison.Ordinal)
            && scannerProjection.Contains(
                "int[] values = [1, 2];",
                StringComparison.Ordinal),
            "direct-snapshot-presentation-denoise-is-conservative",
            checks);
        Require(
            renderedMarkdown.Contains(
                "日本の歴史と食文化 1 PART1 食文化概論",
                StringComparison.Ordinal),
            "direct-snapshot-markdown-render-joins-stacked-title",
            checks);
        Require(
            markdown.Contains(
                "日本の歴史と食文化 1 PART1 食文化概論",
                StringComparison.Ordinal),
            "direct-snapshot-markdown-joins-stacked-title",
            checks);
        Require(
            !markdown.Contains(
                "||||||",
                StringComparison.Ordinal)
            && !markdown.Contains(
                "f] l l l / A",
                StringComparison.Ordinal),
            "direct-snapshot-markdown-hides-extraction-debris",
            checks);
        Require(
            markdown.Contains(
                "### 协议高亮正文",
                StringComparison.Ordinal)
            && markdown.Contains(
                "### 协议卡片与便签",
                StringComparison.Ordinal)
            && markdown.Contains(
                "高亮内容",
                StringComparison.Ordinal)
            && markdown.Contains(
                "卡片内容",
                StringComparison.Ordinal),
            "direct-snapshot-markdown-renders-mark-sections",
            checks);
        Require(
            !markdown.Contains(
                "⟦HIGHLIGHT color=",
                StringComparison.Ordinal)
            && !markdown.Contains(
                "⟦CARD_START",
                StringComparison.Ordinal),
            "direct-snapshot-markdown-hides-protocol-markers",
            checks);
        Require(
            markdown.Contains(
                "日本の歴史と食文化 1 PART1 食文化概論",
                StringComparison.Ordinal)
            && !markdown.Contains(
                "||||||",
                StringComparison.Ordinal)
            && !markdown.Contains(
                "f] l l l / A",
                StringComparison.Ordinal)
            && markdown.Contains(
                "### 协议高亮正文",
                StringComparison.Ordinal)
            && markdown.Contains(
                "### 协议卡片与便签",
                StringComparison.Ordinal)
            && markdown.Contains(
                "高亮内容",
                StringComparison.Ordinal)
            && markdown.Contains(
                "卡片内容",
                StringComparison.Ordinal)
            && !markdown.Contains(
                "⟦HIGHLIGHT color=",
                StringComparison.Ordinal)
            && !markdown.Contains(
                "⟦CARD_START",
                StringComparison.Ordinal),
            "direct-snapshot-markdown-expands-reader-body-and-marks",
            checks);
        Require(
            terminal.Contains(
                "日本の歴史と食文化 1 PART1 食文化概論",
                StringComparison.Ordinal)
            && !terminal.Contains(
                "||||||",
                StringComparison.Ordinal)
            && !terminal.Contains(
                "f] l l l / A",
                StringComparison.Ordinal)
            && terminal.Contains(
                "【高亮与嵌入内容】",
                StringComparison.Ordinal)
            && terminal.Contains(
                "高亮内容",
                StringComparison.Ordinal)
            && terminal.Contains(
                "卡片内容",
                StringComparison.Ordinal),
            "direct-snapshot-terminal-expands-reader-body-and-marks",
            checks);
        Require(
            page.GetProperty("kind").GetString() == "epub"
            && page.GetProperty("fallbackReason").ValueKind
                == JsonValueKind.Null
            && visual.GetProperty("has_ink").GetBoolean()
            && !visual.TryGetProperty("unknownVisual", out _)
            && drawing.GetProperty("freshness").GetString()
                == "recent"
            && drawing.GetProperty("page").GetInt32() == 3
            && !drawing.TryGetProperty("unknownDrawing", out _)
            && !drawing.GetProperty("ref")
                .TryGetProperty("secret", out _)
            && embeds.GetProperty("highlights").GetInt64() == 1
            && !embeds.TryGetProperty("error", out _)
            && !embeds.GetProperty("unanchored")[0]
                .TryGetProperty("privateGeometry", out _)
            && embeds.GetProperty("unanchored")[0]
                .GetProperty("text").GetString()
                == "未锚定\n原文    尾"
            && embeds.GetProperty("unanchored")[0]
                .GetProperty("note").GetString()
                == "note\nline    end"
            && viewport.GetProperty("from").GetInt64() == 1
            && viewport.GetProperty("to").GetInt64() == 14
            && !viewport.TryGetProperty(
                "unknownViewport",
                out _)
            && page.GetProperty("text").GetString()!.Contains(
                "||||||",
                StringComparison.Ordinal)
            && page.GetProperty("text").GetString()!.Contains(
                "⟦HIGHLIGHT color=",
                StringComparison.Ordinal)
            && markdown.Contains(
                "高亮内容",
                StringComparison.Ordinal)
            && markdown.Contains(
                "日本の歴史と食文化 1 PART1 食文化概論",
                StringComparison.Ordinal)
            && !markdown.Contains(
                "||||||",
                StringComparison.Ordinal)
            && !markdown.Contains(
                "f] l l l / A",
                StringComparison.Ordinal)
            && markdown.Contains(
                "图像引用必须同时可见",
                StringComparison.Ordinal)
            && markdown.Contains(
                "### 协议高亮正文",
                StringComparison.Ordinal)
            && markdown.Contains(
                "### 协议卡片与便签",
                StringComparison.Ordinal)
            && markdown.Contains(
                "卡片内容",
                StringComparison.Ordinal)
            && markdown.Contains(
                "在 Reader 中打开原图",
                StringComparison.Ordinal)
            && markdown.Contains(
                "不会携带跨站登录 Cookie",
                StringComparison.Ordinal)
            && !markdown.Contains(
                "![当前页图]",
                StringComparison.Ordinal)
            && markdown.Contains(
                "⟦HIGHLIGHT literal⟧",
                StringComparison.Ordinal)
            && !markdown.Contains(
                "⟦HIGHLIGHT color=",
                StringComparison.Ordinal)
            && !markdown.Contains(
                "⟦CARD_START",
                StringComparison.Ordinal)
            && markdown.Contains(
                "dr_0123456789abcdef",
                StringComparison.Ordinal)
            && terminal.Contains(
                "【当前页正文】",
                StringComparison.Ordinal)
            && terminal.Contains(
                "【高亮与嵌入内容】",
                StringComparison.Ordinal)
            && terminal.Contains(
                "高亮内容",
                StringComparison.Ordinal)
            && terminal.Contains(
                "日本の歴史と食文化 1 PART1 食文化概論",
                StringComparison.Ordinal)
            && !terminal.Contains(
                "||||||",
                StringComparison.Ordinal)
            && !terminal.Contains(
                "f] l l l / A",
                StringComparison.Ordinal)
            && terminal.Contains(
                "https://bwicarus.taile44d0c.ts.net/pdf/api/page-image",
                StringComparison.Ordinal)
            && terminal.Contains(
                "【语音层最近对话】",
                StringComparison.Ordinal)
            && terminal.Contains(
                "用户：语音层问题",
                StringComparison.Ordinal)
            && terminal.Contains(
                "【Codex 线程历史】",
                StringComparison.Ordinal)
            && terminal.Contains(
                "助手：完整线程最终回答",
                StringComparison.Ordinal)
            && markerProjection.PlainText
                == "A⟦HIGHLIGHT literal⟧BCDE"
            && markerProjection.Highlights.Count == 1
            && markerProjection.Highlights[0].Text == "B"
            && markerProjection.Cards.Count == 1
            && markerProjection.Cards[0].Text == "D"
            && scannerProjection.Contains(
                "日本の歴史と食文化 1 PART1 食文化概論",
                StringComparison.Ordinal)
            && !scannerProjection.Contains(
                "||||||",
                StringComparison.Ordinal)
            && !scannerProjection.Contains(
                "f] l l l / A",
                StringComparison.Ordinal)
            && scannerProjection.Contains(
                "x\ny\nz\nw",
                StringComparison.Ordinal)
            && scannerProjection.Contains(
                "X\nY\nZ\nW",
                StringComparison.Ordinal)
            && scannerProjection.Contains(
                "f(x) = [a/b]",
                StringComparison.Ordinal)
            && scannerProjection.Contains(
                "int[] values = [1, 2];",
                StringComparison.Ordinal)
            && DirectSnapshotTerminal.UnescapeReaderText("\\n")
                == "\\n"
            && DirectSnapshotTerminal.UnescapeReaderText(
                "\\\\⟦")
                == "\\⟦"
            && danglingEscapeRejected,
            "direct-snapshot-folds-whitelisted-metadata-and-live-presentations",
            checks);

        using DirectSnapshotViewer localViewer = new(
            snapshotPath,
            DirectBridgeContract.DefaultListenPort);
        DefaultHttpContext viewerContext = new();
        viewerContext.Request.Method = HttpMethods.Get;
        viewerContext.Request.Host = new HostString(
            DirectBridgeContract.ListenHost,
            DirectBridgeContract.DefaultListenPort);
        viewerContext.Connection.RemoteIpAddress =
            IPAddress.Loopback;
        await using MemoryStream viewerBody = new();
        viewerContext.Response.Body = viewerBody;
        await localViewer.HandleViewerAsync(viewerContext)
            .ConfigureAwait(false);
        string viewerHtml = Encoding.UTF8.GetString(
            viewerBody.ToArray());

        DefaultHttpContext snapshotContext = new();
        snapshotContext.Request.Method = HttpMethods.Get;
        snapshotContext.Request.Host = new HostString(
            DirectBridgeContract.ListenHost,
            DirectBridgeContract.DefaultListenPort);
        snapshotContext.Connection.RemoteIpAddress =
            IPAddress.Loopback;
        await using MemoryStream snapshotBody = new();
        snapshotContext.Response.Body = snapshotBody;
        await localViewer.HandleSnapshotAsync(snapshotContext)
            .ConfigureAwait(false);
        using JsonDocument liveProjection = JsonDocument.Parse(
            snapshotBody.ToArray());

        DefaultHttpContext foreignContext = new();
        foreignContext.Request.Method = HttpMethods.Get;
        foreignContext.Request.Host = new HostString(
            DirectBridgeContract.ListenHost,
            DirectBridgeContract.DefaultListenPort);
        foreignContext.Connection.RemoteIpAddress =
            IPAddress.Parse("100.64.0.10");
        await using MemoryStream foreignBody = new();
        foreignContext.Response.Body = foreignBody;
        await localViewer.HandleViewerAsync(foreignContext)
            .ConfigureAwait(false);
        ProcessStartInfo viewerStart =
            DirectSnapshotViewer.CreateEdgeStartInfo(
                @"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                "http://127.0.0.1:43128"
                    + DirectSnapshotViewer.ViewerPath,
                @"C:\runtime\reader-context-viewer-profile");
        string[] viewerArguments = viewerStart.ArgumentList.ToArray();
        Require(
            viewerContext.Response.StatusCode
                == StatusCodes.Status200OK
            && viewerHtml.Contains(
                DirectSnapshotViewer.SnapshotPath,
                StringComparison.Ordinal)
            && viewerHtml.Contains(
                "本地查看器的跨站内嵌请求可能没有登录 Cookie",
                StringComparison.Ordinal)
            && viewerHtml.Contains(
                "function parseReaderText",
                StringComparison.Ordinal)
            && snapshotContext.Response.StatusCode
                == StatusCodes.Status200OK
            && liveProjection.RootElement
                .GetProperty("contextStatus").GetString()
                == "stale"
            && !liveProjection.RootElement
                .GetProperty("currentPage")
                .GetProperty("textAvailable").GetBoolean()
            && foreignContext.Response.StatusCode
                == StatusCodes.Status403Forbidden
            && foreignBody.Length == 0
            && !viewerStart.UseShellExecute
            && viewerStart.FileName.EndsWith(
                @"Microsoft\Edge\Application\msedge.exe",
                StringComparison.Ordinal)
            && viewerArguments.Contains(
                "--app=http://127.0.0.1:43128"
                    + DirectSnapshotViewer.ViewerPath,
                StringComparer.Ordinal)
            && viewerArguments.Contains(
                @"--user-data-dir=C:\runtime\reader-context-viewer-profile",
                StringComparer.Ordinal)
            && viewerArguments.Contains(
                "--disable-extensions",
                StringComparer.Ordinal)
            && viewerArguments.Contains(
                "--disable-sync",
                StringComparer.Ordinal)
            && !viewerArguments.Contains(
                "--reader-context-view",
                StringComparer.Ordinal),
            "direct-snapshot-loopback-viewer-is-local-fresh-and-honest",
            checks);

        adapter = new FileDirectSnapshotContextAdapter(
            snapshotPath,
            () => DateTimeOffset.FromUnixTimeMilliseconds(
                1_750_000_006_000));
        _ = await adapter.ForwardActiveReadingAsync(
            "request-metadata-restart",
            sessionId,
            FileDirectSnapshotContextAdapter.ValidateActiveReading(
                activeValue),
            CancellationToken.None).ConfigureAwait(false);
        using (JsonDocument restored = JsonDocument.Parse(
            await File.ReadAllTextAsync(snapshotPath)
                .ConfigureAwait(false)))
        {
            JsonElement restoredPage = restored.RootElement
                .GetProperty("currentPage");
            Require(
                restoredPage.GetProperty("visual")
                    .GetProperty("drawing")
                    .GetProperty("drawingRevision").GetString()
                    == "dr_0123456789abcdef"
                && restoredPage.GetProperty("embeds")
                    .GetProperty("highlights").GetInt64() == 1
                && restoredPage.GetProperty("viewport")
                    .GetProperty("center").GetInt64() == 7,
                "direct-snapshot-restart-restores-only-validated-page-metadata",
                checks);
        }

        JsonElement leanPage = JsonSerializer.SerializeToElement(new
        {
            v = 1,
            seq = 2,
            type = "page.context",
            ts = 1_750_000_002,
            id = "0000000000000102",
            kind = "epub",
            file = "book.epub",
            title = "Metadata Book",
            page = 3,
            stable = true,
            page_context = new
            {
                reason = "dwell",
                text = "new page state",
                text_available = true,
                text_source = "epub:whole-section",
                fallback_reason = (string?)null,
                truncated = false,
            },
        });
        _ = await adapter.ForwardJournalAsync(
            "request-metadata-lean",
            sessionId,
            new DirectContextEvent(
                2,
                "page.context",
                "0000000000000102",
                leanPage),
            CancellationToken.None).ConfigureAwait(false);
        using JsonDocument lean = JsonDocument.Parse(
            await File.ReadAllTextAsync(snapshotPath)
                .ConfigureAwait(false));
        JsonElement leanCurrent = lean.RootElement.GetProperty(
            "currentPage");
        Require(
            !leanCurrent.TryGetProperty("visual", out _)
            && !leanCurrent.TryGetProperty("embeds", out _)
            && !leanCurrent.TryGetProperty("viewport", out _),
            "direct-snapshot-new-event-does-not-retain-optional-page-metadata",
            checks);

        long revisionBeforeInvalid = lean.RootElement
            .GetProperty("revision").GetInt64();
        JsonElement invalidDrawing = JsonSerializer.SerializeToElement(
            new
            {
                v = 1,
                seq = 3,
                type = "page.context",
                ts = 1_750_000_003,
                id = "0000000000000103",
                kind = "epub",
                file = "book.epub",
                title = "Metadata Book",
                page = 3,
                stable = true,
                page_context = new
                {
                    reason = "dwell",
                    text = "invalid must not commit",
                    text_available = true,
                    text_source = "epub:viewport",
                    fallback_reason = (string?)null,
                    truncated = false,
                    visual = new
                    {
                        page_image =
                            "/pdf/api/page-image?file=book.epub&page=3",
                        has_ink = true,
                        drawing = new
                        {
                            contract = "reader-outgoing-context/1",
                            file = "book.epub",
                            page = 3,
                            freshness = "stable",
                            lastEditedAt = 1_750_000_002.0,
                            freshWindowS = 120.0,
                            inProgress = false,
                            stable = false,
                            drawingRevision = (string?)null,
                            pendingSince = 0.0,
                            @ref = (object?)null,
                            empty = false,
                        },
                    },
                },
            });
        try
        {
            _ = await adapter.ForwardJournalAsync(
                "request-metadata-invalid",
                sessionId,
                new DirectContextEvent(
                    3,
                    "page.context",
                    "0000000000000103",
                    invalidDrawing),
                CancellationToken.None).ConfigureAwait(false);
            throw new InvalidOperationException(
                "invalid drawing freshness was accepted");
        }
        catch (DirectProtocolException exception)
            when (exception.Code
                == "BW_READER_CONTEXT_SNAPSHOT_EVENT_INVALID")
        {
        }
        using JsonDocument afterInvalid = JsonDocument.Parse(
            await File.ReadAllTextAsync(snapshotPath)
                .ConfigureAwait(false));
        Require(
            afterInvalid.RootElement.GetProperty("revision")
                .GetInt64() == revisionBeforeInvalid
            && afterInvalid.RootElement.GetProperty("currentPage")
                .GetProperty("text").GetString()
                == "new page state",
            "direct-snapshot-invalid-metadata-is-atomic-and-fail-closed",
            checks);

        JsonElement pendingDrawing = JsonSerializer.SerializeToElement(
            new
            {
                v = 1,
                seq = 4,
                type = "drawing",
                state = "pending",
                ts = 1_750_000_004,
                file = "book.epub",
                page = 3,
                drawingRevision = (string?)null,
                @ref = (object?)null,
            });
        _ = await adapter.ForwardJournalAsync(
            "request-drawing-pending",
            sessionId,
            new DirectContextEvent(
                4,
                "drawing",
                "0000000000000104",
                pendingDrawing),
            CancellationToken.None).ConfigureAwait(false);
        JsonElement stableDrawing = JsonSerializer.SerializeToElement(
            new
            {
                v = 1,
                seq = 5,
                type = "drawing",
                state = "stable",
                ts = 1_750_000_005,
                file = "book.epub",
                page = 3,
                drawingRevision = "dr_fedcba9876543210",
                @ref = new
                {
                    kind = "drawing",
                    file = "book.epub",
                    page = 3,
                    revision = "dr_fedcba9876543210",
                },
            });
        _ = await adapter.ForwardJournalAsync(
            "request-drawing-stable",
            sessionId,
            new DirectContextEvent(
                5,
                "drawing",
                "0000000000000105",
                stableDrawing),
            CancellationToken.None).ConfigureAwait(false);
        using JsonDocument afterDrawing = JsonDocument.Parse(
            await File.ReadAllTextAsync(snapshotPath)
                .ConfigureAwait(false));
        JsonElement updatedDrawing = afterDrawing.RootElement
            .GetProperty("currentPage")
            .GetProperty("visual")
            .GetProperty("drawing");
        Require(
            updatedDrawing.GetProperty("stable").GetBoolean()
            && !updatedDrawing.GetProperty("inProgress").GetBoolean()
            && updatedDrawing.GetProperty("drawingRevision")
                .GetString() == "dr_fedcba9876543210"
            && updatedDrawing.GetProperty("ref")
                .GetProperty("revision").GetString()
                == "dr_fedcba9876543210",
            "direct-snapshot-independent-drawing-events-update-current-page",
            checks);

        JsonElement unstablePage = JsonSerializer.SerializeToElement(
            new
            {
                v = 1,
                seq = 6,
                type = "page.context",
                ts = 1_750_000_006,
                id = "0000000000000106",
                kind = "epub",
                file = "book.epub",
                title = "Metadata Book",
                page = 3,
                stable = false,
                page_context = new
                {
                    reason = "scroll",
                    text = "unstable must not commit",
                    text_available = true,
                    text_source = "epub:viewport",
                    fallback_reason = (string?)null,
                    truncated = false,
                },
            });
        JsonElement objectPage = JsonSerializer.SerializeToElement(
            new
            {
                v = 1,
                seq = 7,
                type = "page.context",
                ts = 1_750_000_007,
                id = "0000000000000107",
                kind = "epub",
                file = "book.epub",
                title = "Metadata Book",
                page = new { chapter = 3 },
                stable = true,
                page_context = new
                {
                    reason = "dwell",
                    text = "object page must not commit",
                    text_available = true,
                    text_source = "epub:viewport",
                    fallback_reason = (string?)null,
                    truncated = false,
                },
            });
        JsonElement arrayPage = JsonSerializer.SerializeToElement(
            new
            {
                v = 1,
                seq = 8,
                type = "page.context",
                ts = 1_750_000_008,
                id = "0000000000000108",
                kind = "epub",
                file = "book.epub",
                title = "Metadata Book",
                page = new[] { 3 },
                stable = true,
                page_context = new
                {
                    reason = "dwell",
                    text = "array page must not commit",
                    text_available = true,
                    text_source = "epub:viewport",
                    fallback_reason = (string?)null,
                    truncated = false,
                },
            });
        JsonElement mismatchedInk = JsonSerializer.SerializeToElement(
            new
            {
                v = 1,
                seq = 9,
                type = "page.context",
                ts = 1_750_000_009,
                id = "0000000000000109",
                kind = "epub",
                file = "book.epub",
                title = "Metadata Book",
                page = 3,
                stable = true,
                page_context = new
                {
                    reason = "dwell",
                    text = "ink mismatch must not commit",
                    text_available = true,
                    text_source = "epub:viewport",
                    fallback_reason = (string?)null,
                    truncated = false,
                    visual = new
                    {
                        page_image = (string?)null,
                        has_ink = false,
                        drawing = new
                        {
                            contract = "reader-outgoing-context/1",
                            file = "book.epub",
                            page = 3,
                            freshness = "recent",
                            lastEditedAt = 1_750_000_008.0,
                            freshWindowS = 120.0,
                            inProgress = false,
                            stable = true,
                            drawingRevision = "dr_0011223344556677",
                            pendingSince = (double?)null,
                            @ref = new
                            {
                                kind = "drawing",
                                file = "book.epub",
                                page = 3,
                                revision = "dr_0011223344556677",
                            },
                            empty = false,
                        },
                    },
                },
            });
        bool rejectsUnstable = await SnapshotEventRejectedAsync(
            adapter,
            "request-reject-unstable",
            sessionId,
            6,
            "0000000000000106",
            unstablePage).ConfigureAwait(false);
        bool rejectsObjectPage = await SnapshotEventRejectedAsync(
            adapter,
            "request-reject-object-page",
            sessionId,
            7,
            "0000000000000107",
            objectPage).ConfigureAwait(false);
        bool rejectsArrayPage = await SnapshotEventRejectedAsync(
            adapter,
            "request-reject-array-page",
            sessionId,
            8,
            "0000000000000108",
            arrayPage).ConfigureAwait(false);
        bool rejectsInkMismatch = await SnapshotEventRejectedAsync(
            adapter,
            "request-reject-ink-mismatch",
            sessionId,
            9,
            "0000000000000109",
            mismatchedInk).ConfigureAwait(false);
        using JsonDocument afterStrictRejections = JsonDocument.Parse(
            await File.ReadAllTextAsync(snapshotPath)
                .ConfigureAwait(false));
        Require(
            rejectsUnstable
            && rejectsObjectPage
            && rejectsArrayPage
            && rejectsInkMismatch
            && afterStrictRejections.RootElement
                .GetProperty("revision").GetInt64()
                == afterDrawing.RootElement
                    .GetProperty("revision").GetInt64()
            && afterStrictRejections.RootElement
                .GetProperty("currentPage")
                .GetProperty("text").GetString()
                == "new page state",
            "direct-snapshot-rejects-unstable-invalid-page-and-ink-mismatch",
            checks);

        string retryRoot = System.IO.Path.Combine(
            root,
            "snapshot-persist-retry");
        Directory.CreateDirectory(retryRoot);
        string blockedDirectory = System.IO.Path.Combine(
            retryRoot,
            "blocked");
        await File.WriteAllTextAsync(
            blockedDirectory,
            "directory blocker").ConfigureAwait(false);
        string retrySnapshotPath = System.IO.Path.Combine(
            blockedDirectory,
            FileDirectSnapshotContextAdapter.SnapshotFileName);
        FileDirectSnapshotContextAdapter retryAdapter = new(
            retrySnapshotPath,
            () => DateTimeOffset.FromUnixTimeMilliseconds(
                1_750_000_010_000));
        JsonElement retryPage = JsonSerializer.SerializeToElement(
            new
            {
                v = 1,
                seq = 1,
                type = "page.context",
                ts = 1_750_000_010,
                id = "0000000000000201",
                kind = "pdf",
                file = "retry.pdf",
                title = "Retry",
                page = 1,
                stable = true,
                page_context = new
                {
                    reason = "dwell",
                    text = "retry commits once",
                    text_available = true,
                    text_source = "pdf:text-layer",
                    fallback_reason = (string?)null,
                    truncated = false,
                },
            });
        DirectContextEvent retryEvent = new(
            1,
            "page.context",
            "0000000000000201",
            retryPage);
        bool firstWriteFailed = false;
        try
        {
            _ = await retryAdapter.ForwardJournalAsync(
                "request-persist-retry",
                sessionId,
                retryEvent,
                CancellationToken.None).ConfigureAwait(false);
        }
        catch (DirectProtocolException exception)
            when (exception.Code
                == "BW_READER_CONTEXT_SNAPSHOT_WRITE_FAILED")
        {
            firstWriteFailed = true;
        }
        File.Delete(blockedDirectory);
        Directory.CreateDirectory(blockedDirectory);
        DirectSnapshotForwardResult retryAccepted =
            await retryAdapter.ForwardJournalAsync(
                "request-persist-retry",
                sessionId,
                retryEvent,
                CancellationToken.None).ConfigureAwait(false);
        DirectSnapshotForwardResult retryDuplicate =
            await retryAdapter.ForwardJournalAsync(
                "request-persist-retry-duplicate",
                sessionId,
                retryEvent,
                CancellationToken.None).ConfigureAwait(false);
        using JsonDocument retriedSnapshot = JsonDocument.Parse(
            await File.ReadAllTextAsync(retrySnapshotPath)
                .ConfigureAwait(false));
        Require(
            firstWriteFailed
            && retryAccepted.Outcome == "accepted"
            && retryAccepted.Revision == 1
            && retryDuplicate.Outcome == "duplicate"
            && retryDuplicate.Revision == 1
            && retriedSnapshot.RootElement
                .GetProperty("revision").GetInt64() == 1
            && retriedSnapshot.RootElement
                .GetProperty("currentPage")
                .GetProperty("text").GetString()
                == "retry commits once",
            "direct-snapshot-persist-failure-rolls-back-for-identical-retry",
            checks);
    }

    private static async Task<bool> SnapshotEventRejectedAsync(
        FileDirectSnapshotContextAdapter adapter,
        string requestId,
        string sessionId,
        long sequence,
        string eventId,
        JsonElement payload)
    {
        try
        {
            _ = await adapter.ForwardJournalAsync(
                requestId,
                sessionId,
                new DirectContextEvent(
                    sequence,
                    "page.context",
                    eventId,
                    payload),
                CancellationToken.None).ConfigureAwait(false);
            return false;
        }
        catch (DirectProtocolException exception)
            when (exception.Code
                == "BW_READER_CONTEXT_SNAPSHOT_EVENT_INVALID")
        {
            return true;
        }
    }

    private static async Task WriteConfigAsync(
        string configPath,
        string statusPath,
        string origin,
        bool localOptIn,
        bool experimentalSingleUserMode = true,
        string contextDeliveryMode =
            DirectContextDeliveryMode.LegacyInject)
    {
        string? directory = System.IO.Path.GetDirectoryName(configPath);
        if (string.IsNullOrEmpty(directory))
        {
            throw new InvalidOperationException(
                "self-test config directory missing");
        }
        Directory.CreateDirectory(directory);
        string json = JsonSerializer.Serialize(new
        {
            contract = DirectBridgeContract.ConfigContract,
            localOptIn,
            virtualMicrophoneRenderEndpointId =
                "{0.0.0.00000000}."
                + "{11111111-1111-1111-1111-111111111111}",
            virtualMicrophoneCaptureEndpointId =
                "{0.0.1.00000000}."
                + "{22222222-2222-2222-2222-222222222222}",
            virtualSpeakerRenderEndpointId =
                "{0.0.0.00000000}."
                + "{33333333-3333-3333-3333-333333333333}",
            listenHost = DirectBridgeContract.ListenHost,
            listenPort = DirectBridgeContract.DefaultListenPort,
            allowedOrigins = new[] { origin },
            allowedTailscaleUserLogin = "bwicarus@gmail.com",
            experimentalSingleUserMode,
            outputScope = "process-only",
            appKind = "codex-desktop",
            runtimeStatusPath = statusPath,
            contextDeliveryMode,
        }, new JsonSerializerOptions(DirectBridgeContract.JsonOptions)
        {
            WriteIndented = true,
        });
        await File.WriteAllTextAsync(configPath, json)
            .ConfigureAwait(false);
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

    private sealed class FakeDirectWebSocket : WebSocket
    {
        private readonly TaskCompletionSource<FakeReceiveFrame> _nextFrame =
            new(TaskCreationOptions.RunContinuationsAsynchronously);
        private int _activeReceives;
        private int _maximumConcurrentReceives;
        private WebSocketCloseStatus? _closeStatus;
        private string? _closeStatusDescription;
        private WebSocketState _state = WebSocketState.Open;

        internal TaskCompletionSource<bool> ReceiveEntered { get; } =
            new(TaskCreationOptions.RunContinuationsAsynchronously);

        internal int ReceiveCallCount { get; private set; }

        internal int MaximumConcurrentReceives =>
            _maximumConcurrentReceives;

        public override WebSocketCloseStatus? CloseStatus => _closeStatus;

        public override string? CloseStatusDescription =>
            _closeStatusDescription;

        public override WebSocketState State => _state;

        public override string? SubProtocol => null;

        internal void QueueClose()
        {
            _closeStatus = WebSocketCloseStatus.NormalClosure;
            _closeStatusDescription = "reader-stop";
            _state = WebSocketState.CloseReceived;
            _nextFrame.TrySetResult(
                new FakeReceiveFrame(
                    WebSocketMessageType.Close,
                    []));
        }

        internal void QueueText(string value)
        {
            _nextFrame.TrySetResult(
                new FakeReceiveFrame(
                    WebSocketMessageType.Text,
                    Encoding.UTF8.GetBytes(value)));
        }

        internal void QueueBinary(byte[] value)
        {
            _nextFrame.TrySetResult(
                new FakeReceiveFrame(
                    WebSocketMessageType.Binary,
                    value.ToArray()));
        }

        public override void Abort()
        {
            _state = WebSocketState.Aborted;
        }

        public override Task CloseAsync(
            WebSocketCloseStatus closeStatus,
            string? statusDescription,
            CancellationToken cancellationToken)
        {
            _closeStatus = closeStatus;
            _closeStatusDescription = statusDescription;
            _state = WebSocketState.Closed;
            return Task.CompletedTask;
        }

        public override Task CloseOutputAsync(
            WebSocketCloseStatus closeStatus,
            string? statusDescription,
            CancellationToken cancellationToken)
        {
            _closeStatus = closeStatus;
            _closeStatusDescription = statusDescription;
            _state = WebSocketState.CloseSent;
            return Task.CompletedTask;
        }

        public override void Dispose()
        {
            _state = WebSocketState.Closed;
        }

        public override async Task<WebSocketReceiveResult> ReceiveAsync(
            ArraySegment<byte> buffer,
            CancellationToken cancellationToken)
        {
            if (buffer.Array is null)
            {
                throw new ArgumentException(
                    "fake receive buffer is missing",
                    nameof(buffer));
            }
            ValueWebSocketReceiveResult result = await ReceiveCoreAsync(
                buffer.Array.AsMemory(buffer.Offset, buffer.Count),
                cancellationToken).ConfigureAwait(false);
            return new WebSocketReceiveResult(
                result.Count,
                result.MessageType,
                result.EndOfMessage,
                CloseStatus,
                CloseStatusDescription);
        }

        public override ValueTask<ValueWebSocketReceiveResult> ReceiveAsync(
            Memory<byte> buffer,
            CancellationToken cancellationToken) =>
            ReceiveCoreAsync(buffer, cancellationToken);

        public override Task SendAsync(
            ArraySegment<byte> buffer,
            WebSocketMessageType messageType,
            bool endOfMessage,
            CancellationToken cancellationToken) =>
            Task.CompletedTask;

        public override ValueTask SendAsync(
            ReadOnlyMemory<byte> buffer,
            WebSocketMessageType messageType,
            bool endOfMessage,
            CancellationToken cancellationToken) =>
            ValueTask.CompletedTask;

        private async ValueTask<ValueWebSocketReceiveResult> ReceiveCoreAsync(
            Memory<byte> buffer,
            CancellationToken cancellationToken)
        {
            ReceiveCallCount++;
            int active = Interlocked.Increment(ref _activeReceives);
            int observed;
            do
            {
                observed = _maximumConcurrentReceives;
                if (observed >= active)
                {
                    break;
                }
            }
            while (Interlocked.CompareExchange(
                ref _maximumConcurrentReceives,
                active,
                observed) != observed);
            ReceiveEntered.TrySetResult(true);
            try
            {
                FakeReceiveFrame frame = await _nextFrame.Task.WaitAsync(
                    cancellationToken).ConfigureAwait(false);
                if (frame.Payload.Length > buffer.Length)
                {
                    throw new InvalidOperationException(
                        "fake receive payload exceeds buffer");
                }
                frame.Payload.CopyTo(buffer);
                return new ValueWebSocketReceiveResult(
                    frame.Payload.Length,
                    frame.MessageType,
                    endOfMessage: true);
            }
            finally
            {
                Interlocked.Decrement(ref _activeReceives);
            }
        }

        private sealed record FakeReceiveFrame(
            WebSocketMessageType MessageType,
            byte[] Payload);
    }

    private sealed class FakeDirectAppLauncher : IDirectAppLauncher
    {
        internal int EnsureRunningCount { get; private set; }

        internal int WaitReadyCount { get; private set; }

        internal bool ThrowReadyTimeout { get; init; }

        internal bool WaitUntilCanceled { get; init; }

        internal bool CancellationObserved { get; private set; }

        internal TaskCompletionSource<bool> WaitEntered { get; } =
            new(TaskCreationOptions.RunContinuationsAsynchronously);

        public bool IsWired => true;

        public Task EnsureRunningAsync(
            string appKind,
            string appUserModelId,
            CancellationToken cancellationToken)
        {
            EnsureRunningCount++;
            if (
                appKind != "codex-desktop"
                || appUserModelId
                    != DirectBridgeContract.CodexAppUserModelId
            )
            {
                throw new InvalidOperationException(
                    "fake received an unsafe app target");
            }
            return Task.CompletedTask;
        }

        public async Task<DirectAppTarget> WaitForUniqueReadyAsync(
            string appKind,
            string appUserModelId,
            TimeSpan timeout,
            CancellationToken cancellationToken)
        {
            WaitReadyCount++;
            WaitEntered.TrySetResult(true);
            if (ThrowReadyTimeout)
            {
                throw new TimeoutException("fake timeout");
            }
            if (WaitUntilCanceled)
            {
                try
                {
                    await Task.Delay(
                        Timeout.InfiniteTimeSpan,
                        cancellationToken).ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                    CancellationObserved = true;
                    throw;
                }
            }
            return new DirectAppTarget(
                4242,
                133700000000000000,
                appKind,
                appUserModelId);
        }
    }

    private sealed class FakeDirectContextAdapter :
        IDirectContextAdapter
    {
        internal int ForwardCount { get; private set; }

        internal string Outcome { get; set; } = "accepted";

        internal DirectProtocolException? Failure { get; set; }

        internal DirectContextEvent? LastEvent { get; private set; }

        public Task<DirectContextForwardResult> ForwardAsync(
            string requestId,
            string sessionId,
            string contextContract,
            DirectContextEvent contextEvent,
            CancellationToken cancellationToken)
        {
            cancellationToken.ThrowIfCancellationRequested();
            ForwardCount++;
            LastEvent = contextEvent;
            if (Failure is not null)
            {
                return Task.FromException<DirectContextForwardResult>(
                    Failure);
            }
            if (
                !DirectBridgeContract.IsSafeId(requestId)
                || DirectPcmFrameCodec.ParseSessionId(sessionId).Length
                    != 16
                || contextContract
                    != NamedPipeDirectContextAdapter.ContextContract
                || Outcome is not ("accepted" or "duplicate")
            )
            {
                throw new InvalidOperationException(
                    "fake received an invalid context request");
            }
            return Task.FromResult(
                new DirectContextForwardResult(Outcome));
        }
    }

    private sealed class FakeDirectContextIpcTransport :
        IDirectContextIpcTransport
    {
        internal int ExchangeCount { get; private set; }

        internal bool RequestWasExact { get; set; }

        internal string Outcome { get; set; } = "accepted";

        internal Func<byte[], byte[]>? ReplyFactory { get; set; }

        public Task<byte[]> ExchangeAsync(
            ReadOnlyMemory<byte> request,
            CancellationToken cancellationToken)
        {
            cancellationToken.ThrowIfCancellationRequested();
            ExchangeCount++;
            return Task.FromResult(
                (ReplyFactory
                    ?? throw new InvalidOperationException(
                        "fake IPC reply factory missing"))(
                            request.ToArray()));
        }
    }

    private sealed class FakeDirectOutputRouteObserverFactory :
        IDirectOutputRouteObserverFactory
    {
        private readonly bool _verified;
        private readonly Action? _onCreate;

        internal FakeDirectOutputRouteObserverFactory(
            bool verified,
            Action? onCreate = null)
        {
            _verified = verified;
            _onCreate = onCreate;
        }

        internal int CreateCount { get; private set; }

        internal int DisposeCount { get; private set; }

        public IDirectOutputRouteObserver Create(
            string endpointId,
            CodexAppTarget target)
        {
            CreateCount++;
            _onCreate?.Invoke();
            return new FakeDirectOutputRouteObserver(
                endpointId,
                target.RootProcessId,
                _verified,
                () => DisposeCount++);
        }
    }

    private sealed class FakeDirectOutputRouteObserver :
        IDirectOutputRouteObserver
    {
        private readonly Action _onDispose;
        private int _disposed;

        internal FakeDirectOutputRouteObserver(
            string endpointId,
            uint targetRootProcessId,
            bool verified,
            Action onDispose)
        {
            EndpointId = endpointId;
            TargetRootProcessId = targetRootProcessId;
            Verified = verified;
            _onDispose = onDispose;
        }

        public string EndpointId { get; }

        public uint TargetRootProcessId { get; }

        public bool Verified { get; }

        public void Dispose()
        {
            if (Interlocked.Exchange(ref _disposed, 1) == 0)
            {
                _onDispose();
            }
        }
    }

    private sealed class FakeDirectMediaAdapter : IDirectMediaAdapter
    {
        private Func<DirectPcmFrame, CancellationToken, Task>? _sender;
        private TaskCompletionSource<DirectProtocolException?>?
            _completionSource;
        private Task<DirectProtocolException?> _completion =
            Task.FromResult<DirectProtocolException?>(null);
        private readonly TaskCompletionSource<bool> _stopEntered =
            new(TaskCreationOptions.RunContinuationsAsynchronously);
        private readonly TaskCompletionSource<bool> _releaseStop =
            new(TaskCreationOptions.RunContinuationsAsynchronously);

        internal int StartCount { get; private set; }

        internal DirectMediaStartRequest? LastStartRequest {
            get;
            private set;
        }

        internal int StopCount { get; private set; }

        internal bool EmitDuringStart { get; init; }

        internal DirectProtocolException? StopFailure { get; init; }

        internal bool BlockStopUntilReleased { get; init; }

        internal int DisposeFailuresRemaining { get; set; }

        internal int DisposeCount { get; private set; }

        internal bool CleanupOwnership { get; set; }

        internal Task StopEntered => _stopEntered.Task;

        internal bool? StopCancellationCanBeCanceled { get; private set; }

        public bool IsWired => true;

        public bool CaptureActive { get; private set; }

        public bool CleanupPending { get; private set; }

        internal bool RetainCleanupOnStop { get; set; }

        internal bool OutputRouteVerified { get; set; } = true;

        public bool IsOutputRouteVerified(
            DirectBridgeConfig config) =>
            OutputRouteVerified;

        public Task<DirectProtocolException?> Completion => _completion;

        public async Task<DirectMediaStartResult> StartAsync(
            DirectMediaStartRequest request,
            Func<DirectPcmFrame, CancellationToken, Task> sendFrameAsync,
            CancellationToken cancellationToken)
        {
            StartCount++;
            LastStartRequest = request;
            if (
                request.RootProcessId != 4242
                || request.RootProcessStartFileTimeUtc
                    != 133700000000000000
                || request.VirtualMicrophoneRenderEndpointId
                    != "{0.0.0.00000000}."
                        + "{11111111-1111-1111-1111-111111111111}"
                || request.VirtualMicrophoneCaptureEndpointId
                    != "{0.0.1.00000000}."
                        + "{22222222-2222-2222-2222-222222222222}"
                || request.VirtualSpeakerRenderEndpointId
                    != "{0.0.0.00000000}."
                        + "{33333333-3333-3333-3333-333333333333}"
                || !request.AutomatePerAppAudioRoute
            )
            {
                throw new InvalidOperationException(
                    "fake received an unsafe capture target");
            }
            _sender = sendFrameAsync;
            _completionSource = new(
                TaskCreationOptions.RunContinuationsAsynchronously);
            _completion = _completionSource.Task;
            CaptureActive = true;
            CleanupPending = false;
            CleanupOwnership = true;
            if (EmitDuringStart)
            {
                await sendFrameAsync(
                    new DirectPcmFrame(
                        DirectPcmTrack.AppOutput,
                        Sequence: 0,
                        TimestampMicroseconds: 1000,
                        PcmS16Le: new byte[
                            Pcm48kMonoFramer.BytesPerChunk]),
                    cancellationToken).ConfigureAwait(false);
            }
            return new DirectMediaStartResult(
                HostReady: true,
                CaptureActive: true);
        }

        internal Task EmitAsync(DirectPcmFrame frame) =>
            _sender is null
                ? throw new InvalidOperationException(
                    "fake media has not started")
                : _sender(frame, CancellationToken.None);

        internal void Fail(DirectProtocolException exception)
        {
            CaptureActive = false;
            CleanupPending = RetainCleanupOnStop;
            _completionSource?.TrySetResult(exception);
        }

        internal void ReleaseBlockedStop() =>
            _releaseStop.TrySetResult(true);

        public Task PushUplinkFrameAsync(
            DirectPcmFrame frame,
            CancellationToken cancellationToken)
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (
                !CaptureActive
                || frame.Track != DirectPcmTrack.BrowserMicrophone
            )
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_UPLINK_NOT_ACTIVE",
                    "fake uplink is not active");
            }
            return Task.CompletedTask;
        }

        public async Task StopAsync(CancellationToken cancellationToken)
        {
            if (CaptureActive || _sender is not null)
            {
                StopCount++;
                StopCancellationCanBeCanceled =
                    cancellationToken.CanBeCanceled;
                _stopEntered.TrySetResult(true);
                if (BlockStopUntilReleased)
                {
                    await _releaseStop.Task.WaitAsync(cancellationToken)
                        .ConfigureAwait(false);
                }
            }
            CaptureActive = false;
            _sender = null;
            CleanupPending = RetainCleanupOnStop;
            _completionSource?.TrySetResult(StopFailure);
            _completionSource = null;
        }

        public ValueTask DisposeAsync()
        {
            DisposeCount++;
            if (DisposeFailuresRemaining > 0)
            {
                DisposeFailuresRemaining--;
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_FAKE_DISPOSE_FAILED",
                    "fake media dispose failed");
            }
            CleanupOwnership = false;
            CleanupPending = false;
            return ValueTask.CompletedTask;
        }
    }
}
