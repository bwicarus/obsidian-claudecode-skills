using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace BwReader.ComputerVoiceAudio;

internal enum DirectProtocolPhase
{
    AwaitingAuthentication,
    AwaitingStart,
    ContextOnly,
    Starting,
    Active,
}

internal sealed class DirectBridgeProtocolSession
{
    private readonly string _connectionId;
    private readonly DirectBridgeConfigStore _configStore;
    private readonly DirectBridgeCoordinator _coordinator;
    private readonly Func<ReaderResultDeliveryAck, bool>
        _acknowledgeReaderResult;
    private bool _helloSeen;
    private bool _authenticated;
    private string? _contextDeliveryMode;
    private string? _contextOnlySessionId;
    private DirectProtocolPhase _phase =
        DirectProtocolPhase.AwaitingAuthentication;

    internal DirectBridgeProtocolSession(
        string connectionId,
        string origin,
        DirectBridgeConfigStore configStore,
        DirectBridgeCoordinator coordinator,
        Func<DateTimeOffset>? utcNow = null,
        Func<ReaderResultDeliveryAck, bool>? acknowledgeReaderResult = null)
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
        _acknowledgeReaderResult =
            acknowledgeReaderResult ?? (_ => false);
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
                case "context-mode":
                    payload = HandleContextMode(message);
                    break;
                case "context-mode-set":
                    payload = await HandleContextModeSetAsync(
                        message,
                        cancellationToken).ConfigureAwait(false);
                    break;
                case "context-open":
                    payload = HandleContextOpen(message);
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
                case ReaderResultDeliveryProtocol.AckType:
                    payload = HandleReaderResultAck(message);
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

