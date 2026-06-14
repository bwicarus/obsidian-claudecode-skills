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


def _cloud_stt(flac_path: Path) -> str:
    """Cloud Speech-to-Text 转录。中文为主 + 英/日 备选。返回拼接的整段文字。"""
    key = _gcp_key()
    if not key:
        raise RuntimeError("no gcp key")
    content = base64.b64encode(flac_path.read_bytes()).decode()
    r = requests.post(f"{STT_URL}?key={key}", json={
        "config": {
            "encoding": "FLAC",
            "sampleRateHertz": 16000,
            "languageCode": "zh-CN",
            "alternativeLanguageCodes": ["en-US", "ja-JP"],   # 学日/英语,口令多为中文
            "enableAutomaticPunctuation": True,
            # 不指定 model:default 模型支持 zh-CN + 多语言(latest_short/enhanced 对 zh-CN 报 400 不支持)
        },
        "audio": {"content": content},
    }, timeout=120)
    try:
        sys.path.insert(0, str(CLAUDE_DIR / "scripts"))
        from google_api_quota import log_usage
        log_usage("stt", 1, "speech:recognize", note=f"voice status={r.status_code}")
    except Exception:
        pass
    if r.status_code != 200:
        raise RuntimeError(f"STT HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    if "error" in data:
        raise RuntimeError("STT: " + data["error"].get("message", ""))
    parts = []
    for res in data.get("results", []):
        alt = (res.get("alternatives") or [{}])[0]
        t = alt.get("transcript", "").strip()
        if t:
            parts.append(t)
    return " ".join(parts).strip()


@bp.route("/transcribe", methods=["POST"])
def voice_transcribe():
    if not _logged_in():
        return jsonify({"ok": False, "error": "auth"}), 401
    f = request.files.get("audio")
    if not f:
        return jsonify({"ok": False, "error": "no audio"}), 400
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / ("in" + (Path(f.filename or "a.bin").suffix or ".bin"))
        f.save(str(src))
        flac = Path(td) / "a.flac"
        if not _ffmpeg_to_flac(src, flac):
            return jsonify({"ok": False, "error": "ffmpeg 转码失败"}), 500
        try:
            text = _cloud_stt(flac)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)[:200]}), 500
    return jsonify({"ok": True, "text": text})


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


def _fast_intent(t: str, context: dict):
    """命中返回结构,否则 None。仅 PDF 阅读器页生效(那些 window.fn 只在阅读器有)。"""
    if (context or {}).get("page_type") != "pdf":
        return None
    s = t.replace(" ", "")
    # 翻页
    if _re.search(r"(下一?页|后一?页|往后|next)", s):
        return _act("changePage", [1], "好,下一页")
    if _re.search(r"(上一?页|前一?页|往前|previous|back)", s):
        return _act("changePage", [-1], "好,上一页")
    # 跳页:第N页 / 跳到N / 翻到N
    if _re.search(r"(第.*页|跳到|翻到|去第|到第|page)", s):
        n = _parse_num(s)
        if n:
            return _act("goToPage", [n], f"好,翻到第{n}页")
    # 缩放 / 适应
    if _re.search(r"(放大|拉大|zoom\s*in)", s):
        return _act("zoomChange", [0.15], "放大一点")
    if _re.search(r"(缩小|拉小|zoom\s*out)", s):
        return _act("zoomChange", [-0.15], "缩小一点")
    if _re.search(r"(适应|铺满|满屏|自适应|fit)", s):
        return _act("fitWidth", [], "好,适应宽度")
    # 排版模式
    if _re.search(r"双页", s):
        return _act("toggleSpread", [], "切到双页")
    if _re.search(r"(单页模式|切.*单页|单页阅读|^单页$)", s):   # 不匹配「这一页/一页纸」等(那些是提问/数量)
        return _act("toggleReadMode", [], "切到单页")
    if _re.search(r"(连续模式|连续滚动|切.*连续)", s):
        return _act("toggleReadMode", [], "切到连续")
    if _re.search(r"(全屏|fullscreen)", s):
        return _act("toggleFullscreen", [], "全屏")
    if _re.search(r"(去边|裁边|裁切|边距)", s):
        return _act("toggleCrop", [], "切换去边")
    if _re.search(r"(振假名|假名|注音|读音|ruby)", s):
        return _act("toggleRuby", [], "切换注音")
    if _re.search(r"(整页翻译|译页|翻译这页|翻译本页|全页翻译)", s):
        return _act("togglePageTranslate", [], "切换整页翻译")
    # 面板 / 导航
    if _re.search(r"(搜索|查找|找一?下|search)", s):
        return _act("openSearch", [], "打开搜索")
    if _re.search(r"(知识点|关联|侧栏)", s):
        return _act("toggleSidebar", [], "打开知识点")
    if _re.search(r"(生词本|单词本|生词列表)", s):
        return _act("toggleVocab", [], "打开生词本")
    if _re.search(r"(书架|书本列表|回到列表|选书)", s):
        return _act("goPdfList", [], "回到书架")
    return None


# ── agent(阶段 0:会话作答 + 结构化返回;阶段 1/2 接工具映射 + 危险动作确认)──
_AGENT_SYS = (
    "你是一个学习软件的语音助手,用户在用网页 PDF 阅读器 / 技能树 / 学习看板等页面。"
    "你的回答会被语音念出来:用简洁的中文口语,别用 markdown、符号、列表、长段落,控制在 2 句以内。"
    "当前是早期阶段:若用户的请求需要操作页面(翻页、查词、跳转、制卡等),先用一句话应承"
    "『好的,这个功能我马上接上』即可,暂不真正执行。其余问题正常简短作答。"
)


def _agent_brain(transcript: str, context: dict) -> dict:
    """阶段 0:用 Claude 简短口语作答,返回定型结构。"""
    ctx_txt = json.dumps(context, ensure_ascii=False)[:1200] if context else "(无)"
    prompt = f"{_AGENT_SYS}\n\n【当前页面上下文】{ctx_txt}\n【用户说】{transcript}\n\n你的回答(简短中文口语):"
    speak = ""
    try:
        sys.path.insert(0, str(CLAUDE_DIR / "scripts"))
        import ai_client
        speak = (ai_client.ask(prompt, claude_model="haiku", claude_effort="low") or "").strip()
    except Exception as e:
        speak = "我这边出了点问题:" + str(e)[:80]
    if not speak:
        speak = "我没太听清,能再说一遍吗?"
    return {"speak": speak, "client_actions": [], "server_results": [], "confirm": None}


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
        out = _agent_brain(transcript, context)
    return jsonify({"ok": True, **out})


@bp.route("/ping")
def voice_ping():
    return jsonify({"ok": True, "has_gcp_key": bool(_gcp_key()), "ffmpeg": _has_ffmpeg()})


def register_voice(app):
    app.register_blueprint(bp)
