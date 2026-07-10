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
import gzip
import json
import re
import struct
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
SAUC_RID = "volc.bigasr.sauc.duration"                                     # 时长版计费
TTS_URL = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"    # NDJSON 流式合成(备用)
TTS_BIDI_WSS = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"    # 双向流式合成(主用:文本增量进,session 级韵律连贯)
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
            r = await hc.get("/api/assistant/history")
            d = r.json()
            if d.get("ok"):
                out["history"] = d.get("messages") or []
    except Exception as ex:
        sys.stderr.write(f"[voice-rt ctx] {ex}\n")
    return out


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
    role += ("\n你直接接着这本阅读器的工具层(目录在最后)。规则(与系统里另一个编排助手完全同一套):"
             "\n- **下面直接给你的内容(本页文字/生词/选中/插图描述等)能答的直接答,别调工具**。"
             "\n- 需要工具时(找视频/翻页/查别页/看图细节/高亮/做卡片笔记/查词等):"
             "**整条回复只输出一条 JSON**:{\"tool\":\"工具名\",\"args\":{...}}——**一个字都别多说,不要任何开场白**。"
             "系统会把这条静音、替你向用户播一句确认语,再执行;字符串值里**别用双引号、别换行**(要引号用「」)。"
             "系统执行后会把真实结果发给你,那时再口语化讲给用户;自己编的结果都是假的,会害用户。每轮最多调一个工具。"
             "\n例:用户说『翻到第8页』→ 你的完整回复就是:{\"tool\":\"goto_page\",\"args\":{\"page\":8}}"
             "\n**没输出 JSON 页面是不会动的**;之前『先说一句再输出JSON』『直接说翻到第N页』的老办法都已作废。"
             "\n- 语音场景:回答**默认两三句话说清**,别铺开长篇;用户说『详细讲讲/展开』才展开。")
    lines = book.get("tools_lines") or {}
    if lines:
        role += ("\n可用工具目录(冒号后是用途,{}是 args 字段;**最下方的实时状态说某工具当前无效时以状态为准**):\n"
                 + "\n".join(lines.values()))
    # ── ② 中层:跟页走的内容(翻页才变) ──
    page_text = book.get("page_text") or ""
    if page_text:
        name = (file_rel.rsplit("/", 1)[-1] or "这本书")
        role += f"\n用户此刻正在读《{name}》第 {page} 页,本页文字内容(直接可用):\n{page_text}"
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
            parts.append("本页有手写笔迹(问『我圈的/画的』→ 调 see_ink 看合成图再答,别猜)")
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
    "deep_think": "我想想,稍等。",
}
# 深度思考虚拟工具(v3-⑥):不在 TOOLS 注册表(它是语音专属体验)——relay 拦截,调助手 chat 流式,
# answer 增量按句经 ChatTTSText(500) 分片**边生成边播**(不等全文;官方流式协议 start/content/end)。
DEEP_TOOL_LINE = "- deep_think: 复杂专业问题/数学推导/逻辑推理,交给深度思考模型详细解答并念给用户(慢,简单问题别用) {question:完整问题}"
_SENT_SPLIT = re.compile(r"[^。！？!?;；\n]+[。！？!?;；\n]+")


def _speech_clean(s: str) -> str:
    """markdown → 可朗读文本(粗清,与前端 cleanForSpeech 同思路)。"""
    s = re.sub(r"```[\s\S]*?```", " 代码略 ", s)
    s = re.sub(r"\$\$?([^$]{1,120})\$\$?", r"\1", s)
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"[#*_`>|~]+", " ", s)
    s = re.sub(r"[(（]\s*(?:第\s*\d+\s*[-~至]?\s*\d*\s*页|p\.?\s*\d+)\s*[)）]", "", s, flags=re.I)   # 页码引用不念(显示保留)
    return re.sub(r"[ \t]+", " ", s)


