using System.Text.Json;

namespace BwReader.ComputerVoiceAudio;

// 网页表面的实时输出下行通道（2026-08-26 用户拍板方案 A）。
//
// 背景：App 内有常驻 WSS + visual-register，输出实时推送；iPad Safari 网页
// 的上下文上行早已改为一次性 POST（iOS 扩展文档短命保不住 socket，见
// web-context-snapshot-handoff），但下行从来没有对应机制 —— AI 往网页送卡
// 报"来源不在线"。
//
// 方案：HTTP 取件型 lease。扩展 background 对着与上下文 POST 同一个
// Tailscale HTTPS 面做**长轮询 GET**；每次 GET 都是在场心跳，桥据此向
// router Attach 一个 lease（sendAsync = 入队 + 唤醒长轮询）。上游的
// ReaderRealtimeOutput 完全无感知：WaitForSourceAsync 拿到的就是普通
// lease，SendAsync 照发，回执经 POST 回来走同一个 Accept —— AI 端语义
// 与 App 场景逐字一致（真实送达回执，不是"已排队"敷衍）。
internal sealed class ReaderHttpPickupService
{
    private static readonly TimeSpan SessionIdleTimeout =
        TimeSpan.FromSeconds(60);
    private const int MaximumQueuedEvents = 16;
    private const int MaximumSources = 16;

    private sealed class Session
    {
        internal Session(ReaderContextSourceLease lease)
        {
            Lease = lease;
        }

        internal ReaderContextSourceLease Lease { get; }
        internal Queue<object> Events { get; } = new();
        internal TaskCompletionSource<bool>? Waiter { get; set; }
        internal long LastSeenUtcMs { get; set; }
    }

    private readonly object _gate = new();
    private readonly Dictionary<string, Session> _sessions =
        new(StringComparer.Ordinal);
    private readonly ReaderContextSourceRouter _router;
    private readonly Func<DateTimeOffset> _utcNow;

    internal ReaderHttpPickupService(
        ReaderContextSourceRouter router,
        Func<DateTimeOffset>? utcNow = null)
    {
        _router = router;
        _utcNow = utcNow ?? (() => DateTimeOffset.UtcNow);
    }

    /// <summary>
    /// 长轮询取件：确保该 source 有活 lease，取走已排队事件；空队则最多
    /// 等 waitSeconds。每次调用都是在场心跳。
    /// </summary>
    internal async Task<IReadOnlyList<object>> PollAsync(
        string sourceInstanceId,
        int waitSeconds,
        CancellationToken cancellationToken)
    {
        if (!DirectBridgeContract.IsSafeId(sourceInstanceId))
        {
            throw new ArgumentException(
                "sourceInstanceId must be a safe identifier");
        }
        Session session = EnsureSession(sourceInstanceId);
        long now = _utcNow().ToUnixTimeMilliseconds();
        Task<bool>? waitTask = null;
        lock (_gate)
        {
            session.LastSeenUtcMs = now;
            if (session.Events.Count == 0 && waitSeconds > 0)
            {
                session.Waiter ??= new TaskCompletionSource<bool>(
                    TaskCreationOptions.RunContinuationsAsynchronously);
                waitTask = session.Waiter.Task;
            }
        }
        if (waitTask is not null)
        {
            await Task.WhenAny(
                waitTask,
                Task.Delay(
                    TimeSpan.FromSeconds(Math.Min(waitSeconds, 30)),
                    cancellationToken)).ConfigureAwait(false);
            cancellationToken.ThrowIfCancellationRequested();
        }
        lock (_gate)
        {
            session.LastSeenUtcMs = _utcNow().ToUnixTimeMilliseconds();
            List<object> drained = new(session.Events);
            session.Events.Clear();
            return drained;
        }
    }

    /// <summary>网页回执 → 与 WSS 回执同一个 Accept。</summary>
    internal void AcceptReceipt(
        ReaderRealtimeOutputBroker realtimeOutput,
        ReaderRealtimeOutputAck ack)
    {
        Session? session;
        lock (_gate)
        {
            _sessions.TryGetValue(ack.SourceInstanceId, out session);
        }
        if (session is null)
        {
            throw new ReaderRealtimeOutputException(
                "BW_READER_REALTIME_OUTPUT_SOURCE_MISMATCH",
                "该来源没有活跃的取件会话",
                retryable: false);
        }
        realtimeOutput.Accept(session.Lease, ack);
    }

