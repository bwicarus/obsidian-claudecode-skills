"""ambient_jev.py — 环境旁听的 jev 判断（``/api/ambient/*``，2026-09-26）。

iPad 持续旁听周围的声音：本机转写（SFSpeechRecognizer）+ FluidAudio 说话人分离，
按「一段话」为窗口把带说话人标签的转写送到这里。服务器（不是 App）持有 jev 密钥，
一次调用同时问四道选择题：

  meaningful  有没有对用户有意义的内容
  danger      是否有即时危险          → App 立刻开始持续录音 + 通知
  question    有没有要事后解决的疑问 / 待办 → 第二层 jev 分给 AI 或记为待跟进
  record      要不要保存（文字 / 连原声）   → App 保存本窗原声，服务器写 Obsidian

有意义的窗口攒够了由 AI 写一段「旁听摘要」（``state/ambient/context.json``），
供之后的对话当上下文（MCP ``voice_brief`` 会带上它）。

jev 调用在独立模块 ``jev_judge.py``：不依赖语音进程，任何时候都能判断（用户 2026-09-26 要求）。
判断来源由 ``source`` 字段标明（ipad-mic / ipad-call / 以后的 watch、glasses），题目与动作不随来源变。

诊断：每次判断、每个后台动作的成败都写 ``state/ambient/log.jsonl``；App 端的提前
退出走 client-log。没有一处「悄悄什么都不做」。
"""
from __future__ import annotations

import json
import re
import os
import sys
import threading
import time
from pathlib import Path

from flask import Blueprint, jsonify, request, session

import jev_judge
from ambient_people import AmbientPeople, PeopleError

CLAUDE_DIR = Path(os.environ.get("CLAUDE_PROJECT", str(Path(__file__).resolve().parents[1])))
bp = Blueprint("ambient_jev", __name__, url_prefix="/api/ambient")

MAX_UTTERANCES = 240
MAX_TRANSCRIPT_CHARS = 9000
SUMMARY_EVERY_WINDOWS = 6
SUMMARY_MAX_AGE_SECONDS = 3600
FEED_LIMIT = 200

_LOCK = threading.Lock()
# 旁听摘要的「读 → 改 → 写」要整段互斥（判断后的后台线程可能并发）；_LOCK 只管单次文件读写。
_CONTEXT_LOCK = threading.RLock()
_SUMMARY_LOCK = threading.Lock()   # 同一时间只写一份摘要（后到的线程直接跳过，下次窗口再触发）


# ─────────────────────────── 判断题 ───────────────────────────

QUESTIONS = {
    "meaningful": {
        "type": "choice",
        "instructions": (
            "判断这段旁听转写是否含有对用户有意义的内容。只看本段，前情摘要只用来理解指代。"
            "转写是自动识别的，可能有错字漏字；识别乱码、电视广播背景声、与用户无关的路人交谈都不算有意义。"),
        "criteria": {
            "noise": "没有可理解的内容：静音、噪声、识别乱码、零碎词语。",
            "background": "有可理解的语言，但与用户无关或没有信息量：路人闲谈、电视广播、寒暄客套。",
            "meaningful": "对用户有信息量：用户参与的实质对话、别人对用户说的事、约定安排、知识讨论、"
                          "情绪或健康状况的线索。",
        },
    },
    "danger": {
        "type": "choice",
        "instructions": (
            "判断这段转写是否显示用户此刻可能面临需要立即留证或求助的危险。"
            "只依据转写里的明确迹象；谈论、转述危险话题（电影、新闻、玩笑、游戏）不算危险。"),
        "criteria": {
            "none": "没有危险迹象，或只是谈论危险话题。",
            "possible": "有令人担忧但不确定的迹象：激烈争吵、带威胁的语气、语境不清的呼救。"
                        "程序会保存这一段原声并提醒用户留意。",
            "immediate": "明确的即时危险：对用户的威胁或攻击、呼救、事故、急病、索要财物。"
                         "程序会立刻开始持续录音并通知用户。",
        },
    },
    "question": {
        "type": "choice",
        "instructions": (
            "判断这段转写里是否有需要事后解决的疑问或待办。只算与用户有关的："
            "用户提出的、别人问用户的、或用户被托付的。别人之间的问答不算。"),
        "criteria": {
            "none": "没有需要处理的疑问或事项。",
            "question": "有一个值得查证或解答的疑问（知识、事实、做法），答案可以事后查出来告诉用户。",
            "task": "有需要用户之后去做或跟进的事项：约定、托付、截止时间、要买要交的东西。",
        },
    },
    "record": {
        "type": "choice",
        "instructions": "判断这段内容是否值得保存下来供以后回看。",
        "criteria": {
            "skip": "不需要保存。",
            "transcript": "保存文字记录即可：有信息量，但原话措辞不重要。",
            "audio": "值得连同原声一起保存：原话措辞重要（约定、承诺、指示、争执、医嘱、数字密集的信息），"
                     "或内容重要而文字识别明显不可靠。",
        },
    },
}

