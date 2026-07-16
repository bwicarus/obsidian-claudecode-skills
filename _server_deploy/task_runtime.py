"""task_runtime.py — 任务运行时:确定性状态机,编排"有停顿、有等待、有循环"的长任务。

设计见 references/adr-task-runtime.md。第一个 kind = 听写(dictation)。

═══ 两条铁律(都是 2026-07-14 用血换来的)═══

**① LLM 不做等待,也不做循环。**
   让模型在工具循环里挂起等用户按按钮 → 每等一次重发整个上下文(烧钱)、刷新即丢(不可恢复)、
   模型会忘记念到第几个(不可靠)。所以:循环/等待/超时/恢复 全在这里,LLM 只负责
   **生成内容**(words[])和**做判断**(批改)。

**② 绝不许有"阻塞等待的线程"。**
   同日事故:SSE 长连接每条独占一个 gthread 线程,8 条就把线程池吃光、**全站零响应**
   (见 reader_events.py 文件头 + memory sse-thread-starvation)。
   所以这是**事件驱动的状态机**,不是"等待的线程":
     · 状态存文件(state/reader-runs/<rid>.json)→ 进程重启不丢
     · 推进由**事件**触发:① 按钮 HTTP 上报 ② 前端回前台对齐 ③ (将来)定时器
     · **任何时刻零线程**被这个任务占用
   唯一会起线程的地方是"批改"(要调 LLM,是有界的短任务),而且是 fire-and-forget,不是等待。

═══ 状态机 ═══
  status: running(在推进) | waiting(挂起,等事件) | done | error | cancelled
  推进入口只有一个:advance(rid, event) —— 幂等,同一个事件重复到达不会走两步。

⚠ 部署:本文件 + pdf_reader.py + assistant.py 一起 cp 到 /home/bwicarus/webapp/ 并重启 webapp。
"""
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

RUNS_DIR = Path("/home/bwicarus/claude/state/reader-runs")
RUN_TTL = 7 * 86400          # 7 天后清理(听写纸本身留着,只清运行状态)
WAIT_TIMEOUT = 3600          # 挂起超时:1 小时没动静就判 cancelled(防僵尸 run 永远占着页面)

_lock = threading.Lock()

# 前端在点「检查」时随请求带来的**整页渲染截图**(所见即所得,题目+手写都在),按 rid 暂存,
# _check_page 取用后即弃。存内存不落 run 文件(base64 大,别撑爆 state/reader-runs)。
_CHECK_SHOTS = {}


def set_check_shots(rid, shots):
    if rid and isinstance(shots, list):
        _CHECK_SHOTS[str(rid)] = [s for s in shots if isinstance(s, dict) and s.get("b64")][:8]


# ── 存储 ──────────────────────────────────────────────────────────────────────
def _path(rid: str) -> Path:
    return RUNS_DIR / f"{rid}.json"


def load(rid: str):
    try:
        return json.loads(_path(rid).read_text("utf-8"))
    except Exception:
        return None


def _save(run: dict):
    run["updated_at"] = int(time.time())
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    p = _path(run["rid"])
    tmp = p.with_name(p.name + ".tmp")   # 原子替换:读者永远看到完整的旧/新文件
    tmp.write_text(json.dumps(run, ensure_ascii=False), "utf-8")
    tmp.replace(p)


def _gc():
    try:
        cutoff = time.time() - RUN_TTL
        for f in RUNS_DIR.glob("r_*.json"):
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
    except Exception:
        pass


# ── 对前端的"手和嘴"(全部借道已有基础设施,不新建通道)───────────────────────
def _say(run: dict, text: str):
    """让前端念一段话。**没有服务端 TTS**(全站都没有)——借 client-action 遥控前端的
    __vcSpeakText(rc-voicecall.js:1937),它背后是完整的豆包 TTS 链路(分段/打断/AEC)。
    MCP 遥控翻页走的就是这条路。
    ⚠ 页面不可见时 SSE 事件会被前端**直接丢弃**(pdf-tail.js 的 visibilityState 早退)→
      所以状态机**不能依赖"念出去了"**;真正的推进信号是用户按按钮。回前台由 status 接口对齐。"""
    try:
        from reader_events import publish
        publish("client-action", run.get("file") or "", run.get("uid"),
                {"action": {"fn": "__vcSpeakText", "args": [text]}})
    except Exception:
        pass


def _push_run(run: dict):
    """把 run 的当前状态推给前端(更新按钮文案/进度)。同样复用那条唯一的 SSE。"""
    try:
        from reader_events import publish
        publish("run", run.get("file") or "", run.get("uid"),
                {"run": {"rid": run["rid"], "status": run["status"], "kind": run["kind"],
                         "state": run.get("state") or {}, "upage": run.get("upage"),
                         "hint": run.get("hint") or "", "result_md": run.get("result_md") or "",
                         "check_name": run.get("check_name") or "", "check_score": run.get("check_score") or ""}})
    except Exception:
        pass


def _set_blocks(run: dict, blocks: list, kind: str = ""):
    """经**格子布局器**排版后写进插入页 sidecar(直接改 sidecar,运行时就跑在 webapp 进程内)。

    ★ 布局器会给每个块补上 at/span/**rect(页归一化 bbox)** —— 纯算术,不需要前端量。
      rect 与墨迹坐标、与服务端裁图 box 是**同一坐标系**,这是"按填空裁图批改"的根基。
    ⚠ 溢出会被 layout() 拆成多张纸;当前切片只用第一张(多纸支持见 ADR 待办)。"""
    import pdf_reader as P
    import paper as PA
    import fitz
    rel = run["file"]
    pk = run.get("paper") or ("dictation" if run["kind"] == "dictation" else "note")
    try:
        d = fitz.open(str(P._safe_vault_path(rel)))
        r = d[max(0, int(run.get("page") or 1) - 1)].rect
        pw, ph = float(r.width), float(r.height)
        d.close()
    except Exception:
        pw, ph = 595.0, 842.0
    pl = PA.plan(pk, blocks, pw, ph)
    laid = pl["papers"][0] if pl["papers"] else []
    run["n_pages"] = pl["n_pages"]
    run["pages_info"] = [{"upage": run.get("upage"), "page": run.get("page")}]   # 第 1 张
    if pl["n_pages"] > 1:
        # 溢出的纸(第 2 张起)存进 run,等前端建好页调 attach_page 逐张写回(多纸自动补页,#33)
        run["_overflow"] = {"spec": pl["spec"], "pages": pl["papers"][1:], "kind": kind}
        run["hint"] = (run.get("hint") or "") + f"(内容较多,自动补到 {pl['n_pages']} 张纸)"
    else:
        run.pop("_overflow", None)
    with _lock:
        items = P._upages_load(rel)
        for it in items:
            if it.get("id") == run.get("upage"):
                it["blocks"] = laid
                it["paper"] = pl["spec"]      # 前端按这个规格**严格按格子绝对定位**渲染
                if kind:
                    it["kind"] = kind
                it["run_id"] = run["rid"]
                it["updated"] = int(time.time())
                break
        P._upages_save(rel, items)
    try:   # 让打开着这本书的页面立刻重画那一页
        from reader_events import publish
        publish("text", rel, run.get("upage"))
    except Exception:
        pass


