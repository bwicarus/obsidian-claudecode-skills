using System.Text;
using System.Text.Json;

namespace BwReader.ComputerVoiceAudio;

internal enum DirectProtocolPhase
{
    AwaitingAuthentication,
    AwaitingStart,
    ContextOnly,
    Starting,
    Active,
}

internal sealed record DirectCodexVoiceState(
    string Status,
    bool? Active,
    string? Source);

internal sealed record DirectCodexVoiceSetResult(
    DirectCodexVoiceState State,
    bool ShortcutSent);

internal interface IDirectCodexVoiceControl
{
    bool KeepActive { get; }

    DirectCodexVoiceState ReadState();

    Task<DirectCodexVoiceSetResult> SetActiveAsync(
        bool active,
        CancellationToken cancellationToken);

    Task<DirectCodexVoiceSetResult> SetKeepActiveAsync(
        bool enabled,
        CancellationToken cancellationToken);
}

internal sealed class DirectCodexVoiceControl :
    IDirectCodexVoiceControl,
    IAsyncDisposable
{
    internal const string StateSource =
        "windows-microphone-capability-ledger";
    internal static readonly TimeSpan RestartReadySettleDelay =
        TimeSpan.FromSeconds(5);

    internal static DirectCodexVoiceControl Shared { get; } =
        CreateProduction();

    private readonly Func<CodexVoiceActivitySnapshot> _readSnapshot;
    private readonly Func<
        bool,
        CodexVoiceActivitySnapshot,
        CancellationToken,
        Task<CodexVoiceActivitySnapshot>> _transitionAsync;
    private readonly SemaphoreSlim _transitionGate;
    private readonly string? _keepActivePath;
    private readonly TimeSpan _keepActivePollInterval;
    private readonly CancellationTokenSource? _keepActiveLifetime;
    private readonly Task? _keepActiveMonitor;
    private readonly Func<CancellationToken, Task>?
        _recoverStartFailureAsync;
    private int _keepActive;
    private int _automaticRecoveryBlocked;
    private int _disposeStarted;

    internal DirectCodexVoiceControl(
        Func<CodexVoiceActivitySnapshot> readSnapshot,
        Func<
            bool,
            CodexVoiceActivitySnapshot,
            CancellationToken,
            Task<CodexVoiceActivitySnapshot>> transitionAsync,
        SemaphoreSlim? transitionGate = null,
        string? keepActivePath = null,
        TimeSpan? keepActivePollInterval = null,
        Func<CancellationToken, Task>? recoverStartFailureAsync = null)
    {
        _readSnapshot = readSnapshot
            ?? throw new ArgumentNullException(nameof(readSnapshot));
        _transitionAsync = transitionAsync
            ?? throw new ArgumentNullException(nameof(transitionAsync));
        _transitionGate = transitionGate ?? new SemaphoreSlim(1, 1);
        _keepActivePath = string.IsNullOrWhiteSpace(keepActivePath)
            ? null
            : System.IO.Path.GetFullPath(keepActivePath);
        _keepActivePollInterval = keepActivePollInterval
            ?? TimeSpan.FromSeconds(5);
        _recoverStartFailureAsync = recoverStartFailureAsync;
        if (_keepActivePollInterval < TimeSpan.FromSeconds(1))
        {
            throw new ArgumentOutOfRangeException(
                nameof(keepActivePollInterval));
        }
        _keepActive = LoadKeepActive(_keepActivePath) ? 1 : 0;
        if (_keepActivePath is not null)
        {
            _keepActiveLifetime = new CancellationTokenSource();
            _keepActiveMonitor = MonitorKeepActiveAsync(
                _keepActiveLifetime.Token);
        }
    }

    public bool KeepActive => Volatile.Read(ref _keepActive) == 1;

    public DirectCodexVoiceState ReadState()
    {
        try
        {
            return ToState(_readSnapshot());
        }
        catch
        {
            // STATUS must remain a side-effect-free diagnostic even if the
            // Windows capability ledger is temporarily unreadable.
            return new DirectCodexVoiceState(
                "error",
                Active: null,
                StateSource);
        }
    }

    public async Task<DirectCodexVoiceSetResult> SetActiveAsync(
        bool active,
        CancellationToken cancellationToken)
    {
        return await SetActiveSerializedAsync(
            active,
            explicitRequest: true,
            cancellationToken).ConfigureAwait(false);
    }

    private async Task<DirectCodexVoiceSetResult> SetActiveSerializedAsync(
        bool active,
        bool explicitRequest,
        CancellationToken cancellationToken)
    {
        await _transitionGate.WaitAsync(cancellationToken)
            .ConfigureAwait(false);
        try
        {
            if (explicitRequest)
            {
                Volatile.Write(ref _automaticRecoveryBlocked, 0);
            }
            return await SetActiveWithinGateAsync(
                active,
                cancellationToken).ConfigureAwait(false);
        }
        finally
        {
            _transitionGate.Release();
        }
    }

    public async Task<DirectCodexVoiceSetResult> SetKeepActiveAsync(
        bool enabled,
        CancellationToken cancellationToken)
    {
        await _transitionGate.WaitAsync(cancellationToken)
            .ConfigureAwait(false);
        try
        {
            SaveKeepActive(_keepActivePath, enabled);
            Volatile.Write(ref _keepActive, enabled ? 1 : 0);
            Volatile.Write(ref _automaticRecoveryBlocked, 0);
            if (!enabled)
            {
                return new DirectCodexVoiceSetResult(
                    ReadState(),
                    ShortcutSent: false);
            }
            return await SetActiveWithinGateAsync(
                active: true,
                cancellationToken).ConfigureAwait(false);
        }
        finally
        {
            _transitionGate.Release();
        }
    }

    private async Task<DirectCodexVoiceSetResult> SetActiveWithinGateAsync(
        bool active,
        CancellationToken cancellationToken)
    {
        CodexVoiceActivitySnapshot before = ReadRequired();
        if (before.Active == active)
        {
            return new DirectCodexVoiceSetResult(
                ToState(before),
                ShortcutSent: false);
        }

        CodexVoiceActivitySnapshot confirmed;
        try
        {
            confirmed = await _transitionAsync(
                active,
                before,
                cancellationToken).ConfigureAwait(false);
        }
        catch (DirectProtocolException exception) when (
            active
            && exception.Code
                == CodexVoiceActivityController.StartNotConfirmedCode
            && _recoverStartFailureAsync is not null)
        {
            try
            {
                // Retrying F24 in the same failed Codex generation is known to
                // be ineffective. Restart exactly once, wait for the new app
                // generation, then make one fresh state-based start attempt.
                await _recoverStartFailureAsync(cancellationToken)
                    .ConfigureAwait(false);
                CodexVoiceActivitySnapshot afterRestart = ReadRequired();
                confirmed = afterRestart.Active
                    ? afterRestart
                    : await _transitionAsync(
                        true,
                        afterRestart,
                        cancellationToken).ConfigureAwait(false);
            }
            catch
            {
                // The keep-alive monitor must not turn a persistent failure
                // into an endless restart loop. A later explicit user action
                // clears this latch and may try one new bounded recovery.
                Volatile.Write(ref _automaticRecoveryBlocked, 1);
                throw;
            }
        }
        RequireAvailable(confirmed);
        if (confirmed.Active != active)
        {
            throw new DirectProtocolException(
                active
                    ? CodexVoiceActivityController.StartNotConfirmedCode
                    : CodexVoiceActivityController.StopNotConfirmedCode,
                active
                    ? "未确认 Codex 语音已开启"
                    : "未确认 Codex 语音已关闭",
                retryable: true);
        }
        Volatile.Write(ref _automaticRecoveryBlocked, 0);
        return new DirectCodexVoiceSetResult(
            ToState(confirmed),
            ShortcutSent: true);
    }

    private CodexVoiceActivitySnapshot ReadRequired()
    {
        try
        {
            CodexVoiceActivitySnapshot snapshot = _readSnapshot();
            RequireAvailable(snapshot);
            return snapshot;
        }
        catch (DirectProtocolException)
        {
            throw;
        }
        catch (Exception exception)
        {
            throw new DirectProtocolException(
                CodexVoiceActivityController.ActivityReadFailedCode,
                "读取 Codex 语音状态失败",
                retryable: true,
                innerException: exception);
        }
    }

    private static void RequireAvailable(
        CodexVoiceActivitySnapshot snapshot)
    {
        if (snapshot.Status == CodexVoiceActivityReadStatus.Unavailable)
        {
            throw new DirectProtocolException(
                CodexVoiceActivityController.ActivityUnavailableCode,
                "Codex 语音状态当前不可用",
                retryable: true);
        }
        if (snapshot.Status == CodexVoiceActivityReadStatus.Error)
        {
            throw new DirectProtocolException(
                CodexVoiceActivityController.ActivityReadFailedCode,
                "读取 Codex 语音状态失败",
                retryable: true);
        }
    }

    private static DirectCodexVoiceState ToState(
        CodexVoiceActivitySnapshot snapshot) =>
        snapshot.Status switch
        {
            CodexVoiceActivityReadStatus.Available => new(
                "available",
                snapshot.Active,
                StateSource),
            CodexVoiceActivityReadStatus.Unavailable => new(
                "unavailable",
                Active: null,
                StateSource),
            _ => new(
                "error",
                Active: null,
                StateSource),
        };

    internal static DirectCodexVoiceControl CreateProduction(
        string? keepActivePath = null,
        IDirectAppLauncher? appLauncher = null)
    {
        WindowsRegistryCodexVoiceActivitySource source = new(
            DirectAppTargets.CodexDesktop);
        CodexVoiceActivityController controller = new(
            source,
            new SystemCodexVoiceActivityClock());
        WindowsCodexVoiceShortcutSender shortcutSender = new();
        IDirectAppLauncher launcher = appLauncher
            ?? new WindowsDirectAppLauncher();
        return new DirectCodexVoiceControl(
            source.Read,
            async (active, before, cancellationToken) =>
            {
                if (active)
                {
                    DirectAppTargetProfile profile = DirectAppTargets.Require(
                        DirectAppTargets.CodexDesktop);
                    await launcher.EnsureRunningAsync(
                        profile.AppKind,
                        profile.AppUserModelId,
                        cancellationToken).ConfigureAwait(false);
                    _ = await launcher.WaitForUniqueReadyAsync(
                        profile.AppKind,
                        profile.AppUserModelId,
                        TimeSpan.FromSeconds(20),
                        cancellationToken).ConfigureAwait(false);
                }
                CodexAppTarget target = RequireCodexTarget();
                if (active)
                {
                    CodexVoiceStartBaseline baseline = new(before);
                    shortcutSender.Send(target, DirectVoiceCommand.Start);
                    CodexVoiceShortcutReceipt receipt =
                        controller.RecordShortcutSent(baseline, target);
                    CodexVoiceStartConfirmation confirmation =
                        await controller.ConfirmStartedAsync(
                            baseline,
                            receipt,
                            CodexVoiceActivityController.StartObservationTimeout,
                            CodexVoiceActivityController.MonitorInterval,
                            cancellationToken).ConfigureAwait(false);
                    confirmation = await controller.ConfirmUsableAsync(
                        confirmation,
                        CodexVoiceActivityController.StartUsableSettleDelay,
                        cancellationToken).ConfigureAwait(false);
                    return confirmation.Snapshot;
                }

                shortcutSender.Send(target, DirectVoiceCommand.Stop);
                return await controller.ConfirmStoppedAsync(
                    before,
                    CodexVoiceActivityController.StopTransitionTimeout,
                    CodexVoiceActivityController.MonitorInterval,
                    cancellationToken).ConfigureAwait(false);
            },
            keepActivePath: keepActivePath,
            recoverStartFailureAsync: async cancellationToken =>
            {
                DirectAppTargetProfile profile = DirectAppTargets.Require(
                    DirectAppTargets.CodexDesktop);
                CodexAppTarget current = RequireCodexTarget();
                _ = await launcher.RestartAsync(
                    profile.AppKind,
                    profile.AppUserModelId,
                    new DirectAppTarget(
                        current.RootProcessId,
                        current.RootProcessStartFileTimeUtc,
                        profile.AppKind,
                        profile.AppUserModelId),
                    TimeSpan.FromSeconds(30),
                    cancellationToken).ConfigureAwait(false);
                // A window can be discoverable before Codex finishes wiring
                // its voice UI. Let the fresh generation settle, then the
                // caller sends exactly one F24 for the retry.
                await Task.Delay(
                    RestartReadySettleDelay,
                    cancellationToken).ConfigureAwait(false);
            });
    }

    private async Task MonitorKeepActiveAsync(
        CancellationToken cancellationToken)
    {
        using PeriodicTimer timer = new(_keepActivePollInterval);
        try
        {
            while (await timer.WaitForNextTickAsync(cancellationToken)
                .ConfigureAwait(false))
            {
                if (!KeepActive)
                {
                    continue;
                }
                if (Volatile.Read(ref _automaticRecoveryBlocked) != 0)
                {
                    continue;
                }
                try
                {
                    DirectCodexVoiceState state = ReadState();
                    if (state.Status == "available" && state.Active == true)
                    {
                        continue;
                    }
                    _ = await SetActiveSerializedAsync(
                        active: true,
                        explicitRequest: false,
                        cancellationToken).ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                    when (cancellationToken.IsCancellationRequested)
                {
                    return;
                }
                catch
                {
                    // One failed start attempt already performs its single
                    // restart/retry and then latches automatic recovery. The
                    // monitor never turns the failure into repeated F24 input.
                }
            }
        }
        catch (OperationCanceledException)
            when (cancellationToken.IsCancellationRequested)
        {
        }
    }

    private static bool LoadKeepActive(string? path)
    {
        if (path is null || !File.Exists(path))
        {
            return false;
        }
        try
        {
            using JsonDocument document = JsonDocument.Parse(
                File.ReadAllText(path, Encoding.UTF8));
            JsonElement root = document.RootElement;
            if (
                root.ValueKind != JsonValueKind.Object
                || root.GetRawText().Length > 1024
                || root.EnumerateObject().Count() != 2
                || !root.TryGetProperty("contract", out JsonElement contract)
                || contract.GetString()
                    != "reader-codex-voice-keepalive/1"
                || !root.TryGetProperty("enabled", out JsonElement enabled)
                || enabled.ValueKind is not (
                    JsonValueKind.True or JsonValueKind.False)
            )
            {
                return false;
            }
            return enabled.GetBoolean();
        }
        catch
        {
            return false;
        }
    }

    private static void SaveKeepActive(string? path, bool enabled)
    {
        if (path is null)
        {
            return;
        }
        string directory = System.IO.Path.GetDirectoryName(path)
            ?? throw new InvalidOperationException(
                "Codex 语音持续运行配置目录无效");
        Directory.CreateDirectory(directory);
        string temporary = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            File.WriteAllText(
                temporary,
                JsonSerializer.Serialize(new
                {
                    contract = "reader-codex-voice-keepalive/1",
                    enabled,
                }),
                new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
            File.Move(temporary, path, overwrite: true);
        }
        finally
        {
            try { File.Delete(temporary); } catch { }
        }
    }

    public async ValueTask DisposeAsync()
    {
        if (Interlocked.Exchange(ref _disposeStarted, 1) != 0)
        {
            return;
        }
        if (_keepActiveLifetime is null || _keepActiveMonitor is null)
        {
            return;
        }
        _keepActiveLifetime.Cancel();
        try
        {
            await _keepActiveMonitor.ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
        }
        _keepActiveLifetime.Dispose();
    }

    private static CodexAppTarget RequireCodexTarget()
    {
        try
        {
            return WindowsCodexAppProbe.RequireReady(
                DirectAppTargets.CodexDesktop);
        }
        catch (DirectProtocolException)
        {
            throw;
        }
        catch (Exception exception)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_SHORTCUT_TARGET_UNAVAILABLE",
                "无法确认唯一的 Codex 快捷键目标",
                retryable: true,
                innerException: exception);
        }
    }
}

