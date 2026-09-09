using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.AspNetCore.Http;

namespace BwReader.ComputerVoiceAudio;

/// Codex 主动推送的**绑定登记处**（2026-09-09 Codex→Claude 交接）。
///
/// ## 为什么必须由 AI 自己来注册
///
/// 推送要两样东西：命名管道地址，和目标任务 id。两样都只存在于
/// **Codex 亲自启动的进程**的环境里（`CODEX_APP_TOOLS_PIPE_PATH` /
/// `CODEX_THREAD_ID`）。独立启动的 ReaderPC 拿不到 —— 2026-09-09 实测确认。
///
/// 于是设计只剩一种：语音会话开始时，由 AI 调一次这个端点把自己的地址和
/// 任务 id 报上来。这也顺带解决了「绑对任务」这件事 —— 只有它知道自己是谁。
///
/// ## 纪律
///
/// - **失效由实测判定，不由时钟。** 一个陈旧绑定会让板面事件一直推进一个
///   已经死掉的任务。判它死没死的**不是时间**，是推送本身连续失败
///   （见 `Invalidate` 和 `ReaderCodexPush` 的失败计数）；`Lifetime` 只是
///   防"注册完再没人管过"的远期兜底。2026-09-09 从 6 小时的硬 TTL 改过来，
///   因为那种设计会在一段长会话中途把活绑定杀掉，制造出它本要防的静默停摆。
/// - **地址不做任何猜测。** 不枚举命名管道、不去读 Codex 的安装目录 ——
///   交接里明说安装路径带版本号会变。拿不到就不推，并说出原因。
/// - **注册只是"能推"，不等于"该推"。** 推不推由 `ReaderCodexPush.Enabled`
///   决定，而它默认关：消费端还在轮询时两条都开就是双发。
internal static class ReaderCodexEndpoint
{
    internal const string RoutePath = "/reader-codex-endpoint/v1";
    private const string StoreFileName = "codex-push-binding.json";
    private const int MaxBodyBytes = 4 * 1024;
    /// 绑定的**远期兜底**期限。
    ///
    /// ⚠ 2026-09-09 从 6 小时放宽到 72 小时，因为用时钟判"目标还活着吗"
    /// 是错的方向（用户当场问了这个设计）：
    ///   · 时钟答不出目标死没死，而**推送本身答得出** —— 推失败了就是死了；
    ///   · 6 小时会在一段长会话**中途**过期，推送悄悄停掉，
    ///     那正是这个机制本来要防的失败，只不过改由我们的定时器制造；
    ///   · 两种错的代价不对称：过期太早=静默停摆（很糟），
    ///     过期太晚=往死目标推一次并失败（会记在 lastNote 里，便宜）。
    /// 所以真正判失效的是**连续推送失败**（见 `Invalidate`），
    /// 这个时钟只是防"注册完就再没人管过"的兜底。
    internal static readonly TimeSpan Lifetime = TimeSpan.FromHours(72);

    internal sealed record Binding(string PipeName, string ThreadId);

    private static readonly object Gate = new();
    /// 登记这一串动作（读旧状态 → 写新绑定 → 决定要不要发接通提醒）必须
    /// 整体互斥。Codex 的 SessionStart 和 UserPromptSubmit 两个钩子会几乎
    /// 同时登记同一个目标（2026-09-09 实测），不锁的话两边各自读到"还没登记过"
    /// 于是各发一次接通提醒。
    /// ⚠ 与 `Gate` 分开：那把锁只护 `_storeDirectory`，在它里面做文件读写
    /// 会把两件无关的事绑在一起。
    private static readonly object RegistrationGate = new();
    private static string _storeDirectory = string.Empty;

    internal static void Configure()
    {
        lock (Gate)
        {
            if (_storeDirectory.Length > 0) return;
            _storeDirectory = Path.Combine(
                Environment.GetFolderPath(
                    Environment.SpecialFolder.LocalApplicationData),
                "BWReader");
        }
    }

    private static string StorePath
    {
        get
        {
            Configure();
            lock (Gate)
            {
                return Path.Combine(_storeDirectory, StoreFileName);
            }
        }
    }