def attach_page(rid, upage, page, index):
    """多纸自动补页:前端建好第 index 张溢出页后调这个,把 _overflow 里第 index 张的块写进它的
    sidecar,并登记进 run["pages_info"]。index 从 1 起(第 1 张是原始纸)。返回 {ok, done}。"""
    import pdf_reader as P
    run = load(rid)
    if not run:
        return {"ok": False, "error": "run 不存在"}
    ov = run.get("_overflow") or {}
    pages = ov.get("pages") or []
    if index < 1 or index > len(pages):
        return {"ok": False, "error": "index 越界"}
    laid = pages[index - 1]
    with _lock:
        items = P._upages_load(run["file"])
        for it in items:
            if it.get("id") == upage:
                it["blocks"] = laid
                it["paper"] = ov.get("spec")
                if ov.get("kind"):
                    it["kind"] = ov["kind"]
                it["run_id"] = run["rid"]
                it["updated"] = int(time.time())
                break
        P._upages_save(run["file"], items)
    pi = run.setdefault("pages_info", [{"upage": run.get("upage"), "page": run.get("page")}])
    if not any(x.get("upage") == upage for x in pi):
        pi.append({"upage": upage, "page": int(page)})
    _save(run)
    try:
        from reader_events import publish
        publish("text", run["file"], upage)
    except Exception:
        pass
    return {"ok": True, "done": len(pi) >= int(run.get("n_pages") or 1)}


# ── 听写程序(kind='dictation')────────────────────────────────────────────────
# 步骤是**显式的、可恢复的**:任何一步都只看 run["step"] + run["state"],不依赖内存。
def _paper_blocks(words: list, title: str) -> list:
    """一张听写纸的**元素清单**(A 模式:不带坐标,布局器自己排)。
    ⚠ rect **不在这里给** —— 由 paper.layout() 从格子(row,col,span)**纯算术算出**。
      服务端自己就知道每个填空在哪,不需要前端渲染完再量出来写回(那一环又丑又不可靠)。"""
    bs = [{"id": "title", "kind": "text", "text": title, "style": "h1"}]
    for i, _w in enumerate(words):
        bs.append({"id": f"q{i + 1}", "kind": "blank", "label": f"{i + 1}."})
    bs.append({"id": "btn_next", "kind": "button", "label": "▶ 念下一个", "event": "next"})
    bs.append({"id": "btn_done", "kind": "button", "label": "✓ 写完了,批改", "event": "grade"})
    return bs


def _dictation_tick(run: dict, event: str) -> bool:
    """推进听写一步。返回 True=状态有变(要落盘)。**永不阻塞**。"""
    words = (run.get("params") or {}).get("words") or []
    st = run.setdefault("state", {})
    i = int(st.get("i") or 0)

    if run["step"] == 0:                       # ① 铺纸
        _set_blocks(run, _paper_blocks(words, (run.get("params") or {}).get("title") or "听写"),
                    kind="dictation")
        run["step"] = 1
        run["status"] = "waiting"
        run["wait"] = {"event": "start", "since": int(time.time())}
        run["hint"] = f"听写纸已生成({len(words)} 题)。点「▶ 念下一个」开始。"
        return True

    if run["step"] == 1:                       # ② 循环:念一个 → 等按钮
        if event not in ("start", "next"):
            return False                       # 不是我等的事件 → 忽略(幂等)
        if i >= len(words):
            run["step"] = 2
            run["status"] = "running"
            return _dictation_tick(run, "")     # 立刻进批改判定
        _say(run, words[i])
        st["i"] = i + 1
        run["status"] = "waiting"
        run["wait"] = {"event": "next", "since": int(time.time())}
        run["hint"] = f"第 {i + 1}/{len(words)} 个。写完按「▶ 念下一个」。"
        if st["i"] >= len(words):
            run["hint"] = f"最后一个({len(words)}/{len(words)})。写完点「✓ 写完了,批改」。"
        return True

    if run["step"] == 2:                       # ③ 批改(唯一会起线程的地方:调 LLM,fire-and-forget)
        if run.get("status") == "grading":
            return False                       # 已经在批了,别重复起线程
        run["status"] = "grading"
        run["hint"] = "正在批改…"
        _save(run)
        _push_run(run)
        threading.Thread(target=_grade, args=(run["rid"],), daemon=True).start()
        return False                           # 已自行落盘

    return False


def _grade(rid: str):
    """批改:逐个 blank 裁图 → 一次 LLM 判卷 → 结果写回页面。跑在后台线程里(有界短任务)。

    🔴 关键(ADR §3.1):**不整页截图**。overlay 插入页在 PDF 文件里是**一张空白真页**
      (内容只在 sidecar,异步才写回)→ 整页裁出来是白纸,AI 看不到题号。
    绕法:按每个 blank 的 rect 裁出**这一格里用户手写的字**(_figure_crop_png(with_ink=True)),
      题号与正确答案**在 prompt 里用文字给**。零新渲染器、不依赖视口截图、分辨率可控。
    """
    run = load(rid)
    if not run:
        return
    try:
        import pdf_reader as P
        import assistant as A
        rel, page = run["file"], int(run.get("page") or 0)
        words = (run.get("params") or {}).get("words") or []
        ap = P._safe_vault_path(rel)
        strokes = P._page_ink_strokes(rel, page) or []

        blocks = []
        for it in P._upages_load(rel):
            if it.get("id") == run.get("upage"):
                blocks = it.get("blocks") or []
                break
        blanks = [b for b in blocks if b.get("kind") == "blank" and b.get("rect")][:len(words)]

        # ★ 一张整图(白底+全部手写)而非每题一张(省 token + 整页上下文);每题按纵向位置定位。
        import base64
        images, lines = [], []
        rects = [b["rect"] for b in blocks if b.get("rect")]
        if blanks and rects:
            mgn = 0.02
            box = [max(0.0, min(r[0] for r in rects) - mgn), max(0.0, min(r[1] for r in rects) - mgn),
                   min(1.0, max(r[2] for r in rects) + mgn), min(1.0, max(r[3] for r in rects) + mgn)]
            try:
                png = P._figure_crop_png(ap, page, box, with_ink=True, rel=rel, strokes=strokes)
            except Exception:
                png = None
            if png:
                images.append({"media_type": "image/png", "b64": base64.b64encode(png).decode()})
                bh = (box[3] - box[1]) or 1e-6
                for idx, b in enumerate(blanks):
                    cy = ((b["rect"][1] + b["rect"][3]) / 2 - box[1]) / bh
                    lines.append(f"第 {idx + 1} 题(纵向约 {round(cy * 100)}% 处),正确答案:「{words[idx]}」")

        if not images:
            run.update(status="error", error="没裁到任何手写内容(rect 还没写回?或者你还没写)")
            _save(run); _push_run(run)
            return

        prompt = ("这张图是用户在听写纸上**手写作答的一整页**(白底,只有手写)。按每题的**纵向位置**找到对应手写:\n"
                  + "\n".join(lines) +
                  "\n\n逐题判断写得对不对(手写体,允许笔画潦草;错字/漏字/假名写错都算错)。\n"
                  "**只输出 JSON**,形如:"
                  '{"items":[{"n":1,"ok":true,"got":"憂鬱","note":""},...],"score":"18/20","brief":"一句话总评"}')
        out = A.reader_vision(images, prompt, action="dictation_grade", uid=str(run.get("uid") or ""),
                              max_images=len(images))
        try:
            import re as _re
            m = _re.search(r"\{.*\}", out or "", _re.S)
            res = json.loads(m.group(0)) if m else {"brief": (out or "")[:300]}
        except Exception:
            res = {"brief": (out or "")[:300]}

        # 结果写回页面(追加一个 text 块;不动原来的题目块 —— 你手写的还在纸上)
        items = res.get("items") or []
        md = ["### 批改结果  " + str(res.get("score") or "")]
        for it in items:
            n = it.get("n")
            ok = "✅" if it.get("ok") else "❌"
            w = words[n - 1] if isinstance(n, int) and 1 <= n <= len(words) else ""
            got = it.get("got") or ""
            md.append(f"{ok} **{n}.** 正解「{w}」" + (f" · 你写的「{got}」" if got and not it.get("ok") else ""))
        if res.get("brief"):
            md.append("\n> " + str(res["brief"]))
        new_blocks = list(blocks) + [{"id": "grade", "kind": "text", "text": "\n".join(md)}]
        _set_blocks(run, new_blocks)

        run.update(status="done", result=res, hint="批改完成 ✅")
        _save(run); _push_run(run)
    except Exception as ex:
        run = load(rid) or run
        run.update(status="error", error=str(ex)[:200])
        _save(run); _push_run(run)


