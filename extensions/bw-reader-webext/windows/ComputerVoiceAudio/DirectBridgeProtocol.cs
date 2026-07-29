using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace BwReader.ComputerVoiceAudio;

internal enum DirectProtocolPhase
{
    AwaitingAuthentication,
    AwaitingStart,
    Starting,
    Active,
}

internal sealed class DirectBridgeProtocolSession
{
    private readonly string _connectionId;
    private readonly string _origin;
    private readonly DirectBridgeConfigStore _configStore;
    private readonly DirectBridgeCoordinator _coordinator;
    private readonly Func<DateTimeOffset> _utcNow;
    private DirectAuthenticationChallenge? _challenge;
    private bool _helloSeen;
    private bool _authenticated;
    private DirectProtocolPhase _phase =
        DirectProtocolPhase.AwaitingAuthentication;

    internal DirectBridgeProtocolSession(
        string connectionId,
        string origin,
        DirectBridgeConfigStore configStore,
        DirectBridgeCoordinator coordinator,
        Func<DateTimeOffset>? utcNow = null)
    {
        if (!DirectBridgeContract.IsSafeId(connectionId))
        {
            throw new ArgumentException(
                "connectionId must be a safe identifier",
                nameof(connectionId));
        }
        _connectionId = connectionId;
        _origin = origin;
        _configStore = configStore;
        _coordinator = coordinator;
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
                case "pair":
                    payload = await HandlePairAsync(
                        message,
                        cancellationToken).ConfigureAwait(false);
                    break;
                case "auth":
                    payload = HandleAuthentication(message);
                    break;
                case "status":
                    payload = HandleStatus(message);
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
            or CryptographicException
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
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId");
        if (_helloSeen || _authenticated)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_HELLO_REPEATED",
                "每条连接只能发送一次 hello");
        }
        _helloSeen = true;
        _challenge = DirectAuthenticationChallenge.Create(
            _origin,
            _utcNow());
        DirectBridgeConfig config = _configStore.Load();
        return new
        {
            protocolVersion = 1,
            paired = config.HasPairedClient,
            authentication = "ecdsa-p256-sha256",
            signatureFormat = "ieee-p1363-fixed-64",
            challenge = new
            {
                challengeId = _challenge.ChallengeId,
                nonce = _challenge.Nonce,
                expiresAtUtc = _challenge.ExpiresAtUtc,
                signingContract =
                    DirectBridgeContract.AuthenticationContract,
            },
            limits = new
            {
                maxMessageBytes =
                    DirectBridgeContract.MaximumMessageBytes,
                pcmFrameBytes = DirectBridgeContract.PcmFrameBytes,
                pcmQueueLimitMs =
                    DirectBridgeContract.PcmQueueLimitMilliseconds,
                heartbeatIntervalMs =
                    DirectBridgeContract
                        .ClientHeartbeatIntervalMilliseconds,
                heartbeatTimeoutMs =
                    DirectBridgeContract
                        .ClientHeartbeatTimeoutMilliseconds,
            },
        };
    }

    private async Task<object> HandlePairAsync(
        JsonElement message,
        CancellationToken cancellationToken)
    {
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId",
            "pairingCode",
            "clientPublicKeySpki");
        RequireHello();
        if (_authenticated)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_ALREADY_AUTHENTICATED",
                "当前连接已经认证");
        }
        string pairingCode = RequireString(
            message,
            "pairingCode",
            DirectBridgeContract.PairingCodeLength);
        string clientSpki = RequireString(
            message,
            "clientPublicKeySpki",
            512);
        DirectBridgeConfig paired = await _configStore.PairClientAsync(
            pairingCode,
            clientSpki,
            _utcNow(),
            cancellationToken).ConfigureAwait(false);
        return new
        {
            paired = true,
            clientFingerprintSha256 =
                paired.PairedClientFingerprintSha256,
        };
    }

    private object HandleAuthentication(JsonElement message)
    {
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId",
            "challengeId",
            "signature");
        RequireHello();
        if (_authenticated)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_ALREADY_AUTHENTICATED",
                "当前连接已经认证");
        }
        DirectAuthenticationChallenge challenge = _challenge
            ?? throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_CHALLENGE_REQUIRED",
                "认证 challenge 不存在");
        _challenge = null;
        if (
            challenge.ExpiresAtUtc < _utcNow()
            || RequireString(message, "challengeId", 64)
                != challenge.ChallengeId
        )
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_CHALLENGE_INVALID",
                "认证 challenge 无效或已过期");
        }

        DirectBridgeConfig config = _configStore.Load();
        if (!config.HasPairedClient)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_PAIRING_REQUIRED",
                "电脑客户端尚未配对");
        }
        byte[] signature = DirectBase64Url.Decode(
            RequireString(message, "signature", 128),
            64,
            "BW_COMPUTER_VOICE_DIRECT_SIGNATURE_INVALID");
        byte[] spki = DirectBase64Url.Decode(
            config.PairedClientPublicKeySpki,
            256,
            "BW_COMPUTER_VOICE_DIRECT_CONFIG_INVALID");
        try
        {
            if (signature.Length != 64)
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_SIGNATURE_INVALID",
                    "认证签名必须是 64 字节 P1363");
            }
            using ECDsa key = ECDsa.Create();
            key.ImportSubjectPublicKeyInfo(spki, out int bytesRead);
            byte[] payload = DirectBridgeContract
                .BuildAuthenticationPayload(
                    challenge.ChallengeId,
                    challenge.Nonce,
                    _origin);
            bool verified = bytesRead == spki.Length
                && key.KeySize == 256
                && key.VerifyData(
                    payload,
                    signature,
                    HashAlgorithmName.SHA256,
                    DSASignatureFormat
                        .IeeeP1363FixedFieldConcatenation);
            CryptographicOperations.ZeroMemory(payload);
            if (!verified)
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_AUTH_DENIED",
                    "客户端签名验证失败");
            }
        }
        finally
        {
            CryptographicOperations.ZeroMemory(signature);
            CryptographicOperations.ZeroMemory(spki);
        }
        _authenticated = true;
        _phase = DirectProtocolPhase.AwaitingStart;
        return new
        {
            authenticated = true,
            clientFingerprintSha256 =
                config.PairedClientFingerprintSha256,
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
        string state;
        string? reason;
        bool ready;
        if (captureActive)
        {
            state = "active";
            reason = null;
            ready = true;
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
        else
        {
            state = "idle";
            reason = null;
            ready = true;
        }
        return new
        {
            ready,
            state,
            reason,
            localOptIn = config.LocalOptIn,
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

    private void RequireHello()
    {
        if (!_helloSeen)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_HELLO_REQUIRED",
                "必须先完成 hello");
        }
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

internal sealed record DirectAuthenticationChallenge(
    string ChallengeId,
    string Nonce,
    string Origin,
    DateTimeOffset ExpiresAtUtc)
{
    internal static DirectAuthenticationChallenge Create(
        string origin,
        DateTimeOffset nowUtc) =>
        new(
            DirectBase64Url.Encode(
                RandomNumberGenerator.GetBytes(
                    DirectBridgeContract.ChallengeIdBytes)),
            DirectBase64Url.Encode(
                RandomNumberGenerator.GetBytes(
                    DirectBridgeContract.ChallengeBytes)),
            origin,
            nowUtc.AddSeconds(
                DirectBridgeContract.ChallengeLifetimeSeconds));
}
