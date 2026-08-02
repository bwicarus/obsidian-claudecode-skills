using System.Collections.Concurrent;
using System.Runtime.InteropServices;
using System.Runtime.Versioning;
using System.Text;
using System.Text.Json;

namespace BwReader.ComputerVoiceAudio;

internal enum PerAppAudioDataFlow
{
    Render = 0,
    Capture = 1,
}

internal enum PerAppAudioRole
{
    Console = 0,
    Multimedia = 1,
    Communications = 2,
}

internal readonly record struct PerAppAudioRouteKey(
    PerAppAudioDataFlow Flow,
    PerAppAudioRole Role)
{
    internal static IReadOnlyList<PerAppAudioRouteKey> All { get; } =
    [
        new(PerAppAudioDataFlow.Render, PerAppAudioRole.Console),
        new(PerAppAudioDataFlow.Render, PerAppAudioRole.Multimedia),
        new(PerAppAudioDataFlow.Render, PerAppAudioRole.Communications),
        new(PerAppAudioDataFlow.Capture, PerAppAudioRole.Console),
        new(PerAppAudioDataFlow.Capture, PerAppAudioRole.Multimedia),
        new(PerAppAudioDataFlow.Capture, PerAppAudioRole.Communications),
    ];

    internal string FlowName => Flow switch
    {
        PerAppAudioDataFlow.Render => "render",
        PerAppAudioDataFlow.Capture => "capture",
        _ => throw new InvalidOperationException(
            "Unknown per-app audio data flow"),
    };

    internal string RoleName => Role switch
    {
        PerAppAudioRole.Console => "console",
        PerAppAudioRole.Multimedia => "multimedia",
        PerAppAudioRole.Communications => "communications",
        _ => throw new InvalidOperationException(
            "Unknown per-app audio role"),
    };
}

internal enum PersistedAudioEndpointKind
{
    Present,
    Unset,
    Error,
}

internal readonly record struct PersistedAudioEndpoint(
    PersistedAudioEndpointKind Kind,
    string? EndpointId,
    int HResult,
    string? Stage)
{
    internal static PersistedAudioEndpoint Present(string endpointId)
    {
        AudioPolicyEndpointId.ValidateCanonical(endpointId);
        return new(
            PersistedAudioEndpointKind.Present,
            endpointId,
            0,
            null);
    }

    internal static PersistedAudioEndpoint Unset() =>
        new(PersistedAudioEndpointKind.Unset, null, 0, null);

    internal static PersistedAudioEndpoint Error(
        int hresult,
        string stage) =>
        new(PersistedAudioEndpointKind.Error, null, hresult, stage);
}

internal readonly record struct PerAppAudioPolicyWriteResult(
    bool Succeeded,
    int HResult,
    string? Stage)
{
    internal static PerAppAudioPolicyWriteResult Success() =>
        new(true, 0, null);

    internal static PerAppAudioPolicyWriteResult Failure(
        int hresult,
        string stage) =>
        new(false, hresult, stage);
}

internal interface IPerAppAudioPolicyBackend : IDisposable
{
    PersistedAudioEndpoint Read(
        uint processId,
        PerAppAudioRouteKey key);

    PerAppAudioPolicyWriteResult Write(
        uint processId,
        PerAppAudioRouteKey key,
        string? endpointId);
}

internal sealed record PerAppAudioRouteRequest(
    uint ProcessId,
    string RenderEndpointId,
    string CaptureEndpointId,
    string JournalPath)
{
    internal void Validate()
    {
        if (ProcessId == 0)
        {
            throw InvalidRequest();
        }
        AudioPolicyEndpointId.ValidateForFlow(
            RenderEndpointId,
            PerAppAudioDataFlow.Render);
        AudioPolicyEndpointId.ValidateForFlow(
            CaptureEndpointId,
            PerAppAudioDataFlow.Capture);
        if (
            !System.IO.Path.IsPathFullyQualified(JournalPath)
            || !string.Equals(
                System.IO.Path.GetExtension(JournalPath),
                ".json",
                StringComparison.OrdinalIgnoreCase)
        )
        {
            throw InvalidRequest();
        }
    }

    internal string TargetFor(PerAppAudioRouteKey key) =>
        key.Flow switch
        {
            PerAppAudioDataFlow.Render => RenderEndpointId,
            PerAppAudioDataFlow.Capture => CaptureEndpointId,
            _ => throw InvalidRequest(),
        };

    private static DirectProtocolException InvalidRequest() =>
        new(
            "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_REQUEST_INVALID",
            "Codex 按应用音频路由请求无效");
}

internal sealed record PerAppAudioRouteRestoreResult(
    bool Succeeded,
    IReadOnlyList<PerAppAudioRouteKey> Restored,
    IReadOnlyList<PerAppAudioRouteKey> PreservedExternalChanges,
    IReadOnlyList<PerAppAudioRouteKey> Failed);

internal sealed class PerAppAudioRouteController
{
    private readonly IPerAppAudioPolicyBackend _backend;
    private readonly object _gate = new();
    private bool _leaseActive;

    internal PerAppAudioRouteController(
        IPerAppAudioPolicyBackend backend)
    {
        _backend = backend
            ?? throw new ArgumentNullException(nameof(backend));
    }

