using System.Buffers.Binary;
using System.Diagnostics;
using System.Text;
using System.Text.Json;

namespace BwReader.ComputerVoiceAudio;

internal sealed class NativeMessagingHost : IAsyncDisposable
{
    private const string Contract = "reader-computer-voice-native/1";
    private const int MaximumInboundBytes = 64 * 1024;
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        WriteIndented = false,
    };

    private readonly Stream _input;
    private readonly Stream _output;
    private readonly NativeHostConfig _config;
    private readonly object _writeGate = new();
    private readonly SemaphoreSlim _stateGate = new(1, 1);
    private ProcessLoopbackCaptureSession? _outputSession;
    private ExplicitMicrophoneCaptureSession? _microphoneSession;
    private CancellationTokenSource? _captureLifetime;
    private Task? _outputPump;
    private Task? _microphonePump;
    private string? _sessionId;
    private bool _disposed;

    internal NativeMessagingHost(
        Stream input,
        Stream output,
        NativeHostConfig config)
    {
        _input = input;
        _output = output;
        _config = config;
    }

    internal async Task<int> RunAsync(CancellationToken cancellationToken)
    {
        await WriteAsync(new
        {
            contract = Contract,
            type = "hello",
            role = "native-host",
            instanceId = $"windows-{Environment.ProcessId}",
            protocolVersion = 1,
        }, cancellationToken).ConfigureAwait(false);
        await WriteCapabilitiesAsync(cancellationToken).ConfigureAwait(false);

        while (!cancellationToken.IsCancellationRequested)
        {
            JsonDocument? message = await ReadAsync(cancellationToken)
                .ConfigureAwait(false);
            if (message is null)
            {
                break;
            }
            using (message)
            {
                try
                {
                    await HandleAsync(
                        message.RootElement,
                        cancellationToken).ConfigureAwait(false);
                }
                catch (Exception error)
                {
                    await FailAndStopAsync(
                        error,
                        cancellationToken).ConfigureAwait(false);
                }
            }
        }

        await StopCaptureAsync(CancellationToken.None).ConfigureAwait(false);
        return 0;
    }

    private async Task HandleAsync(
        JsonElement message,
        CancellationToken cancellationToken)
    {
        string type = RequireString(message, "type", 32);
        if (RequireString(message, "contract", 128) != Contract)
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_NATIVE_CONTRACT");
        }
        switch (type)
        {
            case "hello":
                RequireExactKeys(
                    message,
                    "contract",
                    "type",
                    "role",
                    "instanceId",
                    "protocolVersion");
                if (
                    RequireString(message, "role", 32) != "extension"
                    || message.GetProperty("protocolVersion").GetInt32() != 1
                )
                {
                    throw new InvalidOperationException(
                        "BW_COMPUTER_VOICE_NATIVE_CONTRACT");
                }
                _ = RequireString(message, "instanceId", 128);
                await WriteCapabilitiesAsync(cancellationToken)
                    .ConfigureAwait(false);
                return;
            case "start":
                await StartAsync(message, cancellationToken)
                    .ConfigureAwait(false);
                return;
            case "stop":
                ValidateStop(message);
                await StopCaptureAsync(cancellationToken)
                    .ConfigureAwait(false);
                await WriteStatsAsync("stopped", false, cancellationToken)
                    .ConfigureAwait(false);
                return;
            default:
                throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_NATIVE_CONTRACT");
        }
    }

    private async Task StartAsync(
        JsonElement message,
        CancellationToken cancellationToken)
    {
        ValidateStart(message);
        await _stateGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            if (_sessionId is not null)
            {
                throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_NATIVE_BUSY");
            }
            if (!_config.LocalOptIn)
            {
                throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_NATIVE_LOCAL_OPT_IN_REQUIRED");
            }

            JsonElement targetValue = message.GetProperty("target");
            uint requestedRoot = targetValue
                .GetProperty("rootProcessId").GetUInt32();
            CodexAppTarget target = WindowsCodexAppProbe.RequireReady();
            if (target.RootProcessId != requestedRoot)
            {
                throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_NATIVE_TARGET_CHANGED");
            }
            string microphoneId = message.GetProperty("microphone")
                .GetProperty("deviceId").GetString() ?? "";
            if (!string.Equals(
                microphoneId,
                _config.MicrophoneEndpointId,
                StringComparison.Ordinal))
            {
                throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_NATIVE_MICROPHONE_CHANGED");
            }

            string sessionId = RequireString(message, "sessionId", 128);
            // Starting the already-approved typist before opening WASAPI keeps
            // the bounded capture queues empty while its launcher performs
            // idempotency/session checks.  If later audio setup fails the
            // typist intentionally remains running; this bridge never stops
            // the user's existing companion.
            await EnsureTypistRunningAsync(cancellationToken)
                .ConfigureAwait(false);
            BoundedPcmPacketQueue outputQueue = new(32, 2 * 1024 * 1024);
            BoundedPcmPacketQueue microphoneQueue = new(32, 2 * 1024 * 1024);
            ProcessLoopbackCaptureSession outputSession =
                ProcessLoopbackCaptureSession.Prepare(
                    requestedRoot,
                    outputQueue);
            ExplicitMicrophoneCaptureSession microphoneSession =
                ExplicitMicrophoneCaptureSession.Prepare(
                    MicCaptureRequest.Create(microphoneId),
                    microphoneQueue);
            CancellationTokenSource lifetime =
                CancellationTokenSource.CreateLinkedTokenSource(
                    cancellationToken);
            try
            {
                await outputSession.StartAsync(lifetime.Token)
                    .ConfigureAwait(false);
                await microphoneSession.StartAsync(lifetime.Token)
                    .ConfigureAwait(false);
                Pcm48kMonoFramer outputFramer = new(
                    outputSession.Format
                    ?? throw new InvalidOperationException(
                        "BW_COMPUTER_VOICE_AUDIO_FORMAT_MISSING"));
                Pcm48kMonoFramer microphoneFramer = new(
                    microphoneSession.Format
                    ?? throw new InvalidOperationException(
                        "BW_COMPUTER_VOICE_AUDIO_FORMAT_MISSING"));

                if (!WindowsCodexAppProbe.SendVoiceShortcut(target))
                {
                    throw new InvalidOperationException(
                        "BW_COMPUTER_VOICE_SHORTCUT_FAILED");
                }

                _sessionId = sessionId;
                _outputSession = outputSession;
                _microphoneSession = microphoneSession;
                _captureLifetime = lifetime;
                _outputPump = PumpAsync(
                    "app-output",
                    sessionId,
                    outputQueue,
                    outputFramer,
                    lifetime.Token);
                _microphonePump = PumpAsync(
                    "user-mic",
                    sessionId,
                    microphoneQueue,
                    microphoneFramer,
                    lifetime.Token);
                await WriteStatsAsync(
                    "active",
                    true,
                    cancellationToken).ConfigureAwait(false);
            }
            catch
            {
                lifetime.Cancel();
                try
                {
                    await microphoneSession.StopAsync(CancellationToken.None)
                        .ConfigureAwait(false);
                }
                catch
                {
                }
                try
                {
                    await outputSession.StopAsync(CancellationToken.None)
                        .ConfigureAwait(false);
                }
                catch
                {
                }
                await microphoneSession.DisposeAsync().ConfigureAwait(false);
                await outputSession.DisposeAsync().ConfigureAwait(false);
                lifetime.Dispose();
                throw;
            }
        }
        finally
        {
            _stateGate.Release();
        }
    }

    private async Task PumpAsync(
        string trackId,
        string sessionId,
        BoundedPcmPacketQueue queue,
        Pcm48kMonoFramer framer,
        CancellationToken cancellationToken)
    {
        try
        {
            while (!cancellationToken.IsCancellationRequested)
            {
                bool progressed = false;
                while (queue.TryRead(out PcmPacket packet))
                {
                    progressed = true;
                    framer.Push(packet);
                    while (framer.TryRead(out PcmFrameChunk chunk))
                    {
                        await WriteAsync(new
                        {
                            contract = Contract,
                            type = "pcm",
                            sessionId,
                            trackId,
                            sequence = chunk.Sequence,
                            timestampUs = chunk.TimestampUs,
                            format = WireFormat(),
                            mediaDestination = "extension-offscreen-only",
                            dataBase64 = Convert.ToBase64String(chunk.Data),
                        }, cancellationToken).ConfigureAwait(false);
                    }
                }
                if (
                    queue.IsCompleted
                    && queue.CompletionError is not null
                )
                {
                    throw queue.CompletionError;
                }
                if (!progressed)
                {
                    await Task.Delay(2, cancellationToken)
                        .ConfigureAwait(false);
                }
            }
        }
        catch (OperationCanceledException)
            when (cancellationToken.IsCancellationRequested)
        {
        }
        catch (Exception error)
        {
            // Do not await StopCaptureAsync from inside one of the two pump
            // tasks: StopCaptureAsync joins both pumps and would wait on this
            // task itself.  Schedule cleanup after this pump has returned.
            _captureLifetime?.Cancel();
            _ = Task.Run(async () =>
            {
                try
                {
                    await FailAndStopAsync(
                        error,
                        CancellationToken.None).ConfigureAwait(false);
                }
                catch
                {
                    // Closing the native pipe already makes Chrome fail
                    // closed; a second error must not resurrect capture.
                }
            });
        }
    }

    private async Task StopCaptureAsync(CancellationToken cancellationToken)
    {
        await _stateGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            CancellationTokenSource? lifetime = _captureLifetime;
            ProcessLoopbackCaptureSession? output = _outputSession;
            ExplicitMicrophoneCaptureSession? microphone =
                _microphoneSession;
            Task? outputPump = _outputPump;
            Task? microphonePump = _microphonePump;
            _captureLifetime = null;
            _outputSession = null;
            _microphoneSession = null;
            _outputPump = null;
            _microphonePump = null;
            _sessionId = null;
            lifetime?.Cancel();
            if (microphone is not null)
            {
                try
                {
                    await microphone.StopAsync(CancellationToken.None)
                        .ConfigureAwait(false);
                }
                catch
                {
                }
            }
            if (output is not null)
            {
                try
                {
                    await output.StopAsync(CancellationToken.None)
                        .ConfigureAwait(false);
                }
                catch
                {
                }
            }
            if (outputPump is not null || microphonePump is not null)
            {
                try
                {
                    await Task.WhenAll(
                        outputPump ?? Task.CompletedTask,
                        microphonePump ?? Task.CompletedTask)
                        .ConfigureAwait(false);
                }
                catch
                {
                }
            }
            if (microphone is not null)
            {
                await microphone.DisposeAsync().ConfigureAwait(false);
            }
            if (output is not null)
            {
                await output.DisposeAsync().ConfigureAwait(false);
            }
            lifetime?.Dispose();
        }
        finally
        {
            _stateGate.Release();
        }
    }

    private async Task EnsureTypistRunningAsync(
        CancellationToken cancellationToken)
    {
        string python = PythonExecutable();
        if (!File.Exists(python)
            || !File.Exists(_config.TypistHelper))
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_TYPIST_HELPER_UNAVAILABLE");
        }
        ProcessStartInfo start = new()
        {
            FileName = python,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
        };
        start.ArgumentList.Add(_config.TypistHelper);
        start.ArgumentList.Add("--ensure-running");
        using Process owner = Process.GetCurrentProcess();
        start.ArgumentList.Add(
            owner.Id.ToString(
                System.Globalization.CultureInfo.InvariantCulture));
        start.ArgumentList.Add(
            owner.StartTime.ToUniversalTime().ToFileTimeUtc().ToString(
                System.Globalization.CultureInfo.InvariantCulture));
        using Process process = Process.Start(start)
            ?? throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_TYPIST_HELPER_UNAVAILABLE");
        string output = await process.StandardOutput.ReadToEndAsync(
            cancellationToken).ConfigureAwait(false);
        string error = await process.StandardError.ReadToEndAsync(
            cancellationToken).ConfigureAwait(false);
        await process.WaitForExitAsync(cancellationToken)
            .ConfigureAwait(false);
        if (process.ExitCode != 0)
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_TYPIST_START_FAILED:" +
                (error.Length != 0 ? error : output));
        }
        using JsonDocument result = JsonDocument.Parse(output);
        if (
            !result.RootElement.TryGetProperty(
                "ok",
                out JsonElement ok)
            || ok.ValueKind != JsonValueKind.True
            || !result.RootElement.TryGetProperty(
                "running",
                out JsonElement running)
            || running.ValueKind != JsonValueKind.True
        )
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_TYPIST_START_FAILED");
        }
    }

    private static string PythonExecutable() => Path.Combine(
        Environment.GetFolderPath(
            Environment.SpecialFolder.UserProfile),
        "AppData",
        "Local",
        "Programs",
        "Python",
        "Python313",
        "python.exe");

    private async Task WriteCapabilitiesAsync(
        CancellationToken cancellationToken)
    {
        CodexAppTarget? target = null;
        try
        {
            target = WindowsCodexAppProbe.RequireReady();
        }
        catch
        {
        }
        bool microphoneReady =
            _config.MicrophoneEndpointId.Length != 0;
        await WriteAsync(new
        {
            contract = Contract,
            type = "capabilities",
            nativeHostReady = true,
            captureScope = "process-only",
            loopbackMode = "include-target-process-tree",
            systemOutputFallback = false,
            microphoneSelection = "explicit-device-only",
            transport = "native-messaging-local",
            mediaDestination = "extension-offscreen-only",
            tracks = new[] { "app-output", "user-mic" },
            format = WireFormat(),
            maxInFlightChunks = 12,
            localOptIn = _config.LocalOptIn,
            shortcutConfigured =
                _config.VoiceStartShortcut == "Ctrl+Shift+C",
            app = new
            {
                ready = target is not null,
                target = target is null
                    ? null
                    : new
                    {
                        appId = "openai-codex-desktop",
                        executable = "ChatGPT.exe",
                        rootProcessId = target.RootProcessId,
                    },
            },
            microphone = new
            {
                available = microphoneReady,
                selection = "explicit-device-only",
                deviceId = microphoneReady
                    ? _config.MicrophoneEndpointId
                    : null,
            },
            companion = new
            {
                kind = "voice-typist",
                launcherAvailable =
                    File.Exists(_config.TypistHelper)
                    && File.Exists(PythonExecutable()),
            },
        }, cancellationToken).ConfigureAwait(false);
    }

    private async Task WriteStatsAsync(
        string state,
        bool active,
        CancellationToken cancellationToken)
    {
        await WriteAsync(new
        {
            contract = Contract,
            type = "stats",
            sessionId = active ? _sessionId : null,
            state,
            nativeHostReady = true,
            captureActive = active,
            credits = new Dictionary<string, int>
            {
                ["app-output"] = 12,
                ["user-mic"] = 12,
            },
            queuedChunks = new Dictionary<string, int>
            {
                ["app-output"] = 0,
                ["user-mic"] = 0,
            },
            droppedChunks = 0,
        }, cancellationToken).ConfigureAwait(false);
    }

    private async Task FailAndStopAsync(
        Exception error,
        CancellationToken cancellationToken)
    {
        string? failedSession = _sessionId;
        await StopCaptureAsync(CancellationToken.None).ConfigureAwait(false);
        await WriteAsync(new
        {
            contract = Contract,
            type = "error",
            sessionId = failedSession,
            code = ErrorCode(error),
            message = error.Message.Length > 1024
                ? error.Message[..1024]
                : error.Message,
            retryable = false,
        }, cancellationToken).ConfigureAwait(false);
    }

    private static string ErrorCode(Exception error)
    {
        string candidate = error.Message.Split(':', 2)[0];
        return candidate.StartsWith(
            "BW_COMPUTER_VOICE_",
            StringComparison.Ordinal)
            && candidate.All(character =>
                character is >= 'A' and <= 'Z'
                or >= '0' and <= '9'
                or '_')
            ? candidate
            : "BW_COMPUTER_VOICE_NATIVE_FAILED";
    }

    private async Task<JsonDocument?> ReadAsync(
        CancellationToken cancellationToken)
    {
        byte[] prefix = new byte[4];
        int received = 0;
        while (received < prefix.Length)
        {
            int count = await _input.ReadAsync(
                prefix.AsMemory(received),
                cancellationToken).ConfigureAwait(false);
            if (count == 0)
            {
                return received == 0
                    ? null
                    : throw new EndOfStreamException(
                        "BW_COMPUTER_VOICE_NATIVE_FRAME_TRUNCATED");
            }
            received += count;
        }
        int length = BinaryPrimitives.ReadInt32LittleEndian(prefix);
        if (length <= 0 || length > MaximumInboundBytes)
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_NATIVE_FRAME_TOO_LARGE");
        }
        byte[] payload = new byte[length];
        received = 0;
        while (received < payload.Length)
        {
            int count = await _input.ReadAsync(
                payload.AsMemory(received),
                cancellationToken).ConfigureAwait(false);
            if (count == 0)
            {
                throw new EndOfStreamException(
                    "BW_COMPUTER_VOICE_NATIVE_FRAME_TRUNCATED");
            }
            received += count;
        }
        return JsonDocument.Parse(payload);
    }

    private Task WriteAsync(
        object value,
        CancellationToken cancellationToken)
    {
        byte[] payload = JsonSerializer.SerializeToUtf8Bytes(
            value,
            JsonOptions);
        if (payload.Length > MaximumInboundBytes)
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_NATIVE_FRAME_TOO_LARGE");
        }
        byte[] prefix = new byte[4];
        BinaryPrimitives.WriteInt32LittleEndian(prefix, payload.Length);
        lock (_writeGate)
        {
            cancellationToken.ThrowIfCancellationRequested();
            _output.Write(prefix);
            _output.Write(payload);
            _output.Flush();
        }
        return Task.CompletedTask;
    }

    private static object WireFormat() => new
    {
        sampleRate = 48_000,
        channels = 1,
        sampleFormat = "s16le",
        frameDurationMs = 20,
        framesPerChunk = 960,
    };

    private static void ValidateStart(JsonElement message)
    {
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId",
            "sessionId",
            "target",
            "captureScope",
            "loopbackMode",
            "microphone",
            "tracks",
            "format",
            "transport",
            "mediaDestination",
            "authorization");
        _ = RequireString(message, "requestId", 128);
        _ = RequireString(message, "sessionId", 128);
        if (
            RequireString(message, "captureScope", 32) != "process-only"
            || RequireString(message, "loopbackMode", 64)
                != "include-target-process-tree"
            || RequireString(message, "transport", 64)
                != "native-messaging-local"
            || RequireString(message, "mediaDestination", 64)
                != "extension-offscreen-only"
        )
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_NATIVE_SCOPE");
        }
        JsonElement target = message.GetProperty("target");
        RequireExactKeys(
            target,
            "appId",
            "executable",
            "rootProcessId");
        if (
            RequireString(target, "appId", 64)
                != "openai-codex-desktop"
            || RequireString(target, "executable", 64)
                != "ChatGPT.exe"
            || target.GetProperty("rootProcessId").GetUInt32() == 0
        )
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_NATIVE_SCOPE");
        }
        JsonElement microphone = message.GetProperty("microphone");
        RequireExactKeys(microphone, "selection", "deviceId");
        if (
            RequireString(microphone, "selection", 64)
                != "explicit-device-only"
            || RequireString(
                microphone,
                "deviceId",
                MicCaptureRequest.MaximumEndpointIdLength).Length == 0
        )
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_NATIVE_SCOPE");
        }
        JsonElement tracks = message.GetProperty("tracks");
        string[] trackValues = tracks.EnumerateArray()
            .Select(value => value.GetString() ?? "")
            .ToArray();
        if (!trackValues.SequenceEqual(
            new[] { "app-output", "user-mic" }))
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_NATIVE_SCOPE");
        }
        ValidateFormat(message.GetProperty("format"));
        JsonElement authorization = message.GetProperty("authorization");
        RequireExactKeys(
            authorization,
            "localOptIn",
            "oneTimeTrigger",
            "paired",
            "nativeHostReady");
        if (
            authorization.EnumerateObject().Any(property =>
                property.Value.ValueKind != JsonValueKind.True)
        )
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_NATIVE_NOT_READY");
        }
    }

    private static void ValidateStop(JsonElement message)
    {
        RequireExactKeys(
            message,
            "contract",
            "type",
            "requestId",
            "sessionId",
            "reason");
        _ = RequireString(message, "requestId", 128);
        _ = RequireString(message, "sessionId", 128);
        _ = RequireString(message, "reason", 256);
    }

    private static void ValidateFormat(JsonElement format)
    {
        RequireExactKeys(
            format,
            "sampleRate",
            "channels",
            "sampleFormat",
            "frameDurationMs",
            "framesPerChunk");
        if (
            format.GetProperty("sampleRate").GetInt32() != 48_000
            || format.GetProperty("channels").GetInt32() != 1
            || RequireString(format, "sampleFormat", 16) != "s16le"
            || format.GetProperty("frameDurationMs").GetInt32() != 20
            || format.GetProperty("framesPerChunk").GetInt32() != 960
        )
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_NATIVE_FORMAT");
        }
    }

    private static void RequireExactKeys(
        JsonElement value,
        params string[] expected)
    {
        if (value.ValueKind != JsonValueKind.Object)
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_NATIVE_INVALID");
        }
        string[] actual = value.EnumerateObject()
            .Select(property => property.Name)
            .Order(StringComparer.Ordinal)
            .ToArray();
        string[] sortedExpected = expected
            .Order(StringComparer.Ordinal)
            .ToArray();
        if (!actual.SequenceEqual(sortedExpected, StringComparer.Ordinal))
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_NATIVE_INVALID");
        }
    }

    private static string RequireString(
        JsonElement value,
        string property,
        int maximum)
    {
        string text = value.GetProperty(property).GetString() ?? "";
        if (
            text.Length == 0
            || text.Length > maximum
            || text.Any(character => char.IsControl(character))
        )
        {
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_NATIVE_INVALID");
        }
        return text;
    }

    public async ValueTask DisposeAsync()
    {
        if (_disposed)
        {
            return;
        }
        _disposed = true;
        await StopCaptureAsync(CancellationToken.None).ConfigureAwait(false);
        _stateGate.Dispose();
    }
}
