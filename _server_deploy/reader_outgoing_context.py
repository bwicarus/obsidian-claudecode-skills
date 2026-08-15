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
INK_FRESH_S = 120.0      # 笔迹"近期"窗口(任务书首版建议 120 秒);超过即 stale
EPUB_VIEWPORT_PAD = 6    # EPUB 以视口为中心向上下各扩展几段(任务书一:不按章节整段灌入)
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

    def __init__(self, stable_s: float = DRAW_STABLE_S, fresh_s: float = INK_FRESH_S):
        self.stable_s = stable_s
        self.fresh_s = fresh_s
        self._st: dict[str, dict] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(file: str, page) -> str:
        return f"{file}#{page}"

    def observe(self, file: str, page, ink, *, now: float | None = None) -> dict:
        """喂入当前墨迹内容,返回该页绘图状态。无副作用地反复调用是安全的。"""
        now = time.time() if now is None else now
        k = self._key(file, page)
        # Sidecar 中没有该页时调用方会传 None。None、空对象与空数组在
        # 产品语义上都是「没有墨迹」，必须折叠为同一个确定性空状态；否则
        # None 会被当成一幅新图，短暂误报 recent/inProgress，随后甚至升出
        # 一个不存在的稳定绘图版本。
        empty = ink is None or ink == {} or ink == []
        canonical_ink = {} if empty else ink
        dg = _digest(canonical_ink)
        with self._lock:
            st = self._st.get(k)
            if st is None or st["digest"] != dg:
                # 内容变了 → 重新计时;此刻**没有**可用的稳定版本(旧版本立即失效)
                st = {
                    "digest": dg,
                    "changed_at": now,
                    "revision": None,
                    "stable_at": None,
                    "file": file,
                    "page": page,
                    "empty": empty,
                }
                self._st[k] = st
            elif (
                not st["empty"]
                and st["revision"] is None
                and (now - st["changed_at"]) >= self.stable_s
            ):
                # 静默够久 → 升版本。版本号由内容摘要派生:同一幅图不会来回换号
                st["revision"] = f"dr_{dg}"
                st["stable_at"] = now
            # `_key()` intentionally treats numeric 7 and string "7" as the
            # same logical page. The emitted reference must nevertheless use
            # the type supplied by the current outer event/page_context, not
            # whichever representation happened to arrive first.
            st["file"] = file
            st["page"] = page
            return self._snapshot(st, now)

    def _snapshot(self, st: dict, now: float) -> dict:
        file, page, empty = st["file"], st["page"], bool(st["empty"])
        # 三态(任务书二):正文问答默认只看正文,靠这个字段决定要不要去读综合图。
        #   none   = 无笔迹,永远不用读图
        #   recent = 刚画过(或正在画),问题涉及圈画/算式/箭头时应直接读最新图,
        #            不要求用户再说一遍"我刚画了"
        #   stale  = 有旧笔迹,除非用户明确提到圈画/标注,否则不读
        # lastEditedAt 取"最后一次内容变化"而非升版本时刻:正在画时也要能报新鲜。
        # ⚠ 字段名是 freshness 不是 state:journal 的 drawing 事件里 `state` 已经表示
        # pending/stable(稳定性),同名会让消费方看到两套值域。
        last_edited = st["changed_at"]
        if empty:
            freshness = "none"
        elif (now - last_edited) <= self.fresh_s:
            freshness = "recent"
        else:
            freshness = "stale"
        return {
            "contract": CONTRACT,
            "file": file, "page": page,
            "freshness": freshness,
            "lastEditedAt": None if empty else round(last_edited, 3),
            "freshWindowS": self.fresh_s,
            "inProgress": st["revision"] is None and not empty,   # 落笔中,尚无稳定版本
            "stable": st["revision"] is not None,
            "drawingRevision": st["revision"],          # 未稳定时为 None,**不给引用**
            "pendingSince": (
                round(now - st["changed_at"], 3)
                if st["revision"] is None and not empty
                else None
            ),
            "ref": ({"kind": "drawing", "file": file, "page": page,
                     "revision": st["revision"]} if st["revision"] else None),
            "empty": empty,
        }

    def current(self, file: str, page) -> dict | None:
        with self._lock:
            st = self._st.get(self._key(file, page))
            return None if st is None else self._snapshot(st, time.time())


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


# 绘图状态的进程内共享单例。build_page_context 与 /api/drawing-state 路由必须看同一份:
# 稳定期计时和 revision 都是有状态的,两份实例会互相看不见对方的观察。
_DRAWINGS = DrawingRevisions()