    internal PerAppAudioRouteLease Acquire(
        PerAppAudioRouteRequest request)
    {
        ArgumentNullException.ThrowIfNull(request);
        request.Validate();
        lock (_gate)
        {
            if (_leaseActive)
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_BUSY",
                    "Codex 按应用音频路由事务已在运行",
                    retryable: true);
            }
            _leaseActive = true;
        }

        try
        {
            if (File.Exists(request.JournalPath))
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_RECOVERY_REQUIRED",
                    "上一次 Codex 音频路由事务尚未恢复",
                    retryable: true);
            }

            Dictionary<PerAppAudioRouteKey, PersistedAudioEndpoint>
                snapshot = SnapshotOrThrow(request.ProcessId);
            PerAppAudioRouteJournal.WriteNew(
                request,
                snapshot);
            try
            {
                ApplyAndReadBackOrThrow(request);
            }
            catch (Exception exception)
            {
                PerAppAudioRouteRestoreResult rollback =
                    RestoreSnapshot(request, snapshot);
                if (rollback.Succeeded)
                {
                    DeleteJournalOrThrow(request.JournalPath);
                    throw WrapApplyFailure(exception);
                }
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_ROLLBACK_INCOMPLETE",
                    "Codex 音频路由应用失败且回滚不完整",
                    retryable: true,
                    innerException: exception);
            }

            return new PerAppAudioRouteLease(
                request,
                snapshot,
                _backend,
                ReleaseLease);
        }
        catch
        {
            ReleaseLease();
            throw;
        }
    }

    internal PerAppAudioRouteRestoreResult RecoverPending(
        uint currentProcessId,
        string journalPath)
    {
        if (currentProcessId == 0)
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_REQUEST_INVALID",
                "Codex 按应用音频路由请求无效");
        }
        PerAppAudioRouteJournal.Loaded? pending =
            PerAppAudioRouteJournal.Load(
                currentProcessId,
                journalPath);
        if (pending is null)
        {
            return new(
                true,
                [],
                [],
                []);
        }

        lock (_gate)
        {
            if (_leaseActive)
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_BUSY",
                    "Codex 按应用音频路由事务已在运行",
                    retryable: true);
            }
            _leaseActive = true;
        }
        try
        {
            PerAppAudioRouteRestoreResult result = RestoreSnapshot(
                pending.Request,
                pending.Snapshot);
            if (result.Succeeded)
            {
                DeleteJournalOrThrow(journalPath);
            }
            return result;
        }
        finally
        {
            ReleaseLease();
        }
    }

    // A target the bridge just launched has not necessarily finished bringing
    // its audio policy online: the per-app store answers ERROR_NOT_FOUND as
    // Unset (a legitimate "nothing pinned yet"), but while the app is still
    // starting the read itself can fail outright. That is a transient cold
    // start condition, not a reason to abandon the whole call, so the snapshot
    // is retried within a bounded window. A target that was already running
    // succeeds on the first pass and pays nothing.
    private const int SnapshotAttempts = 8;
    private const int SnapshotRetryDelayMilliseconds = 400;

    private Dictionary<PerAppAudioRouteKey, PersistedAudioEndpoint>
        SnapshotOrThrow(uint processId)
    {
        PerAppAudioRouteKey failedKey = PerAppAudioRouteKey.All[0];
        int failedHResult = 0;
        for (int attempt = 0; attempt < SnapshotAttempts; attempt++)
        {
            if (attempt > 0)
            {
                Thread.Sleep(SnapshotRetryDelayMilliseconds);
            }
            Dictionary<PerAppAudioRouteKey, PersistedAudioEndpoint>
                snapshot = [];
            bool complete = true;
            foreach (PerAppAudioRouteKey key in PerAppAudioRouteKey.All)
            {
                PersistedAudioEndpoint value = SafeRead(processId, key);
                if (value.Kind == PersistedAudioEndpointKind.Error)
                {
                    failedKey = key;
                    failedHResult = value.HResult;
                    complete = false;
                    break;
                }
                snapshot.Add(key, value);
            }
            if (complete)
            {
                return snapshot;
            }
        }
        // Carry the failing key and HRESULT out instead of collapsing every
        // cause into one opaque code -- the stage lands in runtime status.
        throw new DirectProtocolException(
            "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_SNAPSHOT_FAILED",
            $"无法读取目标应用音频路由(key={failedKey}, "
                + $"hr=0x{unchecked((uint)failedHResult):X8})",
            retryable: true,
            innerException: new AudioCaptureStageException(
                "audio-route.snapshot-read",
                failedHResult));
    }

    private void ApplyAndReadBackOrThrow(
        PerAppAudioRouteRequest request)
    {
        foreach (PerAppAudioRouteKey key in PerAppAudioRouteKey.All)
        {
            string target = request.TargetFor(key);
            PerAppAudioPolicyWriteResult write = SafeWrite(
                request.ProcessId,
                key,
                target);
            if (!write.Succeeded)
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_SET_FAILED",
                    "无法设置 Codex 应用音频路由",
                    retryable: true);
            }
            PersistedAudioEndpoint readBack = SafeRead(
                request.ProcessId,
                key);
            if (!EndpointEqualsTarget(readBack, target))
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_READBACK_FAILED",
                    "Codex 应用音频路由读回不一致",
                    retryable: true);
            }
        }
    }

    private PerAppAudioRouteRestoreResult RestoreSnapshot(
        PerAppAudioRouteRequest request,
        IReadOnlyDictionary<
            PerAppAudioRouteKey,
            PersistedAudioEndpoint> snapshot) =>
        PerAppAudioRouteLease.RestoreSnapshot(
            _backend,
            request,
            snapshot);

    private PersistedAudioEndpoint SafeRead(
        uint processId,
        PerAppAudioRouteKey key)
    {
        try
        {
            return _backend.Read(processId, key);
        }
        catch (Exception exception)
        {
            return PersistedAudioEndpoint.Error(
                Marshal.GetHRForException(exception),
                "backend-read");
        }
    }

    private PerAppAudioPolicyWriteResult SafeWrite(
        uint processId,
        PerAppAudioRouteKey key,
        string? endpointId)
    {
        try
        {
            return _backend.Write(processId, key, endpointId);
        }
        catch (Exception exception)
        {
            return PerAppAudioPolicyWriteResult.Failure(
                Marshal.GetHRForException(exception),
                "backend-write");
        }
    }

    private void ReleaseLease()
    {
        lock (_gate)
        {
            _leaseActive = false;
        }
    }

    private static DirectProtocolException WrapApplyFailure(
        Exception exception) =>
        exception as DirectProtocolException
        ?? new DirectProtocolException(
            "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_SET_FAILED",
            "无法设置 Codex 应用音频路由",
            retryable: true,
            innerException: exception);

    internal static bool EndpointEqualsTarget(
        PersistedAudioEndpoint value,
        string target) =>
        value.Kind == PersistedAudioEndpointKind.Present
        && string.Equals(
            value.EndpointId,
            target,
            StringComparison.OrdinalIgnoreCase);

    internal static void DeleteJournalOrThrow(string journalPath)
    {
        try
        {
            File.Delete(journalPath);
        }
        catch (
            Exception exception
        ) when (
            exception is IOException
            or UnauthorizedAccessException
        )
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_JOURNAL_DELETE_FAILED",
                "Codex 音频路由事务日志无法清除",
                retryable: true,
                innerException: exception);
        }
    }
}

