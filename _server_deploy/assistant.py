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

# claude CLI 的 cwd 决定它加载哪份 CLAUDE.md(从 cwd 向上遍历父目录找)+ 项目 .claude/settings、.mcp.json。
# 本助手只走我们自管的 JSON 工具协议、禁了所有内建工具,**完全不需要项目 memory/settings/MCP**,
# 却因 cwd=项目根 每轮白背整份 CLAUDE.md(~15K token)+ agent 壳。指到**项目树外**的空目录 → 都不加载 → 每轮省一大块。
# ⚠ 必须在 CLAUDE_DIR 之外:子目录会被父目录遍历命中 CLAUDE_DIR/CLAUDE.md。我们工具全用绝对路径,cwd 对功能零影响。
_ASST_CWD = "/tmp/bwicarus-asst-cwd"
try:
    os.makedirs(_ASST_CWD, exist_ok=True)
except Exception:
    _ASST_CWD = "/tmp"


# ──────────────── Gemini(给交互时的「深度解释/总结」+「现场看图」用,省 Claude 额度)────────────────
# 编排器仍是 Claude(工具循环不动);只有这两个**叶子调用**(单次、不需 agentic 循环)优先走 Gemini Flash,
# 失败/空/限额 → 自动回退 Claude(主助手永不因 Gemini 断而挂)。夜间批处理仍 Claude(按用户要求,不烧 Gemini)。
_GEMINI_KEY_FILE = Path("/home/bwicarus/.config/gemini-api-key")
_GEMINI_MODEL = "gemini-2.5-flash"

_gemini_off_until = 0.0   # 遇 429/失败设冷却,避免每次都白打失败请求(多几百ms延迟)再回退 Claude

def _gemini_key():
    if time.time() < _gemini_off_until:   # 冷却中(上次额度耗尽/失败)→ 直接跳过,走 Claude
        return None
    try:
        return _GEMINI_KEY_FILE.read_text().strip() or None
    except Exception:
        return None

def _gemini_cooldown(secs=300):
    global _gemini_off_until
    _gemini_off_until = time.time() + secs

def _gemini_log(label, status, model="", tokens=0, tin=0, tout=0):
    """记进额度日志:units=本次总 token。note 带 model + 输入/输出 token 拆分——算钱**按每次用的模型单价**
    (Flash/Pro 差好几倍、输入输出也差很多);Gemini 没有查余额的 API,真余额去 AI Studio billing 控制台看。"""
    try:
        sys.path.insert(0, str(CLAUDE_DIR / "scripts"))
        from google_api_quota import log_usage
        log_usage("gemini", int(tokens or 0), label,
                  note=f"model={model} in={int(tin)} out={int(tout)} status={status}")
    except Exception:
        pass

def _gemini_usage(j):
    """从 Gemini 响应 json 取 token 用量 {total, prompt, out}。"""
    u = (j or {}).get("usageMetadata") or {}
    return {"total": u.get("totalTokenCount", 0), "prompt": u.get("promptTokenCount", 0),
            "out": u.get("candidatesTokenCount", 0)}

def _gemini_text(prompt, max_tokens=4000, think=True, timeout=90, model=None):
    """Gemini 出文本(深度解释/总结)。model 可指定(默认 _GEMINI_MODEL);失败/空 → None(调用方回退 Claude)。"""
    key = _gemini_key()
    if not key:
        return None
    mdl = model or _GEMINI_MODEL
    try:
        import requests
        cfg = {"temperature": 0.4, "maxOutputTokens": max_tokens}
        if not think:
            cfg["thinkingConfig"] = {"thinkingBudget": 0}
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/{mdl}:generateContent?key={key}")
        r = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": cfg}, timeout=timeout)
        if r.status_code != 200:
            _gemini_log("assistant:text", r.status_code, mdl)
            if r.status_code in (429, 403):   # 额度耗尽/被拒 → 冷却,别每次白打
                _gemini_cooldown()
            return None
        j = r.json(); us = _gemini_usage(j)
        _gemini_log("assistant:text", 200, mdl, us["total"], us["prompt"], us["out"])
        cand = (j.get("candidates") or [{}])[0]
        out = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []))
        return out.strip() or None
    except Exception:
        return None

def _gemini_vision(prompt, images, max_tokens=1500, timeout=90, model=None):
    """Gemini 看图出文字描述。model 可指定(默认 _GEMINI_MODEL)。images=[{media_type,b64}]。失败/空 → None(回退 Claude)。"""
    key = _gemini_key()
    if not key or not images:
        return None
    mdl = model or _GEMINI_MODEL
    try:
        import requests
        parts = [{"text": prompt}]
        for v in images[:3]:
            parts.append({"inlineData": {"mimeType": v.get("media_type", "image/png"), "data": v["b64"]}})
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/{mdl}:generateContent?key={key}")
        r = requests.post(url, json={"contents": [{"parts": parts}],
                                     "generationConfig": {"temperature": 0.3, "maxOutputTokens": max_tokens,
                                                          "thinkingConfig": {"thinkingBudget": 0}}}, timeout=timeout)
        if r.status_code != 200:
            _gemini_log("assistant:vision", r.status_code, mdl)
            if r.status_code in (429, 403):
                _gemini_cooldown()
            return None
        j = r.json(); us = _gemini_usage(j)
        _gemini_log("assistant:vision", 200, mdl, us["total"], us["prompt"], us["out"])
        cand = (j.get("candidates") or [{}])[0]
        out = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []))
        return out.strip() or None
    except Exception:
        return None


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
        if meta:   # 记每轮所在位置(书/页/选中句/用过的图)+ 助手回答的调用轨迹 trace,让历史回看也能显示上下文卡片 / 感叹号步骤
            for k in ("page", "pages", "book", "file_rel", "selection", "figures", "trace"):
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
def _spawn(effort="low", model=None, system=None):
    try:
        cmd = [_APP_CLAUDE, "--print", "--input-format", "stream-json", "--output-format", "stream-json",
               "--include-partial-messages",   # 吐 text_delta → 最终回答可逐字流式
               # ── 省 token:本 agent 只走我们自管的 JSON 工具协议,完全不用 Claude Code 那套壳 ──
               # ① cwd=项目树外空目录(上面 _ASST_CWD)→ 不加载 CLAUDE.md;
               # ② --setting-sources ""    → 不加载 user/project 设置 + engineering 插件(登录不受影响);
               # ③ --tools TodoWrite + 全禁 → 只留 1 个工具 schema(原来 21 个内建工具 schema 白占上下文);
               #    沙盒仍在:--disallowedTools 全禁,模型一个内建工具都用不了(防 prompt injection 读 .env/改脚本)。
               "--setting-sources", "",
               "--tools", "TodoWrite",
               "--disallowedTools", "TodoWrite Bash Edit Write Read NotebookEdit WebFetch WebSearch Glob Grep Task"]
        if system:                              # ④ --system-prompt 替换默认 agent 提示(我们自带完整系统提示)
            cmd += ["--system-prompt", system, "--exclude-dynamic-system-prompt-sections"]
        cmd += ["--verbose", "--model", (model or _AGENT_MODEL), "--effort", effort]
        return subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, cwd=_ASST_CWD)
    except Exception:
        return None


def _deep_ask(prompt, model="opus", effort="high", timeout=150):
    """一次性深度生成(给"生成步"工具用:总结/深度解释等真正需要强模型的活)。
    **优先 Gemini Flash(省 Claude 额度),失败/空 → 回退 Claude opus·high**。返回文本或 None。"""
    g = _gemini_text(prompt, max_tokens=4000, think=True, timeout=min(timeout, 100))
    if g:
        return g
    p = _spawn(effort=effort, model=model)
    if not p:
        return None
    try:
        return _send(p, prompt, timeout=timeout)
    finally:
        _kill(p)


