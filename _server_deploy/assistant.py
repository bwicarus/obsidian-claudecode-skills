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

def _probe_free_batch(models):
    """并发探测一批"未验证"型号的免费档支持情况(只探 unknown 的;已知 ok/no 跳过)。面板首次打开时调,之后命中缓存=0 请求。"""
    todo = [m for m in (models or []) if _free_state(m) == "unknown"]
    if not todo or time.time() < _gemini_off.get("free", 0):
        return
    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(8, len(todo))) as ex:
            list(ex.map(_probe_free, todo))
    except Exception:
        for m in todo:
            _probe_free(m)

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

def _gemini_vision(prompt, images, max_tokens=1500, timeout=90, model=None):
    """Gemini 看图出文字描述。免费 key 优先,**任何失败(限流/5xx/网络/不支持)都自动切付费**。images=[{media_type,b64}]。付费也失败/空 → None(才回退 Claude)。"""
    if not images:
        return None
    keys = _gemini_keys(model or _GEMINI_MODEL)          # '@paid' 后缀在 _gemini_keys 内消化(跳过 free)
    mdl = _variant_paid(model or _GEMINI_MODEL)[0]       # URL/记账/标记一律用裸型号
    if not keys:
        return None
    parts = [{"text": prompt}]
    for v in images[:3]:
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
        if meta:   # 记每轮所在位置(书/页/选中句/用过的图)+ 助手回答的调用轨迹 trace + 搜到的视频,让历史回看也能显示上下文卡片 / 感叹号步骤 / 视频卡
            for k in ("page", "pages", "book", "file_rel", "selection", "figures", "trace", "videos", "undo_cards", "via"):   # via='mcp':外部编排 agent 写入的对话(侧栏可标来源)
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
            txt = (doc[idx].get_text("text") or "").strip()
            # 插入页 overlay 未同步 → PDF 那页空白/旧,用 sidecar md 补真源(设计 v4 批次2 评审 major)
            supp = _overlay_md_for_page(rel, idx + 1)
            if supp:
                txt = (supp + ("\n\n" + txt if txt else "")).strip()
            return txt[:4000]
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
    params = {"text": text}
    img = (args.get("image_url") or "").strip()
    if img:
        params["image_url"] = img   # 刚 search_image 过、这张图也该进卡片 → 一路透传到 _run_snippets_to 真下载存进 Anki 媒体库
    res = _bg_task("anki", params, ctx)
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
                              "query": (it.get("query") or it.get("concept") or "").strip()})
            elif isinstance(it, str) and it.strip():
                items.append({"concept": it.strip(), "query": it.strip()})
    else:
        q = (args.get("query") or ctx.get("selection") or "").strip()
        if q:
            items.append({"concept": q, "query": q})
    if not items:
        return {"error": "缺 queries(要配图的概念 + 关键词列表)"}

    def _one(it):
        try:
            imgs = image_search.search_images(it["query"][:120], n=1)   # 每概念取最匹配 1 张
        except Exception:
            imgs = []
        return {"concept": it["concept"], "found": bool(imgs),
                "image_url": (imgs[0]["image_url"] if imgs else ""),
                "page_url": (imgs[0].get("page_url", "") if imgs else "")}
    with _cf.ThreadPoolExecutor(max_workers=min(6, len(items))) as ex:
        results = list(ex.map(_one, items))
    found = [r for r in results if r["found"]]
    if not found:
        return {"error": "这些关键词都没搜到合适的真实图片(换更通用的英文关键词,或放弃配图)"}
    return {"ok": True, "count": len(found),
            "images": [{"concept": r["concept"], "image_url": r["image_url"], "page_url": r["page_url"]} for r in found],
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
            "client_action": {"fn": "renderVideos", "args": [vids]},
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
        if ctx.get("_want_vision"):   # ㉗:调用方要原图直喂(GPT Realtime 图像输入)→ 不本地转述,图+看图提示一起穿透
            return {"_vision": vis, "看图提示": note, "rendered_pages": done, "inked_pages": inked,
                    "说明": "页面渲染图已直接发给你,结合看图提示自己看图回答"}
        desc = _vision_for(ctx, vis, note)   # 一次性看图 → 返回文字;主循环不背图
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
        try:   # sidecar 回退:调用方没带实时墨迹(语音壳刚重连/侧栏特殊路径)→ 读服务端存档(与 _sys_prompt 同语义)
            import pdf_reader as _pdfm0
            strokes = _pdfm0._page_ink_strokes(file_rel, page) or []
        except Exception:
            strokes = []
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
        if ctx.get("_want_vision"):   # ㉗:GPT Realtime 图像输入开 → 笔迹合成图直喂它自己看(不经视觉模型转述,更快更省一跳)
            return {"_vision": [{"media_type": "image/png", "b64": base64.b64encode(png).decode()}],
                    "看图提示": note, "说明": "笔迹区域合成图已直接发给你,结合看图提示自己看图回答"}
        desc = _vision_for(ctx, [{"media_type": "image/png", "b64": base64.b64encode(png).decode()}], note)
        return {"笔迹标注描述": desc or "(看图失败,可重试)"}
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
    if args.get("page"):   # agent 批量标注:指定在哪页高亮(AI 给的是印刷页 → _to_pdf 转 PDF 索引,否则有 offset 的书标错页)
        try:
            pages = [_to_pdf(ctx, args["page"])]
        except (TypeError, ValueError):
            pages = []
    else:
        pages = [int(p) for p in (ctx.get("pages") or ([ctx.get("page")] if ctx.get("page") else [])) if p]
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
                if not rects:
                    continue
                hid = "h_" + os.urandom(6).hex()
                nrects = [[round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2)] for r in rects]
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
        res = {"highlighted": len(ids), "missed": miss, "_created": created,
               # 自动弹「跳转+撤销/重做」卡片(系统在改动发生时生成,非 AI 生成)
               "client_action": {"fn": "_assistEdit", "args": [{"type": "highlight", "file": file_rel, "items": created}]},
               "_jump_page": (pages[0] if pages else None)}
        if ids:
            import voice
            res["undo_id"] = voice._undo_record("highlight", f"{len(ids)} 处高亮", {"file_rel": file_rel, "ids": ids}, owner=ctx.get("_uid"))
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
        db = _pdf()._hl_load(file_rel)
        hls = db.get("highlights", []) or []
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
        db = _pdf()._hl_load(file_rel)
        hls = db.get("highlights", []) or []
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
        pdf = _pdf()
        ap = pdf._safe_vault_path(file_rel)
        if not ap:
            return {"error": "书路径无效"}
        entries, source = pdf._effective_toc(ap, file_rel)   # page 已归一到印刷页(跟 AI/用户一致)
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
        prompt = ("下面是几页书的正文,每页以【页N】开头。请**逐页**挑出每页 **1~3 句最重要**的(定义/核心结论/关键公式/易错点),"
                  "**逐字照抄原文**(不改写/不翻译/不合并/不跨段)。返回一个 JSON 对象 {\"页N\":[\"原句1\",\"原句2\"], ...};只输出 JSON,别加别的。\n\n"
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
        hr = _t_highlight({"texts": sents, "page": disp, "color": color}, ctx) or {}
        n = int(hr.get("highlighted") or 0)
        total += n
        created.extend(hr.get("_created") or [])   # 汇总各页高亮 → 一张「跳转+撤销/重做」卡片
        reports.append({"page": disp, "n": n, "sentences": [s[:24] for s in sents]})
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
    notes = _pdf()._notes_load(file_rel)
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
    n = next((x for x in _pdf()._notes_load(file_rel) if x.get("id") == nid), None)
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
        res["client_action"] = {"fn": "_assistEdit", "args": [{
            "type": "note", "op": "create", "file": file_rel,
            "items": [{"id": n["id"], "pdf_page": anchor["page"], "disp_page": _to_disp(ctx, anchor["page"]),
                       "note": n}]}]}
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


TOOLS = {
    "read_page": ("读当前页(或指定页)正文。args {page?}", _t_read_page),
    "read_selection": ("读用户当前选中的文字。args {}", _t_read_selection),
    "search_book": ("在当前这本书全文搜关键词,返回命中页+片段。args {query}", _t_search_book),
    "search_all_books": ("跨『我所有的书』全文搜索(用户问『哪本书讲过X/别的书有没有X/之前在哪见过』时用;只搜书库,不是联网搜索)。args {query 必填=要搜的关键词}", _t_search_all_books),
    "recall_notes": ("**召回用户自己学过/记过的**相关内容:知识索引(带摘要)+ vault 笔记全文 + 知识图谱**已学**节点 + Anki 卡(本地查不耗时)。"
                     "想把当前内容跟『他已学过/记过的』串起来、用户问『我之前记过吗/我笔记里有没有X/跟我学的Y有关吗』、或要结合他知识体系深入讲时用。"
                     "**注意:只有召回到的才算他学过**(图谱里没学的节点不会返回);没召回到就别假设他会。args {query:主题词}(不传用选中/焦点)", _t_recall_notes),
    "open_book": ("打开另一本书并可定位到页(跨书跳转)。args {file_rel | book(书名), page?}", _t_open_book),
    "summarize_section": ("取当前页所在『整章/整节』正文交给你总结(read_page 只逐页,『总结这一章』用这个)。args {page?}", _t_summarize_section),
    "translate": ("翻译文字成中文(或 target 语言)。不传 text 则译选中/本页。args {text?, target?}", _t_translate),
    "goto_page": ("翻到指定页(前端跳转)。args {page}", _t_goto_page),
    "make_anki": ("把内容做成 Anki 卡(后台,带原文链接,完成通知)。args {text?, image_url?}"
                 "(不传 text 用选中;image_url 若刚 search_image 过、这张图也该进卡片,把同一个 image_url 传进来——"
                 "会真下载存进 Anki 媒体库、只贴进本次生成的第一张卡,不是外链)", _t_make_anki),
    "make_note": ("把内容整理成 Obsidian 笔记(后台)。args {text?}(不传用选中/本页)", _t_make_note),
    "add_vocab": ("把英文单词加生词本并制卡(后台)。args {word?}(不传用选中)", _t_add_vocab),
    "search_image": ("★配图专用(搜**真实图片**,非 AI 生成;多源 Wikimedia Commons + Google 图搜)。**用户开了配图偏好时**,"
                     "先想清楚这次回答里**哪些概念配图真有帮助**(有明确视觉形象的:实物/结构/示意图/图表/生物/文物/天体/仪器等),"
                     "**一次性**把它们连关键词一起传:args {queries:[{concept:\"中文概念\", query:\"english keyword\"}, ...]}"
                     "(query **用英文**图源覆盖最好;一次最多 8 个)。工具会并行搜、每个概念返回最匹配 1 张。"
                     "拿回结果后:对 images 里每张,在回答对应概念旁用 markdown ![简短中文说明](image_url) 插入;missed 里没搜到的**别硬配、别自己编链接**。"
                     "别对『力/能量』这类无固定形象的抽象词硬配。刚好要制卡也想放这张图,把该 image_url 传给 make_anki。", _t_search_image),
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
    return {"read_page": "读取页面", "read_selection": "读取选中", "search_book": "搜索全书",
            "search_all_books": "跨书搜索", "open_book": "打开书", "summarize_section": "总结本章",
            "translate": "翻译", "goto_page": "翻页", "make_anki": "制卡", "make_note": "整理笔记",
            "add_vocab": "加生词本", "highlight": "高亮", "auto_highlight": "自动标重点(逐页外包)", "read_highlights": "看高亮", "find_highlights": "列出可删高亮", "toc": "查目录", "page_vocab": "查掌握度",
            "lookup_word": "查词典", "see_page": "看页面图", "see_figure": "看这张图", "see_ink": "看笔迹标注",
            "notes_query": "查便签", "notes_read": "读便签", "notes_create": "新建便签", "notes_edit": "修改便签",
            "recall_notes": "召回我的笔记", "undo_last": "撤销", "search_image": "配图搜索", "search_video": "找视频"}.get(name, name)


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
        "★用户说『讲讲这里/这段/当前/这部分/这里的内容』且系统已给了『★用户此刻屏幕上正在看的部分』→ **直接基于那段可见文字讲解**,"
        "**别调 read_section/summarize_section 去读整节/整章**(EPUB 一节=整章,读了会答成全章总结、跑偏用户真正在看的点);"
        "只有用户明确说『这一节/这一章/总结本章/整节』才读整节。找视频/配图的搜索词也**紧扣这段可见内容**,别用章节泛主题。\n"
        "★【追问建议】每次给最终回答时,在正文最后**另起一行**写 2-3 个贴合当前内容、能推进理解的下一步问题,"
        "格式就一行:[[FOLLOWUP]]问题1|问题2|问题3(用 | 分隔,放在整条回答末尾,前端会渲成可点按钮;问题要短、具体)。"
        "**每条最终回答都要带**;只有在调工具(输出 JSON)那几条里不要带。\n\n"
        f"【可用工具】\n{cat}\n\n"
        f"【当前页面】{json.dumps(meta, ensure_ascii=False)}{sel_line}{fig_line}{note_line}{learn_line}"
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
        return base
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
_AP_ACTIONS = ("orchestrator", "summarize", "vision", "deep", "explain", "translate", "dict", "grammar", "pick_video")
# 各 action 出厂默认(无用户预设时 _resolve 回退到这)。depth='auto'(仅 orchestrator)= 按问题自动路由 effort。
_AP_DEFAULTS = {
    "orchestrator": {"backend": "claude", "variant": "sonnet",            "depth": "auto"},
    "deep":         {"backend": "claude", "variant": "opus",              "depth": "high"},   # 语音通话 deep_think 虚拟工具用
    "summarize":    {"backend": "gemini", "variant": "gemini-3.5-flash",  "depth": "think"},
    "vision":       {"backend": "gemini", "variant": "gemini-3.5-flash",  "depth": "think"},
    "explain":      {"backend": "gemini", "variant": "gemini-3.5-flash",  "depth": "think"},
    "translate":    {"backend": "gemini", "variant": "gemini-3.5-flash",  "depth": "none"},
    "dict":         {"backend": "gemini", "variant": "gemini-3.5-flash",  "depth": "think"},
    "grammar":      {"backend": "gemini", "variant": "gemini-3.5-flash",  "depth": "think"},   # 2026-07 从 explain 拆出
    "pick_video":   {"backend": "gemini", "variant": "gemini-3.5-flash",  "depth": "none"},   # 找视频:拟搜索词 + 搜后按相关性筛选(便宜 flash 够用)
}
_AP_LABELS = {   # 设置面板给每个阅读器 action 显示的中文名
    "deep": "深度思考(语音通话专用)",
    "explain": "解释 / 问 AI / 选中查询", "translate": "翻译 / 例句", "dict": "字典 AI / 日语深入讲解",
    "grammar": "语法分析(长句结构 / 语法点)", "pick_video": "找视频(拟搜索词 + 相关性筛选)",
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


def reader_vision(images, prompt, action="vision", uid="", system=None, timeout=120):
    """PDF 阅读器看图/裁图 OCR 的统一入口:**脱壳** claude + Gemini 看图,按「看图」预设 + 互为兜底。
    images=[{media_type,b64}];system 可自定义(OCR 要 LaTeX、目录要抽章节…),传 '' 则不加系统提示。
    返回文本或 None。统一了原先直调 claude CLI(没脱壳,白加载 CLAUDE.md + 工具 schema)的看图/OCR。"""
    if not images:
        return None
    r = _resolve(action, uid)
    if _paid_recover_check(uid, action):   # @paid 且免费恢复 → 预设已摘除,重读让本次就用免费(静默)
        r = _resolve(action, uid)
    sysmsg = _VIS_SYS if system is None else system

    def via_gemini():
        return _gemini_vision((sysmsg + "\n" + prompt) if sysmsg else prompt, images,
                              timeout=min(timeout, 100),
                              model=(r["variant"] if _is_gemini(r["variant"]) else None))

    def via_claude():
        p = _spawn(effort=(r["depth"] if r["depth"] in _EFFORTS else "low"),
                   model=(r["variant"] if r["variant"] in _CLAUDE_VARIANTS else "sonnet"),
                   system=(sysmsg or None))
        if not p:
            return None
        try:
            blocks = [{"type": "text", "text": prompt}]
            for v in images[:6]:
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


def _agent_run(message, ctx, history, force_effort=None, force_model=None):
    """生成 SSE 事件 dict:{event, data}。event ∈ tool|tool-done|answer|actions|trace|error。"""
    _tok_reset()   # 本轮 token 计数清零(本线程内,后续所有 AI 调用累加,收尾写 trace[0].tok)
    uid = (ctx or {}).get("_uid")
    fe = force_effort if force_effort in _EFFORTS else None   # 感叹号「更强重答」强制档(一次性)
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
                _t_tool0 = time.time()
                try:
                    res = TOOLS[name][1](targs, ctx) or {}
                except Exception as e:
                    res = {"error": str(e)[:160]}
                _tool_sec = round(time.time() - _t_tool0, 1)   # 这步耗时(感叹号弹窗显示)
                vision = res.pop("_vision", None) if isinstance(res, dict) else None   # 图片喂回大脑(sonnet 多模态)
                _gm = res.pop("_gen_model", None) if isinstance(res, dict) else None   # 生成步工具用的强模型(如 summarize=opus)
                _ga = res.pop("_gen_action", None) if isinstance(res, dict) else None   # 生成步的动作键(可在 ⚙ 里调它的预设)
                trace.append({"label": _tool_label(name, targs), "model": _gm or "—", "sec": _tool_sec, "action": _ga,
                              "detail": _step_detail(res)})   # 轨迹:任务名+模型+耗时(+动作键)+ 该步完整内容(感叹号里点开看)
                for _ss in ((res.pop("_sub_steps", None) or []) if isinstance(res, dict) else []):   # 工具内部子步骤(如找视频的相关性筛选)各占「!」一行
                    trace.append({"label": _ss.get("label", ""), "model": _ss.get("model", "—"), "sec": _ss.get("sec"),
                                  "action": _ss.get("action"), "detail": _ss.get("detail", "")})
                if isinstance(res, dict) and res.get("client_action"):
                    yield {"event": "actions", "data": [res.pop("client_action")]}   # 实时:工具一执行完就推给前端应用,不等全部输出完
                if isinstance(res, dict) and res.get("task_id"):   # 后台写任务 → 前端轮询完成+给撤销按钮
                    yield {"event": "task", "data": {"task_id": res["task_id"], "label": _tool_label(name, targs)}}
                if isinstance(res, dict) and res.get("undo_id"):   # 同步写操作(高亮)→ 立即给撤销按钮
                    yield {"event": "undo", "data": {"undo_id": res["undo_id"], "label": _tool_label(name, targs), "page": res.pop("_jump_page", None) or (ctx.get("pages") or [ctx.get("page")] or [None])[0]}}
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
                _tools_ran = True
                name = tool["tool"]
                targs = tool.get("args") if isinstance(tool.get("args"), dict) else {}
                yield {"event": "tool", "data": _tool_label(name, targs)}
                _t_tool0 = time.time()
                try:
                    res = TOOLS[name][1](targs, ctx) or {}
                except Exception as e:
                    res = {"error": str(e)[:160]}
                _tool_sec = round(time.time() - _t_tool0, 1)
                vision = res.pop("_vision", None) if isinstance(res, dict) else None
                _gm = res.pop("_gen_model", None) if isinstance(res, dict) else None
                _ga = res.pop("_gen_action", None) if isinstance(res, dict) else None
                trace.append({"label": _tool_label(name, targs), "model": _gm or "—", "sec": _tool_sec, "action": _ga,
                              "detail": _step_detail(res)})
                if isinstance(res, dict) and res.get("client_action"):
                    yield {"event": "actions", "data": [res.pop("client_action")]}   # 实时:工具一执行完就推给前端应用,不等全部输出完
                if isinstance(res, dict) and res.get("task_id"):
                    yield {"event": "task", "data": {"task_id": res["task_id"], "label": _tool_label(name, targs)}}
                if isinstance(res, dict) and res.get("undo_id"):
                    yield {"event": "undo", "data": {"undo_id": res["undo_id"], "label": _tool_label(name, targs), "page": res.pop("_jump_page", None) or (ctx.get("pages") or [ctx.get("page")] or [None])[0]}}
                yield {"event": "tool-done", "data": _tool_label(name, targs)}
                feed = "【工具结果】" + json.dumps(res, ensure_ascii=False)[:6000] + "\n\n继续(调工具只输出 JSON,能答就直接答):"
                contents.append({"role": "model", "parts": [{"text": raw}]})
                uparts = [{"text": feed}]
                if vision:   # see_page 等:渲染图 inlineData 喂回(Gemini 多模态)
                    for v in vision[:2]:
                        uparts.append({"inlineData": {"mimeType": v.get("media_type", "image/png"), "data": v["b64"]}})
                contents.append({"role": "user", "parts": uparts})
                _compact_gemini_contents(contents)   # 压缩较早工具结果(只留最近2个全文 + 丢老图)→ 上下文不随步数线性膨胀
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
                _tools_ran = True
                name = tool["tool"]
                targs = tool.get("args") if isinstance(tool.get("args"), dict) else {}
                yield {"event": "tool", "data": _tool_label(name, targs)}
                _t_tool0 = time.time()
                try:
                    res = TOOLS[name][1](targs, ctx) or {}
                except Exception as e:
                    res = {"error": str(e)[:160]}
                _tool_sec = round(time.time() - _t_tool0, 1)
                vision = res.pop("_vision", None) if isinstance(res, dict) else None
                _gm = res.pop("_gen_model", None) if isinstance(res, dict) else None
                _ga = res.pop("_gen_action", None) if isinstance(res, dict) else None
                trace.append({"label": _tool_label(name, targs), "model": _gm or "—", "sec": _tool_sec, "action": _ga,
                              "detail": _step_detail(res)})
                for _ss in ((res.pop("_sub_steps", None) or []) if isinstance(res, dict) else []):
                    trace.append({"label": _ss.get("label", ""), "model": _ss.get("model", "—"), "sec": _ss.get("sec"),
                                  "action": _ss.get("action"), "detail": _ss.get("detail", "")})
                if isinstance(res, dict) and res.get("client_action"):
                    yield {"event": "actions", "data": [res.pop("client_action")]}
                if isinstance(res, dict) and res.get("task_id"):
                    yield {"event": "task", "data": {"task_id": res["task_id"], "label": _tool_label(name, targs)}}
                if isinstance(res, dict) and res.get("undo_id"):
                    yield {"event": "undo", "data": {"undo_id": res["undo_id"], "label": _tool_label(name, targs), "page": res.pop("_jump_page", None) or (ctx.get("pages") or [ctx.get("page")] or [None])[0]}}
                yield {"event": "tool-done", "data": _tool_label(name, targs)}
                if vision:   # see_page 等出图:turn 输入的 localImage 在多轮语境未验证 → 稳妥先经视觉模型转文字喂回
                    try:
                        _vd = _vision_for(ctx, vision, note="(工具产出的页面/图像渲染,请完整转述内容供编排模型使用)")
                    except Exception as _e:
                        _vd = f"(看图失败:{str(_e)[:80]})"
                    if isinstance(res, dict):
                        res["图像内容(视觉模型转述)"] = (_vd or "")[:2200]
                nxt = "【工具结果】" + json.dumps(res, ensure_ascii=False)[:6000] + "\n\n继续(调工具只输出 JSON,能答就直接答):"
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
        if job.get("answer"):   # 不管客户端在不在,跑完就落库(断连也不丢;历史/感叹号/视频卡都用得上)
            _meta = {}
            if job.get("trace"):
                _meta["trace"] = job["trace"]
            if job.get("videos"):
                _meta["videos"] = job["videos"]
            if job.get("undo_cards"):
                _meta["undo_cards"] = job["undo_cards"]   # H2:高亮撤销卡持久化
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
            force_model = body.get("force_model") if body.get("force_model") in _AP_MODELS else None
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
        res = TOOLS[name][1](body.get("args") or {}, ctx) or {}
    except Exception as ex:
        res = {"error": f"{type(ex).__name__}: {str(ex)[:300]}"}
    return jsonify({"ok": "error" not in res, "tool": name, "result": res})


# 只读(无副作用)工具:语音壳可按「工具+参数+页面状态指纹」缓存结果,重复询问直接复用不再执行
# (写操作/页面动作绝不缓存——"再做一张卡"是合法语义)。挨着 TOOLS 放:加新工具时顺手归类。
VOICE_CACHEABLE_TOOLS = {
    "read_page", "read_selection", "search_book", "search_all_books", "recall_notes",
    "summarize_section", "translate", "see_page", "see_figure", "see_ink",
    "notes_query", "notes_read", "read_highlights", "find_highlights", "toc",
    "page_vocab", "lookup_word", "search_video", "search_image",
}


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
        res = TOOLS[name][1](targs, ctx) or {}
    except Exception as ex:
        res = {"error": f"{type(ex).__name__}: {str(ex)[:300]}"}
    # ㉜ 语音场景配图渲染:search_image 结果在语音链路(仅本端点)附 client_action → 前端图卡进侧栏对话流。
    #    文字助手不走此端点(它由模型在 markdown 回答里嵌图),互不干扰。
    if name == "search_image" and isinstance(res, dict) and res.get("images"):
        res["client_action"] = {"fn": "renderImages", "args": [res["images"]]}
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


def _rtc_cfg() -> dict:
    try:
        return json.loads(_RTC_CFG_PATH.read_text("utf-8"))
    except Exception:
        return {}


@bp.route("/rtc-session", methods=["POST"])
def assistant_rtc_session():
    """WebRTC 会话配置(instructions/tools/vad/voice,镜像 relay 的 WS 版构造;audio format 不带——媒体轨自动协商)。"""
    if not _logged_in():
        return jsonify({"ok": False}), 401
    body = request.get_json(silent=True) or {}
    file_rel = (body.get("file") or "").strip()
    try:
        page = int(body.get("page") or 0)
    except Exception:
        page = 0
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
    parts = [cfg.get("rt_instructions") or cfg.get("system_role") or
             "你是用户的学习伙伴,他在用自己搭的系统自学日语、英语和大学数学物理。",
             lang_line,
             "口语回答,默认两三句话说清,别铺开;用户要求展开才展开。"
             "你配了一套真实工具(function calling):看图细节/翻页/搜索/高亮/做卡片/查词等需要动手的事"
             "**直接调用工具**,拿到真实结果再回答;绝不口头宣称做了没做的事。"
             "你没有**通用网页搜索**能力(search_all_books 只搜他的书库),要网上实时信息就如实说没法查、凭记忆答先声明可能过时;"
             "但 search_image(搜真实图片)和 search_video(搜教学视频)是**真联网工具**——"
             "用户想看图片/照片/视频时**必须调用对应工具**,结果会自动显示在他的界面上。"
             "**绝不要自己输出 markdown 图片或链接占位符**(如 ![...](image_url))假装贴图——界面不会显示任何东西,那是错误行为。"
             "**手写/圈画铁律**:他提到『我写的/我画的/我圈的/帮我看看这个算式』时,永远**先调 see_ink 工具**;"
             "回答『看不到』或让他粘贴/截图都是错误行为。"
             "收到『笔迹已发生变化』的状态消息后,你旧的笔迹记忆即作废——之后他问『现在呢/看到了什么/有没有变化』"
             "这类跟进问题,同样先调 see_ink 重新看;没重新看就凭旧印象答『没有变化』是错误行为。"]
    if file_rel and page:
        try:
            import fitz
            _ap = _pdf()._safe_vault_path(file_rel)
            _d0 = fitz.open(str(_ap))
            _pt = (_d0[page - 1].get_text("text") or "").strip()
            _d0.close()
            if _pt:
                parts.append(f"用户此刻正在读《{file_rel.rsplit('/', 1)[-1]}》第 {page} 页,本页文字内容(直接可用):\n{_pt[:1500]}")
        except Exception:
            pass
    parts.append("页面实时状态(选中/手写笔迹)和翻页后的新页面内容会以 system 消息出现在对话里,永远以最新一条为准;"
                 "**状态消息只是记录,永远不要对它们本身做回应或主动评论**;"
                 "没有听到用户清晰说话时调 wait_for_user 安静结束回合,别自己找话说。")
    # 语音场景 description 覆盖:目录行是给文字助手写的,个别工具的"回答里用 markdown 嵌图"教学
    # 在语音里有毒(模型学着输出 ![..](image_url) 假图)——语音版结果由界面自动渲染,只需口头说明
    _vo = {"search_image": ("★配图/看图片专用:按关键词列表**联网搜真实图片**(Wikimedia Commons + Google 图搜,非 AI 生成)。"
                            "用户想看某物的图片/照片时调它:args {queries:[{concept:\"中文概念\", query:\"english keyword\"}, ...]}"
                            "(query 用英文图源覆盖最好,一次最多 8 个)。搜到的图会**自动显示在用户界面**,"
                            "你只需口头简短说明;没搜到就如实说,绝不编链接或输出 markdown 图片语法。")}
    tools = [{"type": "function", "name": n, "description": _vo.get(n, str(d))[:1024],
              "parameters": {"type": "object", "properties": {}, "additionalProperties": True}}
             for n, (d, _) in TOOLS.items()]
    tools.append({"type": "function", "name": "deep_think",
                  "description": "深度思考:复杂推理/长解答/需要更强模型时转交 Claude 深度回答,结果拿回来讲给用户。args {question:完整问题}",
                  "parameters": {"type": "object", "properties": {"question": {"type": "string"}},
                                 "required": ["question"], "additionalProperties": True}})
    tools.append({"type": "function", "name": "wait_for_user",
                  "description": "当最新音频是静音、背景噪声、等待音乐、电视声或明显不是在对你说话时调用:安静结束本轮、不要说任何话。",
                  "parameters": {"type": "object", "properties": {}, "additionalProperties": False}})
    sess = {"type": "realtime", "model": cfg.get("rt_model") or "gpt-realtime-2.1-mini",
            "output_modalities": ["audio"],
            "reasoning": {"effort": cfg.get("rt_effort") or "low"},
            "max_output_tokens": 2048,
            "instructions": "\n".join(parts),
            "audio": {"input": {"noise_reduction": {"type": "near_field"},
                                "turn_detection": {"type": "semantic_vad",
                                                   "eagerness": cfg.get("rt_eagerness") or "auto",
                                                   "create_response": True, "interrupt_response": True},
                                "transcription": ({"model": "gpt-realtime-whisper", "language": cfg["rt_lang"]}
                                                  if cfg.get("rt_lang") in ("zh", "ja", "en")
                                                  else {"model": "gpt-realtime-whisper"})},
                      "output": {"voice": cfg.get("rt_voice") or "marin"}},
            "tools": tools, "tool_choice": "auto", "parallel_tool_calls": False,
            "truncation": {"type": "retention_ratio", "retention_ratio": 0.8}}
    return jsonify({"ok": True, "session": sess, "model": sess["model"], "rt_image": bool(cfg.get("rt_image"))})


@bp.route("/rtc-call", methods=["POST"])
def assistant_rtc_call():
    """SDP 代理:浏览器 offer → OpenAI POST /v1/realtime/calls(标准 key 只在服务端)→ answer SDP 回浏览器。"""
    if not _logged_in():
        return jsonify({"ok": False}), 401
    body = request.get_json(silent=True) or {}
    sdp = body.get("sdp") or ""
    sess = body.get("session") or {}
    model = (sess.get("model") or body.get("model") or "gpt-realtime-2.1-mini").strip()
    try:
        key = json.loads(_RTC_KEY_PATH.read_text("utf-8")).get("api_key") or ""
    except Exception:
        key = ""
    if not key:
        return jsonify({"ok": False, "error": "缺 OpenAI 凭证(~/.config/openai-realtime.json)"}), 400
    if not sdp:
        return jsonify({"ok": False, "error": "缺 sdp"}), 400
    import requests as _rq
    try:
        r = _rq.post(f"https://api.openai.com/v1/realtime/calls?model={model}",
                     headers={"Authorization": f"Bearer {key}"},
                     files={"sdp": (None, sdp, "application/sdp"),
                            "session": (None, json.dumps(sess, ensure_ascii=False), "application/json")},
                     timeout=25)
        if r.status_code >= 300:
            return jsonify({"ok": False, "error": f"OpenAI {r.status_code}: {r.text[:300]}"}), 502
        # call_id 在 Location header(官方形态):sideband 注入大 payload(图像)要用它
        cid = (r.headers.get("Location") or "").rstrip("/").rsplit("/", 1)[-1]
        return jsonify({"ok": True, "sdp": r.text, "call_id": cid})
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)[:200]}), 502


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
            # 短窗收几帧确认没被拒(error 事件=注入失败,让调用方走 fallback)
            import time as _t
            t_end = _t.time() + 2.0
            while _t.time() < t_end:
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
                    return True   # 至少一张已确认落进对话
        return True
    except Exception as ex:
        print(f"[rtc-sideband] fail: {type(ex).__name__}: {str(ex)[:150]}", flush=True)
        return False


