namespace BwReader.ComputerVoiceAudio;

internal static class CodexVoiceActivitySelfTest
{
    internal static void Run(ICollection<string> checks)
    {
        CheckActivityRule(checks);
        CheckPreexistingVoiceIsNotOwned(checks);
        CheckNewVoiceIsOwned(checks);
        CheckStartTimeoutAndReadError(checks);
        CheckStopConfirmation(checks);
        CheckLocalCloseMonitor(checks);
        CheckOwnershipSafeShortcutStop(checks);
    }

    private static void CheckActivityRule(ICollection<string> checks)
    {
        Require(
            CodexVoiceActivitySnapshot.Available(100, 0).Active
            && CodexVoiceActivitySnapshot.Available(101, 100).Active
            && !CodexVoiceActivitySnapshot.Available(100, 100).Active
            && !CodexVoiceActivitySnapshot.Available(100, 101).Active
            && !CodexVoiceActivitySnapshot.Available(0, 0).Active
            && !CodexVoiceActivitySnapshot.Unavailable().Active
            && !CodexVoiceActivitySnapshot.Error().Active,
            "codex-voice-activity-filetime-rule",
            checks);
    }

    private static void CheckPreexistingVoiceIsNotOwned(
        ICollection<string> checks)
    {
        FakeCodexVoiceActivitySource source = new(
            CodexVoiceActivitySnapshot.Available(300, 0));
        FakeCodexVoiceActivityClock clock = new();
        CodexVoiceActivityController controller = new(source, clock);

        CodexVoiceStartBaseline baseline =
            controller.CaptureStartBaseline();
        CodexVoiceStartConfirmation confirmation =
            controller.ConfirmStartedAsync(
                baseline,
                TimeSpan.FromSeconds(3),
                TimeSpan.FromMilliseconds(250),
                CancellationToken.None).GetAwaiter().GetResult();

        Require(
            !baseline.ShortcutRequired
            && !confirmation.StartedByBridge
            && !confirmation.OwnsVoice
            && !controller.PrepareStop(confirmation).ShortcutRequired
            && source.ReadCount == 2
            && clock.DelayCount == 0,
            "codex-voice-preexisting-active-not-owned",
            checks);
    }

    private static void CheckNewVoiceIsOwned(
        ICollection<string> checks)
    {
        FakeCodexVoiceActivitySource source = new(
            CodexVoiceActivitySnapshot.Available(100, 200),
            CodexVoiceActivitySnapshot.Available(100, 200),
            CodexVoiceActivitySnapshot.Available(300, 0));
        FakeCodexVoiceActivityClock clock = new();
        CodexVoiceActivityController controller = new(source, clock);

        CodexVoiceStartBaseline baseline =
            controller.CaptureStartBaseline();
        CodexVoiceStartConfirmation confirmation =
            controller.ConfirmStartedAsync(
                baseline,
                TimeSpan.FromSeconds(3),
                TimeSpan.FromMilliseconds(250),
                CancellationToken.None).GetAwaiter().GetResult();

        Require(
            baseline.ShortcutRequired
            && confirmation.StartedByBridge
            && confirmation.OwnsVoice
            && controller.PrepareStop(confirmation).ShortcutRequired
            && confirmation.Snapshot.LastUsedTimeStart == 300
            && clock.DelayCount == 1,
            "codex-voice-new-activation-owned",
            checks);
    }

