using Microsoft.Win32;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Runtime.Versioning;
using System.Security;
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

    internal string RegistryValueName
    {
        get
        {
            string role = Role switch
            {
                PerAppAudioRole.Console => "000",
                PerAppAudioRole.Multimedia => "001",
                PerAppAudioRole.Communications => "002",
                _ => throw new InvalidOperationException(
                    "Unknown per-app audio role"),
            };
            string flow = Flow switch
            {
                PerAppAudioDataFlow.Render => "000",
                PerAppAudioDataFlow.Capture => "001",
                _ => throw new InvalidOperationException(
                    "Unknown per-app audio data flow"),
            };
            return role + "_" + flow;
        }
    }
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

    private Dictionary<PerAppAudioRouteKey, PersistedAudioEndpoint>
        SnapshotOrThrow(uint processId)
    {
        Dictionary<PerAppAudioRouteKey, PersistedAudioEndpoint>
            snapshot = [];
        foreach (PerAppAudioRouteKey key in PerAppAudioRouteKey.All)
        {
            PersistedAudioEndpoint value = SafeRead(processId, key);
            if (value.Kind == PersistedAudioEndpointKind.Error)
            {
                throw new DirectProtocolException(
                    "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_SNAPSHOT_FAILED",
                    "无法读取 Codex 原应用音频路由",
                    retryable: true);
            }
            snapshot.Add(key, value);
        }
        return snapshot;
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
    private int _disposed;

    internal NativePerAppAudioPolicyBackend()
    {
        if (!OperatingSystem.IsWindows())
        {
            throw new PlatformNotSupportedException(
                "Per-app audio routing requires Windows");
        }
    }

    public PersistedAudioEndpoint Read(
        uint processId,
        PerAppAudioRouteKey key)
    {
        ThrowIfDisposed();
        return WindowsPersistedAppAudioRouteStore.Read(
            processId,
            key);
    }

    public PerAppAudioPolicyWriteResult Write(
        uint processId,
        PerAppAudioRouteKey key,
        string? endpointId)
    {
        ThrowIfDisposed();
        return WindowsPersistedAppAudioRouteStore.Write(
            processId,
            key,
            endpointId);
    }

    public void Dispose()
    {
        _ = Interlocked.Exchange(ref _disposed, 1);
    }

    private void ThrowIfDisposed() =>
        ObjectDisposedException.ThrowIf(
            Volatile.Read(ref _disposed) != 0,
            this);
}

[SupportedOSPlatform("windows")]
internal static class WindowsPersistedAppAudioRouteStore
{
    internal const string RegistryPath =
        @"Software\Microsoft\Multimedia\Audio\DefaultEndpoint";
    internal const string EndpointPropertySetIdValueName =
        "{9637b4b9-11ee-4c35-b43c-7b2452c993cc},1";
    private const string MmDevicesRegistryPath =
        @"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio";
    private const uint ProcessQueryLimitedInformation = 0x1000;
    private const int ErrorInsufficientBuffer = 122;

    [SupportedOSPlatform("windows")]
    internal static PersistedAudioEndpoint Read(
        uint processId,
        PerAppAudioRouteKey key)
    {
        if (processId == 0)
        {
            return PersistedAudioEndpoint.Error(
                unchecked((int)0x80070057),
                "audio-policy-app-identity");
        }

        try
        {
            using RegistryKey? app = OpenUniqueAppKey(
                processId,
                writable: false,
                createIfMissing: false);
            if (app is null)
            {
                // A freshly reset Windows volume mixer has no per-app key at
                // all. That means every persisted route is unset, not corrupt.
                // The first journaled Write creates the deterministic identity
                // key before adding any endpoint pair.
                return PersistedAudioEndpoint.Unset();
            }
            string valueName = key.RegistryValueName;
            object? raw = app.GetValue(
                valueName,
                defaultValue: null,
                RegistryValueOptions.DoNotExpandEnvironmentNames);
            object? rawPropertySetId = app.GetValue(
                valueName + "_p",
                defaultValue: null,
                RegistryValueOptions.DoNotExpandEnvironmentNames);
            if (raw is null && rawPropertySetId is null)
            {
                return PersistedAudioEndpoint.Unset();
            }
            if (
                raw is not string packed
                || rawPropertySetId is not string propertySetId
                || !AudioPolicyEndpointId.TryUnpack(
                    packed,
                    key.Flow,
                    out string endpointId)
                || !string.Equals(
                    propertySetId,
                    ReadEndpointPropertySetId(endpointId, key.Flow),
                    StringComparison.OrdinalIgnoreCase)
            )
            {
                return PersistedAudioEndpoint.Error(
                    unchecked((int)0x8007000D),
                    "audio-policy-registry-unpack");
            }
            return PersistedAudioEndpoint.Present(endpointId);
        }
        catch (Exception exception) when (
            exception is IOException
            or UnauthorizedAccessException
            or SecurityException
            or ArgumentException
            or ObjectDisposedException
            or InvalidOperationException
            or COMException
        )
        {
            return PersistedAudioEndpoint.Error(
                Marshal.GetHRForException(exception),
                "audio-policy-registry-read");
        }
    }

