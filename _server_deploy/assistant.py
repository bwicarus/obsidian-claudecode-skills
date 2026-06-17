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


# ── 对话服务端持久化(跨设备,可手动清零)。state/assistant-convo/<user_id>.json ──
_CONVO_DIR = CLAUDE_DIR / "state" / "assistant-convo"
_convo_lock = threading.Lock()


def _convo_path(uid):
    return _CONVO_DIR / f"{uid}.json"


def _convo_load(uid):
    p = _convo_path(uid)
    try:
        return json.loads(p.read_text("utf-8"))
    except FileNotFoundError:
        return []
    except Exception:
        # 文件存在但内容坏了(原子替换已防"读半截",这里防"内容本身坏")→ 备份成 .corrupt 再返回 [],
        # 别让随后的 append 直接用空数组覆盖 → 把可恢复的损坏变成不可恢复的历史丢失
        try:
            if p.exists() and p.stat().st_size > 2:
                p.rename(p.with_name(p.name + f".corrupt.{int(time.time())}"))
        except Exception:
            pass
        return []


def _convo_append(uid, role, content, meta=None):
    with _convo_lock:
        msgs = _convo_load(uid)
        rec = {"role": role, "content": content, "ts": int(time.time())}
        if meta:   # 记每轮所在位置(书/页/选中句/用过的图),让助手回看历史时知道"刚才那页"具体是第几页,前端也据此在历史里渲染上下文卡片
            for k in ("page", "pages", "book", "file_rel", "selection", "figures"):
                v = meta.get(k)
                if v:
                    rec[k] = v
        msgs.append(rec)
        try:
            _CONVO_DIR.mkdir(parents=True, exist_ok=True)
            p = _convo_path(uid)
            tmp = p.with_name(p.name + ".tmp")   # 原子替换:锁外读者(/chat 构造 history)永远看到完整旧/新文件,不会读到半截
            tmp.write_text(json.dumps(msgs[-200:], ensure_ascii=False), "utf-8")
            os.replace(tmp, p)
        except Exception:
            pass


def _convo_clear(uid):
    with _convo_lock:
        try:
            p = _convo_path(uid)
            if p.exists():
                p.unlink()
        except Exception:
            pass


