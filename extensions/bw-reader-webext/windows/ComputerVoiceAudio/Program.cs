using System.Diagnostics;
using System.Globalization;
using System.Text.Json;

namespace BwReader.ComputerVoiceAudio;

internal static class Program
{
    private const string DirectServeMutexName =
        @"Global\BWReaderComputerVoiceDirectServe-v1";

    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        WriteIndented = true,
    };

    public static async Task<int> Main(string[] args)
    {
        try
        {
            if (args is ["--describe"])
            {
                return WriteJson(AudioBridgeContract.Describe());
            }
            if (args is ["--self-test"])
            {
                return WriteJson(ContractSelfTest.Run());
            }
            if (args is ["--list-direct-microphones"])
            {
                return WriteJson(new
                {
                    contract =
                        "reader-computer-voice-microphones/1",
                    ok = true,
                    captureStarted = false,
                    devices =
                        DirectMicrophoneDiscovery.EnumerateActive(),
                });
            }
            if (args is ["--list-direct-render-endpoints"])
            {
                return WriteJson(new
                {
                    contract =
                        "reader-computer-voice-render-endpoints/1",
                    ok = true,
                    captureStarted = false,
                    devices = DirectMicrophoneDiscovery
                        .EnumerateActiveRenderEndpoints(),
                });
            }
            if (args is ["--probe-codex-voice-state"])
            {
                CodexVoiceActivitySnapshot state =
                    new WindowsRegistryCodexVoiceActivitySource()
                        .Read();
                return WriteJson(new
                {
                    contract =
                        "reader-computer-voice-state/1",
                    ok = state.Status
                        == CodexVoiceActivityReadStatus.Available,
                    status = state.Status.ToString()
                        .ToLowerInvariant(),
                    active = state.Active,
                    lastUsedTimeStart =
                        state.LastUsedTimeStart,
                    lastUsedTimeStop =
                        state.LastUsedTimeStop,
                    source =
                        "windows-microphone-capability-ledger",
                    proxyFor = "codex-microphone-use",
                    captureStarted = false,
                    shortcutSent = false,
                });
            }
            if (
                args.Length == 3
                && args[0] == "--probe-codex-app-audio-route"
                && args[1] == "--config"
            )
            {
                if (!Path.IsPathFullyQualified(args[2]))
                {
                    throw new DirectProtocolException(
                        "BW_COMPUTER_VOICE_DIRECT_CONFIG_PATH_INVALID",
                        "直连配置必须使用绝对路径");
                }
                DirectBridgeConfigStore configStore = new(
                    Path.GetFullPath(args[2]));
                return WriteJson(CodexAppAudioRouteProbe.Run(
                    configStore.Load()));
            }
            if (
                args.Length == 3
                && args[0]
                    == "--reset-app-audio-routes-to-default"
                && args[1] == "--app-kind"
            )
            {
                return ResetAppAudioRoutesToDefault(args[2]);
            }
            if (
                args.Length == 3
                && args[0] == "--probe-direct-output-route"
                && args[1] == "--config"
            )
            {
                if (!Path.IsPathFullyQualified(args[2]))
                {
                    throw new DirectProtocolException(
                        "BW_COMPUTER_VOICE_DIRECT_CONFIG_PATH_INVALID",
                        "直连配置必须使用绝对路径");
                }
                DirectBridgeConfigStore configStore = new(
                    Path.GetFullPath(args[2]));
                return WriteJson(DirectOutputRouteProbe.Run(
                    configStore.Load()));
            }
            if (
                args.Length == 3
                && args[0] == "--diagnose-direct-audio-no-start"
                && args[1] == "--config"
            )
            {
                if (!Path.IsPathFullyQualified(args[2]))
                {
                    throw new DirectProtocolException(
                        "BW_COMPUTER_VOICE_DIRECT_CONFIG_PATH_INVALID",
                        "直连配置必须使用绝对路径");
                }
                DirectBridgeConfigStore configStore = new(
                    Path.GetFullPath(args[2]));
                return WriteJson(await DirectAudioDiagnostics.RunAsync(
                    configStore.Load(),
                    CancellationToken.None).ConfigureAwait(false));
            }
            if (
                args.Length >= 1
                && args[0] == "--direct-serve"
            )
            {
                return await RunDirectServiceAsync(args)
                    .ConfigureAwait(false);
            }
            if (
                args.Length == 3
                && args[0] == "--reader-context-mcp"
                && args[1] == "--state"
            )
            {
                if (!Path.IsPathFullyQualified(args[2]))
                {
                    throw new DirectProtocolException(
                        "BW_READER_CONTEXT_SNAPSHOT_PATH_INVALID",
                        "Reader 本地快照必须使用绝对路径");
                }
                Console.InputEncoding = new System.Text.UTF8Encoding(
                    encoderShouldEmitUTF8Identifier: false);
                Console.OutputEncoding = new System.Text.UTF8Encoding(
                    encoderShouldEmitUTF8Identifier: false);
                NamedPipeReaderVisualRpcClient visualClient = new();
                NamedPipeReaderBrowserControlRpcClient browserControlClient =
                    new();
                NamedPipeReaderRealtimeOutputRpcClient outputClient = new();
                NamedPipeReaderQueryRpcClient queryClient = new();
                ReaderContextMcpServer server = new(
                    Path.GetFullPath(args[2]),
                    Console.In,
                    Console.Out,
                    fetchVisualAsync: visualClient.RequestAsync,
                    controlBrowserAsync: browserControlClient.RequestAsync,
                    sendOutputAsync: outputClient.SendAsync,
                    probeOutputSourceAsync: outputClient.ProbeSourceAsync,
                    queryReaderAsync: queryClient.RequestAsync);
                return await server.RunAsync(CancellationToken.None)
                    .ConfigureAwait(false);
            }
            if (
                args.Length == 3
                && args[0] == "--reader-context-view"
                && args[1] == "--state"
            )
            {
                if (!Path.IsPathFullyQualified(args[2]))
                {
                    throw new DirectProtocolException(
                        "BW_READER_CONTEXT_SNAPSHOT_PATH_INVALID",
                        "Reader 本地快照必须使用绝对路径");
                }
                return await DirectSnapshotTerminal.RunAsync(
                    Path.GetFullPath(args[2]),
                    CancellationToken.None).ConfigureAwait(false);
            }
            if (IsNativeMessagingInvocation(args))
            {
                NativeHostConfig config = NativeHostConfig.Load(
                    AppContext.BaseDirectory);
                config.RequireOrigin(args[0]);
                await using NativeMessagingHost host = new(
                    Console.OpenStandardInput(),
                    Console.OpenStandardOutput(),
                    config);
                return await host.RunAsync(CancellationToken.None)
                    .ConfigureAwait(false);
            }
            return RejectUnknownCommand();
        }
        catch (DirectProtocolException exception)
        {
            Console.Error.WriteLine(JsonSerializer.Serialize(new
            {
                contract = AudioBridgeContract.Contract,
                ok = false,
                error = exception.Code,
                retryable = exception.Retryable,
                detail = exception.Message,
            }, JsonOptions));
            return 1;
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine(JsonSerializer.Serialize(new
            {
                contract = AudioBridgeContract.Contract,
                ok = false,
                error = "BW_COMPUTER_VOICE_AUDIO_SELF_TEST_FAILED",
                detail = exception.ToString(),
            }, JsonOptions));
            return 1;
        }
    }

    private static async Task<int> RunDirectServiceAsync(string[] args)
    {
        string? readerPcOwnerPid = null;
        if (
            args.Length == 3
            && args[1] == "--config"
        )
        {
            // Retained for package self-tests and legacy standalone callers.
        }
        else if (
            args.Length == 5
            && args[1] == "--config"
            && args[3] == "--readerpc-owner-pid"
        )
        {
            readerPcOwnerPid = args[4];
        }
        else
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_ARGUMENTS_INVALID",
                "直连服务参数无效；ReaderPC 所有权模式必须提供 owner PID");
        }

        if (!Path.IsPathFullyQualified(args[2]))
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_CONFIG_PATH_INVALID",
                "直连配置必须使用绝对路径");
        }
        string configPath = Path.GetFullPath(args[2]);
        using DirectReaderPcOwnerLease? ownerLease =
            readerPcOwnerPid is null
                ? null
                : DirectReaderPcOwnerLease.Open(readerPcOwnerPid);
        using Mutex directServeMutex = new(
            initiallyOwned: false,
            DirectServeMutexName);
        bool ownsDirectServeMutex;
        try
        {
            ownsDirectServeMutex = directServeMutex.WaitOne(0);
        }
        catch (AbandonedMutexException)
        {
            ownsDirectServeMutex = true;
        }
        if (!ownsDirectServeMutex)
        {
            if (ownerLease is not null)
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_READERPC_OWNER_NOT_BOUND",
                    "已有 Direct 实例运行，新的 ReaderPC owner 未获得服务所有权");
            }
            return 0;
        }
        try
        {
            DirectBridgeConfigStore configStore = new(configPath);
            _ = configStore.Load();
            await using DirectBridgeServer server = new(
                configStore,
                new WindowsDirectAppLauncher(),
                new WindowsDirectMediaAdapter(
                    configStore.InstallationRoot),
                new NamedPipeDirectContextAdapter(),
                new FileDirectSnapshotContextAdapter(
                    Path.Combine(
                        configStore.InstallationRoot,
                        "runtime",
                        FileDirectSnapshotContextAdapter
                            .SnapshotFileName)));
            if (ownerLease is null)
            {
                return await server.RunAsync(CancellationToken.None)
                    .ConfigureAwait(false);
            }

            using CancellationTokenSource serviceLifetime = new();
            using CancellationTokenSource ownerMonitorLifetime = new();
            Task ownerMonitor = ownerLease.CancelServiceWhenExitedAsync(
                serviceLifetime,
                ownerMonitorLifetime.Token);
            try
            {
                return await server.RunAsync(serviceLifetime.Token)
                    .ConfigureAwait(false);
            }
            finally
            {
                ownerMonitorLifetime.Cancel();
                try
                {
                    await ownerMonitor.ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                    when (ownerMonitorLifetime.IsCancellationRequested)
                {
                }
            }
        }
        finally
        {
            try
            {
                directServeMutex.ReleaseMutex();
            }
            catch (ApplicationException)
            {
                // Mutex 有线程亲和性:WaitOne 在启动线程,await 之后 finally 跑在
                // 线程池线程 → ReleaseMutex 必抛——这把**每一次正常退出都变成崩溃**
                // (自测 SELF_TEST_FAILED 与生产异常退出同源,2026-08-17 栈实锤)。
                // 吞掉是安全的:进程退出时 OS 释放互斥,下一实例的
                // AbandonedMutexException 分支(上方)已按"获得所有权"处理。
            }
        }
    }

    private static bool IsNativeMessagingInvocation(string[] args)
    {
        if (
            args.Length is < 1 or > 2
            || !args[0].StartsWith(
                "chrome-extension://",
                StringComparison.Ordinal)
            || !args[0].EndsWith("/", StringComparison.Ordinal)
        )
        {
            return false;
        }
        return args.Length == 1
            || (
                args[1].StartsWith(
                    "--parent-window=",
                    StringComparison.Ordinal)
                && uint.TryParse(
                    args[1]["--parent-window=".Length..],
                    out _)
            );
    }

    private static int WriteJson(object value)
    {
        Console.WriteLine(JsonSerializer.Serialize(value, JsonOptions));
        return 0;
    }

    private static int RejectUnknownCommand()
    {
        Console.Error.WriteLine(JsonSerializer.Serialize(new
        {
            contract = AudioBridgeContract.Contract,
            ok = false,
            error = "BW_COMPUTER_VOICE_AUDIO_COMMAND_NOT_ALLOWED",
            allowed = new[] {
                "--describe",
                "--self-test",
                "--list-direct-microphones",
                "--list-direct-render-endpoints",
                "--probe-codex-voice-state",
                "--probe-codex-app-audio-route --config <absolute-path>",
                "--reset-app-audio-routes-to-default --app-kind "
                    + "<codex-desktop|chatgpt-classic>",
                "--probe-direct-output-route --config <absolute-path>",
                "--diagnose-direct-audio-no-start --config <absolute-path>",
                "--direct-serve --config <absolute-path>",
                "--direct-serve --config <absolute-path> "
                    + "--readerpc-owner-pid <positive-pid>",
                "--reader-context-mcp --state <absolute-path>",
                "--reader-context-view --state <absolute-path>",
                "Chrome Native Messaging origin (registered host only)",
            },
        }, JsonOptions));
        return 64;
    }

    private static int ResetAppAudioRoutesToDefault(string appKind)
    {
        DirectAppTargetProfile profile = DirectAppTargets.Require(appKind);
        CodexAppTarget target = WindowsCodexAppProbe.RequireReady(
            profile.AppKind);
        uint processId = WindowsCodexAppProbe.RequireAudioPolicyProcess(
            target);
        using NativePerAppAudioPolicyBackend backend = new();
        List<object> routes = [];
        bool ok = true;
        foreach (PerAppAudioRouteKey key in PerAppAudioRouteKey.All)
        {
            PersistedAudioEndpoint before = backend.Read(processId, key);
            PerAppAudioPolicyWriteResult write = backend.Write(
                processId,
                key,
                endpointId: null);
            PersistedAudioEndpoint after = backend.Read(processId, key);
            bool cleared = write.Succeeded
                && after.Kind == PersistedAudioEndpointKind.Unset;
            ok &= cleared;
            routes.Add(new
            {
                flow = key.FlowName,
                role = key.RoleName,
                before = before.Kind.ToString().ToLowerInvariant(),
                beforeEndpointId = before.EndpointId,
                cleared,
                writeHResult = write.Succeeded
                    ? null
                    : $"0x{unchecked((uint)write.HResult):X8}",
                after = after.Kind.ToString().ToLowerInvariant(),
                afterEndpointId = after.EndpointId,
            });
        }
        Console.WriteLine(JsonSerializer.Serialize(new
        {
            contract = "reader-computer-voice-audio-route-reset/1",
            ok,
            appKind = profile.AppKind,
            processId,
            routes,
            captureStarted = false,
            shortcutSent = false,
            appLaunched = false,
        }, JsonOptions));
        return ok ? 0 : 1;
    }
}

