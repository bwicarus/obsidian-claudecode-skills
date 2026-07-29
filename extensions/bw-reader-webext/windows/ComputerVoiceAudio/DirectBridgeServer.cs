using System.Buffers;
using System.Net;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Server.Kestrel.Core;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Primitives;

namespace BwReader.ComputerVoiceAudio;

internal sealed class DirectBridgeServer : IAsyncDisposable
{
    private const int CoordinatorDisposeAttemptLimit = 2;
    private const string SingleUserReaderOrigin =
        "https://bwicarus.taile44d0c.ts.net";
    private const string SingleUserChromeExtensionOrigin =
        "chrome-extension://jddhhakcblmihidgdobfkcejjinpigak";
    private const string SafariExtensionOriginPrefix =
        "safari-web-extension://";

    private static readonly UTF8Encoding StrictUtf8 = new(
        encoderShouldEmitUTF8Identifier: false,
        throwOnInvalidBytes: true);

    private readonly DirectBridgeConfigStore _configStore;
    private readonly DirectBridgeCoordinator _coordinator;
    private readonly SemaphoreSlim _connectionGate = new(1, 1);
    private readonly SemaphoreSlim _disposeGate = new(1, 1);
    private readonly string _serviceInstanceId;
    private readonly DirectRuntimeStatusWriter _statusWriter;
    private readonly DirectServiceLease _serviceLease;
    private readonly object _runtimeStateGate = new();
    private string _runtimeState = "starting";
    private bool _runtimeReaderConnected;
    private bool _runtimeCaptureActive;
    private bool _connectionActive;
    private bool _disposed;
    private bool _disposeCompleted;

    internal DirectBridgeServer(
        DirectBridgeConfigStore configStore,
        IDirectAppLauncher appLauncher,
        IDirectMediaAdapter mediaAdapter,
        IDirectContextAdapter? contextAdapter = null,
        IDirectSnapshotContextAdapter? snapshotContextAdapter = null)
    {
        _configStore = configStore;
        DirectBridgeConfig config = configStore.Load();
        _coordinator = new DirectBridgeCoordinator(
            configStore,
            appLauncher,
            mediaAdapter,
            contextAdapter: contextAdapter,
            snapshotContextAdapter: snapshotContextAdapter);
        _serviceInstanceId = Guid.NewGuid().ToString("N");
        _statusWriter = new DirectRuntimeStatusWriter(
            config.RuntimeStatusPath,
            _serviceInstanceId);
        _serviceLease = new DirectServiceLease(
            configStore.InstallationRoot,
            configStore.Path);
    }

    internal async Task<int> RunAsync(CancellationToken cancellationToken)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        DirectBridgeConfig config = _configStore.Load();
        await WriteRuntimeStatusAsync(
            "starting",
            readerConnected: false,
            captureActive: false,
            cancellationToken).ConfigureAwait(false);

        WebApplicationBuilder builder =
            WebApplication.CreateSlimBuilder();
        builder.Logging.ClearProviders();
        builder.WebHost.ConfigureKestrel(options =>
        {
            options.AddServerHeader = false;
            options.Limits.MaxRequestBodySize =
                DirectBridgeContract.MaximumMessageBytes;
            options.Limits.RequestHeadersTimeout =
                TimeSpan.FromSeconds(10);
            options.Listen(
                IPAddress.Loopback,
                config.ListenPort,
                listen => listen.Protocols = HttpProtocols.Http1);
        });
        builder.Services.Configure<HostOptions>(options =>
        {
            options.ShutdownTimeout = TimeSpan.FromSeconds(10);
        });

        await using WebApplication app = builder.Build();
        app.UseWebSockets(new WebSocketOptions
        {
            KeepAliveInterval = TimeSpan.FromSeconds(20),
        });
        app.MapGet(
            "/healthz",
            context => HandleHealthAsync(context, cancellationToken));
        app.Map(
            "/reader-computer-voice/v1",
            context => HandleBridgeAsync(context, cancellationToken));
        app.MapFallback(context =>
        {
            context.Response.StatusCode = StatusCodes.Status404NotFound;
            return Task.CompletedTask;
        });