# ── 正文锚定嵌入内容(任务书四)────────────────────────────────────────────
# 高亮、卡片、便签等都绑定到正文中的具体范围,复用同一套锚定/排序/插入机制。
# 铁律:高亮**不复制到页尾列表**,而是在原文范围内用边界标记包住 —— 正文只出现一次。
MARK_L, MARK_R = "⟦", "⟧"      # ⟦ ⟧


def _escape_marks(s: str) -> str:
    """正文原本含保留符号时必须转义,否则消费方会把它误当成我们的边界。

    规则:反斜杠自身先转义,再给 ⟦ ⟧ 各加一个反斜杠。消费方见 `\\⟦` 一律当普通字符。
    """
    return (str(s).replace("\\", "\\\\")
            .replace(MARK_L, "\\" + MARK_L)
            .replace(MARK_R, "\\" + MARK_R))


class MarkEscapeError(ValueError):
    """转义序列不合法。消费端必须 fail closed,不许猜。"""


def unescape_marks(s: str) -> str:
    """`_escape_marks` 的逆运算 —— **消费端硬合同**(Codex 07:37 冻结)。

    规则:**单次从左到右扫描**,只反转 `\\\\`→`\\`、`\\⟦`→`⟦`、`\\⟧`→`⟧`;
    未知反斜杠序列(如 `\\n`)**原样保留两个字符**;末尾悬空的 `\\` **fail closed**。

    ⚠ 为什么必须单次扫描 —— 理由不是"链式 replace 会二次反转义"(实测:对合法的
    `_escape_marks` 输出,链式 replace 恰好 round-trip 等价,因为转义时先处理反斜杠
    再处理标记)。真正的理由是:

        `replace` 无法识别坏输入并 fail closed。

    末尾孤立的反斜杠不可能由合法编码产生,`replace` 却只会静默把它留在原地,产出一个
    看似正常的字符串;单次扫描才能停下来报错。用例两侧都固化在
    tests/test_mark_escaping.py,C# 侧照抄即可对齐。
    """
    src = str(s or "")
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        if i + 1 >= n:
            raise MarkEscapeError("末尾悬空的反斜杠:无法判定是转义还是正文")
        nxt = src[i + 1]
        if nxt in ("\\", MARK_L, MARK_R):
            out.append(nxt)          # 已知转义:吃掉反斜杠,只留被转义的那个字符
        else:
            out.append(c)            # 未知序列:两个字符原样保留,不猜语义
            out.append(nxt)
        i += 2
    return "".join(out)


def _attr(s: str, limit: int = 120) -> str:
    """标记属性值:压成单行、去引号,避免把边界标记本身弄坏。"""
    v = " ".join(str(s or "").split())[:limit]
    return v.replace('"', "'").replace(MARK_L, "").replace(MARK_R, "")


def annotate_page_text(text: str, highlights=(), blocks=()) -> tuple[str, list]:
    """把高亮包进正文原位,块状附属内容按锚定顺序插入。

    返回 `(annotated, unanchored)`。**定位不到的高亮不塞进正文** —— 塞了正文就会出现
    两次,违背"正文只出现一次";改为回报 unanchored,由上游决定要不要单独提。

    重叠的高亮只保留先命中的那条:边界标记不能交叉,否则消费方无法解析。
    """
    src = str(text or "")
    if not src:
        return "", [dict(h, _reason="empty_text") for h in (highlights or [])]

    esc = _escape_marks(src)
    spans: list[tuple[int, int, dict]] = []
    unanchored: list = []
    taken: list[tuple[int, int]] = []

    for h in (highlights or []):
        needle = _escape_marks(" ".join(str(h.get("text") or "").split()))
        if not needle:
            unanchored.append(dict(h, _reason="no_text"))
            continue
        # 正文换行与高亮里的空白往往不一致,先按原样找,找不到再退回压缩空白后找。
        pos = esc.find(needle)
        if pos < 0:
            flat = " ".join(esc.split())
            p2 = flat.find(needle)
            if p2 < 0:
                unanchored.append(dict(h, _reason="not_found_in_page_text"))
                continue
            # 压缩空白后能找到,说明是换行差异:此时不做近似定位(会错位),照实回报。
            unanchored.append(dict(h, _reason="whitespace_mismatch"))
            continue
        end = pos + len(needle)
        if any(pos < b and a < end for a, b in taken):
            unanchored.append(dict(h, _reason="overlaps_earlier_highlight"))
            continue
        taken.append((pos, end))
        spans.append((pos, end, h))

    # 从后往前插入,否则先插入的标记会让后面的偏移全部失效。
    out = esc
    for pos, end, h in sorted(spans, key=lambda x: x[0], reverse=True):
        attrs = ""
        if h.get("color"):
            attrs += f' color="{_attr(h["color"], 24)}"'
        if h.get("note"):
            attrs += f' note="{_attr(h["note"])}"'
        out = (out[:pos] + f"{MARK_L}HIGHLIGHT{attrs}{MARK_R}" + out[pos:end]
               + f"{MARK_L}/HIGHLIGHT{MARK_R}" + out[end:])

    # 块状附属内容:卡片/便签没有正文字符锚,只有页面坐标锚,因此紧随正文之后按序给出,
    # 并带上它绑定的是什么。它们是补充绑定元素的内容,不是对整段的解释。
    for b in (blocks or []):
        kind = _attr(b.get("kind") or "note", 32)
        body = _escape_marks(b.get("text") or "")
        head = f'{MARK_L}CARD_START type="{kind}"'
        if b.get("label"):
            head += f' label="{_attr(b["label"])}"'
        out += f"\n\n{head}{MARK_R}{body}{MARK_L}CARD_END{MARK_R}"

    return out, unanchored