internal sealed class DirectBridgeProtocolSession
{
    private readonly string _connectionId;
    private readonly DirectBridgeConfigStore _configStore;
    private readonly DirectBridgeCoordinator _coordinator;
    private readonly IDirectCodexVoiceControl _codexVoiceControl;
    private readonly Func<string, ReaderContextSourceLease>
        _registerReaderSource;
    private readonly Func<
        ReaderVisualDeliveryChunk,
        ReaderVisualDeliveryAck> _acceptReaderVisual;
    private readonly Action<ReaderBrowserControlResponse>
        _acceptReaderBrowserControl;
    private readonly Func<DateTimeOffset> _utcNow;
    private bool _helloSeen;
    private bool _authenticated;
    private string? _contextDeliveryMode;
    private string? _contextOnlySessionId;
    private string? _activeVoiceSessionId;
    private string? _activeVoiceAppKind;
    private string? _registeredSourceInstanceId;
    private DirectProtocolPhase _phase =
        DirectProtocolPhase.AwaitingAuthentication;

    internal DirectBridgeProtocolSession(
        string connectionId,
        string origin,
        DirectBridgeConfigStore configStore,
        DirectBridgeCoordinator coordinator,
        Func<DateTimeOffset>? utcNow = null,
        IDirectCodexVoiceControl? codexVoiceControl = null,
        Func<string, ReaderContextSourceLease>?
            registerReaderSource = null,
        Func<
            ReaderVisualDeliveryChunk,
            ReaderVisualDeliveryAck>? acceptReaderVisual = null,
        Action<ReaderBrowserControlResponse>?
            acceptReaderBrowserControl = null)
    {
        if (!DirectBridgeContract.IsSafeId(connectionId))
        {
            throw new ArgumentException(
                "connectionId must be a safe identifier",
                nameof(connectionId));
        }
        _connectionId = connectionId;
        _configStore = configStore;
        _coordinator = coordinator;
        _codexVoiceControl = codexVoiceControl
            ?? DirectCodexVoiceControl.Shared;
        _registerReaderSource = registerReaderSource
            ?? (_ => throw new DirectProtocolException(
                "BW_READER_VISUAL_UNAVAILABLE",
                "Reader 视觉来源路由尚未接线",
                retryable: true));
        _acceptReaderVisual = acceptReaderVisual
            ?? (_ => throw new DirectProtocolException(
                "BW_READER_VISUAL_UNAVAILABLE",
                "Reader 视觉接收器尚未接线",
                retryable: true));
        _acceptReaderBrowserControl = acceptReaderBrowserControl
            ?? (_ => throw new DirectProtocolException(
                "BW_READER_BROWSER_CONTROL_UNAVAILABLE",
                "Reader 浏览控制接收器尚未接线",
                retryable: true));
        _utcNow = utcNow ?? (() => DateTimeOffset.UtcNow);
    }

