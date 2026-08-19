#!/usr/bin/env python3
"""把没落进 Anki 的复习补投回去（C 组 #17 的 G5）。

## 它解决什么

阅读器里的每一次评分都会写两个地方：
  - `state/reader-review-events.jsonl` —— 事件日志，**总是**写（只记不改调度）
  - Anki —— 经 `/pdf/api/review-answer` → `answerCards`，真 FSRS 调度

第二个会失败：Anki 没起来、正在 sync、iPad 离线。客户端会把评分塞进 outbox 重投，
但 outbox 是**会话内**的努力 —— 浏览器一关、App 一杀，那条就没了。事件日志是磁盘上
的，它活得更久。这个脚本就是那条更久的补救路径。

## 怎么判断"没落进 Anki"

按 `aid` 跟 `/pdf/api/review-answer` 的幂等台账对账：
  - `state/review-answers-seen.json` —— 已完成的 aid 列表
  - `state/review-answer-receipts.json` —— 每个 aid 的回执（含 state）

台账里是 done 的就跳过。**不查 Anki 的 revlog 来判断** —— 那看起来更"权威"，
但对不上号：revlog 记的是 Anki 自己的时间戳，而补投的时间戳必然不同（见下）。

## 一条不能忽略的事实

`answerCards` **没有时间戳参数**。补投一条三天前的复习，Anki 会把它记成"现在"。
所以：
  - 事件日志里的 `reviewedAt` 是**真实复习时刻**，Anki 里没有这个信息
  - 补投改变的是"这张卡下次什么时候到期"，而不是"历史上什么时候复习过"
  - 因此补投**越早越好**，隔太久的补投会把间隔算错（FSRS 按"距上次复习多久"算）

脚本对超过 `--max-age-days` 的事件默认**只报告不补投**，并说明原因。

## 用法

    # 看有什么要补（默认，不改任何东西）
    python3 scripts/replay_review_events.py

    # 真的补投
    python3 scripts/replay_review_events.py --apply

    # 放宽/收紧年龄门槛
    python3 scripts/replay_review_events.py --apply --max-age-days 3
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

_STATE = config.PROJECT_DIR / "state"
EVENTS = _STATE / "reader-review-events.jsonl"
SEEN = _STATE / "review-answers-seen.json"
RECEIPTS = _STATE / "review-answer-receipts.json"
REPLAYED = _STATE / "reader-review-events.replayed.json"

ANKI_URL = config.ANKI_CONNECT_URL


def _anki(action: str, params: dict | None = None, timeout: int = 20):
    payload = json.dumps(
        {"action": action, "version": 6, "params": params or {}}
    ).encode("utf-8")
    request = urllib.request.Request(
        ANKI_URL, data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8"))
    if body.get("error"):
        raise RuntimeError(str(body["error"])[:200])
    return body.get("result")


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _settled_aids() -> set[str]:
    """已经落进 Anki 的 aid。"""
    settled: set[str] = set()
    seen = _load_json(SEEN, [])
    if isinstance(seen, list):
        settled.update(str(x) for x in seen)
    receipts = _load_json(RECEIPTS, {})
    entries = receipts.get("entries") if isinstance(receipts, dict) else None
    if isinstance(entries, dict):
        for aid, entry in entries.items():
            if isinstance(entry, dict) and entry.get("state") == "done":
                settled.add(str(aid))
    return settled


def _events() -> list[dict]:
    if not EVENTS.exists():
        return []
    rows = []
    for line in EVENTS.read_text("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--apply", action="store_true",
        help="真的补投（默认只报告 —— answerCards 会改你的复习进度）",
    )
    parser.add_argument(
        "--max-age-days", type=float, default=7.0,
        help="超过这个天数的事件只报告不补投（FSRS 按距上次复习多久算间隔，"
             "补投太晚会把间隔算错）",
    )
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()

    events = _events()
    if not events:
        print("事件日志是空的 —— 没有可补投的复习。")
        return 0

    settled = _settled_aids()
    replayed = set(_load_json(REPLAYED, []))
    now_ms = int(time.time() * 1000)
    max_age_ms = args.max_age_days * 24 * 60 * 60 * 1000

    pending, too_old, no_card, done = [], [], [], 0
    for row in events:
        aid = str(row.get("aid") or "")
        if not aid:
            # 早于 2026-08-19 的事件没有 aid —— 跟台账对不上号，宁可不投。
            no_card.append((row, "事件没有 aid（早期格式），无法与台账对账"))
            continue
        if aid in settled or aid in replayed:
            done += 1
            continue
        card_id = str(row.get("ankiCardId") or "").strip()
        if not card_id.isdigit():
            no_card.append((row, "没有 Anki 卡号，投不到具体哪张卡"))
            continue
        age = now_ms - int(row.get("reviewedAt") or 0)
        if age > max_age_ms:
            too_old.append((row, f"距今 {age / 86400000:.1f} 天"))
            continue
        pending.append(row)

    print(f"事件 {len(events)} 条：已落库 {done}、待补投 {len(pending)}、"
          f"太旧 {len(too_old)}、无法定位 {len(no_card)}")
    for row, why in no_card[:5]:
        print(f"  跳过 {row.get('id')}：{why}")
    for row, why in too_old[:5]:
        print(f"  太旧 {row.get('id')}（{why}）—— 补投会把 FSRS 间隔算错")

    if not pending:
        return 0
    if not args.apply:
        print("\n（这是预演。加 --apply 才会真的补投 —— answerCards 会真改调度。）")
        for row in pending[: args.limit]:
            print(f"  待补 card={row.get('ankiCardId')} ease={row.get('ease')} "
                  f"queue={row.get('queue')} 真实时刻="
                  f"{time.strftime('%m-%d %H:%M', time.localtime(int(row.get('reviewedAt') or 0) / 1000))}")
        return 0

    ok, failed = 0, 0
    for row in pending[: args.limit]:
        card_id = int(row["ankiCardId"])
        ease = int(row.get("ease") or 0)
        if ease not in (1, 2, 3, 4):
            failed += 1
            continue
        try:
            # ⚠ 没有时间戳参数 —— Anki 会把它记成"现在"。事件日志里才有真实时刻。
            result = _anki("answerCards", {"answers": [
                {"cardId": card_id, "ease": ease}
            ]})
            if not (isinstance(result, list) and result and result[0]):
                raise RuntimeError(f"answerCards 未接受：{result!r}")
            replayed.add(str(row.get("aid")))
            ok += 1
        except (RuntimeError, urllib.error.URLError, OSError) as exc:
            print(f"  补投失败 card={card_id}：{str(exc)[:120]}", file=sys.stderr)
            failed += 1

    try:
        REPLAYED.parent.mkdir(parents=True, exist_ok=True)
        REPLAYED.write_text(
            json.dumps(sorted(replayed)[-8000:], ensure_ascii=False), "utf-8"
        )
    except OSError as exc:
        # 记不下"补过了"比补投失败更危险：下次会重复投。
        print(f"⚠ 补投记录写入失败，下次可能重复投：{exc}", file=sys.stderr)
        return 1

    print(f"补投完成：成功 {ok}、失败 {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
