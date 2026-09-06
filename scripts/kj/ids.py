"""稳定标识：程序铸造、与名称无关，改名不改 id。

节点 id = ``kj:`` + 10 位 Crockford base32（前 6 位=秒级时间，后 4 位=随机），
例 ``kj:01J9ZK3A7Q``。时间前缀让 id 按创建顺序可排序；随机尾避免同秒冲突。
事件 id / 记录 id / 关系 id 用同一函数、不同前缀。
"""
from __future__ import annotations

import os
import re
import time
import unicodedata

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford，去掉 I L O U


def _b32(n: int, width: int) -> str:
    out = []
    for _ in range(width):
        out.append(_ALPHABET[n & 31])
        n >>= 5
    return "".join(reversed(out))


def mint(prefix: str = "kj", *, now: float | None = None, rand: int | None = None) -> str:
    t = int(now if now is not None else time.time())
    r = rand if rand is not None else int.from_bytes(os.urandom(3), "big") & 0xFFFFF
    return f"{prefix}:{_b32(t, 6)}{_b32(r, 4)}"


_ID_RE = re.compile(r"^[a-z]{1,8}:[0-9A-HJKMNP-TV-Z]{10}$")
_QID_RE = re.compile(r"^Q[1-9][0-9]{0,11}$")


def is_node_id(s: str) -> bool:
    return bool(s) and s.startswith("kj:") and bool(_ID_RE.match(s))


def is_qid(s: str) -> bool:
    return bool(s) and bool(_QID_RE.match(s))


def short(node_id: str) -> str:
    """``kj:01J9ZK3A7Q`` → ``01J9ZK3A7Q``（文件名里用）。"""
    return node_id.split(":", 1)[1] if ":" in node_id else node_id


_UNSAFE = re.compile(r'[\\/:*?"<>|#^\[\]\r\n\t]+')


def safe_filename(name: str, limit: int = 60) -> str:
    """节点名 → 文件名安全片段。保留中日英，去掉 Obsidian/Windows 都不喜欢的字符。"""
    s = unicodedata.normalize("NFC", name or "").strip()
    s = _UNSAFE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    if len(s) > limit:
        s = s[:limit].rstrip(" .")
    return s or "未命名"