    internal bool Authenticated => _authenticated;

    internal bool IsAuthenticated => _authenticated;

    internal DirectProtocolPhase Phase => _phase;

    internal async Task<DirectProtocolReply> HandleAsync(
        string json,
        Func<string, string, Task> reportStatusAsync,
        Func<string, DirectPcmFrame, CancellationToken, Task>
            sendPcmFrameAsync,
        CancellationToken cancellationToken)
    {
        string requestId = "invalid";
        string action = "unknown";
        try
        {
            if (
                Encoding.UTF8.GetByteCount(json)
                    > DirectBridgeContract.MaximumMessageBytes
            )
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_MESSAGE_TOO_LARGE",
                    "消息超过大小上限");
            }
            using JsonDocument document = JsonDocument.Parse(
                json,
                new JsonDocumentOptions
                {
                    AllowTrailingCommas = false,
                    CommentHandling = JsonCommentHandling.Disallow,
                    MaxDepth = 16,
                });
            JsonElement message = document.RootElement;
            RequireObject(message);
            DirectJsonValidation.RequireNoDuplicateKeys(message);
            requestId = RequireSafeId(message, "requestId");
            if (RequireString(message, "contract", 128)
                != DirectBridgeContract.Contract)
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_CONTRACT_INVALID",
                    "直连消息合同不匹配");
            }
            action = RequireString(message, "type", 32);
            object payload;
            Func<CancellationToken, Task>? afterSend = null;
            switch (action)
            {
                case "hello":
                    payload = HandleHello(message);
                    break;
                case "status":
                    payload = HandleStatus(message);
                    break;
                case "codex-voice-set":
                    payload = await HandleCodexVoiceSetAsync(
                        message,
                        cancellationToken).ConfigureAwait(false);
                    break;
                case "codex-voice-keepalive-set":
                    payload = await HandleCodexVoiceKeepAliveSetAsync(
                        message,
                        cancellationToken).ConfigureAwait(false);
                    break;
                case "context-mode":
                    payload = HandleContextMode(message);
                    break;
                case "context-mode-set":
                    payload = HandleContextModeSet(message);
                    break;
                case "context-open":
                    payload = HandleContextOpen(message);
                    break;
                case ReaderVisualDeliveryProtocol.RegisterType:
                    payload = HandleVisualRegister(message);
                    break;
                case ReaderVisualDeliveryProtocol.ChunkType:
                    payload = HandleReaderVisual(message);
                    break;
                case ReaderBrowserControlProtocol.ResponseType:
                    payload = HandleReaderBrowserControl(message);
                    break;
                case "start":
                    DirectStartActionResult start =
                        await HandleStartAsync(
                            message,
                            reportStatusAsync,
                            sendPcmFrameAsync,
                            cancellationToken).ConfigureAwait(false);
                    payload = start.Payload;
                    afterSend = start.AfterSendAsync;
                    break;
                case "heartbeat":
                    payload = await HandleHeartbeatAsync(
                        message,
                        cancellationToken).ConfigureAwait(false);
                    break;
                case "context":
                    payload = await HandleContextAsync(
                        message,
                        cancellationToken).ConfigureAwait(false);
                    break;
                case "active-reading":
                    payload = await HandleActiveReadingAsync(
                        message,
                        cancellationToken).ConfigureAwait(false);
                    break;
                case "context-clear":
                    payload = await HandleContextClearAsync(
                        message,
                        cancellationToken).ConfigureAwait(false);
                    break;
                case "log":
                    payload = await HandleExtensionLogAsync(
                        message,
                        cancellationToken).ConfigureAwait(false);
                    break;
                case "stop":
                    payload = await HandleStopAsync(
                        message,
                        cancellationToken).ConfigureAwait(false);
                    break;
                default:
                    throw new DirectProtocolException(
                        "BW_COMPUTER_VOICE_DIRECT_ACTION_INVALID",
                        "不支持的直连操作");
            }
            return new DirectProtocolReply(
                Success(requestId, action, payload),
                afterSend);
        }
        catch (DirectProtocolException exception)
        {
            return new DirectProtocolReply(
                Failure(
                    requestId,
                    action,
                    exception.Code,
                    exception.Message,
                    exception.Retryable),
                AfterSendAsync: null);
        }
        catch (
            Exception exception
        ) when (
            exception is JsonException
            or FormatException
            or InvalidOperationException
            or ArgumentException
        )
        {
            return new DirectProtocolReply(
                Failure(
                    requestId,
                    action,
                    "BW_COMPUTER_VOICE_DIRECT_MESSAGE_INVALID",
                    "直连消息无效",
                    retryable: false),
                AfterSendAsync: null);
        }
    }

    private object HandleHello(JsonElement message)
    {
        if (_helloSeen || _authenticated)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_HELLO_REPEATED",
                "每条连接只能发送一次 hello");
        }
        _helloSeen = true;
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId",
            "protocolVersion");
        JsonElement protocolVersion = message.GetProperty(
            "protocolVersion");
        if (
            protocolVersion.ValueKind != JsonValueKind.Number
            || !protocolVersion.TryGetInt32(out int version)
            || version != 3
        )
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_PROTOCOL_VERSION_INVALID",
                "直连协议版本不受支持");
        }
        DirectBridgeConfig config = _configStore.Load();
        if (!config.ExperimentalSingleUserMode)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_CONFIG_INVALID",
                "v3 仅支持固定单用户实验模式");
        }

        _authenticated = true;
        _contextDeliveryMode = config.ContextDeliveryMode;
        _phase = DirectProtocolPhase.AwaitingStart;
        return new
        {
            protocolVersion = 3,
            limits = new
            {
                maxMessageBytes =
                    DirectBridgeContract.MaximumMessageBytes,
                pcmFrameBytes = DirectBridgeContract.PcmFrameBytes,
                pcmQueueLimitMs =
                    DirectBridgeContract.PcmQueueLimitMilliseconds,
                uplinkTrack =
                    (byte)DirectPcmTrack.BrowserMicrophone,
                uplinkQueueLimitMs =
                    DirectBridgeContract
                        .UplinkPcmQueueLimitMilliseconds,
                heartbeatIntervalMs =
                    DirectBridgeContract
                        .ClientHeartbeatIntervalMilliseconds,
                heartbeatTimeoutMs =
                    DirectBridgeContract
                        .ClientHeartbeatTimeoutMilliseconds,
            },
        };
    }

    private object HandleContextMode(JsonElement message)
    {
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId");
        RequireAuthenticated();
        return new
        {
            mode = RequireContextDeliveryMode(),
        };
    }

    private object HandleContextModeSet(JsonElement message)
    {
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId",
            "mode",
            "sessionId");
        RequireAuthenticated();
        string sessionId = RequireSafeId(message, "sessionId");
        _ = DirectPcmFrameCodec.ParseSessionId(sessionId);
        string mode = RequireString(message, "mode", 32);
        if (!DirectContextDeliveryMode.IsSupported(mode))
        {
            throw new DirectProtocolException(
                "BW_READER_CONTEXT_DELIVERY_MODE_INVALID",
                "Reader 上下文交付模式无效");
        }
        if (
            _phase != DirectProtocolPhase.AwaitingStart
            || _contextOnlySessionId is not null
            || _coordinator.ActiveSessionId is not null
            || _coordinator.CaptureActive
            || _coordinator.CleanupPending
        )
        {
            throw new DirectProtocolException(
                "BW_READER_CONTEXT_DELIVERY_MODE_BUSY",
                "请先结束电脑语音并清理旧上下文链路",
                retryable: true);
        }

        string previousMode =
            _configStore.SetContextDeliveryMode(mode);
        _contextDeliveryMode = mode;
        return new
        {
            mode,
            previousMode,
        };
    }

    private object HandleContextOpen(JsonElement message)
    {
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId",
            "sessionId");
        RequireAuthenticated();
        if (
            RequireContextDeliveryMode()
                != DirectContextDeliveryMode.SnapshotMcp
        )
        {
            throw new DirectProtocolException(
                "BW_READER_CONTEXT_SNAPSHOT_MODE_REQUIRED",
                "Windows 未启用 Reader 快照 MCP 实验模式");
        }
        if (_phase != DirectProtocolPhase.AwaitingStart)
        {
            throw new DirectProtocolException(
                "BW_READER_CONTEXT_SNAPSHOT_PHASE_INVALID",
                "当前连接不能切换为纯上下文连接");
        }
        string sessionId = RequireSafeId(message, "sessionId");
        _ = DirectPcmFrameCodec.ParseSessionId(sessionId);
        _contextOnlySessionId = sessionId;
        _phase = DirectProtocolPhase.ContextOnly;
        return new
        {
            sessionId,
            state = "context-only",
            mode = DirectContextDeliveryMode.SnapshotMcp,
        };
    }

    private object HandleVisualRegister(JsonElement message)
    {
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId",
            "sessionId",
            "sourceInstanceId");
        RequireAuthenticated();
        if (
            RequireContextDeliveryMode()
                != DirectContextDeliveryMode.SnapshotMcp
            || _phase != DirectProtocolPhase.ContextOnly
        )
        {
            throw new DirectProtocolException(
                "BW_READER_VISUAL_CONTEXT_ONLY_REQUIRED",
                "Reader 视觉来源只允许在纯上下文连接中注册");
        }
        string sessionId = RequireSafeId(message, "sessionId");
        _ = DirectPcmFrameCodec.ParseSessionId(sessionId);
        RequireContextOnlySession(sessionId);
        if (_registeredSourceInstanceId is not null)
        {
            throw new DirectProtocolException(
                "BW_READER_VISUAL_SOURCE_REPEATED",
                "每条 Reader 上下文连接只能注册一次视觉来源");
        }
        string sourceInstanceId = RequireSafeId(
            message,
            "sourceInstanceId");
        _ = _registerReaderSource(sourceInstanceId);
        _registeredSourceInstanceId = sourceInstanceId;
        return new
        {
            sessionId,
            sourceInstanceId,
            state = "registered",
        };
    }

    private object HandleReaderVisual(JsonElement message)
    {
        RequireAuthenticated();
        if (
            RequireContextDeliveryMode()
                != DirectContextDeliveryMode.SnapshotMcp
            || _phase != DirectProtocolPhase.ContextOnly
        )
        {
            throw new DirectProtocolException(
                "BW_READER_VISUAL_CONTEXT_ONLY_REQUIRED",
                "Reader 视觉只允许在纯上下文连接中回传");
        }
        ReaderVisualDeliveryChunk chunk =
            ReaderVisualDeliveryProtocol.ValidateChunk(message);
        RequireContextOnlySession(chunk.SessionId);
        if (
            _registeredSourceInstanceId is null
            || !string.Equals(
                _registeredSourceInstanceId,
                chunk.SourceInstanceId,
                StringComparison.Ordinal)
        )
        {
            throw new DirectProtocolException(
                "BW_READER_VISUAL_SOURCE_MISMATCH",
                "Reader 视觉回传来源与当前连接不匹配");
        }
        ReaderVisualDeliveryAck ack = _acceptReaderVisual(chunk);
        return new
        {
            correlation = ack.Correlation,
            chunkIndex = ack.ChunkIndex,
            accepted = ack.Accepted,
            complete = ack.Complete,
        };
    }

    private object HandleReaderBrowserControl(JsonElement message)
    {
        RequireAuthenticated();
        if (
            RequireContextDeliveryMode()
                != DirectContextDeliveryMode.SnapshotMcp
            || _phase != DirectProtocolPhase.ContextOnly
        )
        {
            throw new DirectProtocolException(
                "BW_READER_BROWSER_CONTROL_CONTEXT_ONLY_REQUIRED",
                "Reader 浏览控制只允许在纯上下文连接中回传");
        }
        ReaderBrowserControlResponse response =
            ReaderBrowserControlProtocol.ValidateResponse(message);
        RequireContextOnlySession(response.SessionId);
        if (
            _registeredSourceInstanceId is null
            || !string.Equals(
                _registeredSourceInstanceId,
                response.SourceInstanceId,
                StringComparison.Ordinal)
        )
        {
            throw new DirectProtocolException(
                "BW_READER_BROWSER_CONTROL_SOURCE_MISMATCH",
                "Reader 浏览控制回传来源与当前连接不匹配");
        }
        _acceptReaderBrowserControl(response);
        return new
        {
            correlation = response.Correlation,
            accepted = true,
        };
    }

    private object HandleStatus(JsonElement message)
    {
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId");
        RequireAuthenticated();
        DirectBridgeConfig config = _configStore.Load();
        bool captureActive = _coordinator.CaptureActive;
        bool outputRouteVerified =
            _coordinator.OutputRouteVerified(config);
        string state;
        string? reason;
        bool ready;
        if (captureActive)
        {
            state = "active";
            reason = outputRouteVerified
                ? null
                : DirectOutputRouteProbe.UnverifiedReason;
            ready = outputRouteVerified;
        }
        else if (_coordinator.CleanupPending)
        {
            state = "faulted";
            reason = _coordinator.LastError?.Code
                ?? "BW_COMPUTER_VOICE_DIRECT_MEDIA_CLEANUP_PENDING";
            ready = false;
        }
        else if (!config.LocalOptIn)
        {
            state = "unavailable";
            reason =
                "BW_COMPUTER_VOICE_DIRECT_LOCAL_OPT_IN_REQUIRED";
            ready = false;
        }
        else if (!_coordinator.AppLauncherReady)
        {
            state = "unavailable";
            reason =
                "BW_COMPUTER_VOICE_DIRECT_APP_LAUNCHER_NOT_WIRED";
            ready = false;
        }
        else if (!_coordinator.MediaHostReady)
        {
            state = "unavailable";
            reason = "BW_COMPUTER_VOICE_DIRECT_MEDIA_NOT_WIRED";
            ready = false;
        }
        else if (
            !_coordinator.ConfiguredRenderEndpointsReady(
                config,
                out reason)
        )
        {
            state = "unavailable";
            ready = false;
        }
        else
        {
            state = "idle";
            reason = outputRouteVerified
                ? null
                : DirectOutputRouteProbe.UnverifiedReason;
            ready = outputRouteVerified;
        }
        return new
        {
            ready,
            state,
            reason,
            localOptIn = config.LocalOptIn,
            lastError = _coordinator.LastError,
            media = new
            {
                hostReady = _coordinator.MediaHostReady,
                captureActive,
            },
            codexVoice = CodexVoicePayload(
                _codexVoiceControl.ReadState(),
                shortcutSent: false),
        };
    }

    private async Task<object> HandleCodexVoiceSetAsync(
        JsonElement message,
        CancellationToken cancellationToken)
    {
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId",
            "active");
        RequireAuthenticated();
        if (_phase is not (
            DirectProtocolPhase.AwaitingStart
            or DirectProtocolPhase.ContextOnly
            or DirectProtocolPhase.Active))
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_PHASE_INVALID",
                "当前连接阶段不能远程控制 Codex 语音");
        }
        DirectCodexVoiceSetResult result =
            await _codexVoiceControl.SetActiveAsync(
                RequireBoolean(message, "active"),
                cancellationToken).ConfigureAwait(false);
        return CodexVoicePayload(
            result.State,
            result.ShortcutSent);
    }

    private async Task<object> HandleCodexVoiceKeepAliveSetAsync(
        JsonElement message,
        CancellationToken cancellationToken)
    {
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId",
            "enabled");
        RequireAuthenticated();
        if (_phase is not (
            DirectProtocolPhase.AwaitingStart
            or DirectProtocolPhase.ContextOnly
            or DirectProtocolPhase.Active))
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_PHASE_INVALID",
                "当前连接阶段不能设置 Codex 语音持续运行");
        }
        DirectCodexVoiceSetResult result =
            await _codexVoiceControl.SetKeepActiveAsync(
                RequireBoolean(message, "enabled"),
                cancellationToken).ConfigureAwait(false);
        return CodexVoicePayload(
            result.State,
            result.ShortcutSent);
    }

    private object CodexVoicePayload(
        DirectCodexVoiceState state,
        bool shortcutSent) =>
        new
        {
            status = state.Status,
            active = state.Active,
            source = state.Source,
            shortcutSent,
            keepActive = _codexVoiceControl.KeepActive,
        };

    private async Task<DirectStartActionResult> HandleStartAsync(
        JsonElement message,
        Func<string, string, Task> reportStatusAsync,
        Func<string, DirectPcmFrame, CancellationToken, Task>
            sendPcmFrameAsync,
        CancellationToken cancellationToken)
    {
        bool hasAppKind = message.TryGetProperty(
            "appKind",
            out _);
        bool hasTakeover = message.TryGetProperty(
            "takeover",
            out _);
        List<string> expectedKeys =
        [
            "contract",
            "type",
            "requestId",
            "sessionId",
        ];
        if (hasAppKind)
        {
            expectedKeys.Add("appKind");
        }
        if (hasTakeover)
        {
            expectedKeys.Add("takeover");
        }
        RequireExactKeys(message, [.. expectedKeys]);
        RequireAuthenticated();
        string sessionId = RequireSafeId(message, "sessionId");
        _ = DirectPcmFrameCodec.ParseSessionId(sessionId);
        string appKind = hasAppKind
            ? RequireString(message, "appKind", 32)
            : DirectAppTargets.CodexDesktop;
        bool takeover = hasTakeover
            && RequireBoolean(message, "takeover");
        _ = DirectAppTargets.Require(appKind);
        if (_phase is not (
            DirectProtocolPhase.AwaitingStart
            or DirectProtocolPhase.Active))
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_PHASE_INVALID",
                "当前连接阶段不接受 START");
        }
        if (
            _phase == DirectProtocolPhase.Active
            && (
                !string.Equals(
                    _activeVoiceSessionId,
                    sessionId,
                    StringComparison.Ordinal)
                || !string.Equals(
                    _activeVoiceAppKind,
                    appKind,
                    StringComparison.Ordinal)
            )
        )
        {
            // Replacing a session on the same transport would also require
            // resetting both PCM sequence guards.  Keep takeover scoped to a
            // second AwaitingStart connection; an active transport may only
            // repeat its exact START idempotently.
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_SESSION_MISMATCH",
                "活动连接上的 START 与当前会话不匹配");
        }
        DirectPcmStartGate pcmGate = new(
            (frame, token) => sendPcmFrameAsync(
                sessionId,
                frame,
                token));
        DirectProtocolPhase previousPhase = _phase;
        _phase = DirectProtocolPhase.Starting;
        try
        {
            DirectMediaStartResult started =
                await _coordinator.StartAsync(
                    _connectionId,
                    sessionId,
                    appKind,
                    RequireContextDeliveryMode(),
                    takeover,
                    reportStatusAsync,
                    pcmGate.SendAsync,
                    cancellationToken).ConfigureAwait(false);
            object payload = new
            {
                sessionId,
                state = "active",
                media = new
                {
                    hostReady = started.HostReady,
                    captureActive = started.CaptureActive,
                },
            };
            _phase = DirectProtocolPhase.Active;
            _activeVoiceSessionId = sessionId;
            _activeVoiceAppKind = appKind;
            return new DirectStartActionResult(
                payload,
                pcmGate.ReleaseAsync);
        }
        catch
        {
            _phase = previousPhase;
            pcmGate.Abort();
            throw;
        }
    }

    private async Task<object> HandleStopAsync(
        JsonElement message,
        CancellationToken cancellationToken)
    {
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId",
            "sessionId");
        RequireAuthenticated();
        string sessionId = RequireSafeId(message, "sessionId");
        await _coordinator.StopAsync(
            _connectionId,
            sessionId,
            cancellationToken).ConfigureAwait(false);
        _phase = DirectProtocolPhase.AwaitingStart;
        _activeVoiceSessionId = null;
        _activeVoiceAppKind = null;
        return new
        {
            sessionId,
            state = "idle",
        };
    }

    private async Task<object> HandleHeartbeatAsync(
        JsonElement message,
        CancellationToken cancellationToken)
    {
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId",
            "sessionId",
            "sequence");
        RequireAuthenticated();
        string sessionId = RequireSafeId(message, "sessionId");
        _ = DirectPcmFrameCodec.ParseSessionId(sessionId);
        uint sequence = RequireUInt32(message, "sequence");
        if (sequence == 0)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_HEARTBEAT_SEQUENCE_INVALID",
                "电脑语音心跳序号必须从 1 开始");
        }
        await _coordinator.RenewHeartbeatAsync(
            _connectionId,
            sessionId,
            sequence,
            cancellationToken).ConfigureAwait(false);
        return new
        {
            sessionId,
            sequence,
            state = "active",
        };
    }

    private async Task<object> HandleContextAsync(
        JsonElement message,
        CancellationToken cancellationToken)
    {
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId",
            "sessionId",
            "contextContract",
            "event");
        RequireAuthenticated();
        string mode = RequireContextDeliveryMode();
        bool activeSession = _phase == DirectProtocolPhase.Active;
        bool contextOnly =
            _phase == DirectProtocolPhase.ContextOnly;
        if (
            mode == DirectContextDeliveryMode.LegacyInject
            && !activeSession
        )
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_CONTEXT_NOT_ACTIVE",
                "Reader context 只允许发送到当前活动通话");
        }
        if (
            mode == DirectContextDeliveryMode.SnapshotMcp
            && !activeSession
            && !contextOnly
        )
        {
            throw new DirectProtocolException(
                "BW_READER_CONTEXT_SNAPSHOT_NOT_OPEN",
                "Reader 本地快照连接尚未打开");
        }
        string sessionId = RequireSafeId(message, "sessionId");
        _ = DirectPcmFrameCodec.ParseSessionId(sessionId);
        if (contextOnly)
        {
            RequireContextOnlySession(sessionId);
        }
        string contextContract = RequireString(
            message,
            "contextContract",
            128);
        if (
            contextContract
                != NamedPipeDirectContextAdapter.ContextContract
        )
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_CONTEXT_SCHEMA_INVALID",
                "Reader outgoing context 合同无效");
        }
        DirectContextEvent contextEvent =
            NamedPipeDirectContextAdapter.ValidateEvent(
                message.GetProperty("event"));
        string requestId = RequireSafeId(message, "requestId");
        string outcome;
        if (mode == DirectContextDeliveryMode.LegacyInject)
        {
            DirectContextForwardResult forwarded =
                await _coordinator.ForwardLegacyContextAsync(
                    _connectionId,
                    requestId,
                    sessionId,
                    contextContract,
                    contextEvent,
                    cancellationToken).ConfigureAwait(false);
            outcome = forwarded.Outcome;
        }
        else
        {
            DirectSnapshotForwardResult forwarded =
                await _coordinator.ForwardSnapshotContextAsync(
                    _connectionId,
                    requestId,
                    sessionId,
                    contextEvent,
                    requireActiveOwner: activeSession,
                    cancellationToken).ConfigureAwait(false);
            outcome = forwarded.Outcome;
        }
        return new
        {
            sessionId,
            eventId = contextEvent.EventId,
            seq = contextEvent.Sequence,
            outcome,
        };
    }

    private async Task<object> HandleActiveReadingAsync(
        JsonElement message,
        CancellationToken cancellationToken)
    {
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId",
            "sessionId",
            "activeContract",
            "active");
        RequireAuthenticated();
        if (
            RequireContextDeliveryMode()
                != DirectContextDeliveryMode.SnapshotMcp
        )
        {
            throw new DirectProtocolException(
                "BW_READER_CONTEXT_SNAPSHOT_MODE_REQUIRED",
                "Windows 未启用 Reader 快照 MCP 实验模式");
        }
        bool activeSession = _phase == DirectProtocolPhase.Active;
        if (
            !activeSession
            && _phase != DirectProtocolPhase.ContextOnly
        )
        {
            throw new DirectProtocolException(
                "BW_READER_CONTEXT_SNAPSHOT_NOT_OPEN",
                "Reader 本地快照连接尚未打开");
        }
        if (
            RequireString(message, "activeContract", 128)
                != FileDirectSnapshotContextAdapter
                    .ActiveReadingContract
        )
        {
            throw new DirectProtocolException(
                "BW_READER_ACTIVE_READING_SCHEMA_INVALID",
                "Reader active-reading 合同无效");
        }
        string requestId = RequireSafeId(message, "requestId");
        string sessionId = RequireSafeId(message, "sessionId");
        _ = DirectPcmFrameCodec.ParseSessionId(sessionId);
        if (!activeSession)
        {
            RequireContextOnlySession(sessionId);
        }
        DirectActiveReading activeReading =
            FileDirectSnapshotContextAdapter.ValidateActiveReading(
                message.GetProperty("active"));
        DirectSnapshotForwardResult forwarded =
            await _coordinator.ForwardActiveReadingAsync(
                _connectionId,
                requestId,
                sessionId,
                activeReading,
                requireActiveOwner: activeSession,
                cancellationToken).ConfigureAwait(false);
        return new
        {
            sessionId,
            revision = forwarded.Revision,
            outcome = forwarded.Outcome,
        };
    }

    private async Task<object> HandleContextClearAsync(
        JsonElement message,
        CancellationToken cancellationToken)
    {
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId",
            "sessionId");
        RequireAuthenticated();
        string mode = RequireContextDeliveryMode();
        bool activeSession = _phase == DirectProtocolPhase.Active;
        bool contextOnly =
            _phase == DirectProtocolPhase.ContextOnly;
        bool legacyTransition =
            mode == DirectContextDeliveryMode.LegacyInject
            && _phase == DirectProtocolPhase.AwaitingStart;
        if (!activeSession && !contextOnly && !legacyTransition)
        {
            throw new DirectProtocolException(
                "BW_READER_CONTEXT_SNAPSHOT_CLEAR_PHASE_INVALID",
                "当前连接不能清空 Reader 本地快照");
        }
        string requestId = RequireSafeId(message, "requestId");
        string sessionId = RequireSafeId(message, "sessionId");
        _ = DirectPcmFrameCodec.ParseSessionId(sessionId);
        if (contextOnly)
        {
            RequireContextOnlySession(sessionId);
        }
        DirectSnapshotForwardResult forwarded =
            await _coordinator.ClearSnapshotContextAsync(
                _connectionId,
                requestId,
                sessionId,
                requireActiveOwner: activeSession,
                cancellationToken).ConfigureAwait(false);
        return new
        {
            sessionId,
            revision = forwarded.Revision,
            outcome = forwarded.Outcome,
        };
    }

    private async Task<object> HandleExtensionLogAsync(
        JsonElement message,
        CancellationToken cancellationToken)
    {
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId",
            "sessionId",
            "entries");
        RequireAuthenticated();
        if (_phase is not (
            DirectProtocolPhase.AwaitingStart
            or DirectProtocolPhase.ContextOnly
            or DirectProtocolPhase.Active))
        {
            throw new DirectProtocolException(
                "BW_READER_EXTENSION_LOG_PHASE_INVALID",
                "当前连接阶段不接受扩展日志");
        }
        string sessionId = RequireSafeId(message, "sessionId");
        _ = DirectPcmFrameCodec.ParseSessionId(sessionId);
        if (_phase == DirectProtocolPhase.ContextOnly)
        {
            RequireContextOnlySession(sessionId);
        }
        else if (
            _phase == DirectProtocolPhase.Active
            && !string.Equals(
                _activeVoiceSessionId,
                sessionId,
                StringComparison.Ordinal)
        )
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_SESSION_MISMATCH",
                "扩展日志 sessionId 与当前语音会话不匹配");
        }
        if (
            !message.TryGetProperty("entries", out JsonElement entriesValue)
            || entriesValue.ValueKind != JsonValueKind.Array
            || entriesValue.GetArrayLength() is < 1 or > 50
        )
        {
            throw new DirectProtocolException(
                "BW_READER_EXTENSION_LOG_ENTRIES_INVALID",
                "扩展日志每批必须包含 1 至 50 条记录");
        }

        List<DirectExtensionLogEntry> entries = [];
        foreach (JsonElement entryValue in entriesValue.EnumerateArray())
        {
            RequireExactKeys(
                entryValue,
                "at",
                "source",
                "stage",
                "detail");
            string at = RequireString(entryValue, "at", 64);
            string source = RequireString(entryValue, "source", 32);
            if (source is not (
                "extension-page"
                or "content-script"
                or "call-page"))
            {
                throw new DirectProtocolException(
                    "BW_READER_EXTENSION_LOG_SOURCE_INVALID",
                    "扩展日志 source 无效");
            }
            string stage = RequireString(entryValue, "stage", 64);
            if (!DirectBridgeContract.IsSafeId(stage))
            {
                throw new DirectProtocolException(
                    "BW_READER_EXTENSION_LOG_STAGE_INVALID",
                    "扩展日志 stage 无效");
            }
            string detail = RequireString(entryValue, "detail", 500);
            entries.Add(new DirectExtensionLogEntry(
                at,
                source,
                stage,
                detail));
        }

        try
        {
            int accepted = await DirectExtensionLogStore.AppendAsync(
                _configStore.InstallationRoot,
                _connectionId,
                sessionId,
                entries,
                _utcNow(),
                cancellationToken).ConfigureAwait(false);
            return new
            {
                ok = true,
                accepted,
            };
        }
        catch (
            Exception exception
        ) when (
            exception is IOException
            or UnauthorizedAccessException
            or NotSupportedException
        )
        {
            throw new DirectProtocolException(
                "BW_READER_EXTENSION_LOG_WRITE_FAILED",
                "Windows 无法写入扩展诊断日志",
                retryable: true,
                innerException: exception);
        }
    }

    internal static object StatusEvent(string state, string reason) =>
        new
        {
            contract = DirectBridgeContract.Contract,
            type = "event",
            @event = "status",
            payload = new
            {
                state,
                reason,
            },
        };

    private void RequireAuthenticated()
    {
        if (!_authenticated)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_AUTH_REQUIRED",
                "当前连接尚未认证");
        }
    }

    private string RequireContextDeliveryMode() =>
        _contextDeliveryMode
        ?? throw new DirectProtocolException(
            "BW_COMPUTER_VOICE_DIRECT_AUTH_REQUIRED",
            "当前连接尚未认证");

    private void RequireContextOnlySession(string sessionId)
    {
        if (
            _contextOnlySessionId is null
            || !string.Equals(
                _contextOnlySessionId,
                sessionId,
                StringComparison.Ordinal)
        )
        {
            throw new DirectProtocolException(
                "BW_READER_CONTEXT_SNAPSHOT_SESSION_MISMATCH",
                "Reader 本地快照 sessionId 与当前连接不匹配");
        }
    }

    private static object Success(
        string requestId,
        string action,
        object payload) =>
        new
        {
            contract = DirectBridgeContract.Contract,
            type = "result",
            requestId,
            ok = true,
            action,
            payload,
        };

    private static object Failure(
        string requestId,
        string action,
        string code,
        string message,
        bool retryable) =>
        new
        {
            contract = DirectBridgeContract.Contract,
            type = "result",
            requestId,
            ok = false,
            action,
            error = new
            {
                code,
                message,
                retryable,
            },
        };

    private static string RequireSafeId(
        JsonElement message,
        string name)
    {
        string result = RequireString(message, name, 160);
        if (!DirectBridgeContract.IsSafeId(result))
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_ID_INVALID",
                $"{name} 无效");
        }
        return result;
    }

    private static string RequireString(
        JsonElement message,
        string name,
        int maximumLength)
    {
        if (
            !message.TryGetProperty(name, out JsonElement value)
            || value.ValueKind != JsonValueKind.String
            || value.GetString() is not string result
            || result.Length is < 1
            || result.Length > maximumLength
        )
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_MESSAGE_INVALID",
                $"{name} 字段无效");
        }
        return result;
    }

    private static uint RequireUInt32(
        JsonElement message,
        string name)
    {
        if (
            !message.TryGetProperty(name, out JsonElement value)
            || value.ValueKind != JsonValueKind.Number
            || !value.TryGetUInt32(out uint result)
        )
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_MESSAGE_INVALID",
                $"{name} 字段无效");
        }
        return result;
    }

    private static bool RequireBoolean(
        JsonElement message,
        string name)
    {
        if (
            !message.TryGetProperty(name, out JsonElement value)
            || value.ValueKind is not (
                JsonValueKind.True or JsonValueKind.False)
        )
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_MESSAGE_INVALID",
                $"{name} 字段无效");
        }
        return value.GetBoolean();
    }

    private static void RequireObject(JsonElement value)
    {
        if (value.ValueKind != JsonValueKind.Object)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_MESSAGE_INVALID",
                "直连消息必须是对象");
        }
    }

    private static void RequireExactKeys(
        JsonElement value,
        params string[] expected)
    {
        RequireObject(value);
        HashSet<string> actual = value.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        if (!actual.SetEquals(expected))
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_MESSAGE_INVALID",
                "直连消息字段不匹配");
        }
    }
}

