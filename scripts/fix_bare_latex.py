"""一次性脚本：手工指定的几张卡片，给裸 LaTeX 命令补上 \\(...\\) 分隔符。

修复对象由 SCAN_FIXES 列表精确指定（record 文件 / card index / 字段 / 新值），
避免对其它字段产生意外修改。
"""

from __future__ import annotations
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from anki_from_note import (  # noqa: E402
    DEFAULT_ANKI_URL,
    anki_request,
    html_text,
    source_footer,
    wait_for_anki,
)

PROJECT_DIR = Path(__file__).resolve().parents[1]
RECORDS_DIR = PROJECT_DIR / "anki" / "records"

# 每条 fix：(record 文件名, card index, 字段, 新值)
FIXES = [
    (
        "010-泰勒级数.json", 1, "text",
        r"\(e^x\) 在 \(x=0\) 处的泰勒级数为 {{c1::\(1+x+\frac{x^2}{2!}+\frac{x^3}{3!}+\cdots\)}}。",
    ),
    (
        "010-泰勒级数.json", 2, "text",
        r"函数 \(f(x)\) 在 \(x=a\) 附近的泰勒展开一般形式为 {{c1::\(f(a)+f'(a)(x-a)+\frac{f''(a)}{2!}(x-a)^2+\cdots\)}}。",
    ),
    (
        "资源__books__微积分__010-指数函数求导.json", 3, "back",
        r"其中 \(a>0\) 且 \(a\neq 1\)；可先写成 \(a^t=e^{\ln(a)t}\)，再用链式法则。",
    ),
    (
        "资源__books__微积分__010-指数函数求导.json", 3, "text",
        r"指数函数的导数公式是 {{c1::\(\frac{d}{dt}a^t=\ln(a)a^t\)}}。",
    ),
    (
        "资源__books__微积分__010-指数函数求导.json", 4, "back",
        r"把 \(a^t\) 改写为 \(e^{\ln(a)t}\)，再使用链式法则求导。",
    ),
]


def build_fields(card, source_link, source_url):
    footer = source_footer(source_link, source_url, card.get("reason", ""), card["local_id"])
    if card["type"] == "cloze":
        extra = html_text(card.get("back", "") or "")
        return {
            "Text":  html_text(card.get("text", "") or ""),
            "Extra": (extra + footer) if extra else footer,
        }
    return {
        "Front": html_text(card.get("front", "") or ""),
        "Back":  html_text(card.get("back", "") or "") + footer,
    }


def main():
    dry = "--dry-run" in sys.argv

    # 按 record 分组
    by_record: dict[str, list[tuple[int, str, str]]] = {}
    for fname, idx, fld, new in FIXES:
        by_record.setdefault(fname, []).append((idx, fld, new))

    print("=== 计划 ===")
    for fname, items in by_record.items():
        print(f"[{fname}]")
        for idx, fld, new in items:
            print(f"  card[{idx}] {fld} -> {new[:80]}{'...' if len(new) > 80 else ''}")
    print()

    if dry:
        print("[dry-run] 退出")
        return

    wait_for_anki(DEFAULT_ANKI_URL, 60)

    success = 0
    failed = []
    for fname, items in by_record.items():
        path = RECORDS_DIR / fname
        data = json.loads(path.read_text(encoding="utf-8"))
        source_link = data.get("source_link") or ""
        source_url  = data.get("source_url") or ""

        # 应用字段更新到内存中的 cards
        affected_idx = sorted({i for i, _, _ in items})
        for idx, fld, new in items:
            data["cards"][idx][fld] = new

        # 对每张被改的卡，重发 fields
        for idx in affected_idx:
            card = data["cards"][idx]
            note_id = card.get("anki_note_id")
            if note_id is None:
                print(f"  SKIP（无 note_id）{fname} card[{idx}]")
                continue
            try:
                fields = build_fields(card, source_link, source_url)
                anki_request(
                    DEFAULT_ANKI_URL,
                    "updateNoteFields",
                    {"note": {"id": note_id, "fields": fields}},
                    timeout=30,
                )
                success += 1
                print(f"  OK note {note_id} ({fname} card[{idx}])")
            except RuntimeError as e:
                failed.append(f"{fname} card[{idx}] note {note_id}: {e}")
                print(f"  FAIL note {note_id}: {e}")

        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"  写回 {fname}")

    print()
    print(f"完成：成功 {success}，失败 {len(failed)}")
    for line in failed:
        print(f"  FAIL {line}")

    if success > 0:
        try:
            anki_request(DEFAULT_ANKI_URL, "sync", timeout=120)
            print("已触发 AnkiWeb 同步")
        except RuntimeError as e:
            print(f"AnkiWeb 同步失败：{e}", file=sys.stderr)


if __name__ == "__main__":
    main()
