#!/usr/bin/env python3
"""这条 Reader 路由到底由谁执行？—— 部署前先问一句。

## 为什么需要它

2026-08-19：同一个错误犯了不止一次 —— 改完 `_server_deploy/*.py` 就部署 Pi，
而那条路由在 App 内其实是**本地实现**，服务端改动对 App 完全无效。有一次还更糟：
顺手给请求加了个查询参数，撞上本地那条的精确参数白名单，把原本能用的功能弄坏了。

根因是判据缺失。文档当时写的是「改了 `_server_deploy/*.py` 仍要部署」——
一句没有前置判断的结论。

## 它看两处，缺一都会误判

1. `ios/BWReader/native_reader_interface_manifest.json` 的 `owner`
   ⚠ **owner 记的是"数据归属"，不是"请求实际打给谁"**。
2. `_server_deploy/static/pdf/native-local-runtime.js` 里有没有该路径的本地分支。
   有的话 App 内根本不出网 —— 无论 manifest 怎么写。

实测（2026-08-19）：108 条 owner=pi 里有 19 条 runtime 已本地化。只看 manifest
会把这 19 条全判错。

## 用法

    python3 scripts/where_does_this_route_run.py /api/assistant/voice-page-text
    python3 scripts/where_does_this_route_run.py --audit     # 列出所有名实不符的
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "ios" / "BWReader" / "native_reader_interface_manifest.json"
RUNTIME = ROOT / "_server_deploy" / "static" / "pdf" / "native-local-runtime.js"


def _routes() -> list[dict]:
    def walk(node):
        if isinstance(node, dict):
            if "owner" in node and "path" in node:
                yield node
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)

    return list(walk(json.loads(MANIFEST.read_text("utf-8"))))


def _runtime_source() -> str:
    return RUNTIME.read_text("utf-8")


def _has_local_branch(path: str, runtime: str) -> bool:
    """runtime 里有没有直接针对这条路径的分支。

    按字面量找 `'<path>'` —— runtime 的分发就是一串
    `if (url.pathname === '/pdf/api/xxx')`，所以这个判据既准又不脆。
    """

    return f"'{path}'" in runtime


def _unmangle(path: str) -> str:
    """Git Bash 会把 `/api/...` 展开成 `C:/Users/.../api/...`。

    在 Windows 上用 Git Bash 跑是常态，不处理的话每次都得记着加 `//` 前缀 ——
    而"忘了加"的表现是"这条路由不存在"，一个看起来很像结论的错误答案。
    """

    if ":" not in path and "\\" not in path:
        return path
    normalized = path.replace("\\", "/")
    # 先试 /pdf/ 再试 /api/：`/pdf/api/translate` 两个标记都含，
    # 按 /api/ 切会得到 `/api/translate` —— 一条不存在的路由，
    # 而它的表现是"查不到"，看起来像结论的错误答案。取**最靠前**的那个。
    candidates = [
        normalized.find(marker) for marker in ("/pdf/", "/api/")
        if normalized.find(marker) > 0
    ]
    return normalized[min(candidates):] if candidates else path


def describe(path: str) -> int:
    path = _unmangle(path)
    routes = {r["path"]: r for r in _routes()}
    runtime = _runtime_source()
    entry = routes.get(path)
    if entry is None:
        # 前缀式路由（manifest 里 match=prefix 的那些）
        for candidate, value in routes.items():
            if candidate.endswith("/") and path.startswith(candidate):
                entry = value
                path = candidate
                break
    if entry is None:
        print(f"✗ manifest 里没有这条路由：{path}")
        print("  → 它可能只存在于 Pi（网页/扩展表面用），App 内不会调它。")
        return 1

    owner = entry.get("owner")
    local = _has_local_branch(path, runtime)
    print(f"路由      {path}")
    print(f"manifest  owner={owner}  methods={entry.get('methods')}")
    print(f"runtime   {'有本地分支' if local else '无本地分支'}")
    print()
    if local:
        print("→ **App 内本地执行，不打 Pi**。")
        print("  改 _server_deploy/*.py 对 App 无效；要改就改 native-local-runtime.js。")
        print("  ⚠ 也别给这条请求加查询参数：本地实现常用精确参数白名单，多一个整条拒。")
        print("  到 iPad 需要新的 TestFlight 构建。")
    elif owner == "pi":
        print("→ App 经 Swift 网关打 Pi。改服务端要部署 Pi 才生效。")
    elif owner == "native":
        print("→ App 原生桥（document-start），既不打 Pi 也不走 runtime 分发。")
    else:
        print("→ manifest 说 local 但 runtime 没有分支：这条在 App 内会报")
        print("  BW_NATIVE_INTERFACE_HANDLER_MISSING —— 也就是压根调不通。")
    return 0


def audit() -> int:
    """列出名实不符的：owner 说 pi、runtime 却已本地化。"""

    runtime = _runtime_source()
    rows = _routes()
    drifted = [
        r for r in rows
        if r["owner"] == "pi" and _has_local_branch(r["path"], runtime)
    ]
    missing = [
        r for r in rows
        if r["owner"] == "local" and not _has_local_branch(r["path"], runtime)
    ]
    print(f"manifest 共 {len(rows)} 条")
    print(f"\nowner=pi 但 runtime 已本地化（{len(drifted)} 条）——")
    print("  改这些的服务端实现对 App 无效：")
    for r in drifted:
        print("   ", r["path"])
    print(f"\nowner=local 但 runtime 没有分支（{len(missing)} 条）——")
    print("  这些在 App 内可能直接报 handler missing：")
    for r in missing:
        print("   ", r["path"])
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("path", nargs="?", help="要查的路由，如 /pdf/api/highlights")
    parser.add_argument("--audit", action="store_true", help="列出所有名实不符的路由")
    args = parser.parse_args()
    if args.audit:
        return audit()
    if not args.path:
        parser.error("给一条路由，或用 --audit")
    return describe(args.path)


if __name__ == "__main__":
    sys.exit(main())