# 第二层：上一层判出「有疑问」之后，决定交给谁。
ROUTE_QUESTION = {
    "type": "choice",
    "instructions": "上一层已判断这段转写里有值得解答的疑问。判断这个疑问该交给谁处理；只看最主要的那个疑问。",
    "criteria": {
        "knowledge": "通用知识或事实问题，不需要用户的私人资料就能回答。程序会让 AI 写一段简短解答放进旁听记录。",
        "personal": "涉及用户自己的日程、资料、书、卡片或设备，需要能操作用户系统的助手处理。"
                    "程序只记录为待跟进事项，不自动执行。",
        "unclear": "疑问指代不清或信息不足，交给谁都无法处理；只记录原话。",
    },
}


# ─────────────────────────── 路径 / 落盘 ───────────────────────────

def state_dir() -> Path:
    path = CLAUDE_DIR / "state" / "ambient"
    path.mkdir(parents=True, exist_ok=True)
    return path


def vault_dir() -> Path | None:
    root = os.environ.get("OBSIDIAN_VAULT")
    if not root:
        return None
    return Path(root) / "AI助手专用" / "环境旁听"


def _append_jsonl(path: Path, row: dict) -> None:
    with _LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def log_event(event: str, **fields) -> None:
    """诊断出口：成功与失败都留一行。"""
    row = {"t": round(time.time() * 1000), "event": event}
    row.update(fields)
    try:
        _append_jsonl(state_dir() / "log.jsonl", row)
    except OSError as exc:  # 诊断通道自己坏了也要出声，但不能打断被诊断的功能
        print(f"[ambient] log write failed: {exc}", file=sys.stderr, flush=True)


def feed_append(kind: str, **fields) -> dict:
    row = {"t": round(time.time() * 1000), "kind": kind}
    row.update(fields)
    _append_jsonl(state_dir() / "feed.jsonl", row)
    return row


def feed_since(since_ms: int, limit: int = FEED_LIMIT) -> list[dict]:
    path = state_dir() / "feed.jsonl"
    if not path.is_file():
        return []
    with _LOCK:
        lines = path.read_text(encoding="utf-8").splitlines()[-2000:]
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if int(row.get("t") or 0) > since_ms:
            rows.append(row)
    return rows[-limit:]


def load_context() -> dict:
    path = state_dir() / "context.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"summary": "", "updatedAt": 0, "pending": []}


