using System.Runtime.InteropServices;
using System.Text.Json;

namespace BwReader.ComputerVoiceAudio;

internal static class PerAppAudioRouteSelfTest
{
    private const uint ProcessId = 4242;
    private const string VirtualRender =
        "{0.0.0.00000000}.{11111111-1111-1111-1111-111111111111}";
    private const string VirtualCapture =
        "{0.0.1.00000000}.{22222222-2222-2222-2222-222222222222}";
    private const string PhysicalRender =
        "{0.0.0.00000000}.{33333333-3333-3333-3333-333333333333}";
    private const string PhysicalCapture =
        "{0.0.1.00000000}.{44444444-4444-4444-4444-444444444444}";
    private const string ExternalRender =
        "{0.0.0.00000000}.{55555555-5555-5555-5555-555555555555}";

    internal static void Run(ICollection<string> checks)
    {
        CheckExactSixTupleContract(checks);
        CheckRegistrySchemaIsPinnedAndScoped(checks);
        CheckWindowsIdentityHashVectors(checks);
        CheckNativeBackendConstructionWithoutMutation(checks);
        CheckPersistedRegistryValueNamesAreExact(checks);
        CheckEndpointPackingIsFlowSpecific(checks);
        CheckReadOnlyProbeReportsSixRoutesWithoutWrites(checks);
        CheckPreexistingTargetSignalIsExact(checks);
        CheckTransactionRestoresAndPreservesExternalChange(checks);
        CheckLeaseRevalidationFailsClosed(checks);
        CheckApplyFailureRollsBack(checks);
        CheckReadbackMismatchRollsBack(checks);
        CheckSnapshotErrorHasNoSideEffect(checks);
        CheckIncompleteRollbackKeepsJournal(checks);
        CheckRestoreCanRetryAfterTransientFailure(checks);
        CheckCrashJournalRecoversConditionally(checks);
        CheckInvalidCrashJournalHasNoWrites(checks);
    }

    private static void CheckWindowsIdentityHashVectors(
        ICollection<string> checks)
    {
        if (!OperatingSystem.IsWindows())
        {
            checks.Add(
                "per-app-audio-route-windows-identity-hash-vectors-windows-only");
            return;
        }
        const string codexIdentity =
            "OpenAI.Codex_2p2nqsd0c76g0!App";
        const string chromeIdentity =
            @"\Device\HarddiskVolume3\Program Files\Google\Chrome\Application\chrome.exe";
        Require(
            WindowsPersistedAppAudioRouteStore.ComputeIdentityKeyHash(
                codexIdentity) == "346b7ff"
            && WindowsPersistedAppAudioRouteStore.ComputeIdentityKeyHash(
                chromeIdentity) == "44813011",
            "per-app-audio-route-windows-identity-hash-vectors",
            checks);
    }

    private static void CheckRegistrySchemaIsPinnedAndScoped(
        ICollection<string> checks)
    {
        Require(
            (int)PerAppAudioDataFlow.Render == 0
            && (int)PerAppAudioDataFlow.Capture == 1
            && (int)PerAppAudioRole.Console == 0
            && (int)PerAppAudioRole.Multimedia == 1
            && (int)PerAppAudioRole.Communications == 2,
            "per-app-audio-registry-schema-is-pinned-and-scoped",
            checks);
    }

    private static void CheckNativeBackendConstructionWithoutMutation(
        ICollection<string> checks)
    {
        if (!OperatingSystem.IsWindows())
        {
            checks.Add(
                "per-app-audio-policy-live-read-windows-only");
            return;
        }
        using NativePerAppAudioPolicyBackend backend = new();
        Require(
            backend is not null,
            "per-app-audio-registry-backend-construction-without-mutation",
            checks);
    }

