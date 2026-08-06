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
    private readonly DirectConnectionOwnership _connectionOwnership = new();
    private readonly SemaphoreSlim _startPromotionGate = new(1, 1);
    private readonly SemaphoreSlim _disposeGate = new(1, 1);
    private readonly string _serviceInstanceId;
    private readonly DirectRuntimeStatusWriter _statusWriter;
    private readonly DirectServiceLease _serviceLease;
    private readonly DirectSnapshotViewer _snapshotViewer;
    private readonly ReaderDocumentCorpusStore _documentCorpus;
    private readonly ReaderContextSourceRouter _readerSourceRouter = new();
    private readonly ReaderVisualDeliveryBroker _readerVisualBroker;
    private readonly NamedPipeReaderVisualRpcServer _readerVisualRpcServer;
    private readonly ReaderBrowserControlBroker _readerBrowserControlBroker;
    private readonly NamedPipeReaderBrowserControlRpcServer
        _readerBrowserControlRpcServer;
    private readonly object _runtimeStateGate = new();
    private string _runtimeState = "starting";
    private bool _runtimeReaderConnected;
    private bool _runtimeCaptureActive;
    private bool _disposed;
    private bool _disposeCompleted;
    private readonly IDirectSnapshotContextAdapter? _snapshotContextAdapter;

    internal DirectBridgeServer(
        DirectBridgeConfigStore configStore,
        IDirectAppLauncher appLauncher,
        IDirectMediaAdapter mediaAdapter,
        IDirectContextAdapter? contextAdapter = null,
        IDirectSnapshotContextAdapter? snapshotContextAdapter = null)
    {
        _configStore = configStore;
        _snapshotContextAdapter = snapshotContextAdapter;
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
        _snapshotViewer = new DirectSnapshotViewer(
            Path.Combine(
                configStore.InstallationRoot,
                "runtime",
                FileDirectSnapshotContextAdapter.SnapshotFileName),
            config.ListenPort);
        string runtimeDirectory = Path.GetDirectoryName(
            config.RuntimeStatusPath)
            ?? Path.Combine(configStore.InstallationRoot, "runtime");
        _documentCorpus = new ReaderDocumentCorpusStore(
            Path.Combine(
                runtimeDirectory,
                ReaderDocumentCorpusStore.CorpusFileName));
        _readerVisualBroker = new ReaderVisualDeliveryBroker(
            _readerSourceRouter);
        _readerVisualRpcServer = new NamedPipeReaderVisualRpcServer(
            _readerVisualBroker);
        _readerBrowserControlBroker = new ReaderBrowserControlBroker(
            _readerSourceRouter);
        _readerBrowserControlRpcServer =
            new NamedPipeReaderBrowserControlRpcServer(
                _readerBrowserControlBroker);
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
            // Web page corpus POSTs are bounded and validated separately.
            // WebSocket text frames retain the stricter 64 KiB contract in
            // ReceiveMessageAsync; raising the HTTP envelope does not widen it.
            // One request can carry the current viewport plus a one-time full
            // document corpus.  The document store still validates every
            // field and caps text at 256 Ki characters.
            options.Limits.MaxRequestBodySize = 2 * 1024 * 1024;
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
        app.MapGet(
            DirectSnapshotViewer.ViewerPath,
            _snapshotViewer.HandleViewerAsync);
        app.MapGet(
            DirectSnapshotViewer.SnapshotPath,
            _snapshotViewer.HandleSnapshotAsync);
        app.MapGet(
            DirectSnapshotViewer.MarkdownPath,
            _snapshotViewer.HandleMarkdownAsync);
        app.Map(
            "/reader-computer-voice/v1",
            context => HandleBridgeAsync(context, cancellationToken));
        app.Map(
            "/reader-context/v1",
            context => HandleContextBridgeAsync(context, cancellationToken));
        // One-shot snapshot delivery.
        //
        // A page's context is used once and discarded -- collect, send, done --
        // yet it was travelling over the voice link's machinery: a WebSocket,
        // a handshake, a session, reconnect backoff. All of that exists to keep
        // a conversation alive, and none of it applies here.
        //
        // The cost was not complexity but reach. A socket must be held by a
        // document that stays alive, and on iOS every extension document is
        // short-lived: the background worker is reclaimed, the popup dies when
        // dismissed, an embedded frame dies with its page. A POST needs none of
        // that. The worker can be woken, post once, and be reclaimed again.
        // POST and its preflight both, on the same path.
        //
        // A JSON body makes this a non-simple request, so a browser sends
        // OPTIONS first and refuses to send the real one until that is
        // answered. Nothing here answered it, so the request never left Safari
        // -- reported as "Load failed", indistinguishable from the host being
        // unreachable. It also explains why this endpoint tested clean from
        // Python: a script issues no preflight, only browsers do.
        app.MapMethods(
            "/reader-context/snapshot",
            new[] { "POST", "OPTIONS" },
            context => HandleSnapshotPostAsync(context, cancellationToken));
        app.MapFallback(context =>
        {
            context.Response.StatusCode = StatusCodes.Status404NotFound;
            return Task.CompletedTask;
        });

        CancellationTokenSource? heartbeatLifetime = null;
        Task? heartbeatTask = null;
        CancellationTokenSource? visualRpcLifetime = null;
        Task? visualRpcTask = null;
        CancellationTokenSource? browserControlRpcLifetime = null;
        Task? browserControlRpcTask = null;
        try
        {
            await app.StartAsync(cancellationToken).ConfigureAwait(false);
            visualRpcLifetime =
                CancellationTokenSource.CreateLinkedTokenSource(
                    cancellationToken);
            visualRpcTask = _readerVisualRpcServer.RunAsync(
                visualRpcLifetime.Token);
            browserControlRpcLifetime =
                CancellationTokenSource.CreateLinkedTokenSource(
                    cancellationToken);
            browserControlRpcTask = _readerBrowserControlRpcServer.RunAsync(
                browserControlRpcLifetime.Token);
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
            if (browserControlRpcLifetime is not null)
            {
                browserControlRpcLifetime.Cancel();
            }
            if (browserControlRpcTask is not null)
            {
                try
                {
                    await browserControlRpcTask.ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                }
            }
            browserControlRpcLifetime?.Dispose();
            if (visualRpcLifetime is not null)
            {
                visualRpcLifetime.Cancel();
            }
            if (visualRpcTask is not null)
            {
                try
                {
                    await visualRpcTask.ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                }
            }
            visualRpcLifetime?.Dispose();
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
            readerContextMcp = new
            {
                path = ReaderContextMcpHttpEndpoint.Path,
                instanceId = _readerContextMcpEndpoint.InstanceId,
            },
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

        string connectionId = "connection-"
            + DirectBase64Url.Encode(
                System.Security.Cryptography.RandomNumberGenerator
                    .GetBytes(12));
        DirectConnectionLease? connection = null;
        try
        {
            using WebSocket socket = await context.WebSockets
                .AcceptWebSocketAsync().ConfigureAwait(false);
            connection =
                await _connectionOwnership.CreateAsync(
                    connectionId,
                    serviceCancellationToken,
                    context.RequestAborted).ConfigureAwait(false);
            CancellationToken connectionToken = connection.Token;
            if (!await _connectionOwnership.TryAttachAsync(
                connection,
                socket,
                connectionToken).ConfigureAwait(false))
            {
                socket.Abort();
                return;
            }
            connectionToken.ThrowIfCancellationRequested();
            DirectConnectionPhaseDeadline phaseDeadline = new();
            DirectSecurityLog.Write(
                _serviceInstanceId,
                "reader-connect",
                "BW_COMPUTER_VOICE_DIRECT_READER_CONNECTED",
                ok: true);
            await RunConnectionAsync(
                socket,
                connection,
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
            if (connection is not null)
            {
                try
                {
                    await _coordinator.StopForConnectionAsync(connectionId)
                        .ConfigureAwait(false);
                }
                finally
                {
                    _snapshotViewer.CloseForConnection(connectionId);
                }
                bool cleanupPending = _coordinator.CleanupPending;
                await _connectionOwnership.CompleteAsync(
                    connection,
                    () => WriteRuntimeStatusAsync(
                        cleanupPending ? "faulted" : "idle",
                        readerConnected: false,
                        captureActive: false,
                        CancellationToken.None)).ConfigureAwait(false);
            }
        }
    }

    // Accepts a whole snapshot in one request: page context, active reading, or
    // both. Same origin rule as the socket endpoints -- this opens no new door.
    private async Task HandleSnapshotPostAsync(
        HttpContext context,
        CancellationToken serviceCancellationToken)
    {
        bool originOk = OriginAllowed(
            context,
            requireOrigin: true,
            out string origin);
        // Granted only to origins that already passed the allow-list, so this
        // widens nothing: a refused origin still gets no header and its
        // preflight fails, exactly as before.
        if (originOk)
        {
            context.Response.Headers["Access-Control-Allow-Origin"] = origin;
            context.Response.Headers["Vary"] = "Origin";
        }
        if (HttpMethods.IsOptions(context.Request.Method))
        {
            AppendSnapshotPostLog(
                origin,
                originOk ? "preflight-ok" : "preflight-refused",
                null);
            if (!originOk)
            {
                context.Response.StatusCode =
                    StatusCodes.Status403Forbidden;
                return;
            }
            context.Response.Headers["Access-Control-Allow-Methods"] =
                "POST, OPTIONS";
            context.Response.Headers["Access-Control-Allow-Headers"] =
                "Content-Type";
            context.Response.Headers["Access-Control-Max-Age"] = "600";
            context.Response.StatusCode = StatusCodes.Status204NoContent;
            return;
        }
        // Every attempt is recorded, accepted or not.
        //
        // "Nothing arrived" and "something arrived and was refused" look
        // identical from the other end of the network, and telling them apart
        // has cost several rounds tonight: one means the extension never sent,
        // the other means it sent and this side said no. The line below answers
        // that in one glance.
        AppendSnapshotPostLog(
            origin,
            originOk ? "origin-ok" : "origin-refused",
            context.Request.ContentLength);
        if (!originOk)
        {
            context.Response.StatusCode = StatusCodes.Status403Forbidden;
            return;
        }
        IDirectSnapshotContextAdapter? adapter = _snapshotContextAdapter;
        if (adapter is null)
        {
            context.Response.StatusCode =
                StatusCodes.Status503ServiceUnavailable;
            return;
        }

        // Synthesised per request. Sessions exist to tie a socket's messages
        // together; a lone POST has nothing to tie.
        string sessionId = "session-" + DirectBase64Url.Encode(
            System.Security.Cryptography.RandomNumberGenerator.GetBytes(16));
        string requestId = "post-" + DirectBase64Url.Encode(
            System.Security.Cryptography.RandomNumberGenerator.GetBytes(9));

        try
        {
            using JsonDocument document = await JsonDocument
                .ParseAsync(
                    context.Request.Body,
                    cancellationToken: serviceCancellationToken)
                .ConfigureAwait(false);
            JsonElement root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
            {
                context.Response.StatusCode =
                    StatusCodes.Status400BadRequest;
                return;
            }
            DirectJsonValidation.RequireNoDuplicateKeys(root);
            HashSet<string> fields = root.EnumerateObject()
                .Select(property => property.Name)
                .ToHashSet(StringComparer.Ordinal);
            if (
                fields.Count == 0
                || fields.Any(field => field is not (
                    "event" or "active" or "viewport" or "document"))
            )
            {
                throw new DirectProtocolException(
                    "BW_READER_CONTEXT_POST_SCHEMA_INVALID",
                    "Reader 快照 POST 字段无效",
                    retryable: false);
            }

            DirectContextEvent? contextEvent = null;
            DirectActiveReading? activeReading = null;
            DirectViewportContext? viewport = null;
            JsonElement? documentValue = null;
            if (
                root.TryGetProperty("event", out JsonElement eventValue)
                && eventValue.ValueKind == JsonValueKind.Object
            )
            {
                contextEvent =
                    NamedPipeDirectContextAdapter.ValidateEvent(
                        eventValue);
            }
            else if (fields.Contains("event"))
            {
                throw new DirectProtocolException(
                    "BW_READER_CONTEXT_POST_SCHEMA_INVALID",
                    "Reader 快照 event 无效",
                    retryable: false);
            }
            if (
                root.TryGetProperty("active", out JsonElement activeValue)
                && activeValue.ValueKind == JsonValueKind.Object
            )
            {
                activeReading =
                    FileDirectSnapshotContextAdapter.ValidateActiveReading(
                        activeValue);
            }
            else if (fields.Contains("active"))
            {
                throw new DirectProtocolException(
                    "BW_READER_CONTEXT_POST_SCHEMA_INVALID",
                    "Reader 快照 active 无效",
                    retryable: false);
            }
            if (
                root.TryGetProperty("viewport", out JsonElement viewportValue)
                && viewportValue.ValueKind == JsonValueKind.Object
            )
            {
                viewport = FileDirectSnapshotContextAdapter.ValidateViewport(
                    viewportValue);
            }
            else if (fields.Contains("viewport"))
            {
                throw new DirectProtocolException(
                    "BW_READER_CONTEXT_POST_SCHEMA_INVALID",
                    "Reader 快照 viewport 无效",
                    retryable: false);
            }
            if (
                root.TryGetProperty("document", out JsonElement corpusValue)
                && corpusValue.ValueKind == JsonValueKind.Object
            )
            {
                _ = ReaderDocumentCorpusStore.Validate(corpusValue);
                documentValue = corpusValue;
            }
            else if (fields.Contains("document"))
            {
                throw new DirectProtocolException(
                    "BW_READER_CONTEXT_POST_SCHEMA_INVALID",
                    "Reader 快照 document 无效",
                    retryable: false);
            }
            if (
                viewport is not null
                && activeReading is null
            )
            {
                throw new DirectProtocolException(
                    "BW_READER_CONTEXT_POST_SCHEMA_INVALID",
                    "Reader 当前视口必须与 active-reading 同时提交",
                    retryable: false);
            }
            if (
                viewport is not null
                && activeReading is not null
                && !string.Equals(
                    viewport.SourceInstanceId,
                    activeReading.SourceInstanceId,
                    StringComparison.Ordinal)
            )
            {
                throw new DirectProtocolException(
                    "BW_READER_CONTEXT_POST_IDENTITY_MISMATCH",
                    "Reader viewport 与 active 来源不一致",
                    retryable: false);
            }
            if (
                documentValue is JsonElement corpus
                && viewport is not null
            )
            {
                ReaderDocumentCorpusEntry documentEntry =
                    ReaderDocumentCorpusStore.Validate(corpus);
                if (
                    !string.Equals(
                        documentEntry.SourceInstanceId,
                        viewport.SourceInstanceId,
                        StringComparison.Ordinal)
                    || !string.Equals(
                        documentEntry.DocumentKey,
                        viewport.DocumentKey,
                        StringComparison.Ordinal)
                )
                {
                    throw new DirectProtocolException(
                        "BW_READER_CONTEXT_POST_IDENTITY_MISMATCH",
                        "Reader 全文与当前视口身份不一致",
                        retryable: false);
                }
            }

            int applied = 0;
            if (contextEvent is not null)
            {
                await adapter.ForwardJournalAsync(
                    requestId,
                    sessionId,
                    contextEvent,
                    serviceCancellationToken).ConfigureAwait(false);
                applied += 1;
            }
            if (activeReading is not null)
            {
                await adapter.ForwardActiveReadingAsync(
                    requestId,
                    sessionId,
                    activeReading,
                    serviceCancellationToken).ConfigureAwait(false);
                applied += 1;
            }
            if (viewport is not null)
            {
                await adapter.ForwardViewportAsync(
                    requestId,
                    sessionId,
                    viewport,
                    serviceCancellationToken).ConfigureAwait(false);
                applied += 1;
            }
            if (documentValue is JsonElement documentEntryValue)
            {
                await _documentCorpus.SaveAsync(
                    documentEntryValue,
                    serviceCancellationToken).ConfigureAwait(false);
                applied += 1;
            }
            if (applied == 0)
            {
                context.Response.StatusCode =
                    StatusCodes.Status400BadRequest;
                return;
            }
            context.Response.StatusCode = StatusCodes.Status204NoContent;
        }
        catch (DirectProtocolException failure)
        {
            // The reason travels back in the body. A caller that cannot see why
            // it was rejected has to guess, and guessing across a network
            // boundary is what made this link so hard to diagnose.
            context.Response.StatusCode = StatusCodes.Status400BadRequest;
            context.Response.ContentType = "application/json; charset=utf-8";
            await context.Response.WriteAsync(
                JsonSerializer.Serialize(new
                {
                    ok = false,
                    code = failure.Code,
                    message = failure.Message,
                }),
                serviceCancellationToken).ConfigureAwait(false);
        }
        catch (JsonException)
        {
            context.Response.StatusCode = StatusCodes.Status400BadRequest;
        }
    }

    // Best-effort by design: a diagnostic must never break what it observes.
    private void AppendSnapshotPostLog(
        string origin,
        string outcome,
        long? contentLength)
    {
        try
        {
            DirectBridgeConfig config = _configStore.Load();
            string? directory = System.IO.Path.GetDirectoryName(
                config.RuntimeStatusPath);
            if (string.IsNullOrEmpty(directory))
            {
                return;
            }
            string path = System.IO.Path.Combine(
                directory,
                "reader-context-post.log");
            string line = string.Format(
                System.Globalization.CultureInfo.InvariantCulture,
                "{0:O}	{1}	{2}	{3}",
                DateTimeOffset.Now,
                outcome,
                string.IsNullOrEmpty(origin) ? "(no-origin)" : origin,
                contentLength?.ToString(
                    System.Globalization.CultureInfo.InvariantCulture)
                    ?? "-");
            System.IO.File.AppendAllText(
                path,
                line + Environment.NewLine,
                new System.Text.UTF8Encoding(false));
            var info = new System.IO.FileInfo(path);
            if (info.Length > 128 * 1024)
            {
                string[] all = System.IO.File.ReadAllLines(path);
                System.IO.File.WriteAllLines(
                    path,
                    all.Skip(Math.Max(0, all.Length - 200)));
            }
        }
        catch
        {
        }
    }

    private async Task HandleContextBridgeAsync(
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
        if (!OriginAllowed(context, requireOrigin: true, out string origin))
        {
            context.Response.StatusCode = StatusCodes.Status403Forbidden;
            return;
        }

        string connectionId = "context-"
            + DirectBase64Url.Encode(
                System.Security.Cryptography.RandomNumberGenerator
                    .GetBytes(12));
        using WebSocket socket = await context.WebSockets
            .AcceptWebSocketAsync().ConfigureAwait(false);
        using CancellationTokenSource lifetime =
            CancellationTokenSource.CreateLinkedTokenSource(
                serviceCancellationToken,
                context.RequestAborted);
        using SemaphoreSlim sendGate = new(1, 1);
        ReaderContextSourceLease? sourceLease = null;
        DirectBridgeProtocolSession protocol = new(
            connectionId,
            origin,
            _configStore,
            _coordinator,
            registerReaderSource: sourceInstanceId =>
            {
                sourceLease = _readerSourceRouter.Attach(
                    sourceInstanceId,
                    connectionId,
                    (message, cancellationToken) => SendJsonAsync(
                        socket,
                        sendGate,
                        message,
                        cancellationToken));
                return sourceLease;
            },
            acceptReaderVisual: chunk =>
            {
                ReaderContextSourceLease lease = sourceLease
                    ?? throw new DirectProtocolException(
                        "BW_READER_VISUAL_SOURCE_NOT_REGISTERED",
                        "Reader 视觉来源尚未注册",
                        retryable: true);
                return _readerVisualBroker.Accept(lease, chunk);
            },
            acceptReaderBrowserControl: response =>
            {
                ReaderContextSourceLease lease = sourceLease
                    ?? throw new DirectProtocolException(
                        "BW_READER_BROWSER_CONTROL_SOURCE_NOT_REGISTERED",
                        "Reader 浏览控制来源尚未注册",
                        retryable: true);
                _readerBrowserControlBroker.Accept(lease, response);
            });

        try
        {
            while (
                !lifetime.IsCancellationRequested
                && socket.State == WebSocketState.Open
            )
            {
                DirectClientMessage? incoming = await ReceiveMessageAsync(
                    socket,
                    lifetime.Token).ConfigureAwait(false);
                if (incoming is null)
                {
                    break;
                }
                if (incoming.MessageType != WebSocketMessageType.Text)
                {
                    await socket.CloseAsync(
                        WebSocketCloseStatus.InvalidMessageType,
                        "context endpoint accepts text only",
                        CancellationToken.None).ConfigureAwait(false);
                    break;
                }
                string message = incoming.Text
                    ?? throw new DirectProtocolException(
                        "BW_COMPUTER_VOICE_DIRECT_MESSAGE_INVALID",
                        "上下文消息无效");
                if (!IsContextEndpointActionAllowed(message))
                {
                    await SendJsonAsync(
                        socket,
                        sendGate,
                        new
                        {
                            contract = DirectBridgeContract.Contract,
                            ok = false,
                            code = "BW_READER_CONTEXT_ACTION_INVALID",
                            message = "上下文端点不接受音频或控制操作",
                            retryable = false,
                        },
                        lifetime.Token).ConfigureAwait(false);
                    continue;
                }
                DirectProtocolReply reply = await protocol.HandleAsync(
                    message,
                    (_, _) => Task.CompletedTask,
                    (_, _, _) => Task.FromException(
                        new DirectProtocolException(
                            "BW_READER_CONTEXT_PCM_FORBIDDEN",
                            "上下文端点不发送音频")),
                    lifetime.Token).ConfigureAwait(false);
                await SendJsonAsync(
                    socket,
                    sendGate,
                    reply.Envelope,
                    lifetime.Token).ConfigureAwait(false);
                if (reply.AfterSendAsync is not null)
                {
                    await reply.AfterSendAsync(lifetime.Token)
                        .ConfigureAwait(false);
                }
            }
        }
        catch (OperationCanceledException)
        {
        }
        catch (WebSocketException)
        {
        }
        finally
        {
            if (sourceLease is not null)
            {
                _readerSourceRouter.Detach(sourceLease);
            }
            socket.Abort();
        }
    }

    internal static bool IsContextEndpointActionAllowed(string json)
    {
        try
        {
            using JsonDocument document = JsonDocument.Parse(json);
            if (
                document.RootElement.ValueKind != JsonValueKind.Object
                || !document.RootElement.TryGetProperty(
                    "type",
                    out JsonElement typeElement)
                || typeElement.ValueKind != JsonValueKind.String
            )
            {
                return false;
            }
            return typeElement.GetString() is
                "hello" or
                "context-mode" or
                "context-open" or
                "context" or
                "active-reading" or
                "context-clear" or
                "log" or
                ReaderVisualDeliveryProtocol.RegisterType or
                ReaderVisualDeliveryProtocol.ChunkType or
                ReaderBrowserControlProtocol.ResponseType;
        }
        catch (JsonException)
        {
            return false;
        }
    }

    private async Task RunConnectionAsync(
        WebSocket socket,
        DirectConnectionLease connection,
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
            _coordinator,
            acknowledgeReaderResult:
                ack => _readerResultBroker.Acknowledge(
                    connectionId,
                    ack),
            acceptReaderVisual:
                chunk => _readerVisualBroker.Accept(
                    connectionId,
                    chunk));
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
            bool ownsAudioTransport =
                protocol.Phase == DirectProtocolPhase.Active
                && await _connectionOwnership.IsCurrentAsync(
                    connection,
                    cancellationToken).ConfigureAwait(false);
            if (ownsAudioTransport && ShouldMonitorMedia(_coordinator))
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
                    connection,
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
                    connection,
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
                    connection,
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
                        connection,
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
                        // Only START invokes this callback, and coordinator
                        // serializes START before the connection is promoted
                        // to owner.  STATUS/HELLO never write global runtime
                        // state and never acquire ownership.
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
                async Task<DirectProtocolReply> StartAndPromoteAsync(
                    CancellationToken startCancellationToken)
                {
                    await _startPromotionGate.WaitAsync(
                        startCancellationToken).ConfigureAwait(false);
                    try
                    {
                        DirectProtocolPhase startPhase = protocol.Phase;
                        DirectProtocolReply startReply =
                            await HandleMessageAsync(startCancellationToken)
                                .ConfigureAwait(false);
                        if (
                            startPhase != DirectProtocolPhase.Active
                            && protocol.Phase
                                == DirectProtocolPhase.Active
                        )
                        {
                            DirectConnectionLease? previous =
                                await _connectionOwnership.PromoteAsync(
                                    connection,
                                    startCancellationToken)
                                    .ConfigureAwait(false);
                            if (previous is not null)
                            {
                                try
                                {
                                    await previous.Released.WaitAsync(
                                        TimeSpan.FromSeconds(2),
                                        startCancellationToken)
                                        .ConfigureAwait(false);
                                }
                                catch (TimeoutException)
                                {
                                    // The old transport is already revoked;
                                    // its slow HTTP unwind cannot hold the
                                    // START/promotion transaction forever.
                                }
                            }
                        }
                        return startReply;
                    }
                    finally
                    {
                        _startPromotionGate.Release();
                    }
                }

                DirectPeerMonitorOutcome<DirectProtocolReply> monitored =
                    await MonitorStartForPeerCloseAsync(
                        socket,
                        StartAndPromoteAsync,
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
                    connection,
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
                    connection,
                    connectionId,
                    socket,
                    sendGate,
                    DirectConnectionPhaseDeadline.TimeoutFailure(
                        protocol.Phase),
                    "connection-phase-timeout").ConfigureAwait(false);
                break;
            }
            bool becameActive =
                phaseBeforeMessage != DirectProtocolPhase.Active
                && protocol.Phase == DirectProtocolPhase.Active;
            bool stoppedBeingActive =
                phaseBeforeMessage == DirectProtocolPhase.Active
                && protocol.Phase != DirectProtocolPhase.Active;
            bool failedAuthenticatedStartWithoutOwner =
                IsStartRequest(message)
                && protocol.IsAuthenticated
                && phaseBeforeMessage != DirectProtocolPhase.Active
                && protocol.Phase != DirectProtocolPhase.Active
                && _coordinator.ActiveSessionId is null
                && _coordinator.LastError is not null;
            if (failedAuthenticatedStartWithoutOwner)
            {
                await WriteRuntimeStatusAsync(
                    "faulted",
                    readerConnected: false,
                    captureActive: false,
                    cancellationToken).ConfigureAwait(false);
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
                if (
                    phaseBeforeMessage
                        == DirectProtocolPhase.AwaitingAuthentication
                    && protocol.IsAuthenticated
                )
                {
                    _readerResultBroker.Attach(
                        connectionId,
                        (envelope, token) => SendJsonAsync(
                            socket,
                            sendGate,
                            envelope,
                            token));
                    _readerVisualBroker.Attach(
                        connectionId,
                        (envelope, token) => SendJsonAsync(
                            socket,
                            sendGate,
                            envelope,
                            token));
                }
                if (reply.AfterSendAsync is not null)
                {
                    await reply.AfterSendAsync(replyLifetime.Token)
                        .ConfigureAwait(false);
                }
                if (
                    becameActive
                )
                {
                    uplinkSequenceGuard.Begin(
                        _coordinator.ActiveSessionId
                        ?? throw new DirectProtocolException(
                            "BW_COMPUTER_VOICE_DIRECT_UPLINK_NOT_ACTIVE",
                            "浏览器麦克风上行尚未启动"));
                    _snapshotViewer.OpenIfSnapshotMode(
                        _configStore.Load().ContextDeliveryMode,
                        connectionId);
                }
                else if (
                    stoppedBeingActive
                )
                {
                    uplinkSequenceGuard.End();
                    _snapshotViewer.CloseForConnection(connectionId);
                    await _connectionOwnership.ReleaseAsync(
                        connection,
                        () => WriteRuntimeStatusAsync(
                            "idle",
                            readerConnected: false,
                            captureActive: false,
                            CancellationToken.None)).ConfigureAwait(false);
                }
            }
            catch (OperationCanceledException)
                when (
                    replyLifetime.IsCancellationRequested
                    && !cancellationToken.IsCancellationRequested
                )
            {
                await HandleConnectionFailureAsync(
                    connection,
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
                        connection,
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
                    connection,
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
                _ = await WriteConnectionRuntimeStatusAsync(
                    connection,
                    "active",
                    readerConnected: true,
                    captureActive: true,
                    cancellationToken).ConfigureAwait(false);
            }
            else
            {
                _ = await WriteConnectionRuntimeStatusAsync(
                    connection,
                    _coordinator.CleanupPending
                        ? "faulted"
                        : "reader-connected",
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
        DirectConnectionLease connection,
        string connectionId,
        WebSocket socket,
        SemaphoreSlim sendGate,
        DirectProtocolException failure,
        string logEvent)
    {
        bool stoppedCurrentOwner =
            await _coordinator.FailAndStopForConnectionAsync(
                connectionId,
                failure,
                logEvent).ConfigureAwait(false);
        DirectSecurityLog.Write(
            _serviceInstanceId,
            logEvent,
            failure.Code,
            ok: false);
        if (stoppedCurrentOwner)
        {
            _ = await WriteConnectionRuntimeStatusAsync(
                connection,
                "faulted",
                readerConnected: true,
                captureActive: false,
                CancellationToken.None).ConfigureAwait(false);
        }
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

    private Task<bool> WriteConnectionRuntimeStatusAsync(
        DirectConnectionLease connection,
        string state,
        bool readerConnected,
        bool captureActive,
        CancellationToken cancellationToken) =>
        _connectionOwnership.RunIfCurrentAsync(
            connection,
            () => WriteRuntimeStatusAsync(
                state,
                readerConnected,
                captureActive,
                cancellationToken),
            cancellationToken);

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
        await RunRuntimeStatusHeartbeatLoopAsync(
            token => timer.WaitForNextTickAsync(token),
            async token =>
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
                    token).ConfigureAwait(false);
            },
            () =>
            {
                try
                {
                    DirectSecurityLog.Write(
                        _serviceInstanceId,
                        "runtime-status-heartbeat-retry",
                        "BW_COMPUTER_VOICE_DIRECT_STATUS_WRITE_RETRY",
                        ok: false);
                }
                catch
                {
                    // Heartbeat recovery cannot depend on stderr being open.
                }
            },
            cancellationToken).ConfigureAwait(false);
    }

    internal static async Task RunRuntimeStatusHeartbeatLoopAsync(
        Func<CancellationToken, ValueTask<bool>> waitNextTickAsync,
        Func<CancellationToken, Task> writeStatusAsync,
        Action recoverableWriteFailure,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(waitNextTickAsync);
        ArgumentNullException.ThrowIfNull(writeStatusAsync);
        ArgumentNullException.ThrowIfNull(recoverableWriteFailure);
        while (await waitNextTickAsync(cancellationToken)
            .ConfigureAwait(false))
        {
            try
            {
                await writeStatusAsync(cancellationToken)
                    .ConfigureAwait(false);
            }
            catch (Exception exception)
                when (exception is IOException
                    or UnauthorizedAccessException)
            {
                recoverableWriteFailure();
            }
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
            try
            {
                await DisposeCoordinatorWithBoundedRetryAsync()
                    .ConfigureAwait(false);
            }
            finally
            {
                _snapshotViewer.Dispose();
            }
            await _connectionOwnership.DisposeAsync()
                .ConfigureAwait(false);
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

internal sealed class DirectConnectionLease
{
    private readonly CancellationTokenSource _lifetime;
    private readonly CancellationToken _token;
    private readonly TaskCompletionSource<bool> _released = new(
        TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly object _socketGate = new();
    private WebSocket? _socket;
    private bool _retired;
    private int _completed;

    internal DirectConnectionLease(
        long generation,
        string connectionId,
        CancellationTokenSource lifetime)
    {
        Generation = generation;
        ConnectionId = connectionId;
        _lifetime = lifetime;
        _token = lifetime.Token;
    }

    internal long Generation { get; }

    internal string ConnectionId { get; }

    internal CancellationToken Token => _token;

    internal Task Released => _released.Task;

    internal bool IsCompleted =>
        Volatile.Read(ref _completed) != 0;

    internal bool TryAttach(WebSocket socket)
    {
        lock (_socketGate)
        {
            if (_retired)
            {
                return false;
            }
            if (_socket is not null)
            {
                return ReferenceEquals(_socket, socket);
            }
            _socket = socket;
            return true;
        }
    }

    internal void Retire()
    {
        WebSocket? socket;
        lock (_socketGate)
        {
            if (_retired)
            {
                return;
            }
            _retired = true;
            socket = _socket;
            _socket = null;
        }
        try
        {
            _lifetime.Cancel();
        }
        catch
        {
            // Cancellation callbacks belong to the retiring connection and
            // cannot be allowed to reject the authenticated replacement.
        }
        try
        {
            socket?.Abort();
        }
        catch
        {
            // The peer may have closed concurrently.  Ownership is already
            // revoked even when the transport has nothing left to abort.
        }
    }

    internal void Complete()
    {
        if (Interlocked.Exchange(ref _completed, 1) != 0)
        {
            return;
        }
        Retire();
        _released.TrySetResult(true);
        _lifetime.Dispose();
    }
}

internal sealed class DirectConnectionOwnership : IAsyncDisposable
{
    private readonly SemaphoreSlim _gate = new(1, 1);
    private DirectConnectionLease? _current;
    private long _generation;
    private bool _disposed;

    internal async Task<DirectConnectionLease> CreateAsync(
        string connectionId,
        CancellationToken serviceCancellationToken,
        CancellationToken requestCancellationToken)
    {
        CancellationTokenSource lifetime =
            CancellationTokenSource.CreateLinkedTokenSource(
                serviceCancellationToken,
                requestCancellationToken);
        DirectConnectionLease? connection = null;
        try
        {
            await _gate.WaitAsync(lifetime.Token).ConfigureAwait(false);
            try
            {
                ObjectDisposedException.ThrowIf(_disposed, this);
                lifetime.Token.ThrowIfCancellationRequested();
                connection = new DirectConnectionLease(
                    checked(++_generation),
                    connectionId,
                    lifetime);
            }
            finally
            {
                _gate.Release();
            }
        }
        catch
        {
            lifetime.Dispose();
            throw;
        }
        return connection!;
    }

    internal async Task<bool> TryAttachAsync(
        DirectConnectionLease connection,
        WebSocket socket,
        CancellationToken cancellationToken)
    {
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            ObjectDisposedException.ThrowIf(_disposed, this);
            return !connection.IsCompleted
                && connection.TryAttach(socket);
        }
        finally
        {
            _gate.Release();
        }
    }

    internal async Task<DirectConnectionLease?> PromoteAsync(
        DirectConnectionLease connection,
        CancellationToken cancellationToken)
    {
        DirectConnectionLease? previous;
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            ObjectDisposedException.ThrowIf(_disposed, this);
            if (connection.IsCompleted)
            {
                throw new OperationCanceledException(
                    "语音连接已经结束",
                    connection.Token);
            }
            if (ReferenceEquals(_current, connection))
            {
                return null;
            }
            previous = _current;
            _current = connection;
        }
        finally
        {
            _gate.Release();
        }
        previous?.Retire();
        return previous;
    }

    internal async Task<bool> ReleaseAsync(
        DirectConnectionLease connection,
        Func<Task> onCurrent)
    {
        await _gate.WaitAsync().ConfigureAwait(false);
        try
        {
            if (!ReferenceEquals(_current, connection))
            {
                return false;
            }
            try
            {
                await onCurrent().ConfigureAwait(false);
            }
            finally
            {
                _current = null;
            }
            return true;
        }
        finally
        {
            _gate.Release();
        }
    }

    internal async Task<bool> IsCurrentAsync(
        DirectConnectionLease connection,
        CancellationToken cancellationToken)
    {
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            return ReferenceEquals(_current, connection);
        }
        finally
        {
            _gate.Release();
        }
    }

    internal async Task<bool> RunIfCurrentAsync(
        DirectConnectionLease connection,
        Func<Task> action,
        CancellationToken cancellationToken)
    {
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            if (!ReferenceEquals(_current, connection))
            {
                return false;
            }
            await action().ConfigureAwait(false);
            return true;
        }
        finally
        {
            _gate.Release();
        }
    }

    internal async Task CompleteAsync(
        DirectConnectionLease connection,
        Func<Task> onCurrent)
    {
        if (connection.IsCompleted)
        {
            return;
        }
        try
        {
            await _gate.WaitAsync().ConfigureAwait(false);
            try
            {
                if (ReferenceEquals(_current, connection))
                {
                    try
                    {
                        await onCurrent().ConfigureAwait(false);
                    }
                    finally
                    {
                        _current = null;
                    }
                }
            }
            finally
            {
                _gate.Release();
            }
        }
        finally
        {
            connection.Complete();
        }
    }

    public async ValueTask DisposeAsync()
    {
        DirectConnectionLease? current;
        await _gate.WaitAsync().ConfigureAwait(false);
        try
        {
            if (_disposed)
            {
                return;
            }
            _disposed = true;
            current = _current;
            _current = null;
        }
        finally
        {
            _gate.Release();
        }
        current?.Complete();
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
