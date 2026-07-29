using System.Buffers.Binary;
using System.Net.WebSockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

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
        const string pairingCode = "ABCDEFGH23";
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
        using ECDsa clientKey = ECDsa.Create(
            ECCurve.NamedCurves.nistP256);
        string clientSpki = DirectBase64Url.Encode(
            clientKey.ExportSubjectPublicKeyInfo());
        await WriteConfigAsync(
            configPath,
            statusPath,
            origin,
            localOptIn: true,
            pairingCodeHash:
                DirectBridgeContract.HashPairingCode(pairingCode),
            pairingExpiresAtUtc: now.AddMinutes(5),
            clientSpki: "",
            clientFingerprint: "").ConfigureAwait(false);

        DirectBridgeConfigStore store = new(configPath);
        DirectBridgeConfig initial = store.Load();
        Require(
            initial.ListenHost == "127.0.0.1"
            && initial.ListenPort == 43128
            && initial.AllowedOrigins.SetEquals(new[] { origin })
            && initial.AllowedTailscaleUserLogin
                == "bwicarus@gmail.com"
            && initial.ExperimentalSingleUserMode
            && initial.HasPairingCode
            && !initial.HasPairedClient,
            "direct-config-localhost-single-user-and-pair-hash",
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
                    "updatedAtUtc",
                })
                && statusDocument.RootElement.GetProperty("state")
                    .GetString() == "idle"
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
        await CheckServiceLeaseAsync(
            installationRoot,
            configPath,
            checks).ConfigureAwait(false);

        FakeDirectAppLauncher appLauncher = new();
        FakeDirectMediaAdapter mediaAdapter = new()
        {
            EmitDuringStart = true,
        };
        await using DirectBridgeCoordinator coordinator = new(
            store,
            appLauncher,
            mediaAdapter);
        DirectBridgeProtocolSession directSession = new(
            "connection-v2-self-test",
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
                requestId = "request-v2-hello",
                protocolVersion = 2,
            },
            directEvents,
            directPcmFrames).ConfigureAwait(false);
        JsonElement directHelloPayload = RequireSuccess(
            directHello,
            "hello");
        Require(
            directHelloPayload.GetProperty("protocolVersion")
                .GetInt32() == 2
            && directHelloPayload.GetProperty("limits")
                .GetProperty("pcmFrameBytes").GetInt32() == 1956
            && directSession.IsAuthenticated
            && directSession.Phase == DirectProtocolPhase.AwaitingStart
            && appLauncher.EnsureRunningCount == 0
            && mediaAdapter.StartCount == 0
            && directEvents.Count == 0
            && directPcmFrames.Count == 0,
            "direct-v2-hello-authenticates-without-side-effects",
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
            },
            events,
            pcmFrames).ConfigureAwait(false);
        JsonElement helloPayload = RequireSuccess(
            hello,
            "hello");
        JsonElement challenge = helloPayload.GetProperty("challenge");
        string challengeId = challenge.GetProperty("challengeId")
            .GetString()!;
        string nonce = challenge.GetProperty("nonce").GetString()!;
        JsonElement limits = helloPayload.GetProperty("limits");
        Require(
            limits.GetProperty("maxMessageBytes").GetInt32() == 65536
            && limits.GetProperty("pcmFrameBytes").GetInt32() == 1956
            && limits.GetProperty("pcmQueueLimitMs").GetInt32() == 400
            && limits.GetProperty("heartbeatIntervalMs").GetInt32()
                == 5_000
            && limits.GetProperty("heartbeatTimeoutMs").GetInt32()
                == 15_000
            && appLauncher.EnsureRunningCount == 0
            && mediaAdapter.StartCount == 0,
            "direct-hello-is-side-effect-free",
            checks);
        Require(
            session.Phase
                == DirectProtocolPhase.AwaitingAuthentication,
            "direct-hello-does-not-complete-authentication",
            checks);

        JsonElement pair = await SendAsync(
            session,
            new
            {
                contract = DirectBridgeContract.Contract,
                type = "pair",
                requestId = "request-pair",
                pairingCode,
                clientPublicKeySpki = clientSpki,
            },
            events,
            pcmFrames).ConfigureAwait(false);
        JsonElement pairPayload = RequireSuccess(pair, "pair");
        string fingerprint = pairPayload
            .GetProperty("clientFingerprintSha256")
            .GetString()!;
        string persistedConfig = await File.ReadAllTextAsync(configPath)
            .ConfigureAwait(false);
        DirectBridgeConfig pairedConfig = store.Load();
        Require(
            fingerprint.Length == 43
            && !persistedConfig.Contains(
                pairingCode,
                StringComparison.Ordinal)
            && pairedConfig.PairingCodeHash.Length == 0
            && pairedConfig.PairingExpiresAtUtc is null
            && pairedConfig.HasPairedClient,
            "direct-pair-consumes-code-and-persists-only-public-key",
            checks);
        Require(
            session.Phase
                == DirectProtocolPhase.AwaitingAuthentication,
            "direct-pair-without-auth-stays-in-auth-deadline",
            checks);

        byte[] authPayload =
            DirectBridgeContract.BuildAuthenticationPayload(
                challengeId,
                nonce,
                origin);
        byte[] signature = clientKey.SignData(
            authPayload,
            HashAlgorithmName.SHA256,
            DSASignatureFormat.IeeeP1363FixedFieldConcatenation);
        JsonElement auth = await SendAsync(
            session,
            new
            {
                contract = DirectBridgeContract.Contract,
                type = "auth",
                requestId = "request-auth",
                challengeId,
                signature = DirectBase64Url.Encode(signature),
            },
            events,
            pcmFrames).ConfigureAwait(false);
        _ = RequireSuccess(auth, "auth");
        CryptographicOperations.ZeroMemory(authPayload);
        CryptographicOperations.ZeroMemory(signature);
        Require(
            session.Authenticated
            && session.IsAuthenticated
            && session.Phase == DirectProtocolPhase.AwaitingStart,
            "direct-auth-ecdsa-p256-p1363-origin-bound",
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
            && session.Phase == DirectProtocolPhase.AwaitingStart,
            "direct-stop-closes-capture-and-surfaces-media-failure",
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
        await CheckServerCloseCancelsBlockedStartAsync(
            configPath,
            checks).ConfigureAwait(false);
        await CheckServerPrefetchPreservesOrderAsync(
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
        CheckConnectionPhaseDeadlines(checks);
        CheckPcmSequenceGate(checks);
        CheckProductionAdapterBoundaries(root, checks);
        CheckStrictOriginConfig(root, checks);
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
                    protocolVersion = 2,
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
            && session.Phase == DirectProtocolPhase.AwaitingStart,
            "direct-explicit-stop-surfaces-release-failure-and-clears-session",
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
                            {"ok":true,"running":true,"pid":4512,"result":"started"}
                            """,
                            "")
                        : new DirectTypistHelperResult(
                            0,
                            """
                            {"ok":true,"running":false,"stopped":true,"result":"stopped","expectedPid":4512}
                            """,
                            "");
                return Task.FromResult(result);
            });
        DirectTypistLease? owned =
            await ownedController.EnsureRunningAsync(
                CancellationToken.None).ConfigureAwait(false);
        Require(
            owned?.ProcessId == 4512
            && calls.Count == 1
            && calls[0].SequenceEqual(
                new[] { "--ensure-running" }),
            "direct-typist-started-result-creates-owned-lease",
            checks);
        await ownedController.ReleaseAsync(
            owned!,
            CancellationToken.None).ConfigureAwait(false);
        Require(
            calls.Count == 2
            && calls[1].SequenceEqual(
                new[] { "--stop-if-owned", "4512" }),
            "direct-typist-owned-lease-releases-with-exact-pid",
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
                new DirectTypistLease(4512),
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
                    new DirectTypistLease(4512),
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
                    new[] { "--stop-if-owned", "4512" })),
            "direct-start-failed-typist-release-retains-and-retries-exact-pid",
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
                            {"ok":true,"running":true,"pid":9002,"result":"{{result}}"}
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
    }

    private static async Task
        CheckAtomicShortcutAndBestEffortCleanupAsync(
            ICollection<string> checks)
    {
        using CancellationTokenSource peerClose = new();
        int shortcutCount = 0;
        int commitCount = 0;
        WindowsDirectMediaAdapter.SendShortcutAtAtomicCommitBoundary(
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

        VoiceShortcutKeyEvent[] activationEvents =
            WindowsCodexAppProbe.VoiceShortcutEvents(
                VoiceShortcutInputBatch.Activation);
        VoiceShortcutKeyEvent[] releaseEvents =
            WindowsCodexAppProbe.VoiceShortcutEvents(
                VoiceShortcutInputBatch.ReleasePressedKeys);
        bool everyPartialReleased = true;
        for (uint inserted = 0; inserted <= 6; inserted++)
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
                            : 3;
                    });
            bool expectedCleanup = inserted is >= 1 and <= 5;
            everyPartialReleased &= accepted == (inserted == 6)
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
                    ? 3u
                    : throw new InvalidOperationException(
                        "release input failed"));
        Require(
            activationEvents.SequenceEqual(
                new[]
                {
                    new VoiceShortcutKeyEvent(0x11, KeyUp: false),
                    new VoiceShortcutKeyEvent(0x10, KeyUp: false),
                    new VoiceShortcutKeyEvent(0x43, KeyUp: false),
                    new VoiceShortcutKeyEvent(0x43, KeyUp: true),
                    new VoiceShortcutKeyEvent(0x10, KeyUp: true),
                    new VoiceShortcutKeyEvent(0x11, KeyUp: true),
                })
            && releaseEvents.SequenceEqual(
                new[]
                {
                    new VoiceShortcutKeyEvent(0x43, KeyUp: true),
                    new VoiceShortcutKeyEvent(0x10, KeyUp: true),
                    new VoiceShortcutKeyEvent(0x11, KeyUp: true),
                })
            && everyPartialReleased
            && cleanupThrowRemainsFailed,
            "direct-shortcut-partial-send-releases-keys-best-effort",
            checks);
        Require(
            !WindowsCodexAppProbe.SupportsOwnedVoiceStop,
            "direct-stop-does-not-claim-unverified-app-voice-toggle",
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
            localOptIn: false,
            pairingCodeHash: "",
            pairingExpiresAtUtc: null,
            clientSpki: "",
            clientFingerprint: "").ConfigureAwait(false);
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
        string? prefetched = outcome.PrefetchedReceiveTask is null
            ? null
            : await outcome.PrefetchedReceiveTask.ConfigureAwait(false);
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
                        DirectPcmTrack.UserMicrophone,
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
            DirectPcmTrack.UserMicrophone,
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
                    "codex-desktop",
                    DirectBridgeContract.CodexAppUserModelId,
                    "mic"),
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
            localOptIn: false,
            pairingCodeHash: "",
            pairingExpiresAtUtc: null,
            clientSpki: "",
            clientFingerprint: "").GetAwaiter().GetResult();
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
            && CheckExperimentalAndStrictOrigins(root),
            "direct-origin-policy-is-explicit-and-canonical",
            checks);
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
            pairingCodeHash: "",
            pairingExpiresAtUtc: null,
            clientSpki: "",
            clientFingerprint: "",
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
            pairingCodeHash: "",
            pairingExpiresAtUtc: null,
            clientSpki: "",
            clientFingerprint: "",
            experimentalSingleUserMode: false).GetAwaiter().GetResult();
        DirectBridgeConfig strict =
            new DirectBridgeConfigStore(strictPath).Load();

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
            && DirectBridgeServer.OriginMatchesAllowlist(
                strict,
                "https://reader.example")
            && !DirectBridgeServer.OriginMatchesAllowlist(
                strict,
                "https://extension-page.example");
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

    private static async Task WriteConfigAsync(
        string configPath,
        string statusPath,
        string origin,
        bool localOptIn,
        string pairingCodeHash,
        DateTimeOffset? pairingExpiresAtUtc,
        string clientSpki,
        string clientFingerprint,
        bool experimentalSingleUserMode = true)
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
            microphoneEndpointId = localOptIn
                ? "explicit-test-microphone"
                : "",
            listenHost = DirectBridgeContract.ListenHost,
            listenPort = DirectBridgeContract.DefaultListenPort,
            allowedOrigins = new[] { origin },
            allowedTailscaleUserLogin = "bwicarus@gmail.com",
            experimentalSingleUserMode,
            pairingCodeHash,
            pairingExpiresAtUtc,
            pairedClientPublicKeySpki = clientSpki,
            pairedClientFingerprintSha256 = clientFingerprint,
            outputScope = "process-only",
            appKind = "codex-desktop",
            runtimeStatusPath = statusPath,
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
                appKind,
                appUserModelId);
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

        public Task<DirectProtocolException?> Completion => _completion;

        public async Task<DirectMediaStartResult> StartAsync(
            DirectMediaStartRequest request,
            Func<DirectPcmFrame, CancellationToken, Task> sendFrameAsync,
            CancellationToken cancellationToken)
        {
            StartCount++;
            if (
                request.RootProcessId != 4242
                || request.MicrophoneEndpointId
                    != "explicit-test-microphone"
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
            _completionSource?.TrySetResult(exception);
        }

        internal void ReleaseBlockedStop() =>
            _releaseStop.TrySetResult(true);

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
            return ValueTask.CompletedTask;
        }
    }
}