    [SupportedOSPlatform("windows")]
    internal static PerAppAudioPolicyWriteResult Write(
        uint processId,
        PerAppAudioRouteKey key,
        string? endpointId)
    {
        string valueName = key.RegistryValueName;
        try
        {
            using RegistryKey app = OpenUniqueAppKey(
                processId,
                writable: true,
                createIfMissing: true)
                ?? throw new InvalidOperationException(
                    "The per-app audio identity cannot be provisioned");
            RegistryValueSnapshot originalValue =
                RegistryValueSnapshot.Read(
                app,
                valueName);
            RegistryValueSnapshot originalPropertySetId =
                RegistryValueSnapshot.Read(
                app,
                valueName + "_p");
            RequireStringOrMissing(originalValue);
            RequireStringOrMissing(originalPropertySetId);

            try
            {
                if (endpointId is null)
                {
                    app.DeleteValue(
                        valueName,
                        throwOnMissingValue: false);
                    app.DeleteValue(
                        valueName + "_p",
                        throwOnMissingValue: false);
                }
                else
                {
                    string packed = AudioPolicyEndpointId.Pack(
                        endpointId,
                        key.Flow);
                    string propertySetId = ReadEndpointPropertySetId(
                        endpointId,
                        key.Flow);
                    app.SetValue(
                        valueName + "_p",
                        propertySetId,
                        RegistryValueKind.String);
                    app.SetValue(
                        valueName,
                        packed,
                        RegistryValueKind.String);
                }
                app.Flush();
                if (!PostconditionMatches(
                    app,
                    valueName,
                    key,
                    endpointId))
                {
                    throw new InvalidOperationException(
                        "The per-app audio route readback mismatched");
                }
                return PerAppAudioPolicyWriteResult.Success();
            }
            catch (Exception exception) when (
                exception is IOException
                or UnauthorizedAccessException
                or SecurityException
                or ArgumentException
                or ObjectDisposedException
                or InvalidOperationException
                or COMException
            )
            {
                try
                {
                    RestorePair(
                        app,
                        valueName,
                        originalValue,
                        originalPropertySetId);
                }
                catch
                {
                    return PerAppAudioPolicyWriteResult.Failure(
                        unchecked((int)0x80004005),
                        "audio-policy-registry-write-rollback");
                }
                return PerAppAudioPolicyWriteResult.Failure(
                    Marshal.GetHRForException(exception),
                    "audio-policy-registry-write");
            }
        }
        catch (Exception exception) when (
            exception is IOException
            or UnauthorizedAccessException
            or SecurityException
            or ArgumentException
            or ObjectDisposedException
            or InvalidOperationException
            or COMException
        )
        {
            return PerAppAudioPolicyWriteResult.Failure(
                Marshal.GetHRForException(exception),
                "audio-policy-registry-write");
        }
    }

