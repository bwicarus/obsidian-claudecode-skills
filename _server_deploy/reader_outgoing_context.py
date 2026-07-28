#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阅读器出向上下文:绘图版本 + 焦点/选区事件(任务书 A5 缺口)。**纯增量、零 AI**。

两条硬规则(用户明确要求,也是本模块存在的理由):
- **旧图不许当成当前**:绘图要"停笔约 1 秒"才算稳定;未稳定时只报 pending,不给引用。
  新稳定版本产生后,旧版本立即失效——上游助手拿到的引用要么是最新稳定版,要么没有。
- **已取消的焦点不许当成当前**:取消是一个显式状态,不是"字段消失"。查询时明确返回
  `cancelled`,并带上被取消对象的摘要,避免上游把历史焦点误当现状。

实现刻意**不改任何既有写路径**:绘图版本从墨迹 sidecar 的内容摘要推导(读文件,不挂钩子),
因此老的 ink 保存链路一行都不用动,也不会被本模块拖累。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time

CONTRACT = "reader-outgoing-context/1"

DRAW_STABLE_S = 1.0      # 停笔多久算稳定(任务书:约 1 秒)
FOCUS_FRESH_S = 300.0    # 焦点新鲜窗口:超过就不再当"当前"
FOCUS_KINDS = ("text", "image", "card", "drawing", "region")


def _digest(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]


class DrawingRevisions:
    """按 (file,page) 跟踪绘图版本。`observe()` 传入当前墨迹内容,内部判定稳定与否。

    为什么不直接用"最后修改时间":连续落笔时 mtime 一直在变,任何一刻取到的都是半截图。
    这里用**内容摘要 + 静默计时**:摘要变了就重新计时,静默满 DRAW_STABLE_S 才升版本。
    """

    def __init__(self, stable_s: float = DRAW_STABLE_S):
        self.stable_s = stable_s
        self._st: dict[str, dict] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(file: str, page) -> str:
        return f"{file}#{page}"

    def observe(self, file: str, page, ink, *, now: float | None = None) -> dict:
        """喂入当前墨迹内容,返回该页绘图状态。无副作用地反复调用是安全的。"""
        now = time.time() if now is None else now
        k = self._key(file, page)
        dg = _digest(ink)
        with self._lock:
            st = self._st.get(k)
            if st is None or st["digest"] != dg:
                # 内容变了 → 重新计时;此刻**没有**可用的稳定版本(旧版本立即失效)
                st = {"digest": dg, "changed_at": now, "revision": None, "stable_at": None}
                self._st[k] = st
            elif st["revision"] is None and (now - st["changed_at"]) >= self.stable_s:
                # 静默够久 → 升版本。版本号由内容摘要派生:同一幅图不会来回换号
                st["revision"] = f"dr_{dg}"
                st["stable_at"] = now
            return self._snapshot(k, st, now)

    def _snapshot(self, k: str, st: dict, now: float) -> dict:
        file, _, page = k.rpartition("#")
        empty = st["digest"] == _digest({}) or st["digest"] == _digest([])
        return {
            "contract": CONTRACT,
            "file": file, "page": page,
            "stable": st["revision"] is not None,
            "drawingRevision": st["revision"],          # 未稳定时为 None,**不给引用**
            "pendingSince": None if st["revision"] else round(now - st["changed_at"], 3),
            "ref": ({"kind": "drawing", "file": file, "page": page,
                     "revision": st["revision"]} if st["revision"] else None),
            "empty": empty,
        }

    def current(self, file: str, page) -> dict | None:
        with self._lock:
            st = self._st.get(self._key(file, page))
            return None if st is None else self._snapshot(self._key(file, page), st, time.time())


