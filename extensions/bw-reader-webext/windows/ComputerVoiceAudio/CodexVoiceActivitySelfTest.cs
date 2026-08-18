namespace BwReader.ComputerVoiceAudio;

internal static class CodexVoiceActivitySelfTest
{
    internal static void Run(ICollection<string> checks)
    {
        CheckShortcutBrokerContract(checks);
        CheckActivityRule(checks);
        CheckPreexistingVoiceIsNotOwned(checks);
        CheckPreexistingVoiceUsesFastPath(checks);
        CheckNewVoiceIsOwned(checks);
        CheckStartedVoiceMustSettle(checks);
        CheckUnattestedTransitionFailsClosed(checks);
        CheckStartTimeoutAndReadError(checks);
        CheckStopConfirmation(checks);
        CheckLocalCloseMonitor(checks);
        CheckOwnershipValidationDoesNotToggleVoice(checks);
    }

    private static void CheckShortcutBrokerContract(
        ICollection<string> checks)
    {
        CodexAppTarget target = Target();
        string requestId =
            CodexVoiceShortcutBrokerContract.NewRequestId();
        string request =
            CodexVoiceShortcutBrokerContract.SerializeRequest(
                requestId,
                target);
        using System.Text.Json.JsonDocument document =
            System.Text.Json.JsonDocument.Parse(request);
        System.Text.Json.JsonElement root = document.RootElement;
        CodexVoiceShortcutBrokerContract.RequireSuccessfulReceipt(
            System.Text.Json.JsonSerializer.Serialize(new
            {
                contract = CodexVoiceShortcutBrokerContract.Contract,
                type = CodexVoiceShortcutBrokerContract.ReceiptType,
                requestId,
                ok = true,
            }),
            requestId);
        DirectProtocolException failure = Capture(() =>
        {
            CodexVoiceShortcutBrokerContract.RequireSuccessfulReceipt(
                System.Text.Json.JsonSerializer.Serialize(new
                {
                    contract =
                        CodexVoiceShortcutBrokerContract.Contract,
                    type =
                        CodexVoiceShortcutBrokerContract.ReceiptType,
                    requestId,
                    ok = false,
                    code =
                        "BW_COMPUTER_VOICE_SHORTCUT_BROKER_SEND_FAILED",
                }),
                requestId);
            return Task.CompletedTask;
        });
        bool invalidExtraRejected = false;
        try
        {
            CodexVoiceShortcutBrokerContract.RequireSuccessfulReceipt(
                System.Text.Json.JsonSerializer.Serialize(new
                {
                    contract =
                        CodexVoiceShortcutBrokerContract.Contract,
                    type =
                        CodexVoiceShortcutBrokerContract.ReceiptType,
                    requestId,
                    ok = true,
                    extra = true,
                }),
                requestId);
        }
        catch (DirectProtocolException exception) when (
            exception.Code
                == "BW_COMPUTER_VOICE_DIRECT_SHORTCUT_BROKER_RECEIPT_INVALID")
        {
            invalidExtraRejected = true;
        }
        Require(
            NamedPipeCodexVoiceShortcutBrokerTransport.PipeName
                == "bw-reader-codex-voice-shortcut-v1"
            && NamedPipeCodexVoiceShortcutBrokerTransport.ExchangeTimeout
                == TimeSpan.FromSeconds(2)
            && root.EnumerateObject().Count() == 6
            && root.GetProperty("contract").GetString()
                == "bw-codex-voice-shortcut/1"
            && root.GetProperty("type").GetString() == "toggle"
            && root.GetProperty("requestId").GetString() == requestId
            && root.GetProperty("rootProcessId").GetUInt32()
                == target.RootProcessId
            && root.GetProperty("rootProcessStartTimeUtc")
                .GetString()!.EndsWith('Z')
            && root.GetProperty("windowHandle").GetInt64()
                == target.WindowHandle.ToInt64()
            && failure.Code
                == "BW_COMPUTER_VOICE_SHORTCUT_BROKER_SEND_FAILED"
            && failure.Retryable
            && invalidExtraRejected,
            "codex-voice-shortcut-broker-one-shot-contract",
            checks);
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

        Require(
            WindowsDirectMediaAdapter.CreateVoiceOwnershipAttestor(
                DirectAppTargets.CodexDesktop)
                is ExactTargetVoiceOwnershipAttestor
            && WindowsDirectMediaAdapter.CreateVoiceOwnershipAttestor(
                DirectAppTargets.ChatGptClassic)
                is ExactTargetVoiceOwnershipAttestor,
            "voice-targets-attest-only-observed-owned-generation",
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
                shortcutReceipt: null,
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
        CodexVoiceActivityController controller = new(
            source,
            clock,
            new MatchingCodexVoiceOwnershipAttestor());

        CodexVoiceStartBaseline baseline =
            controller.CaptureStartBaseline();
        CodexVoiceShortcutReceipt receipt =
            controller.RecordShortcutSent(baseline, Target());
        CodexVoiceStartConfirmation confirmation =
            controller.ConfirmStartedAsync(
                baseline,
                receipt,
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

    private static void CheckPreexistingVoiceUsesFastPath(
        ICollection<string> checks)
    {
        CodexVoiceActivitySnapshot active =
            CodexVoiceActivitySnapshot.Available(300, 0);
        FakeCodexVoiceActivitySource fastSource = new(active, active);
        FakeCodexVoiceActivityClock fastClock = new();
        CodexVoiceActivityController fastController = new(
            fastSource,
            fastClock);
        CodexVoiceStartConfirmation fastConfirmation =
            fastController.ConfirmStartedAsync(
                fastController.CaptureStartBaseline(),
                shortcutReceipt: null,
                TimeSpan.FromSeconds(3),
                TimeSpan.FromMilliseconds(250),
                CancellationToken.None).GetAwaiter().GetResult();
        CodexVoiceStartConfirmation fastResult =
            fastController.ConfirmUsableForStartAsync(
                fastConfirmation,
                shortcutReceipt: null,
                TimeSpan.FromSeconds(8),
                CancellationToken.None).GetAwaiter().GetResult();

        CodexVoiceActivitySnapshot replacement =
            CodexVoiceActivitySnapshot.Available(500, 0);
        FakeCodexVoiceActivitySource changedSource = new(
            active,
            replacement,
            replacement);
        FakeCodexVoiceActivityClock changedClock = new();
        CodexVoiceActivityController changedController = new(
            changedSource,
            changedClock);
        CodexVoiceStartConfirmation changedConfirmation =
            changedController.ConfirmStartedAsync(
                changedController.CaptureStartBaseline(),
                shortcutReceipt: null,
                TimeSpan.FromSeconds(3),
                TimeSpan.FromMilliseconds(250),
                CancellationToken.None).GetAwaiter().GetResult();
        DirectProtocolException changedFailure = Capture(
            () => changedController.ConfirmUsableForStartAsync(
                changedConfirmation,
                shortcutReceipt: null,
                TimeSpan.FromSeconds(8),
                CancellationToken.None));

        FakeCodexVoiceActivitySource recoveringSource = new(
            active,
            CodexVoiceActivitySnapshot.Unavailable(),
            active);
        FakeCodexVoiceActivityClock recoveringClock = new();
        CodexVoiceActivityController recoveringController = new(
            recoveringSource,
            recoveringClock);
        CodexVoiceStartConfirmation recoveringConfirmation =
            recoveringController.ConfirmStartedAsync(
                recoveringController.CaptureStartBaseline(),
                shortcutReceipt: null,
                TimeSpan.FromSeconds(3),
                TimeSpan.FromMilliseconds(250),
                CancellationToken.None).GetAwaiter().GetResult();
        CodexVoiceStartConfirmation recovered =
            recoveringController.ConfirmUsableForStartAsync(
                recoveringConfirmation,
                shortcutReceipt: null,
                TimeSpan.FromSeconds(8),
                CancellationToken.None).GetAwaiter().GetResult();

        Require(
            fastResult.Snapshot.Active
            && fastResult.Snapshot.LastUsedTimeStart == 300
            && fastSource.ReadCount == 2
            && fastClock.DelayCount == 0
            && changedFailure.Code
                == CodexVoiceActivityController.StartNotConfirmedCode
            && changedFailure.Retryable
            && changedSource.ReadCount == 3
            && changedClock.DelayCount == 1
            && changedClock.UtcNow
                == FakeCodexVoiceActivityClock.Start
                    + TimeSpan.FromSeconds(8),
            "codex-voice-preexisting-same-generation-skips-settle",
            checks);
        Require(
            recovered.Snapshot.Active
            && recovered.Snapshot.LastUsedTimeStart == 300
            && recoveringSource.ReadCount == 3
            && recoveringClock.DelayCount == 1
            && recoveringClock.UtcNow
                == FakeCodexVoiceActivityClock.Start
                    + TimeSpan.FromSeconds(8),
            "codex-voice-preexisting-unavailable-fast-check-keeps-bounded-recovery",
            checks);
    }

    private static void CheckUnattestedTransitionFailsClosed(
        ICollection<string> checks)
    {
        FakeCodexVoiceActivitySource source = new(
            CodexVoiceActivitySnapshot.Available(100, 200),
            CodexVoiceActivitySnapshot.Available(300, 0),
            CodexVoiceActivitySnapshot.Available(300, 0));
        CodexVoiceActivityController controller = new(
            source,
            new FakeCodexVoiceActivityClock());
        CodexVoiceStartBaseline baseline =
            controller.CaptureStartBaseline();
        CodexVoiceStartConfirmation confirmation =
            controller.ConfirmStartedAsync(
                baseline,
                controller.RecordShortcutSent(baseline, Target()),
                TimeSpan.FromSeconds(1),
                TimeSpan.FromMilliseconds(250),
                CancellationToken.None).GetAwaiter().GetResult();
        FakeCodexVoiceShortcutSender sender = new();
        DirectProtocolException stopFailure = Capture(
            () => WindowsDirectMediaAdapter.StopOwnedVoiceAsync(
                controller,
                sender,
                () => Target(),
                baseline: null,
                confirmation,
                Target()));
        Require(
            confirmation.ObservedAfterShortcut
            && !confirmation.OwnsVoice
            && sender.SendCount == 0
            && stopFailure.Code
                == "BW_COMPUTER_VOICE_DIRECT_VOICE_OWNERSHIP_UNCONFIRMED",
            "codex-voice-unattested-transition-is-never-auto-closed",
            checks);
    }

    private static void CheckStartedVoiceMustSettle(
        ICollection<string> checks)
    {
        CodexVoiceActivitySnapshot active =
            CodexVoiceActivitySnapshot.Available(300, 0);
        FakeCodexVoiceActivityClock stableClock = new();
        CodexVoiceActivityController stableController = new(
            new FakeCodexVoiceActivitySource(active),
            stableClock);
        CodexVoiceStartConfirmation stable =
            stableController.ConfirmUsableForStartAsync(
                new CodexVoiceStartConfirmation(
                    active,
                    ObservedAfterShortcut: true,
                    OwnershipToken: Token(300)),
                shortcutReceipt: null,
                TimeSpan.FromSeconds(8),
                CancellationToken.None).GetAwaiter().GetResult();

        FakeCodexVoiceActivityClock closedClock = new();
        CodexVoiceActivityController closedController = new(
            new FakeCodexVoiceActivitySource(
                CodexVoiceActivitySnapshot.Available(300, 400)),
            closedClock);
        DirectProtocolException closedFailure = Capture(
            () => closedController.ConfirmUsableForStartAsync(
                new CodexVoiceStartConfirmation(
                    active,
                    ObservedAfterShortcut: true,
                    OwnershipToken: Token(300)),
                shortcutReceipt: null,
                TimeSpan.FromSeconds(8),
                CancellationToken.None));

        Require(
            stable.Snapshot.Active
            && stableClock.UtcNow
                == FakeCodexVoiceActivityClock.Start
                    + TimeSpan.FromSeconds(8)
            && stableClock.DelayCount == 1
            && closedFailure.Code
                == CodexVoiceActivityController.StartNotConfirmedCode
            && closedFailure.Retryable
            && closedClock.DelayCount == 1,
            "codex-voice-start-waits-for-usable-stable-generation",
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
                timeoutController.RecordShortcutSent(
                    timeoutBaseline,
                    Target()),
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
                errorController.RecordShortcutSent(
                    errorBaseline,
                    Target()),
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
                unavailableController.RecordShortcutSent(
                    unavailableBaseline,
                    Target()),
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
            ObservedAfterShortcut: true,
            OwnershipToken: Token(300));
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
                    ObservedAfterShortcut: true,
                    OwnershipToken: Token(300)));

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
                    ObservedAfterShortcut: true,
                    OwnershipToken: Token(300)),
                TimeSpan.FromMilliseconds(250),
                CancellationToken.None).GetAwaiter().GetResult();

