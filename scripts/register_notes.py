"""
register_notes.py — 笔记登记编排脚本

替代 AI 驱动的 /登记新笔记 skill，直接调用脚本模块完成流程，
仅在语义分析步骤（内容分析、查找关联、生成卡片）调用 AI。

进度通过 task_tracker 报告到悬浮监视窗口。
"""

from __future__ import annotations

import functools
import io
import json
import os
import re
import subprocess
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent

sys.path.insert(0, str(SCRIPTS_DIR))

import ai_client
import annotate_note
import config
import connect_note
import note_state
import summarize_note

from platform_utils import WINDOWS, NO_WINDOW_KW  # noqa: E402

PROJECT_DIR  = config.PROJECT_DIR
VAULT_ROOT   = config.VAULT_ROOT
NOTE_PATTERN = config.NOTE_PATTERN
PYTHON       = config.PYTHON
TEMP_DIR     = config.TEMP_DIR
PROMPTS_DIR  = config.PROMPTS_DIR
_NO_WINDOW   = NO_WINDOW_KW.get("creationflags", 0)  # legacy alias，保留以减少下方改动


@functools.lru_cache(maxsize=16)
def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def _load_vault_structure() -> str:
    return (config.REFERENCES_DIR / "vault-structure.md").read_text(encoding="utf-8")


def _subprocess_capture(cmd: list[str]) -> str:
    r = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", cwd=str(PROJECT_DIR), creationflags=_NO_WINDOW,
    )
    return r.stdout.strip()


# ── Obsidian 同步保护 ────────────────────────────────────────────────────────
# Sync 由 obsidian-headless daemon 持续在跑(Windows 计划任务「Obsidian Headless Sync」),
# 不依赖 Obsidian GUI。这里只确保 daemon 健在,daemon 没跑就拉起来。
OBSIDIAN_SYNC_TASK = "Obsidian Headless Sync"
OBSIDIAN_SYNC_WAIT = 15  # 拉起 daemon 后给一点首跑时间


def is_obsidian_sync_daemon_running() -> bool:
    """检查 obsidian-headless 的 node 进程是否在跑。"""
    if os.name != "nt":
        # Linux 服务器侧：obsidian-sync 是 systemd 服务，看 service 状态
        try:
            r = subprocess.run(
                ["systemctl", "is-active", "obsidian-sync.service"],
                capture_output=True, text=True, timeout=5,
            )
            return r.stdout.strip() == "active"
        except Exception:
            return False
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" "
             "| Where-Object { $_.CommandLine -match 'obsidian-headless' } "
             "| Measure-Object).Count"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            startupinfo=si, creationflags=_NO_WINDOW, timeout=15,
        )
        n = (r.stdout or "").strip()
        return n.isdigit() and int(n) > 0
    except Exception:
        return False


def ensure_obsidian_synced(handle: Any, wait_seconds: int = OBSIDIAN_SYNC_WAIT) -> bool:
    """daemon 没跑就启动计划任务,等几秒让首跑同步完。返回是否触发了等待。

    Linux 服务器侧由 systemd `obsidian-sync.service` 管理，is_obsidian_sync_daemon_running
    走 systemctl is-active 检查。daemon inactive 时我们**不**尝试 systemctl start
    (webapp 进程未必有 root)，留给 systemd 自己处理或运维介入 — 直接 return False。
    """
    if is_obsidian_sync_daemon_running():
        return False
    if not WINDOWS:
        handle.update("Obsidian Sync daemon 未运行（systemd），跳过唤醒")
        return False
    handle.update("Obsidian Sync daemon 未运行,启动计划任务")
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             f"Start-ScheduledTask -TaskName '{OBSIDIAN_SYNC_TASK}'"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=_NO_WINDOW, timeout=15,
        )
    except Exception as e:
        handle.update(f"启动 daemon 失败: {e}")
        return False
    elapsed = 0
    step = 5
    while elapsed < wait_seconds:
        remain = wait_seconds - elapsed
        handle.update(f"等待 daemon 同步({remain}s 剩余)")
        time.sleep(min(step, remain))
        elapsed += step
    return True


def _extract_json(text: str) -> dict | None:
    candidates: list[str] = []
    m = re.search(r"```(?:json)?\s*\n([\s\S]*?)\n```", text)
    if m:
        candidates.append(m.group(1).strip())
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        candidates.append(m.group())
    for cand in candidates:
        # 先按原文解析（保留字符串值里的中文弯引号）
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            pass
        # 再尝试把弯引号当 JSON 分隔符替换（兜底应对 AI 误用）
        try:
            return json.loads(cand.replace("“", '"').replace("”", '"'))
        except json.JSONDecodeError:
            pass
    return None