async def _run_deep_think(bws, dws, sid, question: str, file_rel: str, page: int,
                          book: dict, push_sp=None):
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
        await bws.send(json.dumps({"event": "tool_status", "payload": {"status": "running", "label": "深度思考"}}, ensure_ascii=False))
        body = {"message": f"(语音深度解答,直接详细推理回答,少用工具)\n{question}",
                "rid": f"dt{uuid.uuid4().hex[:10]}", "voice": 1,
                "context": ({"file_rel": file_rel, "page": page} if file_rel else {})}
        # 深度模型选型:模型设置面板「深度思考」项(action-prefs deep,⑦的 UI)优先;凭证 deep_model/deep_effort 兜底
        dm, de = cfg.get("deep_model"), cfg.get("deep_effort")
        try:
            async with httpx.AsyncClient(base_url=WEBAPP, headers=_webapp_headers(), timeout=8) as hc0:
                r0 = await hc0.get("/api/assistant/action-prefs")
                pref = ((r0.json().get("actions") or {}).get("deep") or {}).get("pref") or {}
                if pref.get("backend") == "claude":
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
            "status": "done" if started else "error", "tool": "deep_think", "label": "深度思考",
            "result_brief": _speech_clean(answer)[:400]}}, ensure_ascii=False))
    except Exception as ex:
        sys.stderr.write(f"[voice-rt deep] {ex}\n")
        try:
            await bws.send(json.dumps({"event": "tool_status", "payload": {"status": "error", "label": f"深度思考:{str(ex)[:50]}"}}, ensure_ascii=False))
        except Exception:
            pass
    finally:
        (book.get("tasks") or {}).pop("deep_think", None)
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

    cache = book.setdefault("tool_cache", {})
    if targs0 is not None:   # 程序级防重复(用户设计):同工具同参数同页面状态 → 直接复用上次结果,不再执行
        hit = cache.get(_ckey(tname, targs0))
        if hit:
            try:
                if hit.get("ca"):
                    await bws.send(json.dumps({"event": "client_action", "payload": hit["ca"]}, ensure_ascii=False))   # 视频卡等重放
                await bws.send(json.dumps({"event": "tool_status", "payload": {
                    "status": "done", "tool": tname, "label": f"{tname}(复用上次结果)", "cached": True}}, ensure_ascii=False))
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
        await bws.send(json.dumps({"event": "tool_status", "payload": {"status": "running", "label": tname}}, ensure_ascii=False))
        ctx = {"file_rel": file_rel, "page": page} if file_rel else {}
        if book.get("ink_strokes"):
            ctx["ink"] = book["ink_strokes"]   # 实时墨迹随工具走(see_ink/see_page 合成图用它,与侧栏行为一致)
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
        await bws.send(json.dumps({"event": "tool_status", "payload": {
            "status": "done" if d.get("ok") else "error", "tool": tool, "label": d.get("label") or tool,
            "args": d.get("args"), "took_s": d.get("took_s"),
            "result_brief": json.dumps(res, ensure_ascii=False)[:400]}}, ensure_ascii=False))
        slim = {k: v for k, v in res.items() if k != "client_action"}
        # v3-⑩:RAG 回填按工具分级限长(进历史的每个字后续轮轮计费)——列表类给短(模型只需播报要点),
        # 视觉/阅读类给足(信息密度高);统一 3000 的旧上限只留给未知工具兜底
        lim = _RAG_LIMIT.get(tool, 1400)
        content = (json.dumps(slim, ensure_ascii=False)[:lim] if slim else "(无文本结果,界面元素已显示在屏幕上)")
        rag = json.dumps([{"title": f"工具 {tool} 的真实执行结果(涉及的界面元素已显示在用户屏幕上)",
                           "content": content + "\n(请把要点口语化讲给用户;你此前口头猜测的内容一律作废)"}],
                         ensure_ascii=False)
        await dws.send(enc(T_FULL_CLIENT, 502, json.dumps({"external_rag": rag}, ensure_ascii=False).encode(), session_id=sid))
        if d.get("ok") and d.get("cacheable"):   # 只读工具 → 按「工具+参数+页+墨迹版本」缓存,重复询问直接复用
            cache[_ckey(d.get("tool"), d.get("args"))] = {
                "content": content, "ca": ca if (isinstance(ca, dict) and ca.get("fn")) else None}
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


async def _fetch_tools_lines() -> dict:
    """拉工具目录(与编排 agent 同一注册表)→ {name: 压缩行}。O2.0 上下文 12K,desc 只留第一句。
    v3-⑩ 起目录**全量注入且恒定**(进 SP 稳定前缀保前缀缓存);"无笔迹 see_ink 无效"的
    状态语义由 _role_text 尾部的实时状态声明承接(原门控=按状态增删行,一画笔目录就变,
    排在它后面的页文本/生词整层缓存连坐失效)。"""
    try:
        async with httpx.AsyncClient(base_url=WEBAPP, headers=_webapp_headers(), timeout=10) as hc:
            r = await hc.get("/api/assistant/tools")
            d = r.json()
        out = {}
        for t in (d.get("tools") or []):
            desc = re.split(r"[。;;]", (t.get("desc") or "").replace("*", ""))[0][:52]
            out[t["name"]] = f"- {t['name']}: {desc}"
        return out
    except Exception as ex:
        sys.stderr.write(f"[voice-rt tools] {ex}\n")
        return {}


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