# ══════════ 阶段 B:内置按钮动作(任意纸通用,不依赖配方)══════════
# AI 用 write_page 造一张静态纸,按钮的 event 直接是**内置动作名**;运行时按块定义执行。
# 这样"AI 出卷 + 让 AI 检查"这条链**不需要编排 AI**,一次工具调用就能用(ADR 阶段 B)。
_BUILTIN = ("check", "say", "reveal", "hide", "goto")


def _blocks_of(run):
    import pdf_reader as P
    for it in P._upages_load(run["file"]):
        if it.get("id") == run.get("upage"):
            return it.get("blocks") or []
    return []


def _run_pages(run):
    """这个 run 涉及的所有纸(多纸支持):[{upage, page}, …]。单纸时就一项。"""
    pi = run.get("pages_info")
    if pi:
        return pi
    return [{"upage": run.get("upage"), "page": run.get("page")}]


def _blocks_of_page(run, upage):
    import pdf_reader as P
    for it in P._upages_load(run["file"]):
        if it.get("id") == upage:
            return it.get("blocks") or []
    return []


def _client(run, fn, args):
    try:
        from reader_events import publish
        publish("client-action", run.get("file") or "", run.get("uid"), {"action": {"fn": fn, "args": args}})
    except Exception:
        pass


def _reveal(run, block_id, show):
    import pdf_reader as P
    rel = run["file"]
    _upids = {pg.get("upage") for pg in _run_pages(run)}   # 多纸:跨本 run 所有纸找块
    with _lock:
        items = P._upages_load(rel)
        for it in items:
            if it.get("id") in _upids:
                for b in (it.get("blocks") or []):
                    if b.get("id") == block_id:
                        b["hidden"] = not show
                it["updated"] = int(time.time())
        P._upages_save(rel, items)
    try:
        from reader_events import publish
        publish("text", rel, run.get("upage"))
    except Exception:
        pass


def _set_enabled(run, block_id, enabled):
    """动态开/关某个按钮的可点态(#36 显示状态可被别的按钮/流程改)。跨本 run 所有纸找。"""
    import pdf_reader as P
    rel = run["file"]
    _upids = {pg.get("upage") for pg in _run_pages(run)}
    with _lock:
        items = P._upages_load(rel)
        for it in items:
            if it.get("id") in _upids:
                for b in (it.get("blocks") or []):
                    if b.get("id") == block_id and b.get("kind") == "button":
                        b["enabled"] = bool(enabled)
                it["updated"] = int(time.time())
        P._upages_save(rel, items)


def _save_toolshot(b64, mt="image/png"):
    """把一张图(b64)按内容 sha1 落盘到 toolshot 目录 → 返回 /pdf/api/toolshot/<name> URL。
    result_md 里只放 URL(b64 会撑爆 sidecar/历史 JSON)。给"检查判分依据图"用(#1)。"""
    import base64 as _b64, hashlib as _hl
    try:
        if not b64:
            return ""
        raw = _b64.b64decode(b64)
        ext = ".jpg" if ("jpeg" in (mt or "") or "jpg" in (mt or "")) else ".png"
        nm = _hl.sha1(raw).hexdigest()[:24] + ext
        d = Path("/home/bwicarus/claude/state/reader-toolshots")
        d.mkdir(parents=True, exist_ok=True)
        fp = d / nm
        if not fp.exists():
            fp.write_bytes(raw)
        return "/pdf/api/toolshot/" + nm
    except Exception:
        return ""


def _schedule(rid, event, secs):
    """一次性定时器:到点 fire-and-forget 调 advance(rid, event)。用于 wait_ms(#27)。
    不是常驻等待线程 —— 一个短 Timer,触发完即退,跟 _check_page 的后台线程同性质。"""
    def _fire():
        try:
            advance(rid, event)
        except Exception:
            pass
    tm = threading.Timer(max(0.05, float(secs)), _fire)
    tm.daemon = True
    tm.start()


def _call_tool(run, tool, arg):
    """按钮触发**任意工具**(去壳:进程内直接调 A.TOOLS[tool],不走 MCP)。
    #38「CLI 自己设计等待程序」的通用出口:CLI 造纸时给按钮绑 event='call:工具名',
    按下就在当前书/页 ctx 下跑那个工具,产出的 client_action 推给前端应用。"""
    try:
        import assistant as A
    except Exception:
        return
    fn = (A.TOOLS.get(tool) or (None, None))[1]
    if not fn:
        run["hint"] = f"没有叫「{tool}」的工具"
        return
    ctx = {"file_rel": run.get("file"), "page": run.get("page"), "_uid": run.get("uid")}
    try:
        res = fn({}, ctx) or {}
    except Exception as ex:
        run["hint"] = f"{tool} 出错:{str(ex)[:60]}"
        return
    if isinstance(res, dict) and res.get("client_action"):
        ca = res["client_action"]
        _client(run, ca.get("fn"), ca.get("args") or [])
    run["hint"] = (res.get("hint") if isinstance(res, dict) else None) or f"已执行 {tool}"


def _free_tick(run, event):
    """free 纸:按钮事件 = 内置动作名(带可选参数,冒号分隔)。CLI 造纸时给每个按钮绑不同 event
    就等于**自己设计了每个按钮按下干什么**(#38)。支持:
      check[:提示]     批改手写(裁图 + LLM)
      say:文本         念一句
      goto:页码        跳页
      reveal:块id / hide:块id      显/隐某块
      set_enabled:块id / disable:块id   开/关某按钮可点(#36)
      call:工具名       去壳调任意工具(通用出口)
    check/call 起后台或即时;其余同步瞬间完成。"""
    if run["step"] == 0:                       # 铺纸:AI 给的 blocks → 布局器排版
        run["paper"] = (run.get("params") or {}).get("paper") or "note"
        _set_blocks(run, (run.get("params") or {}).get("blocks") or [], kind="free")
        run["step"] = 1
        run["status"] = "waiting"
        run["hint"] = "纸已生成。填写/勾选后点纸上的按钮。"
        return True
    if not event:
        return False
    act, _, arg = event.partition(":")
    if act == "say" and arg:
        _say(run, arg)
        return False
    if act == "goto" and arg.isdigit():
        _client(run, "jumpWithBack", [int(arg)])
        return False
    if act in ("reveal", "hide") and arg:
        _reveal(run, arg, act == "reveal")
        return True
    if act in ("set_enabled", "enable", "disable") and arg:
        _set_enabled(run, arg, act != "disable")
        return True
    if act == "call" and arg:
        _call_tool(run, arg, "")
        return True
    if act == "check":
        if run.get("status") == "checking":
            return False
        run["status"] = "checking"
        run["hint"] = "正在检查…"
        _save(run)
        _push_run(run)
        threading.Thread(target=_check_page, args=(run["rid"], arg or ""), daemon=True).start()
        return False
    return False


