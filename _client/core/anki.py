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

    def sync(self, timeout: int = 120) -> tuple[bool, str]:
        """触发 Anki 跟 AnkiWeb 同步。timeout 较长允许网络慢的同步完成。"""
        try:
            res = self._invoke("sync", timeout=timeout)
        except urllib.error.URLError as e:
            return False, f"AnkiConnect 调用失败：{e.reason}"
        except Exception as e:
            return False, f"AnkiConnect 调用失败：{e}"
        if res.get("error"):
            return False, f"AnkiConnect 报错：{res['error']}"
        return True, "AnkiWeb 同步已触发"

    def ensure_alive(
        self,
        anki_exe: str = "",
        *,
        force_restart: bool = False,
        timeout: int = 180,
    ) -> tuple[bool, str]:
        """确保 AnkiConnect 可用。

        - ping 通 → OK
        - ping 不通 + force_restart=False → False（让上游决定）
        - ping 不通 + force_restart=True → taskkill /F anki.exe + 启动 Anki + 轮询上线

        场景：
        - 凌晨定时任务用 force_restart=True（wake_anki 本身已表达"主动启动"语义）
        - 用户白天点按钮：force_restart=cfg.anki.auto_restart，由用户开关决定
        """
        import subprocess
        import time

        ok, msg = self.ping()
        if ok:
            return True, msg
        if not force_restart:
            return False, msg + "（未启用 auto_restart，不自动重启 Anki）"
        if not anki_exe:
            return False, "需要重启 Anki 但 anki.exe_path 未配置"

        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", "anki.exe"],
                capture_output=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass
        time.sleep(2)

        try:
            subprocess.Popen(
                [anki_exe],
                close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as e:
            return False, f"启动 Anki 失败：{e}"

        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(3)
            ok, msg = self.ping()
            if ok:
                return True, f"已重启 Anki，{msg}"
        return False, f"重启 Anki 后 {timeout}s 内 AnkiConnect 未上线"
