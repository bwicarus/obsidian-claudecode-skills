using System.Text.Json;
using System.Text.Json.Nodes;

namespace BwReader.ComputerVoiceAudio;

internal sealed record ReaderBrowserControlRequest(
    string Correlation,
    string SourceInstanceId,
    long SnapshotRevision,
    string File,
    JsonNode Page,
    string Action,
    string? Target,
    string? SelectionId);

internal sealed record ReaderBrowserControlResponse(
    string SessionId,
    string Correlation,
    string SourceInstanceId,
    long SnapshotRevision,
    string File,
    JsonElement Page,
    string Action,
    string Status,
    double ScrollX,
    double ScrollY,
    string Url,
    string Title);

internal sealed class ReaderBrowserControlException : Exception
{
    internal ReaderBrowserControlException(
        string code,
        string message,
        bool retryable,
        Exception? innerException = null)
        : base(message, innerException)
    {
        Code = code;
        Retryable = retryable;
    }

    internal string Code { get; }
    internal bool Retryable { get; }
}

internal static class ReaderBrowserControlProtocol
{
    internal const string ControlContract = "reader-browser-control/1";
    internal const string EventName = "reader-browser-control-request";
    internal const string ResponseType = "reader-browser-control";
    internal const int MaximumTargetCharacters = 320;
    private const double MaximumScrollCoordinate = 100_000_000;

    internal static bool IsAction(string value) => value is
        "next-viewport"
        or "previous-viewport"
        or "scroll-to-text"
        or "scroll-to-heading"
        or "scroll-to-selection";

    internal static object Event(ReaderBrowserControlRequest request) =>
        new
        {
            contract = DirectBridgeContract.Contract,
            type = "event",
            @event = EventName,
            payload = new
            {
                contract = ControlContract,
                commandKind = "browser-control",
                correlation = request.Correlation,
                sourceInstanceId = request.SourceInstanceId,
                snapshotRevision = request.SnapshotRevision,
                file = request.File,
                page = request.Page,
                action = request.Action,
                target = request.Target,
                selectionId = request.SelectionId,
            },
        };

    internal static ReaderBrowserControlResponse ValidateResponse(
        JsonElement message)
    {
        RequireExactFields(
            message,
            "contract",
            "type",
            "requestId",
            "sessionId",
            "correlation",
            "sourceInstanceId",
            "snapshotRevision",
            "file",
            "page",
            "action",
            "status",
            "scrollX",
            "scrollY",
            "url",
            "title");
        if (
            RequiredString(message, "contract", 128)
                != DirectBridgeContract.Contract
            || RequiredString(message, "type", 64) != ResponseType
        )
        {
            throw Invalid("Reader 浏览控制消息合同无效");
        }
        string sessionId = RequiredSafeId(message, "sessionId");
        _ = DirectPcmFrameCodec.ParseSessionId(sessionId);
        string correlation = RequiredSafeId(message, "correlation");
        string sourceInstanceId = RequiredSafeId(
            message,
            "sourceInstanceId");
        long snapshotRevision = RequiredInt64(
            message,
            "snapshotRevision");
        if (snapshotRevision < 0)
        {
            throw Invalid("Reader 浏览控制 snapshotRevision 无效");
        }
        string file = RequiredString(message, "file", 4096);
        if (file.Any(char.IsControl))
        {
            throw Invalid("Reader 浏览控制 file 无效");
        }
        JsonElement page = message.GetProperty("page");
        if (!ValidPage(page))
        {
            throw Invalid("Reader 浏览控制 page 无效");
        }
        string action = RequiredString(message, "action", 32);
        if (!IsAction(action))
        {
            throw Invalid("Reader 浏览控制 action 无效");
        }
        string status = RequiredString(message, "status", 16);
        if (status is not ("success" or "not-found" or "rejected"))
        {
            throw Invalid("Reader 浏览控制 status 无效");
        }
        double scrollX = RequiredFiniteNumber(message, "scrollX");
        double scrollY = RequiredFiniteNumber(message, "scrollY");
        if (
            Math.Abs(scrollX) > MaximumScrollCoordinate
            || Math.Abs(scrollY) > MaximumScrollCoordinate
        )
        {
            throw Invalid("Reader 浏览控制滚动位置超出上限");
        }
        string url = RequiredString(message, "url", 4096);
        if (
            !Uri.TryCreate(url, UriKind.Absolute, out Uri? parsed)
            || parsed.Scheme is not ("http" or "https")
            || url.Any(char.IsControl)
        )
        {
            throw Invalid("Reader 浏览控制 url 无效");
        }
        string title = RequiredString(
            message,
            "title",
            1024,
            allowEmpty: true);
        if (title.Any(char.IsControl))
        {
            throw Invalid("Reader 浏览控制 title 无效");
        }
        return new ReaderBrowserControlResponse(
            sessionId,
            correlation,
            sourceInstanceId,
            snapshotRevision,
            file,
            page.Clone(),
            action,
            status,
            scrollX,
            scrollY,
            url,
            title);
    }