    private static void CheckPersistedRegistryValueNamesAreExact(
        ICollection<string> checks)
    {
        string[] names = PerAppAudioRouteKey.All.Select(
            key => key.RegistryValueName).ToArray();
        Require(
            names.SequenceEqual(
            [
                "000_000",
                "001_000",
                "002_000",
                "000_001",
                "001_001",
                "002_001",
            ])
            && names.Distinct(StringComparer.Ordinal).Count() == 6,
            "per-app-audio-registry-six-role-flow-value-names",
            checks);
    }

    private static void CheckLeaseRevalidationFailsClosed(
        ICollection<string> checks)
    {
        using TemporaryRouteJournal journal = new();
        using FakePerAppAudioPolicyBackend backend =
            FakePerAppAudioPolicyBackend.WithMixedOriginals();
        PerAppAudioRouteController controller = new(backend);
        PerAppAudioRouteLease lease =
            controller.Acquire(Request(journal.Path));
        lease.RequireStillApplied();
        backend.SetExternal(
            new(
                PerAppAudioDataFlow.Capture,
                PerAppAudioRole.Communications),
            PersistedAudioEndpoint.Present(PhysicalCapture));
        DirectProtocolException changed = Capture(
            lease.RequireStillApplied);
        PerAppAudioRouteRestoreResult restored = lease.Restore();
        Require(
            changed.Code
                == "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_CHANGED"
            && restored.Succeeded
            && restored.PreservedExternalChanges.Contains(
                new(
                    PerAppAudioDataFlow.Capture,
                    PerAppAudioRole.Communications))
            && !File.Exists(journal.Path),
            "per-app-audio-route-revalidated-before-shortcut",
            checks);
    }

    private static void CheckExactSixTupleContract(
        ICollection<string> checks)
    {
        Require(
            PerAppAudioRouteKey.All.Count == 6
            && PerAppAudioRouteKey.All.Distinct().Count() == 6
            && PerAppAudioRouteKey.All.Count(
                key => key.Flow == PerAppAudioDataFlow.Render) == 3
            && PerAppAudioRouteKey.All.Count(
                key => key.Flow == PerAppAudioDataFlow.Capture) == 3
            && Enum.GetValues<PerAppAudioRole>().All(role =>
                PerAppAudioRouteKey.All.Contains(
                    new(
                        PerAppAudioDataFlow.Render,
                        role))
                && PerAppAudioRouteKey.All.Contains(
                    new(
                        PerAppAudioDataFlow.Capture,
                        role))),
            "per-app-audio-route-six-flow-role-tuples",
            checks);
    }

    private static void CheckEndpointPackingIsFlowSpecific(
        ICollection<string> checks)
    {
        string packedRender = AudioPolicyEndpointId.Pack(
            VirtualRender,
            PerAppAudioDataFlow.Render);
        string packedCapture = AudioPolicyEndpointId.Pack(
            VirtualCapture,
            PerAppAudioDataFlow.Capture);
        bool renderRoundTrip = AudioPolicyEndpointId.TryUnpack(
            packedRender,
            PerAppAudioDataFlow.Render,
            out string unpackedRender);
        bool captureRoundTrip = AudioPolicyEndpointId.TryUnpack(
            packedCapture,
            PerAppAudioDataFlow.Capture,
            out string unpackedCapture);
        bool wrongFlowRejected = !AudioPolicyEndpointId.TryUnpack(
            packedRender,
            PerAppAudioDataFlow.Capture,
            out _);
        bool captureMustBeExplicit = false;
        try
        {
            AudioPolicyEndpointId.ValidateForFlow(
                VirtualRender,
                PerAppAudioDataFlow.Capture);
        }
        catch (DirectProtocolException exception)
            when (
                exception.Code
                == "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_ENDPOINT_FLOW_MISMATCH"
            )
        {
            captureMustBeExplicit = true;
        }
        Require(
            renderRoundTrip
            && captureRoundTrip
            && wrongFlowRejected
            && captureMustBeExplicit
            && unpackedRender == VirtualRender
            && unpackedCapture == VirtualCapture
            && packedRender.Contains(
                "e6327cad-dcec-4949-ae8a-991e976a79d2",
                StringComparison.OrdinalIgnoreCase)
            && packedCapture.Contains(
                "2eef81be-33fa-4800-9670-1cd474972c3f",
                StringComparison.OrdinalIgnoreCase),
            "per-app-audio-route-render-capture-endpoints-are-distinct",
            checks);
    }