internal sealed class PerAppAudioRouteLease : IDisposable
{
    private readonly PerAppAudioRouteRequest _request;
    private readonly IReadOnlyDictionary<
        PerAppAudioRouteKey,
        PersistedAudioEndpoint> _snapshot;
    private readonly IPerAppAudioPolicyBackend _backend;
    private readonly Action _release;
    private readonly object _gate = new();
    private PerAppAudioRouteRestoreResult? _result;

    internal PerAppAudioRouteLease(
        PerAppAudioRouteRequest request,
        IReadOnlyDictionary<
            PerAppAudioRouteKey,
            PersistedAudioEndpoint> snapshot,
        IPerAppAudioPolicyBackend backend,
        Action release)
    {
        _request = request;
        _snapshot = snapshot;
        _backend = backend;
        _release = release;
        AlreadyTargetedBeforeAcquire =
            PerAppAudioRouteKey.All.All(key =>
                PerAppAudioRouteController.EndpointEqualsTarget(
                    snapshot[key],
                    request.TargetFor(key)));
    }

    internal bool AlreadyTargetedBeforeAcquire { get; }

    internal PerAppAudioRouteRestoreResult Restore()
    {
        lock (_gate)
        {
            if (_result is not null)
            {
                return _result;
            }
            PerAppAudioRouteRestoreResult result = RestoreSnapshot(
                _backend,
                _request,
                _snapshot);
            if (result.Succeeded)
            {
                try
                {
                    PerAppAudioRouteController.DeleteJournalOrThrow(
                        _request.JournalPath);
                }
                catch (DirectProtocolException)
                {
                    result = result with
                    {
                        Succeeded = false,
                        Failed = PerAppAudioRouteKey.All.ToArray(),
                    };
                }
            }
            if (result.Succeeded)
            {
                _result = result;
                _release();
            }
            return result;
        }
    }

