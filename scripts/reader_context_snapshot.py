#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成「当前阅读/助手上下文」Markdown 快照 + 少量图片资产（供 Windows 本地 Codex 每轮开局读）。

定位（用户拍板）：
  这份快照是**优先读取的第一上下文**，不是唯一事实源，**不替代 MCP**。
  - 先读快照：在读哪本 / 哪页 / 助手在聊什么 / 有什么任务产物 / 本书标注概况。
  - 按需走 MCP：精确页正文、下一页、**实时选区**、翻页/高亮/编辑等**任何操作**，
    以及"用户是否已翻页"的校验。原因是架构性的——"用户此刻看到的正文"只存在于
    前端 ctx.visible_text，服务端取不到（assistant.py 是唯一消费点）。

只读：仅读取 state/ 下现有 sidecar，不调阅读器 API、不写任何既有文件。
输出：<out>/context.md + <out>/assets/*（资产常驻 < 5MB，见 ASSET_BUDGET）。
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import shutil
import time
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
VAULT = Path(os.environ.get("OBSIDIAN_VAULT", "/home/bwicarus/obsidian"))
ST = ROOT / "state"

ASSET_BUDGET = 5 * 1024 * 1024      # 资产目录常驻上限（用户要求 <5MB）
CONVO_TURNS = 30                    # 助手对话保留轮数（尽量长保留，不激进压缩）
CONVO_CHARS = 1200                  # 单轮正文上限
NEAR_PAGES = 2                      # 标注摘要取当前页 ±N


def jload(p, default=None):
    try:
        return json.loads(Path(p).read_text("utf-8"))
    except Exception:
        return default


def _sidecar_account_root():
    """账户分区的**实时** sidecar 目录(webapp 真正在写的那份)。

    ⚠ 这是本次「当前书误判」的第二个根因,比页码规则更隐蔽:webapp 早已把 reader sidecar
    迁到 `reader-sidecars/by-user/<uid>/`,而 `state/` 下那一份是账户认领时**一次性复制、
    此后再不更新**的 legacy 副本(copy-only,原文件永不改写)。快照一直读 legacy,于是看到的是
    冻结在认领时刻的旧世界:legacy 里最新是 07-22 的《応用情報技術者》p43,账户实时那份
    其实是 07-26 的费恩曼 p57——用户看到的「快照/实时/屏幕三者不一致」就是这么来的。
    owner uid 从 legacy-claim.json 读(不硬编码);拿不到就退回 legacy,不猜。"""
    data = Path(os.environ.get("WEBAPP_DATA", "/home/bwicarus/webapp/data"))
    claim = jload(data / "reader-sidecars" / "legacy-claim.json", {}) or {}
    uid = ((claim.get("owner") or {}) if isinstance(claim.get("owner"), dict) else {}).get("user_id")
    if uid is None:
        return None
    root = data / "reader-sidecars" / "by-user" / str(uid)
    return root if root.is_dir() else None


SIDECAR_ACCT = _sidecar_account_root()


def sc(*parts) -> Path:
    """sidecar 路径解析:账户实时优先,该账户没有这项才回退 state/ 的 legacy 副本。
    (convo / cli-tasks / page-brief 等尚未账户化的数据集就走回退,行为不变。)"""
    if SIDECAR_ACCT is not None:
        cand = SIDECAR_ACCT.joinpath(*[str(x) for x in parts])
        if cand.exists():
            return cand
    return ST.joinpath(*[str(x) for x in parts])


def sha16(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def sha40(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def book_sha(rel: str) -> str:
    """pdf-page-brief / pdf-figures 用绝对路径 sha（与 pdf_reader._book_sha 一致）。"""
    return hashlib.sha1(str((VAULT / rel).resolve()).encode()).hexdigest()[:16]


def ts_fmt(t) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(t)))
    except Exception:
        return "—"


def _load_positions() -> list[dict]:
    pos = jload(sc("reader-positions.json"), {}) or {}
    rows = [{"file": f, **v} for f, v in pos.items() if isinstance(v, dict)]
    rows.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return rows


# 活动状态新鲜度阈值:跟服务端 _CTX_ACTIVE_FRESH_SEC 一致(前端可见时 60s 心跳的 3 倍)。
# 超过就判「不新鲜」——此时**必须**说未知,绝不许退回历史表里挑一本冒充当前(本次修复的根因)。
CTX_FRESH_SEC = 180


def _load_active() -> dict | None:
    """当前活动文档(唯一权威源)。前端在活动书/页变化时上报,带 ts。

    ⚠ 别再用 reader-positions.json 的最大 ts 当「当前在读」:那是**每本书各自的续读位置**,
    一本两天前读完的书会永远霸榜(就是它把《応用情報技術者》顶成了「当前」),而此刻正开着
    却没翻页的书根本不在榜首。"""
    d = jload(sc("reader-active.json"), {}) or {}
    return d if isinstance(d, dict) and d.get("ts") else None


def _ctx_sync_enabled() -> bool:
    """双向上下文同步总开关(默认关)。关=前端不上报 + 这里不该被推送。"""
    return bool((jload(sc("reader-context-sync.json"), {}) or {}).get("enabled"))


def _current_html() -> dict | None:
    """HTML 宿主不写 reading-pos，只有「上次打开的文件」。"""
    d = jload(sc("web-last.json"), None)
    if isinstance(d, dict) and d.get("file"):
        return d
    for p in sorted(glob.glob(str(sc("web-last-by-user") / "*.json"))):
        d = jload(p, None)
        if isinstance(d, dict) and d.get("file"):
            return d
    return None


def _pdf_brief(rel: str, page) -> dict | None:
    d = jload(sc("pdf-page-brief", f"{book_sha(rel)}.json"), {}) or {}
    return (d.get("briefs") or {}).get(str(page))


def _marks_summary(rel: str, kind: str, page) -> list[str]:
    """本书标注概况：高亮 / 便签 / 手写 / 插入页（只给计数与当前页附近条目）。"""
    out = []
    try:
        near = range(int(page) - NEAR_PAGES, int(page) + NEAR_PAGES + 1)
    except Exception:
        near = []

    if kind == "pdf":
        hl = (jload(sc("pdf-highlights", f"{sha40(rel)}.json"), {}) or {}).get("highlights") or []
        n_near = [h for h in hl if h.get("page") in near]
        out.append(f"- 高亮：共 {len(hl)} 条；当前页±{NEAR_PAGES} 有 {len(n_near)} 条")
        for h in n_near[:5]:
            txt = (h.get("text") or "").replace("\n", " ")[:60]
            out.append(f"  - p{h.get('page')}「{txt}」{'（有备注）' if (h.get('note') or h.get('body')) else ''}")
        ink = (jload(sc("pdf-ink", f"{sha40(rel)}.json"), {}) or {}).get("pages") or {}
        if ink:
            out.append(f"- 手写：{len(ink)} 页有笔迹（页码 {', '.join(sorted(ink.keys())[:10])}）")
    elif kind == "epub":
        hl = jload(sc("epub-highlights", f"{sha16(rel)}.json"), []) or []
        out.append(f"- 高亮：共 {len(hl)} 条")
        for h in hl[:5]:
            txt = (h.get("text") or "").replace("\n", " ")[:60]
            sec = (h.get("anchor") or {}).get("section")
            out.append(f"  - 第{sec}节「{txt}」")
        ink = (jload(sc("epub-ink", f"{sha16(rel)}.json"), {}) or {}).get("sections") or {}
        if ink:
            out.append(f"- 手写：{len(ink)} 节有笔迹")
    else:
        hl = jload(sc("html-highlights", f"{sha16(rel)}.json"), []) or []
        out.append(f"- 高亮：共 {len(hl)} 条")

    notes = jload(sc("reader-notes", f"{sha16(rel)}.json"), []) or []
    if notes:
        n_near = [n for n in notes if (n.get("anchor") or {}).get("page") in near]
        out.append(f"- 便签：共 {len(notes)} 条；当前页附近 {len(n_near)} 条")
        for n in (n_near or notes)[:3]:
            t = (n.get("text") or "").replace("\n", " ")[:50]
            if t:
                out.append(f"  - 「{t}」")
    ups = jload(sc("reader-userpages", f"{sha16(rel)}.json"), []) or []
    if ups:
        out.append(f"- 插入页：{len(ups)} 页（{', '.join((u.get('title') or u.get('id') or '') for u in ups[:5])}）")
    return out or ["- （本书暂无标注）"]


def _convo(rel: str, kind: str) -> tuple[list[str], str]:
    """助手对话：PDF/全局在 assistant-convo/<uid>.json；EPUB 另有 epub-convo/<uid>/<file16>.json。"""
    cands = sorted(glob.glob(str(sc("assistant-convo") / "*.json")),
                   key=os.path.getmtime, reverse=True)
    if kind == "epub":
        ep = sorted(glob.glob(str(sc("epub-convo") / "*" / f"{sha16(rel)}.json")),
                    key=os.path.getmtime, reverse=True)
        cands = ep + cands
    if not cands:
        return ["- （无对话记录）"], ""
    src = cands[0]
    msgs = jload(src, []) or []
    if isinstance(msgs, dict):
        msgs = msgs.get("messages") or []
    lines = []
    for m in msgs[-CONVO_TURNS:]:
        if not isinstance(m, dict):
            continue
        who = {"user": "我", "assistant": "助手"}.get(m.get("role"), str(m.get("role")))
        meta = ts_fmt(m.get("ts"))
        if m.get("page"):
            meta += f" ｜p{m['page']}"
        if m.get("via"):
            meta += f" ｜{m['via']}"
        body = (m.get("content") or "").strip()
        if len(body) > CONVO_CHARS:
            body = body[:CONVO_CHARS] + "…（截断）"
        lines += [f"**{who}**（{meta}）", "", body, ""]
        if m.get("trace"):
            lines += [f"<sub>本轮有工具调用轨迹 {len(m['trace'])} 步</sub>", ""]
    return (lines or ["- （无对话记录）"]), f"{Path(src).name}（共 {len(msgs)} 轮，取最近 {min(len(msgs), CONVO_TURNS)}）"


TASK_RECENT = 12          # 「当前/最近」详细展开的任务数
TASK_ARCHIVE = 60         # 历史归档段的行数上限（只列一行摘要，长期保留、不做激进摘要）
TASK_INSTR_CHARS = 600    # 指令原文保留长度
TASK_RESULT_CHARS = 500   # 结果/错误保留长度


def _task_one(d: dict, detailed: bool) -> list[str]:
    """把一条 cli-task 渲染成 Markdown。detailed=True 时展开指令原文与每步轨迹。"""
    ts = ts_fmt(d.get("ts"))
    kind = d.get("kind", "?")
    st = d.get("status", "?")
    instr = (d.get("instruction") or "").strip()
    where = ""
    if d.get("file_rel") or d.get("page"):
        where = f" ｜{d.get('file_rel','')}" + (f" p{d['page']}" if d.get("page") else "")
    if not detailed:
        head = instr.replace("\n", " ")[:70] or (d.get("step") or d.get("speak") or "")
        return [f"- `{ts}` [{st}] {kind}{where}：{head}"]

    out = [f"### `{ts}` [{st}] {kind}{where}", ""]
    if instr:
        body = instr if len(instr) <= TASK_INSTR_CHARS else instr[:TASK_INSTR_CHARS] + "…（截断）"
        out += ["**指令原文**：", "", "```", body, "```", ""]
    if d.get("recipe"):
        out.append(f"- 配方：`{d['recipe']}`")
    if d.get("orch"):
        out.append("- 由编排器发起（orch）")
    if d.get("step"):
        out.append(f"- 当前步骤：{d['step']}")
    steps = d.get("steps") or []
    if isinstance(steps, list) and steps:
        out += ["", f"**执行轨迹（{len(steps)} 步）**：", ""]
        for i, stp in enumerate(steps, 1):
            if not isinstance(stp, dict):
                continue
            name = stp.get("name", "?")
            args = stp.get("args")
            argtxt = ""
            if isinstance(args, dict) and args:
                argtxt = " " + json.dumps(args, ensure_ascii=False)[:160]
            out.append(f"{i}. `{name}`{argtxt}")
            if stp.get("rationale"):
                out.append(f"   - 理由：{str(stp['rationale'])[:160]}")
            r = stp.get("result")
            if r:
                out.append(f"   - 结果：{str(r)[:200]}")
        out.append("")
    if d.get("speak"):
        out += [f"**口播**：{str(d['speak'])[:200]}", ""]
    r = d.get("result")
    if r:
        txt = json.dumps(r, ensure_ascii=False) if isinstance(r, (dict, list)) else str(r)
        out += ["**结果**：", "", "```", txt[:TASK_RESULT_CHARS] + ("…（截断）" if len(txt) > TASK_RESULT_CHARS else ""),
                "```", ""]
    if d.get("error"):
        out += [f"**错误**：`{str(d['error'])[:TASK_RESULT_CHARS]}`", ""]
    ca = d.get("client_actions") or []
    if ca:
        out += [f"- 前端动作 {len(ca)} 个：" + json.dumps(ca, ensure_ascii=False)[:200], ""]
    return out


def _tasks() -> list[str]:
    """还原「网页这边正在要求系统做什么」：指令原文 + 参数 + 时间 + 宿主/书页 + 轨迹 + 结果。

    分两段：当前/最近（详细展开）与历史归档（一行一条，长期保留，不做激进摘要）。
    """
    files = sorted(glob.glob(str(sc("cli-tasks") / "*.json")), key=os.path.getmtime, reverse=True)
    tasks = []
    for f in files:
        d = jload(f, {}) or {}
        if isinstance(d, dict) and d:
            tasks.append(d)
    tasks.sort(key=lambda d: d.get("ts") or 0, reverse=True)

    out = []
    running = [d for d in tasks if d.get("status") not in ("done", "error", "cancelled")]
    if running:
        out += ["**进行中**：", ""]
        for d in running[:5]:
            out += _task_one(d, detailed=True)

    recent = [d for d in tasks if d not in running][:TASK_RECENT]
    if recent:
        out += ["**最近完成（按时间倒序，含指令原文与轨迹）**：", ""]
        for d in recent:
            out += _task_one(d, detailed=True)

    older = [d for d in tasks if d not in running][TASK_RECENT:TASK_RECENT + TASK_ARCHIVE]
    if older:
        out += ["<details><summary>**历史归档**（更早的命令，一行一条，长期保留）</summary>", ""]
        for d in older:
            out += _task_one(d, detailed=False)
        out += ["", "</details>", ""]

    cre = sorted(glob.glob(str(sc("assistant-creations") / "*.json")), key=os.path.getmtime, reverse=True)
    if cre:
        items = jload(cre[0], []) or []
        if isinstance(items, dict):
            items = items.get("items") or []
        if items:
            out += ["", f"**创造物库**（共 {len(items)} 条，列最近 8）：", ""]
            for it in items[-8:][::-1]:
                if isinstance(it, dict):
                    ref = f"`{it.get('id')}`" if it.get("id") else ""
                    out.append(f"- [{it.get('kind','?')}] {ref} {(it.get('brief') or it.get('query') or '')[:80]}")
    return out or ["- （无任务记录）"]


def _copy_assets(out: Path, rel: str, kind: str, page) -> list[str]:
    """只带当前页图 + 最近一张工具截图；总量受 ASSET_BUDGET 限制。"""
    ad = out / "assets"
    ad.mkdir(parents=True, exist_ok=True)
    for old in ad.glob("*"):          # 每次重建，避免无限增长
        try:
            old.unlink()
        except Exception:
            pass
    picked, used = [], 0

    def take(src: Path, label: str):
        nonlocal used
        if not src.exists() or used + src.stat().st_size > ASSET_BUDGET:
            return
        dst = ad / src.name
        shutil.copy2(src, dst)
        used += dst.stat().st_size
        picked.append((label, dst.name, dst.stat().st_size))

    if kind == "pdf":
        cands = sorted(glob.glob(str(sc("pdf-page-img") / f"{book_sha(rel)}-p{page}-w*")),
                       key=os.path.getmtime, reverse=True)
        if cands:
            take(Path(cands[0]), "当前页图")
    shots = sorted(glob.glob(str(sc("reader-toolshots") / "*")), key=os.path.getmtime, reverse=True)
    if shots:
        take(Path(shots[0]), "最近工具截图")

    lines = []
    for label, name, size in picked:
        lines += [f"**{label}**", "", f"![{label}](assets/{name})", "",
                  f"<sub>`{name}` · {size//1024} KB</sub>", ""]
    return lines or ["- （无可用图像资产）"]


PAGE_TEXT_LIMIT = 6000     # 单页正文进快照的上限(超出截断并标注)


def _page_text(cur: dict) -> dict:
    """当前页/节的**正文文字**。用户拍板:有文字层就必须给文字,页图只作版式核对与兜底。

    返回固定形状,永远带 text_available / text_source / fallback_reason —— 静默退化
    (有文字层却只给图)是明确禁止的,所以"没取到"必须说清楚是哪一步没取到。
    三宿主各按自己的正文源:PDF=剔噪字符层、EPUB=章节段落、HTML/网页=暂无服务端正文源。
    """
    r = {"text": "", "text_available": False, "text_source": None,
         "fallback_reason": None, "truncated": False}
    if not cur:
        r["fallback_reason"] = "当前在读未知(无新鲜活动上报)"
        return r
    kind = cur.get("kind")
    rel = cur.get("member") or cur.get("file")     # 合并书:正文永远落到真实卷
    pos = cur.get("member_pos") if cur.get("member_pos") is not None else cur.get("pos")
    if not rel:
        r["fallback_reason"] = f"{kind or '未知宿主'} 没有可定位的本地文件(如网页 URL)"
        return r
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(ROOT) / "_server_deploy"))
        if kind == "pdf":
            import pdf_reader as _P
            ap = (VAULT / rel)
            if not ap.exists():
                r["fallback_reason"] = f"文件不存在:{rel}"
                return r
            if not pos:
                r["fallback_reason"] = "活动上报没带页码"
                return r
            txt = _P._page_text_clean(str(ap), rel, int(pos), limit=PAGE_TEXT_LIMIT + 1) or ""
            if txt.strip():
                r.update(text=txt[:PAGE_TEXT_LIMIT], text_available=True,
                         text_source="pdf:字符层(已剔噪/去振假名)",
                         truncated=len(txt) > PAGE_TEXT_LIMIT)
            else:
                # 扫描件常见:没有文字层。此时页图才是唯一可读来源 —— 明说,别让人以为正文空白。
                r["fallback_reason"] = "该页无可用文字层(疑似扫描页;可用页图或 OCR 结果)"
        elif kind == "epub":
            import pdf_reader as _P
            paras = _P._epub_section_paragraphs(rel, int(pos or 0)) or []
            txt = "\n\n".join(str(x) for x in paras if str(x).strip())
            if txt.strip():
                r.update(text=txt[:PAGE_TEXT_LIMIT], text_available=True,
                         text_source="epub:章节段落(消毒后 HTML → 纯文本)",
                         truncated=len(txt) > PAGE_TEXT_LIMIT)
            else:
                r["fallback_reason"] = f"第 {pos} 节取不到段落(空节/解析失败)"
        elif kind == "html":
            r["fallback_reason"] = "HTML 宿主:正文在前端 DOM,服务端暂无等价正文源(需前端上报)"
        else:
            r["fallback_reason"] = f"宿主 {kind!r} 暂无正文提取实现"
    except Exception as ex:
        r["fallback_reason"] = f"提取异常:{type(ex).__name__}: {str(ex)[:120]}"
    return r


def build(out: Path) -> str:
    """生成单一完整快照。连续翻页由调用侧 1s trailing debounce 合并，这里每次都是全量。"""
    out.mkdir(parents=True, exist_ok=True)
    rows = _load_positions()
    active = _load_active()
    sync_on = _ctx_sync_enabled()
    age = (int(time.time()) - int(active.get("ts") or 0)) if active else None
    fresh = bool(active and age is not None and 0 <= age <= CTX_FRESH_SEC)
    # 铁律:只有新鲜的 active 才配当「当前在读」。不新鲜 → cur=None → 全文如实说未知,
    # 下游的页要点/图像/标注/对话也一律留空,而不是拿旧书的数据冒充这一页。
    cur = active if fresh else None
    html_last = _current_html()

    L = ["# 当前阅读上下文快照", ""]
    gen_ts = time.strftime('%Y-%m-%d %H:%M:%S %Z')
    L += [f"> 生成于 {gen_ts}　·　由 Pi 经 SSH 原子更新（停止翻页约 1 秒后即为本页完整上下文）",
          f">　·　双向上下文同步：{'🟢 已开启' if sync_on else '⚪ 已关闭（前端不上报活动状态）'}",
          ">",
          "> **这是快照，不是实时真值。** 用途：每轮开局先读本文，快速知道"
          "在读哪本书哪页、助手在聊什么、有哪些任务产物与标注。",
          "> **以下情形必须改用阅读器 MCP**（见文末对照表）：要精确页正文 / 下一页 / 实时选区，"
          "或要翻页、高亮、编辑、制卡等**任何操作**。", ""]

    # 分层(用户拍板 2026-07-27):把「此刻要用的」和「历史留档」明确切开。
    # 完整历史仍然保留(不做激进摘要),但放到归档区并标注,免得外部助手拿旧记录当现状。
    L += ["---", "",
          "## 📌 当前上下文（高优先 · 判断现状只看这一段）", "",
          "> 下面 一～四 节都是**此刻**的状态：在读什么、这一页的正文与要点、当前选区。",
          "> 第 五 节之后是**历史归档**（标注、既往对话、命令轨迹、各书续读位置）——",
          "> 它们只作背景参考，**不能用来推断用户现在在看什么**。", ""]
    L += ["## 一、当前在读", ""]
    if cur:
        kind = cur.get("kind")
        unit = {"pdf": "页（1-based）", "epub": "节（0-based）"}.get(kind, "")
        L += [f"- **状态**：🟢 实时（{age} 秒前上报）",
              f"- **书/文档**：`{cur.get('file') or cur.get('url') or '—'}`",
              f"- **类型**：{kind}" + ("（**合并书 vbook**：多卷合成一本，页码是全局页）" if cur.get("vbook") else "")]
        if cur.get("member"):
            L.append(f"- **实际所在卷**：`{cur['member']}` 第 {cur.get('member_pos')} 页"
                     "　·　<sub>取正文/插图/标注要用这个卷 rel，不是上面的 vbook 引用</sub>")
        if cur.get("pos") is not None:
            L.append(f"- **位置**：{cur.get('pos')} {unit}".rstrip())
        if cur.get("title"):
            L.append(f"- **标题**：{cur.get('title')}")
        # 选区三态必须可分辨:有选区 / 明确没有 / 根本没上报过。省略字段=外部助手会
        # 拿着早就取消的旧选区做判断,用户明确禁止这种静默退化。
        if cur.get("selection"):
            L.append(f"- **当前选区**：{cur.get('selection')}"
                     + (f"（第 {cur['sel_page']} 页）" if cur.get("sel_page") else ""))
        elif "selection" in cur:
            L.append("- **当前选区**：（无——用户已取消选中，不是没同步）")
        else:
            L.append("- **当前选区**：未上报（该宿主暂未接入选区同步）")
        L += [f"- **上报时间**：{ts_fmt(cur.get('ts'))}",
              "- ⚠ 仍是快照：页内滚动偏移不上报，用户也可能刚翻了页 → "
              "需要精确位置/正文时用 MCP `reading_positions` / `read_page` 校验。", ""]
    else:
        if not sync_on:
            why = "**双向上下文同步开关是关闭的**（设置 → 通用 → 🔁 双向上下文同步），前端不上报活动状态。"
        elif active is None:
            why = "开关已开，但还没收到过任何活动状态上报（可能阅读器页面尚未打开或未翻页）。"
        else:
            mins = (age or 0) // 60
            why = (f"最后一次活动上报已过期（{mins} 分钟前，超过 {CTX_FRESH_SEC//60} 分钟新鲜窗口）——"
                   f"页面可能已关闭或设备离线。")
        L += ["- **当前在读：未知**",
              f"- 原因：{why}",
              "- ⚠ **不要拿下面「历史记录」里的任何一本当成用户此刻在看的书。**"
              "那张表是每本书各自的续读位置，最新的一条只代表「最后一次翻过页的书」，"
              "跟「此刻屏幕上是哪本」是两回事。",
              "- 需要确认当前在读什么：让用户打开开关，或直接用 MCP 询问 / 让用户口头确认。", ""]
    if html_last:
        L += [f"- <sub>历史参考：HTML/网页最近打开过 `{html_last.get('file')}`"
              f"（{ts_fmt(html_last.get('ts'))}）——同样**不代表当前**。</sub>", ""]

    # 合并书:内容类查询一律落到**真实卷**(vbook 引用在磁盘上不存在,直接拿去算 sha 会全空)
    c_rel = (cur.get("member") or cur.get("file")) if cur else None
    c_pos = (cur.get("member_pos") if cur and cur.get("member_pos") is not None else (cur or {}).get("pos"))

    L += ["## 二、当前页要点", ""]
    b = _pdf_brief(c_rel, c_pos) if (cur and cur.get("kind") == "pdf" and c_rel) else None
    if b:
        L += [f"- **摘要**：{b.get('brief','')}",
              f"- **标签**：{'、'.join(b.get('tags') or []) or '—'}",
              f"- **页型**：{b.get('page_type','')}/{b.get('subtype','') or '—'}"]
        for c in (b.get("concepts") or [])[:5]:
            L.append(f"  - 概念「{c.get('name')}」：{str(c.get('evidence',''))[:70]}…")
        L += ["", "⚠ 这是 AI 生成的**页要点**，不是正文原文。要原文用 MCP `read_page`。", ""]
    else:
        L += ["- 本页无 page-brief（按需生成；EPUB/HTML 无此数据）。",
              "- **要正文请用 MCP `read_page`。**", ""]

    L += ["## 三、当前页正文（文字优先）", ""]
    _pt = _page_text(cur)
    L += [f"- **text_available**：{'是' if _pt['text_available'] else '否'}",
          f"- **text_source**：{_pt['text_source'] or '—'}",
          f"- **fallback_reason**：{_pt['fallback_reason'] or '—'}"]
    if _pt["text_available"]:
        L += ["", "```text", _pt["text"], "```",
              (f"<sub>已截断到 {PAGE_TEXT_LIMIT} 字；要完整正文用 MCP `read_page`。</sub>"
               if _pt["truncated"] else "<sub>本页文字层全文（未截断）。</sub>"), ""]
    else:
        L += ["", "⚠ 本页取不到文字层（原因见上）。此时才应改用下面的页图；"
              "**有文字层的页绝不会只给图**。", ""]

    L += ["## 四、图像（版式/插图核对用；不是正文来源）", ""]
    L += _copy_assets(out, c_rel, cur.get("kind"), c_pos) if (cur and c_rel) else ["- （当前在读未知或非本地书，无图像）"]

    L += ["---", "",
          "## 🗄 历史归档（背景参考 · 不代表当前状态）", "",
          "> 以下为完整留档，不做摘要压缩；但判断「用户此刻在做什么」时不要引用本区内容。", ""]
    L += ["## 五、本书标注概况", ""]
    L += _marks_summary(c_rel, cur.get("kind"), c_pos) if (cur and c_rel) else ["- （当前在读未知，不列任何书的标注——避免误当成当前书）"]
    L += ["", "⚠ 非实时；权威列表用 MCP `list_highlights` / `list_notes`。", ""]

    L += ["## 六、侧栏助手对话", ""]
    conv, src = _convo(c_rel or "", (cur or {}).get("kind") or "")
    if src:
        L += [f"<sub>来源 {src}</sub>", ""]
    L += conv

    L += ["## 七、命令与任务（网页侧发出的指令原文 + 轨迹 + 产物）", ""]
    L += _tasks()
    L += ["", "⚠ 状态可能已推进，需要时用 MCP 复核。", ""]

    L += ["## 八、历史记录（每本书各自的续读位置 · **不是当前在读**）", ""]
    for r in rows[:8]:
        L.append(f"- `{r['file']}` — {r.get('kind')} @ {r.get('pos')}（{ts_fmt(r.get('ts'))}）")
    L += [""]

    L += ["---", "", "## 何时必须改用 MCP（不要只信本快照）", "",
          "| 需求 | 正确做法 |", "|---|---|",
          "| 当前页 / 下一页的**精确正文** | MCP `read_page` |",
          "| **实时选区**、用户此刻屏幕所见 | MCP（快照无此数据：选区只存在于前端） |",
          "| **翻页 / 高亮 / 编辑 / 制卡 / 笔记**等任何写操作 | MCP 对应工具 |",
          "| 用户是否已翻页、位置是否变了 | MCP `reading_positions` |",
          "| 高亮 / 便签的**权威全量列表** | MCP `list_highlights` / `list_notes` |", ""]

    text = "\n".join(L)
    (out / "context.md").write_text(text, "utf-8")
    return text


def main() -> int:
    ap = argparse.ArgumentParser(description="生成阅读上下文 Markdown 快照")
    ap.add_argument("--out", required=True, help="输出目录（含 context.md 与 assets/）")
    a = ap.parse_args()
    out = Path(a.out)
    text = build(out)
    total = sum(p.stat().st_size for p in (out / "assets").glob("*")) if (out / "assets").exists() else 0
    print(f"snapshot ok: {out/'context.md'} ({len(text.encode())} B), assets {total//1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