class FocusState:
    """当前焦点对象。支持 text/image/card/drawing/region 与**显式取消**。

    取消不是删字段:`cancelled=True` 会保留在返回里,并附上被取消对象的摘要,
    这样上游助手能说"你刚才选的那个已经取消了",而不是沉默地继续用旧对象。
    """

    def __init__(self, fresh_s: float = FOCUS_FRESH_S):
        self.fresh_s = fresh_s
        self._cur: dict | None = None
        self._seq = 0
        self._lock = threading.Lock()

    def set(self, kind: str, ref: dict, *, task: str = "", now: float | None = None) -> dict:
        if kind not in FOCUS_KINDS:
            raise ValueError(f"focus.kind 必须是 {FOCUS_KINDS} 之一,收到 {kind!r}")
        if not isinstance(ref, dict) or not ref:
            raise ValueError("focus.ref 必填(定位这个对象所需的最少字段)")
        now = time.time() if now is None else now
        with self._lock:
            self._seq += 1
            self._cur = {"kind": kind, "ref": ref, "ts": now, "seq": self._seq,
                         "task": task, "cancelled": False}
            return dict(self._cur)

    def cancel(self, *, task: str = "", now: float | None = None) -> dict:
        now = time.time() if now is None else now
        with self._lock:
            self._seq += 1
            prev = self._cur
            self._cur = {"kind": (prev or {}).get("kind"), "ref": (prev or {}).get("ref"),
                         "ts": now, "seq": self._seq, "task": task, "cancelled": True}
            return dict(self._cur)

    def get(self, *, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        with self._lock:
            cur = dict(self._cur) if self._cur else None
        if cur is None:
            return {"contract": CONTRACT, "state": "never", "focus": None,
                    "note": "本会话从未上报过焦点(不是「没有焦点」)"}
        age = now - cur["ts"]
        if cur["cancelled"]:
            return {"contract": CONTRACT, "state": "cancelled", "focus": None,
                    "cancelledObject": {"kind": cur["kind"], "ref": cur["ref"]},
                    "ageSec": round(age, 3),
                    "note": "此前的焦点对象已被明确取消,不要再当作当前选中"}
        if age > self.fresh_s:
            return {"contract": CONTRACT, "state": "stale", "focus": None,
                    "lastObject": {"kind": cur["kind"], "ref": cur["ref"]},
                    "ageSec": round(age, 3),
                    "note": f"焦点上报已超过 {int(self.fresh_s)} 秒未更新,按未知处理"}
        return {"contract": CONTRACT, "state": "active", "focus":
                {"kind": cur["kind"], "ref": cur["ref"], "seq": cur["seq"], "task": cur["task"]},
                "ageSec": round(age, 3)}


class OutgoingJournal:
    """出向事件的**不可变追加日志**:Windows 用游标轮询消费。

    为什么是日志而不是"当前状态查询":跨机消费方需要知道**发生过什么**,
    而状态查询只能看到"现在是什么" —— 中间的 set→cancel 会被吃掉。
    追加日志 + 单调 seq 让消费方能断点续传,且天然幂等(同一 seq 只处理一次)。

    fail-closed:文件损坏时**抛错**,绝不静默返回空 —— 空列表会被消费方读成
    "没有新事件",从而漏掉真实事件。
    """

    KEEP = 2000          # 保留最近 N 条:日志是传递用的,不是归档
    MAX_WAIT_S = 25.0    # 长轮询上限,必须 < gunicorn/nginx 超时
    MAX_WAITERS = 2      # 并发挂起上限。⚠ 本项目出过"SSE 每条流独占一线程、8 条打死全站"
                         #   的事故;长轮询是同样的风险面,超过就立即返回而不是排队。

    def __init__(self, path_fn):
        self._path_fn = path_fn
        self._lock = threading.Lock()
        self._cv = threading.Condition()      # 与 _lock 分开:等待时**绝不持有**写锁
        self._waiters = 0

    def append(self, kind: str, payload: dict) -> dict:
        p = self._path_fn()
        with self._lock:
            lines = self._read_raw(p)
            seq = (lines[-1]["seq"] + 1) if lines else 1
            ev = {"v": 1, "seq": seq, "type": kind, "ts": int(time.time()),
                  "id": _digest([kind, seq, payload, time.time()]), **payload}
            lines.append(ev)
            if len(lines) > self.KEEP:
                lines = lines[-self.KEEP:]
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n",
                           encoding="utf-8")
            tmp.replace(p)          # 原子替换:读者永远看到完整一份
        with self._cv:
            self._cv.notify_all()   # 唤醒长轮询(在写锁之外唤醒,避免持锁唤醒后立刻争锁)
        return ev

    def _read_raw(self, p) -> list:
        if not p.exists():
            return []
        out = []
        for i, ln in enumerate(p.read_text("utf-8").splitlines()):
            if not ln.strip():
                continue
            try:
                out.append(json.loads(ln))
            except Exception as ex:
                raise ValueError(f"事件日志第 {i+1} 行损坏,拒绝返回部分结果:{ex}") from None
        return out

    def wait_since(self, cursor: int = 0, limit: int = 500, wait_s: float = 0.0) -> dict:
        """长轮询:有新事件立即返回,没有就挂起到超时。

        为什么值得做:实测服务端取一次只要 13-28ms,而客户端按 2-5s 轮询 →
        **99% 的延迟是在等下一次轮询**,不是在传。挂起后延迟降到毫秒级,请求数反而更少。
        护栏:并发挂起数超上限立即返回(不排队)、wait 夹紧到 MAX_WAIT_S、等待期间不持有写锁。
        """
        first = self.since(cursor, limit)
        if first["events"] or wait_s <= 0:
            return dict(first, waited=0.0)
        with self._cv:
            if self._waiters >= self.MAX_WAITERS:
                # 超并发:立即返回空 + 明确标记,客户端据此退回定时轮询,而不是排队占线程
                return dict(first, waited=0.0, waitDenied=True,
                            note="并发挂起已达上限,本次立即返回;请退回定时轮询")
            self._waiters += 1
        t0 = time.time()
        try:
            deadline = t0 + min(float(wait_s), self.MAX_WAIT_S)
            while time.time() < deadline:
                with self._cv:
                    self._cv.wait(timeout=max(0.05, min(1.0, deadline - time.time())))
                r = self.since(cursor, limit)
                if r["events"]:
                    return dict(r, waited=round(time.time() - t0, 3))
            return dict(self.since(cursor, limit), waited=round(time.time() - t0, 3))
        finally:
            with self._cv:
                self._waiters -= 1

    def since(self, cursor: int = 0, limit: int = 500) -> dict:
        p = self._path_fn()
        rows = self._read_raw(p)
        head = rows[0]["seq"] if rows else 0
        tail = rows[-1]["seq"] if rows else 0
        # 消费方的游标比日志起点还旧 → 中间事件已被保留策略丢掉,必须明说而不是假装连续
        gap = bool(rows and cursor and cursor + 1 < head)
        sel = [r for r in rows if r["seq"] > cursor][:limit]
        return {"contract": CONTRACT, "cursor": tail, "head": head,
                "events": sel, "gap": gap,
                "note": ("游标落后于保留窗口,中间事件已丢失;请按 head 重新对齐"
                         if gap else "")}