_VIS_SYS = ("你看到的是 PDF 页面/插图的渲染图。用简洁中文描述图里**文字层读不到的视觉内容**"
            "(图表/示意图/曲线/电路/几何/物理装置/数据表/公式排版/手写批注/版面结构):它画的是什么、"
            "关键要素/结构/数值、在表达什么。抓重点、别冗长、别复述能从文字层读到的普通段落。数学用 $...$。")

def _vision_describe(images, note="", timeout=90):
    """对一组渲染图做**一次精简视觉调用**返回纯文字描述。
    关键省 token:图只在这个**一次性进程**里看一眼 → 主编排循环全程**只走文字、永不背图**
    (否则图留在多轮对话里每轮重读;且这里用精简系统提示,不背 5102 编排壳)。images=[{media_type,b64}]。"""
    if not images:
        return None
    # 优先 Gemini Flash 看图(省 Claude 额度),失败/空 → 回退 Claude 精简视觉
    g = _gemini_vision(_VIS_SYS + "\n" + (note or "描述这些图里文字层读不到的内容。"), images, timeout=min(timeout, 80))
    if g:
        return g
    p = _spawn(effort="low", model="sonnet", system=_VIS_SYS)
    if not p:
        return None
    try:
        blocks = [{"type": "text", "text": (note or "描述这些图里文字层读不到的内容。")}]
        for v in images[:3]:
            blocks.append({"type": "image", "source": {"type": "base64",
                          "media_type": v.get("media_type", "image/png"), "data": v["b64"]}})
        return _send(p, blocks, timeout=timeout)
    finally:
        _kill(p)


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
        _warm_p = _spawn(system=_sys_static())   # 预热进程也带静态系统提示(替换默认壳,跟真请求一致)


def _warm_reap():
    global _warm_p, _warm_on
    with _warm_lock:
        _warm_on = False
        p, _warm_p = _warm_p, None
    _kill(p)


def _take_proc(effort="low", model=None):
    """取进程。sonnet·low(快查/导航)→ 用预热好的 low 进程(秒回);其余(深 effort 或升到 opus 等更强模型,
    如感叹号「更强重答」沿梯子升档)→ 现起对应 模型×effort 的进程(冷启动~几秒可接受,深答本来就慢)。
    预热池只维持 sonnet·low——给最常见的快路径。"""
    if effort != "low" or (model and model != _AGENT_MODEL):
        return _spawn(effort, model=model, system=_sys_static())
    global _warm_p
    with _warm_lock:
        p, _warm_p = _warm_p, None
    if p is None or p.poll() is not None:
        _kill(p)
        p = _spawn(system=_sys_static())
    return p


def _warm_respawn():
    global _warm_p
    with _warm_lock:
        if not _warm_on:
            return
        if _warm_p is not None and _warm_p.poll() is None:
            return
        _warm_p = _spawn(system=_sys_static())


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
# ── 页码对齐:阅读器 UI / 用户 / AI 都用**书上印刷页码**;PyMuPDF / 跳转用 **PDF 页索引**。
# ctx.page_offset(前端按本书的对齐设置传来)= PDF页 - 印刷页。两边在边界转换,内部数据(图/历史)仍存 PDF 页。
def _to_disp(ctx, pdf):   # PDF 页 → 书上印刷页(吐给 AI / 报给用户)
    try:
        return int(pdf) - int((ctx or {}).get("page_offset") or 0)
    except Exception:
        return pdf


def _to_pdf(ctx, disp):   # 书上印刷页 → PDF 页(读页 / 跳转用)
    try:
        return int(disp) + int((ctx or {}).get("page_offset") or 0)
    except Exception:
        return disp


def _figdescs_for(file_rel, printed_pages):
    """本书开了『插图描述』时,取这些**印刷页**的图 caption+desc(纯文本,非视觉)。{印刷页: [(cap,desc),…]}。
    这样跨页/图里的结构(如 V 字模型整张图把上下流各阶段都画在图上)用现成描述就能进上下文,不必读视觉。"""
    try:
        import pdf_reader as _pdfm
        if not (file_rel and _pdfm._book_fig_enabled(file_rel)):
            return {}
        ap = (VAULT_ROOT / file_rel).resolve(); ap.relative_to(VAULT_ROOT.resolve())
        data = _pdfm._fig_load_abs(ap)
        figs = data.get("figures") or []          # 用带描述的 figures(geom 可能缺页,描述都在这)
        want = set(printed_pages); out = {}
        for f in figs:
            pg = f.get("page")
            if pg in want and (f.get("desc") or f.get("caption")):
                out.setdefault(pg, []).append((f.get("caption") or "", f.get("desc") or ""))
        return out
    except Exception:
        return {}


def _read_one(file_rel, ctx, pg, figd, cap_txt=4800, cap_fig=600, label=None):
    """渲一页进上下文:正文(截 cap_txt) + 该页图描述(纯文本)。label 覆盖默认「第N页」标签。"""
    dp = _to_disp(ctx, pg)
    t = _page_text(file_rel, pg)
    figs = figd.get(dp, [])
    if not t and not figs:
        return None
    block = (label or f"【第{dp}页】") + "\n" + (t[:cap_txt] if t else "(本页无文字层)")
    for cap, desc in figs:
        block += f"\n[本页插图「{cap[:40]}」] {desc[:cap_fig]}"
    return block