    /// <summary>闲置会话清扫（对账周期外的轻量步进：每次 Poll 顺路）。</summary>
    internal void SweepIdle()
    {
        long cutoff = _utcNow().ToUnixTimeMilliseconds()
            - (long)SessionIdleTimeout.TotalMilliseconds;
        List<(string, Session)> expired = new();
        lock (_gate)
        {
            foreach ((string key, Session session) in _sessions)
            {
                if (session.LastSeenUtcMs < cutoff)
                {
                    expired.Add((key, session));
                }
            }
            foreach ((string key, _) in expired)
            {
                _sessions.Remove(key);
            }
        }
        foreach ((_, Session session) in expired)
        {
            _router.Detach(session.Lease);
        }
    }

    private Session EnsureSession(string sourceInstanceId)
    {
        SweepIdle();
        lock (_gate)
        {
            if (_sessions.TryGetValue(sourceInstanceId, out Session? live))
            {
                return live;
            }
            if (_sessions.Count >= MaximumSources)
            {
                throw new ReaderRealtimeOutputException(
                    "BW_READER_REALTIME_OUTPUT_CAPACITY",
                    "HTTP 取件会话已达上限",
                    retryable: true);
            }
            string connectionId =
                "httppickup" + Guid.NewGuid().ToString("N");
            Session created = null!;
            ReaderContextSourceLease lease = _router.Attach(
                sourceInstanceId,
                connectionId,
                (payload, _) =>
                {
                    lock (_gate)
                    {
                        if (created.Events.Count >= MaximumQueuedEvents)
                        {
                            throw new ReaderVisualDeliveryException(
                                "BW_READER_REALTIME_OUTPUT_CAPACITY",
                                "取件队列已满",
                                retryable: true);
                        }
                        created.Events.Enqueue(payload);
                        created.Waiter?.TrySetResult(true);
                        created.Waiter = null;
                    }
                    return Task.CompletedTask;
                });
            created = new Session(lease)
            {
                LastSeenUtcMs = _utcNow().ToUnixTimeMilliseconds(),
            };
            _sessions[sourceInstanceId] = created;
            return created;
        }
    }

    /// <summary>
    /// HTTP 回执体校验：与 WSS ValidateAck 同一套字段纪律，但没有会话可
    /// 绑（一次性 POST），sessionId 由这里合成。
    /// </summary>
    internal static ReaderRealtimeOutputAck ParseReceipt(JsonElement body)
    {
        if (body.ValueKind != JsonValueKind.Object)
        {
            throw new ReaderRealtimeOutputException(
                "BW_READER_REALTIME_OUTPUT_RECEIVER_INVALID",
                "回执必须是对象",
                retryable: false);
        }
        string? correlation = body.TryGetProperty(
            "correlation", out JsonElement c) ? c.GetString() : null;
        string? sourceInstanceId = body.TryGetProperty(
            "sourceInstanceId", out JsonElement s) ? s.GetString() : null;
        string? outcome = body.TryGetProperty(
            "outcome", out JsonElement o) ? o.GetString() : null;
        if (
            correlation is null
            || !DirectBridgeContract.IsSafeId(correlation)
            || sourceInstanceId is null
            || !DirectBridgeContract.IsSafeId(sourceInstanceId)
            || outcome is not ("applied" or "replay" or "rejected")
        )
        {
            throw new ReaderRealtimeOutputException(
                "BW_READER_REALTIME_OUTPUT_RECEIVER_INVALID",
                "回执字段无效",
                retryable: false);
        }
        string? error = body.TryGetProperty("error", out JsonElement e)
            && e.ValueKind == JsonValueKind.String
            ? e.GetString() : null;
        if ((outcome == "rejected") != (error is not null))
        {
            throw new ReaderRealtimeOutputException(
                "BW_READER_REALTIME_OUTPUT_RECEIVER_INVALID",
                "拒绝回执必须且只能携带 error",
                retryable: false);
        }
        string? bindOutcome = body.TryGetProperty(
            "bindOutcome", out JsonElement bo)
            && bo.ValueKind == JsonValueKind.String
            ? bo.GetString() : null;
        if (bindOutcome is not (null or "none" or "bound" or "floating"
            or "unknown"))
        {
            throw new ReaderRealtimeOutputException(
                "BW_READER_REALTIME_OUTPUT_RECEIVER_INVALID",
                "bindOutcome 无效",
                retryable: false);
        }
        string? bindReason = body.TryGetProperty(
            "bindReason", out JsonElement br)
            && br.ValueKind == JsonValueKind.String
            ? br.GetString() : null;
        if (error is { Length: > 500 } || bindReason is { Length: > 120 })
        {
            throw new ReaderRealtimeOutputException(
                "BW_READER_REALTIME_OUTPUT_RECEIVER_INVALID",
                "回执字段超长",
                retryable: false);
        }
        return new ReaderRealtimeOutputAck(
            "http-pickup",
            correlation,
            sourceInstanceId,
            outcome,
            error,
            bindOutcome,
            bindReason);
    }
}
