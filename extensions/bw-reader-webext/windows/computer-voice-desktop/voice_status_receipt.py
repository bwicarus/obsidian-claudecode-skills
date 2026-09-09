"""voice_status_receipt — 状态查询的回执落地点（2026-09-09）。

**这个文件解决的是什么。**
Codex 那边报告：它能收到查询、能判断自己的语音标记、也能往已授权位置写结构化
回执 —— 但「具体目的地和回写工具尚未配置」。目的地缺失是卡住整套协议的唯一
一环，这个模块就是那个目的地。

**为什么是脚本而不是让它手写 JSON。**
契约（字段、取值、时间语义）应当由程序保证，不该指望每次都写对。模型只提供
它真正知道的那三样：任务状态、语音状态、证据。其余由这里填。

**时间语义 —— 这是最容易出错、也最贵的一处。**
``respondedAt`` 是**回写时刻**，永远由本脚本填。
``observedAt`` 是**证据产生时刻**，不知道就留空（null）。
⚠ 绝不拿回写时间冒充观测时间：那会让"五分钟前看到的标记"读起来像"刚刚看到"，
于是上层据此做出的重试/放弃判断建立在假新鲜度上。Codex 自己在交接里点了这条，
这里把它变成程序保证而不是约定。

**这里只报告，不改变任何状态。** 不开语音、不发快捷键、不重试。
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

CONTRACT = "reader-voice-status-receipt/1"
RECEIPTS_FILE_NAME = "voice-status-receipts.jsonl"

#: 任务状态的封闭取值。ready = 本任务确实收到并处理了这次查询。
#: ⚠ 它**不**代表音频设备或语音连接已就绪 —— Codex 交接里专门澄清过这点。
TASK_STATUSES = ("ready", "error")

#: 语音状态的封闭取值。
#: active = 系统标记为已激活（**不保证**麦克风/扬声器/传输都正常）；
#: ended  = 系统标记已结束；
#: unknown = 没有足够新鲜的证据。⚠ "没收到语音消息"**不能**推断成 ended。
VOICE_STATUSES = ("active", "ended", "unknown")

#: 单个回执的字节上限。证据字段是自由文本，得有个天花板。
MAX_RECEIPT_BYTES = 8 * 1024
#: 账本保留多少条。这是回执不是历史，攒着没有意义。
MAX_RECEIPTS = 500


def receipts_path(runtime: Path | None = None) -> Path:
    """回执账本的位置。默认在桥的 runtime 目录，与其它回执/日志同处。"""
    if runtime is not None:
        return runtime / RECEIPTS_FILE_NAME
    root = os.environ.get("BW_BRIDGE_RUNTIME")
    base = (
        Path(root)
        if root
        else Path.home() / "bw-computer-voice-bridge" / "runtime"
    )
    return base / RECEIPTS_FILE_NAME


def build_receipt(
    *,
    request_id: str,
    task_status: str,
    voice_status: str,
    evidence: str = "",
    observed_at: str | None = None,
    thread_id: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """组装一条回执。取值非法就抛 ValueError —— 封闭词汇表不做"就近取整"。"""
    if not request_id or len(request_id) > 120:
        raise ValueError("requestId 必填且不超过 120 字符")
    if task_status not in TASK_STATUSES:
        raise ValueError(
            "taskStatus 只能是 %s" % "/".join(TASK_STATUSES))
    if voice_status not in VOICE_STATUSES:
        raise ValueError(
            "voiceStatus 只能是 %s" % "/".join(VOICE_STATUSES))
    stamp = time.gmtime(now if now is not None else time.time())
    return {
        "contract": CONTRACT,
        "requestId": request_id,
        "threadId": thread_id or os.environ.get("CODEX_THREAD_ID") or "",
        "respondedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", stamp),
        "taskStatus": task_status,
        "voiceStatus": voice_status,
        "evidence": str(evidence or "")[:2000],
        # ⚠ 不知道就是 null。见模块头「时间语义」。
        "observedAt": observed_at or None,
    }


def append_receipt(receipt: dict[str, Any],
                   runtime: Path | None = None) -> Path:
    """把回执追加进账本，并把账本裁到 MAX_RECEIPTS 条。"""
    line = json.dumps(receipt, ensure_ascii=False)
    if len(line.encode("utf-8")) > MAX_RECEIPT_BYTES:
        raise ValueError("回执超过 %d 字节" % MAX_RECEIPT_BYTES)
    path = receipts_path(runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    _trim(path)
    return path


def _trim(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) <= MAX_RECEIPTS:
        return
    keep = lines[-MAX_RECEIPTS:]
    try:
        path.write_text("\n".join(keep) + "\n", encoding="utf-8")
    except OSError:
        pass


def read_receipt(
    request_id: str,
    runtime: Path | None = None,
) -> dict[str, Any] | None:
    """按 requestId 找回执。找不到返回 None。

    ⚠ **从尾部往前找**：同一个 requestId 理论上只该有一条，但真出现重复时
    最后写的那条才是最新的答复。
    """
    path = receipts_path(runtime)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line or request_id not in line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(value, dict)
            and value.get("contract") == CONTRACT
            and value.get("requestId") == request_id
        ):
            return value
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="写一条状态查询回执（只报告，不改变任何语音状态）")
    parser.add_argument("--request-id", required=True,
                        help="收到的查询里的 requestId，原样填回")
    parser.add_argument("--task-status", required=True,
                        choices=TASK_STATUSES,
                        help="ready = 本任务收到并处理了这次查询")
    parser.add_argument("--voice-status", required=True,
                        choices=VOICE_STATUSES,
                        help="没有足够新鲜的证据就填 unknown")
    parser.add_argument("--evidence", default="",
                        help="据以判断的依据，例如「系统语音模式标记为已激活」")
    parser.add_argument("--observed-at", default=None,
                        help="证据产生的时刻（ISO8601）。**不知道就别填** —— "
                             "留空比拿现在的时间冒充更有用")
    parser.add_argument("--runtime", type=Path, default=None,
                        help="桥的 runtime 目录（默认自动定位）")
    args = parser.parse_args(argv)
    try:
        receipt = build_receipt(
            request_id=args.request_id,
            task_status=args.task_status,
            voice_status=args.voice_status,
            evidence=args.evidence,
            observed_at=args.observed_at,
        )
        path = append_receipt(receipt, args.runtime)
    except (ValueError, OSError) as error:
        print(json.dumps({"ok": False, "error": str(error)},
                         ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, "written": str(path),
                      "receipt": receipt}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
