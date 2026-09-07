"""网页分析范围（2026-09-07 用户拍板）：扩展阅读的网页也可以做页级分析，但**不是所有网页**——
只有命中这份规则表的网页才和书页一样附提示、走同一条提交路径。规则表存 ``state/kj/web-scope.json``，
设置面板、CLI（``web-scope``）、HTTP（``/kj/api/web-scope``）、AI（``kj_page`` 的 op=scope）都能改。

规则 = {id, pattern, note, enabled, added_by, added_at}。pattern 两种写法：
- 通配：对"去掉协议的 URL"（host/path，小写，忽略 www.）做 fnmatch，如 ``*.wikipedia.org/wiki/*``、``arxiv.org/abs/*``
- 正则：以 ``re:`` 开头，对完整 URL 做 search，如 ``re:^https://docs\\.python\\.org/3/``
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .register import RegisterError
from .store import Ledger

CONTRACT = "kj-web-scope/1"
MAX_PATTERN = 200
DEFAULT_RULES = [
    {"id": "wikipedia", "pattern": "*.wikipedia.org/wiki/*", "note": "维基百科词条（各语言）", "enabled": True, "added_by": "default"},
    {"id": "wikipedia-mobile", "pattern": "*.m.wikipedia.org/wiki/*", "note": "维基百科移动版", "enabled": True, "added_by": "default"},
    {"id": "arxiv-abs", "pattern": "arxiv.org/abs/*", "note": "arXiv 摘要页", "enabled": True, "added_by": "default"},
]


def scope_path(ledger: Ledger) -> Path:
    return ledger.path.parent / "web-scope.json"


def load(ledger: Ledger) -> dict:
    p = scope_path(ledger)
    if p.exists():
        try:
            d = json.loads(p.read_text("utf-8"))
            if isinstance(d, dict) and isinstance(d.get("rules"), list):
                return d
        except Exception:
            pass   # 坏文件不静默：下面重建成默认并保存，日志由调用方看 saved_default
    d = {"contract": CONTRACT, "rules": [dict(r, added_at=int(time.time())) for r in DEFAULT_RULES], "saved_default": True}
    save(ledger, d)
    return d


def save(ledger: Ledger, data: dict) -> None:
    p = scope_path(ledger)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = dict(data)
    data["contract"] = CONTRACT
    data.pop("saved_default", None)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(p)


def normalize_url(url: str) -> str:
    """去协议、小写 host、去 www.、去 fragment；保留 path 与 query（有的站靠 query 区分页面）。"""
    u = (url or "").strip()
    if u.startswith("web:"):
        u = u[4:]
    parts = urlsplit(u if "://" in u else "https://" + u)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return host + path


def _validate_pattern(pattern: str) -> str:
    s = (pattern or "").strip()
    if not s:
        raise RegisterError("bad_pattern", "pattern 不能为空")
    if len(s) > MAX_PATTERN:
        raise RegisterError("bad_pattern", f"pattern 太长（>{MAX_PATTERN}）")
    if s.startswith("re:"):
        try:
            re.compile(s[3:])
        except re.error as e:
            raise RegisterError("bad_pattern", f"正则写错了：{e}")
    else:
        s = s.lower()
        if "://" in s:
            s = s.split("://", 1)[1]
        if s.startswith("www."):
            s = s[4:]
    return s


def _rule_matches(rule: dict, url: str, norm: str) -> bool:
    pat = str(rule.get("pattern") or "")
    if not pat:
        return False
    if pat.startswith("re:"):
        try:
            return re.search(pat[3:], url) is not None
        except re.error:
            return False
    return fnmatch.fnmatchcase(norm, pat.lower()) or fnmatch.fnmatchcase(norm.split("?", 1)[0], pat.lower())


def matches(ledger: Ledger, url: str) -> dict | None:
    """命中的第一条启用规则；没命中 None。"""
    u = (url or "").strip()
    if u.startswith("web:"):
        u = u[4:]
    if not u:
        return None
    norm = normalize_url(u)
    for r in load(ledger).get("rules", []):
        if r.get("enabled", True) and _rule_matches(r, u, norm):
            return r
    return None


def list_rules(ledger: Ledger) -> list[dict]:
    return list(load(ledger).get("rules", []))


def add_rule(ledger: Ledger, pattern: str, *, note: str = "", actor: str = "") -> dict:
    pat = _validate_pattern(pattern)
    data = load(ledger)
    for r in data["rules"]:
        if r.get("pattern") == pat:
            if not r.get("enabled", True):
                r["enabled"] = True
                save(ledger, data)
            return dict(r, existed=True)
    rid = re.sub(r"[^a-z0-9]+", "-", pat.replace("re:", "").lower()).strip("-")[:40] or "rule"
    if any(r.get("id") == rid for r in data["rules"]):
        rid = rid + "-" + hashlib.sha1(pat.encode("utf-8")).hexdigest()[:6]
    rule = {"id": rid, "pattern": pat, "note": (note or "").strip()[:200], "enabled": True,
            "added_by": actor or "manual", "added_at": int(time.time())}
    data["rules"].append(rule)
    save(ledger, data)
    return dict(rule)


def remove_rule(ledger: Ledger, id_or_pattern: str) -> bool:
    key = (id_or_pattern or "").strip()
    data = load(ledger)
    before = len(data["rules"])
    data["rules"] = [r for r in data["rules"] if r.get("id") != key and r.get("pattern") != key and r.get("pattern") != key.lower()]
    if len(data["rules"]) == before:
        return False
    save(ledger, data)
    return True


def set_enabled(ledger: Ledger, id_or_pattern: str, enabled: bool) -> bool:
    key = (id_or_pattern or "").strip()
    data = load(ledger)
    hit = False
    for r in data["rules"]:
        if r.get("id") == key or r.get("pattern") == key:
            r["enabled"] = bool(enabled)
            hit = True
    if hit:
        save(ledger, data)
    return hit


def handle(ledger: Ledger, action: str, *, pattern: str | None = None, note: str = "", rule_id: str | None = None,
           url: str | None = None, actor: str = "") -> dict:
    """统一入口（CLI/HTTP/AI 共用）：list | add | remove | enable | disable | test。"""
    a = (action or "list").strip().lower()
    if a == "list":
        return {"ok": True, "rules": list_rules(ledger), "file": str(scope_path(ledger))}
    if a == "add":
        return {"ok": True, "rule": add_rule(ledger, pattern or "", note=note, actor=actor), "rules": list_rules(ledger)}
    if a == "remove":
        ok = remove_rule(ledger, rule_id or pattern or "")
        if not ok:
            raise RegisterError("rule_not_found", f"没有这条规则：{rule_id or pattern}")
        return {"ok": True, "removed": rule_id or pattern, "rules": list_rules(ledger)}
    if a in ("enable", "disable"):
        ok = set_enabled(ledger, rule_id or pattern or "", a == "enable")
        if not ok:
            raise RegisterError("rule_not_found", f"没有这条规则：{rule_id or pattern}")
        return {"ok": True, "rules": list_rules(ledger)}
    if a == "test":
        r = matches(ledger, url or pattern or "")
        return {"ok": True, "url": url or pattern, "in_scope": r is not None, "rule": r}
    raise RegisterError("bad_action", "action 只能是 list / add / remove / enable / disable / test")