    internal void RequireStillApplied()
    {
        lock (_gate)
        {
            if (_result is not null)
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_LEASE_ENDED",
                    "Codex 按应用音频路由租约已结束");
            }
            foreach (PerAppAudioRouteKey key in PerAppAudioRouteKey.All)
            {
                PersistedAudioEndpoint current;
                try
                {
                    current = _backend.Read(_request.ProcessId, key);
                }
                catch (Exception exception)
                {
                    throw new DirectProtocolException(
                        "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_VERIFY_FAILED",
                        "无法复核 Codex 应用音频路由",
                        retryable: true,
                        innerException: exception);
                }
                if (
                    !PerAppAudioRouteController.EndpointEqualsTarget(
                        current,
                        _request.TargetFor(key))
                )
                {
                    throw new DirectProtocolException(
                        "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_CHANGED",
                        "快捷键发送前 Codex 应用音频路由已变化",
                        retryable: true);
                }
            }
        }
    }

    public void Dispose()
    {
        _ = Restore();
    }

    internal static PerAppAudioRouteRestoreResult RestoreSnapshot(
        IPerAppAudioPolicyBackend backend,
        PerAppAudioRouteRequest request,
        IReadOnlyDictionary<
            PerAppAudioRouteKey,
            PersistedAudioEndpoint> snapshot)
    {
        List<PerAppAudioRouteKey> restored = [];
        List<PerAppAudioRouteKey> preserved = [];
        List<PerAppAudioRouteKey> failed = [];

        foreach (
            PerAppAudioRouteKey key
            in PerAppAudioRouteKey.All.Reverse()
        )
        {
            string target = request.TargetFor(key);
            PersistedAudioEndpoint current;
            try
            {
                current = backend.Read(request.ProcessId, key);
            }
            catch
            {
                failed.Add(key);
                continue;
            }
            if (current.Kind == PersistedAudioEndpointKind.Error)
            {
                failed.Add(key);
                continue;
            }
            if (
                !PerAppAudioRouteController.EndpointEqualsTarget(
                    current,
                    target)
            )
            {
                preserved.Add(key);
                continue;
            }

            PersistedAudioEndpoint original = snapshot[key];
            string? restoreEndpoint = original.Kind switch
            {
                PersistedAudioEndpointKind.Present =>
                    original.EndpointId,
                PersistedAudioEndpointKind.Unset => null,
                _ => throw new InvalidOperationException(
                    "A route snapshot cannot contain an error"),
            };
            PerAppAudioPolicyWriteResult write;
            try
            {
                write = backend.Write(
                    request.ProcessId,
                    key,
                    restoreEndpoint);
            }
            catch
            {
                failed.Add(key);
                continue;
            }
            if (!write.Succeeded)
            {
                failed.Add(key);
                continue;
            }

            PersistedAudioEndpoint readBack;
            try
            {
                readBack = backend.Read(request.ProcessId, key);
            }
            catch
            {
                failed.Add(key);
                continue;
            }
            if (!Equivalent(readBack, original))
            {
                failed.Add(key);
                continue;
            }
            restored.Add(key);
        }

        return new(
            failed.Count == 0,
            restored,
            preserved,
            failed);
    }

    private static bool Equivalent(
        PersistedAudioEndpoint left,
        PersistedAudioEndpoint right) =>
        left.Kind == right.Kind
        && left.Kind switch
        {
            PersistedAudioEndpointKind.Present => string.Equals(
                left.EndpointId,
                right.EndpointId,
                StringComparison.OrdinalIgnoreCase),
            PersistedAudioEndpointKind.Unset => true,
            _ => false,
        };
}

internal static class VirtualCaptureEndpointProbe
{
    internal static void ValidateExactActiveCapture(string endpointId)
    {
        AudioPolicyEndpointId.ValidateForFlow(
            endpointId,
            PerAppAudioDataFlow.Capture);
        IReadOnlyList<DirectMicrophoneEndpoint> active =
            DirectMicrophoneDiscovery.EnumerateActive();
        if (!active.Any(endpoint => string.Equals(
            endpoint.EndpointId,
            endpointId,
            StringComparison.Ordinal)))
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_CAPTURE_ENDPOINT_INACTIVE",
                "虚拟麦克风录音端点不可用",
                retryable: true);
        }
    }
}

internal static class PerAppAudioRouteJournal
{
    private const string Contract =
        "reader-computer-voice-audio-route-transaction/1";
    private const int MaximumJournalBytes = 64 * 1024;

    internal sealed record Loaded(
        PerAppAudioRouteRequest Request,
        IReadOnlyDictionary<
            PerAppAudioRouteKey,
            PersistedAudioEndpoint> Snapshot);

    internal static void WriteNew(
        PerAppAudioRouteRequest request,
        IReadOnlyDictionary<
            PerAppAudioRouteKey,
            PersistedAudioEndpoint> snapshot)
    {
        string fullPath = System.IO.Path.GetFullPath(
            request.JournalPath);
        string? directory = System.IO.Path.GetDirectoryName(fullPath);
        if (directory is null)
        {
            throw JournalFailure();
        }
        string temporary = fullPath
            + "."
            + Guid.NewGuid().ToString("N")
            + ".tmp";
        try
        {
            Directory.CreateDirectory(directory);
            if (File.Exists(fullPath))
            {
                throw new IOException(
                    "Audio route journal already exists");
            }
            object value = new
            {
                contract = Contract,
                processId = request.ProcessId,
                createdAtUtc = DateTimeOffset.UtcNow,
                renderTargetEndpointId =
                    request.RenderEndpointId,
                captureTargetEndpointId =
                    request.CaptureEndpointId,
                routes = PerAppAudioRouteKey.All.Select(key =>
                {
                    PersistedAudioEndpoint original = snapshot[key];
                    return new
                    {
                        flow = key.FlowName,
                        role = key.RoleName,
                        targetEndpointId = request.TargetFor(key),
                        original = original.Kind switch
                        {
                            PersistedAudioEndpointKind.Present =>
                                new
                                {
                                    kind = "present",
                                    endpointId =
                                        original.EndpointId,
                                },
                            PersistedAudioEndpointKind.Unset =>
                                new
                                {
                                    kind = "unset",
                                    endpointId = (string?)null,
                                },
                            _ => throw new InvalidOperationException(
                                "A route journal cannot contain an error"),
                        },
                    };
                }).ToArray(),
            };
            byte[] encoded = Encoding.UTF8.GetBytes(
                JsonSerializer.Serialize(
                    value,
                    DirectBridgeContract.JsonOptions));
            using (
                FileStream stream = new(
                    temporary,
                    FileMode.CreateNew,
                    FileAccess.Write,
                    FileShare.None,
                    bufferSize: 4096,
                    FileOptions.WriteThrough)
            )
            {
                stream.Write(encoded);
                stream.Flush(flushToDisk: true);
            }
            File.Move(temporary, fullPath, overwrite: false);
        }
        catch (
            Exception exception
        ) when (
            exception is IOException
            or UnauthorizedAccessException
            or ArgumentException
            or JsonException
        )
        {
            throw JournalFailure(exception);
        }
        finally
        {
            try
            {
                File.Delete(temporary);
            }
            catch
            {
                // The authoritative destination is never replaced here.
            }
        }
    }