def _t_read_page(args, ctx):
    # 双页模式下读全部可见页(ctx.pages,PDF 索引),不传 page 时默认所有可见页;
    # 传 page 时那是**印刷页码**(AI/用户语言)→ 转成 PDF 页读。
    file_rel = ctx.get("file_rel", "")
    if args.get("page"):
        pages = [_to_pdf(ctx, args["page"])]
        want_next = False                              # 显式指定某页 → 只给那页(AI 自己决定要不要再往下)
    else:
        pages = ctx.get("pages") or [ctx.get("page", 0)]
        want_next = True                               # 默认读当前页:顺带把下一页(文字+图描述)带上,省得漏跨页
    # 本页 + 下一页 的图描述一次取(纯文本,非视觉)
    printed = [_to_disp(ctx, p) for p in pages]
    nxt = (max(pages) + 1) if (want_next and pages) else None
    figd = _figdescs_for(file_rel, printed + ([_to_disp(ctx, nxt)] if nxt else []))
    parts = []
    for pg in pages:
        b = _read_one(file_rel, ctx, pg, figd)
        if b:
            parts.append(b)
    # 下一页:只给**短预览**(开头 1000 字 + 图描述)——多数问题在本页就答完,下页预览只是「够不够、要不要续读」的线索;
    # 不需要整页(那会让每次 read_page 都多背几千字、推高每题成本)。真要看全下页,AI 再 read_page(page=下页)。
    if nxt:
        nb = _read_one(file_rel, ctx, nxt, figd, cap_txt=1000,
                       label=f"【下一页·第{_to_disp(ctx, nxt)}页(开头预览,要看全文再 read_page 它)】")
        if nb:
            parts.append(nb)
    return ({"pages": printed, "text": "\n\n".join(parts)}
            if parts else {"error": "这些页没取到文字(可能纯图/未OCR)"})


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
                hits.append({"page": _to_disp(ctx, int(ps)),   # 报印刷页给 AI
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
        n = int(args.get("page"))   # AI/用户给的是**印刷页码**
    except (TypeError, ValueError):
        return {"error": "page 不是数字"}
    pdf_n = _to_pdf(ctx, n)         # 转成 PDF 页索引再跳(jumpWithBack 收 PDF 页)
    return {"ok": True, "note": f"已翻到第{n}页", "client_action": {"fn": "jumpWithBack", "args": [pdf_n]}}


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
    """看当前页(或指定页)的视觉内容(图表/示意图/公式排版/手写等文字层拿不到的)。
    ★优先复用**已存的离线图描述**(夜间管线生成、read_page 也注入过)——**不重复识别**;
    只有 ① 本页有手写笔迹(离线描述里没有) 或 ② 本书没生成离线描述 时,才现场渲图做一次视觉识别。"""
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
    # 本页有没有手写笔迹?(ctx 实时墨迹 或 服务端 sidecar)→ 有则必须现场看(离线描述里没这些)
    has_ink = bool(ctx.get("ink"))
    if not has_ink:
        try:
            import pdf_reader as _pdfm
            for pg in pages:
                if _pdfm._page_ink_strokes(file_rel, pg):
                    has_ink = True; break
        except Exception:
            pass
    # 没手写 → 先复用已存的离线图描述,有就直接给(省一次视觉调用,也不重复识别)
    if not has_ink:
        printed = [_to_disp(ctx, p) for p in pages]
        figd = _figdescs_for(file_rel, printed)
        if figd:
            parts = []
            for dp in printed:
                for cap, desc in figd.get(dp, []):
                    parts.append(f"[第{dp}页 插图「{cap[:40]}」] {desc}")
            if parts:
                return {"页面图像描述(已存离线描述,无需重新识别)": "\n".join(parts),
                        "note": "这是已生成好的图描述;若用户要的是图里更细节(具体数值/精确排版)而这里没有,请他说具体点。"}
    # 有手写 / 没离线描述 → 现场渲图 + 一次性视觉识别
    try:
        import base64
        import fitz
        import pdf_reader as pdf
        ap = (VAULT_ROOT / file_rel).resolve()
        ap.relative_to(VAULT_ROOT.resolve())
        doc = fitz.open(str(ap))
        vis, done, inked = [], [], []
        try:
            for pg in pages:
                if pg < 1 or pg > doc.page_count:
                    continue
                page = doc[pg - 1]
                longside = max(page.rect.width, page.rect.height) or 1.0
                scale = min(2.0, 1540.0 / longside) or 1.0   # 长边 ~1540px(Claude 视觉甜区),封顶 2x
                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                png = pix.tobytes("png"); eff = scale
                if len(png) > 3_000_000:   # 超大页(扫描大开本)降一档再渲 → 防喂回 stdin 过大 / Pi 8GB OOM
                    eff = scale * 0.6
                    pix = page.get_pixmap(matrix=fitz.Matrix(eff, eff), alpha=False)
                    png = pix.tobytes("png")
                strokes = pdf._page_ink_strokes(file_rel, pg)   # 本页手写批注 → 合成进图,让 AI 看到用户画/写了什么
                if strokes:
                    png = pdf._overlay_ink_on_page_png(png, strokes, eff)
                    inked.append(pg)
                vis.append({"media_type": "image/png", "b64": base64.b64encode(png).decode()})
                done.append(pg)
        finally:
            doc.close()
        if not vis:
            return {"error": "页码超出范围"}
        note = "描述这" + ("几" if len(vis) > 1 else "") + "页里文字层读不到的视觉内容(图表/示意图/公式排版/手写等)。"
        if inked:
            note += f" 这些图**已叠加用户的手写批注**(第 {('、'.join(str(_to_disp(ctx, p)) for p in inked))} 页有手写),重点描述他写/圈/画了什么、标在哪。"
        desc = _vision_describe(vis, note)   # 一次性看图 → 返回文字;主循环不背图
        return {"页面图像描述": desc or "(看图失败,可重试)", "rendered_pages": done, "inked_pages": inked}
    except Exception as e:
        return {"error": str(e)[:140]}


def _t_see_ink(args, ctx):
    """看用户**用笔标注的那块区域的合成图**(裁笔迹附近 + 叠上手写笔迹)。
    用户用笔圈/划/打勾/画箭头标了东西、问『这是什么/我圈的/什么意思/这里』,或没说具体但页面有笔迹时用。返回 _vision 喂回大脑。"""
    file_rel = ctx.get("file_rel") or ""
    strokes = ctx.get("ink") or []
    page = int(ctx.get("page") or 0)
    if not file_rel or not page:
        return {"error": "不在 PDF 书里 / 不知道哪页"}
    if not strokes:
        return {"error": "本页没有手写笔迹(用户没用笔标注,或还没画)"}
    try:
        import base64
        import pdf_reader as pdf
        png = pdf._ink_focus_image(file_rel, page, strokes)
        if not png:
            return {"error": "裁不出笔迹区域"}
        marked = ""
        try:
            marked = pdf._text_under_ink(file_rel, page, strokes=strokes)
        except Exception:
            marked = ""
        note = ("下图=用户用笔标注的区域(已叠加他的手写笔迹)。结合笔迹的位置/形状/指向 + 图里文字,描述他到底圈/划/指/写了什么。")
        if marked:
            note += f" 几何上他大概标的是:「{_clean_tag(marked)[:120]}」(仅参考,以图为准)。"
        desc = _vision_describe([{"media_type": "image/png", "b64": base64.b64encode(png).decode()}], note)
        return {"笔迹标注描述": desc or "(看图失败,可重试)"}
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
        note = "下面是用户带入的图的裁剪渲染图,描述图里的内容。"
        if ink_any:
            note += "（含用户手写笔迹的合成图,重点描述他圈点/标注了什么）"
        return {"图像描述": _vision_describe(vis, note) or "(看图失败,可重试)"}
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


def _t_recall_notes(args, ctx):
    """**本地召回**用户已学/已记的相关笔记:先查『知识索引』(index/*.md,带关键词+摘要),命中少时
    grep vault markdown 笔记全文兜底。纯本地查(读文件 / grep),**不调 AI**,几百毫秒。
    把用户已有的知识拉进来辅助回答(像 references/skill 那样按需召回),让助手结合他的知识体系而非只看当前页。"""
    q = (args.get("query") or "").strip()
    if not q:                                   # 没给主题 → 用选中/焦点兜底
        q = (ctx.get("selection") or "").strip()
        fs = ctx.get("focus_sel") or {}
        if not q and isinstance(fs, dict):
            q = (fs.get("text") or "").strip()
    if not q:
        return {"error": "没给要召回的主题(query)"}
    import re
    q = q[:80]
    # 匹配信号:① 整词/虚词拆的子词(实词,权重 2);② CJK 二元组 bigram(模糊,权重 1)——
    # 编排器常把『向量空间的定义』传成『向量空间定义』,整串子串匹配不到索引(索引是『向量空间』+『定义』分开),
    # bigram(向量/量空/空间/间定/定义)能跟真词条重叠命中多个 → 真相关条目自然排前。
    words, grams = [], set()
    for t in re.split(r"[\s,，、；;。.()（）/\\]+", q):
        if len(t) < 2:
            continue
        for part in [t] + re.split(r"[的是了与和及在之地得着把被让对从向到]", t):
            if len(part) >= 2 and part not in words:
                words.append(part)
        for run in re.findall(r"[一-鿿぀-ヿ]{2,}", t):   # CJK 连续段拆 2-gram
            for i in range(len(run) - 1):
                grams.add(run[i:i + 2])
    words = words[:12]

    def _score(blob):
        b = blob or ""
        bl = b.lower()
        s = sum(2 for w in words if (w.lower() in bl))          # 实词命中 *2
        s += sum(1 for g in grams if g in b)                    # bigram 命中 *1
        return s if (s >= 2 or any(len(w) >= 3 and w.lower() in bl for w in words)) else 0   # 阈值:单 bigram 噪声不算
    hits = []
    seen = set()
    # 1) 知识索引(带摘要,最有价值)
    try:
        for f in (CLAUDE_DIR / "index").glob("*.md"):
            if f.name == "knowledge-index.md":
                continue
            subj = f.stem
            for line in f.read_text("utf-8", errors="ignore").splitlines():
                m = re.match(r"^\s*-\s*\[\[([^\]]+)\]\]\s*`([^`]*)`\s*[—–-]\s*(.*)$", line)
                if not m:
                    continue
                name, kw, summ = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
                blob = name + " " + kw + " " + summ
                score = _score(blob)
                if score and name not in seen:
                    seen.add(name)
                    hits.append({"_s": score + 1, "note": name, "subject": subj,
                                 "keywords": kw[:120], "summary": summ[:220], "src": "知识索引"})
    except Exception:
        pass
    # (不做 raw vault 全文 grep:短/拉丁词子串会匹配到 base64 附件、represent⊃present 等噪声,
    #  把没学过的笔记误当"学过"——违背"只算真学过的"。只用下面三个 curated/用户亲手做的高精度来源。)
    # 3) 知识图谱节点 —— **只召回真学过的**(mastered / 有 containing_notes 笔记 / mastery>0),
    #    没学过的节点(book 结构自动生成、大量 locked)绝不算"已学",免得 AI 误以为用户掌握了。
    try:
        ql = q.lower()
        for f in (CLAUDE_DIR / "knowledge_graph").glob("*.json"):
            if any(x in f.name for x in (".bak", ".pre", ".scan")) or f.name == "kg_audit.json":
                continue
            try:
                kg = json.loads(f.read_text("utf-8", errors="ignore"))
            except Exception:
                continue
            book = kg.get("title") or kg.get("book") or f.stem
            for nd in (kg.get("nodes") or []):
                nm = (nd.get("name") or "").strip()
                if not nm:
                    continue
                learned = bool(nd.get("mastered")) or bool(nd.get("containing_notes")) or bool(nd.get("mastery"))
                if not learned:
                    continue
                score = _score(nm)
                if score:
                    cn = nd.get("containing_notes") or []
                    why = ("已掌握" if nd.get("mastered") else "") + (f"·记过{len(cn)}篇笔记" if cn else "")
                    hits.append({"_s": score + 1, "note": nm, "subject": f"{book}·图谱", "keywords": "",
                                 "summary": f"你学过的知识点({why or '有进度'}),第{nd.get('pages') or '?'}页", "src": "图谱(已学)"})
    except Exception:
        pass
    # 4) Anki 卡(你亲手做的=真学过)。grep records 目录拿命中文件,再抽匹配的卡正面
    try:
        import subprocess
        # 宽召回:grep -E 用 实词+bigram 的 OR 模式(复合词卡里是拆开的,单整词 grep 找不到)→ 再用 _score 精筛
        pat = "|".join(re.escape(x) for x in (words + sorted(grams)) if x) or re.escape(q)
        adir = CLAUDE_DIR / "anki" / "records"
        r = subprocess.run(["grep", "-rIlE", pat[:400], str(adir)], capture_output=True, text=True, timeout=5)
        for path in (r.stdout or "").splitlines()[:12]:
            try:
                rec = json.loads(Path(path).read_text("utf-8", errors="ignore"))
            except Exception:
                continue
            src_note = rec.get("source_note") or Path(path).stem
            for card in (rec.get("cards") or []):
                txt = (card.get("front") or "") + " " + (card.get("back") or "") + " " + " ".join(card.get("tags") or [])
                score = _score(txt)
                if score:
                    front = re.sub(r"\s+", " ", (card.get("front") or "")).strip()
                    hits.append({"_s": score, "note": Path(src_note).stem, "subject": card.get("deck") or "", "keywords": "",
                                 "summary": "Anki 卡:" + front[:110], "src": "Anki卡"})
                    break   # 每个 note 出一张代表卡就够,别刷屏
    except Exception:
        pass
    hits.sort(key=lambda h: -h["_s"])
    top = hits[:8]
    for h in top:
        h.pop("_s", None)
    if not top:
        return {"results": [], "note": f"你的笔记/图谱/Anki 里**没找到**跟『{q}』明显相关的(这块可能还没学过/记过)。就按通用知识回答即可,**别假装他学过、别硬扯**。"}
    return {"results": top, "note": "下面是用户**自己学过/记过**的相关条目(src=来源:知识索引带摘要 / 笔记全文 / 图谱(已学=他真掌握或记过笔记的节点) / Anki卡=他亲手做的卡)。"
            "结合这些他**已学过**的内容回答、帮他串起来(『你在《X》笔记记过…』『这跟你做过的那张卡…』);笔记名用 [[名]] 提及。"
            "**只有这里列出的才算他学过**,没列的别假设他会;别照搬,按理解讲。"}


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
        page = (_to_pdf(ctx, args["page"]) if args.get("page")           # args.page 是印刷页→转 PDF 找章节
                else int((ctx.get("pages") or [ctx.get("page")])[0] or 1))   # ctx 已是 PDF 页
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
                    parts.append(f"【第{_to_disp(ctx, pg)}页】{t}")
                    total += len(t)
                if total > 9000:
                    end = pg
                    break
        finally:
            doc.close()
        if not parts:
            return {"error": "这一节没取到文字(可能纯扫描未OCR,可改用 see_page 逐页看)"}
        section_text = "\n\n".join(parts)[:9000]
        # 生成步:章节总结是真正需要强模型的活 → 现起强模型出高质量总结(编排器保持快模型)。
        # 模型/深度:用户给「summarize」动作设了预设(⚙)就用它,否则默认 opus·high
        _ap = _ap_get(ctx.get("_uid"), "summarize")
        _dm, _de = _ap if _ap else ("opus", "high")
        gen = _deep_ask(
            f"下面是《{ctx.get('book_name', '')}》「{title}」(第{_to_disp(ctx, start)}-{_to_disp(ctx, end)}页)的正文。"
            "请用中文给出**结构化总结**:① 核心要点(分条)② 关键定义 ③ 重要公式(用 $...$)④ 易错点。"
            "引用具体内容时句末标来源页「(第N页)」。简洁但完整,别遗漏主线。\n\n正文:\n" + section_text,
            model=_dm, effort=_de)
        if gen and gen.strip():
            return {"section_title": title, "page_range": [_to_disp(ctx, start), _to_disp(ctx, end)], "summary": gen.strip(),
                    "_gen_model": f"{_dm}·{_de}", "_gen_action": "summarize",
                    "note": "这是深度总结好的章节内容,**直接原样转达给用户**(可微调排版,别再大改/精简),保留来源页标注。给最终回答时按规则附追问。"}
        # opus 失败 → 退回把原文交给编排器自己总结
        return {"section_title": title, "page_range": [_to_disp(ctx, start), _to_disp(ctx, end)], "truncated": total > 9000, "text": section_text,
                "note": "(深度总结暂不可用)这是该章节正文,请你总结成:核心要点/关键定义/重要公式/易错点,标来源页。"}
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
    "recall_notes": ("**召回用户自己学过/记过的**相关内容:知识索引(带摘要)+ vault 笔记全文 + 知识图谱**已学**节点 + Anki 卡(本地查不耗时)。"
                     "想把当前内容跟『他已学过/记过的』串起来、用户问『我之前记过吗/我笔记里有没有X/跟我学的Y有关吗』、或要结合他知识体系深入讲时用。"
                     "**注意:只有召回到的才算他学过**(图谱里没学的节点不会返回);没召回到就别假设他会。args {query:主题词}(不传用选中/焦点)", _t_recall_notes),
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
                 "**本页有手写批注时会自动把『页面+手写笔迹』合成进图**(读不到手写就靠它)。"
                 "read_page 只有文字层、看不见图形/手写;用户问『这张图/这个图表/这页的图/我写的/我圈的/看一下』时用 see_page。args {page?}", _t_see_page),
    "see_figure": ("看用户**当前聚焦的那张图**的裁剪渲染图(他点选/拖进来的图;有手写笔迹则看合成图)。"
                   "已给的图说明不够、要核对图里的具体细节/用户在图上的标注时用。args {}", _t_see_figure),
    "see_ink": ("看用户**用笔标注的那块区域合成图**(裁笔迹附近 + 叠手写笔迹)。比 see_page 聚焦、只看标注那块、更省更快。"
                "用户用笔圈/划/打勾/画箭头标了东西后问『这是什么/我圈的/这里/什么意思』,或没说具体指什么但本页有笔迹时用。args {}", _t_see_ink),
    "undo_last": ("撤销最近一次写操作(删掉刚建的卡/笔记/高亮)。用户说『撤销/取消刚才那个』时用。args {}", _t_undo_last),
}


