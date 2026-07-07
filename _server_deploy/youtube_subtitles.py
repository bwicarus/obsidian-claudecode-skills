"""YouTube 字幕拉取 + Claude CLI 翻译 + SQLite 缓存。

- 拉 YT 自带英文字幕(youtube-transcript-api,免费无 key)
- 一次性整本发给 Claude CLI 翻译(保留行号对齐 + 全局上下文)
- 全局缓存 SQLite(全用户共享,字幕翻译是公开内容)
- 并发锁:同一视频同时多请求只翻 1 次,其余直接回 running 由前端轮询
- 失败负缓存(内存,10min TTL):无字幕/限流视频不被前端反复触发重打外网
- 首次生成默认 nowait 后台化:立即回 {status:"running"},前端每 3s 轮询
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

# 失败负缓存(只进内存,**绝不写 youtube_subtitles 表**:错误行会被 ready/has_cached 误读毒化成功路径)。
# TTL 必须短:前端常规路径无 force 出口,只有失败提示上的「重试」按钮带 force=1。
_ERR_TTL = 600.0   # 10min
_ERR_CACHE: dict[str, tuple[float, str]] = {}   # cache_key -> (写入时刻, 错误信息)


def _err_get(cache_key: str) -> str | None:
    ent = _ERR_CACHE.get(cache_key)
    if ent and time.time() - ent[0] < _ERR_TTL:
        return ent[1]
    return None


def _err_put(cache_key: str, msg: str) -> None:
    now = time.time()
    # 顺手清过期项,防常驻进程慢性膨胀
    for k in [k for k, (ts, _) in _ERR_CACHE.items() if now - ts >= _ERR_TTL]:
        _ERR_CACHE.pop(k, None)
    _ERR_CACHE[cache_key] = (now, msg)


def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB_PATH))
    db.execute("PRAGMA journal_mode = WAL")      # 并发读写不互锁
    db.execute("PRAGMA busy_timeout = 5000")
    db.execute("PRAGMA synchronous = NORMAL")
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
    # 自动生成字幕(ASR)的 duration 常严重过长 → 段与段大量重叠,前端按 duration 判定当前段会高亮到过时的早段
    # (完全对不上时间轴)。用「下一段 start」截断本段 duration(不改 start),让 segments 不重叠、时间轴干净。
    for i in range(len(segs) - 1):
        gap = segs[i + 1]["start"] - segs[i]["start"]
        if gap > 0 and segs[i]["duration"] > gap:
            segs[i]["duration"] = round(gap, 3)
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
        sys.path.insert(0, os.path.join(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"), "scripts"))
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


# ─────────── AI 重组断句翻译(把机器按时长切的碎段按语义重组成完整句再整句翻译)───────────
# YouTube 字幕(尤其自动字幕)是按显示时长机械切的,一句话常被切成几段 → 逐段翻译对着半句翻,
# 译文生硬不连贯。改为:全文碎段带段号给 AI,让它按语义重组成完整句 + 整句翻译,并标注每句覆盖
# 哪几个原段([起-止]);后端据此合并原段——新句 start=首段 start、end=末段 end(时间轴仍精准),
# 英文拼接、中文用整句译文。断句由 AI 决定,时间戳来自原段,两全其美。
_RESEG_PROMPT_HEAD = (
    "你是专业视频字幕翻译。下面英文字幕已按原始时间轴切成碎片段,每段前 [n] 是段号。\n"
    "这些碎片是机器按显示时长切的,常把一句话切成好几段。请你:\n"
    "1. 按**语义**把碎片重组成完整、自然的句子(一个完整意思一句,别太长,便于当字幕看)。\n"
    "2. 每个重组句翻成简洁自然的中文(贴合原意,不过度意译)。\n"
    "3. 术语保留英文并首次出现括号解释(如 hypertrophy(肌肥大)/RIR(剩余次数)/eccentric(离心));动作名给中文译名。\n"
    "**输出格式**:每行一句,格式为「起段号-止段号<TAB>中文翻译」,例:\n"
    "1-1\t这是一个 3。\n"
    "2-4\t它写得很潦草、以极低的 28×28 分辨率渲染,但你的大脑毫不费力就能认出它是 3。\n"
    "**铁律**:段号范围必须连续覆盖全部段、不重叠、不遗漏(上一句止段+1 = 下一句起段);只输出「段号范围+TAB+中文」,不要输出英文原文、不要别的解释。\n\n"
)


def _gemini_call(prompt: str, key: str, max_tokens: int = 32000) -> str:
    """调 Gemini 2.5 Flash 返回纯文本(抽出复用;字幕翻译/重组都用)。失败抛异常。"""
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           "gemini-2.5-flash:generateContent?key=" + key)
    r = _req.post(url, json={
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": max_tokens,
                             "thinkingConfig": {"thinkingBudget": 0}},
    }, timeout=180)
    try:
        sys.path.insert(0, os.path.join(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"), "scripts"))
        from google_api_quota import log_usage
        log_usage("gemini", 1, "generateContent:flash", note=f"reseg status={r.status_code}")
    except Exception:
        pass
    if r.status_code != 200:
        raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"Gemini API: {data['error'].get('message')}")
    cand = (data.get("candidates") or [{}])[0]
    text = "".join(p["text"] for p in (cand.get("content") or {}).get("parts", []) if "text" in p)
    if not text:
        raise RuntimeError(f"Gemini empty (finishReason={cand.get('finishReason')})")
    return text


def _merge_range(segments: list[dict], a: int, b: int, zh) -> dict:
    """合并原段 [a..b](1-based,含端点)成一个新段:start=首段 start、end=末段 end,英文拼接。"""
    a = max(1, a); b = min(len(segments), b)
    s0, s1 = segments[a - 1], segments[b - 1]
    start = s0["start"]; end = s1["start"] + s1["duration"]
    en = " ".join((segments[k]["en"] or "").strip() for k in range(a - 1, b)).strip()
    return {"start": round(start, 3), "duration": round(max(0.1, end - start), 3), "en": en, "zh": zh}


_RESEG_LINE = re.compile(r"^\s*\[?(\d+)\]?\s*[-–—~]\s*\[?(\d+)\]?\s*[\t|:：]+\s*(.+?)\s*$")


def _gemini_resegment(segments: list[dict], key: str) -> list[dict] | None:
    """一块碎段 → AI 重组断句翻译 → 新的完整句段(时间戳来自原段)。失败/覆盖太低返回 None。"""
    n = len(segments)
    if n == 0:
        return []
    lines = "\n".join(f"[{i+1}] {s['en']}" for i, s in enumerate(segments))
    text = _gemini_call(_RESEG_PROMPT_HEAD + f"原文({n} 段):\n{lines}\n\n输出(段号连续覆盖 1..{n}):", key)
    ranges = []
    for line in text.splitlines():
        m = _RESEG_LINE.match(line)
        if m:
            a, b, zh = int(m.group(1)), int(m.group(2)), m.group(3).strip()
            if 1 <= a <= b <= n and zh:
                ranges.append((a, b, zh))
    if not ranges:
        return None
    ranges.sort()
    out, cursor, covered = [], 1, 0
    for a, b, zh in ranges:
        a = max(a, cursor)          # 裁掉与已处理段的重叠
        if a > b:
            continue
        if a > cursor:              # 漏段(AI 没覆盖)→ 补一段,无译文(拼英文,少见)
            out.append(_merge_range(segments, cursor, a - 1, None))
        out.append(_merge_range(segments, a, b, zh))
        covered += (b - a + 1)
        cursor = b + 1
    if cursor <= n:
        out.append(_merge_range(segments, cursor, n, None))
    if covered < n * 0.7:           # AI 覆盖太少 → 判失败,退回逐段翻译
        return None
    return out


def _ai_resegment_translate(segments: list[dict]) -> list[dict] | None:
    """整本 → AI 重组断句翻译。长视频按段分块(块内重组,块边界只影响少数句)。任一块挂 → None(退回逐段)。"""
    if not segments or not GEMINI_KEY_FILE.exists():
        return None
    try:
        key = GEMINI_KEY_FILE.read_text().strip()
    except Exception:
        return None
    CHUNK = 220
    result: list[dict] = []
    for i in range(0, len(segments), CHUNK):
        try:
            part = _gemini_resegment(segments[i:i + CHUNK], key)
        except Exception as e:
            print(f"[subtitles] resegment chunk {i} failed: {e}", file=sys.stderr)
            return None
        if part is None:
            return None
        result.extend(part)
    return result if result else None


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
        sys.path.insert(0, os.path.join(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"), "scripts", "vocab"))
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
        sys.path.insert(0, os.path.join(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"), "scripts"))
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
                     source: str = "auto", force: bool = False,
                     nowait: bool = True) -> dict:
    """cache hit 立即返回;cache miss 默认 nowait=True:后台线程拉+翻+写 cache,
    先回 {status:"running"} 由前端每 3s 轮询(fitness.py 调用处不传 nowait 即后台化;
    summarize_video 内部传 nowait=False 同步等字幕)。失败进内存负缓存 10min,force=1 是唯一出口。

    source:
      "auto" — YouTube 自带 caption + Google 机翻(快,免费)
      "hq"   — **优先 YouTube 英文字幕原文**(准确),无字幕才退回 Cloud STT;再用 **LLM 精翻**
               (Gemini/Claude,带上下文+术语)。比 stt 质量好得多:STT 转录本身易错,而频道多有准确字幕。
      "stt"  — 纯 Cloud Speech-to-Text(仅无字幕视频用;转录易错,保留作底层 fallback)

    返回:{status: "ready"|"running"|"error", segments?: [...], error?: str, from_cache?: bool, source: str}
    """
    if source not in ("auto", "stt", "hq"):
        return {"status": "error", "error": f"unknown source {source!r}"}
    cache_key = f"{video_id}:{source}"
    if force:
        _ERR_CACHE.pop(cache_key, None)   # 「重试」按钮给负缓存一个出口
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
        err = _err_get(cache_key)
        if err:   # 负缓存 TTL 内秒回,不重打 YT/翻译 API
            return {"status": "error", "error": err, "from_cache": True, "source": source}
    with _LOCK:
        ev = _INFLIGHT.get(cache_key)
        if ev is not None:
            wait_ev = ev
        else:
            wait_ev = None
            _INFLIGHT[cache_key] = threading.Event()
    if wait_ev is not None:
        if nowait:   # 轮询绝不落回 wait 占 gunicorn 线程,直接报 running
            return {"status": "running", "source": source}
        wait_ev.wait(timeout=600)
        return get_or_translate(video_id, target_lang, source, force=False, nowait=False)

    def _work() -> dict:
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
                segs = _ai_resegment_translate(segs) or _translate_hq(segs)   # 优先 AI 重组断句+整句翻译;失败退回逐段精翻
            elif source == "stt":
                segs, src = _fetch_stt(video_id)
                segs = _ai_resegment_translate(segs) or _translate_all(segs)
            else:
                segs, src = _fetch_english(video_id)
                segs = _ai_resegment_translate(segs) or _translate_all(segs)   # 优先 AI 重组断句;失败退回逐段
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
            msg = f"{type(e).__name__}: {e}"
            _err_put(cache_key, msg)   # 失败只进内存负缓存,绝不写表
            return {"status": "error", "error": msg, "source": source}
        finally:
            with _LOCK:
                ev2 = _INFLIGHT.pop(cache_key, None)
            if ev2 is not None:
                ev2.set()

    if nowait:
        threading.Thread(target=_work, daemon=True, name=f"yt-sub-{cache_key}").start()
        return {"status": "running", "source": source}
    return _work()


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
        sys.path.insert(0, os.path.join(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"), "scripts"))
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


def summarize_video(video_id: str, target_lang: str = "zh", force: bool = False,
                    nowait: bool = True) -> dict:
    """按字幕 AI 总结要点 + 时间锚点。全局缓存,inflight 去重 + nowait 后台化 + 失败负缓存
    (同 get_or_translate 机制;4 个业务早退也统一进负缓存,防前端反复触发)。

    返回:{status:"ready"|"running"|"error", points?:[{t,text}], from_cache?, error?}
    """
    cache_key = f"sum:{video_id}:{target_lang}"
    if force:
        _ERR_CACHE.pop(cache_key, None)
    if not force:
        db = _db()
        row = db.execute(
            "SELECT points_json FROM youtube_summaries WHERE video_id=? AND target_lang=?",
            (video_id, target_lang),
        ).fetchone()
        db.close()
        if row:
            return {"status": "ready", "from_cache": True, "points": json.loads(row[0])}
        err = _err_get(cache_key)
        if err:
            return {"status": "error", "error": err, "from_cache": True}
    with _LOCK:
        ev = _INFLIGHT.get(cache_key)
        wait_ev = ev if ev is not None else None
        if ev is None:
            _INFLIGHT[cache_key] = threading.Event()
    if wait_ev is not None:
        if nowait:
            return {"status": "running"}
        wait_ev.wait(timeout=300)
        return summarize_video(video_id, target_lang, force=False, nowait=False)

    def _err(msg: str) -> dict:
        _err_put(cache_key, msg)   # 业务早退/异常统一负缓存,绝不写表
        return {"status": "error", "error": msg}

    def _work() -> dict:
        try:
            # 复用字幕(及其缓存);后台线程内同步等字幕(nowait=False)。总结喂英文原文(更准),夹带 zh 兜底
            res = get_or_translate(video_id, target_lang=target_lang, source="auto", nowait=False)
            if res.get("status") != "ready":
                return _err(res.get("error", "字幕获取失败"))
            segs = res.get("segments") or []
            if not segs:
                return _err("该视频无字幕,无法总结")
            max_t = int(segs[-1]["start"] + segs[-1].get("duration", 0))
            lines = "\n".join(
                f"[{int(s['start'])}] {(s.get('en') or s.get('zh') or '').strip()}"
                for s in segs if (s.get("en") or s.get("zh"))
            )
            prompt = _SUMMARY_PROMPT + f"字幕({len(segs)} 行):\n{lines}\n\n现在输出要点(每行 '秒数 | 要点'):"
            text = _summary_ai(prompt)
            if not text.strip():
                return _err("AI 无输出")
            points = _parse_points(text, max_t)
            if not points:
                return _err("AI 输出无法解析为要点")
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
            return _err(f"{type(e).__name__}: {e}")
        finally:
            with _LOCK:
                ev2 = _INFLIGHT.pop(cache_key, None)
            if ev2 is not None:
                ev2.set()

    if nowait:
        threading.Thread(target=_work, daemon=True, name=f"yt-sum-{video_id}").start()
        return {"status": "running"}
    return _work()


def has_cached(video_id: str, target_lang: str = "zh", source: str = "auto") -> bool:
    db = _db()
    row = db.execute(
        "SELECT 1 FROM youtube_subtitles WHERE video_id=? AND target_lang=? AND source=?",
        (video_id, target_lang, source),
    ).fetchone()
    db.close()
    return row is not None