    private static bool ValidPage(JsonElement page)
    {
        if (
            page.ValueKind == JsonValueKind.Number
            && page.TryGetInt64(out long number)
        )
        {
            return number >= 0;
        }
        return page.ValueKind == JsonValueKind.String
            && page.GetString() is string text
            && text.Length is >= 1 and <= 256
            && !text.Any(char.IsControl);
    }

    private static string RequiredSafeId(
        JsonElement message,
        string name)
    {
        string value = RequiredString(message, name, 160);
        if (!DirectBridgeContract.IsSafeId(value))
        {
            throw Invalid($"Reader 浏览控制 {name} 无效");
        }
        return value;
    }

    private static string RequiredString(
        JsonElement message,
        string name,
        int maximumLength,
        bool allowEmpty = false)
    {
        if (
            !message.TryGetProperty(name, out JsonElement value)
            || value.ValueKind != JsonValueKind.String
            || value.GetString() is not string result
            || (!allowEmpty && result.Length == 0)
            || result.Length > maximumLength
        )
        {
            throw Invalid($"Reader 浏览控制 {name} 无效");
        }
        return result;
    }

    private static long RequiredInt64(
        JsonElement message,
        string name)
    {
        if (
            !message.TryGetProperty(name, out JsonElement value)
            || value.ValueKind != JsonValueKind.Number
            || !value.TryGetInt64(out long result)
        )
        {
            throw Invalid($"Reader 浏览控制 {name} 无效");
        }
        return result;
    }

    private static double RequiredFiniteNumber(
        JsonElement message,
        string name)
    {
        if (
            !message.TryGetProperty(name, out JsonElement value)
            || value.ValueKind != JsonValueKind.Number
            || !value.TryGetDouble(out double result)
            || !double.IsFinite(result)
        )
        {
            throw Invalid($"Reader 浏览控制 {name} 无效");
        }
        return result;
    }

    private static void RequireExactFields(
        JsonElement value,
        params string[] expected)
    {
        if (value.ValueKind != JsonValueKind.Object)
        {
            throw Invalid("Reader 浏览控制消息必须是对象");
        }
        DirectJsonValidation.RequireNoDuplicateKeys(value);
        HashSet<string> actual = value.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        if (!actual.SetEquals(expected))
        {
            throw Invalid("Reader 浏览控制消息字段不匹配");
        }
    }

    private static DirectProtocolException Invalid(string message) =>
        new(
            "BW_READER_BROWSER_CONTROL_SCHEMA_INVALID",
            message,
            retryable: false);
}

internal sealed class ReaderBrowserControlBroker
{
    private static readonly TimeSpan DeliveryTimeout =
        TimeSpan.FromSeconds(10);
    private const int MaximumPendingControls = 8;

    private sealed record PendingControl(
        ReaderBrowserControlRequest Request,
        ReaderContextSourceLease Lease,
        TaskCompletionSource<ReaderBrowserControlResponse> Completion);

    private readonly ReaderContextSourceRouter _router;
    private readonly object _gate = new();
    private readonly Dictionary<string, PendingControl> _pending =
        new(StringComparer.Ordinal);

    internal ReaderBrowserControlBroker(ReaderContextSourceRouter router)
    {
        _router = router;
    }

    /// 跟读/写两侧一致的注册等待。
    ///
    /// ⚠ 这里原来也是零等待。跟读那边同一个问题：网络抖一下的瞬间
    /// 来源还没重新注册，一个用户明确要求的导航就直接失败了。
    /// 等 2.5 秒再执行，比让用户重说一遍好。
    internal static readonly TimeSpan SourceRegistrationWait =
        TimeSpan.FromMilliseconds(2_500);

    private static readonly TimeSpan SourceRegistrationPoll =
        TimeSpan.FromMilliseconds(50);

    private async Task<ReaderContextSourceLease?> WaitForSourceAsync(
        string sourceInstanceId,
        CancellationToken cancellationToken)
    {
        DateTimeOffset deadline = DateTimeOffset.UtcNow + SourceRegistrationWait;
        while (true)
        {
            if (
                _router.TryGetLease(
                    sourceInstanceId,
                    out ReaderContextSourceLease? lease)
                && lease is not null
            )
            {
                return lease;
            }
            if (DateTimeOffset.UtcNow >= deadline)
            {
                return null;
            }
            await Task.Delay(
                SourceRegistrationPoll,
                cancellationToken).ConfigureAwait(false);
        }
    }