PAGE_TEXT_LIMIT = 4000   # 单页正文进 journal 的上限;注入侧还会再截一次


def build_page_context(pdf, rel: str, page, *, reason: str = "dwell") -> dict:
    """构造「翻页稳定」的整页上下文(纯确定性,零 AI)。

    正文源与 read.page / 快照**同一套**:PDF=剔噪字符层,EPUB=章节段落。
    取不到时不静默跳过,而是照发事件并说清 fallback_reason —— 否则上游分不清
    "这页没正文"和"事件丢了"。
    视觉资源只给**引用**(页图 URL + 墨迹版本),不塞字节:journal 要小,图由消费方按需取。
    """
    out = {"text": "", "text_available": False, "text_source": None,
           "fallback_reason": None, "truncated": False, "reason": reason}
    try:
        if str(rel).lower().endswith(".epub"):
            paras = pdf._epub_section_paragraphs(rel, int(page or 0)) or []
            txt = "\n\n".join(str(x) for x in paras if str(x).strip())
            src = "epub:章节段落"
        else:
            ap = pdf._safe_vault_path(rel)
            if not ap:
                out["fallback_reason"] = f"文件不可解析:{rel}"
                return out
            txt = pdf._page_text_clean(str(ap), rel, int(page), limit=PAGE_TEXT_LIMIT + 1) or ""
            src = "pdf:字符层(已剔噪)"
        if txt.strip():
            out.update(text=txt[:PAGE_TEXT_LIMIT], text_available=True, text_source=src,
                       truncated=len(txt) > PAGE_TEXT_LIMIT)
        else:
            out["fallback_reason"] = "该页无可用文字层(疑似扫描页);请改用页图/OCR"
    except Exception as ex:
        out["fallback_reason"] = f"提取异常:{type(ex).__name__}: {str(ex)[:120]}"
    # 视觉资源引用(本地综合):页图 URL + 是否有墨迹。消费方要看图时自己取。
    try:
        import urllib.parse as _up
        out["visual"] = {
            "page_image": f"/pdf/api/page-image?file={_up.quote(str(rel))}&page={page}",
            "has_ink": bool((pdf._ink_load(rel) or {}).get("pages", {}).get(str(page))),
        }
    except Exception:
        out["visual"] = {"page_image": None, "has_ink": False}
    return out


