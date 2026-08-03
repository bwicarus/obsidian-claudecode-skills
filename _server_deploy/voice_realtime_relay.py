"""voice_realtime_relay.py — 豆包端到端实时语音(S2S)的浏览器中继。

为什么要中继:浏览器 WebSocket 发不了自定义 header(豆包要 X-Api-App-ID/Access-Key 等),
且豆包用自定义二进制协议 → 本服务两头翻译:

  浏览器 ws(127.0.0.1:8767,经 nginx /voice-rt 反代)
    上行:二进制帧 = PCM 16k int16 mono(20ms/包 640B,直接转 TaskRequest)
    下行:文本帧 = JSON {event, payload}(ASR 字幕/回复文本/状态);二进制帧 = PCM 24k int16(模型语音)
  豆包 wss openspeech.bytedance.com/api/v3/realtime/dialogue(官方二进制协议,见接入文档)

凭证:~/.config/doubao-voice.json  {"app_id": "...", "access_token": "..."}(火山语音控制台;
X-Api-App-Key 是文档给的固定值)。凭证缺失 → 服务照常监听,浏览器连上时收到 error 事件提示。
部署:systemd voice-rt.service(mcp-venv,websockets)。
"""
import asyncio
import base64
import difflib
import gzip
import hashlib
import hmac
import json
import re
import struct
import os
import sys
import time
import urllib.parse
import uuid
from pathlib import Path

import httpx
import websockets

DOUBAO_WSS = "wss://openspeech.bytedance.com/api/v3/realtime/dialogue"
# agent 模式(耳+嘴分离,大脑=侧栏助手):豆包流式 ASR + HTTP 单向流式 TTS(冒烟验证 2026-07-10:全链路通)
SAUC_WSS = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"          # 大模型流式识别
SAUC_RID = "volc.bigasr.sauc.duration"                                     # 流式识别 1.0,时长版计费
SAUC_RID_V2 = "volc.seedasr.sauc.duration"                                 # 流式识别 2.0(Doubao-Seed-ASR,2025-12:关键词召回+20%,1元/h;⚠控制台需先开通 2.0 商品,凭证 asr_v2 开关)
# ASR 固定热词(㉓,用户设计"固定的任务相关关键词"):对 agent 说的指令词认错最伤,恒注入 corpus.context
_ASR_TASK_WORDS = ["翻页", "下一页", "上一页", "跳到", "高亮", "做卡片", "制卡", "Anki", "生词", "查词",
                   "笔记", "总结", "翻译", "解释", "深度思考", "找视频", "配图", "收藏", "便签", "朗读",
                   "挂断", "撤销", "清空", "知识点", "振假名", "豆包"]
TTS_URL = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"    # NDJSON 流式合成(备用)
TTS_BIDI_WSS = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"    # 双向流式合成(1.0/moon 音色用:文本增量进,session 级韵律连贯)
TTS_UNI_WSS = "wss://openspeech.bytedance.com/api/v3/tts/unidirectional/stream"   # 单向流式(2.0/uranus 音色用:context_texts 语气指令+section_id 韵律)
TTS_RID = "volc.service_type.10029"
TTS_SPEAKER = "zh_female_shuangkuaisisi_moon_bigtts"   # ⚠ 10029 只认 moon_bigtts 系音色(vv_jupiter 是 S2S 专属)
FIXED_APP_KEY = "PlgvMymc7f3tQnJ6"   # 文档固定值
CRED_FILE = Path("~/.config/doubao-voice.json").expanduser()
LISTEN_HOST, LISTEN_PORT = "127.0.0.1", 8767
DEBUG = False   # 排障时开(350 句文本等)

# ── 书本上下文/工具桥:经 webapp HTTP(Bearer=mcp-webapp-token,与 MCP 服务器同一套)──
WEBAPP = "http://127.0.0.1:5000"
_TOKEN_FILE = Path("~/.config/mcp-webapp-token").expanduser()
DIALOG_ID_FILE = Path("/home/bwicarus/claude/state/doubao-dialog-id.txt")   # 跨通话记忆(服务端接续最近20轮)
USAGE_FILE = Path("/home/bwicarus/claude/state/doubao-usage.json")          # 154 UsageResponse 记账(v3-⑩ A)
ACK_PCM_DIR = Path("/home/bwicarus/claude/state/doubao-ack-pcm")            # 确认语 PCM 缓存(v3-⑪:首次豆包合成时录下,之后 relay 直接回放,零合成费)
ACK_PCM_DIR.mkdir(parents=True, exist_ok=True)
VOICE_LOG_DIR = Path("/home/bwicarus/claude/state/voice-log")               # 学习时间线全量落盘(v3-⑰:对话+翻页/选中/圈画/工具事件)
VOICE_LOG_DIR.mkdir(parents=True, exist_ok=True)
VOCAB_LOOKUP_LOG = Path("/home/bwicarus/claude/state/vocab-lookups.jsonl")  # 查词日志(vocab 系统写,ts+page+pdf 齐全,聚合时直接合并)


def _vlog(kind: str, **kw):
    """学习时间线事件落盘(按天 jsonl)。写原始事实、**不做任何过滤**——焦点判定(误触/路过)在
    读取聚合(_study_digest)时执行,规则可随时进化重算(用户设计:写时过滤=信息永久丢失)。"""
    try:
        kw.update({"ts": int(time.time()), "kind": kind})
        p = VOICE_LOG_DIR / (time.strftime("%Y-%m-%d") + ".jsonl")
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(kw, ensure_ascii=False) + "\n")
    except Exception as ex:
        sys.stderr.write(f"[voice-rt vlog] {ex}\n")


def _study_digest(span: str = "today") -> str:
    """学习时间线 → 按页聚合的叙事(recall_study 喂给大上下文模型)。焦点规则(用户设计,读取时执行):
      ① 页段时长 <5s → 整段丢弃——**连段内的选中/查词也算误触**;
      ② 丢弃后相邻同页段合并(A→B→A 且 B<5s 的交界抖动/路过自然消失);
      ③ 无操作的纯停留段:≥60s 算"在阅读"留一行,不足丢弃。
    另按时间窗合并查词日志(vocab-lookups.jsonl)。输出限 ~6000 字(超长保最近)。"""
    import datetime as dt
    today = dt.date.today()
    days = ([(today - dt.timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
            if span == "week" else [today.isoformat()])
    evs = []
    for d in days:
        p = VOICE_LOG_DIR / f"{d}.jsonl"
        if not p.exists():
            continue
        for line in p.read_text("utf-8").splitlines():
            try:
                evs.append(json.loads(line))
            except Exception:
                pass
    day0 = dt.datetime.combine(dt.date.fromisoformat(days[0]), dt.time())
    t0, t1 = int(day0.timestamp()), int(time.time())
    try:   # 查词日志合并(通话外的查词也进时间线)
        for line in VOCAB_LOOKUP_LOG.read_text("utf-8").splitlines()[-800:]:
            try:
                e = json.loads(line)
                if t0 <= int(e.get("ts", 0)) <= t1:
                    evs.append({"ts": int(e["ts"]), "kind": "dict", "text": e.get("word"),
                                "page": e.get("page"), "book": e.get("pdf")})
            except Exception:
                pass
    except Exception:
        pass
    cut = 0
    try:
        cut = int(_creds().get("recall_cutoff") or 0)   # 记忆起点(v3-⑰c):只统计此后记录;0=不限
    except Exception:
        pass
    if cut:
        evs = [e for e in evs if int(e.get("ts", 0)) >= cut]
    evs.sort(key=lambda e: e.get("ts", 0))
    if not evs:
        return ""
    segs, cur = [], None   # 按(书,页)切段
    for e in evs:
        pg, bk = e.get("page"), e.get("book") or ""
        if cur is None or pg != cur["page"] or (bk and cur["book"] and bk != cur["book"]):
            cur = {"page": pg, "book": bk or (cur["book"] if cur else ""), "t0": e["ts"], "items": []}
            segs.append(cur)
        if bk and not cur["book"]:
            cur["book"] = bk
        cur["items"].append(e)
    for i, sg in enumerate(segs):   # 段时长=到下一段开始,但封顶"最后事件+5分"(稀疏事件下人早走了,别把整夜算成停留)
        raw = (segs[i + 1]["t0"] - sg["t0"]) if i + 1 < len(segs) else (sg["items"][-1]["ts"] - sg["t0"] + 30)
        sg["dur"] = min(raw, sg["items"][-1]["ts"] - sg["t0"] + 300)
    kept = []
    for sg in segs:
        if sg["dur"] < 5:
            continue   # 误触/路过:整段丢,含段内操作(用户规则)
        if kept and kept[-1]["page"] == sg["page"] and kept[-1]["book"] == sg["book"]:
            kept[-1]["items"] += sg["items"]; kept[-1]["dur"] += sg["dur"]
        else:
            kept.append(sg)
    out = []
    for sg in kept:
        hh = time.strftime("%H:%M", time.localtime(sg["t0"]))
        bookname = (sg["book"] or "").rsplit("/", 1)[-1].rsplit(".", 1)[0]
        head = f"{hh}" + (f"《{bookname}》" if bookname else "") + (f"p{sg['page']}" if sg.get("page") else "")                + f"(约{max(1, sg['dur'] // 60)}分)"
        by = {}
        for it in sg["items"]:
            by.setdefault(it["kind"], []).append(it)
        parts = []
        if by.get("dict"):
            parts.append("查词:" + "、".join(dict.fromkeys(str(i.get("text")) for i in by["dict"] if i.get("text"))))
        if by.get("sel"):
            parts.append("选中:" + ";".join(f"「{str(i.get('text'))[:40]}」" for i in by["sel"][:3] if i.get("text")))
        if by.get("ink"):
            parts.append("有圈画")
        if by.get("tool"):
            parts.append("用了:" + "、".join(dict.fromkeys(str(i.get("label") or i.get("tool")) for i in by["tool"])))
        qs = [str(i.get("text") or "") for i in by.get("q", [])]
        ans = [str(i.get("text") or "") for i in by.get("a", [])]
        for j, q in enumerate(qs):
            a = ans[j] if j < len(ans) else ""
            parts.append(f"问:{q[:80]}" + (f" → 答:{a[:160]}" if a else ""))
        for a in ans[len(qs):]:
            parts.append(f"AI:{a[:160]}")
        if not parts:
            if sg["dur"] >= 60:
                parts = ["阅读"]
            else:
                continue   # 短纯停留:无信息量
        out.append(head + ":" + ";".join(parts))
    return "\n".join(out)[-6000:]

# ── S2S 用量记账(154 UsageResponse 每轮 token 用量)──
# 官方字段(文档 154 事件):input_text_tokens / input_audio_tokens / cached_text_tokens /
#   cached_audio_tokens / output_text_tokens / output_audio_tokens。
# 官方价格表(用户 2026-07-11 截图核对):输入-文本 10 / 输入-音频 80 / 输入-文本cached 5 /
#   输入-音频cached 5 / 输出-文本 80 / 输出-音频 300(元/M token)。
# 计价假设:cached 是 input 的子集(行业通例)→ 未命中部分按全价、命中部分按 5 元。
_USAGE_RATE = {"in_text": 10.0, "in_audio": 80.0, "cached_text": 5.0, "cached_audio": 5.0,
               "out_text": 80.0, "out_audio": 300.0}
_USAGE_KEYS = {"input_text_tokens": "in_text", "input_audio_tokens": "in_audio",
               "cached_text_tokens": "cached_text", "cached_audio_tokens": "cached_audio",
               "output_text_tokens": "out_text", "output_audio_tokens": "out_audio"}


def _usage_classify(pl: dict) -> dict:
    """154 payload → 各类 token。先按官方字段名精确取;全零再退回宽容子串扫(防字段名变更)。"""
    got = {v: 0 for v in _USAGE_KEYS.values()}
    u = pl.get("usage") if isinstance(pl.get("usage"), dict) else pl
    for k, name in _USAGE_KEYS.items():
        try:
            got[name] = int(u.get(k) or 0)
        except Exception:
            pass
    if any(got.values()):
        return got

    def _walk(d, path=""):   # 宽容兜底
        if not isinstance(d, dict):
            return
        for k, v in d.items():
            kp = (path + "_" + str(k)).lower()
            if isinstance(v, dict):
                _walk(v, kp)
            elif isinstance(v, (int, float)) and v and "token" in kp:
                if "cach" in kp:
                    got["cached_audio" if "audio" in kp else "cached_text"] += int(v)
                elif "input" in kp:
                    got["in_audio" if "audio" in kp else "in_text"] += int(v)
                elif "output" in kp:
                    got["out_audio" if "audio" in kp else "out_text"] += int(v)
    _walk(pl)
    return got


def _usage_cost(d: dict) -> float:
    """一段累计的估算花费(元):input 未命中按全价 + cached 按 5 元 + output 全价。"""
    it, ia = d.get("in_text", 0), d.get("in_audio", 0)
    ct, ca = d.get("cached_text", 0), d.get("cached_audio", 0)
    cost = (max(0, it - ct) * 10.0 + ct * 5.0 + max(0, ia - ca) * 80.0 + ca * 5.0
            + d.get("out_text", 0) * 80.0 + d.get("out_audio", 0) * 300.0)
    return round(cost / 1e6, 4)


def _log_usage(pl: dict):
    """154 → 按天累计到 USAGE_FILE(轮数/各类 token/估算花费)。relay 单进程 asyncio,直接读改写。"""
    try:
        got = _usage_classify(pl)
        if not any(got.values()):
            if DEBUG:
                sys.stderr.write(f"[voice-rt 154] 无 token 字段? {str(pl)[:200]}\n")
            return
        try:
            data = json.loads(USAGE_FILE.read_text("utf-8"))
        except Exception:
            data = {"days": {}}
        day = time.strftime("%Y-%m-%d")
        d = data["days"].setdefault(day, {"rounds": 0})
        d["rounds"] = d.get("rounds", 0) + 1
        for k, v in got.items():
            d[k] = d.get(k, 0) + v
        d["cached"] = d.get("cached_text", 0) + d.get("cached_audio", 0)   # 合计列(控制面板显示用)
        d["cost_est"] = _usage_cost(d)
        data["total_cost_est"] = round(sum(x.get("cost_est", 0) for x in data["days"].values()), 4)
        data["days"] = dict(sorted(data["days"].items())[-60:])   # 只留最近 60 天
        USAGE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    except Exception as ex:
        sys.stderr.write(f"[voice-rt usage] {ex}\n")


def _webapp_headers() -> dict:
    try:
        return {"Authorization": "Bearer " + _TOKEN_FILE.read_text().strip()}
    except Exception:
        return {}


async def _fetch_book_ctx(file_rel: str, page: int) -> dict:
    """拉「能直接塞」的纯文本上下文:页文本 + 圈画文字 + 插图离线描述 + 助手最近对话。
    (用户定调:现成纯文本不走 agent 中间层,直接进 S2S 的 system prompt。)失败静默降级。"""
    out = {"page_text": "", "inked": "", "figures": [], "history": []}
    try:
        async with httpx.AsyncClient(base_url=WEBAPP, headers=_webapp_headers(), timeout=10) as hc:
            if file_rel and page:
                r = await hc.get("/pdf/api/page-text", params={"file": file_rel, "page": page})
                d = r.json()
                if d.get("ok"):
                    out["page_text"] = (d.get("text") or "")[:1800]
                try:   # 圈画文字 + 图描述(端点新加;老 webapp 没有也不影响主流程)
                    r = await hc.get("/api/assistant/voice-ctx", params={"file": file_rel, "page": page})
                    d = r.json()
                    if d.get("ok"):
                        out["inked"] = d.get("inked") or ""
                        out["has_ink"] = bool(d.get("has_ink"))
                        out["figures"] = d.get("figures") or []
                        out["vocab"] = d.get("vocab") or []
                except Exception:
                    pass
                out.update(await _fetch_brief(file_rel, page))   # Phase2:本页简述(有则上层注要点替整页)
            r = await hc.get("/api/assistant/history")
            d = r.json()
            if d.get("ok"):
                out["history"] = d.get("messages") or []
    except Exception as ex:
        sys.stderr.write(f"[voice-rt ctx] {ex}\n")
    return out


async def _fetch_brief(file_rel: str, page: int) -> dict:
    """Phase2:拉本页 page-brief(HTTP;relay 是独立进程,不能直接读 sidecar)。
    有简述才返回 {brief,brief_tags,page_type};pending/skip无brief/失败 → {}(上层降级注整页)。短超时静默降级。"""
    if not (file_rel and page):
        return {}
    try:
        async with httpx.AsyncClient(base_url=WEBAPP, headers=_webapp_headers(), timeout=6) as hc:
            r = await hc.get("/pdf/api/page-brief", params={"file": file_rel, "page": page})
            d = r.json()
            if d.get("ok") and (d.get("brief") or "").strip():
                return {"brief": d.get("brief") or "", "brief_tags": d.get("tags") or [],
                        "page_type": d.get("page_type") or ""}
    except Exception:
        pass
    return {}


def _brief_line(book: dict) -> str:
    """book 里已缓存的简述 → 一行要点『本页要点:…(知识点:a、b)[页型:knowledge]』;无简述→""。
    简述优先注入的统一文案源(_role_text / _oa_instructions / grok 每轮 / 翻页增量 / rtc _inject_state 共用)。"""
    br = (book.get("brief") or "").strip()
    if not br:
        return ""
    s = "本页要点:" + br
    tags = book.get("brief_tags") or []
    if tags:
        s += "(知识点:" + "、".join(str(t) for t in tags[:6]) + ")"
    pt = (book.get("page_type") or "").strip()
    if pt:
        s += "[页型:" + pt + "]"
    return s


def _qa_pairs(msgs: list, max_pairs: int = 3) -> list:
    """助手历史 → dialog_context 的严格 user/assistant 交替 QA 对(文档要求完整对+偶数条)。"""
    out: list = []
    i = len(msgs) - 1
    while i >= 1 and len(out) < max_pairs * 2:
        a, u = msgs[i], msgs[i - 1]
        if a.get("role") == "assistant" and u.get("role") == "user":
            out = [{"role": "user", "text": str(u.get("content", ""))[:400]},
                   {"role": "assistant", "text": str(a.get("content", ""))[:400]}] + out
            i -= 2
        else:
            i -= 1
    return out

# ── 豆包二进制协议 ──────────────────────────────────────────────
# header: [0x11, type<<4|flags, serialization<<4|compression, 0x00]
# flags 0b0100=带 event(int32 BE);session 类事件再带 session_id(len+bytes);payload(len+bytes)
T_FULL_CLIENT, T_FULL_SERVER, T_AUDIO_CLIENT, T_AUDIO_SERVER, T_ERROR = 0b0001, 0b1001, 0b0010, 0b1011, 0b1111


def enc(msg_type: int, event: int, payload: bytes, session_id: str | None = None, raw: bool = False) -> bytes:
    b = bytearray([0x11, (msg_type << 4) | 0b0100, (0b0000 if raw else 0b0001) << 4 | 0b0000, 0x00])
    b += event.to_bytes(4, "big")
    if session_id is not None:
        sid = session_id.encode()
        b += len(sid).to_bytes(4, "big") + sid
    b += len(payload).to_bytes(4, "big") + payload
    return bytes(b)


def dec(frame: bytes) -> dict:
    """解豆包下行帧 → {type, event, code?, session_id?, payload(bytes)}。尽量宽容。"""
    out = {"type": None, "event": None, "payload": b""}
    try:
        if not isinstance(frame, (bytes, bytearray)) or len(frame) < 4:
            return out
        mt = frame[1] >> 4
        flags = frame[1] & 0x0F
        comp = frame[2] & 0x0F
        out["type"] = mt
        i = 4
        if mt == T_ERROR:   # 错误帧:先 code(4B)
            out["code"] = int.from_bytes(frame[i:i + 4], "big"); i += 4
        if flags & 0b0100:
            out["event"] = int.from_bytes(frame[i:i + 4], "big"); i += 4
        ev = out.get("event") or 0
        if out["event"] is not None and mt != T_ERROR:
            if ev in (50, 51, 52):   # Connect 类:connect id
                n = int.from_bytes(frame[i:i + 4], "big"); i += 4 + n
            else:                    # Session 类:session id
                n = int.from_bytes(frame[i:i + 4], "big"); i += 4
                out["session_id"] = frame[i:i + n].decode("utf-8", "ignore"); i += n
        n = int.from_bytes(frame[i:i + 4], "big"); i += 4
        pl = bytes(frame[i:i + n])
        if comp == 0b0001:
            import gzip
            pl = gzip.decompress(pl)
        out["payload"] = pl
    except Exception as ex:
        out["error"] = str(ex)
    return out


def _creds() -> dict:
    try:
        return json.loads(CRED_FILE.read_text("utf-8"))
    except Exception:
        return {}


# 语音模式工具白名单(治 OpenAI Realtime TPM 40000/min:全量目录 54 工具≈2675 token/轮,三四轮撞满连续失败)。
# 只保留陪读/答题真正用得到的高频工具,门控在 _role_text 里过滤 tools_lines→少 join 几十行。
# 砍掉的:KG/图谱/mastery 掌握度/diagnostic/situation 近况/material 素材/save_intent/run_saved_task/造纸多步原语等
# (语音里 AI 用不到、或不需要理解的系统内部机制)。⚠ 拿不准=留着(砍错=功能缺失),宁可多留几个。
VOICE_TOOLS_WHITELIST = {
    # 读页 / 内容
    "read_page", "read_selection", "toc", "summarize_section",
    # 查词 / 翻译
    "lookup_word", "translate",
    # 搜索(本书 + 跨书 + 联网三件套)
    "search_book", "search_all_books", "web_search", "search_image", "search_video",
    # 翻页
    "goto_page",
    # 看图 / 看笔迹(文字层读不到的)
    "see_page", "see_figure", "see_ink",
    # 高亮 / 做卡 / 记笔记
    "highlight", "make_anki", "make_note",
    # 语音专属虚拟工具(relay 拦截)
    "deep_think", "recall_study",
}


def _role_text(cfg: dict, book: dict | None, file_rel: str, page: int) -> str:
    """system_role 构造(StartSession 和翻页 UpdateConfig 共用一份逻辑)。
    直塞优先:页文本/圈画文字/插图描述这些现成纯文本直接进 prompt(直接答,零等待);
    「我去查」只留给真需要动手的(搜视频/查资料/别页/精细图像/写操作)。

    v3-⑭ 增量上下文(用户 transformer 洞察:SP 在序列最前,改 SP 尾部一个字=它后面**整个对话
    历史**的前缀缓存全失效——⑩的 SP 内分层救不了历史):笔迹/选中/图/任务等易变状态**整段撤出 SP**,
    改为对话末尾的『(系统状态更新:…)』增量事件(_inject_state,510 追加=前缀不变=历史缓存全保)。
    SP 只剩 ①角色+协议+全量目录(整场不变) ②页文本/插图/生词(翻页才变)→ **SP 只随翻页变**。
    "历史压 SP"定律在此反而是助力:状态写进最近历史,比 SP 快照更能压住旧对话里的过时说法。"""
    book = book or {}
    # ── ① 稳定前缀 ──
    role = cfg.get("system_role",
                   "你是用户的学习伙伴,他在用自己搭的系统自学日语、英语和大学数学物理。回答口语化、简洁自然。")
    role += ("\n你接在这本阅读器的工具层后面(目录在最后)。规则:"
             "\n- 下面直接给你的内容(本页文字/生词/选中/插图描述)能答就直接答,别调工具。"
             "\n- 要调工具时**整条回复只输出一条 JSON**:{\"tool\":\"工具名\",\"args\":{...}}——别加任何开场白;"
             "字符串值别用双引号、别换行(要引号用「」);每轮最多调一个工具。"
             "系统执行后会把真实结果发给你,那时再口语化讲给用户;**别自己编结果**。"
             "\n例:『翻到第8页』→ 完整回复就是 {\"tool\":\"goto_page\",\"args\":{\"page\":8}}。"
             "\n- 联网:web_search 查网上实时信息(额度有限省着用)、search_image 搜真实图片、search_video 搜教学视频;"
             "web_search 失败就如实说查不了,可凭记忆答但要先声明『可能过时』,别把记忆当成刚查到的结果。"
             "\n- 语音场景回答默认两三句说清,用户说『详细/展开』才展开。")
    lines = book.get("tools_lines") or {}
    # 工具精简暂缓(用户 2026-07-21:「工具精简这部分之后讨论或我手动去清理」)——恢复全量工具目录。
    # 要手动砍:改上面 VOICE_TOOLS_WHITELIST set,并在下面 if lines 块首行加回:
    #   lines = {k: v for k, v in lines.items() if k in VOICE_TOOLS_WHITELIST}
    if lines:
        role += ("\n可用工具目录(冒号后是用途,{}是 args 字段;下方实时状态说某工具当前无效时以状态为准):\n"
                 + "\n".join(lines.values()))
    # ── ② 中层:跟页走的内容(翻页才变) ──
    # ⚠ 总页数**不写进 SP**(用户拍板):SP 是开话快照、整场不改(改前缀=prompt cache 全废),
    #   一旦跨书就会拿着上一本的页数自信报错数。改由**工具结果**实时携带『全书总页数』。
    role += ("\n- 页码类(最后一页/一共多少页/还剩几页):别猜,goto_page 的 page 支持 last/first/+1/-1;"
             "书内工具(read_page/toc/search_book/goto_page)结果里带『全书总页数』,以它为准。"
             "\n- 问『第N页写了什么』=要内容,调 read_page(args{page:N});本页内容下面已直接给你,不用调工具。")
    page_text = book.get("page_text") or ""
    _brief_ln = _brief_line(book)   # Phase2:本页简述在手→注要点替整页(省 token);缺失才降级注整页
    _name = (file_rel.rsplit("/", 1)[-1] or "这本书")
    # (原此处有 `(全书 {_pc} 页)` 片段:_pc 从未定义=NameError,且与上文"总页数不写进 SP"的设计相悖 →
    #  借本次改写就地移除死代码,让简述/整页两条分支都不再崩)
    if _brief_ln:
        role += (f"\n用户此刻正在读《{_name}》第 {page} 页。{_brief_ln}"
                 "(以上是本页要点摘要;能答就直接答,需要本页完整原文/更细内容再调 read_page(page:N),别默认整页读)")
    elif page_text:
        role += f"\n用户此刻正在读《{_name}》第 {page} 页,本页文字内容(直接可用):\n{page_text}"
    figs = book.get("figures") or []
    if figs:
        fx = ";".join(f"「{(f.get('caption') or '插图')}」:{(f.get('desc') or '')}" for f in figs[:4] if isinstance(f, dict))
        if fx:
            role += f"\n本页插图的文字描述(问图直接用;要更精细的视觉细节才「我去查」):{fx[:1200]}"
    vocab = book.get("vocab") or []
    if vocab:
        role += ("\n本页『还没掌握』的生词(阅读器下划线词,来自他的生词本掌握度数据;"
                 "他问『这页哪些词我没掌握/有什么生词』直接用这个列表答):" + "、".join(vocab[:30]))
    # ── ③ 实时状态说明(稳定文案,状态本体走对话内增量事件——v3-⑭) ──
    role += ("\n——**页面实时状态**(用户选中了什么/手写笔迹/带入的图/后台任务)不写在这里,"
             "而是随时以『(系统状态更新:…)』的消息出现在**对话里**,**永远以最新一条为准**——"
             "最新一条说没有的东西就是没有;**对话里一条状态消息都没有=本页当前什么都没有**(无选中、无笔迹、无带入图)。"
             "状态消息说某工具此刻无效(如无笔迹时的 see_ink)就别调。")
    return role


def _state_event_text(book: dict) -> str:
    """增量状态事件文本(v3-⑭):合并快照,一条说清当前全部状态(选中/笔迹三态/图)。
    防抖平息后经 510 追加到对话末尾——前缀不变,历史缓存全保;"以最新一条为准"由 SP 说明段兜底。"""
    parts = []
    sel = (book.get("sel") or "").strip()
    parts.append(f"当前选中了「{sel[:200]}」(他说『这段/我选的』就指它)" if sel else "当前没有选中任何文字")
    focus = (book.get("focus") or "").strip()
    if focus:
        parts.append(f"钉住的焦点内容:「{focus[:150]}」")
    if book.get("figs_n"):
        parts.append(f"带入了 {book['figs_n']} 张图(看图内容调 see_figure)")
    else:
        parts.append("没带入图(see_figure 此刻无效)")
    if book.get("has_ink"):
        iv, sv = book.get("ink_ver", 0), book.get("ink_seen_ver", 0)
        if sv and iv > sv:
            parts.append("本页有手写笔迹,且**你上次查看之后又画了新的、你还没看过**——没看就描述=编造;"
                         "问笔迹相关时你唯一正确的回复是 {\"tool\":\"see_ink\",\"args\":{}}")
        elif sv:
            parts.append("本页有手写笔迹,自你上次查看后没有新变化(但他说刚画了就以他为准,重新 see_ink)")
        else:
            parts.append("本页有手写笔迹——他只要用『这个/这里/这段/这是什么/我圈的/什么意思』等**代词或指代不清**的说法,"
                         "或没说具体指什么 → 默认就在问他圈画的那块,**必须先 see_ink** 看合成图再答;别猜、别要求他明说『圈出/画出』")
    else:
        parts.append("本页没有任何手写笔迹(问『我画了什么』直接说没画,see_ink 此刻无效)")
    return "(系统状态更新:" + ";".join(parts) + "。以本条为准,更早的状态描述一律作废。)"


def _tts_cfg(cfg: dict, full: bool = False) -> dict:
    """tts 配置(v3-⑮ 设置面板可调):音色/语速/音量/方言。full=True 含音频格式(StartSession 用);
    UpdateConfig 只带 speaker+audio_config(官方 201 schema)。⚠ tts.extra 置空会报 42000020 → 没方言不带 extra 键。"""
    t = {"speaker": cfg.get("speaker", "zh_female_vv_jupiter_bigtts"),
         "audio_config": {"speech_rate": int(cfg.get("speech_rate", 0)),        # [-50,100] 语速
                          "loudness_rate": int(cfg.get("loudness_rate", 0))}}   # [-50,100] 音量
    if full:
        # PCM s16le 24k:浏览器 Web Audio 直接播,免 opus 解码
        t["audio_config"].update({"channel": 1, "format": "pcm_s16le", "sample_rate": 24000})
        if cfg.get("explicit_dialect"):   # 方言(dongbei/sichuan/shaanxi,仅 2.0 vv 音色生效)
            t["extra"] = {"explicit_dialect": cfg["explicit_dialect"]}
    return t


def _start_session_payload(book: dict | None = None, file_rel: str = "", page: int = 0,
                           fresh: bool = False) -> dict:
    """fresh=True(浮层 🧹 新话题):清 dialog_id 文件 + 不带上次会话/助手历史 → 彻底空白记忆,只留当前书页。"""
    cfg = _creds()   # 可在凭证文件里带可选覆盖(speaker/system_role/bot_name)
    role = _role_text(cfg, book, file_rel, page)
    asr_extra: dict = {
        "end_smooth_window_ms": int(cfg.get("end_smooth_window_ms", 800)),
        "enable_asr_twopass": True,   # 二遍识别(流式先出字+非流式提准):直接提升识别准确率(26.01.15 能力)
    }
    hotwords = list(cfg.get("asr_hotwords") or [])   # 凭证可配长期热词;书名默认加入(当页专业词后续接 KG 词表)
    if file_rel:
        name = file_rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if name:
            hotwords.append(name)
    if hotwords:
        asr_extra["context"] = {"hotwords": [{"word": w} for w in hotwords[:50]]}
    payload = {
        "asr": {"extra": asr_extra},
        "dialog": {
            "bot_name": cfg.get("bot_name", "豆包"),
            "system_role": role,
            "speaking_style": cfg.get("speaking_style", "语气自然友好,不啰嗦。"),
            "extra": {"input_mod": "keep_alive",   # 浏览器切后台/静音时不因音频断流报错
                      "enable_user_query_exit": True,   # 用户说"挂了吧/再见"→ TTSEnded 带 20000002 → 前端自动挂断
                      "model": cfg.get("model", "1.2.1.1")},   # O2.0
        },
        "tts": _tts_cfg(cfg, full=True),
    }
    if cfg.get("enable_music"):
        payload["dialog"]["extra"]["enable_music"] = True   # 唱歌能力(检索版权曲库,仅 1.2.1.1)
    if fresh:
        try:
            DIALOG_ID_FILE.unlink()
        except Exception:
            pass
        return payload
    try:   # 跨通话记忆:带上次 dialog_id(服务端接续最近 20 轮)
        did = DIALOG_ID_FILE.read_text().strip()
        if did:
            payload["dialog"]["dialog_id"] = did
    except Exception:
        pass
    if book and book.get("history"):
        pairs = _qa_pairs(book["history"])
        if pairs:
            payload["dialog"]["dialog_context"] = pairs   # 阅读器助手最近几轮 → 接起电话就知道之前聊到哪
    return payload


# ── 工具协议 v3(S2S 编排化,用户定调):S2S 直接输出**与编排 agent 完全同一套** JSON 工具调用
#    {"tool":"名","args":{...}}(独立成句;350/351 句级静音让用户听不到机器格式)。relay 550 增量检测
#    JSON 完整即触发,559 兜底把整轮丢给 /api/assistant/voice-tool(服务端 _parse_tool 顽强解析+修补+dispatch,
#    与编排 agent 物理同源,升级永远同步)。结果三路:client_action→页面 / 文本→ChatRAGText 回 S2S 播报 /
#    完整调用信息→tool_status 事件(前端状态按钮)。
_TOOL_START = re.compile(r'\{\s*"tool"')
# 工具确认语(relay 经 ChatTTSText(500) 指定文本让豆包播——音频完全可控,不存在"截断自然话"问题;
# 协议已改成编排 agent 同款纪律:调工具时整条回复只有 JSON,确认语由这里代播):
_ACK_TEXT = {   # v3-⑩:确认语按输出音频计费(300元/M,最贵的一类)→ 能短则短
    "search_video": "我找找。", "search_image": "我找图。",
    "see_ink": "我看看。", "see_page": "我看下这页。", "see_figure": "我看下图。",
    "goto_page": "好。", "make_anki": "好,做卡片。", "make_note": "好,记笔记。",
    "summarize_section": "我读下这章。", "auto_highlight": "好,标重点。",
    "deep_think": "我想想,稍等。", "recall_study": "我翻翻记录。",
}
# 深度思考/学习回顾是语音专属虚拟工具，由 assistant.ToolRegistry 声明并由 relay
# 拦截执行；这里不再保存第二份名称、说明或 schema。
_SENT_SPLIT = re.compile(r"[^。！？!?;；\n]+[。！？!?;；\n]+")


def _speech_clean(s: str) -> str:
    """markdown → 可朗读文本(粗清,与前端 cleanForSpeech 同思路)。"""
    s = re.sub(r"```[\s\S]*?```", " 代码略 ", s)
    s = re.sub(r"\$\$?([^$]{1,120})\$\$?", r"\1", s)
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"[#*_`>|~]+", " ", s)
    s = re.sub(r"[(（]\s*(?:第\s*\d+\s*[-~至]?\s*\d*\s*页|p\.?\s*\d+)\s*[)）]", "", s, flags=re.I)   # 页码引用不念(显示保留)
    s = re.sub(r"[\[【]语气[::][^\]】]{0,12}[\]】]?", "", s)   # [语气:XX] 是 2.0 朗读引擎的控制符,bidi 代播链路念不了必须剥
    return re.sub(r"[ \t]+", " ", s)


async def _run_deep_think(bws, dws, sid, question: str, file_rel: str, page: int,
                          book: dict, push_sp=None, tool_name: str = "deep_think", tool_label: str = "深度思考",
                          preamble: str = "(语音深度解答,直接详细推理回答,少用工具;回答会被朗读:口语化短句,需要停顿处用省略号……,别用列表和标记符号)"):
    """深度思考:调助手 chat(SSE,深度模型可配)→ answer 增量攒句 → 500 分片流式代播。
    打断(450→book["deep_abort"])立即停发且**不发 end 包**(官方要求:播报被打断时别补 end)。"""
    cfg = _creds()
    book["deep_abort"] = False
    started, sent_len, tail = False, 0, ""

    async def _say(seg: str, first: bool):
        seg = _speech_clean(seg).strip()
        if not seg:
            return False
        pkt = {"start": first, "content": seg, "end": False}
        await dws.send(enc(T_FULL_CLIENT, 500, json.dumps(pkt, ensure_ascii=False).encode(), session_id=sid))
        await bws.send(json.dumps({"event": 550, "payload": {"content": seg}}, ensure_ascii=False))   # 字幕同步
        return True

    try:
        book.setdefault("tasks", {})["deep_think"] = 1
        if push_sp:
            await push_sp()
        await bws.send(json.dumps({"event": "tool_status", "payload": {"status": "running", "tool": "deep_think", "label": tool_label}}, ensure_ascii=False))
        body = {"message": f"{preamble}\n{question}",
                "rid": f"dt{uuid.uuid4().hex[:10]}", "voice": "s2s",   # 口语化 prompt 要,[语气:XX] 标签指令不要(bidi 代播不吃标签)
                "context": ({"file_rel": file_rel, "page": page} if file_rel else {})}
        # 深度模型选型:模型设置面板「深度思考」项(action-prefs deep,⑦的 UI)优先;凭证 deep_model/deep_effort 兜底
        dm, de = cfg.get("deep_model"), cfg.get("deep_effort")
        try:
            async with httpx.AsyncClient(base_url=WEBAPP, headers=_webapp_headers(), timeout=8) as hc0:
                r0 = await hc0.get("/api/assistant/action-prefs")
                pref = ((r0.json().get("actions") or {}).get("deep") or {}).get("pref") or {}
                if pref.get("backend"):   # 61b:deep 面板三后端全放行(chat 管线新 force_backend;旧版只认 claude)
                    body["force_backend"] = pref["backend"]
                    dm, de = pref.get("variant") or dm, pref.get("depth") or de
        except Exception:
            pass
        if dm:
            body["force_model"] = dm
        if de:
            body["force_effort"] = de
        answer = ""
        async with httpx.AsyncClient(base_url=WEBAPP, headers=_webapp_headers(), timeout=420) as hc:
            async with hc.stream("POST", "/api/assistant/chat", json=body) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"chat http {r.status_code}")
                ev, data = "", ""
                async for line in r.aiter_lines():
                    if book.get("deep_abort"):
                        break
                    if line.startswith("event:"):
                        ev = line[6:].strip()
                    elif line.startswith("data:"):
                        data += line[5:].strip()
                    elif not line.strip():
                        if ev or data:
                            try:
                                parsed = json.loads(data) if data else None
                            except Exception:
                                parsed = data
                            if ev == "answer" and isinstance(parsed, str):
                                answer = re.split(r"\n?FOLLOWUP[::]", parsed)[0]
                                if len(answer) < sent_len:
                                    sent_len = 0   # 编排器工具轮后 answer 从头重来 → 从新文本头接着念,别按旧偏移切空
                                tail += answer[sent_len:]
                                sent_len = len(answer)
                                consumed = 0
                                for m in _SENT_SPLIT.finditer(tail):   # 成句即播(边生成边念,不等全文)
                                    if await _say(m.group(0), not started):
                                        started = True
                                    consumed = m.end()
                                tail = tail[consumed:]
                            elif ev == "actions" and isinstance(parsed, list):
                                for a in parsed:
                                    if isinstance(a, dict) and a.get("fn"):
                                        await bws.send(json.dumps({"event": "client_action", "payload": a}, ensure_ascii=False))
                            elif ev == "done":
                                break
                            elif ev == "error":
                                answer = answer or f"深度思考出错:{str(parsed)[:80]}"
                        ev, data = "", ""
        if not book.get("deep_abort"):
            if tail.strip():
                if await _say(tail, not started):
                    started = True
            if started:   # 正常结束才发 end 包(被打断时官方要求不补发)
                await dws.send(enc(T_FULL_CLIENT, 500, json.dumps(
                    {"start": False, "content": "", "end": True}, ensure_ascii=False).encode(), session_id=sid))
            if answer.strip():   # 答案写进它的记忆(ChatTTSText 是否进上下文文档未明说,510 注入保追问不失忆)
                await _inject_500_memory(dws, sid, question, answer)
        await bws.send(json.dumps({"event": "tool_status", "payload": {
            "status": "done" if started else "error", "tool": tool_name, "label": tool_label,
            "cmd": f"{tool_name}({(dm or '默认模型')}/{(de or '默认深度')}): {question[:200]}",
            "rag": _speech_clean(answer)[:1600],
            "result_brief": _speech_clean(answer)[:400]}}, ensure_ascii=False))
    except Exception as ex:
        sys.stderr.write(f"[voice-rt deep] {ex}\n")
        try:
            await bws.send(json.dumps({"event": "tool_status", "payload": {"status": "error", "tool": "deep_think", "label": f"{tool_label}:{str(ex)[:50]}"}}, ensure_ascii=False))
        except Exception:
            pass
    finally:
        (book.get("tasks") or {}).pop(tool_name, None)
        if push_sp:
            try:
                await push_sp()
            except Exception:
                pass