    private async Task<object> HandleContextModeSetAsync(
        JsonElement message,
        CancellationToken cancellationToken)
    {
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId",
            "mode",
            "sessionId");
        RequireAuthenticated();
        if (
            _phase != DirectProtocolPhase.AwaitingStart
            || _coordinator.CaptureActive
        )
        {
            throw new DirectProtocolException(
                "BW_READER_CONTEXT_DELIVERY_MODE_BUSY",
                "请先结束当前电脑语音，再切换上下文模式",
                retryable: true);
        }
        string mode = RequireString(message, "mode", 32);
        if (!DirectContextDeliveryMode.IsSupported(mode))
        {
            throw new DirectProtocolException(
                "BW_READER_CONTEXT_DELIVERY_MODE_INVALID",
                "Reader 上下文交付模式无效");
        }
        string previousMode = RequireContextDeliveryMode();
        string sessionId = RequireSafeId(message, "sessionId");
        _ = DirectPcmFrameCodec.ParseSessionId(sessionId);
        if (
            previousMode == DirectContextDeliveryMode.SnapshotMcp
            && mode == DirectContextDeliveryMode.LegacyInject
        )
        {
            await _coordinator.ClearSnapshotContextAsync(
                _connectionId,
                RequireSafeId(message, "requestId"),
                sessionId,
                requireActiveOwner: false,
                cancellationToken).ConfigureAwait(false);
        }
        DirectBridgeConfig updated =
            _configStore.UpdateContextDeliveryMode(mode);
        _contextDeliveryMode = updated.ContextDeliveryMode;
        _contextOnlySessionId = null;
        return new
        {
            mode = updated.ContextDeliveryMode,
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
        };
    }

    private async Task<DirectStartActionResult> HandleStartAsync(
        JsonElement message,
        Func<string, string, Task> reportStatusAsync,
        Func<string, DirectPcmFrame, CancellationToken, Task>
            sendPcmFrameAsync,
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
        _ = DirectPcmFrameCodec.ParseSessionId(sessionId);
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
                    RequireContextDeliveryMode(),
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

    private object HandleReaderResultAck(JsonElement message)
    {
        RequireAuthenticated();
        HashSet<string> actual = message.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        HashSet<string> expected = new(
            [
                "contract",
                "type",
                "requestId",
                "correlation",
                "outcome",
            ],
            StringComparer.Ordinal);
        bool hasError = actual.Remove("error");
        if (!actual.SetEquals(expected))
        {
            throw new DirectProtocolException(
                "BW_READER_RESULT_ACK_INVALID",
                "Reader 结果回执字段不匹配");
        }
        string correlation = RequireSafeId(
            message,
            "correlation");
        if (correlation.Length > 40)
        {
            throw new DirectProtocolException(
                "BW_READER_RESULT_ACK_INVALID",
                "Reader 结果回执 correlation 过长");
        }
        string outcome = RequireString(
            message,
            "outcome",
            16);
        if (
            outcome is not (
                ReaderResultDeliveryProtocol.RenderedOutcome
                or ReaderResultDeliveryProtocol.ReplayOutcome
                or ReaderResultDeliveryProtocol.RejectedOutcome)
        )
        {
            throw new DirectProtocolException(
                "BW_READER_RESULT_ACK_INVALID",
                "Reader 结果回执 outcome 无效");
        }
        string? error = null;
        if (hasError)
        {
            error = RequireString(message, "error", 500);
        }
        if (
            (outcome
                == ReaderResultDeliveryProtocol.RejectedOutcome)
                != hasError
        )
        {
            throw new DirectProtocolException(
                "BW_READER_RESULT_ACK_INVALID",
                "Reader 拒绝回执必须且只能携带 error");
        }
        bool matched = _acknowledgeReaderResult(
            new ReaderResultDeliveryAck(
                correlation,
                outcome,
                error));
        return new
        {
            correlation,
            outcome,
            matched,
        };
    }

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

internal sealed record ReaderResultDeliveryRequest(
    string Correlation,
    JsonObject Anchor,
    JsonArray Parts);

internal sealed record ReaderResultDeliveryAck(
    string Correlation,
    string Outcome,
    string? Error);

internal static class ReaderResultDeliveryProtocol
{
    internal const string DeliveryContract =
        "reader-result-delivery/1";
    internal const string EventName = "reader-result";
    internal const string AckType = "reader-result-ack";
    internal const string RenderedOutcome = "rendered";
    internal const string ReplayOutcome = "replay";
    internal const string RejectedOutcome = "rejected";

    internal static object Event(
        ReaderResultDeliveryRequest request) =>
        new
        {
            contract = DirectBridgeContract.Contract,
            type = "event",
            @event = EventName,
            payload = new
            {
                contract = DeliveryContract,
                correlation = request.Correlation,
                anchor = request.Anchor,
                parts = request.Parts,
            },
        };
}

internal sealed class ReaderResultDeliveryException : Exception
{
    internal string Code { get; }

    internal bool Retryable { get; }

    internal ReaderResultDeliveryException(
        string code,
        string message,
        bool retryable,
        Exception? innerException = null)
        : base(message, innerException)
    {
        Code = code;
        Retryable = retryable;
    }
}

internal sealed class ReaderResultDeliveryBroker
{
    private const int MaximumPendingDeliveries = 4;
    private static readonly TimeSpan AckTimeout =
        TimeSpan.FromSeconds(5);

    private readonly object _gate = new();
    private readonly Dictionary<
        string,
        TaskCompletionSource<ReaderResultDeliveryAck>> _pending =
            new(StringComparer.Ordinal);
    private string? _connectionId;
    private Func<object, CancellationToken, Task>? _sendAsync;

    internal void Attach(
        string connectionId,
        Func<object, CancellationToken, Task> sendAsync)
    {
        ArgumentNullException.ThrowIfNull(sendAsync);
        lock (_gate)
        {
            _connectionId = connectionId;
            _sendAsync = sendAsync;
        }
    }

    internal void Detach(string connectionId)
    {
        TaskCompletionSource<ReaderResultDeliveryAck>[] abandoned;
        lock (_gate)
        {
            if (!string.Equals(
                _connectionId,
                connectionId,
                StringComparison.Ordinal))
            {
                return;
            }
            _connectionId = null;
            _sendAsync = null;
            abandoned = _pending.Values.ToArray();
            _pending.Clear();
        }
        ReaderResultDeliveryException failure = new(
            "BW_READER_RESULT_READER_DISCONNECTED",
            "Reader 结果送达前直连已断开",
            retryable: true);
        foreach (
            TaskCompletionSource<ReaderResultDeliveryAck> completion
                in abandoned
        )
        {
            completion.TrySetException(failure);
        }
    }

    internal bool Acknowledge(
        string connectionId,
        ReaderResultDeliveryAck ack)
    {
        TaskCompletionSource<ReaderResultDeliveryAck>? completion;
        lock (_gate)
        {
            if (
                !string.Equals(
                    _connectionId,
                    connectionId,
                    StringComparison.Ordinal)
                || !_pending.TryGetValue(
                    ack.Correlation,
                    out completion)
            )
            {
                return false;
            }
        }
        return completion.TrySetResult(ack);
    }

    internal async Task<ReaderResultDeliveryAck> DeliverAsync(
        ReaderResultDeliveryRequest request,
        CancellationToken cancellationToken)
    {
        Func<object, CancellationToken, Task> sendAsync;
        TaskCompletionSource<ReaderResultDeliveryAck> completion = new(
            TaskCreationOptions.RunContinuationsAsynchronously);
        lock (_gate)
        {
            if (_sendAsync is null || _connectionId is null)
            {
                throw new ReaderResultDeliveryException(
                    "BW_READER_RESULT_READER_OFFLINE",
                    "当前没有已认证的 Reader 直连",
                    retryable: true);
            }
            if (_pending.Count >= MaximumPendingDeliveries)
            {
                throw new ReaderResultDeliveryException(
                    "BW_READER_RESULT_CAPACITY",
                    "Reader 结果待回执队列已满",
                    retryable: true);
            }
            if (!_pending.TryAdd(request.Correlation, completion))
            {
                throw new ReaderResultDeliveryException(
                    "BW_READER_RESULT_DUPLICATE_PENDING",
                    "相同 correlation 的 Reader 结果仍在等待回执",
                    retryable: true);
            }
            sendAsync = _sendAsync;
        }

        try
        {
            await sendAsync(
                ReaderResultDeliveryProtocol.Event(request),
                cancellationToken).ConfigureAwait(false);
            try
            {
                return await completion.Task.WaitAsync(
                    AckTimeout,
                    cancellationToken).ConfigureAwait(false);
            }
            catch (TimeoutException exception)
            {
                throw new ReaderResultDeliveryException(
                    "BW_READER_RESULT_ACK_TIMEOUT",
                    "Reader 结果送达后未在时限内收到渲染回执",
                    retryable: true,
                    exception);
            }
        }
        catch (ReaderResultDeliveryException)
        {
            throw;
        }
        catch (OperationCanceledException)
            when (cancellationToken.IsCancellationRequested)
        {
            throw;
        }
        catch (
            Exception exception
        ) when (
            exception is WebSocketException
            or ObjectDisposedException
            or InvalidOperationException
        )
        {
            throw new ReaderResultDeliveryException(
                "BW_READER_RESULT_SEND_FAILED",
                "Reader 结果无法写入当前直连",
                retryable: true,
                exception);
        }
        finally
        {
            lock (_gate)
            {
                if (
                    _pending.TryGetValue(
                        request.Correlation,
                        out TaskCompletionSource<
                            ReaderResultDeliveryAck>? current)
                    && ReferenceEquals(current, completion)
                )
                {
                    _pending.Remove(request.Correlation);
                }
            }
        }
    }
}