_RTC_USAGE_FILE = CLAUDE_DIR / "state" / "openai-usage.json"
_RTC_RATE = {"in_text": 0.60, "cached_text": 0.06, "out_text": 2.40,
             "in_audio": 10.0, "cached_audio": 0.30, "out_audio": 20.0, "in_image": 0.80}


@bp.route("/rtc-usage", methods=["POST"])
def assistant_rtc_usage():
    """WebRTC 版 usage 记账(response.done.usage 由前端转发;与 relay WS 版同一账本/口径)。"""
    if not _logged_in():
        return jsonify({"ok": False}), 401
    usage = request.get_json(silent=True) or {}
    try:
        itd = usage.get("input_token_details") or {}
        otd = usage.get("output_token_details") or {}
        ctd = itd.get("cached_tokens_details") or {}
        row = {"in_text": int(itd.get("text_tokens") or 0), "in_audio": int(itd.get("audio_tokens") or 0),
               "in_image": int(itd.get("image_tokens") or 0),
               "cached_text": int(ctd.get("text_tokens") or 0), "cached_audio": int(ctd.get("audio_tokens") or 0),
               "out_text": int(otd.get("text_tokens") or 0), "out_audio": int(otd.get("audio_tokens") or 0)}
        cost = ((row["in_text"] - row["cached_text"]) * _RTC_RATE["in_text"]
                + row["cached_text"] * _RTC_RATE["cached_text"]
                + (row["in_audio"] - row["cached_audio"]) * _RTC_RATE["in_audio"]
                + row["cached_audio"] * _RTC_RATE["cached_audio"]
                + row["in_image"] * _RTC_RATE["in_image"]
                + row["out_text"] * _RTC_RATE["out_text"]
                + row["out_audio"] * _RTC_RATE["out_audio"]) / 1_000_000.0
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
    n = 0
    for role, key in (("user", "user"), ("assistant", "assistant")):
        txt = (b.get(key) or "").strip()
        if txt:
            _convo_append(uid, role, txt[:8000], meta)
            n += 1
    try:
        import reader_events
        reader_events.publish("assistant-history", b.get("file") or "", uid)   # 侧栏开着可实时感知(未订阅则无害)
    except Exception:
        pass
    return jsonify({"ok": True, "appended": n})


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