    private static void CheckStartTimeoutAndReadError(
        ICollection<string> checks)
    {
        FakeCodexVoiceActivitySource timeoutSource = new(
            CodexVoiceActivitySnapshot.Available(100, 200));
        FakeCodexVoiceActivityClock timeoutClock = new();
        CodexVoiceActivityController timeoutController =
            new(timeoutSource, timeoutClock);
        CodexVoiceStartBaseline timeoutBaseline =
            timeoutController.CaptureStartBaseline();

        DirectProtocolException timeoutFailure = Capture(
            () => timeoutController.ConfirmStartedAsync(
                timeoutBaseline,
                TimeSpan.FromSeconds(2),
                TimeSpan.FromSeconds(1),
                CancellationToken.None));

        FakeCodexVoiceActivitySource errorSource = new(
            CodexVoiceActivitySnapshot.Available(100, 200),
            CodexVoiceActivitySnapshot.Error());
        CodexVoiceActivityController errorController = new(
            errorSource,
            new FakeCodexVoiceActivityClock());
        CodexVoiceStartBaseline errorBaseline =
            errorController.CaptureStartBaseline();

        DirectProtocolException readFailure = Capture(
            () => errorController.ConfirmStartedAsync(
                errorBaseline,
                TimeSpan.FromSeconds(2),
                TimeSpan.FromSeconds(1),
                CancellationToken.None));

        FakeCodexVoiceActivitySource unavailableSource = new(
            CodexVoiceActivitySnapshot.Available(100, 200),
            CodexVoiceActivitySnapshot.Unavailable());
        CodexVoiceActivityController unavailableController = new(
            unavailableSource,
            new FakeCodexVoiceActivityClock());
        CodexVoiceStartBaseline unavailableBaseline =
            unavailableController.CaptureStartBaseline();

        DirectProtocolException unavailableFailure = Capture(
            () => unavailableController.ConfirmStartedAsync(
                unavailableBaseline,
                TimeSpan.FromSeconds(2),
                TimeSpan.FromSeconds(1),
                CancellationToken.None));

        Require(
            timeoutFailure.Code
                == CodexVoiceActivityController.StartNotConfirmedCode
            && timeoutFailure.Retryable
            && timeoutClock.UtcNow
                == FakeCodexVoiceActivityClock.Start
                    + TimeSpan.FromSeconds(2)
            && readFailure.Code
                == CodexVoiceActivityController.ActivityReadFailedCode
            && readFailure.Retryable
            && unavailableFailure.Code
                == CodexVoiceActivityController.ActivityUnavailableCode
            && unavailableFailure.Retryable,
            "codex-voice-start-timeout-unavailable-and-read-error",
            checks);
    }

    private static void CheckStopConfirmation(
        ICollection<string> checks)
    {
        FakeCodexVoiceActivitySource source = new(
            CodexVoiceActivitySnapshot.Available(300, 0),
            CodexVoiceActivitySnapshot.Available(300, 0),
            CodexVoiceActivitySnapshot.Available(300, 0),
            CodexVoiceActivitySnapshot.Available(300, 400));
        FakeCodexVoiceActivityClock clock = new();
        CodexVoiceActivityController controller = new(source, clock);
        CodexVoiceStartConfirmation confirmation = new(
            CodexVoiceActivitySnapshot.Available(300, 0),
            StartedByBridge: true);
        CodexVoiceStopPlan plan = controller.PrepareStop(confirmation);

        CodexVoiceActivitySnapshot stopped =
            controller.ConfirmStoppedAsync(
                plan.Snapshot,
                TimeSpan.FromSeconds(3),
                TimeSpan.FromMilliseconds(250),
                CancellationToken.None).GetAwaiter().GetResult();

        Require(
            plan.ShortcutRequired
            && !stopped.Active
            && stopped.LastUsedTimeStop == 400
            && clock.DelayCount == 2,
            "codex-voice-stop-confirmed-inactive",
            checks);

        FakeCodexVoiceActivitySource staleStopSource = new(
            CodexVoiceActivitySnapshot.Available(600, 700));
        FakeCodexVoiceActivityClock staleStopClock = new();
        CodexVoiceActivityController staleStopController = new(
            staleStopSource,
            staleStopClock);
        DirectProtocolException staleStopFailure = Capture(
            () => staleStopController.ConfirmStoppedAsync(
                CodexVoiceActivitySnapshot.Available(300, 100),
                TimeSpan.FromSeconds(1),
                TimeSpan.FromSeconds(1),
                CancellationToken.None));

        FakeCodexVoiceActivitySource replacementSource = new(
            CodexVoiceActivitySnapshot.Available(600, 0));
        CodexVoiceActivityController replacementController = new(
            replacementSource,
            new FakeCodexVoiceActivityClock());
        CodexVoiceStopPlan replacementPlan =
            replacementController.PrepareStop(
                new CodexVoiceStartConfirmation(
                    CodexVoiceActivitySnapshot.Available(300, 0),
                    StartedByBridge: true));

        Require(
            staleStopFailure.Code
                == CodexVoiceActivityController.StopNotConfirmedCode
            && staleStopClock.DelayCount == 1
            && !replacementPlan.ShortcutRequired
            && !replacementPlan.VoiceGenerationMatches,
            "codex-voice-stop-requires-new-stop-and-same-generation",
            checks);
    }