    internal static Loaded? Load(
        uint currentProcessId,
        string journalPath)
    {
        if (
            !System.IO.Path.IsPathFullyQualified(journalPath)
            || !string.Equals(
                System.IO.Path.GetExtension(journalPath),
                ".json",
                StringComparison.OrdinalIgnoreCase)
        )
        {
            throw JournalInvalid();
        }
        FileInfo info = new(journalPath);
        if (!info.Exists)
        {
            return null;
        }
        if (info.Length is <= 0 or > MaximumJournalBytes)
        {
            throw JournalInvalid();
        }

        try
        {
            using JsonDocument document = JsonDocument.Parse(
                File.ReadAllText(journalPath, Encoding.UTF8),
                new JsonDocumentOptions
                {
                    AllowTrailingCommas = false,
                    CommentHandling = JsonCommentHandling.Disallow,
                    MaxDepth = 8,
                });
            JsonElement root = document.RootElement;
            RequireExactKeys(
                root,
                "contract",
                "processId",
                "createdAtUtc",
                "renderTargetEndpointId",
                "captureTargetEndpointId",
                "routes");
            if (
                RequireString(root, "contract")
                != Contract
                || !root.TryGetProperty(
                    "processId",
                    out JsonElement oldProcess)
                || !oldProcess.TryGetUInt32(out uint oldProcessId)
                || oldProcessId == 0
                || !root.TryGetProperty(
                    "createdAtUtc",
                    out JsonElement created)
                || created.ValueKind != JsonValueKind.String
                || !created.TryGetDateTimeOffset(out _)
            )
            {
                throw JournalInvalid();
            }
            string renderTarget = RequireString(
                root,
                "renderTargetEndpointId");
            string captureTarget = RequireString(
                root,
                "captureTargetEndpointId");
            PerAppAudioRouteRequest request = new(
                currentProcessId,
                renderTarget,
                captureTarget,
                System.IO.Path.GetFullPath(journalPath));
            request.Validate();

            JsonElement routes = root.GetProperty("routes");
            if (
                routes.ValueKind != JsonValueKind.Array
                || routes.GetArrayLength()
                    != PerAppAudioRouteKey.All.Count
            )
            {
                throw JournalInvalid();
            }
            Dictionary<
                PerAppAudioRouteKey,
                PersistedAudioEndpoint> snapshot = [];
            foreach (JsonElement route in routes.EnumerateArray())
            {
                RequireExactKeys(
                    route,
                    "flow",
                    "role",
                    "targetEndpointId",
                    "original");
                PerAppAudioRouteKey key = new(
                    ParseFlow(RequireString(route, "flow")),
                    ParseRole(RequireString(route, "role")));
                if (
                    !PerAppAudioRouteKey.All.Contains(key)
                    || !snapshot.TryAdd(
                        key,
                        ParseOriginal(
                            route.GetProperty("original"),
                            key.Flow))
                    || !string.Equals(
                        RequireString(route, "targetEndpointId"),
                        request.TargetFor(key),
                        StringComparison.OrdinalIgnoreCase)
                )
                {
                    throw JournalInvalid();
                }
            }
            if (
                snapshot.Count != PerAppAudioRouteKey.All.Count
                || PerAppAudioRouteKey.All.Any(
                    key => !snapshot.ContainsKey(key))
            )
            {
                throw JournalInvalid();
            }
            return new(request, snapshot);
        }
        catch (DirectProtocolException exception)
            when (
                exception.Code
                != "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_JOURNAL_INVALID"
            )
        {
            throw JournalInvalid(exception);
        }
        catch (
            Exception exception
        ) when (
            exception is IOException
            or UnauthorizedAccessException
            or JsonException
            or ArgumentException
            or FormatException
            or InvalidOperationException
        )
        {
            throw JournalInvalid(exception);
        }
    }

    private static PersistedAudioEndpoint ParseOriginal(
        JsonElement original,
        PerAppAudioDataFlow flow)
    {
        RequireExactKeys(original, "kind", "endpointId");
        string kind = RequireString(original, "kind");
        if (kind == "unset")
        {
            if (
                !original.TryGetProperty(
                    "endpointId",
                    out JsonElement endpoint)
                || endpoint.ValueKind != JsonValueKind.Null
            )
            {
                throw JournalInvalid();
            }
            return PersistedAudioEndpoint.Unset();
        }
        if (
            kind != "present"
            || original.GetProperty("endpointId").ValueKind
                != JsonValueKind.String
        )
        {
            throw JournalInvalid();
        }
        string endpointId = RequireString(
            original,
            "endpointId");
        AudioPolicyEndpointId.ValidateForFlow(endpointId, flow);
        return PersistedAudioEndpoint.Present(endpointId);
    }