def register_outgoing_context(bp, *, pdf, jsonify, request, session,
                              drawings=None, focus=None):
    """挂三条只读/上报路由。不触碰既有 endpoint,不写任何旧数据文件。"""
    dr = drawings or DrawingRevisions()
    fs = focus or FocusState()
    jr = OutgoingJournal(lambda: pdf._reader_sidecar_path("reader-outgoing-journal.jsonl"))

    def _ink_of(rel: str):
        try:
            return (pdf._ink_load(rel) or {}).get("pages", {})
        except Exception:
            return {}

    _seen_rev: set = set()

    def _log_if_newly_stable(st):
        rv = st.get("drawingRevision")
        if st.get("stable") and rv and rv not in _seen_rev:
            _seen_rev.add(rv)
            jr.append("drawing", {"state": "stable", "file": st.get("file"),
                                  "page": st.get("page"), "drawingRevision": rv,
                                  "ref": st.get("ref")})
        return st

    def drawing_state():
        if not session.get("user_id"):
            return jsonify({"ok": False, "error": "未登录"}), 401
        rel = str(request.args.get("file") or "").strip()
        if not pdf._safe_vault_path(rel):
            return jsonify({"ok": False, "error": "file 不是 vault 内的有效文件"}), 404
        page = request.args.get("page")
        pages = _ink_of(rel)
        st = _log_if_newly_stable(dr.observe(rel, page, pages.get(str(page)) if page else pages))
        return jsonify({"ok": True, **st})

    def focus_report():
        if not session.get("user_id"):
            return jsonify({"ok": False, "error": "未登录"}), 401
        b = request.get_json(silent=True) or {}
        task = str(b.get("task") or "")[:80]
        try:
            if b.get("cancel"):
                r = fs.cancel(task=task)
                jr.append("focus", {"action": "cancel", "taskId": task,
                                    "cancelledObject": {"kind": r.get("kind"), "ref": r.get("ref")},
                                    "seq_focus": r["seq"]})
                return jsonify({"ok": True, **r})
            r = fs.set(str(b.get("kind") or ""), b.get("ref") or {}, task=task)
            jr.append("focus", {"action": "set", "kind": r["kind"], "ref": r["ref"],
                                "taskId": task, "seq_focus": r["seq"]})
            return jsonify({"ok": True, **r})
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e), "retryable": False}), 400

    def outgoing_state():
        if not session.get("user_id"):
            return jsonify({"ok": False, "error": "未登录"}), 401
        out = {"contract": CONTRACT, "ok": True, "focus": fs.get()}
        rel = str(request.args.get("file") or "").strip()
        if rel and pdf._safe_vault_path(rel):
            page = request.args.get("page")
            pages = _ink_of(rel)
            out["drawing"] = _log_if_newly_stable(dr.observe(rel, page, pages.get(str(page)) if page else pages))
        return jsonify(out)

    def journal():
        """Windows 的生产消费入口:游标轮询不可变事件日志。
        fail-closed:未登录 401;日志损坏 500 并说明,**绝不静默返回空**。"""
        if not session.get("user_id"):
            return jsonify({"ok": False, "error": "未登录"}), 401
        try:
            since = max(0, int(request.args.get("since") or 0))
            limit = min(500, max(1, int(request.args.get("limit") or 200)))
            wait = max(0.0, float(request.args.get("wait") or 0))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "since/limit/wait 必须是数字"}), 400
        try:
            return jsonify({"ok": True, **jr.wait_since(since, limit, wait)})
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e), "retryable": False}), 500

    bp.add_url_rule("/api/outgoing/journal", "pdf_api_outgoing_journal",
                    journal, methods=["GET"])
    bp.add_url_rule("/api/outgoing/drawing", "pdf_api_outgoing_drawing",
                    drawing_state, methods=["GET"])
    bp.add_url_rule("/api/outgoing/focus", "pdf_api_outgoing_focus",
                    focus_report, methods=["POST"])
    bp.add_url_rule("/api/outgoing/state", "pdf_api_outgoing_state",
                    outgoing_state, methods=["GET"])
    return {"drawings": dr, "focus": fs, "journal": jr}
