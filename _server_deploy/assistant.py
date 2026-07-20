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
import re
import select
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Blueprint, Response, jsonify, request, send_file, session

bp = Blueprint("assistant", __name__, url_prefix="/api/assistant")

CLAUDE_DIR = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
VAULT_ROOT = Path(os.environ.get("OBSIDIAN_VAULT", "/home/bwicarus/obsidian"))


def _VB():
    """领域服务(统一书模型)。拿不到就返回 None,调用方退回恒等行为。"""
    try:
        import sys as _s
        _p = str(CLAUDE_DIR / "scripts" / "lib")
        if _p not in _s.path:
            _s.path.insert(0, _p)
        import vbook as _v
        return _v
    except Exception:
        return None


def _vb_src(file_rel, page):
    """(书, 视图页) → (真实成员, 该卷局部页)。**单本书=恒等**,所以无条件调用即可
    ——业务代码里不该再出现 `if 是合并书` 的分支(用户拍板:单本书=单成员的合并书)。"""
    v = _VB()
    if not v:
        return file_rel, page
    try:
        return v.locate(file_rel, int(page or 1))
    except Exception:
        return file_rel, page


_PAGES_CACHE = {}   # rel → (mtime_ns, 页数);fitz.open 每次工具调用都开文件不划算


def _book_total_pages(file_rel):
    """这本书**一共多少页**(合并书=各卷之和,与阅读器/book-meta 同一口径)。拿不到返回 0。
    设计(用户拍板):总页数**不做开话快照**——跨书对话时快照必然过期、还会自信地报错数;
    改成随工具结果实时回报,AI 每次调书内工具都看到当下这本书的真实页数。"""
    try:
        if not file_rel:
            return 0
        if isinstance(file_rel, str) and (file_rel.startswith("web:")
                                          or file_rel.lower().endswith((".html", ".htm", ".md", ".markdown"))):
            return 1   # 网页/HTML/MD=单文档(fitz 会把它 reflow 成多页,那是假页数)
        v = _VB()
        if v:   # 领域服务对两种书都算得出(合并=各卷之和,单本=它自己);拿不到才落回本地 fitz
            n0 = int(v.total_pages(file_rel) or 0)
            if n0:
                return n0
        pdf = _pdf()
        ap = pdf._safe_vault_path(file_rel)
        if not ap:
            return 0
        mt = ap.stat().st_mtime_ns
        hit = _PAGES_CACHE.get(file_rel)
        if hit and hit[0] == mt:
            return hit[1]
        import fitz
        d = fitz.open(str(ap))
        n = d.page_count
        d.close()
        _PAGES_CACHE[file_rel] = (mt, n)
        return n
    except Exception:
        return 0


# 书内定位类工具:结果里回报总页数(『翻到最后一页/还剩几页/第N页有没有』都靠它,且换书自动跟着变)
_PAGES_IN_RESULT = {"read_page", "goto_page", "toc", "search_book", "search_in_book",
                    "summarize_section", "see_page", "read_highlights", "find_highlights",
                    "page_vocab", "auto_highlight", "open_book"}


def _run_tool(name, targs, ctx):
    """工具执行**统一出口**:跑工具 + 给书内工具的结果补上『全书总页数』。"""
    res = TOOLS[name][1](targs, ctx) or {}
    try:
        if name in _PAGES_IN_RESULT and isinstance(res, dict) and not res.get("error"):
            # open_book 是换书:报**目标书**的页数(ctx.file_rel 还是旧书)
            _fr = (targs.get("file_rel") or "") if name == "open_book" else ""
            n = _book_total_pages(_fr or ctx.get("file_rel") or "")
            if n:
                res.setdefault("全书总页数", n)
    except Exception:
        pass
    return res


def _vb_part_of(file_rel, page):
    """视图页 → 它所在的那一卷 (rel, offset)。单本书恒为 (自己, 0)。越界 → (None, 0)。"""
    for _mrel, _moff, _mpgs in _vb_members(file_rel):
        if _moff < page <= _moff + (_mpgs or 10 ** 9):
            return _mrel, _moff
    return None, 0


def _vb_localize(file_rel, pages):
    """一组视图页 → (所在卷 rel, 该卷内的局部页们)。跨卷时只取**第一页所在**那一卷
    (see_page/highlight 这类"一次操作一卷"的工具语义)。单本书=原样返回。"""
    if not pages:
        return file_rel, pages
    mrel, moff = _vb_part_of(file_rel, pages[0])
    if not mrel:
        return None, []
    hi = moff + (dict((m[0], m[2]) for m in _vb_members(file_rel)).get(mrel) or 10 ** 9)
    return mrel, [p - moff for p in pages if moff < p <= hi]


def _vb_hls(file_rel):
    """合并书:各卷高亮合并 + page 全局化(前端/AI 都用全局页;删除走 id 路由跨卷定位)。非合并书=原样。
    网页(web:)走字符偏移 sidecar —— 与阅读器同一套存储(审计 #4:两套存储各自为政会让
    AI 说"没有高亮"而页面上明明有)。"""
    if isinstance(file_rel, str) and file_rel.startswith("web:"):
        try:
            import html_reader as _HR
            return [dict(h, page=1) for h in (_HR._html_hl_load(file_rel) or [])]
        except Exception:
            return []
    pdf = _pdf()
    out = []
    for _mrel, _moff, _ in _vb_members(file_rel):
        for h in ((pdf._hl_load(_mrel) or {}).get("highlights") or []):
            if _moff and isinstance(h.get("page"), (int, float)):
                h = dict(h); h["page"] = int(h["page"]) + _moff
            out.append(h)
    return out


def _vb_notes(file_rel):
    """合并书:各卷便签合并 + anchor.page 全局化。非合并书=原样 _notes_load。"""
    pdf = _pdf()
    out = []
    for _mrel, _moff, _ in _vb_members(file_rel):
        for n in (pdf._notes_load(_mrel) or []):
            a = n.get("anchor") or {}
            if _moff and isinstance(a.get("page"), (int, float)):
                n = dict(n); n["anchor"] = dict(a); n["anchor"]["page"] = int(a["page"]) + _moff
            out.append(n)
    return out


def _vb_note_owner(file_rel, nid):
    """合并书:按便签 id 找到它真正落在哪一卷 → (member_rel, offset)。找不到返回 (None, 0)。"""
    pdf = _pdf()
    for _mrel, _moff, _ in _vb_members(file_rel):
        if any(x.get("id") == nid for x in (pdf._notes_load(_mrel) or [])):
            return _mrel, _moff
    return None, 0


def _vb_members(file_rel):
    """这本书的成员们 → [(rel, offset, pages)]。**单本书=一个成员**(offset 0、pages 真页数),
    所以「遍历整本」的代码对两种书是同一份。"""
    v = _VB()
    if not v:
        return [(file_rel, 0, _book_total_pages(file_rel))]
    try:
        return [(m["rel"], m["offset"], m["pages"]) for m in v.parts(file_rel)]
    except Exception:
        return [(file_rel, 0, _book_total_pages(file_rel))]
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
# 两把 Gemini key:免费优先(额度耗尽/限流自动切付费),各自独立冷却。文件各存一把(不进 git/代码)。
_GEMINI_KEY_FILES = [
    ("free", Path("/home/bwicarus/.config/gemini-api-key-free")),   # 免费档:优先用,省钱
    ("paid", Path("/home/bwicarus/.config/gemini-api-key")),        # 付费档:免费耗尽/限流时兜底
]
_GEMINI_MODEL = "gemini-3.5-flash"   # 最新稳定 Flash(2.x 已过时;型号清单见 ListModels)

_gemini_off = {}            # {tier: off_until_ts}:某把 key 遇额度限流(429/403)后的**临时**冷却(期间整把跳过)
# 某把 key **根本用不了**的模型(免费档没这模型/需付费/无权限)→ 对该模型跳过这把。**持久化**(重启不丢),
# 30 天软过期(防 Google 后来给免费开放了却被永久误跳)。文件格式 {tier: {model: 标记时刻}}。
_GEMINI_UNSUP_FILE = CLAUDE_DIR / "state" / "gemini-unsupported.json"
_GEMINI_UNSUP_TTL = 30 * 86400
_gemini_unsup_cache = None

def _unsup_all():
    global _gemini_unsup_cache
    if _gemini_unsup_cache is None:
        try:
            _gemini_unsup_cache = json.loads(_GEMINI_UNSUP_FILE.read_text("utf-8")) or {}
        except Exception:
            _gemini_unsup_cache = {}
    return _gemini_unsup_cache

def _is_unsupported(tier, model):
    ts = (_unsup_all().get(tier) or {}).get(model)
    return ts is not None and (time.time() - ts) < _GEMINI_UNSUP_TTL

def _variant_paid(v):
    """variant 的「直连付费」后缀约定:'gemini-3.5-flash@paid' = 同型号、跳过 free key 直接用付费档
    (用户在「免费受限已切付费」提示条上点了「以后直接用付费」→ action-pref 存这种 variant)。
    返回 (裸型号, 是否直连付费)。所有拿 variant 当 model 用的地方经此剥后缀。"""
    s = str(v or "")
    return (s[:-5], True) if s.endswith("@paid") else (s, False)


def _gemini_keys(model=None):
    """按优先级返回当前可用的 [(key, tier)]:免费在前。跳过 ① 临时冷却中的 ② 对该 model 不支持的。空=都不可用。
    model 带 '@paid' 后缀(用户选了直连付费)→ 跳过 free key。"""
    model, _paid_first = _variant_paid(model)
    now = time.time(); out = []
    for tier, f in _GEMINI_KEY_FILES:
        if now < _gemini_off.get(tier, 0):
            continue
        if _paid_first and tier == "free":           # 直连付费:不碰免费档(不白吃 429、不加延迟)
            continue
        if model and _is_unsupported(tier, model):   # 这把用不了该模型(持久记录+30天软过期)→ 跳过
            continue
        if model and tier == "free" and _is_paid_only(model):   # 仅付费型号:free 必伪429,直接跳过不白试
            continue
        try:
            k = f.read_text().strip()
        except Exception:
            k = ""
        if k:
            out.append((k, tier))
    return out

def _gemini_cooldown(tier, secs=300):
    _gemini_off[tier] = time.time() + secs

def _mark_unsupported(tier, model):
    """记住"这把 key 用不了该模型",写盘持久化(重启不丢)。"""
    d = _unsup_all()
    d.setdefault(tier, {})[model] = time.time()
    try:
        _GEMINI_UNSUP_FILE.parent.mkdir(parents=True, exist_ok=True)
        _GEMINI_UNSUP_FILE.write_text(json.dumps(d, ensure_ascii=False), "utf-8")
    except Exception:
        pass

# 免费档「确实可用」的持久缓存:跟 _gemini_unsupported 互补——一个记 no(不支持)、一个记 ok(已验证可用),
# 都没有=unknown(没探过)。让面板能在「真用之前」就知道某型号免费能不能用,而不是等用户点了失败才隐藏。
_GEMINI_FREE_OK_FILE = CLAUDE_DIR / "state" / "gemini-free-ok.json"
_GEMINI_FREE_OK_TTL = 30 * 86400
_gemini_free_ok_cache = None

def _free_ok_all():
    global _gemini_free_ok_cache
    if _gemini_free_ok_cache is None:
        try:
            _gemini_free_ok_cache = json.loads(_GEMINI_FREE_OK_FILE.read_text("utf-8")) or {}
        except Exception:
            _gemini_free_ok_cache = {}
    return _gemini_free_ok_cache

def _mark_free_ok(model):
    d = _free_ok_all(); d[model] = time.time()
    try:
        _GEMINI_FREE_OK_FILE.parent.mkdir(parents=True, exist_ok=True)
        _GEMINI_FREE_OK_FILE.write_text(json.dumps(d, ensure_ascii=False), "utf-8")
    except Exception:
        pass

def _free_state(model):
    """免费档对该型号的已知状态:'no'(验证为不支持→面板隐藏)/'ok'(验证可用)/'unknown'(没探过)。"""
    if _is_unsupported("free", model):
        return "no"
    ts = _free_ok_all().get(model)
    if ts is not None and (time.time() - ts) < _GEMINI_FREE_OK_TTL:
        return "ok"
    return "unknown"

# 「仅付费档」型号(如 gemini-3.1-pro-preview)持久缓存:_gemini_models() 合并两把 key 的清单时更新。
# 用途:① 面板给这些型号标 💰仅付费(而不是像以前那样被「免费不支持→隐藏」逻辑吞掉,导致 3.1-pro
# 永远选不到);② _gemini_keys 对它们直接跳过 free key(免得每次白吃一记伪 429)。
# 判定要两个信号并用(见 _is_paid_only):Google 的免费 key ListModels **也会列出** paid-only 型号
# (列出≠能用,generateContent 才吃伪 429),所以光靠清单差集不够。每 6h 随 ListModels 刷新;
# 免费验证走 _GEMINI_UNSUP_FILE 的 30 天软过期自愈(Google 后来给免费开放 → 自动摘掉)。
_GEMINI_PAID_ONLY_FILE = CLAUDE_DIR / "state" / "gemini-paid-only.json"
_gemini_paid_only_cache = None   # {"only": set(免费清单里根本没有的), "paid": set(付费清单全量)}

def _paid_only_all():
    global _gemini_paid_only_cache
    if _gemini_paid_only_cache is None:
        try:
            d = json.loads(_GEMINI_PAID_ONLY_FILE.read_text("utf-8")) or {}
            _gemini_paid_only_cache = {"only": set(d.get("only") or d.get("models") or []),
                                       "paid": set(d.get("paid") or [])}
        except Exception:
            _gemini_paid_only_cache = {"only": set(), "paid": set()}
    return _gemini_paid_only_cache

def _save_paid_only(only, paid):
    global _gemini_paid_only_cache
    _gemini_paid_only_cache = {"only": set(only or ()), "paid": set(paid or ())}
    try:
        _GEMINI_PAID_ONLY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _GEMINI_PAID_ONLY_FILE.write_text(
            json.dumps({"only": sorted(_gemini_paid_only_cache["only"]),
                        "paid": sorted(_gemini_paid_only_cache["paid"]), "ts": time.time()},
                       ensure_ascii=False), "utf-8")
    except Exception:
        pass

def _is_paid_only(model):
    """「仅付费档」判定,两个信号任一即真:
    ① free key 的 ListModels 里根本没有、paid 的有(清单差集);
    ② paid 清单里有 **且** 免费档已被验证为不支持(生成时伪 429 `free_tier…limit: 0` → _mark_unsupported,
       或面板探测标 no)。实测 ② 才是 3.1-pro 的常态:免费 ListModels 会「列出」它但生成必拒。"""
    d = _paid_only_all()
    return model in d["only"] or (model in d["paid"] and _is_unsupported("free", model))

def _probe_free(model, timeout=12):
    """用免费 key 发一个 1-token 试探请求,判定免费档支不支持该型号,结果写持久缓存(每型号只探一次,30 天有效)。
    200→标 ok;正文明确不支持(需付费/无权限)→标 no;限流/网络异常→不写(保持 unknown,下次面板再探)。
    免费档不计费;maxOutputTokens=1 即便被截断只要返回 200 也证明「免费档放行了这个型号」。"""
    if _is_paid_only(model):   # ListModels 已证实仅付费 → 不用探(结论已知,省一次请求)
        return
    if _free_state(model) != "unknown":
        return
    if time.time() < _gemini_off.get("free", 0):   # 免费 key 正冷却 → 结果会被限流污染,先别探
        return
    try:
        free_key = _GEMINI_KEY_FILES[0][1].read_text().strip()
    except Exception:
        free_key = ""
    if not free_key:
        return
    cfg = {"maxOutputTokens": 1}
    if "pro" not in model:   # Pro 是 thinking-only,thinkingBudget=0 会 400
        cfg["thinkingConfig"] = {"thinkingBudget": 0}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key=" + free_key
    try:
        import requests
        r = requests.post(url, json={"contents": [{"parts": [{"text": "hi"}]}], "generationConfig": cfg}, timeout=timeout)
    except Exception:
        return
    _gemini_log("assistant:probe", r.status_code, model, tier="free")
    if r.status_code == 200:
        _mark_free_ok(model)
    elif _is_model_unsupported(r.status_code, r.text):
        _mark_unsupported("free", model)   # 免费档确实用不了 → 持久记住 + 面板隐藏
    # 其它(429/5xx)→ 不写缓存,保持 unknown,下次再探

_probe_fail = {}          # model → 上次探测"没得出结论"的时刻(限流/网络异常)
_PROBE_FAIL_COOLDOWN = 900   # 15 分钟内不再重探同一个型号


def _probe_free_batch(models):
    """并发探测一批"未验证"型号的免费档支持情况(只探 unknown 的;已知 ok/no 跳过)。

    ⚠ 用户实测「模型设置面板有时要加载很久」的根因之一:
      _probe_free 只在 **200(ok)** 或 **明确不支持(no)** 时才写持久缓存;
      **限流/网络异常时不写** → 该型号永远停在 unknown → **每次打开面板都重探一遍**,
      每个 12s 超时 → 面板一直转圈。
      故:探测没得出结论的,记 _probe_fail 冷却 15 分钟,别每次都撞。
    """
    now = time.time()
    todo = [m for m in (models or [])
            if _free_state(m) == "unknown" and now - _probe_fail.get(m, 0) > _PROBE_FAIL_COOLDOWN]
    if not todo or now < _gemini_off.get("free", 0):
        return
    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(8, len(todo))) as ex:
            list(ex.map(_probe_free, todo))
    except Exception:
        for m in todo:
            _probe_free(m)
    for m in todo:   # 探完仍是 unknown = 没得出结论(限流/网络)→ 上冷却,别下次开面板又撞一遍
        if _free_state(m) == "unknown":
            _probe_fail[m] = time.time()

def _is_model_unsupported(status, text):
    """这把 key 是不是"根本用不了这个模型"(免费档没这模型/需付费/无权限)——**永久性**,区别于额度耗尽(临时)。
    **只认响应正文里的明确语义,不无条件信 404**(404 也可能是网关/临时/URL 问题)→ 漏判时退化为临时冷却
    (更安全:临时冷却会过期,误标"永久不支持"会长期误伤)。status 仅作参考,判定以 text 为准。"""
    low = (text or "").lower()
    # 关键:Google 对"免费档根本不含此型号"返回的是**伪 429**——正文写「Please retry in 49s」装成临时限流,
    # 但 quota metric 写死 `...free_tier_requests, limit: 0`(每日上限就是 0,重试永远没用)。这是**永久**信号,
    # 不能当临时冷却,否则面板永远把 pro 标"免费可用"。识别 free_tier + limit:0 → 判为不支持。
    if "free_tier" in low and "limit: 0" in low:
        return True
    return any(s in low for s in (
        "not found for api", "is not found", "not supported for", "not available",
        "is not available", "free quota tier", "free tier", "does not have access",
        "not enabled for", "only accessible", "paid tier", "requires billing"))

def _retry_after(resp_text, default=300):
    """这把 key 遇 429/403 后该跳过多久(秒),按 Google 返回信息自动决定,避免"每次白试":
    每日额度耗尽(RPD)→ 长跳(当天基本别再试);每分钟限流(RPM)→ 用 RetryInfo.retryDelay(几十秒就恢复);其它→default。"""
    t = (resp_text or "")
    low = t.lower()
    if "per day" in low or "perday" in low.replace("_", "").replace(" ", ""):
        return 4 * 3600   # 每日额度用完:跳 4h(免费当天基本没了,别每几分钟白试一次)
    try:
        import re
        m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', t)
        if m:
            return max(10, int(float(m.group(1))) + 3)   # Google 建议的重试间隔(+缓冲)
    except Exception:
        pass
    return default

# 本轮(一次助手回答)累计 token:thread-local(每个回答跑在自己的后台线程里,互不串)。
# 所有 AI 调用(gemini 经 _gemini_log、claude 经 _send/_send_stream)都往这加 → 编排器收尾时取总数写 trace[0].tok。
_tok_acc = threading.local()
def _tok_reset():
    _tok_acc.n = 0
def _tok_add(n):
    try:
        _tok_acc.n = getattr(_tok_acc, "n", 0) + int(n or 0)
    except Exception:
        pass
def _tok_get():
    return getattr(_tok_acc, "n", 0)


# ── AI 产物缓存:总结/挑句这类对**同一段内容是确定性**的输出,按内容哈希存一份 → 重复请求 0 token ──
# 防"缓存了一个将就版本就永远用它"的三道闸:
#   ① 版本号 _AI_CACHE_VER:我们改进了生成 prompt/逻辑 → +1,全量旧缓存立即作废、下次重生成;
#   ② TTL:单条再老(默认 45 天)就当未命中、重新生成一次(拿一份新 sample);
#   ③ 即时覆盖:用户点感叹号「更强重答」→ ctx._no_cache=True,工具跳过缓存重做并**覆盖**旧版(见各工具)。
_AI_CACHE_DIR = CLAUDE_DIR / "state" / "assistant-ai-cache"
_AI_CACHE_VER = 1
_AI_CACHE_TTL = 45 * 86400
def _ai_cache_key(*parts):
    import hashlib
    return hashlib.sha1("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:24]
def _ai_cache_get(key):
    try:
        d = json.loads((_AI_CACHE_DIR / f"{key}.json").read_text("utf-8"))
        if d.get("ver") != _AI_CACHE_VER:                 # 生成逻辑变了 → 旧缓存作废
            return None
        if time.time() - float(d.get("ts", 0)) > _AI_CACHE_TTL:   # 太老 → 当未命中,重生成
            return None
        return d.get("v")
    except Exception:
        return None
def _ai_cache_set(key, value):
    try:
        _AI_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (_AI_CACHE_DIR / f"{key}.json").write_text(
            json.dumps({"v": value, "ts": time.time(), "ver": _AI_CACHE_VER}, ensure_ascii=False), "utf-8")
    except Exception:
        pass


_free_busy = {}             # {model: (失败时刻, status)}:免费档对某型号最近一次**非200**(503过载/429限流) → 面板据此显示"免费暂不可用→走付费",跟实际对上
_FREE_BUSY_TTL = 300        # 5min 内有免费失败就认为免费档当前不可用(成功一次即清掉)

def _gemini_log(label, status, model="", tokens=0, tin=0, tout=0, tier=""):
    """记进额度日志:units=本次总 token。note 带 model + tier(**免费档不计费**,成本面板据此跳过)+ 输入/输出 token。
    算钱**按每次用的模型单价**(Flash/Pro 差好几倍);Gemini 没有查余额的 API,真余额去 AI Studio billing 看。
    顺便跟踪**免费档对该型号的实时近况**(给设置面板:免费现在到底能不能用,而不是只看"支持不支持")。"""
    _tok_add(tokens)   # 累计本轮 token(所有 gemini 调用——编排/总结/看图都经这——一处全收)
    if model and tier == "free":
        try:
            if int(status) == 200:
                _free_busy.pop(model, None)                    # 免费成功 → 清掉"繁忙"标记
            elif status:
                _free_busy[model] = (time.time(), int(status))  # 免费失败(503过载/429限流)→ 记下,面板显示"暂走付费"
        except Exception:
            pass
    try:
        sys.path.insert(0, str(CLAUDE_DIR / "scripts"))
        from google_api_quota import log_usage
        log_usage("gemini", int(tokens or 0), label,
                  note=f"model={model} tier={tier} in={int(tin)} out={int(tout)} status={status}")
    except Exception:
        pass

def _gemini_usage(j):
    """从 Gemini 响应 json 取 token 用量 {total, prompt, out}。"""
    u = (j or {}).get("usageMetadata") or {}
    return {"total": u.get("totalTokenCount", 0), "prompt": u.get("promptTokenCount", 0),
            "out": u.get("candidatesTokenCount", 0)}

def gemini_embed(texts, timeout=30, dim=None):
    """批量文本 → 向量(gemini-embedding-001,3072 维)。复用 _gemini_keys(免费优先→付费兜底)。
    注意力画像的跨语言关联用(vector space ↔ 向量空间 在向量空间里很近)。返回 [[float]…] 或 None。
    单条也传 list。失败(限流/网络)自动切下一把 key;全失败 → None。"""
    if isinstance(texts, str):
        texts = [texts]
    texts = [str(t)[:2000] for t in texts if str(t or "").strip()]
    if not texts:
        return []
    import requests
    keys = _gemini_keys("gemini-embedding-001")
    if not keys:
        return None
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent?key="
    out = []
    for t in texts:
        got = None
        for k, _ in keys:                 # 每条独立试 key(免费断了切付费)
            try:
                _b = {"content": {"parts": [{"text": t}]}}
                if dim:
                    _b["outputDimensionality"] = int(dim)
                r = requests.post(url + k, json=_b, timeout=timeout)
            except Exception:
                continue
            if r.status_code == 200:
                try:
                    got = r.json()["embedding"]["values"]
                    break
                except Exception:
                    pass
        if got is None:
            return None                    # 有一条失败就整批放弃(半批向量没意义)
        out.append(got)
    return out


def _gemini_text(prompt, max_tokens=4000, think=True, timeout=90, model=None):
    """Gemini 出文本(深度解释/总结)。免费 key 优先,**任何失败(限流/5xx/网络/不支持)都自动切付费**;付费也失败/空 → None(调用方才回退 Claude)。"""
    keys = _gemini_keys(model or _GEMINI_MODEL)          # '@paid' 后缀在 _gemini_keys 内消化(跳过 free)
    mdl = _variant_paid(model or _GEMINI_MODEL)[0]       # URL/记账/标记一律用裸型号
    if not keys:
        return None
    cfg = {"temperature": 0.4, "maxOutputTokens": max_tokens}
    if not think and "pro" not in mdl:   # Pro 是 thinking-only,thinkingBudget=0 会 400
        cfg["thinkingConfig"] = {"thinkingBudget": 0}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{mdl}:generateContent?key="
    for key, tier in keys:
        try:
            import requests
            r = requests.post(url + key, json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": cfg}, timeout=timeout)
        except Exception:
            continue   # 网络异常(超时等)→ 试下一把 key(免费断了也要给付费机会),别直接回退 Claude
        if r.status_code == 200:
            j = r.json(); us = _gemini_usage(j)
            _gemini_log("assistant:text", 200, mdl, us["total"], us["prompt"], us["out"], tier)
            cand = (j.get("candidates") or [{}])[0]
            out = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []))
            return out.strip() or None
        _gemini_log("assistant:text", r.status_code, mdl, tier=tier)
        if _is_model_unsupported(r.status_code, r.text):
            _mark_unsupported(tier, mdl)   # 这把永久不支持该模型 → 记住,下次同模型直接跳
        elif r.status_code in (429, 403):
            _gemini_cooldown(tier, _retry_after(r.text))   # 额度限流 → 临时冷却整把
        # 5xx 临时高负载 / 400 构造错 → 不冷却整把,直接试下一把 key
        continue   # 任何非 200 都试下一把 key(免费不行→付费);全失败才 None→回退 Claude
    return None

def _gemini_websearch(query, timeout=45, model=None):
    """网页搜索首选(64):Gemini google_search grounding——3.x 系**每月 5000 次免费**(之后 $14/1k;
    2.x 时代是 1500 次/天 $35/1k)。免费 key 优先同 _gemini_text;返回 {answer, sources} 或 {}。"""
    keys = _gemini_keys(model or _GEMINI_MODEL)
    mdl = _variant_paid(model or _GEMINI_MODEL)[0]
    if not keys:
        return {}
    _sys = ("联网搜索,然后只输出一个 JSON 对象(禁止其他任何文字/代码块标记):\n"
            '{"kind":"weather|news|fact|general","title":"简短标题",\n'
            ' "data": 按 kind 选一种——\n'
            '   weather: {"loc":"地点","date":"日期","cond":"天气现象","lo":最低温数字,"hi":最高温数字,"precip":降水概率数字,"tip":"一句出行建议"}\n'
            '   news: {"items":[{"t":"标题","s":"一句话摘要","src":"来源名"}]}  (最多5条)\n'
            '   fact: {"answer":"直接结论(一句)","detail":"补充说明(一两句)"}\n'
            '   general: {"text":"综合回答(200字内)"}\n'
            ' "brief":"给语音助手的一句话概况,用户的语言"}\n'
            "kind 选最贴合查询意图的;数字字段用数字不要带单位。")
    body = {"contents": [{"role": "user", "parts": [{"text": query}]}],
            "tools": [{"google_search": {}}],
            "systemInstruction": {"parts": [{"text": _sys}]},
            # 70b(2026-07-17 实验修订):关 thinking 的两个旧理由在 3.5 上都不复现——①thought 内容默认根本
            # 不回传(仅 thoughtsTokenCount 计数;下面解析还留了 thought 过滤双保险)②思考 tok 已不挤占输出预算
            # (实测 879 思考+138 输出在 max900 下 finish=STOP 完整)。维持关闭的**现理由**:浅归纳质量无差,
            # 开了每次慢 2~4s + 多烧 ~1k tok,不划算。当年"泄漏"疑为 2.x 行为/解析未滤(用户点破,实验证实)。
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 900, "thinkingConfig": {"thinkingBudget": 0}}}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{mdl}:generateContent?key="
    for key, tier in keys:
        try:
            import requests
            r = requests.post(url + key, json=body, timeout=timeout)
        except Exception:
            continue
        if r.status_code == 200:
            j = r.json(); us = _gemini_usage(j)
            _gemini_log("assistant:websearch", 200, mdl, us["total"], us["prompt"], us["out"], tier)
            cand = (j.get("candidates") or [{}])[0]
            text = "".join(pt.get("text", "") for pt in (cand.get("content") or {}).get("parts", [])
                           if not pt.get("thought")).strip()
            srcs = []
            for ch in ((cand.get("groundingMetadata") or {}).get("groundingChunks") or []):
                w = ch.get("web") or {}
                if w.get("uri") and all(x["url"] != w["uri"] for x in srcs):
                    srcs.append({"title": (w.get("title") or "")[:120], "url": w["uri"]})
            if not text:
                return {}
            card = None
            try:   # 70:结构化输出(kind/title/data/brief)——parse 失败回退纯文本(general)
                m = re.search(r"\{[\s\S]*\}", text)
                if m:
                    c0 = json.loads(m.group(0))
                    if isinstance(c0, dict) and c0.get("kind") and isinstance(c0.get("data"), dict):
                        card = {"kind": str(c0["kind"])[:16], "title": str(c0.get("title") or "")[:80],
                                "data": c0["data"], "brief": str(c0.get("brief") or "")[:300], "sources": srcs[:4]}
            except Exception:
                card = None
            return {"card": card, "answer": text[:1500], "sources": srcs[:5]}
        _gemini_log("assistant:websearch", r.status_code, mdl, tier=tier)
        if r.status_code in (429, 403):
            _gemini_cooldown(tier, _retry_after(r.text))
        continue
    return {}


def _gemini_vision(prompt, images, max_tokens=1500, timeout=90, model=None, max_images=3):
    """Gemini 看图出文字描述。免费 key 优先,**任何失败(限流/5xx/网络/不支持)都自动切付费**。images=[{media_type,b64}]。付费也失败/空 → None(才回退 Claude)。"""
    if not images:
        return None
    keys = _gemini_keys(model or _GEMINI_MODEL)          # '@paid' 后缀在 _gemini_keys 内消化(跳过 free)
    mdl = _variant_paid(model or _GEMINI_MODEL)[0]       # URL/记账/标记一律用裸型号
    if not keys:
        return None
    parts = [{"text": prompt}]
    for v in images[:max(1, int(max_images))]:
        parts.append({"inlineData": {"mimeType": v.get("media_type", "image/png"), "data": v["b64"]}})
    gc = {"temperature": 0.3, "maxOutputTokens": max_tokens}
    if "pro" not in mdl:   # Pro 是 thinking-only,thinkingBudget=0 会 400
        gc["thinkingConfig"] = {"thinkingBudget": 0}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{mdl}:generateContent?key="
    for key, tier in keys:
        try:
            import requests
            r = requests.post(url + key, json={"contents": [{"parts": parts}], "generationConfig": gc}, timeout=timeout)
        except Exception:
            continue   # 网络异常 → 试下一把 key(免费断了也要给付费机会),别直接回退 Claude
        if r.status_code == 200:
            j = r.json(); us = _gemini_usage(j)
            _gemini_log("assistant:vision", 200, mdl, us["total"], us["prompt"], us["out"], tier)
            cand = (j.get("candidates") or [{}])[0]
            out = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []))
            return out.strip() or None
        _gemini_log("assistant:vision", r.status_code, mdl, tier=tier)
        if _is_model_unsupported(r.status_code, r.text):
            _mark_unsupported(tier, mdl)   # 这把永久不支持该模型 → 记住
        elif r.status_code in (429, 403):
            _gemini_cooldown(tier, _retry_after(r.text))   # 额度限流 → 临时冷却整把
        # 5xx 临时高负载 / 400 构造错 → 不冷却整把,直接试下一把 key
        continue   # 任何非 200 都试下一把 key(免费不行→付费);全失败才 None→回退 Claude
    return None


def _logged_in() -> bool:
    return bool(session.get("user_id"))


def _pdf():
    import pdf_reader
    return pdf_reader


# ── 对话服务端持久化(跨设备,可手动清零)。state/assistant-convo/<user_id>.json ──
_CONVO_DIR = CLAUDE_DIR / "state" / "assistant-convo"
_convo_lock = threading.Lock()

# ── 创造物库 Creation Store(用户设计,业界=summary-plus-handle / just-in-time context)──
#   所有**非操作型**工具的最终结果 = 一个创造物:{id(时间戳), kind, brief 告知, query, content|ref, anchor, ts}。
#   工具回复附「告知+句柄」;上下文只注入**最近创造物清单**;AI 用 recall_creation(id) 按需取回全文。
#   铁律(业界共识):①句柄无损保留 ②省略显式告知 ③取回=agent 主动工具调用。设计:references/creation-store.md
_CREATIONS_DIR = CLAUDE_DIR / "state" / "assistant-creations"
_creations_lock = threading.Lock()


def _creations_load(uid):
    try:
        return json.loads((_CREATIONS_DIR / f"{uid}.json").read_text("utf-8"))
    except Exception:
        return []


def _creation_add(uid, kind, brief, query=None, content=None, ref=None, anchor=None):
    """登记一个创造物,返回 id。content=直接存的结果文本;ref=引用型(纸/报告,本体在各自 sidecar 不复制)。"""
    uid = str(uid or "")
    if not uid or not kind:
        return None
    try:   # 沙盒测试产物不入册(否则测试越勤,真实学习记忆被挤出 40 条环越狠;实测已混入「演示小测」等)
        _rf = str(((ref or {}).get("file")) or ((anchor or {}).get("file")) or "")
        if "/.sandbox/" in _rf or _rf.startswith("资源/uploads/.sandbox/"):
            return None
    except Exception:
        pass
    cid = "c_" + format(int(time.time()), "x") + "_" + os.urandom(2).hex()
    with _creations_lock:
        lst = _creations_load(uid)
        lst.append({"id": cid, "kind": kind, "brief": str(brief or "")[:200],
                    "query": (query if isinstance(query, (dict, str)) else None),
                    "content": (str(content)[:8000] if content else None),
                    "ref": (ref if isinstance(ref, dict) else None),
                    "anchor": (anchor if isinstance(anchor, dict) else None), "ts": int(time.time())})
        lst = lst[-40:]
        _CREATIONS_DIR.mkdir(parents=True, exist_ok=True)
        (_CREATIONS_DIR / f"{uid}.json").write_text(json.dumps(lst, ensure_ascii=False), "utf-8")
    return cid


def _creation_rel_time(ts):
    d = int(time.time()) - int(ts or 0)
    if d < 90: return "刚刚"
    if d < 3600: return f"{d // 60}分钟前"
    if d < 86400: return f"{d // 3600}小时前"
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def _creation_paper_state(c):
    """paper 条目的实时状态(未检查/已检查)——从纸的 sidecar 现查,不存快照。"""
    try:
        import pdf_reader as P
        for it in P._upages_load((c.get("ref") or {}).get("file") or ""):
            if it.get("id") == (c.get("ref") or {}).get("upage"):
                return ("已检查 " + (it.get("check_score") or "")) if (it.get("result_md") or "").strip() else "未检查"
    except Exception:
        pass
    return ""


def _creations_recent_line(uid, n=6):
    """给系统提示的**最近创造物清单**(纸/报告优先保留,其余同 kind 去重,共 n 条)。"""
    lst = _creations_load(uid)
    if not lst:
        return ""
    pri = [c for c in reversed(lst) if c.get("kind") in ("paper", "check_report")]
    rest, seen = [], set()
    for c in reversed(lst):
        if c.get("kind") in ("paper", "check_report"):
            continue
        if c.get("kind") in seen:
            continue
        seen.add(c.get("kind"))
        rest.append(c)
    picked = (pri[:4] + rest)[:n]
    picked.sort(key=lambda c: -(c.get("ts") or 0))
    rows = []
    for c in picked:
        st = _creation_paper_state(c) if c.get("kind") == "paper" else ""
        rows.append(f"#{c['id']} {c.get('brief', '')}" + (f"({st})" if st else "") + f" · {_creation_rel_time(c.get('ts'))}")
    return "\n".join(rows)


def _t_recall_creation(args, ctx):
    """取回一个**创造物**的完整内容(之前某次工具产出:练习纸/检查报告/联网搜索/找到的视频/翻译/章节总结/后台任务结果)。
    上下文『最近创造物』清单里的 #id 就是句柄;用户提到『刚才查的/搜的/那张纸/那个结果』→ 调我。
    args {id?: 句柄; kind?: paper/check_report/web_search/search_video/translate/summarize_section/cli_task; query?: 描述模糊}:都不传=最近一条。"""
    uid = str(ctx.get("_uid") or ctx.get("uid") or "")
    lst = _creations_load(uid)
    if not lst:
        return {"error": "还没有任何创造物记录"}
    cid = (args.get("id") or "").strip().lstrip("#")
    kind = (args.get("kind") or "").strip()
    q = (args.get("query") or "").strip()
    cand = list(reversed(lst))
    if cid:
        cand = [c for c in cand if c.get("id") == cid] or cand
    if kind:
        cand = [c for c in cand if c.get("kind") == kind] or cand
    if q:
        cand = [c for c in cand if q in (c.get("brief") or "") or (isinstance(c.get("query"), str) and q in c["query"])] or cand
    cand.sort(key=lambda c0: 0 if c0.get("kind") == "paper" else 1)   # 命中多条→纸优先(纸=本体,报告是它的侧面且随纸附带)
    c = cand[0]
    out = {"id": c["id"], "kind": c.get("kind"), "brief": c.get("brief"),
           "time": _creation_rel_time(c.get("ts")), "anchor": c.get("anchor")}
    ref = c.get("ref") or {}
    if c.get("kind") == "paper" and ref.get("upage"):   # 纸:题目+标准答案(实时读 sidecar)+ 最近检查报告(=纸的侧面)
        try:
            out["纸的内容"] = _upage_read_text(ref.get("file") or "", ref.get("page") or 0) or "(纸还没内容)"
        except Exception:
            out["纸的内容"] = "(读取失败)"
        try:
            import pdf_reader as P
            for it in P._upages_load(ref.get("file") or ""):
                if it.get("id") == ref.get("upage") and it.get("check_name"):
                    r0, _ = _find_check_report(uid, it["check_name"])
                    if r0:
                        out["最近检查报告"] = (r0.get("report") or "")[:2500]
                    break
        except Exception:
            pass
        out["note"] = "题目/标准答案就在上面,据此直接答;报告没有=还没检查过。"
    elif c.get("kind") == "check_report" and ref.get("name"):
        r0, err = _find_check_report(uid, ref["name"])
        out["报告"] = (r0.get("report") or "")[:3500] if r0 else (err or {}).get("error", "(报告丢失)")
    else:
        out["内容"] = c.get("content") or "(这条没有存正文)"
    return out
#   (用户设计:跟笔迹一样"只告知存在,被问到再调工具看",别把全文塞进每轮上下文。)
_CHECK_DIR = CLAUDE_DIR / "state" / "reader-check-reports"
_check_lock = threading.Lock()


def _check_reports_load(uid):
    try:
        return json.loads((_CHECK_DIR / f"{uid}.json").read_text("utf-8"))
    except Exception:
        return []


def _save_check_report(uid, name, file_rel, report, score="", src_page=None, lookups=None, node_results=None):
    """登记一份检查报告,返回**最终报告名**(同名已存在就加序号去重,便于按名精确查)。
    src_page=这张纸参考的**源书页(印刷页)**——题目自制、书里无逐字题,但知识点在这页附近,供按需读原文。
    lookups=造纸 CLI 当时的查找类查询 [{tool,arg},…](读了第几页/搜了什么)——比 src_page 更精确的 provenance,
    后续 AI 可**原样复用这些查询**去书里找知识点(用户设计:工具返回自带查询履历,替代零散上下文注入)。"""
    uid = str(uid or "")
    name = (name or "练习纸检查").strip() or "练习纸检查"
    if not uid or not (report or "").strip():
        return name
    with _check_lock:
        lst = _check_reports_load(uid)
        existing = {x.get("name") for x in lst}
        final = name
        if final in existing:                     # 同名去重:《X》《X (2)》《X (3)》…
            i = 2
            while f"{name} ({i})" in existing:
                i += 1
            final = f"{name} ({i})"
        import hashlib
        rid = hashlib.sha1(f"{final}|{int(time.time())}".encode("utf-8")).hexdigest()[:8]
        _sb = "/.sandbox/" in (file_rel or "")   # 沙盒测试报告:照存(工具测试要能读到)但打标,不当「最近一份」
        lst.append({"id": rid, "name": final, "file": file_rel or "", "score": score or "",
                    "report": report, "src_page": (int(src_page) if src_page else None), "sandbox": _sb,
                    "lookups": (lookups[:8] if isinstance(lookups, list) else None),
                    "node_results": (node_results or None), "ts": int(time.time())})
        lst = lst[-60:]                           # 每人最多留 60 份
        _CHECK_DIR.mkdir(parents=True, exist_ok=True)
        (_CHECK_DIR / f"{uid}.json").write_text(json.dumps(lst, ensure_ascii=False, indent=1), "utf-8")
    try:   # 创造物库:检查报告入册(ref 按名引用报告库;「记忆」开关=dictation_grade)
        if not _creation_enabled(uid, "dictation_grade"):
            raise RuntimeError("off")
        _creation_add(uid, "check_report", "检查了《%s》%s" % (final, ("得分 " + score) if score else ""),
                      ref={"name": final}, anchor={"file": file_rel})
    except Exception:
        pass
    return final


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


def _convo_upsert_turn(uid, turn_id: str, content: str, meta: dict):
    """141(轮次容器):**一个用户轮 = 历史里恰好一条助手消息**,按 turn_id 覆盖而不是追加。

    ⚠ 为什么必须这样(用户实测:刷新后每条回答渲染两遍、天气卡丢失):
      手动放行闸(133)之后,一个用户轮里天然有**多个 response**
      (「我去查一下，稍等」+function_call 是一个;拿到工具结果后的正答是另一个)。
      而前端落库挂在 `response.done` 上 → **一轮落两条库**,每条各带当时的 parts 快照:
        · 回放时同一轮被渲染两遍;
        · 而且第一条快照里还没有工具结果/结果卡 → 卡片丢失。
      按 turn_id 覆盖后,这一轮无论几个 response,历史里始终只有一条、且永远是最新的完整 parts。
    """
    with _convo_lock:
        msgs = _convo_load(uid)
        rec = None
        for m in reversed(msgs):
            if m.get("role") == "assistant" and m.get("turn_id") == turn_id:
                rec = m
                break
        if rec is None:
            return False
        if content:
            rec["content"] = content
        rec["ts"] = int(time.time())
        for k in ("parts", "card", "clip", "trace", "videos", "undo_cards"):
            v = (meta or {}).get(k)
            if v:
                rec[k] = v
        try:
            _CONVO_DIR.mkdir(parents=True, exist_ok=True)
            p = _convo_path(uid)
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(json.dumps(msgs, ensure_ascii=False), "utf-8")
            tmp.replace(p)
        except Exception:
            pass
        return True


_CONVO_ARCHIVE_DIR = CLAUDE_DIR / "state" / "assistant-convo-archive"


def _convo_archive(uid, msgs):
    """把即将丢失的对话消息归档成**纯文本行**(jsonl,append-only):
    用户设计——对话删了/被截断了,文本要留(AI 能查很久以前的询问 + 注意力画像 --rebuild 不丢源),
    图/语音等媒体不进档;180 天后由画像聚合器裁剪(attention_profile.ARCHIVE_KEEP_D)。"""
    try:
        keep = []
        for m in msgs or []:
            if not isinstance(m, dict) or not (m.get("content") or "").strip():
                continue
            keep.append({k: m.get(k) for k in ("role", "content", "ts", "page", "file_rel", "book", "via")
                         if m.get(k) is not None})
        if not keep:
            return
        _CONVO_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        with open(_CONVO_ARCHIVE_DIR / f"{uid}.jsonl", "a", encoding="utf-8") as f:
            for m in keep:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _convo_drop_media(uid, msgs):
    """删除对话时级联清媒体文件(用户设计:文本留档,语音等媒体跟对话一起消失)。目前=语音录音 clip。"""
    try:
        for m in msgs or []:
            cid = (m or {}).get("clip")
            if cid:
                for f in _CLIP_DIR.glob(str(cid) + ".*"):
                    try:
                        f.unlink()
                    except Exception:
                        pass
    except Exception:
        pass


def _convo_append(uid, role, content, meta=None):
    with _convo_lock:
        msgs = _convo_load(uid)
        rec = {"role": role, "content": content, "ts": int(time.time())}
        if meta:   # 记每轮所在位置(书/页/选中句/用过的图)+ 助手回答的调用轨迹 trace + 搜到的视频,让历史回看也能显示上下文卡片 / 感叹号步骤 / 视频卡
            # ⚠ 白名单:没列进来的 meta 字段会被**静默丢掉**。141 的 parts 忘了加就等于没落库。
            for k in ("page", "pages", "book", "file_rel", "selection", "figures", "trace", "videos", "undo_cards", "via", "clip", "card", "parts", "turn_id"):   # clip=语音录音;card=87 结构化卡;parts/turn_id=141 轮次容器
                v = meta.get(k)
                if v:
                    rec[k] = v
        msgs.append(rec)
        try:
            if len(msgs) > 200:
                _convo_archive(uid, msgs[:-200])   # 被截掉的最旧消息 → 纯文本归档(媒体文件不删:消息还可能在别处引用?不——截断即永别,同清空一致)
                _convo_drop_media(uid, msgs[:-200])
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
                try:
                    _old = json.loads(p.read_text("utf-8"))
                except Exception:
                    _old = []
                _convo_archive(uid, _old)      # 🗑 清空:文本留档(180 天),语音等媒体立即删(用户设计)
                _convo_drop_media(uid, _old)
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


def _claude_text(prompt, model="opus", effort="high", timeout=150):
    """单次 Claude 文本生成(剥壳进程)。失败/空 → None。"""
    p = _spawn(effort=(effort if effort in _EFFORTS else "high"),
               model=(model if model in _CLAUDE_VARIANTS else "opus"))
    if not p:
        return None
    try:
        return _send(p, prompt, timeout=timeout)
    finally:
        _kill(p)


_CODEX_RC_HOME = Path("~/.reader-codex/home").expanduser()    # 阅读器专用干净 CODEX_HOME(GPT 建议实测:关掉全部 agent 周边)
_CODEX_RC_CWD = Path("~/.reader-codex/empty").expanduser()    # 空 untrusted cwd(避免项目发现干扰)
_CODEX_RC_CONFIG = """model = "gpt-5.6-luna"
model_reasoning_effort = "low"
approval_policy = "never"
sandbox_mode = "read-only"
check_for_update_on_startup = false
web_search = "disabled"
history.persistence = "none"
[features]
apps = false
hooks = false
goals = false
memories = false
multi_agent = false
remote_plugin = false
shell_tool = false
shell_snapshot = false
unified_exec = false
personality = false
[apps._default]
enabled = false
[projects."%s"]
trust_level = "untrusted"
""" % _CODEX_RC_CWD


def _codex_rc_bootstrap():
    """自举阅读器专用 Codex 环境:干净 CODEX_HOME(auth 从 ~/.codex 拷)+ 精简 config + 空 cwd。
    实测效果(2026-07-11):thread/start 0.8s→0.05s、turn→首delta 3.3-4.7s→1.4-1.8s、端到端 ~5s→~1.7s
    (MCP/Apps/Skills 等 agent 周边初始化全砍;⚠ [mcp_servers.X] enabled 覆盖语法非法会整份配置回默认,
    features.fast_mode 键同样非法——改配置后必须看 configWarning)。"""
    try:
        _CODEX_RC_CWD.mkdir(parents=True, exist_ok=True)
        _CODEX_RC_HOME.mkdir(parents=True, exist_ok=True)
        os.chmod(_CODEX_RC_HOME, 0o700)
        auth = _CODEX_RC_HOME / "auth.json"
        src = Path("~/.codex/auth.json").expanduser()
        if not auth.exists() and src.exists():
            auth.write_bytes(src.read_bytes())
            os.chmod(auth, 0o600)
        cfg = _CODEX_RC_HOME / "config.toml"
        if not cfg.exists():
            cfg.write_text(_CODEX_RC_CONFIG, "utf-8")
    except Exception as ex:
        sys.stderr.write(f"[codex-rc bootstrap] {ex}\n")


class _CodexApp:
    """codex app-server 常驻客户端(JSON-RPC over stdio,官方说明书 2026-07 + 本机实测 schema):
    单例常驻,进程死亡自动重启;每次调用开 **ephemeral thread**(不落盘,任务间零污染),事件按
    threadId 路由,支持并发。跑在**独立干净 CODEX_HOME**(_codex_rc_bootstrap)+ 空 untrusted cwd:
    agent 周边(Apps/MCP/Hooks/Shell/Multi-agent)全关 → 端到端 ~1.7s(原 ~5s)。
    sandbox=read-only + approvalPolicy=never(只当纯文本/看图模型,不让它当 agent)。"""

    def __init__(self):
        self._lk = threading.Lock()
        self._p = None
        self._rid = 100
        self._pending = {}   # rpc id -> Queue(响应)
        self._turns = {}     # threadId -> Queue(事件流)

    def _ensure(self):
        with self._lk:
            if self._p and self._p.poll() is None:
                return
            import shutil as _sh
            cx = _sh.which("codex") or os.environ.get("APP_CODEX") or "codex"
            _codex_rc_bootstrap()
            self._pending = {}; self._turns = {}
            self._p = subprocess.Popen([cx, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                       cwd=str(_CODEX_RC_CWD),
                                       env={**os.environ, "CODEX_HOME": str(_CODEX_RC_HOME)})
            threading.Thread(target=self._reader, args=(self._p,), daemon=True).start()
        self._rpc("initialize", {"clientInfo": {"name": "bwicarus-webapp", "title": "assistant", "version": "1"}}, timeout=15)
        self._notify("initialized", {})

    def _reader(self, p):
        try:
            for line in p.stdout:
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                if "id" in j and ("result" in j or "error" in j):
                    q = self._pending.pop(j["id"], None)
                    if q:
                        q.put(j)
                else:
                    tid = (j.get("params") or {}).get("threadId")
                    q = self._turns.get(tid)
                    if q:
                        q.put(j)
        finally:   # 进程退出:唤醒所有等待者(拿到 None 即知连接没了)
            for q in list(self._pending.values()):
                q.put(None)
            for q in list(self._turns.values()):
                q.put(None)

    def _send(self, obj):
        with self._lk:
            self._p.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
            self._p.stdin.flush()

    def _notify(self, method, params):
        self._send({"method": method, "params": params})

    def _rpc(self, method, params, timeout=20):
        import queue as _qu
        q = _qu.Queue()
        with self._lk:
            self._rid += 1
            rid = self._rid
            self._pending[rid] = q
        self._send({"method": method, "id": rid, "params": params})
        try:
            r = q.get(timeout=timeout)
        except Exception:
            self._pending.pop(rid, None)
            raise RuntimeError(f"codex rpc {method} 超时")
        if r is None:
            raise RuntimeError("codex app-server 进程退出")
        if r.get("error"):
            raise RuntimeError(str(r["error"])[:200])
        return r.get("result") or {}

    # ── 多轮原语(㉖ 编排接入):thread_start → N× turn_stream(同一 threadId,**服务端保存历史**,
    #    每轮只发新内容——与 Anthropic 前缀缓存同解,不重拼历史)→ thread_close。ephemeral=不落盘,
    #    thread 存活于 app-server 进程内存,多 turn 可续(冒烟验证)。──
    def thread_start(self, model=""):
        import queue as _qu
        self._ensure()
        tp = {"cwd": str(_CODEX_RC_CWD), "approvalPolicy": "never", "sandbox": "read-only", "ephemeral": True}
        if model and str(model).startswith("gpt-"):
            tp["model"] = model
        th = self._rpc("thread/start", tp, timeout=25)
        tid = th["thread"]["id"]
        self._turns[tid] = _qu.Queue()
        return tid

    def turn_stream(self, tid, text, effort="medium", timeout=180, image_paths=None):
        """在既有 thread 上追加一轮,yield 文字 delta;失败/超时抛异常。"""
        q = self._turns.get(tid)
        if q is None:
            raise RuntimeError("codex thread 不存在或已关闭")
        inp = [{"type": "text", "text": text}]
        for ip in (image_paths or [])[:3]:
            inp.append({"type": "localImage", "path": ip})
        args = {"threadId": tid, "input": inp}
        if effort in _CODEX_DEPTHS:
            args["effort"] = effort
        self._rpc("turn/start", args, timeout=25)
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                try:
                    self._notify("turn/interrupt", {"threadId": tid})
                except Exception:
                    pass
                raise RuntimeError("codex turn 超时")
            try:
                ev = q.get(timeout=min(left, 30))
            except Exception:
                continue
            if ev is None:
                raise RuntimeError("codex app-server 连接断开")
            m = ev.get("method")
            if m == "item/agentMessage/delta":
                d = (ev.get("params") or {}).get("delta") or ""
                if d:
                    yield d
            elif m == "turn/completed":
                st = ((ev.get("params") or {}).get("turn") or {}).get("status")
                if st != "completed":
                    raise RuntimeError(f"codex turn {st}")
                return
            elif m == "error":
                raise RuntimeError(str((ev.get("params") or {}).get("error"))[:200])

    def thread_close(self, tid):
        self._turns.pop(tid, None)

    def stream(self, prompt, model="", effort="medium", timeout=180, image_paths=None):
        """单轮便捷入口(开 thread→一轮→关):yield 文字 delta;失败抛异常(调用方回落 exec/其它后端)。"""
        tid = self.thread_start(model)
        try:
            yield from self.turn_stream(tid, prompt, effort, timeout, image_paths)
        finally:
            self.thread_close(tid)

    def ask(self, prompt, model="", effort="medium", timeout=180, image_paths=None):
        return "".join(self.stream(prompt, model, effort, timeout, image_paths)).strip() or None


_codex_app = _CodexApp()


def _codex_text(prompt, model="gpt-5.6-luna", effort="low", timeout=180, image_paths=None):
    """单次 Codex 生成:**主路=常驻 app-server**(零启动开销+ephemeral thread);失败回落 `codex exec`
    一次性(启动慢但独立健壮)。Pi 已登录 ChatGPT 订阅——额度与 Claude/Gemini 独立。失败/空 → None。"""
    try:
        t = _codex_app.ask(prompt, model=model, effort=effort, timeout=timeout, image_paths=image_paths)
        if t:
            return t
    except Exception as ex:
        sys.stderr.write(f"[codex-app] {str(ex)[:120]} → 回落 exec\n")
    return _codex_exec_text(prompt, model=model, effort=effort, timeout=timeout, image_paths=image_paths)


def _codex_exec_text(prompt, model="gpt-5.5", effort="medium", timeout=180, image_paths=None):
    """兜底:`codex exec` 一次性无头(app-server 挂/协议变时的独立退路)。"""
    import shutil as _sh, tempfile as _tf
    cx = _sh.which("codex") or os.environ.get("APP_CODEX") or "codex"
    of = _tf.NamedTemporaryFile(prefix="codex-out-", suffix=".txt", delete=False)
    of.close()
    try:
        cmd = [cx, "exec", "--skip-git-repo-check",
               "-m", (model if str(model).startswith("gpt-") else "gpt-5.5-codex"),
               "-c", 'model_reasoning_effort="%s"' % (effort if effort in ("low", "medium", "high", "xhigh") else "high"),
               "-c", 'sandbox_mode="read-only"',
               "-o", of.name]
        for ip in (image_paths or [])[:3]:
            cmd += ["-i", ip]
        cmd.append(prompt)
        subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd="/tmp")
        txt = Path(of.name).read_text("utf-8").strip()
        return txt or None
    except Exception:
        return None
    finally:
        try:
            Path(of.name).unlink()
        except Exception:
            pass


def _deep_ask(prompt, backend="gemini", variant=None, depth="think", timeout=150):
    """一次性深度生成(总结/深度解释等)。按 (backend,variant,depth) 选后端;主后端失败/空 → 另一后端兜底
    (Gemini 省 Claude 额度;互为兜底保证不因一边断而挂)。返回文本或 None。"""
    def via_gemini():
        return _gemini_text(prompt, max_tokens=4000, think=(depth != "none"),
                            timeout=min(timeout, 100),
                            model=(variant if _is_gemini(variant) else None))
    def via_claude():
        return _claude_text(prompt, model=(variant if variant in _CLAUDE_VARIANTS else "opus"),
                            effort=(depth if depth in _EFFORTS else "high"), timeout=timeout)
    def via_codex():
        return _codex_text(prompt, model=variant, effort=depth, timeout=timeout)
    if backend == "claude":
        return via_claude() or via_gemini()
    if backend == "codex":
        return via_codex() or via_gemini() or via_claude()
    return via_gemini() or via_claude()


_VIS_SYS = ("你看到的是 PDF 页面/插图的渲染图。用简洁中文描述图里**文字层读不到的视觉内容**"
            "(图表/示意图/曲线/电路/几何/物理装置/数据表/公式排版/手写批注/版面结构):它画的是什么、"
            "关键要素/结构/数值、在表达什么。抓重点、别冗长、别复述能从文字层读到的普通段落。数学用 $...$。")

def _vision_describe(images, note="", backend="gemini", variant=None, depth="think", timeout=90):
    """对一组渲染图做**一次精简视觉调用**返回纯文字描述。按 (backend,variant,depth) 选后端,主后端失败→另一后端兜底。
    关键省 token:图只在这个**一次性进程**里看一眼 → 主编排循环全程**只走文字、永不背图**。images=[{media_type,b64}]。"""
    if not images:
        return None
    nt = note or "描述这些图里文字层读不到的内容。"
    def via_gemini():
        return _gemini_vision(_VIS_SYS + "\n" + nt, images, timeout=min(timeout, 80),
                              model=(variant if _is_gemini(variant) else None))
    def via_claude():
        p = _spawn(effort=(depth if depth in _EFFORTS else "low"),
                   model=(variant if variant in _CLAUDE_VARIANTS else "sonnet"), system=_VIS_SYS)
        if not p:
            return None
        try:
            blocks = [{"type": "text", "text": nt}]
            for v in images[:3]:
                blocks.append({"type": "image", "source": {"type": "base64",
                              "media_type": v.get("media_type", "image/png"), "data": v["b64"]}})
            return _send(p, blocks, timeout=timeout)
        finally:
            _kill(p)
    def via_codex():
        import base64 as _b64, tempfile as _tf
        paths = []
        try:
            for v in images[:3]:
                f = _tf.NamedTemporaryFile(prefix="cxi-", suffix=".png", delete=False)
                f.write(_b64.b64decode(v["b64"])); f.close(); paths.append(f.name)
            return _codex_text(_VIS_SYS + "\n" + nt, model=variant, effort=depth,
                               timeout=timeout, image_paths=paths)
        finally:
            for p2 in paths:
                try:
                    Path(p2).unlink()
                except Exception:
                    pass
    if backend == "claude":
        return via_claude() or via_gemini()
    if backend == "codex":
        return via_codex() or via_gemini() or via_claude()
    return via_gemini() or via_claude()


def _vision_for(ctx, images, note=""):
    """看图:按用户对 vision 动作的预设(后端/型号/深度)调 _vision_describe。"""
    r = _resolve("vision", (ctx or {}).get("_uid"))
    if _paid_recover_check((ctx or {}).get("_uid"), "vision"):   # @paid 且免费恢复 → 摘除后重读(静默)
        r = _resolve("vision", (ctx or {}).get("_uid"))
    return _vision_describe(images, note, backend=r["backend"], variant=r["variant"], depth=r["depth"])


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
                    o = json.loads(ln)
                    u = o.get("usage") or {}
                    if u:   # claude 叶子调用(总结/看图走 claude 时)的 token → 累计进本轮
                        _tok_add(sum(int(u.get(k, 0) or 0) for k in
                                 ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")))
                    return (o.get("result") or "").strip()
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
                    u = o.get("usage") or {}
                    if u:   # 本轮 claude 编排 token(输入+输出+缓存读/写)→ 累计进本轮
                        _tok_add(sum(int(u.get(k, 0) or 0) for k in
                                 ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")))
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
    """近上限时回一句告警字符串(给前端 notice 事件);正常/查询失败/快照过期都回 None。非阻塞。
    只在**实际跑 Claude 的编排路径**(_agent_run_claude/_eagent_claude)注入——这是 Claude Code 的额度,
    Gemini 编排路径提它纯属噪音(尤其 Claude 100% 而用户根本没在用它时)。"""
    _quota_ensure_loop()
    txt = _quota_note["text"]
    return txt if (txt and time.time() - _quota_note["at"] < 1800) else None


def _paid_fallback_note(model, action, depth):
    """免费 Gemini 本是首选、这次却实际用了付费 key → 给前端一条可操作提示(SSE 'gemini-paid' 事件,
    前端渲提示条 + 一键「以后直接用付费」= 把该 action 的 variant 存成 '<型号>@paid')。
    不算「意外切付费」的情形一律回 None 不提示:用户已选 @paid 直连 / 型号本就仅付费 / 根本没配免费 key。"""
    bare, paid_first = _variant_paid(model or "")
    if paid_first or not bare or _is_paid_only(bare):
        return None
    try:
        if not _GEMINI_KEY_FILES[0][1].read_text().strip():   # 免费 key 没配 → 付费是唯一选择,不提示
            return None
    except Exception:
        return None
    return {"text": "免费 Gemini 额度受限,本次已自动使用付费档。",
            "action": action, "variant": bare, "paid_variant": bare + "@paid",
            "depth": depth if depth in ("none", "think") else "think"}


# ── Gemini 免费额度恢复自动切回(设计:references/reader-userpages-favorites.md 附录)──
# 用户点过「以后直接用付费」→ action-pref 存 '<型号>@paid' 永久直连付费。这里在调用路径上**节流探测**
# 免费档是否恢复:恢复 → 自动摘掉 @paid(存回普通 variant,本次请求重新 _resolve 即刻生效)+ 返回提示 dict
# (调用方经 SSE 'gemini-paid' 通道渲「✅已切回免费」;无该通道的调用方忽略返回值,静默切回)。
_PAID_PROBE_FILE = CLAUDE_DIR / "state" / "gemini-paid-probe.json"
_PAID_PROBE_INTERVAL = 1800          # 每 30min 至多探测一次(全局一个时间戳,持久化,重启不丢)
_PAID_PROBE_OK_FRESH = 300           # 一次探测成功后 5min 内视为"免费已确认恢复":其它 @paid action 不再发请求直接摘除
_paid_probe_lock = threading.Lock()
_paid_probe_mem = {"ts": None, "ok_at": 0.0}   # ts=None 表示还没从盘读


def _paid_probe_due():
    """30min 节流闸。返回 True = 这次轮到探测(窗口已占用:失败也要等下个周期,符合「失败→更新时间戳静默」)。"""
    with _paid_probe_lock:
        if _paid_probe_mem["ts"] is None:
            try:
                _paid_probe_mem["ts"] = float((json.loads(_PAID_PROBE_FILE.read_text("utf-8")) or {}).get("ts") or 0)
            except Exception:
                _paid_probe_mem["ts"] = 0.0
        now = time.time()
        if now - _paid_probe_mem["ts"] < _PAID_PROBE_INTERVAL:
            return False
        _paid_probe_mem["ts"] = now
        try:
            _PAID_PROBE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _PAID_PROBE_FILE.write_text(json.dumps({"ts": now}), "utf-8")
        except Exception:
            pass
        return True


def _paid_recover_check(uid, action):
    """action 预设带 '@paid' 时的免费恢复探测。恢复 → 原地改预设(摘 @paid)+ 返回恢复提示 dict;否则 None。
    探测 = 免费 key 的 1-token generateContent、**3s 超时同步**(30min 至多一次,常态零开销,不拖慢主请求)。
    不用 countTokens:它是独立配额桶,generate 免费额度耗尽时它照样 200 → 必然误判"已恢复"。
    调用方拿到非 None 后应重新 _resolve(action) 让本次请求就用免费。"""
    try:
        pref = _ap_get(uid, action)
        bare, paid = _variant_paid((pref or {}).get("variant") or "")
        if not paid or not _is_gemini(bare) or _is_paid_only(bare):
            return None            # 没设 @paid / 仅付费型号(免费永远不行)→ 不适用
        if time.time() < _gemini_off.get("free", 0):
            return None            # 免费档确认在冷却 = 已知没恢复;不占探测窗口,冷却一过下次就探
        ok_fresh = (time.time() - (_paid_probe_mem.get("ok_at") or 0)) < _PAID_PROBE_OK_FRESH
        if not ok_fresh:
            if not _paid_probe_due():
                return None
            try:
                free_key = _GEMINI_KEY_FILES[0][1].read_text().strip()
            except Exception:
                free_key = ""
            if not free_key:
                return None
            cfg = {"maxOutputTokens": 1}
            if "pro" not in bare:   # Pro 是 thinking-only,thinkingBudget=0 会 400
                cfg["thinkingConfig"] = {"thinkingBudget": 0}
            import requests
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{bare}:generateContent?key=" + free_key,
                json={"contents": [{"parts": [{"text": "hi"}]}], "generationConfig": cfg}, timeout=3)
            _gemini_log("assistant:paid-probe", r.status_code, bare, tier="free")
            if r.status_code != 200:
                if _is_model_unsupported(r.status_code, r.text):
                    _mark_unsupported("free", bare)
                elif r.status_code in (429, 403):
                    _gemini_cooldown("free", _retry_after(r.text))   # 顺手记冷却:窗口内后续调用直接跳过不白探
                return None        # 失败:时间戳已在 _paid_probe_due 更新 → 静默继续付费
            _mark_free_ok(bare)
            _paid_probe_mem["ok_at"] = time.time()   # 免费已确认恢复:短窗内其它 @paid action 免请求直接摘除
        _ap_set(uid, action, "gemini", bare, (pref or {}).get("depth") or "think")
        return {"text": "✅ Gemini 免费额度已恢复,已自动切回免费。", "recovered": True,
                "action": action, "variant": bare}
    except Exception:
        return None


def _recover_gate(gen, pend, uid, action):
    """@paid「免费已恢复」的**真实请求裁决**(2026-07-03,修矛盾双提示):
    _paid_recover_check 的 1-token 探测能过 ≠ 真实请求能过(不同型号配额独立、探测自身还占 RPM 名额),
    曾出现绿条「已恢复切回免费」+黄条「受限已用付费」同屏。故绿条不预发:
    - 流中首个产出类事件(answer/answer-chunk/tool/trace)到达且期间没发生 paid 回退 → 真实请求确实走通
      免费,此刻才补发绿条(出现在回答开头附近);
    - 先出现 paid 回退事件(gemini-paid 且非 recovered)→ 探测误报:回滚该 action 预设为 @paid(维持
      用户「直接用付费」语义,下次不再白试)+ 丢弃绿条;黄条本身照常透传。
    - 流异常提前结束 → 不发绿条(下次探测再验)。"""
    decided = False
    for ev in gen:
        if not decided and isinstance(ev, dict):
            e = ev.get("event")
            d = ev.get("data") if isinstance(ev.get("data"), dict) else {}
            if e == "gemini-paid" and not d.get("recovered"):
                decided = True   # 真实请求回退了付费 → 误报:回滚 @paid,不发绿条
                try:
                    _dep = (_ap_get(uid, action) or {}).get("depth") or "think"
                    _ap_set(uid, action, "gemini", (pend.get("variant") or "") + "@paid", _dep)
                except Exception:
                    pass
            elif e in ("answer", "answer-chunk", "tool", "trace"):
                decided = True   # 走通了免费(回退事件必先于产出) → 现在才宣布恢复
                yield {"event": "gemini-paid", "data": pend}
        yield ev


# ──────────────────────── 本页正文(fitz)────────────────────────
def _overlay_md_for_page(file_rel: str, pdf_page: int) -> str:
    """v4 批次2:插入页 overlay 的文字真源在 sidecar(state/reader-userpages/<sha16>.json),后台同步前
    PDF 那页是空白/旧文字 → get_text 拿不到用户内容。这里对**未同步(脏:md_ver>synced_ver)**的 overlay
    记录取 sidecar md 补上,让 read_page/summarize 等自主路径能拿到用户刚写的内容。同步后(md_ver==synced_ver)
    PDF 已含文字,返回空串不重复补。pdf_page = 1-based PDF 页号。"""
    try:
        import hashlib
        sha = hashlib.sha1((file_rel or "").encode("utf-8")).hexdigest()[:16]
        p = CLAUDE_DIR / "state" / "reader-userpages" / (sha + ".json")
        items = json.loads(p.read_text("utf-8"))
        if not isinstance(items, list):
            return ""
        parts = []
        for it in items:
            if not isinstance(it, dict) or it.get("mode") != "overlay":
                continue
            if it.get("page") != pdf_page:
                continue
            if int(it.get("md_ver", 0) or 0) <= int(it.get("synced_ver", 0) or 0):
                continue   # 已同步:PDF 那页已有文字,不重复补
            t = (it.get("md") or "").strip()
            if t:
                ttl = (it.get("title") or "").strip()
                parts.append((ttl + "\n" + t) if ttl else t)
        return "\n\n".join(parts)
    except Exception:
        return ""


def _web_mat(file_rel):
    """`web:<url>` → {url,title,text}(html_reader 的中间层 resolver);非 web: 返回 None。
    用户实锤 2026-07-19:只在前端把材料标识设成 web: 而后端不认,AI read_page 永远读到空。"""
    if not (isinstance(file_rel, str) and file_rel.startswith("web:")):
        return None
    try:   # html_reader 是模块单例:webapp 启动时 register 已设好 WEB_CACHE_DIR,同进程直取
        import html_reader as _HR
        return _HR.web_material(file_rel)
    except Exception:
        return {"url": file_rel[4:], "title": "", "text": ""}


def _page_text(file_rel: str, page) -> str:
    try:
        _wm = _web_mat(file_rel)
        if _wm is not None:
            return (_wm.get("text") or "")[:4000]   # 网页=单文档,页码无意义
        file_rel, page = _vb_src(file_rel, page)   # 合并书:全局页→真成员局部页
        rel = (file_rel or "").strip()
        if not rel or ".." in rel:
            return ""
        ap = (VAULT_ROOT / rel).resolve()
        ap.relative_to(VAULT_ROOT.resolve())
        if not ap.exists():
            return ""
        if ap.suffix.lower() in (".html", ".htm", ".md", ".markdown"):
            # 网页/HTML 文档(2026-07-19 用户:网页=阅读器的一等信息来源):单文档无页概念,
            # read_page/上下文直塞都取全文纯文本(抓取层已剔噪+白名单消毒,这里只拆标签)
            raw = ap.read_text("utf-8", "ignore")
            if ap.suffix.lower() in (".md", ".markdown"):
                return raw[:4000]
            from bs4 import BeautifulSoup
            return BeautifulSoup(raw, "html.parser").get_text("\n", strip=True)[:4000]
        import fitz
        doc = fitz.open(str(ap))
        try:
            idx = max(0, min(int(page or 1) - 1, doc.page_count - 1))
        finally:
            doc.close()
        # 剔噪后的干净文本(用户拍板:噪声在源头剔,AI 上下文不能是错的——
        # 插图竖线/振假名混排都在 _page_text_clean 里处理,与阅读器字符层同源)
        txt = _pdf()._page_text_clean(ap, rel, idx + 1, limit=4000)
        # 钉在本页的便签/卡片 → 插进绑定对象所在句子末尾(「【便签内容：…】」/「【卡片：…】」,用户设计;
        # assistant/voice/make_anki/read_page 全走这里,一处接入全生效)
        try:
            txt = _pdf()._pin_context_annotations(rel, idx + 1, txt)
        except Exception:
            pass
        # 插入页 overlay 未同步 → PDF 那页空白/旧,用 sidecar md 补真源(设计 v4 批次2 评审 major)
        supp = _overlay_md_for_page(rel, idx + 1)
        if supp:
            txt = (supp + ("\n\n" + txt if txt else "")).strip()
        return txt[:4000]
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


def _figdescs_one(file_rel, printed_pages):
    """单个 PDF 文件内的图描述(局部页)。上层 _figdescs_for 负责按成员切分——别直接调这个。"""
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


def _figdescs_for(file_rel, printed_pages):
    """本书开了『插图描述』时,取这些页的图 caption+desc(纯文本,非视觉)。{视图页: [(cap,desc),…]}。
    这样跨页/图里的结构(如 V 字模型整张图把上下流各阶段都画在图上)用现成描述就能进上下文,不必读视觉。
    **对单本书和合并书是同一条路径**:单本书=只有一个成员、offset 0(用户拍板的统一书模型)。"""
    out = {}
    for _mrel, _moff, _mpgs in _vb_members(file_rel):
        _hi = _moff + (_mpgs or 10 ** 9)
        _loc = [p - _moff for p in printed_pages
                if isinstance(p, (int, float)) and _moff < p <= _hi]
        if not _loc:
            continue
        for lp, v in _figdescs_one(_mrel, _loc).items():
            out[lp + _moff] = v
    return out


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


def _upage_read_text(file_rel, pdf_page):
    """自建页(插入页)的内容:题目原文 + 各空标准答案 +(检查过的话)上次判分。
    read_page 直接读这页会读到**空白 PDF**(插入页文件本身是空白,内容在 overlay sidecar)——
    用户实测:AI read_page 自建页只看到标题、答不出题目的根因。这里改从 sidecar blocks 直接取。"""
    try:
        import pdf_reader as P
        items = [it for it in P._upages_load(file_rel) if int(it.get("page") or 0) == int(pdf_page or 0)]
    except Exception:
        items = []
    if not items:
        return ""
    out = []
    for it in items:
        out.append("【自建页「%s」(用户手写作答页,内容如下,别再找 PDF 正文)】" % (it.get("title") or ""))
        qn = 0
        for b in (it.get("blocks") or []):
            k = b.get("kind")
            if k == "text":
                t = (b.get("text") or "").strip()
                if t:
                    out.append(t)
            elif k == "blank":
                qn += 1
                lab = (b.get("label") or "").strip()
                ans = (b.get("answer") or "").strip()
                out.append("第%d空" % qn + ((" " + lab) if lab else "") + "＿＿"
                           + (("(标准答案:%s)" % ans) if ans else ""))
            elif k == "checkbox":
                out.append("☐ " + (b.get("label") or "").strip())
        rmd = (it.get("result_ai") or it.get("result_md") or "").strip()
        if rmd:
            out.append("〔上次检查/判分〕\n" + rmd[:1500])
    return "\n".join(out)


def _t_read_page(args, ctx):
    # 双页模式下读全部可见页(ctx.pages,PDF 索引),不传 page 时默认所有可见页;
    # 传 page 时那是**印刷页码**(AI/用户语言)→ 转成 PDF 页读。
    file_rel = ctx.get("file_rel", "")
    # 68:参数别名——模型常传复数 pages(照着**返回结构**里的 "pages":[N] 学的,21:05 实锤被静默忽略
    # 回退旧页码=「翻了页还读到上一页」);单数优先,复数取值兜底,别让宽松 schema 静默吞参数
    _pg_arg = args.get("page")
    if not _pg_arg:
        _pl = args.get("pages")
        if isinstance(_pl, list) and _pl:
            _pg_arg = _pl[0]
        elif isinstance(_pl, (int, str)) and str(_pl).strip():
            _pg_arg = _pl
    if _pg_arg:
        try:
            _pg_arg = int(_pg_arg)
        except Exception:
            _pg_arg = 0
    if _pg_arg:
        pages = [_to_pdf(ctx, _pg_arg)]
        want_next = False                              # 显式指定某页 → 只给那页(AI 自己决定要不要再往下)
    else:
        pages = ctx.get("pages") or [ctx.get("page", 0)]
        want_next = True                               # 默认读当前页:顺带把下一页(文字+图描述)带上,省得漏跨页
    # 本页 + 下一页 的图描述一次取(纯文本,非视觉)
    printed = [_to_disp(ctx, p) for p in pages]
    nxt = (max(pages) + 1) if (want_next and pages) else None
    figd = _figdescs_for(file_rel, printed + ([_to_disp(ctx, nxt)] if nxt else []))
    parts = []
    up_hit = False
    for pg in pages:
        up = _upage_read_text(file_rel, pg)   # 自建页:PDF 空白 → 直接读 sidecar blocks(题目+标准答案+上次判分)
        if up:
            parts.append(up); up_hit = True; continue
        b = _read_one(file_rel, ctx, pg, figd)
        if b:
            parts.append(b)
    # 下一页:只给**短预览**(开头 1000 字 + 图描述)——多数问题在本页就答完,下页预览只是「够不够、要不要续读」的线索;
    # 不需要整页(那会让每次 read_page 都多背几千字、推高每题成本)。真要看全下页,AI 再 read_page(page=下页)。
    # 单文档(网页/HTML/MD)没有"下一页"——不给预览,否则会把同一篇全文再贴一遍(实测)
    if nxt and not (str(file_rel).startswith("web:")
                    or str(file_rel).lower().endswith((".html", ".htm", ".md", ".markdown"))):
        nb = _read_one(file_rel, ctx, nxt, figd, cap_txt=1000,
                       label=f"【下一页·第{_to_disp(ctx, nxt)}页(开头预览,要看全文再 read_page 它)】")
        if nb:
            parts.append(nb)
    if not parts:
        return {"error": "这些页没取到文字(可能纯图/未OCR)"}
    result = {"pages": printed, "text": "\n\n".join(parts),
              "页码提醒": "报页码一律用本结果的 pages 字段(系统页,用户界面同款);正文开头/角落出现的数字可能是**原书自印的页码**(与系统页不一致),别抄它报页。"}
    # 自建页(用户手写作答页):再附一张**前端渲染图**(所见即所得,含手写)喂回大脑 —— 前端在场
    #   截了 view_image 才有(_vision 喂模型 + _fed_images 给流程展示);无头/不在视口时纯文字兜底(题目+标准答案已在 text 里)。
    if up_hit and isinstance(ctx.get("view_image"), dict) and ctx["view_image"].get("b64"):
        _vi = ctx["view_image"]
        _fed = [{"media_type": _vi.get("media_type") or "image/jpeg", "b64": _vi["b64"]}]
        result["_vision"] = _fed
        result["_fed_images"] = _fed
        result["看图提示"] = "下图=这张自建页的前端实时截图(题目+用户手写,所见即所得),结合上面文字一起看。"
    return result


def _t_read_selection(args, ctx):
    sel = (ctx.get("selection") or "").strip()
    return {"selection": sel[:4000]} if sel else {"error": "用户当前没有选中文字"}


def _find_check_report(uid, name):
    """按名字(精确→模糊,id 也行)找报告;不传名字=最近一份。返回 (报告 dict, 错误 dict)。"""
    lst = _check_reports_load(uid)
    if not lst:
        return None, {"error": "还没有任何练习纸检查报告(用户做完自制练习纸点『让 AI 检查』后才会生成)"}
    name = (name or "").strip()
    if not name:
        real = [x for x in lst if not x.get("sandbox")]   # 「最近一份」跳过沙盒测试报告(防测试卷顶掉真实成绩)
        return (real[-1] if real else lst[-1]), None
    cand = [x for x in lst if x.get("id") == name or name == (x.get("name") or "")]
    if not cand:
        cand = [x for x in lst if name in (x.get("name") or "") or (x.get("name") or "") in name]
    if not cand:
        return None, {"error": f"没找到叫「{name}」的检查报告", "available": [x.get("name") for x in lst[-8:]]}
    return cand[-1], None


def _t_read_check_report(args, ctx):
    """回答**练习纸检查报告**相关问题(A 折中:默认**同步返回报告内容让你直接答**,追问类小问题秒答、不起重型子 agent;
    只有要**查书核实**知识点时传 verify:true 才起带报告上下文+能查书的子 agent)。
    args {question?: 用户原话问题; name?: 报告名(纸标题,可模糊;不传=最近一份); verify?: true=起查书子 agent}。"""
    uid = str(ctx.get("_uid") or ctx.get("uid") or "")
    r, err = _find_check_report(uid, args.get("name"))
    if err:
        return err
    question = (args.get("question") or args.get("q") or args.get("text") or "").strip()
    verify = bool(args.get("verify") or args.get("deep") or args.get("查书"))
    _sp = r.get("src_page")
    _lks = r.get("lookups") if isinstance(r.get("lookups"), list) else []
    # provenance:题目**自制自**书里的内容 —— 题目书里没有逐字原文,但**知识点在书里**。
    #   优先给**造纸时的原始查询**(lookups:读了第几页/搜了什么)→ AI 直接复用同样的查询去找知识点;
    #   没有 lookups(旧报告)才退回粗粒度的 src_page。
    _verb = {"read_page": "读了第 {} 页", "search_book": "在书里搜了「{}」", "search_in_book": "在书里搜了「{}」",
             "search_all_books": "跨书搜了「{}」", "web_search": "联网搜了「{}」", "lookup_word": "查了词「{}」",
             "see_page": "看了第 {} 页的图", "see_figure": "看了带入的图",
             "summarize_section": "总结了第 {} 页所在章节", "toc": "看了目录", "read_selection": "读了选中内容"}
    _done = []
    for x in _lks[:8]:
        t, a = (x or {}).get("tool"), (x or {}).get("arg")
        tpl = _verb.get(t)
        if not tpl:
            continue
        _done.append(tpl.format(str(a)[:40]) if ("{}" in tpl and a not in (None, "")) else tpl.split("{")[0].rstrip("「 "))
    if _done:   # 有可渲染的查询才走 lookups 分支;渲染不出(全是未知工具)→ 回退 src_page,别给一句空 provenance
        _prov = ("出这张纸时 AI " + "、".join(_done) + "。")
        _prov += ("题目本身书里没有逐字原文,但**它考的知识点就在上面这些查询命中的内容里**——"
                  "用户问『这个知识点/相关原文在书里哪、讲讲原文』时,**复用同样的查询**"
                  "(read_page 同一页 / search_book 同样的关键词)找到书里对应讲解来答;"
                  "**别去找『逐字的题目原文』**(那是纸上自制的,书里没有)。")
    else:
        _prov = (f"这张纸的题目是**根据本书第 {_sp} 页附近的内容自制**的。" if _sp else "这张纸的题目是根据书里某页内容自制的。")
        _prov += ("题目本身书里没有逐字原文,但**它考的知识点在书里**——用户问『这个知识点/相关原文在书里哪、讲讲原文』时,"
                  + (f"就 read_page({_sp}) 读那页、" if _sp else "就 ") + "或 search_book 搜知识点关键词,找到书里对应讲解来答;"
                  "**别去找『逐字的题目原文』**(那是纸上自制的,书里没有)。")
    if verify and question:
        # 需要查书核实 → 起带报告上下文、能自己查书的后台子 agent(产 CLI 卡)
        rr = _resolve("agent", uid)
        instr = (
            f"用户在一张自制练习纸《{r.get('name')}》上作答并已判分。下面是完整**检查报告**"
            f"(题目原文+各空标准答案+用户手写识别+判分)——题目和答案的来源"
            f"(报告已在下面,别再调 read_check_report):\n\n{r.get('report')}\n\n"
            f"【出处】{_prov}\n\n用户的问题:{question}\n\n"
            f"请**紧扣报告**回答;要核对知识点就按上面出处去书里查(read_page/search_book/see_page/lookup_word),"
            f"但**题目与标准答案一律以报告为准**。最后用简明中文给一段可靠、具体的解答。"
        )
        return _bg_task("agent", {"instruction": instr, "backend": rr["backend"],
                                  "model": rr["variant"], "effort": rr["depth"]}, ctx)
    # ★用户实测踩坑:刚建了**新纸还没检查**,问"第一题答案" → 编排无 name 调本工具 → 拿到**旧纸**报告,
    #   答了旧卷的第一题。这里主动探测:当前书里有比该报告**更新且未检查**的答题纸 → 强警告+指路 read_page
    #   (新纸的题目和标准答案就在纸上,read_page 自建页会原样给出)。
    _newer = []
    try:
        import pdf_reader as P
        for _it in P._upages_load((ctx or {}).get("file_rel") or r.get("file") or ""):
            if (_it.get("created") or _it.get("updated") or 0) > (r.get("ts") or 0) and not (_it.get("result_md") or "").strip():
                _ks = {b.get("kind") for b in (_it.get("blocks") or [])}
                if "blank" in _ks or "button" in _ks:
                    _newer.append({"title": _it.get("title") or "", "page": _it.get("page")})
    except Exception:
        pass
    _warn = ""
    if _newer:
        _warn = ("⚠⚠ 注意:用户在这份报告**之后**又建了新练习纸(还没检查、没有报告):"
                 + "、".join("《%s》(第%s页)" % (x["title"], x["page"]) for x in _newer[:3])
                 + "。**若用户问的是新纸的题/答案,这份旧报告不适用**——直接 read_page(新纸页码) 读那张纸"
                 "(自建页会返回题目原文+标准答案)。只有确定在问旧纸才用本报告,回答时**先说明你依据的是哪张纸**。")
    # 默认(A 折中):把报告内容**同步回给编排模型**,让它直接据此作答 —— 追问(题目出处/某题答案/为什么错)秒答。
    return {"name": r.get("name"), "score": r.get("score") or "", "src_page": _sp,
            "report_time": time.strftime("%m-%d %H:%M", time.localtime(r.get("ts") or 0)),
            "newer_unchecked": (_newer[:4] or None),
            "lookups": (_lks[:8] or None),
            "report": (r.get("report") or "")[:3500],
            "note": _warn + "这是练习纸《" + str(r.get("name") or "") + "》的题目原文+标准答案+用户手写+判分。**据此直接回答用户**"
                    "(题目/答案/为什么错/怎么记都在这里)。" + _prov
                    + "(要查书就 read_page/search_book 按需读那几页,不用把整页原文都背下来;深入查证可用 verify:true 起子 agent。)"}


def _t_search_book(args, ctx):
    q = (args.get("query") or "").strip()
    file_rel = ctx.get("file_rel", "")
    if not q or not file_rel or ".." in file_rel:
        return {"error": "缺 query 或没开书"}
    try:
        ql = q.lower()
        hits = []
        _wm0 = _web_mat(file_rel)
        if _wm0 is not None:   # 网页:在缓存正文里搜(单文档,page 恒 1)
            _t0 = _wm0.get("text") or ""
            _low = _t0.lower()
            _p = _low.find(ql)
            while _p >= 0 and len(hits) < 20:
                hits.append({"page": 1, "snippet": _t0[max(0, _p - 30):_p + len(q) + 60].replace("\n", " ").strip()})
                _p = _low.find(ql, _p + max(1, len(ql)))
            return {"total": len(hits), "hits": hits[:10],
                    "note": "网页是单文档,page 恒为 1;要全文用 read_page。"}
        for _mrel, _moff, _mpgs in _vb_members(file_rel):   # 合并书:逐成员扇入;非分卷=单成员零变化
            ap = (VAULT_ROOT / _mrel).resolve()
            ap.relative_to(VAULT_ROOT.resolve())   # 容器校验:file_rel 来自前端不可信,挡 .. / 绝对路径越出 vault
            idx = _pdf()._book_text_index(str(ap), _mrel)
            for ps, txt in idx.items():
                low = (txt or "").lower()
                pos = low.find(ql)
                if pos >= 0:
                    hits.append({"page": _to_disp(ctx, int(ps) + _moff),   # 报印刷页给 AI(合并书=全局)
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


def _symbolic_page(v, ctx):
    """符号页码 → 具体页码:last/end/最后=最后一页,first/开头=1,+N/-N=相对当前页。
    (用户拍板:『翻到最后一页』这种不该逼 AI 先查页数再跳——服务端自己算,一次到位且永不算错。)"""
    sv = str(v or "").strip().lower()
    tot = _book_total_pages(ctx.get("file_rel") or "")
    if sv in ("last", "end", "最后", "最后一页", "末页", "最后页"):
        return _to_disp(ctx, tot) if tot else None
    if sv in ("first", "开头", "第一页", "首页"):
        return _to_disp(ctx, 1)
    if sv.startswith(("+", "-")):
        try:
            cur = int((ctx.get("pages") or [ctx.get("page")])[0] or 1)
            return _to_disp(ctx, max(1, min(tot or 10 ** 9, cur + int(sv))))
        except Exception:
            return None
    return None


def _t_goto_page(args, ctx):
    try:
        # 100:参数名别名兼容(Grok 实测传 page_number → 报错 → 模型重试风暴;68 批 read_page pages 同款教训)
        _pv = args.get("page")
        if _pv is None:
            _pv = args.get("page_number", args.get("pageNumber", args.get("p")))
        _sym = _symbolic_page(_pv, ctx)   # last / first / +1 / -1
        n = int(_sym if _sym is not None else _pv)   # AI/用户给的是**印刷页码**
    except (TypeError, ValueError):
        return {"error": "page 不是数字(参数名用 page;也可以传 last/first/+1/-1)"}
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


def _t_do_task(args, ctx):
    """147(用户点子):**多步任务甩给后台 agent worker**(无头 Claude CLI + 我们自己的 MCP)。
    它自己规划/调工具/收敛,回来一句话 —— 语音模型只花 1 次工具调用。
    值在哪:N 步任务走语音模型 = N+1 次 realtime response(每次全量 input 重算 + 工具结果全堆进语音上下文);
    走 worker = **恒定 2 次** response(调 do_task 一次 + 播报结果一次),语音模型只看见最后那句摘要。

    **⚠ 省的轮数 = N - 1,所以 1 步任务是零收益**(直接调 2 轮 vs worker 也是 2 轮 —— 不省钱、不省时间、
       还白烧一次 CLI 额度)。**1 个工具能答的一律直接调**,这条与后端无关。

    **148 实测(账本 218 次 response @ $0.0053/轮;realtime 每轮往返 p10=2.0s / p25=5.0s)**:
      步数   直接调(N+1轮)        worker/opus     worker/codex
       1     5~11s  / $0.011      8.8s(零收益)   9.0s(零收益)
       2     8~17s  / $0.016      **9.1s**/$0.011  15.3s
       3     17~29s / $0.021      **11.1s**       15.9s
       5     20~35s / $0.037      **11.1s**       18.8s
    worker 内部连调 MCP **没有 realtime 往返**,所以耗时几乎不随步数增长(opus 封顶 ~11s);
    直接调每加一步就多一整轮往返。⇒ **opus 从 2 步起就又快又省**(codex 要 3 步才追平)。
    ⇒ 门槛定「**≥2 个工具**」(默认 opus)。CLI 走订阅额度不是 API 计费 → 把贵的 realtime 轮次换成订阅额度。"""
    instr = (args.get("instruction") or args.get("task") or args.get("text") or "").strip()
    if len(instr) < 3:
        return {"error": "要做什么?说清楚点"}
    # 148:后端/型号/思考深度走**统一的 per-action 预设**(设置面板「agent」那一行,跟其它工具一套 UI),
    #   不再写死 env。默认 codex/gpt-5.6-luna/low(白嫖 ChatGPT 额度);codex 失败 worker 会自动降级 claude。
    r = _resolve("agent", str(ctx.get("uid") or ""))
    return _bg_task("agent", {"instruction": instr, "backend": r["backend"],
                              "model": r["variant"], "effort": r["depth"]}, ctx)


def _t_make_paper(args, ctx):
    """★用户设计(#38/#55):编排 AI 唯一能看到的**造纸入口**(它自己没有 page_* 工具)。
    造一张让用户**在页面上手写作答**的交互纸(出题/填空/试卷/听写/清单/『给我出…我写』/『在纸上做』)。
    内部把活儿甩给后台 CLI(CLI 那边经 MCP 才有 page_new/page_add/page_show),产出**一张 CLI 卡**、可保存为工具。
    args {intent: 一句话说清要造什么纸(题目/数量/紧扣哪页…),把用户原话原样带上}。"""
    intent = (args.get("intent") or args.get("instruction") or args.get("task") or args.get("text") or "").strip()
    if len(intent) < 2:
        return {"error": "要造什么纸?一句话说清(如『出3道填空题让我写』)"}
    r = _resolve("paper", str(ctx.get("uid") or ""))   # 造纸=设计插入内容,用**更深思考**的独立预设(默认 opus·high;≠ do_task 的 agent)
    # 把造纸规矩直接写进给 CLI 的指令:page_new → page_add(可 blocks=[…] 批量)→ page_show。
    #   #38:按钮由**你自己设计行为** —— 每个按钮的 event 决定按下干什么。
    instr = ("造一张让用户在页面上手写作答的交互纸:" + intent +
             "。步骤:page_new 开纸 → page_add 加元素(题干 kind:text、作答 kind:blank;"
             "**可一次 page_add(blocks=[…]) 批量加完**,一次决定所有元素好安排位置)→ page_show 生成。\n"
             "★ 按钮(kind:button)的行为你自己定,event 可选:\n"
             "  check = 批改手写(答题/填空/试卷纸**务必**放一个 {kind:button,label:'让 AI 检查',event:'check'})\n"
             "  reveal:块id / hide:块id = 显/隐某块(如做完再显示答案/提示)\n"
             "  set_enabled:块id / disable:块id = 开/关某按钮(如写完前禁用交卷)\n"
             "  say:文本 = 念一句;goto:页码 = 跳页;call:工具名 = 触发任意工具\n"
             "  按钮可加 enabled:false 初始禁用。blank 可加 answer:'正解' 供 check 判对错。")
    return _bg_task("agent", {"instruction": instr, "backend": r["backend"],
                              "model": r["variant"], "effort": r["depth"]}, ctx)


def _card_extra(ctx):
    """61b(用户需求):制卡/记笔记的后台 AI 自动带上**对话现场**——最近工具结果(网页搜索摘要/配图 URL)
    + 近几轮对话。语音模型的 text 种子往往只有一句话,搜过的资料不注入就永远进不了卡片。
    返回 (资料文本块, 图URL列表)。"""
    parts, imgs = [], []
    for t in (ctx.get("recent_tools") or [])[-4:]:
        try:
            lbl = t.get("label") or t.get("tool") or "工具"
            rag = (t.get("rag") or "").strip()
            if rag:
                parts.append(f"[{lbl}] {rag[:600]}")
            for u in (t.get("images") or [])[:3]:
                if u and u not in imgs:
                    imgs.append(u)
        except Exception:
            continue
    try:
        for m in _convo_load(ctx.get("_uid"))[-6:]:
            c = (m.get("content") or "").strip()
            if c:
                parts.append(("用户:" if m.get("role") == "user" else "AI:") + c[:300])
    except Exception:
        pass
    return ("\n".join(parts)[:3000], imgs[:4])


def _t_make_anki(args, ctx):
    # 2026-07-21 用户拍板:制卡工具**同步等草稿做完**才返回(带"生成了N张"),不再派发即回报→
    #   AI 拿到真结果才口头汇报,不会过早说"已做好"(消除承诺核查幻影补交的源头)。
    #   且把**用户的具体要求**(数量/难度/角度,args.requirement + 对话现场)转述给制卡 AI。
    text = (args.get("text") or "").strip() or (ctx.get("selection") or "").strip() \
        or _page_text(ctx.get("file_rel", ""), ctx.get("page", 0))
    if not text:
        return {"error": "缺要做卡的内容(给 text 或先选中)"}
    req = (args.get("requirement") or args.get("instruction") or "").strip()
    extra, imgs = _card_extra(ctx)
    img = (args.get("image_url") or "").strip()
    if not img and len(imgs) == 1:
        img = imgs[0]
    elif not img and imgs:
        extra = (extra or "") + "\n(对话配图候选,内容相关可参考:" + " ".join(imgs) + ")"
    fullreq = (req + ("\n" + extra if extra else "")).strip()
    src = (ctx.get("file_rel") or "") + (("#p" + str(ctx.get("page"))) if ctx.get("page") else "")
    try:
        import voice as _voice
        out = _voice._pdf_mod()._run_snippets_to([{"text": text, "source": src}], False, True, "", "opus", "high",
                                                 image_url=img or None, defer_add=True, requirement=fullreq)
    except Exception as e:
        return {"error": "制卡失败:" + str(e)[:120]}
    _mark_source_highlight(ctx, "#b9f6ca")   # 双向回链:原文留绿色高亮"这段做过卡"
    if not out.get("ok"):
        return {"error": out.get("error") or out.get("anki_error") or "制卡失败"}
    cards = out.get("anki_cards") or []
    if not cards:
        return {"error": "AI 没生成卡片(内容可能不适合制卡)"}
    n = len(cards)
    brief = []   # 每张卡一行大意:喂回语音模型/recentTools/下一个制卡 CLI 都吃它(全文只给 UI;
    #   截断喂回残 JSON=「AI 不知道自己做过什么卡」的根因,用户 2026-07-20 实锤)
    for c in cards[:12]:
        _f = (c.get("cloze") or c.get("front") or "").strip().replace("\n", " ")[:60]
        _b = (c.get("back") or "").strip().replace("\n", " ")[:40]
        brief.append(_f + ((" → " + _b) if _b else ""))
    return {"ok": True, "n": n, "cards_brief": brief, "cards": cards, "deferred": True,
            "speak": f"做好了{n}张卡片草稿，你在卡片上确认后入库", "note": f"生成了{n}张卡片草稿(等你确认入库)"}


def _t_make_note(args, ctx):
    text = (args.get("text") or "").strip() or (ctx.get("selection") or "").strip() \
        or _page_text(ctx.get("file_rel", ""), ctx.get("page", 0))
    if not text:
        return {"error": "没有要整理的内容"}
    params = {"text": text}
    extra, imgs = _card_extra(ctx)
    if extra:
        params["extra_ctx"] = extra + (("\n(对话配图,合适可用 markdown 嵌入笔记:" + " ".join(imgs) + ")") if imgs else "")
    res = _bg_task("note", params, ctx)
    _mark_source_highlight(ctx, "#a7d8ff")   # 双向回链:原文留蓝色高亮"这段整理进笔记了"
    return res


def _t_add_vocab(args, ctx):
    word = (args.get("word") or "").strip() or (ctx.get("selection") or "").strip()
    if not word:
        return {"error": "缺单词"}
    return _bg_task("vocab", {"word": word}, ctx)


def _search_wikipedia_image(query: str, timeout: float = 5.0) -> dict:
    """配图核心实现(纯函数,不依赖 ctx,方便独立脚本测试)——免费无 key、真实图片(非生成)。
    流程:先中文 zh.wikipedia 全文搜索拿最匹配标题;没结果再试英文 en.wikipedia;
    拿到标题后调 REST summary API 取缩略图 + 摘要 + 条目页链接。该条目没配图 → ok:false(别让上层瞎编图)。
    网络失败/超时一律兜底返回 error,不抛异常拖垮整个助手对话。"""
    import requests
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "缺 query"}
    headers = {"User-Agent": "bwicarus-claude-assistant/1.0 (learning tool; contact: bwicarus2@gmail.com)"}

    def _search_title(lang):
        try:
            r = requests.get(
                f"https://{lang}.wikipedia.org/w/api.php",
                params={"action": "query", "list": "search", "format": "json",
                        "srsearch": q, "srlimit": 1},
                timeout=timeout, headers=headers)
            if r.status_code != 200:
                return None
            hits = ((r.json() or {}).get("query") or {}).get("search") or []
            return hits[0]["title"] if hits else None
        except Exception:
            return None

    title, lang = _search_title("zh"), "zh"
    if not title:
        title, lang = _search_title("en"), "en"
    if not title:
        return {"ok": False, "error": "Wikipedia 没搜到相关条目"}
    try:
        import urllib.parse as _up
        r = requests.get(
            f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{_up.quote(title)}",
            timeout=timeout, headers=headers)
        if r.status_code != 200:
            return {"ok": False, "error": f"摘要接口返回 {r.status_code}"}
        data = r.json() or {}
    except Exception as e:
        return {"ok": False, "error": f"网络请求失败:{str(e)[:100]}"}
    thumb = (data.get("thumbnail") or {}).get("source")
    if not thumb:
        return {"ok": False, "error": "该条目没有配图"}
    extract = (data.get("extract") or "").strip()[:150]
    page_url = ((data.get("content_urls") or {}).get("desktop") or {}).get("page") or ""
    return {"ok": True, "title": data.get("title") or title, "extract": extract,
            "image_url": thumb, "page_url": page_url, "lang": lang}


def _verify_image_match(image_url: str, query: str, extract: str = "") -> tuple:
    """核实 Wikipedia 缩略图是不是真的展示了 query 本身(而非原材料/无关场景/示意图)。
    标题/摘要文字对得上不代表图对——Wikipedia 条目的配图常是"相关但不是那张图"(如「七草粥」条目配图
    实为节日展示生七草,不是煮好的粥)。走 _vision_describe(默认 Gemini Flash,便宜;失败回退 Claude haiku,
    同样便宜),一次性判断,不进主对话上下文。任何环节失败(下载/视觉调用都不可用)→ 默认放行,
    别让核实本身的不确定性挡住这个本来就是锦上添花的功能。"""
    try:
        dl = _pdf()._download_image_for_anki(image_url)
    except Exception:
        dl = None
    if not dl:
        return True, "(核实跳过:图片下载失败,默认放行)"
    fname, b64 = dl
    ext = os.path.splitext(fname)[1].lstrip(".").lower()
    mt = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
          "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/jpeg")
    note = (f"这张图打算用来配「{query}」(摘要:{extract[:100]})。"
            "判断图片实际画面是不是真的展示了这个概念/菜品/实物**本身的成品或实体**,"
            "而不是原材料、制作过程、无关场景或别的东西。"
            "只回答第一个字「是」或「否」,后面跟一句简短理由。")
    out = _vision_describe([{"media_type": mt, "b64": b64}], note=note,
                           backend="gemini", variant="haiku", depth="low", timeout=25)
    if not out:
        return True, "(核实跳过:视觉模型不可用,默认放行)"
    out = out.strip()
    return out.startswith("是"), out[:150]


def _t_web_search(args, ctx):
    """通用网页搜索。61b 主路=OpenAI 内建 web_search(Responses API,综合回答+来源,无每日次数额度,
    ≈$0.004/次);Google CSE 只作兜底(它还没启用/有 100 次日限)。模型没有内建联网——这就是它的联网通道。"""
    q = str(args.get("query") or "").strip()[:160]
    if not q:
        return {"error": "缺 query(搜索关键词)"}
    try:
        _wsr = _resolve("web_search", ctx.get("_uid"))   # 91:型号走设置项(感叹号「本环节设置」可直改)
        r = _gemini_websearch(q, model=(_wsr.get("variant") if _is_gemini(_wsr.get("variant") or "") else None))
        card = r.get("card")
        if card and card.get("kind") in ("weather", "news", "fact", "general"):
            # 70(用户设计):结构化结果卡——卡片经 client_action 显示(侧栏卡/字幕模式浮层),
            # 2.1 只拿 brief=「已显示+一句概况」,不用念细节(与 route 同哲学:知道任务完成即可)
            brief = card.get("brief") or (card.get("data") or {}).get("text") or card.get("title") or "结果已显示"
            idx = ""
            if card["kind"] == "news":   # 70c(用户指正):给 2.1 一份条目索引——追问"第二条是什么"时它知道有哪些,不用重搜
                _ts = [it.get("t") or "" for it in (card.get("data") or {}).get("items") or [] if it.get("t")]
                if _ts:
                    idx = " 卡片条目:" + ";".join(_ts[:5]) + "。"
            return {"ok": True, "kind": card["kind"], "silent": True,
                    "note": "搜索结果已用卡片显示在用户屏幕上,本轮到此结束(系统不会请你发言)。"
                            "结果概况:" + brief + "。" + idx +
                            "用户下次说话时若与此相关,直接运用这些信息回答;不要主动复述卡片内容。",
                    "client_action": {"fn": "renderInfoCard", "args": [card]}}
        if r.get("answer"):
            return {"ok": True, "answer": r["answer"], "sources": r.get("sources") or [],
                    "note": "answer 是联网搜索的综合结论,口头转述并提一句来源。"}
        import image_search
        r = image_search.openai_web(q)   # 次选:OpenAI web_search(≈$0.004/次)
        if r.get("answer"):
            return {"ok": True, "answer": r["answer"], "sources": r.get("sources") or [],
                    "note": "answer 是联网搜索的综合结论,口头转述并提一句来源。"}
        rs = image_search.search_web(q, n=5)
    except Exception as ex:
        return {"error": f"搜索失败: {str(ex)[:120]}"}
    if not rs:
        return {"error": "没搜到结果(联网搜索暂时不可用)。如实告诉用户,凭你的知识回答并声明可能过时。"}
    return {"ok": True, "results": rs,
            "note": "凭 snippet 口头总结,提一句信息来源;snippet 不够就如实说只搜到概要。"}


def _t_search_image(args, ctx):
    """配图专用:按**关键词列表**并行搜真实图片(非 AI 生成),多源=Wikimedia Commons 全库 + Google 图搜(若配好)。
    配图偏好开时**一次性**传 queries=[{concept, query}...] 把该配图的概念都列上(query 用英文覆盖最好);
    工具脱开上下文、只认关键词去搜,每个概念返回最匹配 1 张。args {queries:[{concept?,query}], query?(单个兼容)}。"""
    import concurrent.futures as _cf
    import image_search
    ql = args.get("queries")
    items = []
    if isinstance(ql, list) and ql:
        for it in ql[:8]:   # 上限 8,防一次配太多图
            if isinstance(it, dict) and (it.get("query") or it.get("concept")):
                items.append({"concept": (it.get("concept") or it.get("query") or "").strip(),
                              "query": (it.get("query") or it.get("concept") or "").strip(),
                              "query_en": (it.get("query_en") or "").strip()})
            elif isinstance(it, str) and it.strip():
                items.append({"concept": it.strip(), "query": it.strip(), "query_en": ""})
    else:
        q = (args.get("query") or ctx.get("selection") or "").strip()
        if q:
            items.append({"concept": q, "query": q, "query_en": ""})
    if not items:
        return {"error": "缺 queries(要配图的概念 + 关键词列表)"}

    def _short(q):
        # 裁短降阶(用户实战发现:关键词太长 Commons 全文匹配不到,口头让 AI 缩短就中了——固化成程序兜底):
        # 带空格的(英文/罗马字)裁到前 3 个词;日文无空格不裁(截半个词更糟,靠提示词管)
        ws = q.split()
        return " ".join(ws[:3]) if len(ws) > 3 else ""

    def _one(it):
        # 落阶链(用户设计):原语言 → 英文保底 → 各自裁短(3 词)——一次调用内完成,不烧第二轮对话
        q0, qe = it["query"], (it.get("query_en") or "")
        imgs = []
        tried = set()
        hitq = {"q": ""}
        def _try(qq):
            qq = (qq or "").strip()
            if not qq or qq.lower() in tried:
                return []
            tried.add(qq.lower())
            try:
                r0 = image_search.search_images(qq[:120], n=1)   # 每概念取最匹配 1 张
                if r0:
                    hitq["q"] = qq   # 88:记命中词(感叹号溯源"哪条链路搜到的")
                return r0
            except Exception:
                return []
        for qq in (q0, qe, _short(q0), _short(qe)):
            imgs = _try(qq)
            if imgs:
                break
        if not imgs:
            # 76(用户方案"wiki 关键词查询"):Gemini 免费文本档把词规范化成 Commons 必中的检索名
            # ("日本富士山 真实照片"→"Mount Fuji"/"富士山"),再搜一轮——纯知识任务,不占 grounding 额度
            try:
                _nr = _resolve("img_norm", ctx.get("_uid"))   # 77:规范化模型走设置项(面板「配图关键词规范化」,非硬编码)
                cj = _gemini_text('「' + (q0 or qe) + '」这个事物在 Wikimedia Commons 图库最可能命中的检索词。'
                                  '只输出 JSON 数组(英文规范名+原语言规范名,只要事物名称本身):["English name","原名"]',
                                  max_tokens=80, think=False, timeout=15,
                                  model=(_nr.get("variant") if _is_gemini(_nr.get("variant") or "") else None))
                import re as _re2
                mm = _re2.search(r"\[[\s\S]*?\]", cj or "")
                for cand in (json.loads(mm.group(0)) if mm else [])[:3]:
                    imgs = _try(str(cand))
                    if imgs:
                        break
            except Exception:
                pass
        return {"concept": it["concept"], "found": bool(imgs),
                "image_url": (imgs[0]["image_url"] if imgs else ""),
                "page_url": (imgs[0].get("page_url", "") if imgs else ""),
                "source": (imgs[0].get("source", "commons") if imgs else ""),
                "matched_query": hitq["q"]}
    with _cf.ThreadPoolExecutor(max_workers=min(6, len(items))) as ex:
        results = list(ex.map(_one, items))
    found = [r for r in results if r["found"]]
    if not found:
        # "没搜到"是合法空结果不是故障(error 会让语音工具卡亮 ⚠、模型当成系统坏了)——引导换词重试
        return {"ok": False, "found": 0,
                "note": "这些关键词没搜到图。**换另一种语言或更通用的词**再调一次"
                        "(日本特有事物用日语原名,通用/西方概念用英文通称/学名);"
                        "再搜不到就如实告诉用户没找到合适的图,绝不编图片链接。"}
    return {"ok": True, "count": len(found),
            "images": [{"concept": r["concept"], "image_url": r["image_url"], "page_url": r["page_url"],
                        "source": r.get("source", ""), "matched_query": r.get("matched_query", "")} for r in found],
            "missed": [r["concept"] for r in results if not r["found"]],
            "_note": "把这些图用 markdown ![简短中文说明](image_url) 插进回答里对应概念旁(每张配一句说明)。"
                     "missed 里的没搜到图 → 别硬配、更别自己编图片链接。"}


def _optimize_video_query(topic, r=None):
    """把用户想看的主题 → **一个优质 YouTube 搜索关键词**(用 pick_video 那档模型,默认便宜 Gemini Flash)。
    学术/机制类优先英文+教学词(命中 Khan/科普动画那类高质量讲解),通俗/文化类用中文。失败返回 None(调用方退回原词)。"""
    try:
        prompt = (
            "用户在读书,想在 YouTube 找**讲解**下面这个主题的视频:\n「" + str(topic)[:200] + "」\n\n"
            "请给出**一个**最能搜到**高质量讲解视频**的 YouTube 搜索关键词。规则:\n"
            "- **某国专属的历史/文化/料理/地理/人物/传统**(如日本史、法国料理、意大利艺术)→ **用该国母语搜**"
            "(日→日文、法→法文、德→德文…),母语频道讲本国题材质量与数量都远高;\n"
            "- 学术/科学/技术/机制/数学类(不特定于某国)→ **优先英文**(英文讲解视频质量远高,如物理/生物/数学的机制动画),"
            "加上 explained / animation / lecture / how it works 之类教学向词;\n"
            "- 其它通俗/中文特定主题 → 用中文,加 讲解/原理/教程 之类;\n"
            "- 只保留**核心概念**,别照抄整句、别加书名/章节号/作者名。\n"
            "**只输出这一个关键词本身(一行),不要引号、不要解释、不要给多个选项。**"
        )
        out = (_deep_ask(prompt, backend=r["backend"], variant=r["variant"], depth="none", timeout=20)
               if r else _gemini_text(prompt, max_tokens=120, think=False, timeout=20)) or ""
        out = out.strip()
        if out:
            out = out.splitlines()[0].strip().strip('「」""\'` ')
        return out[:120] or None
    except Exception:
        return None


def _optimize_video_queries(topic, r=None):
    """一次 AI → 两个搜索关键词:YouTube(按内容原语言) + Bilibili(一律中文)。失败各自返 None(调用方退回原词)。
    「YT 按内容原语言、B站中文」是用户拍板的策略:B站以中文讲解为主,YT 保留原生优质内容(英语→英文、日语→日文)。"""
    yt_q = bili_q = None
    try:
        prompt = (
            "用户在读书,想找**讲解**下面这个主题的视频:\n「" + str(topic)[:200] + "」\n\n"
            "请分别给出用于 YouTube 和 Bilibili(B站)的搜索关键词各一个。\n"
            "【YouTube 关键词】按内容原语言:\n"
            "- 某国专属历史/文化/料理/地理/人物/传统 → 用该国母语(日→日文、法→法文…),母语频道讲本国题材质量最高;\n"
            "- 学术/科学/技术/机制/数学(不特定某国)→ 优先英文,加 explained / animation / lecture / how it works 之类;\n"
            "- 其它通俗/中文特定主题 → 中文,加 讲解/原理/教程 之类。\n"
            "【Bilibili 关键词】**一律用中文**(B站以中文讲解内容为主),加 讲解/教程/原理 之类。\n"
            "两个都**只保留核心概念**,别照抄整句、别加书名/章节号/作者名。\n"
            "**严格只输出这两行(不要引号/解释/多余内容):**\n"
            "YT: <关键词>\n"
            "B站: <中文关键词>"
        )
        out = (_deep_ask(prompt, backend=r["backend"], variant=r["variant"], depth="none", timeout=20)
               if r else _gemini_text(prompt, max_tokens=160, think=False, timeout=20)) or ""
        for line in out.splitlines():
            ls = line.strip()
            m = re.match(r"^(?:YT|YouTube|油管)\s*[:：]\s*(.+)$", ls, re.I)
            if m and not yt_q:
                yt_q = m.group(1).strip().strip("「」\"“”'` ")[:120]
            m2 = re.match(r"^(?:B站|Bili|Bilibili|哔哩|哔哩哔哩)\s*[:：]\s*(.+)$", ls, re.I)
            if m2 and not bili_q:
                bili_q = m2.group(1).strip().strip("「」\"“”'` ")[:120]
    except Exception:
        pass
    return (yt_q or None), (bili_q or None)


def _filter_relevant_videos(topic, vids, r):
    """搜到候选后再让 AI 按相关性筛一遍(标题+频道+描述节选):明显跑题/低质的剔除,返回保留子集(保序,≥1)。
    只用搜索一次就免费带回的元数据(标题/频道/描述),不额外拉每个视频的字幕(6 个逐一拉太慢太贵)。失败→原样不筛。"""
    try:
        if len(vids) <= 1:
            return vids

        def _play_h(n):   # 播放量人性化(B站质量信号):12345→1.2万
            try:
                n = int(n)
            except Exception:
                return ""
            return (f"{n/10000:.1f}万" if n >= 10000 else str(n))
        lines = "\n".join(
            f"[{i+1}] {v.get('title','')} · 频道:{v.get('channel','')}" +
            (f" · 播放:{_play_h(v.get('play'))}" if v.get('play') else "") +
            (f" · 标签:{v.get('tag')}" if v.get('tag') else "") +
            (f" · 简介:{(v.get('desc') or '')[:140]}" if v.get('desc') else "")
            for i, v in enumerate(vids))
        prompt = (
            "用户在读书,想找**讲解「" + str(topic)[:160] + "」**的视频。下面是搜到的候选(播放量高=质量/热度信号):\n" + lines + "\n\n"
            "请判断每个是否**真的在专门讲这个主题**、是不是像样的讲解/教学视频。\n"
            "**剔除**:明显跑题、只是顺带提到、宽泛合集(如「四级真题合集/英语学习大全」而非本主题专讲)、纯娱乐/引流、片段混剪、标题党——"
            "**即使它播放量很高也要剔除**(高播放≠切题)。在**都切题**的前提下,再优先播放量高的。\n"
            "**只输出要保留的编号**,按相关度从高到低用逗号分隔(例:3,1,5)。至少保留 1 个;若全部明显跑题才输出 none。\n"
            "只输出编号或 none,不要解释。")
        out = (_deep_ask(prompt, backend=r["backend"], variant=r["variant"], depth="none", timeout=25) or "").strip()
        if not out or "none" in out.lower():
            return vids[:3]   # 全跑题/无输出 → 兜底给前 3,不留空
        import re as _re
        keep_idx, seen = [], set()
        for m in _re.finditer(r"\d+", out):
            k = int(m.group()) - 1
            if 0 <= k < len(vids) and k not in seen:
                seen.add(k); keep_idx.append(k)
        picked = [vids[k] for k in keep_idx] or vids[:3]
        return picked[:6]
    except Exception:
        return vids


def _t_search_video(args, ctx):
    """搜教学视频(YouTube + Bilibili 两源)并在对话里渲染**可播放**卡片。只在用户明确要『找视频/看视频讲解/有没有视频』时用,
    别对每个概念都配视频。args {query?}(传**核心主题**即可)。工具内部:AI 拟两个搜索词(YT 按内容原语言 / B站中文)→
    并行搜两源 → 各自 AI 相关性筛掉跑题的 → 交替合并(两源都露脸)。"""
    q = (args.get("query") or "").strip() or (ctx.get("selection") or "").strip() or (ctx.get("focus_text") or "").strip()
    if not q:
        return {"error": "缺 query(要搜什么视频)"}
    r = _resolve("pick_video", ctx.get("_uid"))
    if _paid_recover_check(ctx.get("_uid"), "pick_video"):   # @paid 且免费恢复 → 摘除后重读(静默)
        r = _resolve("pick_video", ctx.get("_uid"))
    yt_q, bili_q = _optimize_video_queries(q[:200], r)       # 一次 AI → YT(原语言) + B站(中文)两个搜索词
    yt_q = yt_q or q[:120]
    bili_q = bili_q or q[:120]
    import concurrent.futures as _cf

    def _search_yt():
        try:
            import youtube_search
            res = youtube_search.search(yt_q, max_results=5)
            return [dict(v, src="yt") for v in res.get("videos", [])] if res.get("ok") else []
        except Exception:
            return []

    def _search_bili():
        try:
            import bilibili_search
            res = bilibili_search.search(bili_q, max_results=8)   # 多取(已按播放量+点赞+收藏质量分排序过)→ AI 从高质池筛相关 3 个
            return res.get("videos", []) if res.get("ok") else []   # 已带 src='bili'
        except Exception:
            return []

    _ts = time.time()
    with _cf.ThreadPoolExecutor(max_workers=2) as ex:
        f_yt, f_bili = ex.submit(_search_yt), ex.submit(_search_bili)
        yt_raw, bili_raw = f_yt.result(), f_bili.result()
    if not yt_raw and not bili_raw:
        return {"error": "两个源都没搜到视频,换个关键词"}
    # 各源分别相关性筛(分开筛保证两源都能留下,不会被另一源标题抢占)。
    # **主线程串行**跑(不放线程池):_filter_relevant_videos 内的 AI token 计数是 thread-local,
    #   放 worker 线程会漏计到本回合总量(额度 DB 不受影响,但 trace 显示会少算)。两次 Flash 很快,
    #   慢的网络搜索已并行,这里串行代价小。
    _tf = time.time()
    yt_keep = (_filter_relevant_videos(q, yt_raw, r) if yt_raw else [])[:3]
    bili_keep = (_filter_relevant_videos(q, bili_raw, r) if bili_raw else [])[:3]
    _filter_sec = round(time.time() - _tf, 1)
    # 交替合并:B站、YT、B站、YT…(中文源优先露头,两边都露脸)
    vids = []
    for i in range(max(len(yt_keep), len(bili_keep))):
        if i < len(bili_keep):
            vids.append(bili_keep[i])
        if i < len(yt_keep):
            vids.append(yt_keep[i])
    if not vids:
        return {"error": "搜到的视频都跑题了,换个关键词"}
    _mdl = f"{_variant_short(r['variant'])}·{r['depth']}"
    _filter_detail = (
        f"YouTube 搜索词「{yt_q}」→ 搜到 {len(yt_raw)}、保留 {len(yt_keep)}\n"
        f"Bilibili 搜索词「{bili_q}」→ 搜到 {len(bili_raw)}、保留 {len(bili_keep)}\n\n"
        "保留:\n" + "\n".join(
            f"✓ [{'B站' if v.get('src') == 'bili' else 'YT'}] {v.get('title','')} · {v.get('channel','')}" for v in vids))
    return {"ok": True, "count": len(vids), "query_used": f"YT「{yt_q}」/ B站「{bili_q}」",
            # 只把标题/频道/来源回给 agent(省 token;id/缩略图只给前端渲染,别进模型上下文)
            "videos": [{"title": v.get("title", ""), "channel": v.get("channel", ""),
                        "source": ("B站" if v.get("src") == "bili" else "YouTube")} for v in vids],
            "client_action": {"fn": "renderVideos", "args": [vids, {"q": f"YT「{yt_q}」/ B站「{bili_q}」"}]},
            "_gen_model": _mdl, "_gen_action": "pick_video",
            # 相关性筛选单独占「!」一行(独立子步骤),点开看两源各搜到/保留了哪些
            "_sub_steps": [{"label": "搜索+筛选视频(YT+B站)", "model": _mdl, "action": "pick_video",
                            "sec": _filter_sec, "detail": _filter_detail}],
            "_note": "视频卡片已渲染(YouTube + Bilibili 两源、已按相关性筛过),用户可直接点开。简短说一句『给你找到这些视频(YT 和 B站都有)』,别复述标题/链接。"}


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


def _viewshot_result(ctx, note_extra=""):
    """㉟c 前端视口截图(用户设计"把即时的重叠渲染后的结果交给AI"):所见即所得——
    正文+手写笔迹+插入页 overlay 全在一张图里。EPUB(服务端无法渲染 HTML)恒用;PDF 渲染失败(如
    插入页还没写回文件)兜底用。ctx.view_image = {media_type, b64}。"""
    vimg = ctx.get("view_image")
    if not isinstance(vimg, dict) or not vimg.get("b64"):
        # 请求时没带截图(WS relay 拿不到/前端截图失败等)→ 回退读**服务端缓存的笔迹合成图**
        # (EPUB 每次存笔迹时前端顺带存的,见 pdf_reader /api/epub-ink-shot)——让 see_ink 全链路可靠。
        try:
            import base64
            import pdf_reader as _pdfm_vs
            _p = _pdfm_vs._epub_inkshot_path(ctx.get("file_rel") or "")
            if _p.exists():
                _raw = _p.read_bytes()
                if len(_raw) > 2000:
                    vimg = {"media_type": "image/jpeg", "b64": base64.b64encode(_raw).decode()}
        except Exception:
            vimg = None
    if not isinstance(vimg, dict) or not vimg.get("b64"):
        return None
    note = ("下图=用户屏幕当前可见区域的**实时截图**(正文+他的手写笔迹叠加,所见即所得)。"
            "结合笔迹的位置/形状/指向和图里文字回答。"
            "若笔迹是手写字/算式:先看整体结构再逐个认字符,易混对(G↔A↔C、r↔n↔v)用整体含义合理性定夺;"
            "他学的内容不限于本页主题。" + (note_extra or ""))
    _fed = [{"media_type": vimg.get("media_type") or "image/jpeg", "b64": vimg["b64"]}]
    if ctx.get("_want_vision"):
        return {"_vision": _fed, "_fed_images": _fed,
                "看图提示": note, "说明": "屏幕实时截图已直接发给你,结合看图提示自己看图回答"}
    desc = _vision_for(ctx, _fed, note)
    return {"画面描述": desc or "(看图失败,可重试)", "_fed_images": _fed}   # #8 前端截图=实际发给AI的图,回卡显示


def _t_see_page(args, ctx):
    """看当前页(或指定页)的视觉内容(图表/示意图/公式排版/手写等文字层拿不到的)。
    ★优先复用**已存的离线图描述**(夜间管线生成、read_page 也注入过)——**不重复识别**;
    只有 ① 本页有手写笔迹(离线描述里没有) 或 ② 本书没生成离线描述 时,才现场渲图做一次视觉识别。"""
    file_rel = ctx.get("file_rel") or ""
    if not file_rel:
        return {"error": "当前不在 PDF 书里,没法看页面"}
    if file_rel.startswith("web:"):   # 网页:真实页面渲在浏览器 iframe 里,服务端无从渲染
        r = _viewshot_result(ctx, " 这是用户正在看的网页视口截图。")
        return r if r else {"error": "网页的视觉内容需要前端视口截图,这次没拿到;"
                                     "正文可以直接用 read_page(已抽好),图片类问题请让用户描述或稍后重试"}
    if file_rel.lower().endswith(".epub"):   # ㉟c EPUB:服务端渲不了 HTML,看页面=前端视口截图
        r = _viewshot_result(ctx)
        return r if r else {"error": "EPUB 看页面需要前端视口截图,这次没拿到;请让用户稍后再试或口头描述"}
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
                _mr, _lp = _vb_src(file_rel, pg)   # 合并书:墨迹边车在真实成员名下
                if _pdfm._page_ink_strokes(_mr, _lp):
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
        file_rel, pages = _vb_localize(file_rel, pages)   # 视图页→所在卷局部页(单本书恒等)
        if not file_rel:
            return {"error": "页越界"}
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
        if ctx.get("_want_vision"):   # ㉗:调用方要原图直喂(GPT Realtime 图像输入)→ 不本地转述,图+看图提示一起穿透
            return {"_vision": vis, "看图提示": note, "rendered_pages": done, "inked_pages": inked,
                    "说明": "页面渲染图已直接发给你,结合看图提示自己看图回答"}
        desc = _vision_for(ctx, vis, note)   # 一次性看图 → 返回文字;主循环不背图
        return {"页面图像描述": desc or "(看图失败,可重试)", "rendered_pages": done, "inked_pages": inked}
    except Exception as e:
        return {"error": str(e)[:140]}


def _is_overlay_page(rel, page):
    """这一页是不是**插入页**(覆盖层内容,PDF 文件本身空白)。是→see_ink 用前端整页截图;
    否(普通 PDF 页)→ 走服务端**精确局部裁图**(_ink_focus_image,只裁笔迹附近,不发整屏)。"""
    try:
        import pdf_reader as _P
        for it in _P._upages_load(rel):
            if it.get("mode") == "overlay" and int(it.get("page") or 0) == int(page):
                return True
    except Exception:
        pass
    return False


def _t_see_ink(args, ctx):
    """看用户**用笔标注的那块区域的合成图**(裁笔迹附近 + 叠上手写笔迹)。
    用户用笔圈/划/打勾/画箭头标了东西、问『这是什么/我圈的/什么意思/这里』,或没说具体但页面有笔迹时用。返回 _vision 喂回大脑。"""
    file_rel = ctx.get("file_rel") or ""
    strokes = ctx.get("ink") or []
    page = int(ctx.get("page") or 0)
    if not file_rel or not page:
        return {"error": "不在 PDF 书里 / 不知道哪页"}
    if file_rel.lower().endswith(".epub"):   # ㉟c EPUB:笔迹画在 HTML 上,服务端渲不了 → 前端视口截图(所见即所得)
        r = _viewshot_result(ctx, " 用户问的是他的手写/圈画,重点看截图里的笔迹。")
        return r if r else {"error": "EPUB 的笔迹需要前端视口截图,这次没拿到;请让用户稍后再试"}
    if ctx.get("view_image"):   # 前端已按**笔迹外接框截了局部图**(所见即所得、聚焦圈画,不是整屏)→ 优先用它;
        r = _viewshot_result(ctx, " 用户问的是他圈/画/写的那块,截图已聚焦到笔迹区域,结合题目和手写一起看。")
        if r:                    #   服务端 _ink_focus_image 退为兜底(view_image 没拿到时才裁,见下)
            return r
    if not strokes:
        try:   # sidecar 回退:调用方没带实时墨迹(语音壳刚重连/侧栏特殊路径)→ 读服务端存档(与 _sys_prompt 同语义)
            import pdf_reader as _pdfm0
            _mr0, _lp0 = _vb_src(file_rel, page)   # 合并书:墨迹边车在真实成员名下
            strokes = _pdfm0._page_ink_strokes(_mr0, _lp0) or []
        except Exception:
            strokes = []
    if not strokes:
        r0 = _viewshot_result(ctx, " 用户问他的手写/圈画,服务端没有这页的笔迹存档(可能画在刚插入的自建页上)——以截图为准。")
        if r0:   # ㉟c 插入页兜底:自建页还没写回 PDF 文件时服务端两手空空,前端截图=用户真实所见
            return r0
        return {"error": "本页没有手写笔迹(用户没用笔标注,或还没画)"}
    try:
        import base64
        import pdf_reader as pdf
        file_rel, page = _vb_src(file_rel, page)   # 合并书:渲图/取文字都按真实成员局部页
        png = pdf._ink_focus_image(file_rel, page, strokes)
        if not png:
            r1 = _viewshot_result(ctx, " (服务端裁不出笔迹区域,已改用屏幕实时截图。)")
            if r1:
                return r1
            return {"error": "裁不出笔迹区域"}
        marked = ""
        try:
            marked = pdf._text_under_ink(file_rel, page, strokes=strokes)
        except Exception:
            marked = ""
        note = ("下图=用户用笔标注的区域(已叠加他的手写笔迹)。结合笔迹的位置/形状/指向 + 图里文字,描述他到底圈/划/指/写了什么。"
                "若笔迹本身是**手写的字/算式/公式**:先看整体结构(有无分数线/上标/下标/等号)再逐个认字符,"
                "手写易混对(G↔A↔C、r↔n↔v、×↔x、9↔g、2↔z)用整体含义的合理性来定夺——比如分数结构『?mm/r²』里首字母是 G(万有引力)远比 A 合理;"
                "他在学的内容不限于本页主题(可能在空白处写任何学科的公式)。")
        if marked:
            note += f" 几何上他大概标的是:「{_clean_tag(marked)[:120]}」(仅参考,以图为准)。"
        prev = _clean_tag(str(ctx.get("prev_ink_desc") or ""))[:400]
        if prev:   # 对比模式:带上次描述——新增笔画常是**同一图形的补笔**(给花加瓣/加叶),别轻易当成独立新物体
            note += (f" ⚠此前这块区域的笔迹是:「{prev}」,用户在此基础上**又添加了笔画**。"
                     "请对比着讲**变化了什么**;注意新增笔画很可能是对原图形的补笔/修饰(比如给花加花瓣、给箭头加分叉),"
                     "先判断整体是否仍是一个图形,确实独立时才说是新的东西。")
        _fed = [{"media_type": "image/png", "b64": base64.b64encode(png).decode()}]
        if ctx.get("_want_vision"):   # ㉗:GPT Realtime 图像输入开 → 笔迹合成图直喂它自己看(不经视觉模型转述,更快更省一跳)
            return {"_vision": _fed, "_fed_images": _fed,
                    "看图提示": note, "说明": "笔迹区域合成图已直接发给你,结合看图提示自己看图回答"}
        desc = _vision_for(ctx, _fed, note)
        return {"笔迹标注描述": desc or "(看图失败,可重试)", "_fed_images": _fed}   # #8 实际发给AI的图,回卡显示
    except Exception as e:
        return {"error": str(e)[:140]}


def _t_undo_last(args, ctx):
    try:
        import voice
        r = voice._undo_do(None, owner=ctx.get("_uid"))   # 撤销自己最近一次没撤过的写操作(隔离)
        if isinstance(r, dict) and r.get("ok") and r.get("kind") in ("sticky", "sticky_edit"):
            r["client_action"] = {"fn": "notesReload", "args": []}   # 撤的是便签 → 前端重挂页面便签
        return r
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
        _mr, _lp = _vb_src(file_rel, pg)   # 合并书:生词按真实成员页查
        for m in pdf.page_unmastered_vocab(_mr, _lp):
            lem = m.get("lemma") or m.get("word")
            if lem and lem not in seen:
                seen[lem] = {"word": m.get("word"), "lemma": m.get("lemma"),
                             "mastery": m.get("mastery"), "level": m.get("level"), "page": pg}
    items = list(seen.values())
    return {"unmastered_on_page": items, "count": len(items),
            "note": "这是当前页你**还没掌握**的生词(=页面下划线词,来自掌握度数据库)。"
                    "不在此列表的词:要么已掌握、要么从没查过(系统不视为生词)。"
                    "回答『我没掌握哪些词/这页生词』就用这个列表,别拿正文里的词自己猜掌握与否。"}


def _hl_char_match(pdf, ap, rel, pg, needle):
    """search_for 失败时的兜底:**char 层归一化子串匹配** → 行合并 rects。
    治两类真实 miss(用户实测 highlighted:0):①句子跨换行(文字层"推\\n進" vs AI 给的连续"推進",
    search_for 匹配不上)②全半角/中点等微差(NFKC 归一)。命中返回 rects(page pt),否则 None。"""
    try:
        r = pdf._page_chars_cached(ap, rel, pg)
        chars = r[0] if r else None
    except Exception:
        chars = None
    if not chars:
        return None
    import unicodedata

    def _n(ch):
        ch = unicodedata.normalize("NFKC", ch)
        return "·" if ch in "·・•‧∙" else ch
    seq = [( _n(c.get("c") or ""), c) for c in chars
           if (c.get("c") or "").strip()]
    S = "".join(x[0] for x in seq)
    T = "".join(_n(ch) for ch in needle if not ch.isspace())[:120]
    if len(T) < 4:
        return None
    i = S.find(T)
    if i < 0:
        return None
    rows = []
    for _, c in seq[i:i + len(T)]:
        bb = (float(c.get("x0", 0)), float(c.get("y0", 0)), float(c.get("x1", 0)), float(c.get("y1", 0)))
        cy = (bb[1] + bb[3]) / 2
        for row in rows:
            if abs(cy - row[4]) < max(2.0, (bb[3] - bb[1]) * 0.6):   # 同一行(y 中心接近)→ 合并
                row[0] = min(row[0], bb[0]); row[1] = min(row[1], bb[1])
                row[2] = max(row[2], bb[2]); row[3] = max(row[3], bb[3])
                row[4] = (row[1] + row[3]) / 2
                break
        else:
            rows.append([bb[0], bb[1], bb[2], bb[3], cy])
    return [[round(r0[0], 2), round(r0[1], 2), round(r0[2], 2), round(r0[3], 2)] for r0 in rows] or None


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
    if args.get("page"):   # agent 批量标注:指定在哪页高亮(AI 给的是印刷页 → _to_pdf 转 PDF 索引,否则有 offset 的书标错页)
        try:
            pages = [_to_pdf(ctx, args["page"])]
        except (TypeError, ValueError):
            pages = []
    else:
        pages = [int(p) for p in (ctx.get("pages") or ([ctx.get("page")] if ctx.get("page") else [])) if p]
    _wm = _web_mat(file_rel)
    if _wm is not None:   # 网页(审计 #3):按文本在正文里的偏移写字符锚 sidecar,不走 PyMuPDF
        import html_reader as _HR, uuid as _u3, time as _t3
        _body = _wm.get("text") or ""
        _items = _HR._html_hl_load(file_rel) or []
        _made = []
        for _tx in texts:
            _i = _body.find(_tx)
            if _i < 0:
                continue
            _h = {"id": "h" + _u3.uuid4().hex[:10], "start": _i, "end": _i + len(_tx),
                  "text": _tx[:2000], "color": color, "note": "", "sentence": "",
                  "time": int(_t3.time())}
            _items.append(_h); _made.append(_h)
        if _made:
            _HR._html_hl_save(file_rel, _items)
        return ({"ok": True, "n": len(_made), "highlighted": [h["text"][:40] for h in _made],
                 "client_action": {"fn": "_assistEdit", "args": [{"type": "highlight", "file": file_rel,
                                                                  "items": _made}]}}
                if _made else {"error": "这些文字在网页正文里没找到(可能是图片内文字或已翻页)"})
    file_rel, pages = _vb_localize(file_rel, pages)   # 视图页→所在卷局部页(单本书恒等)
    if not file_rel:
        return {"error": "页越界"}
    try:
        import fitz
        ap = (VAULT_ROOT / file_rel).resolve()
        ap.relative_to(VAULT_ROOT.resolve())
        doc = fitz.open(str(ap))
        pdf = _pdf()
        db = pdf._hl_load(file_rel)
        ids, miss, created = [], [], []
        for t in texts:
            placed = False
            for pg in (pages or [1]):
                if pg < 1 or pg > doc.page_count:
                    continue
                p = doc[pg - 1]
                rects = p.search_for(t[:180])   # search_for 上限,长句截断匹配
                if rects:
                    nrects = [[round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2)] for r in rects]
                else:
                    nrects = _hl_char_match(pdf, ap, file_rel, pg, t)   # 兜底:char 层归一化匹配(跨换行/全半角/中点差异)
                    if not nrects:
                        continue
                hid = "h_" + os.urandom(6).hex()
                db["highlights"].append({
                    "id": hid, "page": pg, "rects": nrects,
                    "color": color, "text": t[:2000], "note": "", "kind": "note",
                    "sentence": "", "body": "", "page_w": p.rect.width, "page_h": p.rect.height,
                    "time": int(time.time()),
                })
                ids.append(hid)
                # 给前端自动「跳转 / 撤销·重做」卡片用:跳转用 PDF 索引,重做用这些字段重建
                created.append({"id": hid, "pdf_page": pg, "disp_page": _to_disp(ctx, pg),
                                "color": color, "text": t[:120], "rects": nrects,
                                "page_w": p.rect.width, "page_h": p.rect.height})
                placed = True
                break
            if not placed:
                miss.append(t[:18])
        doc.close()
        if ids:
            pdf._hl_save(file_rel, db)
        if not ids:   # 全 miss:必须是显式 error(曾静默返回 0 → AI 说"我先高亮一下"就结束,用户以为画了实际全无)
            return {"error": "一处都没画上——给的句子在该页文字层里找不到(必须**逐字来自 read_page 返回的原文**,"
                             "别改写/别翻译/别自行加标点)。没找到的:%s。可先 read_page 核对原文再重试。" % "、".join(miss),
                    "missed": miss}
        res = {"highlighted": len(ids), "missed": miss, "_created": created,
               # 自动弹「跳转+撤销/重做」卡片(系统在改动发生时生成,非 AI 生成)
               "client_action": {"fn": "_assistEdit", "args": [{"type": "highlight", "file": file_rel, "items": created}]},
               "_jump_page": (pages[0] if pages else None)}
        if ids:
            import voice
            res["undo_id"] = voice._undo_record("highlight", f"{len(ids)} 处高亮", {"file_rel": file_rel, "ids": ids}, owner=ctx.get("_uid"))
            if not (ctx or {}).get("_suppress_creation") and _creation_enabled(str(ctx.get("_uid") or ""), "highlight"):   # auto_highlight 逐页调我时由它汇总登;「记忆」开关可关
                try:   # 创造物库:**写操作也留痕**(用户实锤:"取消刚才你高亮的"——AI 没有"我高亮过什么"的记忆)。
                    _pgs = sorted({c0.get("disp_page") for c0 in created if c0.get("disp_page")})
                    _creation_add(str(ctx.get("_uid") or ""), "highlight",
                                  "在第 %s 页高亮了 %d 处" % ("、".join(str(x) for x in _pgs) or "?", len(ids)),
                                  content="\n".join("[%s] p%s %s" % (c0.get("id"), c0.get("pdf_page"), c0.get("text") or "") for c0 in created),
                                  anchor={"file": file_rel, "page": (pages[0] if pages else None)})
                except Exception:
                    pass
        return res
    except Exception as e:
        return {"error": str(e)[:120]}


def _t_read_highlights(args, ctx):
    """读某页/全书已有的高亮(标了哪些内容、颜色、备注)。批量标注前可先看,避免重复标;也用于回答『这页我高亮了什么』。
    args {page?}:不传=当前页;page=数字=该页;page=\"all\"=全书。"""
    file_rel = ctx.get("file_rel", "")
    if not file_rel:
        return {"error": "没开书"}
    try:
        hls = _vb_hls(file_rel)   # 合并书=各卷扇入+页全局化;非分卷=原样
        pg = args.get("page")
        if pg in ("all", "0", 0):
            pass   # 全书
        elif pg is not None:
            try:
                n = _to_pdf(ctx, int(pg)); hls = [h for h in hls if h.get("page") == n]   # AI 给的是印刷页 → 转 PDF 页再比(否则有 offset 的书永远找不到)
            except (TypeError, ValueError):
                pass
        else:   # 默认当前页(ctx.pages 已是 PDF 页,不转)
            cur = [int(p) for p in (ctx.get("pages") or ([ctx.get("page")] if ctx.get("page") else [])) if p]
            if cur:
                hls = [h for h in hls if h.get("page") in cur]
        out = [{"page": _to_disp(ctx, h.get("page")), "text": (h.get("text") or "")[:200],   # 报印刷页(跟用户/AI 一致)
                "color": h.get("color"), "note": (h.get("note") or "")[:160]} for h in hls]
        return {"count": len(out), "highlights": out}
    except Exception as e:
        return {"error": str(e)[:120]}


def _t_find_highlights(args, ctx):
    """用户想**删除/取消/清理**某些高亮时用:把匹配的高亮逐条列在对话里,每条带「跳转」+「删除」按钮让用户自己点。
    不替用户批量删(安全+可控:用户点哪条删哪条,也能先跳过去看)。
    args:不传=当前页;page=数字=该页;page=\"all\"=全书;pages=[13,14]=指定多页(整章/自序等跨页范围)。"""
    file_rel = ctx.get("file_rel", "")
    if not file_rel:
        return {"error": "没开书"}
    try:
        hls = _vb_hls(file_rel)   # 合并书=各卷扇入+页全局化;非分卷=原样
        pages = args.get("pages")
        pg = args.get("page")
        rfrom = args.get("from"); rto = args.get("to")
        if rfrom is not None and rto is not None:   # 印刷页范围(整章最常用,配合 toc):from~to
            try:
                a = _to_pdf(ctx, int(rfrom)); b = _to_pdf(ctx, int(rto))
                if a > b: a, b = b, a
                hls = [h for h in hls if a <= (h.get("page") if h.get("page") is not None else -1) <= b]
            except (TypeError, ValueError):
                pass
        elif isinstance(pages, list) and pages:
            want = set()
            for p in pages:
                try: want.add(_to_pdf(ctx, int(p)))   # 印刷页 → PDF 页
                except (TypeError, ValueError): pass
            hls = [h for h in hls if h.get("page") in want]
        elif pg in ("all", "0", 0):
            pass   # 全书
        elif pg is not None:
            try:
                n = _to_pdf(ctx, int(pg)); hls = [h for h in hls if h.get("page") == n]   # 印刷页 → PDF 页
            except (TypeError, ValueError):
                pass
        else:   # 默认当前页(ctx.pages 已是 PDF 页,不转)
            cur = [int(p) for p in (ctx.get("pages") or ([ctx.get("page")] if ctx.get("page") else [])) if p]
            if cur:
                hls = [h for h in hls if h.get("page") in cur]
        items = [{"id": h.get("id"),
                  "page": _to_disp(ctx, h.get("page")),   # 显示/报给 AI:印刷页(跟用户一致)
                  "pdf_page": h.get("page"),               # 跳转用:PDF 页(jumpWithBack 收 PDF 页)
                  "text": (h.get("text") or "")[:120], "color": h.get("color"),
                  "rects": h.get("rects")}                  # M9:删除后「↪ 重做」重建高亮要几何
                 for h in hls if h.get("id")]
        items.sort(key=lambda x: (x["pdf_page"], x["text"]))
        if not items:
            return {"count": 0, "note": "这个范围没有高亮,没什么可删的。"}
        return {"count": len(items),
                "note": (f"已在对话里逐条列出 {len(items)} 处高亮,每条都带「跳转」+「删除」按钮,用户自己点删即可。"
                         "你**别再说没有删除工具**;只需简短说一句『下面是这些高亮,可逐个跳转或删除』。"),
                "client_action": {"fn": "_showHlPicker", "args": [{"file_rel": file_rel, "items": items}]}}
    except Exception as e:
        return {"error": str(e)[:120]}


def _t_toc(args, ctx):
    """看这本书的**目录**(章节标题 + 起始印刷页)。把『第一章/某节/前言』映射到页范围用——比硬猜准。
    拿到后:某章范围 = 该章起始页 ~ (下一个同级条目的起始页 - 1);再配合 find_highlights(from,to) / goto_page / read_page。args {}。"""
    file_rel = ctx.get("file_rel", "")
    if not file_rel:
        return {"error": "没开书"}
    try:
        _wm = _web_mat(file_rel)
        if _wm is not None:
            # 网页:没有 PDF 书签。用正文里的短行当大纲(维基/文档站的小标题多是独立短行),
            # 给 AI 一个"这页有哪些部分"的骨架;page 恒 1(单文档)。
            _lines = [l.strip() for l in (_wm.get("text") or "").split("\n")]
            _out = [{"title": l[:60], "page": 1, "level": 1}
                    for l in _lines if 2 <= len(l) <= 30 and not l.endswith(("。", ".", "、", ","))][:60]
            return {"count": len(_out), "source": "web-outline", "entries": _out,
                    "note": "网页无书签,这是按正文短行推出的粗略大纲;要精确内容用 read_page。"}
        pdf = _pdf()
        entries, source = [], "none"
        for _mrel, _moff, _ in _vb_members(file_rel):   # 单本书=只循环一次、offset 0(统一书模型)
            _apm = pdf._safe_vault_path(_mrel)
            if not _apm:
                continue
            _es, _s2 = pdf._effective_toc(_apm, _mrel)   # page 已归一到印刷页(跟 AI/用户一致)
            if _es and source == "none":
                source = _s2
            for _e in _es:
                _e2 = dict(_e) if _moff else _e
                if _moff and isinstance(_e2.get("page"), (int, float)):
                    _e2["page"] = int(_e2["page"]) + _moff
                entries.append(_e2)
        if source == "none" and not entries and not any(pdf._safe_vault_path(m[0]) for m in _vb_members(file_rel)):
            return {"error": "书路径无效"}
        if not entries:
            return {"count": 0, "note": "这本书没录入目录(没建过也没原生书签)。定位章节改用 find_highlights(page=\"all\") 看高亮都在哪几页,或直接问用户大概页码。"}
        out = [{"title": (e.get("title") or "")[:80], "page": e.get("page"), "level": e.get("level", 1)} for e in entries if e.get("page") is not None]
        return {"count": len(out), "source": source, "entries": out[:300]}
    except Exception as e:
        return {"error": str(e)[:120]}


def _t_auto_highlight(args, ctx):
    """【专家外包式·整章标重点】主流程只下**一个**指令,本工具内部**逐页**把"这一页正文"单独外包给一个挑句专家
    (scoped 子调用,只给它这页文字 + "挑 1~3 句重点"的任务)→ 画高亮 → **只把简报回主流程**。
    → 每页正文**压根不进主编排循环**(不像主流程逐页 read_page+highlight 那样反复重发正文),整章高亮的 token 从
    O(页数×正文) 降到 O(页数×一句报告)。范围:from+to(印刷页区间,配合 toc 最常用)/ pages[列表] / page / 不传=当前页。"""
    file_rel = ctx.get("file_rel", "")
    if not file_rel:
        return {"error": "没开书"}
    pdf_pages = []
    rf, rt = args.get("from"), args.get("to")
    if rf is not None and rt is not None:
        try:
            a, b = _to_pdf(ctx, int(rf)), _to_pdf(ctx, int(rt))
            if a > b: a, b = b, a
            pdf_pages = list(range(a, b + 1))
        except (TypeError, ValueError):
            pass
    elif isinstance(args.get("pages"), list):
        for p in args["pages"]:
            try: pdf_pages.append(_to_pdf(ctx, int(p)))
            except (TypeError, ValueError): pass
    elif args.get("page") is not None:
        try: pdf_pages = [_to_pdf(ctx, int(args["page"]))]
        except (TypeError, ValueError): pass
    else:
        pdf_pages = [int(p) for p in (ctx.get("pages") or ([ctx.get("page")] if ctx.get("page") else [])) if p]
    pdf_pages = sorted(set(p for p in pdf_pages if p and p > 0))[:25]   # 上限 25 页,防一次爆量
    if not pdf_pages:
        return {"error": "没定位到要标的页(给 from/to 或 pages;不确定章范围先 toc)"}
    r = _resolve("summarize", ctx.get("_uid"))   # 用「总结」那档模型当挑句专家(默认 Gemini flash,便宜)
    if _paid_recover_check(ctx.get("_uid"), "summarize"):   # @paid 且免费恢复 → 摘除后重读(静默)
        r = _resolve("summarize", ctx.get("_uid"))
    color = (args.get("color") or "#fff59d")
    import re as _re
    # ① 读各页正文 + 查"挑句缓存"(同页正文同模型 → 命中=0 token);未命中的进 need 批处理
    page_text, picks, need = {}, {}, []
    for pg in pdf_pages:
        text = _page_text(file_rel, pg)
        if not text or len(text.strip()) < 12:
            page_text[pg] = None; continue
        page_text[pg] = text
        ck = _ai_cache_key("pick", r["variant"], r["depth"], _ai_cache_key(text))
        cached = None if (ctx or {}).get("_no_cache") else _ai_cache_get(ck)   # 感叹号「更强重答」跳过缓存重挑
        if cached is not None:
            picks[pg] = [str(s) for s in cached][:3]
        else:
            need.append((pg, text, ck))
    # ② 批处理:每 4 页一次叶子调用挑句(省每次调用的固定开销),返回 {页码:[句]}
    for bi in range(0, len(need), 4):
        batch = need[bi:bi + 4]
        prompt = (_tps(ctx.get("_uid") or "", "auto_highlight", "main") + "\n\n"   # 140:可在工具详情窗里改
                  + "\n\n".join(f"【页{pg}】\n{text[:3500]}" for pg, text, _ in batch))
        out = _deep_ask(prompt, backend=r["backend"], variant=r["variant"], depth=r["depth"])
        parsed = {}
        if out:
            mm = _re.search(r"\{.*\}", out, _re.S)
            if mm:
                try:
                    parsed = json.loads(mm.group(0))
                except Exception:
                    parsed = {}
        for pg, text, ck in batch:
            raw = parsed.get(str(pg)) if isinstance(parsed, dict) else None
            if raw is None and isinstance(parsed, dict):
                raw = parsed.get(f"页{pg}") or parsed.get(pg)
            sents = [str(s).strip() for s in raw if isinstance(s, str) and str(s).strip()][:3] if isinstance(raw, list) else []
            picks[pg] = sents
            if sents:
                _ai_cache_set(ck, sents)   # 存:挑句对同一页正文是确定性的 → 下次重标 0 token
    # ③ 逐页画高亮(本机 search_for+写,不调 AI)
    reports, total, created = [], 0, []
    for pg in pdf_pages:
        disp = _to_disp(ctx, pg)
        if page_text.get(pg) is None:
            reports.append({"page": disp, "n": 0, "skip": "无文字层/空页"}); continue
        sents = picks.get(pg) or []
        if not sents:
            reports.append({"page": disp, "n": 0, "skip": "没挑出重点"}); continue
        hr = _t_highlight({"texts": sents, "page": disp, "color": color}, dict(ctx or {}, _suppress_creation=True)) or {}
        n = int(hr.get("highlighted") or 0)
        total += n
        created.extend(hr.get("_created") or [])   # 汇总各页高亮 → 一张「跳转+撤销/重做」卡片
        reports.append({"page": disp, "n": n, "sentences": [s[:24] for s in sents]})
    try:   # 创造物库:写操作留痕(汇总一条,逐页子调用已抑制;「记忆」开关可关)
        _apgs = sorted({c0.get("disp_page") for c0 in created if c0.get("disp_page")})
        if created and _creation_enabled(str((ctx or {}).get("_uid") or ""), "auto_highlight"):
            _creation_add(str((ctx or {}).get("_uid") or ""), "highlight",
                          "自动标重点:第 %s 页共 %d 句" % ("、".join(str(x) for x in _apgs) or "?", total),
                          content="\n".join("[%s] p%s %s" % (c0.get("id"), c0.get("pdf_page"), c0.get("text") or "") for c0 in created)[:6000],
                          anchor={"file": file_rel})
    except Exception:
        pass
    return {"done": True, "total_highlighted": total, "pages": reports,
            "client_action": {"fn": "_assistEdit", "args": [{"type": "highlight", "file": file_rel, "items": created}]},
            "note": f"已逐页自动标重点,共 {total} 句(分布见 pages)。**别再逐页 read_page**;直接把'标了哪些页、共多少句'简洁告诉用户即可。"}


def _t_see_figure(args, ctx):
    """看用户**带入的图**(裁出图框区域的渲染图;有手写笔迹则看叠加合成图)。多张时 args{index}指定第几张(从1起),不传=全部(≤3)。
    已给的图文字说明不够、要核对图像里的具体细节时用。返回 _vision 喂回大脑。"""
    figs = ctx.get("figures") or ([ctx["figure"]] if ctx.get("figure") else [])
    figs = [f for f in figs if (f.get("box") or f.get("fbox") or (f.get("kind") == "note" and f.get("note_id")))]
    nid = (args.get("note_id") or "").strip() if isinstance(args, dict) else ""
    if nid:   # notes_read/notes_query 联动:AI 主动看某便签的合成图(不依赖用户双击带入)
        figs = [{"kind": "note", "note_id": nid}]
    if not figs:
        return {"error": "当前没有带入的图(让用户先点/拖一张图进来;要看某条便签的手写就传 note_id)"}
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
        def _figpg(f):
            return int((f.get("ref") or {}).get("page") or f.get("page") or 0)
        _pg0 = next((_figpg(f) for f in figs if _figpg(f)), 0)
        if _pg0:
            file_rel, _lp0 = _vb_src(file_rel, _pg0)   # 图在哪一卷(单本书恒等 → _d=0,下面全是空操作)
            _d = _pg0 - _lp0
            for f in figs:
                if _d and f.get("page"):
                    f["page"] = int(f["page"]) - _d
                if _d and (f.get("ref") or {}).get("page"):
                    f["ref"] = dict(f["ref"]); f["ref"]["page"] = int(f["ref"]["page"]) - _d
        ap = (VAULT_ROOT / file_rel).resolve(); ap.relative_to(VAULT_ROOT.resolve())
        vis = []; ink_any = False
        for fg in figs:
            if fg.get("kind") == "note" and fg.get("note_id"):   # 双击带入的手写便签:按 note_id 现场重合成(文字+笔画整体一张图,永远最新 sidecar)
                try:
                    png_n = pdf._note_composite_png(fg.get("file_rel") or file_rel, fg.get("note_id"))
                except Exception:
                    png_n = None
                if png_n:
                    vis.append({"media_type": "image/png", "b64": base64.b64encode(png_n).decode()})
                    ink_any = True
                continue
            _ref = fg.get("ref") or {}   # 统一 opaque ref 优先(设计 §8);旧字段 box/page 兜底
            box = _ref.get("box") or fg.get("box") or fg.get("fbox"); page = _ref.get("page") or fg.get("page") or ctx.get("page")
            if not box or not page:
                continue
            ink_strokes = fg.get("ink")    # 客户端随图带来的当前笔迹(优先,不依赖服务端保存时机)
            has_ink = bool(ink_strokes) or bool(fg.get("has_ink"))
            png = pdf.resolve_figure_image({"kind": "pdf", "path": ap, "page": int(page), "box": box, "rel": file_rel, "has_ink": has_ink}, ink_strokes)   # 统一中间层入口(设计 §8 步骤4);behavior 等价于原 _figure_crop_png
            vis.append({"media_type": "image/png", "b64": base64.b64encode(png).decode()})
            if has_ink:
                ink_any = True
        if not vis:
            return {"error": "图框无效"}
        note = "下面是用户带入的图的裁剪渲染图,描述图里的内容。"
        if any(f.get("kind") == "note" for f in figs):
            note += "（其中含用户的**手写便签**合成图:便签文字+手写笔画整体一张图,认清他写了/画了什么）"
        if ink_any:
            note += "（含用户手写笔迹的合成图,重点描述他圈点/标注了什么）"
        return {"图像描述": _vision_for(ctx, vis, note) or "(看图失败,可重试)"}
    except Exception as e:
        return {"error": str(e)[:140]}


# ── 便签(sticky notes)四工具:查询/读取/新建/编辑(阶段3 AI 工具;设计见 references/sticky-notes-design.md)。
#   数据层直接用 pdf_reader 的便签 sidecar(_notes_load/_notes_save,PDF/EPUB 同一套,anchor 不透明)。
#   kind 由注册方定:PDF 助手='pdf'(位置说印刷页),EPUB 助手='epub'(位置说 section idx),行为一致。
#   写工具的撤销卡(系统自动,非 AI 生成):
#     · PDF 侧走 client_action _assistEdit(type:'note')→ 会话内「撤销⇄重做」卡(同高亮 _t_highlight 先例);
#     · EPUB 侧走 res["action"] → epub_assistant 的持久 [查看详情/撤销/重做] 卡(落库 meta.actions,刷新后仍在),
#       undo/redo 由 /pdf/api/epub-action 的 sticky_delete/sticky_create/sticky_set op 执行。
#   两侧都另记 voice._undo_record(kind='sticky'/'sticky_edit')→ 聊天里说『撤销刚才那个』的 undo_last 也能撤。
_NOTE_COLOR_NAMES = {"#ffffff": "白", "#fff8c5": "黄", "#cfe3ff": "蓝", "#d5f2d9": "绿",
                     "#ffd9e8": "粉", "#2d3440": "石墨", "#1f3a2e": "墨绿"}   # 同 rc-stickynote.js COLORS 色板


def _note_color_label(hexv):
    h = (hexv or "").strip().lower()
    return _NOTE_COLOR_NAMES.get(h, h or "白")


def _note_color_arg(v):
    """query 的 color 参数:hex 原样;中文色名(白/黄/蓝/绿/粉/石墨/墨绿)映射回 hex;认不出返回原串(按 hex 比)。"""
    s = (v or "").strip().lower()
    if not s or s.startswith("#"):
        return s
    for h, n in _NOTE_COLOR_NAMES.items():
        if n == s or n in s:
            return h
    return s


def _note_color_norm(v, dflt="#ffffff"):
    """create/edit 的 color 参数 → 合法 hex(色名映射;认不出/没给 → dflt)。"""
    s = _note_color_arg(v)
    return s[:16] if (s and s.startswith("#")) else dflt


def _note_place(ctx, anchor, kind):
    """便签位置描述(给 AI/用户):PDF=印刷页,EPUB=section idx(EPUB 助手的口径)。"""
    a = anchor or {}
    if a.get("kind") == "epub" or (kind == "epub" and a.get("section") is not None):
        return f"第{a.get('section')}章(section idx)"
    return f"第{_to_disp(ctx, a.get('page'))}页"


def _t_notes_query(args, ctx, kind="pdf"):
    """查当前书的便签列表:color/keyword/page|section 三个过滤条件可组合,全空=列全部(上限 50 条)。"""
    file_rel = ctx.get("file_rel") or ""
    if not file_rel:
        return {"error": "没开书"}
    notes = _vb_notes(file_rel)   # 合并书:各卷扇入+anchor.page 全局化
    color = _note_color_arg(args.get("color"))
    kw = (args.get("keyword") or "").strip().lower()
    loc = args.get("section") if kind == "epub" else args.get("page")
    want = None
    if loc is not None:
        try:
            want = int(loc) if kind == "epub" else _to_pdf(ctx, int(loc))
        except (TypeError, ValueError):
            want = None
    out = []
    for n in notes:
        a = n.get("anchor") or {}
        if want is not None and (a.get("section") if kind == "epub" else a.get("page")) != want:
            continue
        if color and (n.get("color") or "#ffffff").strip().lower() != color:
            continue
        if kw and kw not in (n.get("text") or "").lower():
            continue
        out.append({"id": n.get("id"), "color": _note_color_label(n.get("color")),
                    "位置": _note_place(ctx, a, kind),
                    "text": (n.get("text") or "").replace("\n", " ").strip()[:60],
                    "has_ink": bool(n.get("strokes"))})
    return {"count": len(out), "notes": out[:50],
            "note": "text 是摘要(≤60字),看全文用 notes_read(id)。has_ink=true 的含手写笔画(文字字段读不到),"
                    "要看手写内容用 see_figure(args {note_id:该便签id})看合成图。"}


def _t_notes_read(args, ctx, kind="pdf"):
    """读某条便签全文 + 位置;含手写时提示用 see_figure(note_id) 看合成图。"""
    file_rel = ctx.get("file_rel") or ""
    if not file_rel:
        return {"error": "没开书"}
    nid = (args.get("id") or "").strip()
    if not nid:
        return {"error": "缺 id(先 notes_query 拿便签 id)"}
    n = next((x for x in _vb_notes(file_rel) if x.get("id") == nid), None)
    if not n:
        return {"error": "没找到这个 id 的便签(先 notes_query 拿最新列表)"}
    out = {"id": nid, "位置": _note_place(ctx, n.get("anchor"), kind),
           "color": _note_color_label(n.get("color")),
           "text": (n.get("text") or "")[:4000] or "(无文字)"}
    if n.get("strokes"):
        out["has_ink"] = True
        out["note"] = (f"此便签**含手写内容**(文字字段读不到笔画画了什么);"
                       f"要看手写就调 see_figure(args {{\"note_id\":\"{nid}\"}}),会给你文字+笔画整体合成图的描述。")
    return out


def _t_notes_create(args, ctx, kind="pdf"):
    """建便签(anchor 按 reader 类型组装 x/y 比例锚;EPUB 前端 mount 时懒迁移自动升级成内容锚)。写后带撤销卡。"""
    file_rel = ctx.get("file_rel") or ""
    if not file_rel:
        return {"error": "没开书"}
    text = (args.get("text") or "").strip()
    if not text:
        return {"error": "缺 text(便签内容)"}

    def _f01(v, dflt):
        try:
            return min(0.98, max(0.0, float(v)))
        except (TypeError, ValueError):
            return dflt
    x, y = _f01(args.get("x"), 0.72), _f01(args.get("y"), 0.25)
    if kind == "epub":
        try:
            sec = int(args["section"]) if args.get("section") is not None else int(ctx.get("current_section_idx") or 0)
        except (TypeError, ValueError):
            sec = int(ctx.get("current_section_idx") or 0)
        anchor = {"kind": "epub", "section": max(0, sec), "x": x, "y": y}
    else:
        if args.get("page") is not None:
            try:
                pg = _to_pdf(ctx, int(args["page"]))
            except (TypeError, ValueError):
                return {"error": "page 不是数字(传印刷页码)"}
        else:
            vis = ctx.get("pages") or ([ctx.get("page")] if ctx.get("page") else [])
            pg = int(vis[0]) if vis else 0
        if not pg or pg < 1:
            return {"error": "不知道贴哪页(传 page=印刷页码)"}
        anchor = {"kind": "pdf", "page": pg, "x": x, "y": y}
    place = _note_place(ctx, anchor, kind)
    color = _note_color_norm(args.get("color"))   # 缺省白(同前端 DEFAULT_COLOR);色名自动映射 hex
    import uuid as _u
    pdf = _pdf()
    now = int(time.time())
    n = {"id": "n" + _u.uuid4().hex[:11], "anchor": anchor, "text": text[:8000], "color": color,
         "w": 260, "h": 180, "collapsed": False, "strokes": [], "created": now, "updated": now}
    _view_rel, _view_pg = file_rel, anchor.get("page")   # 前端活在视图坐标(单本书=同一个值)
    if anchor.get("kind") == "pdf" and anchor.get("page"):
        file_rel, _lp = _vb_src(file_rel, anchor["page"])   # 落盘:贴到真正那一卷(局部页=持久真相;单本恒等)
        if _lp != anchor["page"]:
            anchor = dict(anchor); anchor["page"] = _lp
            n["anchor"] = anchor
    items = pdf._notes_load(file_rel)
    items.append(n)
    pdf._notes_save(file_rel, items)
    try:
        import voice
        voice._undo_record("sticky", "便签「" + text.replace("\n", " ")[:14] + "」",
                           {"file_rel": file_rel, "ids": [n["id"]]}, owner=ctx.get("_uid"))
    except Exception:
        pass
    res = {"ok": True, "id": n["id"],
           "note": f"已在{place}创建便签(页面会自动刷新显示;下方自动出现撤销卡,你不用再教用户怎么撤销)。"}
    if kind == "epub":
        res["action"] = {"id": "act_nnew_" + n["id"], "kind": "notes_create",
                         "title": "新建便签:" + text[:24].replace("\n", " "),
                         "detail": f"位置:{place}\n颜色:{_note_color_label(color)}\n内容:{text[:600]}",
                         "undo": {"op": "sticky_delete", "file": file_rel, "ids": [n["id"]]},
                         "redo": {"op": "sticky_create", "file": file_rel, "notes": [n]},
                         "state": "done", "ts": now}
        res["client_action"] = {"fn": "notesReload", "args": []}
    else:
        _vn = dict(n)   # 给前端的副本:anchor 用视图页(合并书里前端按全局页排版)
        if _view_pg is not None:
            _vn["anchor"] = dict(anchor); _vn["anchor"]["page"] = _view_pg
        res["client_action"] = {"fn": "_assistEdit", "args": [{
            "type": "note", "op": "create", "file": _view_rel,
            "items": [{"id": n["id"], "pdf_page": _view_pg, "disp_page": _to_disp(ctx, _view_pg),
                       "note": _vn}]}]}
    return res


def _t_notes_edit(args, ctx, kind="pdf"):
    """改便签的文字/颜色。**工具层硬拦**:只收 text/color 两个字段,strokes/anchor/尺寸是用户数据绝不改
    (不只是 prompt 约束——实现里根本不读那些参数)。写前存旧值快照,写后带撤销卡(撤销=恢复旧 text/color)。"""
    file_rel = ctx.get("file_rel") or ""
    if not file_rel:
        return {"error": "没开书"}
    nid = (args.get("id") or "").strip()
    if not nid:
        return {"error": "缺 id(先 notes_query 拿便签 id)"}
    new_text, new_color = args.get("text"), args.get("color")
    if new_text is None and new_color is None:
        return {"error": "text / color 至少给一个(只能改文字和颜色;手写笔画/位置/尺寸不能动)"}
    pdf = _pdf()
    _own, _ = _vb_note_owner(file_rel, nid)   # 这条便签落在哪一卷(单本书=它自己)
    if not _own:
        return {"error": "没找到这个 id 的便签(先 notes_query 拿最新列表)"}
    file_rel = _own
    items = pdf._notes_load(file_rel)
    n = next((x for x in items if x.get("id") == nid), None)
    if not n:
        return {"error": "没找到这个 id 的便签(先 notes_query 拿最新列表)"}
    old = {"text": n.get("text") or "", "color": n.get("color") or "#ffffff"}   # 旧值快照(撤销用)
    if new_text is not None:
        n["text"] = str(new_text)[:8000]
    if new_color is not None:
        n["color"] = _note_color_norm(new_color, dflt=old["color"])   # 色名自动映射;认不出保持原色
    now = int(time.time())
    n["updated"] = now
    pdf._notes_save(file_rel, items)
    new = {"text": n.get("text") or "", "color": n.get("color") or "#ffffff"}
    try:
        import voice
        voice._undo_record("sticky_edit", "改便签「" + (new["text"] or old["text"]).replace("\n", " ")[:14] + "」",
                           {"file_rel": file_rel, "id": nid, "old": old}, owner=ctx.get("_uid"))
    except Exception:
        pass
    res = {"ok": True, "id": nid,
           "note": "便签已更新(只改了文字/颜色,手写笔画和位置不受影响;下方自动出现撤销卡)。"}
    if kind == "epub":
        res["action"] = {"id": "act_nedit_" + nid + "_" + os.urandom(3).hex(), "kind": "notes_edit",
                         "title": "修改便签:" + (new["text"] or old["text"])[:24].replace("\n", " "),
                         "detail": (f"位置:{_note_place(ctx, n.get('anchor'), kind)}\n"
                                    f"改前:[{_note_color_label(old['color'])}]{old['text'][:300]}\n"
                                    f"改后:[{_note_color_label(new['color'])}]{new['text'][:300]}"),
                         "undo": {"op": "sticky_set", "file": file_rel, "id": nid, "fields": old},
                         "redo": {"op": "sticky_set", "file": file_rel, "id": nid, "fields": new},
                         "state": "done", "ts": now}
        res["client_action"] = {"fn": "notesReload", "args": []}
    else:
        a = n.get("anchor") or {}
        res["client_action"] = {"fn": "_assistEdit", "args": [{
            "type": "note", "op": "edit", "file": file_rel,
            "items": [{"id": nid, "pdf_page": a.get("page"),
                       "disp_page": (_to_disp(ctx, a.get("page")) if a.get("page") else None),
                       "old": old, "new": new}]}]}
    return res


def _t_search_all_books(args, ctx):
    """跨『我所有的书』全文搜索(SQLite FTS5 全局索引)。用户问『我哪本书讲过 X / 别的书有没有 X / 之前在哪见过』时用。"""
    import sqlite3
    q = (args.get("query") or "").strip()
    if not q:
        return {"error": "缺 query:必须带要搜的关键词重新调用,如 {\"tool\":\"search_all_books\",\"args\":{\"query\":\"关键词\"}}"}
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
    _wm = _web_mat(file_rel)
    if _wm is not None:   # 网页=单文档:整篇正文交给上层总结(没有"章节切片"可言)
        _t = (_wm.get("text") or "")[:9000]
        if not _t.strip():
            return {"error": "这个网页没抓到正文(可能是纯 JS 页面)"}
        return {"section_title": _wm.get("title") or "整页", "page_range": "整篇",
                "text": _t, "note": "网页是单文档,这里给的是整篇正文。"}
    try:
        page = (_to_pdf(ctx, args["page"]) if args.get("page")           # args.page 是印刷页→转 PDF 找章节
                else int((ctx.get("pages") or [ctx.get("page")])[0] or 1))   # ctx 已是 PDF 页
    except Exception:
        page = 1
    file_rel, page = _vb_src(file_rel, page)   # 合并书:定位到所在卷,按该卷 TOC 切章
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
        # 生成步:章节总结是真正需要强模型的活。后端/型号/深度:用户给「summarize」预设了就用它,否则默认 Gemini Flash·think。
        _r = _resolve("summarize", ctx.get("_uid"))
        if _paid_recover_check(ctx.get("_uid"), "summarize"):   # @paid 且免费恢复 → 摘除后重读(静默)
            _r = _resolve("summarize", ctx.get("_uid"))
        _ck = _ai_cache_key("summ", _r["backend"], _r["variant"], _r["depth"], _ai_cache_key(section_text))  # 同章同模型 → 命中缓存,0 token
        gen = None if (ctx or {}).get("_no_cache") else _ai_cache_get(_ck)   # 感叹号「更强重答」跳过缓存重做
        if not gen:
            gen = _deep_ask(
                f"下面是《{ctx.get('book_name', '')}》「{title}」(第{_to_disp(ctx, start)}-{_to_disp(ctx, end)}页)的正文。"
                "请用中文给出**结构化总结**:① 核心要点(分条)② 关键定义 ③ 重要公式(用 $...$)④ 易错点。"
                "引用具体内容时句末标来源页「(第N页)」。简洁但完整,别遗漏主线。\n\n正文:\n" + section_text,
                backend=_r["backend"], variant=_r["variant"], depth=_r["depth"])
            if gen and len(gen.strip()) > 80:   # 质量闸:太短(疑似截断/出错)不入缓存;够长才存(感叹号重做也会覆盖旧版)
                _ai_cache_set(_ck, gen.strip())
        if gen and gen.strip():
            return {"section_title": title, "page_range": [_to_disp(ctx, start), _to_disp(ctx, end)], "summary": gen.strip(),
                    "_gen_model": f"{_variant_short(_r['variant'])}·{_r['depth']}", "_gen_action": "summarize",
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


# ══════════ 分步造纸(阶段 B+,用户拍板)══════════
# 根因:让对话模型**一次手打整个 blocks 嵌套数组**(几十行 JSON)太脆 —— parse 常残缺 → 建空纸 →
#   AI 反复重试还编「权限没授权」的借口(实测)。改成:AI 每次只加一个元素(一小段 JSON,不会烂),
#   服务端攒成草稿,最后一次性铺纸。这正是 ADR「AI 一次加一个元素,不手搓大结构」的方向。
_PAGE_DRAFTS = {}   # uid → {"paper":.., "title":.., "blocks":[..]}(内存草稿,就一次造纸过程,不落盘)


def _pd_key(ctx):
    return str((ctx or {}).get("_uid") or (ctx or {}).get("uid") or "?")


def _t_page_new(args, ctx):
    """开一张**新草稿纸**(造纸第一步)。args {paper?: dictation|exam|math|draw|note(默认note), title?}。
    之后用 page_add 逐个加元素,最后 page_show 生成。"""
    _PAGE_DRAFTS[_pd_key(ctx)] = {"paper": (args.get("paper") or "note"),
                                  "title": (args.get("title") or "")[:60], "blocks": []}
    return {"草稿已开": (args.get("title") or "新纸"),
            "下一步": "用 page_add 加元素(**可一次批量** page_add(blocks=[…]) 一把加完,更好安排位置),再 page_show 生成。", "silent": True}


# 元素字段白名单:kind/内容 + style + 定位(at/cols/span)+ 按钮态(event/enabled)。
_BLOCK_KEYS = ("kind", "text", "style", "label", "answer", "event", "id", "enabled", "at", "cols", "span", "options", "node_id", "layer", "picked")
_BLOCK_KINDS = ("text", "blank", "checkbox", "button", "hr", "choice")


def _norm_block(a, idx):
    b = {k: v for k, v in a.items() if k in _BLOCK_KEYS}
    # 顽强容错:AI 常把内容放错字段(text 块的题目写进 label,或反之)。text/hr 用 `text`,
    #   blank/button/checkbox 用 `label` —— 放错就自动搬到正确字段(否则渲染不出=用户实测"page_add 失败")。
    kind = b.get("kind")
    _t = str(b.get("text") or "").strip()
    _l = str(b.get("label") or "").strip()
    if kind in ("text", "hr", "choice"):   # choice 题干也用 text
        if not _t and _l:
            b["text"] = b.pop("label")
    else:   # blank / button / checkbox 用 label
        if not _l and _t:
            b["label"] = b.pop("text")
    if kind == "choice":
        opts = b.get("options")
        if isinstance(opts, str):
            opts = [x.strip() for x in opts.split("/") if x.strip()]
        if not isinstance(opts, list) or not opts:
            b["kind"] = "text"          # 没给选项 → 降级普通文字,别渲染出空壳
            b.pop("options", None)
        else:
            import re as _re2
            b["options"] = [_re2.sub(r"^[A-Da-d][\.、\)]\s*", "", str(o)).strip() for o in opts[:6]]
            if b.get("answer"):
                b["answer"] = str(b["answer"]).strip().upper()[:1]
    b.setdefault("id", "b%d" % idx)
    return b


def _t_page_add(args, ctx):
    """往草稿纸**加元素**(造纸第二步)。**可一次一个,也可一次一批**(args.blocks=[…])。
    单个元素例:
      {kind:'text', text:'写出假名', style?:'h1'}
      {kind:'blank', label?:'1.', answer?:'ばら'}          # answer 给了 check 时判对错
      {kind:'choice', text:'题干', options:['81.47歳','85.57歳','87.57歳','90.57歳'], answer:'C'}
        # ★选择题**必须用 choice**(题干/选项/作答线分层排版,自动换行);
        #   把"题干+A.xx B.xx"塞进 blank 的 label 会挤成一行被截断
      {kind:'checkbox', label:'我写完了'}
      {kind:'button', label:'让 AI 检查', event:'check', enabled?:true}
    批量(#49 D,一次决定所有元素、好安排彼此位置):{blocks:[{…},{…},…]}
    ★ 每个元素可选:
      at:[行,列]   精确定位(B 模式;不给=按顺序自动流式排,碰撞自动下移)
      enabled:false 按钮初始**禁用/不可点**(#36 显示状态;之后可被 set_enabled 事件打开)
      event 内置动作:check | say:话 | goto:页 | reveal:块id | hide:块id | call:工具名(按钮触发)
    """
    d = _PAGE_DRAFTS.get(_pd_key(ctx))
    if not d:
        return {"error": "还没开草稿。先调 page_new。"}
    batch = args.get("blocks")
    items = batch if isinstance(batch, list) else [args]
    added = 0
    for a in items:
        if not isinstance(a, dict):
            continue
        if a.get("kind") not in _BLOCK_KINDS:
            return {"error": "kind 只能是 text/blank/checkbox/button/hr(第 %d 个是 %r)" % (added + 1, a.get("kind"))}
        d["blocks"].append(_norm_block(a, len(d["blocks"])))
        added += 1
    if not added:
        return {"error": "没加成任何元素(检查 kind;批量用 args.blocks=[…])"}
    return {"已加": added, "当前共": len(d["blocks"]), "silent": True}


def _t_save_intent_tool(args, ctx):
    """把一个想法**直接铸成新工具**(意图配方,kind:'intent')——用户说「做一个工具:…/把 X 升级成
    带 AI 的工具/保存这个思路当工具」时用(用户设计:简单工具可组合升级成复杂工具+AI 功能)。
    args {name: 短名, instruction: 这个工具要做什么(把用户想法完整写清楚:步骤/内容来源/产出形式)}。
    运行时 = CLI 按该意图+当次调整在当前上下文执行(天然带 AI 创作)。"""
    import task_runtime as TR
    import re as _re
    name = _re.sub(r"[^\w一-鿿-]", "", str(args.get("name") or ""))[:60]
    instr = str(args.get("instruction") or "").strip()
    if not name or len(instr) < 8:
        return {"error": "要 name(短名)和 instruction(这个工具做什么,写清楚步骤与产出)"}
    TR.RECIPES_DIR.mkdir(parents=True, exist_ok=True)
    _new = not (TR.RECIPES_DIR / (name + ".json")).exists()
    if not _new and not args.get("overwrite"):   # 撞名拒绝静默覆盖(审查:一次好心铸造可毁掉精心调好的工具)
        return {"error": "已有同名工具《%s》。要覆盖就把选择权交给用户:确认后再调我并带 overwrite:true,"
                         "或者换个名字。" % name}
    if not _new:
        TR._recipe_snapshot(name)   # 覆盖前快照进 _history(可回滚)
    rec = {"name": name, "desc": instr[:80], "kind": "intent", "instruction": instr[:2000],
           "origin": "", "route": "", "partial": False, "calls": [],
           "inputs": {}, "owner": str((ctx or {}).get("_uid") or ""),
           "created": int(time.time()), "updated": int(time.time())}
    (TR.RECIPES_DIR / (name + ".json")).write_text(json.dumps(rec, ensure_ascii=False), "utf-8")
    TR._extract_inputs_async(name, instr)   # 参数槽后台补写(不阻塞铸造)
    _sys_cache_reset()   # 已存工具清单在系统提示静态段里
    return {"已保存": name, "新建" if _new else "覆盖": True,
            "note": "已铸成**重新生成型**工具《%s》(工具库/列表可见)。运行=run_saved_task(name, adjust?)。"
                    "告诉用户已保存,并用一两句话说明它运行时会怎么做。" % name}


def _t_run_saved_task(args, ctx):
    """运行一个**已保存的工具**(用户之前保存的任务,如「日语听写」)。
    args {name: 工具名, source?: 数据源(如"高亮"/"未掌握词",工具有多个来源时选一个), params?}。
    工具有哪些、各有什么数据源,用 list_saved_tasks 查。"""
    import task_runtime as TR
    name = (args.get("name") or "").strip()
    rec = TR._load_recipe(name)
    if not rec:
        return {"error": "没有叫「%s」的已保存工具。用 list_saved_tasks 看有哪些。" % name}
    rel = (ctx or {}).get("file_rel") or ""
    if not rel or not rel.lower().endswith(".pdf"):
        return {"error": "目前只支持 PDF 阅读器"}
    # ★intent 型(重新生成):不回放字面轨迹——重新起 CLI,指令=原意图+本次调整+当前上下文。
    #   (用户点破:10题工具要15题、换页出新题,只有"AI 重新在场"才成立;那时 AI 的上下文=一次全新造纸会话。)
    if rec.get("kind") == "intent":
        adjust = args.get("adjust") or ""
        _pv = args.get("params") or args.get("source") or ""
        if isinstance(_pv, dict) and _pv:   # 参数槽(rec.inputs)结构化注入,比自由文本稳(审查:inputs 原是死字段)
            adjust = ("本次参数:" + ";".join("%s=%s" % (k, v) for k, v in list(_pv.items())[:6]) +
                      ("。" + str(adjust).strip() if str(adjust).strip() else ""))
        elif _pv and not adjust:
            adjust = _pv
        adjust = str(adjust).strip()
        rr = _resolve("paper", str(ctx.get("uid") or ctx.get("_uid") or ""))
        route = (rec.get("route") or "").strip()
        _origin = str(rec.get("origin") or "").strip()
        if rec.get("partial") and _origin:   # 节选:origin(按路线总结的自洽意图)是唯一意图,原始 instruction 含被框掉的语义不给
            _part = ("本工具是从更大任务中**节选**的子流程。它的意图:『%s』。"
                     "执行范围**严格以下面的操作路线为准**——路线里没有的步骤类型(比如没有查高亮的步骤就绝不查高亮、"
                     "没有做卡的步骤就绝不做卡)**一律不要做**。\n" % _origin)
        elif rec.get("partial"):
            _part = ("⚠ 本工具只保存了原任务的**一部分步骤**(用户框选):执行范围**严格以下面的操作路线为准**,"
                     "路线里没有的步骤类型**一律不要做**。\n")
        else:
            _part = ""
        _head = ("运行已保存工具《%s》。\n" % name) if rec.get("partial") else \
                ("运行已保存工具《%s》。它的**原始意图**:『%s』。\n" % (name, rec.get("instruction") or ""))
        instr = (_head + _part +
                 (("本次用户的调整(与原意图冲突时**以本次为准**):%s。\n" % adjust) if adjust else "") +
                 (("这是它上次**成功执行的操作路线**(已验证的指挥棒;**严格按此步骤顺序与结构执行**,"
                   "内容按本次要求重新生成,[可调]数字按本次调整):\n%s\n" % route) if route else "") +
                 "在**当前上下文**执行:先读当前页,内容基于当前页**重新生成**(不要照搬任何旧题面);"
                 "数量/难度/形式按本次调整优先。")
        return _bg_task("agent", {"instruction": instr, "backend": rr["backend"],
                                  "model": rr["variant"], "effort": rr["depth"],
                                  "recipe": name}, ctx)   # recipe:收尾回写运行履历
    # 选数据源(合并型工具有 sources_menu)
    # trace 型(CLI 执行轨迹)→ 进程内回放整串工具(去壳),收集 client_action 给前端应用
    if rec.get("kind") == "trace":
        r = TR.run_trace(rec, {"file_rel": rel, "page": (ctx or {}).get("page"), "_uid": (ctx or {}).get("_uid")})
        cas = r.get("client_actions") or []
        if not r.get("ok"):   # 回放失败要明说(去 silent),别让"已运行"骗过编排 AI(审查实锤:步步失败也报成功)
            _f = "; ".join("%s(%s)" % (x.get("tool"), x.get("error")) for x in (r.get("failed") or [])[:3])
            out = {"error": "工具《%s》回放失败:%s。如实告诉用户,别说已完成。" % (name, _f or "没有任何产出")}
            if cas:
                out["client_actions"] = cas
                out["error"] += "(部分步骤有产出,页面上可能已出现部分内容)"
            return out
        out = {"已运行": name, "silent": True}
        if len(cas) == 1:
            out["client_action"] = cas[0]
        elif cas:
            out["client_actions"] = cas
        return out
    # page/flow 型(交互纸)→ 建纸起状态机
    src = args.get("source") or ""
    menu = rec.get("sources_menu") or {}
    sources = {}
    if menu:
        chosen = menu.get(src) or (list(menu.values())[0] if len(menu) == 1 else None)
        if not chosen:
            return {"error": "工具「%s」要选数据源,有:%s" % (name, "/".join(menu.keys()))}
        sources = {"words": chosen}
    params = dict(args.get("params") or {})
    params.update({"recipe": name, "sources": sources, "paper": rec.get("paper")})
    return {"已启动": name, "接下来": "系统会在你当前位置建纸并开始;你按纸上按钮推进。",
            "client_action": {"fn": "__upStartTask",
                              "args": [{"kind": "recipe", "title": name, "paper": rec.get("paper") or "note",
                                        "params": params}]},
            "silent": True}


def _t_list_saved_tasks(args, ctx):
    """列出所有**已保存的工具**及其数据源。args {}。"""
    import task_runtime as TR
    lst = TR.list_recipes()
    if not lst:
        return {"结果": "还没有保存过工具。做完一个任务后点卡片上的「保存为工具」即可。"}
    return {"已保存的工具": [{"名字": x["name"],
                             "类型": ("重新生成型(可 adjust 调数量/难度,在当前页重新出内容)" if x.get("kind") == "intent" else "机械回放型"),
                             "意图": (str(x.get("instruction") or "")[:80] or None),
                             "数据源": x.get("sources") or x.get("inputs") or []} for x in lst]}


def _t_page_show(args, ctx):
    """把草稿纸**生成出来**(造纸最后一步):在当前位置插一张纸,起运行时。args {}。"""
    d = _PAGE_DRAFTS.pop(_pd_key(ctx), None)
    if not d or not d["blocks"]:
        return {"error": "草稿是空的(先 page_new + page_add)。"}
    rel = (ctx or {}).get("file_rel") or ""
    if not rel or not rel.lower().endswith(".pdf"):
        return {"error": "目前只支持 PDF 阅读器"}
    # 兜底(#2 纸上没按钮):有填空/作答块却没按钮 → 自动补一个「让 AI 检查」按钮,
    #   不依赖 CLI 记得加。答题纸必有批改入口;纯笔记/绘画纸(无 blank)不加。
    kinds = {b.get("kind") for b in d["blocks"]}
    if "blank" in kinds and "button" not in kinds:
        d["blocks"].append({"kind": "button", "label": "让 AI 检查", "event": "check",
                            "id": "b_check%d" % len(d["blocks"])})
    return {"已生成": "%d 个元素" % len(d["blocks"]),
            "接下来": "系统会在你当前位置插一张纸(即时出现,不用刷新)。用户填/写完点纸上的按钮。",
            "client_action": {"fn": "__upStartTask",
                              "args": [{"kind": "free", "title": d["title"], "paper": d["paper"],
                                        "params": {"blocks": d["blocks"], "paper": d["paper"], "title": d["title"]}}]},
            "silent": True}


def _t_start_dictation(args, ctx):
    """出题 + **遥控前端**建一张听写纸。

    ⚠ **不在后端建页**(用户拍板):前端已有一套乐观新建链路(立刻插一个虚拟页、马上可用,
      PDF 写回后台异步跑)—— 不刷新、不卡顿。后端同步插页会在大书上卡好几秒,
      还会让浏览器里那份 PDF 作废(页数变了)。前端建完页会回调 /pdf/api/run-start 起运行时。

    这是 LLM 在整个听写里**唯一的一次参与**(出题);之后的「念一个 → 等你写 → 念下一个 → 批改」
    全由 task_runtime 的状态机跑(见 references/adr-task-runtime.md 的铁律一)。
    """
    words = [str(w).strip() for w in (args.get("words") or []) if str(w).strip()][:50]
    if not words:
        return {"error": "没给要听写的词(args.words)"}
    rel = (ctx or {}).get("file_rel") or ""
    if not rel or not rel.lower().endswith(".pdf"):
        return {"error": "听写目前只支持 PDF 阅读器(EPUB 插入页是虚拟段,坐标系不同,还没做)"}
    title = (args.get("title") or "听写")[:60]
    return {"已出题": f"{len(words)} 个词",
            "接下来": "系统会在你当前位置插一张听写纸(即时出现,不用刷新)。"
                      "点纸上的「▶ 念下一个」开始:它念一个、你手写一个,最后点「✓ 写完了,批改」。"
                      "你(AI)不用再管了 —— 循环和批改由系统负责。",
            "client_action": {"fn": "__upStartTask",
                              "args": [{"kind": "dictation", "title": title,
                                        "params": {"words": words, "title": title}}]},
            "silent": True}



def _t_material_graph(args, ctx):
    """展开一份材料的**关系链条**(卡片↔源笔记↔书页↔KG知识点↔前置根源),从任意位置出发、任意方向。
    配合 relate_material(找到材料)+ read_material(读某层内容):这个给「链条结构」,让你能挑任意一层去读。
    args {ref: 材料地址, direction?: up(来源)|down(派生/前置)|both(默认), depth?: 展开几跳(默认3)}。
    典型:「这张老错的卡背后的知识点前置是什么」→ material_graph(卡ref, down) → 看到前置节点 → read_material 读它。"""
    import sys as _sys
    _sys.path.insert(0, "/home/bwicarus/claude/scripts")
    try:
        import attention_profile as AP
    except Exception as e:
        return {"error": "注意力画像不可用:%s" % str(e)[:80]}
    ref = str(args.get("ref") or "").strip()
    if not ref:
        return {"error": "要给材料地址 ref"}
    return AP.material_graph(ref, direction=(args.get("direction") or "both"),
                             depth=max(1, min(int(args.get("depth") or 3), 5)))



def _t_read_material(args, ctx):
    """读一份材料的**详细内容**(配合 relate_material:先找到材料 → 再读它)。
    args {ref: 材料地址(relate_material 返回的 ref,如 anki:123 / note:x.md / book:资源/..#p9)}。
    anki 卡返回正反面、note 返回笔记全文、book 返回那页正文。检查报告请用 read_check_report。"""
    import sys as _sys
    _sys.path.insert(0, "/home/bwicarus/claude/scripts")
    try:
        import attention_profile as AP
    except Exception as e:
        return {"error": "注意力画像不可用:%s" % str(e)[:80]}
    ref = str(args.get("ref") or args.get("material") or "").strip()
    if not ref:
        return {"error": "要给材料地址 ref(从 relate_material 的结果里拿)"}
    return AP.read_material(ref)



def _t_relate_material(args, ctx):
    """回答**「关于 X 我学过/关注过哪些材料」「某个知识点我在哪些地方碰到过」**类问题:
    给一个词/概念,返回我**实际关注过**的材料(书页/笔记/Anki牌组/检查报告),按相关度排序、带出处。
    数据 = 所有学习行为的时间表(注意力画像),跨语言归一(中文词也能找到英/日原文材料)。

    args:
      term   必填,要查的词/概念(如「子空间」「公衆衛生」「vector space」)
      order  'relevance'(默认,最相关在前;半年前学的高相关材料也找得到)| 'recent'(最近碰的在前)
      when? / days?  只看某时间段(不传=全部历史)
      top?   要几份(默认 10)
    典型:「关于子空间我学过啥」「向量空间我在哪些资料里见过」「我最近碰的公衆衛生材料」→ 调我。
    ⚠ 这是「我关注过的」材料(有行为证据);不是全文搜索(那找「存在这个词的所有页,含没看过的」)。
    """
    import sys as _sys
    _sys.path.insert(0, "/home/bwicarus/claude/scripts")
    try:
        import attention_profile as AP
    except Exception as e:
        return {"error": "注意力画像不可用:%s" % str(e)[:80]}
    term = str(args.get("term") or args.get("word") or args.get("concept") or "").strip()
    if not term:
        return {"error": "要给一个词/概念(term)"}
    order = "recent" if str(args.get("order") or "").startswith("rec") else "relevance"
    r = AP.relate_material(term, when=args.get("when") or "", days=args.get("days"),
                           top=max(1, min(int(args.get("top") or 10), 25)), order=order)
    if not r.get("materials"):
        return {"词": term, "结果": "没有找到你关注过这个词的材料(可能还没学到,或换个说法)"}
    out = []
    for m in r["materials"]:
        it = {"材料": m["label"], "相关度": m["rel"], "关注次数": m["hits"],
              "最近": m["last_when"], "来源": m["channels"], "ref": m["ref"]}
        if m["ref"].startswith("book:") and "#p" in m["ref"]:
            _f, _, _pg = m["ref"][5:].partition("#p")
            it["file"], it["page"] = _f, (int(_pg) if _pg.isdigit() else 0)
        out.append(it)
    return {"词": term, "归一键": r["key"], "排序": ("最近优先" if order == "recent" else "相关度优先"),
            "材料": out,
            "怎么用": "这些是用户**实际关注过**的材料(书页用 read_page(file,page) 读原文;"
                      "Anki 答错多=薄弱)。别把材料标签当用户原话,是从行为统计出来的。"}



def _t_learning_focus(args, ctx):
    """回答**「我最近/某段时间在学什么」**类问题:按时间窗给出学习焦点词(带出处书页)。
    数据=所有学习行为(查词/高亮/问 AI/自测)的加权术语画像(references/attention-kb-design.md)。
    args: when?(今天/昨天/本周/上周/本月/上个月/最近三天/最近一周/最近一个月/最近三个月/全部)
          days?(直接给天数,优先于 when)  scope?("book"=只看当前书 | "convo"=当前这轮对话的焦点)
          channels?(["lookup","highlight","qa","check"])  top?(默认 12)
    """
    import sys as _sys
    _sys.path.insert(0, "/home/bwicarus/claude/scripts")
    try:
        import attention_profile as AP
    except Exception as e:
        return {"error": "注意力画像不可用:%s" % str(e)[:80]}
    top = max(1, min(int(args.get("top") or 12), 30))
    scope = (args.get("scope") or "").strip()
    if scope == "convo":   # 当前对话的焦点:本轮上下文抽词 + 全库 IDF 排序(不查历史事件)
        txt = str(args.get("text") or "") + " " + str((ctx or {}).get("selection") or "")
        if len(txt.strip()) < 4:
            try:
                _msgs = _convo_load(str((ctx or {}).get("_uid") or ""))[-8:]
                txt = " ".join(str(m.get("content") or "")[:400] for m in _msgs)
            except Exception:
                txt = ""
        r = AP.focus_of_text(txt, top=top)
        return {"范围": "当前对话", "焦点词": [{"词": x["term"], "本轮出现": x["n"],
                                             "出现过的书数": x.get("seen_in_books", 0)} for x in r["top"]],
                "怎么用": "这是**本轮对话**在谈的知识点;要看用户的历史学习焦点请不带 scope 再调一次。"}
    file = ((ctx or {}).get("file_rel") or "") if scope == "book" else ""
    r = AP.focus_window(when=args.get("when") or "", days=args.get("days"),
                        channels=args.get("channels"), file=file, top=top)
    if not r.get("top"):
        return {"范围": r["window"]["label"], "结果": "这段时间没有学习行为记录"}
    items = []
    for x in r["top"]:
        it = {"词": x["term"], "热度": x["score"], "次数": x["n"], "来源渠道": x["channels"]}
        if x.get("refs"):
            _f = x["refs"][0]
            it["出处"] = "%s 第%s页" % (_f["file"].split("/")[-1], _f["page"] or "?")
            it["file"], it["page"] = _f["file"], _f["page"]
        items.append(it)
    return {"范围": r["window"]["label"] + ("(仅当前这本书)" if file else ""),
            "行为数": r["n_events"], "焦点词": items,
            "怎么用": "按热度排序(渠道加权 × 跨书泛词降权)。要读原文用 read_page(file,page)。"
                      "**这些是从行为统计出来的词,不是用户原话**。"}


def _t_situation_feedback(args, ctx):
    """记录用户对某个「学习近况」困难点的**明确表态**(学习近况消解条件第四条)。只在用户主动说清楚时调,别替他判断。
    args {concept: 哪个知识点(用上下文「学习近况」里给的 concept 名),
          kind: understood(说懂了/会了 → 转解决) | still_stuck(说确实还不会/更糊涂 → 加强) | mute(说别再提/不学了 → 归档)}。
    """
    concept = str(args.get("concept") or "").strip()
    kind = str(args.get("kind") or "").strip()
    if not concept or kind not in ("understood", "still_stuck", "mute"):
        return {"error": "要给 concept + kind(understood/still_stuck/mute)"}
    import sys as _sys
    _sys.path.insert(0, "/home/bwicarus/claude/scripts")
    try:
        import learning_situations as _LS
        return _LS.feedback(str((ctx or {}).get("_uid") or ""), concept, kind)
    except Exception as e:
        return {"error": "学习近况反馈失败:%s" % str(e)[:80]}


def _gen_choice_question(node):
    """为一个 KG 节点出一道单项选择题(1c)。返回 {text, options[≤4], answer} 或 None。"""
    name = node.get("name") or ""
    ref = "参考:" + str(node.get("summary"))[:300] if node.get("summary") else ""
    prompt = ("为知识点「%s」出一道**单项选择题**,检验是否真正掌握其核心定义/性质(不要死记硬背式)。"
              "给 4 个选项、只有一个正确,干扰项要似是而非(针对常见误解)。%s\n"
              "只输出 JSON:" % (name, ref)
              + '{"text":"题干","options":["A项","B项","C项","D项"],"answer":"A"}')
    try:
        raw = _gemini_text(prompt, max_tokens=500, think=False) or ""
        import re as _re
        m = _re.search(r"\{.*\}", raw, _re.S)
        d = json.loads(m.group(0)) if m else {}
        opts = d.get("options") or []
        ans = str(d.get("answer") or "").strip().upper()[:1]
        if d.get("text") and isinstance(opts, list) and len(opts) >= 2 and ans in "ABCD":
            return {"text": str(d["text"])[:400], "options": [str(o)[:120] for o in opts[:4]], "answer": ans}
    except Exception:
        pass
    return None


def _t_make_diagnostic(args, ctx):
    """针对一个知识点出一张**分层诊断卷**(1c):沿 prereq 链先测前置、再测目标,每道选择题挂 node_id。
    用户点选作答 → 点「让 AI 检查」= 客观判分(选择题不烧 AI),结果按知识点聚合(供掌握度提案)。
    诊断逻辑:前置错=根源在前置;前置对而目标错=目标本身没吃透。
    args {concept: 知识点名,或 kg 节点 id 如 kg:LADR#...}。"""
    concept = str(args.get("concept") or args.get("node") or args.get("term") or "").strip()
    if not concept:
        return {"error": "要给知识点(concept)"}
    rel = (ctx or {}).get("file_rel") or ""
    if not rel or not rel.lower().endswith(".pdf"):
        return {"error": "诊断卷目前只支持 PDF 阅读器"}
    import sys as _sys
    _sys.path.insert(0, "/home/bwicarus/claude/scripts")
    try:
        import attention_profile as AP
    except Exception as e:
        return {"error": "KG 不可用:%s" % str(e)[:80]}
    kg = AP._kg_all()
    target = None
    if concept.startswith("kg:"):
        target = kg["nodes"].get(concept.split("#", 1)[-1])
    if not target:
        key = AP.norm_key(concept) or concept
        for nid, n in kg["nodes"].items():
            if (AP.norm_key(n.get("name") or "") or "") == key:
                target = n
                break
        if not target:
            for nid, n in kg["nodes"].items():
                if concept in (n.get("name") or ""):
                    target = n
                    break
    if not target:
        return {"error": "知识图谱里找不到「%s」这个知识点(换个更准的名字?)" % concept}
    tid = target.get("id")
    prereqs = []
    for e in kg["edges"]:
        if e.get("kind") == "prereq" and e.get("to") == tid:
            pn = kg["nodes"].get(e.get("from"))
            if pn and pn not in prereqs:
                prereqs.append(pn)
    nodes_spec = [(pp, "prereq") for pp in prereqs[:2]] + [(target, "target")]
    blocks = [{"kind": "text", "text": "诊断卷:%s" % target.get("name"), "style": "h1", "id": "b0"}]
    bi = 1
    nq = 0
    for node, layer in nodes_spec:
        q = _gen_choice_question(node)
        if not q:
            continue
        blocks.append({"kind": "text", "text": "〔%s〕%s" % ("前置" if layer == "prereq" else "目标", node.get("name")),
                       "id": "bl%d" % bi})
        bi += 1
        blocks.append({"kind": "choice", "text": q["text"], "options": q["options"], "answer": q["answer"],
                       "node_id": "kg:%s#%s" % (node.get("_book"), node.get("id")), "layer": layer, "id": "bq%d" % bi})
        bi += 1
        nq += 1
    if nq == 0:
        return {"error": "没能为「%s」生成题目(稍后再试)" % concept}
    blocks.append({"kind": "button", "label": "让 AI 检查", "event": "check", "id": "bchk"})
    title = "诊断卷·%s" % target.get("name")
    return {"已生成诊断卷": "%d 题(%d 前置 + 目标)" % (nq, min(len(prereqs), 2)),
            "接下来": "系统在你当前位置插一张诊断卷。点选作答后点「让 AI 检查」→ 客观判分,结果按知识点聚合。",
            "client_action": {"fn": "__upStartTask",
                              "args": [{"kind": "free", "title": title, "paper": "exam",
                                        "params": {"blocks": blocks, "paper": "exam", "title": title}}]},
            "silent": True}


def _t_mastery_proposal(args, ctx):
    """读最近一张**诊断卷**的判分结果,按知识点聚合 → 掌握度**变更提案**(1d;只提议,绝不改)。
    把提案讲给用户,用户确认某条 → 调 apply_mastery 落实。守铁律:不确认不改。args {}。"""
    uid = str((ctx or {}).get("_uid") or "")
    reports = _check_reports_load(uid)
    rep = None
    for r in reversed(reports):
        if r.get("node_results") and not r.get("sandbox"):
            rep = r
            break
    if not rep:
        return {"提案": [], "说明": "还没有带知识点结果的诊断卷 —— 先用 make_diagnostic 出一张,点选作答后点『让 AI 检查』。"}
    import sys as _sys
    _sys.path.insert(0, "/home/bwicarus/claude/scripts")
    try:
        import attention_profile as AP
        _lbl = AP._material_label
    except Exception:
        _lbl = lambda x: x
    props = []
    for nid, e in rep["node_results"].items():
        tot = e.get("total") or 0
        cor = e.get("correct") or 0
        ratio = (cor / tot) if tot else 0
        layer = e.get("layer")
        base = {"node": nid, "name": _lbl(nid), "结果": "%d/%d" % (cor, tot), "layer": layer}
        if tot and ratio >= 1.0:
            base.update({"建议": "标为已掌握", "mastery": 0.9})
        elif ratio < 0.5:
            base.update({"建议": ("根源可能在这个前置,先补它" if layer == "prereq" else "目标本身没吃透,建议重学"),
                         "mastery": None})
        else:
            base.update({"建议": "掌握一般,再练练", "mastery": None})
        props.append(base)
    return {"提案": props, "来自": rep.get("name"),
            "怎么用": "把提案讲给用户。用户确认要把某个『标为已掌握』→ 调 apply_mastery(node=该 node id, mastery=0.9)。不确认不要改。"}


def _recompute_book_mastery(book):
    """R3-G2 即时重算:override 写完后 detached 跑 link_and_mastery(读磁盘 records,不连 Anki)。"""
    try:
        import subprocess as _sp
        kg_path = CLAUDE_DIR / "knowledge_graph" / ("%s.json" % book)
        script = CLAUDE_DIR / "scripts" / "kg" / "link_and_mastery.py"
        if not kg_path.exists() or not script.exists():
            return
        env = os.environ.copy()
        env.setdefault("CLAUDE_PROJECT", str(CLAUDE_DIR))
        logp = CLAUDE_DIR / "state" / "logs" / "kg_edit_recompute.log"
        logp.parent.mkdir(parents=True, exist_ok=True)
        with logp.open("ab") as lf:
            _sp.Popen(["/usr/bin/python3", str(script), "--kg", str(kg_path), "--in-place"],
                      env=env, stdout=lf, stderr=_sp.STDOUT, start_new_session=True)
    except Exception:
        pass


def _mastery_proposal_backed(uid, nid, mastery):
    """R3-G2 软背书:标已掌握(≥0.8)最好来自最近诊断卷该节点满分;<0.8 一律视为通过。
    容错匹配 node_results 键(bare nid 或 kg:书#nid),兼容 assistant/skilltree 两种格式。"""
    try:
        if float(mastery) < 0.8:
            return True
    except Exception:
        return False
    try:
        reports = _check_reports_load(uid)
    except Exception:
        return False
    suffix = "#" + nid
    for r in reversed(reports):
        if r.get("sandbox"):
            continue
        for k, e in (r.get("node_results") or {}).items():
            if k == nid or k.endswith(suffix):
                tot = e.get("total") or 0
                cor = e.get("correct") or 0
                if tot and cor / tot >= 1.0:
                    return True
    return False


def _t_apply_mastery(args, ctx):
    """**用户确认后**把某知识点的掌握度写进 KG override(1d/②)。**只有用户明确同意才调**,别自己决定。
    args {node: kg 节点 id(如 kg:LADR#..), mastery: 0~1(标已掌握用 0.9), reason?}。改错了可撤销(告诉用户找我 remove)。
    R3-G2:硬校验 0≤mastery≤1 + 节点必须真存在;软校验诊断背书(决定 source 标签);写完即时重算。"""
    node = str(args.get("node") or "").strip()
    try:
        mastery = float(args.get("mastery"))
    except Exception:
        return {"error": "mastery 要是 0~1 的数(标已掌握用 0.9)"}
    if not (0.0 <= mastery <= 1.0):
        return {"error": "mastery 必须在 0~1 之间"}
    if not node.startswith("kg:") or "#" not in node:
        return {"error": "node 要是 kg:书#节点id 形式"}
    book, nid = node[3:].split("#", 1)
    import sys as _sys
    _sys.path.insert(0, "/home/bwicarus/claude/scripts")
    _sys.path.insert(0, "/home/bwicarus/claude/scripts/kg")
    try:
        import attention_profile as AP
        _nodes = AP._kg_all()["nodes"]
    except Exception:
        _nodes = {}
    if nid not in _nodes:
        return {"error": "节点不存在:%s(先确认 node id 对不对,别改不存在的节点)" % node}
    uid = str((ctx or {}).get("_uid") or "")
    _backed = _mastery_proposal_backed(uid, nid, mastery)
    try:
        import mastery_overrides as MO
        MO.set_override(book, nid, mastery,
                        source=("diagnostic" if _backed else "chat-manual"),
                        reason=(args.get("reason") or ("诊断卷确认" if _backed else "对话确认")))
    except Exception as e:
        return {"error": "写 override 失败:%s" % str(e)[:80]}
    _recompute_book_mastery(book)   # R3-G2:即时重算,override 沿 DAG 传播
    _resolved = ""
    try:
        import learning_situations as LS
        nm = (_nodes.get(nid) or {}).get("name") or ""
        if nm and mastery >= 0.8:
            fb = LS.feedback(uid, nm, "understood")
            if fb.get("ok"):
                _resolved = nm
    except Exception:
        pass
    return {"已确认": node, "掌握度": mastery,
            "note": "已写入 KG 掌握度 override 并触发重算(下次 daily 也不覆盖)。"
                    + ("" if _backed else "(注:这条非诊断卷满分背书,按你的对话确认改的。)")
                    + (("相关学习近况「%s」已转已解决。" % _resolved) if _resolved else "")}


def _t_remove_mastery(args, ctx):
    """**用户确认后**撤销某知识点的掌握度 override,回到 Anki 反算的自然掌握度(可逆)。
    只有用户明确要求撤销才调。args {node: kg 节点 id(如 kg:LADR#..)}。R3-G2:撤销即时重算,清徽标。"""
    node = str(args.get("node") or "").strip()
    if not node.startswith("kg:") or "#" not in node:
        return {"error": "node 要是 kg:书#节点id 形式"}
    book, nid = node[3:].split("#", 1)
    import sys as _sys
    _sys.path.insert(0, "/home/bwicarus/claude/scripts")
    _sys.path.insert(0, "/home/bwicarus/claude/scripts/kg")
    try:
        import mastery_overrides as MO
        gone = MO.remove(book, nid)
    except Exception as e:
        return {"error": "撤销失败:%s" % str(e)[:80]}
    if not gone:
        return {"note": "该节点本来就没有 override,无需撤销。"}
    _recompute_book_mastery(book)
    return {"已撤销": node, "原override": gone.get("mastery"),
            "note": "已移除人工 override 并触发重算,掌握度回到 Anki 反算值;技能树刷新后生效。"}


def _t_error_patterns(args, ctx):
    """回答"我有什么系统性/元认知层面的弱点"(错误模式元画像:证明题弱/定义混/跨语言术语对应不清 等)。
    比学习近况高一层——不是单个知识点,而是**跨知识点的共性弱点** + 针对性学习策略。args {}。"""
    uid = str((ctx or {}).get("_uid") or "")
    import sys as _sys
    _sys.path.insert(0, "/home/bwicarus/claude/scripts")
    try:
        import error_meta_profile as EMP
        d = EMP.load(uid)
    except Exception as e:
        return {"error": "元画像不可用:%s" % str(e)[:80]}
    pats = d.get("patterns") or []
    if not pats:
        return {"弱点模式": [], "说明": d.get("note") or "还没归纳出模式(错题攒够了 daily 会自动生成)"}
    return {"弱点模式": [{"模式": p["name"], "证据": p["evidence"], "建议": p["strategy"]} for p in pats],
            "样本数": d.get("n_samples")}


TOOLS = {
    "read_page": ("读当前页(或指定页)正文。args {page?}", _t_read_page),
    "recall_creation": ("取回一个**创造物**的完整内容(之前工具的产出:练习纸/检查报告/联网搜索/视频/翻译/章节总结)。"
                        "上下文『最近创造物』清单里的 #id 就是句柄;用户提到『刚才查的/搜的/那张纸/那个结果/第几题的答案』→ 调我拿全文再答。"
                        "纸类条目返回**题目+标准答案+检查报告(有的话)**。args {id?:句柄; kind?:类型; query?:描述模糊;都不传=最近一条}", _t_recall_creation),
    "read_check_report": ("拿到**练习纸检查报告**的内容(题目原文+标准答案+用户手写+判分),**默认直接返回给你、你据此直接答**。"
                          "上下文里若提到『最近有检查报告《X》』,用户又在问这张纸的题/答案/错在哪/怎么改/**题目出处/原文在哪**,就调它拿报告再答——"
                          "报告名放 name(不传=最近一份)。⚠ 这纸是**用户自制**的,题目和答案**都在报告里、书本正文没有**,"
                          "**别自己 read_page/search_book 去书里找『题目原文』**。要深入**查书核实**某个知识点才传 verify:true(那时才起查书子 agent)。"
                          "args {name?:报告名(可模糊); question?:用户问题; verify?:true=起查书子 agent}", _t_read_check_report),
    "read_selection": ("读用户当前选中的文字。args {}", _t_read_selection),
    "search_book": ("在当前这本书全文搜关键词,返回命中页+片段。args {query}", _t_search_book),
    "search_all_books": ("跨『我所有的书』全文搜索(用户问『哪本书讲过X/别的书有没有X/之前在哪见过』时用;只搜书库,不是联网搜索)。args {query 必填=要搜的关键词}", _t_search_all_books),
    "recall_notes": ("**召回用户自己学过/记过的**相关内容:知识索引(带摘要)+ vault 笔记全文 + 知识图谱**已学**节点 + Anki 卡(本地查不耗时)。"
                     "想把当前内容跟『他已学过/记过的』串起来、用户问『我之前记过吗/我笔记里有没有X/跟我学的Y有关吗』、或要结合他知识体系深入讲时用。"
                     "**注意:只有召回到的才算他学过**(图谱里没学的节点不会返回);没召回到就别假设他会。args {query:主题词}(不传用选中/焦点)", _t_recall_notes),
    "open_book": ("打开另一本书并可定位到页(跨书跳转)。args {file_rel | book(书名), page?}", _t_open_book),
    "summarize_section": ("取当前页所在『整章/整节』正文交给你总结(read_page 只逐页,『总结这一章』用这个)。args {page?}", _t_summarize_section),
    "translate": ("翻译文字成中文(或 target 语言)。不传 text 则译选中/本页。args {text?, target?}", _t_translate),
    "goto_page": ("翻到指定页(前端跳转)。args {page};page 可以是数字,也可以是 last(最后一页)/first/+1/-1。结果里带『全书总页数』", _t_goto_page),
    "make_anki": ("把内容做成 Anki 卡片草稿供用户预览确认(**同步等做完才返回**,报告生成了几张;未确认不入库)。"
                 "args {text?, requirement?, image_url?}。**requirement=把用户对卡片的具体要求原样转述**"
                 "(几张/难度/角度/语言,如'只做一张''简单点''考细节'——用户说什么就原样填,别自作主张)。"
                 "不传 text 用选中/本页;image_url 若刚 search_image 过、这张图也进卡片就把同一个 image_url 传进来", _t_make_anki),
    "make_note": ("把内容整理成 Obsidian 笔记(后台)。args {text?}(不传用选中/本页)", _t_make_note),
    "do_task": ("后台 agent(自己规划、自己连着调工具、干完回报一句话)。**一件事需要调 2 个以上工具就用它**:"
                "①要 2 步以上才能答的(如\"我在读的那本书一共多少页\"=先查在读哪本再查页数、"
                "\"把这章重点标出来再逐条做成卡片\");"
                "②探索性的活,你事先不知道要翻几本书/查几次(如\"找找我读过的书里哪本提过X\");"
                "③要跑很久的活(整章处理),不能让用户干等;"
                "④**造一张让用户在页面上手写作答的交互纸**(出题/填空/试卷/听写/清单/『给我出…我写』/『在纸上做』)"
                "——你**没有**直接造纸的工具,这类一律交给它(它那边的 CLI 有造纸工具)。"
                "**只有 1 个工具就能答的,自己直接调**(那种情况用它没有任何好处)。"
                "用它时把用户原话**原样**转述,别自己拆步骤。args {instruction}",
                _t_do_task),
    "make_paper": ("★造一张让用户**在页面上手写作答**的交互纸(出题/填空/试卷/听写/清单/『给我出…我写』/『在纸上做』/"
                   "『让我在纸上写』)。**你没有别的造纸工具**——凡是要用户在页面上手写的,一律用它(它会交给后台 CLI 造)。"
                   "args {intent:一句话说清要造什么纸,把用户原话原样带上}",
                   _t_make_paper),
    "add_vocab": ("把英文单词加生词本并制卡(后台)。args {word?}(不传用选中)", _t_add_vocab),
    "search_image": ("★配图专用(搜**真实图片**,非 AI 生成;多源 Wikimedia Commons + Google 图搜)。**用户开了配图偏好时**,"
                     "先想清楚这次回答里**哪些概念配图真有帮助**(有明确视觉形象的:实物/结构/示意图/图表/生物/文物/天体/仪器等),"
                     "**一次性**把它们连关键词一起传:args {queries:[{concept:\"中文概念\", query:\"所属语言关键词\", query_en:\"english fallback\"}, ...]}"
                     "(query 用**最可能命中的语言**:日本特有事物用日语原名,通用/西方概念用英文;query_en 恒带英文翻译,"
                     "工具先搜 query、没中自动用 query_en 保底;**关键词必须简短**——事物名称本身 1~3 个词,"
                     "别写修饰语和描述句(图库按名称索引,长句反而搜不到);一次最多 8 个)。工具会并行搜、每个概念返回最匹配 1 张。"
                     "拿回结果后:对 images 里每张,在回答对应概念旁用 markdown ![简短中文说明](image_url) 插入;missed 里没搜到的**别硬配、别自己编链接**。"
                     "别对『力/能量』这类无固定形象的抽象词硬配。刚好要制卡也想放这张图,把该 image_url 传给 make_anki。", _t_search_image),
    "web_search": ("联网网页搜索:查**网上的实时信息/事实/新闻/资料**时用,args {query:\"简洁关键词或问题\"}。"
                   "返回联网综合回答(answer)+来源(sources),口头转述并提一句来源。"
                   "一个问题最多搜一两次,能凭自己知识答好的不用搜。", _t_web_search),
    "search_video": ("搜教学视频(YouTube)并在对话里渲染**可播放**的视频卡片。用户明确要『找/看视频、有没有视频讲解、放个视频』时用,"
                     "别对每个概念都配视频(大多数回答不需要)。拿到结果只需简短说一句『给你找到这些视频』,"
                     "**别复述标题/链接**(卡片已经显示了、能直接点开播放)。args {query?}(不传用选中/焦点)", _t_search_video),
    "highlight": ("在 PDF 上把**你已经选定的**重点句子画高亮(可撤销)。args {texts:[\"原句1\",\"原句2\"], color?, page?}。"
                  "texts 必须是页面上的**原文逐字**(从 read_page 结果照抄,别改写/别翻译),否则定位不到。"
                  "page 不传=当前页。**适合标当前页你已读到的几句**;要标整章/多页用 auto_highlight(别自己逐页 read+highlight)", _t_highlight),
    "auto_highlight": ("**整章/多页『自动标重点』专用**(『把这一章/第X-Y页的重点都高亮』就用它)。它内部逐页把正文外包给挑句专家、"
                       "画好高亮,只回简报——**正文不进你的上下文,省大量 token**。范围:from+to(印刷页区间,配合 toc 拿章的起止页)/ pages[列表] / page。"
                       "调它一次就够,**别再自己逐页 read_page+highlight**。color 可选(默认黄)", _t_auto_highlight),
    "read_highlights": ("读某页/全书**已有高亮**(标了哪些内容、颜色、备注)。批量标注前先看可避免重复标;也答『这页我高亮了啥』。"
                        "args {page?}:不传=当前页,page=数字=该页,page=\"all\"=全书", _t_read_highlights),
    "find_highlights": ("用户要**删除/取消/清理/去掉**某些高亮时用:把匹配的高亮逐条列在对话里,每条带「跳转+删除」按钮让用户自己点。"
                        "**这就是删除高亮的工具**——别说没有。范围:不传=当前页;page=数字=某页;page=\"all\"=全书;pages=[13,14]=指定多页;"
                        "**from=起始印刷页, to=结束印刷页 = 一段范围(整章首选,配合 toc 拿到章的起止页)**。args {page?|pages?|from?,to?}", _t_find_highlights),
    "toc": ("看这本书的**目录**(章节标题+起始印刷页)。要把『第N章/某节/前言』换算成页范围时用(再配合 find_highlights(from,to)/goto_page)。"
            "某章范围=该章起始页~下一同级条目起始页减1。args {}", _t_toc),
    "page_vocab": ("查掌握度数据库:不传 words=当前页『还没掌握』的生词(权威,跟页面下划线一致);"
                   "传 words(数组)=逐词查掌握度(英+日)。args {words?:[...]}", _t_page_vocab),
    "lookup_word": ("查词典:英→ECDICT(音标+中文释义+原形)、日→unidic **权威读音+声调**。"
                    "**读音/释义以它为准,别自己编读音**;你只结合上下文挑义项+讲解。args {word?}(不传用选中)", _t_lookup_word),
    "see_page": ("**真正看到**当前页(或指定页)的渲染图——图表/示意图/曲线/公式排版/手写等文字层读不到的东西。"
                 "**本页有手写批注时会自动把『页面+手写笔迹』合成进图**(读不到手写就靠它)。"
                 "read_page 只有文字层、看不见图形/手写;用户问『这张图/这个图表/这页的图/我写的/我圈的/看一下』时用 see_page。args {page?}", _t_see_page),
    "see_figure": ("看用户**当前聚焦的那张图**的裁剪渲染图(他点选/拖进来的图;有手写笔迹则看合成图)。"
                   "已给的图说明不够、要核对图里的具体细节/用户在图上的标注时用。"
                   "也可传 note_id(notes_query/notes_read 拿到的便签 id)看**某条便签**的文字+手写合成图。args {note_id?}", _t_see_figure),
    "see_ink": ("看用户**用笔标注的那块区域合成图**(裁笔迹附近 + 叠手写笔迹)。比 see_page 聚焦、只看标注那块、更省更快。"
                "用户用笔圈/划/打勾/画箭头标了东西后问『这是什么/我圈的/这里/什么意思』,或没说具体指什么但本页有笔迹时用。args {}", _t_see_ink),
    "notes_query": ("查用户贴在书页上的**便签**(sticky notes)列表。用户问『我记了什么便签/哪页有便签/我便签里写没写过X/找我那张黄色便签』时用;"
                    "回答『这页讲什么/总结』**不需要**查便签。args {color?, keyword?, page?} 三个过滤可组合,全不传=列全部"
                    "(color 可给色名 白/黄/蓝/绿/粉/石墨/墨绿 或 hex;page=印刷页码)", _t_notes_query),
    "notes_read": ("读某条便签的**全文**+位置(notes_query 的 text 只是摘要)。args {id}(id 从 notes_query 拿)", _t_notes_read),
    "notes_create": ("在书页上**新建一张便签**(有副作用的写操作,只有用户明确要求才调,如『帮我在这页记个便签/贴张便签写上…』)。"
                     "args {text, page?, x?, y?, color?}:text=便签内容(必填);page=印刷页码(不传=当前页);"
                     "x/y=页内位置比例 0~1(不传默认右上区);color 可给色名(白/黄/蓝/绿/粉/石墨/墨绿)或 hex,缺省白。"
                     "建完系统会自动给撤销卡,你不用解释怎么撤销", _t_notes_create),
    "notes_edit": ("**修改已有便签**的文字/颜色(写操作,只有用户明确要求才调,如『把那张便签改成…/便签换个颜色』)。"
                   "args {id, text?, color?}(id 从 notes_query 拿;text/color 至少给一个)。"
                   "**只能改文字和颜色**——手写笔画/位置/尺寸动不了(工具层面就不接收),别答应用户改这些", _t_notes_edit),
    "undo_last": ("撤销最近一次写操作(删掉刚建的卡/笔记/高亮/便签)。用户说『撤销/取消刚才那个』时用。args {}", _t_undo_last),
    "page_new": ("造一张交互纸的**第一步**:开一张新草稿。args {paper?:dictation|exam|math|draw|note(默认note), title?}。"
                 "接着用 page_add 一个个加元素,最后 page_show 生成。"
                 "适合『给我出题我写』『做张清单/试卷』;要**逐个念、念一个等一个**的听写用 start_dictation。", _t_page_new),
    "page_add": ("给草稿纸**加一个元素**(可多次,一次一个):text/blank/checkbox/button。"
                 "args=元素本身,如 {kind:'blank',label:'1.',answer:'ばら'} 或 "
                 "{kind:'button',label:'让 AI 检查',event:'check'}。别一次塞一堆,一次一个最稳。", _t_page_add),
    "save_intent_tool": ("把一个想法**铸成新工具**(重新生成型):用户说『做一个工具:…/把某工具升级成带 AI 的/保存这个思路当工具』时调。"
                         "args {name:短名, instruction:这个工具要做什么(步骤/内容来源/产出形式写清楚)}。存完告诉用户已保存+简述运行方式。", _t_save_intent_tool),
    "run_saved_task": ("运行一个**已保存的工具**。两类:**重新生成型**(如『出N题』——按原意图在当前页重新出内容,"
                       "用户说的数量/难度/主题调整放 args.adjust 原话带上,如 adjust:\"15道题,难一点\");"
                       "**机械回放型**(如听写——数据源驱动,选 source)。args {name, adjust?, source?, params?}。"
                       "不知道有哪些就先 list_saved_tasks。", _t_run_saved_task),
    "material_graph": ("展开材料的关系链条(卡片↔源笔记↔书页↔KG知识点↔前置根源),任意位置出发/任意方向。"
                       "配合 relate_material+read_material。args {ref, direction?:up|down|both, depth?}。", _t_material_graph),
    "read_material": ("读一份材料的详细内容(配合 relate_material:先找到 → 再读)。"
                      "args {ref: relate_material 返回的材料地址}。anki 卡给正反面/note 给全文/book 给那页正文。", _t_read_material),
    "relate_material": ("回答「关于X我学过/关注过哪些材料」「某知识点我在哪些地方碰到过」「我最近碰的X相关材料」:"
                        "给词/概念→我**实际关注过**的材料(书页/笔记/Anki牌组/检查报告),按相关度排序带出处,"
                        "跨语言归一(中文词也找到英/日原文)。args {term必填, order?:relevance|recent, when?, days?, top?}。"
                        "⚠ 这是「关注过的」不是全文搜索。", _t_relate_material),
    "learning_focus": ("回答「我最近/昨天/上个月在学什么」「这本书我关注了啥」「这轮在聊什么知识点」:"
                       "按时间窗给学习焦点词+出处书页。args {when?:今天/昨天/本周/上个月/最近三个月/全部, "
                       "days?:天数, scope?:book(只看当前书)|convo(当前对话), channels?, top?}。"
                       "数据=查词/高亮/问AI/自测行为的加权画像。", _t_learning_focus),
    "situation_feedback": ("★用户对上下文『学习近况』里某个困难点**明确表态**时记录(消解第四条)。"
                           "说『这个懂了/会了』→ kind:understood;『还是不会/更糊涂』→ still_stuck;"
                           "『别再提这个/不想学了』→ mute。args {concept:近况里的知识点名, kind}。"
                           "**只在用户明确表态时调**,别替他判断、别每轮都调。", _t_situation_feedback),
    "make_diagnostic": ("针对一个知识点出一张**分层诊断卷**:沿 prereq 链先测前置、再测目标,选择题点选作答→客观判分,"
                        "结果按知识点聚合(能定位『是前置没掌握还是目标本身』)。学习近况建议做卷、或用户说『考考我X/出张X的卷子/我到底哪没掌握』时用。"
                        "args {concept: 知识点名 或 kg 节点 id}。", _t_make_diagnostic),
    "mastery_proposal": ("读最近诊断卷的判分结果,按知识点聚合出**掌握度变更提案**(只提议不改)。"
                         "用户做完诊断卷、想知道『我到底哪掌握了/结果怎样』时调,再把提案讲给他。args {}。", _t_mastery_proposal),
    "apply_mastery": ("★**用户确认后**把某知识点标为已掌握/改掌握度(写进 KG,会传播解锁后续)。"
                      "只有用户明确同意某条提案才调,别自作主张。args {node: kg 节点 id, mastery: 0.9, reason?}。", _t_apply_mastery),
    "remove_mastery": ("**用户确认后**撤销某知识点的掌握度 override(回到 Anki 反算自然值,可逆)。"
                       "用户说『刚才那个标错了/撤销掉』时调。args {node: kg 节点 id}。", _t_remove_mastery),
    "error_patterns": ("回答『我有什么系统性弱点/我是不是XX类题总不行/该怎么调整学习方法』:给**跨知识点的共性弱点模式**"
                       "(证明弱/定义混/术语对应不清 等)+ 学习策略。比 learning_focus/近况 高一层(元认知)。args {}。", _t_error_patterns),
    "list_saved_tasks": ("列出所有已保存的工具及其数据源。args {}。", _t_list_saved_tasks),
    "page_show": ("把草稿纸**生成出来**(造纸最后一步)。args {}。", _t_page_show),
    "start_dictation": ("★ 开始一次**听写**:在当前位置新建一张听写纸(N 个填空格 + 「念下一个」按钮),"
                        "然后由**系统**逐个念、等用户手写、最后按格裁图批改。"
                        "args {words:[要听写的词,按顺序], title?:纸的标题}。"
                        "词由**你**来出(按用户要求的语言/难度/数量;用户没说数量就给 10 个)。"
                        "调完这个工具你的任务就结束了 —— **循环和等待由系统负责,你不要再管**,"
                        "也不要自己去念词、不要问用户写完没有。", _t_start_dictation),
}



def _step_detail(res):
    """从工具结果抽出"这步产出的完整内容"(给感叹号弹窗点开看):去掉控制字段(_开头 / client_action / task_id / undo_id / action)、截到 4000。"""
    try:
        if isinstance(res, dict):
            d = {k: v for k, v in res.items()
                 if not str(k).startswith("_") and k not in ("client_action", "task_id", "undo_id", "action")}
            return json.dumps(d, ensure_ascii=False, indent=1)[:4000]
        return str(res)[:4000]
    except Exception:
        return ""


def _tool_label(name, args):
    if name in ("do_task", "make_paper", "read_check_report", "run_saved_task"):   # CLI 卡标题 = 用户任务原话(不是通用工具名),前端拿它当卡头
        if name == "run_saved_task":
            _nm0 = (args.get("name") or "").strip(); _adj0 = str(args.get("adjust") or "").strip()
            return ("运行工具·" + _nm0 + ((" (" + _adj0[:20] + ")") if _adj0 else ""))[:44] if _nm0 else "运行工具"
        instr = (args.get("intent") or args.get("instruction") or args.get("task") or args.get("question") or args.get("text") or "").strip()
        _dft = {"make_paper": "造纸", "read_check_report": "讲解检查报告"}.get(name, "后台任务")
        return (instr[:40] + ("…" if len(instr) > 40 else "")) if instr else _dft
    return {"page_new": "新建纸", "page_add": "加元素", "page_show": "生成纸", "run_saved_task": "运行工具", "list_saved_tasks": "列出工具", "start_dictation": "开始听写", "read_page": "读取页面", "read_selection": "读取选中", "search_book": "搜索全书",
            "search_all_books": "跨书搜索", "open_book": "打开书", "summarize_section": "总结本章",
            "translate": "翻译", "goto_page": "翻页", "make_anki": "制卡", "make_note": "整理笔记",
            "add_vocab": "加生词本", "highlight": "高亮", "auto_highlight": "自动标重点(逐页外包)", "read_highlights": "看高亮", "find_highlights": "列出可删高亮", "toc": "查目录", "page_vocab": "查掌握度",
            "lookup_word": "查词典", "see_page": "看页面图", "see_figure": "看这张图", "see_ink": "看笔迹标注",
            "notes_query": "查便签", "notes_read": "读便签", "notes_create": "新建便签", "notes_edit": "修改便签",
            "recall_notes": "召回我的笔记", "undo_last": "撤销", "search_image": "配图搜索", "search_video": "找视频"}.get(name, name)


# 只读工具集:跑过它们**不该**挡住"编排后端没响应→回退 Claude 整轮重跑"(重跑无副作用)。
#   用户实测:codex 第二轮 240s 无响应,但因 read_page 已跑过被判 _tools_ran → 不回退,整轮 error 收场。
_READONLY_TOOLS = {"read_page", "read_selection", "search_book", "search_all_books", "toc", "page_vocab",
                   "lookup_word", "see_page", "see_figure", "see_ink", "read_highlights", "find_highlights",
                   "notes_query", "notes_read", "read_check_report", "summarize_section", "web_search",
                   "search_image", "search_video", "recall_notes", "list_saved_tasks"}

# 创造物自动登记:非操作型工具完成 → 入库(告知 brief 含实际查询词;read_page 不登——可随时重读,登了只添噪)。
_CREATION_KINDS = {
    "web_search":        ("联网搜了「{}」", lambda a: a.get("query")),
    "search_video":      ("找了视频「{}」", lambda a: a.get("query")),
    "search_image":      ("搜了配图「{}」", lambda a: a.get("query")),
    "translate":         ("翻译了「{}」", lambda a: str(a.get("text") or "")[:24]),
    "summarize_section": ("总结了第 {} 页所在章节", lambda a: a.get("page")),
    "search_all_books":  ("跨书搜了「{}」", lambda a: a.get("query")),
}


# 默认记忆的工具(白名单 + 特殊登记点对应的工具名);其它工具默认不记,但都可在工具设置里用「记忆」开关改
_CREATION_DEFAULT_ON = set(_CREATION_KINDS) | {"highlight", "auto_highlight", "make_paper", "do_task", "dictation_grade"}


def _creation_enabled(uid, tool, default_on=None):
    """工具「记忆」开关(工具设置面板可改):on/off 覆盖,没设=默认(白名单 on,其它 off)。"""
    if default_on is None:
        default_on = tool in _CREATION_DEFAULT_ON
    try:
        v = ((_tp_load().get(str(uid)) or {}).get(tool) or {}).get("creation")
        if v == "on":
            return True
        if v == "off":
            return False
    except Exception:
        pass
    return default_on


# ── 注意力画像 · tool 渠道(用户主动查找的**查询词** = 强意图信号)────────────────────
#   ★为什么这个渠道要埋点(其它 6 个都是零侵入导入):工具调用**没有天然的 append-only 源**
#     —— convo 的 trace 只存编排 AI 的散文、不含工具参数;vtask 落盘只有 CLI 任务。
#     这正是账本(state/attention/raw-events.jsonl)的用途:没源的渠道走 append_raw。
_ATTN_LOOKUP_TOOLS = {          # 只收**查找类**(参数=用户想找什么);操作类(高亮/造纸)已被各自渠道覆盖
    "search_book": "query", "search_in_book": "query", "search_all_books": "query",
    "web_search": "query", "lookup_word": "word", "search_notes": "query",
    "search_vocab": "query", "grammar_lookup": "text",
}


def _attn_tool_event(uid, name, targs, res, ctx):
    """查找类工具调用 → 账本(注意力画像 tool 渠道)。失败/空查询/沙盒不记。"""
    try:
        k = _ATTN_LOOKUP_TOOLS.get(name)
        if not k or not isinstance(res, dict) or res.get("error"):
            return
        q = str((targs or {}).get(k) or "").strip()
        if len(q) < 2 or len(q) > 200:
            return
        rel = str((ctx or {}).get("file_rel") or "")
        if "/.sandbox/" in rel:
            return
        import sys as _sys
        _sys.path.insert(0, "/home/bwicarus/claude/scripts")
        import attention_profile as AP
        AP.append_raw("tool", q, file=rel, page=int((ctx or {}).get("page") or 0), uid=str(uid or ""),
                      actor="user",     # 查询词是**用户想找的东西**(AI 只是执行者)
                      lang=[],          # 查询词的语言 = 用户话语的语言,不是书语言
                      turn_id=str((ctx or {}).get("turn_id") or "") or None,
                      extra={"tool": name})
    except Exception:
        pass


def _creation_register(uid, name, targs, res, ctx):
    """工具 done 统一钩子:开关允许且无 error → 登记创造物(brief=告知+结果要点)。
    白名单工具默认记;其它工具默认不记但开了开关也能记(通用模板)。编排循环 + voice-tool 端点共用。"""
    try:
        if not isinstance(res, dict) or res.get("error"):
            return
        if not _creation_enabled(uid, name, default_on=(name in _CREATION_KINDS)):
            return
        gist = re.sub(r'[{}"\[\]]', "", _step_detail(res)).replace("\n", " ").strip()[:80]   # 去 JSON 符号,brief 是给模型看的一句人话
        v = None
        if name in _CREATION_KINDS:
            tmpl, getk = _CREATION_KINDS[name]
            try:
                v = getk(targs or {})
            except Exception:
                pass
            head = tmpl.format(str(v)[:40]) if ("{}" in tmpl and v not in (None, "")) else tmpl.split("{")[0]
        else:   # 非白名单但用户开了记忆 → 通用告知(工具人话名 + 结果要点)
            head = _tool_label(name, targs or {})
        _creation_add(uid, name, head + ":" + gist, query=(v if isinstance(v, str) else None),
                      content=_step_detail(res)[:6000],
                      anchor={"file": (ctx or {}).get("file_rel"), "page": (ctx or {}).get("page")})
    except Exception:
        pass


# 写操作幻觉硬防线(用户连抓两次:luna·low 只 read_page 就回复「第66页重点已高亮,共5句」,
#   prompt 铁律无效):最终回答声称完成了写操作、但本轮**没调对应工具** → 服务端拦截,打回重试一次。
_WRITE_CLAIMS = (
    (re.compile(r"已高亮|高亮了|已标[注记]好?了?重点"), ("highlight", "auto_highlight", "read_highlights", "find_highlights"), "highlight / auto_highlight"),   # 读类也算:刚 read_highlights 后说「已高亮的内容有…」是合理转述,不拦
    (re.compile(r"已制卡|已做好?了?卡|卡片已(做|建|加)"), ("make_anki",), "make_anki"),
    (re.compile(r"已记好?了?笔记|笔记已(记|建|写)"), ("make_note",), "make_note"),
    (re.compile(r"已加[入进]?生词"), ("add_vocab",), "add_vocab"),
)


def _claim_fix_msg(raw, used):
    """回答里有完成话术但对应写工具没调过 → 返回打回指令(调用方喂回模型重试);没问题 → None。"""
    for rx, tools, label in _WRITE_CLAIMS:
        if rx.search(raw or "") and not any(t in (used or ()) for t in tools):
            return ("系统拦截:你刚才的回答声称完成了写操作,但你本轮**没有调用 " + label + "**,"
                    "页面上实际什么都没发生——该回答是编造,已被拒绝、用户看不到。现在二选一:"
                    "①真要做 → **立刻只输出对应工具调用的 JSON**(整页标重点用 auto_highlight;个别句子用 highlight);"
                    "②做不了 → 重写回答,如实说没有执行。**禁止**在工具成功之前说『已…』。"
                    "另:报页码只能用工具返回的 pages 字段,不许用正文里印的数字。")
    return None


# ──────────────────────── agent 循环 ────────────────────────
# ★ 用户设计(#52/#55):编排 AI **不该**有直接造纸工具 —— 一遇到"出题让用户写/造交互纸"这种多步活,
#   就该发现"没有直接可调用的工具"→ 交给 CLI(do_task)去做(CLI 那边经 MCP 才看得到 page_*)。
#   这样造纸永远是**一张 CLI 卡**(可保存),不会退化成编排侧内联的一堆 page_new/add/show 工具 chip。
#   ⚠ 只从**编排侧目录**摘除,page_* 仍留在 TOOLS 里给 MCP/CLI 调(去壳回放也靠它)。
_ORCH_DROP = {"page_new", "page_add", "page_show"}
# CLI 委托类后台任务:进度/结果显示在**卡内**(tool2 → _trackCliTask),不发卡外浮动 task 事件(#1 状态在卡外的根因)。
#   read_check_report=报告问答子 agent,同样走 CLI 卡。
_AGENT_TASKS = {"do_task", "make_paper", "read_check_report", "run_saved_task"}   # run_saved_task 的 intent 分支返回 task_id → 卡内 trackCliTask


def _recipes_prompt_line():
    """已存工具清单(注入系统提示;审查实锤:不注入的话 AI 连『有没有』都不知道,复用入口是断的)。"""
    try:
        import task_runtime as TR
        recs = TR.list_recipes()
        if not recs:
            return ""
        rows = []
        for r in recs[:20]:
            gist = str(r.get("instruction") or r.get("desc") or "")[:60]
            rows.append("- 《%s》(%s)%s" % (r.get("name"), r.get("kind") or "?", (" — " + gist) if gist else ""))
        return ("\n\n★已保存的工具(用户铸造的成品,各自带已验证的执行路线):\n" + "\n".join(rows) +
                "\n用户的要求与某个已存工具的意图相符 → **优先 run_saved_task 运行它**(可带 adjust 传本次调整),"
                "别从头重新编排;不确定就先 list_saved_tasks 看详情。")
    except Exception:
        return ""


def _sys_cache_reset():
    global _SYS_STATIC_CACHE
    _SYS_STATIC_CACHE = None


def _sys_prompt(ctx):
    _uid0 = ctx.get("_uid") or ctx.get("uid") or ""
    # 140:工具说明支持 per-user 覆盖(详情窗里改 → 这里就是它进 AI 的地方)
    cat = "\n".join(f"- {n}: {_tp(_uid0, n, 'desc', d)}" for n, (d, _) in TOOLS.items() if n not in _ORCH_DROP)
    cat += _recipes_prompt_line()   # 已存工具目录(在 _SYS_STATIC_CACHE 里;保存/删除配方时须 _sys_cache_reset)
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
                     "\n★★制卡/记笔记时**直接用上面的选中原文**(make_anki 的 text 参数就填它),"
                     "**别先 read_page 读整页**——用户实锤:钉了一段说「把这里做成卡」,助手却答"
                     "「我先把这一页抓取一下」,做出整页的卡、还慢。选中就是他要的范围,不用再找。"
                     "查词/读音**先 lookup_word** 拿权威读音+释义再挑义项(日语同字多音、严禁自己编读音)。"
                     "上下文**优先用上面的『选中所在句』**——有它就别再 read_page,只有所在句不足以定义项时才 read_page。"
                     "**选中只是文字、上下文没涉及图就别 see_page**;只有指代某图(『图1-3/如下图』)或用户明说『看这张图』才 see_page。")
    # 带入的图(用户点/拖进来的,可多张):带上各自 AI 描述当上下文,要核对图像细节才 see_figure
    figs = ctx.get("figures") or ([ctx["figure"]] if ctx.get("figure") else [])
    fig_line = ""
    if figs:
        items = []
        for i, fg in enumerate(figs):
            if fg.get("kind") == "note":   # 双击带入的手写便签(kind:'note';文字/位置正文先给足,笔画内容 see_figure 看合成图)
                it = (f"[{i + 1}] 用户的**手写便签**(贴在 p{fg.get('page')};便签文字如下,"
                      f"手写笔画的内容要 see_figure 看合成图):「{_clean_tag(fg.get('desc'))[:300]}」")
                near = _clean_tag(fg.get("near"))
                if near:
                    it += f";便签位置附近正文:「{near[:400]}」"
                items.append(it)
                continue
            fcap = _clean_tag(fg.get("caption")); fdesc = _clean_tag(fg.get("desc"))
            ink = "(有手写笔迹/圈点)" if fg.get("has_ink") else ""
            items.append(f"[{i + 1}] 「{fcap[:48]}」p{fg.get('page')}{ink}:{fdesc[:300]}")
        if len(figs) == 1:
            fig_line = "\n用户带入了一张图,默认在问它:\n" + items[0] + \
                "。先据这段说明回答;说明不够或需核对图里细节/手写标注时才 see_figure(args {})。"
        else:
            fig_line = f"\n用户带入了 {len(figs)} 张图(默认在问/对比这些图):\n" + "\n".join(items) + \
                "\n先据这些说明回答;要核对某张图的细节/手写标注时用 see_figure(args {index:第几张,从1起;不传=全部)。"
    # 双击带入的便签(无笔画:文字+锚点附近正文走文本通道;有笔画的以 kind:'note' 并在上面 figures 里走视觉)
    notes_att = [n for n in (ctx.get("notes") or []) if isinstance(n, dict) and (n.get("text") or n.get("near"))]
    note_line = ""
    if notes_att:
        nitems = []
        for i, nb in enumerate(notes_att[:4], 1):
            loc = f"(贴在 p{nb.get('page')})" if nb.get("page") else ""
            it = f"[{i}]{loc} 便签文字:「{_clean_tag(nb.get('text'))[:400]}」"
            near = _clean_tag(nb.get("near"))
            if near:
                it += f";便签位置附近正文:「{near[:600]}」"
            nitems.append(it)
        note_line = ("\n用户双击带入了自己写的便签(默认在问它/要你结合它答;便签位置附近正文已给好,**别为此再 read_page**):\n"
                     + "\n".join(nitems))
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
    # 视口焦点:用户此刻**屏幕上正在看的正文**(前端按视口相交采集)。EPUB 一节=整章内容太长、AI 只知章节
    #   会答偏(如把「酶」问成整章「生物物理」);给可见部分 → 回答/找视频/配图/拟搜索词都紧扣当前注意力。限长防 prompt 膨胀。
    _vm = ctx.get("voice_mode")
    if _vm:
        # 口语段=所有语音链路共通;[语气:XX] 标签段只给前端朗读(_vm==1,单向 2.0 引擎吃标签)。
        #   S2S 深度思考代播(_vm=="s2s")走 bidi 内置 TTS,不吃标签,给了指令反而可能把标签念出来。
        _vseg = ("★语音对话中(问题来自语音转写,可能有同音错字,按语境理解;你的回答会被逐句朗读):"
                 "回答要**像当面聊天**——口语、短句、直接说结论,**默认两三句话说完**;"
                 "别铺开、别客套、别复述问题,听者想深入自然会追问;内容确实多就只讲最重要的一点,末尾问一句要不要展开。"
                 "少用列表/表格/标题结构;数学符号和公式用中文口头表述(如「x 的平方除以 2」),别写 LaTeX。"
                 "**用标点控制朗读节奏**:逗号断句;需要明显停顿(转折/换话题/给听者反应时间)的地方用省略号……。"
                 "TTS 按标点自然停顿,别用其它标记符号。工具照常用。")
        if _vm == 1:
            _vseg += ("**回答的最开头先输出语气标签「[语气:XX]」**(XX 用 2~6 字描述情绪,"
                      "如 平静/开心/严肃认真/温柔/惋惜/兴奋,按内容选,普通内容用 平静;标签里别用标点)。"
                      "**情绪有转折时**(比如从平静叙述转到惋惜感叹),就在转折的那句话前面再插一个新的「[语气:XX]」——"
                      "之后的句子都按新语气念,直到下一个标签。标签不会显示也不会被念出,朗读引擎按它调整声音情绪。")
        learn_bits.append(_vseg)
    mp = ctx.get("media_prefer") or {}
    if mp.get("image"):
        learn_bits.append("★用户开了「配图」偏好:回答里凡是**有明确视觉形象**的概念(实物/结构/示意图/图表/生物/文物/天体/仪器…)都该配图。"
                          "做法:想清楚这次哪些概念该配(可以好几个),**一次性**调 search_image 传 queries=[{concept,query}...]"
                          "(query 用**英文**关键词图源覆盖最好),别一个个零散调、也别问『要不要配图』。拿回图后每张配一句中文说明插到对应概念旁。只有纯抽象推导/基础常识才跳过。")
    if mp.get("video"):
        learn_bits.append("★用户开了「视频」偏好:内容适合视频讲解时**直接调 search_video 找了放进对话,别问『要不要我找视频』**(他已用开关表明要视频);只有完全不适合视频才跳过。")
    vtext = _clean_tag(ctx.get("visible_text") or "")
    if vtext:
        learn_bits.append("★用户此刻**屏幕上正在看的部分**(注意力焦点;整节/整章其余内容只是背景。回答、找视频/"
                          f"配图、拟搜索关键词都要紧扣这里,别退回泛泛的章节主题):「{vtext[:900]}」")
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
                _mrc, _lpc = _vb_src(ctx["file_rel"], cur_p)   # 合并书:圈画文字按真实成员页
                circled = _clean_tag(_pdfm._text_under_ink(_mrc, _lpc, strokes=fe_ink))
            # 其余可见页 / 没拿到时回退服务端 sidecar
            for p in vis:
                p = int(p) if p else 0
                if not p or p == cur_p:
                    continue
                _mrp, _lpp = _vb_src(ctx["file_rel"], p)   # 合并书:其余可见页同理
                if _pdfm._page_ink_strokes(_mrp, _lpp):
                    inked_pages.append(p)
                    if not circled:
                        circled = _clean_tag(_pdfm._text_under_ink(_mrp, _lpp))
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
    # ★创造物库(用户设计,替代零散的 recent_check 专线):上下文只注入**最近创造物清单**(告知+句柄),
    #   AI 用 recall_creation(id) 按需取回全文——纸/报告/联网搜索/视频/翻译一并覆盖(summary-plus-handle)。
    check_line = ""
    _cl = _creations_recent_line(_uid0)
    if _cl:
        check_line = ("\n★最近创造物(之前工具的产出,句柄=#id;**内容不在这里**,要用就 recall_creation(id=…)取回):\n" + _cl +
                      "\n用户提到『刚才查的/搜的/那张纸/那个结果/第几题』→ 先 recall_creation 拿到对应创造物再答。"
                      "纸类条目会给**题目+标准答案+检查报告(有的话)**——题目是纸上自制的,书里没有逐字原文,别去书里找题目。")
    # ★学习近况(困难档案):recency 最近 active + relevance 当前阅读语境检索;注入=清单式(concept+一句原因+怀疑/建议),
    #   详情靠对话自然展开。别主动打断,用户问到相关内容时才结合。archived 不注入,resolved 仅语境命中时低权翻出。
    situation_line = ""
    try:
        import learning_situations as _LS
        _sq = " ".join(x for x in [ctx.get("book_name") or "", sel or "", sent or ""] if x)[:500]
        situation_line = _LS.context_line(_uid0, _sq)
    except Exception:
        pass
    return (
        "你是网页 PDF 阅读器的侧边栏助手,像 Copilot 一样陪用户读书。用简洁中文口语聊天。\n"
        "你能调用下面的工具来读页面内容、搜索、翻译、制卡、整理笔记、跳页等,可以连续调用多个工具来完成复合请求"
        "(例如『总结这页再做成卡』= 先 read_page,再据此回答,再 make_anki)。\n"
        "★【工具一定可用·别幻觉】这些工具**随时都能调用**,书也开着(上面给了当前书/页)。"
        "**严禁**回复『我读不到页面内容/工具暂时没法调用/无法访问书本/没法确认这页』之类的话——那是错的,你只是**还没去调**。"
        "要页面正文就 read_page、要整章正文就 summarize_section、不确定章节页范围就 toc、要画/删高亮就 highlight/find_highlights——**先调工具拿到真实内容再回答**,别凭空说做不到、别让用户自己把内容贴给你。"
        "只有当某次工具**真的返回了 error 字段**时,才如实把那个具体错误告诉用户(并说你试了什么),不许笼统甩锅『工具不可用』。\n"
        "★【写操作守卫·最高优先级】make_anki(制卡)/ make_note(整理笔记)/ add_vocab(加生词)/ notes_create·notes_edit(建/改便签)是**有副作用**的写操作,"
        "**只有用户在这条消息里明确要求**才能调:出现『做成卡/制卡/做张卡/加到 Anki』才 make_anki;『整理成笔记/记成笔记/存成笔记』才 make_note;『加生词/加到生词本/收藏这个词』才 add_vocab;"
        "『帮我记个便签/在这页贴张便签/把便签改成…』才 notes_create/notes_edit(问『我记了什么便签』只是**读**,用 notes_query/notes_read,不算写)。"
        "用户只说『总结/讲解/读一下/这页讲了啥/这页知识点/翻译/解释』——这些都只是要**文字回答**,"
        "**绝不许**顺手 make_anki / make_note / add_vocab / notes_create。拿不准用户到底要不要卡时:先给文字总结,然后在回答里问一句『要我做成 Anki 卡吗?』,**别擅自制卡**。\n"
        "★配图:讲到具体/生僻、视觉信息真有帮助的概念时(某种矿物/历史文物/生物物种/机械结构/天体/建筑/仪器等有明确实物形象的东西),"
        "可用 search_image(Wikipedia 真实图片,免费无 key,非 AI 生成)拿到图后在回答里用 ![简短说明](image_url) 插入;"
        "**别对每个词都调**,基础常见词(力/能量/速度等)和抽象理论/数学推导**不需要**配图,大多数回答根本用不上这个工具。\n"
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
        "★**批量标注整章/多页重点**(如『把这一章/第X-Y页的重点都高亮』)→ **直接用 auto_highlight**(它内部逐页把正文外包给挑句专家、画好高亮、只回简报,正文不进你的上下文,省大量 token):"
        "说『这一章』先 **toc** 查目录拿到该章起止印刷页 → auto_highlight(from=起, to=止);用户直接给了页就传 pages=[...] 或 from/to。"
        "**别自己逐页 read_page+highlight**(那会把每页正文反复灌进上下文,又慢又贵)。auto_highlight 回来后,把『标了哪些页、共多少句』简洁告诉用户即可。\n"
        "★**删除/取消/清理高亮**(如『把这页/这一章/自序的高亮都取消』)→ 拿 **find_highlights** 列出匹配高亮(每条带「跳转+删除」按钮让用户自己点)。"
        "**这就是删除工具,绝不要说没有**。定范围:① 用户给了页 → 传 page/pages;② 说『第N章/某节/前言』→ **先 toc 查目录**拿到该章起止印刷页,再 find_highlights(from=起, to=止);③ 目录没有/拿不到 → find_highlights(page=\"all\") 列全书让用户自己挑。"
        "**严禁为了删高亮去 summarize_section / 总结 / read_page**(定位章节用 toc,不是 summarize_section——后者会真总结全章,纯浪费;用户只想删,没要总结/正文)。"
        "find_highlights 调完就**停**:只简短说一句『下面是这些高亮,可逐个跳转或删除』,**别再做任何别的事**(别总结、别制卡、别读正文),更别自己替用户删。\n"
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
        "★**绝对禁止编造图片链接**:回答里任何 ![](url) 图片的 url **只能是 search_image 工具刚返回的 image_url**,"
        "**严禁**凭记忆写维基/教科书/任何你以为存在的图片 URL——编的链接一定加载失败、显示成破图。要配图**必须先调 search_image** 拿真实 url 再插;搜不到就纯文字讲、别放图。\n"
        "★**写操作诚实铁律**:『已高亮/已制卡/已记笔记/已加生词/已建纸/已创建便签』这类**完成话术,只有本轮真调了对应工具且它返回成功之后才许说**。"
        "没调工具就绝不能声称做了(那是编造,用户实测抓到过:模型只 read_page 就说『已高亮5句』,页面上什么都没有)。"
        "用户要你高亮/制卡/记笔记 → **当场调 highlight/auto_highlight/make_anki/make_note 等对应工具**;不打算调就明说没做。\n"
        "★用户说『讲讲这里/这段/当前/这部分/这里的内容』且系统已给了『★用户此刻屏幕上正在看的部分』→ **直接基于那段可见文字讲解**,"
        "**别调 read_section/summarize_section 去读整节/整章**(EPUB 一节=整章,读了会答成全章总结、跑偏用户真正在看的点);"
        "只有用户明确说『这一节/这一章/总结本章/整节』才读整节。找视频/配图的搜索词也**紧扣这段可见内容**,别用章节泛主题。\n"
        "★【追问建议】每次给最终回答时,在正文最后**另起一行**写 2-3 个贴合当前内容、能推进理解的下一步问题,"
        "格式就一行:[[FOLLOWUP]]问题1|问题2|问题3(用 | 分隔,放在整条回答末尾,前端会渲成可点按钮;问题要短、具体)。"
        "**每条最终回答都要带**;只有在调工具(输出 JSON)那几条里不要带。\n\n"
        f"【可用工具】\n{cat}\n\n"
        f"【当前页面】{json.dumps(meta, ensure_ascii=False)}{sel_line}{fig_line}{note_line}{learn_line}{check_line}{situation_line}"
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

def _explicit_attach_lines(ctx):
    """用户**显式选中/带入**的内容(右侧带 ✕ 的 chip):选中文字 / 图 / 便签 / 钉住片段。
    「书页」关(no_book)时也保留这些——当成脱离书本的独立片段/图喂给 AI(用户诉求:关书页不该连选中都看不见)。"""
    out = []
    sel = _clean_tag(ctx.get("selection"))
    if sel:
        out.append(f"· 用户选中的文字(独立片段,别管它出自哪本书):「{sel[:400]}」")
    figs = ctx.get("figures") or ([ctx["figure"]] if ctx.get("figure") else [])
    if figs:
        items = []
        for i, fg in enumerate(figs, 1):
            if fg.get("kind") == "note":
                items.append(f"  [{i}] 手写便签:「{_clean_tag(fg.get('desc'))[:300]}」(笔画内容用 see_figure 看)")
            else:
                cap = _clean_tag(fg.get("caption")); desc = _clean_tag(fg.get("desc"))
                items.append(f"  [{i}]「{cap[:48]}」:{desc[:300] or '(无描述,see_figure 看)'}")
        out.append("· 用户带入的图(独立看待;要核对图里细节用 see_figure,args {index:第几张}):\n" + "\n".join(items))
    notes_att = [n for n in (ctx.get("notes") or []) if isinstance(n, dict) and n.get("text")]
    if notes_att:
        items = [f"  [{i}]「{_clean_tag(nb.get('text'))[:400]}」" for i, nb in enumerate(notes_att[:4], 1)]
        out.append("· 用户带入的便签:\n" + "\n".join(items))
    fsel = ctx.get("focus_sel") or {}
    if isinstance(fsel, dict) and fsel.get("text") and not sel:
        out.append(f"· 用户钉住的片段:「{_clean_tag(fsel.get('text'))[:400]}」")
    return "\n".join(out)


def _ctx_block(ctx):
    """动态部分(【当前页面】+ 选中/图/知识点/笔迹),每轮随 ctx 变 → 拼进 user message。"""
    if ctx.get("no_book"):   # 用户点暗「书页」开关:不喂书本大上下文,当通用助手答(可问跟书无关的问题)
        base = ("【当前状态】用户临时关闭了「书页」上下文开关——这一轮请当**通用助手**回答,"
                "不使用书里的定位/周边内容、别主动调**读书导航类**工具(read_page/search_book/summarize_section/toc 等),"
                "除非用户在本条消息里明确要求查书。")
        att = _explicit_attach_lines(ctx)   # 但用户显式选中/带入的 chip 仍保留(独立片段/图)
        if att:
            base += ("\n【用户提供的内容(独立片段,与整本书无关)】\n" + att +
                     "\n→ 用户仍显式带来了上面这些内容,请**针对它们**回答(可用 lookup_word/translate/explain/see_figure 处理它们),"
                     "只是别把它们跟书的其余内容/章节挂钩、也别为它们去 read_page。")
        return base + _pinned_lines(ctx)
    full = _sys_prompt(ctx)
    i = full.rfind("【当前页面】")
    out = full[i:] if i >= 0 else ""
    return out + _pinned_lines(ctx)


def _pinned_lines(ctx):
    """97(用户设计):长按带入的卡片(语音/文字模式通用)——文字助手 send 时经 ctx.pinned 注入。"""
    pn = ctx.get("pinned") or []
    if not isinstance(pn, list) or not pn:
        return ""
    lines = []
    for p0 in pn[:8]:
        if isinstance(p0, dict) and (p0.get("text") or "").strip():
            lines.append(f"· 「{_clean_tag(p0.get('label'))[:60]}」:{_clean_tag(p0.get('text'))[:2000]}")
    if not lines:
        return ""
    return "\n【用户长按带入的卡片(明确要你参考的内容)】\n" + "\n".join(lines)


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


def _tool2(name, label, args=None, status="running", res=None, sec=None, sub_steps=None, model=None, action=None):
    """工具指示器 v2 的结构化事件(与旧 tool/tool-done 并行发,老前端忽略即可)。
    前端 rc-toolchip 用 name 判类型/颜色、用 task_id 继续轮询后台步骤、用 brief 填方块。"""
    d = {"name": name, "label": label, "status": status, "args": args or {}}
    if sec is not None:
        d["sec"] = sec
    if sub_steps:   # 137(用户):工具内部又调了别的工具/模型 → 它们是**外层卡的步骤**(长条里滚 + 流程图节点),不另起一张卡
        d["sub_steps"] = [{"label": x.get("label", ""), "detail": (x.get("detail") or "")[:600],
                           "model": x.get("model"), "action": x.get("action"), "sec": x.get("sec")} for x in sub_steps][:12]
    if model:   # 139(用户):这一步用了哪个模型 / 哪个动作预设 → 详情窗要显示"模型选择"
        d["model"] = model
    if action:
        d["action"] = action
    if isinstance(res, dict):
        if res.get("task_id"):
            d["task_id"] = res["task_id"]
        if res.get("_fed_images"):   # ★ #8:把**实际喂给 AI 的图**带给前端 → 流程「AI 请求」节点展示(点击放大)
            try:
                d["vision"] = [{"media_type": v.get("media_type", "image/png"), "b64": v.get("b64")}
                               for v in res["_fed_images"]
                               if isinstance(v, dict) and v.get("b64") and len(v["b64"]) < 1300000][:3]   # 太大不塞 SSE
            except Exception:
                pass
            res.pop("_fed_images", None)   # 抽完就从 res 拿掉:b64 别再进 brief / 别再喂回模型 content(省 token + 杜绝裸 base64 泄漏)
        if res.get("error"):
            d["status"] = "error"
            d["brief"] = str(res["error"])[:300]
        else:
            try:
                d["brief"] = _step_detail(res)[:4000]   # 放宽(原800):感叹号"每步 detail 全量"语义并入卡片流程,靠它承载
            except Exception:
                d["brief"] = ""
    return {"event": "tool2", "data": d}


_TOOL_START_RE = re.compile(r'\{\s*"tool"')


def _display_prefix(acc):
    """流式:accumulated 文本里**工具调用 JSON 起点之前**的可显示散文(工具 JSON 不吐给前端)。
    模型有时在工具 JSON 前先写几句(如"好的,我来找视频")→ 只显示到 {"tool" 前,JSON 藏起来由 _parse_tool 执行;
    整串以 { 开头但 tool 键还没成形 → 返回 ''(全隐,等成形/收尾)。用户报的『{}泄漏到流式输出』根治。"""
    if not acc:
        return acc
    m = _TOOL_START_RE.search(acc)
    if m is not None:
        return acc[:m.start()]
    if acc.lstrip().startswith("{"):
        return ""
    return acc


def _parse_tool(raw):
    """开头是 {"tool":...} JSON → 工具调用;否则 None(当作给用户的回答)。
    用 raw_decode 只解析**开头那个 JSON 对象**,容忍尾部多余内容(模型偶尔在工具 JSON 后跟了
    [[FOLLOWUP]]/解释 → 整串 json.loads 会失败,导致工具 JSON 被当回答显示、工具不执行)。容忍 ```json 围栏。"""
    import re
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    if not s.startswith("{"):
        # 前导散文容错:模型偶尔在工具 JSON 前先写几句(如"好的,我来找视频")→ 从第一个 {"tool" 处起解析,
        #   否则整串被当回答显示、工具不执行(用户报的『{}泄漏到输出且没执行』根因)。
        m = re.search(r'\{\s*"tool"', s)
        if not m:
            return None
        s = s[m.start():]
    # 字面控制字符(模型把多行 OCR 文本照抄进字符串值、没转义换行 → JSON 非法解析失败 → 工具不执行)→ 换空格。
    # 合法 JSON 的控制字符必是 \n/\uXXXX 多字符转义,绝不会是字面 0x00-0x1f,故这步对合法 JSON 是 no-op。
    import re
    s = re.sub(r"[\x00-\x1f]", " ", s)   # 字面控制字符(没转义的换行)→ 空格
    # 容错两遍:① 原样 ② 修**非法反斜杠转义**——模型常在 text 里写 LaTeX(如 $\leftrightarrow$ / \frac),
    #   \l \f 等在 JSON 里非法 → 把 \ 后不是 " \ / b f n r t u 的改成字面 \\(既能解析,笔记里也保留 LaTeX 源码)。
    for cand in (s, re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', s)):
        try:
            d, _end = json.JSONDecoder().raw_decode(cand)   # 解析开头第一个 JSON 值,忽略其后内容
            if isinstance(d, dict) and "tool" in d:
                return d
        except Exception:
            continue
    return None


# 每用户「按动作」的 (后端/型号/深度) 预设(感叹号弹窗的 ⚙ + 🐢/🎯 + 模型设置面板 写它)。
# action ∈ {orchestrator(根:分配+回答), summarize(章节总结), vision(看图)};值 = {backend, variant, depth}。
# 向后兼容旧 {model, effort}(无 backend → 视作 claude, model→variant, effort→depth)。无预设 → 用 _AP_DEFAULTS。
# ── 140(用户设计):工具提示词覆盖 ──
#   「长按工具 → 直接改 AI 工具里的 prompt,甚至工具的说明 —— 凡是会进 AI 并实际产生影响的,
#     都给一个输入框」。存 per-user 覆盖;运行时**真的**用它(不是只显示)。
#   两类可改文本:
#     · desc   工具说明 —— 进**工具目录**(文字 agent 的 _sys_prompt + 实时语音 session 的 tools[].description)
#     · slots  工具内部自己调 AI 时用的 prompt(下面 TOOL_SLOTS 注册)
_TP_PATH = CLAUDE_DIR / "state" / "assistant-tool-prompts.json"
_tp_lock = threading.Lock()


def _tp_load():
    try:
        return json.loads(_TP_PATH.read_text("utf-8"))
    except Exception:
        return {}


def _tp_save(d):
    with _tp_lock:
        _TP_PATH.parent.mkdir(parents=True, exist_ok=True)
        _TP_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")


def _tp(uid, tool, slot, default):
    """取生效文本:用户改过就用改的,没改用默认。**运行时唯一入口**——所有喂给 AI 的地方都走它。"""
    if slot.startswith("_"):
        return default
    try:
        v = ((_tp_load().get(str(uid)) or {}).get(tool) or {}).get(slot)
        if isinstance(v, str) and v.strip():
            return v
    except Exception:
        pass
    return default


# 工具内部 prompt 的可改槽位:{工具: {槽位: (人话标签, 默认文本)}} —— 详情窗按这个渲染输入框
TOOL_SLOTS = {}


def _slot(tool, key, label, default):
    TOOL_SLOTS.setdefault(tool, {})[key] = (label, default)
    return default


def _tps(uid, tool, key):
    """取某个已注册槽位的生效文本(用户改过 → 用改的;没改 → 默认)。"""
    d = (TOOL_SLOTS.get(tool) or {}).get(key)
    return _tp(uid, tool, key, d[1] if d else "")


# 工具内部**真正喂给 AI**的 prompt(改了立刻生效):
_slot("auto_highlight", "main", "挑句指令(逐页挑重点)",
      "下面是几页书的正文,每页以【页N】开头。请**逐页**挑出每页 **1~3 句最重要**的(定义/核心结论/关键公式/易错点),"
      "**逐字照抄原文**(不改写/不翻译/不合并/不跨段)。返回一个 JSON 对象 {\"页N\":[\"原句1\",\"原句2\"], ...};只输出 JSON,别加别的。")
_slot("web_search", "sys", "联网搜索的系统指令(决定它怎么搜、怎么组织结果)",
      "联网搜索,然后只输出一个 JSON 对象(禁止其他任何文字/代码块标记):\n")
_slot("dictation_grade", "main", "判分指令(纸上「让 AI 检查」按钮按下时,判分 AI 怎么看手写、怎么打分)",
      "逐空:识别用户写在空里的手写内容,判断对错/点评(手写体,允许潦草)。有标准答案的判对错。")


_AP_PATH = CLAUDE_DIR / "state" / "assistant-action-prefs.json"
_ap_lock = threading.Lock()
_BACKENDS = ("claude", "gemini", "codex")
_CLAUDE_VARIANTS = ("haiku", "sonnet", "opus")
_CODEX_VARIANTS = ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.5",
                   "gpt-5.4", "gpt-5.4-mini")             # model/list 实测清单;luna 前置=官方定位"清晰重复的提取/转换/摘要"正合阅读场景
_CODEX_DEPTHS = ("low", "medium", "high", "xhigh", "max", "ultra")   # 5.6 系到 ultra;选了型号不支持的档 API 报错→自动回落
# *-latest 别名永远指向当代最新(现 flash-latest=3.5-flash、pro-latest=3.1-pro);Google 出新版自动跟,不用改代码。
# 另列具体版本号给想锁定版本的。pro 线目前最高只有 3.1(还没 3.5-pro),pro 是最强推理档(版本号低≠更弱)。
# 兜底型号清单:仅当 ListModels 拉取失败时用(正常面板走 _gemini_models() 动态拉真实可用清单 → 新模型自动出现)。
_GEMINI_VARIANTS = ("gemini-flash-latest", "gemini-pro-latest",
                    "gemini-3.5-flash", "gemini-3.1-pro-preview", "gemini-2.5-pro")
_gemini_models_cache = {"ts": 0.0, "list": None}
_GEMINI_MODELS_TTL = 6 * 3600   # 动态清单缓存 6h(新模型 6h 内自动出现在面板)


def _is_gemini(v):
    return bool(v) and str(v).startswith("gemini-")


def _sort_gemini_models(names):
    """排序:*-latest 最前,再按版本号降序(新在前),同级 pro 在 flash 前。"""
    import re
    def k(n):
        mv = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
        return (0 if n.endswith("-latest") else 1, -float(mv.group(1)) if mv else 0.0,
                0 if "pro" in n else 1, n)
    return sorted(names, key=k)


def _list_gemini_models_for_key(key):
    """单把 key 的 ListModels → 文本生成型号集合(排除 image/tts/embedding 等);失败返回 None
    (跟空集区分开:单边拉失败不能拿去算「仅付费」差集,会把免费也有的型号误标付费)。"""
    try:
        import requests
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models?key=" + key, timeout=20)
        if r.status_code != 200:
            return None
        names = set()
        for m in (r.json().get("models") or []):
            n = (m.get("name") or "").replace("models/", "")
            if (n.startswith("gemini")
                    and "generateContent" in (m.get("supportedGenerationMethods") or [])
                    and not any(x in n for x in ("image", "tts", "embedding", "robotics", "computer-use", "aqa"))):
                names.add(n)
        return names or None
    except Exception:
        return None


def _gemini_models():
    """动态拉 ListModels → 当前可用的**文本生成**型号。**合并 free+paid 两把 key 的返回**:
    paid-only 型号(如 gemini-3.1-pro-preview)只出现在付费 key 的清单里,以前只用 free key 拉 →
    面板永远没有它(2026-07 修)。两边都拉成功时顺便算出「仅付费」差集持久化(_save_paid_only →
    面板标💰 + _gemini_keys 跳过 free)。缓存 6h;失败→上次结果→静态兜底清单。"""
    now = time.time()
    c = _gemini_models_cache
    if c["list"] is not None and now - c["ts"] < _GEMINI_MODELS_TTL:
        return c["list"]
    per = {}   # {tier: set(型号)}:两把 key 各自的清单(ListModels 不耗生成额度,不看冷却)
    for tier, f in _GEMINI_KEY_FILES:
        try:
            k = f.read_text().strip()
        except Exception:
            k = ""
        if k:
            got = _list_gemini_models_for_key(k)
            if got:
                per[tier] = got
    merged = set()
    for s in per.values():
        merged |= s
    names = _sort_gemini_models(list(merged))
    if names:
        c["ts"] = now; c["list"] = names
        if per.get("paid"):
            prev = _paid_only_all()
            # 差集只在两边都拉成功时更新(单边失败会误标);paid 全量清单拉到就更新
            only = (per["paid"] - per["free"]) if per.get("free") else prev["only"]
            _save_paid_only(only, per["paid"])
    return names or c["list"] or list(_GEMINI_VARIANTS)
_AP_MODELS = _CLAUDE_VARIANTS              # 兼容旧引用(感叹号 force_model 仍只在 Claude 三档里爬梯子)
# orchestrator/summarize/vision = 侧边栏助手;explain/translate/dict/grammar = 阅读器其它 AI 入口
# (解释·问AI·选中查询 / 翻译·例句 / 字典AI·日语深入讲解 / 语法分析),统一走脱壳 claude + Gemini 双后端。
_AP_ACTIONS = ("orchestrator", "summarize", "vision", "deep", "agent", "paper", "explain", "translate", "dict", "grammar", "pick_video", "img_norm", "web_search", "route_text", "dictation_grade")
# 各 action 出厂默认(无用户预设时 _resolve 回退到这)。depth='auto'(仅 orchestrator)= 按问题自动路由 effort。
_AP_DEFAULTS = {
    "orchestrator": {"backend": "claude", "variant": "sonnet",            "depth": "auto"},
    "deep":         {"backend": "claude", "variant": "opus",              "depth": "high"},   # 语音通话 deep_think 虚拟工具用
    # 148:do_task 后台 agent worker(无头 CLI + 我们的 MCP)。默认 **claude/opus**(用户 Max 5x,额度够用)。
    #   实测 worker 耗时**几乎不随步数增长**(内部连调 MCP 无 realtime 往返):
    #     opus  2步 9.1s / 3步 11.1s / 5步 11.1s   ← 封顶 ~11s
    #     codex 2步 15.3s / 3步 15.9s / 5步 18.8s  ← 慢,但白嫖 ChatGPT 额度(不动 Claude 池)
    #   而直接调每加一步就多一整轮 realtime 往返(实测 p10=2.0s,p25=5.0s)。
    #   ⇒ opus 从 **2 步**就开始占优;codex 要 3 步。**1 步两者都零收益**(见 _t_do_task)。
    #   opus 失败 → 自动降级 codex(白嫖兜底),见 voice.py::_task_agent。
    "agent":        {"backend": "claude", "variant": "opus",              "depth": "low"},
    # 造纸 / 设计插入内容(出题、排布 blocks):认知要求高 → 默认给**更深的思考档**(用户拍板;可在 ⚙/长按第一行工具条改)
    "paper":        {"backend": "claude", "variant": "opus",              "depth": "high"},
    "img_norm":     {"backend": "gemini", "variant": _GEMINI_MODEL,       "depth": "none"},   # 77:配图关键词规范化(可自定义型号)
    "web_search":   {"backend": "gemini", "variant": _GEMINI_MODEL,       "depth": "none"},   # 91:联网搜索结构卡(grounding,深度无效恒不思考)
    "route_text":   {"backend": "gemini", "variant": _GEMINI_MODEL,       "depth": "none"},   # 91:路由详答文字引擎(SSE 流式)
    "summarize":    {"backend": "gemini", "variant": "gemini-3.5-flash",  "depth": "think"},
    "vision":       {"backend": "gemini", "variant": "gemini-3.5-flash",  "depth": "think"},
    # 听写批改:看的是**手写体**,比一般看图难 → 默认给更强的档(可在 ⚙ 模型面板改)
    "dictation_grade": {"backend": "gemini", "variant": "gemini-3.5-pro", "depth": "think"},
    "explain":      {"backend": "gemini", "variant": "gemini-3.5-flash",  "depth": "think"},
    "translate":    {"backend": "gemini", "variant": "gemini-3.5-flash",  "depth": "none"},
    "dict":         {"backend": "gemini", "variant": "gemini-3.5-flash",  "depth": "think"},
    "grammar":      {"backend": "gemini", "variant": "gemini-3.5-flash",  "depth": "think"},   # 2026-07 从 explain 拆出
    "pick_video":   {"backend": "gemini", "variant": "gemini-3.5-flash",  "depth": "none"},   # 找视频:拟搜索词 + 搜后按相关性筛选(便宜 flash 够用)
}
_AP_LABELS = {   # 设置面板给每个阅读器 action 显示的中文名
    "paper": "造纸 / 设计练习纸(出题+排布 blocks,思考要深)",
    "dictation_grade": "听写批改(看手写体,比一般看图难)",
    "deep": "深度思考(语音通话专用)",
    "img_norm": "配图关键词规范化(搜图没中时转 Commons 规范名)",
    "explain": "解释 / 问 AI / 选中查询", "translate": "翻译 / 例句", "dict": "字典 AI / 日语深入讲解",
    "grammar": "语法分析(长句结构 / 语法点)", "pick_video": "找视频(拟搜索词 + 相关性筛选)",
    "web_search": "联网搜索(天气/新闻/事实 结构卡)", "route_text": "路由详答(语音转文字长回答引擎)",
}
_VARIANT_SHORT = {"gpt-5.5-codex": "5.5-codex", "gpt-5.5": "5.5",
                  "gemini-flash-latest": "flash-latest", "gemini-pro-latest": "pro-latest",
                  "gemini-3.5-flash": "3.5-flash", "gemini-3.1-flash-lite": "3.1-lite",
                  "gemini-3.1-pro-preview": "3.1-pro", "gemini-2.5-flash": "2.5-flash",
                  "gemini-2.5-flash-lite": "2.5-lite", "gemini-2.5-pro": "2.5-pro"}


def _variant_short(v):
    bare, paid = _variant_paid(v)   # '@paid' 直连付费后缀 → 简称后标「·付费」
    if paid:
        return _variant_short(bare) + "·付费"
    if v in _VARIANT_SHORT:
        return _VARIANT_SHORT[v]
    if str(v).startswith("gemini-"):   # 动态型号:从名字生成简称
        return v.replace("gemini-", "").replace("-preview", "")
    return v


def _variant_ok(backend, variant):
    if backend == "gemini":
        return _is_gemini(variant)   # 动态清单 → 宽松:任何 gemini-* 都收(前端只从 ListModels 拉的清单里选)
    if backend == "codex":
        return str(variant).startswith("gpt-")   # 宽松同 gemini 哲学(新型号自己填也收)
    return variant in _CLAUDE_VARIANTS


def _depth_ok(backend, depth):
    if backend == "gemini":
        return depth in ("none", "think")
    if backend == "codex":
        return depth in _CODEX_DEPTHS
    return depth == "auto" or depth in _EFFORTS   # claude: auto(仅 orchestrator) + low..max


def _ap_norm(d):
    """把存储里一条预设(新三维 or 旧 {model,effort})规整成 {backend,variant,depth} 或 None(非法/缺)。"""
    if not isinstance(d, dict):
        return None
    if d.get("backend"):                          # 新格式
        b = d.get("backend")
        if b in _BACKENDS and _variant_ok(b, d.get("variant")) and _depth_ok(b, d.get("depth")):
            return {"backend": b, "variant": d["variant"], "depth": d["depth"]}
        return None
    if d.get("model") in _CLAUDE_VARIANTS and d.get("effort") in _EFFORTS:   # 旧格式 → claude
        return {"backend": "claude", "variant": d["model"], "depth": d["effort"]}
    return None


def _ap_all(uid):
    try:
        return (json.loads(_AP_PATH.read_text("utf-8")) or {}).get(str(uid), {}) or {}
    except Exception:
        return {}


def _ap_get(uid, action):
    """该用户给某动作设的预设 → {backend,variant,depth} 或 None(用默认)。"""
    return _ap_norm(_ap_all(uid).get(action))


def _ap_set(uid, action, backend, variant, depth):
    """设/清某动作预设(三者非法 → 清除回默认)。返回保存后的 dict 或 None。"""
    with _ap_lock:
        try:
            full = json.loads(_AP_PATH.read_text("utf-8")) if _AP_PATH.exists() else {}
        except Exception:
            full = {}
        if not isinstance(full, dict):
            full = {}
        u = full.setdefault(str(uid), {})
        if backend in _BACKENDS and _variant_ok(backend, variant) and _depth_ok(backend, depth):
            u[action] = {"backend": backend, "variant": variant, "depth": depth}
        else:
            u.pop(action, None)
        try:
            _AP_PATH.parent.mkdir(parents=True, exist_ok=True)
            _AP_PATH.write_text(json.dumps(full, ensure_ascii=False), "utf-8")
        except Exception:
            pass
        try:   # ★预设即配置本体(用户设计):有「应用中」预设 → 每个单项改动**同步固化进该预设**
               #   (点开哪个预设改的就是哪个,切走再切回改动还在);没建过预设 → 只写生效配置(行为同旧)。
            allp = json.loads(_APF_PATH.read_text("utf-8")) if _APF_PATH.exists() else {}
            mine = allp.get(str(uid)) if isinstance(allp, dict) else None
            act = mine.get("_active") if isinstance(mine, dict) else None
            if act and isinstance(mine.get(act), dict):
                mine[act] = dict(u)   # 固化 = 该预设直接持有当前全套生效配置
                _APF_PATH.write_text(json.dumps(allp, ensure_ascii=False), "utf-8")
        except Exception:
            pass
        return u.get(action)


def _resolve(action, uid, force=None):
    """该 action 最终用的 {backend,variant,depth}。优先级:force(感叹号一次性) > 用户预设 > 出厂默认。"""
    base = dict(_AP_DEFAULTS.get(action) or _AP_DEFAULTS["orchestrator"])
    pref = _ap_get(uid, action)
    if pref:
        base = dict(pref)
    if isinstance(force, dict) and force.get("backend") in _BACKENDS:
        b = force["backend"]
        v = force.get("variant"); d = force.get("depth")
        return {"backend": b,
                "variant": v if _variant_ok(b, v) else (base["variant"] if base["backend"] == b else _AP_DEFAULTS[action]["variant"]),
                "depth":   d if _depth_ok(b, d) else (base["depth"] if base["backend"] == b else _AP_DEFAULTS[action]["depth"])}
    return base


_READER_SYS = ("你是 PDF 阅读辅助。回答简洁、准确、就事论事。"
               "数学公式一律用 $...$ 或 $$...$$,不要用反引号包数学。")


def reader_ask(prompt, action="explain", uid="", system=None, timeout=90):
    """供 PDF 阅读器其它 AI 入口(翻译/解释/字典…)的**同步**调用:按 action 预设选脱壳 claude/Gemini
    (一边失败自动兜底另一边),返回文本。统一了原先各处分散的 ai_backends 调用。"""
    r = _resolve(action, uid)
    if _paid_recover_check(uid, action):   # @paid 且免费恢复 → 预设已摘除,重读让本次就用免费(无提示通道,静默)
        r = _resolve(action, uid)
    p = ((system or _READER_SYS) + "\n\n" + prompt) if (system or True) else prompt
    return _deep_ask(p, backend=r["backend"], variant=r["variant"], depth=r["depth"], timeout=timeout) or ""


def reader_stream(prompt, action="explain", uid="", system=None, timeout=120):
    """**流式**版:按 action 预设选后端 yield 文本块;主后端流式失败(且未吐过内容)→ 兜底另一后端,
    再不行 → 退一次性 _deep_ask。给 SSE 端点逐字吐字用。"""
    r = _resolve(action, uid)
    if _paid_recover_check(uid, action):   # @paid 且免费恢复 → 预设已摘除,重读让本次就用免费(静默)
        r = _resolve(action, uid)
    sysmsg = system or _READER_SYS

    def via_gemini():
        contents = [{"role": "user", "parts": [{"text": prompt}]}]
        got = False
        for kind, val in _gemini_stream(sysmsg, contents,
                                        model=(r["variant"] if _is_gemini(r["variant"]) else None),
                                        think=(r["depth"] != "none"), timeout=timeout):
            if kind == "delta":
                got = True; yield val
            elif kind == "err":
                if got:
                    return
                raise RuntimeError(val)

    def via_claude():
        p = _spawn(effort=(r["depth"] if r["depth"] in _EFFORTS else "low"),
                   model=(r["variant"] if r["variant"] in _CLAUDE_VARIANTS else "sonnet"), system=sysmsg)
        if not p:
            raise RuntimeError("claude-spawn-failed")
        try:
            last = ""
            for kind, val in _send_stream(p, prompt, timeout=timeout):
                if kind == "delta":
                    new = val[len(last):]; last = val
                    if new:
                        yield new
                elif kind == "result" and val is None and not last:
                    raise RuntimeError("claude-empty")
        finally:
            _kill(p)

    if r["backend"] == "codex":   # 主路=app-server 真文字流式;失败(未吐字)→ exec 一次性 → 再落 gemini→claude
        _got = False
        try:
            for d in _codex_app.stream(sysmsg + "\n\n" + prompt, model=r["variant"], effort=r["depth"], timeout=timeout):
                _got = True
                yield d
            return
        except Exception:
            if _got:
                return
        txt0 = _codex_exec_text(sysmsg + "\n\n" + prompt, model=r["variant"], effort=r["depth"], timeout=timeout)
        if txt0:
            yield txt0
            return
    primary, secondary = (via_claude, via_gemini) if r["backend"] == "claude" else (via_gemini, via_claude)
    streamed = False
    try:
        for chunk in primary():
            streamed = True; yield chunk
        return
    except Exception:
        if streamed:
            return
    try:
        for chunk in secondary():
            yield chunk
        return
    except Exception:
        pass
    txt = _deep_ask(prompt, backend=r["backend"], variant=r["variant"], depth=r["depth"], timeout=timeout)
    if txt:
        yield txt


def reader_vision(images, prompt, action="vision", uid="", system=None, timeout=120, max_images=6):
    """PDF 阅读器看图/裁图 OCR 的统一入口:**脱壳** claude + Gemini 看图,按「看图」预设 + 互为兜底。
    images=[{media_type,b64}];system 可自定义(OCR 要 LaTeX、目录要抽章节…),传 '' 则不加系统提示。
    max_images:一次最多喂几张(默认 6;批改整张卷子要按题数放宽,否则只判前几题)。
    返回文本或 None。统一了原先直调 claude CLI(没脱壳,白加载 CLAUDE.md + 工具 schema)的看图/OCR。"""
    if not images:
        return None
    r = _resolve(action, uid)
    if _paid_recover_check(uid, action):   # @paid 且免费恢复 → 预设已摘除,重读让本次就用免费(静默)
        r = _resolve(action, uid)
    sysmsg = _VIS_SYS if system is None else system
    imgs = images[:max(1, int(max_images))]

    def via_gemini():
        return _gemini_vision((sysmsg + "\n" + prompt) if sysmsg else prompt, imgs,
                              timeout=min(timeout, 100),
                              model=(r["variant"] if _is_gemini(r["variant"]) else None),
                              max_images=len(imgs))

    def via_claude():
        p = _spawn(effort=(r["depth"] if r["depth"] in _EFFORTS else "low"),
                   model=(r["variant"] if r["variant"] in _CLAUDE_VARIANTS else "sonnet"),
                   system=(sysmsg or None))
        if not p:
            return None
        try:
            blocks = [{"type": "text", "text": prompt}]
            for v in imgs:
                blocks.append({"type": "image", "source": {"type": "base64",
                              "media_type": v.get("media_type", "image/png"), "data": v["b64"]}})
            return _send(p, blocks, timeout=timeout)
        finally:
            _kill(p)

    if r["backend"] == "claude":
        return via_claude() or via_gemini()
    return via_gemini() or via_claude()


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

# 免工具快路:解释词 + 工具信号(任一信号出现就**不**走快路,回完整编排)
_FAST_EXPLAIN_RE = r"解释|什么意思|啥意思|讲讲|讲解|说说|怎么理解|如何理解|这句|这段|为什么|为何|含义|意思是|是什么意思"
_FAST_BLOCK_RE = (r"高亮|标记|划重点|制卡|做成?卡|做张卡|生词|笔记|记成|存成|翻到|跳到|跳转|打开|搜索|搜一下|查一下|"
                  r"找.{0,5}页|这本书|别的书|其[它他]书|之前在哪|目录|这一?[章节]|整[章节]|总结这|看图|看一下|看这张|"
                  r"删除|取消|清理|去掉|撤销|画出|标出|读一下|第\s*\d+\s*页|查词|查一下.{0,2}词")
def _is_pure_explain(message, ctx):
    """纯解释类问题 + 上下文已就位 + 零工具信号 → 走免工具快路。**极保守**:必须有选中/焦点、命中解释词、
    不含任何工具信号、没带图、问题短。任一不满足都回完整编排(宁可不省、不可误判把该用工具的问题截了)。"""
    import re
    m = (message or "").strip()
    if not m or len(m) > 160:
        return False
    c = ctx or {}
    if c.get("figures") or c.get("figure"):        # 带了图 → 可能要 see_figure
        return False
    has_ctx = bool(c.get("selection") or c.get("selection_sentence") or (c.get("focus_sel") or {}).get("text"))
    if not has_ctx:
        return False
    if re.search(_FAST_BLOCK_RE, m):               # 任何工具信号 → 走完整编排
        return False
    return bool(re.search(_FAST_EXPLAIN_RE, m))

def _fast_answer(message, ctx, history, uid):
    """免工具快路:纯解释问题、上下文已就位 → **一次** _deep_ask 直接答,不带工具清单/不进 agentic 循环
    (单题系统开销从 ~5-6k 降到 ~1.5k)。返回 (answer, trace) 或 None(失败 → 调用方回退完整编排)。"""
    _t0 = time.time()
    rr = _resolve("orchestrator", uid)
    # 快路是**单轮** _deep_ask,codex 完全支持 → 不再降级(旧守卫把 codex 预设静默改回 Claude,
    # Claude 限流时用户就地撞"额度用完"——正是用户报告的 bug 之一)
    c = ctx or {}
    sel = _clean_tag(c.get("selection")); sent = _clean_tag(c.get("selection_sentence"))
    foc = _clean_tag((c.get("focus_sel") or {}).get("text"))
    bits = []
    if sel: bits.append(f"用户选中:「{sel[:320]}」")
    if sent and sent.replace(" ", "") != (sel or "").replace(" ", ""): bits.append(f"所在句:「{sent[:320]}」")
    if foc and foc != sel: bits.append(f"用户钉住的焦点:「{foc[:320]}」")
    hist = _format_history(history, int(c.get("page_offset") or 0))
    prompt = ("你是 PDF 阅读助手。**简洁**中文回答用户对下面这段选中/焦点内容的问题(解释/讲解/为什么…)。"
              "数学一律用 $...$。直接答,别提工具、别要更多信息、别复述原文。\n"
              + "\n".join(bits) + "\n" + (hist or "") + f"\n用户问题:{message}\n\n回答:")
    ans = _deep_ask(prompt, backend=rr["backend"], variant=rr["variant"], depth=rr["depth"], timeout=90)
    if not ans or not ans.strip():
        return None
    lbl = f"{(_variant_short(rr['variant']) if _is_gemini(rr['variant']) else rr['variant'])}·{rr['depth']}(免工具)"
    trace = [{"label": "直接回答(免工具)", "model": lbl, "action": "orchestrator", "sec": round(time.time() - _t0, 1),
              "detail": ans.strip()[:6000]}]
    if _tok_get():
        trace[0]["tok"] = _tok_get()
    return ans.strip(), trace


def _agent_run(message, ctx, history, force_effort=None, force_model=None, force_backend=None):
    """生成 SSE 事件 dict:{event, data}。event ∈ tool|tool-done|answer|actions|trace|error。"""
    _tok_reset()   # 本轮 token 计数清零(本线程内,后续所有 AI 调用累加,收尾写 trace[0].tok)
    uid = (ctx or {}).get("_uid")
    fe = force_effort if force_effort in _EFFORTS else None   # 感叹号「更强重答」强制档(一次性)
    # 61b:显式后端覆盖(deep 面板选 gemini/codex 时经此;此前 force_model 只认 Claude 梯子=「deep 仅 claude」根因)
    if force_backend == "gemini":
        yield from _agent_run_gemini(message, ctx, history, force_model or None, fe or "high", uid)
        return
    if force_backend == "codex":
        yield from _agent_run_codex(message, ctx, history, force_model or None, fe or "high", uid)
        return
    fm = force_model if force_model in _CLAUDE_VARIANTS else None
    if fm or fe:                                  # 感叹号「更强重答」:一次性强制 Claude 升档(始终 claude)
        if isinstance(ctx, dict):
            ctx["_no_cache"] = True               # 用户点「更强重答」= 对结果不满意 → 工具**跳过 AI 产物缓存重新生成**(并用新版覆盖旧的将就版)
        yield from _agent_run_claude(message, ctx, history, (fm or "opus"), (fe or "high"), uid)
        return
    # @paid 预设:节流探测免费额度是否恢复 → 恢复则自动摘除(下面的 _resolve 重读预设即刻生效)。
    # ⚠ 绿条不在这里预发——1-token 探测能过 ≠ 真实请求能过(探测还占 RPM 名额,曾出现"刚宣布恢复
    # 紧接着又受限切付费"的矛盾双提示):交给 _recover_gate 按**真实请求的实际结果**裁决(见其 docstring)。
    _pn_rec = _paid_recover_check(uid, "orchestrator")

    def _rest():
        if _is_pure_explain(message, ctx):        # 免工具快路:纯解释问题、上下文已就位 → 一次直答,不带工具清单
            _fast = _fast_answer(message, ctx, history, uid)
            if _fast:
                yield {"event": "answer", "data": _fast[0]}
                yield {"event": "trace", "data": _fast[1]}
                return
            # _fast=None(快路 AI 失败)→ 落回完整编排
        rr = _resolve("orchestrator", uid)
        if rr.get("backend") == "codex":          # ㉖:根 agent 跑在 Codex(app-server threadId 多轮,失败自动回退 Claude)
            yield from _agent_run_codex(message, ctx, history, rr["variant"], rr["depth"], uid)
            return
        if rr["backend"] == "gemini":             # 二期:根 agent 跑在 Gemini 工具循环(省 Claude 额度)
            yield from _agent_run_gemini(message, ctx, history, rr["variant"], rr["depth"], uid)
            return
        if _is_quick(message):                    # claude:导航/写动作走快档(effort 压低保秒回)
            eff, mdl = "low", (rr["variant"] or _AGENT_MODEL)   # 但**尊重用户设的型号**(设了 opus 就用 opus,只把 effort 压低)
        else:
            eff = (rr["depth"] if rr["depth"] in _EFFORTS else None) or _effort_for(message, ctx, uid)
            mdl = rr["variant"] or _AGENT_MODEL
        yield from _agent_run_claude(message, ctx, history, mdl, eff, uid)

    yield from (_recover_gate(_rest(), _pn_rec, uid, "orchestrator") if _pn_rec else _rest())


def _agent_run_claude(message, ctx, history, mdl, eff, uid, fallback_from=None):
    """orchestrator 跑在 Claude(剥壳进程,stream-json 多轮)。生成同样的 SSE 事件。
    fallback_from=<gemini型号>:本轮是「设的是 Gemini 但 Gemini 挂了转 Claude」→ trace 写明,别让用户误以为设置没生效。"""
    if fallback_from:
        trace = [{"label": "编排+回答(Gemini 不可用→Claude)",
                  "model": f"{_variant_short(fallback_from)}→{mdl}·{eff}", "action": "orchestrator"}]
    else:
        trace = [{"label": "编排+回答", "model": f"{mdl}·{eff}", "action": "orchestrator"}]   # 编排器档
    p = _take_proc(eff, model=mdl)
    if not p:
        yield {"event": "error", "data": "助手起不来(claude 起不来)"}
        return
    _qw = _quota_warning()   # 额度护栏:近上限只提醒,不降级、不阻断
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
        # 总时长放宽:整章高亮/逐页处理这类多步任务本就要跑很久;orchestrator 跑在 detached 后台线程(不占请求),
        # 且高亮/制卡是**即时落盘**的——即便到点也不丢已完成的。900s 兜底纯防真·runaway(模型死循环烧额度)。
        _total_to = 1200.0 if _heavy else 900.0
        for step in range(400):   # 步数不再当限制(整章逐页可能上百步);真正护栏是上面总超时,这只防真·死循环
            if time.time() - _t_start > _total_to:
                yield {"event": "answer", "data": "(这个任务很大,先做到这——已完成的部分都已保存;想接着干就再说一句『继续处理后面的』)"}
                break
            raw = None
            _last_emit = 0.0
            for kind, val in _send_stream(p, content, timeout=_round_to):
                if kind == "delta":
                    # 只吐**工具 JSON 之前**的散文;工具调用 JSON(含前导散文后的)不流式,由 _parse_tool 执行(末尾 3116 补完整 answer)
                    disp = _display_prefix(val)
                    if disp.strip():
                        now = time.time()
                        if now - _last_emit > 0.1:   # 节流 ~100ms,减 SSE/重渲(末尾 answer 会补完整)
                            _last_emit = now
                            yield {"event": "answer", "data": disp}
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
                yield _tool2(name, _tool_label(name, targs), targs, "running")
                _t_tool0 = time.time()
                try:
                    res = _run_tool(name, targs, ctx)
                except Exception as e:
                    res = {"error": str(e)[:160]}
                _tool_sec = round(time.time() - _t_tool0, 1)   # 这步耗时(感叹号弹窗显示)
                vision = res.pop("_vision", None) if isinstance(res, dict) else None   # 图片喂回大脑(sonnet 多模态)
                _gm = res.pop("_gen_model", None) if isinstance(res, dict) else None   # 生成步工具用的强模型(如 summarize=opus)
                _ga = res.pop("_gen_action", None) if isinstance(res, dict) else None   # 生成步的动作键(可在 ⚙ 里调它的预设)
                trace.append({"label": _tool_label(name, targs), "model": _gm or "—", "sec": _tool_sec, "action": _ga,
                              "detail": _step_detail(res)})   # 轨迹:任务名+模型+耗时(+动作键)+ 该步完整内容(感叹号里点开看)
                _subs = (res.pop("_sub_steps", None) or []) if isinstance(res, dict) else []
                for _ss in _subs:   # 工具内部子步骤(如找视频的相关性筛选)各占「!」一行
                    trace.append({"label": _ss.get("label", ""), "model": _ss.get("model", "—"), "sec": _ss.get("sec"),
                                  "action": _ss.get("action"), "detail": _ss.get("detail", "")})
                try:
                    _used_tools.add(name)
                except NameError:
                    _used_tools = {name}   # 本轮已调工具集(写操作幻觉硬防线用)
                _creation_register(str(ctx.get("_uid") or ""), name, targs, res, ctx)   # 创造物库:非操作型结果入库(summary-plus-handle)
                _attn_tool_event(str(ctx.get("_uid") or ""), name, targs, res, ctx)     # 注意力画像:查找类工具的查询词 → 账本(tool 渠道无天然源)
                _ae_card = isinstance(res, dict) and ((res.get("client_action") or {}).get("fn") == "_assistEdit")   # 高亮/便签卡自带逐条撤销/重做 → 抑制下面重复的 undo 行(用户实测双条)
                if isinstance(res, dict) and res.get("client_action"):
                    yield {"event": "actions", "data": [res.pop("client_action")]}   # 实时:工具一执行完就推给前端应用,不等全部输出完
                if isinstance(res, dict) and res.get("task_id") and name not in _AGENT_TASKS:   # 后台写任务(制卡/笔记)→ 卡外浮动轮询+撤销;CLI 委托任务(do_task/make_paper)走卡内 _trackCliTask,不重复
                    yield {"event": "task", "data": {"task_id": res["task_id"], "label": _tool_label(name, targs)}}
                if isinstance(res, dict) and res.get("undo_id") and not _ae_card:   # 同步写操作 → 撤销按钮(高亮/便签有 _assistEdit 卡则不重复发,治双条)
                    yield {"event": "undo", "data": {"undo_id": res["undo_id"], "label": _tool_label(name, targs), "page": res.pop("_jump_page", None) or (ctx.get("pages") or [ctx.get("page")] or [None])[0]}}
                yield {"event": "tool-done", "data": _tool_label(name, targs)}
                yield _tool2(name, _tool_label(name, targs), targs, "done", res, _tool_sec, locals().get("_subs"), _gm, _ga)
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
            _fix = None if locals().get("_claim_retried") else _claim_fix_msg(raw, locals().get("_used_tools"))
            if _fix:   # 写操作幻觉 → 拦下,打回重试一次(仅一次,防循环)
                _claim_retried = True
                content = _fix
                continue
            trace[0]["detail"] = (raw or "")[:6000]   # 编排步的「完整内容」= 最终回答(感叹号里点开看)
            yield {"event": "answer", "data": raw}
            break
        else:
            yield {"event": "answer", "data": "(步骤已经非常多了,先到这——已完成的部分都已保存,要继续就再说一句)"}
        if client_actions:
            yield {"event": "actions", "data": client_actions}
        _tool_total = sum(t.get("sec", 0) for t in trace[1:])   # 编排器自身耗时 = 总耗时 - 各工具耗时
        trace[0]["sec"] = round(max(0.0, (time.time() - _t_start) - _tool_total), 1)
        _tt = _tok_get()
        if _tt:
            trace[0]["tok"] = _tt   # 本轮累计 token(编排 + 总结/看图等所有 AI 调用)→ 前端在回答上显示「3.6k」
        yield {"event": "trace", "data": trace}   # 调用轨迹(感叹号里展示:每步任务名 + 模型 + 耗时)
    finally:
        _kill(p)
        threading.Thread(target=_warm_respawn, daemon=True).start()


def _gemini_stream(system, contents, model=None, think=True, timeout=180.0):
    """Gemini 多轮流式(streamGenerateContent SSE)。yield ("delta", chunk) 逐块;出错 yield ("err", msg)。
    正常结束不额外 yield(调用方用累积文本)。记账 label=assistant:orch(按 model 单价算钱)。"""
    keys = _gemini_keys(model or _GEMINI_MODEL)          # '@paid' 后缀在 _gemini_keys 内消化(跳过 free)
    mdl = _variant_paid(model or _GEMINI_MODEL)[0]       # URL/记账/标记一律用裸型号
    if not keys:
        yield ("err", "gemini-key-missing"); return
    cfg = {"temperature": 0.3, "maxOutputTokens": 2000}
    if not think and "pro" not in mdl:   # Pro 是 thinking-only,thinkingBudget=0 会 400
        cfg["thinkingConfig"] = {"thinkingBudget": 0}
    body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents, "generationConfig": cfg}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{mdl}:streamGenerateContent?alt=sse&key="
    last_err = "http-?"
    for key, tier in keys:
        usage = {}; emitted = False
        try:
            import requests
            with requests.post(url + key, json=body, timeout=timeout, stream=True) as r:
                if r.status_code != 200:
                    _gemini_log("assistant:orch", r.status_code, mdl, tier=tier)
                    last_err = f"http-{r.status_code}"
                    if _is_model_unsupported(r.status_code, r.text):
                        _mark_unsupported(tier, mdl)   # 永久不支持该模型 → 记住
                    elif r.status_code in (429, 403):
                        _gemini_cooldown(tier, _retry_after(r.text))   # 额度限流 → 临时冷却整把
                    # 5xx 临时高负载 / 其它 → 不直接 return,统一 continue 试下一把 key(免费不行→付费)
                    continue
                yield ("tier", tier)   # 这把(free/paid)成功放行 → 上报实际用了哪档,trace 标「免费/付费」
                for line in r.iter_lines():
                    if not line:
                        continue
                    s = line.decode("utf-8", "ignore")
                    if not s.startswith("data:"):
                        continue
                    payload = s[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        j = json.loads(payload)
                    except Exception:
                        continue
                    if j.get("usageMetadata"):
                        usage = _gemini_usage(j)
                    cand = (j.get("candidates") or [{}])[0]
                    for part in (cand.get("content") or {}).get("parts", []):
                        t = part.get("text", "")
                        if t:
                            emitted = True
                            yield ("delta", t)
        except Exception as e:
            last_err = str(e)[:120]
            if emitted:
                yield ("err", last_err); return   # 已吐内容 → 不能换 key 重试(会重复)
            continue   # 还没吐过 → 试下一把 key(如付费),别直接回退 Claude
        if usage:
            _gemini_log("assistant:orch", 200, mdl, usage.get("total", 0), usage.get("prompt", 0), usage.get("out", 0), tier)  # _gemini_log 内已累计本轮 token
        return   # 这把跑完(成功或正常结束)→ 不再试下一把
    yield ("err", last_err)   # 所有 key 都 429/403


def _compact_gemini_contents(contents, keep_full=3):
    """无状态 Gemini 编排器**每步都重发整个 contents** → 多步任务(读 N 页 / 看图)上下文随步数线性膨胀,
    早期那几页全文 + 图(inlineData,每张几千 token)被白白重发 N 次。这里把**较早的工具结果**截断、丢图,
    只保留**最近 keep_full 个**全文。设计上让"截断不致误事":
      ① 最近 keep_full=3 个结果**全文保留**——模型决定下一步、收尾合成靠的就是最近几步,这几步不动;
      ② 被截的也**保留开头 400 字**(含「第N页」「工具名」等标识 + 首段)——不是删光,是留梗概;
      ③ **没有不可逆丢失**:真要用到某个老结果的细节,模型可**重新调那个工具**(read_page(N) 等)取回——
         所以适合的是"逐页处理"这种顺序任务(处理完就不回头);需要跨多结果综合的任务通常步数少、压根到不了截断。
    → token 从 O(步数²) 降到 ~O(步数)。**只动「工具结果」user 消息**,不碰首条(任务+页面上下文)和模型的工具调用。"""
    tr_idx = [i for i, m in enumerate(contents)
              if m.get("role") == "user" and m.get("parts")
              and isinstance(m["parts"][0].get("text"), str)
              and m["parts"][0]["text"].startswith("【工具结果】")]
    for i in (tr_idx[:-keep_full] if keep_full else tr_idx):
        m = contents[i]
        t = m["parts"][0].get("text", "")
        if len(t) > 500 or len(m["parts"]) > 1:   # 长结果 或 带了图 → 留开头(含工具/页标识+首段)+ 丢图
            m["parts"] = [{"text": t[:400] + "…(该步详情已略;若后面要用可重新调对应工具取回)"}]


def _agent_run_gemini(message, ctx, history, variant, depth, uid):
    """orchestrator 跑在 Gemini(二期)。同一套工具协议/系统提示/SSE 事件,只是后端=Gemini 多轮 contents
    (无状态,每轮带完整 contents)。Gemini 首轮失败(且还没调过工具)→ 自动回退 Claude,保证不挂。"""
    model = variant if _is_gemini(variant) else _GEMINI_MODEL
    think = (depth != "none") and not _is_quick(message)   # 导航/写动作关思考更快更省
    if "pro" in model:
        think = True   # Pro 是 thinking-only(不能关),强制 think → 不触发 400 + trace 标记准确
    trace = [{"label": "编排+回答", "model": f"{_variant_short(model)}·{'think' if think else 'fast'}", "action": "orchestrator"}]
    system = _sys_static()
    content_txt = (f"{_ctx_block(ctx)}\n\n{_format_history(history, int((ctx or {}).get('page_offset') or 0))}"
                   f"【用户】{message}\n\n现在开始(调工具就只输出 JSON,能答就直接答):")
    contents = [{"role": "user", "parts": [{"text": content_txt}]}]
    client_actions = []
    _t_start = time.time()
    _tools_ran = False
    _repair = 0
    _paid_noted = False   # 「免费受限→已用付费」提示每轮请求最多一次
    try:
        for step in range(400):   # 步数不再当限制(整章逐页可能上百步);真正护栏是下面总超时
            if time.time() - _t_start > 900.0:   # 放宽:多步任务(整章高亮)要跑很久;后台线程跑、写操作即时落盘,到点不丢已完成的
                yield {"event": "answer", "data": "(这个任务很大,先做到这——已完成的部分都已保存;想接着干就再说一句『继续处理后面的』)"}
                break
            raw_parts = []; _last_emit = 0.0; _emit_len = 0; err = None
            for kind, val in _gemini_stream(system, contents, model=model, think=think):
                if kind == "delta":
                    raw_parts.append(val)
                    # 只吐**工具 JSON 之前**的散文;工具调用 JSON(含前导散文后的)一出现 disp 就冻结,不再吐(末尾补完整 answer)。
                    # ⚠ data=轮内**全量**(与 claude 管线一致):前端/relay 都按全量替换消费,增量片段会让屏幕只剩最新一小段、朗读反复重念
                    disp = _display_prefix("".join(raw_parts))
                    if len(disp) > _emit_len:
                        now = time.time()
                        if now - _last_emit > 0.1:
                            _last_emit = now
                            yield {"event": "answer", "data": disp}
                            _emit_len = len(disp)
                elif kind == "err":
                    err = val
                elif kind == "tier":
                    trace[0]["tier"] = val   # 实际服务这条的档:free/paid → trace 标「免费/付费」
                    if val == "paid" and not _paid_noted:   # 免费本是首选却落到付费 → 轻量提示(带一键转直连付费)
                        _paid_noted = True
                        _pn = _paid_fallback_note(model, "orchestrator", depth)
                        if _pn:
                            yield {"event": "gemini-paid", "data": _pn}
            raw = "".join(raw_parts).strip()
            if not raw:
                if not _tools_ran:   # 还没调过工具 → 回退 Claude 整轮重跑,保证用户有答
                    _why = f"Gemini({_variant_short(model)})不可用,转 Claude" + (f":{err}" if err else "")
                    yield {"event": "tool", "data": _why}
                    yield {"event": "tool-done", "data": _why}
                    # fallback_from=model → trace 写明「Gemini→Claude」,用户在感叹号里能看出是 Gemini 挂了不是设置没生效
                    yield from _agent_run_claude(message, ctx, history, _AGENT_MODEL, _effort_for(message, ctx, uid), uid, fallback_from=model)
                    return
                yield {"event": "error", "data": f"助手没响应({err or '超时'})。再问我一次试试。"}
                return
            tool = _parse_tool(raw)
            if tool is None and raw.startswith("{") and '"tool"' in raw[:400] and _repair < 2:
                _repair += 1   # 像工具调用却没解析出(_parse_tool 容错也救不了)→ 让 Gemini 重出一条合法 JSON
                yield {"event": "tool", "data": "整理指令"}
                yield {"event": "tool-done", "data": "整理指令"}
                contents.append({"role": "model", "parts": [{"text": raw}]})
                contents.append({"role": "user", "parts": [{"text": "你上一条像是工具调用,但 JSON 没解析成功(常见:字符串里有没转义的反斜杠/引号/换行)。"
                    "请只重新输出**一条合法 JSON**工具调用:字符串里反斜杠写成 \\\\、引号用「」、整条别带换行,别加别的字。"}]})
                continue
            if tool and tool.get("tool") in TOOLS:
                name = tool["tool"]
                if name not in _READONLY_TOOLS:
                    _tools_ran = True   # **写过**工具才挡回退(重跑会重复写);纯读轮(read_page等)后端没响应照样回退 Claude 保答
                targs = tool.get("args") if isinstance(tool.get("args"), dict) else {}
                yield {"event": "tool", "data": _tool_label(name, targs)}
                yield _tool2(name, _tool_label(name, targs), targs, "running")
                _t_tool0 = time.time()
                try:
                    res = _run_tool(name, targs, ctx)
                except Exception as e:
                    res = {"error": str(e)[:160]}
                _tool_sec = round(time.time() - _t_tool0, 1)
                vision = res.pop("_vision", None) if isinstance(res, dict) else None
                _gm = res.pop("_gen_model", None) if isinstance(res, dict) else None
                _ga = res.pop("_gen_action", None) if isinstance(res, dict) else None
                trace.append({"label": _tool_label(name, targs), "model": _gm or "—", "sec": _tool_sec, "action": _ga,
                              "detail": _step_detail(res)})
                try:
                    _used_tools.add(name)
                except NameError:
                    _used_tools = {name}   # 本轮已调工具集(写操作幻觉硬防线用)
                _creation_register(str(ctx.get("_uid") or ""), name, targs, res, ctx)   # 创造物库:非操作型结果入库(summary-plus-handle)
                _attn_tool_event(str(ctx.get("_uid") or ""), name, targs, res, ctx)     # 注意力画像:查找类工具的查询词 → 账本(tool 渠道无天然源)
                _ae_card = isinstance(res, dict) and ((res.get("client_action") or {}).get("fn") == "_assistEdit")   # 高亮/便签卡自带逐条撤销/重做 → 抑制下面重复的 undo 行(用户实测双条)
                if isinstance(res, dict) and res.get("client_action"):
                    yield {"event": "actions", "data": [res.pop("client_action")]}   # 实时:工具一执行完就推给前端应用,不等全部输出完
                if isinstance(res, dict) and res.get("task_id") and name not in _AGENT_TASKS:   # CLI 委托任务(do_task/make_paper)走卡内 _trackCliTask,不发卡外浮动 task 事件(三后端一致)
                    yield {"event": "task", "data": {"task_id": res["task_id"], "label": _tool_label(name, targs)}}
                if isinstance(res, dict) and res.get("undo_id") and not _ae_card:
                    yield {"event": "undo", "data": {"undo_id": res["undo_id"], "label": _tool_label(name, targs), "page": res.pop("_jump_page", None) or (ctx.get("pages") or [ctx.get("page")] or [None])[0]}}
                yield {"event": "tool-done", "data": _tool_label(name, targs)}
                yield _tool2(name, _tool_label(name, targs), targs, "done", res, _tool_sec, locals().get("_subs"), _gm, _ga)
                feed = "【工具结果】" + json.dumps(res, ensure_ascii=False)[:6000] + "\n\n继续(调工具只输出 JSON,能答就直接答):"
                contents.append({"role": "model", "parts": [{"text": raw}]})
                uparts = [{"text": feed}]
                if vision:   # see_page 等:渲染图 inlineData 喂回(Gemini 多模态)
                    for v in vision[:2]:
                        uparts.append({"inlineData": {"mimeType": v.get("media_type", "image/png"), "data": v["b64"]}})
                contents.append({"role": "user", "parts": uparts})
                _compact_gemini_contents(contents)   # 压缩较早工具结果(只留最近2个全文 + 丢老图)→ 上下文不随步数线性膨胀
                continue
            _fix = None if locals().get("_claim_retried") else _claim_fix_msg(raw, locals().get("_used_tools"))
            if _fix:   # 写操作幻觉 → 拦下,打回重试一次(仅一次,防循环)
                _claim_retried = True
                contents.append({"role": "model", "parts": [{"text": raw}]})
                contents.append({"role": "user", "parts": [{"text": _fix}]})
                continue
            trace[0]["detail"] = (raw or "")[:6000]   # 编排步的「完整内容」= 最终回答
            yield {"event": "answer", "data": raw}   # 最终回答(补完整,前端以此为准)
            break
        else:
            yield {"event": "answer", "data": "(步骤已经非常多了,先到这——已完成的部分都已保存,要继续就再说一句)"}
        if client_actions:
            yield {"event": "actions", "data": client_actions}
        _tool_total = sum(t.get("sec", 0) for t in trace[1:])
        trace[0]["sec"] = round(max(0.0, (time.time() - _t_start) - _tool_total), 1)
        _tt = _tok_get()
        if _tt:
            trace[0]["tok"] = _tt   # 本轮累计 token(编排 + 总结/看图等所有 AI 调用)→ 前端显示「3.6k」
        yield {"event": "trace", "data": trace}
    except Exception as e:
        yield {"event": "error", "data": f"Gemini 编排出错:{str(e)[:120]}"}


def _agent_run_codex(message, ctx, history, variant, depth, uid):
    """orchestrator 跑在 Codex(㉖,用户拍板):app-server **threadId 多轮会话**——服务端保存历史,
    每轮只发新内容(工具结果),不重拼历史(与 Anthropic 前缀缓存同解)。同一套工具协议/系统提示/
    SSE 事件。Codex 的编程 agent 本性由三重锁驯服:read-only 沙盒 + 空 untrusted cwd + prompt 明令
    只用我们的 JSON 工具协议。首轮失败(app-server 挂/无响应)自动回退 Claude,保证有答。"""
    model = variant if variant in _CODEX_VARIANTS else "gpt-5.6-luna"
    eff = depth if depth in _CODEX_DEPTHS else "medium"
    trace = [{"label": "编排+回答", "model": f"{model}·{eff}", "action": "orchestrator"}]
    first = (_sys_static() + "\n\n"
             "(补充纪律:你运行在只读沙盒的**空目录**里——**不要**使用你内置的 shell/文件/编辑工具,那里什么都没有;"
             "上面的 JSON 工具协议是你唯一的工具通道,系统会执行并把【工具结果】发给你。)\n\n"
             f"{_ctx_block(ctx)}\n\n{_format_history(history, int((ctx or {}).get('page_offset') or 0))}"
             f"【用户】{message}\n\n现在开始(调工具就只输出 JSON,能答就直接答):")
    try:
        tid = _codex_app.thread_start(model)
    except Exception as ex:
        _why = f"Codex 起不来({str(ex)[:60]}),转 Claude"
        yield {"event": "tool", "data": _why}
        yield {"event": "tool-done", "data": _why}
        yield from _agent_run_claude(message, ctx, history, _AGENT_MODEL, _effort_for(message, ctx, uid), uid, fallback_from=model)
        return
    client_actions = []
    _t_start = time.time()
    _tools_ran = False
    _repair = 0
    nxt = first
    try:
        for step in range(400):
            if time.time() - _t_start > 900.0:
                yield {"event": "answer", "data": "(这个任务很大,先做到这——已完成的部分都已保存;想接着干就再说一句『继续处理后面的』)"}
                break
            parts = []
            _last_emit = 0.0
            err = None
            try:
                for d0 in _codex_app.turn_stream(tid, nxt, eff, timeout=240):
                    parts.append(d0)
                    # 轮内全量语义(与 claude/gemini 一致):只吐工具 JSON 之前的散文
                    disp = _display_prefix("".join(parts))
                    if disp.strip():
                        now = time.time()
                        if now - _last_emit > 0.1:
                            _last_emit = now
                            yield {"event": "answer", "data": disp}
            except Exception as ex:
                err = str(ex)[:120]
            raw = "".join(parts).strip()
            if not raw:
                if not _tools_ran:   # 还没调过工具 → 回退 Claude 整轮重跑,保证用户有答
                    _why = f"Codex 没响应({err or '超时'}),转 Claude"
                    yield {"event": "tool", "data": _why}
                    yield {"event": "tool-done", "data": _why}
                    yield from _agent_run_claude(message, ctx, history, _AGENT_MODEL, _effort_for(message, ctx, uid), uid, fallback_from=model)
                    return
                yield {"event": "error", "data": f"Codex 没响应({err or '超时'})。再问我一次试试。"}
                return
            tool = _parse_tool(raw)
            if tool is None and raw.startswith("{") and '"tool"' in raw[:400] and _repair < 2:
                _repair += 1
                yield {"event": "tool", "data": "整理指令"}
                yield {"event": "tool-done", "data": "整理指令"}
                nxt = ("你上一条像是工具调用,但 JSON 没解析成功(常见:字符串里有没转义的引号/换行)。"
                       "请只重新输出**一条合法 JSON**工具调用:字符串里的引号一律换成中文引号「」、不要带换行,别加别的字。")
                continue
            if tool and tool.get("tool") in TOOLS:
                name = tool["tool"]
                if name not in _READONLY_TOOLS:
                    _tools_ran = True   # **写过**工具才挡回退(重跑会重复写);纯读轮(read_page等)后端没响应照样回退 Claude 保答
                targs = tool.get("args") if isinstance(tool.get("args"), dict) else {}
                yield {"event": "tool", "data": _tool_label(name, targs)}
                yield _tool2(name, _tool_label(name, targs), targs, "running")
                _t_tool0 = time.time()
                try:
                    res = _run_tool(name, targs, ctx)
                except Exception as e:
                    res = {"error": str(e)[:160]}
                _tool_sec = round(time.time() - _t_tool0, 1)
                vision = res.pop("_vision", None) if isinstance(res, dict) else None
                _gm = res.pop("_gen_model", None) if isinstance(res, dict) else None
                _ga = res.pop("_gen_action", None) if isinstance(res, dict) else None
                trace.append({"label": _tool_label(name, targs), "model": _gm or "—", "sec": _tool_sec, "action": _ga,
                              "detail": _step_detail(res)})
                _subs = (res.pop("_sub_steps", None) or []) if isinstance(res, dict) else []
                for _ss in _subs:
                    trace.append({"label": _ss.get("label", ""), "model": _ss.get("model", "—"), "sec": _ss.get("sec"),
                                  "action": _ss.get("action"), "detail": _ss.get("detail", "")})
                try:
                    _used_tools.add(name)
                except NameError:
                    _used_tools = {name}   # 本轮已调工具集(写操作幻觉硬防线用)
                _creation_register(str(ctx.get("_uid") or ""), name, targs, res, ctx)   # 创造物库:非操作型结果入库(summary-plus-handle)
                _attn_tool_event(str(ctx.get("_uid") or ""), name, targs, res, ctx)     # 注意力画像:查找类工具的查询词 → 账本(tool 渠道无天然源)
                _ae_card = isinstance(res, dict) and ((res.get("client_action") or {}).get("fn") == "_assistEdit")   # 同上:高亮/便签卡接管撤销,不再发 undo 行
                if isinstance(res, dict) and res.get("client_action"):
                    yield {"event": "actions", "data": [res.pop("client_action")]}
                if isinstance(res, dict) and res.get("task_id") and name not in _AGENT_TASKS:   # CLI 委托任务(do_task/make_paper)走卡内 _trackCliTask,不发卡外浮动 task 事件(三后端一致)
                    yield {"event": "task", "data": {"task_id": res["task_id"], "label": _tool_label(name, targs)}}
                if isinstance(res, dict) and res.get("undo_id") and not _ae_card:
                    yield {"event": "undo", "data": {"undo_id": res["undo_id"], "label": _tool_label(name, targs), "page": res.pop("_jump_page", None) or (ctx.get("pages") or [ctx.get("page")] or [None])[0]}}
                yield {"event": "tool-done", "data": _tool_label(name, targs)}
                yield _tool2(name, _tool_label(name, targs), targs, "done", res, _tool_sec, locals().get("_subs"), _gm, _ga)
                if vision:   # see_page 等出图:turn 输入的 localImage 在多轮语境未验证 → 稳妥先经视觉模型转文字喂回
                    try:
                        _vd = _vision_for(ctx, vision, note="(工具产出的页面/图像渲染,请完整转述内容供编排模型使用)")
                    except Exception as _e:
                        _vd = f"(看图失败:{str(_e)[:80]})"
                    if isinstance(res, dict):
                        res["图像内容(视觉模型转述)"] = (_vd or "")[:2200]
                nxt = "【工具结果】" + json.dumps(res, ensure_ascii=False)[:6000] + "\n\n继续(调工具只输出 JSON,能答就直接答):"
                continue
            _fix = None if locals().get("_claim_retried") else _claim_fix_msg(raw, locals().get("_used_tools"))
            if _fix:   # 写操作幻觉 → 拦下,打回重试一次(仅一次,防循环)
                _claim_retried = True
                nxt = _fix
                continue
            trace[0]["detail"] = (raw or "")[:6000]
            yield {"event": "answer", "data": raw}
            break
        else:
            yield {"event": "answer", "data": "(步骤已经非常多了,先到这——已完成的部分都已保存,要继续就再说一句)"}
        if client_actions:
            yield {"event": "actions", "data": client_actions}
        _tool_total = sum(t.get("sec", 0) or 0 for t in trace[1:])
        trace[0]["sec"] = round(max(0.0, (time.time() - _t_start) - _tool_total), 1)
        _tt = _tok_get()
        if _tt:
            trace[0]["tok"] = _tt
        yield {"event": "trace", "data": trace}
    except Exception as e:
        yield {"event": "error", "data": f"Codex 编排出错:{str(e)[:120]}"}
    finally:
        try:
            _codex_app.thread_close(tid)
        except Exception:
            pass


# ── 助手生成任务:detached(后台线程跑到完,客户端断了也不杀、跑完照样落库)+ 按 rid 缓冲事件供重连续读 ──
# 根治「切后台→连接断→只能叫你刷新」:生成不再绑请求生命周期,客户端拿同一个 rid 接着读即可,全程零操作。
_chat_jobs = {}
_chat_jobs_lock = threading.Lock()


def _chat_worker(rid, message, ctx, history, force_effort, force_model, uid, force_backend=None):
    job = _chat_jobs[rid]
    try:
        for ev in _agent_run(message, ctx, history, force_effort=force_effort, force_model=force_model, force_backend=force_backend):
            with job["lock"]:
                job["events"].append(ev)
                if ev["event"] == "answer":
                    job["answer"] = ev["data"]
                elif ev["event"] == "error":
                    job["error"] = str(ev["data"])[:300]   # 错误也落库(否则前端断连+刷新后什么都看不到="迟迟没有回答")
                elif ev["event"] == "trace":
                    job["trace"] = ev["data"]
                elif ev["event"] == "actions":   # client_actions:提取 renderVideos 的视频 → 随回合落库,刷新不丢(镜像 EPUB 阶段C)
                    for a in (ev["data"] or []):
                        if isinstance(a, dict) and a.get("fn") == "renderVideos":
                            vs = (a.get("args") or [None])[0]
                            if isinstance(vs, list) and vs:
                                job.setdefault("videos", []).extend(vs)
                elif ev["event"] == "undo":   # H2:高亮撤销卡 → 随回合落库,刷新回放(undo_id 存 state/assistant-undo-log.json 持久,80 条内有效)
                    d = ev["data"] or {}
                    if d.get("undo_id"):
                        job.setdefault("undo_cards", []).append({"undo_id": d["undo_id"], "label": d.get("label"), "page": d.get("page")})
    except Exception as e:
        with job["lock"]:
            job["events"].append({"event": "error", "data": str(e)[:160]})
    finally:
        with job["lock"]:
            job["events"].append({"event": "done", "data": {}})
            job["done"] = True
        if not job.get("answer") and job.get("error"):   # 失败轮:落一条错误说明(断连+刷新后用户能看到失败原因,而不是永远空等)
            job["answer"] = "⚠️ " + job["error"]
        if job.get("answer"):   # 不管客户端在不在,跑完就落库(断连也不丢;历史/感叹号/视频卡都用得上)
            _meta = {}
            if job.get("trace"):
                _meta["trace"] = job["trace"]
            if job.get("videos"):
                _meta["videos"] = job["videos"]
            if job.get("undo_cards"):
                _meta["undo_cards"] = job["undo_cards"]   # H2:高亮撤销卡持久化
            if job.get("turn_id"):
                _meta["turn_id"] = job["turn_id"]   # 文字工具轮:_syncParts 按 turn_id upsert parts → 刷新回放仍是完整卡
            # 落库前剥 [语气:XX](朗读控制符):历史干净 → 关掉朗读后模型不会照着自己旧回答模仿输出标签
            _ans = re.sub(r"[\[【]语气[::][^\]】]{0,12}[\]】]", "", str(job["answer"]))
            _convo_append(uid, "assistant", _ans[:1500], _meta or None)
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
            force_backend = body.get("force_backend") if body.get("force_backend") in ("claude", "gemini", "codex") else None
            # 61b:带 force_backend 时型号白名单按该后端放行(gemini/codex 的型号不在 Claude 梯子里)
            force_model = (body.get("force_model") or None) if force_backend in ("gemini", "codex") \
                else (body.get("force_model") if body.get("force_model") in _AP_MODELS else None)

            ctx = body.get("context") or {}
            ctx["media_prefer"] = body.get("media_prefer")   # 偏好独立字段(不进 message)
            _v = body.get("voice")   # 1=前端朗读点亮(2.0 引擎,要口语化+语气标签) / "s2s"=relay 深度思考代播(bidi,只要口语化) / 0=文字模式(纯净 prompt)
            ctx["voice_mode"] = "s2s" if _v == "s2s" else (1 if _v else 0)
            ctx["_base"] = request.host_url.rstrip("/")
            ctx["_uid"] = uid   # 写操作记 owner=本用户 → 撤销只能撤自己的
            history = [{k: m.get(k) for k in ("role", "content", "page", "pages", "book", "file_rel", "selection")}
                       for m in _convo_load(uid)[-6:]]
            _convo_append(uid, "user", message, {   # 用户消息进 agent 前就落库 → 断连也不丢这轮 + 保住"刚才那页"链
                "page": ctx.get("page"), "pages": ctx.get("pages"),
                "book": ctx.get("book_name"), "file_rel": ctx.get("file_rel"),
                "selection": ctx.get("selection"),
                "figures": [{k: f.get(k) for k in ("page", "box", "caption", "group", "has_ink", "file_rel", "kind", "note_id")}
                            for f in (ctx.get("figures") or [])][:6],
            })
            job = _chat_jobs[rid] = {"events": [], "answer": "", "trace": None, "done": False,
                                     "lock": threading.Lock(), "uid": uid,
                                     "turn_id": str(body.get("turn_id") or "")[:24]}   # 轮次容器 id:落库带上 → _syncParts 的 upsert 才能命中(文字工具轮 parts 持久化)
            threading.Thread(target=_chat_worker, daemon=True,
                             args=(rid, message, ctx, history, force_effort, force_model, uid, force_backend)).start()
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


# ── ㊲ 对话历史压缩(官方 Realtime 指南 8.4 形态:滚动摘要替代无限历史,低频批量优于频繁删除)──
# 挂断时后台压缩(空闲期做功),新开语音回放"摘要+近几轮原文"——压缩发生在会话之间,不碰任何在用缓存前缀。
# 摘要 sidecar 与对话历史同命运:🗑 清空时一并删除。
def _summary_path(uid, file_rel: str = ""):
    if file_rel and file_rel.lower().endswith(".epub"):
        import epub_assistant as _ea
        return _ea._ECONVO_DIR / str(uid) / (_ea._file_key(file_rel) + ".summary.json")
    return _CONVO_DIR / f"{uid}.summary.json"


def _summary_load(uid, file_rel: str = "") -> dict:
    try:
        return json.loads(_summary_path(uid, file_rel).read_text("utf-8"))
    except Exception:
        return {}


def _load_msgs_for(uid, file_rel: str = "") -> list:
    if file_rel and file_rel.lower().endswith(".epub"):
        import epub_assistant as _ea
        return _ea._econvo_load(uid, file_rel)
    return _convo_load(uid)


def _compact_view(uid, file_rel: str = "") -> dict:
    """历史的压缩视图:{summary, messages=摘要覆盖点之后的原文}。语音重连回放用它,替代全量原文。"""
    sm = _summary_load(uid, file_rel)
    upto = sm.get("upto_ts") or 0
    msgs = [m for m in _load_msgs_for(uid, file_rel) if (m.get("ts") or 0) > upto]
    return {"summary": (sm.get("summary") or ""), "messages": msgs[-20:]}


@bp.route("/compact-history", methods=["POST"])
def assistant_compact_history():
    """挂断触发(前端 fire-and-forget):未压缩轮次够多才压(幂等防抖),便宜文字模型滚动合并旧摘要。"""
    if not _logged_in():
        return jsonify({"ok": False}), 401
    b = request.get_json(silent=True) or {}
    uid = session["user_id"]
    file_rel = (b.get("file") or "").strip()
    force = bool(b.get("force"))   # ㊳ 会话内压缩:阈值降到 8 轮(通话中触发时轮次可能不多但音频 token 已重)
    sm = _summary_load(uid, file_rel)
    upto = sm.get("upto_ts") or 0
    fresh = [m for m in _load_msgs_for(uid, file_rel)
             if (m.get("ts") or 0) > upto and m.get("role") in ("user", "assistant") and (m.get("content") or "").strip()]
    KEEP = 6   # 最近几轮保留原文(摘要丢局部语义,官方 8.4 同款建议)
    if len(fresh) < (8 if force else 14):
        return jsonify({"ok": True, "skipped": "对话不够长,暂不压缩"})
    cut = len(fresh) - KEEP
    while cut > 1 and (fresh[cut - 1].get("ts") or 0) == (fresh[cut].get("ts") or 0):
        cut -= 1   # 打包边界别拆开同一秒的轮次(upto_ts 按秒,拆开会把保留段误滤掉)
    pack = fresh[:cut]
    if len(pack) < 8:
        return jsonify({"ok": True, "skipped": "可打包轮次太少"})
    lines = [("用户: " if m["role"] == "user" else "AI: ") + str(m["content"]).replace("\n", " ")[:400] for m in pack]
    old = (sm.get("summary") or "").strip()
    prompt = ("你在为一个语音学习助手压缩对话记忆。把下面的对话(连同已有摘要)合并成**一段 300 字以内**的摘要,"
              "只保留:用户的偏好与习惯、已确认的事实和结论、学习进度(在读什么书/学到哪)、还没办完的事项。"
              "丢掉寒暄、过程性内容和重复。直接输出摘要正文,不加任何前后缀。\n"
              + (f"[已有摘要]\n{old}\n" if old else "") + "[新增对话]\n" + "\n".join(lines))
    out = (_gemini_text(prompt, max_tokens=600, think=False, timeout=45) or "").strip()
    if not out:
        return jsonify({"ok": False, "error": "压缩模型没返回"}), 502
    # 竞态守卫:压缩期间用户可能点了清空——重载校验,历史已空就放弃写入(别把清掉的记忆复活)
    if not _load_msgs_for(uid, file_rel):
        return jsonify({"ok": True, "skipped": "历史已被清空,放弃写入"})
    p = _summary_path(uid, file_rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"summary": out[:2000], "upto_ts": pack[-1].get("ts") or int(time.time()),
                             "ts": int(time.time())}, ensure_ascii=False), "utf-8")
    print(f"[compact] uid={uid} file={file_rel or 'global'} packed={len(pack)} → {len(out)}字", flush=True)
    return jsonify({"ok": True, "packed": len(pack), "summary_chars": len(out), "summary": out[:2000]})


@bp.route("/history")
def assistant_history():
    if not _logged_in():
        return jsonify({"ok": False}), 401
    if request.args.get("compact"):   # ㊲:语音回放用压缩视图(摘要+近几轮原文),侧栏显示仍走全量
        v = _compact_view(session["user_id"])
        return jsonify({"ok": True, "summary": v["summary"], "messages": v["messages"]})
    return jsonify({"ok": True, "messages": _convo_load(session["user_id"])[-100:]})


@bp.route("/voice-page-text", methods=["GET"])
def assistant_voice_page_text():
    """WebRTC 语音路的页文本兜底(用户实锤:扫描书/图片模式前端 pendText 采不到可见文本 →
    通话 AI 不知道页面内容)。服务端 _page_text(OCR 文字层 + 钉入便签/卡片注入都在)截 1500,
    前端缓存进 _rtc._ptCache,用户开口时随 _rtcFlushCtx 注入。"""
    if not _logged_in():
        return jsonify({"ok": False}), 401
    f = (request.args.get("file") or "").strip()
    try:
        pg = int(request.args.get("page") or 0)
    except Exception:
        pg = 0
    if not f or not pg:
        return jsonify({"ok": True, "text": ""})
    try:
        t = (_page_text(f, pg) or "")[:1500]
    except Exception:
        t = ""
    return jsonify({"ok": True, "text": t})


@bp.route("/voice-ctx", methods=["GET", "POST"])
def assistant_voice_ctx():
    """语音伴读(S2S)的**直塞上下文**:圈画文字 + 本页插图离线描述——纯文本、现成可得的内容
    直接进 S2S 的 system prompt,不走「我去查」agent 中间层(用户定调:能直接塞的就直接塞)。
    POST 可带 strokes(前端内存实时墨迹,通话中新圈的,不等 sidecar 防抖保存——镜像侧栏 ctx["ink"] 机制)。"""
    if not _logged_in():
        return jsonify({"ok": False}), 401
    body = request.get_json(silent=True) or {}
    file_rel = request.args.get("file") or body.get("file") or ""
    try:
        page = int(request.args.get("page") or body.get("page") or 0)
    except Exception:
        page = 0
    strokes = body.get("strokes") or None
    inked, figs, has_ink, vocab = "", [], False, []
    file_rel, page = _vb_src(file_rel, page)   # 合并书:语音直塞上下文也走真成员(第三通道)
    if file_rel.lower().endswith(".epub"):   # ㉟:圈画/生词提取全按 PDF 页渲染,epub 无意义→干净空结构(页文本另有 page-text 分流)
        return jsonify({"ok": True, "inked_text": "", "figures": [], "has_ink": False, "vocab": []})
    if file_rel and page:
        try:
            import pdf_reader as _pdfm
            # 有笔迹但提取不到字(纯涂画/箭头)也要让壳子知道;实时 strokes 优先于服务端 sidecar
            has_ink = bool(strokes) or bool(_pdfm._page_ink_strokes(file_rel, page))
            if has_ink:
                inked = _clean_tag(_pdfm._text_under_ink(file_rel, page, strokes=strokes) or "")[:500]
        except Exception:
            inked = ""
        try:   # 本页『还没掌握』的生词(与阅读器 F7 下划线同一套 mastery 判定;侧栏 visible_vocab 的服务端等价物)
            import pdf_reader as _pdfm
            abs_path = _pdfm._safe_vault_path(file_rel)
            res = _pdfm._page_chars_cached(abs_path, file_rel, page) if abs_path else None
            if res:
                chars = res[0]
                _pdfm._apply_char_offset(chars, _pdfm._char_offset_for(file_rel, page))
                marks = _pdfm._build_vocab_marks(chars)
                if _pdfm._page_allows_ja(chars, file_rel):
                    marks += _pdfm._build_jp_vocab_marks(chars)
                seen = set()
                for m in marks:
                    w = (m.get("lemma") or m.get("word") or "").strip()
                    if w and w.lower() not in seen:
                        seen.add(w.lower())
                        vocab.append(w)
                vocab = vocab[:30]
        except Exception:
            vocab = []
        try:
            figd = _figdescs_for(file_rel, [_to_disp({"file_rel": file_rel}, page)])
            for lst in figd.values():
                for cap, desc in lst:
                    figs.append({"caption": (cap or "")[:80], "desc": (desc or "")[:400]})
        except Exception:
            pass
    return jsonify({"ok": True, "inked": inked, "has_ink": has_ink, "figures": figs[:4], "vocab": vocab})


@bp.route("/tools")
def assistant_tools_dir():
    """外部编排 agent(MCP)桥①:内置工具层的目录(name+描述)。外部 AI 先看这个发现能力。"""
    if not _logged_in():
        return jsonify({"ok": False}), 401
    return jsonify({"ok": True, "tools": [{"name": n, "desc": d} for n, (d, _) in TOOLS.items()]})


@bp.route("/tool", methods=["POST"])
def assistant_tool_call():
    """外部编排 agent(MCP)桥②:直接 dispatch 内置助手的工具层——外部 AI 临时取代最外层编排 agent,
    共享同一副"身体"(30+ 工具:read_page/see_page/highlight/make_anki/notes/search_book…)。
    body: {name, args?, ctx?:{file_rel, page, selection, ...}}。ctx 字段与侧栏助手同口径。"""
    if not _logged_in():
        return jsonify({"ok": False}), 401
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if name not in TOOLS:
        return jsonify({"ok": False, "error": f"unknown tool: {name}", "hint": "GET /api/assistant/tools 看目录"}), 400
    ctx = dict(body.get("ctx") or {})
    ctx["_uid"] = session["user_id"]
    try:
        res = _run_tool(name, body.get("args") or {}, ctx)
    except Exception as ex:
        res = {"error": f"{type(ex).__name__}: {str(ex)[:300]}"}
    try:   # MCP 遥控(2026-07-13):外部 agent 没有浏览器在等 client_action——前端动作经阅读器
        # SSE 总线广播,打开着且可见的阅读器页面真执行("让他翻页但页面没变"的根治;file 空=广播全部)
        ca = res.get("client_action") if isinstance(res, dict) else None
        if ca and ca.get("fn"):
            import reader_events
            reader_events.publish("client-action", ctx.get("file_rel") or "", ctx["_uid"],
                                  extra={"action": ca})
    except Exception:
        pass
    return jsonify({"ok": "error" not in res, "tool": name, "result": res})


# 只读(无副作用)工具:语音壳可按「工具+参数+页面状态指纹」缓存结果,重复询问直接复用不再执行
# (写操作/页面动作绝不缓存——"再做一张卡"是合法语义)。挨着 TOOLS 放:加新工具时顺手归类。
VOICE_CACHEABLE_TOOLS = {
    "read_page", "read_selection", "search_book", "search_all_books", "recall_notes",
    "summarize_section", "translate", "see_page", "see_figure", "see_ink",
    "notes_query", "notes_read", "read_highlights", "find_highlights", "toc",
    "page_vocab", "lookup_word", "search_video", "search_image", "web_search",
}


# ── 143(用户设计):**调用前垫话策略**(A/B 混合)────────────────────────────────
#   A = 调工具前先说一句「我去查一下」;B = 静默直接调。实测(scratchpad/rt_probe.py)两者**都只是 1 次 response**
#   ——垫话和 function_call 挤在同一个 response 里,差别只有那句话的 output token(≈$0.0008/次)。
#   所以这不是成本问题,是**体验**问题:秒回的工具垫话反而啰嗦,慢工具不垫话就是干等。
#   → 每个工具一条策略:auto(默认)/ always / never。**auto 用账本里这个工具的真实中位耗时判定**,
#     不拍脑袋:median >= 阈值(默认 2.5s)→ 垫话,否则静默。策略经工具 description 注入模型(唯一入口)。
_FILLER_DEF = {"mode": "auto", "threshold_s": 2.5}
_fill_cache = {"t": 0.0, "med": {}}


def _tool_median_s(tool: str) -> float:
    """这个工具在账本里的**中位**耗时(取中位不取均值:一次 164.9s 的挂起会把均值毁掉)。60s 缓存。"""
    import statistics
    import sqlite3 as _sq
    now = time.time()
    if now - _fill_cache["t"] > 60:
        med = {}
        try:
            c = _sq.connect(str(CLAUDE_DIR / "state" / "voice-ledger.db"), timeout=3)
            rows = c.execute("SELECT tool, took_s FROM tool_calls WHERE took_s IS NOT NULL").fetchall()
            c.close()
            byt = {}
            for t, sec in rows:
                byt.setdefault(t, []).append(float(sec or 0))
            for t, xs in byt.items():
                med[t] = round(statistics.median(xs[-50:]), 2)
        except Exception:
            pass
        _fill_cache["t"] = now
        _fill_cache["med"] = med
    return _fill_cache["med"].get(tool, -1.0)   # -1 = 账本里没这工具的数据


def _filler_global(uid: str) -> dict:
    g = ((_tp_load().get(str(uid)) or {}).get("_global") or {}).get("filler") or {}
    return {"mode": g.get("mode") or _FILLER_DEF["mode"],
            "threshold_s": float(g.get("threshold_s") or _FILLER_DEF["threshold_s"])}


def _filler_mode(uid: str, tool: str) -> str:
    """最终策略:per-tool 没设 → 跟全局;全局 auto → 按中位耗时判。恒返回 always/never。"""
    g = _filler_global(uid)
    m = (((_tp_load().get(str(uid)) or {}).get(tool) or {}).get("filler") or "").strip()
    if m not in ("always", "never", "auto"):
        m = g["mode"]
    if m == "auto":
        med = _tool_median_s(tool)
        if med < 0:                       # 没跑过 → 保守垫话(宁可多说一句,也别让人干等)
            return "always"
        return "always" if med >= g["threshold_s"] else "never"
    return m if m in ("always", "never") else "always"


_FILLER_TXT = {
    "always": "【调用前】先用一句很短的话告诉用户你要去做什么(如「我去查一下」),然后立刻调用本工具(说话和调用在同一轮内完成,不要分两次)。",
    "never": "【调用前】不要说任何话,直接调用本工具。",
}


def _tool_desc_rtc(uid: str, tool: str, base: str, cap: int) -> str:
    """实时语音会话里这个工具的 description = 用户可改的说明(截断) + 垫话策略注入语。"""
    d = _tp(uid, tool, "desc", base)[:cap]
    return (d + " " + _FILLER_TXT[_filler_mode(uid, tool)])[: cap + 90]


# 148(用户设计):工具卡长按面板里也能改**这个工具用哪个模型**。工具 → AI 预设 action 的映射
#   (只列**真的会调 LLM** 的工具;没列的工具面板不显示模型段)。跟总设置面板共用同一套 action-prefs 存储,
#   两边改的是同一份数据 —— 在哪改都一样,不会打架。
_TOOL_ACTION = {
    "do_task":      "agent",        # 后台 agent worker(无头 CLI + MCP)
    "make_paper":   "paper",        # 造纸 = 设计插入内容,独立更深预设(长按第一行工具条可调模型/深度)
    "web_search":   "web_search",
    "search_image": "img_norm",     # 配图关键词规范化
    "search_video": "pick_video",
    "see_page":     "vision",
    "see_figure":   "vision",
    "see_ink":      "vision",
    "translate":    "translate",
    "summarize_section": "summarize",
    "make_note":    "summarize",
    "make_anki":    "summarize",
    "lookup_word":  "dict",
    "dictation_grade": "dictation_grade",   # #1:判分 AI(纸上「让 AI 检查」按钮)也能在工具设置里换模型
}


def _tool_model_block(uid: str, tool: str):
    """给工具卡面板的「模型」段:该工具对应 action 的当前预设 + 出厂默认 + 可选目录。无对应 action → None。"""
    action = _TOOL_ACTION.get(tool)
    if not action:
        return None
    return {"action": action,
            "pref": _ap_get(uid, action) or {},
            "default": _AP_DEFAULTS[action],
            "backends": ["claude", "codex", "gemini"],
            "variants": {"claude": list(_CLAUDE_VARIANTS),
                         "codex": list(_CODEX_VARIANTS),
                         "gemini": list(_GEMINI_VARIANTS)},
            "depths": {"claude": list(_EFFORTS), "codex": list(_EFFORTS), "gemini": ["none", "think"]}}


@bp.route("/tool-prompt", methods=["GET", "POST"])
def assistant_tool_prompt():
    """140(用户设计):工具的**说明**和**内部 prompt** —— 凡是会进 AI 并实际产生影响的,都能在这里改。

    GET  ?tool=X → 每个字段给三份:cur(现在生效的) / sys(系统原始默认) / mine(你自己设的默认,可能没有)
    POST {tool, fields:{key:text}, op}
      op=save       写成**生效**文本(空串=清掉覆盖,回到"你的默认"或系统默认)
      op=setdefault 把当前文本**记为你的默认**(以后按「默认」就填回它)
      op=factory    清掉这个工具的所有覆盖 + 你的默认(彻底回系统原始)
    """
    if not _logged_in():
        return jsonify({"ok": False}), 401
    uid = str(session["user_id"])

    def _fields(tool):
        out = []
        d0 = TOOLS.get(tool)
        if d0:
            out.append(("desc", "工具说明(决定 AI 何时/如何调它 —— 进工具目录)", str(d0[0])))
        for k, (lb, dv) in (TOOL_SLOTS.get(tool) or {}).items():
            out.append((k, lb, dv))
        return out

    if request.method == "GET":
        tool = (request.args.get("tool") or "").strip()
        if tool == "_global":   # 143:总设置页读全局垫话策略(不是真工具,没有 fields)
            return jsonify({"ok": True, "tool": "_global", "fields": [], "filler": {"global": _filler_global(uid)},
                            "medians": _fill_cache["med"] if (_tool_median_s("") or True) else {}})
        fs = _fields(tool)
        if not fs:
            return jsonify({"ok": False, "error": f"没有可改的提示词:{tool}"}), 404
        store = _tp_load()
        cur = ((store.get(uid) or {}).get(tool) or {})
        mine = cur.get("_defaults") or {}
        g = _filler_global(uid)   # 143:垫话策略(per-tool + 全局 + 实测中位耗时,前端要显示"自动会怎么判")
        return jsonify({"ok": True, "tool": tool, "fields": [
            {"key": k, "label": lb, "sys": dv,
             "cur": cur.get(k) if isinstance(cur.get(k), str) else "",
             "mine": mine.get(k) if isinstance(mine.get(k), str) else ""}
            for k, lb, dv in fs],
            "filler": {"mode": cur.get("filler") or "",           # '' = 跟全局
                       "resolved": _filler_mode(uid, tool),       # 最终生效:always / never
                       "median_s": _tool_median_s(tool),          # -1 = 账本里没数据
                       "global": g},
            "model": _tool_model_block(uid, tool),                # 148:该工具用哪个模型(跟总设置同一份数据)
            "creation": {"mode": (cur.get("creation") if cur.get("creation") in ("on", "off") else ""),
                         "default": tool in _CREATION_DEFAULT_ON}})   # 记忆开关:结果是否记入创造物库(用户设计)

    body = request.get_json(silent=True) or {}
    tool = (body.get("tool") or "").strip()
    op0 = (body.get("op") or "").strip()

    if op0 == "model":   # 148:工具卡面板里改这个工具用的模型 —— 写的是**总设置同一份** action-prefs
        action = _TOOL_ACTION.get(tool)
        if not action:
            return jsonify({"ok": False, "error": f"这个工具不调 AI 模型:{tool}"}), 400
        bk = (body.get("backend") or "").strip()
        va = (body.get("variant") or "").strip()
        dp = (body.get("depth") or "").strip()
        saved = _ap_set(uid, action, bk, va, dp)   # 三者非法 → 清除,回出厂默认
        return jsonify({"ok": True, "action": action, "pref": saved or {},
                        "default": _AP_DEFAULTS[action]})

    if op0 == "filler_global":   # 143:总设置——默认策略 + 自动的耗时阈值
        st = _tp_load()
        u = st.setdefault(uid, {})
        gg = u.setdefault("_global", {}).setdefault("filler", {})
        m = (body.get("mode") or "").strip()
        if m in ("auto", "always", "never"):
            gg["mode"] = m
        try:
            th = float(body.get("threshold_s"))
            if 0.2 <= th <= 30:
                gg["threshold_s"] = round(th, 1)
        except (TypeError, ValueError):
            pass
        _tp_save(st)
        return jsonify({"ok": True, "global": _filler_global(uid)})

    if op0 == "creation":   # 记忆开关:'' = 默认(白名单记/其它不记),on/off = 强制
        m = (body.get("mode") or "").strip()
        if m not in ("", "on", "off"):
            return jsonify({"ok": False, "error": "mode 只能是 on/off 或空(默认)"}), 400
        if not _fields(tool):
            return jsonify({"ok": False, "error": "未知工具"}), 400
        st = _tp_load()
        t = st.setdefault(uid, {}).setdefault(tool, {})
        if m:
            t["creation"] = m
        else:
            t.pop("creation", None)
        _tp_save(st)
        return jsonify({"ok": True, "mode": m, "resolved": _creation_enabled(uid, tool)})

    if op0 == "filler":   # 143:单个工具——'' = 跟全局
        m = (body.get("mode") or "").strip()
        if m not in ("", "auto", "always", "never"):
            return jsonify({"ok": False, "error": "策略只能是 auto/always/never 或空(跟全局)"}), 400
        if not _fields(tool):
            return jsonify({"ok": False, "error": "未知工具"}), 400
        st = _tp_load()
        t = st.setdefault(uid, {}).setdefault(tool, {})
        if m:
            t["filler"] = m
        else:
            t.pop("filler", None)
        _tp_save(st)
        return jsonify({"ok": True, "mode": m, "resolved": _filler_mode(uid, tool), "median_s": _tool_median_s(tool)})

    if not _fields(tool):
        return jsonify({"ok": False, "error": "未知工具"}), 400
    op = body.get("op") or "save"
    store = _tp_load()
    u = store.setdefault(uid, {})
    t = u.setdefault(tool, {})
    if op == "factory":
        u.pop(tool, None)
    else:
        fields = body.get("fields") or {}
        if op == "setdefault":
            dd = t.setdefault("_defaults", {})
            for k, v in fields.items():
                if isinstance(v, str) and v.strip():
                    dd[k] = v
                else:
                    dd.pop(k, None)
        else:   # save:写成生效文本(空 = 清覆盖)
            for k, v in fields.items():
                if isinstance(v, str) and v.strip():
                    t[k] = v
                else:
                    t.pop(k, None)
        if not t:
            u.pop(tool, None)
    _tp_save(store)
    return jsonify({"ok": True})


@bp.route("/voice-tool", methods=["POST"])
def assistant_voice_tool():
    """语音套壳(S2S 编排化)桥③:S2S 说出的原始命令文本 → **与编排 agent 同一套** JSON 工具协议:
    `_parse_tool` 顽强解析(围栏/前导散文/控制字符/非法转义容错)→ 正则级修补再试 → dispatch TOOLS。
    解析失败返回 feedback(编排 agent 同款自愈措辞),relay 经 ChatRAGText 喂回让 S2S 重出一条合法 JSON。
    这样工具协议/解析器/工具层三者物理同源,编排 agent 升级语音壳自动跟进(用户定调)。
    body: {cmd, ctx?:{file_rel,page,...}}"""
    if not _logged_in():
        return jsonify({"ok": False}), 401
    body = request.get_json(silent=True) or {}
    raw = (body.get("cmd") or "").strip()
    tool = _parse_tool(raw)
    if tool is None and '"tool"' in raw:
        # 正则级修补(语音模型常见坏点:中文引号当 JSON 引号、尾逗号)后再试一次
        fixed = re.sub(r",\s*([}\]])", r"\1", raw.replace("“", '"').replace("”", '"'))
        tool = _parse_tool(fixed)
    if not tool or tool.get("tool") not in TOOLS:
        return jsonify({"ok": False, "error": "unparseable",
                        "feedback": ("你上一条像是工具调用,但 JSON 没解析成功(很可能字符串里有**没转义的双引号**或**换行**)。"
                                     "请重新只输出**一条合法的 JSON**工具调用:字符串里的引号一律换成中文引号「」、"
                                     "不要带换行、整条只输出 JSON 别加别的字。"
                                     if '"tool"' in raw else
                                     f"「{(tool or {}).get('tool', '?')}」不是有效工具" if tool else "没识别出工具调用")})
    ctx = dict(body.get("ctx") or {})
    ctx["_uid"] = session["user_id"]
    name = tool["tool"]
    targs = tool.get("args") if isinstance(tool.get("args"), dict) else {}
    t0 = time.time()
    try:
        res = _run_tool(name, targs, ctx)
    except Exception as ex:
        res = {"error": f"{type(ex).__name__}: {str(ex)[:300]}"}
    if isinstance(res, dict) and res.get("_sub_steps"):   # 137:工具内部子步骤 → 透出给前端当**外层卡的步骤**(不另起卡)
        res["sub_steps"] = [{"label": x.get("label", ""), "detail": (x.get("detail") or "")[:600], "sec": x.get("sec")}
                            for x in (res.pop("_sub_steps") or [])][:12]
    if isinstance(res, dict) and res.get("error"):   # 语音工具报错上服务器日志(排障:journalctl -u webapp | grep voice-tool)
        print(f"[voice-tool] {name} args={json.dumps(targs, ensure_ascii=False)[:200]} err={str(res['error'])[:200]}", flush=True)
    else:   # ㊸ 成功调用也记一行(工具名+耗时)——"到底调没调"从此一句 grep 实锤,不再靠推理
        print(f"[voice-tool] {name} ok {round(time.time() - t0, 1)}s", flush=True)
    _creation_register(str(ctx.get("_uid") or ""), name, targs, res, ctx)   # 创造物库:语音链路走本端点不经编排循环——漏了它=语音轮产出全不入册(用户实锤"查完天气就忘")
    _attn_tool_event(str(ctx.get("_uid") or ""), name, targs, res, ctx)     # 注意力画像:查找类工具的查询词 → 账本(tool 渠道无天然源)
    # ㉜ 语音场景配图渲染:search_image 结果在语音链路(仅本端点)附 client_action → 前端图卡进侧栏对话流。
    #    文字助手不走此端点(它由模型在 markdown 回答里嵌图),互不干扰。
    if name == "search_image" and isinstance(res, dict) and res.get("images"):
        res["client_action"] = {"fn": "renderImages", "args": [res["images"]]}
        # 89(用户设计):配图与搜索同构静默入库——卡片已显示,回填只带概况;是否放行口头回报由 rt_tool_reply 开关定(relay/前端 gate)
        res["silent"] = True
    if isinstance(res, dict) and res.get("ok") and name == "search_video" and res.get("videos"):
        # 98(用户设计):视频与配图/搜索统一静默——卡片已显示,2.1 本轮不发言("给你找到6个视频…请自行点击"式废话轮=没静默的产物)
        res["silent"] = True
        res["_note"] = ("视频已用卡片显示在用户屏幕上(" + "、".join([v0.get("title", "")[:24] for v0 in res["videos"][:4]]) +
                        ")。本轮到此结束;用户下次说话时若相关直接参考,不要主动复述标题。")
    # 102(用户设计):**统一静默表**——动作/展示型工具(结果用户已在界面上看到:翻页/高亮/加生词/开书)成功即静默,
    # 不再逐工具散落打标;与上面搜索/配图/视频三个专段(带个性 note)同一机制(silent 标),relay/前端按 rt_tool_reply 统一 gate。
    # 信息型工具(read_page/查词/看图/回顾…结果=回答原料)绝不进此表——静默它们=问了白问。任务型(make_*)保留简短确认。
    _SILENT_ACT = {"goto_page", "highlight", "auto_highlight", "add_vocab", "open_book"}
    if isinstance(res, dict) and not res.get("error") and name in _SILENT_ACT and not res.get("silent"):
        res["silent"] = True
        _n0 = res.get("note") or res.get("_note") or "操作已完成"
        res["_note"] = str(_n0)[:200] + "(已在界面上生效,用户看得见)。本轮到此结束,不要发言;用户下次说话时正常继续。"
        res["_note"] = ("图片已用卡片显示在用户屏幕上(含:" +
                        "、".join([i0.get("concept") or "图" for i0 in res.get("images", [])][:6]) +
                        ")。本轮到此结束;用户下次说话时若相关直接参考,不要主动描述图片内容。")
    # WebRTC 通话(带 rtc_call_id)的图像走 sideband 服务端注入,绝不进响应让前端挤 data channel
    if isinstance(res, dict) and res.get("_vision") and body.get("rtc_call_id"):
        if _rtc_sideband_images(str(body["rtc_call_id"]), res["_vision"]):
            res.pop("_vision", None)
            res["图像"] = "已直接发到对话里,请看图回答"
        else:
            res.pop("_vision", None)   # sideband 失败也不给前端(dc 发大图会把通道弄死),如实告知
            res["图像"] = "传输失败,这次没法看到图;请如实告诉用户图像传输出了问题"
    return jsonify({"ok": "error" not in res, "tool": name, "args": targs,
                    "label": _tool_label(name, targs), "took_s": round(time.time() - t0, 1),
                    "cacheable": name in VOICE_CACHEABLE_TOOLS,
                    "result": res})


# ── GPT Realtime WebRTC 直连(㉚,用户拍板治本):浏览器媒体直连 OpenAI——WebRTC 路径的浏览器 AEC
#    **真正生效**(外放无回声+全双工打断回归,WS 模式的半双工妥协不再需要)。密钥不下发前端:
#    SDP 经 /rtc-call 代理签给 OpenAI;session 配置由 /rtc-session 下发;工具循环在前端
#    (data channel 收 function_call → fetch /voice-tool 执行 → dc 回填),client_action 本地直执行。──
_RTC_CFG_PATH = Path("~/.config/doubao-voice.json").expanduser()
_RTC_KEY_PATH = Path("~/.config/openai-realtime.json").expanduser()
# 133:通话票据密钥(与 voice_realtime_relay.py 共享同一文件)。用途见 relay 的 _ticket_uid 注释:
#   接管旧通话必须确认"两路 call 是同一个人的",而 call_id 是 OpenAI 生成的、**不保证含应用用户身份**,
#   从中猜 uid 可能踢掉别人的通话。所以由 webapp(唯一持有 session 的一方)签发,relay 验签。
_VOICE_TICKET_KEY = Path("/home/bwicarus/claude/state/voice-ticket.key")


def _voice_ticket(uid, call_id: str) -> str:
    try:
        import hmac as _hm
        if not _VOICE_TICKET_KEY.exists():
            _VOICE_TICKET_KEY.parent.mkdir(parents=True, exist_ok=True)
            _VOICE_TICKET_KEY.write_text(os.urandom(32).hex(), "utf-8")
            try:
                _VOICE_TICKET_KEY.chmod(0o600)
            except Exception:
                pass
        sec = _VOICE_TICKET_KEY.read_text("utf-8").strip().encode()
        import hashlib as _hl2
        return _hm.new(sec, f"{uid}|{call_id}".encode(), _hl2.sha256).hexdigest()[:32]
    except Exception:
        return ""


def _rtc_cfg() -> dict:
    try:
        return json.loads(_RTC_CFG_PATH.read_text("utf-8"))
    except Exception:
        return {}


def _voice_budget_gate():
    """125(#284):预算硬闸——读 relay 的 SQLite 账本(WAL 跨进程读安全)。
    安全:默认 $5/天(缺 rt_budget_usd 时不再形同虚设);cfg 里显式写 0 才是关闭闸。"""
    try:
        cfg = json.loads(_VOICE_CFG_PATH.read_text("utf-8"))
        _rb = cfg.get("rt_budget_usd")
        b = float(_rb) if _rb is not None else 5.0
    except Exception:
        b = 5.0
    if b <= 0:
        return True, 0.0
    try:
        import sqlite3
        import time as _t2
        c = sqlite3.connect(str(CLAUDE_DIR / "state" / "voice-ledger.db"), timeout=3)
        r = c.execute("SELECT COALESCE(SUM(est_usd),0) FROM usage_events WHERE day=?",
                      (_t2.strftime("%Y-%m-%d"),)).fetchone()
        c.close()
        spent = float(r[0] or 0)
    except Exception:
        return True, 0.0
    return spent < b, spent


def _build_rtc_session(uid, file_rel, page):
    """WebRTC 会话配置**服务端自建**(instructions/tools/vad/voice,镜像 relay 的 WS 版构造;
    audio format 不带——媒体轨自动协商)。/rtc-session 和 /rtc-call 共用这一份:配置**绝不能**
    让前端塞进来——否则任一注册用户就能自带贵型号/超大 max_output_tokens 绕开预算烧 key。
    返回 (sess, compact_tokens, rt_image)。"""
    cfg = _rtc_cfg()
    lang = (cfg.get("rt_lang") or "").strip()
    if lang == "zh":
        lang_line = "默认用中文口语回答;朗读书里的日语/英语原文时用该语言的**原生发音**念,绝不用中文读音念外语。"
    elif lang == "ja":
        lang_line = "日本語で答えてください。本の原文を読み上げるときは、その言語本来の発音で読んでください。"
    elif lang == "en":
        lang_line = "Respond in English. When reading passages aloud, pronounce them in their original language."
    else:
        lang_line = ("**跟随用户说话的语言**回答;朗读书页原文按内容本身的语言用**原生发音**念——日语内容不要用中文读音。")
    # 59 转写升级:whisper 无语境音近错认("这一页"→"毕业")+模型名 gpt-realtime-whisper 非官方——
    # 换 gpt-4o-mini-transcribe(官方,中文 WER 低于 whisper,$3/M 音频≈$0.003/分钟,独立账单)
    # + prompt 语境(书名+高频指令词,与豆包 ASR 热词㉓同思路;转写模型每句独立调用,不碰主模型缓存)
    _book_t = (file_rel.rsplit("/", 1)[-1].rsplit(".", 1)[0] if file_rel else "")
    # 133(实测事故):这里的 prompt 会被转写模型在**静音/噪声轮**里原样复述回来当成"用户说的话"
    #   (prompt-copy 式幻觉;日志里出现过逐字一致的假转写)。prompt 越长越像句子,可复述的面就越大。
    #   → 缩成**短的稀有词表**:只保留 ASR 真容易听错、又确实需要偏置的专有词;
    #     "这一页/下一页/翻到第N页"这类是常见中文,不给提示也认得准,留着只是给幻觉送弹药。
    #   ⚠ 缩短只能**减少**假转写,不能根治 VAD 假轮 —— 真正的闸是 relay 的手动放行(见 voice_realtime_relay.py)。
    #   ⚠ 改这里的措辞要同步改 relay 的 _ASR_PROMPT_MIRROR / _ASR_GHOST_ANCHORS。
    _tr = {"model": "gpt-4o-mini-transcribe",
           "prompt": "关键词:Anki、笔迹、振假名、生词、假名"}
    if cfg.get("rt_lang") in ("zh", "ja", "en"):
        _tr["language"] = cfg["rt_lang"]
    # 61:route_to_text 恒挂+恒定说明(不随模式变=不破 instructions 前缀缓存);实际放行由 relay 按
    # rt_voice_mode 程序门控(模式按钮通话中热切立即生效,不像 53 的 auto 开关要重拨)
    _route_line = ("**route_to_text 工具**:回答明显较长(详细解释/多步骤/公式推导/长列表)不适合口头念时调用;"
                   "**读完工具结果(如 read_page)要转述大段内容时也一样先调它**,绝不口头念长文。"
                   "调用的同一轮**先口头说一句等待语**(按话题自然说,比如『我把这部分整理成文字给你,稍等』),再调工具。"
                   "intent=一句话概括用户想要什么;系统会用文字模型写出完整回答显示在屏幕上,你调用后本轮结束不要再口头重复。"
                   "系统按用户当前输出模式决定是否放行,被驳回时按提示口头简要回答即可。短答/陪聊/发音示范永远直接说。")
    # rt_instructions 是**附加**指令(UI placeholder 也这么写):旧的 or 链是互斥替换语义,
    # 线上只填了一句发音偏好就把默认人设整段顶掉了 → 默认人设恒在,用户内容按官方骨架另起一段追加。
    _persona = (cfg.get("system_role") or "").strip() or "你是用户的学习伙伴,他在用自己搭的系统自学日语、英语和大学数学物理。"
    _extra = (cfg.get("rt_instructions") or "").strip()
    parts = ["# Role & Objective\n" + _persona]
    if _extra:
        parts.append("# Personality & Tone\n" + _extra)
    parts += [lang_line,
             "**回答长度规则(可测量,请严格遵守)**:快问快答≤8秒;普通讲解≤15秒;内容确实长时先给≤20秒的摘要"
             "并问『要继续展开吗』;绝不复述用户的问题,绝不复述界面卡片上已显示的标题/链接;"
             "用户要听整段原文时,请他用界面上的朗读按钮(那是专用通道),你别整段念。"
             "你配了一套真实工具(function calling):看图细节/翻页/搜索/高亮/做卡片/查词等需要动手的事"
             "**直接调用工具**,拿到真实结果再回答;绝不口头宣称做了没做的事——"
             "尤其做卡片=必须调 make_anki、记笔记=必须调 make_note,没调工具就说『已放进后台/已做好』是欺骗,系统会核查。"
             "联网能力=三个真工具:web_search(查网上实时信息/资料,额度有限省着用)、search_image(搜真实图片)、"
             "search_video(搜教学视频)——用户想查网/看图/看视频时**必须调用对应工具**,图片视频结果会自动显示在他的界面上;"
             "工具失败就如实说暂时查不了,凭记忆答先声明可能过时。search_all_books 只搜他自己的书库,不是互联网。"
             "**绝不要自己输出 markdown 图片或链接占位符**(如 ![...](image_url))假装贴图——界面不会显示任何东西,那是错误行为。"
             "**手写/圈画铁律**:他提到『我写的/我画的/我圈的/帮我看看这个算式』时,永远**先调 see_ink 工具**;"
             "回答『看不到』或让他粘贴/截图都是错误行为。"
             "收到『笔迹已发生变化』的状态消息后,你旧的笔迹记忆即作废——之后他问『现在呢/看到了什么/有没有变化』"
             "这类跟进问题,同样先调 see_ink 重新看;没重新看就凭旧印象答『没有变化』是错误行为。"]
    # ㊶ 指南§8.1:instructions 里绝不放动态数据(书名/页文本)——那会让每次开话前缀都不同,
    # 跨会话缓存永远 miss。页面内容全部走拉模式(前端 pendText,用户开口时以末端 system item 注入)。
    if file_rel:
        _name = file_rel.rsplit("/", 1)[-1]
        parts.append(f"他正在读的书:《{_name}》(位置和页面内容会在他提问时以 system 消息给你;需要更多就调 read_page)。")
    parts.append(_route_line)
    parts.append("**指代铁律**:他用『这个/这里/其他的/剩下的/把它…』这类指代而没点名对象时,默认指**当前页面内容或刚才工具的产出**;"
                 "拿不准就先 read_page / recall_creation 看一眼再回应,别当成闲聊、别凭空猜。")
    parts.append("**页面内容铁律**:他问『这页写了什么/这页讲什么』而上下文里没有当前页文本时,永远**先调 read_page**;"
                 "回答『我看不到页面』或让他把内容/截图发给你,都是错误行为——你有工具,自己去看。")
    parts.append("页面实时状态(选中/手写笔迹)和翻页后的新页面内容会以 system 消息出现在对话里,永远以最新一条为准;"
                 "**状态消息只是记录,永远不要对它们本身做回应或主动评论**;"
                 "没有听到用户清晰说话时调 wait_for_user 安静结束回合,别自己找话说。")
    # 语音场景 description 覆盖:目录行是给文字助手写的,个别工具的"回答里用 markdown 嵌图"教学
    # 在语音里有毒(模型学着输出 ![..](image_url) 假图)——语音版结果由界面自动渲染,只需口头说明
    _vo = {"search_image": ("★配图/看图片专用:按关键词列表**联网搜真实图片**(Wikimedia Commons + Google 图搜,非 AI 生成)。"
                            "用户想看某物的图片/照片时调它:args {queries:[{concept:\"概念\", query:\"所属语言关键词\", query_en:\"english fallback\"}, ...]}"
                            "(query 用**最可能命中的语言**:日本特有事物用日语原名,通用/西方概念用英文;"
                            "query_en 恒带英文翻译,工具先搜 query、没中自动用 query_en 保底;"
                            "**关键词必须简短**——事物名称本身 1~3 个词,别写修饰语和描述句;一次最多 8 个)。"
                            "搜到的图会**自动显示在用户界面**,你只需口头简短说明;"
                            "没搜到就换更通用的词再试一次,再没有就如实说,绝不编链接或输出 markdown 图片语法。")}
    # 工具 description=目录行原文(本就简洁:全量合计≈3.7k 字,"用途+args"结构,符合官方§10.1 的简洁要求)。
    # [:280] 只是防御 cap(防未来有人写出长目录行),现存目录行零截断;_vo 覆盖项(search_image)保留完整。
    # 75(用户裁定):read_selection **永久不挂**——选中内容程序保证经 state 通道注入上下文,
    # 工具是纯重复入口(工具表每次会话恒定一致=前缀缓存无伤;长选中引导 read_page 该页)
    _RTC_DROP = {"read_selection"} | _ORCH_DROP   # 语音模型同样不直接造纸,交给 do_task(见 _ORCH_DROP)
    tools = [{"type": "function", "name": n, "description": _tool_desc_rtc(uid, n, _vo.get(n, str(d)), 1024 if n in _vo else 280),   # 143:说明 + 垫话策略
              "parameters": {"type": "object", "properties": {}, "additionalProperties": True}}
             for n, (d, _) in TOOLS.items() if n not in _RTC_DROP]
    tools.append({"type": "function", "name": "deep_think",
                  "description": "深度思考:复杂推理/长解答/需要更强模型时转交 Claude 深度回答,结果拿回来讲给用户。args {question:完整问题}",
                  "parameters": {"type": "object", "properties": {"question": {"type": "string"}},
                                 "required": ["question"], "additionalProperties": True}})
    tools.append({"type": "function", "name": "route_to_text",
                  "description": "回答较长(详细解释/多步骤/公式/长列表)不适合口头念时调用:intent=一句话概括用户想要什么,"
                                 "系统转文字模型写完整回答显示在屏幕上。**调用的同一轮先口头说一句简短等待语**"
                                 "(按话题自然措辞,如『这个说来话长,我详细写给你,稍等两秒』),说完就调,"
                                 "**绝不口头讲解内容本身**。短答/陪聊/发音示范不要用。",
                  "parameters": {"type": "object", "properties": {"intent": {"type": "string"}},
                                 "required": ["intent"], "additionalProperties": False}})
    tools.append({"type": "function", "name": "wait_for_user",
                  "description": "当最新音频是静音、背景噪声、等待音乐、电视声或明显不是在对你说话时调用:安静结束本轮、不要说任何话。",
                  "parameters": {"type": "object", "properties": {}, "additionalProperties": False}})
    sess = {"type": "realtime", "model": cfg.get("rt_model") or "gpt-realtime-2.1-mini",
            # 127(用户拍板):GPT 走**官方 WebRTC 直连**,别再叠中间层——会话级模态按四态档定
            # (stt=纯文字:输出音频费归零;其余=语音)。工具结果轮仍可用带 modalities 的 response.create 覆盖。
            "output_modalities": (["text"] if (cfg.get("rt_voice_mode") == "stt") else ["audio"]),
            "reasoning": {"effort": cfg.get("rt_effort") or "low"},
            "max_output_tokens": 2048,   # 64 用户拍板:全档 2048(≈100s 音频),不搞小预算硬截断;时长靠 prompt 规则+route 自觉
            "instructions": "\n".join(parts),
            "audio": {"input": {"noise_reduction": {"type": ("far_field" if cfg.get("rt_noise") == "far" else "near_field")},   # 86:官方降噪双档——far_field=桌面外放场景,环境音/操作声抑制更强
                                "turn_detection": {"type": "semantic_vad",
                                                   "eagerness": cfg.get("rt_eagerness") or "auto",
                                                   # 133(用户实测「一句话被回答很多次」+ 外部评审 P0):**退回手动挡**。
                                                   # 127 当年为省一个跨海 RTT 开了自动挡,代价是把"这一轮该不该回答"
                                                   # 整个交给 VAD —— 而 VAD 会把 AI 的回声/环境噪声切成**假轮**,
                                                   # 于是白答一次;interrupt_response=True 还让假轮**打断**正在跑的工具
                                                   # (实测 see_ink 被打断标"已过期")。这不是调参能治的,必须夺回放行权。
                                                   # 现在:VAD 只负责断句 → relay 拿到转写验明真伪 → 真人才 response.create,
                                                   # 假轮直接删掉 item(见 voice_realtime_relay.py 的 gate)。
                                                   # 代价=首字多等一次 ASR(有 4s 超时兜底,绝不会该答不答)。
                                                   "create_response": False, "interrupt_response": False},
                                "transcription": _tr},
                      "output": {"voice": cfg.get("rt_voice") or "marin",
                                 # 92:语速设置(官方 audio.output.speed 0.25-1.5,session 级,下次通话生效)
                                 "speed": max(0.25, min(1.5, float(cfg.get("rt_speed") or 1.0)))}},
            "tools": tools, "tool_choice": "auto", "parallel_tool_calls": False,
            "truncation": {"type": "retention_ratio", "retention_ratio": 0.8,
                           "token_limits": {"post_instructions": 24000}}}   # ㊶ 指南§5:上下文硬顶(㊳摘要12k先行,这是官方截断兜底)
    try:   # ㊳ 会话内压缩阈值:每轮 input_tokens 超过它=历史携带成本已值得付一次缓存失效换摘要(0=关)
        _cth = int(cfg.get("rt_compact_tokens")) if cfg.get("rt_compact_tokens") is not None else 24000   # 124(#285):Cookbook 重做完成(root摘要+等deleted确认)→按审核约定默认启用,阈值24k
    except Exception:
        _cth = 0
    return sess, _cth, bool(cfg.get("rt_image"))


@bp.route("/rtc-session", methods=["POST"])
def assistant_rtc_session():
    """WebRTC 会话配置下发:预算闸 + 服务端自建 session(见 _build_rtc_session)。"""
    if not _logged_in():
        return jsonify({"ok": False}), 401
    uid = session["user_id"]   # 140:工具说明的 per-user 覆盖要用它
    _bok, _bspent = _voice_budget_gate()
    if not _bok:
        return jsonify({"ok": False, "error": f"今日语音预算已用完(${_bspent:.2f})——设置里调 rt_budget_usd 或明天再聊"}), 429
    body = request.get_json(silent=True) or {}
    file_rel = (body.get("file") or "").strip()
    try:
        page = int(body.get("page") or 0)
    except Exception:
        page = 0
    sess, _cth, _rt_image = _build_rtc_session(uid, file_rel, page)
    return jsonify({"ok": True, "session": sess, "model": sess["model"], "rt_image": _rt_image,
                    "compact_tokens": _cth})


@bp.route("/rtc-call", methods=["POST"])
def assistant_rtc_call():
    """SDP 代理:浏览器 offer → OpenAI POST /v1/realtime/calls(标准 key 只在服务端)→ answer SDP 回浏览器。
    安全:① 这里也跑一次预算闸(此前只有 /rtc-session 有,直接 POST /rtc-call 能绕开);
    ② session 配置**一律服务端重建**,忽略前端回传的 body["session"](防篡改型号/token 上限烧 key)。
    前端可信的只有 sdp;file/page 仅用于书目上下文,缺失也能正常建连。"""
    if not _logged_in():
        return jsonify({"ok": False}), 401
    _bok, _bspent = _voice_budget_gate()
    if not _bok:
        return jsonify({"ok": False, "error": f"今日语音预算已用完(${_bspent:.2f})——设置里调 rt_budget_usd 或明天再聊"}), 429
    body = request.get_json(silent=True) or {}
    sdp = body.get("sdp") or ""
    if not sdp:
        return jsonify({"ok": False, "error": "缺 sdp"}), 400
    uid = session["user_id"]
    file_rel = (body.get("file") or "").strip()
    try:
        page = int(body.get("page") or 0)
    except Exception:
        page = 0
    sess, _cth, _rt_image = _build_rtc_session(uid, file_rel, page)   # 忽略 body["session"],服务端自建
    model = (sess.get("model") or "gpt-realtime-2.1-mini").strip()
    try:
        key = json.loads(_RTC_KEY_PATH.read_text("utf-8")).get("api_key") or ""
    except Exception:
        key = ""
    if not key:
        return jsonify({"ok": False, "error": "缺 OpenAI 凭证(~/.config/openai-realtime.json)"}), 400
    import requests as _rq
    try:
        import hashlib as _hl
        _sid = _hl.sha256(f"bw-{session['user_id']}".encode()).hexdigest()[:32]   # ㊶ 指南§4.1:稳定且隐私保护的用户哈希
        r = _rq.post(f"https://api.openai.com/v1/realtime/calls?model={model}",
                     headers={"Authorization": f"Bearer {key}", "OpenAI-Safety-Identifier": _sid},
                     files={"sdp": (None, sdp, "application/sdp"),
                            "session": (None, json.dumps(sess, ensure_ascii=False), "application/json")},
                     timeout=25)
        if r.status_code >= 300:
            return jsonify({"ok": False, "error": f"OpenAI {r.status_code}: {r.text[:300]}"}), 502
        # call_id 在 Location header(官方形态):sideband 注入大 payload(图像)要用它
        cid = (r.headers.get("Location") or "").rstrip("/").rsplit("/", 1)[-1]
        # 133:连同票据一起下发 —— 前端把它带在 sideband URL 上,relay 验签后才敢做"接管旧通话"
        return jsonify({"ok": True, "sdp": r.text, "call_id": cid,
                        "uid": str(uid), "ticket": _voice_ticket(uid, cid)})
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)[:200]}), 502


@bp.route("/rtc-hangup", methods=["POST"])
def assistant_rtc_hangup():
    """133:真·挂断一路 WebRTC 通话(官方 POST /v1/realtime/calls/{call_id}/hangup)。

    为什么需要服务端挂断:媒体是浏览器↔OpenAI **直连**,前端 pc.close() 只切断自己这一端;
    若前端因竞态把某个 pc 的引用弄丢了(见 rc-voicecall.js 的世代漏洞),那路 call **在 OpenAI 侧仍然活着**
    ——继续收音频、继续计费、继续跟别的 call 抢答。只有这个官方端点能从服务端把它真正终止。
    用途:① 前端过期拨号自我了断(_rtcAbandon);② 后续的单通话唯一性租约(踢掉被接管的旧通话)。
    """
    if not _logged_in():
        return jsonify({"ok": False}), 401
    cid = ((request.get_json(silent=True) or {}).get("call_id") or "").strip()
    if not cid:
        return jsonify({"ok": False, "error": "缺 call_id"}), 400
    try:
        key = json.loads(_RTC_KEY_PATH.read_text("utf-8")).get("api_key") or ""
    except Exception:
        key = ""
    if not key:
        return jsonify({"ok": False, "error": "缺 OpenAI 凭证"}), 400
    import requests as _rq
    try:
        r = _rq.post(f"https://api.openai.com/v1/realtime/calls/{cid}/hangup",
                     headers={"Authorization": f"Bearer {key}"}, timeout=10)
        ok = r.status_code < 300
        sys.stderr.write(f"[rtc-hangup] uid={session['user_id']} call={cid[:14]} → {r.status_code}\n")
        return jsonify({"ok": ok, "status": r.status_code})
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)[:200]}), 502


@bp.route("/route-text", methods=["POST"])
def assistant_route_text():
    """route 档(61):语音模型只发一句 intent(几十 token,不占 512 音频硬顶),长正文由便宜文本模型
    (Gemini flash)**流式**生成——SSE: delta*n → done{summary}。审核二轮方案:决定与写作解耦。"""
    if not _logged_in():
        return jsonify({"ok": False}), 401
    body = request.get_json(silent=True) or {}
    intent = (body.get("intent") or "").strip()[:500]
    q = (body.get("q") or "").strip()[:2000]
    file_rel = (body.get("file") or "").strip()
    try:
        page = int(body.get("page") or 0)
    except Exception:
        page = 0
    ptext = ""
    if file_rel:
        try:
            ptext = (_page_text(file_rel, page) or "")[:3500]
        except Exception:
            ptext = ""
    sysmsg = ("你是阅读学习助手的文字详答引擎:用户在语音通话中提了需要长篇文字回答的问题,语音助手转给你写正文。"
              "直接输出最终回答——结构清晰、要点完整但不啰嗦,可用 Markdown(数学严格用 $...$ 包裹,禁止反引号包数学);"
              "第一句就进入正题,不写「好的/当然」等开场白。用用户提问的语言回答。"
              "正文写完后,**另起一行**输出 [[BRIEF]] 开头的一句话概括(30 字内,给语音助手看的要点,不面向用户)。")
    pieces = []
    if ptext:
        pieces.append(f"[用户正看的第 {page} 页内容节选]\n{ptext}")
    if q:
        pieces.append(f"[用户原话]{q}")
    pieces.append(f"[语音助手对需求的概括]{intent or '(未提供)'}")
    prompt = "\n\n".join(pieces)

    _rt = _resolve("route_text", session.get("user_id"))   # 91:路由详答型号走设置项
    _rt_model = _rt.get("variant") if _is_gemini(_rt.get("variant") or "") else None

    def gen():
        # 79:正文流式外发,[[BRIEF]] 段拦下不外流(尾部 hold-back 12 字防标记被 chunk 切两半);
        # done.summary=引擎专门写的简介(给 2.1 静默入库,与天气/新闻卡同逻辑),没写就前 240 字兜底
        full, sent = "", 0
        MARK = "[[BRIEF]]"
        def _flush(force=False):
            nonlocal sent
            mi = full.find(MARK)
            limit = mi if mi >= 0 else (len(full) if force else max(sent, len(full) - 12))
            if limit > sent:
                seg = full[sent:limit]
                sent = limit
                return "event: delta\ndata: " + json.dumps(seg, ensure_ascii=False) + "\n\n"
            return ""
        for kind, val in _gemini_stream(sysmsg, [{"role": "user", "parts": [{"text": prompt}]}],
                                        model=_rt_model, think=False, timeout=120):
            if kind == "delta":
                full += val
                out0 = _flush()
                if out0:
                    yield out0
            elif kind == "err" and not full:
                try:   # 流式失败一次性兜底(慢但别哑)
                    full = _gemini_text(sysmsg + "\n\n" + prompt, max_tokens=2000, model=_rt_model, think=False, timeout=90) or ""
                except Exception:
                    full = ""
                if not full:
                    yield "event: err\ndata: {}\n\n"
                    return
        tail = _flush(force=True)
        if tail:
            yield tail
        mi = full.find(MARK)
        brief = full[mi + len(MARK):].strip().strip(":: ").splitlines()[0][:120] if mi >= 0 else ""
        body = full[:mi].rstrip() if mi >= 0 else full
        yield "event: done\ndata: " + json.dumps({"summary": brief or body[:240]}, ensure_ascii=False) + "\n\n"

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _rtc_sideband_images(call_id: str, images: list) -> bool:
    """经 sideband WS(官方服务端通道,wss://…/v1/realtime?call_id=X)把图像注入 WebRTC 会话。
    为什么不走前端 data channel:SCTP 单条消息上限(Safari≈64KB),base64 笔迹图几百 KB,
    超限发送按规范**直接关闭 dc**=通话哑死——图这种大 payload 必须从服务端边带进去。"""
    try:
        key = json.loads(_RTC_KEY_PATH.read_text("utf-8")).get("api_key") or ""
        if not key or not call_id:
            return False
        from websockets.sync.client import connect as _ws_connect
        with _ws_connect(f"wss://api.openai.com/v1/realtime?call_id={call_id}",
                         additional_headers={"Authorization": f"Bearer {key}"},
                         open_timeout=8, close_timeout=3, max_size=None) as ws:
            for im in images[:2]:
                ws.send(json.dumps({"type": "conversation.item.create", "item": {
                    "type": "message", "role": "user",
                    "content": [{"type": "input_image", "detail": "high",
                                 "image_url": f"data:{im.get('media_type') or 'image/png'};base64,{im.get('b64') or ''}"}]}}))
            # 短窗收确认:等**每张**都 created 再关连接(60,审核指正:旧版第一张确认就返回,
            # 后续图的确认没等到;error 事件=注入失败走 fallback)
            import time as _t
            n_sent, n_ok = min(len(images), 2), 0
            t_end = _t.time() + 3.0
            while _t.time() < t_end and n_ok < n_sent:
                try:
                    ev = json.loads(ws.recv(timeout=max(0.1, t_end - _t.time())))
                except TimeoutError:
                    break
                except Exception:
                    break
                if ev.get("type") == "error":
                    print(f"[rtc-sideband] error: {json.dumps(ev.get('error') or {})[:200]}", flush=True)
                    return False
                if ev.get("type") == "conversation.item.created":
                    n_ok += 1
            if n_ok < n_sent:
                print(f"[rtc-sideband] 确认不全 {n_ok}/{n_sent}(短窗超时,已发送帧通常仍会送达)", flush=True)
            return n_ok >= 1
        return True
    except Exception as ex:
        print(f"[rtc-sideband] fail: {type(ex).__name__}: {str(ex)[:150]}", flush=True)
        return False


_RTC_USAGE_FILE = CLAUDE_DIR / "state" / "openai-usage.json"
# ㊶ 指南§7.1:标准版与 mini 价差 6-8 倍,记账必须按模型分表(前端 usage 上报带 _model)
_RTC_RATE_STD = {"in_text": 4.00, "cached_text": 0.40, "out_text": 24.00,
                 "in_audio": 32.00, "cached_audio": 0.40, "out_audio": 64.00, "in_image": 5.00}
_RTC_RATE = {"in_text": 0.60, "cached_text": 0.06, "out_text": 2.40,
             "in_audio": 10.0, "cached_audio": 0.30, "out_audio": 20.0, "in_image": 0.80}


@bp.route("/rtc-usage", methods=["POST"])
def assistant_rtc_usage():
    """WebRTC 版 usage 记账。125(#283 P3):记账权威=relay sideband(自读 response.done.usage 写 SQLite 账本),
    本端点**退位为兼容 no-op**(旧页面 JS 还在上报;继续写账=双记)。"""
    if not _logged_in():
        return jsonify({"ok": False}), 401
    return jsonify({"ok": True, "note": "ledger-by-relay"})
    usage = request.get_json(silent=True) or {}
    _R = _RTC_RATE if "mini" in str(usage.get("_model") or "mini") else _RTC_RATE_STD   # ㊶ 按模型选价表
    try:
        itd = usage.get("input_token_details") or {}
        otd = usage.get("output_token_details") or {}
        ctd = itd.get("cached_tokens_details") or {}
        row = {"in_text": int(itd.get("text_tokens") or 0), "in_audio": int(itd.get("audio_tokens") or 0),
               "in_image": int(itd.get("image_tokens") or 0),
               "cached_text": int(ctd.get("text_tokens") or 0), "cached_audio": int(ctd.get("audio_tokens") or 0),
               "out_text": int(otd.get("text_tokens") or 0), "out_audio": int(otd.get("audio_tokens") or 0)}
        cost = ((row["in_text"] - row["cached_text"]) * _R["in_text"]
                + row["cached_text"] * _R["cached_text"]
                + (row["in_audio"] - row["cached_audio"]) * _R["in_audio"]
                + row["cached_audio"] * _R["cached_audio"]
                + row["in_image"] * _R["in_image"]
                + row["out_text"] * _R["out_text"]
                + row["out_audio"] * _R["out_audio"]) / 1_000_000.0
        day = time.strftime("%Y-%m-%d")
        try:
            data = json.loads(_RTC_USAGE_FILE.read_text("utf-8"))
        except Exception:
            data = {}
        d = data.setdefault(day, {k: 0 for k in row} | {"usd": 0.0, "turns": 0})
        for k, v in row.items():
            d[k] = d.get(k, 0) + v
        d["usd"] = round(d.get("usd", 0.0) + cost, 6)
        d["turns"] = d.get("turns", 0) + 1
        _RTC_USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _RTC_USAGE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    except Exception:
        pass
    return jsonify({"ok": True})


@bp.route("/log", methods=["POST"])
def assistant_log_external():
    """外部编排 agent(MCP)桥③:把外部 AI 跟用户的对话写进助手会话历史(标 via:'mcp')——
    阅读器侧栏能看到这些对话,内置助手接手时也有完整上下文。body: {user?, assistant?, file?, page?}。"""
    if not _logged_in():
        return jsonify({"ok": False}), 401
    b = request.get_json(silent=True) or {}
    uid = session["user_id"]
    meta = {"via": b.get("via") if b.get("via") in ("mcp", "voice") else "mcp"}   # ㉛:通话轮次落库标 voice
    if b.get("file"):
        meta["file_rel"] = b["file"]   # _convo_append 白名单字段名是 file_rel
    if b.get("page"):
        meta["page"] = b["page"]
    # 141(轮次容器):同一 turn_id 再次上报 = 这一轮又产生了新内容(多 response / 工具结果 / 结果卡)
    #   → **覆盖**那条助手消息,而不是再追加一条。不这么做就会:同一轮渲两遍 + 早期快照缺卡片。
    _tid = str(b.get("turn_id") or "")[:40]
    if _tid and _convo_upsert_turn(uid, _tid, (b.get("assistant") or "").strip(),
                                   {"parts": b.get("parts") if isinstance(b.get("parts"), list) else None,
                                    "clip": re.sub(r"[^A-Za-z0-9_-]", "", str(b.get("clip") or ""))[:40] or None}):
        return jsonify({"ok": True, "n": 0, "upserted": True})
    # ⚠ upsert_only:容器的"内容变了就同步"走这条 —— **记录不存在就什么都不做**。
    #   否则它可能先于 response.done 到达 → 先建出一条没有用户提问的助手消息 →
    #   随后 response.done 的落库走 upsert 提前返回 → **用户的提问从历史里彻底消失**。
    if b.get("upsert_only"):
        return jsonify({"ok": True, "n": 0, "upserted": False})
    if _tid:
        meta["turn_id"] = _tid
    n = 0
    for role, key in (("user", "user"), ("assistant", "assistant")):
        txt = (b.get(key) or "").strip()
        card = b.get("card") if (role == "assistant" and isinstance(b.get("card"), dict)
                                 and len(json.dumps(b.get("card"), ensure_ascii=False)) < 8000) else None
        if not txt and card:   # 87:卡片可独立成一条(content=概要,结构在 meta.card)
            txt = str(card.get("brief") or card.get("title") or "[卡片]")[:300]
        if txt:
            m2 = dict(meta)
            if role == "assistant" and b.get("clip"):   # 66:通话录下的该轮语音,历史回放用
                m2["clip"] = re.sub(r"[^A-Za-z0-9_-]", "", str(b["clip"]))[:40]
            # 141(轮次容器):落全量 part 结构 → 历史回放用**同一个渲染器**复原,不再退化成纯文本。
            #   ⚠ 体积闸:图必须走 URL 不能走 base64(单张 10-50 万字节,几十轮就把历史撑爆;见 ADR §4)。
            if role == "assistant" and isinstance(b.get("parts"), list):
                try:
                    _pj = json.dumps(b["parts"], ensure_ascii=False)
                    if len(_pj) < 24000:
                        m2["parts"] = b["parts"]
                    else:
                        sys.stderr.write(f"[log] parts 过大({len(_pj)}字)已丢弃 —— 图是不是又走了 base64?\n")
                except Exception:
                    pass
            if card:
                m2["card"] = card
            _convo_append(uid, role, txt[:8000], m2)
            n += 1
    try:
        import reader_events
        reader_events.publish("assistant-history", b.get("file") or "", uid)   # 侧栏开着可实时感知(未订阅则无害)
    except Exception:
        pass
    return jsonify({"ok": True, "appended": n})


_CLIP_DIR = CLAUDE_DIR / "state" / "voice-clips"


def _clip_id_ok(cid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", cid or "")[:40]


@bp.route("/voice-clip", methods=["POST"])
def assistant_voice_clip_up():
    """66:通话按轮录下的 AI 语音上传(前端 MediaRecorder blob)。?id=<clipId>,body=音频字节。"""
    if not _logged_in():
        return jsonify({"ok": False}), 401
    cid = _clip_id_ok(request.args.get("id", ""))
    data = request.get_data()
    if not cid or not data or len(data) > 8 * 1024 * 1024:
        return jsonify({"ok": False, "error": "bad id/size"}), 400
    mt = (request.content_type or "audio/mp4").split(";")[0]
    ext = "mp4" if "mp4" in mt else ("webm" if "webm" in mt else "bin")
    d = _CLIP_DIR / str(session["user_id"])
    d.mkdir(parents=True, exist_ok=True)
    f0 = d / f"{cid}.{ext}"
    f0.write_bytes(data)
    if ext == "mp4":   # 97(实测根因):iOS MediaRecorder 分段 fMP4 的 moov 时长只有首段(0.8s)→回放只响一声;-c copy remux 重写正确时长
        try:
            import subprocess as _sp
            _tmp = d / f"{cid}.fix.mp4"
            r0 = _sp.run(["ffmpeg", "-y", "-v", "error", "-i", str(f0), "-c", "copy", "-movflags", "+faststart", str(_tmp)],
                         capture_output=True, timeout=20)
            if r0.returncode == 0 and _tmp.exists() and _tmp.stat().st_size > 1000:
                _tmp.replace(f0)
            else:
                _tmp.unlink(missing_ok=True)
        except Exception:
            pass
    try:   # 简单配额:每用户保留最近 400 段,旧的按 mtime 清
        fs = sorted(d.iterdir(), key=lambda f: f.stat().st_mtime)
        for f in fs[:-400]:
            f.unlink(missing_ok=True)
    except Exception:
        pass
    return jsonify({"ok": True, "id": cid})


@bp.route("/voice-clip/<cid>")
def assistant_voice_clip_dl(cid):
    if not _logged_in():
        return jsonify({"ok": False}), 401
    cid = _clip_id_ok(cid)
    d = _CLIP_DIR / str(session["user_id"])
    for ext, mt in (("mp4", "audio/mp4"), ("webm", "audio/webm"), ("bin", "application/octet-stream")):
        f = d / f"{cid}.{ext}"
        if cid and f.exists():
            resp = send_file(str(f), mimetype=mt, conditional=True)   # 97:conditional=Range 支持(iOS <audio> 对无 Range 源易异常)
            resp.headers["Cache-Control"] = "private, max-age=86400"
            return resp
    return jsonify({"ok": False}), 404


_VCARD_DIR = CLAUDE_DIR / "state" / "voice-cards"


@bp.route("/voice-cards", methods=["GET", "POST"])
def assistant_voice_cards():
    """78:卡片收藏夹(用户设计)——独立于会话持久存储(清空对话不清它)。
    卡片自带元数据(书/页/触发问题/时间),长回答类内容离开上下文也能自释。
    GET → {cards};POST {op:'add', card} / {op:'del', id}。"""
    if not _logged_in():
        return jsonify({"ok": False}), 401
    uid = session["user_id"]
    _VCARD_DIR.mkdir(parents=True, exist_ok=True)
    f = _VCARD_DIR / f"{uid}.json"
    try:
        cards = json.loads(f.read_text("utf-8"))
    except Exception:
        cards = []
    now = int(time.time())
    n_before = len(cards)
    cards = [c for c in cards if not c.get("deleted") or now - c["deleted"] < 86400]   # 80:回收站保 1 天,过期真删
    if len(cards) != n_before:
        f.write_text(json.dumps(cards, ensure_ascii=False), "utf-8")
    if request.method == "GET":
        if request.args.get("trash"):   # 80:回收站视图(1 天内删除的)
            return jsonify({"ok": True, "cards": [c for c in cards if c.get("deleted")][-100:]})
        return jsonify({"ok": True, "cards": [c for c in cards if not c.get("deleted")][-200:]})
    b = request.get_json(silent=True) or {}
    if b.get("op") == "restore" and b.get("id"):   # 80:从回收站恢复
        for c in cards:
            if c.get("id") == b["id"]:
                c.pop("deleted", None)
        f.write_text(json.dumps(cards, ensure_ascii=False), "utf-8")
        return jsonify({"ok": True})
    if b.get("op") == "add" and isinstance(b.get("card"), dict):
        c = b["card"]
        rec = {"id": re.sub(r"[^A-Za-z0-9_-]", "", str(c.get("id") or ""))[:40] or f"v{int(time.time()*1000)}",
               "label": str(c.get("label") or "卡片")[:80],
               "kind": str(c.get("kind") or "")[:24],
               "raw": str(c.get("raw") or "")[:20000],
               "isHtml": bool(c.get("isHtml")),
               "text": str(c.get("text") or "")[:4000],
               "meta": {k: str((c.get("meta") or {}).get(k) or "")[:300] for k in ("file", "page", "q")},
               "ts": int(time.time())}
        cards = [x for x in cards if x.get("id") != rec["id"]]
        cards.append(rec)
        f.write_text(json.dumps(cards[-200:], ensure_ascii=False), "utf-8")
        return jsonify({"ok": True, "id": rec["id"]})
    if b.get("op") == "del" and (b.get("id") or b.get("ids")):
        ids = set([b["id"]] if b.get("id") else (b.get("ids") or []))
        for c in cards:
            if c.get("id") in ids:
                c["deleted"] = now   # 80:软删进回收站(1 天可恢复)
        f.write_text(json.dumps(cards, ensure_ascii=False), "utf-8")
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 400


@bp.route("/clip-attach", methods=["POST"])
def assistant_clip_attach():
    """66:历史消息补挂语音(灰钮 TTS 生成保存后回写)——按 ts+内容前缀定位该条 assistant 消息。"""
    if not _logged_in():
        return jsonify({"ok": False}), 401
    b = request.get_json(silent=True) or {}
    uid = session["user_id"]
    clip = _clip_id_ok(b.get("clip") or "")
    head = (b.get("head") or "").strip()[:60]
    try:
        ts = int(b.get("ts") or 0)
    except Exception:
        ts = 0
    if not clip or not head:
        return jsonify({"ok": False}), 400
    with _convo_lock:
        msgs = _convo_load(uid)
        hit = None
        for m in reversed(msgs):
            if (m.get("role") == "assistant" and (not ts or m.get("ts") == ts)
                    and (m.get("content") or "").startswith(head[:40])):
                hit = m
                break
        if not hit:
            return jsonify({"ok": False, "error": "not found"}), 404
        hit["clip"] = clip
        try:
            p = _convo_path(uid)
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(json.dumps(msgs[-200:], ensure_ascii=False), "utf-8")
            os.replace(tmp, p)
        except Exception:
            return jsonify({"ok": False}), 500
    return jsonify({"ok": True})


@bp.route("/clear", methods=["POST"])
def assistant_clear():
    if not _logged_in():
        return jsonify({"ok": False}), 401
    _convo_clear(session["user_id"])
    try:   # ㊲:摘要与历史同命运——清空=原文+压缩记忆一起消失
        _summary_path(session["user_id"]).unlink(missing_ok=True)
    except Exception:
        pass
    try:   # 66b:语音原声与记录同命运——清空对话=该用户的录音文件一并删
        import shutil
        shutil.rmtree(_CLIP_DIR / str(session["user_id"]), ignore_errors=True)
    except Exception:
        pass
    return jsonify({"ok": True})


@bp.route("/undo", methods=["POST"])
def assistant_undo():
    if not _logged_in():
        return jsonify({"ok": False}), 401
    undo_id = (request.get_json(silent=True) or {}).get("id")
    import voice
    return jsonify(voice._undo_do(undo_id, owner=session["user_id"]))   # owner 校验:只能撤自己的(防猜 id 删别人的)


_APF_PATH = CLAUDE_DIR / "state" / "assistant-pref-profiles.json"

# ── 语音通话(豆包 S2S)设置(v3-⑮):设置面板读写凭证文件的**非密钥白名单字段**——
#    api_key 绝不经此暴露;relay 每次 _creds() 现读,写完即生效(通话中由前端发 {type:"cfg"} 触发热更)。
_VOICE_CFG_PATH = Path("~/.config/doubao-voice.json").expanduser()
_VOICE_CFG_FIELDS = ("speaker", "speech_rate", "loudness_rate", "explicit_dialect",
                     "bot_name", "speaking_style", "system_role", "enable_music",
                     "end_smooth_window_ms", "tts_speaker", "tts_speech_rate", "tts_instruction", "recall_cutoff", "asr_v2",
                     "rt_engine", "rt_model", "rt_voice", "rt_effort", "rt_image", "rt_lang",
                     "rt_instructions", "rt_eagerness", "rt_full_duplex", "rt_compact_tokens",
                     "rt_voice_mode", "rt_auto_text", "rt_tts_speak", "rt_noise", "rt_tool_reply", "rt_speed", "rt_grok_voice",
                     "rt_grok_vad", "rt_grok_replace", "rt_budget_usd")


@bp.route("/voice-config", methods=["GET", "POST"])
def assistant_voice_config():
    """语音通话设置:GET → 白名单字段当前值;POST {字段:值,…} → merge 写回(值为 ""/null = 删字段回默认)。"""
    if not _logged_in():
        return jsonify({"ok": False}), 401
    try:
        cfg = json.loads(_VOICE_CFG_PATH.read_text("utf-8"))
    except Exception:
        cfg = {}
    if request.method == "POST":
        b = request.get_json(silent=True) or {}
        for k in _VOICE_CFG_FIELDS:
            if k not in b:
                continue
            v = b[k]
            if v in ("", None, False):   # 清空/关掉 = 删字段回默认(enable_music False 也删,别留死字段)
                cfg.pop(k, None)
            else:
                cfg[k] = v
        try:
            _VOICE_CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), "utf-8")
        except Exception as ex:
            return jsonify({"ok": False, "error": str(ex)[:120]}), 500
    return jsonify({"ok": True, "cfg": {k: cfg.get(k) for k in _VOICE_CFG_FIELDS}})


@bp.route("/creations-brief")
def assistant_creations_brief():
    """最近创造物清单(告知+句柄)——语音 RTC 前端注入用,与文字 _sys_prompt 同一个源(_creations_recent_line)。"""
    if not _logged_in():
        return jsonify({"ok": False}), 401
    return jsonify({"ok": True, "line": _creations_recent_line(str(session["user_id"]))})


@bp.route("/pref-profiles", methods=["GET", "POST"])
def assistant_pref_profiles():
    """模型设置**预设方案**(用户设计:面板顶部几个按钮,点击=应用整套配置,可保存/删除)。
    GET → {profiles:[names]};POST {op:"save"|"apply"|"delete", name}。
    save=把当前用户的全套 action-prefs 存为 name;apply=整包写回 action-prefs;delete=删方案。"""
    if not _logged_in():
        return jsonify({"ok": False}), 401
    uid = str(session["user_id"])
    with _ap_lock:
        try:
            allp = json.loads(_APF_PATH.read_text("utf-8")) if _APF_PATH.exists() else {}
        except Exception:
            allp = {}
        mine = allp.get(uid) or {}
        if request.method == "GET":
            return jsonify({"ok": True, "profiles": sorted(k for k in mine.keys() if not k.startswith("_")),
                            "active": mine.get("_active")})   # active=当前应用中的预设名(chip 高亮;单项改过即被清)
        b = request.get_json(silent=True) or {}
        op, name = b.get("op"), (b.get("name") or "").strip()[:20]
        if not name or name.startswith("_") or op not in ("save", "apply", "delete"):
            return jsonify({"ok": False, "error": "bad op/name"}), 400
        try:
            ap = json.loads(_AP_PATH.read_text("utf-8")) if _AP_PATH.exists() else {}
        except Exception:
            ap = {}
        if op == "save":
            mine[name] = dict(ap.get(uid) or {})
            mine["_active"] = name        # 存完当前 = 应用中就是它
        elif op == "delete":
            mine.pop(name, None)
            if mine.get("_active") == name:
                mine.pop("_active", None)
        elif op == "apply":
            if name not in mine:
                return jsonify({"ok": False, "error": "no such profile"}), 404
            ap[uid] = dict(mine[name])
            _AP_PATH.parent.mkdir(parents=True, exist_ok=True)
            _AP_PATH.write_text(json.dumps(ap, ensure_ascii=False), "utf-8")
            mine["_active"] = name
        allp[uid] = mine
        _APF_PATH.parent.mkdir(parents=True, exist_ok=True)
        _APF_PATH.write_text(json.dumps(allp, ensure_ascii=False), "utf-8")
        return jsonify({"ok": True, "profiles": sorted(k for k in mine.keys() if not k.startswith("_")),
                        "active": mine.get("_active")})


@bp.route("/action-pref", methods=["POST"])
def assistant_action_pref():
    """按动作存 (后端/型号/深度) 预设(感叹号 ⚙/🐢/🎯 + 模型设置面板)。
    body: {action, backend, variant, depth}。三者非法(或空)→ 清除该动作预设、回默认。
    兼容旧前端 {action, model, effort}(无 backend 时按 claude 解释)。"""
    if not _logged_in():
        return jsonify({"ok": False}), 401
    b = request.get_json(silent=True) or {}
    action = b.get("action")
    if action not in _AP_ACTIONS:
        return jsonify({"ok": False, "error": "bad action"}), 400
    backend, variant, depth = b.get("backend"), b.get("variant"), b.get("depth")
    if not backend and (b.get("model") or b.get("effort")):   # 旧前端:{model,effort} → claude
        backend, variant, depth = "claude", b.get("model"), b.get("effort")
    saved = _ap_set(session["user_id"], action, backend, variant, depth)
    return jsonify({"ok": True, "pref": saved})   # saved=None → 已清除回默认


@bp.route("/action-prefs", methods=["GET"])
def assistant_action_prefs():
    """模型设置面板拉数据:每个动作的当前预设 + 出厂默认 + 可选项目录(后端/型号/深度)。"""
    if not _logged_in():
        return jsonify({"ok": False}), 401
    uid = session["user_id"]
    actions = {a: {"pref": _ap_get(uid, a), "default": _AP_DEFAULTS[a]} for a in _AP_ACTIONS}
    # 主动探测:免费档对各型号支不支持(只探"没探过"的,结果持久缓存 30 天 → 之后命中缓存 0 请求)。
    # 这样 pro 等付费型号在用户「真点」之前就被验证并隐藏,而不是等点了失败才隐藏。
    gall = _gemini_models()
    # ⚠ **不阻塞面板**:探测是真出网(每型号 12s 超时),放在请求线程里 → 用户点开「⚙ 模型」要干等十几秒
    #   (用户实测「有时加载很久」)。改成后台跑:本次用**已知缓存**立即出面板,探测结果下次打开生效。
    #   (缓存是持久化的 30 天,所以"下次"通常就是稳定态;首次/过期那一次少几个型号也无伤。)
    _todo = [m for m in gall if not _is_paid_only(m)]
    if _todo:
        threading.Thread(target=_probe_free_batch, args=(_todo,), daemon=True).start()
    # 「免费档验证为不支持」且不在付费清单 → 真不可用,隐藏;**仅付费**的保留并标💰
    # (以前一律隐藏 → 3.1-pro 这类 paid-only 型号面板里永远选不到,2026-07 修)
    gmods = [m for m in gall if _free_state(m) != "no" or _is_paid_only(m)]
    for _a in _AP_ACTIONS:   # 但保留用户当前预设用到的型号(即使免费不支持),否则面板显示不出他选的那个
        _pv = ((actions.get(_a) or {}).get("pref") or {}).get("variant")
        if _pv and _is_gemini(_pv) and _pv not in gmods:
            gmods.append(_pv)
    vshort = dict(_VARIANT_SHORT)
    for m in gmods:
        vshort.setdefault(m, _variant_short(m))     # 给动态型号补简称
    _now = time.time(); _free_off = _gemini_off.get("free", 0)
    gemini_status = {}   # 免费档状态:可用 / 临时限流 / **当前过载(503)** / 不支持
    for m in gmods:
        bz = _free_busy.get(m)
        if _is_paid_only(m):   # ListModels 证实只在付费清单 → 恒走付费,面板标💰(不是临时状态,retry=0)
            gemini_status[m] = {"free": False, "reason": "仅付费档(按量计费)", "retry": 0, "paid_only": True}
        elif _is_unsupported("free", m):
            gemini_status[m] = {"free": False, "reason": "免费档不支持此型号", "retry": 0}
        elif _now < _free_off:
            gemini_status[m] = {"free": False, "reason": "免费额度限流/耗尽", "retry": int(_free_off - _now)}
        elif bz and (_now - bz[0]) < _FREE_BUSY_TTL:   # 免费档对该型号刚失败过 → 跟实际对上:现在用它会落付费
            _st = bz[1]
            _rsn = ("免费档当前过载(高峰),暂走付费" if _st == 503
                    else "免费额度限流,暂走付费" if _st in (429, 403)
                    else f"免费档暂不可用(HTTP {_st}),走付费")
            gemini_status[m] = {"free": False, "reason": _rsn, "retry": int(_FREE_BUSY_TTL - (_now - bz[0]))}
        else:
            gemini_status[m] = {"free": True, "reason": "", "retry": 0}
    return jsonify({"ok": True, "actions": actions,
                    "names": {"orchestrator": "编排 agent(根:分配+回答)", "summarize": "章节总结", "vision": "看图",
                              **_AP_LABELS},
                    "catalog": {
                        "backends": list(_BACKENDS),
                        # 按任务限制可选后端:deep=relay 只透传 claude 选型(编排 ㉖ 起三后端全通:claude/gemini/codex)
                        "backends_by_action": {"deep": ["claude", "gemini", "codex"], "img_norm": ["gemini"],
                                               "web_search": ["gemini"], "route_text": ["gemini"]},
                        "variants": {"claude": list(_CLAUDE_VARIANTS), "gemini": gmods, "codex": list(_CODEX_VARIANTS)},
                        "depths": {"claude": ["auto"] + list(_EFFORTS), "gemini": ["none", "think"], "codex": list(_CODEX_DEPTHS)},
                        "variant_short": vshort,
                        "gemini_status": gemini_status,   # {型号:{free,reason,retry秒[,paid_only]}} → 前端标「免费 / 付费(原因)/ 💰仅付费」
                        "gemini_paid_only": sorted(m for m in gmods if _is_paid_only(m)),   # 仅付费型号清单(前端标💰)
                    },
                    "locked": {}})   # 二期已放开:根 agent 可跑 Gemini 工具循环


@bp.route("/prewarm", methods=["POST"])
def assistant_prewarm():
    if not _logged_in():
        return jsonify({"ok": False}), 401
    off = bool((request.get_json(silent=True) or {}).get("off"))
    threading.Thread(target=(_warm_reap if off else _warm_prewarm), daemon=True).start()
    return jsonify({"ok": True})


def register_assistant(app):
    app.register_blueprint(bp)