# ──────────────────────── claude 进程(stream-json 多轮)────────────────────────
def _spawn():
    try:
        return subprocess.Popen(
            [_APP_CLAUDE, "--print", "--input-format", "stream-json", "--output-format", "stream-json",
             "--include-partial-messages",   # 吐 text_delta → 最终回答可逐字流式
             # 沙盒:禁掉所有内建工具(本 agent 只走我们的 JSON 工具协议,从不调内建工具)→ 防 prompt injection
             # 诱导模型用 Bash/Read 读 .env(API key)/改脚本/读别人的 convo。user message 是不可信输入。
             "--disallowedTools", "Bash Edit Write Read NotebookEdit WebFetch WebSearch Glob Grep Task",
             "--verbose", "--model", _AGENT_MODEL, "--effort", "low"],   # low:聊天/工具路由不需要深思考 → 最影响首字延迟,用 low 最快(复杂任务靠多步工具补)
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


def _send_stream(p, content, timeout: float = 90.0):
    """发一轮 user 消息,**流式** yield:('delta', 累计文本) 每来一段 text_delta;最后 ('result', 完整文本/None)。
    供 _agent_run 把最终回答逐字吐给前端(tool 调用 JSON 不流式,见 _agent_run 的 startswith('{') 判别)。"""
    try:
        p.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n")
        p.stdin.flush()
    except Exception:
        yield ("result", None); return
    acc = ""
    t0 = time.time()
    while time.time() - t0 < timeout:
        r, _, _ = select.select([p.stdout], [], [], 0.5)
        if r:
            ln = p.stdout.readline()
            if not ln:
                break
            if '"text_delta"' in ln:
                try:
                    o = json.loads(ln)
                    ev = o.get("event") or o
                    t = ((ev.get("delta") or {}).get("text")) or ""
                    if t:
                        acc += t
                        yield ("delta", acc)
                except Exception:
                    pass
            elif '"result"' in ln:   # 可能是 result 事件 → 解析确认 type==result(避免回答文本里含 "type":"result" 子串被误判→提前收口截断)
                o = None
                try:
                    o = json.loads(ln)
                except Exception:
                    pass
                if isinstance(o, dict) and o.get("type") == "result":
                    full = (o.get("result") or "").strip()
                    yield ("result", full or acc.strip() or None); return
        if p.poll() is not None:
            break
    yield ("result", acc.strip() or None)


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


# ──────────────────────── 额度护栏(只告警,不降级/不阻断)────────────────────────
# 后台周期查实时额度(scripts/lib/claude_quota,零 token + 自带缓存),接近上限就给前端一句提醒。
# 用户明确「不用 Gemini 降级」→ 这里**只**提醒、绝不切后端,助手照常用 Claude。
_quota_note = {"text": None, "at": 0.0}
_quota_started = [False]
_quota_lock = threading.Lock()


def _quota_loop():
    lib = str(CLAUDE_DIR / "scripts" / "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    while True:
        try:
            import claude_quota as cq
            cq.fetch_quota(timeout=8, cache_ttl=1)
            h5, s7 = cq.util_5h(), cq.util_7d_sonnet()
            if h5 >= 95 or s7 >= 97:
                _quota_note["text"] = f"⚠️ Claude 额度紧张(5h {h5:.0f}% · 7d-sonnet {s7:.0f}%),回答可能变慢或中断。"
            elif h5 >= 85 or s7 >= 90:
                _quota_note["text"] = f"额度提醒:接近上限(5h {h5:.0f}% · 7d-sonnet {s7:.0f}%)。"
            else:
                _quota_note["text"] = None
            _quota_note["at"] = time.time()
        except Exception:
            pass
        time.sleep(150)


def _quota_ensure_loop():
    with _quota_lock:
        if _quota_started[0]:
            return
        _quota_started[0] = True
    threading.Thread(target=_quota_loop, daemon=True).start()


def _quota_warning():
    """近上限时回一句告警字符串(给前端 notice 事件);正常/查询失败/快照过期都回 None。非阻塞。"""
    _quota_ensure_loop()
    txt = _quota_note["text"]
    return txt if (txt and time.time() - _quota_note["at"] < 1800) else None


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
    # 双页模式下读全部可见页(ctx.pages),不传 page 时默认所有可见页
    if args.get("page"):
        pages = [args["page"]]
    else:
        pages = ctx.get("pages") or [ctx.get("page", 0)]
    parts = []
    for pg in pages:
        t = _page_text(ctx.get("file_rel", ""), pg)
        if t:
            parts.append(f"【第{pg}页】\n{t[:2800]}")
    return {"pages": pages, "text": "\n\n".join(parts)} if parts else {"error": "这些页没取到文字(可能纯图/未OCR)"}


def _t_read_selection(args, ctx):
    sel = (ctx.get("selection") or "").strip()
    return {"selection": sel[:4000]} if sel else {"error": "用户当前没有选中文字"}


def _t_search_book(args, ctx):
    q = (args.get("query") or "").strip()
    file_rel = ctx.get("file_rel", "")
    if not q or not file_rel or ".." in file_rel:
        return {"error": "缺 query 或没开书"}
    try:
        ap = (VAULT_ROOT / file_rel).resolve()
        ap.relative_to(VAULT_ROOT.resolve())   # 容器校验:file_rel 来自前端不可信,挡 .. / 绝对路径越出 vault
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
    return {"ok": True, "note": f"已翻到第{n}页", "client_action": {"fn": "jumpWithBack", "args": [n]}}


def _bg_task(kind, params, ctx):
    """重内容生成(制卡/笔记/生词)→ 复用 voice 后台任务框架(opus,完成发系统通知)。返回提示。"""
    try:
        import voice
        tid = voice._vtask_new(kind)
        base = ctx.get("_base", "")
        tctx = {k: ctx.get(k) for k in ("file_rel", "page", "book_name", "selection", "_uid")}
        threading.Thread(target=voice._run_task, args=(tid, kind, params, tctx, base), daemon=True).start()
        return {"ok": True, "task_id": tid, "note": "已在后台开始,完成会弹系统通知。"}
    except Exception as e:
        return {"error": str(e)[:120]}


def _t_make_anki(args, ctx):
    text = (args.get("text") or "").strip() or (ctx.get("selection") or "").strip()
    if not text:
        return {"error": "缺要做卡的内容(给 text 或先选中)"}
    res = _bg_task("anki", {"text": text}, ctx)
    _mark_source_highlight(ctx, "#b9f6ca")   # 双向回链:原文留绿色高亮"这段做过卡"
    return res


def _t_make_note(args, ctx):
    text = (args.get("text") or "").strip() or (ctx.get("selection") or "").strip() \
        or _page_text(ctx.get("file_rel", ""), ctx.get("page", 0))
    if not text:
        return {"error": "没有要整理的内容"}
    res = _bg_task("note", {"text": text}, ctx)
    _mark_source_highlight(ctx, "#a7d8ff")   # 双向回链:原文留蓝色高亮"这段整理进笔记了"
    return res


def _t_add_vocab(args, ctx):
    word = (args.get("word") or "").strip() or (ctx.get("selection") or "").strip()
    if not word:
        return {"error": "缺单词"}
    return _bg_task("vocab", {"word": word}, ctx)


def _t_lookup_word(args, ctx):
    """查词:走现成确定性词典 + unidic 权威读音(读音/释义以此为准,LLM 别自己编)。
    英文→ECDICT(音标+中文释义+原形);日语→unidic 离线读音+声调(权威,毫秒级)。args {word?}(不传用选中)。"""
    w = (args.get("word") or "").strip() or (ctx.get("selection") or "").strip()
    if not w:
        return {"error": "没给要查的词"}
    w = w[:60]
    import sys as _sys
    vp = str(CLAUDE_DIR / "scripts" / "vocab")
    if vp not in _sys.path:
        _sys.path.insert(0, vp)
    out = {"word": w}
    is_ja = any(("぀" <= c <= "ヿ") or ("一" <= c <= "鿿") for c in w) and not w.isascii()
    try:
        import dict_sources as ds
        if is_ja:
            ra = ds._jp_reading_accent(w) or {}
            if ra.get("reading"):
                out["reading"] = ra.get("reading")          # 平假名,unidic 权威读音
                out["accent"] = ra.get("accent")            # 声调核(0=平板)
                out["mora"] = ra.get("mora")
            try:
                base = (_pdf()._jp_inflection(w) or {}).get("base")
                if base and base != w:
                    out["lemma"] = base
            except Exception:
                pass
            out["source"] = "unidic(离线权威读音)"
            out["_note"] = "reading 是 unidic 权威平假名读音,**以它为准**,别自己编;含义请结合上下文用中文讲。"
        else:
            ec = ds.lookup_ecdict(w) or {}
            if ec:
                out["phonetic"] = ec.get("phonetic")
                out["lemma"] = ec.get("lemma")
                out["meaning_zh"] = ec.get("translation")   # ECDICT 中文释义
                out["definition_en"] = ec.get("definition")
                out["pos"] = ec.get("pos")
                out["freq_rank"] = ec.get("frq")
                out["source"] = "ECDICT(离线)"
                out["_note"] = "音标/释义以 ECDICT 为准;结合上下文挑最贴切的义项讲。"
            else:
                out["note"] = "ECDICT 未收录,请结合上下文给出读音/释义"
    except Exception as e:
        return {"error": str(e)[:140]}
    return out


def _t_see_page(args, ctx):
    """把当前页(或指定页)渲染成图片让 agent **真正看到**(图表/示意图/公式排版/手写等文字层拿不到的)。
    返回 `_vision`(image block 列表),_agent_run 会把它作为图片喂回大脑(sonnet 多模态)。"""
    file_rel = ctx.get("file_rel") or ""
    if not file_rel:
        return {"error": "当前不在 PDF 书里,没法看页面"}
    if args.get("page"):
        pages = [int(args["page"])]
    else:
        pages = [int(p) for p in (ctx.get("pages") or ([ctx.get("page")] if ctx.get("page") else [])) if p]
    pages = pages[:2]   # 双页最多 2 张
    if not pages:
        return {"error": "不知道看哪页"}
    try:
        import base64
        import fitz
        ap = (VAULT_ROOT / file_rel).resolve()
        ap.relative_to(VAULT_ROOT.resolve())
        doc = fitz.open(str(ap))
        vis, done = [], []
        try:
            for pg in pages:
                if pg < 1 or pg > doc.page_count:
                    continue
                page = doc[pg - 1]
                longside = max(page.rect.width, page.rect.height) or 1.0
                scale = min(2.0, 1540.0 / longside) or 1.0   # 长边 ~1540px(Claude 视觉甜区),封顶 2x
                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                png = pix.tobytes("png")
                if len(png) > 3_000_000:   # 超大页(扫描大开本)降一档再渲 → 防喂回 stdin 过大 / Pi 8GB OOM
                    pix = page.get_pixmap(matrix=fitz.Matrix(scale * 0.6, scale * 0.6), alpha=False)
                    png = pix.tobytes("png")
                vis.append({"media_type": "image/png", "b64": base64.b64encode(png).decode()})
                done.append(pg)
        finally:
            doc.close()
        if not vis:
            return {"error": "页码超出范围"}
        return {"_vision": vis, "rendered_pages": done,
                "note": "下面是这些页的渲染图。看图回答用户(图表/示意图/公式排版/手写等文字层读不到的内容)。"}
    except Exception as e:
        return {"error": str(e)[:140]}


def _t_undo_last(args, ctx):
    try:
        import voice
        return voice._undo_do(None, owner=ctx.get("_uid"))   # 撤销自己最近一次没撤过的写操作(隔离)
    except Exception as e:
        return {"error": str(e)[:120]}


def _t_page_vocab(args, ctx):
    """查掌握度数据库(权威,别靠猜):不传 words → 当前页『还没掌握』的生词(=页面下划线词);
    传 words → 逐词查掌握度(英+日,日语自动按原形)。"""
    pdf = _pdf()
    words = args.get("words")
    if isinstance(words, list) and words:
        return {"lookups": pdf.vocab_mastery_for(words),
                "note": "mastered=true=已掌握;tracked=false=生词库里没有(=从没查过)。以此为准回答,别自己猜。"}
    file_rel = ctx.get("file_rel") or ""
    if not file_rel:
        return {"error": "当前不在 PDF 书里,无法查页面生词"}
    pages = ctx.get("pages") or ([ctx.get("page")] if ctx.get("page") else [])
    seen = {}
    for pg in pages:
        try:
            pg = int(pg)
        except Exception:
            continue
        for m in pdf.page_unmastered_vocab(file_rel, pg):
            lem = m.get("lemma") or m.get("word")
            if lem and lem not in seen:
                seen[lem] = {"word": m.get("word"), "lemma": m.get("lemma"),
                             "mastery": m.get("mastery"), "level": m.get("level"), "page": pg}
    items = list(seen.values())
    return {"unmastered_on_page": items, "count": len(items),
            "note": "这是当前页你**还没掌握**的生词(=页面下划线词,来自掌握度数据库)。"
                    "不在此列表的词:要么已掌握、要么从没查过(系统不视为生词)。"
                    "回答『我没掌握哪些词/这页生词』就用这个列表,别拿正文里的词自己猜掌握与否。"}


def _t_highlight(args, ctx):
    """把原文句子在 PDF 上画高亮:PyMuPDF search_for 文字→rects(同 char 层坐标系)→写高亮 sidecar。可撤销。"""
    file_rel = ctx.get("file_rel", "")
    if not file_rel:
        return {"error": "没开书"}
    texts = args.get("texts")
    if not texts:
        t = args.get("text")
        texts = [t] if t else []
    texts = [x.strip() for x in texts if isinstance(x, str) and x.strip()][:15]
    if not texts:
        return {"error": "没给要高亮的原文句子"}
    color = args.get("color") or "#fff59d"
    pages = [int(p) for p in (ctx.get("pages") or ([ctx.get("page")] if ctx.get("page") else [])) if p]
    try:
        import fitz
        ap = (VAULT_ROOT / file_rel).resolve()
        ap.relative_to(VAULT_ROOT.resolve())
        doc = fitz.open(str(ap))
        pdf = _pdf()
        db = pdf._hl_load(file_rel)
        ids, miss = [], []
        for t in texts:
            placed = False
            for pg in (pages or [1]):
                if pg < 1 or pg > doc.page_count:
                    continue
                p = doc[pg - 1]
                rects = p.search_for(t[:180])   # search_for 上限,长句截断匹配
                if not rects:
                    continue
                hid = "h_" + os.urandom(6).hex()
                db["highlights"].append({
                    "id": hid, "page": pg,
                    "rects": [[round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2)] for r in rects],
                    "color": color, "text": t[:2000], "note": "", "kind": "note",
                    "sentence": "", "body": "", "page_w": p.rect.width, "page_h": p.rect.height,
                    "time": int(time.time()),
                })
                ids.append(hid)
                placed = True
                break
            if not placed:
                miss.append(t[:18])
        doc.close()
        if ids:
            pdf._hl_save(file_rel, db)
        res = {"highlighted": len(ids), "missed": miss, "client_action": {"fn": "_reloadHighlights", "args": []}}
        if ids:
            import voice
            res["undo_id"] = voice._undo_record("highlight", f"{len(ids)} 处高亮", {"file_rel": file_rel, "ids": ids}, owner=ctx.get("_uid"))
        return res
    except Exception as e:
        return {"error": str(e)[:120]}


def _t_see_figure(args, ctx):
    """看用户**带入的图**(裁出图框区域的渲染图;有手写笔迹则看叠加合成图)。多张时 args{index}指定第几张(从1起),不传=全部(≤3)。
    已给的图文字说明不够、要核对图像里的具体细节时用。返回 _vision 喂回大脑。"""
    figs = ctx.get("figures") or ([ctx["figure"]] if ctx.get("figure") else [])
    figs = [f for f in figs if (f.get("box") or f.get("fbox"))]
    if not figs:
        return {"error": "当前没有带入的图(让用户先点/拖一张图进来)"}
    file_rel = ctx.get("file_rel") or ""
    if not file_rel:
        return {"error": "不在 PDF 书里"}
    idx = args.get("index")
    if idx:
        try:
            figs = [figs[int(idx) - 1]]
        except Exception:
            pass
    figs = figs[:3]
    try:
        import base64
        import pdf_reader as pdf
        ap = (VAULT_ROOT / file_rel).resolve(); ap.relative_to(VAULT_ROOT.resolve())
        vis = []; ink_any = False
        for fg in figs:
            box = fg.get("box") or fg.get("fbox"); page = fg.get("page") or ctx.get("page")
            if not box or not page:
                continue
            ink_strokes = fg.get("ink")    # 客户端随图带来的当前笔迹(优先,不依赖服务端保存时机)
            has_ink = bool(ink_strokes) or bool(fg.get("has_ink"))
            png = pdf._figure_crop_png(ap, int(page), box, with_ink=has_ink, rel=file_rel, strokes=ink_strokes)
            vis.append({"media_type": "image/png", "b64": base64.b64encode(png).decode()})
            if has_ink:
                ink_any = True
        if not vis:
            return {"error": "图框无效"}
        note = "下面是用户带入的图的裁剪渲染图,看图回答。"
        if ink_any:
            note += "（含用户手写笔迹的合成图,结合圈点/标注理解他想问什么）"
        return {"_vision": vis, "note": note}
    except Exception as e:
        return {"error": str(e)[:140]}


def _t_search_all_books(args, ctx):
    """跨『我所有的书』全文搜索(SQLite FTS5 全局索引)。用户问『我哪本书讲过 X / 别的书有没有 X / 之前在哪见过』时用。"""
    import sqlite3
    q = (args.get("query") or "").strip()
    if not q:
        return {"error": "缺 query"}
    db = _pdf()._SEARCH_DB
    if not db.exists():
        return {"error": "全局搜索索引未建(scripts/build_search_index.py)"}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            names = dict(con.execute("SELECT file, name FROM meta").fetchall())
            if len(q) >= 3:
                fts = '"' + q.replace('"', '""') + '"'
                rows = con.execute("SELECT d.file, d.page, d.body FROM pages_fts JOIN pages_data d ON d.id=pages_fts.rowid "
                                   "WHERE pages_fts MATCH ? ORDER BY bm25(pages_fts) LIMIT 40", (fts,)).fetchall()
            else:
                like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                rows = con.execute("SELECT file, page, body FROM pages_data WHERE body LIKE ? ESCAPE '\\' LIMIT 40", (like,)).fetchall()
        finally:
            con.close()
    except Exception as e:
        return {"error": str(e)[:120]}
    cur = ctx.get("file_rel", "")
    hits = []
    for file, page, body in rows[:15]:
        low = (body or "").lower(); pos = low.find(q.lower())
        snip = (body[max(0, pos - 15):pos + len(q) + 25] if pos >= 0 else (body or "")[:60]).replace("\n", " ").strip()
        hits.append({"book": names.get(file, file), "file_rel": file, "page": page, "snippet": snip, "is_current_book": file == cur})
    return {"total": len(rows), "hits": hits,
            "note": "is_current_book=true 是用户正在读的书。要跳到别的书用 open_book(file_rel, page)。"}


def _t_open_book(args, ctx):
    """打开另一本书(可定位到页),前端导航。args {file_rel | book(书名,模糊匹配书架), page?}。"""
    file_rel = (args.get("file_rel") or "").strip()
    if not file_rel:
        name = (args.get("book") or "").strip()
        for b in (ctx.get("books") or []):
            if name and (name in (b.get("name") or "") or (b.get("name") or "") in name):
                file_rel = b.get("rel"); break
    if not file_rel or ".." in file_rel:
        return {"error": "没找到要打开的书(给 file_rel 或准确书名)"}
    try:
        page = int(args.get("page") or 1)
    except Exception:
        page = 1
    return {"ok": True, "note": f"正在打开《{file_rel.split('/')[-1]}》第{page}页",
            "client_action": {"fn": "openBookAt", "args": [file_rel, page]}}


def _t_summarize_section(args, ctx):
    """取『当前页所在章节』的正文(按 PDF 书签/TOC 切片,上限 ~9000 字)交给你总结。read_page 只逐页,『总结这一章/这一节』用这个。args {page?}"""
    file_rel = ctx.get("file_rel", "")
    if not file_rel or ".." in file_rel:
        return {"error": "没开书"}
    try:
        page = int(args.get("page") or (ctx.get("pages") or [ctx.get("page")])[0] or 1)
    except Exception:
        page = 1
    try:
        import fitz
        ap = (VAULT_ROOT / file_rel).resolve(); ap.relative_to(VAULT_ROOT.resolve())
        doc = fitz.open(str(ap))
        try:
            toc = doc.get_toc() or []
            n = doc.page_count
            start, end, title = 1, n, ""
            cands = [(t[2], t[1]) for t in toc if t[2] and t[2] <= page]
            if cands:
                start, title = max(cands, key=lambda x: x[0])
            nexts = [t[2] for t in toc if t[2] and t[2] > start]
            if nexts:
                end = min(nexts) - 1
            end = min(end, start + 30)   # 防超长章爆量(无 TOC 时退化为当前页起 30 页)
            if not toc:
                start, end = max(1, page - 2), min(n, page + 12)
            parts, total = [], 0
            for pg in range(start, end + 1):
                t = (doc[pg - 1].get_text("text") or "").strip()
                if t:
                    parts.append(f"【第{pg}页】{t}")
                    total += len(t)
                if total > 9000:
                    end = pg
                    break
        finally:
            doc.close()
        if not parts:
            return {"error": "这一节没取到文字(可能纯扫描未OCR,可改用 see_page 逐页看)"}
        return {"section_title": title, "page_range": [start, end], "truncated": total > 9000,
                "text": "\n\n".join(parts)[:9000],
                "note": "这是当前页所在章节的正文,请总结成:核心要点 / 关键定义 / 重要公式 / 易错点。引用具体内容时标注来源页(第N页)。"}
    except Exception as e:
        return {"error": str(e)[:140]}


def _mark_source_highlight(ctx, color):
    """双向回链:制卡/做笔记后,在 PDF 原文(用户选中的那段)留一个高亮 → 翻回去能看出『这段已沉淀』。"""
    try:
        sel = (ctx.get("selection") or "").strip()
        if sel and len(sel) > 4 and ctx.get("file_rel"):
            _t_highlight({"texts": [sel], "color": color}, ctx)
    except Exception:
        pass


TOOLS = {
    "read_page": ("读当前页(或指定页)正文。args {page?}", _t_read_page),
    "read_selection": ("读用户当前选中的文字。args {}", _t_read_selection),
    "search_book": ("在当前这本书全文搜关键词,返回命中页+片段。args {query}", _t_search_book),
    "search_all_books": ("跨『我所有的书』全文搜索(用户问『哪本书讲过X/别的书有没有X/之前在哪见过』时用)。args {query}", _t_search_all_books),
    "open_book": ("打开另一本书并可定位到页(跨书跳转)。args {file_rel | book(书名), page?}", _t_open_book),
    "summarize_section": ("取当前页所在『整章/整节』正文交给你总结(read_page 只逐页,『总结这一章』用这个)。args {page?}", _t_summarize_section),
    "translate": ("翻译文字成中文(或 target 语言)。不传 text 则译选中/本页。args {text?, target?}", _t_translate),
    "goto_page": ("翻到指定页(前端跳转)。args {page}", _t_goto_page),
    "make_anki": ("把内容做成 Anki 卡(后台,带原文链接,完成通知)。args {text?}(不传用选中)", _t_make_anki),
    "make_note": ("把内容整理成 Obsidian 笔记(后台)。args {text?}(不传用选中/本页)", _t_make_note),
    "add_vocab": ("把英文单词加生词本并制卡(后台)。args {word?}(不传用选中)", _t_add_vocab),
    "highlight": ("在 PDF 上把重点句子画高亮(可撤销)。args {texts:[\"原句1\",\"原句2\"], color?}。"
                  "texts 必须是页面上的**原文逐字**(从 read_page 结果照抄,别改写/别翻译),否则定位不到", _t_highlight),
    "page_vocab": ("查掌握度数据库:不传 words=当前页『还没掌握』的生词(权威,跟页面下划线一致);"
                   "传 words(数组)=逐词查掌握度(英+日)。args {words?:[...]}", _t_page_vocab),
    "lookup_word": ("查词典:英→ECDICT(音标+中文释义+原形)、日→unidic **权威读音+声调**。"
                    "**读音/释义以它为准,别自己编读音**;你只结合上下文挑义项+讲解。args {word?}(不传用选中)", _t_lookup_word),
    "see_page": ("**真正看到**当前页(或指定页)的渲染图——图表/示意图/曲线/公式排版/手写等文字层读不到的东西。"
                 "read_page 只有文字层、看不见图形;用户问『这张图/这个图表/这页的图/看一下』时用 see_page。args {page?}", _t_see_page),
    "see_figure": ("看用户**当前聚焦的那张图**的裁剪渲染图(他点选/拖进来的图;有手写笔迹则看合成图)。"
                   "已给的图说明不够、要核对图里的具体细节/用户在图上的标注时用。args {}", _t_see_figure),
    "undo_last": ("撤销最近一次写操作(删掉刚建的卡/笔记/高亮)。用户说『撤销/取消刚才那个』时用。args {}", _t_undo_last),
}


def _tool_label(name, args):
    return {"read_page": "读取页面", "read_selection": "读取选中", "search_book": "搜索全书",
            "search_all_books": "跨书搜索", "open_book": "打开书", "summarize_section": "总结本章",
            "translate": "翻译", "goto_page": "翻页", "make_anki": "制卡", "make_note": "整理笔记",
            "add_vocab": "加生词本", "highlight": "高亮", "page_vocab": "查掌握度",
            "lookup_word": "查词典", "see_page": "看页面图", "see_figure": "看这张图", "undo_last": "撤销"}.get(name, name)


# ──────────────────────── agent 循环 ────────────────────────
def _sys_prompt(ctx):
    cat = "\n".join(f"- {n}: {d}" for n, (d, _) in TOOLS.items())
    vis = ctx.get("pages") or ([ctx.get("page")] if ctx.get("page") else [])
    meta = {"book": ctx.get("book_name"), "当前可见页": vis, "共": ctx.get("total")}
    if ctx.get("langs"):
        meta["书语言"] = ctx.get("langs")              # 让助手知道用 en/ja 处理,不必猜
    if ctx.get("read_mode") and ctx.get("read_mode") != "continuous":
        meta["排版"] = ctx.get("read_mode")            # spread=双页,知道在看哪两页
    meta = {k: v for k, v in meta.items() if v}
    sel = _clean_tag(ctx.get("selection"))
    sent = _clean_tag(ctx.get("selection_sentence"))
    sel_line = ""
    if sel:
        sel_line = f"\n用户当前选中:「{sel[:200]}」"
        if sent and sent.replace(" ", "") != sel.replace(" ", ""):
            sel_line += f"\n选中所在句(已给好的上下文,可直接据此判读音/义项,**不必**再 read_page):「{sent[:300]}」"
    # 带入的图(用户点/拖进来的,可多张):带上各自 AI 描述当上下文,要核对图像细节才 see_figure
    figs = ctx.get("figures") or ([ctx["figure"]] if ctx.get("figure") else [])
    fig_line = ""
    if figs:
        items = []
        for i, fg in enumerate(figs):
            fcap = _clean_tag(fg.get("caption")); fdesc = _clean_tag(fg.get("desc"))
            ink = "(有手写笔迹/圈点)" if fg.get("has_ink") else ""
            items.append(f"[{i + 1}] 「{fcap[:48]}」p{fg.get('page')}{ink}:{fdesc[:300]}")
        if len(figs) == 1:
            fig_line = "\n用户带入了一张图,默认在问它:\n" + items[0] + \
                "。先据这段说明回答;说明不够或需核对图里细节/手写标注时才 see_figure(args {})。"
        else:
            fig_line = f"\n用户带入了 {len(figs)} 张图(默认在问/对比这些图):\n" + "\n".join(items) + \
                "\n先据这些说明回答;要核对某张图的细节/手写标注时用 see_figure(args {index:第几张,从1起;不传=全部)。"
    # 本页知识点 / 未掌握生词 / 用户钉住的焦点(前端 __voiceContext 已采集好,直接给 → 答这类问题免工具往返)
    learn_bits = []
    nodes = ctx.get("visible_kg_nodes") or []
    if nodes:
        nm = "、".join(_clean_tag(n.get("name")) for n in nodes[:20] if n.get("name"))
        if nm:
            learn_bits.append(f"本页知识点(技能图谱):{nm}")
    vocab = ctx.get("visible_vocab") or []
    if vocab:
        vv = "、".join(_clean_tag(w) for w in vocab[:30] if w)
        if vv:
            learn_bits.append(f"本页『还没掌握』的生词(页面下划线词):{vv}")
    fsel = ctx.get("focus_sel") or {}
    if isinstance(fsel, dict) and fsel.get("text"):
        fkind = "公式" if fsel.get("kind") == "formula" else "段落"
        learn_bits.append(f"★用户钉住了一个焦点{fkind}(右侧 chip,默认在专门问它):「{_clean_tag(fsel.get('text'))[:240]}」")
    learn_line = ("\n" + "\n".join(learn_bits)) if learn_bits else ""
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
        "  ——第2步绝不能省,用户要的是『卡』不是只看总结。同理『翻译并制卡』要 translate 后再 make_anki。\n"
        "★高亮重点:先 read_page 拿到正文,再把要强调的几句**原句逐字**(从正文照抄,不要改写/翻译)"
        "**一次性**放进 highlight 的 texts 数组(一次调用搞定,别一句一调),否则在 PDF 上定位不到。\n"
        "★【最近对话】里每条用户消息都带括号标注了当时所在的书/页/选中句。用户说『刚才那页/上一页/回到那页/前面说的那段』时,"
        "**从最近对话的标注里取出确切页码**,直接 goto_page(或 read_page 指定该页),别反问『哪一页』。\n"
        "★凡涉及『我(没)掌握哪些词/这页生词/某词我会不会』——**必须调 page_vocab 查掌握度数据库**,"
        "**严禁**拿正文里的词自己猜谁掌握没掌握(数据库才是准的:已掌握的词不算生词、从没查过的词系统不视为生词)。"
        "不传 words 拿本页未掌握生词;问具体某些词会不会就传 words:[...]。\n"
        "★查词/读音(尤其日语读音、英语音标释义)**一律先用 lookup_word**——它走 ECDICT/unidic 离线权威词典,"
        "**读音和释义以它为准,严禁自己编读音**(LLM 读音不可靠);你只负责结合上下文挑最贴切的义项、讲解。\n"
        "★用户**当前有选中文字**(见【当前页面】末尾『用户当前选中』)时,默认他就是在问这段选中内容——"
        "优先针对**选中**回答/查词/翻译/解释/语法/制卡。"
        "**为定准读音和含义,先 lookup_word 拿权威读音+释义**,再结合上下文挑义项(日语同字多音、含义随语境变);"
        "上下文优先用末尾已给好的『选中所在句』——**有它就别再 read_page**,只有所在句仍不足以定义项时才 read_page 拿更多段落。"
        "但**『页内有图』≠『要看图』**:选中只是文字、其上下文里并没有图时,**别 see_page**;"
        "只有选中内容/它的上下文**确实涉及某张图**(如『图1-3』『如下图』指代某图),或用户明确说『这张图/这页的图/图里画的』,才 see_page。\n"
        "★see_page 收紧:**别因为『页面里有图』就主动去看图**(漫画/插图书每页都有图,但用户多半在问文字/选中)。\n"
        "★『总结这一章/这一节/这部分』用 summarize_section(它按书签切出整章正文);只『总结这页』才用 read_page。\n"
        "★『我哪本书讲过X/别的书有没有X/之前在哪见过』用 search_all_books;要跳到搜到的别的书用 open_book(file_rel,page)。\n"
        "★可溯源:凡复述/引用书里的具体内容,在句末标来源页「(第N页)」,N 必须来自工具实际返回的页码(read_page/search_book/summarize_section 都带页码),**不许编页码**。前端会把『第N页』变成可点跳转。\n"
        "★【追问建议】每次给最终回答时,在正文最后**另起一行**写 2-3 个贴合当前内容、能推进理解的下一步问题,"
        "格式就一行:[[FOLLOWUP]]问题1|问题2|问题3(用 | 分隔,放在整条回答末尾,前端会渲成可点按钮;问题要短、具体)。"
        "**每条最终回答都要带**;只有在调工具(输出 JSON)那几条里不要带。\n\n"
        f"【可用工具】\n{cat}\n\n"
        f"【当前页面】{json.dumps(meta, ensure_ascii=False)}{sel_line}{fig_line}{learn_line}"
    )