        CancellationTokenSource? heartbeatLifetime = null;
        Task? heartbeatTask = null;
        try
        {
            await app.StartAsync(cancellationToken).ConfigureAwait(false);
            await _serviceLease.WriteAsync(cancellationToken)
                .ConfigureAwait(false);
            await WriteRuntimeStatusAsync(
                "idle",
                readerConnected: false,
                captureActive: false,
                cancellationToken).ConfigureAwait(false);
            heartbeatLifetime =
                CancellationTokenSource.CreateLinkedTokenSource(
                    cancellationToken);
            heartbeatTask = HeartbeatAsync(heartbeatLifetime.Token);
            DirectSecurityLog.Write(
                _serviceInstanceId,
                "service-start",
                "BW_COMPUTER_VOICE_DIRECT_SERVICE_STARTED",
                ok: true);
            await app.WaitForShutdownAsync(cancellationToken)
                .ConfigureAwait(false);
            return 0;
        }
        catch (OperationCanceledException)
            when (cancellationToken.IsCancellationRequested)
        {
            return 0;
        }
        finally
        {
            if (heartbeatLifetime is not null)
            {
                heartbeatLifetime.Cancel();
            }
            if (heartbeatTask is not null)
            {
                try
                {
                    await heartbeatTask.ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                }
            }
            heartbeatLifetime?.Dispose();
            try
            {
                await WriteRuntimeStatusAsync(
                    "stopping",
                    readerConnected: false,
                    captureActive: _coordinator.CaptureActive,
                    CancellationToken.None).ConfigureAwait(false);
                await app.StopAsync(CancellationToken.None)
                    .ConfigureAwait(false);
                await WriteRuntimeStatusAsync(
                    "stopped",
                    readerConnected: false,
                    captureActive: false,
                    CancellationToken.None).ConfigureAwait(false);
                DirectSecurityLog.Write(
                    _serviceInstanceId,
                    "service-stop",
                    "BW_COMPUTER_VOICE_DIRECT_SERVICE_STOPPED",
                    ok: true);
            }
            finally
            {
                await _serviceLease.ClearIfOwnedAsync()
                    .ConfigureAwait(false);
            }
        }
    }

    private async Task HandleHealthAsync(
        HttpContext context,
        CancellationToken serviceCancellationToken)
    {
        if (!OriginAllowed(
            context,
            requireOrigin: false,
            out _))
        {
            context.Response.StatusCode = StatusCodes.Status403Forbidden;
            return;
        }
        context.Response.Headers.CacheControl = "no-store";
        context.Response.ContentType = "application/json";
        await context.Response.WriteAsJsonAsync(new
        {
            contract = "reader-computer-voice-direct-health/1",
            ok = true,
            serviceInstanceId = _serviceInstanceId,
            state = _coordinator.CaptureActive ? "active" : "idle",
            captureActive = _coordinator.CaptureActive,
        }, DirectBridgeContract.JsonOptions, serviceCancellationToken)
            .ConfigureAwait(false);
    }

    private async Task HandleBridgeAsync(
        HttpContext context,
        CancellationToken serviceCancellationToken)
    {
        if (
            !HttpMethods.IsGet(context.Request.Method)
            || !context.WebSockets.IsWebSocketRequest
        )
        {
            context.Response.StatusCode =
                StatusCodes.Status426UpgradeRequired;
            return;
        }
        if (!OriginAllowed(
            context,
            requireOrigin: true,
            out string origin))
        {
            DirectSecurityLog.Write(
                _serviceInstanceId,
                "origin-denied",
                "BW_COMPUTER_VOICE_DIRECT_ORIGIN_DENIED",
                ok: false);
            context.Response.StatusCode = StatusCodes.Status403Forbidden;
            return;
        }

        await _connectionGate.WaitAsync(serviceCancellationToken)
            .ConfigureAwait(false);
        try
        {
            if (_connectionActive)
            {
                context.Response.StatusCode =
                    StatusCodes.Status409Conflict;
                return;
            }
            _connectionActive = true;
        }
        finally
        {
            _connectionGate.Release();
        }

        string connectionId = "connection-"
            + DirectBase64Url.Encode(
                System.Security.Cryptography.RandomNumberGenerator
                    .GetBytes(12));
        try
        {
            using CancellationTokenSource connectionLifetime =
                CancellationTokenSource.CreateLinkedTokenSource(
                    serviceCancellationToken,
                    context.RequestAborted);
            CancellationToken connectionToken =
                connectionLifetime.Token;
            using WebSocket socket = await context.WebSockets
                .AcceptWebSocketAsync().ConfigureAwait(false);
            DirectConnectionPhaseDeadline phaseDeadline = new();
            DirectSecurityLog.Write(
                _serviceInstanceId,
                "reader-connect",
                "BW_COMPUTER_VOICE_DIRECT_READER_CONNECTED",
                ok: true);
            await WriteRuntimeStatusAsync(
                "reader-connected",
                readerConnected: true,
                captureActive: false,
                connectionToken).ConfigureAwait(false);
            await RunConnectionAsync(
                socket,
                connectionId,
                origin,
                phaseDeadline,
                connectionToken).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
        }
        catch (WebSocketException)
        {
            DirectSecurityLog.Write(
                _serviceInstanceId,
                "reader-disconnect",
                "BW_COMPUTER_VOICE_DIRECT_SOCKET_CLOSED",
                ok: false);
        }
        finally
        {
            await _coordinator.StopForConnectionAsync(connectionId)
                .ConfigureAwait(false);
            await WriteRuntimeStatusAsync(
                "idle",
                readerConnected: false,
                captureActive: false,
                CancellationToken.None).ConfigureAwait(false);
            await _connectionGate.WaitAsync().ConfigureAwait(false);
            try
            {
                _connectionActive = false;
            }
            finally
            {
                _connectionGate.Release();
            }
        }
    }

    private async Task RunConnectionAsync(
        WebSocket socket,
        string connectionId,
        string origin,
        DirectConnectionPhaseDeadline phaseDeadline,
        CancellationToken cancellationToken)
    {
        using SemaphoreSlim sendGate = new(1, 1);
        DirectPcmSequenceGuard downlinkSequenceGuard = new();
        DirectUplinkSequenceGuard uplinkSequenceGuard = new();
        DirectBridgeProtocolSession protocol = new(
            connectionId,
            origin,
            _configStore,
            _coordinator);
        Task<DirectClientMessage?>? prefetchedReceiveTask = null;

        while (
            !cancellationToken.IsCancellationRequested
            && socket.State == WebSocketState.Open
        )
        {
            Task<DirectClientMessage?> receiveTask =
                prefetchedReceiveTask
                ?? ReceiveMessageAsync(
                    socket,
                    cancellationToken);
            prefetchedReceiveTask = null;
            Task<DirectProtocolException?>? mediaCompletion = null;
            Task? heartbeatTimeout = null;
            Task? phaseTimeout = null;
            using CancellationTokenSource deadlineLifetime =
                CancellationTokenSource.CreateLinkedTokenSource(
                    cancellationToken);
            List<Task> monitoredTasks = [receiveTask];
            phaseDeadline.Observe(protocol.Phase);
            int? phaseRemaining =
                phaseDeadline.GetRemainingMilliseconds(protocol.Phase);
            if (phaseRemaining is int remainingInPhase)
            {
                phaseTimeout = Task.Delay(
                    remainingInPhase,
                    deadlineLifetime.Token);
                monitoredTasks.Add(phaseTimeout);
            }
            if (ShouldMonitorMedia(_coordinator))
            {
                mediaCompletion = _coordinator.MediaCompletion;
                monitoredTasks.Add(mediaCompletion);
            }
            int? heartbeatRemaining =
                _coordinator.GetHeartbeatRemainingMilliseconds(
                    connectionId);
            if (heartbeatRemaining is int remaining)
            {
                heartbeatTimeout = Task.Delay(
                    remaining,
                    deadlineLifetime.Token);
                monitoredTasks.Add(heartbeatTimeout);
            }
            Task winner = await Task.WhenAny(monitoredTasks)
                .ConfigureAwait(false);
            deadlineLifetime.Cancel();
            if (cancellationToken.IsCancellationRequested)
            {
                socket.Abort();
                await ObserveReceiveAsync(receiveTask)
                    .ConfigureAwait(false);
                break;
            }

            if (winner == mediaCompletion)
            {
                DirectProtocolException failure =
                    await mediaCompletion!.ConfigureAwait(false)
                    ?? new DirectProtocolException(
                        "BW_COMPUTER_VOICE_DIRECT_MEDIA_STOPPED_UNEXPECTEDLY",
                        "媒体捕获意外停止");
                await HandleConnectionFailureAsync(
                    connectionId,
                    socket,
                    sendGate,
                    failure,
                    "media-fault").ConfigureAwait(false);
                await ObserveReceiveAsync(receiveTask)
                    .ConfigureAwait(false);
                break;
            }
            if (
                winner == phaseTimeout
                || phaseDeadline.IsExpired(protocol.Phase)
            )
            {
                await HandleConnectionFailureAsync(
                    connectionId,
                    socket,
                    sendGate,
                    DirectConnectionPhaseDeadline.TimeoutFailure(
                        protocol.Phase),
                    "connection-phase-timeout").ConfigureAwait(false);
                await ObserveReceiveAsync(receiveTask)
                    .ConfigureAwait(false);
                break;
            }
            if (
                winner == heartbeatTimeout
                || _coordinator.IsHeartbeatExpired(connectionId)
            )
            {
                await HandleConnectionFailureAsync(
                    connectionId,
                    socket,
                    sendGate,
                    new DirectProtocolException(
                        "BW_COMPUTER_VOICE_DIRECT_HEARTBEAT_TIMEOUT",
                        "阅读器心跳超时"),
                    "heartbeat-timeout").ConfigureAwait(false);
                await ObserveReceiveAsync(receiveTask)
                    .ConfigureAwait(false);
                break;
            }

            DirectClientMessage? incoming =
                await receiveTask.ConfigureAwait(false);
            if (incoming is null)
            {
                break;
            }
            if (incoming.MessageType == WebSocketMessageType.Binary)
            {
                try
                {
                    if (protocol.Phase != DirectProtocolPhase.Active)
                    {
                        throw new DirectProtocolException(
                            "BW_COMPUTER_VOICE_DIRECT_UPLINK_NOT_ACTIVE",
                            "START 成功前不接受浏览器麦克风 binary");
                    }
                    DirectDecodedPcmFrame decoded =
                        DirectPcmFrameCodec.DecodeUplink(
                            incoming.BinaryPayload.Span);
                    string activeSessionId =
                        _coordinator.ActiveSessionId
                        ?? throw new DirectProtocolException(
                            "BW_COMPUTER_VOICE_DIRECT_UPLINK_NOT_ACTIVE",
                            "浏览器麦克风上行尚未启动");
                    uplinkSequenceGuard.Validate(
                        activeSessionId,
                        decoded);
                    await _coordinator.PushUplinkFrameAsync(
                        connectionId,
                        decoded.SessionId,
                        decoded.Frame,
                        cancellationToken).ConfigureAwait(false);
                }
                catch (DirectProtocolException failure)
                {
                    await HandleConnectionFailureAsync(
                        connectionId,
                        socket,
                        sendGate,
                        failure,
                        "uplink-rejected").ConfigureAwait(false);
                    break;
                }
                continue;
            }
            string message = incoming.Text
                ?? throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_MESSAGE_INVALID",
                    "直连文本消息无效");

            DirectProtocolPhase phaseBeforeMessage = protocol.Phase;
            Task<DirectProtocolReply> HandleMessageAsync(
                CancellationToken messageCancellationToken) =>
                protocol.HandleAsync(
                    message,
                    async (state, reason) =>
                    {
                        await WriteRuntimeStatusAsync(
                            state,
                            readerConnected: true,
                            captureActive: state == "active",
                            messageCancellationToken).ConfigureAwait(false);
                        await SendJsonAsync(
                            socket,
                            sendGate,
                            DirectBridgeProtocolSession.StatusEvent(
                                state,
                                reason),
                            messageCancellationToken).ConfigureAwait(false);
                    },
                    async (sessionId, frame, token) =>
                    {
                        downlinkSequenceGuard.Validate(sessionId, frame);
                        byte[] encoded = DirectPcmFrameCodec.Encode(
                            sessionId,
                            frame);
                        await SendBinaryAsync(
                            socket,
                            sendGate,
                            encoded,
                            token).ConfigureAwait(false);
                    },
                    messageCancellationToken);

            DirectProtocolReply reply;
            if (IsStartRequest(message))
            {
                DirectPeerMonitorOutcome<DirectProtocolReply> monitored =
                    await MonitorStartForPeerCloseAsync(
                        socket,
                        HandleMessageAsync,
                        cancellationToken).ConfigureAwait(false);
                if (monitored.PeerClosed)
                {
                    await _coordinator.StopForConnectionAsync(connectionId)
                        .ConfigureAwait(false);
                    break;
                }
                reply = monitored.Result
                    ?? throw new InvalidOperationException(
                        "START monitor returned no protocol reply");
                prefetchedReceiveTask =
                    monitored.PrefetchedReceiveTask;
            }
            else
            {
                reply = await HandleMessageAsync(cancellationToken)
                    .ConfigureAwait(false);
            }
            if (
                phaseBeforeMessage
                    == DirectProtocolPhase.AwaitingAuthentication
                && protocol.IsAuthenticated
                && phaseDeadline.IsAuthenticationExpired()
            )
            {
                await HandleConnectionFailureAsync(
                    connectionId,
                    socket,
                    sendGate,
                    DirectConnectionPhaseDeadline.TimeoutFailure(
                        DirectProtocolPhase.AwaitingAuthentication),
                    "connection-phase-timeout").ConfigureAwait(false);
                break;
            }
            phaseDeadline.Observe(protocol.Phase);
            if (phaseDeadline.IsExpired(protocol.Phase))
            {
                await HandleConnectionFailureAsync(
                    connectionId,
                    socket,
                    sendGate,
                    DirectConnectionPhaseDeadline.TimeoutFailure(
                        protocol.Phase),
                    "connection-phase-timeout").ConfigureAwait(false);
                break;
            }
            using CancellationTokenSource replyLifetime =
                CancellationTokenSource.CreateLinkedTokenSource(
                    cancellationToken);
            int? replyRemaining =
                phaseDeadline.GetRemainingMilliseconds(protocol.Phase);
            if (replyRemaining is int remainingForReply)
            {
                replyLifetime.CancelAfter(remainingForReply);
            }
            try
            {
                await SendJsonAsync(
                    socket,
                    sendGate,
                    reply.Envelope,
                    replyLifetime.Token).ConfigureAwait(false);
                if (reply.AfterSendAsync is not null)
                {
                    await reply.AfterSendAsync(replyLifetime.Token)
                        .ConfigureAwait(false);
                }
                if (
                    phaseBeforeMessage != DirectProtocolPhase.Active
                    && protocol.Phase == DirectProtocolPhase.Active
                )
                {
                    uplinkSequenceGuard.Begin(
                        _coordinator.ActiveSessionId
                        ?? throw new DirectProtocolException(
                            "BW_COMPUTER_VOICE_DIRECT_UPLINK_NOT_ACTIVE",
                            "浏览器麦克风上行尚未启动"));
                }
                else if (
                    phaseBeforeMessage == DirectProtocolPhase.Active
                    && protocol.Phase != DirectProtocolPhase.Active
                )
                {
                    uplinkSequenceGuard.End();
                }
            }
            catch (OperationCanceledException)
                when (
                    replyLifetime.IsCancellationRequested
                    && !cancellationToken.IsCancellationRequested
                )
            {
                await HandleConnectionFailureAsync(
                    connectionId,
                    socket,
                    sendGate,
                    DirectConnectionPhaseDeadline.TimeoutFailure(
                        protocol.Phase),
                    "connection-phase-timeout").ConfigureAwait(false);
                break;
            }
            if (
                ShouldMonitorMedia(_coordinator)
                && _coordinator.MediaCompletion.IsCompleted
            )
            {
                DirectProtocolException? failure =
                    await _coordinator.MediaCompletion
                        .ConfigureAwait(false);
                if (failure is not null)
                {
                    await HandleConnectionFailureAsync(
                        connectionId,
                        socket,
                        sendGate,
                        failure,
                        "media-fault").ConfigureAwait(false);
                    break;
                }
            }
            if (_coordinator.IsHeartbeatExpired(connectionId))
            {
                await HandleConnectionFailureAsync(
                    connectionId,
                    socket,
                    sendGate,
                    new DirectProtocolException(
                        "BW_COMPUTER_VOICE_DIRECT_HEARTBEAT_TIMEOUT",
                        "阅读器心跳超时"),
                    "heartbeat-timeout").ConfigureAwait(false);
                break;
            }

            if (_coordinator.CaptureActive)
            {
                await WriteRuntimeStatusAsync(
                    "active",
                    readerConnected: true,
                    captureActive: true,
                    cancellationToken).ConfigureAwait(false);
            }
            else
            {
                await WriteRuntimeStatusAsync(
                    "reader-connected",
                    readerConnected: true,
                    captureActive: false,
                    cancellationToken).ConfigureAwait(false);
            }
        }
    }

    internal static bool ShouldMonitorMedia(
        DirectBridgeCoordinator coordinator) =>
        coordinator.ActiveSessionId is not null;

    internal static bool IsStartRequest(string json)
    {
        try
        {
            using JsonDocument document = JsonDocument.Parse(
                json,
                new JsonDocumentOptions
                {
                    AllowTrailingCommas = false,
                    CommentHandling = JsonCommentHandling.Disallow,
                    MaxDepth = 16,
                });
            return document.RootElement.ValueKind == JsonValueKind.Object
                && document.RootElement.TryGetProperty(
                    "type",
                    out JsonElement action)
                && action.ValueKind == JsonValueKind.String
                && action.GetString() == "start";
        }
        catch (JsonException)
        {
            return false;
        }
    }

    internal static async Task<DirectPeerMonitorOutcome<T>>
        MonitorStartForPeerCloseAsync<T>(
            WebSocket socket,
            Func<CancellationToken, Task<T>> startOperation,
            CancellationToken cancellationToken)
        where T : class
    {
        using CancellationTokenSource startLifetime =
            CancellationTokenSource.CreateLinkedTokenSource(
                cancellationToken);
        Task<DirectClientMessage?> receiveTask = ReceiveMessageAsync(
            socket,
            cancellationToken);
        Task<T> operationTask = startOperation(startLifetime.Token);
        Task winner = await Task.WhenAny(
            operationTask,
            receiveTask).ConfigureAwait(false);

        if (winner == receiveTask || receiveTask.IsCompleted)
        {
            DirectClientMessage? prefetched;
            try
            {
                prefetched = await receiveTask.ConfigureAwait(false);
            }
            catch
            {
                startLifetime.Cancel();
                await ObserveCanceledOperationAsync(
                    operationTask,
                    startLifetime.Token).ConfigureAwait(false);
                throw;
            }
            if (prefetched is null)
            {
                startLifetime.Cancel();
                await ObserveCanceledOperationAsync(
                    operationTask,
                    startLifetime.Token).ConfigureAwait(false);
                return new DirectPeerMonitorOutcome<T>(
                    Result: null,
                    PrefetchedReceiveTask: null,
                    PeerClosed: true);
            }
            if (prefetched.MessageType == WebSocketMessageType.Binary)
            {
                startLifetime.Cancel();
                await ObserveCanceledOperationAsync(
                    operationTask,
                    startLifetime.Token).ConfigureAwait(false);
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_UPLINK_NOT_ACTIVE",
                    "START 成功前不接受浏览器麦克风 binary");
            }

            T completed = await operationTask.ConfigureAwait(false);
            return new DirectPeerMonitorOutcome<T>(
                completed,
                Task.FromResult<DirectClientMessage?>(prefetched),
                PeerClosed: false);
        }

        return new DirectPeerMonitorOutcome<T>(
            await operationTask.ConfigureAwait(false),
            receiveTask,
            PeerClosed: false);
    }

    private static async Task ObserveCanceledOperationAsync<T>(
        Task<T> operationTask,
        CancellationToken cancellationToken)
    {
        try
        {
            _ = await operationTask.ConfigureAwait(false);
        }
        catch (OperationCanceledException)
            when (cancellationToken.IsCancellationRequested)
        {
        }
    }

    private async Task HandleConnectionFailureAsync(
        string connectionId,
        WebSocket socket,
        SemaphoreSlim sendGate,
        DirectProtocolException failure,
        string logEvent)
    {
        _ = _coordinator.RecordFailure(failure, logEvent);
        await _coordinator.StopForConnectionAsync(connectionId)
            .ConfigureAwait(false);
        DirectSecurityLog.Write(
            _serviceInstanceId,
            logEvent,
            failure.Code,
            ok: false);
        await WriteRuntimeStatusAsync(
            "faulted",
            readerConnected: true,
            captureActive: false,
            CancellationToken.None).ConfigureAwait(false);
        try
        {
            using CancellationTokenSource notificationDeadline = new(
                TimeSpan.FromSeconds(2));
            if (socket.State == WebSocketState.Open)
            {
                await SendJsonAsync(
                    socket,
                    sendGate,
                    DirectBridgeProtocolSession.StatusEvent(
                        "error",
                        failure.Code),
                    notificationDeadline.Token).ConfigureAwait(false);
                await socket.CloseOutputAsync(
                    WebSocketCloseStatus.InternalServerError,
                    failure.Code,
                    notificationDeadline.Token).ConfigureAwait(false);
            }
        }
        catch (
            Exception exception
        ) when (
            exception is OperationCanceledException
            or WebSocketException
            or ObjectDisposedException
        )
        {
        }
        finally
        {
            socket.Abort();
        }
    }

    private static async Task ObserveReceiveAsync(
        Task<DirectClientMessage?> receiveTask)
    {
        try
        {
            _ = await receiveTask.WaitAsync(
                TimeSpan.FromSeconds(2)).ConfigureAwait(false);
        }
        catch
        {
        }
    }

    private bool OriginAllowed(
        HttpContext context,
        bool requireOrigin,
        out string origin)
    {
        StringValues values = context.Request.Headers.Origin;
        if (values.Count == 0)
        {
            origin = "";
            return !requireOrigin;
        }
        if (
            values.Count != 1
            || string.IsNullOrEmpty(values[0])
        )
        {
            origin = "";
            return false;
        }
        origin = values[0]!;
        DirectBridgeConfig config = _configStore.Load();
        if (!OriginMatchesAllowlist(config, origin))
        {
            return false;
        }
        return TailscaleLoginMatches(
            config,
            context.Request.Headers["Tailscale-User-Login"]);
    }

    internal static bool OriginMatchesAllowlist(
        DirectBridgeConfig config,
        string origin)
    {
        if (!config.ExperimentalSingleUserMode)
        {
            return config.AllowedOrigins.Contains(origin);
        }

        // Direct v3 has no browser-held client key.  Its browser boundary is
        // therefore deliberately smaller than the configurable v1 allowlist:
        // the exact Reader PWA or a controlled extension background origin.
        // Content scripts must relay through that background origin instead of
        // making a request whose Origin is an arbitrary visited web page.
        return string.Equals(
                origin,
                SingleUserReaderOrigin,
                StringComparison.Ordinal)
            || IsCanonicalChromeExtensionOrigin(origin)
            || IsCanonicalSafariExtensionOrigin(origin);
    }

    private static bool IsCanonicalChromeExtensionOrigin(string origin) =>
        string.Equals(
            origin,
            SingleUserChromeExtensionOrigin,
            StringComparison.Ordinal);

    private static bool IsCanonicalSafariExtensionOrigin(string origin)
    {
        if (!TryGetExtensionOriginIdentifier(
            origin,
            SafariExtensionOriginPrefix,
            out string identifier)
            || identifier.Length != 36
            || identifier[8] != '-'
            || identifier[13] != '-'
            || identifier[18] != '-'
            || identifier[23] != '-')
        {
            return false;
        }
        for (int index = 0; index < identifier.Length; index++)
        {
            if (index is 8 or 13 or 18 or 23)
            {
                continue;
            }
            char value = identifier[index];
            if (!(
                value is >= '0' and <= '9'
                || value is >= 'a' and <= 'f'
                || value is >= 'A' and <= 'F'
            ))
            {
                return false;
            }
        }
        return true;
    }

    private static bool TryGetExtensionOriginIdentifier(
        string origin,
        string prefix,
        out string identifier)
    {
        identifier = "";
        if (!origin.StartsWith(prefix, StringComparison.Ordinal))
        {
            return false;
        }
        identifier = origin[prefix.Length..];
        if (
            identifier.Length == 0
            || identifier.IndexOfAny(
                ['/', '\\', ':', '@', '?', '#']) >= 0
            || !Uri.TryCreate(origin, UriKind.Absolute, out Uri? uri)
            || uri.UserInfo.Length != 0
            || uri.Port != -1
            // System.Uri represents an authority-only custom-scheme URL with
            // a synthetic "/" AbsolutePath.  The raw identifier check above
            // still rejects any literal slash supplied by the caller.
            || uri.AbsolutePath != "/"
            || uri.Query.Length != 0
            || uri.Fragment.Length != 0
            || !string.Equals(
                uri.Scheme + "://",
                prefix,
                StringComparison.Ordinal)
        )
        {
            identifier = "";
            return false;
        }
        return true;
    }

    internal static bool TailscaleLoginMatches(
        DirectBridgeConfig config,
        StringValues values) =>
        values.Count == 1
        && !string.IsNullOrEmpty(values[0])
        && string.Equals(
            values[0],
            config.AllowedTailscaleUserLogin,
            StringComparison.OrdinalIgnoreCase);

    private async Task WriteRuntimeStatusAsync(
        string state,
        bool readerConnected,
        bool captureActive,
        CancellationToken cancellationToken)
    {
        lock (_runtimeStateGate)
        {
            _runtimeState = state;
            _runtimeReaderConnected = readerConnected;
            _runtimeCaptureActive = captureActive;
        }
        await _statusWriter.WriteAsync(
            state,
            readerConnected,
            captureActive,
            _coordinator.LastError,
            cancellationToken).ConfigureAwait(false);
    }

    private async Task HeartbeatAsync(
        CancellationToken cancellationToken)
    {
        using PeriodicTimer timer = new(
            DirectBridgeContract.RuntimeStatusHeartbeatInterval);
        while (await timer.WaitForNextTickAsync(cancellationToken)
            .ConfigureAwait(false))
        {
            string state;
            bool readerConnected;
            bool captureActive;
            lock (_runtimeStateGate)
            {
                state = _runtimeState;
                readerConnected = _runtimeReaderConnected;
                captureActive = _runtimeCaptureActive;
            }
            await _statusWriter.WriteAsync(
                state,
                readerConnected,
                captureActive,
                _coordinator.LastError,
                cancellationToken).ConfigureAwait(false);
        }
    }

    private static async Task<DirectClientMessage?> ReceiveMessageAsync(
        WebSocket socket,
        CancellationToken cancellationToken)
    {
        byte[] rented = ArrayPool<byte>.Shared.Rent(8192);
        try
        {
            using MemoryStream message = new(
                DirectBridgeContract.MaximumMessageBytes);
            WebSocketMessageType? messageType = null;
            while (true)
            {
                ValueWebSocketReceiveResult result =
                    await socket.ReceiveAsync(
                        rented.AsMemory(0, 8192),
                        cancellationToken).ConfigureAwait(false);
                if (result.MessageType == WebSocketMessageType.Close)
                {
                    // Return immediately so an in-flight START observes
                    // cancellation before application/audio side effects can
                    // continue. The per-connection socket is terminated after
                    // coordinator cleanup.
                    return null;
                }
                if (result.MessageType is not (
                    WebSocketMessageType.Text
                    or WebSocketMessageType.Binary))
                {
                    await socket.CloseAsync(
                        WebSocketCloseStatus.InvalidMessageType,
                        "unsupported websocket message type",
                        CancellationToken.None).ConfigureAwait(false);
                    return null;
                }
                if (
                    messageType.HasValue
                    && messageType.Value != result.MessageType
                )
                {
                    await socket.CloseAsync(
                        WebSocketCloseStatus.InvalidMessageType,
                        "websocket message type changed",
                        CancellationToken.None).ConfigureAwait(false);
                    return null;
                }
                messageType ??= result.MessageType;
                int maximumBytes =
                    messageType == WebSocketMessageType.Binary
                        ? DirectBridgeContract.PcmFrameBytes
                        : DirectBridgeContract.MaximumMessageBytes;
                if (
                    message.Length + result.Count
                        > maximumBytes
                )
                {
                    await socket.CloseAsync(
                        WebSocketCloseStatus.MessageTooBig,
                        "message too large",
                        CancellationToken.None).ConfigureAwait(false);
                    return null;
                }
                message.Write(rented, 0, result.Count);
                if (result.EndOfMessage)
                {
                    int length = checked((int)message.Length);
                    if (messageType == WebSocketMessageType.Binary)
                    {
                        return DirectClientMessage.Binary(
                            message.GetBuffer().AsMemory(0, length)
                                .ToArray());
                    }
                    return DirectClientMessage.TextMessage(
                        StrictUtf8.GetString(
                            message.GetBuffer(),
                            0,
                            length));
                }
            }
        }
        catch (DecoderFallbackException)
        {
            await socket.CloseAsync(
                WebSocketCloseStatus.InvalidPayloadData,
                "invalid utf-8",
                CancellationToken.None).ConfigureAwait(false);
            return null;
        }
        finally
        {
            ArrayPool<byte>.Shared.Return(rented, clearArray: true);
        }
    }

    private static async Task SendJsonAsync(
        WebSocket socket,
        SemaphoreSlim sendGate,
        object value,
        CancellationToken cancellationToken)
    {
        byte[] payload = JsonSerializer.SerializeToUtf8Bytes(
            value,
            DirectBridgeContract.JsonOptions);
        if (payload.Length > DirectBridgeContract.MaximumMessageBytes)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_SERVER_MESSAGE_TOO_LARGE",
                "服务端控制消息超过大小上限");
        }
        await sendGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            await socket.SendAsync(
                payload,
                WebSocketMessageType.Text,
                endOfMessage: true,
                cancellationToken).ConfigureAwait(false);
        }
        finally
        {
            sendGate.Release();
        }
    }

    private static async Task SendBinaryAsync(
        WebSocket socket,
        SemaphoreSlim sendGate,
        ReadOnlyMemory<byte> payload,
        CancellationToken cancellationToken)
    {
        if (payload.Length != DirectBridgeContract.PcmFrameBytes)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_PCM_FRAME_INVALID",
                "PCM 帧大小无效");
        }
        await sendGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            await socket.SendAsync(
                payload,
                WebSocketMessageType.Binary,
                endOfMessage: true,
                cancellationToken).ConfigureAwait(false);
        }
        finally
        {
            sendGate.Release();
        }
    }

    public async ValueTask DisposeAsync()
    {
        await _disposeGate.WaitAsync().ConfigureAwait(false);
        try
        {
            if (_disposeCompleted)
            {
                return;
            }
            // Keep the server terminal after the first disposal attempt, but
            // keep ownership reachable until a bounded owner-aware retry has
            // had a chance to release transient cleanup failures.  Program
            // uses one await-using disposal call in production, so that one
            // call must consume the retry budget itself.
            _disposed = true;
            await DisposeCoordinatorWithBoundedRetryAsync()
                .ConfigureAwait(false);
            _connectionGate.Dispose();
            _disposeCompleted = true;
        }
        finally
        {
            _disposeGate.Release();
        }
    }

    private async Task DisposeCoordinatorWithBoundedRetryAsync()
    {
        List<Exception>? failures = null;
        for (
            int attempt = 0;
            attempt < CoordinatorDisposeAttemptLimit;
            attempt++)
        {
            try
            {
                await _coordinator.DisposeAsync().ConfigureAwait(false);
                return;
            }
            catch (Exception exception)
            {
                failures ??= [];
                failures.Add(exception);
            }
        }

        Exception finalFailure = failures![^1];
        if (finalFailure is DirectProtocolException protocol)
        {
            throw new DirectProtocolException(
                protocol.Code,
                protocol.Message,
                protocol.Retryable,
                new AggregateException(failures));
        }
        throw new AggregateException(failures);
    }
}