internal sealed class DirectReaderPcOwnerLease : IDisposable
{
    private readonly Process _owner;
    private bool _disposed;

    private DirectReaderPcOwnerLease(Process owner)
    {
        _owner = owner;
    }

    internal int OwnerProcessId => _owner.Id;

    internal static DirectReaderPcOwnerLease Open(string? rawOwnerPid)
    {
        if (
            !int.TryParse(
                rawOwnerPid,
                NumberStyles.None,
                CultureInfo.InvariantCulture,
                out int ownerPid)
            || ownerPid <= 0
            || ownerPid == Environment.ProcessId
        )
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_READERPC_OWNER_PID_INVALID",
                "ReaderPC owner PID 必须是另一个仍在运行的正整数进程");
        }

        Process? owner = null;
        try
        {
            owner = Process.GetProcessById(ownerPid);
            // Force the waitable process handle to open now. Holding this
            // handle binds the lease to the original process even if Windows
            // later recycles the numeric PID.
            _ = owner.SafeHandle;
            if (owner.HasExited)
            {
                throw new InvalidOperationException(
                    "ReaderPC owner already exited");
            }
            return new DirectReaderPcOwnerLease(owner);
        }
        catch (Exception exception)
            when (
                exception is ArgumentException
                or InvalidOperationException
                or System.ComponentModel.Win32Exception
            )
        {
            owner?.Dispose();
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_READERPC_OWNER_UNAVAILABLE",
                "ReaderPC owner 进程不存在、已退出或无法等待",
                retryable: false,
                innerException: exception);
        }
    }

    internal async Task CancelServiceWhenExitedAsync(
        CancellationTokenSource serviceLifetime,
        CancellationToken cancellationToken)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        ArgumentNullException.ThrowIfNull(serviceLifetime);
        try
        {
            await _owner.WaitForExitAsync(cancellationToken)
                .ConfigureAwait(false);
        }
        catch (OperationCanceledException)
            when (cancellationToken.IsCancellationRequested)
        {
            return;
        }
        finally
        {
            if (!cancellationToken.IsCancellationRequested)
            {
                serviceLifetime.Cancel();
            }
        }
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }
        _disposed = true;
        _owner.Dispose();
    }
}
