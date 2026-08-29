using System.Buffers;
using System.Globalization;
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
    private static readonly TimeSpan HostShutdownTimeout =
        TimeSpan.FromSeconds(10);
    private static readonly TimeSpan OwnedCleanupTimeout =
        TimeSpan.FromSeconds(30);
    internal static readonly TimeSpan GracefulShutdownMaximumWait =
        HostShutdownTimeout + OwnedCleanupTimeout;
    internal static readonly TimeSpan DisconnectCleanupWatchdogDelay =
        TimeSpan.FromSeconds(30);
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
    private readonly DirectContextConnectionHealth _contextConnectionHealth =
        new();
    private readonly SemaphoreSlim _startPromotionGate = new(1, 1);
    private readonly SemaphoreSlim _disposeGate = new(1, 1);
    private readonly string _serviceInstanceId;
    private readonly DirectRuntimeStatusWriter _statusWriter;
    private readonly DirectServiceLease _serviceLease;
    private readonly DirectSnapshotViewer _snapshotViewer;
    private readonly bool _bridgeOnlyMode;
    private readonly bool _voiceEnabled;
    private readonly string _runtimeDirectory;

    // 桥接模式旗标走独立意图文件:keepalive/direct-config/runtime-status 三者都是
    // exact 合同(四处副本校验键集),加键任何一个都会被判无效或服务离线。
    internal const string ServiceModeContract = "readerpc-service-mode/1";
    internal const string ShutdownRequestContract =
        "readerpc-direct-shutdown/1";
    internal const string ShutdownRequestFileName =
        "readerpc-direct-shutdown.json";
    internal const string ShutdownReceiptContract =
        "readerpc-direct-shutdown-result/1";
    internal const string ShutdownReceiptFileName =
        "readerpc-direct-shutdown-result.json";
    internal static readonly TimeSpan ShutdownRequestPollInterval =
        TimeSpan.FromMilliseconds(100);

    internal static bool TryConsumeShutdownRequest(
        string runtimeDirectory,
        string serviceInstanceId)
    {
        try
        {
            string path = Path.Combine(
                runtimeDirectory,
                ShutdownRequestFileName);
            if (!File.Exists(path)
                || !DirectBridgeContract.IsServiceInstanceId(
                    serviceInstanceId))
            {
                return false;
            }
            using JsonDocument document = JsonDocument.Parse(
                File.ReadAllText(path));
            JsonElement root = document.RootElement;
            HashSet<string> keys = root.ValueKind == JsonValueKind.Object
                ? root.EnumerateObject()
                    .Select(property => property.Name)
                    .ToHashSet(StringComparer.Ordinal)
                : [];
            if (
                !keys.SetEquals(new[]
                {
                    "contract",
                    "serviceInstanceId",
                })
                || root.GetProperty("contract").ValueKind
                    != JsonValueKind.String
                || root.GetProperty("contract").GetString()
                    != ShutdownRequestContract
                || root.GetProperty("serviceInstanceId").ValueKind
                    != JsonValueKind.String
                || root.GetProperty("serviceInstanceId").GetString()
                    != serviceInstanceId
            )
            {
                return false;
            }
            File.Delete(path);
            return true;
        }
        catch (Exception exception) when (
            exception is IOException
            or UnauthorizedAccessException
            or JsonException)
        {
            return false;
        }
    }

    internal static void WriteShutdownReceipt(
        string runtimeDirectory,
        string serviceInstanceId,
        string state,
        string? code = null)
    {
        if (!DirectBridgeContract.IsServiceInstanceId(serviceInstanceId))
        {
            throw new ArgumentException(
                "shutdown receipt requires a service instance id",
                nameof(serviceInstanceId));
        }
        object payload = state switch
        {
            "accepted" when code is null => new
            {
                contract = ShutdownReceiptContract,
                serviceInstanceId,
                state,
                maximumWaitMs = checked((int)
                    GracefulShutdownMaximumWait.TotalMilliseconds),
            },
            "success" when code is null => new
            {
                contract = ShutdownReceiptContract,
                serviceInstanceId,
                state,
            },
            "failed" when IsSafeShutdownCode(code) => new
            {
                contract = ShutdownReceiptContract,
                serviceInstanceId,
                state,
                code,
            },
            _ => throw new ArgumentOutOfRangeException(
                nameof(state),
                "shutdown receipt state is invalid"),
        };
        Directory.CreateDirectory(runtimeDirectory);
        string path = Path.Combine(
            runtimeDirectory,
            ShutdownReceiptFileName);
        string temporary = path
            + ".tmp-"
            + Environment.ProcessId.ToString(CultureInfo.InvariantCulture)
            + "-"
            + Guid.NewGuid().ToString("N");
        try
        {
            File.WriteAllText(
                temporary,
                JsonSerializer.Serialize(payload),
                new UTF8Encoding(false));
            File.Move(temporary, path, overwrite: true);
        }
        finally
        {
            try
            {
                File.Delete(temporary);
            }
            catch (IOException)
            {
            }
            catch (UnauthorizedAccessException)
            {
            }
        }
    }

    private static bool IsSafeShutdownCode(string? code) =>
        !string.IsNullOrEmpty(code)
        && code.Length <= 128
        && code.All(character =>
            character is >= 'A' and <= 'Z'
            or >= '0' and <= '9'
            or '_');

    private static string ShutdownFailureCode(Exception exception) =>
        exception is DirectProtocolException protocol
            && IsSafeShutdownCode(protocol.Code)
                ? protocol.Code
                : "BW_COMPUTER_VOICE_DIRECT_SHUTDOWN_CLEANUP_FAILED";

    internal static string ReadServiceMode(string runtimeDirectory)
    {
        try
        {
            string path = Path.Combine(
                runtimeDirectory,
                "readerpc-service-mode.json");
            if (!File.Exists(path))
            {
                return "full";
            }
            using JsonDocument document = JsonDocument.Parse(
                File.ReadAllText(path));
            JsonElement root = document.RootElement;
            JsonElement mode = default;
            bool valid = root.ValueKind == JsonValueKind.Object
                && root.TryGetProperty(
                    "contract",
                    out JsonElement contract)
                && contract.ValueKind == JsonValueKind.String
                && contract.GetString() == ServiceModeContract
                && root.TryGetProperty("mode", out mode)
                && mode.ValueKind == JsonValueKind.String;
            string? value = valid ? mode.GetString() : null;
            return value is "full" or "bridge-only" ? value : "full";
        }
        catch (Exception)
        {
            // 读不出 = 完整模式:失败回落到现状行为,而不是悄悄改变语音语义。
            return "full";
        }
    }

    internal static bool ReadBridgeOnlyMode(string runtimeDirectory) =>
        ReadServiceMode(runtimeDirectory) == "bridge-only";

    internal static bool ReadVoiceEnabled(string runtimeDirectory)
    {
        try
        {
            string path = Path.Combine(
                runtimeDirectory,
                "readerpc-service-mode.json");
            if (!File.Exists(path))
            {
                return true;
            }
            using JsonDocument document = JsonDocument.Parse(
                File.ReadAllText(path));
            JsonElement root = document.RootElement;
            if (
                root.ValueKind != JsonValueKind.Object
                || !root.TryGetProperty(
                    "contract",
                    out JsonElement contract)
                || contract.ValueKind != JsonValueKind.String
                || contract.GetString() != ServiceModeContract
            )
            {
                return true;
            }
            if (!root.TryGetProperty(
                "voiceEnabled",
                out JsonElement value))
            {
                // Backward compatibility: released intent files predate the
                // second axis and therefore mean voice-on.
                return true;
            }
            return value.ValueKind switch
            {
                JsonValueKind.False => false,
                JsonValueKind.True => true,
                _ => true,
            };
        }
        catch (Exception)
        {
            // 旧文件/损坏文件保持已发布语音默认开启语义。
            return true;
        }
    }

    internal static bool ReadSnapshotViewerHidden(string runtimeDirectory)
    {
        try
        {
            string path = Path.Combine(
                runtimeDirectory,
                "readerpc-service-mode.json");
            if (!File.Exists(path))
            {
                return false;
            }
            using JsonDocument document = JsonDocument.Parse(
                File.ReadAllText(path));
            JsonElement root = document.RootElement;
            return root.ValueKind == JsonValueKind.Object
                && root.TryGetProperty(
                    "contract",
                    out JsonElement contract)
                && contract.ValueKind == JsonValueKind.String
                && contract.GetString() == ServiceModeContract
                && root.TryGetProperty(
                    "snapshotViewer",
                    out JsonElement viewer)
                && viewer.ValueKind == JsonValueKind.String
                && viewer.GetString() == "hidden";
        }
        catch (Exception)
        {
            return false;   // 读不出 = 显示查看器(现状行为)
        }
    }

    internal static void WriteServiceModeIntent(
        string runtimeDirectory,
        string? mode = null,
        bool? voiceEnabled = null)
    {
        // 原子写(tmp+move):模式意图文件同时被 ReaderPC 收敛循环轮询,半写状态
        // 会被它当无效丢弃,但没必要制造这种窗口。App 可独立改 mode 或
        // voiceEnabled；缺失轴从现有意图读回。静默快照键(snapshotViewer)
        // 由 ReaderPC 界面所有,这里同样保留,不冲掉。
        string path = Path.Combine(
            runtimeDirectory,
            "readerpc-service-mode.json");
        string viewer = ReadSnapshotViewerHidden(runtimeDirectory)
            ? "hidden"
            : "visible";
        string resolvedMode = mode ?? ReadServiceMode(runtimeDirectory);
        if (resolvedMode is not ("full" or "bridge-only"))
        {
            throw new ArgumentOutOfRangeException(
                nameof(mode),
                "ReaderPC service mode must be full or bridge-only");
        }
        bool resolvedVoiceEnabled = voiceEnabled
            ?? ReadVoiceEnabled(runtimeDirectory);
        string payload = JsonSerializer.Serialize(new
        {
            contract = ServiceModeContract,
            mode = resolvedMode,
            voiceEnabled = resolvedVoiceEnabled,
            snapshotViewer = viewer,
        });
        string temporary = path + ".tmp-" + Environment.ProcessId;
        File.WriteAllText(
            temporary,
            payload,
            new UTF8Encoding(false));
        File.Move(temporary, path, overwrite: true);
    }
    private readonly ReaderDocumentCorpusStore _documentCorpus;
    private readonly ReaderContextSourceRouter _readerSourceRouter = new();
    private readonly ReaderVisualDeliveryBroker _readerVisualBroker;
    private readonly NamedPipeReaderVisualRpcServer _readerVisualRpcServer;
    private readonly ReaderBrowserControlBroker _readerBrowserControlBroker;
    private readonly NamedPipeReaderBrowserControlRpcServer
        _readerBrowserControlRpcServer;
    private readonly ReaderQueryBroker _readerQueryBroker;
    private readonly NamedPipeReaderQueryRpcServer _readerQueryRpcServer;
    private readonly ReaderRealtimeOutputBroker _readerRealtimeOutputBroker;
    private readonly ReplicationCommandSpool _replicationCommandSpool;
    private readonly string _replicationDigestsPath;
    private readonly ReaderHttpPickupService _readerHttpPickup;
    private readonly ReaderMapTiles _mapTiles = new();
    private readonly NamedPipeReaderRealtimeOutputRpcServer
        _readerRealtimeOutputRpcServer;
    private readonly IDirectCodexVoiceControl _codexVoiceControl;
    private readonly CodexCliReaderDictionaryFallback _dictionaryFallback;
    private readonly ReaderLocalAnkiRegistry _localAnkiRegistry;
    private readonly ReaderLocalAnkiWriter _localAnkiWriter;
    private readonly object _runtimeStateGate = new();
    private string _runtimeState = "starting";
    private bool _runtimeReaderConnected;
    private bool _runtimeCaptureActive;
    private int _gracefulShutdownRequested;
    private bool _disposed;
    private bool _disposeCompleted;
    private readonly IDirectSnapshotContextAdapter? _snapshotContextAdapter;

    // The Direct process is the non-voice ReaderPC foundation.  Snapshot,
    // query, visual delivery and MCP lifetime must never be coupled to Codex
    // Voice keepalive or health; there is no independent non-voice-off axis.
    private const bool SnapshotServiceRequested = true;

    internal DirectBridgeServer(
        DirectBridgeConfigStore configStore,
        IDirectAppLauncher appLauncher,
        IDirectMediaAdapter mediaAdapter,
        IDirectContextAdapter? contextAdapter = null,
        IDirectSnapshotContextAdapter? snapshotContextAdapter = null,
        IDirectCodexVoiceControl? codexVoiceControl = null,
        bool manageSnapshotViewerProcess = true)
    {
        _configStore = configStore;
        _snapshotContextAdapter = snapshotContextAdapter;
        DirectBridgeConfig config = configStore.Load();
        string runtimeDirectory = Path.GetDirectoryName(
            config.RuntimeStatusPath)
            ?? Path.Combine(configStore.InstallationRoot, "runtime");
        _dictionaryFallback = new CodexCliReaderDictionaryFallback();
        _localAnkiRegistry = new ReaderLocalAnkiRegistry(
            Path.Combine(
                runtimeDirectory,
                ReaderLocalAnkiRegistry.RegistryFileName));
        _localAnkiWriter = new ReaderLocalAnkiWriter(
            _localAnkiRegistry);
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
        // 静默快照(2026-08-17 用户需求):意图文件带 snapshotViewer:"hidden" 时
        // 不管理查看器窗口——快照服务本体照跑,只是不开展示窗。唯一的打开入口
        // SynchronizeServiceIntent 守 _manageViewerProcess,无旁路。
        _snapshotViewer = new DirectSnapshotViewer(
            Path.Combine(
                configStore.InstallationRoot,
                "runtime",
                FileDirectSnapshotContextAdapter.SnapshotFileName),
            config.ListenPort,
            manageSnapshotViewerProcess
                && !ReadSnapshotViewerHidden(runtimeDirectory));
        _runtimeDirectory = runtimeDirectory;
        // 提示板从这里读已有的 notifications-*.json —— 它不存数据，
        // 只是把本来就在实时更新的东西挑一挑再渲出来。
        ReaderAttentionBoard.Configure(runtimeDirectory);
        _bridgeOnlyMode = ReadBridgeOnlyMode(runtimeDirectory);
        _voiceEnabled = ReadVoiceEnabled(runtimeDirectory);
        // 桥接模式语义(2026-08-17 用户更正):语音**留在电脑**——Codex 照常自动
        // 拉起、F24 保活照常(keepalive 链完整装载),音频走电脑自己的设备;唯一
        // 被拒的是 App 发起的 START(那才是把音频路由到虚拟设备、PCM 隧道到
        // App 的动作)。"不接管"指不接走音频,不是不管语音。
        _codexVoiceControl = !_voiceEnabled
            ? new DirectDisabledCodexVoiceControl()
            : codexVoiceControl
            ?? DirectCodexVoiceControl.CreateProduction(
                Path.Combine(
                    runtimeDirectory,
                    "codex-voice-keepalive.json"),
                appLauncher,
                keepActiveChanged: enabled =>
                    _snapshotViewer.SynchronizeServiceIntent(
                        _configStore.Load().ContextDeliveryMode,
                        enabled),
                automaticRecoveryFailed: exception =>
                    _ = _coordinator.RecordFailure(
                        exception,
                        "codex-voice-keepalive"),
                automaticRecoverySucceeded:
                    _coordinator.ClearRecoveryFailure);
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
        _readerQueryBroker = new ReaderQueryBroker(_readerSourceRouter);
        _readerQueryRpcServer = new NamedPipeReaderQueryRpcServer(
            _readerQueryBroker);
        _readerRealtimeOutputBroker = new ReaderRealtimeOutputBroker(
            _readerSourceRouter,
            Path.Combine(
                runtimeDirectory,
                ReaderRealtimeOutputOutbox.FileName));
        _readerRealtimeOutputRpcServer =
            new NamedPipeReaderRealtimeOutputRpcServer(
                _readerRealtimeOutputBroker);
        _readerHttpPickup = new ReaderHttpPickupService(
            _readerSourceRouter);
        _replicationCommandSpool = new ReplicationCommandSpool(
            Path.Combine(
                runtimeDirectory,
                ReplicationCommandSpool.DirectoryName));
        _replicationDigestsPath = Path.Combine(
            runtimeDirectory,
            ReplicationCommandProtocol.DigestsFileName);
    }

    internal async Task<int> RunAsync(CancellationToken cancellationToken)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        using CancellationTokenSource serviceLifetime =
            CancellationTokenSource.CreateLinkedTokenSource(
                cancellationToken);
        CancellationToken serviceToken = serviceLifetime.Token;
        DirectBridgeConfig config = _configStore.Load();
        await WriteRuntimeStatusAsync(
            "starting",
            readerConnected: false,
            captureActive: false,
            serviceToken).ConfigureAwait(false);

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
            options.ShutdownTimeout = HostShutdownTimeout;
        });

        await using WebApplication app = builder.Build();
        app.UseWebSockets(new WebSocketOptions
        {
            KeepAliveInterval = TimeSpan.FromSeconds(20),
        });
        app.MapGet(
            "/healthz",
            context => HandleHealthAsync(context, serviceToken));
        app.MapGet(
            DirectSnapshotViewer.ViewerPath,
            _snapshotViewer.HandleViewerAsync);
        app.MapGet(
            DirectSnapshotViewer.SnapshotPath,
            _snapshotViewer.HandleSnapshotAsync);
        app.MapGet(
            DirectSnapshotViewer.MarkdownPath,
            _snapshotViewer.HandleMarkdownAsync);
        app.MapGet(
            DirectSnapshotViewer.ActivityViewerPath,
            _snapshotViewer.HandleActivityViewerAsync);
        app.MapGet(
            DirectSnapshotViewer.ActivityReportPath,
            _snapshotViewer.HandleActivityReportAsync);
        // 摄像头（2026-08-27）。全部经 PrepareLocalResponse 的回环闸 ——
        // 家里的实时画面只在这台机器上打得开,不经 Tailscale、不经网页。
        app.MapGet(
            DirectSnapshotViewer.CameraListPath,
            _snapshotViewer.HandleCameraListAsync);
        app.MapGet(
            DirectSnapshotViewer.CameraFramePath,
            _snapshotViewer.HandleCameraFrameAsync);
        app.MapMethods(
            DirectSnapshotViewer.CameraSnapPath,
            new[] { "POST" },
            _snapshotViewer.HandleCameraSnapAsync);
        app.MapMethods(
            DirectSnapshotViewer.CameraLabelPath,
            new[] { "POST" },
            _snapshotViewer.HandleCameraLabelAsync);
        app.Map(
            "/reader-computer-voice/v1",
            context => HandleBridgeAsync(context, serviceToken));
        app.Map(
            "/reader-context/v1",
            context => HandleContextBridgeAsync(context, serviceToken));
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
        // iOS 小组件的只读数据端点（2026-08-26 用户拍板：小组件更新不能
        // 依赖 App 开启）。widget extension 的 timeline provider 每 15 分钟
        // 直接经 Tailscale 拉这里 —— 与快照/取件同一个 HTTPS 面。非浏览器
        // 客户端，无 CORS；身份闸 = Tailscale serve 注入的登录头。
        app.MapMethods(
            "/widget/system-data",
            new[] { "GET" },
            context => HandleWidgetSystemDataAsync(context, serviceToken));
        // 地图瓦片代取：密钥与会话都留在本机，设备端 URL 不带任何凭据。
        app.MapMethods(
            "/map/tile",
            new[] { "GET" },
            context => HandleMapTileAsync(context, serviceToken));
        app.MapMethods(
            "/reader-output/pending",
            new[] { "POST", "OPTIONS" },
            context => HandleOutputPendingAsync(context, serviceToken));
        app.MapMethods(
            "/reader-output/receipt",
            new[] { "POST", "OPTIONS" },
            context => HandleOutputReceiptAsync(context, serviceToken));
        app.MapMethods(
            "/reader-context/snapshot",
            new[] { "POST", "OPTIONS" },
            context => HandleSnapshotPostAsync(context, serviceToken));
        // 书库：设备把书传到这台服务器,也从这里取回。
        // ⚠ 它服务的是用户 2026-08-28 拍板的那条规矩 ——「本地的书必须先上传
        // 服务器才能开始使用」,所以它的可靠性直接决定书能不能打开。
        app.MapMethods(
            "/reader-library/upload",
            new[] { "POST", "OPTIONS" },
            context => HandleLibraryUploadAsync(context, serviceToken));
        app.MapMethods(
            "/reader-library/list",
            new[] { "POST", "OPTIONS" },
            context => HandleLibraryListAsync(context, serviceToken));
        // 语音助手的主动提示板。它盯着 BoardPath 一个地方就够。
        // ⚠ 板子**不存数据** —— 只是把 runtime 目录里已有的
        // notifications-*.json 和位置挑一挑、渲成一份很小的文本。
        // 曾经还有一条 /notify 投递口，2026-08-29 拆掉了：那是重复造，
        // Windows 侧本来就在实时更新这些文件。
        app.MapMethods(
            ReaderAttentionBoard.BoardPath,
            new[] { "GET", "OPTIONS" },
            context => HandleAttentionBoardAsync(context, serviceToken));
        app.MapMethods(
            ReaderAttentionBoard.AckPath,
            new[] { "GET", "POST", "OPTIONS" },
            context => HandleAttentionAckAsync(context, serviceToken));
        // VoIP 来电的设备 token。App 每次拿到都会上报（token 会因重装、
        // 恢复备份、系统更新而变）。⚠ 没有它就永远打不进来，而且失败
        // 完全静默：推送方推了、APNs 收下了、设备上什么也没发生。
        app.MapMethods(
            "/reader-voip/token",
            new[] { "POST", "OPTIONS" },
            context => HandleVoipTokenAsync(context, serviceToken));
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
        CancellationTokenSource? realtimeOutputRpcLifetime = null;
        Task? realtimeOutputRpcTask = null;
        CancellationTokenSource? queryRpcLifetime = null;
        Task? queryRpcTask = null;
        CancellationTokenSource? shutdownRequestLifetime = null;
        Task? shutdownRequestTask = null;
        try
        {
            await app.StartAsync(serviceToken).ConfigureAwait(false);
            shutdownRequestLifetime =
                CancellationTokenSource.CreateLinkedTokenSource(
                    cancellationToken);
            shutdownRequestTask = MonitorShutdownRequestAsync(
                () =>
                {
                    serviceLifetime.Cancel();
                    app.Lifetime.StopApplication();
                },
                shutdownRequestLifetime.Token);
            visualRpcLifetime =
                CancellationTokenSource.CreateLinkedTokenSource(
                    serviceToken);
            visualRpcTask = _readerVisualRpcServer.RunAsync(
                visualRpcLifetime.Token);
            browserControlRpcLifetime =
                CancellationTokenSource.CreateLinkedTokenSource(
                    serviceToken);
            browserControlRpcTask = _readerBrowserControlRpcServer.RunAsync(
                browserControlRpcLifetime.Token);
            queryRpcLifetime =
                CancellationTokenSource.CreateLinkedTokenSource(
                    serviceToken);
            queryRpcTask = _readerQueryRpcServer.RunAsync(
                queryRpcLifetime.Token);
            realtimeOutputRpcLifetime =
                CancellationTokenSource.CreateLinkedTokenSource(
                    serviceToken);
            realtimeOutputRpcTask = _readerRealtimeOutputRpcServer.RunAsync(
                realtimeOutputRpcLifetime.Token);
            await _serviceLease.WriteAsync(serviceToken)
                .ConfigureAwait(false);
            await WriteRuntimeStatusAsync(
                "idle",
                readerConnected: false,
                captureActive: false,
                serviceToken).ConfigureAwait(false);
            _snapshotViewer.SynchronizeServiceIntent(
                config.ContextDeliveryMode,
                SnapshotServiceRequested);
            heartbeatLifetime =
                CancellationTokenSource.CreateLinkedTokenSource(
                    serviceToken);
            heartbeatTask = HeartbeatAsync(heartbeatLifetime.Token);
            DirectSecurityLog.Write(
                _serviceInstanceId,
                "service-start",
                "BW_COMPUTER_VOICE_DIRECT_SERVICE_STARTED",
                ok: true);
            await app.WaitForShutdownAsync(serviceToken)
                .ConfigureAwait(false);
            return 0;
        }
        catch (OperationCanceledException)
            when (serviceToken.IsCancellationRequested)
        {
            return 0;
        }
        finally
        {
            if (shutdownRequestLifetime is not null)
            {
                shutdownRequestLifetime.Cancel();
            }
            if (shutdownRequestTask is not null)
            {
                try
                {
                    await shutdownRequestTask.ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                }
            }
            shutdownRequestLifetime?.Dispose();
            if (queryRpcLifetime is not null)
            {
                queryRpcLifetime.Cancel();
            }
            if (queryRpcTask is not null)
            {
                try
                {
                    await queryRpcTask.ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                }
            }
            queryRpcLifetime?.Dispose();
            if (realtimeOutputRpcLifetime is not null)
            {
                realtimeOutputRpcLifetime.Cancel();
            }
            if (realtimeOutputRpcTask is not null)
            {
                try
                {
                    await realtimeOutputRpcTask.ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                }
            }
            realtimeOutputRpcLifetime?.Dispose();
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

    private async Task MonitorShutdownRequestAsync(
        Action stopApplication,
        CancellationToken cancellationToken)
    {
        if (TryConsumeShutdownRequest(
            _runtimeDirectory,
            _serviceInstanceId))
        {
            AcceptGracefulShutdown(stopApplication);
            return;
        }
        using PeriodicTimer timer = new(ShutdownRequestPollInterval);
        while (await timer.WaitForNextTickAsync(cancellationToken)
            .ConfigureAwait(false))
        {
            if (!TryConsumeShutdownRequest(
                _runtimeDirectory,
                _serviceInstanceId))
            {
                continue;
            }
            AcceptGracefulShutdown(stopApplication);
            return;
        }
    }

    private void AcceptGracefulShutdown(Action stopApplication)
    {
        Volatile.Write(ref _gracefulShutdownRequested, 1);
        try
        {
            WriteShutdownReceipt(
                _runtimeDirectory,
                _serviceInstanceId,
                "accepted");
        }
        catch (Exception exception)
        {
            DirectSecurityLog.Write(
                _serviceInstanceId,
                "service-stop",
                ShutdownFailureCode(exception),
                ok: false);
        }
        finally
        {
            // Cancel the service-owned token before asking Kestrel to stop.
            // Long-lived WSS/RPC work then leaves immediately instead of
            // consuming the whole host drain window ahead of media cleanup.
            stopApplication();
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
        DirectContextConnectionHealthSnapshot contextHealth =
            _contextConnectionHealth.Snapshot();
        await context.Response.WriteAsJsonAsync(new
        {
            contract = "reader-computer-voice-direct-health/1",
            ok = true,
            serviceInstanceId = _serviceInstanceId,
            state = _coordinator.CaptureActive ? "active" : "idle",
            captureActive = _coordinator.CaptureActive,
            contextConnected = contextHealth.ConnectionCount > 0,
            contextConnectionCount = contextHealth.ConnectionCount,
            contextLastSeenAtUtc = contextHealth.LastSeenAtUtc,
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
                bool stoppedOwnedConnection =
                    await _coordinator.StopForConnectionAsync(connectionId)
                        .ConfigureAwait(false);
                bool cleanupPending = _coordinator.CleanupPending;
                await CompleteConnectionAndArmDisconnectWatchdogAsync(
                    () => _connectionOwnership.CompleteAsync(
                        connection,
                        () => WriteRuntimeStatusAsync(
                            cleanupPending ? "faulted" : "idle",
                            readerConnected: false,
                            captureActive: false,
                            CancellationToken.None)),
                    stoppedOwnedConnection && cleanupPending,
                    () =>
                    {
                        // A remote transport can vanish before STOP reaches
                        // us. Immediate teardown above remains the fast path;
                        // this is one delayed retry only, never a polling loop.
                        // The exact connection id remains the generation fence,
                        // so a later START makes this retired watchdog a no-op.
                        _ = ObserveDisconnectCleanupWatchdogAsync(
                            connectionId,
                            serviceCancellationToken);
                    }).ConfigureAwait(false);
            }
        }
    }

    internal static async Task
        CompleteConnectionAndArmDisconnectWatchdogAsync(
        Func<Task> completeConnectionAsync,
        bool cleanupWatchdogRequired,
        Action armCleanupWatchdog)
    {
        ArgumentNullException.ThrowIfNull(completeConnectionAsync);
        ArgumentNullException.ThrowIfNull(armCleanupWatchdog);
        try
        {
            await completeConnectionAsync().ConfigureAwait(false);
        }
        finally
        {
            // Runtime status publication is diagnostic, while releasing an
            // owned per-app audio-route lease is correctness. A transient
            // status-file failure must therefore never suppress the sole
            // delayed cleanup retry.
            if (cleanupWatchdogRequired)
            {
                armCleanupWatchdog();
            }
        }
    }

    private async Task ObserveDisconnectCleanupWatchdogAsync(
        string connectionId,
        CancellationToken serviceCancellationToken)
    {
        try
        {
            bool cleanupSettled = false;
            bool retried = await RunDisconnectCleanupWatchdogAsync(
                _coordinator,
                connectionId,
                DisconnectCleanupWatchdogDelay,
                async () =>
                {
                    cleanupSettled = true;
                    await WriteRuntimeStatusAsync(
                        "idle",
                        readerConnected: false,
                        captureActive: false,
                        CancellationToken.None).ConfigureAwait(false);
                },
                serviceCancellationToken).ConfigureAwait(false);
            if (retried)
            {
                DirectSecurityLog.Write(
                    _serviceInstanceId,
                    "disconnect-cleanup-watchdog",
                    cleanupSettled
                        ? "BW_COMPUTER_VOICE_DIRECT_DISCONNECT_CLEANUP_RESTORED"
                        : "BW_COMPUTER_VOICE_DIRECT_MEDIA_CLEANUP_PENDING",
                    ok: cleanupSettled);
            }
        }
        catch (OperationCanceledException)
            when (serviceCancellationToken.IsCancellationRequested)
        {
        }
        catch (Exception exception)
        {
            // This task has no awaiting peer.  Observe every failure so it can
            // never become an unobserved task exception, but do not replace the
            // runtime's existing media failure or cleanup ownership state.
            string code = exception is DirectProtocolException protocol
                ? protocol.Code
                : "BW_COMPUTER_VOICE_DIRECT_DISCONNECT_CLEANUP_FAILED";
            DirectSecurityLog.Write(
                _serviceInstanceId,
                "disconnect-cleanup-watchdog",
                code,
                ok: false);
        }
    }

    internal static async Task<bool> RunDisconnectCleanupWatchdogAsync(
        DirectBridgeCoordinator coordinator,
        string connectionId,
        TimeSpan delay,
        Func<Task>? onCleanupSettled,
        CancellationToken cancellationToken,
        Func<TimeSpan, CancellationToken, Task>? delayAsync = null)
    {
        ArgumentNullException.ThrowIfNull(coordinator);
        if (delay < TimeSpan.Zero)
        {
            throw new ArgumentOutOfRangeException(nameof(delay));
        }
        await (delayAsync ?? Task.Delay)(delay, cancellationToken)
            .ConfigureAwait(false);
        return await coordinator.StopForConnectionAsync(
            connectionId,
            onCleanupSettled).ConfigureAwait(false);
    }

    // Accepts a whole snapshot in one request: page context, active reading, or
    // both. Same origin rule as the socket endpoints -- this opens no new door.
    // 网页表面的实时输出下行（方案 A，2026-08-26）：长轮询取件 + 回执。
    // CORS/来源纪律与 snapshot POST 完全同款 —— 同一批扩展 origin。
    // 取件端点的到达日志（与 reader-context-post.log 同一课的产物：
    // "什么都没来"和"来了但被拒"在网络另一端看完全一样，必须能一眼分开。
    // 2026-08-26 排查网页送卡时正是因为缺这行日志，无法区分
    // "iPad 扩展没在轮询"和"轮询了但事件没匹配"）。
    private void AppendOutputPickupLog(string what)
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
                "reader-output-pickup.log");
            System.IO.File.AppendAllText(
                path,
                string.Format(
                    System.Globalization.CultureInfo.InvariantCulture,
                    "{0:O}	{1}",
                    DateTimeOffset.Now,
                    what) + Environment.NewLine,
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

    private bool PrepareOutputCors(HttpContext context, string methods)
    {
        bool originOk = OriginAllowed(
            context, requireOrigin: true, out string origin);
        if (originOk)
        {
            context.Response.Headers["Access-Control-Allow-Origin"] = origin;
            context.Response.Headers["Vary"] = "Origin";
        }
        if (!originOk && !HttpMethods.IsOptions(context.Request.Method))
        {
            AppendOutputPickupLog(
                "origin-refused	"
                + (string.IsNullOrEmpty(origin) ? "(no-origin)" : origin));
        }
        if (HttpMethods.IsOptions(context.Request.Method))
        {
            if (!originOk)
            {
                context.Response.StatusCode = StatusCodes.Status403Forbidden;
                return false;
            }
            context.Response.Headers["Access-Control-Allow-Methods"] = methods;
            context.Response.Headers["Access-Control-Allow-Headers"] =
                "Content-Type";
            context.Response.Headers["Access-Control-Max-Age"] = "600";
            context.Response.StatusCode = StatusCodes.Status204NoContent;
            return false;
        }
        if (!originOk)
        {
            context.Response.StatusCode = StatusCodes.Status403Forbidden;
            return false;
        }
        return true;
    }

    private async Task HandleMapTileAsync(
        HttpContext context,
        CancellationToken serviceCancellationToken)
    {
        if (!HttpMethods.IsGet(context.Request.Method))
        {
            context.Response.StatusCode =
                StatusCodes.Status405MethodNotAllowed;
            return;
        }
        DirectBridgeConfig config = _configStore.Load();
        if (!TailscaleLoginMatches(
            config,
            context.Request.Headers["Tailscale-User-Login"]))
        {
            context.Response.StatusCode = StatusCodes.Status403Forbidden;
            return;
        }
        // ⚠ 参数校验必须在这里判成 400,不能让它掉进下面的 503:503 是
        // "谷歌那边不可用"的信号,前端见了会**整体**退回 OSM。一个越界
        // 的瓦片请求就把整轮地图样式换掉,而且没人看得出为什么。
        if (!int.TryParse(context.Request.Query["z"], out int zoom)
            || !int.TryParse(context.Request.Query["x"], out int x)
            || !int.TryParse(context.Request.Query["y"], out int y)
            || zoom is < 0 or > 22
            || x < 0 || x >= (1 << zoom)
            || y < 0 || y >= (1 << zoom))
        {
            context.Response.StatusCode = StatusCodes.Status400BadRequest;
            return;
        }
        byte[]? tile = await _mapTiles
            .TileAsync(zoom, x, y, serviceCancellationToken)
            .ConfigureAwait(false);
        if (tile is null)
        {
            // 503 而不是空图片：前端据此**整体**退回 OpenStreetMap，
            // 而不是显示一片破图。失败原因写日志，否则"地图怎么变样了"
            // 无从查起。
            AppendOutputPickupLog(
                "map-tile-unavailable\t" + (_mapTiles.LastError ?? "unknown"));
            context.Response.StatusCode =
                StatusCodes.Status503ServiceUnavailable;
            return;
        }
        context.Response.ContentType = "image/png";
        // 瓦片是纯静态内容，值得长缓存（省配额也省流量）。
        context.Response.Headers["Cache-Control"] =
            "private, max-age=604800, immutable";
        await context.Response.Body
            .WriteAsync(tile, serviceCancellationToken)
            .ConfigureAwait(false);
    }

    private async Task HandleWidgetSystemDataAsync(
        HttpContext context,
        CancellationToken serviceCancellationToken)
    {
        if (!HttpMethods.IsGet(context.Request.Method))
        {
            context.Response.StatusCode =
                StatusCodes.Status405MethodNotAllowed;
            return;
        }
        DirectBridgeConfig config = _configStore.Load();
        if (!TailscaleLoginMatches(
            config,
            context.Request.Headers["Tailscale-User-Login"]))
        {
            // 拒绝也要出声:同款拒绝在 snapshot POST 那边一直有日志,
            // 这里没有 —— "widget 一直没数据"时会完全看不到它来过。
            AppendOutputPickupLog("widget-denied\tidentity");
            context.Response.StatusCode = StatusCodes.Status403Forbidden;
            return;
        }
        // widget 侧上报的上一轮排程结果。widget extension 里没有控制台,
        // 它排通知成没成在真机上等价于静默 —— 让它捎在拉取请求里回来。
        string? widgetSchedule = context.Request.Query["widgetSchedule"];
        if (!string.IsNullOrEmpty(widgetSchedule) && widgetSchedule.Length <= 40)
        {
            AppendOutputPickupLog("widget-fetch	" + widgetSchedule);
        }
        object view;
        try
        {
            view = ReplicationCommandProtocol.ReadNotificationsView(
                System.IO.Path.Combine(
                    System.IO.Path.GetDirectoryName(_replicationDigestsPath)!,
                    "notifications-user.json"));
        }
        catch (DirectProtocolException exception)
        {
            // 读不到就明说读不到。返回假的空列表会让 widget 把它当权威,
            // 撤销掉所有已排的到点通知(复核确认的最严重一条)。
            AppendOutputPickupLog("widget-error\t" + exception.Code);
            context.Response.StatusCode =
                StatusCodes.Status503ServiceUnavailable;
            await context.Response.WriteAsJsonAsync(
                new { error = exception.Code },
                serviceCancellationToken).ConfigureAwait(false);
            return;
        }
        catch (Exception exception)
        {
            // 兜底留痕：ASP.NET 把未处理异常变成一个**没有任何线索**的
            // 500，在真机上等价于静默。先把类型与消息写进日志再抛回去。
            AppendOutputPickupLog(
                "widget-crash\t" + exception.GetType().Name + ": "
                + exception.Message.Replace('\n', ' ').Replace('\t', ' '));
            throw;
        }
        context.Response.ContentType = "application/json; charset=utf-8";
        context.Response.Headers["Cache-Control"] = "no-store";
        try
        {
            await context.Response.WriteAsJsonAsync(
                view, serviceCancellationToken).ConfigureAwait(false);
        }
        catch (Exception exception)
        {
            AppendOutputPickupLog(
                "widget-serialize\t" + exception.GetType().Name + ": "
                + exception.Message.Replace('\n', ' ').Replace('\t', ' '));
            throw;
        }
    }

    private async Task HandleOutputPendingAsync(
        HttpContext context,
        CancellationToken serviceCancellationToken)
    {
        // ⚠ 轮询必须是 POST，这不是风格问题（2026-08-26 真机实锤）：
        // 浏览器对扩展特权 fetch 的 GET **不带 Origin 头**（POST 一律带，
        // 快照上行因此一直没事），白名单看不到来源只能拒。而反过来放宽
        // "无 Origin 的 GET"也不行——恶意网页可以发 no-cors GET（同样无
        // Origin、经 Tailscale serve 还自带合法身份头），响应虽读不到，
        // 队列却被无声排空。POST + JSON 必触发预检，预检必带 Origin。
        if (!PrepareOutputCors(context, "POST, OPTIONS"))
        {
            return;
        }
        if (!HttpMethods.IsPost(context.Request.Method))
        {
            context.Response.StatusCode =
                StatusCodes.Status405MethodNotAllowed;
            return;
        }
        string? source = null;
        int wait = 0;
        try
        {
            using JsonDocument pollBody = await JsonDocument.ParseAsync(
                context.Request.Body,
                cancellationToken: serviceCancellationToken)
                .ConfigureAwait(false);
            if (pollBody.RootElement.ValueKind == JsonValueKind.Object)
            {
                if (pollBody.RootElement.TryGetProperty(
                    "sourceInstanceId", out JsonElement sourceValue))
                {
                    source = sourceValue.GetString();
                }
                if (pollBody.RootElement.TryGetProperty(
                    "wait", out JsonElement waitValue)
                    && waitValue.TryGetInt32(out int parsed))
                {
                    wait = Math.Clamp(parsed, 0, 30);
                }
            }
        }
        catch (JsonException)
        {
            context.Response.StatusCode = StatusCodes.Status400BadRequest;
            return;
        }
        if (source is null || !DirectBridgeContract.IsSafeId(source))
        {
            context.Response.StatusCode = StatusCodes.Status400BadRequest;
            return;
        }
        using CancellationTokenSource linked =
            CancellationTokenSource.CreateLinkedTokenSource(
                serviceCancellationToken,
                context.RequestAborted);
        IReadOnlyList<object> events;
        try
        {
            events = await _readerHttpPickup.PollAsync(
                source, wait, linked.Token).ConfigureAwait(false);
        }
        catch (ReaderRealtimeOutputException exception)
        {
            context.Response.StatusCode = StatusCodes.Status429TooManyRequests;
            await context.Response.WriteAsJsonAsync(
                new { error = exception.Code },
                serviceCancellationToken).ConfigureAwait(false);
            return;
        }
        catch (OperationCanceledException)
        {
            return;
        }
        AppendOutputPickupLog(
            "pending	" + source + "	wait=" + wait
            + "	events=" + events.Count);
        context.Response.ContentType = "application/json; charset=utf-8";
        await context.Response.WriteAsJsonAsync(
            new { contract = "reader-output-pickup/1", events },
            serviceCancellationToken).ConfigureAwait(false);
    }

    private async Task HandleLibraryUploadAsync(
        HttpContext context,
        CancellationToken serviceCancellationToken)
    {
        // 与其它出站端点同一道闸:Origin 白名单 + Tailscale 注入的身份头。
        if (!PrepareOutputCors(context, "POST, OPTIONS")) return;
        if (!HttpMethods.IsPost(context.Request.Method))
        {
            context.Response.StatusCode = StatusCodes.Status405MethodNotAllowed;
            return;
        }
        // ⚠ 用框架自带的 multipart 解析,不手搓 —— 手搓 multipart 是 bug 的
        // 温床,而这条链路一旦出错的表现是「书打不开而且不知道为什么」。
        if (!context.Request.HasFormContentType)
        {
            context.Response.StatusCode = StatusCodes.Status400BadRequest;
            await context.Response.WriteAsJsonAsync(
                new
                {
                    ok = false,
                    code = "BW_LIBRARY_FORM_REQUIRED",
                    message = "需要 multipart/form-data(字段名 file)",
                },
                serviceCancellationToken).ConfigureAwait(false);
            return;
        }
        IFormCollection form;
        try
        {
            form = await context.Request
                .ReadFormAsync(serviceCancellationToken).ConfigureAwait(false);
        }
        catch (Exception error)
        {
            context.Response.StatusCode = StatusCodes.Status400BadRequest;
            await context.Response.WriteAsJsonAsync(
                new
                {
                    ok = false,
                    code = "BW_LIBRARY_FORM_INVALID",
                    // 原因原样透出:「表单坏了」和「书太大」该做的事完全不同。
                    message = "表单解析失败:" + error.GetType().Name,
                },
                serviceCancellationToken).ConfigureAwait(false);
            return;
        }
        IFormFile? file = form.Files["file"] ?? form.Files.FirstOrDefault();
        if (file is null)
        {
            context.Response.StatusCode = StatusCodes.Status400BadRequest;
            await context.Response.WriteAsJsonAsync(
                new
                {
                    ok = false,
                    code = "BW_LIBRARY_FILE_MISSING",
                    message = "表单里没有文件",
                },
                serviceCancellationToken).ConfigureAwait(false);
            return;
        }
        string name = (form["name"].FirstOrDefault() ?? file.FileName ?? "")
            .Trim();
        await using Stream source = file.OpenReadStream();
        ReaderLibraryStore.SaveOutcome outcome = await ReaderLibraryStore
            .SaveAsync(name, source, serviceCancellationToken)
            .ConfigureAwait(false);
        AppendOutputPickupLog(
            "library-upload	" + name + "	" + outcome.Code);
        await ReaderLibraryStore
            .WriteOutcomeAsync(context, outcome, serviceCancellationToken)
            .ConfigureAwait(false);
    }

    private async Task HandleLibraryListAsync(
        HttpContext context,
        CancellationToken serviceCancellationToken)
    {
        if (!PrepareOutputCors(context, "POST, OPTIONS")) return;
        if (!HttpMethods.IsPost(context.Request.Method))
        {
            context.Response.StatusCode = StatusCodes.Status405MethodNotAllowed;
            return;
        }
        await ReaderLibraryStore
            .WriteListAsync(context, serviceCancellationToken)
            .ConfigureAwait(false);
    }

    private async Task HandleAttentionBoardAsync(
        HttpContext context,
        CancellationToken serviceCancellationToken)
    {
        if (!PrepareOutputCors(context, "GET, OPTIONS")) return;
        if (!HttpMethods.IsGet(context.Request.Method))
        {
            context.Response.StatusCode = StatusCodes.Status405MethodNotAllowed;
            return;
        }
        await ReaderAttentionBoard
            .WriteBoardAsync(context, serviceCancellationToken)
            .ConfigureAwait(false);
    }

    /// 收下 App 上报的 VoIP 推送 token，写进 runtime 目录给推送方读。
    ///
    /// ⚠ 每次上报都覆盖写，不做"没变就跳过"的优化：少存一次的代价是
    /// 电话永远打不进来，多存一次只是一次几百字节的写。
    private async Task HandleVoipTokenAsync(
        HttpContext context,
        CancellationToken serviceCancellationToken)
    {
        // ⚠ **不要求 Origin**，只查 Tailscale 身份 —— 跟小组件那条路一样。
        //
        // 2026-08-29 实测：小组件用同一个地址打到这台机器是通的，而它
        // 不发 Origin；我第一版用 PrepareOutputCors（要 Origin 且要在白名单
        // 里），App 发的 origin 又不在白名单 —— 结果 token 上不来，
        // 而且看不出是"发了被拒"还是"根本没发"。
        // 安全边界没有变松：Tailscale-User-Login 由 tailscale serve 注入，
        // 伪造不了；Kestrel 也只绑回环。
        if (HttpMethods.IsOptions(context.Request.Method))
        {
            context.Response.StatusCode = StatusCodes.Status204NoContent;
            return;
        }
        if (!HttpMethods.IsPost(context.Request.Method))
        {
            context.Response.StatusCode = StatusCodes.Status405MethodNotAllowed;
            return;
        }
        if (!TailscaleLoginMatches(
            _configStore.Load(),
            context.Request.Headers["Tailscale-User-Login"]))
        {
            // 拒绝也要出声 —— 否则"token 一直没上来"时完全看不到它来过。
            AppendOutputPickupLog("voip-token-denied\tidentity");
            context.Response.StatusCode = StatusCodes.Status403Forbidden;
            return;
        }
        string token;
        try
        {
            using JsonDocument body = await JsonDocument
                .ParseAsync(
                    context.Request.Body,
                    cancellationToken: serviceCancellationToken)
                .ConfigureAwait(false);
            token = body.RootElement.TryGetProperty("token", out JsonElement t)
                && t.ValueKind == JsonValueKind.String
                ? (t.GetString() ?? string.Empty)
                : string.Empty;
        }
        catch (JsonException)
        {
            context.Response.StatusCode = StatusCodes.Status400BadRequest;
            return;
        }
        // APNs 的 device token 是十六进制串。形状不对就拒 —— 存下一个
        // 坏 token 的表现是 APNs 回 BadDeviceToken，而那看起来像别的问题。
        if (token.Length is < 32 or > 200
            || !token.All(Uri.IsHexDigit))
        {
            context.Response.StatusCode = StatusCodes.Status400BadRequest;
            await context.Response.WriteAsJsonAsync(
                new { ok = false, code = "BW_VOIP_TOKEN_SHAPE" },
                serviceCancellationToken).ConfigureAwait(false);
            return;
        }
        string path = Path.Combine(_runtimeDirectory, "voip-token.json");
        string temporary = path + ".tmp-" + Environment.ProcessId;
        await File.WriteAllTextAsync(
            temporary,
            "{\"contract\":\"reader-voip-token/1\",\"token\":\""
                + token.ToLowerInvariant() + "\"}",
            serviceCancellationToken).ConfigureAwait(false);
        File.Move(temporary, path, overwrite: true);
        await context.Response.WriteAsJsonAsync(
            new { ok = true },
            serviceCancellationToken).ConfigureAwait(false);
    }

    private async Task HandleAttentionAckAsync(
        HttpContext context,
        CancellationToken serviceCancellationToken)
    {
        if (!PrepareOutputCors(context, "GET, POST, OPTIONS")) return;
        await ReaderAttentionBoard
            .WriteAckAsync(context, serviceCancellationToken)
            .ConfigureAwait(false);
    }

    private async Task HandleOutputReceiptAsync(
        HttpContext context,
        CancellationToken serviceCancellationToken)
    {
        if (!PrepareOutputCors(context, "POST, OPTIONS"))
        {
            return;
        }
        if (!HttpMethods.IsPost(context.Request.Method))
        {
            context.Response.StatusCode =
                StatusCodes.Status405MethodNotAllowed;
            return;
        }
        JsonDocument body;
        try
        {
            body = await JsonDocument.ParseAsync(
                context.Request.Body,
                cancellationToken: serviceCancellationToken)
                .ConfigureAwait(false);
        }
        catch (JsonException)
        {
            context.Response.StatusCode = StatusCodes.Status400BadRequest;
            return;
        }
        using (body)
        {
            try
            {
                ReaderRealtimeOutputAck ack =
                    ReaderHttpPickupService.ParseReceipt(body.RootElement);
                _readerHttpPickup.AcceptReceipt(
                    _readerRealtimeOutputBroker, ack);
                AppendOutputPickupLog(
                    "receipt	" + ack.SourceInstanceId
                    + "	" + ack.Correlation + "	" + ack.Outcome
                    + "	bind=" + (ack.BindOutcome ?? "-")
                    + (string.IsNullOrEmpty(ack.BindReason)
                        ? "" : "	why=" + ack.BindReason));
            }
            catch (ReaderRealtimeOutputException exception)
            {
                AppendOutputPickupLog("receipt-error	" + exception.Code);
                context.Response.StatusCode =
                    StatusCodes.Status409Conflict;
                await context.Response.WriteAsJsonAsync(
                    new { error = exception.Code, detail = exception.Message },
                    serviceCancellationToken).ConfigureAwait(false);
                return;
            }
        }
        context.Response.ContentType = "application/json; charset=utf-8";
        await context.Response.WriteAsJsonAsync(
            new { ok = true },
            serviceCancellationToken).ConfigureAwait(false);
    }

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
            context.Response.Headers["Access-Control-Expose-Headers"] =
                "X-BW-Snapshot-Revision";
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
            long? appliedRevision = null;
            if (contextEvent is not null)
            {
                DirectSnapshotForwardResult result =
                    await adapter.ForwardJournalAsync(
                    requestId,
                    sessionId,
                    contextEvent,
                    serviceCancellationToken).ConfigureAwait(false);
                appliedRevision = result.Revision;
                applied += 1;
            }
            if (activeReading is not null)
            {
                DirectSnapshotForwardResult result =
                    await adapter.ForwardActiveReadingAsync(
                    requestId,
                    sessionId,
                    activeReading,
                    serviceCancellationToken).ConfigureAwait(false);
                appliedRevision = result.Revision;
                applied += 1;
            }
            if (viewport is not null)
            {
                DirectSnapshotForwardResult result =
                    await adapter.ForwardViewportAsync(
                    requestId,
                    sessionId,
                    viewport,
                    serviceCancellationToken).ConfigureAwait(false);
                appliedRevision = result.Revision;
                applied += 1;
                // 提示板只要「人在哪儿」，**不要正文**。
                // DocumentKey 当身份（稳定），Title/Url 只作显示（会变）。
                // 是不是一次注意力转移由板子自己判（停留门槛 + 迟滞），
                // 这里不做判断 —— 每一帧都报，让判据只有一处。
                ReaderAttentionBoard.NoteLocation(
                    viewport.DocumentKey,
                    string.IsNullOrWhiteSpace(viewport.Title)
                        ? viewport.Url
                        : viewport.Title,
                    DateTimeOffset.UtcNow);
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
            if (appliedRevision is long revision)
            {
                context.Response.Headers["X-BW-Snapshot-Revision"] =
                    revision.ToString(CultureInfo.InvariantCulture);
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
            codexVoiceControl: _codexVoiceControl,
            bridgeOnlyMode: _bridgeOnlyMode,
            voiceEnabled: _voiceEnabled,
            writeServiceModeIntent: (mode, voiceEnabled) => WriteServiceModeIntent(
                _runtimeDirectory,
                mode,
                voiceEnabled),
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
            acceptReplicationCommand: (envelope, cancellationToken) =>
                _replicationCommandSpool.AcceptAsync(
                    envelope,
                    cancellationToken),
            queryReplicationDigests: replicationBookId =>
                ReplicationCommandProtocol.ReadDigestsView(
                    _replicationDigestsPath,
                    replicationBookId),
            queryReplicationNotifications: () =>
                ReplicationCommandProtocol.ReadNotificationsView(
                    System.IO.Path.Combine(
                        System.IO.Path.GetDirectoryName(
                            _replicationDigestsPath)!,
                        "notifications-user.json")),
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
            },
            acceptReaderQuery: response =>
            {
                ReaderContextSourceLease lease = sourceLease
                    ?? throw new DirectProtocolException(
                        "BW_READER_QUERY_SOURCE_NOT_REGISTERED",
                        "Reader 查询来源尚未注册",
                        retryable: true);
                _readerQueryBroker.Accept(lease, response);
            },
            acceptReaderRealtimeOutput: ack =>
            {
                ReaderContextSourceLease lease = sourceLease
                    ?? throw new DirectProtocolException(
                        "BW_READER_REALTIME_OUTPUT_SOURCE_NOT_REGISTERED",
                        "Reader 输出来源尚未注册",
                        retryable: true);
                _readerRealtimeOutputBroker.Accept(lease, ack);
            },
            contextDeliveryModeChanged: mode =>
                _snapshotViewer.SynchronizeServiceIntent(
                    mode,
                    SnapshotServiceRequested),
            dictionaryFallback: _dictionaryFallback,
            localAnkiWriter: _localAnkiWriter);

        _contextConnectionHealth.Connected();
        _snapshotViewer.SynchronizeServiceIntent(
            _configStore.Load().ContextDeliveryMode,
            SnapshotServiceRequested);
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
                _contextConnectionHealth.MessageSeen();
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
            _contextConnectionHealth.Disconnected();
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
                "service-mode-set" or
                "context-open" or
                "context" or
                "active-reading" or
                "context-clear" or
                "dictionary-lookup" or
                "anki-add-cards-local" or
                "anki-card-operation-local" or
                "log" or
                ReaderVisualDeliveryProtocol.RegisterType or
                ReaderVisualDeliveryProtocol.ChunkType or
                ReaderBrowserControlProtocol.ResponseType or
                ReaderQueryProtocol.ResponseType or
                ReaderRealtimeOutputProtocol.AckType or
                ReplicationCommandProtocol.CommandType or
                ReplicationCommandProtocol.ChunkType or
                ReplicationCommandProtocol.DigestQueryType or
                ReplicationCommandProtocol.NotificationsQueryType;
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
            codexVoiceControl: _codexVoiceControl,
            bridgeOnlyMode: _bridgeOnlyMode,
            voiceEnabled: _voiceEnabled,
            writeServiceModeIntent: (mode, voiceEnabled) => WriteServiceModeIntent(
                _runtimeDirectory,
                mode,
                voiceEnabled),
            contextDeliveryModeChanged: mode =>
                _snapshotViewer.SynchronizeServiceIntent(
                    mode,
                    SnapshotServiceRequested),
            dictionaryFallback: _dictionaryFallback,
            localAnkiWriter: _localAnkiWriter);
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
                if (reply.AfterSendAsync is not null)
                {
                    await reply.AfterSendAsync(replyLifetime.Token)
                        .ConfigureAwait(false);
                }
                if (becameActive)
                {
                    uplinkSequenceGuard.Begin(
                        _coordinator.ActiveSessionId
                            ?? throw new DirectProtocolException(
                                "BW_COMPUTER_VOICE_DIRECT_UPLINK_NOT_ACTIVE",
                                "浏览器麦克风上行尚未启动"));
                }
                else if (
                    stoppedBeingActive
                )
                {
                    uplinkSequenceGuard.End();
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
        // the exact Reader PWA, the fixed native App loopback shell, or a
        // controlled extension background origin.
        // Content scripts must relay through that background origin instead of
        // making a request whose Origin is an arbitrary visited web page.
        return string.Equals(
                origin,
                SingleUserReaderOrigin,
                StringComparison.Ordinal)
            || string.Equals(
                origin,
                DirectBridgeConfigStore.NativeAppOrigin,
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
        while (await timer.WaitForNextTickAsync(cancellationToken)
            .ConfigureAwait(false))
        {
            try
            {
                DirectBridgeConfig config = _configStore.Load();
                _snapshotViewer.SynchronizeServiceIntent(
                    config.ContextDeliveryMode,
                    SnapshotServiceRequested);
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
            catch (Exception exception)
                when (exception is not OperationCanceledException)
            {
                // 心跳循环是本进程的生命线（2026-08-26 实锤 1249 次
                // service-start 对 76 次体面关停的主嫌）：这里每 5 秒读
                // 配置、写状态文件，与 MCP 实例/安装器/查看器共用同一批
                // 文件 —— 一次瞬时共享冲突把异常冒出去 = 心跳任务整个
                // 死掉 = 状态文件停更 = supervisor 15-30 秒后判死并
                // **无日志强杀**。单次失败跳过本拍，留下痕迹，下一拍
                // 照常 —— supervisor 只应在进程真死时接管。
                Console.Error.WriteLine(
                    "[heartbeat] 单拍失败(已跳过): "
                    + exception.GetType().Name + ": "
                    + exception.Message);
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
                try
                {
                    await DisposeOwnedResourcesAsync().WaitAsync(
                        OwnedCleanupTimeout).ConfigureAwait(false);
                }
                catch (TimeoutException exception)
                {
                    throw new DirectProtocolException(
                        "BW_COMPUTER_VOICE_DIRECT_SHUTDOWN_CLEANUP_TIMEOUT",
                        "Direct 退出时清理语音、媒体与路由超时",
                        retryable: true,
                        innerException: exception);
                }
                _disposeCompleted = true;
                if (Volatile.Read(ref _gracefulShutdownRequested) != 0)
                {
                    WriteShutdownReceipt(
                        _runtimeDirectory,
                        _serviceInstanceId,
                        "success");
                }
            }
            catch (Exception exception)
            {
                if (Volatile.Read(ref _gracefulShutdownRequested) != 0)
                {
                    try
                    {
                        WriteShutdownReceipt(
                            _runtimeDirectory,
                            _serviceInstanceId,
                            "failed",
                            ShutdownFailureCode(exception));
                    }
                    catch (Exception receiptException)
                    {
                        throw new AggregateException(
                            exception,
                            receiptException);
                    }
                }
                throw;
            }
        }
        finally
        {
            _disposeGate.Release();
        }
    }

    private async Task DisposeOwnedResourcesAsync()
    {
        try
        {
            await DisposeCoordinatorWithBoundedRetryAsync()
                .ConfigureAwait(false);
        }
        finally
        {
            _snapshotViewer.Dispose();
            _dictionaryFallback.Dispose();
            if (_codexVoiceControl is IAsyncDisposable disposableVoice)
            {
                await disposableVoice.DisposeAsync()
                    .ConfigureAwait(false);
            }
        }
        await _connectionOwnership.DisposeAsync()
            .ConfigureAwait(false);
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

internal sealed record DirectContextConnectionHealthSnapshot(
    int ConnectionCount,
    DateTimeOffset? LastSeenAtUtc);

internal sealed class DirectContextConnectionHealth
{
    private readonly object _gate = new();
    private readonly Func<DateTimeOffset> _utcNow;
    private int _connectionCount;
    private DateTimeOffset? _lastSeenAtUtc;

    internal DirectContextConnectionHealth(
        Func<DateTimeOffset>? utcNow = null)
    {
        _utcNow = utcNow ?? (() => DateTimeOffset.UtcNow);
    }

    internal void Connected()
    {
        lock (_gate)
        {
            _connectionCount += 1;
            _lastSeenAtUtc = _utcNow().ToUniversalTime();
        }
    }

    internal void MessageSeen()
    {
        lock (_gate)
        {
            if (_connectionCount > 0)
            {
                _lastSeenAtUtc = _utcNow().ToUniversalTime();
            }
        }
    }

    internal void Disconnected()
    {
        lock (_gate)
        {
            if (_connectionCount > 0)
            {
                _connectionCount -= 1;
            }
        }
    }

    internal DirectContextConnectionHealthSnapshot Snapshot()
    {
        lock (_gate)
        {
            return new DirectContextConnectionHealthSnapshot(
                _connectionCount,
                _lastSeenAtUtc);
        }
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