def _grade_report(run, res):
    """给**前端编排 AI**的富报告:题目原文 + 各空标准答案 + 我的手写识别 + 判分。
    纸上只显示判分结论(result_md),但 AI 要分析错题必须看到**题干**——而题干在自制纸的
    overlay blocks 里(text 块=题干,blank.answer=标准答案),不在 PDF 文字层,read_page 读不到。
    (用户实测:AI 拿到『8题全空0分』却答不出『第一题题目是什么』的根因。)"""
    stem = []
    qn = 0
    try:
        for pg in _run_pages(run):
            for b in _blocks_of_page(run, pg.get("upage")):
                k = b.get("kind")
                if k == "text":
                    t = (b.get("text") or "").strip()
                    if t:
                        stem.append(t)
                elif k == "blank":
                    qn += 1
                    lab = (b.get("label") or "").strip()
                    ans = (b.get("answer") or "").strip()
                    seg = "第%d空" % qn + ((" " + lab) if lab else "")
                    seg += "＿＿" + (("(标准答案:%s)" % ans) if ans else "(无标准答案)")
                    stem.append(seg)
    except Exception:
        pass
    out = ["这是我在一张**自制练习纸**上的作答,已用 AI 判分。下面给你**题目原文 + 各空标准答案 + 我的手写识别 + 判分**,请据此帮我分析(题目不在书页正文里,就在下面,别再去 read_page 找):", ""]
    out.append("【题目纸原文(含各空标准答案)】")
    out += stem or ["(这张纸没有题干文字)"]
    out.append("")
    out.append("【判分】" + (("  得分 " + str(res.get("score"))) if res.get("score") else ""))
    for it in (res.get("items") or []):
        ok = "✅ 正确" if it.get("ok") else "❌ 错/空"
        out.append("第%s空 %s — 我写的:%s%s" % (it.get("n"), ok, (it.get("got") or "(空)"),
                   ("  · 判语:" + it.get("note")) if it.get("note") else ""))
    if res.get("brief"):
        out.append("总评:" + str(res.get("brief")))
    return "\n".join(out)


