# -*- coding: utf-8 -*-
"""error_reports.py — 「没按预期工作」的一键报告（AI 自动化环境·支柱①）。

用户在任何入口说"这个不对/出错了/记录一下"，系统把**当时和之前的环境**打包成
一份结构化报告：用户描述 + 当前书页/选区 + 最近的工具调用（voice-log 尾部）+
最近几轮对话。报告落 Pi（权威），随即广播 SSE（kind=error-report），Windows
镜像守护秒级拉到 `state/pi-mirror/error-reports/` —— 调试侧的 AI 直接读文件夹，
省掉"报错后猜环境"的整段时间。

三条设计约束：

  · **采集必须是确定性的**。收集器只读现成日志，不调 AI 不做总结 ——
    总结是调试侧 AI 的事，采集侧多做一步就多一个采集本身出错的可能。

  · **采集失败也要出报告**。某个日志源读不动，就在报告里写明
    "voiceLog: unavailable(<原因>)"，其它源照常 —— 报告系统自己静默失败
    是最讽刺的事故（《silent-failure-lessons》第五条）。

  · **只截尾部、只看最近**。整份日志上传既是隐私面也是噪声；调试要的是
    "错误发生前后"，尾部 N 条 + 时间窗足够。
"""
from __future__ import annotations

import glob
import json
import os
import re
import time
import uuid
from pathlib import Path

CONTRACT = "reader-error-report/1"
VOICE_LOG_TAIL = 40          # voice-log 取最近 N 条(含工具调用与问答)
CONVO_TAIL = 8               # 对话取最近 N 条
RECENT_WINDOW_SECONDS = 30 * 60   # 只收这个时间窗内的日志


def _reports_dir(claude_dir: Path) -> Path:
    return claude_dir / "state" / "error-reports"


def _tail_voice_log(claude_dir: Path, book: str | None) -> dict:
    """最近的 voice-log 事件(工具调用/问答)。book 给了就按书过滤。"""
    try:
        files = sorted(glob.glob(str(claude_dir / "state" / "voice-log" / "*.jsonl")))
        if not files:
            return {"status": "absent", "events": []}
        cutoff = int(time.time()) - RECENT_WINDOW_SECONDS
        events: list[dict] = []
        for path in files[-2:]:                     # 最多跨昨天+今天两个文件
            for line in open(path, encoding="utf-8", errors="replace"):
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("kind") == "rtcstats":     # 网络统计与错误无关,纯噪声
                    continue
                if int(d.get("ts") or 0) < cutoff:
                    continue
                if book and d.get("book") and d.get("book") != book:
                    continue
                events.append(d)
        return {"status": "ok", "events": events[-VOICE_LOG_TAIL:]}
    except Exception as error:
        return {"status": f"unavailable({type(error).__name__})", "events": []}


def _tail_convo(claude_dir: Path, uid) -> dict:
    """当前用户最近几轮助手对话。"""
    try:
        path = claude_dir / "state" / "assistant-convo" / f"{uid}.json"
        if not path.is_file():
            return {"status": "absent", "messages": []}
        data = json.loads(path.read_text(encoding="utf-8"))
        tail = [
            {"role": m.get("role"), "content": (m.get("content") or "")[:600],
             "ts": m.get("ts"), "page": m.get("page"), "file_rel": m.get("file_rel")}
            for m in data[-CONVO_TAIL:] if isinstance(m, dict)
        ]
        return {"status": "ok", "messages": tail}
    except Exception as error:
        return {"status": f"unavailable({type(error).__name__})", "messages": []}


def _server_stamp(claude_dir: Path) -> dict:
    try:
        stamp = (claude_dir.parent / "webapp" / "reader-git-stamp.txt")
        if stamp.is_file():
            return {"readerStamp": stamp.read_text(encoding="utf-8").strip()[:80]}
    except Exception:
        pass
    return {}