def _tool_label(name, args):
    return {"read_page": "读取页面", "read_selection": "读取选中", "search_book": "搜索全书",
            "search_all_books": "跨书搜索", "open_book": "打开书", "summarize_section": "总结本章",
            "translate": "翻译", "goto_page": "翻页", "make_anki": "制卡", "make_note": "整理笔记",
            "add_vocab": "加生词本", "highlight": "高亮", "page_vocab": "查掌握度",
            "lookup_word": "查词典", "see_page": "看页面图", "see_figure": "看这张图", "see_ink": "看笔迹标注",
            "recall_notes": "召回我的笔记", "undo_last": "撤销"}.get(name, name)


# ──────────────────────── agent 循环 ────────────────────────
def _sys_prompt(ctx):
    cat = "\n".join(f"- {n}: {d}" for n, (d, _) in TOOLS.items())
    _off = int(ctx.get("page_offset") or 0)   # PDF页 - 印刷页;给 AI 看的页码一律转成书上印刷页(跟用户一致)
    vis = ctx.get("pages") or ([ctx.get("page")] if ctx.get("page") else [])
    meta = {"book": ctx.get("book_name"), "当前可见页": [int(p) - _off for p in vis if p],
            "共": (int(ctx["total"]) - _off) if ctx.get("total") else ctx.get("total")}
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
        # 选中处理规则(只在有选中时才进上下文,不常驻基础提示):
        sel_line += ("\n★有选中=默认他在问这段选中内容,优先针对**选中**回答/查词/翻译/解释/语法/制卡。"
                     "查词/读音**先 lookup_word** 拿权威读音+释义再挑义项(日语同字多音、严禁自己编读音)。"
                     "上下文**优先用上面的『选中所在句』**——有它就别再 read_page,只有所在句不足以定义项时才 read_page。"
                     "**选中只是文字、上下文没涉及图就别 see_page**;只有指代某图(『图1-3/如下图』)或用户明说『看这张图』才 see_page。")
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
    # 本页是否有手写批注:① 先尝试**提取圈/划下的文字**直接当焦点(用户用笔圈词=就问这个词,最强信号);
    #                      ② 拿不到文字(纯涂画/箭头)再提示用 see_page 看合成图。
    if ctx.get("file_rel"):
        circled = ""; inked_pages = []
        try:
            import pdf_reader as _pdfm
            # 前端把当前页内存墨迹放在 ctx["ink"](实时,不靠服务端保存时机)→ 优先用它算圈下文字
            fe_ink = ctx.get("ink") or []
            cur_p = int(ctx.get("page") or (vis[0] if vis else 0) or 0)
            if fe_ink and cur_p:
                inked_pages.append(cur_p)
                circled = _clean_tag(_pdfm._text_under_ink(ctx["file_rel"], cur_p, strokes=fe_ink))
            # 其余可见页 / 没拿到时回退服务端 sidecar
            for p in vis:
                p = int(p) if p else 0
                if not p or p == cur_p:
                    continue
                if _pdfm._page_ink_strokes(ctx["file_rel"], p):
                    inked_pages.append(p)
                    if not circled:
                        circled = _clean_tag(_pdfm._text_under_ink(ctx["file_rel"], p))
        except Exception:
            pass
        has_fe_ink = bool(ctx.get("ink"))
        if inked_pages or has_fe_ink:
            tip = f"★本页有用户的**手写笔迹/标注**"
            if circled:
                tip += f"(几何上大概标在「{circled[:120]}」附近,仅参考)"
            tip += ("。**你来判断**这条问的跟他的标注有没有关:\n"
                    "  · 跟标注有关(『这是什么/我圈的/这里/什么意思/解释下』等指代不清、或明显在问他标的东西),"
                    "**或** 他没说具体指什么但本页有笔迹 → **先调 see_ink**(看『笔迹区域合成图』,据笔迹位置/形状/指向判断他标了啥再答,任意涂画/箭头/勾都行);\n"
                    "  · 问的**明显跟标注无关**(如『下一页讲什么』『总结整章』『翻译某段』『查某词』)→ **别看图**,直接按常规答(更快省额度)。")
            learn_bits.append(tip)
    # see_page 收紧规则:只在本书开了插图描述(read_page 才会给『本页插图…』文字描述,有得可用)时才进上下文;
    # 纯文字书没图描述,这条是噪声 → 不给,prompt 更纯净、选工具更准。
    try:
        import pdf_reader as _pdfm2
        if ctx.get("file_rel") and _pdfm2._book_fig_enabled(ctx["file_rel"]):
            learn_bits.append("★本页插图内容多已在 read_page 的『本页插图…』描述里——**先用那段文字**;别因『页面有图』就 see_page,"
                              "只有描述不够、要核对图里具体细节/排版/手写时,才对**当前页** see_page。")
    except Exception:
        pass
    learn_line = ("\n" + "\n".join(learn_bits)) if learn_bits else ""
    return (
        "你是网页 PDF 阅读器的侧边栏助手,像 Copilot 一样陪用户读书。用简洁中文口语聊天。\n"
        "你能调用下面的工具来读页面内容、搜索、翻译、制卡、整理笔记、跳页等,可以连续调用多个工具来完成复合请求"
        "(例如『总结这页再做成卡』= 先 read_page,再据此回答,再 make_anki)。\n"
        "★【写操作守卫·最高优先级】make_anki(制卡)/ make_note(整理笔记)/ add_vocab(加生词)是**有副作用**的写操作,"
        "**只有用户在这条消息里明确要求**才能调:出现『做成卡/制卡/做张卡/加到 Anki』才 make_anki;『整理成笔记/记成笔记/存成笔记』才 make_note;『加生词/加到生词本/收藏这个词』才 add_vocab。"
        "用户只说『总结/讲解/读一下/这页讲了啥/这页知识点/翻译/解释』——这些都只是要**文字回答**,"
        "**绝不许**顺手 make_anki / make_note / add_vocab。拿不准用户到底要不要卡时:先给文字总结,然后在回答里问一句『要我做成 Anki 卡吗?』,**别擅自制卡**。\n"
        "调用工具时:**整条消息只输出一行 JSON**,格式 {\"tool\":\"工具名\",\"args\":{...}},别加任何别的字。\n"
        "我执行后会把【工具结果】返回给你,你再决定继续调工具还是回答。\n"
        "能回答用户时:直接输出给用户看的中文回答(纯文本,不要 JSON、不要工具)。回答简洁自然,别太长。\n"
        "★数学一律用 LaTeX 写进 $...$(行内)或 $$...$$(独立成行):如 $x^2$、$\\frac{a}{b}$、$P(A_1)P(A_2)$、$\\lambda$。"
        "**严禁**用反引号包数学(`x^2` 会被当代码块,公式不渲染)、**严禁**用纯文本或 Unicode 上下标(要写 $x^2$ 不是 x²、$a_i$ 不是 aᵢ);"
        "凡变量、希腊字母、下标、分式、根号、求和/积分一律进 $...$,否则前端显示成乱码而非公式。\n"
        "需要页面内容/选中文字时务必先用 read_page / read_selection 拿,别凭空编。\n"
        "★【上下文范围·跨页】read_page(不带 page,读当前页)返回的是:**本页正文 + 本页及下一页的插图描述(若本书开了插图描述,纯文本) + 下一页正文预览**(已标「下一页」)。"
        "默认就据这些回答——书的内容常跨页(主题/列表/步骤/某模型各阶段续到下页),所以下一页已先给你了。"
        "**问题模糊、范围较大、本页+下页仍不足以答全**时,继续 read_page(page=再下一页码)往下读,**最多读到本小节结束**(到下一个标题、约再 +1~2 页为止),别无限翻;够答即止。"
        "**非当前页只有文字+图描述进上下文**——要看**实际图像**(图表细节/排版/手写)只能看**当前页**(see_page/see_figure),别对下一页/更远页用视觉工具。\n"
        "★复合请求(含多个动作,如『总结再做成卡』『翻译并制卡』『找到X页并跳过去』)必须把每个动作都执行完——"
        "逐个调工具,做完一个再做下一个,全部完成后才给最终回答,**别只做第一步就停**。\n"
        "★用户说『跳过去/打开/翻到』且目标明确(或搜索只有一个最相关命中)时,直接调 goto_page,别反问;"
        "只有真有多个差不多的选项才反问。\n"
        "例:用户**明确说**「总结这页**并做成卡**」(带了『做成卡』三个字)。正确顺序:\n"
        "  第1步 → {\"tool\":\"read_page\",\"args\":{}}\n"
        "  第2步(拿到正文后)→ {\"tool\":\"make_anki\",\"args\":{\"text\":\"<你总结出的要点>\"}}\n"
        "  第3步(制卡已提交后)→ 才给最终回答:「总结好了:…;卡也在做了,完成会通知你」。\n"
        "  ——这里第2步不能省,**是因为用户带了『做成卡』**;若用户只说「总结这页」(没提卡),就**只做第1步 read_page + 给文字总结**,到此为止,绝不 make_anki。\n"
        "★高亮重点:先 read_page 拿到正文,再把要强调的几句**原句逐字**(从正文照抄,不要改写/翻译)"
        "**一次性**放进 highlight 的 texts 数组(一次调用搞定,别一句一调),否则在 PDF 上定位不到。\n"
        "★【最近对话】里每条用户消息都带括号标注了当时所在的书/页/选中句。用户说『刚才那页/上一页/回到那页/前面说的那段』时,"
        "**从最近对话的标注里取出确切页码**,直接 goto_page(或 read_page 指定该页),别反问『哪一页』。\n"
        "★凡涉及『我(没)掌握哪些词/这页生词/某词我会不会』——**必须调 page_vocab 查掌握度数据库**,"
        "**严禁**拿正文里的词自己猜谁掌握没掌握(数据库才是准的:已掌握的词不算生词、从没查过的词系统不视为生词)。"
        "不传 words 拿本页未掌握生词;问具体某些词会不会就传 words:[...]。\n"
        "★查词/读音(尤其日语读音、英语音标释义)**一律先用 lookup_word**——它走 ECDICT/unidic 离线权威词典,"
        "**读音和释义以它为准,严禁自己编读音**(LLM 读音不可靠);你只负责结合上下文挑最贴切的义项、讲解。"
        "(用户当前有选中文字时,如何针对选中处理的细则会随『用户当前选中』一并给出;本书开了插图描述时,see_page 收紧规则也会一并给出。)\n"
        "★『总结这一章/这一节/这部分』用 summarize_section(它按书签切出整章正文);只『总结这页』才用 read_page。\n"
        "★『我哪本书讲过X/别的书有没有X/之前在哪见过』用 search_all_books;要跳到搜到的别的书用 open_book(file_rel,page)。\n"
        "★想把当前内容跟用户**已学过/已记过的笔记**串起来(用户问『我之前记过吗/我笔记里有没有X/跟我学的Y有关』,"
        "或你要结合他的知识体系深入讲)→ 用 recall_notes(query=主题)召回他自己的笔记(本地查不耗时);"
        "召回到就点出『你在《X》笔记里记过…』帮他连点成线,没召回到就按通用知识答、别硬扯。\n"
        "★页码口径:所有页码(你看到的当前页、工具返回的页、你说的页、goto_page 传的页)**一律是书上印刷页码**(跟用户看到的、跟书页角标一致),系统已自动跟 PDF 索引对齐,你**只管用印刷页码**别自己换算。\n"
        "★可溯源:凡复述/引用书里的具体内容,在句末标来源页「(第N页)」,N 必须来自工具实际返回的页码(read_page/search_book/summarize_section 都带页码),**不许编页码**。前端会把『第N页』变成可点跳转。\n"
        "★【追问建议】每次给最终回答时,在正文最后**另起一行**写 2-3 个贴合当前内容、能推进理解的下一步问题,"
        "格式就一行:[[FOLLOWUP]]问题1|问题2|问题3(用 | 分隔,放在整条回答末尾,前端会渲成可点按钮;问题要短、具体)。"
        "**每条最终回答都要带**;只有在调工具(输出 JSON)那几条里不要带。\n\n"
        f"【可用工具】\n{cat}\n\n"
        f"【当前页面】{json.dumps(meta, ensure_ascii=False)}{sel_line}{fig_line}{learn_line}"
    )


