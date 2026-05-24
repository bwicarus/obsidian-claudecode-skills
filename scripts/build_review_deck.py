"""
build_review_deck.py — 每日重建「必复习::今日」牌组

由于用户的 AnkiConnect 不支持 createFilteredDeck，采用永久移动方案：
  1. 把上次移过去且仍在「必复习::今日」里的卡片移回原牌组（home deck）
  2. 找今日候选卡片：is:due 加上 review_priority 排序前 N 篇笔记的所有卡
  3. 记录每张卡当前的 home deck 到 state/review_deck_homes.json
  4. changeDeck 全部到「必复习::今日」
  5. 触发 AnkiWeb 同步

state/review_deck_homes.json 是 home 映射，丢了也能用 records 兜底恢复。

使用：
  python scripts/build_review_deck.py
  python scripts/build_review_deck.py --top-n 30 --max-cards 300
  python scripts/build_review_deck.py --dry-run
  python scripts/build_review_deck.py --reset-only   # 只把今日卡移回，不重建
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config

DECK_NAME       = "必复习::今日"
TOP_N_NOTES     = 20
MAX_DECK_SIZE   = 200
ANKI_URL        = config.ANKI_CONNECT_URL
HOMES_FILE      = config.STATE_DIR / "review_deck_homes.json"


def anki_request(action: str, params: dict | None = None, timeout: int = 30):
    payload = {"action": action, "version": 6}
    if params is not None:
        payload["params"] = params
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        ANKI_URL, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8")
    result = json.loads(body)
    if result.get("error"):
        raise RuntimeError(f"AnkiConnect error: {result['error']}")
    return result.get("result")


# ── home 映射读写 ────────────────────────────────────────────────────────────
# 格式: {"<card_id>": {"home": "<deck>", "reps_at_add": <int>}}
# 兼容旧格式: {"<card_id>": "<deck>"}（reps 视为 0）

def load_homes() -> dict[str, dict]:
    if not HOMES_FILE.exists():
        return {}
    try:
        raw = json.loads(HOMES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    out: dict[str, dict] = {}
    for k, v in raw.items():
        if isinstance(v, str):
            out[k] = {"home": v, "reps_at_add": 0}
        elif isinstance(v, dict) and "home" in v:
            out[k] = {"home": v["home"], "reps_at_add": int(v.get("reps_at_add", 0))}
    return out


def save_homes(homes: dict[str, dict]) -> None:
    HOMES_FILE.parent.mkdir(parents=True, exist_ok=True)
    HOMES_FILE.write_text(
        json.dumps(homes, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def home_of(homes: dict[str, dict], card_id: int) -> str | None:
    rec = homes.get(str(card_id))
    return rec["home"] if rec else None


# ── 兜底：从 anki records 反查卡片的原始 deck ──────────────────────────────────

def build_card_to_deck_map_from_records() -> dict[int, str]:
    """从 anki/records/*.json 里反查 anki_note_id → deck 名。"""
    nid_to_deck: dict[int, str] = {}
    for rf in config.RECORDS_DIR.glob("*.json"):
        try:
            rec = json.loads(rf.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for c in rec.get("cards", []):
            nid = c.get("anki_note_id")
            deck = c.get("deck")
            if nid and deck:
                nid_to_deck[int(nid)] = deck
    return nid_to_deck


# ── Anki 查询封装 ────────────────────────────────────────────────────────────

def get_card_decks(card_ids: list[int]) -> dict[int, str]:
    """获取每张卡当前所在 deck。"""
    if not card_ids:
        return {}
    info = anki_request("cardsInfo", {"cards": card_ids})
    return {int(c["cardId"]): c["deckName"] for c in info if c.get("cardId")}


def cards_to_nids(card_ids: list[int]) -> dict[int, int]:
    """获取每张卡对应的 note_id。"""
    if not card_ids:
        return {}
    pairs = anki_request("cardsToNotes", {"cards": card_ids})
    return dict(zip(card_ids, pairs))


def ensure_deck(name: str) -> None:
    deck_names = anki_request("deckNames")
    if name not in deck_names:
        anki_request("createDeck", {"deck": name})


def change_decks_grouped(by_target: dict[str, list[int]]) -> int:
    """按目标 deck 分组批量 changeDeck。返回总移动数。"""
    moved = 0
    for target, cids in by_target.items():
        if not cids:
            continue
        ensure_deck(target)
        anki_request("changeDeck", {"cards": cids, "deck": target})
        moved += len(cids)
    return moved


# ── 收集今日候选 ─────────────────────────────────────────────────────────────

def collect_project_nids_and_priority(top_n: int) -> tuple[set[int], list[int], list[str]]:
    """扫描所有 records，返回：
       - all_nids: 本项目管理的所有 anki_note_id（用于约束搜索范围）
       - priority_nids: 按 review_priority 排序前 N 篇笔记的 nid（候选优先填这些）
       - priority_names: 这 N 篇的文件名
    """
    all_nids: set[int] = set()
    rows: list[tuple[float, str, list[int]]] = []

    for rf in config.RECORDS_DIR.glob("*.json"):
        try:
            rec = json.loads(rf.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        nids = [
            int(c["anki_note_id"])
            for c in rec.get("cards", [])
            if c.get("anki_note_id") and c.get("status") == "synced"
        ]
        if not nids:
            continue
        all_nids.update(nids)

        if rec.get("status") in ("no_cards", "orphan"):
            continue
        priority = float(rec.get("priority_snapshot", {}).get("score", 0) or 0)
        if priority <= 0:
            continue
        name = Path(rec.get("source_note", "")).stem or rf.stem
        rows.append((priority, name, nids))

    rows.sort(reverse=True)
    priority_nids: list[int] = []
    priority_names: list[str] = []
    for _, name, nids in rows[:top_n]:
        priority_nids.extend(nids)
        priority_names.append(name)
    return all_nids, priority_nids, priority_names


def _nid_clause(nids) -> str:
    return " OR ".join(f"nid:{n}" for n in nids)


def find_candidate_cards(
    project_nids: set[int],
    priority_nids: list[int],
    exclude_deck: str,
    limit: int,
) -> list[int]:
    """只在本项目管理的卡片中找候选。
       优先填高优先级笔记的卡，剩余配额从 is:due 中（同样限于本项目）取。
    """
    if not project_nids:
        return []

    project_clause = _nid_clause(project_nids)
    selected: list[int] = []
    seen: set[int] = set()

    # 1. 高优先级笔记的所有卡（项目内）
    if priority_nids:
        q1 = f'-is:suspended -deck:"{exclude_deck}" ({_nid_clause(priority_nids)})'
        try:
            for c in anki_request("findCards", {"query": q1}) or []:
                if c not in seen:
                    seen.add(c); selected.append(c)
        except RuntimeError as e:
            print(f"高优先级查询失败：{e}", file=sys.stderr)

    # 2. 项目内的 is:due 卡，填到 limit
    if len(selected) < limit:
        q2 = f'-is:suspended -deck:"{exclude_deck}" is:due ({project_clause})'
        try:
            for c in anki_request("findCards", {"query": q2}) or []:
                if len(selected) >= limit:
                    break
                if c not in seen:
                    seen.add(c); selected.append(c)
        except RuntimeError as e:
            print(f"is:due 查询失败：{e}", file=sys.stderr)

    return selected[:limit]


# ── 主流程 ───────────────────────────────────────────────────────────────────

def restore_existing_today_cards() -> int:
    """把仍在「必复习::今日」里的卡片移回 home deck。"""
    today_cards = anki_request("findCards", {"query": f'deck:"{DECK_NAME}"'})
    if not today_cards:
        return 0

    homes = load_homes()
    fallback = build_card_to_deck_map_from_records()

    # 用 cards_to_nids 把 cardId 映射到 noteId（fallback 用）
    nid_map = cards_to_nids([int(c) for c in today_cards])

    by_home: dict[str, list[int]] = {}
    unresolved: list[int] = []
    for cid in today_cards:
        home = home_of(homes, int(cid))
        if not home:
            # 兜底：用 record 里的 deck
            nid = nid_map.get(int(cid))
            if nid:
                home = fallback.get(int(nid))
        if not home:
            unresolved.append(int(cid))
            continue
        if home == DECK_NAME:
            # 安全防御：home 是「必复习」本身，不动
            continue
        by_home.setdefault(home, []).append(int(cid))

    moved = change_decks_grouped(by_home)
    if unresolved:
        print(f"  警告：{len(unresolved)} 张卡找不到 home，留在「{DECK_NAME}」", file=sys.stderr)
    return moved


def rebuild_deck(args) -> int:
    # 1. 健康检查
    try:
        anki_request("version", timeout=10)
    except (RuntimeError, urllib.error.URLError, ConnectionError, TimeoutError) as e:
        print(f"AnkiConnect 不可达：{e}", file=sys.stderr)
        return 1

    # 2. 把上次的卡移回 home
    if args.dry_run:
        existing = anki_request("findCards", {"query": f'deck:"{DECK_NAME}"'}) or []
        print(f"[dry-run] 会把当前在「{DECK_NAME}」里的 {len(existing)} 张卡移回 home")
    else:
        moved_back = restore_existing_today_cards()
        print(f"已将 {moved_back} 张昨日/上轮卡片移回原牌组")

    if args.reset_only:
        if not args.dry_run:
            save_homes({})  # 清空映射
        return 0

    # 3. 收集本项目所有 nid + 高优先级 nid
    project_nids, priority_nids, names = collect_project_nids_and_priority(args.top_n)
    print(f"本项目卡片: {len(project_nids)} 张（散落在 records 里）")
    print(f"高优先级前 {args.top_n} 篇笔记: {len(names)} 篇 / {len(priority_nids)} 张卡")
    if names:
        preview = ", ".join(names[:8]) + (" ..." if len(names) > 8 else "")
        print(f"  覆盖: {preview}")

    # 4. 找候选卡（限于本项目；已排除「必复习::今日」中的卡，因为上一步已移回）
    cand = find_candidate_cards(project_nids, priority_nids, DECK_NAME, args.max_cards)
    print(f"今日候选: {len(cand)} 张卡（上限 {args.max_cards}）")

    if args.dry_run:
        print(f"[dry-run] 会把这 {len(cand)} 张卡移到「{DECK_NAME}」")
        return 0

    if not cand:
        print("无候选卡，跳过创建。")
        save_homes({})
        return 0

    # 5. 记录每张卡当前 home + 当前 reps 计数，然后移动
    info = anki_request("cardsInfo", {"cards": cand})
    info_by_id = {int(c["cardId"]): c for c in info}
    new_homes: dict[str, dict] = {}
    for cid in cand:
        c = info_by_id.get(int(cid), {})
        home = c.get("deckName")
        reps = int(c.get("reps", 0))
        if home and home != DECK_NAME:
            new_homes[str(cid)] = {"home": home, "reps_at_add": reps}

    ensure_deck(DECK_NAME)
    anki_request("changeDeck", {"cards": cand, "deck": DECK_NAME})
    print(f"已移动 {len(cand)} 张卡到「{DECK_NAME}」")

    save_homes(new_homes)

    # 6. 同步
    if not args.no_sync:
        try:
            anki_request("sync", timeout=120)
            print("已触发 AnkiWeb 同步")
        except RuntimeError as e:
            print(f"同步失败（可手动同步）：{e}", file=sys.stderr)

    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="重建「必复习::今日」牌组（永久移动方案）")
    p.add_argument("--top-n", type=int, default=TOP_N_NOTES,
                   help=f"按 review_priority 排序取前 N 篇笔记（默认 {TOP_N_NOTES}）")
    p.add_argument("--max-cards", type=int, default=MAX_DECK_SIZE,
                   help=f"今日牌组最大卡片数（默认 {MAX_DECK_SIZE}）")
    p.add_argument("--dry-run", action="store_true", help="只预览不修改 Anki")
    p.add_argument("--no-sync", action="store_true", help="不触发 AnkiWeb 同步")
    p.add_argument("--reset-only", action="store_true",
                   help="只把今日牌组里的卡移回 home，不再重建")
    args = p.parse_args()
    return rebuild_deck(args)


if __name__ == "__main__":
    sys.exit(main())