# ── 第1步：扫描 ────────────────────────────────────────────────────────────────

def scan_pending() -> list[Path]:
    last_scan = note_state.get_last_scan("登记新笔记")
    results: list[Path] = []
    skipped_failed: list[str] = []
    for md in sorted(VAULT_ROOT.rglob("*.md")):
        if not NOTE_PATTERN.match(md.name):
            continue
        # 连续失败 ≥3 次：跳过，避免反复浪费 token
        if note_state.should_skip_failed(md, "summarize", threshold=3):
            skipped_failed.append(md.name)
            continue
        if note_state.has_record(md, "summarize"):
            if not note_state.is_unchanged(md, "summarize"):
                results.append(md)
        else:
            if last_scan is None or md.stat().st_mtime > last_scan.timestamp():
                results.append(md)
    if skipped_failed:
        print(f"跳过反复失败的笔记 {len(skipped_failed)} 篇：{', '.join(skipped_failed)}",
              file=sys.stderr)
    return results


# ── 第2步：PDF 标注 ────────────────────────────────────────────────────────────

def annotate_pdfs(note_path: Path) -> int:
    buf = io.StringIO()
    with redirect_stdout(buf):
        annotate_note.process_note(str(note_path))
    return buf.getvalue().count("OK（")


# ── 第2.5步：图片标注 ──────────────────────────────────────────────────────────