    private static PerAppAudioDataFlow ParseFlow(string value) =>
        value switch
        {
            "render" => PerAppAudioDataFlow.Render,
            "capture" => PerAppAudioDataFlow.Capture,
            _ => throw JournalInvalid(),
        };

    private static PerAppAudioRole ParseRole(string value) =>
        value switch
        {
            "console" => PerAppAudioRole.Console,
            "multimedia" => PerAppAudioRole.Multimedia,
            "communications" => PerAppAudioRole.Communications,
            _ => throw JournalInvalid(),
        };

    private static string RequireString(
        JsonElement value,
        string name)
    {
        if (
            !value.TryGetProperty(name, out JsonElement property)
            || property.ValueKind != JsonValueKind.String
            || property.GetString() is not string result
            || result.Length is < 1 or > 2048
        )
        {
            throw JournalInvalid();
        }
        return result;
    }

    private static void RequireExactKeys(
        JsonElement value,
        params string[] expected)
    {
        if (value.ValueKind != JsonValueKind.Object)
        {
            throw JournalInvalid();
        }
        HashSet<string> actual = value.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        if (!actual.SetEquals(expected))
        {
            throw JournalInvalid();
        }
    }

    private static DirectProtocolException JournalFailure(
        Exception? inner = null) =>
        new(
            "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_JOURNAL_FAILED",
            "无法持久化 Codex 音频路由事务日志",
            retryable: true,
            innerException: inner);

    private static DirectProtocolException JournalInvalid(
        Exception? inner = null) =>
        new(
            "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_JOURNAL_INVALID",
            "Codex 音频路由事务日志无效",
            retryable: true,
            innerException: inner);
}

internal static class AudioPolicyEndpointId
{
    private const int MaximumEndpointIdLength = 1024;
    private const string MmDevicePrefix = @"\\?\SWD#MMDEVAPI#";
    private const string RenderSuffix =
        "#{e6327cad-dcec-4949-ae8a-991e976a79d2}";
    private const string CaptureSuffix =
        "#{2eef81be-33fa-4800-9670-1cd474972c3f}";
    private const string RenderEndpointPrefix =
        "{0.0.0.00000000}.";
    private const string CaptureEndpointPrefix =
        "{0.0.1.00000000}.";

    internal static void ValidateCanonical(string endpointId)
    {
        if (
            string.IsNullOrWhiteSpace(endpointId)
            || endpointId.Length > MaximumEndpointIdLength
            || endpointId.Any(char.IsControl)
        )
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_ENDPOINT_INVALID",
                "Codex 按应用音频端点无效");
        }
    }

    internal static void ValidateForFlow(
        string endpointId,
        PerAppAudioDataFlow flow)
    {
        ValidateCanonical(endpointId);
        string prefix = flow switch
        {
            PerAppAudioDataFlow.Render => RenderEndpointPrefix,
            PerAppAudioDataFlow.Capture => CaptureEndpointPrefix,
            _ => throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_ENDPOINT_INVALID",
                "Codex 按应用音频端点无效"),
        };
        string deviceGuid = endpointId.StartsWith(
                prefix,
                StringComparison.OrdinalIgnoreCase)
            ? endpointId[prefix.Length..]
            : "";
        if (
            !Guid.TryParseExact(
                deviceGuid,
                "B",
                out _)
            || deviceGuid.Length != 38
        )
        {
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_ENDPOINT_FLOW_MISMATCH",
                "Codex 按应用音频端点方向不匹配");
        }
    }

    internal static string Pack(
        string endpointId,
        PerAppAudioDataFlow flow)
    {
        ValidateForFlow(endpointId, flow);
        return MmDevicePrefix
            + endpointId
            + Suffix(flow);
    }

    internal static bool TryUnpack(
        string? packed,
        PerAppAudioDataFlow flow,
        out string endpointId)
    {
        endpointId = "";
        if (
            string.IsNullOrEmpty(packed)
            || !packed.StartsWith(
                MmDevicePrefix,
                StringComparison.OrdinalIgnoreCase)
        )
        {
            return false;
        }
        string suffix = Suffix(flow);
        if (!packed.EndsWith(
            suffix,
            StringComparison.OrdinalIgnoreCase))
        {
            return false;
        }
        int length = packed.Length
            - MmDevicePrefix.Length
            - suffix.Length;
        if (length <= 0)
        {
            return false;
        }
        string unpacked = packed.Substring(
            MmDevicePrefix.Length,
            length);
        try
        {
            ValidateForFlow(unpacked, flow);
        }
        catch (DirectProtocolException)
        {
            return false;
        }
        endpointId = unpacked;
        return true;
    }

    private static string Suffix(PerAppAudioDataFlow flow) =>
        flow switch
        {
            PerAppAudioDataFlow.Render => RenderSuffix,
            PerAppAudioDataFlow.Capture => CaptureSuffix,
            _ => throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_ENDPOINT_INVALID",
                "Codex 按应用音频端点无效"),
        };
}