def _tts_channel(bws, key: str, speaker: str):
    """双向流式 TTS 通道(v3-⑬ 抽共用:agent 模式朗读 / tts-only 朗读通道两处用):
    一轮回答 = 一条 ws + 一个 session(session 内文本增量连续合成 → 韵律连贯、句间零连接开销)。
    打断(cancel)= 直接 close 连接:立即哑火(FinishSession 会把已收文本全部合成完,打断场景不能用)。
    返回 {"speak","done","cancel"} 三个协程,音频帧直接裸转发给 bws。"""
    tts = {"ws": None, "sid": None, "reader": None, "gen": 0}

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

    async def speak(text: str):
        text = (text or "").strip()
        if not text:
            return
        try:
            await _ensure()
            await tts["ws"].send(enc(T_FULL_CLIENT, 200, json.dumps({
                "user": {"uid": "voice-agent"}, "event": 200, "namespace": "BidirectionalTTS",
                "req_params": {"text": text},
            }, ensure_ascii=False).encode(), session_id=tts["sid"]))
        except Exception as ex:
            sys.stderr.write(f"[voice-tts>] {ex}\n")
            await cancel()

    async def done():   # 这轮回答文本发完:FinishSession → 服务端把尾巴合成完 → 152
        if tts["ws"] and tts["sid"]:
            try:
                await tts["ws"].send(enc(T_FULL_CLIENT, 102, b"{}", session_id=tts["sid"]))
            except Exception:
                await cancel()

    return {"speak": speak, "done": done, "cancel": cancel}


async def handle_tts_only(bws):
    """朗读专用通道(v3-⑬,`?mode=tts`):侧栏助手回答的 T2S 流式播放——**不开麦、不连 ASR**。
    「🔊 朗读」点亮且没在语音通话时前端 lazy 连这条;speak/speak_done/cancel 协议与 agent 模式同款。"""
    cred = _creds()
    if not cred.get("api_key"):
        await bws.send(json.dumps({"event": -1, "payload": {"error": "缺凭证:~/.config/doubao-voice.json"}}, ensure_ascii=False))
        await bws.close()
        return
    ch = _tts_channel(bws, cred["api_key"], cred.get("tts_speaker", TTS_SPEAKER))
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
                await ch["speak"](j["text"])
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


async def handle_agent(bws):
    cred = _creds()
    if not cred.get("api_key"):
        await bws.send(json.dumps({"event": -1, "payload": {"error": "缺凭证:~/.config/doubao-voice.json"}}, ensure_ascii=False))
        await bws.close()
        return
    key = cred["api_key"]
    speaker = cred.get("tts_speaker", TTS_SPEAKER)
    asr_headers = {"X-Api-Key": key, "X-Api-Resource-Id": SAUC_RID,
                   "X-Api-Connect-Id": str(uuid.uuid4()), "X-Api-Request-Id": str(uuid.uuid4())}
    asr_cfg = {"user": {"uid": "voice-agent"},
               "audio": {"format": "pcm", "codec": "raw", "rate": 16000, "bits": 16, "channel": 1},
               # end_window_size:静音多久判一句说完(definite)。不设的话默认窗口极长,连续流里永远等不到终稿
               "request": {"model_name": "bigmodel", "enable_punc": True,
                           "end_window_size": int(cred.get("asr_end_window_ms", 800))}}
    ch = _tts_channel(bws, key, speaker)   # 朗读(可选):speak/speak_done/cancel 由 up() 转发
    try:
        async with websockets.connect(SAUC_WSS, additional_headers=asr_headers,
                                      max_size=10 * 1024 * 1024, open_timeout=15) as aws:
            await aws.send(_sauc_frame(0b0001, 0b0001, 1, json.dumps(asr_cfg, ensure_ascii=False).encode()))
            await aws.recv()   # 握手回包(空 result)
            await bws.send(json.dumps({"event": "agent_ready"}, ensure_ascii=False))
            seq = 1
            buf = bytearray()

            async def up():   # 浏览器 → (音频)豆包ASR / (speak)TTS 队列
                nonlocal seq
                async for msg in bws:
                    if isinstance(msg, (bytes, bytearray)):
                        buf.extend(msg)
                        while len(buf) >= 3200:      # 攒成 100ms 包再发(sauc 推荐粒度)
                            seq += 1
                            await aws.send(_sauc_frame(0b0010, 0b0001, seq, bytes(buf[:3200])))
                            del buf[:3200]
                        continue
                    try:
                        j = json.loads(msg)
                    except Exception:
                        continue
                    t = j.get("type")
                    if t == "speak" and (j.get("text") or "").strip():
                        await ch["speak"](j["text"])
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


