using System.Security.Cryptography;
using System.Text.Json;

namespace BwReader.ComputerVoiceAudio;

internal static class DisconnectCleanupWatchdogSelfTest
{
    internal static void Run(ICollection<string> checks) =>
        RunAsync(checks).GetAwaiter().GetResult();

    private static async Task RunAsync(ICollection<string> checks)
    {
        string root = Path.Combine(
            Path.GetTempPath(),
            "bw-disconnect-watchdog-"
                + Convert.ToHexString(RandomNumberGenerator.GetBytes(6)));
        Directory.CreateDirectory(root);
        try
        {
            string configPath = Path.Combine(
                root,
                "native-host",
                "direct.json");
            Directory.CreateDirectory(Path.GetDirectoryName(configPath)!);
            await WriteConfigAsync(configPath, root).ConfigureAwait(false);
            await CheckDelayedRetryAsync(configPath, checks)
                .ConfigureAwait(false);
            await CheckStatusFailureStillRetriesAsync(configPath, checks)
                .ConfigureAwait(false);
            await CheckReplacementOwnerWinsAsync(configPath, checks)
                .ConfigureAwait(false);
        }
        finally
        {
            Directory.Delete(root, recursive: true);
        }
    }

    private static async Task CheckDelayedRetryAsync(
        string configPath,
        ICollection<string> checks)
    {
        WatchdogMedia media = new() { RetainCleanupOnStop = true };
        await using DirectBridgeCoordinator coordinator = new(
            new DirectBridgeConfigStore(configPath),
            new WatchdogLauncher(),
            media);
        await StartAsync(coordinator, "connection-old", "session-old")
            .ConfigureAwait(false);
        _ = await coordinator.StopForConnectionAsync("connection-old")
            .ConfigureAwait(false);
        media.RetainCleanupOnStop = false;
        int delays = 0;
        int idlePublications = 0;
        bool retried = await DirectBridgeServer
            .RunDisconnectCleanupWatchdogAsync(
                coordinator,
                "connection-old",
                DirectBridgeServer.DisconnectCleanupWatchdogDelay,
                () =>
                {
                    idlePublications++;
                    return Task.CompletedTask;
                },
                CancellationToken.None,
                (delay, _) =>
                {
                    if (delay == TimeSpan.FromSeconds(30))
                    {
                        delays++;
                    }
                    return Task.CompletedTask;
                }).ConfigureAwait(false);
        Require(
            retried
            && delays == 1
            && idlePublications == 1
            && media.StopCount == 2
            && !media.CleanupPending
            && coordinator.ActiveSessionId is null,
            "direct-disconnect-watchdog-retries-once-and-releases-route-owner",
            checks);
    }

    private static async Task CheckStatusFailureStillRetriesAsync(
        string configPath,
        ICollection<string> checks)
    {
        WatchdogMedia media = new() { RetainCleanupOnStop = true };
        await using DirectBridgeCoordinator coordinator = new(
            new DirectBridgeConfigStore(configPath),
            new WatchdogLauncher(),
            media);
        await StartAsync(coordinator, "connection-status", "session-status")
            .ConfigureAwait(false);
        _ = await coordinator.StopForConnectionAsync("connection-status")
            .ConfigureAwait(false);
        media.RetainCleanupOnStop = false;

        await using DirectConnectionOwnership ownership = new();
        DirectConnectionLease connection = await ownership.CreateAsync(
            "connection-status",
            CancellationToken.None,
            CancellationToken.None).ConfigureAwait(false);
        _ = await ownership.PromoteAsync(
            connection,
            CancellationToken.None).ConfigureAwait(false);

        Task<bool>? watchdog = null;
        bool statusFailureObserved = false;
        try
        {
            await DirectBridgeServer
                .CompleteConnectionAndArmDisconnectWatchdogAsync(
                    () => ownership.CompleteAsync(
                        connection,
                        () => throw new IOException(
                            "simulated runtime status write failure")),
                    cleanupWatchdogRequired: true,
                    () =>
                    {
                        watchdog = DirectBridgeServer
                            .RunDisconnectCleanupWatchdogAsync(
                                coordinator,
                                "connection-status",
                                TimeSpan.Zero,
                                onCleanupSettled: null,
                                CancellationToken.None,
                                (_, _) => Task.CompletedTask);
                    }).ConfigureAwait(false);
        }
        catch (IOException)
        {
            statusFailureObserved = true;
        }

        bool retried = watchdog is not null
            && await watchdog.ConfigureAwait(false);
        Require(
            statusFailureObserved
            && connection.IsCompleted
            && retried
            && media.StopCount == 2
            && !media.CleanupPending
            && coordinator.ActiveSessionId is null,
            "direct-disconnect-watchdog-status-failure-still-retries-cleanup",
            checks);
    }