        // 关闭要连续多帧都成立才算数（2026-08-18）：源在第 2 次读起一直返回"已关闭"，
        // 所以第 1 次 delay 之后还要再等 N-1 次才定罪。
        Require(
            failure?.Code
                == CodexVoiceActivityController.ClosedLocallyCode
            && !failure.Retryable
            && clock.DelayCount
                == CodexVoiceActivityController.LocalCloseConfirmations,
            "codex-voice-monitor-detects-local-close",
            checks);

        // 而单帧的闪烁**不得**挂断通话 —— 这正是旧的单帧定罪造成"说着说着自己没了"
        // 的地方：两个时间戳分两次读、没有原子快照，撕裂读会把活着的通话读成已结束。
        CodexVoiceActivitySnapshot live =
            CodexVoiceActivitySnapshot.Available(300, 0);
        FakeCodexVoiceActivitySource flapping = new(
            live,
            CodexVoiceActivitySnapshot.Available(300, 400),   // 一帧看着像关了
            live,                                            // 立刻又活着
            CodexVoiceActivitySnapshot.Available(500, 0));   // 换代 → 才是真关闭
        FakeCodexVoiceActivityClock flappingClock = new();
        CodexVoiceActivityController flappingController =
            new(flapping, flappingClock);
        DirectProtocolException? flappingFailure =
            flappingController.MonitorForLocalCloseAsync(
                new CodexVoiceStartConfirmation(
                    live,
                    ObservedAfterShortcut: true,
                    OwnershipToken: Token(300)),
                TimeSpan.FromMilliseconds(250),
                CancellationToken.None).GetAwaiter().GetResult();
        Require(
            flappingFailure?.Code
                == CodexVoiceActivityController.ClosedLocallyCode
            && flappingClock.DelayCount
                > CodexVoiceActivityController.LocalCloseConfirmations,
            "codex-voice-monitor-ignores-single-frame-flap",
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
                    ObservedAfterShortcut: true,
                    OwnershipToken: Token(300)),
                TimeSpan.FromMilliseconds(250),
                CancellationToken.None).GetAwaiter().GetResult();
        Require(
            replacementFailure?.Code
                == CodexVoiceActivityController.ClosedLocallyCode,
            "codex-voice-monitor-detects-generation-replacement",
            checks);
    }

    private static void CheckOwnershipValidationDoesNotToggleVoice(
        ICollection<string> checks)
    {
        CodexAppTarget ownedTarget = new(
            4242,
            133700000000000000,
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
                ObservedAfterShortcut: true,
                OwnershipToken: Token(300)),
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
                ObservedAfterShortcut: false,
                OwnershipToken: null),
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
                    ObservedAfterShortcut: true,
                    OwnershipToken: Token(300)),
                ownedTarget));

        FakeCodexVoiceShortcutSender provisionalSender = new();
        DirectProtocolException provisionalFailure = Capture(
            () => WindowsDirectMediaAdapter.StopOwnedVoiceAsync(
            new CodexVoiceActivityController(
                new FakeCodexVoiceActivitySource(
                    CodexVoiceActivitySnapshot.Available(300, 0)),
                new FakeCodexVoiceActivityClock()),
            provisionalSender,
            () => ownedTarget,
            new CodexVoiceStartBaseline(
                CodexVoiceActivitySnapshot.Available(100, 200)),
            confirmation: null,
            ownedTarget));

        Require(
            ownedSender.SendCount == 0
            && preexistingSender.SendCount == 0
            && replacementSender.SendCount == 0
            && replacementFailure.Code
                == "BW_COMPUTER_VOICE_DIRECT_VOICE_REPLACED_CLEANUP_PENDING"
            && replacementFailure.Retryable
            && provisionalSender.SendCount == 0
            && provisionalFailure.Code
                == "BW_COMPUTER_VOICE_DIRECT_VOICE_OWNERSHIP_UNCONFIRMED",
            "codex-voice-stop-validates-ownership-without-toggle",
            checks);
    }

    private static CodexAppTarget Target() => new(
        4242,
        133700000000000000,
        new HashSet<uint> { 4242 },
        (nint)17);

    private static CodexVoiceOwnershipToken Token(
        long voiceStartFileTimeUtc) =>
        new(
            Guid.Parse("11111111-1111-1111-1111-111111111111"),
            4242,
            133700000000000000,
            voiceStartFileTimeUtc);

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

        public void Send(
            CodexAppTarget target,
            DirectVoiceCommand command)
        {
            ArgumentNullException.ThrowIfNull(target);
            SendCount++;
        }
    }

    private sealed class MatchingCodexVoiceOwnershipAttestor
        : ICodexVoiceOwnershipAttestor
    {
        public CodexVoiceOwnershipToken? TryAttest(
            CodexVoiceShortcutReceipt receipt,
            CodexVoiceActivitySnapshot observedTransition) =>
            new(
                receipt.AttemptId,
                receipt.RootProcessId,
                receipt.RootProcessStartFileTimeUtc,
                observedTransition.LastUsedTimeStart);
    }
}