def _viewport_center(viewport, total: int):
    """把客户端上报的视口折算成"以第几段为中心"。拿不到就返回 None(退回整章)。

    接受两种上报,哪种客户端方便就用哪种:
      {"para": 12}      —— 可见区中心的段序号
      {"ratio": 0.35}   —— 可见区中心在本章的相对位置(0~1),epub.js 的百分比进度直接可用
    不接受猜测:没有可用字段时不瞎估位置,错位的"视口"比整章更有害。
    """
    if not isinstance(viewport, dict) or total <= 0:
        return None
    p = viewport.get("para", viewport.get("index"))
    if isinstance(p, bool) is False and isinstance(p, int) and 0 <= p < total:
        return p
    r = viewport.get("ratio", viewport.get("progress"))
    if isinstance(r, bool) is False and isinstance(r, (int, float)) and 0.0 <= float(r) <= 1.0:
        return min(total - 1, max(0, int(round(float(r) * (total - 1)))))
    return None


def _page_embeds(pdf, rel: str, page) -> tuple[list, list]:
    """取该页的高亮与块状附属内容。任一 sidecar 缺失/损坏都只让那一类为空,不影响正文。

    PDF 高亮按 `page` 过滤;EPUB 高亮的锚是 `anchor.section`(page 参数在 EPUB 语义下即 section)。
    便签只有页面坐标锚、没有正文字符锚,所以归到块状内容;其中 card/html/video 便签
    是任务书说的"绑定在页面元素上的工具卡",纯文本便签同样走这条管线。
    """
    is_epub = str(rel).lower().endswith(".epub")
    pg = str(page)
    hls: list = []
    try:
        if is_epub:
            for h in (pdf._epub_hl_load(rel) or []):
                a = h.get("anchor") if isinstance(h.get("anchor"), dict) else {}
                if str(a.get("section", "")) == pg:
                    hls.append(h)
        else:
            for h in ((pdf._hl_load(rel) or {}).get("highlights") or []):
                if str(h.get("page", "")) == pg:
                    hls.append(h)
    except Exception:
        hls = []

    blocks: list = []
    try:
        for n in (pdf._notes_load(rel) or []):
            a = n.get("anchor") if isinstance(n.get("anchor"), dict) else {}
            # anchor 的页字段在两种阅读器下命名不同;取不到页就不猜,宁可漏也不错挂到别页。
            npg = a.get("page", a.get("section", a.get("p")))
            if npg is None or str(npg) != pg:
                continue
            if isinstance(n.get("card"), dict):
                cards = n["card"].get("cards") or []
                text = "\n".join(
                    " / ".join(str(c.get(k) or "") for k in ("front", "back", "cloze")
                               if c.get(k)) for c in cards if isinstance(c, dict))
                blocks.append({"kind": "anki", "text": text, "label": n.get("id")})
            elif isinstance(n.get("html"), dict):
                blocks.append({"kind": "card",
                               "text": str(n["html"].get("content") or ""),
                               "label": n["html"].get("label") or n.get("id")})
            elif isinstance(n.get("video"), dict):
                blocks.append({"kind": "video",
                               "text": str(n["video"].get("title") or ""),
                               "label": n.get("id")})
            elif str(n.get("text") or "").strip():
                blocks.append({"kind": "note", "text": n["text"], "label": n.get("id")})
    except Exception:
        blocks = []
    return hls, blocks