    private static void CheckLocalCloseMonitor(
        ICollection<string> checks)
    {
        CodexVoiceActivitySnapshot active =
            CodexVoiceActivitySnapshot.Available(300, 0);
        FakeCodexVoiceActivitySource source = new(
            active,
            CodexVoiceActivitySnapshot.Available(300, 400));
        FakeCodexVoiceActivityClock clock = new();
        CodexVoiceActivityController controller = new(source, clock);

        DirectProtocolException? failure =
            controller.MonitorForLocalCloseAsync(
                new CodexVoiceStartConfirmation(
                    active,
                    StartedByBridge: true),
                TimeSpan.FromMilliseconds(250),
                CancellationToken.None).GetAwaiter().GetResult();

        Require(
            failure?.Code
                == CodexVoiceActivityController.ClosedLocallyCode
            && !failure.Retryable
            && clock.DelayCount == 1,
            "codex-voice-monitor-detects-local-close",
            checks);

        FakeCodexVoiceActivitySource replacementSource = new(
            active,
            CodexVoiceActivitySnapshot.Available(500, 0));
        DirectProtocolException? replacementFailure =
            new CodexVoiceActivityController(
                replacementSource,
                new FakeCodexVoiceActivityClock())
            .MonitorForLocalCloseAsync(
                new CodexVoiceStartConfirmation(
                    active,
                    StartedByBridge: true),
                TimeSpan.FromMilliseconds(250),
                CancellationToken.None).GetAwaiter().GetResult();
        Require(
            replacementFailure?.Code
                == CodexVoiceActivityController.ClosedLocallyCode,
            "codex-voice-monitor-detects-generation-replacement",
            checks);
    }

    private static void CheckOwnershipSafeShortcutStop(
        ICollection<string> checks)
    {
        CodexAppTarget ownedTarget = new(
            4242,
            new HashSet<uint> { 4242 },
            (nint)17);
        FakeCodexVoiceShortcutSender ownedSender = new();
        CodexVoiceActivityController ownedController = new(
            new FakeCodexVoiceActivitySource(
                CodexVoiceActivitySnapshot.Available(300, 0),
                CodexVoiceActivitySnapshot.Available(300, 400)),
            new FakeCodexVoiceActivityClock());
        WindowsDirectMediaAdapter.StopOwnedVoiceAsync(
            ownedController,
            ownedSender,
            () => ownedTarget,
            baseline: null,
            new CodexVoiceStartConfirmation(
                CodexVoiceActivitySnapshot.Available(300, 0),
                StartedByBridge: true),
            ownedTarget).GetAwaiter().GetResult();

        FakeCodexVoiceShortcutSender preexistingSender = new();
        WindowsDirectMediaAdapter.StopOwnedVoiceAsync(
            new CodexVoiceActivityController(
                new FakeCodexVoiceActivitySource(
                    CodexVoiceActivitySnapshot.Available(300, 0)),
                new FakeCodexVoiceActivityClock()),
            preexistingSender,
            () => throw new InvalidOperationException(
                "preexisting target provider must not run"),
            baseline: null,
            new CodexVoiceStartConfirmation(
                CodexVoiceActivitySnapshot.Available(300, 0),
                StartedByBridge: false),
            ownedTarget).GetAwaiter().GetResult();

        FakeCodexVoiceShortcutSender replacementSender = new();
        DirectProtocolException replacementFailure = Capture(
            () => WindowsDirectMediaAdapter.StopOwnedVoiceAsync(
                new CodexVoiceActivityController(
                    new FakeCodexVoiceActivitySource(
                        CodexVoiceActivitySnapshot.Available(500, 0)),
                    new FakeCodexVoiceActivityClock()),
                replacementSender,
                () => throw new InvalidOperationException(
                    "replacement target provider must not run"),
                baseline: null,
                new CodexVoiceStartConfirmation(
                    CodexVoiceActivitySnapshot.Available(300, 0),
                    StartedByBridge: true),
                ownedTarget));

        FakeCodexVoiceShortcutSender provisionalSender = new();
        WindowsDirectMediaAdapter.StopOwnedVoiceAsync(
            new CodexVoiceActivityController(
                new FakeCodexVoiceActivitySource(
                    CodexVoiceActivitySnapshot.Available(300, 0),
                    CodexVoiceActivitySnapshot.Available(300, 0),
                    CodexVoiceActivitySnapshot.Available(300, 400)),
                new FakeCodexVoiceActivityClock()),
            provisionalSender,
            () => ownedTarget,
            new CodexVoiceStartBaseline(
                CodexVoiceActivitySnapshot.Available(100, 200)),
            confirmation: null,
            ownedTarget).GetAwaiter().GetResult();

        Require(
            ownedSender.SendCount == 1
            && preexistingSender.SendCount == 0
            && replacementSender.SendCount == 0
            && replacementFailure.Code
                == "BW_COMPUTER_VOICE_DIRECT_VOICE_REPLACED_CLEANUP_PENDING"
            && replacementFailure.Retryable
            && provisionalSender.SendCount == 1,
            "codex-voice-stop-only-toggles-owned-same-generation",
            checks);
    }