    private static void CheckPreexistingTargetSignalIsExact(
        ICollection<string> checks)
    {
        using TemporaryRouteJournal targetedJournal = new();
        using FakePerAppAudioPolicyBackend targetedBackend =
            FakePerAppAudioPolicyBackend.WithTargets();
        PerAppAudioRouteLease targeted =
            new PerAppAudioRouteController(targetedBackend).Acquire(
                Request(targetedJournal.Path));

        using TemporaryRouteJournal mixedJournal = new();
        using FakePerAppAudioPolicyBackend mixedBackend =
            FakePerAppAudioPolicyBackend.WithMixedOriginals();
        PerAppAudioRouteLease mixed =
            new PerAppAudioRouteController(mixedBackend).Acquire(
                Request(mixedJournal.Path));

        Require(
            targeted.AlreadyTargetedBeforeAcquire
            && !mixed.AlreadyTargetedBeforeAcquire,
            "per-app-audio-route-preexisting-target-signal-is-all-six",
            checks);
        targeted.Dispose();
        mixed.Dispose();
    }

    private static void
        CheckReadOnlyProbeReportsSixRoutesWithoutWrites(
            ICollection<string> checks)
    {
        using FakePerAppAudioPolicyBackend backend =
            FakePerAppAudioPolicyBackend.WithMixedOriginals();
        backend.SetExternal(
            new(
                PerAppAudioDataFlow.Capture,
                PerAppAudioRole.Communications),
            PersistedAudioEndpoint.Error(
                unchecked((int)0x80004005),
                "fake-probe-read"));
        DirectBridgeConfig config = new(
            Path: @"C:\bw-test\native-host\config.json",
            LocalOptIn: true,
            VirtualMicrophoneRenderEndpointId: PhysicalRender,
            VirtualMicrophoneCaptureEndpointId: VirtualCapture,
            VirtualSpeakerRenderEndpointId: VirtualRender,
            ListenHost: DirectBridgeContract.ListenHost,
            ListenPort: DirectBridgeContract.DefaultListenPort,
            AllowedOrigins: new HashSet<string>(
                StringComparer.Ordinal)
            {
                "https://bwicarus.taile44d0c.ts.net",
            },
            AllowedTailscaleUserLogin: "bwicarus@gmail.com",
            ExperimentalSingleUserMode: true,
            OutputScope: AudioBridgeContract.CaptureScope,
            AppKind: "codex-desktop",
            RuntimeStatusPath:
                @"C:\bw-test\runtime\computer-voice-direct.status.json",
            ContextDeliveryMode:
                DirectContextDeliveryMode.SnapshotMcp,
            PerAppAudioRouteAutomationEnabled: true);
        object result = CodexAppAudioRouteProbe.Run(
            config,
            () => new CodexAppTarget(
                ProcessId,
                new HashSet<uint> { ProcessId },
                WindowHandle: 1),
            () => backend);
        using JsonDocument document = JsonDocument.Parse(
            JsonSerializer.Serialize(
                result,
                DirectBridgeContract.JsonOptions));
        JsonElement root = document.RootElement;
        JsonElement routes = root.GetProperty("routes");
        HashSet<string> states = routes.EnumerateArray()
            .Select(item =>
                item.GetProperty("state").GetString() ?? "")
            .ToHashSet(StringComparer.Ordinal);
        bool exactRouteShapes = routes.EnumerateArray().All(item =>
            item.EnumerateObject()
                .Select(property => property.Name)
                .ToHashSet(StringComparer.Ordinal)
                .SetEquals(
                [
                    "flow",
                    "role",
                    "target",
                    "state",
                    "endpointId",
                    "match",
                    "hResult",
                    "stage",
                ]));
        bool targetsFollowFlow = routes.EnumerateArray().All(item =>
            item.GetProperty("target").GetString()
                == (
                    item.GetProperty("flow").GetString() == "render"
                        ? VirtualRender
                        : VirtualCapture
                ));
        Require(
            root.GetProperty("contract").GetString()
                == CodexAppAudioRouteProbe.Contract
            && root.GetProperty("ok").GetBoolean()
            && root.GetProperty("processId").GetUInt32()
                == ProcessId
            && root.GetProperty(
                "automationConfigured").GetBoolean()
            && !root.GetProperty("allMatch").GetBoolean()
            && !root.GetProperty(
                "audioRouteMutated").GetBoolean()
            && !root.GetProperty("captureStarted").GetBoolean()
            && !root.GetProperty("shortcutSent").GetBoolean()
            && !root.GetProperty("appLaunched").GetBoolean()
            && routes.GetArrayLength() == 6
            && states.SetEquals(["Present", "Unset", "Error"])
            && exactRouteShapes
            && targetsFollowFlow
            && backend.ReadCount == 6
            && backend.Writes.Count == 0,
            "per-app-audio-route-cli-probe-is-read-only-and-six-role",
            checks);

        int legacyBackendFactoryCalls = 0;
        DirectBridgeConfig legacy = config with
        {
            VirtualMicrophoneCaptureEndpointId = "",
            PerAppAudioRouteAutomationEnabled = false,
        };
        DirectProtocolException legacyRejected = Capture(() =>
            CodexAppAudioRouteProbe.Run(
                legacy,
                () => throw new InvalidOperationException(
                    "legacy must fail before process probe"),
                () =>
                {
                    legacyBackendFactoryCalls++;
                    return backend;
                }));
        Require(
            legacyRejected.Code
                == "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_NOT_CONFIGURED"
            && legacyBackendFactoryCalls == 0
            && backend.Writes.Count == 0,
            "per-app-audio-route-cli-probe-requires-explicit-v5",
            checks);
    }

