"""全站语音助手后端(阶段 0 管道)。

路由(注册到 app.py 的 register_voice):
  POST /api/voice/transcribe   multipart 'audio' 音频 → Cloud STT 转文字 → {ok,text}
  POST /api/voice/agent        {transcript, context} → agent → {ok,speak,client_actions,server_results,confirm}
  GET  /api/voice/ping         自检(key/ffmpeg 在否)

阶段 0 只打通"说话→文字→念回":transcribe 真转录;agent 先做"会话作答 + 结构化返回",
工具映射(112 个动作)+ 危险动作确认在阶段 1/2 接上。返回结构已定型,前端契约不变。

- STT:Google Cloud Speech-to-Text(GCP key `AIzaSy*`,走 GCP 赠金 —— Gemini key 额度已枯竭,
  改用这条;两个池子不通,见 skill google-apis §0.2)。中文为主 + 英/日 备选语言。
  iPad MediaRecorder 出的 mp4/webm 先 ffmpeg 转 flac 16k mono。
- Agent 大脑:复用 scripts/ai_client.ask(Claude;settings 里的后端)。
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import requests
from flask import Blueprint, jsonify, request, session

bp = Blueprint("voice", __name__, url_prefix="/api/voice")

CLAUDE_DIR = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
GCP_KEY_FILE = Path(os.environ.get("GCP_API_KEY_FILE", "/home/bwicarus/.config/gcp-vision-key"))
STT_URL = "https://speech.googleapis.com/v1/speech:recognize"


def _gcp_key() -> str:
    try:
        return GCP_KEY_FILE.read_text("utf-8").strip()
    except Exception:
        return os.environ.get("GCP_API_KEY", "")


def _logged_in() -> bool:
    return bool(session.get("user_id"))


def _has_ffmpeg() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        return True
    except Exception:
        return False


def _ffmpeg_to_flac(src: Path, dst: Path) -> bool:
    """任意 iOS 录音(mp4/webm/m4a)→ flac 16k 单声道。失败回 False。"""
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-ar", "16000", "-ac", "1", str(dst)],
            check=True, capture_output=True, timeout=60)
        return dst.exists() and dst.stat().st_size > 0
    except Exception:
        return False


# 常用命令/导航词:音频层发音偏置(屏幕实体另走高 boost,见 _cloud_stt)
_STT_CORE_HINTS = ["下一页", "上一页", "翻页", "跳到", "放大", "缩小", "适应宽度",
                   "双页", "单页", "连续滚动", "全屏", "去白边", "注音", "振假名", "整页翻译",
                   "搜索", "知识点", "侧栏", "生词本", "回书架", "返回", "确定", "取消",
                   "翻译", "健身", "学习看板", "技能树", "复习仪表盘"]
_LANG_ALTS = ["cmn-Hans-CN", "en-US", "ja-JP"]


def _stt_parse(data: dict) -> str:
    if "error" in data:
        raise RuntimeError("STT: " + data["error"].get("message", ""))
    parts = []
    for res in data.get("results", []):
        alt = (res.get("alternatives") or [{}])[0]
        t = alt.get("transcript", "").strip()
        if t:
            parts.append(t)
    return " ".join(parts).strip()


def _cloud_stt(audio_b64: str, encoding: str, sample_rate: int, lang: str, hints) -> str:
    """Cloud STT。command_and_search(短指令)+ speechContexts(屏幕实体当发音偏置,谐音纠错的音频层)。
    失败自动降级到最小 config(去掉 alternatives/speechContexts)再试一次 —— 优雅降级,别比啥都没做更差。"""
    key = _gcp_key()
    if not key:
        raise RuntimeError("no gcp key")
    lang = lang or "cmn-Hans-CN"
    ctx = []
    ents = [h for h in (hints or []) if isinstance(h, str) and h.strip()][:200]
    if ents:
        ctx.append({"phrases": ents, "boost": 15})        # 屏幕实体:核心偏置(研究建议 12-18,别全局 20)
    ctx.append({"phrases": _STT_CORE_HINTS, "boost": 14})  # 常用命令词:必中(研究建议 12-18)
    full_cfg = {
        "languageCode": lang,
        "model": "command_and_search",   # 老 model 对 inline speechContexts 稳定生效(latest_* 中文 adaptation 覆盖不确定)
        "encoding": encoding,
        "sampleRateHertz": sample_rate,
        "audioChannelCount": 1,
        "enableAutomaticPunctuation": False,
        "maxAlternatives": 1,
        "alternativeLanguageCodes": [c for c in _LANG_ALTS if c != lang][:2],
        "speechContexts": ctx,
    }
    min_cfg = {"languageCode": lang, "model": "command_and_search",
               "encoding": encoding, "sampleRateHertz": sample_rate, "audioChannelCount": 1}
    last = ""
    for cfg in (full_cfg, min_cfg):
        r = requests.post(f"{STT_URL}?key={key}",
                          json={"config": cfg, "audio": {"content": audio_b64}}, timeout=60)
        try:
            sys.path.insert(0, str(CLAUDE_DIR / "scripts"))
            from google_api_quota import log_usage
            log_usage("stt", 1, "speech:recognize", note=f"voice status={r.status_code}")
        except Exception:
            pass
        if r.status_code == 200:
            return _stt_parse(r.json())
        last = f"STT HTTP {r.status_code}: {r.text[:160]}"
        # 仅在 full→min 之间重试(min 再失败就抛)
    raise RuntimeError(last)


@bp.route("/transcribe", methods=["POST"])
def voice_transcribe():
    if not _logged_in():
        return jsonify({"ok": False, "error": "auth"}), 401
    f = request.files.get("audio")
    if not f:
        return jsonify({"ok": False, "error": "no audio"}), 400
    lang = (request.form.get("lang") or "cmn-Hans-CN").strip()
    try:
        hints = json.loads(request.form.get("hints") or "[]")
        if not isinstance(hints, list):
            hints = []
    except Exception:
        hints = []
    raw = f.read()
    fn = (f.filename or "").lower()
    is_wav = fn.endswith(".wav") or raw[:4] == b"RIFF"
    try:
        if is_wav:
            # 浏览器 Web Audio 编的 16k mono LINEAR16 WAV → 直接喂(WAV 自带头,无需 ffmpeg)
            text = _cloud_stt(base64.b64encode(raw).decode(), "LINEAR16", 16000, lang, hints)
        else:
            # 老客户端 mp4/webm → ffmpeg 转 flac 兜底
            with tempfile.TemporaryDirectory() as td:
                src = Path(td) / ("in" + (Path(fn).suffix or ".bin"))
                src.write_bytes(raw)
                flac = Path(td) / "a.flac"
                if not _ffmpeg_to_flac(src, flac):
                    return jsonify({"ok": False, "error": "ffmpeg 转码失败"}), 500
                text = _cloud_stt(base64.b64encode(flac.read_bytes()).decode(), "FLAC", 16000, lang, hints)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500
    return jsonify({"ok": True, "text": text})


_VOICE_LOG = CLAUDE_DIR / "state" / "logs" / "voice.log"


@bp.route("/log", methods=["POST"])
def voice_log():
    """前端 beacon 日志(关键步骤)。sendBeacon 可能不带 cookie → 宽松接收,只落盘不鉴权。"""
    try:
        data = (request.get_data(as_text=True) or "")[:2000].replace("\n", " ").replace("\r", " ")
        _VOICE_LOG.parent.mkdir(parents=True, exist_ok=True)
        try:
            if _VOICE_LOG.exists() and _VOICE_LOG.stat().st_size > 2_000_000:
                _VOICE_LOG.replace(_VOICE_LOG.with_suffix(".log.1"))
        except Exception:
            pass
        with _VOICE_LOG.open("a", encoding="utf-8") as fp:
            fp.write(data + "\n")
    except Exception:
        pass
    return ("", 204)


# ── 快路径意图(规则匹配,零 LLM、即时):PDF 阅读器常用口令直接出客户端动作 ──
import re as _re

_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6,
           "七": 7, "八": 8, "九": 9, "十": 10, "百": 100}


def _parse_num(s: str):
    """从口令里抠页码:阿拉伯数字优先;否则解析中文数字(到几百足够)。"""
    m = _re.search(r"\d+", s)
    if m:
        return int(m.group())
    cn = _re.search(r"[零一二两三四五六七八九十百]+", s)
    if not cn:
        return None
    t = cn.group(); v = 0; section = 0
    for ch in t:
        n = _CN_NUM.get(ch, 0)
        if n == 100:
            section = (section or 1) * 100; v += section; section = 0
        elif n == 10:
            section = (section or 1) * 10; v += section; section = 0
        else:
            section += n
    return (v + section) or None


def _act(fn, args, speak):
    return {"speak": speak, "client_actions": [{"fn": fn, "args": args}],
            "server_results": [], "confirm": None}


# ── 动作清单(同时给:fast_intent 出动作 + LLM 兜底用作工具表 + 服务端白名单校验)──
# 每项 (fn, 说明, 参数格式)。fn 必须是前端 window 全局函数(reader.js 或 voice.js 定义)。
_PDF_ACTIONS = [
    ("changePage", "翻页", "args=[1]下一页 / [-1]上一页"),
    ("goToPage", "跳到指定页", "args=[页码整数]"),
    ("zoomChange", "缩放", "args=[0.15]放大 / [-0.15]缩小"),
    ("fitWidth", "适应宽度铺满", "args=[]"),
    ("toggleSpread", "单页↔双页 切换", "args=[]"),
    ("toggleReadMode", "单页模式↔连续滚动 切换", "args=[]"),
    ("toggleFullscreen", "全屏 开关", "args=[]"),
    ("toggleCrop", "去白边 开关", "args=[]"),
    ("toggleRuby", "注音(振假名/英文音标) 开关", "args=[]"),
    ("togglePageTranslate", "整页翻译 开关", "args=[]"),
    ("openSearch", "打开搜索框", "args=[]"),
    ("toggleSidebar", "知识点侧栏 开关", "args=[]"),
    ("toggleVocab", "生词本 开关", "args=[]"),
    ("goPdfList", "回到书架/书本列表", "args=[]"),
]
_GLOBAL_ACTIONS = [
    ("__voiceGo", "跳转到网站某个页面",
     "args=[路径]: /pdf/ 书架, /insights/ 学习看板, /skilltree/ 技能树, "
     "/dashboard/ 复习仪表盘, /private/fitness/ 健身, /history/ 问答历史, /profile/ 个人设置"),
]
_WHITELIST = {fn for fn, _, _ in _PDF_ACTIONS} | {fn for fn, _, _ in _GLOBAL_ACTIONS}
_NAV_OK = {"/pdf/", "/insights/", "/skilltree/", "/dashboard/", "/private/fitness/", "/history/", "/profile/"}

# 全局导航(任何页面都可用),映射到 window.__voiceGo
_NAV = [
    (r"(健身|训练|锻炼|举铁|肌肉)", "/private/fitness/", "好,去健身"),
    (r"(学习看板|数据看板|学习数据|学习分析|洞察|insights)", "/insights/", "好,打开学习看板"),
    (r"(技能树|知识图谱|知识树|skilltree)", "/skilltree/", "好,打开技能树"),
    (r"(复习仪表盘|复习面板|今日复习|复习计划|仪表盘|dashboard)", "/dashboard/", "好,打开复习仪表盘"),
    (r"(书架|看书|读书|阅读器|去读|pdf)", "/pdf/", "好,去书架"),
    (r"(问答历史|历史记录|对话历史|history)", "/history/", "好,打开历史"),
    (r"(个人设置|账号设置|我的设置|profile)", "/profile/", "好,打开个人设置"),
]


def _nav_intent(s):
    for pat, path, speak in _NAV:
        if _re.search(pat, s):
            return _act("__voiceGo", [path], speak)
    return None


def _fast_intent(t: str, context: dict):
    """命中常用口令→即时出动作(零 LLM),否则 None(交给 LLM 兜底)。"""
    s = t.replace(" ", "")
    if (context or {}).get("page_type") == "pdf":
        # 翻页
        if _re.search(r"(下一?[页张个]|后一?[页张]|往[后下]|翻过去|next)", s):
            return _act("changePage", [1], "好,下一页")
        if _re.search(r"(上一?[页张个]|前一?[页张]|往[前上]|退回去|previous|back)", s):
            return _act("changePage", [-1], "好,上一页")
        # 跳页:第N页 / 跳到N / 翻到N
        if _re.search(r"(第.*[页张]|跳到|翻到|去第|到第|page)", s):
            n = _parse_num(s)
            if n:
                return _act("goToPage", [n], f"好,翻到第{n}页")
        # 缩放 / 适应
        if _re.search(r"(放大|拉大|大一[点些]|zoom\s*in)", s):
            return _act("zoomChange", [0.15], "放大一点")
        if _re.search(r"(缩小|拉小|小一[点些]|zoom\s*out)", s):
            return _act("zoomChange", [-0.15], "缩小一点")
        if _re.search(r"(适应|铺满|满屏|自适应|合适宽度|fit)", s):
            return _act("fitWidth", [], "好,适应宽度")
        # 排版模式
        if _re.search(r"双页", s):
            return _act("toggleSpread", [], "切到双页")
        if _re.search(r"(单页模式|切.*单页|单页阅读|^单页$)", s):   # 不匹配「这一页/一页纸」等(那些是提问/数量)
            return _act("toggleReadMode", [], "切到单页")
        if _re.search(r"(连续模式|连续滚动|切.*连续|滚动模式)", s):
            return _act("toggleReadMode", [], "切到连续")
        if _re.search(r"(全屏|fullscreen)", s):
            return _act("toggleFullscreen", [], "全屏")
        if _re.search(r"(去边|裁边|裁切|去白边|边距)", s):
            return _act("toggleCrop", [], "切换去边")
        if _re.search(r"(振假名|假名|注音|音标|ruby)", s):
            return _act("toggleRuby", [], "切换注音")
        if _re.search(r"(整页翻译|译页|翻译这页|翻译本页|全页翻译|翻译整页)", s):
            return _act("togglePageTranslate", [], "切换整页翻译")
        # 面板
        if _re.search(r"(搜索|查找|找一?下|search)", s):
            return _act("openSearch", [], "打开搜索")
        if _re.search(r"(知识点|关联|侧栏)", s):
            return _act("toggleSidebar", [], "打开知识点")
        if _re.search(r"(生词本|单词本|生词列表)", s):
            return _act("toggleVocab", [], "打开生词本")
        if _re.search(r"(回书架|书本列表|回到列表|选书)", s):
            return _act("goPdfList", [], "回到书架")
    # 全局导航(任意页面)
    nav = _nav_intent(s)
    if nav:
        return nav
    return None


# ── LLM 兜底:把自然语言映射成真实可执行动作(fast_intent 没命中时走这里)──
def _action_catalog(page_type: str) -> str:
    lines = []
    if page_type == "pdf":
        lines.append("【PDF 阅读器动作(仅当前在阅读器时可用)】")
        for fn, desc, args in _PDF_ACTIONS:
            lines.append(f"- {fn}:{desc}({args})")
    lines.append("【全站导航(任何页面都可用)】")
    for fn, desc, args in _GLOBAL_ACTIONS:
        lines.append(f"- {fn}:{desc}({args})")
    return "\n".join(lines)


def _validate_actions(actions):
    """只放行白名单函数 + 规整参数,挡住 LLM 幻觉的函数名/坏参数。"""
    out = []
    for a in (actions or []):
        if not isinstance(a, dict):
            continue
        fn = a.get("fn")
        if fn not in _WHITELIST:
            continue
        args = a.get("args", [])
        if not isinstance(args, list):
            args = [args]
        try:
            if fn == "goToPage":
                args = [int(args[0])]
            elif fn == "changePage":
                args = [1 if int(args[0]) >= 0 else -1]
            elif fn == "zoomChange":
                args = [float(args[0])]
            elif fn == "__voiceGo":
                p = str(args[0]).strip()
                if p not in _NAV_OK:
                    continue
                args = [p]
            else:
                args = []   # 其余都是无参 toggle
        except (IndexError, ValueError, TypeError):
            continue
        out.append({"fn": fn, "args": args})
    return out


def _extract_json(text: str):
    """从 LLM 输出里抠出第一个完整 JSON 对象(容忍前后噪声/代码块围栏)。"""
    i = text.find("{")
    if i < 0:
        return None
    depth = 0
    for j in range(i, len(text)):
        c = text[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[i:j + 1])
                except Exception:
                    return None
    return None


_LLM_SYS = (
    "你是学习网站的语音指挥助手。用户用语音说一句话,你判断意图并直接给出可执行结果:\n"
    "1) 操作类(翻页/缩放/跳转/打开某功能/去某页面等)→ 从下方动作清单选出要执行的动作,"
    "返回 {\"say\":\"一句简短中文应承\",\"actions\":[{\"fn\":\"函数名\",\"args\":[参数]}]}。可一次多个动作。"
    "函数名必须严格来自清单,参数照清单格式,别编造。\n"
    "2) 提问/闲聊(不需要操作页面)→ 返回 {\"say\":\"简短中文口语回答,≤2句\",\"actions\":[]}。\n"
    "只输出 JSON 本身,不要解释、不要 markdown、不要代码块围栏。say 会被语音念出,务必简短自然。"
)


def _llm_intent(transcript: str, context: dict) -> dict:
    """Claude 把口令映射成真实动作(或简短作答)。返回定型结构,动作经白名单校验。"""
    pt = (context or {}).get("page_type")
    ctx_txt = json.dumps(context, ensure_ascii=False)[:800] if context else "(无)"
    prompt = (f"{_LLM_SYS}\n\n【可用动作清单】\n{_action_catalog(pt)}\n\n"
              f"【当前页面】{ctx_txt}\n【用户说】{transcript}\n\n只输出 JSON:")
    raw = ""
    try:
        sys.path.insert(0, str(CLAUDE_DIR / "scripts"))
        import ai_client
        raw = (ai_client.ask(prompt, claude_model="haiku", claude_effort="low") or "").strip()
    except Exception as e:
        return {"speak": "我这边出了点问题:" + str(e)[:80],
                "client_actions": [], "server_results": [], "confirm": None}
    data = _extract_json(raw) or {}
    say = (data.get("say") or "").strip()
    actions = _validate_actions(data.get("actions"))
    if not say:
        # JSON 解析失败但原文像句答话 → 当作答;否则兜底
        say = raw if (raw and len(raw) < 200 and "{" not in raw) else (
            "好的" if actions else "没太听清,能再说一遍吗?")
    return {"speak": say, "client_actions": actions, "server_results": [], "confirm": None}


@bp.route("/agent", methods=["POST"])
def voice_agent():
    if not _logged_in():
        return jsonify({"ok": False, "error": "auth"}), 401
    body = request.get_json(silent=True) or {}
    transcript = (body.get("transcript") or "").strip()
    context = body.get("context") or {}
    if not transcript:
        return jsonify({"ok": False, "error": "empty transcript"}), 400
    out = _fast_intent(transcript, context)   # 常用口令:规则即时执行,不走 LLM
    if out is None:
        out = _llm_intent(transcript, context)   # 兜底:Claude 映射成真实动作 / 简短作答
    return jsonify({"ok": True, **out})


@bp.route("/ping")
def voice_ping():
    return jsonify({"ok": True, "has_gcp_key": bool(_gcp_key()), "ffmpeg": _has_ffmpeg()})


def register_voice(app):
    app.register_blueprint(bp)