    private static DirectProtocolException Capture(
        Func<Task> action)
    {
        try
        {
            action().GetAwaiter().GetResult();
        }
        catch (DirectProtocolException exception)
        {
            return exception;
        }
        throw new InvalidOperationException(
            "BW_COMPUTER_VOICE_ACTIVITY_EXPECTED_FAILURE_MISSING");
    }

    private static void Require(
        bool condition,
        string name,
        ICollection<string> checks)
    {
        if (!condition)
        {
            throw new InvalidOperationException(
                $"BW_COMPUTER_VOICE_ACTIVITY_SELF_TEST_FAILED:{name}");
        }
        checks.Add(name);
    }

    private sealed class FakeCodexVoiceActivitySource
        : ICodexVoiceActivitySource
    {
        private readonly Queue<CodexVoiceActivitySnapshot> _snapshots;
        private CodexVoiceActivitySnapshot _last;

        internal FakeCodexVoiceActivitySource(
            params CodexVoiceActivitySnapshot[] snapshots)
        {
            if (snapshots.Length == 0)
            {
                throw new ArgumentException(
                    "BW_COMPUTER_VOICE_ACTIVITY_FAKE_SOURCE_EMPTY",
                    nameof(snapshots));
            }
            _snapshots = new Queue<CodexVoiceActivitySnapshot>(snapshots);
            _last = snapshots[^1];
        }

        internal int ReadCount { get; private set; }

        public CodexVoiceActivitySnapshot Read()
        {
            ReadCount++;
            if (_snapshots.TryDequeue(
                out CodexVoiceActivitySnapshot? snapshot))
            {
                _last = snapshot;
            }
            return _last;
        }
    }

    private sealed class FakeCodexVoiceActivityClock
        : ICodexVoiceActivityClock
    {
        internal static readonly DateTimeOffset Start =
            new(2026, 7, 30, 0, 0, 0, TimeSpan.Zero);

        public DateTimeOffset UtcNow { get; private set; } = Start;

        internal int DelayCount { get; private set; }

        public Task DelayAsync(
            TimeSpan delay,
            CancellationToken cancellationToken)
        {
            cancellationToken.ThrowIfCancellationRequested();
            DelayCount++;
            UtcNow += delay;
            return Task.CompletedTask;
        }
    }

    private sealed class FakeCodexVoiceShortcutSender
        : ICodexVoiceShortcutSender
    {
        internal int SendCount { get; private set; }

        public void Send(CodexAppTarget target)
        {
            ArgumentNullException.ThrowIfNull(target);
            SendCount++;
        }
    }
}
