"""PDF 阅读器侧边栏 Copilot —— 沙盒 agent + 工具循环。

理念(用户定的方向):不再把能力压成固定语音命令,而是一个**带工具的对话智能体**。
- 大脑 = 预热/常驻 claude(sonnet),我们**自管工具循环**(agent 输出 tool-call JSON → 服务端执行
  → 喂回结果 → 循环到 final answer),**不上 MCP/原生 tool-use**(订阅版 CLI 装不上+每调多 ~10s)。
- 工具 = 把现有稳定能力拆成颗粒(read_page/search/translate/make_anki/make_note/add_vocab/goto_page…),
  agent 自由组合解复合请求(「总结这页再做成卡」= 它自己 读页→答→制卡)。
- 沙盒 = 每页一张工具清单,agent 只能用清单里的。
- 多轮:前端持对话历史(≤6 轮)每次带上;一个用户消息内的工具循环在同一 claude 进程多轮跑。
- 输出:SSE 流式(tool 进度 + final answer + client_actions)。语音退化为输入框系统听写,不在此处理。
"""
from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Blueprint, Response, jsonify, request, session

bp = Blueprint("assistant", __name__, url_prefix="/api/assistant")

CLAUDE_DIR = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
VAULT_ROOT = Path(os.environ.get("OBSIDIAN_VAULT", "/home/bwicarus/obsidian"))
_APP_CLAUDE = os.environ.get("APP_CLAUDE") or "claude"
_AGENT_MODEL = "sonnet"   # 推理 + 工具决策:sonnet 平衡(快+好用);重内容生成的工具内部各用 opus


def _logged_in() -> bool:
    return bool(session.get("user_id"))


def _pdf():
    import pdf_reader
    return pdf_reader