    private static void
        CheckTransactionRestoresAndPreservesExternalChange(
            ICollection<string> checks)
    {
        using TemporaryRouteJournal journal = new();
        using FakePerAppAudioPolicyBackend backend =
            FakePerAppAudioPolicyBackend.WithMixedOriginals();
        PerAppAudioRouteController controller = new(backend);
        PerAppAudioRouteRequest request = Request(journal.Path);
        PerAppAudioRouteLease lease = controller.Acquire(request);

        bool allTargetsApplied = PerAppAudioRouteKey.All.All(key =>
            backend.Read(ProcessId, key) is
            {
                Kind: PersistedAudioEndpointKind.Present,
                EndpointId: not null,
            } value
            && string.Equals(
                value.EndpointId,
                request.TargetFor(key),
                StringComparison.OrdinalIgnoreCase));
        PerAppAudioRouteKey externallyChanged = new(
            PerAppAudioDataFlow.Render,
            PerAppAudioRole.Multimedia);
        backend.SetExternal(
            externallyChanged,
            PersistedAudioEndpoint.Present(ExternalRender));

        PerAppAudioRouteRestoreResult restored = lease.Restore();
        Require(
            allTargetsApplied
            && File.Exists(journal.Path) == false
            && restored.Succeeded
            && restored.PreservedExternalChanges.SequenceEqual(
                [externallyChanged])
            && backend.Read(ProcessId, externallyChanged).EndpointId
                == ExternalRender
            && backend.Read(
                ProcessId,
                new(
                    PerAppAudioDataFlow.Render,
                    PerAppAudioRole.Console)).EndpointId
                == PhysicalRender
            && backend.Read(
                ProcessId,
                new(
                    PerAppAudioDataFlow.Capture,
                    PerAppAudioRole.Console)).EndpointId
                == PhysicalCapture
            && backend.Read(
                ProcessId,
                new(
                    PerAppAudioDataFlow.Capture,
                    PerAppAudioRole.Multimedia)).Kind
                == PersistedAudioEndpointKind.Unset
            && backend.Writes.Any(write =>
                write.EndpointId is null),
            "per-app-audio-route-restore-is-conditional-and-exact",
            checks);
    }

