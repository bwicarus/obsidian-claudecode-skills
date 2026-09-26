"""jev_judge.py — 独立的 jev 实时判断模块（2026-09-26）。

用户 2026-09-26：「jev 实时判断要做成一个独立的模块，它可以在任意时间工作，而不是只有在语音开启时才可以工作。」

所以这里**不依赖语音进程的任何状态**：输入是一段状态文本 + 一组选择题，输出是每道题的
选项与概率。谁都可以调 —— 环境旁听（ambient_jev.py）是第一个使用者，以后手表、眼镜、
打字输入都走同一个入口，只是各自出题不同。

⚠ 端点、模型号、密钥文件与 ``computer-voice-desktop/voice_jev_context.py::predict`` 相同。
那边是语音进程内 3 秒硬超时的注入路径，保留它自己的一份；改端点或模型号时两处一起改。
"""
from __future__ import annotations

import json
import math
import os
import re
import urllib.request
from pathlib import Path

JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-1.13.0"
DEFAULT_TIMEOUT_SECONDS = 12
MAX_STATE_CHARS = 24000


class JevUnavailable(RuntimeError):
    """凭据缺失、网络 / HTTP 失败、响应不安全 —— 调用方应当出声并降级，而不是悄悄当成「没事」。"""


def key_file() -> Path:
    configured = os.environ.get("JEV_KEY_FILE")
    return Path(configured) if configured else Path.home() / "Desktop" / "jev api.txt"


def _read_key() -> str:
    try:
        text = key_file().read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise JevUnavailable("credential_unavailable") from exc
    key = next((s.strip() for s in text.splitlines() if re.fullmatch(r"[A-Za-z0-9_.+/=-]{40,}", s.strip())), None)
    if not key:
        raise JevUnavailable("credential_unavailable")
    return key


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def top_choice(answer: dict, criteria: dict) -> tuple[str, float]:
    probabilities = (answer or {}).get("probabilities") or {}
    if not probabilities or any(k not in criteria or isinstance(v, bool) or not isinstance(v, (int, float))
                                or not math.isfinite(v) or not 0 <= v <= 1 for k, v in probabilities.items()):
        raise ValueError("invalid_probabilities")
    choice, probability = max(probabilities.items(), key=lambda item: item[1])
    return choice, float(probability)


def validate_questions(questions: dict) -> None:
    if not isinstance(questions, dict) or not questions:
        raise ValueError("questions_required")
    for name, question in questions.items():
        criteria = (question or {}).get("criteria")
        if question.get("type") != "choice" or not isinstance(criteria, dict) or len(criteria) < 2:
            raise ValueError(f"bad_question:{name}")


def predict(state: str, questions: dict, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, dict]:
    """返回 {题名: {choice, probability, probabilities}}。凭据只用于这个固定端点，不进日志、不进返回值。"""
    validate_questions(questions)
    if not state or len(state) > MAX_STATE_CHARS:
        raise ValueError("state_size")
    key = _read_key()
    payload = {"model": JEV_MODEL, "state": state, "questions": questions}
    req = urllib.request.Request(JEV_ENDPOINT, data=json.dumps(payload, ensure_ascii=False).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as response:
            raw = response.read().decode()
    except Exception as exc:  # noqa: BLE001 — 网络/HTTP 错误统一交给调用方出声
        raise JevUnavailable(type(exc).__name__) from exc
    if key in raw:
        raise JevUnavailable("unsafe_response")
    answers = json.loads(raw)["answers"]
    out = {}
    for name, question in questions.items():
        choice, probability = top_choice(answers.get(name), question["criteria"])
        out[name] = {"choice": choice, "probability": round(probability, 4),
                     "probabilities": answers[name]["probabilities"]}
    return out