def save_context(context: dict) -> None:
    path = state_dir() / "context.json"
    tmp = path.with_suffix(".tmp")
    with _LOCK:
        tmp.write_text(json.dumps(context, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)


def vault_append(title: str, body: str) -> bool:
    folder = vault_dir()
    if folder is None:
        log_event("vault_skipped", reason="OBSIDIAN_VAULT 未设置")
        return False
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (time.strftime("%Y-%m-%d") + ".md")
        with _LOCK:
            fresh = not path.exists()
            with path.open("a", encoding="utf-8") as handle:
                if fresh:
                    handle.write(f"# 环境旁听 {time.strftime('%Y-%m-%d')}\n\n")
                handle.write(f"## {time.strftime('%H:%M')} {title}\n\n{body.rstrip()}\n\n")
        return True
    except OSError as exc:
        log_event("vault_failed", error=str(exc))
        return False


# ─────────────────────────── jev（独立模块 jev_judge） ───────────────────────────

JevUnavailable = jev_judge.JevUnavailable
top_choice = jev_judge.top_choice


def jev_predict(state: str, questions: dict) -> dict[str, dict]:
    return jev_judge.predict(state, questions)


# 测试替换点
_predict = jev_predict


def _ask_ai(prompt: str) -> str:
    scripts = str(CLAUDE_DIR / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import ai_client  # noqa: WPS433 — 懒加载：网页进程启动时不去解析 AI 配置
    return (ai_client.ask(prompt) or "").strip()


_ai = _ask_ai
_spawn = lambda target, *args: threading.Thread(target=target, args=args, daemon=True).start()  # noqa: E731


# ─────────────────────────── 窗口 → 状态文本 ───────────────────────────

def _clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def normalize_window(body: dict) -> dict:
    utterances = body.get("utterances")
    if not isinstance(utterances, list) or not utterances:
        raise ValueError("utterances_required")
    if len(utterances) > MAX_UTTERANCES:
        raise ValueError("too_many_utterances")
    rows = []
    total = 0
    for item in utterances:
        if not isinstance(item, dict):
            raise ValueError("bad_utterance")
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        total += len(text)
        rows.append({"speaker": str(item.get("speaker") or "?")[:24], "isUser": bool(item.get("isUser")),
                     "text": text[:1200], "t0": float(item.get("t0") or 0), "t1": float(item.get("t1") or 0),
                     "slotKey": str(item.get("slotKey") or "")[:80],
                     # 这一句用什么语言转写的；langConfirmed=false 表示是推测（服务器累计投票，人物页待确认）
                     "lang": str(item.get("lang") or "")[:12], "langConfirmed": bool(item.get("langConfirmed", True))})
    if not rows:
        raise ValueError("empty_transcript")
    if total > MAX_TRANSCRIPT_CHARS:
        raise ValueError("transcript_too_long")
    return {
        "windowId": str(body.get("windowId") or "")[:80] or f"w{int(time.time() * 1000)}",
        "startedAt": int(body.get("startedAt") or 0),
        "endedAt": int(body.get("endedAt") or 0),
        "speakerCount": int(body.get("speakerCount") or len({r["speaker"] for r in rows})),
        "locale": str(body.get("locale") or "")[:16],
        "source": str(body.get("source") or "ipad-mic")[:24],
        "utterances": rows,
        # 每个声音块一条：{slotKey, personId?（App 声纹比对认出的人）, vector?（声纹特征，256 维）}
        "speakers": [sp for sp in (body.get("speakers") or [])[:8] if isinstance(sp, dict)],
    }


def transcript_text(window: dict) -> str:
    return "\n".join(f"[{_clock(u['t0'])} {u['speaker']}] {u['text']}" for u in window["utterances"])


def build_state(window: dict, summary: str, clues: list[str] | None = None) -> str:
    who = "多人说话" if window["speakerCount"] >= 2 else "只有一个说话人"
    has_user = any(u["isUser"] for u in window["utterances"])
    lines = [
        "用户随身的 iPad 在日常生活中持续旁听周围的声音。以下是一段按说话人分开的自动语音转写，"
        "可能有错字漏字；说话人编号由声纹聚类得出，不保证准确。",
        "「我」是已登记声纹的用户本人；写了名字的是用户登记过声音的熟人；「说话人N」是没登记的人；"
        "「?」是没分出来是谁。方括号里是这段录音内的时间。",
        f"场景：{who}；{'用户本人有发言' if has_user else '没有识别到用户本人的发言'}。",
    ]
    if summary:
        lines.append("之前的旁听摘要（只用于理解指代，不要据此判断本段）：" + summary[:1500])
    if clues:
        lines.append("在场熟人的资料（用户写的介绍与 AI 整理的关系，只作背景线索）：\n" + "\n".join(clues[:6]))
    lines.append("本段转写：")
    lines.append(transcript_text(window))
    return "\n".join(lines)


# ─────────────────────────── 判断 → 动作 ───────────────────────────

def decide(judgments: dict) -> list[str]:
    actions = []
    danger = judgments["danger"]["choice"]
    if danger == "immediate":
        actions.append("danger_record")
    elif danger == "possible":
        actions.append("danger_watch")
    record = judgments["record"]["choice"]
    if record == "audio" or danger in ("immediate", "possible"):
        actions.append("save_audio")
    if record in ("audio", "transcript"):
        actions.append("save_transcript")
    question = judgments["question"]["choice"]
    if question == "question":
        actions.append("route_question")
    elif question == "task":
        actions.append("note_task")
    return actions


def _route_question(window: dict, summary: str) -> None:
    state = build_state(window, summary)
    try:
        routed = _predict(state, {"route": ROUTE_QUESTION})["route"]
    except Exception as exc:  # noqa: BLE001
        log_event("route_failed", windowId=window["windowId"], error=type(exc).__name__, detail=str(exc)[:200])
        feed_append("question", windowId=window["windowId"], route="unrouted", text=transcript_text(window)[-600:])
        return
    log_event("route", windowId=window["windowId"], choice=routed["choice"], probability=routed["probability"])
    if routed["choice"] != "knowledge":
        feed_append("question", windowId=window["windowId"], route=routed["choice"], text=transcript_text(window)[-600:])
        vault_append("待跟进的疑问", transcript_text(window))
        return
    prompt = ("下面是用户身边的一段自动语音转写（可能有识别错误；「我」是用户本人）。"
              "找出其中最主要的那个疑问，用中文写一段简短准确的解答（不超过 300 字）。"
              "第一行写「问：」加一句话复述疑问，之后写「答：」。不要寒暄。\n\n" + transcript_text(window))
    started = time.monotonic()
    try:
        answer = _ai(prompt)
    except Exception as exc:  # noqa: BLE001
        log_event("answer_failed", windowId=window["windowId"], error=type(exc).__name__, detail=str(exc)[:200])
        feed_append("question", windowId=window["windowId"], route="knowledge_failed", text=transcript_text(window)[-600:])
        return
    if not answer:
        log_event("answer_failed", windowId=window["windowId"], error="empty_answer")
        return
    log_event("answered", windowId=window["windowId"], ms=round((time.monotonic() - started) * 1000), chars=len(answer))
    feed_append("answer", windowId=window["windowId"], text=answer[:2000])
    vault_append("AI 解答", answer + "\n\n> 原话：\n> " + transcript_text(window).replace("\n", "\n> "))


def _maybe_summarize() -> None:
    if not _SUMMARY_LOCK.acquire(blocking=False):
        return
    try:
        _summarize_locked()
    finally:
        _SUMMARY_LOCK.release()


def _summarize_locked() -> None:
    context = load_context()
    pending = context.get("pending") or []
    if not pending:
        return
    # 从「上次摘要」或（还没有摘要时）「第一条待摘要片段」起算，而不是从 0 起算 ——
    # 否则第一段有意义的话一进来就会触发摘要。
    anchor = float(context.get("updatedAt") or context.get("pendingSince") or time.time() * 1000)
    age = time.time() - anchor / 1000
    if len(pending) < SUMMARY_EVERY_WINDOWS and age < SUMMARY_MAX_AGE_SECONDS:
        return
    prompt = ("你在维护一份「用户身边最近发生了什么」的滚动摘要，供之后与用户对话时当背景。"
              "下面是旧摘要和之后新旁听到的有意义片段（自动转写，「我」是用户本人）。"
              "写一份新的中文摘要，不超过 500 字：保留仍然相关的旧信息，加入新的约定、事项、话题和情绪线索，"
              "去掉已经过时的；只写事实，不要推测。\n\n"
              "旧摘要：\n" + (context.get("summary") or "（无）") + "\n\n新片段：\n" + "\n---\n".join(pending[-12:]))
    try:
        summary = _ai(prompt)
    except Exception as exc:  # noqa: BLE001
        log_event("summary_failed", error=type(exc).__name__, detail=str(exc)[:200], pending=len(pending))
        return
    if not summary:
        log_event("summary_failed", error="empty_summary", pending=len(pending))
        return
    consumed = len(pending)
    with _CONTEXT_LOCK:
        current = load_context()
        rest = (current.get("pending") or [])[consumed:]
        now = round(time.time() * 1000)
        save_context({"summary": summary[:3000], "updatedAt": now, "pending": rest,
                      "pendingSince": now if rest else 0})
    feed_append("summary", text=summary[:3000])
    log_event("summarized", chars=len(summary), consumed=consumed)


def _after_judgment(window: dict, judgments: dict, actions: list[str], summary: str) -> None:
    text = transcript_text(window)
    if "save_transcript" in actions or "danger_record" in actions or "danger_watch" in actions:
        label = {"danger_record": "⚠ 可能的危险（已持续录音）", "danger_watch": "需要留意"}.get(
            next((a for a in actions if a.startswith("danger")), ""), "记录")
        vault_append(label, text)
    if "note_task" in actions:
        feed_append("task", windowId=window["windowId"], text=text[-600:])
        vault_append("待办 / 约定", text)
    if judgments["meaningful"]["choice"] == "meaningful":
        with _CONTEXT_LOCK:
            context = load_context()
            if not context.get("pending"):
                context["pendingSince"] = round(time.time() * 1000)
            context.setdefault("pending", []).append(text[-1500:])
            context["pending"] = context["pending"][-40:]
            save_context(context)
    if "route_question" in actions:
        _route_question(window, summary)
    _maybe_summarize()


# ─────────────────────────── 路由 ───────────────────────────

def _uid() -> str:
    return str(session.get("user_id") or "")


@bp.before_request
def _guard():
    if not _uid():
        return jsonify({"ok": False, "code": "unauthorized", "error": "login required"}), 401
    return None


@bp.post("/judge")
def judge():
    started = time.monotonic()
    try:
        window = normalize_window(request.get_json(silent=True) or {})
    except (TypeError, ValueError) as exc:
        log_event("judge_rejected", reason=str(exc))
        return jsonify({"ok": False, "code": str(exc)}), 400
    summary = load_context().get("summary") or ""
    clues = _ingest_people(window)
    try:
        judgments = _predict(build_state(window, summary, clues), QUESTIONS)
    except JevUnavailable as exc:
        log_event("judge_failed", windowId=window["windowId"], error=str(exc))
        return jsonify({"ok": False, "code": "jev_unavailable", "error": str(exc)}), 503
    except Exception as exc:  # noqa: BLE001
        log_event("judge_failed", windowId=window["windowId"], error=type(exc).__name__, detail=str(exc)[:200])
        return jsonify({"ok": False, "code": "jev_failed", "error": type(exc).__name__}), 502
    actions = decide(judgments)
    compact = {name: {"choice": v["choice"], "probability": v["probability"]} for name, v in judgments.items()}
    ms = round((time.monotonic() - started) * 1000)
    log_event("judged", windowId=window["windowId"], source=window["source"], ms=ms, utterances=len(window["utterances"]),
              speakers=window["speakerCount"], judgments=compact, actions=actions)
    if compact["meaningful"]["choice"] != "noise" or actions:
        feed_append("window", windowId=window["windowId"], source=window["source"], judgments=compact, actions=actions,
                    speakers=window["speakerCount"], text=transcript_text(window)[-800:])
    _spawn(_after_judgment, window, judgments, actions, summary)
    return jsonify({"ok": True, "windowId": window["windowId"], "judgments": compact, "actions": actions, "ms": ms,
                    "names": window.get("names") or {}})


@bp.get("/feed")
def feed():
    try:
        since = int(request.args.get("since") or 0)
    except ValueError:
        since = 0
    return jsonify({"ok": True, "entries": feed_since(since)})


@bp.get("/context")
def context():
    data = load_context()
    return jsonify({"ok": True, "summary": data.get("summary") or "", "updatedAt": data.get("updatedAt") or 0,
                    "pending": len(data.get("pending") or [])})


# ─────────────────────────── 人物 / 时间轴（ambient_people + KJ） ───────────────────────────

_PEOPLE = None
_PEOPLE_LOCK = threading.Lock()
SUMMARIZE_AFTER_WINDOWS = 5


def people() -> AmbientPeople:
    """懒加载：人物文字资料写 KJ（与 /kj/api 共用同一个 KJService，渲染进 Obsidian KJ/）。测试替换 _PEOPLE。"""
    global _PEOPLE
    with _PEOPLE_LOCK:
        if _PEOPLE is None:
            import kj_nodes
            _PEOPLE = AmbientPeople(state_dir(), kj_nodes._svc())
        return _PEOPLE


def _ingest_people(window: dict) -> list[str]:
    """记时间轴、更新声音块；按服务器已知的块→人把标签换成名字（用户在时间轴上事后起的名 App 可能还不知道）；
    返回给 jev 的熟人线索。失败只出声，不拦判断。"""
    try:
        store = people()
        mapping = store.ingest(window, window.get("speakers") or [])
        if getattr(store, "last_superseded", 0):
            log_event("stream_rows_superseded", windowId=window.get("windowId"), dropped=store.last_superseded)
        names = {}
        for u in window["utterances"]:
            pid = mapping.get(u.get("slotKey"))
            if pid:
                name = store.person(pid)["name"]
                u["speaker"] = name
                u["isUser"] = u["isUser"] or pid == "me"
                names[u["slotKey"]] = {"personId": pid, "name": name}
        window["names"] = names
        present = {v["personId"] for v in names.values() if v["personId"] != "me"}
        if present:
            _spawn(_maybe_profile, sorted(present))
        return store.clues([u["speaker"] for u in window["utterances"]])
    except Exception as exc:  # noqa: BLE001
        log_event("people_ingest_failed", windowId=window["windowId"], error=type(exc).__name__, detail=str(exc)[:200])
        return []


def summarize_person(person_id: str) -> str:
    """让 AI 按这个人的对话历史重写「和我的关系 / 商量过什么 / 近况」，写回 KJ（取代旧的一条）。"""
    store = people()
    info = store.person(person_id)
    windows = store.history(person_id, limit=400)
    text = []
    for w in windows[:40]:
        text.append("\n".join(f"{line.get('name') or line.get('label') or '?'}：{line['text']}" for line in w["lines"]))
    corpus = "\n---\n".join(text)[-9000:]
    if not corpus:
        raise PeopleError("no_history", "还没有这个人的对话记录")
    prompt = ("下面是用户（「我」）身边的自动语音转写里，和「" + info["name"] + "」有关的对话片段（新的在前，可能有识别错误）。\n"
              "用户写的介绍：" + (info["intro"] or "（无）") + "\n旧的整理：" + (info["profile"] or "（无）") + "\n\n"
              "请用中文写一份新的整理，三段，每段以固定标题开头：\n关系：他 / 她和用户是什么关系、怎么称呼。\n"
              "商量过：一起讨论或约定过的事（带大致日期）。\n近况：最近的状态、在意的事。\n"
              "只写对话里有依据的事实，没有依据就写「未知」；保留旧整理中仍然成立的内容。总共不超过 500 字。\n\n" + corpus)
    result = _ai(prompt)
    if not result:
        raise PeopleError("empty_summary", "AI 没有返回内容")
    store.write_profile(info["id"], result, by="ai")
    log_event("person_summarized", personId=info["id"], chars=len(result), windows=len(windows))
    return result


def _maybe_profile(person_ids: list[str]) -> None:
    """有新对话的熟人：距上次整理又攒了 5 段对话就自动重写一次整理。"""
    store = people()
    for pid in person_ids:
        try:
            profile = store.profile_definition(pid)
            since = int(profile["created_at"]) if profile else 0
            since_ms = since * 1000 if since < 10_000_000_000 else since
            fresh = [w for w in store.history(pid, limit=200) if w["t0"] > since_ms]
            if len(fresh) >= SUMMARIZE_AFTER_WINDOWS:
                summarize_person(pid)
        except Exception as exc:  # noqa: BLE001
            log_event("person_summary_failed", personId=pid, error=type(exc).__name__, detail=str(exc)[:200])


def _people_reply(fn):
    try:
        return jsonify({"ok": True, **fn()})
    except PeopleError as exc:
        log_event("people_rejected", code=exc.code, error=exc.message)
        return jsonify({"ok": False, "code": exc.code, "error": exc.message}), (404 if exc.code.endswith("not_found") else 400)


@bp.get("/people")
def people_list():
    return _people_reply(lambda: {"people": people().people()})


@bp.get("/people/<person_id>")
def person_detail(person_id):
    limit = min(200, int(request.args.get("windows") or 60))
    return _people_reply(lambda: {"person": people().person(person_id),
                                  "history": people().history(person_id)[:limit]})


@bp.patch("/people/<person_id>")
def person_update(person_id):
    body = request.get_json(silent=True) or {}
    return _people_reply(lambda: {"person": people().update_person(
        person_id, name=body.get("name"), intro=body.get("intro"), profile=body.get("profile"),
        language=body.get("language"))})


@bp.delete("/people/<person_id>")
def person_delete(person_id):
    def go():
        purge = request.args.get("purge") in ("1", "true")
        result = people().delete_person(person_id, purge=purge)
        log_event("person_deleted", **result)
        return result
    return _people_reply(go)


@bp.post("/people/<person_id>/merge")
def person_merge(person_id):
    body = request.get_json(silent=True) or {}
    return _people_reply(lambda: {"person": people().merge(person_id, into=str(body.get("into") or ""))})


@bp.post("/people/<person_id>/summarize")
def person_summarize(person_id):
    return _people_reply(lambda: {"profile": summarize_person(person_id)})


@bp.post("/slots/assign")
def slot_assign():
    body = request.get_json(silent=True) or {}
    return _people_reply(lambda: {"person": people().assign_slot(
        str(body.get("slotKey") or ""), name=body.get("name"), person_id=body.get("personId"),
        vector=body.get("vector") if isinstance(body.get("vector"), list) else None)})


@bp.post("/slots/unassign")
def slot_unassign():
    body = request.get_json(silent=True) or {}
    return _people_reply(lambda: (people().unassign_slot(str(body.get("slotKey") or "")), {})[1])


@bp.post("/slots/delete")
def slot_delete():
    body = request.get_json(silent=True) or {}
    def go():
        result = people().delete_slot(str(body.get("slotKey") or ""))
        log_event("slot_deleted", **result)
        return result
    return _people_reply(go)


@bp.post("/revise")
def revise():
    """App 空闲时后台逐段重转完，修正时间轴上那一段（用该说话人的语言重转的结果替换主线识别的文字）。"""
    body = request.get_json(silent=True) or {}
    try:
        t0, t1 = int(body.get("t0") or 0), int(body.get("t1") or 0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "code": "bad_range"}), 400

    def go():
        count = people().revise(str(body.get("slotKey") or ""), t0, t1, str(body.get("text") or ""),
                                lang=str(body.get("lang") or ""), confirmed=bool(body.get("langConfirmed", True)))
        log_event("revised", slotKey=body.get("slotKey"), replaced=count, lang=body.get("lang"))
        return {"replaced": count}
    return _people_reply(go)


def refine_translation(lines: list[dict]) -> list[str]:
    """精翻（2026-09-27 用户）：整段对话连同说话人一起交给 AI，结合上下文翻成简体中文，按顺序逐行给回。
    输入每项 {speaker, text, personId?}；说话人数量不限。定了人且写过介绍 / 有 AI 整理的，附在提示里当背景。
    返回与输入等长的译文列表（没对上的行为空串，不错位）。"""
    items = [l for l in lines if isinstance(l, dict) and str(l.get("text") or "").strip()]
    rows = [(str(l.get("speaker") or "?").strip()[:40], str(l.get("text") or "").strip()[:1500]) for l in items]
    if not rows:
        raise PeopleError("empty", "没有要翻译的句子")
    if len(rows) > 200 or sum(len(t) for _, t in rows) > 16000:
        raise PeopleError("too_long", "一次最多 200 句 / 16000 字，把显示范围缩小一些再试")
    numbered = "\n".join(f"{i + 1}. {speaker}：{text}" for i, (speaker, text) in enumerate(rows))
    # 出场人物的介绍（人物页里用户写的 + AI 整理），帮 AI 理解称呼、关系、话题
    profiles = []
    store = people()
    for pid in dict.fromkeys(str(l.get("personId") or "") for l in items):
        if not pid or pid == "me":
            continue
        try:
            info = store.person(pid)
        except Exception:  # noqa: BLE001 — 人被删 / 合并了就不带介绍
            continue
        about = " ".join(x.strip() for x in (info.get("intro") or "", (info.get("profile") or "")[:400]) if x.strip())
        if about:
            profiles.append(f"- {info['name']}：{about[:600]}")
    background = ("出场人物（供理解上下文，不用翻译）：\n" + "\n".join(profiles) + "\n\n") if profiles else ""
    prompt = ("下面是按时间顺序的多人对话（自动语音转写，可能有识别错误），每行是「序号. 说话人：原文」。\n"
              "请结合上下文把每一行翻译成自然的简体中文；识别错误按上下文合理还原。\n"
              f"严格输出 {len(rows)} 行，与输入一一对应、顺序相同，每行格式「序号. 译文」，译文里不要带说话人；"
              "原文已经是中文的行照抄；不要输出任何别的内容。\n\n" + background + numbered)
    answer = _ai(prompt) or ""
    out = [""] * len(rows)
    for raw in answer.splitlines():
        m = re.match(r"\s*(\d+)\s*[.、)）:：]\s*(.*)$", raw)
        if not m:
            continue
        index = int(m.group(1)) - 1
        if 0 <= index < len(out) and not out[index]:
            text = m.group(2).strip()
            speaker = rows[index][0]
            if speaker and text.startswith(speaker + "："):
                text = text[len(speaker) + 1:].strip()
            out[index] = text
    got = sum(1 for t in out if t)
    log_event("refined", lines=len(rows), translated=got)
    if not got:
        raise PeopleError("empty_translation", "AI 没有按格式返回译文")
    return out


@bp.post("/translate")
def translate():
    body = request.get_json(silent=True) or {}
    return _people_reply(lambda: {"translations": refine_translation(body.get("lines") or [])})


@bp.get("/timeline")
def timeline():
    now = int(time.time() * 1000)
    try:
        t_to = int(request.args.get("to") or now)
        t_from = int(request.args.get("from") or t_to - 3 * 3600 * 1000)
    except ValueError:
        return jsonify({"ok": False, "code": "bad_range"}), 400
    if t_to - t_from > 7 * 86_400_000:
        return jsonify({"ok": False, "code": "range_too_long", "error": "一次最多 7 天"}), 400
    return _people_reply(lambda: people().timeline(t_from, t_to))


@bp.get("/voiceprints")
def voiceprints():
    return _people_reply(lambda: {"people": people().voiceprints()})


def register_ambient(app) -> None:
    app.register_blueprint(bp)