def build_page_context(pdf, rel: str, page, *, reason: str = "dwell",
                       viewport: dict | None = None) -> dict:
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
            paras = [str(x) for x in paras if str(x).strip()]
            # 任务书一:**不按章节整段灌入**,以当前阅读视口为中心向上下扩展。
            # viewport 由客户端上报(段序号或 0~1 相对位置);拿不到就只能给整章,
            # 但要在 text_source 里说清 —— 否则上游会把"整章"误当成"用户正在看的一屏"。
            center = _viewport_center(viewport, len(paras))
            if center is None:
                src = "epub:整章段落(无视口上报)"
            else:
                lo = max(0, center - EPUB_VIEWPORT_PAD)
                hi = min(len(paras), center + EPUB_VIEWPORT_PAD + 1)
                out["viewport"] = {"center": center, "from": lo, "to": hi,
                                   "total": len(paras), "pad": EPUB_VIEWPORT_PAD}
                paras = paras[lo:hi]
                src = f"epub:视口段落[{lo},{hi})/{out['viewport']['total']}"
            txt = "\n\n".join(paras)
        else:
            ap = pdf._safe_vault_path(rel)
            if not ap:
                out["fallback_reason"] = f"文件不可解析:{rel}"
                return out
            txt = pdf._page_text_clean(str(ap), rel, int(page), limit=PAGE_TEXT_LIMIT + 1) or ""
            src = "pdf:字符层(已剔噪)"
        if txt.strip():
            body = txt[:PAGE_TEXT_LIMIT]
            # 正文锚定嵌入内容:高亮包回原位,卡片/便签紧随其后(任务书四)。
            # 失败不能影响正文本身 —— 正文是主线,标注是增强。
            embeds = {"highlights": 0, "blocks": 0, "unanchored": []}
            try:
                hls, blocks = _page_embeds(pdf, rel, page)
                body, unanchored = annotate_page_text(body, hls, blocks)
                embeds = {"highlights": len(hls) - len(unanchored),
                          "blocks": len(blocks), "unanchored": unanchored}
            except Exception as ex:
                embeds["error"] = f"{type(ex).__name__}: {str(ex)[:120]}"
            out.update(text=body, text_available=True, text_source=src,
                       truncated=len(txt) > PAGE_TEXT_LIMIT, embeds=embeds)
        else:
            out["fallback_reason"] = "该页无可用文字层(疑似扫描页);请改用页图/OCR"
    except Exception as ex:
        out["fallback_reason"] = f"提取异常:{type(ex).__name__}: {str(ex)[:120]}"
    # 视觉资源引用(本地综合):页图 URL + 绘图三态。消费方要看图时自己取。
    # 绘图**不默认进视觉上下文**(任务书二):这里只给状态和引用,由上游按问题内容决定读不读。
    try:
        import urllib.parse as _up
        ink = (pdf._ink_load(rel) or {}).get("pages", {}).get(str(page))
        # 走共享单例,与 /api/drawing-state 路由同一份状态:否则两边各判各的稳定期,
        # 上游会看到同一页忽 recent 忽 stale。
        drawing = _DRAWINGS.observe(str(rel), page, ink)
        out["visual"] = {
            "page_image": f"/pdf/api/page-image?file={_up.quote(str(rel))}&page={page}",
            "has_ink": bool(ink),                 # 保留:旧消费方仍在读这个字段
            "drawing": drawing,                   # freshness: none/recent/stale + lastEditedAt + ref
        }
    except Exception:
        out["visual"] = {"page_image": None, "has_ink": False, "drawing": None}
    # 这页在讲什么概念。带上它是因为这是助手最常需要再问一轮的东西 ——
    # 正文说的是"这页写了什么字",知识点说的是"这页在讲什么"。
    # 拿不到时照样带上字段并说明原因:少一个字段,上游分不清"这本书没建过图"
    # 和"这段代码没跑",于是会把后者当前者讲出来。
    try:
        import kg_page_index as _KG
        out["knowledge"] = _KG.knowledge_for_page(rel, page)
    except Exception as ex:
        out["knowledge"] = {
            "available": False,
            "reason": f"知识图谱不可用:{type(ex).__name__}: {str(ex)[:80]}",
            "book": None, "section": None, "concepts": [],
        }
    return out


def register_outgoing_context(bp, *, pdf, jsonify, request, session,
                              drawings=None, focus=None):
    """挂三条只读/上报路由。不触碰既有 endpoint,不写任何旧数据文件。"""
    dr = drawings or _DRAWINGS
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