# 把 _sys_prompt 拆成 (静态规则+工具目录, 动态【当前页面】块):静态恒定 → 走 --system-prompt 替换 Claude Code
# 默认提示(省每轮那 ~6.8K 默认壳);动态随 ctx → 留 user message。按唯一锚 "【当前页面】" 切,不挪文本、零风险。
_SYS_STATIC_CACHE = None
def _sys_static():
    """静态系统提示(规则+工具目录),恒定 → 缓存。给 --system-prompt(替换默认),预热池无 ctx 也能取。"""
    global _SYS_STATIC_CACHE
    if _SYS_STATIC_CACHE is None:
        full = _sys_prompt({})                       # 空 ctx:静态前缀跟任何真 ctx 一致,动态部分丢弃
        i = full.rfind("【当前页面】")
        _SYS_STATIC_CACHE = (full[:i].rstrip() if i >= 0 else full)
    return _SYS_STATIC_CACHE

def _ctx_block(ctx):
    """动态部分(【当前页面】+ 选中/图/知识点/笔迹),每轮随 ctx 变 → 拼进 user message。"""
    full = _sys_prompt(ctx)
    i = full.rfind("【当前页面】")
    return full[i:] if i >= 0 else ""


def _clean_tag(s):
    """规整用户内容(选中句/书名)再拼进 prompt:折叠所有空白(含换行)成单空格 +
    去掉 【】「」(它们是 _agent_run 切分 turn/【最近对话】分段的标签,裸拼会破坏结构甚至被注入伪造段)。"""
    s = " ".join(str(s or "").split())
    return s.translate({ord(c): None for c in "【】「」"})