[SupportedOSPlatform("windows")]
internal sealed class NativePerAppAudioPolicyBackend :
    IPerAppAudioPolicyBackend
{
    private readonly BlockingCollection<
        Action<NativeAudioPolicyConfigSession>> _work = new();
    private readonly TaskCompletionSource<Exception?> _ready =
        new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly Thread _thread;
    private int _disposed;

    internal NativePerAppAudioPolicyBackend()
    {
        if (!OperatingSystem.IsWindows())
        {
            throw new PlatformNotSupportedException(
                "Per-app audio routing requires Windows");
        }
        _thread = new Thread(Worker)
        {
            IsBackground = true,
            Name = "BW per-app audio policy",
        };
        _thread.Start();
        Exception? failure = _ready.Task.GetAwaiter().GetResult();
        if (failure is not null)
        {
            _work.Dispose();
            throw new DirectProtocolException(
                "BW_COMPUTER_VOICE_DIRECT_AUDIO_POLICY_UNAVAILABLE",
                "Windows 按应用音频策略接口不可用",
                retryable: true,
                innerException: failure);
        }
    }

    public PersistedAudioEndpoint Read(
        uint processId,
        PerAppAudioRouteKey key) =>
        Invoke(session => session.Read(processId, key));

    public PerAppAudioPolicyWriteResult Write(
        uint processId,
        PerAppAudioRouteKey key,
        string? endpointId) =>
        Invoke(session => session.Write(
            processId,
            key,
            endpointId));

    public void Dispose()
    {
        if (Interlocked.Exchange(ref _disposed, 1) != 0)
        {
            return;
        }
        _work.CompleteAdding();
        if (_thread.Join(TimeSpan.FromSeconds(5)))
        {
            _work.Dispose();
        }
    }

    private T Invoke<T>(
        Func<NativeAudioPolicyConfigSession, T> action)
    {
        ObjectDisposedException.ThrowIf(
            Volatile.Read(ref _disposed) != 0,
            this);
        TaskCompletionSource<T> result =
            new(TaskCreationOptions.RunContinuationsAsynchronously);
        try
        {
            _work.Add(session =>
            {
                try
                {
                    result.TrySetResult(action(session));
                }
                catch (Exception exception)
                {
                    result.TrySetException(exception);
                }
            });
        }
        catch (InvalidOperationException)
        {
            throw new ObjectDisposedException(
                nameof(NativePerAppAudioPolicyBackend));
        }
        return result.Task.GetAwaiter().GetResult();
    }

    private void Worker()
    {
        bool initialized = false;
        try
        {
            int initializeResult =
                NativeAudioPolicyConfigSession.RoInitialize(
                    1 /* RO_INIT_MULTITHREADED */);
            if (initializeResult < 0)
            {
                Marshal.ThrowExceptionForHR(initializeResult);
            }
            initialized = true;
            using NativeAudioPolicyConfigSession session = new();
            _ready.TrySetResult(null);
            foreach (
                Action<NativeAudioPolicyConfigSession> action
                in _work.GetConsumingEnumerable()
            )
            {
                action(session);
            }
        }
        catch (Exception exception)
        {
            _ready.TrySetResult(exception);
        }
        finally
        {
            if (initialized)
            {
                NativeAudioPolicyConfigSession.RoUninitialize();
            }
        }
    }
}

[SupportedOSPlatform("windows")]
internal sealed class NativeAudioPolicyConfigSession : IDisposable
{
    private const string RuntimeClass =
        "Windows.Media.Internal.AudioPolicyConfig";
    private const int ErrorNotFoundHResult =
        unchecked((int)0x80070490);
    // IUnknown (3) + IInspectable (3) + the 19 methods preceding
    // SetPersistedDefaultAudioEndpoint in the 21H2 contract.
    private const int SetPersistedDefaultAudioEndpointSlot = 25;
    private const int GetPersistedDefaultAudioEndpointSlot = 26;
    private static readonly Guid AudioPolicyConfigFactoryIid =
        new("ab3d4648-e242-459f-b02f-541c70306324");
    private IntPtr _factoryPointer;

    internal NativeAudioPolicyConfigSession()
    {
        IntPtr className = IntPtr.Zero;
        IntPtr factoryPointer = IntPtr.Zero;
        try
        {
            ThrowIfFailed(WindowsCreateString(
                RuntimeClass,
                (uint)RuntimeClass.Length,
                out className));
            Guid iid = AudioPolicyConfigFactoryIid;
            ThrowIfFailed(RoGetActivationFactory(
                className,
                ref iid,
                out factoryPointer));
            // .NET 8's built-in COM marshaller rejects InterfaceIsIInspectable
            // RCWs. Retain the queried ABI pointer and call the two required
            // methods through their stable vtable slots instead.
            _factoryPointer = factoryPointer;
            factoryPointer = IntPtr.Zero;
        }
        finally
        {
            if (factoryPointer != IntPtr.Zero)
            {
                Marshal.Release(factoryPointer);
            }
            if (className != IntPtr.Zero)
            {
                _ = WindowsDeleteString(className);
            }
        }
    }