# ──────────────────────── claude 进程(stream-json 多轮)────────────────────────
def _spawn():
    try:
        return subprocess.Popen(
            [_APP_CLAUDE, "--print", "--input-format", "stream-json", "--output-format", "stream-json",
             "--verbose", "--model", _AGENT_MODEL, "--effort", "high"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, cwd=str(CLAUDE_DIR))
    except Exception:
        return None


def _kill(p):
    if not p:
        return
    try:
        if p.stdin:
            p.stdin.close()
    except Exception:
        pass
    try:
        p.terminate()
    except Exception:
        pass


def _send(p, content: str, timeout: float = 90.0):
    """发一轮 user 消息,读到 result 事件返回文本;失败回 None。"""
    try:
        p.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n")
        p.stdin.flush()
    except Exception:
        return None
    t0 = time.time()
    while time.time() - t0 < timeout:
        r, _, _ = select.select([p.stdout], [], [], 0.5)
        if r:
            ln = p.stdout.readline()
            if not ln:
                break
            if '"type":"result"' in ln:
                try:
                    return (json.loads(ln).get("result") or "").strip()
                except Exception:
                    return None
        if p.poll() is not None:
            break
    return None


# 预热一个待命进程(侧边栏打开时 /prewarm 调),省冷启动
_warm_lock = threading.Lock()
_warm_p = None
_warm_on = False


def _warm_prewarm():
    global _warm_p, _warm_on
    with _warm_lock:
        _warm_on = True
        if _warm_p is not None and _warm_p.poll() is None:
            return
        _warm_p = _spawn()


def _warm_reap():
    global _warm_p, _warm_on
    with _warm_lock:
        _warm_on = False
        p, _warm_p = _warm_p, None
    _kill(p)


def _take_proc():
    """取预热进程(没有就现起);用完调用方 _kill + _warm_respawn。"""
    global _warm_p
    with _warm_lock:
        p, _warm_p = _warm_p, None
    if p is None or p.poll() is not None:
        _kill(p)
        p = _spawn()
    return p


def _warm_respawn():
    global _warm_p
    with _warm_lock:
        if not _warm_on:
            return
        if _warm_p is not None and _warm_p.poll() is None:
            return
        _warm_p = _spawn()


# ──────────────────────── 本页正文(fitz)────────────────────────
def _page_text(file_rel: str, page) -> str:
    try:
        rel = (file_rel or "").strip()
        if not rel or ".." in rel:
            return ""
        ap = (VAULT_ROOT / rel).resolve()
        ap.relative_to(VAULT_ROOT.resolve())
        if not ap.exists():
            return ""
        import fitz
        doc = fitz.open(str(ap))
        try:
            idx = max(0, min(int(page or 1) - 1, doc.page_count - 1))
            return (doc[idx].get_text("text") or "").strip()[:4000]
        finally:
            doc.close()
    except Exception:
        return ""


def _deep_link(base, file_rel, page):
    from urllib.parse import quote
    return f"{(base or '').rstrip('/')}/pdf/view?file={quote(file_rel or '', safe='')}&page={page or 1}"


# ──────────────────────── 工具(沙盒:PDF 页可用)────────────────────────
def _t_read_page(args, ctx):
    pg = args.get("page") or ctx.get("page", 0)
    txt = _page_text(ctx.get("file_rel", ""), pg)
    return {"page": pg, "text": txt} if txt else {"error": "这页没取到文字(可能是纯图/未OCR)"}


def _t_read_selection(args, ctx):
    sel = (ctx.get("selection") or "").strip()
    return {"selection": sel[:4000]} if sel else {"error": "用户当前没有选中文字"}


def _t_search_book(args, ctx):
    q = (args.get("query") or "").strip()
    file_rel = ctx.get("file_rel", "")
    if not q or not file_rel:
        return {"error": "缺 query 或没开书"}
    try:
        ap = (VAULT_ROOT / file_rel).resolve()
        idx = _pdf()._book_text_index(str(ap), file_rel)
        ql = q.lower()
        hits = []
        for ps, txt in idx.items():
            low = (txt or "").lower()
            pos = low.find(ql)
            if pos >= 0:
                hits.append({"page": int(ps),
                             "snippet": (txt[max(0, pos - 15):pos + len(q) + 25] or "").replace("\n", " ").strip()})
        hits.sort(key=lambda x: x["page"])
        return {"total": len(hits), "hits": hits[:10]}
    except Exception as e:
        return {"error": str(e)[:120]}


def _t_translate(args, ctx):
    text = (args.get("text") or "").strip()
    if not text:
        text = (ctx.get("selection") or "").strip() or _page_text(ctx.get("file_rel", ""), ctx.get("page", 0))
    if not text:
        return {"error": "没有要翻译的文字"}
    try:
        sys.path.insert(0, str(CLAUDE_DIR / "scripts"))
        from translate import translate as _tr  # type: ignore
        return {"translation": _tr(text[:3000], args.get("target") or "zh")}
    except Exception as e:
        return {"error": str(e)[:120]}


def _t_goto_page(args, ctx):
    try:
        n = int(args.get("page"))
    except (TypeError, ValueError):
        return {"error": "page 不是数字"}
    return {"ok": True, "note": f"已翻到第{n}页", "client_action": {"fn": "goToPage", "args": [n]}}


def _bg_task(kind, params, ctx):
    """重内容生成(制卡/笔记/生词)→ 复用 voice 后台任务框架(opus,完成发系统通知)。返回提示。"""
    try:
        import voice
        tid = voice._vtask_new(kind)
        base = ctx.get("_base", "")
        tctx = {k: ctx.get(k) for k in ("file_rel", "page", "book_name", "selection")}
        threading.Thread(target=voice._run_task, args=(tid, kind, params, tctx, base), daemon=True).start()
        return {"ok": True, "task_id": tid, "note": "已在后台开始,完成会弹系统通知。"}
    except Exception as e:
        return {"error": str(e)[:120]}


def _t_make_anki(args, ctx):
    text = (args.get("text") or "").strip() or (ctx.get("selection") or "").strip()
    if not text:
        return {"error": "缺要做卡的内容(给 text 或先选中)"}
    return _bg_task("anki", {"text": text}, ctx)


def _t_make_note(args, ctx):
    text = (args.get("text") or "").strip() or (ctx.get("selection") or "").strip() \
        or _page_text(ctx.get("file_rel", ""), ctx.get("page", 0))
    if not text:
        return {"error": "没有要整理的内容"}
    return _bg_task("note", {"text": text}, ctx)


def _t_add_vocab(args, ctx):
    word = (args.get("word") or "").strip() or (ctx.get("selection") or "").strip()
    if not word:
        return {"error": "缺单词"}
    return _bg_task("vocab", {"word": word}, ctx)


TOOLS = {
    "read_page": ("读当前页(或指定页)正文。args {page?}", _t_read_page),
    "read_selection": ("读用户当前选中的文字。args {}", _t_read_selection),
    "search_book": ("在当前这本书全文搜关键词,返回命中页+片段。args {query}", _t_search_book),
    "translate": ("翻译文字成中文(或 target 语言)。不传 text 则译选中/本页。args {text?, target?}", _t_translate),
    "goto_page": ("翻到指定页(前端跳转)。args {page}", _t_goto_page),
    "make_anki": ("把内容做成 Anki 卡(后台,带原文链接,完成通知)。args {text?}(不传用选中)", _t_make_anki),
    "make_note": ("把内容整理成 Obsidian 笔记(后台)。args {text?}(不传用选中/本页)", _t_make_note),
    "add_vocab": ("把英文单词加生词本并制卡(后台)。args {word?}(不传用选中)", _t_add_vocab),
}


def _tool_label(name, args):
    return {"read_page": "读取页面", "read_selection": "读取选中", "search_book": "搜索全书",
            "translate": "翻译", "goto_page": "翻页", "make_anki": "制卡", "make_note": "整理笔记",
            "add_vocab": "加生词本"}.get(name, name)


# ──────────────────────── agent 循环 ────────────────────────
def _sys_prompt(ctx):
    cat = "\n".join(f"- {n}: {d}" for n, (d, _) in TOOLS.items())
    meta = {k: ctx.get(k) for k in ("book_name", "page", "total") if ctx.get(k)}
    sel = (ctx.get("selection") or "").strip()
    sel_line = f"\n用户当前选中:「{sel[:200]}」" if sel else ""
    return (
        "你是网页 PDF 阅读器的侧边栏助手,像 Copilot 一样陪用户读书。用简洁中文口语聊天。\n"
        "你能调用下面的工具来读页面内容、搜索、翻译、制卡、整理笔记、跳页等,可以连续调用多个工具来完成复合请求"
        "(例如『总结这页再做成卡』= 先 read_page,再据此回答,再 make_anki)。\n"
        "调用工具时:**整条消息只输出一行 JSON**,格式 {\"tool\":\"工具名\",\"args\":{...}},别加任何别的字。\n"
        "我执行后会把【工具结果】返回给你,你再决定继续调工具还是回答。\n"
        "能回答用户时:直接输出给用户看的中文回答(纯文本,不要 JSON、不要工具)。回答简洁自然,别太长。\n"
        "需要页面内容/选中文字时务必先用 read_page / read_selection 拿,别凭空编。\n"
        "★复合请求(含多个动作,如『总结再做成卡』『翻译并制卡』『找到X页并跳过去』)必须把每个动作都执行完——"
        "逐个调工具,做完一个再做下一个,全部完成后才给最终回答,**别只做第一步就停**。\n"
        "★用户说『跳过去/打开/翻到』且目标明确(或搜索只有一个最相关命中)时,直接调 goto_page,别反问;"
        "只有真有多个差不多的选项才反问。\n"
        "例:用户说「总结这页并做成卡」。正确顺序:\n"
        "  第1步 → {\"tool\":\"read_page\",\"args\":{}}\n"
        "  第2步(拿到正文后)→ {\"tool\":\"make_anki\",\"args\":{\"text\":\"<你总结出的要点>\"}}\n"
        "  第3步(制卡已提交后)→ 才给最终回答:「总结好了:…;卡也在做了,完成会通知你」。\n"
        "  ——第2步绝不能省,用户要的是『卡』不是只看总结。同理『翻译并制卡』要 translate 后再 make_anki。\n\n"
        f"【可用工具】\n{cat}\n\n"
        f"【当前页面】{json.dumps(meta, ensure_ascii=False)}{sel_line}"
    )


def _format_history(history):
    out = []
    for h in (history or [])[-6:]:
        role = "用户" if h.get("role") == "user" else "助手"
        c = (h.get("content") or "").strip()
        if c:
            out.append(f"{role}:{c[:600]}")
    return ("【最近对话】\n" + "\n".join(out) + "\n") if out else ""


def _parse_tool(raw):
    """整条消息是 {"tool":...} JSON → 工具调用;否则 None(当作给用户的回答)。容忍 ```json 围栏。"""
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    if not s.startswith("{"):
        return None
    try:
        d = json.loads(s)
        return d if isinstance(d, dict) and "tool" in d else None
    except Exception:
        return None


def _agent_run(message, ctx, history):
    """生成 SSE 事件 dict:{event, data}。event ∈ tool|tool-done|answer|actions|error。"""
    p = _take_proc()
    if not p:
        yield {"event": "error", "data": "助手起不来(claude 起不来)"}
        return
    client_actions = []
    try:
        content = f"{_sys_prompt(ctx)}\n\n{_format_history(history)}【用户】{message}\n\n现在开始(调工具就只输出 JSON,能答就直接答):"
        for step in range(6):
            raw = _send(p, content)
            if not raw:
                yield {"event": "error", "data": "助手没响应(超时)"}
                return
            tool = _parse_tool(raw)
            if tool and tool.get("tool") in TOOLS:
                name = tool["tool"]
                targs = tool.get("args") if isinstance(tool.get("args"), dict) else {}
                yield {"event": "tool", "data": _tool_label(name, targs)}
                try:
                    res = TOOLS[name][1](targs, ctx) or {}
                except Exception as e:
                    res = {"error": str(e)[:160]}
                if isinstance(res, dict) and res.get("client_action"):
                    client_actions.append(res.pop("client_action"))
                yield {"event": "tool-done", "data": _tool_label(name, targs)}
                content = "【工具结果】" + json.dumps(res, ensure_ascii=False)[:3500] + "\n\n继续(调工具只输出 JSON,能答就直接答):"
                continue
            # 不是工具调用 = 给用户的最终回答
            yield {"event": "answer", "data": raw}
            break
        else:
            yield {"event": "answer", "data": "(想了太多步,先到这吧)"}
        if client_actions:
            yield {"event": "actions", "data": client_actions}
    finally:
        _kill(p)
        threading.Thread(target=_warm_respawn, daemon=True).start()


# ──────────────────────── 路由 ────────────────────────
@bp.route("/chat", methods=["POST"])
def assistant_chat():
    if not _logged_in():
        return jsonify({"ok": False, "error": "auth"}), 401
    body = request.get_json(silent=True) or {}
    message = (body.get("message") or "").strip()
    if not message:
        return jsonify({"ok": False, "error": "empty"}), 400
    ctx = body.get("context") or {}
    ctx["_base"] = request.host_url.rstrip("/")
    history = body.get("history") or []

    def gen():
        try:
            for ev in _agent_run(message, ctx, history):
                yield f"event: {ev['event']}\ndata: {json.dumps(ev['data'], ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"event: error\ndata: {json.dumps(str(e)[:160], ensure_ascii=False)}\n\n"
        yield "event: done\ndata: {}\n\n"

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@bp.route("/prewarm", methods=["POST"])
def assistant_prewarm():
    if not _logged_in():
        return jsonify({"ok": False}), 401
    off = bool((request.get_json(silent=True) or {}).get("off"))
    threading.Thread(target=(_warm_reap if off else _warm_prewarm), daemon=True).start()
    return jsonify({"ok": True})


def register_assistant(app):
    app.register_blueprint(bp)