def _loc_tag(h, offset=0):
    """把某轮对话发生时的位置(书/页/选中句)拼成一小段标注,供助手定位『刚才那页』。
    历史存的 page 是 PDF 索引 → 报给 AI 时减 offset 转成书上印刷页(跟 AI 整体口径一致)。"""
    bits = []
    book = _clean_tag(h.get("book"))
    pages = h.get("pages") or ([h.get("page")] if h.get("page") else [])
    pages = [int(p) - offset for p in pages if p]
    if book:
        bits.append(book)
    if pages:
        bits.append("第" + "/".join(str(p) for p in pages) + "页")
    sel = _clean_tag(h.get("selection"))
    if sel:
        bits.append("选中「" + sel[:40] + "」")
    return ("(" + "，".join(bits) + ")") if bits else ""


def _format_history(history, offset=0):
    out = []
    for h in (history or [])[-6:]:
        if h.get("role") == "user":
            role, tag = "用户", _loc_tag(h, offset)   # 用户那轮标上当时所在页(印刷)/书/选中句
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


# 每用户「按动作」的模型/深度预设(感叹号弹窗的 ⚙ 设置 + 🐢 太慢/🎯 更强 写它)。
# action ∈ {orchestrator(回答/解释), summarize(章节总结)};值 = {model, effort}。无 → 用默认。
_AP_PATH = CLAUDE_DIR / "state" / "assistant-action-prefs.json"
_ap_lock = threading.Lock()
_AP_MODELS = ("haiku", "sonnet", "opus")
_AP_ACTIONS = ("orchestrator", "summarize")