internal sealed record DirectProtocolReply(
    object Envelope,
    Func<CancellationToken, Task>? AfterSendAsync);

internal sealed record DirectStartActionResult(
    object Payload,
    Func<CancellationToken, Task> AfterSendAsync);

internal sealed record DirectExtensionLogEntry(
    string At,
    string Source,
    string Stage,
    string Detail);

internal static class DirectExtensionLogStore
{
    private const long MaximumLogBytes = 5L * 1024 * 1024;
    private const string LogContract = "reader-extension-runtime-log/1";
    private static readonly UTF8Encoding Utf8 = new(
        encoderShouldEmitUTF8Identifier: false,
        throwOnInvalidBytes: true);
    private static readonly SemaphoreSlim WriteGate = new(1, 1);

    internal static string GetLogPath(string installationRoot) =>
        Path.Combine(
            Path.GetFullPath(installationRoot),
            "runtime",
            "extension-log.jsonl");

    internal static async Task<int> AppendAsync(
        string installationRoot,
        string connectionId,
        string sessionId,
        IReadOnlyList<DirectExtensionLogEntry> entries,
        DateTimeOffset receivedAtUtc,
        CancellationToken cancellationToken)
    {
        if (entries.Count is < 1 or > 50)
        {
            throw new ArgumentOutOfRangeException(nameof(entries));
        }
        string path = GetLogPath(installationRoot);
        StringBuilder payload = new();
        foreach (DirectExtensionLogEntry entry in entries)
        {
            payload.Append(JsonSerializer.Serialize(new
            {
                contract = LogContract,
                receivedAtUtc,
                at = entry.At,
                source = entry.Source,
                stage = entry.Stage,
                detail = entry.Detail,
                connectionId,
                sessionId,
            }, DirectBridgeContract.JsonOptions));
            payload.Append('\n');
        }
        byte[] bytes = Utf8.GetBytes(payload.ToString());

        await WriteGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            string? directory = Path.GetDirectoryName(path);
            if (string.IsNullOrWhiteSpace(directory))
            {
                throw new InvalidOperationException(
                    "Extension log directory is unavailable");
            }
            Directory.CreateDirectory(directory);
            FileInfo current = new(path);
            if (
                current.Exists
                && current.Length + bytes.Length > MaximumLogBytes
            )
            {
                string previousPath = path + ".1";
                File.Move(path, previousPath, overwrite: true);
            }
            await using FileStream stream = new(
                path,
                FileMode.Append,
                FileAccess.Write,
                FileShare.Read,
                bufferSize: 4096,
                FileOptions.Asynchronous | FileOptions.WriteThrough);
            await stream.WriteAsync(bytes, cancellationToken)
                .ConfigureAwait(false);
            await stream.FlushAsync(cancellationToken).ConfigureAwait(false);
            return entries.Count;
        }
        finally
        {
            WriteGate.Release();
        }
    }
}
