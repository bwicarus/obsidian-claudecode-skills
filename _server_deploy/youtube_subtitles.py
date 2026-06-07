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
# ai_client→config 用 CLAUDE_PROJECT 当 Claude CLI 的 cwd(-C),没设会回退 Windows 默认 C:\claude
# → Claude 兜底翻译/总结在非 webapp 场景(独立脚本/测试)报 "No such file C:\claude"。
# 这里在 import ai_client(会连带 import config)前补好默认,webapp 已由 .env 设则不覆盖。
os.environ.setdefault("CLAUDE_PROJECT", str(PROJECT_ROOT))

from ai_client import ask  # noqa: E402

import requests as _req  # 跟 ai_client 的依赖分开

DATA_ROOT = Path(os.environ.get("WEBAPP_DATA", "/home/bwicarus/webapp/data"))
DB_PATH = DATA_ROOT / "youtube_subtitles.db"
GEMINI_KEY_FILE = Path("/home/bwicarus/.config/gemini-api-key")

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
    # migrate: add source column + change PK to (video_id, target_lang, source)
    cols = [r[1] for r in db.execute("PRAGMA table_info(youtube_subtitles)")]
    if "source" not in cols:
        db.execute(
            "ALTER TABLE youtube_subtitles ADD COLUMN source TEXT NOT NULL DEFAULT 'auto'"
        )
    # 唯一索引保证 (video_id, lang, source) 唯一
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uniq_subtitle "
        "ON youtube_subtitles(video_id, target_lang, source)"
    )
    # AI 要点总结(按字幕,带时间锚点;全局缓存,公开视频内容)
    db.execute("""
        CREATE TABLE IF NOT EXISTS youtube_summaries (
            video_id TEXT NOT NULL,
            target_lang TEXT NOT NULL,
            points_json TEXT NOT NULL,   -- [{"t": 秒, "text": "要点"}]
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
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


_TRANSLATE_PROMPT_HEAD = (
    "你是健身视频字幕翻译。把下面英文字幕翻成简洁自然的中文。\n"
    "严格规则:\n"
    "1. 保留每行 [n] 行号,一行一段,不合并不拆分行\n"
    "2. 长度贴近原文,不要过度意译\n"
    "3. 半句话按上下文连贯翻\n"
    "4. 专业术语保留英文但首次出现括号解释:RIR(剩余次数)/RPE(自感强度)/ROM(动作幅度)/"
    "hypertrophy(肌肥大)/eccentric(离心)/concentric(向心)/deload(减载周)\n"
    "5. 动作名第一次出现时给中文译名:bench press(卧推)/squat(深蹲)/deadlift(硬拉) 等\n\n"
)


def _parse_lines(text: str, total: int, segments: list[dict]) -> None:
    out_map: dict[int, str] = {}
    for line in text.splitlines():
        m = re.match(r"^\s*\[(\d+)\]\s*(.+?)\s*$", line)
        if m:
            out_map[int(m.group(1))] = m.group(2)
    for i, s in enumerate(segments):
        zh = out_map.get(i + 1)
        if zh and zh != s["en"]:
            s["zh"] = zh


def _translate_gemini_flash(segments: list[dict], key: str) -> bool:
    """走 Gemini 2.5 Flash 翻译。返回是否成功(任一段翻译到就算)。"""
    lines = "\n".join(f"[{i+1}] {s['en']}" for i, s in enumerate(segments))
    prompt = _TRANSLATE_PROMPT_HEAD + f"原文({len(segments)} 段):\n{lines}\n\n输出每行一段,带 [n] 行号:"
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.5-flash:generateContent?key=" + key
    )
    r = _req.post(
        url,
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 32000,
                "thinkingConfig": {"thinkingBudget": 0},  # 字幕不需要 reasoning
            },
        },
        timeout=120,
    )
    # quota log
    try:
        sys.path.insert(0, "/home/bwicarus/claude/scripts")
        from google_api_quota import log_usage
        log_usage("gemini", 1, "generateContent:flash",
                  note=f"{len(segments)} segs, status={r.status_code}")
    except Exception:
        pass
    if r.status_code != 200:
        raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"Gemini API: {data['error'].get('message')}")
    cand = (data.get("candidates") or [{}])[0]
    text = ""
    for part in (cand.get("content") or {}).get("parts", []):
        if "text" in part:
            text += part["text"]
    if not text:
        raise RuntimeError(f"Gemini empty response (finishReason={cand.get('finishReason')})")
    _parse_lines(text, len(segments), segments)
    return any(s.get("zh") for s in segments)


def _translate_claude(segments: list[dict]) -> bool:
    """走 ai_client.ask()(Claude/Codex 按 ai-settings.json)。Fallback 用。"""
    lines = "\n".join(f"[{i+1}] {s['en']}" for i, s in enumerate(segments))
    prompt = _TRANSLATE_PROMPT_HEAD + f"原文({len(segments)} 段):\n{lines}\n\n输出每行一段,带 [n] 行号:"
    try:
        resp = ask(prompt)
    except Exception as e:
        print(f"[subtitles] Claude ask failed: {e}", file=sys.stderr)
        return False
    if not resp:
        return False
    _parse_lines(resp, len(segments), segments)
    return any(s.get("zh") for s in segments)


def _translate_google(segments: list[dict]) -> bool:
    """Google Cloud Translation 批量(EN→ZH 快+质量好+走 GCP 赠金)。返回是否翻到。"""
    try:
        sys.path.insert(0, "/home/bwicarus/claude/scripts/vocab")
        from translate import gtranslate_batch
    except Exception:
        return False
    res = gtranslate_batch([s["en"] for s in segments])
    if not res:
        return False
    for s, zh in zip(segments, res):
        if zh and zh != s["en"]:
            s["zh"] = zh
    try:
        sys.path.insert(0, "/home/bwicarus/claude/scripts")
        from google_api_quota import log_usage
        log_usage("translate", len(segments), "v2:batch", note=f"{len(segments)} segs")
    except Exception:
        pass
    return any(s.get("zh") for s in segments)


def _translate_all(segments: list[dict]) -> list[dict]:
    """整本字幕一次翻译。优先 Google Translate(快+质量好+走赠金),再 Gemini Flash,最后 Claude。"""
    if not segments:
        return segments
    # 优先 Google Cloud Translation(Gemini 赠金常耗尽 429,Google 走 GCP 赠金更稳)
    try:
        if _translate_google(segments):
            return segments
        print("[subtitles] Google 翻译未生效,试 Gemini/Claude", file=sys.stderr)
    except Exception as e:
        print(f"[subtitles] Google failed ({e}),试 Gemini/Claude", file=sys.stderr)
    # 其次 Gemini Flash
    if GEMINI_KEY_FILE.exists():
        try:
            key = GEMINI_KEY_FILE.read_text().strip()
            if _translate_gemini_flash(segments, key):
                return segments
            print("[subtitles] Gemini 无翻译输出,fallback Claude", file=sys.stderr)
        except Exception as e:
            print(f"[subtitles] Gemini failed ({e}),fallback Claude", file=sys.stderr)
    # fallback Claude
    _translate_claude(segments)
    return segments


def _translate_hq(segments: list[dict]) -> list[dict]:
    """HQ 翻译:LLM 优先(Gemini 2.5 Flash → Claude),带全文上下文、术语规则,质量/连贯性优先。
    不走 Google 机翻(机翻偏直译生硬)。源英文已是准确的 YouTube 字幕原文 → LLM 据此精翻。
    注:Gemini 若 429(AI Studio 预付费余额耗尽,见 fitness 踩坑#1)→ 落 Claude。
    Claude 对长字幕**分批**(每 80 段一次),避免一次 200+ 行超 nginx 300s 超时;翻完整本缓存,下次秒出。"""
    if not segments:
        return segments
    # HQ 的核心诉求 = **AI 处理准确字幕原文**(机翻太生硬)。所以 AI 优先,Google 仅在 AI 全挂时兜底。
    # 1) Gemini(AI,快;429 余额耗尽则跳过 → 见踩坑#1)
    if GEMINI_KEY_FILE.exists():
        try:
            key = GEMINI_KEY_FILE.read_text().strip()
            if _translate_gemini_flash(segments, key):
                return segments
            print("[subtitles] HQ Gemini 无输出,转 Claude(分批)", file=sys.stderr)
        except Exception as e:
            print(f"[subtitles] HQ Gemini failed ({e}),转 Claude(分批)", file=sys.stderr)
    # 2) Claude AI 精翻,分批(每 60 段一次,避免一次 200+ 行超 nginx 300s;断连也由 inflight+cache 兜住)
    BATCH = 60
    for i in range(0, len(segments), BATCH):
        try:
            _translate_claude(segments[i:i + BATCH])
        except Exception as e:
            print(f"[subtitles] HQ Claude batch {i} failed: {e}", file=sys.stderr)
    if any(s.get("zh") for s in segments):
        return segments
    # 3) AI 全挂的最后兜底:Google 机翻(至少有译文,不留空)
    print("[subtitles] HQ AI 全失败,兜底 Google 机翻", file=sys.stderr)
    try:
        _translate_google(segments)
    except Exception:
        pass
    return segments


def _fetch_stt(video_id: str) -> tuple[list[dict], str]:
    """走 Cloud Speech-to-Text(慢 + 烧赠金,但 YT auto-caption 质量太差时用)。

    返回 (segments, source_lang='en')。失败抛异常。
    """
    sys.path.insert(0, str(Path(__file__).parent))
    from youtube_speech import transcribe_youtube
    segs = transcribe_youtube(video_id, language="en-US")
    return segs, "en"


def get_or_translate(video_id: str, target_lang: str = "zh",
                     source: str = "auto", force: bool = False) -> dict:
    """同步:cache hit 立即返回;cache miss 拉 + 翻译 + 写 cache。

    source:
      "auto" — YouTube 自带 caption + Google 机翻(快,免费)
      "hq"   — **优先 YouTube 英文字幕原文**(准确),无字幕才退回 Cloud STT;再用 **LLM 精翻**
               (Gemini/Claude,带上下文+术语)。比 stt 质量好得多:STT 转录本身易错,而频道多有准确字幕。
      "stt"  — 纯 Cloud Speech-to-Text(仅无字幕视频用;转录易错,保留作底层 fallback)

    返回:{status: "ready"|"error", segments?: [...], error?: str, from_cache?: bool, source: str}
    """
    if source not in ("auto", "stt", "hq"):
        return {"status": "error", "error": f"unknown source {source!r}"}
    cache_key = f"{video_id}:{source}"
    if not force:
        db = _db()
        row = db.execute(
            "SELECT segments_json FROM youtube_subtitles "
            "WHERE video_id=? AND target_lang=? AND source=?",
            (video_id, target_lang, source),
        ).fetchone()
        db.close()
        if row:
            return {
                "status": "ready",
                "from_cache": True,
                "source": source,
                "segments": json.loads(row[0]),
            }
    with _LOCK:
        ev = _INFLIGHT.get(cache_key)
        if ev is not None:
            wait_ev = ev
        else:
            wait_ev = None
            _INFLIGHT[cache_key] = threading.Event()
    if wait_ev is not None:
        wait_ev.wait(timeout=600)
        return get_or_translate(video_id, target_lang, source, force=False)
    try:
        if source == "hq":
            # 优先 YouTube 英文字幕原文(准确);无字幕才退回 STT(易错)
            try:
                segs, src = _fetch_english(video_id)
                src = "yt-caption"
            except Exception as cap_err:
                try:
                    segs, src = _fetch_stt(video_id)
                except Exception:
                    raise cap_err   # 字幕和 STT 都失败 → 报字幕缺失更直观
            segs = _translate_hq(segs)   # LLM 精翻
        elif source == "stt":
            segs, src = _fetch_stt(video_id)
            segs = _translate_all(segs)
        else:
            segs, src = _fetch_english(video_id)
            segs = _translate_all(segs)
        db = _db()
        db.execute(
            "INSERT OR REPLACE INTO youtube_subtitles "
            "(video_id, target_lang, source, segments_json, source_lang, translated_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now'))",
            (video_id, target_lang, source, json.dumps(segs, ensure_ascii=False), src),
        )
        db.commit()
        db.close()
        return {
            "status": "ready",
            "from_cache": False,
            "source": source,
            "segments": segs,
            "source_lang": src,
        }
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
    finally:
        with _LOCK:
            ev = _INFLIGHT.pop(cache_key, None)
        if ev is not None:
            ev.set()


# ───────────────────────── AI 要点总结(带时间锚点) ─────────────────────────
_SUMMARY_PROMPT = (
    "你是专业健身教练。下面是一个健身教学视频的字幕,每行开头 [N] 是该句在视频里**开始的秒数**。\n"
    "请提炼 6-10 个最有价值的要点(动作技术细节 / 常见错误 / 训练安排建议),用简洁中文。\n"
    "每个要点必须给出它在视频里**开始讲解**的时间(秒,取自对应字幕行开头的 [N])。\n"
    "严格规则:\n"
    "1. 每行一个要点,格式严格为: 秒数 | 要点中文\n"
    "2. 秒数是纯整数(例如 125),不要写成 mm:ss\n"
    "3. 按时间从早到晚排序\n"
    "4. 只输出要点行,不要标题/编号/解释/空行/markdown\n"
    "5. 专业术语保留英文并首次括号解释:RIR(剩余次数)/ROM(动作幅度)/hypertrophy(肌肥大)/eccentric(离心)等\n\n"
)


def _parse_points(text: str, max_t: int) -> list[dict]:
    """解析 '秒数 | 要点' 行 → [{t, text}]。容忍 [N]/全角竖线/冒号分隔。"""
    pts: list[dict] = []
    seen: set[str] = set()
    for line in text.splitlines():
        m = re.match(r"^\s*\[?\s*(\d+)\s*\]?\s*[|｜:：\-]\s*(.+?)\s*$", line)
        if not m:
            continue
        t = int(m.group(1))
        txt = re.sub(r"^[\-\*\d\.、)\s]+", "", m.group(2)).strip()  # 去掉残留的项目符号/编号
        if not txt or txt in seen:
            continue
        seen.add(txt)
        if max_t and t > max_t:
            t = max(0, max_t - 2)   # 越界 → 夹到视频末尾附近
        pts.append({"t": t, "text": txt})
    pts.sort(key=lambda p: p["t"])
    return pts


def _gemini_text(prompt: str, key: str, max_tokens: int = 8000) -> str:
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           "gemini-2.5-flash:generateContent?key=" + key)
    r = _req.post(url, json={
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": max_tokens,
                             "thinkingConfig": {"thinkingBudget": 0}},
    }, timeout=120)
    try:
        sys.path.insert(0, "/home/bwicarus/claude/scripts")
        from google_api_quota import log_usage
        log_usage("gemini", 1, "generateContent:summary", note=f"status={r.status_code}")
    except Exception:
        pass
    if r.status_code != 200:
        raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:150]}")
    cand = (r.json().get("candidates") or [{}])[0]
    return "".join(p["text"] for p in (cand.get("content") or {}).get("parts", []) if "text" in p)


def _summary_ai(prompt: str) -> str:
    """Gemini Flash 优先(快),失败落 Claude(ai_client.ask)。"""
    if GEMINI_KEY_FILE.exists():
        try:
            t = _gemini_text(prompt, GEMINI_KEY_FILE.read_text().strip())
            if t.strip():
                return t
            print("[summary] Gemini 空输出,转 Claude", file=sys.stderr)
        except Exception as e:
            print(f"[summary] Gemini failed ({e}),转 Claude", file=sys.stderr)
    try:
        return ask(prompt) or ""
    except Exception as e:
        print(f"[summary] Claude failed: {e}", file=sys.stderr)
        return ""


def summarize_video(video_id: str, target_lang: str = "zh", force: bool = False) -> dict:
    """按字幕 AI 总结要点 + 时间锚点。全局缓存,inflight 去重(同 get_or_translate 机制)。

    返回:{status:"ready"|"error", points?:[{t,text}], from_cache?, error?}
    """
    if not force:
        db = _db()
        row = db.execute(
            "SELECT points_json FROM youtube_summaries WHERE video_id=? AND target_lang=?",
            (video_id, target_lang),
        ).fetchone()
        db.close()
        if row:
            return {"status": "ready", "from_cache": True, "points": json.loads(row[0])}
    cache_key = f"sum:{video_id}:{target_lang}"
    with _LOCK:
        ev = _INFLIGHT.get(cache_key)
        wait_ev = ev if ev is not None else None
        if ev is None:
            _INFLIGHT[cache_key] = threading.Event()
    if wait_ev is not None:
        wait_ev.wait(timeout=300)
        return summarize_video(video_id, target_lang, force=False)
    try:
        # 复用字幕(及其缓存);总结喂英文原文(更准),夹带 zh 兜底
        res = get_or_translate(video_id, target_lang=target_lang, source="auto")
        if res.get("status") != "ready":
            return {"status": "error", "error": res.get("error", "字幕获取失败")}
        segs = res.get("segments") or []
        if not segs:
            return {"status": "error", "error": "该视频无字幕,无法总结"}
        max_t = int(segs[-1]["start"] + segs[-1].get("duration", 0))
        lines = "\n".join(
            f"[{int(s['start'])}] {(s.get('en') or s.get('zh') or '').strip()}"
            for s in segs if (s.get("en") or s.get("zh"))
        )
        prompt = _SUMMARY_PROMPT + f"字幕({len(segs)} 行):\n{lines}\n\n现在输出要点(每行 '秒数 | 要点'):"
        text = _summary_ai(prompt)
        if not text.strip():
            return {"status": "error", "error": "AI 无输出"}
        points = _parse_points(text, max_t)
        if not points:
            return {"status": "error", "error": "AI 输出无法解析为要点"}
        db = _db()
        db.execute(
            "INSERT OR REPLACE INTO youtube_summaries "
            "(video_id, target_lang, points_json, created_at) VALUES (?, ?, ?, datetime('now'))",
            (video_id, target_lang, json.dumps(points, ensure_ascii=False)),
        )
        db.commit()
        db.close()
        return {"status": "ready", "from_cache": False, "points": points}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
    finally:
        with _LOCK:
            ev = _INFLIGHT.pop(cache_key, None)
        if ev is not None:
            ev.set()


def has_cached(video_id: str, target_lang: str = "zh", source: str = "auto") -> bool:
    db = _db()
    row = db.execute(
        "SELECT 1 FROM youtube_subtitles WHERE video_id=? AND target_lang=? AND source=?",
        (video_id, target_lang, source),
    ).fetchone()
    db.close()
    return row is not None