internal sealed record DirectPeerMonitorOutcome<T>(
    T? Result,
    Task<DirectClientMessage?>? PrefetchedReceiveTask,
    bool PeerClosed)
    where T : class;

internal sealed record DirectClientMessage(
    WebSocketMessageType MessageType,
    string? Text,
    ReadOnlyMemory<byte> BinaryPayload)
{
    internal static DirectClientMessage TextMessage(string text) =>
        new(WebSocketMessageType.Text, text, ReadOnlyMemory<byte>.Empty);

    internal static DirectClientMessage Binary(byte[] payload) =>
        new(WebSocketMessageType.Binary, null, payload);
}

internal sealed class DirectPcmSequenceGuard
{
    private readonly object _gate = new();
    private readonly Dictionary<DirectPcmTrack, (uint Sequence, ulong Timestamp)>
        _last = [];
    private string? _sessionId;

    internal void Validate(string sessionId, DirectPcmFrame frame)
    {
        _ = DirectPcmFrameCodec.ParseSessionId(sessionId);
        lock (_gate)
        {
            if (_sessionId is null)
            {
                _sessionId = sessionId;
            }
            else if (_sessionId != sessionId)
            {
                _last.Clear();
                _sessionId = sessionId;
            }
            if (!_last.TryGetValue(frame.Track, out var previous))
            {
                if (frame.Sequence != 0)
                {
                    throw Invalid();
                }
            }
            else if (
                previous.Sequence == uint.MaxValue
                || frame.Sequence != previous.Sequence + 1
                || frame.TimestampMicroseconds <= previous.Timestamp
            )
            {
                throw Invalid();
            }
            _last[frame.Track] = (
                frame.Sequence,
                frame.TimestampMicroseconds);
        }
    }