    private static RegistryKey? OpenUniqueAppKey(
        uint processId,
        bool writable,
        bool createIfMissing)
    {
        string identity = ReadApplicationUserModelId(processId);
        using RegistryKey? root = createIfMissing
            ? Registry.CurrentUser.CreateSubKey(
                RegistryPath,
                writable: true)
            : Registry.CurrentUser.OpenSubKey(
                RegistryPath,
                writable: false);
        if (root is null)
        {
            return null;
        }

        string? matchingSubKey = null;
        foreach (string subKeyName in root.GetSubKeyNames())
        {
            using RegistryKey? candidate = root.OpenSubKey(
                subKeyName,
                writable: false);
            string? candidateIdentity = candidate?.GetValue(
                "",
                defaultValue: null,
                RegistryValueOptions.DoNotExpandEnvironmentNames)
                as string;
            if (!string.Equals(
                candidateIdentity,
                identity,
                StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }
            if (matchingSubKey is not null)
            {
                throw new InvalidOperationException(
                    "The per-app audio identity is ambiguous");
            }
            matchingSubKey = subKeyName;
        }
        if (matchingSubKey is null)
        {
            if (!createIfMissing)
            {
                return null;
            }
            matchingSubKey = ProvisionIdentityKey(root, identity);
        }
        return Registry.CurrentUser.OpenSubKey(
            RegistryPath + "\\" + matchingSubKey,
            writable)
            ?? throw new InvalidOperationException(
                "The per-app audio route key cannot be opened");
    }

    internal static string ComputeIdentityKeyHash(string identity)
    {
        if (
            string.IsNullOrWhiteSpace(identity)
            || identity.Length > 4096
            || identity.Any(char.IsControl)
        )
        {
            throw new ArgumentException(
                "The per-app audio identity is invalid",
                nameof(identity));
        }
        uint hash = 0;
        foreach (char value in identity)
        {
            hash = unchecked((hash * 33) + value);
        }
        return hash.ToString("x", CultureInfo.InvariantCulture);
    }

    private static string ProvisionIdentityKey(
        RegistryKey root,
        string identity)
    {
        string keyPrefix = ComputeIdentityKeyHash(identity);
        for (int collision = 0; collision < 4096; collision++)
        {
            string candidateName = keyPrefix
                + "_"
                + collision.ToString(CultureInfo.InvariantCulture);
            using RegistryKey? existing = root.OpenSubKey(
                candidateName,
                writable: true);
            if (existing is not null)
            {
                string? existingIdentity = existing.GetValue(
                    "",
                    defaultValue: null,
                    RegistryValueOptions.DoNotExpandEnvironmentNames)
                    as string;
                if (string.Equals(
                    existingIdentity,
                    identity,
                    StringComparison.OrdinalIgnoreCase))
                {
                    return candidateName;
                }
                continue;
            }

            using RegistryKey created = root.CreateSubKey(
                candidateName,
                writable: true)
                ?? throw new InvalidOperationException(
                    "The per-app audio identity key cannot be created");
            string? racedIdentity = created.GetValue(
                "",
                defaultValue: null,
                RegistryValueOptions.DoNotExpandEnvironmentNames)
                as string;
            if (
                racedIdentity is not null
                && !string.Equals(
                    racedIdentity,
                    identity,
                    StringComparison.OrdinalIgnoreCase)
            )
            {
                continue;
            }
            if (
                racedIdentity is null
                && (
                    created.ValueCount != 0
                    || created.SubKeyCount != 0
                )
            )
            {
                // Do not claim a concurrently created or malformed key.
                continue;
            }
            if (racedIdentity is null)
            {
                created.SetValue(
                    "",
                    identity,
                    RegistryValueKind.String);
                created.Flush();
            }
            string? verifiedIdentity = created.GetValue(
                "",
                defaultValue: null,
                RegistryValueOptions.DoNotExpandEnvironmentNames)
                as string;
            if (!string.Equals(
                verifiedIdentity,
                identity,
                StringComparison.OrdinalIgnoreCase))
            {
                throw new InvalidOperationException(
                    "The per-app audio identity key readback mismatched");
            }
            return candidateName;
        }
        throw new InvalidOperationException(
            "The per-app audio identity collision limit was exceeded");
    }

    private static string ReadEndpointPropertySetId(
        string endpointId,
        PerAppAudioDataFlow flow)
    {
        AudioPolicyEndpointId.ValidateForFlow(endpointId, flow);
        string endpointGuid = endpointId[^38..];
        string flowName = flow switch
        {
            PerAppAudioDataFlow.Render => "Render",
            PerAppAudioDataFlow.Capture => "Capture",
            _ => throw new ArgumentOutOfRangeException(nameof(flow)),
        };
        using RegistryKey? properties = Registry.LocalMachine.OpenSubKey(
            MmDevicesRegistryPath
            + "\\"
            + flowName
            + "\\"
            + endpointGuid
            + "\\Properties",
            writable: false);
        string? propertySetId = properties?.GetValue(
            EndpointPropertySetIdValueName,
            defaultValue: null,
            RegistryValueOptions.DoNotExpandEnvironmentNames)
            as string;
        if (
            propertySetId is null
            || !Guid.TryParseExact(propertySetId, "B", out _)
        )
        {
            throw new InvalidOperationException(
                "The endpoint property-set identity is unavailable");
        }
        return propertySetId;
    }

    private static bool PostconditionMatches(
        RegistryKey app,
        string valueName,
        PerAppAudioRouteKey key,
        string? endpointId)
    {
        object? actualValue = app.GetValue(
            valueName,
            defaultValue: null,
            RegistryValueOptions.DoNotExpandEnvironmentNames);
        object? actualPropertySetId = app.GetValue(
            valueName + "_p",
            defaultValue: null,
            RegistryValueOptions.DoNotExpandEnvironmentNames);
        if (endpointId is null)
        {
            return actualValue is null
                && actualPropertySetId is null;
        }
        return actualValue is string packed
            && actualPropertySetId is string propertySetId
            && string.Equals(
                packed,
                AudioPolicyEndpointId.Pack(endpointId, key.Flow),
                StringComparison.OrdinalIgnoreCase)
            && string.Equals(
                propertySetId,
                ReadEndpointPropertySetId(endpointId, key.Flow),
                StringComparison.OrdinalIgnoreCase);
    }

    private static void RestorePair(
        RegistryKey app,
        string valueName,
        RegistryValueSnapshot originalValue,
        RegistryValueSnapshot originalPropertySetId)
    {
        originalPropertySetId.Restore(app, valueName + "_p");
        originalValue.Restore(app, valueName);
        app.Flush();
    }

    private static void RequireStringOrMissing(
        RegistryValueSnapshot value)
    {
        if (
            value.Exists
            && value.Kind != RegistryValueKind.String
        )
        {
            throw new InvalidOperationException(
                "The per-app audio route has an unexpected value type");
        }
    }

    private static string ReadApplicationUserModelId(uint processId)
    {
        IntPtr process = OpenProcess(
            ProcessQueryLimitedInformation,
            inheritHandle: false,
            processId);
        if (process == IntPtr.Zero)
        {
            Marshal.ThrowExceptionForHR(
                Marshal.GetHRForLastWin32Error());
        }
        try
        {
            uint length = 0;
            int first = GetApplicationUserModelId(
                process,
                ref length,
                null);
            if (
                first != ErrorInsufficientBuffer
                || length is < 2 or > 4096
            )
            {
                Marshal.ThrowExceptionForHR(
                    HResultFromWin32(first));
            }
            StringBuilder identity = new(checked((int)length));
            int second = GetApplicationUserModelId(
                process,
                ref length,
                identity);
            if (second != 0 || identity.Length == 0)
            {
                Marshal.ThrowExceptionForHR(
                    HResultFromWin32(second));
            }
            return identity.ToString();
        }
        finally
        {
            _ = CloseHandle(process);
        }
    }

    private static int HResultFromWin32(int error) =>
        error <= 0
            ? error
            : unchecked((int)(0x80070000U | (uint)error));

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr OpenProcess(
        uint desiredAccess,
        [MarshalAs(UnmanagedType.Bool)] bool inheritHandle,
        uint processId);

    [DllImport("kernel32.dll", ExactSpelling = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CloseHandle(IntPtr handle);

    [DllImport(
        "kernel32.dll",
        CharSet = CharSet.Unicode,
        ExactSpelling = true)]
    private static extern int GetApplicationUserModelId(
        IntPtr process,
        ref uint applicationUserModelIdLength,
        StringBuilder? applicationUserModelId);

    private sealed record RegistryValueSnapshot(
        bool Exists,
        RegistryValueKind Kind,
        object? Value)
    {
        internal static RegistryValueSnapshot Read(
            RegistryKey key,
            string valueName)
        {
            object? value = key.GetValue(
                valueName,
                defaultValue: null,
                RegistryValueOptions.DoNotExpandEnvironmentNames);
            return value is null
                ? new(false, RegistryValueKind.Unknown, null)
                : new(true, key.GetValueKind(valueName), value);
        }

        internal void Restore(
            RegistryKey key,
            string valueName)
        {
            if (!Exists)
            {
                key.DeleteValue(
                    valueName,
                    throwOnMissingValue: false);
                return;
            }
            key.SetValue(
                valueName,
                Value
                    ?? throw new InvalidOperationException(
                        "A present registry value cannot be null"),
                Kind);
        }
    }
}