def collect_report(claude_dir: Path, *, what: str, ctx: dict, uid) -> dict:
    """打包一份报告。任何一个源失败都不拦整体 —— 状态写进对应字段。"""
    book = str(ctx.get("file_rel") or "") or None
    report = {
        "contract": CONTRACT,
        "id": time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6],
        "createdAtEpochSeconds": int(time.time()),
        "what": str(what or "")[:2000],
        "context": {
            "file": book,
            "page": ctx.get("page"),
            "selection": (str(ctx.get("selection") or "")[:400] or None),
            "via": ctx.get("via") or None,
        },
        "voiceLog": _tail_voice_log(claude_dir, book),
        "conversation": _tail_convo(claude_dir, uid),
        "server": _server_stamp(claude_dir),
    }
    return report


def save_report(claude_dir: Path, report: dict) -> Path:
    directory = _reports_dir(claude_dir)
    directory.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^0-9A-Za-z_-]", "", str(report.get("id") or "report"))
    path = directory / f"{safe_id}.json"
    tmp = directory / f"{safe_id}.json.tmp-{os.getpid()}"
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, path)
    return path


def list_reports(claude_dir: Path, since_epoch: int = 0) -> list[dict]:
    directory = _reports_dir(claude_dir)
    out: list[dict] = []
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.json")):
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            # 列表接口不因一份坏文件整个失败,但要出声
            out.append({"id": path.stem, "error": "unreadable"})
            continue
        if int(d.get("createdAtEpochSeconds") or 0) < since_epoch:
            continue
        out.append({"id": d.get("id"), "what": (d.get("what") or "")[:120],
                    "createdAtEpochSeconds": d.get("createdAtEpochSeconds"),
                    "file": (d.get("context") or {}).get("file")})
    return out


def read_report(claude_dir: Path, report_id: str) -> dict | None:
    safe_id = re.sub(r"[^0-9A-Za-z_-]", "", str(report_id or ""))
    path = _reports_dir(claude_dir) / f"{safe_id}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def register_error_reports(bp, *, claude_dir, publish, jsonify, request, session):
    """挂三条路由。publish=reader_events.publish(报告落盘后广播,镜像守护秒级拉走)。"""

    def submit():
        if not session.get("user_id"):
            return jsonify({"ok": False, "error": "未登录"}), 401
        body = request.get_json(silent=True) or {}
        what = str(body.get("what") or "").strip()
        if not what:
            return jsonify({"ok": False, "error": "缺 what(哪里没按预期?一句话)"}), 400
        ctx = dict(body.get("ctx") or {})
        report = collect_report(claude_dir, what=what, ctx=ctx,
                                uid=session["user_id"])
        save_report(claude_dir, report)
        try:
            publish("error-report", (ctx.get("file_rel") or ""), report["id"])
        except Exception:
            pass   # 广播失败不影响报告本身;镜像守护的周期追赶会兜住
        return jsonify({"ok": True, "id": report["id"]})

    def listing():
        if not session.get("user_id"):
            return jsonify({"ok": False, "error": "未登录"}), 401
        try:
            since = int(request.args.get("since") or 0)
        except (TypeError, ValueError):
            since = 0
        return jsonify({"ok": True, "contract": CONTRACT,
                        "reports": list_reports(claude_dir, since)})

    def single(rid):
        if not session.get("user_id"):
            return jsonify({"ok": False, "error": "未登录"}), 401
        report = read_report(claude_dir, rid)
        if report is None:
            return jsonify({"ok": False, "error": "没有这份报告"}), 404
        return jsonify({"ok": True, "report": report})

    bp.add_url_rule("/api/error-report", "pdf_api_error_report",
                    submit, methods=["POST"])
    bp.add_url_rule("/api/error-reports", "pdf_api_error_reports",
                    listing, methods=["GET"])
    bp.add_url_rule("/api/error-report/<rid>", "pdf_api_error_report_one",
                    single, methods=["GET"])