    internal PersistedAudioEndpoint Read(
        uint processId,
        PerAppAudioRouteKey key)
    {
        IntPtr factoryPointer = _factoryPointer;
        if (factoryPointer == IntPtr.Zero)
        {
            throw new ObjectDisposedException(
                nameof(NativeAudioPolicyConfigSession));
        }
        GetPersistedDefaultAudioEndpointDelegate getPersisted =
            VtableDelegate<GetPersistedDefaultAudioEndpointDelegate>(
                factoryPointer,
                GetPersistedDefaultAudioEndpointSlot);
        IntPtr value = IntPtr.Zero;
        int hresult;
        try
        {
            hresult = getPersisted(
                factoryPointer,
                processId,
                key.Flow,
                key.Role,
                out value);
            if (hresult == ErrorNotFoundHResult)
            {
                return PersistedAudioEndpoint.Unset();
            }
            if (hresult < 0)
            {
                return PersistedAudioEndpoint.Error(
                    hresult,
                    "audio-policy-get");
            }
            if (value == IntPtr.Zero)
            {
                return PersistedAudioEndpoint.Unset();
            }
            IntPtr buffer = WindowsGetStringRawBuffer(
                value,
                out uint length);
            if (buffer == IntPtr.Zero || length == 0)
            {
                return PersistedAudioEndpoint.Unset();
            }
            string? packed = Marshal.PtrToStringUni(
                buffer,
                checked((int)length));
            if (!AudioPolicyEndpointId.TryUnpack(
                packed,
                key.Flow,
                out string endpointId))
            {
                return PersistedAudioEndpoint.Error(
                    unchecked((int)0x8007000D),
                    "audio-policy-unpack");
            }
            return PersistedAudioEndpoint.Present(endpointId);
        }
        catch (Exception exception)
        {
            return PersistedAudioEndpoint.Error(
                Marshal.GetHRForException(exception),
                "audio-policy-get");
        }
        finally
        {
            if (value != IntPtr.Zero)
            {
                _ = WindowsDeleteString(value);
            }
        }
    }

    internal PerAppAudioPolicyWriteResult Write(
        uint processId,
        PerAppAudioRouteKey key,
        string? endpointId)
    {
        IntPtr factoryPointer = _factoryPointer;
        if (factoryPointer == IntPtr.Zero)
        {
            throw new ObjectDisposedException(
                nameof(NativeAudioPolicyConfigSession));
        }
        SetPersistedDefaultAudioEndpointDelegate setPersisted =
            VtableDelegate<SetPersistedDefaultAudioEndpointDelegate>(
                factoryPointer,
                SetPersistedDefaultAudioEndpointSlot);
        IntPtr value = IntPtr.Zero;
        try
        {
            if (endpointId is not null)
            {
                string packed = AudioPolicyEndpointId.Pack(
                    endpointId,
                    key.Flow);
                int createResult = WindowsCreateString(
                    packed,
                    (uint)packed.Length,
                    out value);
                if (createResult < 0)
                {
                    return PerAppAudioPolicyWriteResult.Failure(
                        createResult,
                        "audio-policy-create-string");
                }
            }
            int hresult = setPersisted(
                factoryPointer,
                processId,
                key.Flow,
                key.Role,
                value);
            return hresult >= 0
                ? PerAppAudioPolicyWriteResult.Success()
                : PerAppAudioPolicyWriteResult.Failure(
                    hresult,
                    "audio-policy-set");
        }
        catch (Exception exception)
        {
            return PerAppAudioPolicyWriteResult.Failure(
                Marshal.GetHRForException(exception),
                "audio-policy-set");
        }
        finally
        {
            if (value != IntPtr.Zero)
            {
                _ = WindowsDeleteString(value);
            }
        }
    }

    public void Dispose()
    {
        IntPtr factoryPointer = Interlocked.Exchange(
            ref _factoryPointer,
            IntPtr.Zero);
        if (factoryPointer != IntPtr.Zero)
        {
            Marshal.Release(factoryPointer);
        }
    }

    private static T VtableDelegate<T>(
        IntPtr instance,
        int slot)
        where T : Delegate
    {
        IntPtr vtable = Marshal.ReadIntPtr(instance);
        IntPtr method = Marshal.ReadIntPtr(
            vtable,
            checked(slot * IntPtr.Size));
        return Marshal.GetDelegateForFunctionPointer<T>(method);
    }

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate int SetPersistedDefaultAudioEndpointDelegate(
        IntPtr instance,
        uint processId,
        PerAppAudioDataFlow flow,
        PerAppAudioRole role,
        IntPtr deviceId);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate int GetPersistedDefaultAudioEndpointDelegate(
        IntPtr instance,
        uint processId,
        PerAppAudioDataFlow flow,
        PerAppAudioRole role,
        out IntPtr deviceId);

    private static void ThrowIfFailed(int hresult)
    {
        if (hresult < 0)
        {
            Marshal.ThrowExceptionForHR(hresult);
        }
    }

    [DllImport("combase.dll", ExactSpelling = true)]
    internal static extern int RoInitialize(uint initType);

    [DllImport("combase.dll", ExactSpelling = true)]
    internal static extern void RoUninitialize();

    [DllImport("combase.dll", ExactSpelling = true)]
    private static extern int RoGetActivationFactory(
        IntPtr activatableClassId,
        ref Guid iid,
        out IntPtr factory);

    [DllImport("combase.dll", ExactSpelling = true)]
    private static extern int WindowsCreateString(
        [MarshalAs(UnmanagedType.LPWStr)] string source,
        uint length,
        out IntPtr value);

    [DllImport("combase.dll", ExactSpelling = true)]
    private static extern int WindowsDeleteString(IntPtr value);

    [DllImport("combase.dll", ExactSpelling = true)]
    private static extern IntPtr WindowsGetStringRawBuffer(
        IntPtr value,
        out uint length);

}