def _check_page(rid, prompt_hint=""):
    """★ 阶段 B 的核心:让 AI 看这一页的手写并点评。**任意纸通用**(不只听写)。
    按每个 blank 的 rect 裁出手写(有 answer 字段就带上正确答案);题号/答案在 prompt 里用文字给。
    把"AI 出题 → 你手写 → AI 批改"从听写里解耦 —— 任何纸配一个『让 AI 检查』按钮即可。"""
    run = load(rid)
    if not run:
        return
    try:
        import base64
        import re as _re
        import pdf_reader as P
        import assistant as A
        # #1:判分指令可在流程「检查·判分 AI」条里改(槽位 dictation_grade/main);
        #     本次 check 若带了 prompt_hint(按钮自定义)优先它,否则用用户设的判分指令。
        _instr = (prompt_hint or "").strip() or A._tps(str(run.get("uid") or ""), "dictation_grade", "main")
        rel = run["file"]
        ap = P._safe_vault_path(rel)
        shots = _CHECK_SHOTS.pop(rid, None)               # 前端渲染的整页截图(所见即所得:题目+手写都在图上)
        images = []
        lines = []
        qn = 0                                            # 跨页连续空号
        if shots:
            # ★ 首选:用**前端截图**(题干和手写都在,AI 看到的跟用户一样,最准)。按页对到每张图,
            #   每个空只需说"第 M 张图 从上往下第 K 个空 标准答案 X",AI 直接读页面判断。
            by_page = {}
            for s in shots:
                if isinstance(s, dict) and s.get("b64"):
                    by_page[int(s.get("page") or 0)] = s
            for pg in _run_pages(run):
                page = int(pg.get("page") or 0)
                s = by_page.get(page)
                if not s:
                    continue
                images.append({"media_type": s.get("media_type", "image/jpeg"), "b64": s["b64"]})
                imgno = len(images)
                blanks = sorted([b for b in _blocks_of_page(run, pg.get("upage"))
                                 if b.get("kind") == "blank" and b.get("rect")],
                                key=lambda b: b["rect"][1])   # 从上到下
                for k, b in enumerate(blanks):
                    qn += 1
                    ans = b.get("answer")
                    lines.append("第 %d 空 = 第 %d 张图从上往下第 %d 个空%s" % (qn, imgno, k + 1,
                                 ("(标准答案「%s」)" % ans) if ans else ""))
            base = ("每张图是用户**手写作答的一整页**(题目文字和手写都在图上,所见即所得)。共 %d 张图。\n"
                    % len(images) + "\n".join(lines) + "\n\n"
                    + _instr
                    + '\n**只输出 JSON**:{"items":[{"n":1,"ok":true,"got":"识别内容","note":"点评"}],'
                    + '"score":"可空","brief":"总评"}')
        else:
            # 回退:服务端拼图(一页一张整图,白底+合成手写;题目不在图上,靠纵向位置)。
            for pg in _run_pages(run):
                page = int(pg.get("page") or 0)
                strokes = P._page_ink_strokes(rel, page) or []
                blocks = _blocks_of_page(run, pg.get("upage"))
                blanks = [b for b in blocks if b.get("kind") == "blank" and b.get("rect")]
                if not blanks:
                    continue
                rects = [b["rect"] for b in blocks if b.get("rect")]
                mgn = 0.02
                box = [max(0.0, min(r[0] for r in rects) - mgn), max(0.0, min(r[1] for r in rects) - mgn),
                       min(1.0, max(r[2] for r in rects) + mgn), min(1.0, max(r[3] for r in rects) + mgn)]
                try:
                    png = P._figure_crop_png(ap, page, box, with_ink=True, rel=rel, strokes=strokes)
                except Exception:
                    png = None
                if not png:
                    continue
                images.append({"media_type": "image/png", "b64": base64.b64encode(png).decode()})
                imgno = len(images)
                bh = (box[3] - box[1]) or 1e-6
                for b in blanks:
                    qn += 1
                    cy = ((b["rect"][1] + b["rect"][3]) / 2 - box[1]) / bh
                    ans = b.get("answer")
                    lines.append("第 %d 空:第 %d 张图 纵向约 %d%% 处%s" % (qn, imgno, round(cy * 100),
                                 ("(标准答案「%s」)" % ans) if ans else ""))
            base = ("每张图是用户**手写作答的一整页**(白底,只有手写笔迹;题目文字不在图上)。"
                    "共 %d 张图。按下面每空的**纵向位置**在图里找到对应手写:\n" % len(images)
                    + "\n".join(lines) + "\n\n"
                    + _instr
                    + '\n**只输出 JSON**:{"items":[{"n":1,"ok":true,"got":"识别内容","note":"点评"}],'
                    + '"score":"可空","brief":"总评"}')
        if not images:
            run.update(status="error", error="这页没有可检查的手写填空(还没写?)")
            _save(run)
            _push_run(run)
            return
        out = A.reader_vision(images, base, action="dictation_grade", uid=str(run.get("uid") or ""),
                              max_images=min(len(images), 8))
        try:
            m = _re.search(r"\{.*\}", out or "", _re.S)
            res = json.loads(m.group(0)) if m else {"brief": (out or "")[:400]}
        except Exception:
            res = {"brief": (out or "")[:400]}
        # 检查结果是 **AI 的回复** → 放**卡片里**(用户拍板:AI 的回复在卡片中输出),
        #   **绝不塞进纸的格子** —— 一大段 markdown 当 text 块塞格子,布局器按字数估成几十行、
        #   字号=格高×ratio 就爆炸撑破整页(用户实测那张巨字图的根因)。
        md = ["### 检查结果  " + str(res.get("score") or "")]
        for it in (res.get("items") or []):
            ok = "✅" if it.get("ok") else "❌"
            md.append("%s **%s.** %s%s" % (ok, it.get("n"), it.get("got") or "",
                      (" — " + it.get("note")) if it.get("note") else ""))
        if res.get("brief"):
            md.append("\n> " + str(res["brief"]))
        # #1(用户):把**检查这个 AI 的相关信息**也显示出来——用了哪个模型、实际看的是哪张图(点击放大)。
        try:
            _mr = A._resolve("dictation_grade", str(run.get("uid") or ""))
            _model = _mr.get("variant") or _mr.get("backend") or "视觉模型"
            _shots = [u for u in (_save_toolshot(im.get("b64"), im.get("media_type")) for im in images) if u]
            _foot = ["\n---", "🔍 **判分依据** · 用 `%s` 看了下面这张图作答判分:" % _model]
            _foot += ["![判分图](%s)" % u for u in _shots]
            rmd = "\n".join(md + _foot)
        except Exception:
            rmd = "\n".join(md)
        # 富报告(题目原文+标准答案+手写+判分)。**不塞进上下文**(跟笔迹一样只告知存在),
        #   而是登记成一份**带名字的检查报告**;AI 被问到时调 read_check_report(name) 才拿全文。
        try:
            rai = _grade_report(run, res)
        except Exception:
            rai = rmd
        _cscore = str((res or {}).get("score") or "")
        _upids = {pg.get("upage") for pg in _run_pages(run)}   # 多纸:结果写到本 run 每张纸(哪页点检查都看得到)
        _cname = ""
        try:
            import pdf_reader as P
            with _lock:
                for it in P._upages_load(run["file"]):
                    if it.get("id") in _upids and (it.get("title") or "").strip():
                        _cname = it.get("title").strip(); break
        except Exception:
            pass
        # 源书页(provenance):这张纸参考的**印刷页**(run.page 是 PDF 页 → 减偏移)。供 read_check_report 说明"题目自制自第 X 页附近、按需读原文"。
        _srcp = None
        try:
            import pdf_reader as P
            _pdfp = int(run.get("page") or 0)
            if _pdfp:
                try:
                    _off = P._page_offset_for(run["file"])
                except Exception:
                    _off = 0
                _srcp = (_pdfp - _off) if (_pdfp - _off) >= 1 else _pdfp
        except Exception:
            _srcp = None
        # 登记报告(供 read_check_report 工具按名查)。返回**最终报告名**(可能加了序号去重)。
        #   lookups=造纸 CLI 当时的查找类查询(读了第几页/搜了什么),随纸的 params 一路带过来(provenance 跟工件走)。
        _lk = (run.get("params") or {}).get("lookups") or None
        try:
            import assistant as A
            _cname = A._save_check_report(run.get("uid"), _cname or "练习纸检查", run.get("file"), rai, _cscore,
                                          src_page=_srcp, lookups=_lk)
        except Exception:
            _cname = _cname or "练习纸检查"
        run.update(status="done", result=res, result_md=rmd, result_ai=rai,
                   check_name=_cname, check_score=_cscore, hint="检查完成 ✅")
        _save(run)
        # ★ 结果**存进纸的 sidecar**(不是格子块) → 刷新/回前台/换设备都能看到,不靠那一次性 SSE。
        #   (后台线程发的 SSE 若那刻页面不可见就被 visibility 早退丢了 —— 用户实测"结果没出现"的根因。)
        try:
            import pdf_reader as P
            with _lock:
                items = P._upages_load(run["file"])
                for it in items:
                    if it.get("id") in _upids:
                        it["result_md"] = rmd
                        it["result_ai"] = rai
                        it["check_name"] = _cname
                        it["check_score"] = _cscore
                        it["updated"] = int(time.time())
                P._upages_save(run["file"], items)
        except Exception:
            pass
        _push_run(run)                                   # 实时推(在线就立刻显示)
        try:                                             # 再补发 text 事件 → __upRerender 重画(更可靠)
            from reader_events import publish
            for pg in _run_pages(run):
                publish("text", run["file"], pg.get("upage"))
        except Exception:
            pass
    except Exception as ex:
        run = load(rid) or run
        run.update(status="error", error=str(ex)[:200])
        _save(run)
        _push_run(run)


# ══════════ 阶段 C:声明式配方解释器(流程即数据,不是代码)══════════
# ADR adr-page-orchestrator.md 栏杆①:流程是一份**声明式 flow**(白名单 step),由这个解释器
# 确定性执行 —— 编排 AI(阶段 D)产出的是这份数据,不是可执行代码。
#
# ⚠ 解释器必须能**挂起再恢复**(不是一次跑完):遇到 wait 就停在那儿存 pc,
#   下次 advance(事件到达)从 pc 继续。所有游标存 run["vm"] 里(durable),不靠内存。
#
# flow = [ step, step, ... ];step 是下面白名单之一:
#   {"page": [blocks]}              铺纸(模板变量 {{x}} / {{i}} / {{item}} 由 _tpl 展开)
#   {"say": "文本"}                 念(TTS 遥控)
#   {"wait": "事件名"}              挂起,等某个按钮事件(带 WAIT_TIMEOUT 超时)
#   {"wait_ms": 3000}              计划内停顿(定时器;当前实现为立即过,定时留阶段 D)
#   {"loop": {"over":"words", "do":[...]}}   对 params[over] 逐项循环,item/i 进作用域
#   {"check": {"answers":"words", "hint":"..."}}  按 blank 裁图 → 有界 LLM 点评(起后台线程)
#   {"write": [blocks]}             往页里追加块
#   {"reveal"/"hide": "块id"}
#   {"set_enabled": {"id":.., "on":bool}}
# 校验:validate_flow() 在**保存/运行前**跑,未知 step / 悬空引用 → 拒(栏杆①)。
_FLOW_STEPS = ("page", "say", "wait", "wait_ms", "loop", "check", "write", "reveal", "hide", "set_enabled", "call")


def validate_flow(flow):
    """返回 (ok, error)。宁可严:不认识的 step 一律拒(防编排 AI 编出跑不了的东西)。"""
    if not isinstance(flow, list) or not flow:
        return False, "flow 必须是非空数组"
    def _walk(steps, depth=0):
        if depth > 6:
            return "嵌套太深"
        for st in steps:
            if not isinstance(st, dict) or len(st) < 1:
                return "step 必须是对象"
            key = next(iter(st))
            if key not in _FLOW_STEPS:
                return f"未知 step「{key}」"
            if key == "loop":
                lp = st[key] or {}
                if not lp.get("over") or not isinstance(lp.get("do"), list):
                    return "loop 需要 over(字符串) + do(数组)"
                e = _walk(lp["do"], depth + 1)
                if e:
                    return e
        return ""
    e = _walk(flow)
    return (not e), e