    internal async Task<ReaderBrowserControlResponse> RequestAsync(
        ReaderBrowserControlRequest request,
        CancellationToken cancellationToken)
    {
        ReaderContextSourceLease? lease = await WaitForSourceAsync(
            request.SourceInstanceId,
            cancellationToken).ConfigureAwait(false);
        if (lease is null)
        {
            throw Failure(
                "BW_READER_BROWSER_CONTROL_SOURCE_OFFLINE",
                "快照指定的 Reader 页面来源当前不在线（已等待 "
                    + $"{SourceRegistrationWait.TotalSeconds:0.#} 秒仍未注册）",
                retryable: true);
        }
        PendingControl pending = new(
            request,
            lease,
            new TaskCompletionSource<ReaderBrowserControlResponse>(
                TaskCreationOptions.RunContinuationsAsynchronously));
        lock (_gate)
        {
            if (_pending.Count >= MaximumPendingControls)
            {
                throw Failure(
                    "BW_READER_BROWSER_CONTROL_CAPACITY",
                    "Reader 浏览控制请求仍在处理中",
                    retryable: true);
            }
            if (!_pending.TryAdd(request.Correlation, pending))
            {
                throw Failure(
                    "BW_READER_BROWSER_CONTROL_DUPLICATE_PENDING",
                    "相同 Reader 浏览控制请求仍在处理中",
                    retryable: true);
            }
        }
        try
        {
            try
            {
                await _router.SendAsync(
                    lease,
                    ReaderBrowserControlProtocol.Event(request),
                    cancellationToken).ConfigureAwait(false);
            }
            catch (ReaderVisualDeliveryException exception)
            {
                throw Failure(
                    "BW_READER_BROWSER_CONTROL_SOURCE_OFFLINE",
                    exception.Message,
                    retryable: true,
                    exception);
            }
            Task winner = await Task.WhenAny(
                pending.Completion.Task,
                lease.LeaseRetired,
                Task.Delay(DeliveryTimeout, cancellationToken))
                .ConfigureAwait(false);
            cancellationToken.ThrowIfCancellationRequested();
            if (winner == pending.Completion.Task)
            {
                return await pending.Completion.Task.ConfigureAwait(false);
            }
            if (winner == lease.LeaseRetired)
            {
                throw Failure(
                    "BW_READER_BROWSER_CONTROL_SOURCE_OFFLINE",
                    "控制期间指定 Reader 页面来源已离线",
                    retryable: true);
            }
            throw Failure(
                "BW_READER_BROWSER_CONTROL_TIMEOUT",
                "Reader 浏览控制超时",
                retryable: true);
        }
        finally
        {
            lock (_gate)
            {
                if (
                    _pending.TryGetValue(
                        request.Correlation,
                        out PendingControl? current)
                    && ReferenceEquals(current, pending)
                )
                {
                    _pending.Remove(request.Correlation);
                }
            }
        }
    }

    internal void Accept(
        ReaderContextSourceLease lease,
        ReaderBrowserControlResponse response)
    {
        PendingControl pending;
        lock (_gate)
        {
            if (
                !_pending.TryGetValue(response.Correlation, out pending!)
                || !ReferenceEquals(pending.Lease, lease)
            )
            {
                throw Failure(
                    "BW_READER_BROWSER_CONTROL_NOT_PENDING",
                    "Reader 浏览控制请求不存在或已过期",
                    retryable: false);
            }
            ReaderBrowserControlRequest request = pending.Request;
            if (
                request.SourceInstanceId != response.SourceInstanceId
                || request.SnapshotRevision != response.SnapshotRevision
                || request.File != response.File
                || !ReaderVisualDeliveryProtocol.PageEquivalent(
                    request.Page,
                    response.Page)
                || request.Action != response.Action
            )
            {
                throw Failure(
                    "BW_READER_BROWSER_CONTROL_IDENTITY_MISMATCH",
                    "Reader 浏览控制来源、页面、版本或动作不匹配",
                    retryable: false);
            }
            _pending.Remove(response.Correlation);
            pending.Completion.TrySetResult(response);
        }
    }

    private static ReaderBrowserControlException Failure(
        string code,
        string message,
        bool retryable,
        Exception? innerException = null) =>
        new(code, message, retryable, innerException);
}