async def handle_browser(bws):
    """一个浏览器连接 = 一路豆包通话。ws URL 可带 ?file=<rel>&page=<n>(阅读器浮层传)→ 书页上下文注入。"""
    file_rel, page, fresh = "", 0, False
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(bws.request.path).query)
        m0 = (q.get("mode") or [""])[0]
        if m0 == "tts":      # 朗读专用通道(v3-⑬):不开麦,只有 T2S 流式合成(侧栏「🔊 朗读」lazy 连)
            await handle_tts_only(bws)
            return
        if m0 == "agent":    # agent 模式:耳+嘴分离,大脑=侧栏助手(见 handle_agent)
            await handle_agent(bws)
            return
        file_rel = (q.get("file") or [""])[0]
        page = int((q.get("page") or ["0"])[0] or "0")
        fresh = (q.get("fresh") or ["0"])[0] == "1"   # 🧹 新话题:清空记忆重新开始
    except Exception:
        pass
    cred = _creds()
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
    book["tools_lines"] = await _fetch_tools_lines()   # 工具目录(与编排 agent 同一注册表,开话拉一次;SP 组装时按状态门控)
    book["tools_lines"]["deep_think"] = DEEP_TOOL_LINE   # 深度思考虚拟工具(语音专属,relay 拦截流式代播)
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
                            await dws.send(enc(T_FULL_CLIENT, 501, json.dumps(
                                {"content": j["content"]}, ensure_ascii=False).encode(), session_id=sid))
                        elif t == "page":   # 用户翻页 → 拉新页文本 → UpdateConfig(201)热更新 system prompt(通话上下文跟着页走)
                            try:
                                np = int(j.get("page") or 0)
                            except Exception:
                                np = 0
                            if np and np != page and file_rel:
                                page = np
                                ctx2 = await _fetch_book_ctx(file_rel, np)
                                book.update({k: ctx2.get(k) for k in ("page_text", "inked", "has_ink", "figures", "vocab")})   # 直塞内容整体换页
                                book["ink_strokes"] = []          # 换页:上页实时墨迹作废(新页的由 syncInk 再推)
                                book["ink_seen_ver"] = 0          # "看过"记录跨页无效(新页的笔迹没看过)
                                await _push_sp()                   # SP 换页文本(v3-⑭ 后 SP 唯一的变化源)
                                _push_state_debounced()            # 换页状态清零(无笔迹/无选中)也要告知
                                sys.stderr.write(f"[voice-rt] 翻页同步 → p{np}({len(book.get('page_text') or '')}字)\n")
                        elif t == "state":   # 选中/chip 状态实时同步(前端指纹去重后推;relay 再比对,变了才热更 SP)
                            ns = {"sel": (j.get("sel") or "")[:300],
                                  "focus": (j.get("focus") or "")[:200],
                                  "figs_n": int(j.get("figs") or 0)}
                            if any(book.get(k) != v for k, v in ns.items()):
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
                                book["ink_ver"] = book.get("ink_ver", 0) + 1   # 版本+1(前端指纹去重,到达即真变化)→ 三态/缓存键都随它走
                                _push_state_debounced()
                                sys.stderr.write(f"[voice-rt] 圈画同步 p{ip} strokes={len(strokes)} v{book['ink_ver']}\n")
                        elif t == "cfg":   # 设置面板改了语音配置(音色/语速/人设)→ 热更(指纹含 tts,变了才真发)
                            asyncio.create_task(_push_sp())
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
                            asyncio.create_task(_push_sp())
                        elif ev == 451:   # 记录用户语音终稿(笔迹询问程序兜底判定用)
                            try:
                                r0 = (pl.get("results") or [{}])[0]
                                if r0.get("text") and r0.get("is_interim") is False:
                                    book["user_q"] = r0["text"]
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
                                    asyncio.create_task(_run_voice_tool(bws, dws, sid, cmd, file_rel, page, book=book, push_sp=_push_sp))
                        elif ev == 559:   # 回复结束:flush 未发的尾巴;增量没触发过但全文含 "tool" → 整段交服务端顽强解析兜底
                            rid = pl.get("reply_id") or "_"
                            full = reply_buf.pop(rid, "")
                            if rid not in suppress:
                                rest = full[sent_len.get(rid, 0):]
                                if rest:
                                    await bws.send(json.dumps({"event": 550, "payload": {"content": rest}}, ensure_ascii=False))
                            if full and rid not in reply_fired and '"tool"' in full:
                                asyncio.create_task(_run_voice_tool(bws, dws, sid, full, file_rel, page, book=book, push_sp=_push_sp))
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
    async with websockets.serve(handle_browser, LISTEN_HOST, LISTEN_PORT, max_size=2 * 1024 * 1024):
        print(f"[voice-rt] listening ws://{LISTEN_HOST}:{LISTEN_PORT}", file=sys.stderr)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