    private static void CheckApplyFailureRollsBack(
        ICollection<string> checks)
    {
        using TemporaryRouteJournal journal = new();
        using FakePerAppAudioPolicyBackend backend =
            FakePerAppAudioPolicyBackend.WithMixedOriginals();
        backend.FailWriteNumbers.Add(4);
        PerAppAudioRouteController controller = new(backend);
        DirectProtocolException failure = Capture(
            () => controller.Acquire(Request(journal.Path)));
        Require(
            failure.Code
                == "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_SET_FAILED"
            && backend.MatchesMixedOriginals()
            && !File.Exists(journal.Path),
            "per-app-audio-route-set-failure-rolls-back",
            checks);
    }

    private static void CheckReadbackMismatchRollsBack(
        ICollection<string> checks)
    {
        using TemporaryRouteJournal journal = new();
        using FakePerAppAudioPolicyBackend backend =
            FakePerAppAudioPolicyBackend.WithMixedOriginals();
        backend.IgnoreWriteNumbers.Add(2);
        PerAppAudioRouteController controller = new(backend);
        DirectProtocolException failure = Capture(
            () => controller.Acquire(Request(journal.Path)));
        Require(
            failure.Code
                == "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_READBACK_FAILED"
            && backend.MatchesMixedOriginals()
            && !File.Exists(journal.Path),
            "per-app-audio-route-readback-mismatch-rolls-back",
            checks);
    }

    private static void CheckSnapshotErrorHasNoSideEffect(
        ICollection<string> checks)
    {
        using TemporaryRouteJournal journal = new();
        using FakePerAppAudioPolicyBackend backend =
            FakePerAppAudioPolicyBackend.WithMixedOriginals();
        backend.SetExternal(
            new(
                PerAppAudioDataFlow.Capture,
                PerAppAudioRole.Communications),
            PersistedAudioEndpoint.Error(
                unchecked((int)0x80004005),
                "fake-read"));
        PerAppAudioRouteController controller = new(backend);
        DirectProtocolException failure = Capture(
            () => controller.Acquire(Request(journal.Path)));
        Require(
            failure.Code
                == "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_SNAPSHOT_FAILED"
            && backend.Writes.Count == 0
            && !File.Exists(journal.Path),
            "per-app-audio-route-snapshot-error-has-no-writes",
            checks);
    }

    private static void CheckIncompleteRollbackKeepsJournal(
        ICollection<string> checks)
    {
        using TemporaryRouteJournal journal = new();
        using FakePerAppAudioPolicyBackend backend =
            FakePerAppAudioPolicyBackend.WithMixedOriginals();
        backend.FailWriteNumbers.Add(4);
        backend.FailWriteNumbers.Add(5);
        PerAppAudioRouteController controller = new(backend);
        DirectProtocolException failure = Capture(
            () => controller.Acquire(Request(journal.Path)));
        Require(
            failure.Code
                == "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_ROLLBACK_INCOMPLETE"
            && File.Exists(journal.Path),
            "per-app-audio-route-incomplete-rollback-keeps-journal",
            checks);
    }