def _ap_all(uid):
    try:
        return (json.loads(_AP_PATH.read_text("utf-8")) or {}).get(str(uid), {}) or {}
    except Exception:
        return {}


def _ap_get(uid, action):
    """该用户给某动作设的预设 → (model, effort) 或 None(用默认)。"""
    d = _ap_all(uid).get(action)
    if isinstance(d, dict) and d.get("model") in _AP_MODELS and d.get("effort") in _EFFORTS:
        return d["model"], d["effort"]
    return None


def _ap_set(uid, action, model, effort):
    """设/清某动作预设(model/effort 非法 → 清除回默认)。返回保存后的 {model,effort} 或 None。"""
    with _ap_lock:
        try:
            full = json.loads(_AP_PATH.read_text("utf-8")) if _AP_PATH.exists() else {}
        except Exception:
            full = {}
        if not isinstance(full, dict):
            full = {}
        u = full.setdefault(str(uid), {})
        if model in _AP_MODELS and effort in _EFFORTS:
            u[action] = {"model": model, "effort": effort}
        else:
            u.pop(action, None)
        try:
            _AP_PATH.parent.mkdir(parents=True, exist_ok=True)
            _AP_PATH.write_text(json.dumps(full, ensure_ascii=False), "utf-8")
        except Exception:
            pass
        return u.get(action)


# 路由正则(模块级,_is_quick / _effort_for 共用)
_DEEP_RE = (r"为什么|为何|怎么|如何|什么意思|是什么|含义|解释|讲讲|讲解|说说|说明|原理|推导|证明|理解|"
            r"区别|差别|比较|对比|本质|分析|总结|概括|关系|意义|作用|举例|例子|思路|联系|论证")
_QUICK_RE = r"跳到|翻到|打开第|第\s*\d+\s*页|高亮|制卡|做成卡|加生词|生词本|翻译这|译一下|查一下.{0,4}页"


def _is_quick(message):
    """导航/写动作(跳页/高亮/制卡…):恒走快档,不被预设拖慢(秒回)。"""
    import re
    m = message or ""
    return bool(re.search(_QUICK_RE, m)) and not re.search(_DEEP_RE, m)


def _effort_for(message, ctx, uid=None):
    """无预设时按问题选思考深度:导航/写动作恒 low;解释/推导/总结 + 钉了焦点 → high;其余 low(快)。"""
    import re
    m = message or ""
    if _is_quick(m):
        return "low"
    if (isinstance(ctx, dict) and ctx.get("focus_sel")) or re.search(_DEEP_RE, m):
        return "high"
    return "low"


_EFFORTS = ("low", "medium", "high", "xhigh", "max")   # claude CLI 合法 effort 枚举(无 "ultra")


def _agent_run(message, ctx, history, force_effort=None, force_model=None):
    """生成 SSE 事件 dict:{event, data}。event ∈ tool|tool-done|answer|actions|trace|error。"""
    uid = (ctx or {}).get("_uid")
    fe = force_effort if force_effort in _EFFORTS else None   # 感叹号「更强重答」强制档(一次性)
    fm = force_model if force_model in _AP_MODELS else None
    ap = None if _is_quick(message) else _ap_get(uid, "orchestrator")   # 导航/写动作不套预设(保持秒回);否则用「回答」动作的用户预设(⚙/🐢/🎯 写的)
    eff = fe or (ap[1] if ap else None) or _effort_for(message, ctx, uid)
    mdl = fm or (ap[0] if ap else None) or _AGENT_MODEL
    trace = [{"label": "编排+回答", "model": f"{mdl}·{eff}", "action": "orchestrator"}]   # 编排器档(默认 sonnet/分档,可被预设/重答改)
    p = _take_proc(eff, model=mdl)
    if not p:
        yield {"event": "error", "data": "助手起不来(claude 起不来)"}
        return
    _qw = _quota_warning()   # 额度护栏:近上限只提醒,不降级、不阻断(用户不用 Gemini 降级)
    if _qw:
        yield {"event": "notice", "data": _qw}
    client_actions = []
    try:
        # 静态规则已走 --system-prompt(_take_proc spawn 时设),这里只发 动态【当前页面】块 + 历史 + 用户消息
        content = f"{_ctx_block(ctx)}\n\n{_format_history(history, int((ctx or {}).get('page_offset') or 0))}【用户】{message}\n\n现在开始(调工具就只输出 JSON,能答就直接答):"
        _t_start = time.time()
        _repair_tries = 0
        _resp_retry = 0          # 首轮无响应(预热进程失效)→ 换新进程重试一次
        _tools_ran = False       # 调过工具后进程里有对话上下文,不能再随意换进程
        _heavy = eff in ("xhigh", "max")   # 高档位(尤其 opus·max)思考久 → 放宽单轮/总超时,否则深答被腰斩成"没响应"
        _round_to = 180.0 if _heavy else 90.0
        _total_to = 320.0 if _heavy else 240.0
        for step in range(40):   # 步数放很高(40):真正的护栏是下面的总超时,步数只当 runaway 兜底,别因步数砍掉复杂多工具任务
            if time.time() - _t_start > _total_to:   # 总超时:防卡死的 claude 占住 gunicorn worker
                yield {"event": "answer", "data": "(处理用时太久,先到这——可以再问我一次,或换个更具体的问法)"}
                break
            raw = None
            _last_emit = 0.0
            for kind, val in _send_stream(p, content, timeout=_round_to):
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
                # 模型这轮没吐字。常见原因:预热池那个 claude 进程放久了、底层会话失效 → 发过去就卡到超时。
                # 还没调过工具(进程里没对话上下文要保)→ 杀掉它、**现起一个全新进程**重试一次(绕开失效的预热进程)。
                if not _tools_ran and _resp_retry < 1:
                    _resp_retry += 1
                    try: _kill(p)
                    except Exception: pass
                    p = _spawn(effort=eff, model=mdl, system=_sys_static())   # 强制全新进程(不取可能也失效的预热池)
                    if not p:
                        yield {"event": "error", "data": "助手起不来(claude 起不来)"}
                        return
                    _t_start = time.time()   # 重起算新开始,重置总超时计时
                    yield {"event": "tool", "data": "重连助手"}
                    yield {"event": "tool-done", "data": "重连助手"}
                    continue                 # 用同样的 content 重发
                yield {"event": "error", "data": "助手没响应(超时)。再问我一次试试。"}
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
                _tools_ran = True   # 进程里已有对话上下文 → 之后无响应不能再换进程(会丢上下文)
                name = tool["tool"]
                targs = tool.get("args") if isinstance(tool.get("args"), dict) else {}
                yield {"event": "tool", "data": _tool_label(name, targs)}
                _t_tool0 = time.time()
                try:
                    res = TOOLS[name][1](targs, ctx) or {}
                except Exception as e:
                    res = {"error": str(e)[:160]}
                _tool_sec = round(time.time() - _t_tool0, 1)   # 这步耗时(感叹号弹窗显示)
                vision = res.pop("_vision", None) if isinstance(res, dict) else None   # 图片喂回大脑(sonnet 多模态)
                _gm = res.pop("_gen_model", None) if isinstance(res, dict) else None   # 生成步工具用的强模型(如 summarize=opus)
                _ga = res.pop("_gen_action", None) if isinstance(res, dict) else None   # 生成步的动作键(可在 ⚙ 里调它的预设)
                trace.append({"label": _tool_label(name, targs), "model": _gm or "—", "sec": _tool_sec, "action": _ga})   # 轨迹:任务名+模型+耗时(+ 可调预设的动作键)
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
        _tool_total = sum(t.get("sec", 0) for t in trace[1:])   # 编排器自身耗时 = 总耗时 - 各工具耗时
        trace[0]["sec"] = round(max(0.0, (time.time() - _t_start) - _tool_total), 1)
        yield {"event": "trace", "data": trace}   # 调用轨迹(感叹号里展示:每步任务名 + 模型 + 耗时)
    finally:
        _kill(p)
        threading.Thread(target=_warm_respawn, daemon=True).start()