def _tpl(v, scope):
    """展开模板变量:{{title}} {{i}} {{item}} → scope 里的值。"""
    if isinstance(v, str):
        for k, val in scope.items():
            v = v.replace("{{%s}}" % k, str(val))
        return v
    if isinstance(v, list):
        return [_tpl(x, scope) for x in v]
    if isinstance(v, dict):
        return {k: _tpl(x, scope) for k, x in v.items()}
    return v


def _recipe_tick(run, event):
    """声明式配方的执行器。用**扁平化的指令流 + pc**驱动(loop 在 vm 初始化时展开成线性序列),
    这样"挂起-恢复"只需要存一个整数 pc,不用重建循环栈 —— 最简单、最可靠。"""
    vm = run.setdefault("vm", {})
    if "prog" not in vm:                       # 首次:把 flow 展开成扁平指令流
        vm["prog"] = _flatten(run.get("flow") or [], run.get("params") or {})
        vm["pc"] = 0
    prog = vm["prog"]

    # 若正挂起等事件:先看这个事件是不是它等的
    if run.get("status") == "waiting":
        want = (run.get("wait") or {}).get("event")
        if want and event and event != want and event not in ("start",):
            return False                       # 不是等的事件 → 忽略(幂等)
        run["status"] = "running"              # 收到 → 继续往下走

    changed = False
    while vm["pc"] < len(prog):
        ins = prog[vm["pc"]]
        vm["pc"] += 1
        op = ins["op"]
        if op == "page":
            _set_blocks(run, ins["blocks"], kind=run.get("kind") or "free")
            changed = True
        elif op == "say":
            _say(run, ins["text"])
        elif op == "write":
            cur = _blocks_of(run)
            _set_blocks(run, list(cur) + ins["blocks"], kind=run.get("kind") or "free")
            changed = True
        elif op in ("reveal", "hide"):
            _reveal(run, ins["id"], op == "reveal")
            changed = True
        elif op == "wait":
            run["status"] = "waiting"
            run["wait"] = {"event": ins["event"], "since": int(time.time())}
            run["hint"] = ins.get("hint") or run.get("hint") or ""
            return True                        # ★ 挂起:存 pc,等下次事件
        elif op == "call":
            # ★★ 去壳:MCP 只是 TOOLS 的薄门面(mcp_server 全线 HTTP 转发到 /api/assistant/tool →
            #   TOOLS[name][1])。所以"进程内直接调 TOOLS[name][1]" = MCP 转发到的同一个函数,
            #   零拷贝、零标注、改一个工具所有配方自动跟着变(ADR §5.5.3)。
            try:
                import assistant as A
                fn = (A.TOOLS.get(ins["tool"]) or (None, None))[1]
                if fn:
                    ctx = {"file_rel": run["file"], "page": run.get("page"), "_uid": run.get("uid")}
                    res = fn(ins["args"] or {}, ctx) or {}
                    val = res
                    ex = ins.get("into")
                    # 简单 extract:结果里挑一个 list/str 字段(配方里可指定 res 的键,默认整体)
                    if isinstance(res, dict) and ins.get("extract"):
                        val = res.get(ins["extract"])
                    if ex:
                        run.setdefault("params", {})[ex] = val
                        # call 改了 params → 后续 loop 要重新展开(prog 里 loop 已展开的除外;call 应放在 loop 前)
            except Exception as _ex:
                sys.stderr.write("[recipe] call %s 失败: %s\n" % (ins.get("tool"), str(_ex)[:80]))
            continue
        elif op == "wait_ms":
            ms = int(ins.get("ms") or 0)
            if ms <= 0:
                continue
            # 计划内停顿(#27 定时几秒→下一步)。**非阻塞**:挂起存 pc + 一次性 Timer 到点自动 advance,
            #   不是常驻等待线程(跟 check 的 fire-and-forget 一致,不违反零阻塞铁律)。
            run["status"] = "waiting"
            run["wait"] = {"event": "__timer", "since": int(time.time())}
            run["hint"] = ins.get("hint") or run.get("hint") or ""
            _save(run); _push_run(run)
            _schedule(run["rid"], "__timer", ms / 1000.0)
            return True
        elif op == "check":
            if run.get("status") == "checking":
                return changed
            run["status"] = "checking"
            run["hint"] = "正在检查…"
            _save(run); _push_run(run)
            threading.Thread(target=_check_page, args=(run["rid"], ins.get("hint") or ""),
                             daemon=True).start()
            return False                       # 后台线程会自行落盘/收尾
        elif op == "set_enabled":
            _set_enabled(run, ins["id"], ins["on"])
            changed = True
    run.update(status="done", hint=run.get("hint") or "完成 ✅")
    return True


def _flatten(flow, params):
    """flow(可能含 loop)→ 扁平指令流。loop 在这里按 params 展开(item/i 进模板作用域)。"""
    out = []
    def _emit(steps, scope):
        for st in steps:
            key = next(iter(st))
            body = st[key]
            if key == "loop":
                over = params.get(body["over"]) or []
                for i, item in enumerate(over):
                    _emit(body["do"], dict(scope, i=i + 1, item=item, n=len(over)))
            elif key == "page":
                out.append({"op": "page", "blocks": _expand_blocks(body, scope, params)})
            elif key == "write":
                out.append({"op": "write", "blocks": _expand_blocks(body, scope, params)})
            elif key == "say":
                out.append({"op": "say", "text": _tpl(body, scope)})
            elif key == "wait":
                out.append({"op": "wait", "event": _tpl(body, scope) if isinstance(body, str) else body.get("event"),
                            "hint": _tpl(body.get("hint"), scope) if isinstance(body, dict) else ""})
            elif key == "wait_ms":
                out.append({"op": "wait_ms", "ms": int(body or 0)})
            elif key == "check":
                out.append({"op": "check", "hint": _tpl((body or {}).get("hint"), scope)})
            elif key == "call":
                out.append({"op": "call", "tool": body.get("tool"), "args": _tpl(body.get("args") or {}, scope),
                            "into": body.get("into")})   # ★ 去壳:进程内调 TOOLS[tool],结果 extract 后存进 params[into]
            elif key in ("reveal", "hide"):
                out.append({"op": key, "id": _tpl(body, scope)})
            elif key == "set_enabled":
                out.append({"op": "set_enabled", "id": body.get("id"), "on": bool(body.get("on"))})
    _emit(flow, {"title": params.get("title") or ""})
    return out


def _expand_blocks(blocks, scope, params):
    """块列表:普通块直接展开模板;带 {repeat:"key"} 的块 → 对 params[key] 逐项展开(i/item 进作用域)。
    这样"20 个填空"在铺纸时一次全放上(批改要裁到它们),不必写 20 遍。"""
    out = []
    for b in (blocks or []):
        rep = b.get("repeat") if isinstance(b, dict) else None
        if rep:
            items = params.get(rep) or []
            base = {k: v for k, v in b.items() if k != "repeat"}
            for i, item in enumerate(items):
                out.append(_tpl(base, dict(scope, i=i + 1, item=item, n=len(items))))
        else:
            out.append(_tpl(b, scope))
    return out