    private static void CheckRestoreCanRetryAfterTransientFailure(
        ICollection<string> checks)
    {
        using TemporaryRouteJournal journal = new();
        using FakePerAppAudioPolicyBackend backend =
            FakePerAppAudioPolicyBackend.WithMixedOriginals();
        PerAppAudioRouteController controller = new(backend);
        PerAppAudioRouteLease lease =
            controller.Acquire(Request(journal.Path));
        backend.FailWriteNumbers.Add(7);
        PerAppAudioRouteRestoreResult first = lease.Restore();
        bool journalKeptAfterFirst = File.Exists(journal.Path);
        backend.FailWriteNumbers.Clear();
        PerAppAudioRouteRestoreResult second = lease.Restore();
        Require(
            !first.Succeeded
            && journalKeptAfterFirst
            && File.Exists(journal.Path) == false
            && second.Succeeded
            && backend.MatchesMixedOriginals(),
            "per-app-audio-route-restore-retries-transient-failure",
            checks);
    }

    private static void CheckCrashJournalRecoversConditionally(
        ICollection<string> checks)
    {
        using TemporaryRouteJournal journal = new();
        using FakePerAppAudioPolicyBackend backend =
            FakePerAppAudioPolicyBackend.WithMixedOriginals();
        PerAppAudioRouteController abandonedController = new(backend);
        _ = abandonedController.Acquire(Request(journal.Path));
        PerAppAudioRouteKey externallyChanged = new(
            PerAppAudioDataFlow.Render,
            PerAppAudioRole.Communications);
        backend.SetExternal(
            externallyChanged,
            PersistedAudioEndpoint.Present(ExternalRender));

        PerAppAudioRouteController recoveryController = new(backend);
        PerAppAudioRouteRestoreResult result =
            recoveryController.RecoverPending(
                ProcessId,
                journal.Path);
        Require(
            result.Succeeded
            && !File.Exists(journal.Path)
            && result.PreservedExternalChanges.SequenceEqual(
                [externallyChanged])
            && backend.Read(ProcessId, externallyChanged).EndpointId
                == ExternalRender
            && backend.Read(
                ProcessId,
                new(
                    PerAppAudioDataFlow.Render,
                    PerAppAudioRole.Console)).EndpointId
                == PhysicalRender
            && backend.Read(
                ProcessId,
                new(
                    PerAppAudioDataFlow.Capture,
                    PerAppAudioRole.Console)).EndpointId
                == PhysicalCapture,
            "per-app-audio-route-crash-journal-recovers-conditionally",
            checks);
    }

    private static void CheckInvalidCrashJournalHasNoWrites(
        ICollection<string> checks)
    {
        using TemporaryRouteJournal journal = new();
        Directory.CreateDirectory(
            System.IO.Path.GetDirectoryName(journal.Path)!);
        File.WriteAllText(
            journal.Path,
            "{\"contract\":\"wrong\"}");
        using FakePerAppAudioPolicyBackend backend =
            FakePerAppAudioPolicyBackend.WithMixedOriginals();
        PerAppAudioRouteController controller = new(backend);
        DirectProtocolException failure = Capture(() =>
            controller.RecoverPending(ProcessId, journal.Path));
        Require(
            failure.Code
                == "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_JOURNAL_INVALID"
            && backend.Writes.Count == 0
            && File.Exists(journal.Path),
            "per-app-audio-route-invalid-journal-fails-closed",
            checks);
    }

    private static PerAppAudioRouteRequest Request(
        string journalPath) =>
        new(
            ProcessId,
            VirtualRender,
            VirtualCapture,
            journalPath);

    private static DirectProtocolException Capture(Action action)
    {
        try
        {
            action();
        }
        catch (DirectProtocolException exception)
        {
            return exception;
        }
        throw new InvalidOperationException(
            "Expected DirectProtocolException");
    }

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

    private sealed class TemporaryRouteJournal : IDisposable
    {
        private readonly string _directory = System.IO.Path.Combine(
            System.IO.Path.GetTempPath(),
            "bw-audio-route-self-test-"
                + Guid.NewGuid().ToString("N"));

        internal TemporaryRouteJournal()
        {
            Path = System.IO.Path.Combine(
                _directory,
                "route-transaction.json");
        }

        internal string Path { get; }

        public void Dispose()
        {
            if (Directory.Exists(_directory))
            {
                Directory.Delete(_directory, recursive: true);
            }
        }
    }