    private static DirectProtocolException Invalid() =>
        new(
            "BW_COMPUTER_VOICE_DIRECT_PCM_SEQUENCE_INVALID",
            "PCM 帧序列或时间戳无效");
}

internal sealed class DirectUplinkSequenceGuard
{
    private readonly object _gate = new();
    private string? _sessionId;
    private uint _lastSequence;
    private ulong _lastTimestamp;
    private bool _hasFrame;

    internal void Begin(string sessionId)
    {
        _ = DirectPcmFrameCodec.ParseSessionId(sessionId);
        lock (_gate)
        {
            if (_sessionId is not null)
            {
                throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_DIRECT_UPLINK_GUARD_ACTIVE");
            }
            _sessionId = sessionId;
            _lastSequence = 0;
            _lastTimestamp = 0;
            _hasFrame = false;
        }
    }

    internal void Validate(
        string activeSessionId,
        DirectDecodedPcmFrame decoded)
    {
        lock (_gate)
        {
            if (
                _sessionId is null
                || _sessionId != activeSessionId
                || decoded.SessionId != activeSessionId
                || decoded.Frame.Track
                    != DirectPcmTrack.BrowserMicrophone
            )
            {
                throw InvalidSession();
            }
            if (!_hasFrame)
            {
                if (decoded.Frame.Sequence != 0)
                {
                    throw InvalidSequence();
                }
            }
            else if (
                _lastSequence == uint.MaxValue
                || decoded.Frame.Sequence != _lastSequence + 1
                || decoded.Frame.TimestampMicroseconds <= _lastTimestamp
            )
            {
                throw InvalidSequence();
            }
            _lastSequence = decoded.Frame.Sequence;
            _lastTimestamp = decoded.Frame.TimestampMicroseconds;
            _hasFrame = true;
        }
    }

    internal void End()
    {
        lock (_gate)
        {
            _sessionId = null;
            _lastSequence = 0;
            _lastTimestamp = 0;
            _hasFrame = false;
        }
    }

    private static DirectProtocolException InvalidSession() =>
        new(
            "BW_COMPUTER_VOICE_DIRECT_SESSION_MISMATCH",
            "浏览器麦克风 binary 会话不匹配");

    private static DirectProtocolException InvalidSequence() =>
        new(
            "BW_COMPUTER_VOICE_DIRECT_UPLINK_SEQUENCE_INVALID",
            "浏览器麦克风序列或时间戳无效");
}
