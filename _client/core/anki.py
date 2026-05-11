"""AnkiConnect 调用（http://localhost:8765 默认）。

第一档只做 ping（拿 version 看是否在跑）；后续接 addNote / updateNoteFields / sync 等。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request


class AnkiClient:
    def __init__(self, base_url: str = "http://localhost:8765"):
        self.base = (base_url or "http://localhost:8765").rstrip("/")

    def _invoke(self, action: str, params: dict | None = None, timeout: int = 8) -> dict:
        body = {"action": action, "version": 6, "params": params or {}}
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.base,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def ping(self) -> tuple[bool, str]:
        try:
            res = self._invoke("version")
        except urllib.error.URLError as e:
            return False, f"连不上 AnkiConnect（{self.base}）：{e.reason}（请确认 Anki 已开 + 装了 AnkiConnect 插件）"
        except Exception as e:
            return False, f"AnkiConnect 调用失败：{e}"
        if res.get("error"):
            return False, f"AnkiConnect 报错：{res['error']}"
        v = res.get("result")
        return True, f"AnkiConnect OK · v{v}（{self.base}）"