    private sealed class FakePerAppAudioPolicyBackend :
        IPerAppAudioPolicyBackend
    {
        private readonly Dictionary<
            PerAppAudioRouteKey,
            PersistedAudioEndpoint> _routes;
        private int _writeNumber;

        private FakePerAppAudioPolicyBackend(
            Dictionary<
                PerAppAudioRouteKey,
                PersistedAudioEndpoint> routes)
        {
            _routes = routes;
        }

        internal HashSet<int> FailWriteNumbers { get; } = [];

        internal HashSet<int> IgnoreWriteNumbers { get; } = [];

        internal List<(
            PerAppAudioRouteKey Key,
            string? EndpointId)> Writes
        { get; } = [];

        internal int ReadCount { get; private set; }

        internal static FakePerAppAudioPolicyBackend
            WithMixedOriginals()
        {
            Dictionary<
                PerAppAudioRouteKey,
                PersistedAudioEndpoint> values = [];
            foreach (PerAppAudioRouteKey key in PerAppAudioRouteKey.All)
            {
                values[key] = key switch
                {
                    {
                        Flow: PerAppAudioDataFlow.Render,
                        Role: PerAppAudioRole.Console,
                    } => PersistedAudioEndpoint.Present(PhysicalRender),
                    {
                        Flow: PerAppAudioDataFlow.Capture,
                        Role: PerAppAudioRole.Console,
                    } => PersistedAudioEndpoint.Present(
                        PhysicalCapture),
                    _ => PersistedAudioEndpoint.Unset(),
                };
            }
            return new(values);
        }

        internal static FakePerAppAudioPolicyBackend WithTargets()
        {
            Dictionary<
                PerAppAudioRouteKey,
                PersistedAudioEndpoint> values = [];
            foreach (PerAppAudioRouteKey key in PerAppAudioRouteKey.All)
            {
                values[key] = PersistedAudioEndpoint.Present(
                    key.Flow == PerAppAudioDataFlow.Render
                        ? VirtualRender
                        : VirtualCapture);
            }
            return new(values);
        }

        public PersistedAudioEndpoint Read(
            uint processId,
            PerAppAudioRouteKey key)
        {
            ReadCount++;
            if (processId != ProcessId)
            {
                return PersistedAudioEndpoint.Error(
                    unchecked((int)0x80070057),
                    "fake-process");
            }
            return _routes[key];
        }

        public PerAppAudioPolicyWriteResult Write(
            uint processId,
            PerAppAudioRouteKey key,
            string? endpointId)
        {
            _writeNumber++;
            Writes.Add((key, endpointId));
            if (FailWriteNumbers.Contains(_writeNumber))
            {
                return PerAppAudioPolicyWriteResult.Failure(
                    unchecked((int)0x80004005),
                    "fake-write");
            }
            if (!IgnoreWriteNumbers.Contains(_writeNumber))
            {
                _routes[key] = endpointId is null
                    ? PersistedAudioEndpoint.Unset()
                    : PersistedAudioEndpoint.Present(endpointId);
            }
            return PerAppAudioPolicyWriteResult.Success();
        }

        internal void SetExternal(
            PerAppAudioRouteKey key,
            PersistedAudioEndpoint value)
        {
            _routes[key] = value;
        }

        internal bool MatchesMixedOriginals() =>
            _routes[new(
                PerAppAudioDataFlow.Render,
                PerAppAudioRole.Console)].EndpointId
                == PhysicalRender
            && _routes[new(
                PerAppAudioDataFlow.Capture,
                PerAppAudioRole.Console)].EndpointId
                == PhysicalCapture
            && PerAppAudioRouteKey.All
                .Where(key =>
                    key.Role != PerAppAudioRole.Console)
                .All(key =>
                    _routes[key].Kind
                    == PersistedAudioEndpointKind.Unset);

        public void Dispose()
        {
        }
    }
}
