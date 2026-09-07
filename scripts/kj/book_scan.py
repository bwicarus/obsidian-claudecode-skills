#!/usr/bin/env python3
"""整本节点析出（手动触发）：滚动会话逐页读图+文字，AI 交页分析，程序走 page-submit 落账。

设计（2026-09-07 用户拍板）：
- **页是最小单位**，整本只是页的连续；每页走与读书时完全相同的提交路径（``pages.submit``）。
- **滚动会话**：一章一个会话（没目录就按 ``--window-pages`` 页一段），逐页追加，前缀吃提示缓存；
  换窗口时开新会话，开场塞账本生成的**接力包**（本书已登记节点/别名/记号 + 上一窗口的页标注 + 末页原文 + 待确认名字）。
- 图只在当页那一轮给；旧页留在缓存前缀里不再重发。
- 两家后端：Claude CLI（``--input-format stream-json`` 多轮同进程）/ Codex CLI（``exec --json`` + ``exec resume``）。
  用量（input / cached / output）逐页记进状态文件 —— 这就是用户要的"每页真实 token"。
- 状态文件 ``state/kj/scans/<sha>.json``，书库页轮询；``<status>.cancel`` 存在就停；重跑跳过已分析页（``--force`` 重做）。

用法：python scripts/kj/book_scan.py --book <abs.pdf> --status <path.json> [--book-id ID --sha SHA --title T]
       [--backend auto|claude|codex] [--model M] [--effort low|medium|high] [--pages 1-30,45] [--force]
       [--window-tokens 90000] [--window-pages 25] [--max-image-edge 1568] [--dry-run]
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))   # scripts/

from kj import pages as PG  # noqa: E402
from kj.service import KJService, project_root  # noqa: E402

CONTRACT = "kj-book-scan/1"
TEXT_CAP = 6000
MAX_CONSECUTIVE_ERRORS = 3

SYSTEM_INSTRUCTIONS = """你是学习资料的页级分析员。每一轮我给你一页书：页图、字符层文字（可能有 OCR 噪声）、这页 YOLO 框出的公式/图表的编号与位置。
你只输出**一个 JSON 对象**（不要 Markdown 围栏、不要解释），结构：
{
 "summary": "这页在干什么（一句话）",
 "kind": ["definition","theorem","proof","example","exercise","prose" 中的若干],
 "notation": [{"symbol":"L(V)","meaning":"V 上全体算子","concept":"线性映射"}],
 "concepts": [
   {"name":"概念名（用书里的叫法）","kind":"concept|theorem|person|method|problem","role":"defined|stated|used|exercised",
    "aliases":["别名/英文名"],"qid":"能确定的 Wikidata 编号，不确定就不填",
    "definition":{"text":"被定义/被陈述时的原句（逐字，公式用 LaTeX）","uses":["看懂这句必须先会的概念名"]}}
 ],
 "formulas": [{"idx":0,"latex":"..."}],
 "figures": [{"idx":0,"desc":"图表内容描述"}],
 "exercises": [{"label":"5.A.1","concepts":["练到的概念"]}],
 "pitfalls": [{"text":"书里点明的易错处原句","concept":"相关概念"}]
}
规则：
- concepts 只收这页**真正定义、陈述、用到、练到**的概念；定理、引理、命题也是概念（kind=theorem），statement 放 definition.text。
- definition.uses 只写"不会它就看不懂这句"的概念，不是所有出现的词；只是顺带出现的写进 concepts 里 role=used 即可。
- 同名概念优先沿用接力包里已登记的名字；接力包里没有、书里首次出现的才是新概念。
- formulas 按给出的 idx 填 LaTeX，看不清就不填；figures 按 idx 写描述。没给框就留空数组。
- 不要编造书里没有的内容；OCR 噪声按上下文纠正后再引用。
"""


# ── 状态文件 ────────────────────────────────────────────────────────────────
class Status:
    def __init__(self, path: Path, base: dict):
        self.path = path
        self.data = dict(base)
        self.data.setdefault("contract", CONTRACT)
        self.data.setdefault("pages", {})
        self.data.setdefault("tokens", {"input": 0, "cached": 0, "cache_write": 0, "output": 0, "turns": 0})
        self.data.setdefault("errors", [])
        self.data["pid"] = os.getpid()

    def set(self, **kw) -> None:
        self.data.update(kw)
        self.data["updated_at"] = int(time.time())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, self.path)

    def add_usage(self, usage: dict | None) -> None:
        if not usage:
            return
        t = self.data["tokens"]
        # Claude 的口径：input=未缓存输入，cached=缓存读取，cache_write=本轮新写进缓存的输入（首轮 4 万多都在这里）。
        # 三项都记，不然总账看起来"输入 4"，把最贵的一项藏了（2026-09-07 实测）。
        t["input"] += int(usage.get("input") or 0)
        t["cached"] += int(usage.get("cached") or 0)
        t["cache_write"] = t.get("cache_write", 0) + int(usage.get("cache_write") or 0)
        t["output"] += int(usage.get("output") or 0)
        t["turns"] += 1

    @property
    def cancel_requested(self) -> bool:
        return Path(str(self.path) + ".cancel").exists()


# ── 后端会话 ────────────────────────────────────────────────────────────────
class SessionError(RuntimeError):
    pass


def _extract_json(text: str) -> dict | None:
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s[:-3]
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
        return v if isinstance(v, dict) else None
    except Exception:
        return None


class ClaudeSession:
    """一个 CLI 进程 = 一个会话；stream-json 多轮，前缀吃提示缓存。"""

    def __init__(self, model: str, effort: str, workdir: Path):
        exe = os.environ.get("APP_CLAUDE") or shutil.which("claude") or "claude"
        cmd = [exe, "--print", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--setting-sources", "",
               "--disallowedTools", "Bash Edit Write Read NotebookEdit WebFetch WebSearch Glob Grep Task"]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        self.p = subprocess.Popen(cmd, cwd=str(workdir), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1)

    def send(self, text: str, image_png: bytes | None, timeout: float = 600) -> tuple[str, dict]:
        content: list[dict] = []
        if image_png:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                        "data": base64.b64encode(image_png).decode("ascii")}})
        content.append({"type": "text", "text": text})
        assert self.p.stdin is not None and self.p.stdout is not None
        self.p.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n")
        self.p.stdin.flush()
        t0 = time.time()
        while time.time() - t0 < timeout:
            ln = self.p.stdout.readline()
            if not ln:
                if self.p.poll() is not None:
                    err = (self.p.stderr.read() if self.p.stderr else "")[:300]
                    raise SessionError(f"claude 进程退出 rc={self.p.returncode} {err}")
                continue
            try:
                ev = json.loads(ln)
            except Exception:
                continue
            if ev.get("type") == "result":
                res = (ev.get("result") or "").strip()
                if ev.get("is_error") or res.startswith("Failed to authenticate"):
                    raise SessionError(res or "claude 返回错误")
                u = ev.get("usage") or {}
                usage = {"input": u.get("input_tokens", 0),
                         "cached": u.get("cache_read_input_tokens", 0),
                         "output": u.get("output_tokens", 0),
                         "cache_write": u.get("cache_creation_input_tokens", 0)}
                return res, usage
        raise SessionError("claude 回合超时")

    def close(self) -> None:
        try:
            if self.p.stdin:
                self.p.stdin.close()
            self.p.wait(timeout=15)
        except Exception:
            try:
                self.p.kill()
            except Exception:
                pass


class CodexSession:
    """codex exec --json 开线程，之后 exec resume <thread> 续；图用 -i 文件。"""

    def __init__(self, model: str, workdir: Path):
        exe = os.environ.get("APP_CODEX") or shutil.which("codex") or shutil.which("codex.cmd") or "codex"
        self.base = ["cmd.exe", "/d", "/c", exe] if exe.lower().endswith((".cmd", ".bat")) else [exe]
        self.model = model
        self.workdir = workdir
        self.thread: str | None = None

    def send(self, text: str, image_png: bytes | None, timeout: float = 900) -> tuple[str, dict]:
        img_path = None
        if image_png:
            fd, img_path = tempfile.mkstemp(prefix="kjpage-", suffix=".png", dir=str(self.workdir))
            os.close(fd)
            Path(img_path).write_bytes(image_png)
        try:
            if self.thread is None:
                cmd = self.base + ["exec", "--json", "--sandbox", "read-only", "--skip-git-repo-check"]
                if self.model:
                    cmd += ["-m", self.model]
                if img_path:
                    cmd += ["-i", img_path]
                cmd.append("-")
            else:
                cmd = self.base + ["exec", "resume", "--json", "--skip-git-repo-check"]
                if self.model:
                    cmd += ["-m", self.model]
                if img_path:
                    cmd += ["-i", img_path]
                cmd += [self.thread, "-"]
            r = subprocess.run(cmd, input=text, cwd=str(self.workdir), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout)
        finally:
            if img_path:
                try:
                    os.unlink(img_path)
                except Exception:
                    pass
        out_text, usage = "", {}
        for ln in (r.stdout or "").splitlines():
            try:
                ev = json.loads(ln)
            except Exception:
                continue
            t = ev.get("type") or ""
            if t == "thread.started" and ev.get("thread_id"):
                self.thread = str(ev["thread_id"])
            elif t == "item.completed":
                item = ev.get("item") or {}
                if item.get("type") in ("agent_message", "message", "assistant_message") and item.get("text"):
                    out_text = str(item["text"])
            elif t == "turn.completed":
                u = ev.get("usage") or {}
                usage = {"input": u.get("input_tokens", 0), "cached": u.get("cached_input_tokens", 0),
                         "cache_write": u.get("cache_write_input_tokens", 0),
                         "output": u.get("output_tokens", 0), "reasoning": u.get("reasoning_output_tokens", 0)}
        if r.returncode != 0 and not out_text:
            raise SessionError(f"codex rc={r.returncode}: {(r.stderr or '')[:300]}")
        if not out_text:
            raise SessionError("codex 没有返回文本")
        return out_text, usage

    def close(self) -> None:
        pass


def make_session(backend: str, model: str, effort: str, workdir: Path):
    if backend == "codex":
        return CodexSession(model, workdir)
    return ClaudeSession(model, effort, workdir)


# ── 取材 ────────────────────────────────────────────────────────────────────
def _token() -> str | None:
    t = (os.environ.get("MCP_WEBAPP_TOKEN") or "").strip()
    if t:
        return t
    p = Path.home() / ".config" / "mcp-webapp-token"
    return p.read_text("utf-8").strip() if p.is_file() else None


def page_text_via_flask(rel: str | None, page: int) -> str | None:
    """走阅读器同一条页文字接口（含 OCR 层与公式注入）；拿不到返回 None 由 PyMuPDF 兜底。"""
    if not rel:
        return None
    tok = _token()
    if not tok:
        return None
    try:
        import urllib.parse
        import urllib.request
        base = os.environ.get("BW_KJ_WEBAPP_BASE") or "http://127.0.0.1:5000"
        url = base + "/pdf/api/page-text?" + urllib.parse.urlencode({"file": rel, "page": page})
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok})
        with urllib.request.urlopen(req, timeout=30) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        if isinstance(d, dict) and d.get("ok") and d.get("text"):
            return str(d["text"])
    except Exception:
        return None
    return None


def render_page(doc, page_no: int, max_edge: int) -> bytes:
    import fitz  # PyMuPDF
    page = doc[page_no - 1]
    w, h = float(page.rect.width), float(page.rect.height)
    scale = max_edge / max(w, h, 1.0)
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    return pix.tobytes("png")


def chapter_ranges(doc, total: int, window_pages: int) -> list[tuple[int, int, str]]:
    """按目录一级条目切窗口；没目录就固定页数一段。"""
    try:
        toc = doc.get_toc(simple=True) or []
    except Exception:
        toc = []
    starts = sorted({int(p) for lvl, _t, p in toc if int(lvl) == 1 and 1 <= int(p) <= total})
    titles = {int(p): str(t) for lvl, t, p in toc if int(lvl) == 1}
    if len(starts) >= 2:
        out = []
        for i, s in enumerate(starts):
            e = (starts[i + 1] - 1) if i + 1 < len(starts) else total
            if s > 1 and i == 0:
                out.append((1, s - 1, "前言"))
            out.append((s, e, titles.get(s, f"第 {i + 1} 章")))
        return out
    return [(s, min(total, s + window_pages - 1), f"第 {s}-{min(total, s + window_pages - 1)} 页") for s in range(1, total + 1, window_pages)]


def handoff_pack(svc: KJService, key: str, title: str, last_summaries: list[str], last_text: str, unresolved: list[str]) -> str:
    """接力包：本书已登记的节点（名/别名/记号）+ 上一窗口的页标注 + 末页原文 + 待确认名字。全部从账本生成。"""
    L = svc.ledger
    rows = L.db.execute("SELECT DISTINCT node_id FROM page_nodes WHERE book=? ORDER BY page", (key,)).fetchall()
    names = []
    for r in rows[:300]:
        n = L.node(r[0])
        if not n:
            continue
        al = [a["alias"] for a in L.aliases(r[0])][:6]
        names.append(n["name"] + ("（" + "、".join(al) + "）" if al else ""))
    parts = [f"《{title}》接力包（程序从账本生成，不是转述）：",
             "已登记的概念（{}个，沿用这些名字，不要重复建）：".format(len(names)) + ("；".join(names) if names else "还没有")]
    if last_summaries:
        parts.append("上一段各页标注：" + " / ".join(s for s in last_summaries[-12:] if s))
    if unresolved:
        parts.append("上一段没解析到的名字（这段若见到其定义请当新概念登记）：" + "、".join(sorted(set(unresolved))[:40]))
    if last_text:
        parts.append("上一页原文（供衔接）：" + last_text[:1500])
    return "\n".join(parts)


def page_prompt(page_no: int, total: int, text: str, boxes: dict, first_in_window: bool) -> str:
    fb = [{"idx": f["idx"], "bbox": f["bbox"]} for f in boxes.get("formulas", [])]
    gb = [{"idx": g["idx"], "bbox": g["bbox"], "caption": g.get("caption") or ""} for g in boxes.get("figures", [])]
    head = f"第 {page_no} 页（共 {total} 页）。"
    if first_in_window:
        head += "这是本段第一页。"
    body = [head,
            "YOLO 框（页内编号，bbox 为归一化 [x0,y0,x1,y1]）：公式 " + json.dumps(fb, ensure_ascii=False) + "；图表 " + json.dumps(gb, ensure_ascii=False),
            "字符层文字（可能有 OCR 噪声）：\n" + (text or "（这页没有文字层，只看图）")[:TEXT_CAP],
            "只输出这页分析的 JSON。"]
    return "\n".join(body)


# ── 主流程 ──────────────────────────────────────────────────────────────────
def parse_pages(spec: str | None, total: int) -> list[int]:
    if not spec:
        return list(range(1, total + 1))
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(max(1, int(a)), min(total, int(b)) + 1))
        else:
            n = int(part)
            if 1 <= n <= total:
                out.add(n)
    return sorted(out)


def _ai_settings() -> dict:
    try:
        sys.path.insert(0, str(HERE.parents[1]))
        import ai_client  # type: ignore
        return ai_client.load_settings() or {}
    except Exception:
        return {}


def pick_backend(pref: str) -> str:
    if pref in ("claude", "codex"):
        return pref
    b = _ai_settings().get("backend") or "auto-claude"
    return "codex" if b == "codex" else "claude"


def pick_model(backend: str, explicit: str) -> str:
    """Codex 不给 -m 会用 CLI 默认（本机现为 gpt-6-astra，旧版 CLI 直接 400）；跟 ai_client 一样默认 gpt-5.5。"""
    if explicit:
        return explicit
    if backend == "codex":
        return str(_ai_settings().get("model") or "").strip() or "gpt-5.5"
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--book", required=True, help="PDF 绝对路径")
    ap.add_argument("--status", required=True, help="状态文件路径（书库页轮询）")
    ap.add_argument("--book-id", default="")
    ap.add_argument("--sha", default="", help="书库 contentSha256（只作记录）")
    ap.add_argument("--title", default="")
    ap.add_argument("--rel", default="", help="书库相对路径（走阅读器页文字接口用）；不给就从 OBSIDIAN_VAULT 推")
    ap.add_argument("--backend", default="auto", choices=["auto", "claude", "codex"])
    ap.add_argument("--model", default="")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--pages", default="", help="如 1-30,45；默认全书")
    ap.add_argument("--force", action="store_true", help="已分析页也重做")
    ap.add_argument("--window-tokens", type=int, default=90_000)
    ap.add_argument("--window-pages", type=int, default=25)
    ap.add_argument("--max-image-edge", type=int, default=1568)
    ap.add_argument("--dry-run", action="store_true", help="只列出要处理的页与窗口，不调 AI")
    a = ap.parse_args()

    book = Path(a.book).resolve()
    status_path = Path(a.status)
    key = PG.book_key(str(book))
    title = a.title or book.stem
    rel = a.rel
    if not rel:
        vault = os.environ.get("OBSIDIAN_VAULT")
        if vault:
            try:
                rel = book.relative_to(Path(vault).resolve()).as_posix()
            except Exception:
                rel = ""
    st = Status(status_path, {"book_id": a.book_id, "sha": a.sha, "book": str(book), "key": key, "title": title,
                              "state": "starting", "started_at": int(time.time()), "backend": None})
    st.set()
    try:
        import fitz  # noqa: F401
    except Exception as exc:
        st.set(state="error", message=f"缺 PyMuPDF：{exc}")
        return 2
    import fitz
    try:
        doc = fitz.open(str(book))
    except Exception as exc:
        st.set(state="error", message=f"打不开 PDF：{exc}")
        return 2
    total = doc.page_count
    svc = KJService(actor=f"book-scan:{a.backend}")
    L = svc.ledger
    wanted = parse_pages(a.pages, total)
    if not a.force:
        done = {r[0] for r in L.db.execute("SELECT page FROM page_analyses WHERE book=?", (key,))}
        wanted = [p for p in wanted if p not in done]
    windows = chapter_ranges(doc, total, a.window_pages)
    plan = [(s, e, t, [p for p in wanted if s <= p <= e]) for s, e, t in windows]
    plan = [w for w in plan if w[3]]
    backend = pick_backend(a.backend)
    st.set(state="planned" if a.dry_run else "running", total_pages=total, todo=len(wanted), done=0, backend=backend,
           model=pick_model(backend, a.model), windows=[{"from": s, "to": e, "title": t, "pages": len(ps)} for s, e, t, ps in plan])
    if a.dry_run:
        print(json.dumps(st.data, ensure_ascii=False, indent=1))
        return 0
    if not plan:
        st.set(state="done", message="没有需要处理的页（都分析过了；--force 重做）")
        return 0

    workdir = Path(tempfile.mkdtemp(prefix="kj-scan-"))
    consecutive_errors = 0
    done_count = 0
    last_summaries: list[str] = []
    last_text = ""
    unresolved: list[str] = []
    try:
        for s, e, wtitle, pgs in plan:
            if st.cancel_requested:
                st.set(state="cancelled", message="用户取消")
                return 0
            session = make_session(backend, pick_model(backend, a.model), a.effort, workdir)
            window_tokens = 0
            first = True
            st.set(window={"from": s, "to": e, "title": wtitle}, message=f"开始「{wtitle}」")
            try:
                for pno in pgs:
                    if st.cancel_requested:
                        st.set(state="cancelled", message="用户取消")
                        return 0
                    if window_tokens > a.window_tokens and not first:
                        # 窗口到头：换会话，接力包开场
                        session.close()
                        session = make_session(backend, pick_model(backend, a.model), a.effort, workdir)
                        window_tokens = 0
                        first = True
                    t0 = time.time()
                    st.set(current_page=pno, message=f"第 {pno} 页")
                    text = page_text_via_flask(rel, pno) or doc[pno - 1].get_text("text")
                    boxes = PG.page_boxes(L, key, pno)
                    prompt = page_prompt(pno, total, text, boxes, first)
                    if first:
                        prompt = SYSTEM_INSTRUCTIONS + "\n" + handoff_pack(svc, key, title, last_summaries, last_text, unresolved) + "\n\n" + prompt
                        unresolved = []
                    png = render_page(doc, pno, a.max_image_edge)
                    try:
                        out, usage = session.send(prompt, png)
                    except SessionError as exc:
                        msg = str(exc)
                        if backend == "claude" and "authenticate" in msg.lower() and a.backend == "auto":
                            st.set(message="Claude CLI 未登录，切到 Codex 继续")
                            backend = "codex"
                            session.close()
                            session = make_session(backend, pick_model(backend, a.model), a.effort, workdir)
                            first = True
                            window_tokens = 0
                            st.set(backend=backend, model=pick_model(backend, a.model))
                            out, usage = session.send(SYSTEM_INSTRUCTIONS + "\n" + handoff_pack(svc, key, title, last_summaries, last_text, unresolved) + "\n\n" + prompt, png)
                        else:
                            raise
                    st.add_usage(usage)
                    window_tokens += int(usage.get("input") or 0) + int(usage.get("cached") or 0)
                    payload = _extract_json(out)
                    entry: dict[str, Any] = {"ms": int((time.time() - t0) * 1000), "usage": usage, "text_chars": len(text or "")}
                    if payload is None:
                        entry["error"] = "AI 没有返回可解析的 JSON"
                        st.data["errors"].append({"page": pno, "error": entry["error"], "raw": (out or "")[:300]})
                        consecutive_errors += 1
                    else:
                        payload["book"] = str(book)
                        payload["page"] = pno
                        payload["book_title"] = title
                        rep = svc.page_submit(payload)
                        if not rep.get("ok"):
                            entry["error"] = rep.get("error") or rep.get("code")
                            st.data["errors"].append({"page": pno, "error": entry["error"]})
                            consecutive_errors += 1
                        else:
                            consecutive_errors = 0
                            done_count += 1
                            entry["report"] = {k: (len(rep.get(k) or []) if isinstance(rep.get(k), list) else rep.get(k))
                                               for k in ("nodes_created", "nodes_resolved", "definitions_added", "prereqs_added",
                                                         "redundant", "rejected", "unresolved_uses", "ambiguous", "records_added", "notation_set")}
                            side = rep.get("sidecar") or {}
                            entry["report"]["formulas_written"] = side.get("formulas_written", 0)
                            entry["report"]["figures_written"] = side.get("figures_written", 0)
                            last_summaries.append(str(payload.get("summary") or "")[:120])
                            unresolved.extend(x for x in rep.get("unresolved_uses") or [] if isinstance(x, str))
                    last_text = text or ""
                    st.data["pages"][str(pno)] = entry
                    st.set(done=done_count)
                    first = False
                    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        st.set(state="error", message=f"连续 {consecutive_errors} 页失败，停止；看 errors")
                        return 3
            finally:
                session.close()
        st.set(state="done", message=f"完成：{done_count} 页", current_page=None)
        return 0
    except Exception as exc:
        st.data["errors"].append({"page": st.data.get("current_page"), "error": f"{type(exc).__name__}: {exc}"})
        st.set(state="error", message=f"{type(exc).__name__}: {str(exc)[:200]}")
        return 1
    finally:
        try:
            svc.close()
        except Exception:
            pass
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