def _set_enabled(run, block_id, on):
    import pdf_reader as P
    rel = run["file"]
    with _lock:
        items = P._upages_load(rel)
        for it in items:
            if it.get("id") == run.get("upage"):
                for b in (it.get("blocks") or []):
                    if b.get("id") == block_id:
                        b["enabled"] = bool(on)
                it["updated"] = int(time.time())
                break
        P._upages_save(rel, items)
    try:
        from reader_events import publish
        publish("text", rel, run.get("upage"))
    except Exception:
        pass


_PROGRAMS = {"dictation": _dictation_tick, "free": _free_tick, "recipe": _recipe_tick}


# ── 对外三个入口 ──────────────────────────────────────────────────────────────
# ── 内置配方(阶段 C:dictation 用**纯声明式数据**重写,证明"流程即数据")──
#   阶段 E 用户保存的配方 = 同一种数据,存 state/recipes/<name>.json。
RECIPES = {
    "dictation": {
        "name": "听写",
        "paper": "dictation",
        "flow": [
            {"page": [
                {"id": "title", "kind": "text", "text": "{{title}}", "style": "h1"},
                {"repeat": "words", "id": "q{{i}}", "kind": "blank", "label": "{{i}}.", "answer": "{{item}}"},
                {"id": "b_next", "kind": "button", "label": "▶ 念下一个", "event": "next"},
                {"id": "b_done", "kind": "button", "label": "✓ 写完了", "event": "done"},
            ]},
            {"loop": {"over": "words", "do": [
                {"say": "{{item}}"},
                {"wait": {"event": "next", "hint": "第 {{i}} 个,写完按「▶ 念下一个」"}},
            ]}},
            {"check": {"hint": "逐题判断听写对错(手写体,错字/漏字/假名错都算错)。"}},
        ],
    },
}


RECIPES_DIR = Path("/home/bwicarus/claude/state/recipes")


def _load_recipe(name):
    """从 state/recipes/<name>.json 读用户保存的配方。"""
    if not name:
        return None
    try:
        import re as _re
        safe = _re.sub(r"[^\w\u4e00-\u9fff-]", "", str(name))[:60]
        f = RECIPES_DIR / (safe + ".json")
        return json.loads(f.read_text("utf-8")) if f.exists() else None
    except Exception:
        return None


def list_recipes():
    """列出所有已保存配方(供 run_saved_task 的目录 / AI 看有哪些工具)。"""
    out = []
    try:
        for f in RECIPES_DIR.glob("*.json"):
            try:
                d = json.loads(f.read_text("utf-8"))
                out.append({"name": d.get("name") or f.stem,
                            "desc": d.get("desc") or "",
                            "sources": list((d.get("sources") or {}).keys()),
                            "inputs": list((d.get("inputs") or {}).keys())})
            except Exception:
                pass
    except Exception:
        pass
    return out


def recent_run(uid):
    """该用户最近一个 free/recipe run(保存按钮不带 rid 时用)。"""
    best, bt = None, 0
    try:
        for f in RUNS_DIR.glob("r_*.json"):
            try:
                d = json.loads(f.read_text("utf-8"))
            except Exception:
                continue
            if str(d.get("uid")) != str(uid):
                continue
            if d.get("kind") not in ("free", "recipe"):
                continue
            if (d.get("updated_at") or 0) > bt:
                best, bt = d, d.get("updated_at") or 0
    except Exception:
        pass
    return best


def _flow_signature(flow, page_blocks):
    """结构指纹(忽略具体数据):同指纹 = 同一种纸/流程 → 保存时可合并成一个工具的不同数据源(ADR §5.5.5)。
    只取"骨架":flow 的 step 类型序列 + page 里块的 kind 序列。"""
    def _skel(steps):
        out = []
        for st in (steps or []):
            k = next(iter(st)) if isinstance(st, dict) and st else "?"
            if k == "loop":
                out.append("loop[" + _skel((st[k] or {}).get("do")) + "]")
            elif k == "page":
                out.append("page")
            else:
                out.append(k)
        return ",".join(out)
    kinds = ",".join(b.get("kind", "?") for b in (page_blocks or []) if not b.get("repeat")) \
            + "|rep:" + ",".join(b.get("kind", "?") for b in (page_blocks or []) if b.get("repeat"))
    import hashlib
    return hashlib.md5((_skel(flow) + "||" + kinds).encode()).hexdigest()[:16]


def save_recipe(run, name, desc="", source_label="", source_spec=None):
    """把一次 run 冻成配方文件。同结构指纹已存在 → **合并**(只加一个数据源选项),否则新建。
    返回 {ok, name, merged}。"""
    import re as _re
    safe = _re.sub(r"[^\w\u4e00-\u9fff-]", "", str(name or ""))[:60]
    if not safe:
        return {"ok": False, "error": "工具名不能为空"}
    flow = run.get("flow")
    # 从 flow 里抽 page 模板(第一个 page step 的块)
    page_blocks = []
    for st in (flow or []):
        if isinstance(st, dict) and "page" in st:
            page_blocks = st["page"]; break
    if not flow:   # free 纸没有 flow,用它的 blocks 当 page(合成一个极简 flow)
        page_blocks = (run.get("params") or {}).get("blocks") or []
        flow = [{"page": page_blocks}]
    sig = _flow_signature(flow, page_blocks)
    RECIPES_DIR.mkdir(parents=True, exist_ok=True)
    # 找同指纹的已存配方 → 合并
    for f in RECIPES_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text("utf-8"))
        except Exception:
            continue
        if d.get("sig") == sig and source_label and source_spec:
            d.setdefault("sources_menu", {})[source_label] = source_spec
            f.write_text(json.dumps(d, ensure_ascii=False), "utf-8")
            return {"ok": True, "name": d.get("name"), "merged": True,
                    "hint": "已合并进已有工具「%s」,新增数据源「%s」" % (d.get("name"), source_label)}
    # 新建
    rec = {"name": safe, "desc": desc or ("一键" + safe), "sig": sig, "paper": run.get("paper") or "note",
           "page": page_blocks, "flow": flow, "inputs": {}, "sources": {}}
    if source_label and source_spec:
        rec["sources_menu"] = {source_label: source_spec}
    (RECIPES_DIR / (safe + ".json")).write_text(json.dumps(rec, ensure_ascii=False), "utf-8")
    return {"ok": True, "name": safe, "merged": False, "hint": "已保存为工具「%s」" % safe}


def save_trace_recipe(name, desc, steps, uid, source_label="", source_spec=None):
    """把一次 **CLI 多步任务的执行轨迹** 冻成可复用工具(用户拍板:所有走 CLI 的多步任务都能保存)。
    steps = [{name, args}, ...](CLI 调过的工具序列)。回放 = 按序进程内调 TOOLS[name](去壳)。
    同"工具序列"已存在 → 合并加数据源;否则新建。"""
    import re as _re
    safe = _re.sub(r"[^\w\u4e00-\u9fff-]", "", str(name or ""))[:60]
    if not safe:
        return {"ok": False, "error": "工具名不能为空"}
    calls = [{"tool": st.get("name"), "args": st.get("args") or {}}
             for st in (steps or []) if st.get("name")]
    if not calls:
        return {"ok": False, "error": "这次任务没有可复用的工具调用"}
    import hashlib
    sig = "trace:" + hashlib.md5(",".join(c["tool"] for c in calls).encode()).hexdigest()[:14]
    RECIPES_DIR.mkdir(parents=True, exist_ok=True)
    for f in RECIPES_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text("utf-8"))
        except Exception:
            continue
        if d.get("sig") == sig and source_label and source_spec:
            d.setdefault("sources_menu", {})[source_label] = source_spec
            f.write_text(json.dumps(d, ensure_ascii=False), "utf-8")
            return {"ok": True, "name": d.get("name"), "merged": True,
                    "hint": "已合并进「%s」,新增数据源「%s」" % (d.get("name"), source_label)}
    rec = {"name": safe, "desc": desc or ("一键" + safe), "sig": sig, "kind": "trace",
           "calls": calls}
    if source_label and source_spec:
        rec["sources_menu"] = {source_label: source_spec}
    (RECIPES_DIR / (safe + ".json")).write_text(json.dumps(rec, ensure_ascii=False), "utf-8")
    return {"ok": True, "name": safe, "merged": False, "hint": "已保存为工具「%s」" % safe}