    private static async Task CheckReplacementOwnerWinsAsync(
        string configPath,
        ICollection<string> checks)
    {
        WatchdogMedia media = new() { RetainCleanupOnStop = true };
        await using DirectBridgeCoordinator coordinator = new(
            new DirectBridgeConfigStore(configPath),
            new WatchdogLauncher(),
            media);
        await StartAsync(coordinator, "connection-old", "session-old")
            .ConfigureAwait(false);
        _ = await coordinator.StopForConnectionAsync("connection-old")
            .ConfigureAwait(false);
        media.RetainCleanupOnStop = false;
        await StartAsync(coordinator, "connection-new", "session-new")
            .ConfigureAwait(false);
        int stopsBeforeOldWatchdog = media.StopCount;
        bool retried = await DirectBridgeServer
            .RunDisconnectCleanupWatchdogAsync(
                coordinator,
                "connection-old",
                TimeSpan.Zero,
                onCleanupSettled: null,
                CancellationToken.None,
                (_, _) => Task.CompletedTask).ConfigureAwait(false);
        Require(
            !retried
            && media.StopCount == stopsBeforeOldWatchdog
            && media.CaptureActive
            && coordinator.ActiveSessionId == "session-new",
            "direct-disconnect-watchdog-old-owner-is-noop-after-reconnect",
            checks);
    }

    private static Task StartAsync(
        DirectBridgeCoordinator coordinator,
        string connectionId,
        string sessionId) =>
        coordinator.StartAsync(
            connectionId,
            sessionId,
            (_, _) => Task.CompletedTask,
            (_, _) => Task.CompletedTask,
            CancellationToken.None);

    private static async Task WriteConfigAsync(string path, string root)
    {
        string json = JsonSerializer.Serialize(new
        {
            contract = DirectBridgeContract.ConfigContract,
            localOptIn = true,
            virtualMicrophoneRenderEndpointId = "{0.0.0.00000000}.{11111111-1111-1111-1111-111111111111}",
            virtualMicrophoneCaptureEndpointId = "{0.0.1.00000000}.{22222222-2222-2222-2222-222222222222}",
            virtualSpeakerRenderEndpointId = "{0.0.0.00000000}.{33333333-3333-3333-3333-333333333333}",
            listenHost = DirectBridgeContract.ListenHost,
            listenPort = DirectBridgeContract.DefaultListenPort,
            allowedOrigins = new[] { "https://bwicarus.taile44d0c.ts.net" },
            allowedTailscaleUserLogin = "bwicarus@gmail.com",
            experimentalSingleUserMode = true,
            outputScope = "process-only",
            appKind = DirectAppTargets.CodexDesktop,
            runtimeStatusPath = Path.Combine(
                root,
                "runtime",
                "computer-voice-direct.status.json"),
            contextDeliveryMode = DirectContextDeliveryMode.LegacyInject,
        }, DirectBridgeContract.JsonOptions);
        await File.WriteAllTextAsync(path, json).ConfigureAwait(false);
    }

    private static void Require(
        bool condition,
        string name,
        ICollection<string> checks)
    {
        if (!condition)
        {
            throw new InvalidOperationException("self-test failed: " + name);
        }
        checks.Add(name);
    }

    private sealed class WatchdogLauncher : IDirectAppLauncher
    {
        public bool IsWired => true;

        public Task EnsureRunningAsync(
            string appKind,
            string appUserModelId,
            CancellationToken cancellationToken) => Task.CompletedTask;

        public Task<DirectAppTarget> WaitForUniqueReadyAsync(
            string appKind,
            string appUserModelId,
            TimeSpan timeout,
            CancellationToken cancellationToken) => Task.FromResult(
                new DirectAppTarget(4242, 133700000000000000, appKind, appUserModelId));

        public Task<DirectAppTarget> RestartAsync(
            string appKind,
            string appUserModelId,
            DirectAppTarget expected,
            TimeSpan timeout,
            CancellationToken cancellationToken) => Task.FromResult(expected);
    }

    private sealed class WatchdogMedia : IDirectMediaAdapter
    {
        private TaskCompletionSource<DirectProtocolException?>? _completion;

        internal bool RetainCleanupOnStop { get; set; }
        internal int StopCount { get; private set; }
        public bool IsWired => true;
        public bool CaptureActive { get; private set; }
        public bool CleanupPending { get; private set; }
        public Task<DirectProtocolException?> Completion { get; private set; } =
            Task.FromResult<DirectProtocolException?>(null);

        public bool IsOutputRouteVerified(DirectBridgeConfig config) => true;

        public Task<DirectMediaStartResult> StartAsync(
            DirectMediaStartRequest request,
            Func<DirectPcmFrame, CancellationToken, Task> sendFrameAsync,
            CancellationToken cancellationToken)
        {
            _completion = new(TaskCreationOptions.RunContinuationsAsynchronously);
            Completion = _completion.Task;
            CaptureActive = true;
            CleanupPending = true;
            return Task.FromResult(new DirectMediaStartResult(true, true));
        }

        public Task PushUplinkFrameAsync(
            DirectPcmFrame frame,
            CancellationToken cancellationToken) => Task.CompletedTask;

        public Task StopAsync(CancellationToken cancellationToken)
        {
            StopCount++;
            CaptureActive = false;
            CleanupPending = RetainCleanupOnStop;
            _completion?.TrySetResult(null);
            return Task.CompletedTask;
        }

        public ValueTask DisposeAsync()
        {
            CaptureActive = false;
            CleanupPending = false;
            return ValueTask.CompletedTask;
        }
    }
}
