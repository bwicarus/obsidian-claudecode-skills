#!/usr/bin/env python3
"""一次性：让服务器侧 Anki 登录 AnkiWeb + 强制 full download。

背景：VPS / Pi 的 Anki 从来没真正登录过 AnkiWeb（profile syncKey=None），
daily 里的「AnkiWeb 同步」一直是 AnkiConnect 静默 no-op。结果服务器算掌握度
用的是导入时那份静态 collection，从没拉过手机/电脑的真实复习记录。

本脚本：
  1. 交互输入 AnkiWeb 邮箱/密码（getpass，不回显）→ col.sync_login 拿 hkey
  2. col.sync_collection 探方向：
       FULL_DOWNLOAD → 强制 download（AnkiWeb 新、本地旧，正是我们要的）
       FULL_UPLOAD   → **abort**（AnkiWeb 比本地旧，download 会丢你的改动；
                        多半是手机没 push 成功，去手机重新 sync 再来）
       NORMAL/NO_CHANGES → 直接完成
  3. hkey + 邮箱写进 prefs21.db profile（以后 AnkiConnect / daily sync 真正生效）
  4. 重开 collection 验证 FSRS / scm

⚠️ 用 `!` 前缀在你自己的终端跑（密码 prompt 不进 Claude 对话记录）：
   !cd ~/claude && /opt/anki-venv/bin/python3 scripts/anki_weblogin.py

跑前先 sudo systemctl stop anki-headless（释放 collection / profile db 锁）。
本地有备份 ~/anki-fsrs-backup-clean-*/，出问题可回滚。
"""
from __future__ import annotations

import getpass
import os
import pickle
import sqlite3
import sys
from pathlib import Path

ANKI_BASE = Path(os.environ.get(
    "ANKI_BASE", str(Path.home() / ".local/share/Anki2")
))
PROFILE = os.environ.get("ANKI_PROFILE", "User 1")
COLLECTION = ANKI_BASE / PROFILE / "collection.anki2"
PREFS_DB = ANKI_BASE / "prefs21.db"


def die(msg: str, code: int = 1):
    print(f"\n✗ {msg}", file=sys.stderr)
    sys.exit(code)


def main() -> int:
    if not COLLECTION.exists():
        die(f"collection 不存在: {COLLECTION}")

    # 防止 anki-headless 还跑着（db 锁 / 同步打架）
    import subprocess
    try:
        act = subprocess.run(
            ["systemctl", "is-active", "anki-headless.service"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        if act == "active":
            die("anki-headless 还在跑，先 `sudo systemctl stop anki-headless` 再来。")
    except Exception:
        pass

    from anki.collection import Collection
    from anki import sync_pb2

    print(f"collection: {COLLECTION}")
    email = input("AnkiWeb 邮箱: ").strip()
    if not email:
        die("邮箱为空")
    password = getpass.getpass("AnkiWeb 密码（不回显）: ")
    if not password:
        die("密码为空")

    col = Collection(str(COLLECTION))

    # ── 1. 登录拿 hkey ──
    print("\n[1] sync_login ...")
    try:
        auth = col.sync_login(email, password, None)
    except Exception as e:
        col.close()
        die(f"登录失败（账号密码错 / 网络 / 需要在网页先过验证码）：{e}")
    print(f"    ✓ 拿到 hkey（{auth.hkey[:8]}...），endpoint={auth.endpoint or '(待 server 分配)'}")

    # ── 2. 探同步方向 ──
    print("[2] sync_collection 探方向 ...")
    out = col.sync_collection(auth, False)
    if out.new_endpoint:
        print(f"    server 分配 endpoint: {out.new_endpoint}，更新后重探")
        auth.endpoint = out.new_endpoint
        out = col.sync_collection(auth, False)

    Req = sync_pb2.SyncCollectionResponse.ChangesRequired
    req = out.required
    name = {v: k for k, v in Req.items()}.get(req, str(req))
    print(f"    required = {name} ({req})")
    if out.server_message:
        print(f"    server_message: {out.server_message}")

    if req == Req.NO_CHANGES:
        print("\n本地已和 AnkiWeb 一致（无变化）。可能你那边还没 push？")
    elif req == Req.NORMAL_SYNC:
        print("\n✓ NORMAL_SYNC 已自动完成（增量合并）。")
    elif req in (Req.FULL_SYNC, Req.FULL_DOWNLOAD):
        print(f"\n[3] 需要 full download（AnkiWeb 新、本地旧）→ 强制 download")
        col.close_for_full_sync()
        col.full_upload_or_download(
            auth=auth, server_usn=out.server_media_usn or 0, upload=False
        )
        print("    ✓ full download 完成")
    elif req == Req.FULL_UPLOAD:
        col.close()
        die(
            "server 要求 FULL_UPLOAD —— 说明 AnkiWeb 上比本地还旧！\n"
            "  强制 download 会拉到旧数据，upload 会把服务器旧 collection 覆盖你手机的改动。\n"
            "  两个方向都不对。请去手机/电脑 Anki 再 sync 一次（确认 FSRS 改动真的传上 AnkiWeb），\n"
            "  确认后重跑本脚本。本次未改动任何数据。"
        )
    else:
        col.close()
        die(f"未知 required={req}，安全起见不动数据。")

    # ── 4. hkey 写进 profile（以后 AnkiConnect / daily sync 真正生效）──
    print("[4] 写 hkey 到 profile ...")
    try:
        conn = sqlite3.connect(str(PREFS_DB))
        row = conn.execute(
            "SELECT data FROM profiles WHERE name=?", (PROFILE,)
        ).fetchone()
        if row:
            prof = pickle.loads(row[0])
            prof["syncKey"] = auth.hkey
            prof["syncUser"] = email
            if auth.endpoint:
                prof["currentSyncUrl"] = auth.endpoint
            conn.execute(
                "UPDATE profiles SET data=? WHERE name=?",
                (pickle.dumps(prof), PROFILE),
            )
            conn.commit()
            print("    ✓ profile.syncKey / syncUser 已写入")
        else:
            print(f"    ⚠ profile {PROFILE!r} 不存在，跳过（AnkiConnect 仍需手动登录）")
        conn.close()
    except Exception as e:
        print(f"    ⚠ 写 profile 失败（不影响本次 download）：{e}")

    # ── 5. 验证 ──
    print("[5] 验证 ...")
    try:
        col2 = Collection(str(COLLECTION))
        fsrs_on = col2.get_config("fsrs", None)
        ncards = col2.card_count()
        scm = col2.db.scalar("SELECT scm FROM col")
        import datetime as dt
        print(f"    fsrs config = {fsrs_on}")
        print(f"    cards = {ncards}, schema_mod = {dt.datetime.fromtimestamp(scm/1000)}")
        col2.close()
    except Exception as e:
        print(f"    验证读取失败（不一定是错，重启 anki-headless 再看）：{e}")

    print(
        "\n✓ 完成。下一步：\n"
        "  sudo systemctl start anki-headless\n"
        "  然后告诉 Claude 验证 FSRS + 改 mastery 代码读 difficulty/stability。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
