"""
Anki 制卡机械脚本（agent 驱动版）。

AI 判断与卡片生成由 Claude Code / Codex 在脚本外完成；本脚本只负责：
  1. 批量收集待处理 md，并按 records JSON 判断是否跳过
  2. 输出单篇笔记 + references 的制卡上下文，供外层 agent 阅读
  3. 接收外层 agent 生成的卡片 JSON
  4. 调用 AnkiConnect 创建卡片
  5. 将 Anki 返回的 note_id 写入独立 records JSON

原始 md 文件不会被修改。

用法：
  # 列出待处理笔记
  python scripts/anki_from_note.py --list --dir <目录> --recursive

  # 输出某篇笔记的制卡上下文，给 Claude/Codex 判断
  python scripts/anki_from_note.py --read <笔记路径>

  # 同步 Claude/Codex 生成的卡片 JSON
  python scripts/anki_from_note.py --sync <笔记路径> --cards-json <cards.json>

  # 记录“无需制卡”，避免下次重复消耗 token
  python scripts/anki_from_note.py --sync <笔记路径> --no-cards-reason "主要是学习计划"
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import note_state

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

PROJECT_DIR = Path(__file__).resolve().parents[1]
REFERENCES_DIR = PROJECT_DIR / "references"
DEFAULT_RECORDS_DIR = PROJECT_DIR / "anki" / "records"
DEFAULT_VAULT_ROOT = Path(os.environ.get("OBSIDIAN_VAULT", r"C:\obsidian"))
DEFAULT_VAULT_NAME = "Obsidian Vault"
DEFAULT_ANKI_URL = os.environ.get("ANKI_CONNECT_URL", "http://127.0.0.1:8765")

DEFAULT_DECK = "Obsidian::未分类"
DEFAULT_BASIC_MODEL = "Obsidian-basic"
DEFAULT_REVERSE_MODEL = "Obsidian-basic-reversed"
DEFAULT_CLOZE_MODEL = "Obsidian-cloze"
VALID_CARD_TYPES = {"basic", "cloze", "reverse", "problem"}

EXPECTED_JSON = {
    "should_create_cards": True,
    "reason": "为什么这篇笔记值得或不值得制卡",
    "sections_processed": ["节名1", "节名2"],
    "cards": [
        {
            "type": "basic | cloze | reverse | problem",
            "deck": "数学::线性代数",
            "front": "非 cloze 卡的问题；cloze 卡留空",
            "back": "答案；cloze 卡可作为补充解释",
            "text": "仅 cloze 卡使用，必须包含 {{c1::...}}",
            "reason": "这张卡的制卡理由",
            "tags": ["数学", "线性代数"],
        }
    ],
}


# ── Excalidraw 检测 ───────────────────────────────────────────────────────────

def is_excalidraw(path: Path) -> bool:
    try:
        return "excalidraw-plugin:" in path.read_text(encoding="utf-8", errors="ignore")[:512]
    except OSError:
        return False


def find_excalidraw_image(path: Path) -> Path | None:
    for ext in [".png", ".svg"]:
        img = path.with_suffix(ext)
        if img.exists():
            return img
    return None


# ── 通用工具 ──────────────────────────────────────────────────────────────────

def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def now_local() -> dt.datetime:
    return dt.datetime.now().astimezone()


def batch_id(ts: dt.datetime) -> str:
    return ts.strftime("%Y%m%d%H%M%S")


def clean_note(text: str, max_chars: int) -> tuple[str, bool]:
    """去掉明显噪声，保留正文和 PDF 注释中的原文内容。"""
    text = re.sub(r"^---\s*\n.*?\n---\s*\n", "", text, flags=re.DOTALL)
    text = re.sub(r"!\[\[.+?\]\]\n?", "", text)
    text = re.sub(r"\n*---\n## 相关笔记\n.*", "", text, flags=re.DOTALL)
    text = re.sub(r"^> \[!.*?\].*\n", "", text, flags=re.MULTILINE)
    text = re.sub(r"^> ", "", text, flags=re.MULTILINE)
    text = re.sub(r"- \[.\] ", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    truncated = False
    if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n\n[内容过长，脚本已截断到 max-chars 限制。]"
        truncated = True
    return text, truncated


_AUTO_MANAGED_HEADINGS = {"相关笔记"}


def split_sections(text: str) -> list[tuple[str, str]]:
    """按 # 标题切分文本，返回 (标题, 内容) 列表。
    自动跳过「相关笔记」等自动管理的节（它们由 connect 流程维护，不参与制卡判断）。"""
    sections: list[tuple[str, str]] = []
    current_heading = ""
    current_lines: list[str] = []
    for line in text.splitlines(keepends=True):
        m = re.match(r'^#{1,6}\s+(.+)', line)
        if m:
            content = "".join(current_lines)
            if current_heading or content.strip():
                sections.append((current_heading, content))
            current_heading = m.group(1).strip()
            current_lines = []
        else:
            current_lines.append(line)
    content = "".join(current_lines)
    if current_heading or content.strip():
        sections.append((current_heading, content))
    return [(h, c) for h, c in sections if h not in _AUTO_MANAGED_HEADINGS]


def section_hash(content: str) -> str:
    return hashlib.sha256(content.strip().encode("utf-8")).hexdigest()[:16]


def load_record_data(record_path: Path) -> dict[str, Any]:
    if not record_path.exists():
        return {}
    try:
        return json.loads(record_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def find_pending_sections(
    sections: list[tuple[str, str]],
    stored_hashes: dict[str, str],
) -> list[tuple[str, str]]:
    """返回哈希变动或新增的节。"""
    return [(h, c) for h, c in sections if stored_hashes.get(h) != section_hash(c)]


def resolve_existing_file(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.exists():
        raise ValueError(f"文件不存在：{value}")
    if not path.is_file():
        raise ValueError(f"不是文件：{value}")
    if path.suffix.lower() != ".md":
        raise ValueError(f"不是 md 文件：{value}")
    return path.resolve()


def safe_record_stem(source_note: str) -> str:
    if source_note.lower().endswith(".md"):
        source_note = source_note[:-3]
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "__", source_note)
    value = re.sub(r"__+", "__", value).strip(" ._")
    return value or "note"


def source_note_path(note_path: Path, vault_root: Path) -> str:
    note_resolved = note_path.resolve()
    vault_resolved = vault_root.resolve()
    try:
        return note_resolved.relative_to(vault_resolved).as_posix()
    except ValueError:
        return str(note_resolved)


def record_path_for(note_path: Path, vault_root: Path, records_dir: Path) -> Path:
    source_note = source_note_path(note_path, vault_root)
    return records_dir / f"{safe_record_stem(source_note)}.json"


def source_link_for(note_path: Path) -> str:
    return f"[[{note_path.stem}]]"


def obsidian_url(vault_name: str, source_note: str) -> str:
    file_path = source_note.replace("\\", "/")
    if file_path.lower().endswith(".md"):
        file_path = file_path[:-3]
    return (
        "obsidian://open?vault="
        + urllib.parse.quote(vault_name, safe="")
        + "&file="
        + urllib.parse.quote(file_path, safe="")
    )


def sanitize_tag(tag: str) -> str:
    tag = tag.strip().lstrip("#")
    tag = re.sub(r"\s+", "_", tag)
    tag = re.sub(r"[,;，；]+", "_", tag)
    return tag.strip("_")


_MATH_RE = re.compile(r"\\\(.*?\\\)|\\\[.*?\\\]", re.S)


def html_text(text: str) -> str:
    """转 HTML：普通文本 escape + 换行→<br>；LaTeX 区段 \\(...\\) \\[...\\]
    原样保留——否则其中的 < > & " 被 html.escape 转义，MathJax 渲染失败
    （不等式、矩阵 & 列分隔、\\text 里的引号等全会坏）。"""
    text = text.strip()
    out: list[str] = []
    last = 0
    for m in _MATH_RE.finditer(text):
        out.append(html.escape(text[last:m.start()]).replace("\n", "<br>"))
        out.append(m.group(0))            # LaTeX 原样交给 MathJax
        last = m.end()
    out.append(html.escape(text[last:]).replace("\n", "<br>"))
    return "".join(out)


# ── 文件收集与 reference ─────────────────────────────────────────────────────

def collect_notes(args: argparse.Namespace) -> list[Path]:
    values: list[str] = []
    values.extend(args.note or [])
    values.extend(args.notes or [])

    paths: list[Path] = []
    for value in values:
        paths.append(resolve_existing_file(value))

    for dir_value in args.dir or []:
        directory = Path(dir_value).expanduser()
        if not directory.exists():
            raise ValueError(f"目录不存在：{dir_value}")
        if not directory.is_dir():
            raise ValueError(f"不是目录：{dir_value}")
        iterator = directory.rglob("*.md") if args.recursive else directory.glob("*.md")
        paths.extend(path.resolve() for path in iterator if path.is_file())

    deduped: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path).casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return sorted(deduped, key=lambda p: str(p).casefold())


def load_references() -> dict[str, str]:
    return {
        "selection": read_text(REFERENCES_DIR / "anki-selection-rules.md"),
        "format": read_text(REFERENCES_DIR / "anki-card-format.md"),
        "vault": read_text(REFERENCES_DIR / "vault-structure.md"),
    }


def pending_note_rows(notes: list[Path], args: argparse.Namespace) -> list[dict[str, Any]]:
    rows = []
    for note_path in notes:
        record_path = record_path_for(note_path, args.vault_root, args.records_dir)
        exists = record_path.exists()
        if exists and not args.force:
            if note_state.is_unchanged(note_path, "anki"):
                status = "skipped"
            else:
                status = "changed"  # record exists but content has been updated
        else:
            status = "pending"
        rows.append(
            {
                "status": status,
                "note": str(note_path),
                "source_note": source_note_path(note_path, args.vault_root),
                "record": str(record_path),
                "record_exists": exists,
            }
        )
    return rows


# ── AnkiConnect ──────────────────────────────────────────────────────────────

def post_json(url: str, payload: dict[str, Any], timeout: int = 60) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(str(e.reason)) from e
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"返回内容不是 JSON：{body[:500]}") from e


def anki_request(anki_url: str, action: str, params: dict[str, Any] | None = None, timeout: int = 15) -> Any:
    payload = {"action": action, "version": 6}
    if params is not None:
        payload["params"] = params
    result = post_json(anki_url, payload, timeout=timeout)
    if result.get("error"):
        raise RuntimeError(str(result["error"]))
    return result.get("result")


_ANKI_LOG = PROJECT_DIR / "state" / "logs" / "anki_launch.log"


def _anki_log(msg: str) -> None:
    """写诊断日志到 state/logs/anki_launch.log（每行带时间戳，1MB 滚动）。"""
    try:
        _ANKI_LOG.parent.mkdir(parents=True, exist_ok=True)
        if _ANKI_LOG.exists() and _ANKI_LOG.stat().st_size > 1_000_000:
            bak = _ANKI_LOG.with_suffix(".log.1")
            bak.unlink(missing_ok=True)
            _ANKI_LOG.replace(bak)
        with _ANKI_LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{dt.datetime.now().isoformat(timespec='seconds')}] {msg}\n")
    except OSError:
        pass


def is_anki_running() -> bool:
    """检查是否已有 anki.exe 进程在跑。"""
    if os.name != "nt":
        return False
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq anki.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            startupinfo=si, creationflags=subprocess.CREATE_NO_WINDOW, timeout=10,
        )
        return "anki.exe" in (r.stdout or "").lower()
    except Exception:
        return False


def open_anki() -> bool:
    """启动 Anki。返回是否成功执行了启动命令。

    所有路径都加 CREATE_NO_WINDOW，避免被 GUI 父进程（如 bwicarus-client.exe）
    调用时弹空白终端窗口。
    """
    import shutil
    no_win_flags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )
    candidates = [
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Anki\anki.exe"),
        r"C:\Program Files\Anki\anki.exe",
        os.path.expandvars(r"%USERPROFILE%\scoop\apps\anki\current\anki.exe"),
    ]
    # PATH 兜底（如果 anki 加进了 PATH）
    which = shutil.which("anki") or shutil.which("anki.exe")
    if which:
        candidates.append(which)
    for path in candidates:
        if path and os.path.exists(path):
            try:
                subprocess.Popen(
                    [path],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=no_win_flags,
                )
                _anki_log(f"已启动 Anki: {path}")
                return True
            except OSError as e:
                _anki_log(f"启动失败 {path}: {e}")
    _anki_log("未找到 anki.exe，无法启动 Anki")
    return False


def wait_for_anki(anki_url: str, wait_seconds: int) -> int:
    """轮询 AnkiConnect；未启动时尝试启动，每 30 秒最多重试一次。"""
    deadline = time.time() + wait_seconds
    last_launch_at = 0.0
    launch_attempts = 0
    LAUNCH_INTERVAL = 30

    running = is_anki_running()
    _anki_log(f"开始等待 AnkiConnect: timeout={wait_seconds}s, anki.exe running={running}")

    while True:
        try:
            version = anki_request(anki_url, "version", timeout=5)
            _anki_log(f"AnkiConnect OK: version {version}, 用时 {int(time.time() - (deadline - wait_seconds))}s")
            print(f"AnkiConnect OK: version {version}")
            return int(version)
        except RuntimeError as e:
            now = time.time()
            if now >= deadline:
                _anki_log(
                    f"超时退出 ({wait_seconds}s 未上线)；err={str(e)[:80]}; "
                    f"anki.exe running={is_anki_running()}; 启动尝试={launch_attempts}"
                )
                raise RuntimeError(
                    f"未检测到 AnkiConnect（等待 {wait_seconds}s）：{e}\n"
                    f"诊断日志：{_ANKI_LOG}"
                ) from e

            if not is_anki_running() and (now - last_launch_at) > LAUNCH_INTERVAL:
                _anki_log(f"anki.exe 未运行，尝试启动（第 {launch_attempts + 1} 次）")
                print("AnkiConnect 未响应，尝试启动 Anki...")
                if open_anki():
                    launch_attempts += 1
                    last_launch_at = now
                else:
                    _anki_log("找不到 anki.exe，无法启动")
            else:
                # Anki 在跑但 AnkiConnect 还没响应 → 耐心等
                print("等待 AnkiConnect 上线...")
            time.sleep(3)


# ── 卡片 JSON 处理 ───────────────────────────────────────────────────────────

def normalize_json_quotes(text: str) -> str:
    """把中文弯引号替换为 JSON 兼容的直引号。"""
    return text.replace("“", '"').replace("”", '"')


def load_cards_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.cards_json:
        path = Path(args.cards_json).expanduser()
        return json.loads(normalize_json_quotes(read_text(path)))
    if args.cards:
        return json.loads(normalize_json_quotes(args.cards))
    if args.cards_stdin:
        return json.loads(normalize_json_quotes(sys.stdin.read()))
    if args.no_cards_reason:
        return {"should_create_cards": False, "reason": args.no_cards_reason, "cards": []}
    raise ValueError("同步时需要 --cards-json、--cards、--cards-stdin 或 --no-cards-reason")


def normalize_cards(result: dict[str, Any], max_cards: int) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    cards: list[dict[str, Any]] = []

    for index, raw_card in enumerate(result.get("cards", []), start=1):
        if not isinstance(raw_card, dict):
            warnings.append(f"第 {index} 张卡不是对象，已跳过")
            continue

        card_type = str(raw_card.get("type", "")).strip().lower()
        if card_type not in VALID_CARD_TYPES:
            warnings.append(f"第 {index} 张卡类型无效：{card_type or '空'}，已跳过")
            continue

        deck = str(raw_card.get("deck", "")).strip() or DEFAULT_DECK
        front = str(raw_card.get("front", "")).strip()
        back = str(raw_card.get("back", "")).strip()
        text = str(raw_card.get("text", "")).strip()
        reason = str(raw_card.get("reason", "")).strip() or "未说明"
        tags = raw_card.get("tags", [])
        if not isinstance(tags, list):
            tags = []
        tags = [tag for tag in (sanitize_tag(str(tag)) for tag in tags) if tag]

        if card_type == "cloze":
            if not text or "{{c" not in text:
                warnings.append(f"第 {index} 张 cloze 卡缺少有效挖空，已跳过")
                continue
        elif not front or not back:
            warnings.append(f"第 {index} 张 {card_type} 卡缺少 front/back，已跳过")
            continue

        cards.append(
            {
                "type": card_type,
                "deck": deck,
                "front": front,
                "back": back,
                "text": text,
                "reason": reason,
                "tags": tags,
            }
        )

    if max_cards > 0 and len(cards) > max_cards:
        warnings.append(f"输入包含 {len(cards)} 张卡，已截断为前 {max_cards} 张")
        cards = cards[:max_cards]

    return cards, warnings


def no_cards_record(
    source_note: str,
    source_link: str,
    generated_at: dt.datetime,
    reason: str,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "source_note": source_note,
        "source_link": source_link,
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "generator": "agent",
        "status": "no_cards",
        "reason": reason,
        "warnings": warnings or [],
        "cards": [],
    }


# ── Anki 同步 ─────────────────────────────────────────────────────────────────

def source_footer(source_link: str, source_url: str, reason: str, local_id: str) -> str:
    lines = [
        f'来源：<a href="{html.escape(source_url, quote=True)}">{html.escape(source_link)}</a>',
        f"原因：{html.escape(reason)}",
        f"Local ID：{html.escape(local_id)}",
    ]
    qa = os.environ.get("QA_PUBLIC_URL", "").rstrip("/")
    if qa:
        lines.append(f'<a href="{qa}/?card={local_id}">问 AI / 改进这张卡</a>')
    # ★机器可读来源(HTML 注释,不显示;注意力画像 read_material 用 obsidian_to_ref 归一成 material ref)。
    #   用户实锤:制卡自动注入的来源要**可迅速提取**(原来只有 <a> 显示格式,机器提取要解析 URL)。
    src_marker = "<!--@src:%s-->" % source_link if source_link else ""
    return '<hr><div style="font-size:0.85em;color:#666;">' + "<br>".join(lines) + "</div>" + src_marker


def build_anki_note(
    card: dict[str, Any],
    local_id: str,
    source_link: str,
    source_url: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    card_type = card["type"]
    footer = source_footer(source_link, source_url, card["reason"], local_id)
    tags = ["obsidian", "ai_generated", *card["tags"]]

    if card_type == "cloze":
        model_name = args.cloze_model
        extra = html_text(card["back"])
        fields = {
            "Text": html_text(card["text"]),
            "Extra": (extra + footer) if extra else footer,
        }
    else:
        model_name = args.reverse_model if card_type == "reverse" else args.basic_model
        fields = {
            "Front": html_text(card["front"]),
            "Back": html_text(card["back"]) + footer,
        }

    return {
        "deckName": card["deck"],
        "modelName": model_name,
        "fields": fields,
        "options": {"allowDuplicate": False},
        "tags": tags,
    }


def sync_cards_to_anki(
    cards: list[dict[str, Any]],
    source_link: str,
    source_url: str,
    started_at: dt.datetime,
    args: argparse.Namespace,
) -> tuple[list[dict[str, Any]], str]:
    records: list[dict[str, Any]] = []
    failed = 0
    id_prefix = batch_id(started_at)

    for index, card in enumerate(cards, start=1):
        local_id = f"{id_prefix}-{index:03d}"
        card_record = {"local_id": local_id, **card}
        try:
            anki_request(args.anki_url, "createDeck", {"deck": card["deck"]})
            note = build_anki_note(card, local_id, source_link, source_url, args)
            note_id = anki_request(args.anki_url, "addNote", {"note": note}, timeout=30)
            # AnkiConnect × Anki 25:addNote 的 deckName 不生效(插件靠 notetype 缓存传 did,而
            # startEditing()→requireReset()→mw.reset() 先把缓存清了)→ 卡落「系统默认」。显式归位。
            if note_id:
                try:
                    cids = anki_request(args.anki_url, "findCards",
                                        {"query": f"nid:{note_id}"}, timeout=20) or []
                    if cids:
                        anki_request(args.anki_url, "changeDeck",
                                     {"cards": cids, "deck": card["deck"]}, timeout=20)
                except Exception:
                    pass
            card_record["anki_note_id"] = note_id
            card_record["status"] = "synced"
            print(f"  OK {local_id} → Anki note {note_id}")
        except RuntimeError as e:
            failed += 1
            card_record["anki_note_id"] = None
            card_record["status"] = "failed"
            card_record["error"] = str(e)
            print(f"  FAIL {local_id}: {e}")
        records.append(card_record)

    if failed == 0:
        return records, "synced"
    if failed == len(records):
        return records, "failed"
    return records, "partial_failed"


# ── 模式实现 ─────────────────────────────────────────────────────────────────

def run_list(args: argparse.Namespace) -> None:
    notes = collect_notes(args)
    rows = pending_note_rows(notes, args)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return

    pending = [row for row in rows if row["status"] in ("pending", "changed")]
    skipped = [row for row in rows if row["status"] == "skipped"]
    print(f"待处理 {len(pending)} 个，跳过 {len(skipped)} 个。")
    for row in pending:
        marker = "[更新]" if row["status"] == "changed" else "[新增]"
        print(f"{marker} {row['note']}")


def run_read(args: argparse.Namespace) -> None:
    note_path = resolve_existing_file(args.read)
    record_path = record_path_for(note_path, args.vault_root, args.records_dir)
    references = load_references()
    source_note = source_note_path(note_path, args.vault_root)
    source_link = source_link_for(note_path)

    # Excalidraw 绘图笔记：用整体文件哈希作为单一 "(visual)" 节
    if is_excalidraw(note_path):
        img = find_excalidraw_image(note_path)
        raw_text = read_text(note_path)
        vis_hash = section_hash(raw_text)
        record_data = load_record_data(record_path)
        stored_hashes: dict[str, str] = record_data.get("section_hashes", {})
        record_exists = record_path.exists()

        if record_exists and not args.force and stored_hashes.get("(visual)") == vis_hash:
            print("# Anki 制卡上下文")
            print()
            print("- record_exists: True")
            print("- pending_sections: []")
            print()
            print("绘图内容未变更，无需重新制卡。")
            return

        excalidraw_expected = dict(EXPECTED_JSON)
        excalidraw_expected["sections_processed"] = ["(visual)"]

        print("# Anki 制卡上下文")
        print()
        print("TYPE: excalidraw")
        print(f"PNG: {img or 'none'}")
        print()
        print("这是一篇 Excalidraw 绘图笔记。请用 Read 工具查看上方 PNG 路径的图片，")
        print("根据图片内容判断是否值得制卡，并生成卡片。")
        print("sections_processed 固定填 [\"(visual)\"]。")
        print()
        print("## 元数据")
        print(f"- note: {note_path}")
        print(f"- source_note: {source_note}")
        print(f"- source_link: {source_link}")
        print(f"- record: {record_path}")
        print(f'- pending_sections: ["(visual)"]')
        print(f"- record_exists: {record_exists}")
        print()
        print("## expected_json")
        print("```json")
        print(json.dumps(excalidraw_expected, ensure_ascii=False, indent=2))
        print("```")
        print()
        print("## reference: anki-selection-rules.md")
        print(references["selection"])
        print()
        print("## reference: anki-card-format.md")
        print(references["format"])
        return

    raw_text = read_text(note_path)
    note_text, truncated = clean_note(raw_text, args.max_chars)

    # 节级哈希追踪
    raw_sections = split_sections(raw_text)
    record_data = load_record_data(record_path)
    stored_hashes = record_data.get("section_hashes", {})
    record_exists = record_path.exists()

    if not record_exists or args.force:
        # 新笔记或强制模式：所有节均待处理
        pending = raw_sections
    elif stored_hashes:
        # 有节级追踪记录：只处理变动节
        pending = find_pending_sections(raw_sections, stored_hashes)
    else:
        # 旧 record（无节哈希）：跳过，保持向后兼容
        pending = []

    pending_names = [h for h, _ in pending]

    if not pending:
        print("# Anki 制卡上下文")
        print()
        print(f"- record_exists: {record_exists}")
        print(f"- pending_sections: []")
        print()
        print("所有节内容均未变更，无需重新制卡。")
        return

    # 从已清理的笔记文本中取出待处理节的内容
    cleaned_map = {h: c for h, c in split_sections(note_text)}
    display_parts: list[str] = []
    for name in pending_names:
        content = cleaned_map.get(name, "").rstrip()
        display_parts.append(f"# {name}\n{content}" if name else content)
    display_text = "\n\n".join(display_parts)

    package = {
        "note": str(note_path),
        "source_note": source_note,
        "source_link": source_link,
        "record": str(record_path),
        "pending_sections": pending_names,
        "truncated": truncated,
        "expected_json": EXPECTED_JSON,
        "references": references,
        "note_text": display_text,
    }
    if args.json:
        print(json.dumps(package, ensure_ascii=False, indent=2))
        return

    print("# Anki 制卡上下文")
    print()
    print("请根据 references 判断是否值得制卡，并只输出符合 expected_json 结构的 JSON。")
    print("records JSON 是脚本状态，不要把旧 records 传入判断；原 md 不会被修改。")
    print("sections_processed 必须列出你已处理的所有节名（即使某节不制卡）。")
    print()
    print("## 元数据")
    print(f"- note: {note_path}")
    print(f"- source_note: {source_note}")
    print(f"- source_link: {source_link}")
    print(f"- record: {record_path}")
    print(f"- pending_sections: {json.dumps(pending_names, ensure_ascii=False)}")
    print(f"- truncated: {truncated}")
    print()
    print("## expected_json")
    print("```json")
    print(json.dumps(EXPECTED_JSON, ensure_ascii=False, indent=2))
    print("```")
    print()
    print("## reference: anki-selection-rules.md")
    print(references["selection"])
    print()
    print("## reference: anki-card-format.md")
    print(references["format"])
    print()
    print("## reference: vault-structure.md")
    print(references["vault"])
    print()
    print(f"## 待处理节内容（共 {len(pending_names)} 节）")
    print(display_text)


def run_sync(args: argparse.Namespace) -> int:
    note_path = resolve_existing_file(args.sync)
    record_path = record_path_for(note_path, args.vault_root, args.records_dir)
    if record_path.exists() and not args.force:
        if note_state.is_unchanged(note_path, "anki"):
            print(f"跳过（内容未变）：{record_path}")
            return 0

    started_at = now_local()
    source_note = source_note_path(note_path, args.vault_root)
    source_link = source_link_for(note_path)
    source_url = obsidian_url(args.vault_name, source_note)

    # 加载已有 record，用于合并 cards 和 section_hashes
    existing_record = load_record_data(record_path)
    existing_cards: list[dict[str, Any]] = existing_record.get("cards", [])
    existing_section_hashes: dict[str, str] = existing_record.get("section_hashes", {})

    try:
        payload = load_cards_payload(args)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        print(f"ERROR: 读取卡片 JSON 失败：{e}", file=sys.stderr)
        return 1

    cards, warnings = normalize_cards(payload, args.max_cards)
    reason = str(payload.get("reason", "")).strip() or "未说明"
    for warning in warnings:
        print(f"  WARN {warning}")

    # 计算各节当前哈希，用于更新 section_hashes
    if is_excalidraw(note_path):
        current_hash_map = {"(visual)": section_hash(read_text(note_path))}
    else:
        raw_sections = split_sections(read_text(note_path))
        current_hash_map = {h: section_hash(c) for h, c in raw_sections}

    # 确定本次处理了哪些节
    sections_processed_from_payload = payload.get("sections_processed")
    if sections_processed_from_payload is not None:
        sections_processed = list(sections_processed_from_payload)
    else:
        # --no-cards-reason 或旧格式：将所有待处理节标记为已完成
        sections_processed = [h for h, _ in find_pending_sections(raw_sections, existing_section_hashes)]

    new_section_hashes = dict(existing_section_hashes)
    for name in sections_processed:
        if name in current_hash_map:
            new_section_hashes[name] = current_hash_map[name]

    if not cards:
        record: dict[str, Any] = {
            "source_note": source_note,
            "source_link": source_link,
            "generated_at": started_at.isoformat(timespec="seconds"),
            "generator": "agent",
            "status": existing_record.get("status", "no_cards") if existing_cards else "no_cards",
            "reason": reason,
            "warnings": warnings,
            "section_hashes": new_section_hashes,
            "cards": existing_cards,
        }
        if args.dry_run:
            print(json.dumps(record, ensure_ascii=False, indent=2))
        else:
            write_json(record_path, record)
            note_state.mark_processed(note_path, "anki")
            print(f"no_cards record: {record_path}")
        return 0

    if args.dry_run:
        card_records = [
            {"local_id": f"{batch_id(started_at)}-{index:03d}", "status": "dry_run", **card}
            for index, card in enumerate(cards, start=1)
        ]
        status = "dry_run"
    else:
        try:
            wait_for_anki(args.anki_url, max(args.wait_seconds, 0))
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        card_records, status = sync_cards_to_anki(cards, source_link, source_url, started_at, args)

    record = {
        "source_note": source_note,
        "source_link": source_link,
        "source_url": source_url,
        "generated_at": started_at.isoformat(timespec="seconds"),
        "generator": "agent",
        "status": status,
        "reason": reason,
        "warnings": warnings,
        "section_hashes": new_section_hashes,
        "cards": existing_cards + card_records,
    }

    if args.dry_run:
        print(json.dumps(record, ensure_ascii=False, indent=2))
    else:
        write_json(record_path, record)
        note_state.mark_processed(note_path, "anki")
        print(f"record: {record_path}")
        if not args.no_sync and status in {"synced", "partial_failed"}:
            try:
                anki_request(args.anki_url, "sync", timeout=120)
                print("已触发 AnkiWeb 同步")
            except RuntimeError as e:
                print(f"同步失败（可手动同步）：{e}", file=sys.stderr)

    return 2 if status in {"failed", "partial_failed"} else 0


# ── CLI ──────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从 Obsidian md 笔记同步 Anki 卡片（Claude/Codex 驱动）")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true", help="列出待处理 md 文件")
    mode.add_argument("--read", metavar="NOTE", help="输出单篇笔记的制卡上下文，供外层 agent 使用")
    mode.add_argument("--sync", metavar="NOTE", help="同步外层 agent 生成的卡片 JSON 到 Anki")

    parser.add_argument("--note", action="append", default=[], help="配合 --list 使用：单个笔记路径；可重复传入")
    parser.add_argument("--notes", nargs="+", default=[], help="配合 --list 使用：多个笔记路径")
    parser.add_argument("--dir", action="append", default=[], help="配合 --list 使用：目录路径；可重复传入")
    parser.add_argument("--recursive", action="store_true", help="配合 --dir 递归处理子目录")
    parser.add_argument("--force", action="store_true", help="已有 records JSON 时仍视为待处理/允许覆盖")
    parser.add_argument("--json", action="store_true", help="--list 或 --read 输出 JSON")
    parser.add_argument("--dry-run", action="store_true", help="--sync 时预览 records，不写入也不调用 AnkiConnect")

    parser.add_argument("--cards-json", help="--sync 使用：Claude/Codex 生成的卡片 JSON 文件")
    parser.add_argument("--cards", help="--sync 使用：直接传入卡片 JSON 字符串")
    parser.add_argument("--cards-stdin", action="store_true", help="--sync 使用：从 stdin 读取卡片 JSON")
    parser.add_argument("--no-cards-reason", help="--sync 使用：记录无需制卡的原因")

    parser.add_argument("--records-dir", type=Path, default=DEFAULT_RECORDS_DIR, help="records JSON 输出目录")
    parser.add_argument("--vault-root", type=Path, default=DEFAULT_VAULT_ROOT, help="Obsidian vault 根目录")
    parser.add_argument("--vault-name", default=None, help="obsidian:// 链接使用的 vault 名称，默认 Obsidian Vault（与 iOS 端 vault 名一致）")
    parser.add_argument("--anki-url", default=DEFAULT_ANKI_URL, help="AnkiConnect URL")
    parser.add_argument("--wait-seconds", type=int, default=60, help="等待 AnkiConnect 的秒数")
    parser.add_argument("--no-sync", action="store_true", help="同步完卡片后不触发 AnkiWeb 同步")
    parser.add_argument("--max-chars", type=int, default=60000, help="--read 时输出的最大笔记字符数，0 表示不限制")
    parser.add_argument("--max-cards", type=int, default=15, help="--sync 时单篇笔记最多同步的卡片数，0 表示不限制")
    parser.add_argument("--basic-model", default=DEFAULT_BASIC_MODEL, help="Anki basic/problem 使用的 note type")
    parser.add_argument("--reverse-model", default=DEFAULT_REVERSE_MODEL, help="Anki reverse 使用的 note type")
    parser.add_argument("--cloze-model", default=DEFAULT_CLOZE_MODEL, help="Anki cloze 使用的 note type")
    args = parser.parse_args()

    args.records_dir = args.records_dir.expanduser().resolve()
    args.vault_root = args.vault_root.expanduser().resolve()
    if not args.vault_name:
        args.vault_name = DEFAULT_VAULT_NAME
    return args


def main() -> None:
    args = parse_args()

    try:
        if args.list:
            run_list(args)
            return
        if args.read:
            run_read(args)
            return
        if args.sync:
            sys.exit(run_sync(args))
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        print(f"ERROR: 文件读写失败：{e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