async def _inject_500_memory(dws, sid, question: str, answer: str):
    """深度答案注入对话历史(510):追问不失忆。v3-⑩ 限长 800→500(进历史轮轮计费)。"""
    pl = {"items": [{"role": "user", "text": f"(我刚才请你深度解答:{question[:120]})"},
                    {"role": "assistant", "text": _speech_clean(answer)[:500]}]}
    try:
        await dws.send(enc(T_FULL_CLIENT, 510, json.dumps(pl, ensure_ascii=False).encode(), session_id=sid))
    except Exception:
        pass


def _ack_pcm_path(txt: str) -> Path:
    import hashlib
    return ACK_PCM_DIR / (hashlib.md5(txt.encode("utf-8")).hexdigest() + ".pcm")


async def _say_ack(bws, dws, sid, tool: str, book: dict):
    """fire 后立刻播确认语。v3-⑪(用户点子):确认语是固定集合,**首次**让豆包合成(500 ChatTTSText)
    并把下行 PCM 录进 state/doubao-ack-pcm/ —— 之后同句直接由 relay 回放缓存给前端:
    零合成费(输出音频 300元/M 一分不花)+ 零延迟(不用等豆包排轮,1.6s→即时)。"""
    txt = _ACK_TEXT.get(tool, "好,我来处理。")
    p = _ack_pcm_path(txt)
    if p.exists():
        try:
            data = p.read_bytes()
            for i in range(0, len(data), 4800):   # ~100ms/片(24k s16le),沿用前端 playPcm 排队机制
                await bws.send(data[i:i + 4800])
            return txt
        except Exception as ex:
            sys.stderr.write(f"[voice-rt ack replay] {ex}\n")
    try:
        book["ack_rec"] = {"txt": txt, "buf": bytearray(), "on": False}   # 下一个 chat_tts_text 轮开录(350 置 on)
        for pkt in ({"start": True, "content": txt, "end": False},
                    {"start": False, "content": "", "end": True}):
            await dws.send(enc(T_FULL_CLIENT, 500, json.dumps(pkt, ensure_ascii=False).encode(), session_id=sid))
    except Exception as ex:
        sys.stderr.write(f"[voice-rt ack] {ex}\n")
    return txt
# 翻页唯一特例兜底:模型对"轻操作"顽固地只嘴上应承不出 JSON(fresh 实测仍如此)→ 它说「翻到第N页」
# 且该轮没 fire 过工具时直接执行;翻页后 setPage→UpdateConfig 链路自动把新页同步回模型,闭环自洽。
_GOPAGE_FALLBACK = re.compile(r"翻到第\s*(\d+)\s*页")

# v3-⑩:RAG 回填的每工具字符上限(未列出的用 1400 默认;仅影响喂回 S2S 的文本,client_action/侧栏展示不受限)
_RAG_LIMIT = {
    "search_video": 900, "search_image": 600,       # 列表类:title/摘要够播报,链接卡片在屏幕上
    "see_ink": 1600, "see_figure": 1600, "see_page": 1800,   # 视觉描述:信息密度高给足
    "read_page": 2200, "read_selection": 1600, "summarize_section": 2200,
    "goto_page": 300, "auto_highlight": 600, "make_anki": 600, "make_note": 600,
}


def _prep_tool_result(res, tool_name):
    """工具结果统一预处理——三条引擎路径(豆包 _run_voice_tool / OpenAI-WS handle_openai._tool /
    RTC handle_rtc_ctl._tool)**共用一份**,根治"改一处漏两处"(制卡预览不显示反复复发的元凶:
    各路径手抄这段各自漏字段/漏 result → tool_status 少 cards 完整体,前端 parse 残 rag 炸掉)。
    → (slim, slim_full, rag):
      slim      = 喂回模型的精简版:剔 _ 前缀控制字段(_fed_images 等 b64)与 client_action;
                  cards/images 有对应 brief 时剔全文(省 token,cards_brief/found_brief 顶上播报)
      slim_full = 完整版(含 cards/images 全文)→ 供 tool_status.result 让前端渲预览卡(见 _tool_preview_result)
      rag       = slim 按 _RAG_LIMIT[tool] 分级限长的 JSON 串(空则统一兜底文案;进历史每个字后续轮轮计费)"""
    slim = {k: v for k, v in res.items() if not str(k).startswith("_") and k != "client_action"}
    slim_full = dict(slim)
    if slim.get("cards") and slim.get("cards_brief"):
        slim = {k: v for k, v in slim.items() if k != "cards"}      # cards_brief 顶上;全文截断成残 JSON=「AI 不知道做过什么卡」
    if slim.get("images") and slim.get("found_brief"):
        slim = {k: v for k, v in slim.items() if k != "images"}     # found_brief/missed 顶上;图 URL 挤爆预算
    lim = _RAG_LIMIT.get(tool_name, 1400)
    rag = json.dumps(slim, ensure_ascii=False)[:lim] if slim else "(无文本结果,界面元素已显示在用户屏幕上)"
    return slim, slim_full, rag


def _tool_preview_result(slim_full):
    """tool_status.result:前端渲预览卡的完整体(目前=制卡 cards 全文;喂回模型的 slim 已剔它,故单独送)。"""
    return slim_full if (slim_full or {}).get("cards") else None


def _is_cmd_sent(t: str) -> bool:
    """350 TTSSentenceStart 的句文本是否属于工具 JSON(命令句;TTS 可能把一条 JSON 拆成多句)→ 静音。"""
    t = (t or "").strip()
    if not t:
        return False
    if '"tool"' in t or '"args"' in t:
        return True
    if t[0] in "{}" or t.endswith(("{", "}", '",', '":')):
        return True
    return bool(re.search(r'"[^"]{0,40}"\s*[::]', t))


def _extract_tool_json(buf: str) -> str | None:
    """从攒的回复文本里提取**完整的**工具 JSON(标准解析;不完整返回 None 等更多增量)。"""
    m = _TOOL_START.search(buf)
    if not m:
        return None
    s = buf[m.start():]
    try:
        _d, end = json.JSONDecoder().raw_decode(re.sub(r"[\x00-\x1f]", " ", s))
        return s[:end]
    except Exception:
        return None


async def _run_voice_tool(bws, dws, sid, cmd: str, file_rel: str, page: int,
                          book: dict | None = None, push_sp=None):
    """把 S2S 的命令文本交给 webapp 统一端点(解析+修补+dispatch 同源)→ 结果三路分发。
    book/push_sp 传入时:①ctx 带前端实时墨迹(侧栏 ctx["ink"] 同款,see_ink 不再依赖 sidecar 保存时机)
    ②任务进程写进 SP(用户催的时候 S2S 知道"正在做",不会重复触发)。"""
    book = book if isinstance(book, dict) else {}
    tname, targs0 = "工具任务", None
    try:
        if cmd.lstrip().startswith("{"):
            _t0 = json.loads(cmd)
            tname = _t0.get("tool") or tname
            targs0 = _t0.get("args")
    except Exception:
        pass

    def _ckey(t, a):   # 缓存键:工具+参数+页码+墨迹版本——任一变化(翻页/新画/改画)自然失效
        ink_fp = str(book.get("ink_ver", 0))
        try:
            aj = json.dumps(a or {}, ensure_ascii=False, sort_keys=True)
        except Exception:
            aj = str(a)
        return f"{t}|{aj}|p{page}|ink{ink_fp}"

    if tname == "deep_think":   # 深度思考虚拟工具:不走 voice-tool,relay 直接流式代播(见 _run_deep_think)
        q = (targs0 or {}).get("question") if isinstance(targs0, dict) else ""
        ack = await _say_ack(bws, dws, sid, "deep_think", book)
        await bws.send(json.dumps({"event": 550, "payload": {"content": ack}}, ensure_ascii=False))
        await _run_deep_think(bws, dws, sid, q or (book.get("user_q") or ""), file_rel, page, book, push_sp)
        return

    if tname == "recall_study":   # 学习回顾(v3-⑰):S2S 只出一条 JSON(~30 token),读日志+聚合+大模型代答全在这
        q = (targs0 or {}).get("question") if isinstance(targs0, dict) else ""
        span = str((targs0 or {}).get("span") or "today").lower()
        ack = await _say_ack(bws, dws, sid, "recall_study", book)
        await bws.send(json.dumps({"event": 550, "payload": {"content": ack}}, ensure_ascii=False))
        digest = _study_digest("week" if span.startswith("w") else "today")
        _vlog("tool", tool="recall_study", label="回顾学习", page=page, book=file_rel, ok=bool(digest))
        if not digest:
            rag = json.dumps([{"title": "学习记录查询结果",
                               "content": "记录为空——这段时间还没有留下学习记录,如实告诉用户即可。"}], ensure_ascii=False)
            await dws.send(enc(T_FULL_CLIENT, 502, json.dumps({"external_rag": rag}, ensure_ascii=False).encode(), session_id=sid))
            await bws.send(json.dumps({"event": "tool_status", "payload": {
                "status": "done", "tool": "recall_study", "label": "回顾学习", "rag": "(记录为空)"}}, ensure_ascii=False))
            return
        q2 = ("根据下面的学习活动记录回答用户的问题。记录里只有页码和操作摘要、**没有页面原文**——"
              "需要具体内容时用 read_page 工具按需拉取(只拉记录里学习重点所在的几页,别整本拉;"
              "read_page 只能读当前这本书,别的书就依据记录摘要说)。"
              "只依据记录和拉到的原文说话,不要编造;口语化、抓重点、按主题归纳(别逐条流水账)。"
              "\n【学习记录(按时间,含页码/查词/选中/问答)】\n"
              + digest + "\n【用户问题】" + (q or "今天学了什么"))
        await _run_deep_think(bws, dws, sid, q2, file_rel, page, book, push_sp,
                              tool_name="recall_study", tool_label="回顾学习",
                              preamble="(语音回顾:可用工具查证;答案会被朗读:口语化短句,按主题归纳,需要停顿处用省略号……,别用列表)")
        return

    cache = book.setdefault("tool_cache", {})
    if targs0 is not None:   # 程序级防重复(用户设计):同工具同参数同页面状态 → 直接复用上次结果,不再执行
        hit = cache.get(_ckey(tname, targs0))
        if hit:
            try:
                if hit.get("ca"):
                    await bws.send(json.dumps({"event": "client_action", "payload": hit["ca"]}, ensure_ascii=False))   # 视频卡等重放
                _hc = hit.get("content") or ""
                await bws.send(json.dumps({"event": "tool_status", "payload": {
                    "status": "done", "tool": tname, "label": f"{hit.get('label') or tname}(复用上次结果)", "cached": True,
                    "cmd": str(cmd)[:500], "vision": hit.get("vision") or [],   # #8 缓存命中也重发"实际发给AI的图"(否则看图类工具复用时无图)
                    "rag": _hc[:1600]}}, ensure_ascii=False))
                rag = json.dumps([{"title": f"工具 {tname} 的结果(页面状态没变,这是**此前同样查询的结果直接复用**,没有重新执行)",
                                   "content": hit.get("content") or "(界面元素已重新显示)"}], ensure_ascii=False)
                await dws.send(enc(T_FULL_CLIENT, 502, json.dumps({"external_rag": rag}, ensure_ascii=False).encode(), session_id=sid))
            except Exception as ex:
                sys.stderr.write(f"[voice-rt cache] {ex}\n")
            return
    try:
        book.setdefault("tasks", {})[tname] = 1   # 任务开始 → SP 标"正在执行"
        if push_sp:
            await push_sp()
        ack = await _say_ack(bws, dws, sid, tname, book)   # 代播确认语(模型自己整轮只有 JSON,已被静音);缓存命中=relay 直接回放 PCM
        await bws.send(json.dumps({"event": 550, "payload": {"content": ack}}, ensure_ascii=False))   # 字幕同步补上确认语
        await bws.send(json.dumps({"event": "tool_status", "payload": {"status": "running", "tool": tname, "label": tname}}, ensure_ascii=False))
        ctx = {"file_rel": file_rel, "page": page} if file_rel else {}
        if book.get("ink_strokes"):
            ctx["ink"] = book["ink_strokes"]   # 实时墨迹随工具走(see_ink/see_page 合成图用它,与侧栏行为一致)
        if book.get("view_shot"):
            ctx["view_image"] = book["view_shot"]   # EPUB 笔迹合成图(前端 syncInk 拍,存 book;PDF 走服务端裁图不设此字段)→ see_ink/see_page 直接看这张图
        if book.get("sel"):
            ctx["selection"] = book["sel"]     # 当前选中随工具走(read_selection/translate/make_anki 等吃它,与侧栏同口径)
        if tname == "see_ink" and book.get("last_ink_desc"):
            ctx["prev_ink_desc"] = book["last_ink_desc"]   # 对比模式:上次笔迹描述随调用走(补笔≠新物体,视觉端据此判断)
        async with httpx.AsyncClient(base_url=WEBAPP, headers=_webapp_headers(), timeout=180) as hc:
            r = await hc.post("/api/assistant/voice-tool", json={"cmd": cmd, "ctx": ctx})
            d = r.json()
        if not d.get("ok") and d.get("feedback"):
            # 解析失败 → 编排 agent 同款自愈:反馈喂回,S2S 会重出一条合法 JSON(再次触发本函数)
            await bws.send(json.dumps({"event": "tool_status", "payload": {"status": "error", "label": "指令未解析,让它重说"}}, ensure_ascii=False))
            rag = json.dumps([{"title": "系统提示(工具调用未执行)", "content": d["feedback"]}], ensure_ascii=False)
            await dws.send(enc(T_FULL_CLIENT, 502, json.dumps({"external_rag": rag}, ensure_ascii=False).encode(), session_id=sid))
            return
        tool = d.get("tool") or "?"
        res = d.get("result") or {}
        ca = res.get("client_action")
        if isinstance(ca, dict) and ca.get("fn"):   # 页面副作用照旧(视频卡进侧栏/跳页/高亮…)
            await bws.send(json.dumps({"event": "client_action", "payload": ca}, ensure_ascii=False))
        # 剔控制字段(_开头=_fed_images 的 b64 等 / client_action):否则 b64 ①烧进喂豆包的 content ②裸露在前端
        #   详情卡 result_brief(用户实测那串 base64)。图单独走 vision 展示。
        _vision = []
        try:
            for v in (res.get("_fed_images") or []):
                if isinstance(v, dict) and v.get("b64") and len(v["b64"]) < 1300000:
                    _vision.append({"media_type": v.get("media_type", "image/png"), "b64": v["b64"]})
            _vision = _vision[:3]
        except Exception:
            _vision = []
        slim, slim_full, content = _prep_tool_result(res, tool)   # 三引擎共用:剔控制字段/cards·images 精简/限长(见 _prep_tool_result)
        rag = json.dumps([{"title": f"工具 {tool} 的真实执行结果(涉及的界面元素已显示在用户屏幕上)",
                           "content": content + "\n(请把要点口语化讲给用户;你此前口头猜测的内容一律作废)"}],
                         ensure_ascii=False)
        await dws.send(enc(T_FULL_CLIENT, 502, json.dumps({"external_rag": rag}, ensure_ascii=False).encode(), session_id=sid))
        _vlog("tool", tool=tool, label=d.get("label") or tool, page=page, book=file_rel, ok=bool(d.get("ok")),
              args=d.get("args"), brief=content[:300])   # 结果摘要落盘:事后可查"它播报的到底有没有依据"
        # 完整调用过程 → tool_status(前端小按钮 + 侧栏对话流详情卡,v3-⑯:S2S指令/上下文/喂回结果全程可查)
        _rb = json.dumps(slim, ensure_ascii=False)[:400]
        await bws.send(json.dumps({"event": "tool_status", "payload": {
            "status": "done" if d.get("ok") else "error", "tool": tool, "label": d.get("label") or tool,
            "args": d.get("args"), "took_s": d.get("took_s"),
            "cmd": str(cmd)[:500],                                       # S2S 输出的原始指令 JSON
            "ctx_brief": {"page": page, "ink": len(ctx.get("ink") or []),
                          "sel": len(ctx.get("selection") or "")},       # 随调用携带的页面上下文概要
            "rag": content[:1600],                                       # 喂回豆包播报的真实结果
            "vision": _vision,                                           # #8 实际发给 AI 的图 → 前端「AI 请求」节点展示
            "result": _tool_preview_result(slim_full),   # 制卡完整体(前端渲预览卡)
            "result_brief": _rb}}, ensure_ascii=False))   # slim=已剔 b64,不再裸露
        if d.get("ok") and d.get("cacheable"):   # 只读工具 → 按「工具+参数+页+墨迹版本」缓存,重复询问直接复用
            cache[_ckey(d.get("tool"), d.get("args"))] = {
                "content": content, "label": d.get("label") or tool,
                "vision": _vision,   # #8 连"实际发给AI的图"一起缓存 → 命中时能重发(看图类工具复用不丢图)
                "ca": ca if (isinstance(ca, dict) and ca.get("fn")) else None}
            while len(cache) > 20:
                cache.pop(next(iter(cache)))
        if d.get("ok") and d.get("tool") in ("see_ink", "see_page", "see_figure"):
            book["ink_seen_ver"] = book.get("ink_ver", 0)   # 记录"看过这个版本"→ SP 三态据此判断(finally 的 push_sp 会带上)
            if d.get("tool") == "see_ink":
                book["last_ink_desc"] = str(res.get("笔迹标注描述") or "")[:400]   # 下次 see_ink 的对比上下文(补笔≠新物体)
    except Exception as ex:
        sys.stderr.write(f"[voice-rt tool] {ex}\n")
        try:
            await bws.send(json.dumps({"event": "tool_status", "payload": {"status": "error", "label": str(ex)[:60]}}, ensure_ascii=False))
        except Exception:
            pass
    finally:
        try:
            (book.get("tasks") or {}).pop(tname, None)   # 任务结束 → SP 摘牌
            if push_sp:
                await push_sp()
        except Exception:
            pass


async def _fetch_tools_catalog(surface: str) -> list[dict]:
    """Fetch one trusted production projection from assistant.ToolRegistry."""

    try:
        async with httpx.AsyncClient(
            base_url=WEBAPP,
            headers=_webapp_headers(),
            timeout=10,
        ) as hc:
            r = await hc.get(
                "/api/assistant/tools",
                params={"surface": surface},
            )
            r.raise_for_status()
            d = r.json()
        return [
            row
            for row in (d.get("tools") or [])
            if isinstance(row, dict) and row.get("name")
        ]
    except Exception as ex:
        sys.stderr.write(f"[voice-rt tools] {ex}\n")
        return []


def _catalog_to_realtime_tools(catalog: list[dict]) -> list[dict]:
    """Convert registry API rows without inventing a second local schema."""

    tools = []
    for row in catalog:
        schema = row.get("parameters")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            schema = {
                "type": "object",
                "properties": {},
                "additionalProperties": True,
            }
        tools.append({
            "type": "function",
            "name": row["name"],
            "description": str(
                row.get("description") or row.get("desc") or ""
            )[:1024],
            "parameters": schema,
        })
    return tools