_APF_PATH = CLAUDE_DIR / "state" / "assistant-pref-profiles.json"

# ── 语音通话(豆包 S2S)设置(v3-⑮):设置面板读写凭证文件的**非密钥白名单字段**——
#    api_key 绝不经此暴露;relay 每次 _creds() 现读,写完即生效(通话中由前端发 {type:"cfg"} 触发热更)。
_VOICE_CFG_PATH = Path("~/.config/doubao-voice.json").expanduser()
_VOICE_CFG_FIELDS = ("speaker", "speech_rate", "loudness_rate", "explicit_dialect",
                     "bot_name", "speaking_style", "system_role", "enable_music",
                     "end_smooth_window_ms", "tts_speaker", "tts_speech_rate", "tts_instruction", "recall_cutoff", "asr_v2",
                     "rt_engine", "rt_model", "rt_voice", "rt_effort", "rt_image", "rt_lang",
                     "rt_instructions", "rt_eagerness", "rt_full_duplex")


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
            return jsonify({"ok": True, "profiles": sorted(mine.keys())})
        b = request.get_json(silent=True) or {}
        op, name = b.get("op"), (b.get("name") or "").strip()[:20]
        if not name or op not in ("save", "apply", "delete"):
            return jsonify({"ok": False, "error": "bad op/name"}), 400
        try:
            ap = json.loads(_AP_PATH.read_text("utf-8")) if _AP_PATH.exists() else {}
        except Exception:
            ap = {}
        if op == "save":
            mine[name] = dict(ap.get(uid) or {})
        elif op == "delete":
            mine.pop(name, None)
        elif op == "apply":
            if name not in mine:
                return jsonify({"ok": False, "error": "no such profile"}), 404
            ap[uid] = dict(mine[name])
            _AP_PATH.parent.mkdir(parents=True, exist_ok=True)
            _AP_PATH.write_text(json.dumps(ap, ensure_ascii=False), "utf-8")
        allp[uid] = mine
        _APF_PATH.parent.mkdir(parents=True, exist_ok=True)
        _APF_PATH.write_text(json.dumps(allp, ensure_ascii=False), "utf-8")
        return jsonify({"ok": True, "profiles": sorted(mine.keys())})


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
    _probe_free_batch([m for m in gall if not _is_paid_only(m)])   # 已知仅付费的不用探
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
                        "backends_by_action": {"deep": ["claude"]},
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