def run_trace(rec, ctx):
    """回放一个 trace 配方:按序**进程内**调 TOOLS[tool](args)(去壳,不走 MCP)。
    工具产生的 client_action(如建纸)收集起来返回给前端应用。返回 {ok, client_actions, last}。"""
    import assistant as A
    cas, last = [], None
    tctx = {"file_rel": (ctx or {}).get("file_rel"), "page": (ctx or {}).get("page"),
            "_uid": (ctx or {}).get("_uid")}
    for c in (rec.get("calls") or []):
        fn = (A.TOOLS.get(c.get("tool")) or (None, None))[1]
        if not fn:
            continue
        try:
            res = fn(c.get("args") or {}, tctx) or {}
            last = res
            if isinstance(res, dict) and res.get("client_action"):
                cas.append(res["client_action"])
        except Exception as ex:
            sys.stderr.write("[trace] %s 失败: %s\n" % (c.get("tool"), str(ex)[:80]))
    return {"ok": True, "client_actions": cas, "last": last}


def _run_sources(run, sources):
    """★ 去壳预取:进程内直接调 TOOLS[工具],结果填进 params[input名]。
    MCP 只是 TOOLS 的薄门面 → 进程内调 = MCP 转发到的同一个函数,零拷贝零标注(ADR §5.5.3)。"""
    if not sources:
        return
    import assistant as A
    ctx = {"file_rel": run["file"], "page": run.get("page"), "_uid": run.get("uid")}
    for into, spec in sources.items():
        if not isinstance(spec, dict) or not spec.get("call"):
            continue
        fn = (A.TOOLS.get(spec["call"]) or (None, None))[1]
        if not fn:
            sys.stderr.write("[recipe] 数据源工具不存在:%s\n" % spec.get("call"))
            continue
        try:
            res = fn(spec.get("args") or {}, ctx) or {}
            val = res.get(spec["extract"]) if (isinstance(res, dict) and spec.get("extract")) else res
            run.setdefault("params", {})[into] = val
        except Exception as ex:
            sys.stderr.write("[recipe] 数据源 %s 失败: %s\n" % (spec.get("call"), str(ex)[:80]))


def start(kind: str, params: dict, ctx: dict) -> dict:
    """起一个 run。返回 {ok, rid} 或 {ok:False, error}。**同步、瞬间返回**(不做任何等待)。"""
    if kind not in _PROGRAMS:
        return {"ok": False, "error": f"未知任务类型 {kind}"}
    upage = (params or {}).get("upage") or ""
    page = int((params or {}).get("page") or ctx.get("page") or 0)
    if not upage or not page:
        return {"ok": False, "error": "缺 upage/page(得先建一张插入页)"}
    _gc()
    run = {
        "rid": "r_" + uuid.uuid4().hex[:8],
        "kind": kind, "uid": str(ctx.get("_uid") or ctx.get("uid") or ""),
        "file": ctx.get("file_rel") or "", "page": page, "upage": upage,
        "status": "running", "step": 0, "state": {}, "params": params or {},
        "result": None, "hint": "", "created_at": int(time.time()),
    }
    try:   # 创造物库:纸出生即入册(ref 引用 sidecar 不复制;「记忆」开关=make_paper)
        import assistant as A
        if not A._creation_enabled(run["uid"], "make_paper"):
            raise RuntimeError("off")
        _t = (params or {}).get("title") or kind
        A._creation_add(run["uid"], "paper", "建了练习纸《%s》(第%s页)" % (_t, page),
                        ref={"upage": upage, "file": run["file"], "page": page},
                        anchor={"file": run["file"], "page": page})
    except Exception:
        pass
    if kind == "recipe":                       # 声明式配方:flow 从内置库或 params 拿,**校验后**才跑
        rec_name = (params or {}).get("recipe") or ""
        rdef = RECIPES.get(rec_name) or _load_recipe(rec_name) or {}
        flow = rdef.get("flow") or (params or {}).get("flow")
        ok, err = validate_flow(flow)
        if not ok:
            return {"ok": False, "error": "配方无效:" + err}
        # ★ sources(去壳预取):配方里 sources={input名:{call:工具, extract:字段, args}} →
        #   在跑 flow 前进程内直接调 TOOLS[工具],把结果填进 params[input名]。
        #   这就是"高亮听写/未掌握词听写"共用一个配方、只换数据源的实现(ADR §5.5)。
        _run_sources(run, rdef.get("sources") or (params or {}).get("sources") or {})
        run["flow"] = flow
        run["paper"] = rdef.get("paper") or (params or {}).get("paper") or "note"
    _PROGRAMS[kind](run, "")
    _save(run)
    _push_run(run)
    # n_pages / overflow:前端据此决定要不要再建溢出页(多纸自动补页,#33)。
    ov = run.get("_overflow") or {}
    return {"ok": True, "rid": run["rid"], "hint": run.get("hint") or "",
            "n_pages": int(run.get("n_pages") or 1),
            "overflow": len(ov.get("pages") or [])}


def advance(rid: str, event: str) -> dict:
    """事件驱动的唯一推进入口(按钮点击 / 定时器 / 回前台)。**幂等**。"""
    with _lock:
        run = load(rid)
        if not run:
            return {"ok": False, "error": "run 不存在(可能已过期)"}
        if run["status"] in ("done", "error", "cancelled"):
            return {"ok": True, "status": run["status"], "hint": run.get("hint") or ""}
        if event == "cancel":
            run.update(status="cancelled", hint="已取消")
            _save(run); _push_run(run)
            return {"ok": True, "status": "cancelled"}
        # 挂起超时 → 判死(防僵尸 run 永远占着这张纸)
        w = run.get("wait") or {}
        if run["status"] == "waiting" and w.get("since") and time.time() - w["since"] > WAIT_TIMEOUT:
            run.update(status="cancelled", hint="太久没动静,已取消")
            _save(run); _push_run(run)
            return {"ok": True, "status": "cancelled"}
        changed = _PROGRAMS[run["kind"]](run, event)
        if changed:
            _save(run)
            _push_run(run)
    return {"ok": True, "status": run["status"], "hint": run.get("hint") or "",
            "state": run.get("state") or {}}


def status(rid: str) -> dict:
    """前端回前台时用它**对齐状态机** —— SSE 在页面不可见时会丢事件,不能只靠推送。"""
    run = load(rid)
    if not run:
        return {"ok": False, "error": "run 不存在"}
    return {"ok": True, "rid": rid, "kind": run["kind"], "status": run["status"],
            "step": run["step"], "state": run.get("state") or {}, "hint": run.get("hint") or "",
            "upage": run.get("upage"), "result": run.get("result"), "error": run.get("error")}