    /// `NamedPipeClientStream` 要的是**管道名**，不是 `\\.\pipe\` 全路径。
    /// 环境变量里两种形态都可能出现，所以这里统一削平。
    internal static string NormalizePipeName(string raw)
    {
        string text = (raw ?? string.Empty).Trim();
        const string prefix = @"\\.\pipe\";
        if (text.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
        {
            text = text[prefix.Length..];
        }
        // 反斜杠在管道名里非法（那是路径分隔符），出现就说明还没削干净。
        return text.Contains('\\') ? string.Empty : text;
    }

    /// 把绑定标成失效，并**记下原因**。由推送侧在连续失败到上限时调。
    ///
    /// 不直接删文件：删掉之后再问"为什么不推了"就没有答案了，
    /// 而这条链没有界面，原因丢了就等于没发生过。重新登记会清掉这个标记。
    internal static void Invalidate(string reason)
    {
        try
        {
            string path = StorePath;
            if (!File.Exists(path)) return;
            if (JsonNode.Parse(File.ReadAllText(path)) is not JsonObject value)
            {
                return;
            }
            value["invalidAtMs"] = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
            value["invalidReason"] = reason.Length > 200
                ? reason[..200] : reason;
            string temporary = path + ".tmp-" + Environment.ProcessId;
            File.WriteAllText(temporary, value.ToJsonString(
                new JsonSerializerOptions { WriteIndented = true }));
            File.Move(temporary, path, overwrite: true);
        }
        catch (Exception)
        {
            // 记不下就算了：这一步是为了留线索，不该反过来把推送弄坏。
        }
    }

    /// 读出整条绑定记录（不做新鲜度/失效判断）。读不出来返回 null。
    private static JsonObject? ReadRecord()
    {
        try
        {
            string path = StorePath;
            if (!File.Exists(path)) return null;
            return JsonNode.Parse(File.ReadAllText(path)) as JsonObject;
        }
        catch (Exception)
        {
            return null;
        }
    }

    /// 绑定为什么失效了。没失效返回空串 —— 给状态查询用。
    internal static string InvalidReason()
    {
        try
        {
            string path = StorePath;
            if (!File.Exists(path)) return string.Empty;
            if (JsonNode.Parse(File.ReadAllText(path)) is not JsonObject value)
            {
                return string.Empty;
            }
            return (string?)value["invalidReason"] ?? string.Empty;
        }
        catch (Exception)
        {
            return string.Empty;
        }
    }

    /// 当前可用的绑定。没注册过、读不出来、被判失效、或超过兜底期限都返回 null。
    internal static Binding? Current()
    {
        try
        {
            string path = StorePath;
            if (!File.Exists(path)) return null;
            JsonNode? parsed = JsonNode.Parse(File.ReadAllText(path));
            if (parsed is not JsonObject value) return null;
            // 连续推失败判定的失效**优先于**时钟：它是实测的，时钟是猜的。
            if (!string.IsNullOrEmpty((string?)value["invalidReason"]))
            {
                return null;
            }
            long at = (long?)value["registeredAtMs"] ?? 0;
            if (at <= 0) return null;
            long now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
            if (now - at > (long)Lifetime.TotalMilliseconds) return null;
            string pipe = NormalizePipeName((string?)value["pipeName"] ?? "");
            string thread = ((string?)value["threadId"] ?? "").Trim();
            if (pipe.Length == 0 || thread.Length == 0) return null;
            return new Binding(pipe, thread);
        }
        catch (Exception)
        {
            return null;
        }
    }

    internal static async Task WriteResponseAsync(
        HttpContext context,
        CancellationToken cancellationToken)
    {
        JsonObject? body = await ReadBodyAsync(context, cancellationToken)
            .ConfigureAwait(false);
        if (body is null)
        {
            await Fail(context, "请求不是 JSON 对象，或超过 4 KiB")
                .ConfigureAwait(false);
            return;
        }

        // 注销：AI 结束值守时报一次，别等 TTL 慢慢过期。
        if (body["unregister"] is JsonValue flag
            && flag.TryGetValue(out bool wantsClear) && wantsClear)
        {
            try
            {
                File.Delete(StorePath);
            }
            catch (Exception exception)
            {
                await Fail(context, "清除失败：" + exception.Message)
                    .ConfigureAwait(false);
                return;
            }
            ReaderCodexPush.SetEnabled(false);
            await Ok(context, new JsonObject
            {
                ["ok"] = true,
                ["unregistered"] = true,
            }, cancellationToken).ConfigureAwait(false);
            return;
        }

        // 请正在通话的线程挂断（2026-09-09）。走的是**已有的推送通道**，
        // 所以协议只有一份实现；策略、校验与兜底都在 ReaderPC 那边。
        //
        // ⚠ 这里只回"送出去了没有"，**不回"关掉了没有"** —— 后者要看麦克风
        // 台账，而那是调用方的事。把传输成功说成挂断成功正是这一带最容易
        // 出的那种交待。
        if (body["hangUpVoice"] is JsonValue hangUp
            && hangUp.TryGetValue(out bool wantsHangUp) && wantsHangUp)
        {
            string inCall = Text(body["threadId"], 100);
            if (inCall.Length == 0)
            {
                await Fail(context, "hangUpVoice 需要 threadId（正在通话的那条线程）")
                    .ConfigureAwait(false);
                return;
            }
            bool sent = await ReaderCodexPush.RequestVoiceHangUpAsync(
                inCall,
                Text(body["reason"], 200),
                Text(body["requestId"], 80),
                cancellationToken).ConfigureAwait(false);
            await Ok(context, new JsonObject
            {
                ["ok"] = true,
                ["hangUpRequested"] = sent,
                ["threadId"] = inCall,
                ["note"] = sent
                    ? "已把挂断请求送到该线程；关没关成要看麦克风台账"
                    : "没送出去（看 pushNote）",
                ["pushNote"] = ReaderCodexPush.LastNote,
            }, cancellationToken).ConfigureAwait(false);
            return;
        }

        // 状态查询（2026-09-09）。只要回答，不改变任何状态。
        //
        // ⚠ 同样只回"发出去了没有"。回执是否写成要去看回执账本 ——
        // Codex 自己在交接里点了这条：「推送接口接受消息不等于状态回执已写入」。
        if (body["statusQuery"] is JsonValue query
            && query.TryGetValue(out bool wantsStatus) && wantsStatus)
        {
            string statusThread = Text(body["threadId"], 100);
            string requestId = Text(body["requestId"], 120);
            if (statusThread.Length == 0 || requestId.Length == 0)
            {
                await Fail(context, "statusQuery 需要 threadId 与 requestId")
                    .ConfigureAwait(false);
                return;
            }
            int validSeconds = 120;
            if (body["validSeconds"] is JsonValue seconds
                && seconds.TryGetValue(out int parsedSeconds)
                && parsedSeconds is > 0 and <= 3600)
            {
                validSeconds = parsedSeconds;
            }
            bool asked = await ReaderCodexPush.RequestStatusReportAsync(
                statusThread,
                requestId,
                validSeconds,
                cancellationToken).ConfigureAwait(false);
            await Ok(context, new JsonObject
            {
                ["ok"] = true,
                ["statusRequested"] = asked,
                ["requestId"] = requestId,
                ["note"] = asked
                    ? "查询已送到该线程；回执写没写要看回执账本"
                    : "没送出去（看 pushNote）",
                ["pushNote"] = ReaderCodexPush.LastNote,
            }, cancellationToken).ConfigureAwait(false);
            return;
        }

        // 兜底：推送两次都没让通话结束时才按 F24（2026-09-09 用户选的策略）。
        //
        // ⚠ F24 是**切换**：按在"其实已经挂断了"的状态上会**反向开一通**并
        // 开始计费。所以这里**自己再读一次台账**，不信调用方的判断 ——
        // 危险动作的守卫必须长在动作自己身上，否则总有一天会有第二个调用方
        // 绕过它。台账说不在通话就直接跳过，这不是失败。
        if (body["hangUpVoiceFallback"] is JsonValue fallback
            && fallback.TryGetValue(out bool wantsFallback) && wantsFallback)
        {
            CodexVoiceActivitySnapshot ledger =
                new WindowsRegistryCodexVoiceActivitySource().Read();
            if (!ledger.Active)
            {
                // ⚠ 台账**读不到**时 Active 也是 false —— 但那是"不知道"，
                // 不是"已经挂了"。两者都不该按 F24，可原因必须分开说：
                // 把不知道折成结论，排查的人就会去错的方向。
                bool readable =
                    ledger.Status == CodexVoiceActivityReadStatus.Available;
                await Ok(context, new JsonObject
                {
                    ["ok"] = true,
                    ["pressed"] = false,
                    ["skipped"] = readable
                        ? "台账显示已经不在通话，按 F24 反而会开一通"
                        : "台账读不到，不知道在不在通话；这种时候按 F24 是赌",
                    ["ledgerKnown"] = readable,
                    ["ledgerStatus"] = ledger.Status.ToString(),
                }, cancellationToken).ConfigureAwait(false);
                return;
            }
            try
            {
                CodexAppTarget target = WindowsCodexAppProbe.RequireReady();
                new WindowsCodexVoiceShortcutSender()
                    .Send(target, DirectVoiceCommand.Stop);
            }
            catch (Exception exception)
            {
                await Fail(context, "F24 兜底失败：" + exception.Message)
                    .ConfigureAwait(false);
                return;
            }
            await Ok(context, new JsonObject
            {
                ["ok"] = true,
                ["pressed"] = true,
                ["note"] = "已按 F24；关没关成仍要看台账",
            }, cancellationToken).ConfigureAwait(false);
            return;
        }

        string pipeName = NormalizePipeName(Text(body["pipePath"], 200));
        string threadId = Text(body["threadId"], 100);
        if (pipeName.Length == 0)
        {
            await Fail(context, "pipePath 是必需的（可以是名字或 \\\\.\\pipe\\ 全路径）")
                .ConfigureAwait(false);
            return;
        }
        if (threadId.Length == 0)
        {
            await Fail(context, "threadId 是必需的").ConfigureAwait(false);
            return;
        }
        // enabled 缺省**不改**当前开关：注册和"要不要推"是两件事，
        // 一次注册顺手把推送打开会在消费端还在轮询时造成双发。
        bool? wantEnabled = body["enabled"] is JsonValue enabledValue
            && enabledValue.TryGetValue(out bool parsedEnabled)
            ? parsedEnabled
            : null;

        // 先记下上一次是不是被判死过，登记完在响应里回给 AI ——
        // 否则它永远不知道中间断过一段，也就想不到去问为什么。
        string previousInvalid = InvalidReason();
        long now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        JsonObject record = new()
        {
            ["contract"] = "reader-codex-push-binding/1",
            ["pipeName"] = pipeName,
            ["threadId"] = threadId,
            ["registeredAtMs"] = now,
            // 重新登记 = "我又活了"：把上一次的失效判定清掉。
            // 不清的话，一次网络抖动判死之后就再也起不来了。
            ["invalidAtMs"] = null,
            ["invalidReason"] = null,
            ["expiresAtMs"] = now + (long)Lifetime.TotalMilliseconds,
        };
        // ── 读旧状态 → 写新绑定 → 决定要不要发接通提醒。**整段互斥。**
        //
        // Codex 的 SessionStart 与 UserPromptSubmit 两个钩子会几乎同时登记
        // 同一个目标（2026-09-09 Codex 反馈），不锁的话两边各自读到
        // "还没登记过"，于是各发一次接通提醒。
        string? writeError = null;
        bool shouldAnnounce = false;
        Binding announceTarget = new(pipeName, threadId);
        bool pushEnabledAfter;
        lock (RegistrationGate)
        {
            JsonObject? previous = ReadRecord();
            previousInvalid = (string?)previous?["invalidReason"] ?? string.Empty;
            string previousPipe = NormalizePipeName(
                (string?)previous?["pipeName"] ?? string.Empty);
            string previousThread =
                ((string?)previous?["threadId"] ?? string.Empty).Trim();
            bool wasEnabled = ReaderCodexPush.Enabled;
            // 「同一个有效绑定」= 对话和管道都没变。只有这种情况才是纯续期。
            bool sameTarget = previous is not null
                && string.Equals(previousPipe, pipeName, StringComparison.Ordinal)
                && string.Equals(previousThread, threadId, StringComparison.Ordinal);

            try
            {
                Configure();
                Directory.CreateDirectory(_storeDirectory);
                string path = StorePath;
                string temporary = path + ".tmp-" + Environment.ProcessId;
                File.WriteAllText(temporary, record.ToJsonString(
                    new JsonSerializerOptions { WriteIndented = true }));
                File.Move(temporary, path, overwrite: true);
            }
            catch (Exception exception)
            {
                writeError = exception.Message;
            }

            if (writeError is null)
            {
                if (wantEnabled is bool decided)
                {
                    ReaderCodexPush.SetEnabled(decided);
                }
                pushEnabledAfter = ReaderCodexPush.Enabled;
                // 换了目标、或从失效里恢复：失败计数从头算，别把上一个
                // 死绑定攒下的次数记在新目标头上。
                if (!sameTarget || previousInvalid.Length > 0)
                {
                    ReaderCodexPush.ResetFailures();
                }
                // 只有**真的（重新）接上**才发全量提醒。四种情形：
                //   第一次登记 / 换了对话或管道 / 从关变开 / 从失效里恢复。
                // 单纯续期不发 —— 用户每说一句话就续一次，那会变成每句话
                // 都收到一条"把两块板完整读一遍"（2026-09-09 实测重复）。
                shouldAnnounce = pushEnabledAfter
                    && (previous is null
                        || !sameTarget
                        || !wasEnabled
                        || previousInvalid.Length > 0);
            }
            else
            {
                pushEnabledAfter = ReaderCodexPush.Enabled;
            }
        }
        if (writeError is not null)
        {
            await Fail(context, "写入失败：" + writeError).ConfigureAwait(false);
            return;
        }
        // ⚠ 把**这次登记定下的目标**传进去，不让它异步时再去读全局绑定 ——
        //   中间可能已经被另一段对话覆盖，那样提醒就发到别人那里去了
        //   （2026-09-09 Codex 明确点出这一条）。
        // ⚠ 不 await，且用 `CancellationToken.None`：拿这个请求的 token 的话，
        //   响应一返回推送就被取消，表现是"登记成功但全量提醒从来没到"。
        if (shouldAnnounce)
        {
            _ = ReaderCodexPush.NotifyConnectedAsync(
                announceTarget, CancellationToken.None);
        }
        await Ok(context, new JsonObject
        {
            ["ok"] = true,
            ["threadId"] = threadId,
            ["expiresAtMs"] = now + (long)Lifetime.TotalMilliseconds,
            ["pushEnabled"] = pushEnabledAfter,
            // 续期还是接上，回给调用方 —— 否则验收时分不清"没发"和"发丢了"。
            ["announcedConnect"] = shouldAnnounce,
            ["renewedOnly"] = pushEnabledAfter && !shouldAnnounce,
            ["lastNote"] = ReaderCodexPush.LastNote,
            // 渲染循环最近一次失败的原因。**这是它唯一的出口** ——
            // 那个循环的异常没有任何人观察，板子停更跟"状态确实没变"
            // 长得一模一样（2026-09-09 就是这么查了半天）。
            // 没失败过时不出这个字段，免得每次登记都多一行噪音。
            // 焦点判定的现场：没收到 / 还在等停留 / 已确认，三者处置完全不同。
            ["boardFocus"] = ReaderAttentionBoard.FocusDiagnosis(),
            ["boardFlushFailure"] =
                ReaderAttentionBoard.LastFlushFailure.Length == 0
                    ? null
                    : ReaderAttentionBoard.LastFlushFailure,
            ["previousInvalidReason"] = previousInvalid.Length == 0
                ? null : previousInvalid,
        }, cancellationToken).ConfigureAwait(false);
    }

    private static async Task<JsonObject?> ReadBodyAsync(
        HttpContext context,
        CancellationToken cancellationToken)
    {
        try
        {
            using MemoryStream buffer = new();
            await context.Request.Body
                .CopyToAsync(buffer, cancellationToken)
                .ConfigureAwait(false);
            if (buffer.Length is 0 or > MaxBodyBytes) return null;
            return JsonNode.Parse(buffer.ToArray()) as JsonObject;
        }
        catch (Exception)
        {
            return null;
        }
    }

    private static string Text(JsonNode? node, int limit)
    {
        if (node is not JsonValue value
            || !value.TryGetValue(out string? text))
        {
            return string.Empty;
        }
        string trimmed = (text ?? string.Empty).Trim();
        return trimmed[..Math.Min(trimmed.Length, limit)];
    }

    private static async Task Ok(
        HttpContext context, JsonObject body, CancellationToken token)
    {
        context.Response.StatusCode = StatusCodes.Status200OK;
        context.Response.ContentType = "application/json; charset=utf-8";
        await context.Response
            .WriteAsync(body.ToJsonString(), token)
            .ConfigureAwait(false);
    }

    private static async Task Fail(HttpContext context, string detail)
    {
        context.Response.StatusCode = StatusCodes.Status400BadRequest;
        context.Response.ContentType = "application/json; charset=utf-8";
        // detail 原样端出去：折成"操作失败"等于把唯一有用的一句话丢掉。
        await context.Response.WriteAsync(
            new JsonObject { ["ok"] = false, ["detail"] = detail }.ToJsonString())
            .ConfigureAwait(false);
    }
}
