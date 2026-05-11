"""服务端 HTTP 通讯封装。"""
from __future__ import annotations

import urllib.error
import urllib.request
import urllib.parse
import json
import mimetypes
import os
import uuid
from pathlib import Path


class ApiClient:
    def __init__(self, base_url: str, token: str):
        self.base = base_url.rstrip("/")
        self.token = token

    # ── 简单连通测试：拉 /static/client/manifest.json（不需要鉴权）+ 试 /api/upload/<bad> 看 401 ──
    def ping(self) -> tuple[bool, str]:
        # 1. 服务端可达？
        try:
            req = urllib.request.Request(
                f"{self.base}/static/client/manifest.json",
                headers={"User-Agent": "bwicarus-client"},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                if r.status != 200:
                    return False, f"manifest 返回 {r.status}"
        except Exception as e:
            return False, f"无法连接服务端：{e}"

        # 2. token 有效？发个空 upload 看响应
        try:
            req = urllib.request.Request(
                f"{self.base}/api/upload/dashboard",
                method="POST",
                data=b"",
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "X-Path": ".ping",
                    "Content-Type": "application/octet-stream",
                    "User-Agent": "bwicarus-client",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                if r.status == 200:
                    return True, f"服务端 + token 都正常（{self.base}）"
                return False, f"上传测试返回 {r.status}"
        except urllib.error.HTTPError as e:
            if e.code == 401:
                return False, "token 无效（401）"
            if e.code == 400:
                return False, f"请求被拒（400），但服务端可达：{self.base}"
            return False, f"HTTP {e.code}：{e.reason}"
        except Exception as e:
            return False, f"token 测试失败：{e}"

    # ── 上传单个文件 (octet-stream) ──
    def upload(self, dataset: str, rel_path: str, data: bytes) -> dict:
        rel_path = rel_path.replace("\\", "/").lstrip("/")
        req = urllib.request.Request(
            f"{self.base}/api/upload/{urllib.parse.quote(dataset)}",
            method="POST",
            data=data,
            headers={
                "Authorization": f"Bearer {self.token}",
                "X-Path": rel_path,
                "Content-Type": "application/octet-stream",
                "User-Agent": "bwicarus-client",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
            return json.loads(body.decode("utf-8")) if body else {"ok": True}
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code} {e.reason}: {err[:200]}") from e
