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
    out = _agent_brain(transcript, context)
    return jsonify({"ok": True, **out})


@bp.route("/ping")
def voice_ping():
    return jsonify({"ok": True, "has_gcp_key": bool(_gcp_key()), "ffmpeg": _has_ffmpeg()})


def register_voice(app):
    app.register_blueprint(bp)
