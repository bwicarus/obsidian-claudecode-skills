"""voice_start_step — 语音入口的一次尝试（2026-09-09 用户拍板）。

外部程序按状态回报要求跑这个脚本时，它做**一次**尝试，然后如实回报结果。
成没成由调用方决定要不要再跑一次。

**为什么是一次性，而不是设一个"保持开着"的意图。**
保活是**持续**语义：设上之后语音一旦结束，收敛循环几秒内又会把它拉回来 ——
那会跟语音智能关闭互相打架，表现是"关了又开、来回抖"，最难查的那一种。
入口要的是"试一次，成了就成了，不成让调用方决定再试还是放弃"。

**守卫不在这里，在桥那边。**
已经在通话就不动作（那个快捷键是**切换**，按下去会挂断）；台账读不到也不按
（那是"不知道"，不是"没在通话"）。守卫长在执行动作的那一侧，这样即使被连着
跑两次，第二次也不会做出相反的事。

⚠ **前置条件也由桥那侧准备**（2026-09-09 用户：「即使是一次性启动也需要能
拉起 codex 啊」）。桥的 startVoiceOnce 走的是跟保活**同一条**链，链的最前面
就是"拉起 Codex、等它出现、按已运行时长沉降"。第一版端点自己拼了一条按键链、
恰好漏掉这一步，于是一次性方式下 Codex 没在跑时两次尝试必然全废。

reason 是封闭词汇表：already-active / started / cooldown / not-confirmed /
unknown / voice-off 来自桥，unreachable 由这里产生（连不上桥）。
其中 **cooldown 不是失败** —— 是"刚按过，这次我们选择不按"，稍等再看。

**等待时长是暂定的，靠记录来调**（用户 2026-09-09：「一开始应该把时间搞得
长一点，一会查看他的记录就知道时间改多少合适了」）。所以每次尝试都往
``voice-start-attempts.jsonl`` 写一条带**实际耗时**的记录 —— 攒够之后照分布
把 ATTEMPT_TIMEOUT_SECONDS 收到该收的地方。宁可一开始等久些：等久了只是慢，
等短了会把本来会成功的那次判成失败，然后去按第二下 —— 而那一下可能正好
把刚起来的通话关掉。

退出码：0 = 已进入语音（或本来就在）；1 = 这次没成（可以再试一次）；
2 = **别再试了**（原因在 NO_POINT_RETRYING 里，比如电脑锁屏，等也不会成）。
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import voice_autoclose

#: 一次尝试的等待上限。
#:
#: **第一批真实样本（2026-09-10，用户实测两次成功）**：3.67 / 3.69 / 5.79 秒。
#: 跟 CodexVoiceActivity 里实测的"台账 active → 渲染侧出声 = 2.25 秒"一致。
#:
#: ⚠ **但据此收到 10 秒是错的** —— 那三次 Codex 都**已经在跑**。真正撑起这个
#: 数字的是冷启动那条路：WaitForUniqueReadyAsync 20 秒 + 沉降 5 秒 + 观察窗
#: 10 秒 ≈ 35 秒，而它**一个样本都还没有**。拿热样本去收超时，正是模块头警告的
#: 那件事：等短了会把本来会成功的那次判成失败，然后去按第二下 —— 而那一下可能
#: 正好把刚起来的通话关掉。
#:
#: 所以维持 60 秒，等出现一条冷启动样本（形态是 10~35 秒那一档）再谈收。
ATTEMPT_TIMEOUT_SECONDS = 60.0

ATTEMPTS_FILE_NAME = "voice-start-attempts.jsonl"
#: 记录保留条数。这是给调参用的样本，不是历史档案。
MAX_ATTEMPTS_KEPT = 400


def attempts_path(runtime: Path | None = None) -> Path:
    if runtime is not None:
        return runtime / ATTEMPTS_FILE_NAME
    root = os.environ.get("BW_BRIDGE_RUNTIME")
    base = (
        Path(root)
        if root
        else Path.home() / "bw-computer-voice-bridge" / "runtime"
    )
    return base / ATTEMPTS_FILE_NAME


def record_attempt(entry: dict[str, object],
                   runtime: Path | None = None) -> Path | None:
    """把这次尝试记下来。**记录失败不影响主流程** —— 但也不静默：
    调用方拿得到 None，据此知道这次没留下样本。"""
    path = attempts_path(runtime)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > MAX_ATTEMPTS_KEPT:
            path.write_text(
                "\n".join(lines[-MAX_ATTEMPTS_KEPT:]) + "\n",
                encoding="utf-8")
    except OSError:
        return None
    return path


#: 冷却期挡下时最多等多久再问一次。桥会在回答里给 cooldownSeconds，
#: 这个只是"桥给的数离谱时"的上界，不是我们自己猜的冷却长度。
MAX_COOLDOWN_WAIT_SECONDS = 30.0

#: 桥不在时在这一层重试几次、每次隔多久。
#: 覆盖一次 Direct 重装的空窗（实测约 40 秒）还留有余量。
TRANSPORT_RETRIES = 4
TRANSPORT_RETRY_SECONDS = 15.0


def _transport_blip(result: dict[str, object]) -> bool:
    """这次失败是"桥不在"，而不是"试了没接通"。

    502/503/504 来自 tailscale serve 转不到后端；unreachable 是连都没连上。
    两者都说明**请求没有到达执行方**，跟语音开不开得起来无关。
    """
    if result.get("reason") == "unreachable":
        return True
    status = result.get("httpStatus")
    return isinstance(status, int) and status in (502, 503, 504)


#: 桥会给出的 reason（封闭词汇表）。不在表里的一律当"没说清"。
BRIDGE_REASONS = frozenset({
    "already-active", "started", "cooldown", "no-desktop", "not-confirmed",
    "unknown", "voice-off",
})

#: 这些原因**不是"试了没成"，而是"根本没得试"** —— 不该花掉重试预算。
#:
#: ``no-desktop`` = 桌面锁着 / 会话断开，注入的按键没有前台窗口可落。
#: 2026-09-11 实测：冷启动后每 30 秒按一次、连按 10 轮跨 9.5 分钟全部
#: ``not-confirmed``，而 Codex 主窗口一直在；同一时刻 OpenInputDesktop 打不开、
#: GetForegroundWindow() == 0。这类局面里"再试一次"是确定无效的，
#: 而每一次要烧掉 22 秒确认窗口，还会把"两次不成就放弃"的预算用光 ——
#: 于是真正该说的那句话（去解锁电脑）永远说不出来。
NO_POINT_RETRYING = frozenset({"no-desktop"})


def _post_once(url: str, timeout: float) -> dict[str, object]:
    """问桥一次。任何失败都折成**带 reason 且说得出原因**的回答，不抛。

    ⚠ 回答里没有 reason 时不能就这么记下去（2026-09-10 被自己咬到）：
    两条样本记成 pressed/confirmed/reason 全 null，于是事后完全无从判断
    那次到底发生了什么 —— 而"记了一条说不出原因的失败"跟没记一样。
    所以这里给不认识的回答补上 reason 和一段原文,状态码也一并留下。
    """
    data = json.dumps({"startVoiceOnce": True}).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"})
    status: object = None
    body = b""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            body = response.read() or b""
            result = json.loads(body or b"{}")
    except urllib.error.HTTPError as error:
        status = error.code
        try:
            body = error.read() or b""
        except OSError:
            body = b""
        try:
            result = json.loads(body or b"{}")
        except ValueError:
            result = {"ok": False, "reason": "http",
                      "detail": "回应不是 JSON"}
    except OSError as error:
        return {"ok": False, "reason": "unreachable", "httpStatus": None,
                "detail": "连不上桥：%s" % str(error)[:160]}
    if not isinstance(result, dict):
        result = {"ok": False}
    result["httpStatus"] = status
    if result.get("reason") not in BRIDGE_REASONS:
        # 桥的 400 只带 detail(没有 reason);空体则连 detail 都没有。
        # 两种都要说得出话来,而不是留三个 null。
        result.setdefault("detail", "回应里没有可辨认的 reason：%s"
                          % (body[:200].decode("utf-8", "replace") or "空回应"))
        result["reason"] = "unexpected-reply"
        result["ok"] = False
    return result


def start_once(
    endpoint: str | None = None,
    timeout: float = ATTEMPT_TIMEOUT_SECONDS,
    runtime: Path | None = None,
    clock=None,
    sleeper=None,
) -> dict[str, object]:
    """请桥按一次并等确认。返回桥的原样回答，外加这次的实际耗时。

    ⚠ **冷却期要等过去再问，不能当成一次尝试用掉**（2026-09-09）。
    桥的守卫是"上一次按键的确认还没走完之前不许再按"，长度 = 确认窗口 10 秒
    + 沉降 3 秒 = 13 秒；而第一次尝试正是在确认窗口耗尽时返回的。所以调用方
    紧接着跑的第二次必然落在冷却里 —— 不等就问，等于把"重试一次"变成一次
    假重试：什么都没按，却把重试预算花掉了。
    """
    now = clock or time.monotonic
    rest = sleeper or time.sleep
    url = endpoint or voice_autoclose.ENDPOINT
    started = now()

    def sample(result: dict[str, object], elapsed: float,
               waited: float) -> None:
        record_attempt({
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "elapsedSeconds": elapsed,
            "timeoutSeconds": timeout,
            "cooldownWaitSeconds": waited,
            "pressed": result.get("pressed"),
            "confirmed": result.get("confirmed"),
            "reason": result.get("reason"),
            # 说不出原因的样本等于没样本 —— 状态码与原文一并留下。
            "httpStatus": result.get("httpStatus"),
            "detail": str(result.get("detail") or "")[:200] or None,
        }, runtime)

    # 桥暂时不在（重装/重启）时**不算用掉一次机会**。
    #
    # ⚠ 2026-09-10 实测：一次入口通知恰好落在桥的维护窗口里（安装 Direct 用了
    # 40 秒），两次尝试都拿到 502 空回应，于是 Codex 按说明放弃并上报失败 ——
    # 而语音其实完全开得起来。「两次不成就放弃」那条规则说的是**试了没接通**，
    # 不是**根本没试成**；把传输抖动算进去，等于让一次例行升级吃掉整个预算。
    # 所以这一层自己消化：短暂等待后重问，仍不通才交回上层。
    for attempt in range(TRANSPORT_RETRIES + 1):
        result = _post_once(url, timeout)
        if not _transport_blip(result) or attempt == TRANSPORT_RETRIES:
            break
        sample(result, round(now() - started, 2), 0.0)   # 每次都留样本
        rest(TRANSPORT_RETRY_SECONDS)
    waited = 0.0
    if result.get("reason") == "cooldown":
        # 冷却那一次也留样本 —— 否则记录里看不出我们等过，
        # 调超时的时候会把等待算进"按一次要多久"。
        sample(result, round(now() - started, 2), waited)
        given = result.get("cooldownSeconds")
        waited = min(
            float(given)
            if isinstance(given, (int, float)) and not isinstance(given, bool)
            and given > 0
            else MAX_COOLDOWN_WAIT_SECONDS,
            MAX_COOLDOWN_WAIT_SECONDS,
        )
        rest(waited)
        result = _post_once(url, timeout)
    elapsed = round(now() - started, 2)
    result["elapsedSeconds"] = elapsed
    if waited:
        result["cooldownWaitSeconds"] = waited
    # 样本：之后照这些数字把超时收到该收的地方。
    sample(result, elapsed, waited)
    return result


#: 一次调用里最多按几次。原来这个数写在给 AI 的指令里（"false 就再跑一次，
#: 不要运行第三次"），于是每多一次就多一个模型回合。收进脚本后 AI 只跑一行。
DEFAULT_ATTEMPTS = 2


def run(
    endpoint: str | None = None,
    timeout: float = ATTEMPT_TIMEOUT_SECONDS,
    runtime: Path | None = None,
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    report_failure: bool = False,
    clock=None,
    sleeper=None,
    reporter=None,
) -> dict[str, object]:
    """按最多 ``attempts`` 次，直到确认；都没成且要求上报时替 AI 报失败。

    ⚠ 这是**一个进程里跑完整套**，而不是让 AI 跑一次、看一眼、再跑一次。
    9 月 5–12 日的会话记录：voice_start_step 117 次中位 7.2 s，而 AI 为了等它
    结束又空写了 170 次 stdin、中位 5 s —— 每次等都是一整个模型回合。
    重试次数、放弃上报都收进来之后，指令只剩一行，AI 只用一个回合。

    ``NO_POINT_RETRYING`` 仍然生效：桌面锁着时第二次必然一样，不按。
    """
    attempts = max(1, int(attempts))
    result: dict[str, object] = {}
    for index in range(attempts):
        result = start_once(endpoint, timeout, runtime,
                            clock=clock, sleeper=sleeper)
        result["attempt"] = index + 1
        if result.get("confirmed") is True:
            return result
        if result.get("reason") in NO_POINT_RETRYING:
            break
    result["attemptsUsed"] = result.get("attempt", attempts)
    if report_failure:
        # 放弃必须留下痕迹（voice_start_failed 模块头写着为什么）。
        # 原来这一步是让 AI 另跑一个脚本 —— 又一个回合。这里直接进程内做掉。
        report = reporter or _report_failure
        try:
            result["failureReport"] = report(
                attempts=int(result["attemptsUsed"]),
                detail=str(result.get("reason") or ""),
                runtime=runtime,
            )
        except Exception as exc:  # 上报失败不该把主结果一起吞掉
            result["failureReport"] = {"ok": False, "error": str(exc)[:200]}
    return result


def _report_failure(*, attempts: int, detail: str, runtime: Path | None):
    import voice_start_failed
    return voice_start_failed.report(
        attempts=attempts, detail=detail, runtime=runtime)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="语音入口：按到确认为止（已在通话中则不做任何动作）")
    parser.add_argument("--endpoint", default=None)
    parser.add_argument("--timeout", type=float,
                        default=ATTEMPT_TIMEOUT_SECONDS)
    parser.add_argument("--runtime", type=Path, default=None)
    parser.add_argument("--attempts", type=int, default=1,
                        help="最多按几次（默认 1，保持旧行为）")
    parser.add_argument("--report-failure", action="store_true",
                        help="都没成时替调用方写失败回执（等价于跑 "
                             "voice_start_failed.py）")
    args = parser.parse_args(argv)
    result = run(args.endpoint, args.timeout, args.runtime,
                 attempts=args.attempts, report_failure=args.report_failure)
    print(json.dumps(result, ensure_ascii=False))
    if result.get("confirmed") is True:
        return 0
    # 2 = **别再试了**（不是"这次没成"）。分开报是因为调用方对这两种要做的事
    # 完全相反：1 该再按一次，2 该停下来把原因说给人听。
    if result.get("reason") in NO_POINT_RETRYING:
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