async def _fetch_tools_lines(surface: str = "doubao_s2s") -> dict:
    """拉 registry surface 目录 → {name: 压缩行}。O2.0 上下文 12K,desc 只留第一句。
    v3-⑩ 起目录**全量注入且恒定**(进 SP 稳定前缀保前缀缓存);"无笔迹 see_ink 无效"的
    状态语义由 _role_text 尾部的实时状态声明承接(原门控=按状态增删行,一画笔目录就变,
    排在它后面的页文本/生词整层缓存连坐失效)。"""
    out = {}
    for row in await _fetch_tools_catalog(surface):
        desc = re.split(
            r"[。;;]",
            (row.get("description") or row.get("desc") or "").replace("*", ""),
        )[0][:52]
        out[row["name"]] = f"- {row['name']}: {desc}"
    return out


# ═══════════ agent 模式:豆包只当耳朵(sauc 流式 ASR)+ 嘴(流式 TTS),大脑 = 侧栏助手 ═══════════
# 浏览器单 ws 全双工:上行二进制=麦克风 PCM16k;上行 JSON {type:'speak'|'cancel'};
#   下行二进制=TTS PCM24k(前端现有播放器直接播);下行 JSON {event:'asr'|'utterance'|'tts_end'}。
# 大脑不经过本服务:前端拿到 utterance 终稿后调侧栏助手原有 SSE 管线(工具/历史/渲染全复用),
#   回答文本增量再经 {type:'speak'} 送回来合成。

def _sauc_frame(mtype: int, flags: int, seq: int, payload: bytes) -> bytes:
    """sauc v3 帧:header(4B) + seq(int32 BE) + size(uint32) + gzip(payload)。"""
    payload = gzip.compress(payload)
    head = bytes([0x11, (mtype << 4) | flags, (0b0001 << 4) | 0b0001, 0x00])
    return head + struct.pack(">i", seq) + struct.pack(">I", len(payload)) + payload


def _sauc_parse(frame: bytes) -> dict:
    mtype, flags = frame[1] >> 4, frame[1] & 0x0F
    comp = frame[2] & 0x0F
    off = 4
    out = {"mtype": mtype}
    if flags & 0x01:
        out["seq"] = struct.unpack(">i", frame[off:off + 4])[0]; off += 4
    if mtype == 0b1111:
        out["code"] = struct.unpack(">I", frame[off:off + 4])[0]; off += 4
    size = struct.unpack(">I", frame[off:off + 4])[0]; off += 4
    pl = frame[off:off + size]
    if comp == 0b0001:
        try:
            pl = gzip.decompress(pl)
        except Exception:
            pass
    try:
        out["payload"] = json.loads(pl.decode("utf-8", "replace"))
    except Exception:
        out["payload"] = {}
    return out


async def _tts_stream(key: str, speaker: str, text: str):
    """HTTP 单向流式 TTS → yield PCM24k chunk(NDJSON:{"code":0,"data":<b64>},20000000=完)。"""
    headers = {"X-Api-Key": key, "X-Api-Resource-Id": TTS_RID,
               "X-Api-Request-Id": str(uuid.uuid4()), "Content-Type": "application/json"}
    body = {"user": {"uid": "voice-agent"},
            "req_params": {"text": text, "speaker": speaker,
                           "audio_params": {"format": "pcm", "sample_rate": 24000}}}
    async with httpx.AsyncClient(timeout=60) as hc:
        async with hc.stream("POST", TTS_URL, headers=headers, json=body) as r:
            if r.status_code != 200:
                raise RuntimeError(f"tts http {r.status_code}")
            async for line in r.aiter_lines():
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("code") == 0 and d.get("data"):
                    yield base64.b64decode(d["data"])
                elif d.get("code") == 20000000:
                    return
                elif d.get("code"):
                    raise RuntimeError(f"tts code={d.get('code')} {str(d.get('message'))[:80]}")


def _uni_req_frame(payload: dict) -> bytes:
    """单向流式 TTS 的请求帧:Full-client、flags=0000(无 event 号)、JSON 无压缩(官方协议实测)。"""
    b = json.dumps(payload, ensure_ascii=False).encode()
    return bytes([0x11, 0x10, 0x10, 0x00]) + struct.pack(">I", len(b)) + b


def _tts_channel(bws, key: str, speaker: str, span: str = ""):
    """朗读 TTS 通道(v3-⑱ 双引擎,按音色自动选,speak 时现读凭证可热切):
    - `*_uranus_bigtts`(2.0)→ **单向流式 + seed-tts-2.0**:每句一个请求但同 section_id(服务端保持
      对话式韵律);`context_texts` 载入用户配置的**朗读语气指令**(自然语言,实测生效且不被念出);
      speech_rate 同名参数。⚠ 2.0 不吃 SSML(实测被剥),停顿靠标点/省略号(prompt 已引导 AI 写)。
    - 其余(moon 系 1.0)→ 双向流式(原实现):一轮一 session 增量喂文本。
    打断(cancel)= close 连接+作废世代:立即哑火。返回 {"speak","done","cancel"},音频裸转发 bws。"""
    tts = {"ws": None, "sid": None, "reader": None, "gen": 0}
    uni = {"q": None, "worker": None, "section": None, "gen": 0, "ws": None}
    uni_end = object()   # 排在本轮所有 speak 之后；worker 到这里才可宣告 tts_end
    _tts_span = [span]   # 146:记账要 span

    async def _uni_synth_one(text: str, g: int, mood: str = ""):
        _ledger_volc_tts(_tts_span[0], len(text or ""), "seed-tts-2.0")   # 146:按字符入账(3元/万字)
        try:   # 字幕帧(v3-⑳):worker 串行合成,帧紧贴本句音频首块 → 前端把句子绑到该块的播放时刻,字幕与声音同步
            await bws.send(json.dumps({"event": "tts_seg", "payload": {"text": text}}, ensure_ascii=False))
        except Exception:
            pass
        c = _creds()
        spk = c.get("tts_speaker") or speaker
        h = {"X-Api-Key": key, "X-Api-Resource-Id": "seed-tts-2.0", "X-Api-Connect-Id": str(uuid.uuid4())}
        adds = {}
        # 语气优先级(v3-⑱b 用户设计):AI 按内容给的本轮语气(回答首行 [语气:XX] 标记,前端剥离后随 speak 传来)
        # > 面板固定语气(tts_instruction,兜底)。都是 context_texts 自然语言指令,不计费不进文本。
        # ㉒音色锁定(用户报告"有时完全变了一个人"):2.0 是 LLM 式 TTS,念到引语/对话内容会自己"演绎"换声线——
        # 每句恒带音色锁定指令,语气只许变情绪不许变人。
        instr = (f"用{mood}的语气说" if mood else (c.get("tts_instruction") or "").strip())
        adds["context_texts"] = ["始终保持同一个说话人的声音和音色,不要模仿内容里的角色变声" + (f",{instr}" if instr else "")]
        if uni["section"]:
            adds["section_id"] = uni["section"]   # 同一通话同一 section:跨请求保持对话式韵律
        rp = {"text": text, "speaker": spk,
              "audio_params": {"format": "pcm", "sample_rate": 24000,
                               "speech_rate": int(c.get("tts_speech_rate", 0))}}
        if adds:
            rp["additions"] = json.dumps(adds, ensure_ascii=False)
        try:
            async with websockets.connect(TTS_UNI_WSS, additional_headers=h,
                                          max_size=10 * 1024 * 1024, open_timeout=10) as w:
                uni["ws"] = w
                await w.send(_uni_req_frame({"user": {"uid": "voice-agent"}, "req_params": rp}))
                while True:
                    if g != uni["gen"]:
                        return
                    d = dec(await asyncio.wait_for(w.recv(), timeout=30))
                    if d["type"] == T_AUDIO_SERVER:
                        if g == uni["gen"]:
                            await bws.send(d["payload"])
                    elif d["type"] == T_ERROR:
                        pl = d["payload"]
                        sys.stderr.write(f"[voice-tts uni] err {(pl[:120].decode('utf-8','ignore') if isinstance(pl,(bytes,bytearray)) else str(pl)[:120])}\n")
                        return
                    elif d.get("event") == 152:
                        return
        finally:
            if uni["ws"] is not None:
                uni["ws"] = None

    async def _uni_worker(g: int):
        """串行消费句队列(保证音频顺序);cancel 换代即退出。"""
        try:
            while True:
                item = await uni["q"].get()
                if item is None or g != uni["gen"]:
                    return
                if item is uni_end:
                    try:
                        await bws.send(json.dumps({"event": "tts_end"}, ensure_ascii=False))
                    except Exception:
                        pass
                    continue
                try:
                    await _uni_synth_one(item[0], g, item[1])
                except Exception as ex:
                    sys.stderr.write(f"[voice-tts uni] {str(ex)[:120]}\n")
        except asyncio.CancelledError:
            pass

    def _is_uni() -> bool:
        try:
            return "_uranus_" in (_creds().get("tts_speaker") or speaker or "")
        except Exception:
            return False

    async def _tts_reader(tws, g):
        try:
            async for frame in tws:
                if g != tts["gen"]:
                    break
                d = dec(frame)
                if d["type"] == T_AUDIO_SERVER:
                    await bws.send(d["payload"])          # PCM24k 裸转发,浏览器即到即播
                elif d["type"] == T_ERROR:
                    sys.stderr.write(f"[voice-tts] err {d.get('code')} {d['payload'][:120]}\n")
                    break
                elif d.get("event") == 152:               # SessionFinished:这轮念完
                    if g == tts["gen"]:
                        await bws.send(json.dumps({"event": "tts_end"}, ensure_ascii=False))
                    break
        except Exception:
            pass
        finally:
            try:
                await tws.close()
            except Exception:
                pass
            if tts["ws"] is tws:
                tts["ws"] = None
                tts["sid"] = None

    async def _ensure():
        if tts["ws"] is not None:
            return
        # 朗读音色**现读凭证**(v3-⑮:设置面板 tts_speaker 可调):一轮回答一个 session,改完下一轮生效。
        # ⚠ 双向流式 TTS(10029)只认 *_moon_bigtts 系音色,S2S 的 jupiter 系传过来报 55000000。
        c0 = _creds()
        spk = c0.get("tts_speaker") or speaker
        tsr = int(c0.get("tts_speech_rate", 0))   # 朗读语速(独立于 S2S 的 speech_rate;实测 bidi 认此参数,80≈-45%时长)
        h = {"X-Api-Key": key, "X-Api-Resource-Id": TTS_RID, "X-Api-Connect-Id": str(uuid.uuid4())}
        tws = await websockets.connect(TTS_BIDI_WSS, additional_headers=h, max_size=10 * 1024 * 1024, open_timeout=10)
        await tws.send(enc(T_FULL_CLIENT, 1, b"{}"))
        await tws.recv()   # ConnectionStarted(50)
        sid_t = str(uuid.uuid4())
        await tws.send(enc(T_FULL_CLIENT, 100, json.dumps({
            "user": {"uid": "voice-agent"}, "event": 100, "namespace": "BidirectionalTTS",
            "req_params": {"speaker": spk, "audio_params": {"format": "pcm", "sample_rate": 24000, "speech_rate": tsr}},
        }, ensure_ascii=False).encode(), session_id=sid_t))
        tts["ws"], tts["sid"] = tws, sid_t
        tts["reader"] = asyncio.create_task(_tts_reader(tws, tts["gen"]))   # 150 由 reader 吞,音频/152 它管

    async def cancel():
        tts["gen"] += 1
        tws, tts["ws"], tts["sid"] = tts["ws"], None, None
        if tws:
            try:
                await tws.close()
            except Exception:
                pass

    async def speak(text: str, mood: str = ""):
        text = (text or "").strip()
        if not text:
            return
        if _is_uni():   # 2.0 音色 → 单向引擎(句队列串行合成,同 section 保韵律;mood 随句走)
            if uni["q"] is None:
                uni["q"] = asyncio.Queue()
            if not uni["section"]:
                uni["section"] = str(uuid.uuid4())
            if uni["worker"] is None or uni["worker"].done():
                uni["gen"] += 1
                uni["worker"] = asyncio.create_task(_uni_worker(uni["gen"]))
            await uni["q"].put((text, (mood or "").strip()[:12]))
            return
        try:
            await bws.send(json.dumps({"event": "tts_seg", "payload": {"text": text}}, ensure_ascii=False))   # 字幕帧(bidi 音频不分句,退化为略超前)
            await _ensure()
            await tts["ws"].send(enc(T_FULL_CLIENT, 200, json.dumps({
                "user": {"uid": "voice-agent"}, "event": 200, "namespace": "BidirectionalTTS",
                "req_params": {"text": text},
            }, ensure_ascii=False).encode(), session_id=tts["sid"]))
        except Exception as ex:
            sys.stderr.write(f"[voice-tts>] {ex}\n")
            await cancel()

    async def done():   # 这轮回答文本发完
        if _is_uni():   # 单向引擎:结束标记也进串行队列,不得越过仍在合成的 speak
            if uni["q"] is not None and uni["worker"] is not None and not uni["worker"].done():
                await uni["q"].put(uni_end)
            else:
                try:
                    await bws.send(json.dumps({"event": "tts_end"}, ensure_ascii=False))
                except Exception:
                    pass
            return
        if tts["ws"] and tts["sid"]:
            try:
                await tts["ws"].send(enc(T_FULL_CLIENT, 102, b"{}", session_id=tts["sid"]))
            except Exception:
                await cancel()

    async def cancel_all():
        uni["gen"] += 1   # 作废单向世代(worker/在流请求自毁)
        if uni["q"] is not None:
            try:
                while not uni["q"].empty():
                    uni["q"].get_nowait()
            except Exception:
                pass
            try:
                uni["q"].put_nowait(None)   # 唤醒 worker 退出
            except Exception:
                pass
        uni["worker"] = None
        w = uni["ws"]
        if w is not None:
            try:
                await w.close()
            except Exception:
                pass
        await cancel()   # bidi 侧照旧

    return {"speak": speak, "done": done, "cancel": cancel_all}


async def handle_tts_only(bws):
    """朗读专用通道(v3-⑬,`?mode=tts`):侧栏助手回答的 T2S 流式播放——**不开麦、不连 ASR**。
    「🔊 朗读」点亮且没在语音通话时前端 lazy 连这条;speak/speak_done/cancel 协议与 agent 模式同款。"""
    cred = _creds()
    if not cred.get("api_key"):
        await bws.send(json.dumps({"event": -1, "payload": {"error": "缺凭证:~/.config/doubao-voice.json"}}, ensure_ascii=False))
        await bws.close()
        return
    _tspan = uuid.uuid4().hex[:12]   # 146:纯朗读连接也要记账(它没有对话 span,自建一个)
    ch = _tts_channel(bws, cred["api_key"], cred.get("tts_speaker", TTS_SPEAKER), span=_tspan)
    try:
        await bws.send(json.dumps({"event": "tts_ready"}, ensure_ascii=False))
        async for msg in bws:
            if isinstance(msg, (bytes, bytearray)):
                continue
            try:
                j = json.loads(msg)
            except Exception:
                continue
            t = j.get("type")
            if t == "speak" and (j.get("text") or "").strip():
                await ch["speak"](j["text"], j.get("mood") or "")
            elif t == "speak_done":
                await ch["done"]()
            elif t == "cancel":
                await ch["cancel"]()
            elif t == "finish":
                break
    except Exception:
        pass
    finally:
        await ch["cancel"]()
        try:
            await bws.close()
        except Exception:
            pass


# ── GPT Realtime 引擎(㉔):OpenAI gpt-realtime-2.1-mini 作第二实时语音引擎(凭证 rt_engine=="openai" 切换)──
# 定位=**协议翻译层**:OpenAI GA 事件 ↔ 前端既有豆包事件语义(450/451/550/359/tool_status/client_action),
# 前端零逻辑改动(仅 up_rate 事件把上行采样切 24k——OpenAI 只吃 24kHz pcm)。
# 工具=原生 function calling(session.tools),不需要豆包那套 JSON 协议/静音/代播确认语 hack;
# 工具描述直接用 voice-tools 目录行(args 用法在 description 里),参数 schema 宽松透传给 dispatch。
# 上下文 128k(豆包 12K 的 10 倍):页文本进 instructions(session.update 部分更新不重置对话),
# 状态(选中/笔迹)走 conversation.item.create system 消息——与豆包 510 增量哲学同构。
OPENAI_RT_CRED = Path("~/.config/openai-realtime.json").expanduser()
OPENAI_RT_URL = "wss://api.openai.com/v1/realtime?model="

# 高频参数 schema 已迁到 assistant.ToolRegistry；relay 只消费 API 投影，不再保留副本。
XAI_RT_CRED = Path("~/.config/xai-grok.json").expanduser()          # 94:Grok Voice 第三引擎(协议兼容 OpenAI Realtime)
XAI_RT_URL = "wss://api.x.ai/v1/realtime?model="


def _openai_key() -> str:
    try:
        return json.loads(OPENAI_RT_CRED.read_text("utf-8")).get("api_key") or ""
    except Exception:
        return ""


OPENAI_USAGE_FILE = Path(__file__).resolve().parent.parent / "state" / "openai-usage.json"
# gpt-realtime-2.1-mini 官方单价($/1M tokens):文本 in 0.60/cached 0.06/out 2.40;音频 in 10/cached 0.30/out 20;图像 in 0.80
_OA_RATE = {"in_text": 0.60, "cached_text": 0.06, "out_text": 2.40,
            "in_audio": 10.0, "cached_audio": 0.30, "out_audio": 20.0, "in_image": 0.80}


# 146:**火山侧单价**(官方计费页 https://www.volcengine.com/docs/6561/1359370,2026-07-14 核对)。
#   之前账本只记 OpenAI,火山的耳朵(ASR)和嘴(TTS)一分没记 → "我们花了多少钱"根本答不上来。
#   ⚠ 人民币!换算 USD 用 RMB_USD。买了资源包就改 env(后付费→资源包能再省 30%)。
RMB_USD = float(os.environ.get("RMB_USD", "7.2"))
VOLC_ASR_RMB_H = {                      # 元/小时(按时长,精确到毫秒)
    "v2": float(os.environ.get("VOLC_ASR2_RMB_H", "1.0")),    # 豆包流式语音识别模型2.0(在用)
    "v1": float(os.environ.get("VOLC_ASR1_RMB_H", "4.5")),    # 大模型流式语音识别(贵 4.5×)
}
VOLC_TTS_RMB_10K = {                    # 元/万字符(1 汉字 = 1 字符)
    "seed-tts-2.0": float(os.environ.get("VOLC_TTS20_RMB_10K", "3.0")),   # 豆包语音合成模型2.0(uranus 音色)
    "volc.service_type.10029": float(os.environ.get("VOLC_TTS10_RMB_10K", "5.0")),  # 大模型语音合成(moon 1.0 音色)
}


LEDGER_DB = Path("/home/bwicarus/claude/state/voice-ledger.db")


