"""YouTube 字幕拉取 + Claude CLI 翻译 + SQLite 缓存。

- 拉 YT 自带英文字幕(youtube-transcript-api,免费无 key)
- 一次性整本发给 Claude CLI 翻译(保留行号对齐 + 全局上下文)
- 全局缓存 SQLite(全用户共享,字幕翻译是公开内容)
- 并发锁:同一视频同时多请求只翻 1 次,其余 wait + 用缓存
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

# 加 scripts 进 sys.path 用 ai_client(主项目)
PROJECT_ROOT = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from ai_client import ask  # noqa: E402

DATA_ROOT = Path(os.environ.get("WEBAPP_DATA", "/home/bwicarus/webapp/data"))
DB_PATH = DATA_ROOT / "youtube_subtitles.db"

_LOCK = threading.Lock()
_INFLIGHT: dict[str, threading.Event] = {}


def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB_PATH))
    db.execute("""
        CREATE TABLE IF NOT EXISTS youtube_subtitles (
            video_id TEXT NOT NULL,
            target_lang TEXT NOT NULL,
            segments_json TEXT NOT NULL,
            source_lang TEXT,
            fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
            translated_at TEXT,
            PRIMARY KEY (video_id, target_lang)
        )
    """)
    return db


def _fetch_english(video_id: str) -> tuple[list[dict], str]:
    """拉 YouTube 英文字幕。返回 (segments, source_lang)。"""
    from youtube_transcript_api import YouTubeTranscriptApi
    api = YouTubeTranscriptApi()
    # 优先 en;失败 try 任意可用语言
    try:
        ts = api.fetch(video_id, languages=["en", "en-US", "en-GB"])
        src = "en"
    except Exception:
        # 拉任意能找到的字幕
        tl = api.list(video_id)
        tr = None
        for entry in tl:
            tr = entry
            break
        if tr is None:
            raise RuntimeError("视频无任何字幕")
        ts = tr.fetch()
        src = getattr(tr, "language_code", "?") or "?"
    segs = [
        {
            "start": float(s.start),
            "duration": float(s.duration),
            "en": (s.text or "").replace("\n", " ").strip(),
            "zh": None,
        }
        for s in ts.snippets
    ]
    return segs, src


def _translate_all(segments: list[dict]) -> list[dict]:
    """整本字幕一次翻译。失败的段 zh=None,保留 en 显示。"""
    if not segments:
        return segments
    lines = "\n".join(f"[{i+1}] {s['en']}" for i, s in enumerate(segments))
    prompt = (
        "你是健身视频字幕翻译。把下面英文字幕翻成简洁自然的中文。\n"
        "严格规则:\n"
        "1. 保留每行 [n] 行号,一行一段,不合并不拆分行\n"
        "2. 长度贴近原文,不要过度意译\n"
        "3. 半句话按上下文连贯翻\n"
        "4. 专业术语保留英文但首次出现括号解释:RIR(剩余次数)/RPE(自感强度)/ROM(动作幅度)/"
        "hypertrophy(肌肥大)/eccentric(离心)/concentric(向心)/deload(减载周)\n"
        "5. 动作名第一次出现时给中文译名:bench press(卧推)/squat(深蹲)/deadlift(硬拉) 等\n\n"
        f"原文({len(segments)} 段):\n{lines}\n\n"
        "输出每行一段,带 [n] 行号:"
    )
    try:
        resp = ask(prompt)
    except Exception as e:
        print(f"[subtitles] ai ask failed: {e}", file=sys.stderr)
        return segments
    if not resp:
        return segments
    out_map: dict[int, str] = {}
    for line in resp.splitlines():
        m = re.match(r"^\s*\[(\d+)\]\s*(.+?)\s*$", line)
        if m:
            out_map[int(m.group(1))] = m.group(2)
    for i, s in enumerate(segments):
        zh = out_map.get(i + 1)
        if zh and zh != s["en"]:
            s["zh"] = zh
    return segments


def get_or_translate(video_id: str, target_lang: str = "zh",
                     force: bool = False) -> dict:
    """同步:cache hit 立即返回;cache miss 拉 + 翻译 + 写 cache。

    返回:{status: "ready"|"error", segments?: [...], error?: str, from_cache?: bool}
    """
    if not force:
        db = _db()
        row = db.execute(
            "SELECT segments_json FROM youtube_subtitles "
            "WHERE video_id=? AND target_lang=?",
            (video_id, target_lang),
        ).fetchone()
        db.close()
        if row:
            return {
                "status": "ready",
                "from_cache": True,
                "segments": json.loads(row[0]),
            }
    # 并发锁:同一视频同时多请求,后到的等已发起的完成 → 走 cache
    with _LOCK:
        ev = _INFLIGHT.get(video_id)
        if ev is not None:
            wait_ev = ev
        else:
            wait_ev = None
            _INFLIGHT[video_id] = threading.Event()
    if wait_ev is not None:
        wait_ev.wait(timeout=120)
        return get_or_translate(video_id, target_lang, force=False)
    try:
        segs, src = _fetch_english(video_id)
        segs = _translate_all(segs)
        db = _db()
        db.execute(
            "INSERT OR REPLACE INTO youtube_subtitles "
            "(video_id, target_lang, segments_json, source_lang, translated_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (video_id, target_lang, json.dumps(segs, ensure_ascii=False), src),
        )
        db.commit()
        db.close()
        return {
            "status": "ready",
            "from_cache": False,
            "segments": segs,
            "source_lang": src,
        }
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
    finally:
        with _LOCK:
            ev = _INFLIGHT.pop(video_id, None)
        if ev is not None:
            ev.set()


def has_cached(video_id: str, target_lang: str = "zh") -> bool:
    db = _db()
    row = db.execute(
        "SELECT 1 FROM youtube_subtitles WHERE video_id=? AND target_lang=?",
        (video_id, target_lang),
    ).fetchone()
    db.close()
    return row is not None