# ── 助手生成任务:detached(后台线程跑到完,客户端断了也不杀、跑完照样落库)+ 按 rid 缓冲事件供重连续读 ──
# 根治「切后台→连接断→只能叫你刷新」:生成不再绑请求生命周期,客户端拿同一个 rid 接着读即可,全程零操作。
_chat_jobs = {}
_chat_jobs_lock = threading.Lock()


def _chat_worker(rid, message, ctx, history, force_effort, force_model, uid):
    job = _chat_jobs[rid]
    try:
        for ev in _agent_run(message, ctx, history, force_effort=force_effort, force_model=force_model):
            with job["lock"]:
                job["events"].append(ev)
                if ev["event"] == "answer":
                    job["answer"] = ev["data"]
                elif ev["event"] == "trace":
                    job["trace"] = ev["data"]
    except Exception as e:
        with job["lock"]:
            job["events"].append({"event": "error", "data": str(e)[:160]})
    finally:
        with job["lock"]:
            job["events"].append({"event": "done", "data": {}})
            job["done"] = True
        if job.get("answer"):   # 不管客户端在不在,跑完就落库(断连也不丢;历史/感叹号都用得上)
            _convo_append(uid, "assistant", str(job["answer"])[:1500], {"trace": job.get("trace")} if job.get("trace") else None)
        def _cleanup():
            with _chat_jobs_lock:
                _chat_jobs.pop(rid, None)
        t = threading.Timer(180, _cleanup); t.daemon = True; t.start()   # 留 3min 给重连续读,之后清


# ──────────────────────── 路由 ────────────────────────
@bp.route("/chat", methods=["POST"])
def assistant_chat():
    if not _logged_in():
        return jsonify({"ok": False, "error": "auth"}), 401
    body = request.get_json(silent=True) or {}
    uid = session["user_id"]
    rid = (str(body.get("rid") or "").strip())[:64] or f"c{int(time.time() * 1000)}-{len(_chat_jobs)}"
    try:
        frm = max(0, int(body.get("from") or 0))   # 重连:从第几个缓冲事件接着读
    except Exception:
        frm = 0
    with _chat_jobs_lock:
        job = _chat_jobs.get(rid)
        if job is None:
            message = (body.get("message") or "").strip()
            if not message:
                # rid 给了但任务已不在(>3min 被清)→ 让前端走历史恢复(答案早落库了),而不是报错
                if body.get("rid"):
                    return jsonify({"ok": False, "error": "gone"}), 410
                return jsonify({"ok": False, "error": "empty"}), 400
            force_effort = body.get("force_effort") if body.get("force_effort") in _EFFORTS else None
            force_model = body.get("force_model") if body.get("force_model") in _AP_MODELS else None
            ctx = body.get("context") or {}
            ctx["_base"] = request.host_url.rstrip("/")
            ctx["_uid"] = uid   # 写操作记 owner=本用户 → 撤销只能撤自己的
            history = [{k: m.get(k) for k in ("role", "content", "page", "pages", "book", "file_rel", "selection")}
                       for m in _convo_load(uid)[-6:]]
            _convo_append(uid, "user", message, {   # 用户消息进 agent 前就落库 → 断连也不丢这轮 + 保住"刚才那页"链
                "page": ctx.get("page"), "pages": ctx.get("pages"),
                "book": ctx.get("book_name"), "file_rel": ctx.get("file_rel"),
                "selection": ctx.get("selection"),
                "figures": [{k: f.get(k) for k in ("page", "box", "caption", "group", "has_ink", "file_rel")}
                            for f in (ctx.get("figures") or [])][:6],
            })
            job = _chat_jobs[rid] = {"events": [], "answer": "", "trace": None, "done": False,
                                     "lock": threading.Lock(), "uid": uid}
            threading.Thread(target=_chat_worker, daemon=True,
                             args=(rid, message, ctx, history, force_effort, force_model, uid)).start()
        elif job.get("uid") != uid:
            return jsonify({"ok": False, "error": "forbidden"}), 403   # 别人的 rid 不给读

    def gen():
        yield f"event: meta\ndata: {json.dumps({'rid': rid})}\n\n"   # 回 rid 给前端(断线用它重连);meta 不进缓冲计数
        i = frm
        idle = 0
        while True:
            with job["lock"]:
                n = len(job["events"])
                evs = list(job["events"][i:n])
                done = job["done"]
            for ev in evs:
                yield f"event: {ev['event']}\ndata: {json.dumps(ev['data'], ensure_ascii=False)}\n\n"
            i = n
            if done and i >= len(job["events"]):
                return
            idle = 0 if evs else idle + 1
            if idle > 2400:   # ~6min 没动静(worker 卡死)兜底收
                return
            time.sleep(0.1)

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


@bp.route("/action-pref", methods=["POST"])
def assistant_action_pref():
    """按动作存「模型+深度」预设(感叹号弹窗的 ⚙ 设置 / 🐢 太慢调快 / 🎯 更强写它)。
    body: {action, model, effort}。model/effort 非法(或空)→ 清除该动作预设、回默认。"""
    if not _logged_in():
        return jsonify({"ok": False}), 401
    b = request.get_json(silent=True) or {}
    action = b.get("action")
    if action not in _AP_ACTIONS:
        return jsonify({"ok": False, "error": "bad action"}), 400
    saved = _ap_set(session["user_id"], action, b.get("model"), b.get("effort"))
    return jsonify({"ok": True, "pref": saved})   # saved=None → 已清除回默认


@bp.route("/prewarm", methods=["POST"])
def assistant_prewarm():
    if not _logged_in():
        return jsonify({"ok": False}), 401
    off = bool((request.get_json(silent=True) or {}).get("off"))
    threading.Thread(target=(_warm_reap if off else _warm_prewarm), daemon=True).start()
    return jsonify({"ok": True})


def register_assistant(app):
    app.register_blueprint(bp)