def _ledger_conn():
    import sqlite3
    LEDGER_DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(LEDGER_DB, timeout=5)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS usage_events(
        id INTEGER PRIMARY KEY, ts INTEGER, day TEXT, engine TEXT, kind TEXT, span TEXT,
        resp_id TEXT, model TEXT, in_tok INTEGER, out_tok INTEGER, cached_tok INTEGER,
        audio_in_s REAL, audio_out_s REAL, text_items INTEGER, est_usd REAL, meta TEXT,
        UNIQUE(engine, kind, resp_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS tool_calls(
        id INTEGER PRIMARY KEY, ts INTEGER, day TEXT, engine TEXT, span TEXT,
        call_id TEXT, tool TEXT, ok INTEGER, took_s REAL, cached INTEGER,
        UNIQUE(engine, call_id))""")
    return c


def _ledger_usage(engine, span, resp_id, model="", in_tok=0, out_tok=0, cached_tok=0,
                  audio_in_s=0.0, audio_out_s=0.0, text_items=0, est_usd=0.0, kind="response", meta=""):
    """#284:usage 事件入账(幂等:同 engine+kind+resp_id 只记一次——response.done 重复投递不双记)。"""
    try:
        c = _ledger_conn()
        with c:
            c.execute("INSERT OR IGNORE INTO usage_events(ts,day,engine,kind,span,resp_id,model,in_tok,out_tok,"
                      "cached_tok,audio_in_s,audio_out_s,text_items,est_usd,meta) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (int(time.time()), time.strftime("%Y-%m-%d"), engine, kind, span, resp_id or f"~{time.time()}",
                       model, in_tok, out_tok, cached_tok, audio_in_s, audio_out_s, text_items, est_usd, meta[:200]))
        c.close()
    except Exception as ex:
        sys.stderr.write(f"[ledger] {ex}\n")


def _ledger_volc_asr(span: str, seconds: float, v2: bool = True):
    """146:火山 ASR 按时长入账。relay 每帧固定 100ms(3200B@16k/16bit/mono),数帧即得秒数。"""
    if seconds <= 0:
        return
    rmb = VOLC_ASR_RMB_H["v2" if v2 else "v1"] * seconds / 3600.0
    _ledger_usage("volc_asr", span, f"asr_{span}_{int(time.time()*1000)}", model=("asr-2.0" if v2 else "asr-bigmodel"),
                  audio_in_s=round(seconds, 2), est_usd=round(rmb / RMB_USD, 6), kind="asr")


def _ledger_volc_tts(span: str, chars: int, rid: str):
    """146:火山 TTS 按字符入账(1 汉字=1 字符;标点/空格也算)。"""
    if chars <= 0:
        return
    rmb = VOLC_TTS_RMB_10K.get(rid, 5.0) * chars / 10000.0
    _ledger_usage("volc_tts", span, f"tts_{span}_{int(time.time()*1000000)}", model=rid,
                  text_items=chars, est_usd=round(rmb / RMB_USD, 6), kind="tts")


def _ledger_tool(engine, span, call_id, tool, ok, took_s=0.0, cached=False):
    try:
        c = _ledger_conn()
        with c:
            c.execute("INSERT OR IGNORE INTO tool_calls(ts,day,engine,span,call_id,tool,ok,took_s,cached) "
                      "VALUES(?,?,?,?,?,?,?,?,?)",
                      (int(time.time()), time.strftime("%Y-%m-%d"), engine, span,
                       call_id or f"~{time.time()}", tool, 1 if ok else 0, float(took_s or 0), 1 if cached else 0))
        c.close()
    except Exception as ex:
        sys.stderr.write(f"[ledger] {ex}\n")


def _ledger_day_spent(day=None):
    try:
        c = _ledger_conn()
        r = c.execute("SELECT COALESCE(SUM(est_usd),0) FROM usage_events WHERE day=?",
                      (day or time.strftime("%Y-%m-%d"),)).fetchone()
        c.close()
        return float(r[0] or 0)
    except Exception:
        return 0.0


def _budget_gate():
    """#284 预算硬闸:当日已花≥预算 → (False, 已花)。
    安全:默认 $5/天(缺 rt_budget_usd 时不再形同虚设);cfg 里显式写 0 才是关闭闸。"""
    try:
        _rb = _creds().get("rt_budget_usd")
        b = float(_rb) if _rb is not None else 5.0
    except Exception:
        b = 5.0
    if b <= 0:
        return True, 0.0
    spent = _ledger_day_spent()
    return spent < b, spent


def _oa_log_usage(usage: dict, engine: str = "openai_ws", span: str = "", resp_id: str = ""):
    """response.done.usage 按天入账(官方口径:分模态/缓存;cached 是 input 子集,按扣减计价)。"""
    try:
        itd = usage.get("input_token_details") or {}
        otd = usage.get("output_token_details") or {}
        ctd = itd.get("cached_tokens_details") or {}
        row = {"in_text": int(itd.get("text_tokens") or 0), "in_audio": int(itd.get("audio_tokens") or 0),
               "in_image": int(itd.get("image_tokens") or 0),   # >0 = 模型真看到了图(rt_image 直喂的最硬验证信号)
               "cached_text": int(ctd.get("text_tokens") or 0), "cached_audio": int(ctd.get("audio_tokens") or 0),
               "out_text": int(otd.get("text_tokens") or 0), "out_audio": int(otd.get("audio_tokens") or 0)}
        cost = ((row["in_text"] - row["cached_text"]) * _OA_RATE["in_text"]
                + row["cached_text"] * _OA_RATE["cached_text"]
                + (row["in_audio"] - row["cached_audio"]) * _OA_RATE["in_audio"]
                + row["cached_audio"] * _OA_RATE["cached_audio"]
                + row["in_image"] * _OA_RATE["in_image"]
                + row["out_text"] * _OA_RATE["out_text"]
                + row["out_audio"] * _OA_RATE["out_audio"]) / 1_000_000.0
        day = time.strftime("%Y-%m-%d")
        try:
            data = json.loads(OPENAI_USAGE_FILE.read_text("utf-8"))
        except Exception:
            data = {}
        d = data.setdefault(day, {k: 0 for k in row} | {"usd": 0.0, "turns": 0})
        for k, v in row.items():
            d[k] = d.get(k, 0) + v
        d["usd"] = round(d.get("usd", 0.0) + cost, 6)
        d["turns"] = d.get("turns", 0) + 1
        OPENAI_USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        OPENAI_USAGE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
        _ledger_usage(engine, span, resp_id, model="gpt-realtime",
                      in_tok=int(usage.get("input_tokens") or 0), out_tok=int(usage.get("output_tokens") or 0),
                      cached_tok=row["cached_text"] + row["cached_audio"], est_usd=round(cost, 6))   # 284:双写过渡(JSON 留只读历史)
    except Exception as ex:
        sys.stderr.write(f"[voice-oa usage] {ex}\n")


def _oa_instructions(book: dict, file_rel: str, page: int) -> str:
    """OpenAI 版 system instructions:角色 + 直塞内容(页文本/生词/插图),无 JSON 工具协议段(原生 FC)。"""
    cfg = _creds()
    lang = (cfg.get("rt_lang") or "").strip()   # ㉖b:回答语言(""=自动跟随;写死中文曾让它用中文读音念日语)
    if lang == "zh":
        lang_line = "默认用中文口语回答;朗读书里的日语/英语原文时用该语言的**原生发音**念,绝不用中文读音念外语。"
    elif lang == "ja":
        lang_line = "日本語で答えてください。本の原文を読み上げるときは、その言語本来の発音で読んでください。"
    elif lang == "en":
        lang_line = "Respond in English. When reading passages aloud, pronounce them in their original language."
    else:
        lang_line = ("**跟随用户说话的语言**回答(他说中文就用中文,日本語なら日本語で);"
                     "朗读书页原文时按内容本身的语言用**原生发音**念——日语内容用日语读音,不要用中文读音念日语汉字。")
    # rt_instructions 是**附加**指令(UI placeholder 也这么写):旧的 or 链是互斥替换语义,
    # 线上只填了一句发音偏好就把默认人设整段顶掉了 → 默认人设恒在,用户内容按官方骨架另起一段追加。
    _persona = (cfg.get("system_role") or "").strip() or "你是用户的学习伙伴,他在用自己搭的系统自学日语、英语和大学数学物理。"
    _extra = (cfg.get("rt_instructions") or "").strip()
    parts = ["# Role & Objective\n" + _persona]
    if _extra:
        parts.append("# Personality & Tone\n" + _extra)
    parts += [lang_line,
             "口语回答,默认两三句话说清,别铺开;用户要求展开才展开。"
             "你连着他的阅读器,配了一套真实工具(function calling):看图细节/翻页/搜索/高亮/做卡片/查词等"
             "需要动手的事**直接调用工具**,拿到真实结果再回答;绝不口头宣称做了没做的事。"
             "**手写/圈画铁律**:用户提到『我写的/我画的/我圈的/帮我看看这个算式』时,永远**先调 see_ink 工具**"
             "(它会给你笔迹区域的合成图)——你有这个能力,回答『看不到』或让他粘贴/截图都是错误行为。"
             "工具描述里写了 args 字段的用法,照着填。联网能力=三个真工具:web_search(查网上信息,额度有限省着用)/"
             "search_image(搜真实图片)/search_video(搜教学视频)——查网/看图/看视频就调它们,图和视频自动显示在界面上;"
             "工具失败就如实说暂时查不了。search_all_books 只搜他的书库。**绝不要自己输出 markdown 图片/链接占位符假装贴图**。"
             "只读查询(查词/看图/搜索/读页)意图清楚就直接调;写操作(高亮/做卡片/写笔记/插入页)先用一句话说清你要做什么再做。"
             "工具成功返回后才能说『已完成』;同样参数失败别重复调超过一次。音频含糊/有噪声时别猜,简短请他重说一遍。"
             "**本页正文已在上文(下面直接给你 / 系统状态消息 / 工具结果里)时,回答·高亮·做卡直接用它,别再对当前页调 read_page;只有需要**其它页**的内容才 read_page(page:N)。**"]
    _brief_ln = _brief_line(book)   # Phase2:简述优先替整页
    pt = (book.get("page_text") or "").strip()
    _name = (file_rel.rsplit("/", 1)[-1] or "这本书")
    if _brief_ln:
        parts.append(f"用户此刻正在读《{_name}》第 {page} 页。{_brief_ln}(本页要点摘要;能答就直接答,需要完整原文再 read_page(page:N))")
    elif pt:
        parts.append(f"用户此刻正在读《{_name}》第 {page} 页,本页文字内容(直接可用):\n{pt[:1500]}")
    figs = book.get("figures") or []
    fx = ";".join(f"「{(f.get('caption') or '插图')}」:{(f.get('desc') or '')}" for f in figs[:4] if isinstance(f, dict))
    if fx:
        parts.append(f"本页插图的文字描述:{fx[:1000]}")
    vocab = book.get("vocab") or []
    if vocab:
        parts.append("本页『还没掌握』的生词:" + "、".join(vocab[:30]))
    parts.append("页面实时状态(选中/手写笔迹)和**翻页后的新页面内容**都会以 system 消息出现在对话里,永远以最新一条为准;"
                 "一条状态消息都没有=本页当前无选中无笔迹。"
                 "**状态消息只是记录,永远不要对它们本身做回应或主动评论**(不要说『我看到你画了/选了…』);"
                 "没有听到用户清晰说话时调 wait_for_user 安静结束回合,别自己找话说。")
    return "\n".join(parts)


def _norm_vm(v) -> str:
    """输出模式归一(61 四态:sts 纯语音/stt 纯文字/half 混合/route 智能路由);旧值映射兼容存量配置。"""
    v = (v or "sts").strip()
    return {"audio": "sts", "mixed": "half", "text": "stt", "tts": "stt"}.get(v, v if v in ("sts", "stt", "half", "route") else "sts")


async def _oa_deep(question: str, file_rel: str, page: int) -> str:
    """deep_think(OpenAI 版):转交侧栏 chat(Claude,深度预设选型)拿完整回答当 function output 回填,GPT 自己念。"""
    if not question:
        return "(问题为空)"
    body = {"message": "(语音深度思考,直接给最终回答,口语化短句)\n" + question,
            "rid": f"oa{uuid.uuid4().hex[:10]}", "voice": "s2s",
            "context": ({"file_rel": file_rel, "page": page} if file_rel else {})}
    answer = ""
    try:
        async with httpx.AsyncClient(base_url=WEBAPP, headers=_webapp_headers(), timeout=300) as hc:
            async with hc.stream("POST", "/api/assistant/chat", json=body) as r:
                ev, data = "", ""
                async for line in r.aiter_lines():
                    if line.startswith("event:"):
                        ev = line[6:].strip()
                    elif line.startswith("data:"):
                        data += line[5:].strip()
                    elif not line.strip():
                        if ev == "answer" and data:
                            try:
                                parsed = json.loads(data)
                                if isinstance(parsed, str):
                                    answer = re.split(r"\n?FOLLOWUP[::]", parsed)[0]
                            except Exception:
                                pass
                        ev, data = "", ""
    except Exception as ex:
        return f"(深度思考失败:{str(ex)[:100]})"
    return answer[:3000] or "(没拿到回答)"


async def handle_openai(bws, file_rel: str = "", page: int = 0, engine: str = "openai", fresh: bool = False):
    # 94:engine="grok" 复用整条 GPT-WS 管线(xAI Voice Agent 协议兼容)。实测差异:恒纯语音(text 模态被忽略)、
    # 无视觉(input_image 被静默收下但模型看不见,回答幻觉)→ 视觉走文字转述;OpenAI 特有字段裁掉防 session.update 整条被拒。
    if engine == "grok":
        try:
            key = json.loads(XAI_RT_CRED.read_text("utf-8")).get("api_key") or ""
        except Exception:
            key = ""
        if not key:
            await bws.send(json.dumps({"event": -1, "payload": {"error": "缺 xAI 凭证:~/.config/xai-grok.json"}}, ensure_ascii=False))
            await bws.close()
            return
    else:
        key = _openai_key()
        if not key:
            await bws.send(json.dumps({"event": -1, "payload": {"error": "缺 OpenAI 凭证:~/.config/openai-realtime.json"}}, ensure_ascii=False))
            await bws.close()
            return
    cred = _creds()
    _span = uuid.uuid4().hex[:12]   # 284:voice_span_id——本会话全部 usage/工具事件的贯穿键
    _bok, _bspent = _budget_gate()
    if not _bok:   # 284:预算硬闸(rt_budget_usd,0=关)
        await bws.send(json.dumps({"event": -1, "payload": {"error": f"今日语音预算已用完(${_bspent:.2f})——明天再聊,或在设置调高 rt_budget_usd"}}, ensure_ascii=False))
        await bws.close()
        return
    model = "grok-voice-think-fast-1.0" if engine == "grok" else (cred.get("rt_model") or "gpt-realtime-2.1-mini")   # 114:固定型号(latest 指向会漂移)
    vc = await _fetch_book_ctx(file_rel, page)
    book = {"page": page, "page_text": vc.get("page_text") or "", "vocab": vc.get("vocab") or [],
            "figures": vc.get("figures") or [], "sel": "", "ink_strokes": None,
            "brief": vc.get("brief") or "", "brief_tags": vc.get("brief_tags") or [], "page_type": vc.get("page_type") or ""}
    catalog = await _fetch_tools_catalog("realtime_ws")
    if len(catalog) < 10:   # 工具目录拉取失败(webapp 重启窗口等)→ 重试一次;仍失败=跛脚会话(没 see_ink/看图),宁可报错别哑巴开场
        await asyncio.sleep(1.5)
        catalog = await _fetch_tools_catalog("realtime_ws")
        if len(catalog) < 10:
            await bws.send(json.dumps({"event": -1, "payload": {"error": "工具目录拉取失败(服务可能在重启),几秒后重拨"}}, ensure_ascii=False))
            await bws.close()
            return
    tools = _catalog_to_realtime_tools(catalog)
    sess = {"type": "realtime", "output_modalities": ["audio"],
            "reasoning": {"effort": cred.get("rt_effort") or "low"},   # 官方:普通语音代理从 low 起,别默认 high(延迟/成本)
            "max_output_tokens": 2048,                                  # 护栏(1–4096/inf);朗读答案本就该短,分段更好
            "instructions": _oa_instructions(book, file_rel, page),
            "audio": {"input": {"format": {"type": "audio/pcm", "rate": 24000},
                                "noise_reduction": {"type": "far_field"},   # ㉘ 官方降噪:远场(iPad 外放/桌面麦场景);回声残留/环境噪对 VAD 的干扰双降
                                # semantic_vad(2.1 招牌):按**语义**判断说完没——说话中途停顿思考不抢话(官方推荐配置;server_vad 是纯静音计时的降级品)
                                "turn_detection": {"type": "semantic_vad",
                                                   "eagerness": (cred.get("rt_eagerness") or "auto"),
                                                   "create_response": True, "interrupt_response": True},
                                "transcription": ({"model": "gpt-realtime-whisper", "language": cred["rt_lang"]}
                                                  if cred.get("rt_lang") in ("zh", "ja", "en")
                                                  else {"model": "gpt-realtime-whisper"})},   # 语言提示跟设置走(转写准确率也受益;自动=不带)
                      "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": cred.get("rt_voice") or "marin"}},   # ⚠ rate 必填:漏掉=session.update 整条被拒,会话跑默认裸配置(无人设无工具无转写=复读机)
            "tools": tools, "tool_choice": "auto", "parallel_tool_calls": False,   # 我们的工具多有副作用/顺序依赖,串行更稳
            "truncation": {"type": "retention_ratio", "retention_ratio": 0.8}}     # 官方:低频批量截断,保缓存前缀稳定(比逐轮小截强)
    if engine == "grok":   # 94/97a:按官方文档定稿(docs.x.ai voice-agent,2026-07-13 核实)——裁 OpenAI 特有字段防 session.update 整条被拒
        sess.pop("truncation", None)
        sess.pop("max_output_tokens", None)
        sess["audio"]["input"].pop("noise_reduction", None)
        # 97a:转写=grok-transcribe(设了才发事件;事件名 .updated=累积全文,非 OpenAI 的 .delta/.completed)
        _gtr = {"model": "grok-transcribe"}
        if cred.get("rt_lang") in ("zh", "ja", "en"):
            _gtr["language_hint"] = cred["rt_lang"]
        _kt = [w for w in [Path(file_rel).stem if file_rel else "", "这一页", "翻页", "做卡片", "笔记"] if w]
        _gtr["keyterms"] = _kt[:20]   # 114/123:转写热词——实测上限 20(API err "exceeds maximum of 20",官方文档的 100 是假的)
        sess["audio"]["input"]["transcription"] = _gtr
        sess["resumption"] = {"enabled": True}   # 114:断线恢复(conversation_id 30min 内重连保历史;续接=#290 二期)
        if cred.get("rt_grok_vad") == "server":   # 121 实验开关:官方推荐路("Enable server_vad for automatic, natural barge-in")
            sess["audio"]["input"]["turn_detection"] = {"type": "server_vad", "threshold": 0.85,
                                                        "silence_duration_ms": 600, "prefix_padding_ms": 333}
            # 代价:与静默停推互斥(全程推流按 $0.05/min 计费);打断/truncate 走 speech_started 现成管线
        else:
            sess["audio"]["input"]["turn_detection"] = None   # 111(用户设计):手动轮次——本地 VAD 判边界+commit+response.create
        sess["audio"]["output"].pop("voice", None)
        sess["voice"] = cred.get("rt_grok_voice") or "eve"   # 97a:官方形制=session 顶层(94 放 audio.output 无效,一直是默认 eve)
        try:   # 121:replace 发音映射(xAI 扩展:只改读音不改字幕;设置 rt_grok_replace={"词":"读法"} JSON)
            _rp = cred.get("rt_grok_replace")
            if isinstance(_rp, str) and _rp.strip():
                _rp = json.loads(_rp)
            if isinstance(_rp, dict) and _rp:
                sess["replace"] = {str(k)[:50]: str(v)[:50] for k, v in list(_rp.items())[:50]}
        except Exception:
            pass
        try:
            sess["audio"]["output"]["speed"] = max(0.7, min(1.5, float(cred.get("rt_speed") or 1.0)))   # 官方 0.7-1.5
        except Exception:
            pass
        sess["reasoning"] = {"effort": "high"}   # 117(文档定稿):high=默认,工具选择/多步判断更可靠(33 工具场景);嫌慢再调 none
        # 122(官方 schema+实测背书):tools 切嵌套 Chat-Completions 形制({"type":"function","function":{...}}),
        # 扁平 OpenAI-Realtime 式靠 xAI 兼容层撑着——排障隐患归一
        sess["tools"] = [{"type": "function", "function": {k: t0[k] for k in ("name", "description", "parameters") if k in t0}}
                         for t0 in tools if t0.get("type") == "function"]
        sess.pop("tool_choice", None)
        sess.pop("parallel_tool_calls", None)
        sess.pop("output_modalities", None)   # 不在官方 schema(实测被忽略),裁掉
    _url_extra = ""
    if engine == "grok":
        if fresh:
            _GROK_CONV["id"], _GROK_CONV["ts"] = "", 0.0   # 🧹 新话题:不续旧会话
        elif _GROK_CONV["id"] and time.time() - _GROK_CONV["ts"] < 1700:   # 官方:闲置 30min 后历史失效
            _url_extra = "&conversation_id=" + _GROK_CONV["id"]
            sys.stderr.write(f"[voice-oa] resumption 续接 conv={_GROK_CONV['id'][:20]}\n")
    try:
        ows = await websockets.connect((XAI_RT_URL if engine == "grok" else OPENAI_RT_URL) + model + _url_extra,
                                       additional_headers={"Authorization": f"Bearer {key}"},
                                       max_size=16 * 1024 * 1024, open_timeout=15)
    except Exception as ex:
        await bws.send(json.dumps({"event": -1, "payload": {"error": f"OpenAI 连接失败:{str(ex)[:80]}"}}, ensure_ascii=False))
        await bws.close()
        return
    _bridge = {"pc": None, "q": None, "pend": b""}   # 99 WebRTC 桥(纯音频面);q=None=未建(ws 音频照旧)
    _tfail = {"key": "", "n": 0}   # 100:工具熔断(Grok 实测 goto_page 错参失败→模型无限重试;GPT 也防)
    _tr_pend = {}     # 112:item_id → 最新累计全文(可修订)
    _tr_timers = {}   # 112:item_id → debounce 定稿任务
    _turn = {"n": 0}   # 104:话轮计数(图像焚旧参照)
    import array as _arr
    import collections as _coll
    import time as _tm2
    _vg = {"last": 0.0, "pre": _coll.deque(maxlen=30), "active": False, "busy": False,
           "tools_n": 0, "aEnd": 0.0}   # 111/117:本地 VAD 状态+并行工具在飞计数+输出音频播放结束估算
    _gk = {"t0": time.time(), "in_b": 0, "out_b": 0}   # 110:grok 自记账(推送/接收音频字节+连接时长)
    _conv = {"id": ""}   # 120:conversation.created 的 id(resumption 续接钥匙,完整续接=#290)
    _img_items = []    # 104:(turn, item_id) 直喂图记账
    played = {"id": None, "bytes": 0}   # 正在播的 assistant 音频 item
    pend_trunc = {"id": None, "fb": 0}  # 打断后待 truncate:等前端回报**真实已播毫秒**(600ms 兜底用已转发字节——它远超实际已播,官方语义是"用户实际听到的毫秒")
    cur_a = {"txt": ""}

    _SLOW_TOOLS = {"deep_think", "web_search", "search_image", "search_video", "see_page", "see_figure", "see_ink", "summarize_section", "recall_study"}

    async def _tool(name: str, args: dict, call_id: str):
        await bws.send(json.dumps({"event": "tool_status", "payload": {"status": "running", "tool": name, "label": name}}, ensure_ascii=False))
        if engine == "grok" and name in _SLOW_TOOLS and _vg.get("tools_n", 0) <= 1:
            try:   # 120:force_message 垫话(xAI 扩展:硬编码 TTS 即时出声,自带完整响应生命周期,不跟 response.create)
                await ows.send(json.dumps({"type": "conversation.item.create", "item": {
                    "type": "force_message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "稍等,我处理一下。"}]}}, ensure_ascii=False))
            except Exception:
                pass
        out, ok, label, took = "", True, name, None
        slim_full = None   # #img/anki(2026-07-21 用户实锤"制卡预览不显示"):制卡等 cards 完整体,给 tool_status 前端渲预览卡用
        _silent = [False]   # 113:展示型工具静默入库(卡片已显示,本轮不让模型发言)
        try:
            if name == "recall_study":
                span = str(args.get("span") or "today").lower()
                out = _study_digest("week" if span.startswith("w") else "today") or "(记录为空——这段时间还没有学习记录,如实告诉用户)"
                label = "回顾学习"
            elif name == "deep_think":
                out = await _oa_deep(str(args.get("question") or ""), file_rel, book.get("page") or page)
                label = "深度思考"
            else:
                cmd = json.dumps({"tool": name, "args": args}, ensure_ascii=False)
                ctx = {"file_rel": file_rel, "page": book.get("page") or page}
                if _creds().get("rt_image") and engine != "grok":
                    ctx["_want_vision"] = 1   # ㉗:看图/看笔迹类工具跳过本地转述,原图穿透 → input_image 直喂 GPT(94:grok 无视觉,恒走文字转述)
                if book.get("ink_strokes"):
                    ctx["ink"] = book["ink_strokes"]
                if book.get("view_shot"):
                    ctx["view_image"] = book["view_shot"]   # EPUB 笔迹合成图(前端 syncInk 拍;grok 无视觉 → webapp _viewshot_result 走视觉模型转述这张图)
                if book.get("sel"):
                    ctx["selection"] = book["sel"]
                async with httpx.AsyncClient(base_url=WEBAPP, headers=_webapp_headers(), timeout=180) as hc:
                    # ⚠不带 rtc_call_id:此处 call_id 是**函数调用 ID**非 WebRTC 会话 ID(58 曾误加,外部审核揪出);
                    # WS 模式图像走下方 ows 直喂 input_image,不经 sideband
                    r = await hc.post("/api/assistant/voice-tool", json={"cmd": cmd, "ctx": ctx})
                    d = r.json()
                ok = bool(d.get("ok")); label = d.get("label") or name; took = d.get("took_s")
                _subs = ((d.get("result") or {}).get("sub_steps") or d.get("sub_steps") or [])[:12]   # 137:工具内部子步骤 → 外层卡的步骤(不另起卡)
                res = d.get("result") or {}
                if isinstance(res, dict) and res.get("silent") and not _creds().get("rt_tool_reply"):
                    _silent[0] = True   # 113(用户实测):silent gate 当年只做在 RTC 版——WS 版(Grok)工具后无条件 create=静默失效
                ca = res.get("client_action")
                if isinstance(ca, dict) and ca.get("fn"):
                    await bws.send(json.dumps({"event": "client_action", "payload": ca}, ensure_ascii=False))
                vis = res.pop("_vision", None) if isinstance(res, dict) else None   # 原图(b64)绝不进文本 output(会被截成烂 JSON)
                if isinstance(res, dict) and not vis and res.get("_fed_images"):
                    vis = res.get("_fed_images")   # 前端截图路径的图在 _fed_images(同样直喂 GPT 看)
                slim, slim_full, out = _prep_tool_result(res, d.get("tool") or name)   # 三引擎共用(见 _prep_tool_result)
                # ㉕ 图像直喂(凭证 rt_image 开关,⚪格式按 GA conversation item 推定待实测):看图类工具返回的渲染图
                # 直接给 GPT 自己看(2.1 原生视觉),不再经 Claude 文字转述;失败(模型不支持/格式不对)由 error 事件暴露,关掉开关即回文字链路
                if vis and _creds().get("rt_image") and engine != "grok":
                    try:
                        for _vi, v in enumerate(vis[:2]):
                            _iid2 = f"img{_turn['n']}x{len(_img_items)}{_vi}"   # 104:自带 id → 新话轮焚旧图省 token
                            _img_items.append((_turn["n"], _iid2))
                            await ows.send(json.dumps({"type": "conversation.item.create", "item": {
                                "id": _iid2, "type": "message", "role": "user",
                                "content": [{"type": "input_image", "detail": "high",   # 显式高清(auto 的降档不赌)
                                             "image_url": f"data:{v.get('media_type', 'image/png')};base64,{v['b64']}"}]}}))
                        out += "\n(相关图像已直接发给你,请看图回答)"
                    except Exception as ex:
                        sys.stderr.write(f"[voice-oa img] {str(ex)[:100]}\n")
                elif vis:
                    out += "\n(该工具产出了图像,但「图像输入」开关未开,只能按上面的文本回答;要看图请让用户在语音设置里打开图像输入)"
        except Exception as ex:
            ok, out = False, json.dumps({"error": str(ex)[:200]}, ensure_ascii=False)
        # 100(Grok 实测 goto_page 错参无限重试):同名同参连续失败=熔断。≥3 换强提示;≥6 不 create 硬断循环(用户说话重启)
        _tk = name + "|" + json.dumps(args, ensure_ascii=False, sort_keys=True)[:120]
        if not ok:
            _tfail["n"] = _tfail["n"] + 1 if _tfail["key"] == _tk else 1
            _tfail["key"] = _tk
        else:
            _tfail["key"], _tfail["n"] = "", 0
        if not ok and _tfail["n"] >= 3:
            out = json.dumps({"error": "这个工具已连续失败多次,系统已熔断。**禁止再调用它**——直接告诉用户『这个操作暂时做不了』,然后安静等用户说话。"}, ensure_ascii=False)
        try:
            await ows.send(json.dumps({"type": "conversation.item.create",
                                       "item": {"type": "function_call_output", "call_id": call_id, "output": out}}))
            if engine == "grok":
                _vg["tools_n"] = max(0, _vg["tools_n"] - 1)   # 119b(实测bug):回填即出飞——117 把出飞放在判定后,单工具看到"自己还在飞"=永不 create,每个工具都要用户追问一次才出结果
            if not ok and _tfail["n"] >= 6:
                sys.stderr.write(f"[voice-oa] 工具熔断硬断: {_tk[:80]} ×{_tfail['n']}\n")
            elif _silent[0] and ok:
                sys.stderr.write(f"[voice-oa] 工具静默入库(不 create): {name}\n")   # 113:与 RTC 版 no_create 同语义
            elif engine == "grok" and _vg["tools_n"] > 0:
                sys.stderr.write(f"[grok-diag] 工具回填暂不 create(在飞还有 {_vg['tools_n']} 个)\n")   # 117:收齐并行工具再单次 create(末位工具负责 create)
            else:
                if engine == "grok":   # 117:工具续答前等上段音频播完(官方提醒:立即 create=语音重叠)
                    _wait0 = _vg["aEnd"] - time.time()
                    if 0 < _wait0 < 20:
                        await asyncio.sleep(_wait0)
                await ows.send(json.dumps({"type": "response.create"}))   # 必须手发,模型才会用结果继续说
                if engine == "grok":
                    sys.stderr.write(f"[grok-diag] create(tool:{name})\n")   # 115:双响应取证
        except Exception:
            pass
        await bws.send(json.dumps({"event": "tool_status", "payload": {
            "status": "done" if ok else "error", "tool": name, "label": label, "took_s": took,
            "args": args, "rag": out[:1600],
            "result": _tool_preview_result(slim_full)}}, ensure_ascii=False))   # 制卡完整体(前端渲预览卡)
        if engine == "grok":
            _gk["tools"] = _gk.get("tools", 0) + 1   # 114:账本补工具计数(出飞已在回填处前移,119b)
        _ledger_tool(engine, _span, call_id, name, ok, took or 0)   # 284
        _vlog("tool", tool=name, label=label, page=book.get("page") or page, book=file_rel, ok=ok,
              args=args, brief=out[:300])

    async def up():
        abuf = bytearray()   # 攒 100ms 再 append(前端 20ms/帧 → 50 msg/s 太碎;24k·16bit·100ms=4800B)
        # 109(用户设计:静默别烧钱):grok 按音频时长计费——静音也是音频,持续推流=持续计费。
        # RMS 语音门:静默段不转发(只进 0.6s 预滚缓冲),开口时先补发预滚(不吃句首)+800ms hangover(不切句尾)。
        def _rms(b):
            try:
                a = _arr.array("h", bytes(b[:len(b) - (len(b) % 2)]))
                return (sum(x * x for x in a) / max(1, len(a))) ** 0.5
            except Exception:
                return 9999.0

        async def _grok_commit():   # 116:真提交(延迟窗到期)——补尾静音→flush→commit+create
            _vg["endT"] = None
            sys.stderr.write("[grok-diag] turn_end(延迟窗到期,提交)\n")
            abuf.extend(b"\x00" * 4800)   # 120:尾静音 0.5→0.1s(官方只要求 commit;自加尾静音直接贡献延迟+计费)
            if abuf:
                try:
                    await ows.send(json.dumps({"type": "input_audio_buffer.append",
                                               "audio": base64.b64encode(bytes(abuf)).decode()}))
                except Exception:
                    return
                abuf.clear()
            try:
                await ows.send(json.dumps({"type": "input_audio_buffer.commit"}))
                _cr = {"type": "response.create"}
                # 117/118/120:per-response instructions 是**整体替换**(官方原文:"replaces the session-level
                # instructions for this response only")——必须带**完整 SP**,否则 see_ink 铁律/工具规则等每轮失效
                # (行为怪异根源之一);xAI 计价无 token 维度=全文注入免费。附加本轮实时状态。
                _bits = [f"当前第 {book.get('page') or page} 页"]
                _brief0 = _brief_line(book)   # Phase2:简述优先替整页
                _pt0 = (book.get("page_text") or "")[:1800]
                if _brief0:
                    _bits.append(f"{_brief0}(要点摘要;需要完整原文再 read_page(page:N))")
                elif _pt0:
                    _bits.append(f"本页正文(直接参考,不用 read_page 读当前页):「{_pt0}」")
                if book.get("sel"):
                    _bits.append(f"用户选中:「{str(book['sel'])[:400]}」")
                if book.get("ink_strokes"):
                    _bits.append("本页有用户手写笔迹(问到时调 see_ink)")
                try:
                    _sp_full = _oa_instructions(book, file_rel, book.get("page") or page)
                except Exception:
                    _sp_full = "你是简短口语化的伴读助手。"
                _cr["response"] = {"instructions": _sp_full + "\n【本轮实时状态】" + ";".join(_bits) + "。结合最新状态回答本轮;需要**其它页**才用 read_page。",
                                   "metadata": {"src": "turn_end"}}   # 120:官方取证钩子(created/done 回显)
                book.pop("_dirty", None)
                await ows.send(json.dumps(_cr, ensure_ascii=False))
                _vg["pend_resp"] = True   # 116:create 已发但 response.created 未到的竞态窗(打断要覆盖它)
                sys.stderr.write("[grok-diag] create(turn_end+状态instructions)\n")
            except Exception:
                pass

        async def _grok_end_timer():   # 116:说完≠立即提交——450ms 反悔窗,句中停顿回来=取消提交继续同轮
            try:
                await asyncio.sleep(0.3)   # 120:反悔窗 0.45→0.3s
            except asyncio.CancelledError:
                return
            await _grok_commit()

        async def _grok_turn_start():   # 111:本地判定开口——打断进行中的响应+前端清播放+话轮计数/焚图(原 speech_started 职责)
            _vg["active"] = True
            # 117(文档 9.3 定稿):状态注入改 per-response instructions($0,只影响本轮,官方确认之后自动恢复
            # session instructions)——替代 114 的 assistant item($0.004/条)。内容在 _grok_commit 组装。
            _vg["aEnd"] = 0.0   # 117:打断=播放已清,别让工具续答再等
            if _vg["busy"] or _vg.get("pend_resp"):   # 116:create 已发但 created 未到的窗口也要打断(旧响应照跑=双答来源之一)
                try:
                    await ows.send(json.dumps({"type": "response.cancel"}))
                except Exception:
                    pass
                _vg["pend_resp"] = False
                _vg["cancelled_id"] = _vg.get("resp_id") or ""   # 120:cancel 后按 response_id 丢弃迟到音频(残尾音)
            try:
                await bws.send(json.dumps({"event": 450, "payload": {}}, ensure_ascii=False))
            except Exception:
                pass
            if played["id"]:   # 120(P0):打断必须 truncate——否则被打断的全文(含没听到的尾巴)留在上下文,追问对不齐
                pend_trunc["id"], pend_trunc["fb"] = played["id"], int(played["bytes"] / 48)
                played.update({"id": None, "bytes": 0})

                async def _trunc_fb_g(pid=pend_trunc["id"], ms=pend_trunc["fb"]):
                    await asyncio.sleep(0.6)
                    if pend_trunc.get("id") == pid:
                        pend_trunc["id"] = None
                        try:
                            await ows.send(json.dumps({"type": "conversation.item.truncate", "item_id": pid,
                                                       "content_index": 0, "audio_end_ms": ms}))
                        except Exception:
                            pass
                asyncio.create_task(_trunc_fb_g())
            if _bridge["q"] is not None:   # 桥模式:清未播缓冲
                _bridge["pend"] = b""
                try:
                    while True:
                        _bridge["q"].get_nowait()
                except Exception:
                    pass
            _turn["n"] += 1
            _keep = [it for it in _img_items if it[0] >= _turn["n"] - 2]
            for _ep0, _iid0 in _img_items:
                if _ep0 < _turn["n"] - 2:
                    try:
                        await ows.send(json.dumps({"type": "conversation.item.delete", "item_id": _iid0}))
                    except Exception:
                        pass
            _img_items[:] = _keep

        async def _feed_audio(chunk):
            if engine == "grok" and _creds().get("rt_grok_vad") == "server":
                await _feed_audio_raw(chunk)   # 121:server_vad 实验模式=全程推流,轮次/打断交服务端(计费 $0.05/min 全程)
                return
            if engine == "grok":   # 111/116:本地 VAD 判轮次+450ms 延迟提交(句中喘气不切轮="一句话答好几遍"根修)
                _nw = _tm2.time()
                _voiced = _rms(chunk) > 350
                if _voiced:
                    _vg["last"] = _nw
                if not _vg["active"]:
                    if _voiced:
                        if _vg.get("endT"):   # 反悔窗内回来了:撤销提交,同轮继续(不 turn_start=不 cancel 不焚图)
                            _vg["endT"].cancel()
                            _vg["endT"] = None
                            sys.stderr.write("[grok-diag] 句中停顿回归,同轮继续\n")
                        else:
                            await _grok_turn_start()
                        _vg["active"] = True
                        for c0 in _vg["pre"]:   # 预滚补发,句首不丢(反悔窗期间的静音帧也在 pre,音频连续)
                            abuf.extend(c0)
                        _vg["pre"].clear()
                    else:
                        _vg["pre"].append(bytes(chunk))
                        return
                else:
                    if _nw - _vg["last"] > 0.6:   # 120:hangover 0.8→0.6s(静态延迟≈0.9s,对齐 2.1 手感)
                        await _feed_audio_raw(chunk)
                        _vg["active"] = False
                        _vg["endT"] = asyncio.create_task(_grok_end_timer())
                        return
            await _feed_audio_raw(chunk)

        async def _feed_audio_raw(chunk):
            if engine == "grok":
                _gk["in_b"] += len(chunk)
            abuf.extend(chunk)
            if len(abuf) >= 4800:
                try:
                    await ows.send(json.dumps({"type": "input_audio_buffer.append",
                                               "audio": base64.b64encode(bytes(abuf)).decode()}))
                except Exception:
                    return
                abuf.clear()

        async def _bridge_setup(sdp):
            """99(用户设计):Pi 上的 WebRTC 桥——浏览器媒体走 WebRTC(<audio> 播放=浏览器 AEC 参考,
            外放回声治本,同 #280 原理),Pi 侧转回引擎 WS。纯音频面:事件/字幕/控制照旧走本 ws。"""
            try:
                import av
                from aiortc import RTCPeerConnection, RTCSessionDescription
                pc = RTCPeerConnection()
                _bridge["pc"] = pc
                q = asyncio.Queue(maxsize=600)   # ~12s 音频缓冲上限(防泄漏;满了丢最旧)
                # 103b(用户实测 Grok 全哑):下行改道**必须等 connected**——offer 一到就改道的话,
                # 桥连不成(ICE 不通/answer 未达)时音频全进无人播放的队列=全哑。connected 前照走 ws。

                @pc.on("track")
                def _on_track(track):
                    async def _pump():
                        resampler = av.AudioResampler(format="s16", layout="mono", rate=24000)
                        while True:
                            try:
                                frame = await track.recv()
                            except Exception:
                                break
                            try:
                                for f2 in resampler.resample(frame):
                                    await _feed_audio(bytes(f2.planes[0]))
                            except Exception:
                                pass
                    asyncio.create_task(_pump())

                @pc.on("connectionstatechange")
                def _on_cs():
                    if _bridge.get("pc") is not pc:
                        return
                    if pc.connectionState == "connected":
                        _bridge["q"] = q   # 103b:真正连通才改道下行(与前端 connected 才停发麦一致)
                        sys.stderr.write("[voice-oa] 桥已连通,音频改道 WebRTC\n")
                    elif pc.connectionState in ("failed", "closed", "disconnected"):
                        _bridge["q"] = None   # 99:桥断→下行回 ws(前端同步回退,双侧一致)
                        _bridge["pend"] = b""
                        sys.stderr.write("[voice-oa] 桥断开,音频回退 ws\n")

                pc.addTrack(_mk_bridge_track(q))
                await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
                await pc.setLocalDescription(await pc.createAnswer())
                await bws.send(json.dumps({"event": "bridge_answer", "payload": {"sdp": pc.localDescription.sdp}}, ensure_ascii=False))
                sys.stderr.write("[voice-oa] 桥已建(WebRTC 音频面)\n")
            except Exception as ex:
                _bridge["q"] = None
                sys.stderr.write(f"[voice-oa] 桥建立失败(前端将回退 ws 音频): {str(ex)[:120]}\n")

        async for msg in bws:
            if isinstance(msg, (bytes, bytearray)):
                await _feed_audio(msg)
                continue
            try:
                j = json.loads(msg)
            except Exception:
                continue
            t = j.get("type")
            if engine == "grok" and t in ("page", "state", "ink"):
                # 114/120:grok 状态单通道——只落 book+标脏(instructions 每轮免费注入),**continue 拦掉**通用
                # system item 注入(120 审计:漏 continue=双通道双份钱,且 book["page"] 只有通用分支更新=instructions 页码陈旧)
                book["_dirty"] = True
                if t == "page":
                    try:
                        _np0 = int(j.get("page") or 0)
                    except Exception:
                        _np0 = 0
                    if _np0 and _np0 != book.get("_pt_page"):
                        book["_pt_page"] = _np0
                        book["page"] = _np0

                        async def _refresh_pt(np1=_np0):   # 118:翻页后台刷新页正文(供免费 instructions 注入)
                            try:
                                vc2 = await _fetch_book_ctx(file_rel, np1)
                                if book.get("_pt_page") == np1:   # 没被更新的翻页顶掉才写
                                    book["page_text"] = vc2.get("page_text") or ""
                                    book["vocab"] = vc2.get("vocab") or []
                                    book["figures"] = vc2.get("figures") or []
                                    try:   # 121:keyterms mid-session 更新(官方支持)——本页生词进转写热词
                                        _kt2 = [w for w in [Path(file_rel).stem if file_rel else "", "这一页", "翻页", "做卡片", "笔记"] if w]
                                        _kt2 += [str(v)[:50] for v in (book.get("vocab") or [])[:15]]
                                        await ows.send(json.dumps({"type": "session.update", "session": {
                                            "audio": {"input": {"transcription": {"model": "grok-transcribe",
                                                     **({"language_hint": _creds().get("rt_lang")} if _creds().get("rt_lang") in ("zh", "ja", "en") else {}),
                                                     "keyterms": _kt2[:20]}}}}}, ensure_ascii=False))   # 123:实测上限 20
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                        asyncio.create_task(_refresh_pt())
                elif t == "note":   # 统一注入端口(references/voice-context-injection.md):前端通告→存 book,下次开口(450)注入
                    _nt0 = (j.get("text") or "").strip()[:800]
                    if _nt0:
                        book.setdefault("ctx_notes", []).append(_nt0)
                        if len(book["ctx_notes"]) > 10:
                            book["ctx_notes"] = book["ctx_notes"][-10:]
                elif t == "state":
                    book["sel"] = (j.get("sel") or "").strip()[:400]
                elif t == "ink":
                    try:
                        _ip0 = int(j.get("page") or 0)
                    except Exception:
                        _ip0 = 0
                    if _ip0 and _ip0 == (book.get("page") or page):
                        book["ink_strokes"] = (j.get("strokes") or [])[:60]
                        book["view_shot"] = j.get("shot")   # EPUB 笔迹合成图(前端 syncInk 拍;PDF/空=None 自动清)→ see_ink 用
                continue
            if t == "finish":
                return
            if t == "bridge_offer":   # 99:WebRTC 桥信令(纯音频面)
                asyncio.create_task(_bridge_setup(j.get("sdp") or ""))
                continue
            if t == "played_ms":   # 打断时前端回报的真实已播毫秒 → 精确 truncate(官方语义)
                pid = pend_trunc.get("id")
                if pid:
                    pend_trunc["id"] = None
                    try:
                        await ows.send(json.dumps({"type": "conversation.item.truncate", "item_id": pid,
                                                   "content_index": 0, "audio_end_ms": int(j.get("ms") or 0)}))
                    except Exception:
                        pass
            elif t in ("cancel", "tool_abort"):
                try:
                    await ows.send(json.dumps({"type": "response.cancel"}))
                except Exception:
                    pass
            elif t == "page":
                np = int(j.get("page") or 0)
                f2 = j.get("file") or file_rel
                if np and np != book.get("page"):
                    vc2 = await _fetch_book_ctx(f2, np)
                    book.update({"page": np, "page_text": vc2.get("page_text") or "",
                                 "vocab": vc2.get("vocab") or [], "figures": vc2.get("figures") or [],
                                 "brief": vc2.get("brief") or "", "brief_tags": vc2.get("brief_tags") or [],
                                 "page_type": vc2.get("page_type") or ""})
                    # ⚠ 不改 instructions(改稳定前缀=整个 prompt cache 作废,cached $0.06/M vs 全价 $0.6——
                    # 官方成本指南明确反对;豆包⑭同款教训):新页内容走**对话内 system 增量消息**,前缀整场稳定
                    _brief2 = _brief_line(book)   # Phase2:翻页增量也简述优先替整页
                    if _brief2:
                        note = f"(用户翻到第 {np} 页。{_brief2}(要点摘要;需要完整原文再 read_page(page:N))"
                    else:
                        note = f"(用户翻到第 {np} 页,本页文字内容:{(book['page_text'] or '(本页无文字层)')[:1200]}"
                    if book.get("vocab"):
                        note += ";本页未掌握生词:" + "、".join(book["vocab"][:20])
                    note += "。之前页面的内容已翻过去了,回答以本条为准)"
                    try:
                        await ows.send(json.dumps({"type": "conversation.item.create", "item": {
                            "type": "message", "role": "system",
                            "content": [{"type": "input_text", "text": note}]}}, ensure_ascii=False))
                        sys.stderr.write(f"[voice-oa] 翻页增量 → p{np}({len(book['page_text'])}字)\n")
                    except Exception:
                        pass
            elif t == "state":   # 选中/chip 同步(字段与豆包版同源:sel/focus/figs 数量)
                sel = (j.get("sel") or "").strip()
                book["sel"] = sel[:400]
                if sel == book.get("_sel_fp"):   # 去重:同一选中别反复注入
                    continue
                book["_sel_fp"] = sel
                note0 = (f"用户当前选中了「{sel[:200]}」(他说『这段/我选的』就指它)" if sel
                         else "用户当前没有选中文字")
                try:   # 状态=对话内 system 增量消息(与豆包 510 同哲学:前缀不动,历史缓存全保)
                    await ows.send(json.dumps({"type": "conversation.item.create", "item": {
                        "type": "message", "role": "system",
                        "content": [{"type": "input_text", "text": "(状态更新:" + note0 + ";状态记录,不要回应本条)"}]}}, ensure_ascii=False))
                    sys.stderr.write(f"[voice-oa] 状态注入 sel={len(sel)}字\n")
                except Exception:
                    pass
            elif t == "ink":     # 通话中圈画(字段与豆包版同源:page/strokes;⚠ink 消息没有 sel 字段,别碰 book['sel'])
                try:
                    ip = int(j.get("page") or 0)
                except Exception:
                    ip = 0
                strokes = j.get("strokes") or []
                if ip and ip == (book.get("page") or page):
                    book["ink_strokes"] = strokes[:60]
                    book["view_shot"] = j.get("shot")   # EPUB 笔迹合成图(前端 syncInk 拍;PDF/空=None 自动清)→ see_ink 用
                    # 去重(㉘e):画一幅图前端会推好几次(每次落笔防抖后),每次都注入=模型每回合评论一次笔迹。
                    # 指纹=页+笔画数+末笔末点坐标(粗略但够辨"真变了")。
                    try:
                        _lp = (strokes[-1].get("p") or [[0, 0]])[-1] if strokes else [0, 0]
                        fp1 = f"{ip}:{len(strokes)}:{round(float(_lp[0]), 3)}:{round(float(_lp[1]), 3)}"
                    except Exception:
                        fp1 = f"{ip}:{len(strokes)}"
                    if fp1 == book.get("_ink_fp"):
                        continue
                    book["_ink_fp"] = fp1
                    # 措辞条件化(㉘e):旧版"必须立即调用 see_ink"被当成**行动指令**,模型在下个回合(含 VAD 误触发的
                    # 空回合)主动评论笔迹、连评两次——状态记录≠行动请求,能力提示保留,主动性去掉。
                    note1 = ((f"用户在本页的手写笔迹有更新(共 {len(strokes)} 笔)。这只是状态记录——**不要对本条做任何回应、"
                              "不要主动评论或提起他画了什么**。只有当他之后问『我写的/我画的/我圈的/这个对不对』时,"
                              "才调 see_ink 工具看笔迹合成图回答;那时绝不要说你看不到,也不要让他粘贴或截图。")
                             if strokes else ("用户已擦掉本页**全部**笔迹,当前页面没有任何手写内容——"
                                              "**不要再调 see_ink**(看了也是空白);他若再提到笔迹,直接说已经擦掉了。"
                                              "直到下一条『笔迹有更新』的状态消息出现前,这一直成立。(状态记录,不要回应本条。)"))
                    try:
                        await ows.send(json.dumps({"type": "conversation.item.create", "item": {
                            "type": "message", "role": "system",
                            "content": [{"type": "input_text", "text": "(状态更新:" + note1 + ")"}]}}, ensure_ascii=False))
                        sys.stderr.write(f"[voice-oa] 圈画注入 p{ip} strokes={len(strokes)}\n")
                    except Exception:
                        pass
            elif t == "text" and (j.get("content") or "").strip():   # 打字提问(不说话时)
                if engine == "grok":
                    _gk["items"] = _gk.get("items", 0) + 1
                try:
                    await ows.send(json.dumps({"type": "conversation.item.create", "item": {
                        "type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": j["content"][:2000]}]}}))
                    await ows.send(json.dumps({"type": "response.create"}))
                except Exception:
                    pass

    async def down():
        async for raw in ows:
            try:
                e = json.loads(raw)
            except Exception:
                continue
            t = e.get("type")
            if t == "response.output_audio.delta":
                if engine == "grok" and _vg.get("cancelled_id") and e.get("response_id") == _vg["cancelled_id"]:
                    continue   # 120:已取消响应的迟到音频=残尾,丢弃
                try:
                    pcm = base64.b64decode(e.get("delta") or "")
                except Exception:
                    continue
                played["id"] = e.get("item_id") or played["id"]
                played["bytes"] += len(pcm)
                if engine == "grok":
                    _gk["out_b"] += len(pcm)   # 110:接收音频记账
                    _nw3 = time.time()
                    _vg["aEnd"] = max(_vg["aEnd"], _nw3) + len(pcm) / 48000.0   # 117:播放结束估算(工具续答仲裁用)
                if _bridge["q"] is not None:   # 99:桥模式=音频改道 WebRTC 轨(960B=20ms 定长块),ws 不再发 binary
                    _bridge["pend"] += pcm
                    while len(_bridge["pend"]) >= 960:
                        try:
                            _bridge["q"].put_nowait(_bridge["pend"][:960])
                        except asyncio.QueueFull:
                            try:
                                _bridge["q"].get_nowait()
                                _bridge["q"].put_nowait(_bridge["pend"][:960])
                            except Exception:
                                pass
                        _bridge["pend"] = _bridge["pend"][960:]
                else:
                    await bws.send(pcm)   # PCM16 24k 裸转发,前端 playPcm 原样吃
            elif t == "response.output_audio_transcript.delta":
                d0 = e.get("delta") or ""
                cur_a["txt"] += d0
                await bws.send(json.dumps({"event": 550, "payload": {"content": d0}}, ensure_ascii=False))
            elif t == "input_audio_buffer.speech_started":
                _turn["n"] += 1
                _keep = [it for it in _img_items if it[0] >= _turn["n"] - 2]   # 104:焚上上轮及更早的直喂图
                for _ep0, _iid0 in _img_items:
                    if _ep0 < _turn["n"] - 2:
                        try:
                            await ows.send(json.dumps({"type": "conversation.item.delete", "item_id": _iid0}))
                        except Exception:
                            pass
                _img_items[:] = _keep
                if _bridge["q"] is not None:   # 99:桥模式打断=清桥缓冲(未播的丢弃)
                    _bridge["pend"] = b""
                    try:
                        while True:
                            _bridge["q"].get_nowait()
                    except Exception:
                        pass
                await bws.send(json.dumps({"event": 450, "payload": {}}, ensure_ascii=False))   # 前端清播放队列+回报已播毫秒
                if played["id"]:
                    pend_trunc["id"], pend_trunc["fb"] = played["id"], int(played["bytes"] / 48)
                    played.update({"id": None, "bytes": 0})

                    async def _trunc_fb(pid=pend_trunc["id"], ms=pend_trunc["fb"]):
                        await asyncio.sleep(0.6)   # 前端 600ms 没回报(断连等)→ 按已转发字节兜底截
                        if pend_trunc.get("id") == pid:
                            pend_trunc["id"] = None
                            try:
                                await ows.send(json.dumps({"type": "conversation.item.truncate", "item_id": pid,
                                                           "content_index": 0, "audio_end_ms": ms}))
                            except Exception:
                                pass
                    asyncio.create_task(_trunc_fb())
            elif t in ("conversation.item.input_audio_transcription.completed",
                       "conversation.item.input_audio_transcription.updated"):
                # 112(用户规范):xAI 的 updated/completed=同 item 的**可修订累计全文**——对同一 item_id 覆盖式更新
                # (不追加、不因已见过而 return——101 取首个 completed 会把更完整的迟到修订挡掉);
                # speech_stopped 后迟到修订照收,以最后一次为准,debounce 0.8s 后定稿落库。
                txt = (e.get("transcript") or "").strip()
                _iid = e.get("item_id") or "~"
                if txt and engine == "grok" and t.endswith(".completed"):
                    # 117(最新协议正式提供 completed):到达即定稿;updated 的 debounce 只作无 completed 的兜底
                    _t_old = _tr_timers.pop(_iid, None)
                    if _t_old:
                        _t_old.cancel()
                    _tr_pend.pop(_iid, None)
                    await bws.send(json.dumps({"event": 451, "payload": {"results": [
                        {"text": txt, "is_interim": False, "iid": _iid}]}}, ensure_ascii=False))
                    _vlog("q", text=txt, page=book.get("page") or page, book=file_rel)
                elif txt and engine == "grok":
                    _tr_pend[_iid] = txt
                    await bws.send(json.dumps({"event": 451, "payload": {"results": [
                        {"text": txt, "is_interim": True, "iid": _iid}]}}, ensure_ascii=False))
                    _t_old = _tr_timers.pop(_iid, None)
                    if _t_old:
                        _t_old.cancel()

                    async def _tr_fin(iid0=_iid):
                        try:
                            await asyncio.sleep(0.8)
                        except asyncio.CancelledError:
                            return
                        txt0 = _tr_pend.pop(iid0, "")
                        _tr_timers.pop(iid0, None)
                        if txt0:
                            try:
                                await bws.send(json.dumps({"event": 451, "payload": {"results": [
                                    {"text": txt0, "is_interim": False, "iid": iid0}]}}, ensure_ascii=False))
                            except Exception:
                                pass
                            _vlog("q", text=txt0, page=book.get("page") or page, book=file_rel)
                    _tr_timers[_iid] = asyncio.create_task(_tr_fin())
                elif txt and t.endswith(".completed"):   # OpenAI 形制:一轮一个 completed,即时定稿
                    await bws.send(json.dumps({"event": 451, "payload": {"results": [{"text": txt, "is_interim": False}]}}, ensure_ascii=False))
                    _vlog("q", text=txt, page=book.get("page") or page, book=file_rel)
            elif t == "response.function_call_arguments.done":
                name = e.get("name") or ""
                try:
                    args = json.loads(e.get("arguments") or "{}")
                except Exception:
                    args = {}
                if name == "wait_for_user":   # 静音 no-op:回空 output 结束本轮,**不发 response.create**(不出声)
                    try:
                        await ows.send(json.dumps({"type": "conversation.item.create",
                                                   "item": {"type": "function_call_output",
                                                            "call_id": e.get("call_id") or "", "output": "{}"}}))
                    except Exception:
                        pass
                elif name:
                    _vg["tools_n"] += 1   # 117:并行工具收齐——最后一个回填才 create
                    asyncio.create_task(_tool(name, args if isinstance(args, dict) else {}, e.get("call_id") or ""))
            elif t == "response.created":
                _vg["busy"] = True
                _vg["pend_resp"] = False
                _vg["resp_id"] = ((e.get("response") or {}).get("id")) or ""
                if engine == "grok":
                    sys.stderr.write(f"[grok-diag] response.created id={_vg['resp_id'][:14]} meta={(e.get('response') or {}).get('metadata')}\n")
            elif t == "response.done":
                _vg["busy"] = False
                if engine == "grok":
                    sys.stderr.write(f"[grok-diag] response.done status={(e.get('response') or {}).get('status')}\n")
                if cur_a["txt"].strip():
                    _vlog("a", text=cur_a["txt"][:2000], page=book.get("page") or page, book=file_rel)
                    cur_a["txt"] = ""
                try:
                    u = (e.get("response") or {}).get("usage") or {}
                    if u and engine != "grok":
                        _oa_log_usage(u, engine="openai_ws", span=_span, resp_id=(e.get("response") or {}).get("id") or "")
                    elif u and engine == "grok":   # 284:grok 每响应也入账(est_usd=0,时长费在会话级)
                        _ledger_usage("grok", _span, (e.get("response") or {}).get("id") or "",
                                      model=model, in_tok=int(u.get("input_tokens") or 0), out_tok=int(u.get("output_tokens") or 0))
                except Exception:
                    pass
                # 安全(#284 加固):拨号只查一次挡不住会话中途超支——每轮记账后复查,超了就收场。
                # return 退出 down() → asyncio.wait FIRST_COMPLETED 触发 → finally 关 ows/bws。
                _bok2, _bspent2 = _budget_gate()
                if not _bok2:
                    sys.stderr.write(f"[voice-oa] 预算超支 ${_bspent2:.2f},中断通话\n")
                    try:
                        await bws.send(json.dumps({"event": -1, "payload": {"error": f"今日语音预算已用完(${_bspent2:.2f})——通话结束,明天再聊或调高 rt_budget_usd"}}, ensure_ascii=False))
                    except Exception:
                        pass
                    return
                await bws.send(json.dumps({"event": 359, "payload": {}}, ensure_ascii=False))
                played.update({"id": None, "bytes": 0})
            elif t == "error":
                m0 = str(((e.get("error") or {}) or {}).get("message") or "")[:120]
                sys.stderr.write(f"[voice-oa] err {m0}\n")
                if "cancel" not in m0.lower():   # 无进行中 response 时 cancel 的报错是常态噪声,不上屏
                    await bws.send(json.dumps({"event": -1, "payload": {"error": m0}}, ensure_ascii=False))

    try:
        first = json.loads(await asyncio.wait_for(ows.recv(), timeout=15))
        if first.get("type") == "error":   # 额度不足/鉴权失败等 → 明确回前端,别让它以为莫名断线
            em = str(((first.get("error") or {}) or {}).get("message") or "OpenAI 拒绝了连接")[:140]
            code = ((first.get("error") or {}) or {}).get("code") or ""
            hint = "(OpenAI 账户额度不足,去 platform.openai.com 充值/检查这个 key 的额度)" if "quota" in str(code).lower() else ""
            await bws.send(json.dumps({"event": -1, "payload": {"error": "GPT Realtime:" + em + hint}}, ensure_ascii=False))
            sys.stderr.write(f"[voice-oa] 首帧 error: {em}\n")
            return
        if first.get("type") != "session.created":
            sys.stderr.write(f"[voice-oa] 首帧异常: {str(first)[:150]}\n")
        await ows.send(json.dumps({"type": "session.update", "session": sess}, ensure_ascii=False))
        # 等配置确认(㉕b):session.updated=生效;error=整条被拒——被拒时**必须收场**,否则会话以默认裸配置
        # 跑起来(无人设/无工具/无转写/默认VAD)=复读机+白烧钱(这正是"疯狂打招呼"事故的一半根因)
        try:
            while True:
                ev0 = json.loads(await asyncio.wait_for(ows.recv(), timeout=8))
                t0 = ev0.get("type")
                if t0 == "conversation.created":   # 120/121:存续接钥匙(模块级,跨连接;前端断线重连即无缝续接)
                    _conv["id"] = ((ev0.get("conversation") or {}).get("id")) or ""
                    if _conv["id"] and engine == "grok":
                        _GROK_CONV["id"], _GROK_CONV["ts"] = _conv["id"], time.time()
                        sys.stderr.write(f"[voice-oa] conversation.id={_conv['id'][:20]}(resumption 钥匙已存)\n")
                    continue
                if t0 == "session.updated":
                    break
                if t0 == "error":
                    em = str(((ev0.get("error") or {}) or {}).get("message") or "")[:140]
                    await bws.send(json.dumps({"event": -1, "payload": {"error": "GPT 会话配置被拒:" + em}}, ensure_ascii=False))
                    sys.stderr.write(f"[voice-oa] session.update 被拒: {em}\n")
                    return
        except asyncio.TimeoutError:
            sys.stderr.write("[voice-oa] session.updated 超时(继续,但配置状态未知)\n")
        # half_duplex(㉙,默认开):AI 播放期整段静麦——外放防回声的**成熟可靠解**(AEC 环回在 Safari/iPad 不保证生效;
        # 全有全无的静麦是干净静默,不像能量门那样把话剪碎)。耳机用户在设置勾「全双工打断」恢复语音打断。
        await bws.send(json.dumps({"event": "up_rate", "payload": {
            "rate": 24000, "half_duplex": (not cred.get("rt_full_duplex"))}}, ensure_ascii=False))
        await bws.send(json.dumps({"event": 150, "payload": {"engine": "openai", "model": model}}, ensure_ascii=False))
        sys.stderr.write(f"[voice-oa] session up model={model} tools={len(tools)} p{page}({len(book['page_text'])}字)\n")
        t_up = asyncio.create_task(up())
        t_dn = asyncio.create_task(down())
        _done, _pend = await asyncio.wait({t_up, t_dn}, return_when=asyncio.FIRST_COMPLETED)
        for p_ in _pend:
            p_.cancel()
    except Exception as ex:
        sys.stderr.write(f"[voice-oa] {ex}\n")
    finally:
        if engine == "grok" and _GROK_CONV["id"]:
            _GROK_CONV["ts"] = time.time()   # 121:断开时刷新活跃时刻(30min 窗口从此刻算)
        if engine == "grok":
            try:   # 110:grok 会话账单(两种口径各估一笔,与 console 实际扣费对照即知计费口径)
                _cm = (time.time() - _gk["t0"]) / 60.0
                _am = (_gk["in_b"] + _gk["out_b"]) / 48000.0 / 60.0   # 24k·16bit=48000B/s
                _rec = {"ts": int(time.time()), "conn_min": round(_cm, 2), "audio_min": round(_am, 2),
                        "text_items": _gk.get("items", 0), "tool_calls": _gk.get("tools", 0),
                        "est_by_audio_usd": round(_am * 0.05 + _gk.get("items", 0) * 0.004, 4),
                        "est_by_conn_usd": round(_cm * 0.05, 4)}
                _gp = Path("/home/bwicarus/claude/state/grok-usage.json")
                try:
                    _gd = json.loads(_gp.read_text("utf-8"))
                except Exception:
                    _gd = []
                _gd.append(_rec)
                _gp.write_text(json.dumps(_gd[-500:], ensure_ascii=False, indent=1), "utf-8")
                _ledger_usage("grok", _span, _span, model=model, kind="session",
                              audio_in_s=round(_gk["in_b"] / 48000.0, 1), audio_out_s=round(_gk["out_b"] / 48000.0, 1),
                              text_items=_gk.get("items", 0), est_usd=_rec["est_by_audio_usd"])   # 284
                sys.stderr.write(f"[grok-usage] 连接{_cm:.1f}min 音频{_am:.1f}min → 按音频≈${_am * 0.05:.3f} / 按连接≈${_cm * 0.05:.3f}\n")
            except Exception:
                pass
        try:
            if _bridge.get("pc"):
                await _bridge["pc"].close()   # 99:会话结束随手关桥
        except Exception:
            pass
        for w in (ows, bws):
            try:
                await w.close()
            except Exception:
                pass


async def handle_agent(bws, file_rel: str = "", page: int = 0):
    cred = _creds()
    if not cred.get("api_key"):
        await bws.send(json.dumps({"event": -1, "payload": {"error": "缺凭证:~/.config/doubao-voice.json"}}, ensure_ascii=False))
        await bws.close()
        return
    key = cred["api_key"]
    _span = uuid.uuid4().hex[:12]   # 146:本连接的 voice_span_id(handle_agent 原来没有;ASR/TTS 记账要它)
    speaker = cred.get("tts_speaker", TTS_SPEAKER)
    asr_headers = {"X-Api-Key": key, "X-Api-Resource-Id": (SAUC_RID_V2 if cred.get("asr_v2") else SAUC_RID),
                   "X-Api-Connect-Id": str(uuid.uuid4()), "X-Api-Request-Id": str(uuid.uuid4())}
    asr_cfg = {"user": {"uid": "voice-agent"},
               "audio": {"format": "pcm", "codec": "raw", "rate": 16000, "bits": 16, "channel": 1},
               # end_window_size:静音多久判一句说完(definite)。不设的话默认窗口极长,连续流里永远等不到终稿
               "request": {"model_name": "bigmodel", "enable_punc": True,
                           "end_window_size": int(cred.get("asr_end_window_ms", 800))}}
    # ── ASR 语境注入(㉓,用户设计:固定任务词 + 页面动态内容;协议只认握手配置 → 每次开 ASR 快照当下语境)──
    # hotwords(1.0/2.0 都吃,词条式权重高)=固定指令词表 + 书名 + 本页未掌握生词;
    # dialog_ctx(仅 2.0,成段语境,≤800 tokens 从新到旧)=页面文本摘要 + 最近两轮对话(2.0 的 +20% 关键词召回主打场景)。
    try:
        vc = await _fetch_book_ctx(file_rel, page)
        hot = list(_ASR_TASK_WORDS)
        if file_rel:
            hot.insert(0, (file_rel.rsplit("/", 1)[-1].rsplit(".", 1)[0])[:24])   # 书名
        hot += [w for w in (vc.get("vocab") or []) if w][:30]
        corpus = {"context": json.dumps({"hotwords": [{"word": w} for w in dict.fromkeys(hot)][:50]}, ensure_ascii=False)}
        if cred.get("asr_v2"):
            cd = []
            pt = (vc.get("page_text") or "").strip()
            if pt:
                cd.append({"text": "用户正在读的页面内容:" + pt[:350]})
            for qa in reversed(_qa_pairs(vc.get("history") or [], 2)):   # 从新到旧(官方要求的排列)
                cd.append({"text": ("用户说过:" if qa.get("role") == "user" else "助手答过:") + str(qa.get("text", ""))[:120]})
            if cd:
                corpus["context_type"] = "dialog_ctx"
                corpus["context_data"] = cd[:20]
        asr_cfg["request"]["corpus"] = corpus
        sys.stderr.write(f"[voice-rt asr] 语境注入 hot={len(dict.fromkeys(hot))} ctx={len(corpus.get('context_data') or [])} v2={bool(cred.get('asr_v2'))}\n")
    except Exception as ex:
        sys.stderr.write(f"[voice-rt asr-ctx] {str(ex)[:120]}\n")   # 拉不到语境照常裸连,识别退回无语境
    ch = _tts_channel(bws, key, speaker, span=_span)   # 朗读(可选):speak/speak_done/cancel 由 up() 转发。146:带 span 记账
    try:
        async with websockets.connect(SAUC_WSS, additional_headers=asr_headers,
                                      max_size=10 * 1024 * 1024, open_timeout=15) as aws:
            await aws.send(_sauc_frame(0b0001, 0b0001, 1, json.dumps(asr_cfg, ensure_ascii=False).encode()))
            await aws.recv()   # 握手回包(空 result)
            await bws.send(json.dumps({"event": "agent_ready"}, ensure_ascii=False))
            seq = 1
            buf = bytearray()

            _asr_n = [0]      # 146:ASR 帧计数(list 免 nonlocal 声明)
            async def up():   # 浏览器 → (音频)豆包ASR / (speak)TTS 队列
                nonlocal seq
                async for msg in bws:
                    if isinstance(msg, (bytes, bytearray)):
                        buf.extend(msg)
                        while len(buf) >= 3200:      # 攒成 100ms 包再发(sauc 推荐粒度)
                            seq += 1
                            await aws.send(_sauc_frame(0b0010, 0b0001, seq, bytes(buf[:3200])))
                            del buf[:3200]
                            _asr_n[0] += 1                       # 146:计量——每帧恒 100ms
                            if _asr_n[0] >= 300:                 #   每 30s 落一笔(不必等会话结束,断连也不丢)
                                _ledger_volc_asr(_span, _asr_n[0] * 0.1, v2=bool(cred.get("asr_v2")))
                                _asr_n[0] = 0
                        continue
                    try:
                        j = json.loads(msg)
                    except Exception:
                        continue
                    t = j.get("type")
                    if t == "speak" and (j.get("text") or "").strip():
                        await ch["speak"](j["text"], j.get("mood") or "")
                    elif t == "speak_done":          # 这轮回答文本发完:FinishSession → 服务端把尾巴合成完 → 152
                        await ch["done"]()
                    elif t == "cancel":              # 打断:断连立即哑火(FinishSession 语义是"合成完剩余文本",打断不能用)
                        await ch["cancel"]()
                    elif t == "finish":
                        return

            async def down():  # 豆包ASR → 浏览器(字幕增量 + definite 终稿句)
                n_def = 0
                async for frame in aws:
                    r = _sauc_parse(frame)
                    if DEBUG:
                        _res = (r.get("payload") or {}).get("result") or {}
                        sys.stderr.write(f"[voice-agent asr<] mtype={r['mtype']} seq={r.get('seq')} text={_res.get('text')!r} utts={[(u.get('text'), u.get('definite')) for u in (_res.get('utterances') or [])]}\n")
                    if r["mtype"] == 0b1111:
                        await bws.send(json.dumps({"event": -1, "payload": {"error": f"asr {r.get('code')}"}}, ensure_ascii=False))
                        break
                    res = (r.get("payload") or {}).get("result") or {}
                    utts = res.get("utterances") or []
                    cur = ""
                    for u in utts[n_def:]:
                        if u.get("definite"):        # 新终结的句子 → 前端拿去发给助手
                            n_def += 1
                            txt = (u.get("text") or "").strip()
                            if txt:
                                await bws.send(json.dumps({"event": "utterance", "payload": {"text": txt}}, ensure_ascii=False))
                        else:
                            cur = u.get("text") or cur
                    if cur:                          # 进行中的句子 → 字幕
                        await bws.send(json.dumps({"event": "asr", "payload": {"text": cur}}, ensure_ascii=False))

            try:
                done, pending = await asyncio.wait(
                    [asyncio.create_task(up()), asyncio.create_task(down())],
                    return_when=asyncio.FIRST_COMPLETED)
                for p in pending:
                    p.cancel()
            finally:
                await ch["cancel"]()
    except Exception as ex:
        sys.stderr.write(f"[voice-agent] {ex}\n")
        try:
            await bws.send(json.dumps({"event": -1, "payload": {"error": str(ex)[:120]}}, ensure_ascii=False))
        except Exception:
            pass
    finally:
        try:
            await bws.close()
        except Exception:
            pass


OPENAI_RT_CALL_URL = "wss://api.openai.com/v1/realtime?call_id="


_RTC_CTL_LIVE = {}   # 93/133:uid → {call_id: 挂上时刻} —— **真** 并发注册表(挂上加、关闭删)。
# ⚠ 133:旧版是 uid → 最近 call_id 且**只写不删**,于是"同 uid 双 call 并存"对**每一次新通话**都报警
#   (哪怕上一通半小时前就 session_expired 了)= 纯假阳性,把根因排查带沟里过一次。判并发只看 len(dict)。
_GROK_CONV = {"id": "", "ts": 0.0}   # 121:grok resumption 续接钥匙(跨连接;30min 内重连带 conversation_id=历史无缝恢复)


def _mk_bridge_track(q):
    """99 WebRTC 桥出向音轨:24k mono s16,20ms/帧实时 pacing(照 aiortc AudioStreamTrack 模式);
    队列(元素=960B 定长块)空时发静音——track 必须持续出帧,否则浏览器侧断流。"""
    import fractions
    import time as _tm

    import av
    from aiortc.mediastreams import MediaStreamTrack

    class _T(MediaStreamTrack):
        kind = "audio"

        def __init__(self):
            super().__init__()
            self._start = None
            self._ts = 0

        async def recv(self):
            if self._start is None:
                self._start = _tm.time()
            else:
                wait = self._start + self._ts / 24000 - _tm.time()
                if wait > 0:
                    await asyncio.sleep(wait)
            try:
                buf = q.get_nowait()
            except asyncio.QueueEmpty:
                buf = b"\x00" * 960
            frame = av.AudioFrame(format="s16", layout="mono", samples=480)
            frame.planes[0].update(buf)
            frame.sample_rate = 24000
            frame.pts = self._ts
            frame.time_base = fractions.Fraction(1, 24000)
            self._ts += 480
            return frame

    return _T()


# ══════════ 133:ASR「提示词泄漏式幻觉」判别(手动放行闸的判据)══════════
# 现象(实测):AI 出声/静音段被 VAD 误判成一个新的音频轮,转写模型对着这段没有人声的音频
#   **把我们喂给它的 prompt 原样复述**成"用户说的话"。日志铁证:转写文本与 assistant.py 的
#   _tr["prompt"] 逐字一致。这是 prompt-copy 式幻觉,不是"Whisper 的 bug"那么笼统。
# ⚠ 因果澄清(外部评审纠正,我原先讲反了):假转写**不是**多次回答的触发源——Realtime 模型直接
#   听原始音频,转写是**旁路异步**产物(日志里 response.created 早于 transcription.completed)。
#   真凶是 **VAD 假轮 + create_response=true**。所以本判别器是**闸门的判据**,不是闸门本身;
#   光靠过滤转写救不了自动挡(那时回答早已生成)——必须同时关掉自动挡,见 handle_rtc_ctl 的 gate。
# 判别原则:**高精度硬拒,宁可漏判也绝不错杀真实语音**。用户真的会说「下一页」「笔迹」「生词」,
#   所以严禁"转写是 prompt 的子串就拒"这类一般性规则(评审明确点名)。
# ⚠ 血的教训:这里曾放过「关键词」「常说」——而用户完全可能说「这一页的**关键词**是什么?」
#   → 命中锚点 → 真实提问被当成假轮**删掉且不回答**,用户干等、界面零反馈。违反了下面自己写的原则。
#   锚点只能放**人类不可能自然说出口**的整串。prompt 被复读的场景由下面的 LCS 判据兜底(去标点后
#   与 mirror 的最长公共子串轻松 ≥10),所以删掉它们**零损失**。
_ASR_GHOST_ANCHORS = ("学习伴读通话",)
# ⚠ 必须与 assistant.py 的 _tr["prompt"] 保持一致(改那边记得改这里)。anchors 是主判据,这个是补充。
_ASR_PROMPT_MIRROR = ("关键词:Anki、笔迹、振假名、生词、假名"
                      "|学习伴读通话。常说:这一页/这页讲了什么/上一页/下一页/翻到第N页/读一下/"
                      "做卡片/记笔记/生词/翻译/解释/公式/我画的/笔迹")   # 含旧版,防旧会话残留
_GHOST_LCS_MIN = 10   # 与 prompt 的最长公共子串阈值:「翻到第N页」才5字、「下一页」3字 → 10 字才不会误杀真人
_GHOST_COV_MIN_LEN = 10   # 复读变体判据:假转写去标点后至少这么长才启用(短句不判,防误杀)
_GHOST_COV_RATIO = 0.8    # 累计匹配块占假转写比例≥此=复读变体(同音错字/漏字把连续子串打断→LCS 判不出;用户实测「笔记」vs「笔迹」占比 0.94、真人 0.50)


def _strip_punct(s: str) -> str:
    return re.sub(r"[\s,，。、:：;；/·!！?？…\-]+", "", s or "")


def _is_asr_ghost(tx: str):
    """判定这一轮音频是不是"假轮"。返回 (是否假, 原因)。"""
    s = (tx or "").strip()
    if not s:
        return True, "空转写(纯静音轮)"
    for a in _ASR_GHOST_ANCHORS:
        if a in s:
            return True, f"含 prompt 锚点「{a}」"
    a1, a2 = _strip_punct(s), _strip_punct(_ASR_PROMPT_MIRROR)
    if a1 and a2:
        sm = difflib.SequenceMatcher(None, a1, a2, autojunk=False)
        m = sm.find_longest_match(0, len(a1), 0, len(a2))
        if m.size >= _GHOST_LCS_MIN:
            return True, f"与 ASR prompt 最长公共子串 {m.size} 字(≥{_GHOST_LCS_MIN})"
        matched = sum(b.size for b in sm.get_matching_blocks())   # 复读变体:同音错字/漏字打断连续子串→LCS 漏,但整体仍高度重合(用户实测「笔记」vs mirror「笔迹」一字之差)
        if len(a1) >= _GHOST_COV_MIN_LEN and matched >= _GHOST_COV_RATIO * len(a1):
            return True, f"与 ASR prompt 整体重合 {matched}/{len(a1)} 字(复读变体·同音错字)"
    return False, ""


# ══════════ 133:通话票据(webapp 签发 / relay 验签)══════════
# 为什么需要它:接管旧通话必须知道"这两路 call 是**同一个人**的"。
# ⚠ 绝不能从 call_id 猜 —— `rtc_u7_E1S05` 这串**完全来自 OpenAI 的 Location header**,我们没参与生成。
#   日志里它恒为 `rtc_u7_`,但**无法证明** `u7` 是"用户"而不是"组织/项目"级前缀。
#   万一是后者,`call_id.split("_")[1]` 对所有用户都相同 → 接管逻辑会去**踢掉别人的通话**。
#   所以:uid 只认 webapp 用共享密钥签发的票据;**验不过就绝不踢人**(只告警),宁可不去重也不误伤。
VOICE_TICKET_KEY = Path("/home/bwicarus/claude/state/voice-ticket.key")
# 141:喂给 AI 的图落盘(按内容 sha1 去重);part 里只带 URL,webapp 的 /pdf/api/toolshot/<name> 提供
TOOLSHOT_DIR = Path("/home/bwicarus/claude/state/reader-toolshots")


def _ticket_secret() -> bytes:
    try:
        if not VOICE_TICKET_KEY.exists():
            VOICE_TICKET_KEY.parent.mkdir(parents=True, exist_ok=True)
            VOICE_TICKET_KEY.write_text(os.urandom(32).hex(), "utf-8")
            try:
                VOICE_TICKET_KEY.chmod(0o600)
            except Exception:
                pass
        return VOICE_TICKET_KEY.read_text("utf-8").strip().encode()
    except Exception:
        return b""


def _ticket_uid(uid: str, call_id: str, tk: str) -> str:
    """验签通过 → 返回可信 uid;否则空串(调用方据此**放弃接管**)。"""
    sec = _ticket_secret()
    if not (sec and uid and call_id and tk):
        return ""
    want = hmac.new(sec, f"{uid}|{call_id}".encode(), hashlib.sha256).hexdigest()[:32]
    return uid if hmac.compare_digest(want, tk) else ""


async def _openai_hangup(call_id: str):
    """官方 POST /v1/realtime/calls/{id}/hangup —— 从服务端真正终止一路通话。
    媒体是浏览器↔OpenAI 直连,relay 拆不了;但这个端点可以。用于接管旧通话。"""
    key = _openai_key()
    if not key or not call_id:
        return False
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"https://api.openai.com/v1/realtime/calls/{call_id}/hangup",
                             headers={"Authorization": f"Bearer {key}"})
        sys.stderr.write(f"[rtc-ctl] ☎hangup call={call_id[:14]} → {r.status_code}\n")
        return r.status_code < 300
    except Exception as ex:
        sys.stderr.write(f"[rtc-ctl] hangup 失败 call={call_id[:14]}: {str(ex)[:80]}\n")
        return False


async def _supersede_others(uid: str, new_call: str):
    """★ 133:单通话唯一性 —— 同一用户开新通话 → **接管**(踢掉)他所有旧通话。

    ⚠ 这里埋着一个我踩过的坑(commit 0b9999c 因此被 revert):**纯后端踢人会跟前端自动重连打乒乓** ——
      踢旧 → 旧前端以为"意外断线" → 指数退避自动重连 → 建出新 call → 又把用户真正在用的那路踢掉 → 死循环。
      后端**无法区分**"被踢的旧通话在重连" vs "用户开的全新通话"。
    破解点:**先给旧前端发一条它看得懂的 superseded**,让它进**终态**(等同用户主动挂断,不触发重连),
      再挂断。带上 call_id,前端只对"确实是自己当前那路"生效(防迟到消息误杀新通话)。
    """
    live = _RTC_CTL_LIVE.get(uid) or {}
    olds = [c for c in list(live.keys()) if c != new_call]
    for old in olds:
        rec = live.get(old) or {}
        obws = rec.get("bws")
        if obws is not None:
            try:   # ① 先告知:这是"被接管",不是断线 → 前端进终态,**不许自动重连**
                await obws.send(json.dumps({"event": "superseded",
                                            "payload": {"call_id": old, "by": new_call}}, ensure_ascii=False))
            except Exception:
                pass
        asyncio.create_task(_openai_hangup(old))   # ② 再真正终止服务端那路(否则它继续收音频、继续答、继续计费)
        live.pop(old, None)
        sys.stderr.write(f"[rtc-ctl] ⛔接管:旧 call={old[:14]} 被 {new_call[:14]} 取代\n")
        if obws is not None:
            async def _close_later(w):
                await asyncio.sleep(0.5)   # 给 superseded 一点时间送达再关
                try:
                    await w.close()
                except Exception:
                    pass
            asyncio.create_task(_close_later(obws))


async def handle_rtc_ctl(bws, call_id: str, file_rel: str = "", page: int = 0, fe: int = 1,
                         uid_q: str = "", tk_q: str = ""):
    _span = uuid.uuid4().hex[:12]   # 284:voice_span_id
    _bok, _bspent = _budget_gate()
    if not _bok:
        try:
            await bws.send(json.dumps({"event": -1, "payload": {"error": f"今日语音预算已用完(${_bspent:.2f})"}}, ensure_ascii=False))
        except Exception:
            pass
    """㊺P2 RtcController:sideband 控制面接管**工具执行**(唯一执行者)——
    复用 handle_openai._tool 语义:voice-tool 执行/client_action+tool_status 经控制 WS 下行/
    图像 sideband 直喂;新增**工具缓存**(同工具+同参数+同页+同笔迹=复用,治 read_page 重复调用)
    与 **need_shot 截图往返**(see_ink/see_page 的视口截图只有前端能拍)。
    reply_text/wait_for_user 是纯前端语义,留给前端 dc 处理(前端放行名单)。
    usage 记账 P3 接管,现仍由前端上报;response.create 带模态(rt_voice_mode,src='tool')。
    **版本握手(59)**:前端 URL 带 fe=2 才接管工具——旧页面(部署前加载的 JS 没有分工逻辑,
    自己会执行工具)不带 fe → 本函数退回 P1 观察模式,否则新旧换代窗口必双执行
    (实锤:同一 call 前端 Safari 与 relay httpx 各跑一遍 read_page + create 撞
    conversation_already_has_active_response)。"""
    key = _openai_key()
    if not key or not call_id:
        try:
            await bws.send(json.dumps({"event": "rtc_ctl", "payload": {"ok": False, "error": "缺 key/call_id"}}))
        except Exception:
            pass
        await bws.close()
        return
    try:
        ows = await websockets.connect(OPENAI_RT_CALL_URL + call_id,
                                       additional_headers={"Authorization": f"Bearer {key}"},
                                       max_size=16 * 1024 * 1024, open_timeout=10)
    except Exception as ex:
        sys.stderr.write(f"[rtc-ctl] sideband 连接失败 call={call_id[:12]}: {str(ex)[:100]}\n")
        try:
            await bws.send(json.dumps({"event": "rtc_ctl", "payload": {"ok": False, "error": str(ex)[:80]}}))
        except Exception:
            pass
        await bws.close()
        return
    sys.stderr.write(f"[rtc-ctl] {'P2 已挂' if fe >= 2 else 'P1 观察(前端旧版 fe<2)'} call={call_id[:12]} file={file_rel[:30]} p{page}\n")
    # 93/133(双回答事故取证):登记到真并发注册表;**只有此刻别的 call 还挂着**才是真并存(前端双拨/多标签/多设备)。
    # 133:uid **只认票据**(见 _ticket_uid 的说明:从 call_id 猜会误踢别人的通话)
    _uid_m = _ticket_uid(uid_q, call_id, tk_q)
    if not _uid_m:
        sys.stderr.write(f"[rtc-ctl] ⚠ 票据无效/缺失(uid={uid_q!r}) → 本路不参与单通话唯一性(不踢人)\n")
    try:
        if not _uid_m:
            raise RuntimeError("no ticket")
        _live = _RTC_CTL_LIVE.setdefault(_uid_m, {})
        _others = [c for c in _live if c != call_id]
        if _others:
            sys.stderr.write(f"[rtc-ctl] ⚠ 同 uid 多 call 并存({len(_others) + 1} 条): "
                             f"在挂={[c[:12] for c in _others]} 新={call_id[:12]} → 接管\n")
        _live[call_id] = {"ts": time.time(), "bws": bws}
        if _others:
            await _supersede_others(_uid_m, call_id)   # 133:最新通话独占 —— 旧的进终态 + 官方 hangup
    except Exception:
        pass
    book = {"page": page, "sel": "", "ink_strokes": None, "_ink_fp": "", "_over": (not _bok)}   # _over:超支后 sideband 掐生成(入口已超支=从首轮就掐,含 #290 重连场景)
    tool_cache = {}          # 只读工具缓存:name|args|page|ink哈希|sel哈希 → {out, ca};写工具成功=整体清空(域失效)
    shot_fut = {}            # need_shot 往返:shot_id → Future(带 ID 防两轮工具重叠时错配/迟到截图被下一请求接走)
    shot_seq = {"n": 0}
    epoch = {"n": 0}         # 轮次纪元(审核P0#2):用户开口/打字=+1;旧纪元工具完成→回填但不 create(旧结果不抢新话轮)
    recent_tools = []        # 61b(用户需求):最近工具结果(搜索摘要/配图URL)——make_anki/make_note 时随 ctx 带给制卡 AI
    _NO_CACHE = {"see_ink", "see_page", "see_figure",       # 视觉:viewport/缩放/滚动不在键里,误命中=拿旧图说新话
                 "web_search", "search_image", "search_video"}   # 时变数据:整场通话缓存不合适
    try:
        await bws.send(json.dumps({"event": "rtc_ctl", "payload": {"ok": True, "p": 2 if fe >= 2 else 1}}))
    except Exception:
        pass
    pend = {"create": False, "ep": 0}   # 59:create 撞车被拒→记下,response.done 时补发(纪元变了=用户已开新话,放弃)
    turn = {"text": False}              # 67:relay 最近下发的模态(text 轮里再调 route_to_text=驳回,防三段式冗余)

    def _resp_create(long_tool=False, user=False):
        """response.create——模态按服务器持久化档位(61 四态)。126(P4):user=True=用户轮(speech_stopped
        仲裁归 relay),half 档用户轮=音频(与前端 _rtcRespCreate 对齐);工具结果轮语义不变。"""
        m = _norm_vm(_creds().get("rt_voice_mode"))
        want_audio = m == "sts" or (m == "route" and not long_tool) or (m == "half" and user)
        turn["text"] = not want_audio
        return {"type": "response.create",
                "response": {"output_modalities": ["audio" if want_audio else "text"],
                             "max_output_tokens": 2048}}

    async def _need_shot(tool=""):
        """向前端要一张截图(see_ink/see_page 用;WebRTC 模式截图只有浏览器能拍)。shot_id 配对防错配。
        带 tool:前端 see_ink 时按笔迹外接框**截局部**(灵活位置/大小),see_page 截整视口。"""
        shot_seq["n"] += 1
        sid = shot_seq["n"]
        fut = asyncio.get_event_loop().create_future()
        shot_fut[sid] = fut
        try:
            await bws.send(json.dumps({"event": "need_shot", "payload": {"shot_id": sid, "tool": tool}}))
            return await asyncio.wait_for(fut, timeout=6)
        except Exception:
            return None
        finally:
            shot_fut.pop(sid, None)

    async def _oa_route(intent: str):
        """route 档长文生成(61):调 webapp /route-text(Gemini flash SSE),delta 边收边经控制 WS
        下行给前端(显示+可选 TTS 代念,尽快开口);返回 (全文, err)。"""
        full, err, brief = "", "", ""
        try:
            async with httpx.AsyncClient(base_url=WEBAPP, headers=_webapp_headers(), timeout=150) as hc:
                async with hc.stream("POST", "/api/assistant/route-text",
                                     json={"intent": intent, "q": book.get("last_q") or "",
                                           "file": file_rel, "page": book.get("page") or page}) as r:
                    ev, data = "", ""
                    async for line in r.aiter_lines():
                        if line.startswith("event:"):
                            ev = line[6:].strip()
                        elif line.startswith("data:"):
                            data += line[5:].strip()
                        elif not line.strip():
                            if ev == "delta" and data:
                                try:
                                    seg = json.loads(data)
                                except Exception:
                                    seg = ""
                                if seg:
                                    full += seg
                                    await bws.send(json.dumps({"event": "route_text", "payload": {"delta": seg}}, ensure_ascii=False))
                            elif ev == "done" and data:
                                try:
                                    brief = (json.loads(data) or {}).get("summary") or ""
                                except Exception:
                                    brief = ""
                            elif ev == "err":
                                err = "生成后端不可用"
                            ev, data = "", ""
        except Exception as ex:
            err = str(ex)[:100]
        if full:
            await bws.send(json.dumps({"event": "route_text", "payload": {"done": True, "text": full}}, ensure_ascii=False))
        return full, brief, ("" if full else (err or "空结果"))

    _tfail_r = {"key": "", "n": 0}   # 100:RTC 版工具熔断(同 WS 版)
    _img_items = []   # 104:(epoch, item_id) 直喂图记账——新话轮焚旧
    _state_evt = {"fp": "", "iid": None, "n": 0}   # 142(TPM 去重):上条状态的指纹/item_id/计数——照豆包路 _state_evt,另加覆盖式删旧
    _read_pages = set()   # 142:本轮已把整页正文灌进上下文的页号(当前页 state 注入 + read_page 拉过的页)→ 重复 read 回指针不再灌整页
    # Item3(TPM 感知覆盖):1 分钟滑动窗口累计 input_tokens(response.done 时挂钩喂入)。
    #   纯成本看删旧状态几乎总更亏(破坏后面前缀缓存 $0.06→$0.60,损失 > 删掉省的);
    #   只有逼近 OpenAI Realtime 40000 tok/min 硬限时才删旧状态 item 保命(少读一段字)。
    _OA_TPM_LIMIT = 40000     # OpenAI Realtime input TPM 硬限
    TPM_DANGER = 10000        # 剩余额度(headroom)低于此值=快撞墙→删旧状态保命;否则保住 10× 便宜的前缀缓存
    _tpm_win = []             # [(t, input_tokens)] —— 60 秒滑动窗口

    def _tpm_used_1min() -> int:
        """窗口内(近 60 秒)已读 input_tokens 之和;顺手剔除过期条目。"""
        _cut = time.time() - 60.0
        while _tpm_win and _tpm_win[0][0] < _cut:
            _tpm_win.pop(0)
        return sum(_n for _t, _n in _tpm_win)

    async def _tool(name: str, args: dict, call_id2: str, ep0: int):
        _subs = []   # 137:工具内部子步骤 —— 它们是**外层卡的步骤**,不另起一张卡
        _lbl0 = "路由详答·生成中" if name == "route_to_text" else name   # 64/65:路由专属标签(工具卡+字幕状态行同用,Apple 化去 emoji)
        await bws.send(json.dumps({"event": "tool_status", "payload": {"status": "running", "tool": name, "label": _lbl0}}, ensure_ascii=False))
        out, ok, label, took, cached = "", True, name, None, False
        vis, readonly, no_create = None, True, False
        slim_full = None   # #img/anki(2026-07-21 用户实锤"制卡预览不显示"):制卡等 cards 完整体,给 tool_status 前端渲预览卡用
        try:
            _ink_fp = book.get("_ink_fp") or ""
            # 142(read_page 当前页短路,治 TPM):安全解析目标页(非法参数不抛,交给正常链路)
            try:
                _rp_arg = int(str(args.get("page") or "").strip() or 0)
            except Exception:
                _rp_arg = 0
            _rp_cur = book.get("page") or page
            _rp_tgt = _rp_arg or _rp_cur
            # 缓存键:sel 全文哈希(58b 的前 80 字有前缀碰撞)+ink 全笔画哈希(_up 里算)——审核 P1
            ck = (f"{name}|{json.dumps(args, ensure_ascii=False, sort_keys=True)}|"
                  f"{book.get('page') or page}|{_ink_fp}|{hashlib.md5((book.get('sel') or '').encode()).hexdigest()[:8]}")
            if name == "route_to_text":
                # 61 程序门控(替代 prompt 门控):按**当前**输出模式放行——模式按钮通话中热切立即生效
                m_now = _norm_vm(_creds().get("rt_voice_mode"))
                if turn["text"]:   # 67(20:51 实锤三段式冗余):文字轮里转手调 route_to_text=浪费一轮+Gemini 双引擎——驳回让它自己写
                    out = "(你当前这轮就是文字回答:**直接写出完整讲解**,不要调用任何工具、不要写等待语,现在开始写正文)"
                    label = "文字路由(已在文字轮)"
                elif m_now != "route":
                    out = "(当前输出模式未启用文字路由:请直接口头简要回答重点;想看长文可让用户把模式切到「路由」)"
                    label = "文字路由(未启用)"
                else:
                    label = "路由详答"
                    full, rbrief, rerr = await _oa_route(str(args.get("intent") or ""))
                    if rerr:
                        ok, out = False, f"(文字生成失败:{rerr};请口头简要回答)"
                    else:
                        # 79(用户设计):与天气/新闻卡同逻辑——回填只有引擎写的**简介**(静默入库,全文在卡上;
                        # 用户长按卡片时全文才进 2.1 上下文)
                        out = ("(文字详答已显示在用户屏幕上,本轮到此结束。内容简介:" + (rbrief or full[:200]) +
                               "。用户下次说话时若相关直接运用;想让你看全文他会长按卡片带入。)")
                        no_create = True   # 长文已显示(可选 TTS 代念),不再花一轮输出音频
            elif name == "read_selection" and (book.get("sel") or "").strip():
                # 74(用户设计):选中内容早已随 state 在手——重复类工具程序短路,零 webapp 调用零延迟
                # (不从工具表摘除:动态改 tools=前缀变=缓存全灭,㊶教训;短路等效且缓存无伤)
                out = "(选中内容就在这里,无需再查:「" + book["sel"] + "」——直接使用)"
                label = "读取选中(免调用)"
            elif name == "recall_study":
                span = str(args.get("span") or "today").lower()
                out = _study_digest("week" if span.startswith("w") else "today") or "(记录为空——这段时间还没有学习记录,如实告诉用户)"
                label = "回顾学习"
            elif name == "deep_think":
                out = await _oa_deep(str(args.get("question") or ""), file_rel, book.get("page") or page)
                label = "深度思考"
            elif name == "read_page" and _rp_tgt == _rp_cur and _rp_cur in _read_pages:
                # 142(最痛:那次同页 read 两次各灌整页 ~500-600×2 token):当前页整页正文已随页面状态注入上文 → 回指针,别再灌整页。
                # Phase2 共存:短路门槛从"vtext/page_text 非空"改为"_rp_cur 已在 _read_pages"(=整页正文确已进上文)——
                #   本页只注了**简述**时 _rp_cur 不在名单 → 不短路,read_page 正常执行把整页原文按需拉给深问(简述替整页的逃生路)。
                out = (f"(第 {_rp_cur} 页正文已经在上文的页面状态消息里,直接据此回答/高亮/做卡,"
                       "无需重复读取整页;要**其它页**内容才用 read_page(page:N)。)")
                label = "读取本页(免调用)"
                _read_pages.add(_rp_cur)
            elif name == "read_page" and _rp_tgt in _read_pages:
                # 142:本轮通话已经读过这一页,整页正文就在上文 → 回指针,别重复灌整页
                out = f"(第 {_rp_tgt} 页正文本轮通话已经读过、就在上文里,直接使用,别重复读取整页。)"
                label = "读取页面(免调用)"
            elif ck in tool_cache and name not in _NO_CACHE:
                hit = tool_cache[ck]
                out, cached = hit["out"], True
                if hit.get("ca"):
                    await bws.send(json.dumps({"event": "client_action", "payload": hit["ca"]}, ensure_ascii=False))
                label = name + "(复用)"
            else:
                cmd = json.dumps({"tool": name, "args": args}, ensure_ascii=False)
                ctx = {"file_rel": file_rel, "page": book.get("page") or page}
                if _creds().get("rt_image"):
                    ctx["_want_vision"] = 1
                if book.get("ink_strokes"):
                    ctx["ink"] = book["ink_strokes"]
                if book.get("sel"):
                    ctx["selection"] = book["sel"]
                if name in ("make_anki", "make_note"):
                    ctx["recent_tools"] = recent_tools[-4:]   # 对话现场随卡走(webapp _card_extra 消费)
                if name in ("see_ink", "see_page"):
                    shot = await _need_shot(name)   # see_ink → 前端按笔迹外接框截局部
                    if shot and shot.get("b64"):
                        ctx["view_image"] = {"media_type": shot.get("media_type") or "image/jpeg", "b64": shot["b64"]}
                # ⚠不带 rtc_call_id(58 的修复曾错落到 WS 版此处漏改=视觉链路断,审核实锤):
                # P2 有**自己的持久 sideband(ows)**,_vision 直接在下面注入,不让 webapp 再开第二条临时连接
                async with httpx.AsyncClient(base_url=WEBAPP, headers=_webapp_headers(), timeout=180) as hc:
                    r = await hc.post("/api/assistant/voice-tool", json={"cmd": cmd, "ctx": ctx})
                    d = r.json()
                ok = bool(d.get("ok")); label = d.get("label") or name; took = d.get("took_s")
                _subs = ((d.get("result") or {}).get("sub_steps") or d.get("sub_steps") or [])[:12]   # 137:工具内部子步骤 → 外层卡的步骤(不另起卡)
                readonly = bool(d.get("cacheable"))   # 白名单=只读集合;写工具 stale 时仍回填真实结果
                res = d.get("result") or {}
                ca = res.get("client_action")
                if isinstance(ca, dict) and ca.get("fn"):
                    await bws.send(json.dumps({"event": "client_action", "payload": ca}, ensure_ascii=False))
                vis = res.pop("_vision", None)   # 直喂 Realtime 的图(GPT 自己看);b64 绝不进文本 output(会被截成烂 JSON)
                card_vis = res.get("_fed_images") or vis   # 工具卡展示的图(落盘发 URL)——see_ink 走文字描述路时图在 _fed_images
                slim, slim_full, out = _prep_tool_result(res, d.get("tool") or name)   # 三引擎共用(见 _prep_tool_result)
                _imgs = []
                try:
                    for _im in (res.get("images") or []):
                        _u = _im.get("image_url") or _im.get("url") or ""
                        if _u:
                            _imgs.append(_u)
                except Exception:
                    pass
                recent_tools.append({"tool": name, "label": label, "rag": out[:600], "images": _imgs[:3]})
                del recent_tools[:-6]
                if name == "read_page" and ok:
                    _read_pages.add(_rp_tgt)   # 142:整页正文已进上下文 → 登记,后续同页 read 短路回指针
                # 66b(日志分析:route 档 read_page 后模型口头念整页 60s,instructions 远端规则命中率 0)——
                # 提醒放到**离决策最近的地方**:长工具结果尾部就地一行(just-in-time,非硬兜底)
                if (ok and readonly and len(out) > 800
                        and _norm_vm(_creds().get("rt_voice_mode")) == "route"):
                    out += "\n(系统提示:内容较长,本轮已是**文字**回答——直接开始写完整讲解(结构清晰,可用 Markdown);不要写『稍等/我整理一下』这类过渡句,也不要再调用工具)"
                if ok and d.get("cacheable") and name not in _NO_CACHE:
                    tool_cache[ck] = {"out": out, "ca": ca if isinstance(ca, dict) else None}
                if ok and not d.get("cacheable"):
                    tool_cache.clear()   # 写操作成功=便签/高亮/生词等状态变了,粗粒度域失效(审核 P1:revision 的保守替身)
                if res.get("silent") and not _creds().get("rt_tool_reply"):
                    no_create = True   # 74/89:展示型工具静默入库(卡片已显示);设置「工具完成后口头回报」开=放行它自由回答
        except Exception as ex:
            ok, out = False, json.dumps({"error": str(ex)[:200]}, ensure_ascii=False)
        stale = (epoch["n"] != ep0)   # 审核P0#2:工具跑着的时候用户开了新话轮——旧结果不抢话(不 create)
        if stale and readonly:
            # 71(用户截图实锤):杂音被 VAD 当新话轮→正当结果被整个作废,而卡片其实已显示=模型说"没查到"用户却看着卡。
            # 改降级:结果概况照给,模型自己判断上一句是真换话题还是杂音/等待(它有对话上下文,程序判不了这个)
            out = ("(结果已生成" + ("、卡片已显示给用户" if "client_action" in out or "卡片" in out else "") +
                   ",但期间有新的语音输入打断。结果概况:" + out[:500] +
                   "——若用户上一句其实是在等这个结果(或只是杂音/无关短语),直接据此回答;若确实换了新话题,忽略本条。)")
            vis = None
        try:
            if vis and not stale and _creds().get("rt_image"):
                for _vi, v in enumerate(vis[:2]):   # P2 视觉:经**本函数已持有的 sideband** 直喂(与 GPT-WS 版 ows 直喂同构)
                    _iid2 = f"img{epoch['n']}x{len(_img_items)}{_vi}"   # 104:自带 id(客户端可指定)→ 新话轮焚旧图省 token
                    _img_items.append((epoch["n"], _iid2))
                    await ows.send(json.dumps({"type": "conversation.item.create", "item": {
                        "id": _iid2, "type": "message", "role": "user",
                        "content": [{"type": "input_image", "detail": "high",
                                     "image_url": f"data:{v.get('media_type', 'image/png')};base64,{v['b64']}"}]}}))
                out += "\n(相关图像已直接发给你,请看图回答)"
            elif vis and not stale:
                out += "\n(该工具产出了图像,但「图像输入」开关未开,只能按文本回答)"
            # 100:RTC 版工具熔断(同 WS 版——同名同参连续失败≥3 强提示;≥6 不 create 硬断循环)
            _tk2 = name + "|" + json.dumps(args, ensure_ascii=False, sort_keys=True)[:120]
            if not ok:
                _tfail_r["n"] = _tfail_r["n"] + 1 if _tfail_r["key"] == _tk2 else 1
                _tfail_r["key"] = _tk2
            else:
                _tfail_r["key"], _tfail_r["n"] = "", 0
            if not ok and _tfail_r["n"] >= 3:
                out = json.dumps({"error": "这个工具已连续失败多次,系统已熔断。**禁止再调用它**——直接告诉用户『这个操作暂时做不了』,然后安静等用户说话。"}, ensure_ascii=False)
            await ows.send(json.dumps({"type": "conversation.item.create",
                                       "item": {"type": "function_call_output", "call_id": call_id2, "output": out}}))
            if not ok and _tfail_r["n"] >= 6:
                sys.stderr.write(f"[rtc-ctl] 工具熔断硬断: {_tk2[:80]} ×{_tfail_r['n']}\n")
            elif not stale and not no_create:
                # 133:有候选音频轮正在等转写验证 → **工具结果先回填,但暂缓它的 create**。
                # 否则:候选若是真人插话,工具轮会抢在用户前面说话;候选若是假轮,又不能让工具永远闭嘴。
                # 所以挂起来——REJECT 时放行(_reject_turn),ACCEPT 时丢弃(用户轮的回答会带上工具结果)。
                if gate["pending"]:
                    gate["hold_tool"] = {"long": bool(readonly and len(out) > 800), "ep": ep0}
                    sys.stderr.write(f"[rtc-ctl] ⏸工具轮 create 暂缓({name}:有候选输入待验证)\n")
                else:
                    await _do_create(long_tool=bool(readonly and len(out) > 800))
        except Exception:
            pass
        # 141(用户):视觉类工具(see_ink/see_page/see_figure)**把真正喂给 AI 的那张图也带给前端**,
        #   在工具卡的「AI 请求」节点里显示(可点开放大)。以前前端只看到 "(无参数)" —— 到底喂了什么图
        #   全靠猜,笔迹裁歪了也无从发现。图本就已经过网发给 OpenAI 了,再镜像一份到本机浏览器成本可忽略。
        # 141(ADR §4):**落盘 + 只发 URL**,绝不发 base64 ——
        #   b64 既撑爆 ctl WS 的 payload,又会撑爆历史 JSON(单张 10-50 万字节,几十轮就废了)。
        _vshot = []
        try:
            TOOLSHOT_DIR.mkdir(parents=True, exist_ok=True)
            for _v in (card_vis or [])[:2]:
                _b64 = (_v or {}).get("b64") or ""
                if not _b64:
                    continue
                _raw = base64.b64decode(_b64)
                _mt = (_v.get("media_type") or "image/png")
                _ext = ".jpg" if "jpeg" in _mt or "jpg" in _mt else ".png"
                _nm = hashlib.sha1(_raw).hexdigest()[:24] + _ext
                _fp = TOOLSHOT_DIR / _nm
                if not _fp.exists():
                    _fp.write_bytes(_raw)
                _vshot.append("/pdf/api/toolshot/" + _nm)
        except Exception as _ex:
            sys.stderr.write(f"[rtc-ctl] toolshot 落盘失败: {str(_ex)[:80]}\n")
            _vshot = []
        await bws.send(json.dumps({"event": "tool_status", "payload": {
            "status": "done" if ok else "error", "tool": name, "label": label + ("(已过期)" if stale else ""), "took_s": took,
            "args": args, "rag": out[:1600], "sub_steps": _subs, "vision": _vshot,
            "result": _tool_preview_result(slim_full)}}, ensure_ascii=False))   # 制卡完整体(前端渲预览卡)
        _ledger_tool("openai_rtc", _span, call_id2, name, ok, took or 0, cached=bool(cached))   # 284
        _vlog("tool", tool=name, label=label, page=book.get("page") or page, book=file_rel, ok=ok,
              args=args, brief=("[cache] " if cached else "") + ("[stale] " if stale else "") + out[:300])

    # ══════════ 133 手动放行(外部评审 P0):由 relay 仲裁"这一轮该不该回答" ══════════
    # 旧(自动挡):create_response=true → VAD 一判轮次结束就**自动生成回答**。于是噪声/AI 回声被切成的
    #   假轮直接白答一次;interrupt_response=true 还让假轮**打断**正在跑的工具轮(实测 see_ink 被打断标"已过期")。
    # 新(手动挡):speech_started **只登记候选**——不推进纪元、不消费状态、不作废在途工具;
    #   等这个 item 的转写回来判真伪:假轮 → 删掉 item、不生成;真轮 → 推进纪元 + 注入状态 + response.create。
    # 代价:首字延迟多等一次 ASR。用超时兜底(转写不来也照常放行)——**绝不允许出现"该答却不答"**。
    gate = {
        "pending": {},        # item_id → 登记时刻:等转写验证的候选音频轮
        "active_resp": None,  # 当前在生成的 response id(定向 cancel 用;无 ID 的 cancel 会误杀新回合)
        "want_user": False,   # 已 ACCEPT 但撞上活跃 response → 等 done 再 create
        "hold_tool": None,    # 因有候选待验而暂缓的工具轮 create
        "seg": 0,             # 语音段号(speech_started 递增)
        "decided": 0,         # 已裁决到哪一段
        "seg_of": {},         # item_id → 它属于哪一段(M7:decided 必须记"被裁决的那段",不是"当前段")
        # ★ B2:**create 已发、response.created 未回** 的窗口(跨海 RTT 200-500ms)。
        #   没有它,active_resp 还是 None → 第二个 create 被无条件发出 → 撞
        #   conversation_already_has_active_response → pend 补发 → **同一句话答两遍**(症状根本没关掉)。
        #   同文件的 WS(Grok)路径早有等价守卫(_vg["pend_resp"]:1726),RTC 这条路漏配了。
        "inflight": False,
        "inflight_t": 0.0,
        "kill_next": False,   # 在途那条 create 一 created 就掐(此刻还没有 id,无法定向 cancel)
        "answered_ep": 0,     # 最近一次**真的出了内容**的纪元(用来发现"该答却哑掉")
        "retry_ep": 0,        # 已为哪个纪元补答过(每纪元只补一次,防死循环)
    }
    TURN_ASR_WAIT = 4.0

    async def _do_create(long_tool=False, user=False):
        """★ response.create 的**唯一出口**(B2)。任何绕过它的直发都会重开双答的口子。"""
        if gate["active_resp"] or gate["inflight"]:
            return False
        gate["inflight"] = True
        gate["inflight_t"] = time.time()
        try:
            await ows.send(json.dumps(_resp_create(long_tool, user)))
            return True
        except Exception:
            gate["inflight"] = False
            return False

    async def _burn_old_images():
        # 104:焚"上上轮及更早"的直喂图(保留最近一轮供追问;历史不再重复携带图 token)
        _keep = [it for it in _img_items if it[0] >= epoch["n"] - 2]
        for _ep0, _iid0 in _img_items:
            if _ep0 < epoch["n"] - 2:
                try:
                    await ows.send(json.dumps({"type": "conversation.item.delete", "item_id": _iid0}))
                except Exception:
                    pass
        _img_items[:] = _keep

    async def _inject_state():
        """把当前 页/选中/笔迹 状态注入对话。**只在 ACCEPT 后调**(133:确认是真轮才消费 _dirty3/_ink_fresh,
        否则一个噪声假轮就会把"刚画过"这个一次性边沿吃掉,真问题来时反而没有笔迹提示)。
        142(TPM 去重,治 Realtime 每 response 全额重算):① 照豆包路 _state_evt fp 模式——内容+笔迹指纹没变
        就不重注(重注一份 ≤1500 字 vtext 直接抬高每轮基线);② 覆盖式——注新的前先删上一条自己发的状态 item
        (item.delete 基建同 _burn_old_images/_reject_turn),只留最新一条,别让旧状态在上下文里堆积重复计费。"""
        if not (fe >= 3 and book.pop("_dirty3", None)):
            return
        _pg3 = book.get("page") or page
        # Phase2:本页简述在手→注要点替整页(省 token,深问再 read_page);缺失才降级注整页视口文本。
        #   rtc 不走 _fetch_book_ctx,简述按页懒拉一次(换页先清旧简述,再 HTTP 拉;pending/失败→"" 自动降级)。
        if book.get("_brief_pg") != _pg3:
            book["_brief_pg"] = _pg3
            book["brief"] = ""; book["brief_tags"] = []; book["page_type"] = ""
            try:
                book.update(await _fetch_brief(file_rel, _pg3))
            except Exception:
                pass
        _brief3 = _brief_line(book)
        _vt3 = (book.get("vtext") or book.get("page_text") or "")[:1500]
        if _brief3:
            _b3 = [f"用户此刻在第 {_pg3} 页。{_brief3}(要点摘要,能答就直接答;需要本页完整原文/更细内容才调 read_page(page:N))"]
            _full3 = False   # 只注了简述,整页正文**没**进上文 → 不登记短路名单,read_page 深问可正常拉全文
        else:
            _b3 = [f"用户此刻在第 {_pg3} 页" + (
                f",当前可见内容:{_vt3}(本页正文已给你,回答/高亮/做卡直接用它,别对当前页调 read_page;要**其它页**内容才调 read_page)"
                if _vt3 else ",需要页面内容就调 read_page")]
            _full3 = bool(_vt3)
        if book.get("sel"):
            _b3.append(f"选中了「{str(book['sel'])[:200]}」(他说『这段/我选的』就指它)")
        if book.get("ink_strokes"):
            if book.pop("_ink_fresh", None):
                _b3.append("他本页有手写笔迹、而且**刚刚画过**(你之前 see_ink 看到的内容已过时作废)"
                           "——接下来他若问「这是什么/这个/这里/什么意思」这类**没指明对象**的话,"
                           "默认就是在问这笔迹:**先调 see_ink**(不是 see_page,别看整页)再答;"
                           "他若点到具体的词/概念或要求翻页等,就按他说的来,别硬套笔迹")
            else:
                _b3.append("本页有他的手写笔迹(问到手写内容先调 see_ink,别用 see_page 看整页)")
        _txt3 = "(" + ";".join(_b3) + "。状态记录,不要回应本条。)"
        _fp3 = _txt3 + f"|ink{book.get('_ink_fp') or ''}"   # 掺笔迹指纹:字面相同但又画了新笔迹也要重注(照豆包 _state_evt)
        if _fp3 == _state_evt["fp"]:
            sys.stderr.write(f"[rtc-ctl] P3 状态未变,跳过重注 p{book.get('page')}\n")
            return   # 状态没变 → 不注(省 token + 防上下文灌水)
        _state_evt["fp"] = _fp3
        try:
            # Item3(TPM 感知覆盖,142→本批):旧代码无条件删上一条状态 item——但删会让它之后一大段前缀
            # 缓存失效($0.06→$0.60,10× 贵),纯成本看几乎总更亏。改成:只有这一分钟已读 input_tokens
            # 逼近 40000/min 硬墙(headroom<TPM_DANGER)时才删旧状态保命(少读一段字);还宽裕就保留旧的
            # (靠 truncation retention 0.8 慢裁),保住便宜的前缀缓存。不删也照常 create 新状态 + 更新 iid。
            _old3 = _state_evt.get("iid")
            if _old3:
                _headroom = _OA_TPM_LIMIT - _tpm_used_1min()
                if _headroom < TPM_DANGER:
                    try:
                        await ows.send(json.dumps({"type": "conversation.item.delete", "item_id": _old3}))
                    except Exception:
                        pass
                    sys.stderr.write(f"[rtc-ctl] Item3 删旧状态(TPM 紧张 headroom={_headroom})\n")
                else:
                    sys.stderr.write(f"[rtc-ctl] Item3 保留旧状态(TPM 宽裕 headroom={_headroom},省缓存)\n")
            _state_evt["n"] += 1
            _iid3 = f"st{_state_evt['n']}"
            _state_evt["iid"] = _iid3
            if _full3:   # 只有**整页正文**随本条注入才登记短路名单(简述替整页时不登记 → read_page 深问正常拉全文)
                _read_pages.add(_pg3)
            await ows.send(json.dumps({"type": "conversation.item.create", "item": {
                "id": _iid3, "type": "message", "role": "system",
                "content": [{"type": "input_text", "text": _txt3}]}}, ensure_ascii=False))
            sys.stderr.write(f"[rtc-ctl] P3 注入 p{book.get('page')}(vt={len(_vt3)}字 ink={bool(book.get('ink_strokes'))} iid={_iid3})\n")
        except Exception:
            pass

    async def _accept_turn(item_id: str, tx: str, why: str = ""):
        """这一轮是真人说话 → 推进纪元、注入状态、生成回答。"""
        # M7:裁决的是**这个 item 所属的那一段**,不是"当前段"(seg 在 speech_started 就已经往前跑了)。
        gate["decided"] = max(gate["decided"], gate["seg_of"].pop(item_id, gate["seg"]))
        epoch["n"] += 1            # 只有真轮才推进(在途工具结果随之作废=旧结果不抢新话轮)
        # M8:丢弃被暂缓的工具轮时,**别把它的模态决策位一起丢了** —— hold_tool["long"]=长工具结果该走文字模态,
        #     丢了会让 route 档口头念整页正文,念到 2048 保险丝被截断。
        _lt = bool(gate["hold_tool"] and gate["hold_tool"].get("long"))
        gate["hold_tool"] = None
        if tx:
            book["last_q"] = tx[:500]
        sys.stderr.write(f"[rtc-ctl] ✅放行 call={call_id[:12]} ep={epoch['n']} 「{tx[:30]}」{why}\n")
        # B1 回执:告诉前端"这一轮我接管了" → 前端撤销它的哑火兜底定时器(否则会重复 create)
        try:
            await bws.send(json.dumps({"event": "turn", "payload": {"verdict": "accept"}}))
        except Exception:
            pass
        await _burn_old_images()
        await _inject_state()
        if gate["active_resp"] or gate["inflight"]:
            # 用户插话打断。interrupt_response 已关 → 打断归我们管。
            # ⚠ M6:**先写 want_user 再发 cancel** —— 反过来的话,cancel 与 response.done 之间若让出事件循环,
            #    done 会先跑、看到 want_user 还是 False → 该轮永久哑,且防哑网被 want_user 永久缴械。
            gate["want_user"] = True
            gate["want_long"] = _lt
            if gate["active_resp"]:
                # 有 id → 定向 cancel(无 ID 的 cancel 会误杀比它更新的合法 response)
                try:
                    await ows.send(json.dumps({"type": "response.cancel", "response_id": gate["active_resp"]}))
                    await ows.send(json.dumps({"type": "output_audio_buffer.clear"}))
                except Exception:
                    pass
            else:
                gate["kill_next"] = True   # create 在途、还没有 id → 等 created 一到立刻掐
        else:
            await _do_create(long_tool=_lt, user=True)

    async def _reject_turn(item_id: str, tx: str, why: str):
        """假轮 → 从对话里删掉这条假的用户输入,不生成任何回答。
        (手动挡下此刻**还没有** response 被创建,所以 delete 是干净的——这正是评审强调的:
         自动挡里"回答都已经说出口了再删 item"是不可靠的工作流。)"""
        gate["decided"] = max(gate["decided"], gate["seg_of"].pop(item_id, gate["seg"]))   # M7:裁决"这个 item 那一段"
        sys.stderr.write(f"[rtc-ctl] 🚫假轮丢弃 call={call_id[:12]} 「{tx[:30]}」({why})\n")
        # ★ B1 回执(**关键**):假轮不会产生 response.created —— 不回执的话,前端的哑火兜底定时器
        #   会在超时后**替这个假轮补发一次 create**,整个闸门当场白做。必须显式告诉它"我判假了"。
        try:
            await bws.send(json.dumps({"event": "turn", "payload": {"verdict": "reject"}}))
        except Exception:
            pass
        try:
            await ows.send(json.dumps({"type": "conversation.item.delete", "item_id": item_id}))
        except Exception:
            pass
        # 候选清空 → 之前因它而暂缓的工具轮 create 可以放行了
        if not gate["pending"] and gate["hold_tool"] and not gate["active_resp"]:
            ht = gate["hold_tool"]
            gate["hold_tool"] = None
            if ht["ep"] == epoch["n"]:
                await _do_create(long_tool=ht["long"])

    async def _turn_timeout(item_id: str):
        """转写迟迟不来(ASR 慢/失败)→ 按真轮放行。宁可多答一次,也绝不让用户等不到回答。"""
        await asyncio.sleep(TURN_ASR_WAIT)
        if item_id in gate["pending"]:
            gate["pending"].pop(item_id, None)
            await _accept_turn(item_id, "", "(转写超时,按真轮放行)")

    async def _silence_watchdog(seg: int):
        """★ 防哑网(自审补):整条闸挂在 input_audio_buffer.committed 上。万一这个事件不来
        (语义 VAD 行为变了/协议改了/事件丢了),就会没有候选、没有超时 → **永远不生成回答**,
        从"答太多次"变成"一句都不答"——那比原病更糟。
        所以:说完话之后若这一段迟迟无人裁决、且没有候选在验、也没有回答在生成 → 强行放行。"""
        await asyncio.sleep(TURN_ASR_WAIT + 1.5)
        # M6:回收卡死的在途 create(created 永不回来 → inflight 永真 → 谁都别想再 create)
        if gate["inflight"] and time.time() - gate["inflight_t"] > 8:
            sys.stderr.write("[rtc-ctl] 🛟回收僵死 inflight(create 已发但 created 迟迟不来)\n")
            gate["inflight"] = False
        # M7:**不能**把 active_resp 列入缴械条件 —— 用户插话时 active_resp 恰恰非空,
        #     那正是防哑网最该救的场景。decided 才是"这一段有没有人管"的唯一判据。
        if gate["decided"] >= seg or gate["pending"] or gate["want_user"]:
            return
        sys.stderr.write(f"[rtc-ctl] 🛟防哑网:seg={seg} 无人裁决 → 强行放行(committed 事件可能没来)\n")
        await _accept_turn("", "", "(防哑网)")

    async def _down():
        n = {}
        async for raw in ows:
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            t = ev.get("type") or "?"
            n[t] = n.get(t, 0) + 1
            if t == "input_audio_buffer.speech_started":
                # 133 手动放行:这里**只知道"有声音"**,不知道是人还是噪声/回声 —— 什么都不做。
                # 旧代码在这里 epoch+=1 并消费状态,于是一个噪声假轮就能把正在跑的 see_ink 判成"已过期"
                # (用户实测"工具被打断"的直接原因),还会白吃掉"刚画过"这个一次性边沿。
                gate["seg"] += 1
            elif t == "input_audio_buffer.committed":
                # VAD 判定一轮结束并落成 item → 登记候选,等转写来验真伪(超时兜底见 _turn_timeout)
                _iid = ev.get("item_id") or ""
                if _iid:
                    gate["pending"][_iid] = time.time()
                    gate["seg_of"][_iid] = gate["seg"]   # M7:这个 item 属于哪一段
                    sys.stderr.write(f"[rtc-ctl] ⇢committed seg={gate['seg']} item={_iid[:14]}\n")   # M10
                    asyncio.create_task(_turn_timeout(_iid))
            elif t == "input_audio_buffer.speech_stopped":
                # 133:不再在这里 create —— 放行权归转写验证(见 transcription.completed)。
                # 但要挂一道防哑网:万一 committed/转写 这条链断了,这段话不能就此石沉大海。
                asyncio.create_task(_silence_watchdog(gate["seg"]))
            elif t == "response.function_call_arguments.done":
                if fe < 2:
                    continue   # 版本握手:旧前端自己执行工具,relay 只观察(防双执行)
                name = ev.get("name") or ""
                if name in ("reply_text", "wait_for_user"):
                    continue   # 纯前端语义(显示/静音),前端 dc 处理
                try:
                    a = json.loads(ev.get("arguments") or "{}")
                except Exception:
                    a = {}
                asyncio.create_task(_tool(name, a if isinstance(a, dict) else {}, ev.get("call_id") or "", epoch["n"]))
            elif t == "conversation.item.created":
                # 133 探针:前端经 data channel 直发的**系统状态消息**(墨迹/选中)relay 看不见发送侧,
                # 但 OpenAI 会把 item.created 广播给本 call 的所有连接——这里就能确认它到底进没进对话。
                _it = ev.get("item") or {}
                _c0 = (_it.get("content") or [{}])[0]
                sys.stderr.write(f"[rtc-ctl] ⇣item call={call_id[:12]} role={_it.get('role')} type={_it.get('type')} "
                                 f"「{str(_c0.get('text') or _c0.get('transcript') or '')[:50]}」\n")
            elif t == "response.created":
                # 133:记住**当前活跃 response 的 ID** —— 打断必须定向 cancel(无 ID 的 cancel 会误杀更新的合法回合)。
                gate["active_resp"] = ((ev.get("response") or {}).get("id") or "") or None
                gate["inflight"] = False   # B2:在途窗口关闭
                sys.stderr.write(f"[rtc-ctl] created call={call_id[:12]} resp={(ev.get('response') or {}).get('id', '')[:14]}\n")
                if gate["kill_next"] and gate["active_resp"]:
                    # B2:用户在"create 已发、created 未回"的窗口里插了话 —— 现在拿到 id 了,定向掐掉它
                    gate["kill_next"] = False
                    try:
                        await ows.send(json.dumps({"type": "response.cancel", "response_id": gate["active_resp"]}))
                        await ows.send(json.dumps({"type": "output_audio_buffer.clear"}))
                    except Exception:
                        pass
                # 安全(#284 加固):超支后 fe>=4 前端仍会经自己的 data channel 直发 response.create——
                # 本 sideband 对同 call_id 发 response.cancel 即可掐掉这一轮生成(输出音频=大头成本)。
                if book.get("_over"):
                    try:
                        await ows.send(json.dumps({"type": "response.cancel"}))
                    except Exception:
                        pass
            elif t == "response.done":
                gate["active_resp"] = None   # 133:本轮生成结束(含被我们 cancel 掉的)
                gate["inflight"] = False
                r0 = ev.get("response") or {}
                u = r0.get("usage") or {}
                otd = u.get("output_token_details") or {}
                _oa_log_usage(u, engine="openai_rtc", span=_span, resp_id=(ev.get("response") or {}).get("id") or "")   # 284/P3:usage 记账归 relay(sideband 自读,不再依赖前端上报)
                _tpm_win.append((time.time(), int(u.get("input_tokens") or 0)))   # Item3:喂进 1 分钟滑动窗口,供 TPM 感知覆盖决策
                sys.stderr.write(f"[rtc-ctl] done call={call_id[:12]} in={u.get('input_tokens')} out={u.get('output_tokens')} "
                                 f"[audio={otd.get('audio_tokens', 0)} text={otd.get('text_tokens', 0)}] "
                                 f"status={r0.get('status')}\n")
                if r0.get("status") not in ("completed", None):   # failed/cancelled 必须能查原因(只打 status 等于没打)
                    sys.stderr.write(f"[rtc-ctl] ⚠{r0.get('status')} 详情="
                                     f"{json.dumps(r0.get('status_details') or {}, ensure_ascii=False)[:400]}\n")
                # 安全(#284 加固):WebRTC 直连路径也在每轮记账后复查预算。媒体是浏览器↔OpenAI 直连、
                # relay 无法强拆,但 sideband 是服务端控制面(持同 call_id 的 ows),超支即置标记,之后每轮
                # response.created 立刻 response.cancel 让助手噤声——止住烧钱、逼用户挂断。event:-1 供留痕。
                if not book.get("_over"):
                    _bok3, _bspent3 = _budget_gate()
                    if not _bok3:
                        book["_over"] = True
                        sys.stderr.write(f"[rtc-ctl] 预算超支 ${_bspent3:.2f},sideband 掐断后续生成\n")
                        try:
                            await ows.send(json.dumps({"type": "response.cancel"}))
                        except Exception:
                            pass
                        try:
                            await bws.send(json.dumps({"event": -1, "payload": {"error": f"今日语音预算已用完(${_bspent3:.2f})——助手已停止应答,请挂断,明天再聊或调高 rt_budget_usd"}}, ensure_ascii=False))
                        except Exception:
                            pass
# 64:硬兜底已撤(用户拍板:体验差,大部分做对即可)——incomplete 只落盘记录,供之后按日志调 prompt/工具描述
                if r0.get("status") == "incomplete":
                    _vlog("truncated", page=book.get("page") or page, book=file_rel,
                          mode=_norm_vm(_creds().get("rt_voice_mode")), q=(book.get("last_q") or "")[:120],
                          brief="回复被 2048 保险丝掐断(分析素材:该轮该走 route 却口头念了)")
                # ★ 哑火自愈:response 直接 failed(实测 in=0 out=0,服务端瞬拒)时,这一轮用户就**什么都没听到**。
                #   代码本身的原则是"绝不允许该答却不答"——所以本纪元若一句都没产出,补发一次(每纪元只补一次)。
                if (u.get("output_tokens") or 0) > 0 and r0.get("status") == "completed":
                    gate["answered_ep"] = epoch["n"]
                elif (r0.get("status") in ("failed", "cancelled")
                      and not (u.get("output_tokens") or 0)
                      and gate["answered_ep"] < epoch["n"] and gate["retry_ep"] < epoch["n"]
                      and not gate["want_user"] and not gate["pending"] and not book.get("_over")):
                    gate["retry_ep"] = epoch["n"]
                    sys.stderr.write(f"[rtc-ctl] ♻哑火自愈 ep={epoch['n']}(上一条 {r0.get('status')} 且零输出)→ 补发一次\n")
                    await asyncio.sleep(0.35)   # 让服务端把上一条的状态收干净,免得补发又撞
                    await _do_create(user=True)
                    continue
                # 133:补发被暂缓的 create。优先级:**用户轮 > 撞车补发 > 工具轮**(用户永远优先)。
                if gate["want_user"]:      # 打断后等到 done 了 → 现在给用户这一轮生成回答
                    gate["want_user"] = False
                    await _do_create(long_tool=bool(gate.pop("want_long", False)), user=True)
                elif pend["create"]:   # 59:撞车被拒的工具回填 create 在此补发(active response 已结束)
                    pend["create"] = False
                    if pend["ep"] == epoch["n"]:   # 纪元没变才补(用户已开新话=旧工具结果不该再触发回答)
                        await _do_create()
                elif gate["hold_tool"] and not gate["pending"]:   # 候选已判完且无人接管 → 工具轮可以说话了
                    _ht = gate["hold_tool"]
                    gate["hold_tool"] = None
                    if _ht["ep"] == epoch["n"]:
                        await _do_create(long_tool=_ht["long"])
            elif t == "conversation.item.input_audio_transcription.completed":
                # ★ 133:手动放行闸的**判决点**。转写是旁路异步产物,但在手动挡下它正好可以当"这一轮是不是真人"的判据。
                _iid = ev.get("item_id") or ""
                _tx0 = (ev.get("transcript") or "").strip()
                # 指南§7.5/:258:转写是独立账单(usage 在本事件里),不混进 response.done——先记日志供估费(正式入账=#284)
                u2 = ev.get("usage") or {}
                sys.stderr.write(f"[rtc-ctl] 转写 call={call_id[:12]} 「{_tx0[:40]}」 usage={json.dumps(u2)[:80]}\n")
                if _iid and _iid in gate["pending"]:
                    gate["pending"].pop(_iid, None)
                    _ghost, _why = _is_asr_ghost(_tx0)
                    if _ghost:
                        await _reject_turn(_iid, _tx0, _why)
                    else:
                        await _accept_turn(_iid, _tx0)
                elif _tx0:
                    book["last_q"] = _tx0[:500]   # 已被超时放行/不归本闸管 → 只更新原话(route 档要用)
            elif t == "conversation.item.input_audio_transcription.failed":
                # 转写失败 → 无从判真伪。宁可多答一次,不可该答不答。
                _iid = ev.get("item_id") or ""
                if _iid and _iid in gate["pending"]:
                    gate["pending"].pop(_iid, None)
                    await _accept_turn(_iid, "", "(转写失败,按真轮放行)")
            elif t == "error":
                e0 = ev.get("error") or {}
                gate["inflight"] = False   # B2:create 被拒也要关掉在途窗口,否则永久卡死没人能再 create
                if e0.get("code") == "conversation_already_has_active_response":
                    pend["create"], pend["ep"] = True, epoch["n"]   # 59:等 response.done 补发,工具结果不至于永远无人回答
                sys.stderr.write(f"[rtc-ctl] err: {json.dumps(e0)[:150]}\n")
        sys.stderr.write(f"[rtc-ctl] sideband 关闭 call={call_id[:12]} 事件统计={json.dumps(n)[:300]}\n")

    async def _up():
        async for raw in bws:
            if isinstance(raw, bytes):
                continue
            try:
                j = json.loads(raw)
            except Exception:
                continue
            t = j.get("type")
            if t == "page":
                np = j.get("page") or 0
                if j.get("text") is not None:
                    book["vtext"] = str(j.get("text") or "")[:2000]   # 126(P3):前端视口文本(EPUB 动态窗/PDF 可见文字)
                    book["_dirty3"] = True
                if np and np != book["page"]:
                    book["page"] = np
                    book["_ink_fp"] = ""   # 换页:笔迹指纹作废(缓存键随之翻新)
                    book["_dirty3"] = True

                    async def _rf3(np1=np):   # 126(P3):翻页后台刷新页正文(注入归 relay 的原料)
                        try:
                            vc3 = await _fetch_book_ctx(file_rel, np1)
                            if book.get("page") == np1:
                                book["page_text"] = vc3.get("page_text") or ""
                        except Exception:
                            pass
                    asyncio.create_task(_rf3())
            elif t == "rtcstats":   # 124(#287):WebRTC 质量遥测(丢包/抖动ms/RTTms)→学习时间线,诊断"断续"用数据说话
                _st0 = j.get("s") or {}
                _vlog("rtcstats", text=json.dumps(_st0, ensure_ascii=False), page=book.get("page") or page, book=file_rel)
            elif t == "text":
                # H4:打字 = 用户改用别的方式表达了 → 之前那段还在等转写的音频**作废**。
                # 不清的话:4s 后 _turn_timeout 会把那半句废话"按真轮放行" → 定向 cancel 掉打字的回答
                # → 打字的答案被拦腰砍断,然后再答一遍那半句废话。
                gate["pending"].clear()
                gate["seg_of"].clear()
                gate["decided"] = gate["seg"]
                gate["hold_tool"] = None
                epoch["n"] += 1   # 打字提问=新话轮(与 speech_started 同语义)
            elif t == "state":
                book["sel"] = (j.get("sel") or "")[:400]
                book["_dirty3"] = True
            elif t == "ink":
                strokes = j.get("strokes") or []
                sys.stderr.write(f"[rtc-ctl] ⇡ink call={call_id[:12]} p{j.get('page')} 笔画={len(strokes)}\n")   # 133 探针:前端到底推没推墨迹
                if strokes:
                    book["_ink_fresh"] = True   # 133 边沿:刚画过 → 下一次 speech_started 注入强措辞(模糊指代默认指笔迹)
                book["ink_strokes"] = strokes[:60]
                book["view_shot"] = j.get("shot")   # EPUB 笔迹合成图(前端 syncInk 拍;PDF/空=None 自动清)→ see_ink 用
                book["_dirty3"] = True
                try:   # 全笔画哈希(审核 P1:"笔画数+末点"不同图形易撞)
                    book["_ink_fp"] = hashlib.md5(json.dumps(strokes, sort_keys=True).encode()).hexdigest()[:10] if strokes else ""
                except Exception:
                    book["_ink_fp"] = str(len(strokes))
            elif t == "shot":
                sid = j.get("shot_id")
                f = shot_fut.get(sid) if sid is not None else (next(iter(shot_fut.values()), None))   # 无 id=59 旧前端,兼容取唯一 pending
                if f and not f.done():
                    f.set_result({"b64": j.get("b64") or "", "media_type": j.get("media_type") or "image/jpeg"})

    try:
        done, pending = await asyncio.wait(
            [asyncio.create_task(_down()), asyncio.create_task(_up())],
            return_when=asyncio.FIRST_COMPLETED)
        for p_ in pending:
            p_.cancel()
    finally:
        try:   # 133:退出并发注册表(不删=假阳性告警的老根源)
            _lv = _RTC_CTL_LIVE.get(_uid_m)
            if _lv is not None:
                _lv.pop(call_id, None)
                if not _lv:
                    _RTC_CTL_LIVE.pop(_uid_m, None)
        except Exception:
            pass
        try:
            await ows.close()
        except Exception:
            pass
        try:
            await bws.close()
        except Exception:
            pass


async def handle_browser(bws):
    """一个浏览器连接 = 一路豆包通话。ws URL 可带 ?file=<rel>&page=<n>(阅读器浮层传)→ 书页上下文注入。"""
    file_rel, page, fresh = "", 0, False
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(bws.request.path).query)
        m0 = (q.get("mode") or [""])[0]
        if m0 == "tts":      # 朗读专用通道(v3-⑬):不开麦,只有 T2S 流式合成(侧栏「🔊 朗读」lazy 连)
            await handle_tts_only(bws)
            return
        if m0 == "agent":    # agent 模式:耳+嘴分离,大脑=侧栏助手(见 handle_agent);file/page → ASR 语境注入(㉓)
            await handle_agent(bws,
                               (q.get("file") or [""])[0],
                               int((q.get("page") or ["0"])[0] or "0"))
            return
        if m0 == "rtc":      # ㊺P1 RtcController:WebRTC 通话的服务端控制面(sideband,设计见 references/rtc-controller-design.md)
            await handle_rtc_ctl(bws,
                                 (q.get("call_id") or [""])[0],
                                 (q.get("file") or [""])[0],
                                 int((q.get("page") or ["0"])[0] or "0"),
                                 int((q.get("fe") or ["1"])[0] or "1"),
                                 (q.get("uid") or [""])[0],
                                 (q.get("tk") or [""])[0])
            return
        file_rel = (q.get("file") or [""])[0]
        page = int((q.get("page") or ["0"])[0] or "0")
        fresh = (q.get("fresh") or ["0"])[0] == "1"   # 🧹 新话题:清空记忆重新开始
    except Exception:
        pass
    cred = _creds()
    if cred.get("rt_engine") in ("openai", "grok"):   # ㉔/94:通话引擎切 GPT Realtime / Grok Voice(电话按钮同一入口,relay 层换引擎)
        await handle_openai(bws, file_rel, page, engine=cred.get("rt_engine"), fresh=fresh)
        return
    if cred.get("api_key"):
        # 新版鉴权(实测 2026-07):单 API Key,只要 X-Api-Key + Resource-Id,无需 APP ID/固定 App-Key
        headers = {
            "X-Api-Key": cred["api_key"],
            "X-Api-Resource-Id": "volc.speech.dialog",
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }
    elif cred.get("app_id") and cred.get("access_token"):
        # 旧版鉴权(AppID + Access Token + 固定 App-Key)
        headers = {
            "X-Api-App-ID": str(cred["app_id"]),
            "X-Api-Access-Key": cred["access_token"],
            "X-Api-Resource-Id": "volc.speech.dialog",
            "X-Api-App-Key": FIXED_APP_KEY,
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }
    else:
        await bws.send(json.dumps({"event": -1, "payload": {
            "error": "缺凭证:把 API Key 填进 ~/.config/doubao-voice.json 的 api_key 字段"}}, ensure_ascii=False))
        await bws.close()
        return
    sid = str(uuid.uuid4())
    book = await _fetch_book_ctx(file_rel, page)   # 书页文本 + 助手历史(没有也照常通话)
    book["tools_lines"] = await _fetch_tools_lines("doubao_s2s")   # registry 的豆包投影；虚拟工具也由同一目录给出
    try:
        async with websockets.connect(DOUBAO_WSS, additional_headers=headers,
                                      max_size=10 * 1024 * 1024, open_timeout=15) as dws:
            # 握手:StartConnection → StartSession(注入书页/助手历史/上次 dialog_id)
            await dws.send(enc(T_FULL_CLIENT, 1, b"{}"))
            await dws.send(enc(T_FULL_CLIENT, 100,
                               json.dumps(_start_session_payload(book, file_rel, page, fresh=fresh),
                                          ensure_ascii=False).encode(), session_id=sid))

            # v3-⑩ B:SP 指纹 + 251 ack 确认制——内容没变且上次已被豆包确认 → 不发 UpdateConfig
            # (450 每次开口的"无条件重推"改为"没确认送达才重推":UpdateConfig 被丢的兜底语义保留,
            #  但 SP 没变的开口不再打掉前缀缓存)。confirmed 只在收到 251 ConfigUpdated 时前移。
            _sp_deb = {"t": None}
            _sp_state = {"confirmed": "", "pending": ""}

            def _sp_fp(txt: str) -> str:
                import hashlib
                return hashlib.md5(txt.encode("utf-8")).hexdigest()

            async def _push_sp():   # UpdateConfig(201) 热更新 SP+tts(翻页/设置面板改音色语速;闭包读最新 book/page)
                c2 = _creds()
                role_txt = _role_text(c2, book, file_rel, page)
                tts_now = _tts_cfg(c2)
                fp = _sp_fp(role_txt + json.dumps(tts_now, sort_keys=True))
                if fp == _sp_state["confirmed"]:
                    return   # 没变且已确认 → 不推(省 UpdateConfig + 保前缀缓存)
                if fp == _sp_state["pending"] and time.monotonic() - _sp_state.get("pending_at", 0) < 5:
                    return   # 同内容已在途且未超时;超 5s 没等到 251 视为被丢 → 放行重推(原"开口兜底"语义)
                _sp_state["pending"] = fp
                _sp_state["pending_at"] = time.monotonic()
                upd = {"tts": tts_now,
                       "dialog": {"bot_name": c2.get("bot_name", "豆包"),
                                  "system_role": role_txt,
                                  "speaking_style": c2.get("speaking_style", "语气自然友好,不啰嗦。")}}
                await dws.send(enc(T_FULL_CLIENT, 201, json.dumps(upd, ensure_ascii=False).encode(), session_id=sid))

            # StartSession 已带初始 SP → 视为已生效(151 SessionStarted 无 SP ack,这里直接锚定基线)
            try:
                _c0 = _creds()
                _sp_state["confirmed"] = _sp_fp(_role_text(_c0, book, file_rel, page) + json.dumps(_tts_cfg(_c0), sort_keys=True))
            except Exception:
                pass

            async def _inject_memory(u_text: str, a_text: str):
                """ConversationCreate(510):把系统事件按时序写进对话历史(不带 timestamp=追加到末尾)。
                用户定调:与其程序兜底,不如更新 AI 的认知——"历史压 SP"的机制反过来用:
                把正确认知+它自己的承诺写成**最近记忆**,模型倾向自我一致,下轮自然兑现。"""
                pl = {"items": [{"role": "user", "text": u_text}, {"role": "assistant", "text": a_text}]}
                await dws.send(enc(T_FULL_CLIENT, 510, json.dumps(pl, ensure_ascii=False).encode(), session_id=sid))

            # v3-⑭:状态变化不再碰 SP(改 SP=打掉其后**整个对话历史**的前缀缓存,用户 transformer 洞察)——
            # 防抖平息后把合并状态快照经 510 **追加**到对话末尾(前缀不变,历史缓存全保)。
            # 原"笔迹记忆注入"被状态事件吸收(三态语义都在 _state_event_text 里);指纹去重防重复注入。
            _state_evt = {"fp": ""}

            async def _inject_state():
                txt = _state_event_text(book)
                fp = txt + f"|v{book.get('ink_ver', 0)}"   # 掺笔迹版本:字面相同但又画了新笔迹也要重注
                if fp == _state_evt["fp"]:
                    return   # 状态没变 → 不注(省 token + 防历史灌水)
                _state_evt["fp"] = fp
                await _inject_memory(txt, "收到,以这条最新状态为准。")
                sys.stderr.write(f"[voice-rt] 状态事件注入({len(txt)}字)\n")

            def _push_state_debounced(delay: float = 1.2):
                """防抖:连续画笔/滚动选中的风暴平息后注一条合并状态事件(510 追加,零缓存损失)。"""
                t = _sp_deb.get("t")
                if t and not t.done():
                    t.cancel()

                async def _later():
                    try:
                        await asyncio.sleep(delay)
                        await _inject_state()
                    except asyncio.CancelledError:
                        pass
                    except Exception as ex:
                        sys.stderr.write(f"[voice-rt state] {ex}\n")
                _sp_deb["t"] = asyncio.create_task(_later())

            # 开话时页面已有状态(sidecar 笔迹等)→ 立即注初始状态事件(SP 说明是"没有消息=什么都没有")
            if book.get("has_ink") or (book.get("sel") or "").strip() or book.get("figs_n"):
                asyncio.create_task(_inject_state())

            async def up():   # 浏览器 → 豆包
                nonlocal page
                async for msg in bws:
                    if isinstance(msg, (bytes, bytearray)):
                        await dws.send(enc(T_AUDIO_CLIENT, 200, bytes(msg), session_id=sid, raw=True))
                    else:
                        try:
                            j = json.loads(msg)
                        except Exception:
                            continue
                        t = j.get("type")
                        if t == "hello" and j.get("content"):      # 开场白
                            await dws.send(enc(T_FULL_CLIENT, 300, json.dumps(
                                {"content": j["content"]}, ensure_ascii=False).encode(), session_id=sid))
                        elif t == "text" and j.get("content"):     # 文本 query(不说话时打字;意图由模型回复侧协议解析)
                            book["user_q"] = j["content"]           # 记本轮用户问题(笔迹询问程序兜底用)
                            _vlog("q", text=j["content"], page=page, book=file_rel)
                            await dws.send(enc(T_FULL_CLIENT, 501, json.dumps(
                                {"content": j["content"]}, ensure_ascii=False).encode(), session_id=sid))
                        elif t == "page":   # 用户翻页 → 拉新页文本 → UpdateConfig(201)热更新 system prompt(通话上下文跟着页走)
                            try:
                                np = int(j.get("page") or 0)
                            except Exception:
                                np = 0
                            _vtext = (j.get("text") or "").strip()   # ㉟b EPUB 动态窗口:前端直接带"实际显示内容"(整章太长,视口文本才是用户在看的)
                            if np and _vtext and file_rel:
                                page = np
                                book["page_text"] = _vtext[:2000]
                                book["brief"] = ""; book["brief_tags"] = []; book["page_type"] = ""   # 前端直供视口文本,无简述→降级用整页文本
                                await _push_sp()
                                _vlog("page", page=np, book=file_rel)
                                sys.stderr.write(f"[voice-rt] 视口同步 → p{np}({len(book['page_text'])}字,前端直供)\n")
                            elif np and np != page and file_rel:
                                page = np
                                ctx2 = await _fetch_book_ctx(file_rel, np)
                                book.update({k: ctx2.get(k) for k in ("page_text", "inked", "has_ink", "figures", "vocab")})   # 直塞内容整体换页
                                book.update({"brief": ctx2.get("brief") or "", "brief_tags": ctx2.get("brief_tags") or [],
                                             "page_type": ctx2.get("page_type") or ""})   # Phase2:换页简述同步
                                book["ink_strokes"] = []          # 换页:上页实时墨迹作废(新页的由 syncInk 再推)
                                book["view_shot"] = None          # 上页笔迹合成图也作废(防 see_ink 用到陈旧图)
                                book["ink_seen_ver"] = 0          # "看过"记录跨页无效(新页的笔迹没看过)
                                await _push_sp()                   # SP 换页文本(v3-⑭ 后 SP 唯一的变化源)
                                _push_state_debounced()            # 换页状态清零(无笔迹/无选中)也要告知
                                _vlog("page", page=np, book=file_rel)
                                sys.stderr.write(f"[voice-rt] 翻页同步 → p{np}({len(book.get('page_text') or '')}字)\n")
                        elif t == "state":   # 选中/chip 状态实时同步(前端指纹去重后推;relay 再比对,变了才热更 SP)
                            ns = {"sel": (j.get("sel") or "")[:300],
                                  "focus": (j.get("focus") or "")[:200],
                                  "figs_n": int(j.get("figs") or 0)}
                            if any(book.get(k) != v for k, v in ns.items()):
                                if ns["sel"] and ns["sel"] != book.get("sel"):
                                    _vlog("sel", text=ns["sel"][:200], page=page, book=file_rel)
                                book.update(ns)
                                _push_state_debounced()
                                sys.stderr.write(f"[voice-rt] 选中/chip 同步 sel={len(ns['sel'])}字 figs={ns['figs_n']}\n")
                        elif t == "ink":   # 通话中新圈画:同步 has_ink 状态 + 存**实时 strokes**(随工具调用走,see_ink 不等 sidecar)
                            try:
                                ip = int(j.get("page") or 0)
                            except Exception:
                                ip = 0
                            if ip and ip == page and file_rel:
                                strokes = j.get("strokes") or []
                                book["has_ink"] = bool(strokes)
                                book["ink_strokes"] = strokes[:60]
                                book["view_shot"] = j.get("shot")   # EPUB 笔迹合成图(前端 syncInk 拍;PDF/空=None 自动清)→ see_ink 用
                                book["ink_ver"] = book.get("ink_ver", 0) + 1   # 版本+1(前端指纹去重,到达即真变化)→ 三态/缓存键都随它走
                                _vlog("ink", n=len(strokes), page=ip, book=file_rel)
                                _push_state_debounced()
                                sys.stderr.write(f"[voice-rt] 圈画同步 p{ip} strokes={len(strokes)} v{book['ink_ver']}\n")
                        elif t == "cfg":   # 设置面板改了语音配置(音色/语速/人设)→ 热更(指纹含 tts,变了才真发)
                            asyncio.create_task(_push_sp())
                        elif t == "tool_abort":   # 用户点转圈按钮中止工具执行(v3-⑯b)
                            book["deep_abort"] = True   # 深度思考走自己的打断路径(停发不补 end 包)
                            tk = book.get("tool_task")
                            if tk and not tk.done():
                                tk.cancel()   # 掐掉 relay 侧等待(webapp 里已发出的执行不追);finally 已摘牌
                                try:
                                    rag = json.dumps([{"title": "系统通知",
                                                       "content": "用户手动取消了刚才那个工具的执行,不会再有结果;简短确认一句即可,别道歉太多。"}], ensure_ascii=False)
                                    await dws.send(enc(T_FULL_CLIENT, 502, json.dumps({"external_rag": rag}, ensure_ascii=False).encode(), session_id=sid))
                                except Exception:
                                    pass
                            await bws.send(json.dumps({"event": "tool_status", "payload": {"status": "aborted", "label": "已中止"}}, ensure_ascii=False))
                            sys.stderr.write("[voice-rt] 工具执行被用户中止\n")
                        elif t == "finish":
                            await dws.send(enc(T_FULL_CLIENT, 102, b"{}", session_id=sid))

            async def down():   # 豆包 → 浏览器
                reply_buf: dict = {}       # reply_id → 攒的模型回复文本(550 增量)
                reply_fired: set = set()   # 本轮已触发工具的 reply_id(550 即时检测与 559 兜底去重)
                suppress: set = set()      # 该轮 550 已进入 JSON 段 → 后续增量不进字幕
                sent_len: dict = {}        # reply_id → 已重组下发给前端的字符数(字幕撕裂防护)
                mute = False               # 静音 v1(350.text 判命令句):实测 S2S 的 350 text 恒空、粒度=整轮 → 基本不触发,留作兜底
                drop_audio = False         # 静音 v2(时序法):JSON 固定在回复末尾 → fire 时刻后到达的音频≈JSON 段 → 丢到 359 TTSEnded
                async for frame in dws:
                    d = dec(frame)
                    if d["type"] == T_AUDIO_SERVER:                     # TTSResponse 音频(pcm_s16le 24k)
                        if not mute and not drop_audio:
                            await bws.send(d["payload"])
                            rec = book.get("ack_rec")   # v3-⑪ 确认语录音窗口(350 chat_tts_text 开 → 359 存):tee 一份
                            if rec and rec.get("on") and len(rec["buf"]) < 500_000:
                                rec["buf"] += d["payload"]
                        continue
                    if d["type"] in (T_FULL_SERVER, T_ERROR):
                        try:
                            pl = json.loads(d["payload"].decode("utf-8", "ignore") or "{}")
                        except Exception:
                            pl = {"raw": d["payload"][:200].decode("utf-8", "ignore")}
                        ev = d.get("event")
                        fwd = True   # 本事件是否转发前端
                        if ev == 150 and pl.get("dialog_id"):   # 存 dialog_id → 下次通话接续记忆
                            try:
                                DIALOG_ID_FILE.write_text(pl["dialog_id"])
                            except Exception:
                                pass
                        elif ev in (251, 567, 568, 570, 571):   # 配置/上下文操作的 ack(低频,常开日志:验证 UpdateConfig/记忆注入是否真生效)
                            if ev == 251 and _sp_state["pending"]:
                                _sp_state["confirmed"] = _sp_state["pending"]   # 豆包确认收到这版 SP → 指纹前移
                                _sp_state["pending"] = ""
                            sys.stderr.write(f"[voice-rt ack] ev={ev} {str(pl)[:80]}\n")
                        elif ev == 154:   # UsageResponse:每轮 token 用量 → 按天记账(v3-⑩ A)
                            fwd = False
                            _log_usage(pl)
                        elif ev == 450:   # 用户开口:①重推最新 SP(指纹确认制:没变且已确认的不推) ②打断深度思考播报(官方要求:被打断不补 end 包)
                            book["deep_abort"] = True
                            book["ack_rec"] = None   # 确认语合成被打断 → 丢弃残缺录音(防缓存半句)
                            if book.get("ctx_notes"):   # 统一端口:pending 通告随开口注入(502 external_rag,append-only 不动 SP 前缀)
                                _cn0 = book.pop("ctx_notes")
                                _rag_n = json.dumps([{"title": "系统状态通告(背景信息,不要复述本条)",
                                                      "content": ";".join(_cn0)[:1500]}], ensure_ascii=False)
                                asyncio.create_task(dws.send(enc(T_FULL_CLIENT, 502, json.dumps({"external_rag": _rag_n}, ensure_ascii=False).encode(), session_id=sid)))
                            asyncio.create_task(_push_sp())
                        elif ev == 451:   # 记录用户语音终稿(笔迹询问程序兜底判定用)
                            try:
                                r0 = (pl.get("results") or [{}])[0]
                                if r0.get("text") and r0.get("is_interim") is False:
                                    book["user_q"] = r0["text"]
                                    _vlog("q", text=r0["text"], page=page, book=file_rel)
                            except Exception:
                                pass
                        elif ev == 350:   # 轮级合成开始:按 tts_type 区分——确认语(chat_tts_text)/结果播报(external_rag)绝不丢
                            tt = pl.get("tts_type") or ""
                            if tt in ("chat_tts_text", "external_rag"):
                                drop_audio = False
                            if tt == "chat_tts_text" and book.get("ack_rec") is not None:
                                book["ack_rec"]["on"] = True   # 确认语合成轮开始 → 开录(v3-⑪)
                            if DEBUG:
                                sys.stderr.write(f"[voice-rt 350] tt={tt} drop={drop_audio}\n")
                            if _is_cmd_sent(pl.get("text") or ""):   # v1 兜底(实测 text 恒空,基本不触发)
                                mute = True
                                fwd = False
                        elif ev == 351:   # 句结束:解除静音 v1
                            if mute:
                                mute = False
                                fwd = False
                        elif ev == 359:   # 本轮 TTS 结束:解除静音 v2(RAG 播报等下一轮音频不受影响)
                            drop_audio = False
                            rec = book.get("ack_rec")
                            if rec and rec.get("on"):   # 确认语录完 → 存盘,同句以后 relay 直接回放(v3-⑪)
                                book["ack_rec"] = None
                                if len(rec["buf"]) > 4800:   # <0.1s 的不存(异常/被打断)
                                    try:
                                        _ack_pcm_path(rec["txt"]).write_bytes(bytes(rec["buf"]))
                                        sys.stderr.write(f"[voice-rt] 确认语已缓存「{rec['txt']}」({len(rec['buf'])//1000}KB)\n")
                                    except Exception as ex:
                                        sys.stderr.write(f"[voice-rt ack save] {ex}\n")
                        elif ev == 550:   # 回复增量:攒 + JSON 完整即触发;字幕**重组转发**(JSON 段整段不出现在字幕,含撕裂的前缀)
                            rid = pl.get("reply_id") or "_"
                            reply_buf[rid] = reply_buf.get(rid, "") + (pl.get("content") or "")
                            fwd = False   # 由下面重组下发,原事件不转发
                            if rid not in suppress:
                                buf, start = reply_buf[rid], sent_len.get(rid, 0)
                                m = _TOOL_START.search(buf)
                                if m:
                                    suppress.add(rid)
                                    drop_audio = True   # JSON **开头一出现**就开丢(TTS 合成超实时,等 JSON 完整时念白早放出去了)
                                    emit = buf[start:m.start()]   # JSON 起点前未发的部分发掉
                                    sent_len[rid] = m.start()
                                else:
                                    avail = buf[start:]
                                    hold = 0   # 尾部若可能是 `{"tool"` 的撕裂前缀 → 扣下等下一增量确认
                                    k = avail.rfind("{")
                                    if k >= 0:
                                        body = avail[k + 1:].lstrip()
                                        if body == "" or '"tool"'.startswith(body[:7]):
                                            hold = len(avail) - k
                                    emit = avail[:len(avail) - hold] if hold else avail
                                    sent_len[rid] = start + len(emit)
                                if emit:
                                    await bws.send(json.dumps({"event": 550, "payload": {"content": emit}}, ensure_ascii=False))
                            if rid not in reply_fired:
                                cmd = _extract_tool_json(reply_buf[rid])
                                if cmd:
                                    reply_fired.add(rid)
                                    drop_audio = True   # 静音 v2:此后到达的音频≈JSON 段,丢到 359(TTSEnded)
                                    book['tool_task'] = asyncio.create_task(_run_voice_tool(bws, dws, sid, cmd, file_rel, page, book=book, push_sp=_push_sp))
                        elif ev == 559:   # 回复结束:flush 未发的尾巴;增量没触发过但全文含 "tool" → 整段交服务端顽强解析兜底
                            rid = pl.get("reply_id") or "_"
                            full = reply_buf.pop(rid, "")
                            if rid not in suppress:
                                rest = full[sent_len.get(rid, 0):]
                                if rest:
                                    await bws.send(json.dumps({"event": 550, "payload": {"content": rest}}, ensure_ascii=False))
                            if full and rid not in reply_fired and '"tool"' in full:
                                book['tool_task'] = asyncio.create_task(_run_voice_tool(bws, dws, sid, full, file_rel, page, book=book, push_sp=_push_sp))
                            elif full and rid not in reply_fired:
                                g = _GOPAGE_FALLBACK.search(full)   # 翻页特例兜底(见常量注释)
                                if g:
                                    await bws.send(json.dumps({"event": "client_action",
                                                               "payload": {"fn": "goToPage", "args": [int(g.group(1))]}}, ensure_ascii=False))
                                # (笔迹询问的程序兜底已撤——用户裁定:程序兜底有漏洞且模型升级体验不跟涨,
                                #  改用 ConversationCreate(510) 记忆注入按时序更新它的认知,见 _push_sp_debounced)
                            # v3-⑩ F:长对话摘要护栏——自然对话轮(非工具 JSON 轮)记 QA 摘录;
                            # 攒满 26 轮 → 最旧 12 轮压成一条 510 注入(拼接式,不调外部模型),防服务端
                            # 滚出 12K 窗口时丢早期脉络。豆包 12K 硬限已封顶输入费用,这条主要保认知连续。
                            if full and rid not in reply_fired and '"tool"' not in full:
                                _vlog("a", text=full[:2000], page=page, book=file_rel)
                                ql = book.setdefault("qa_log", [])
                                ql.append(((book.get("user_q") or "")[:24], full[:36]))
                                if len(ql) >= 26:
                                    old, book["qa_log"] = ql[:12], ql[12:]
                                    digest = ";".join(f"我:{u}→你:{a}" for u, a in old if u or a)[:700]
                                    asyncio.create_task(_inject_memory(
                                        f"(我们更早聊过这些,给你留个备忘:{digest})",
                                        "好,前面聊过的这些我记住了,后面可以直接接着说。"))
                                    sys.stderr.write(f"[voice-rt] 长对话摘要注入({len(old)}轮压缩)\n")
                            reply_fired.discard(rid)
                            suppress.discard(rid)
                            sent_len.pop(rid, None)
                        if fwd:
                            await bws.send(json.dumps({"event": ev, "code": d.get("code"),
                                                       "payload": pl}, ensure_ascii=False))
                        if d["type"] == T_ERROR or ev in (152, 153):
                            break

            done, pending = await asyncio.wait(
                [asyncio.create_task(up()), asyncio.create_task(down())],
                return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            try:   # 尽量优雅收尾(文档:发完 FinishSession 再断)
                await dws.send(enc(T_FULL_CLIENT, 102, b"{}", session_id=sid))
            except Exception:
                pass
    except Exception as ex:
        try:
            await bws.send(json.dumps({"event": -1, "payload": {"error": f"豆包连接失败: {type(ex).__name__}: {str(ex)[:160]}"}},
                                      ensure_ascii=False))
        except Exception:
            pass
    finally:
        try:
            await bws.close()
        except Exception:
            pass


async def main():
    c = _creds()
    if not (c.get("api_key") or c.get("app_id")):
        print("[voice-rt] ⚠ 未配置凭证(~/.config/doubao-voice.json),服务先监听;浏览器连接会收到提示", file=sys.stderr)
    async with websockets.serve(handle_browser, LISTEN_HOST, LISTEN_PORT, max_size=8 * 1024 * 1024):   # 截图上行(P2 shot)可达数MB,2MiB 超限=整条控制 WS 被关
        print(f"[voice-rt] listening ws://{LISTEN_HOST}:{LISTEN_PORT}", file=sys.stderr)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