def annotate_images_step(note_path: Path) -> int:
    scan_output = _subprocess_capture([
        PYTHON, str(SCRIPTS_DIR / "annotate_images.py"),
        "scan", "--note", str(note_path),
    ])
    if not scan_output or scan_output.startswith("没有"):
        return 0
    try:
        scan_data = json.loads(scan_output)
    except json.JSONDecodeError:
        return 0

    sections = scan_data.get("sections", [])
    if not sections:
        return 0

    sections_block_parts = []
    for si, sec in enumerate(sections):
        heading = sec.get("heading") or "(无标题)"
        sections_block_parts.append(f"段落 {si}（标题：{heading}）：")
        for img in sec["images"]:
            sections_block_parts.append(f"  - 图片 {img['index']}: {img['path']}")
        sections_block_parts.append("")
    sections_block = "\n".join(sections_block_parts)

    prompt = _load_prompt("image_describe").format(sections_block=sections_block)
    response = ai_client.ask(prompt)
    result_data = _extract_json(response)
    if not result_data:
        return 0

    for i, sec_result in enumerate(result_data.get("sections", [])):
        if i < len(sections):
            sec_result["images"] = sections[i]["images"]

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    result_path = TEMP_DIR / "img_result.json"
    result_path.write_text(
        json.dumps(result_data, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    apply_output = _subprocess_capture([
        PYTHON, str(SCRIPTS_DIR / "annotate_images.py"),
        "apply", "--note", str(note_path), "--result", str(result_path),
    ])
    try:
        result_path.unlink(missing_ok=True)
    except Exception:
        pass

    m = re.search(r"(\d+)\s*张", apply_output)
    return int(m.group(1)) if m else 0


# ── 第3-4步：读取 + AI 分析 ────────────────────────────────────────────────────

def _parse_analysis(response: str) -> tuple[str, str, str]:
    kw = sm = cat = ""
    for line in response.strip().splitlines():
        line = line.strip()
        if line.startswith("KEYWORDS:"):
            kw = line[9:].strip()
        elif line.startswith("SUMMARY:"):
            sm = line[8:].strip()
        elif line.startswith("CATEGORY:"):
            cat = line[9:].strip()
    return kw, sm, cat


def ai_analyze(note_text: str, vault_structure: str) -> tuple[str, str, str]:
    prompt = _load_prompt("analyze").format(
        vault_structure=vault_structure, note_text=note_text,
    )
    return _parse_analysis(ai_client.ask(prompt))


def ai_analyze_excalidraw(png_path: str, vault_structure: str) -> tuple[str, str, str]:
    prompt = _load_prompt("analyze_excalidraw").format(
        png_path=png_path, vault_structure=vault_structure,
    )
    return _parse_analysis(ai_client.ask(prompt))


# ── 第5步：写入索引 ────────────────────────────────────────────────────────────

def write_index(note_path: str, keywords: str, summary: str, category: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        summarize_note.write_to_index(note_path, keywords, summary, category)
    return "更新" if "更新" in buf.getvalue() else "新增"


# ── 第6步：AI 查找关联 ─────────────────────────────────────────────────────────

def ai_find_related(
    note_name: str, keywords: str, category: str,
) -> list[tuple[str, str]]:
    subject = category.split("/")[0].strip()
    index_path = PROJECT_DIR / "index" / f"{subject}.md"
    if not index_path.exists():
        return []
    index_content = index_path.read_text(encoding="utf-8")

    branch_dir = PROJECT_DIR / "index" / subject
    if branch_dir.is_dir():
        for bf in sorted(branch_dir.glob("*.md")):
            index_content += "\n\n" + bf.read_text(encoding="utf-8")

    prompt = _load_prompt("find_related").format(
        note_name=note_name, keywords=keywords, index_content=index_content,
    )
    response = ai_client.ask(prompt)
    links: list[tuple[str, str]] = []
    for line in response.strip().splitlines():
        line = line.strip().lstrip("- ")
        if "|" not in line:
            continue
        name, reason = line.split("|", 1)
        name = name.strip().replace("[[", "").replace("]]", "")
        if name.lower().endswith(".md"):
            name = name[:-3]
        reason = reason.strip()
        if name and reason and name != note_name:
            links.append((name, reason[:20]))
    return links[:6]


# ── 第7步：写入正向链接 + 反向传播 ─────────────────────────────────────────────

_RELATED_HEADING_RE = re.compile(r"\n##\s+相关笔记\s*\n", re.UNICODE)
_LINK_LINE_RE       = re.compile(r"-\s+\[\[([^\]]+)\]\]\s*[—–-]?\s*(.*)")


def parse_existing_links(text: str) -> list[tuple[str, str]]:
    """从文本中解析「相关笔记」节的现有条目，返回 [(name, reason)]。"""
    m = _RELATED_HEADING_RE.search(text)
    if not m:
        return []
    section = text[m.end():]
    next_h = re.search(r"\n#{1,2}\s+", section)
    if next_h:
        section = section[:next_h.start()]
    entries: list[tuple[str, str]] = []
    for line in section.splitlines():
        s = line.strip()
        if not s.startswith("-"):
            continue
        em = _LINK_LINE_RE.match(s)
        if em:
            name = em.group(1).strip()
            if name.lower().endswith(".md"):
                name = name[:-3]
            entries.append((name, em.group(2).strip()))
    return entries


@functools.lru_cache(maxsize=2048)
def _resolve_target(target_name: str) -> Path | None:
    name = target_name.strip()
    if name.lower().endswith(".md"):
        name = name[:-3]
    if not name:
        return None
    for p in VAULT_ROOT.rglob(f"{name}.md"):
        return p
    return None


def _merge_links(
    new_items: list[tuple[str, str]],
    existing:  list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """合并：new_items 优先，已有的追加在后；按 name 去重（不区分大小写）。"""
    seen: set[str] = set()
    merged: list[tuple[str, str]] = []
    for name, reason in list(new_items) + list(existing):
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append((name, reason))
    return merged


def append_links(note_path: Path, links: list[tuple[str, str]]) -> int:
    """写入正向链接；connect_note.update_note 默认 merge 模式自动保留已有条目。"""
    with open(note_path, encoding="utf-8") as f:
        content = f.read()
    items = connect_note.dedupe_links(links)
    new_content, _ = connect_note.update_note(content, items, mode="merge")
    with open(note_path, "w", encoding="utf-8") as f:
        f.write(new_content)
    note_state.mark_processed(note_path, "connect")
    # 实际写入条目数 = 现有 + 新增去重后
    return len(connect_note.parse_existing_items(new_content))


def add_back_link(target_path: Path, source_name: str, reason: str) -> bool:
    """向 target 笔记追加一条指向 source 的反向链接。幂等：已存在则不动。"""
    if not target_path.exists():
        return False
    try:
        text = target_path.read_text(encoding="utf-8")
    except OSError:
        return False
    existing = parse_existing_links(text)
    if any(n.lower() == source_name.lower() for n, _ in existing):
        return False
    new_links = existing + [(source_name, reason)]
    new_text, _ = connect_note.update_note(text, new_links)
    target_path.write_text(new_text, encoding="utf-8")
    return True


def propagate_back_links(source_path: Path) -> int:
    """遍历 source 的「相关笔记」节，向每个目标笔记追加反向链接。"""
    try:
        text = source_path.read_text(encoding="utf-8")
    except OSError:
        return 0
    forward = parse_existing_links(text)
    source_name = source_path.stem
    source_resolved = source_path.resolve()
    added = 0
    for target_name, reason in forward:
        target_path = _resolve_target(target_name)
        if target_path is None or target_path.resolve() == source_resolved:
            continue
        if add_back_link(target_path, source_name, reason):
            added += 1
    return added


# ── 第8步：Anki 制卡 ──────────────────────────────────────────────────────────

def anki_step(note_path: Path) -> str:
    context = _subprocess_capture([
        PYTHON, str(SCRIPTS_DIR / "anki_from_note.py"),
        "--read", str(note_path),
    ])
    if "pending_sections: []" in context:
        return "跳过"

    prompt = _load_prompt("anki_cards").format(context=context)
    response = ai_client.ask(prompt)
    cards_data = _extract_json(response)
    if not cards_data:
        return "JSON解析失败"

    cards_path = PROJECT_DIR / "anki" / "tmp_cards.json"
    cards_path.parent.mkdir(parents=True, exist_ok=True)
    cards_path.write_text(
        json.dumps(cards_data, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    sync_output = _subprocess_capture([
        PYTHON, str(SCRIPTS_DIR / "anki_from_note.py"),
        "--sync", str(note_path), "--cards-json", str(cards_path),
    ])
    try:
        cards_path.unlink(missing_ok=True)
    except Exception:
        pass

    if "no_cards" in sync_output:
        return "不制卡"
    ok_count = sync_output.count("OK ")
    fail_count = sync_output.count("FAIL ")
    if ok_count or fail_count:
        s = f"{ok_count}张"
        if fail_count:
            s += f"({fail_count}失败)"
        return s
    return "已同步"


# ── 单篇处理 ──────────────────────────────────────────────────────────────────

class _NullHandle:
    def update(self, msg: str) -> None:
        pass

    def set_progress(self, current: int, total: int) -> None:
        pass

    def log(self, msg: str) -> None:
        pass


def process_note(
    note_path: Path,
    handle: Any,
    idx: int,
    total: int,
    vault_structure: str,
) -> dict[str, Any]:
    prefix = f"[{idx}/{total}] {note_path.stem}"
    result: dict[str, Any] = {
        "name": note_path.name, "pdf": 0, "images": 0,
        "index": "", "links": 0, "back_links": 0, "anki": "", "time": 0,
    }
    start = time.time()
    is_exc = summarize_note.is_excalidraw(str(note_path))

    # ── PDF 标注 ──
    if not is_exc:
        handle.update(f"{prefix}: PDF 标注")
        try:
            result["pdf"] = annotate_pdfs(note_path)
        except Exception:
            pass

    # ── 图片标注 ──
    if not is_exc:
        handle.update(f"{prefix}: 图片标注")
        try:
            result["images"] = annotate_images_step(note_path)
        except Exception:
            pass

    # ── 读取 + AI 分析 ──
    handle.update(f"{prefix}: AI 分析")
    note_text = summarize_note.clean_note(str(note_path))

    if is_exc and note_text.startswith("TYPE: excalidraw"):
        png_lines = [l for l in note_text.splitlines() if l.startswith("PNG:")]
        png_path = png_lines[0].split(":", 1)[1].strip() if png_lines else "none"
        if png_path == "none":
            result["index"] = "Excalidraw无图"
            result["time"] = int(time.time() - start)
            return result
        keywords, summary, category = ai_analyze_excalidraw(png_path, vault_structure)
    else:
        keywords, summary, category = ai_analyze(note_text, vault_structure)

    if not keywords or not summary or not category:
        result["index"] = "分析失败"
        note_state.mark_failed(note_path, "summarize", "AI 未返回完整 KEYWORDS/SUMMARY/CATEGORY")
        result["time"] = int(time.time() - start)
        return result

    # ── 写入索引 ──
    handle.update(f"{prefix}: 写入索引")
    try:
        result["index"] = write_index(str(note_path), keywords, summary, category)
    except Exception as e:
        result["index"] = f"失败:{e}"
        note_state.mark_failed(note_path, "summarize", f"write_index: {e}")
        result["time"] = int(time.time() - start)
        return result

    # ── 查找关联 + 写入正向链接 + 反向传播 ──
    # 已有 connect 记录 → 该笔记的正向链接已 AI 评估过，不再重新判断；
    # 但仍允许其他更新的笔记把反向链接补到本笔记节里（由它们各自的 propagate 完成）。
    if note_state.has_record(note_path, "connect"):
        result["links"] = "保留"
    else:
        handle.update(f"{prefix}: 查找关联")
        try:
            links = ai_find_related(note_path.stem, keywords, category)
        except Exception:
            links = []
        if links:
            handle.update(f"{prefix}: 写入正向链接")
            try:
                result["links"] = append_links(note_path, links)
            except Exception:
                pass
            handle.update(f"{prefix}: 反向传播")
            try:
                result["back_links"] = propagate_back_links(note_path)
            except Exception:
                pass

    # ── Anki 制卡 ──
    handle.update(f"{prefix}: Anki 制卡")
    try:
        result["anki"] = anki_step(note_path)
    except Exception as e:
        result["anki"] = f"失败:{e}"

    # ── 标记已处理 ──
    note_state.mark_processed(note_path, "summarize")

    result["time"] = int(time.time() - start)
    return result


# ── 入口 ──────────────────────────────────────────────────────────────────────

def run(handle: Any = None) -> list[dict[str, Any]]:
    if handle is None:
        handle = _NullHandle()

    # 启动后停留一下，让监视窗口（500ms 轮询）能捕获到任务
    handle.update("启动中")
    time.sleep(1.0)

    # 远程触发场景:Obsidian 没开则拉起并等同步,避免读到旧 vault
    ensure_obsidian_synced(handle)

    handle.update("扫描待处理笔记")
    notes = scan_pending()

    if not notes:
        handle.update("无待处理笔记，3 秒后退出")
        if hasattr(handle, "set_summary"):
            handle.set_summary("无待处理笔记")
        time.sleep(3.0)  # 让用户看到结果
        return []

    handle.set_progress(0, len(notes))
    handle.update(f"共 {len(notes)} 篇待处理")
    vault_structure = _load_vault_structure()

    results: list[dict[str, Any]] = []
    for i, note_path in enumerate(notes, 1):
        result = process_note(note_path, handle, i, len(notes), vault_structure)
        results.append(result)
        summary = _format_summary(result)
        handle.log(summary)
        handle.set_progress(i, len(notes))

    note_state.update_last_scan("登记新笔记")

    total_time = sum(r["time"] for r in results)
    ok = sum(1 for r in results if r.get("index") in ("新增", "更新"))
    failed = len(results) - ok
    summary = f"完成 {ok}/{len(results)} 篇" + (f"（{failed} 失败）" if failed else "") + f"  总耗时 {total_time}s"
    handle.update(summary)
    if hasattr(handle, "set_summary"):
        handle.set_summary(summary)
    return results


def _format_summary(r: dict[str, Any]) -> str:
    name = r["name"]
    if name.lower().endswith(".md"):
        name = name[:-3]
    parts = [f"✓ {name} ({r['time']}s)"]
    extras = []
    if r["index"]:
        extras.append(f"索引:{r['index']}")
    if r["links"]:
        extras.append(f"链接:{r['links']}")
    if r.get("back_links"):
        extras.append(f"反向:{r['back_links']}")
    if r["anki"]:
        extras.append(f"Anki:{r['anki']}")
    if extras:
        parts.append(" ".join(extras))
    return " | ".join(parts)


# ── 单步执行：供 /summarize /connect /anki /img-mark /pdf-mark 等 skill 调用 ──

def run_step(note_path: Path, step: str, handle: Any = None) -> dict[str, Any]:
    """对指定笔记只执行某一步骤。step ∈ {pdf, img, summarize, connect, anki, all}。"""
    if handle is None:
        handle = _NullHandle()
    note_path = note_path.resolve()
    name = note_path.stem
    result: dict[str, Any] = {
        "name": note_path.name, "pdf": 0, "images": 0,
        "index": "", "links": 0, "back_links": 0, "anki": "", "time": 0,
    }
    start = time.time()
    is_exc = summarize_note.is_excalidraw(str(note_path))
    vault_structure = _load_vault_structure()

    def do_pdf():
        if is_exc: return
        handle.update(f"{name}: PDF 标注")
        result["pdf"] = annotate_pdfs(note_path)

    def do_img():
        if is_exc: return
        handle.update(f"{name}: 图片标注")
        result["images"] = annotate_images_step(note_path)

    def do_summarize() -> tuple[str, str, str] | None:
        handle.update(f"{name}: AI 分析")
        note_text = summarize_note.clean_note(str(note_path))
        if is_exc and note_text.startswith("TYPE: excalidraw"):
            png_lines = [l for l in note_text.splitlines() if l.startswith("PNG:")]
            png_path = png_lines[0].split(":", 1)[1].strip() if png_lines else "none"
            if png_path == "none":
                result["index"] = "Excalidraw无图"
                return None
            kw, sm, cat = ai_analyze_excalidraw(png_path, vault_structure)
        else:
            kw, sm, cat = ai_analyze(note_text, vault_structure)
        if not (kw and sm and cat):
            result["index"] = "分析失败"
            return None
        handle.update(f"{name}: 写入索引")
        result["index"] = write_index(str(note_path), kw, sm, cat)
        return kw, sm, cat

    def do_connect():
        # 读 frontmatter 拿 keywords + category（用 summarize_note 的逻辑）
        kw_cat = _read_keywords_category(note_path)
        if not kw_cat:
            result["links"] = "无frontmatter"
            return
        keywords, category = kw_cat
        handle.update(f"{name}: 查找关联")
        try:
            links = ai_find_related(name, keywords, category)
        except Exception:
            links = []
        if links:
            handle.update(f"{name}: 写入正向链接")
            result["links"] = append_links(note_path, links)
            handle.update(f"{name}: 反向传播")
            result["back_links"] = propagate_back_links(note_path)

    def do_anki():
        handle.update(f"{name}: Anki 制卡")
        result["anki"] = anki_step(note_path)

    if step in ("pdf", "all"):       do_pdf()
    if step in ("img", "all"):       do_img()
    if step in ("summarize", "all"): do_summarize()
    if step in ("connect", "all"):   do_connect()
    if step in ("anki", "all"):      do_anki()

    if step == "all":
        note_state.mark_processed(note_path, "summarize")

    result["time"] = int(time.time() - start)
    return result


def _read_keywords_category(note_path: Path) -> tuple[str, str] | None:
    r"""从知识索引中查找该笔记的条目，提取 keywords 和 category。

    索引条目格式：`- [[文件名]] \`kw1, kw2, kw3\` — 摘要`
    遍历 `index/科目.md` 和 `index/科目/分支.md`，找到匹配的条目，
    返回 (keywords, '科目/分支')。
    """
    note_name = note_path.stem
    index_root = config.INDEX_DIR
    if not index_root.exists():
        return None

    entry_re = re.compile(
        r"-\s*\[\[" + re.escape(note_name) + r"\]\]\s*`([^`]+)`",
        re.UNICODE,
    )

    for index_file in sorted(index_root.rglob("*.md")):
        if index_file.name == "knowledge-index.md":
            continue
        try:
            text = index_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = entry_re.search(text)
        if not m:
            continue
        keywords = m.group(1).strip()

        # 推断 category
        rel = index_file.relative_to(index_root)
        if len(rel.parts) == 1:
            # index/科目.md → 找对应的 ### 分支 标题
            subject = rel.stem
            entry_idx = m.start()
            preceding = text[:entry_idx]
            heading_m = list(re.finditer(r"\n###\s+(.+?)\s*\n", preceding))
            branch = heading_m[-1].group(1).strip() if heading_m else "其他"
        else:
            # index/科目/分支.md
            subject = rel.parts[0]
            branch = rel.stem
        return keywords, f"{subject}/{branch}"

    return None


def _print_result(r: dict[str, Any]) -> None:
    print(
        f"{r['name']} | PDF:{r['pdf']} 图片:{r['images']} | "
        f"索引:{r['index']} | 链接:{r['links']} 反向:{r.get('back_links', 0)} | "
        f"Anki:{r['anki']} | {r['time']}s"
    )


def main():
    import argparse
    import task_tracker

    p = argparse.ArgumentParser(description="笔记登记编排（批量或单篇）")
    p.add_argument("--note", help="只处理指定笔记的路径")
    p.add_argument(
        "--only", choices=("pdf", "img", "summarize", "connect", "anki", "all"),
        default="all",
        help="只运行某一步骤；默认 all（完整流程）",
    )
    args = p.parse_args()

    if args.note:
        note_path = Path(args.note).resolve()
        if not note_path.exists():
            print(f"ERROR: 笔记不存在：{note_path}", file=sys.stderr)
            sys.exit(1)
        with task_tracker.track(f"register_notes:{args.only}") as h:
            r = run_step(note_path, args.only, h)
        _print_result(r)
        return

    with task_tracker.track("登记新笔记") as h:
        results = run(h)
    for r in results:
        _print_result(r)


if __name__ == "__main__":
    main()