def _clean_tag(s):
    """规整用户内容(选中句/书名)再拼进 prompt:折叠所有空白(含换行)成单空格 +
    去掉 【】「」(它们是 _agent_run 切分 turn/【最近对话】分段的标签,裸拼会破坏结构甚至被注入伪造段)。"""
    s = " ".join(str(s or "").split())
    return s.translate({ord(c): None for c in "【】「」"})


def _loc_tag(h):
    """把某轮对话发生时的位置(书/页/选中句)拼成一小段标注,供助手定位『刚才那页』。"""
    bits = []
    book = _clean_tag(h.get("book"))
    pages = h.get("pages") or ([h.get("page")] if h.get("page") else [])
    pages = [p for p in pages if p]
    if book:
        bits.append(book)
    if pages:
        bits.append("第" + "/".join(str(p) for p in pages) + "页")
    sel = _clean_tag(h.get("selection"))
    if sel:
        bits.append("选中「" + sel[:40] + "」")
    return ("(" + "，".join(bits) + ")") if bits else ""


def _format_history(history):
    out = []
    for h in (history or [])[-6:]:
        if h.get("role") == "user":
            role, tag = "用户", _loc_tag(h)   # 用户那轮标上当时所在页/书/选中句
        else:
            role, tag = "助手", ""
        c = (h.get("content") or "").strip()
        if c:
            out.append(f"{role}{tag}:{c[:600]}")
    return ("【最近对话】\n" + "\n".join(out) + "\n") if out else ""


