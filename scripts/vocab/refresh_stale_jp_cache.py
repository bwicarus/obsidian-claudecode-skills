#!/usr/bin/env python3
"""refresh_stale_jp_cache.py — 夜间把日语词典缓存里的旧版词条按当前 prompt 重生成。

为什么要它(2026-09-07 用户:「我要的是自动的解决方案」):lookup_jp 的 stale-while-revalidate 只在用户点到
那个词时才后台升级;外来语(片假名)旧条目缺 source_word(英文源词),用户看到的就是"没有英文原文"(ピラミッド),
等他点到再升级等于手动方案。这个脚本每晚由 Windows 计划任务「JP Dict Refresh」(bin/jp_dict_refresh.cmd)跑一批:
  ① 片假名且没 source_word 键(用户能直接看见缺失) → ② 其余 pv 落后的(含汉字词的伪朋友修正)
每次上限 --limit(默认 200;Haiku low ≈ 2–4 s/条,约 10 分钟);AI 后端连败 --max-fail 次即停
(登录失效/限流时别空烧)。新鲜度规则与 lookup_jp 共用 dict_sources._jp_entry_fresh,不另写一份。
状态写 state/dict-cache-refresh.json(上次运行、成功/失败/剩余);stdout 由 .cmd 重定向到 state/logs/jp-dict-refresh.log。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _p in (str(HERE), str(HERE.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import dict_sources as ds  # noqa: E402
import ai_client  # noqa: E402
from config import STATE_DIR  # noqa: E402

STATUS_FILE = STATE_DIR / "dict-cache-refresh.json"
# 片假名词(含中点/长音)。只有这类词的 source_word 缺失是用户直接看得见的
KATAKANA_RE = re.compile(r"^[゠-ヿㇰ-ㇿ・･ー]+$")


def _now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def stale_entries():
    """遍历缓存目录里的 jp-*.json,按 mtime 新→旧(最近查过的先修),产出 (word, entry, path)。"""
    cache_dir = ds._cache_dir()
    files = sorted(cache_dir.glob("jp-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files:
        try:
            entry = json.loads(path.read_text("utf-8"))
        except Exception:
            continue
        if not isinstance(entry, dict) or entry.get("_err") or entry.get("_404"):
            continue
        word = str(entry.get("word") or "").strip()
        if not word or ds._jp_entry_fresh(word, entry):
            continue
        yield word, entry, path


def build_queue(limit: int) -> tuple[list, int, int]:
    first: list = []
    rest: list = []
    for word, entry, path in stale_entries():
        if KATAKANA_RE.match(word) and "source_word" not in entry:
            first.append((word, entry, path))
        else:
            rest.append((word, entry, path))
    queue = (first + rest)[: max(0, limit)]
    return queue, len(first), len(rest)


def run_batch(words: list, model: str = "haiku") -> dict:
    """子进程入口:一问多词;没回来的词逐个单查兜底。返回 {word: source_word 或 None(失败)}。
    每个进程各有一把 ai_client._claude_lock,所以多进程才能真并行(线程池会被那把锁串成一条)。"""
    got = {}
    try:
        batch = ds.jp_ai_fetch_batch(words, model, None)
    except Exception:
        batch = {}
    for w in words:
        e = batch.get(w)
        if not e:
            try:
                e = ds._jp_ai_fetch(w, "", model, None)
            except Exception:
                e = None
        got[w] = (e.get("source_word", "") if e else None)
    return got


def write_status(payload: dict) -> None:
    try:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATUS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATUS_FILE)
    except Exception as exc:  # 状态文件写不了不影响刷新本身,但要出声
        print(f"  ! 状态文件写失败: {exc}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="夜间刷新日语词典缓存里的旧版词条")
    ap.add_argument("--limit", type=int, default=200, help="本次最多重生成多少条(默认 200)")
    ap.add_argument("--max-fail", type=int, default=3, help="AI 连败多少次就停(默认 3)")
    ap.add_argument("--sleep", type=float, default=0.5, help="每条之间歇多久秒(默认 0.5)")
    ap.add_argument("--dry-run", action="store_true", help="只列队列不调 AI")
    ap.add_argument("--batch", type=int, default=8, help="一问几词(默认 8;1=逐词老路径)")
    ap.add_argument("--workers", type=int, default=1, help="并行进程数(默认 1;即时全量刷新可开 4-6)")
    ap.add_argument("--model", default="haiku", help="Claude 模型(默认 haiku;auto 路由失败时自动改道 Codex)")
    args = ap.parse_args(argv)

    health = ai_client.ai_health()
    cred = health.get("claude_credentials") or {}
    print(f"[{_now()}] AI 健康: claude_cooldown={health['claude_in_cooldown']} "
          f"refresh_token_expired={cred.get('refresh_expired')} token_file={health['claude_token_file']} "
          f"codex_default={health['codex_default_model']}")
    if health.get("claude_auth_error"):
        print(f"  上次 Claude 登录失效: {health['claude_auth_error']}")

    queue, n_first, n_rest = build_queue(args.limit)
    total = n_first + n_rest
    print(f"旧版词条 {total}(片假名缺源词 {n_first} / 其余 {n_rest}),本次上限 {args.limit},排入 {len(queue)}")
    if args.dry_run:
        for word, entry, _ in queue:
            print(f"  - {word} pv={entry.get('pv')} source_word={'有' if 'source_word' in entry else '无'}")
        return 0

    ok = fail = consecutive = 0
    stopped_reason = ""
    t_start = time.time()
    if args.batch > 1 or args.workers > 1:
        batch = max(1, args.batch)
        chunks = [[w for w, _, _ in queue[i:i + batch]] for i in range(0, len(queue), batch)]
        print(f"批量模式: {len(chunks)} 批 × {batch} 词, {max(1, args.workers)} 进程并行")
        done_batches = 0
        try:
            with ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
                pending = set()
                next_i = 0
                while next_i < len(chunks) or pending:
                    while next_i < len(chunks) and len(pending) < max(1, args.workers) and not stopped_reason:
                        pending.add(pool.submit(run_batch, chunks[next_i], args.model))
                        next_i += 1
                    if not pending:
                        break
                    finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for fut in finished:
                        done_batches += 1
                        try:
                            got = fut.result()
                        except Exception as exc:
                            got = {}
                            print(f"  ! 批次异常: {type(exc).__name__}: {exc}")
                        n_ok = sum(1 for v in got.values() if v is not None)
                        n_fail = len(got) - n_ok if got else 0
                        ok += n_ok
                        fail += n_fail
                        shown = ", ".join(f"{w}→{v or '-'}" for w, v in list(got.items())[:8] if v is not None)
                        missed = [w for w, v in got.items() if v is None]
                        print(f"  [{done_batches}/{len(chunks)}] ✓{n_ok} ✗{n_fail} {shown}"
                              + (f" | 失败: {' '.join(missed)}" if missed else "") + f" ({time.time() - t_start:.0f}s)")
                        consecutive = 0 if n_ok else consecutive + 1
                        if consecutive >= args.max_fail and not stopped_reason:
                            stopped_reason = f"AI 后端连续 {consecutive} 批全败"
                            print(f"{stopped_reason},不再提交新批次(等在途批次结束);看 state/ai-health.json 与 state/logs/ai_calls.log")
                    if stopped_reason and not pending:
                        break
        except KeyboardInterrupt:
            stopped_reason = "手动中断"
            print("手动中断")
        remaining = max(0, total - ok)
        summary = {
            "last_run": _now(), "duration_s": round(time.time() - t_start, 1), "queued": len(queue),
            "ok": ok, "failed": fail, "remaining_stale": remaining, "batch": batch, "workers": max(1, args.workers),
            "katakana_missing_source_word_before": n_first, "stopped_reason": stopped_reason,
            "ai_health": {k: v for k, v in health.items() if k != "claude_credentials"},
        }
        write_status(summary)
        print(f"[{_now()}] 完成: 成功 {ok} / 失败 {fail} / 剩余旧版 {remaining} / 用时 {summary['duration_s']}s"
              + (f" / 停止原因: {stopped_reason}" if stopped_reason else ""))
        return 0 if (ok or not queue) else 1
    try:
        for word, entry, _ in queue:
            t0 = time.time()
            data = None
            err = ""
            try:
                data = ds._jp_ai_fetch(word, "", "haiku", None)
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
            dt = time.time() - t0
            if data:
                ok += 1
                consecutive = 0
                print(f"  ✓ {word} source_word={data.get('source_word', '')!r} {dt:.1f}s")
            else:
                fail += 1
                consecutive += 1
                print(f"  ✗ {word} 失败 {dt:.1f}s {err}")
                if consecutive >= args.max_fail:
                    stopped_reason = f"AI 后端连败 {consecutive} 次"
                    print(f"{stopped_reason},停止;看 state/ai-health.json 与 state/logs/ai_calls.log")
                    break
            if args.sleep > 0:
                time.sleep(args.sleep)
    except KeyboardInterrupt:
        stopped_reason = "手动中断"
        print("手动中断")

    remaining = max(0, total - ok)
    summary = {
        "last_run": _now(),
        "duration_s": round(time.time() - t_start, 1),
        "queued": len(queue),
        "ok": ok,
        "failed": fail,
        "remaining_stale": remaining,
        "katakana_missing_source_word_before": n_first,
        "stopped_reason": stopped_reason,
        "ai_health": {k: v for k, v in health.items() if k != "claude_credentials"},
    }
    write_status(summary)
    print(f"[{_now()}] 完成: 成功 {ok} / 失败 {fail} / 剩余旧版 {remaining} / 用时 {summary['duration_s']}s"
          + (f" / 停止原因: {stopped_reason}" if stopped_reason else ""))
    if queue and ok == 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