def _parse_tool(raw):
    """开头是 {"tool":...} JSON → 工具调用;否则 None(当作给用户的回答)。
    用 raw_decode 只解析**开头那个 JSON 对象**,容忍尾部多余内容(模型偶尔在工具 JSON 后跟了
    [[FOLLOWUP]]/解释 → 整串 json.loads 会失败,导致工具 JSON 被当回答显示、工具不执行)。容忍 ```json 围栏。"""
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    if not s.startswith("{"):
        return None
    # 字面控制字符(模型把多行 OCR 文本照抄进字符串值、没转义换行 → JSON 非法解析失败 → 工具不执行)→ 换空格。
    # 合法 JSON 的控制字符必是 \n/\uXXXX 多字符转义,绝不会是字面 0x00-0x1f,故这步对合法 JSON 是 no-op。
    import re
    s = re.sub(r"[\x00-\x1f]", " ", s)
    try:
        d, _end = json.JSONDecoder().raw_decode(s)   # 解析第一个 JSON 值,忽略其后任何内容
        return d if isinstance(d, dict) and "tool" in d else None
    except Exception:
        return None


def _agent_run(message, ctx, history):
    """生成 SSE 事件 dict:{event, data}。event ∈ tool|tool-done|answer|actions|error。"""
    p = _take_proc()
    if not p:
        yield {"event": "error", "data": "助手起不来(claude 起不来)"}
        return
    _qw = _quota_warning()   # 额度护栏:近上限只提醒,不降级、不阻断(用户不用 Gemini 降级)
    if _qw:
        yield {"event": "notice", "data": _qw}
    client_actions = []
    try:
        content = f"{_sys_prompt(ctx)}\n\n{_format_history(history)}【用户】{message}\n\n现在开始(调工具就只输出 JSON,能答就直接答):"
        _t_start = time.time()
        _repair_tries = 0
        for step in range(40):   # 步数放很高(40):真正的护栏是下面的总超时(200s),步数只当 runaway 兜底,别因步数砍掉复杂多工具任务
            if time.time() - _t_start > 240:   # 总超时:防卡死的 claude 占住 gunicorn worker
                yield {"event": "answer", "data": "(处理用时太久,先到这——可以再问我一次,或换个更具体的问法)"}
                break
            raw = None
            _last_emit = 0.0
            for kind, val in _send_stream(p, content):
                if kind == "delta":
                    # tool 调用是裸 JSON(以 { 开头)→ 不流式;最终回答是文字 → 逐字吐 answer(前端边接边渲染)
                    if val and not val.lstrip().startswith("{"):
                        now = time.time()
                        if now - _last_emit > 0.1:   # 节流 ~100ms,减 SSE/重渲(末尾 answer 会补完整)
                            _last_emit = now
                            yield {"event": "answer", "data": val}
                else:
                    raw = val
            if not raw:
                yield {"event": "error", "data": "助手没响应(超时)"}
                return
            tool = _parse_tool(raw)
            # 自愈:看着像工具调用却解析不出(texts 里常有没转义的引号/换行 → JSON 非法)→ 别当回答显示,
            # 反馈给模型让它重出一条合法 JSON(最多 2 次),避免"工具 JSON 被当成回答、工具没执行"
            rs = (raw or "").strip()
            if tool is None and rs.startswith("{") and '"tool"' in rs[:400] and _repair_tries < 2:
                _repair_tries += 1
                yield {"event": "tool", "data": "整理指令"}
                yield {"event": "tool-done", "data": "整理指令"}
                content = ("你上一条像是工具调用,但 JSON 没解析成功(很可能字符串里有**没转义的双引号**或**换行**)。"
                           "请重新只输出**一条合法的 JSON**工具调用:字符串里的引号一律换成中文引号「」、**不要带换行**、整条只输出 JSON 别加别的字。")
                continue
            if tool and tool.get("tool") in TOOLS:
                name = tool["tool"]
                targs = tool.get("args") if isinstance(tool.get("args"), dict) else {}
                yield {"event": "tool", "data": _tool_label(name, targs)}
                try:
                    res = TOOLS[name][1](targs, ctx) or {}
                except Exception as e:
                    res = {"error": str(e)[:160]}
                vision = res.pop("_vision", None) if isinstance(res, dict) else None   # 图片喂回大脑(sonnet 多模态)
                if isinstance(res, dict) and res.get("client_action"):
                    client_actions.append(res.pop("client_action"))
                if isinstance(res, dict) and res.get("task_id"):   # 后台写任务 → 前端轮询完成+给撤销按钮
                    yield {"event": "task", "data": {"task_id": res["task_id"], "label": _tool_label(name, targs)}}
                if isinstance(res, dict) and res.get("undo_id"):   # 同步写操作(高亮)→ 立即给撤销按钮
                    yield {"event": "undo", "data": {"undo_id": res["undo_id"], "label": _tool_label(name, targs)}}
                yield {"event": "tool-done", "data": _tool_label(name, targs)}
                text_part = "【工具结果】" + json.dumps(res, ensure_ascii=False)[:6000] + "\n\n继续(调工具只输出 JSON,能答就直接答):"
                if vision:   # see_page:把渲染图作为 image block 喂回(大脑 sonnet 能看图)
                    content = [{"type": "text", "text": text_part}]
                    for v in vision[:2]:
                        content.append({"type": "image", "source": {
                            "type": "base64", "media_type": v.get("media_type", "image/png"), "data": v["b64"]}})
                else:
                    content = text_part
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
    uid = session["user_id"]
    ctx = body.get("context") or {}
    ctx["_base"] = request.host_url.rstrip("/")
    ctx["_uid"] = uid   # 写操作(制卡/笔记/高亮)记 owner=本用户 → 撤销只能撤自己的(防越权)
    # 服务端历史(含每轮所在页/书/选中句,让助手能定位"刚才那页")。先取(=本轮之前的),再早落库本轮用户消息。
    history = [{k: m.get(k) for k in ("role", "content", "page", "pages", "book", "file_rel", "selection")}
               for m in _convo_load(uid)[-6:]]
    # 用户消息**进入 agent 之前**就落库 → 移动端切后台/锁屏断连也不丢这轮,且保住"刚才那页"定位链
    _convo_append(uid, "user", message, {
        "page": ctx.get("page"), "pages": ctx.get("pages"),
        "book": ctx.get("book_name"), "file_rel": ctx.get("file_rel"),
        "selection": ctx.get("selection"),
        "figures": [{k: f.get(k) for k in ("page", "box", "caption", "group", "has_ink", "file_rel")}
                    for f in (ctx.get("figures") or [])][:6],
    })

    def gen():
        final = ['']
        try:
            for ev in _agent_run(message, ctx, history):
                if ev['event'] == 'answer':
                    final[0] = ev['data']
                yield f"event: {ev['event']}\ndata: {json.dumps(ev['data'], ensure_ascii=False)}\n\n"
            yield "event: done\ndata: {}\n\n"
        except Exception as e:   # 普通异常才回 error 事件;客户端断连是 GeneratorExit(BaseException,不被此捕获)→ 直接走 finally 落库
            try:
                yield f"event: error\ndata: {json.dumps(str(e)[:160], ensure_ascii=False)}\n\n"
            except Exception:
                pass
        finally:
            # 助手回答在 finally 落库:断连/出错时也保住已流式出来的部分(跨设备续上不断片)
            if final[0]:
                _convo_append(uid, "assistant", str(final[0])[:1500])

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@bp.route("/history")
def assistant_history():
    if not _logged_in():
        return jsonify({"ok": False}), 401
    return jsonify({"ok": True, "messages": _convo_load(session["user_id"])[-100:]})


@bp.route("/clear", methods=["POST"])
def assistant_clear():
    if not _logged_in():
        return jsonify({"ok": False}), 401
    _convo_clear(session["user_id"])
    return jsonify({"ok": True})


@bp.route("/undo", methods=["POST"])
def assistant_undo():
    if not _logged_in():
        return jsonify({"ok": False}), 401
    undo_id = (request.get_json(silent=True) or {}).get("id")
    import voice
    return jsonify(voice._undo_do(undo_id, owner=session["user_id"]))   # owner 校验:只能撤自己的(防猜 id 删别人的)


@bp.route("/prewarm", methods=["POST"])
def assistant_prewarm():
    if not _logged_in():
        return jsonify({"ok": False}), 401
    off = bool((request.get_json(silent=True) or {}).get("off"))
    threading.Thread(target=(_warm_reap if off else _warm_prewarm), daemon=True).start()
    return jsonify({"ok": True})


def register_assistant(app):
    app.register_blueprint(bp)
