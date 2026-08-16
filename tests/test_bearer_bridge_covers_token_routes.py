"""拿 session 当鉴权的端点，必须也能被 API token 打开。

app.py 的 `current_user()` 只读 session cookie。Bearer token 要靠
`require_login_global()` 里的桥换成 session，而那个桥只对
`PROTECTED_PREFIXES` 里的路径生效。

于是有一个很容易踩、又很难看出来的组合：某个模块用 `current_user` 做鉴权
（写法完全正确），路由前缀却没进 PROTECTED_PREFIXES。结果是浏览器里一切
正常（有 cookie），只有拿 token 的客户端恒 401 —— 而 401 的字面意思是
「凭据无效」，会把人引去查 token，查不出问题。

2026-08-16 实测：`/api/kg/*`（电脑侧只读拉知识图谱）就是这样。用户生成的
token 完全有效，是端点没挂桥。app.py 里 `/api/assistant`、`/api/fitness`
的那句注释说明同样的事以前也发生过。
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "_server_deploy"
APP_PY = DEPLOY / "app.py"

ROUTE_RE = re.compile(r"""@(?:app|bp)\.route\(\s*["']([^"']+)["']""")


def _protected_prefixes() -> tuple[str, ...]:
    """从 app.py 源码里读出 PROTECTED_PREFIXES，不 import（app.py 有重副作用）。"""
    tree = ast.parse(APP_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "PROTECTED_PREFIXES":
                value = ast.literal_eval(node.value)
                return tuple(value)
    raise AssertionError("app.py 里找不到 PROTECTED_PREFIXES")


def _modules_authed_by_current_user() -> dict[str, str]:
    """找 `register_X(app, current_user)` 这类注册调用 → {模块名: 注册函数名}。

    app.py 用「把鉴权可调用对象注入进去」的方式给各模块传 current_user，
    所以调用点的实参名就是可靠信号。
    """
    src = APP_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported: dict[str, str] = {}      # 注册函数名 -> 模块名
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                imported[alias.asname or alias.name] = node.module

    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        fname = node.func.id
        if fname not in imported:
            continue
        passes_current_user = any(
            isinstance(a, ast.Name) and a.id == "current_user" for a in node.args
        )
        if passes_current_user:
            found[imported[fname]] = fname
    return found


class BearerBridgeCoversTokenRoutesTests(unittest.TestCase):
    def test_current_user_modules_are_reachable_by_token(self) -> None:
        prefixes = _protected_prefixes()
        modules = _modules_authed_by_current_user()
        self.assertTrue(
            modules,
            "一个都没找到，说明注册写法变了，本测试已失效 —— 修探测逻辑，别删测试",
        )

        uncovered: list[str] = []
        for module, register_fn in sorted(modules.items()):
            path = DEPLOY / f"{module}.py"
            if not path.exists():
                continue
            for route in ROUTE_RE.findall(path.read_text(encoding="utf-8")):
                if not route.startswith("/"):
                    continue
                if not any(route.startswith(p) for p in prefixes):
                    uncovered.append(f"{module}.py {route}（经 {register_fn} 注册）")

        self.assertEqual(
            uncovered,
            [],
            "这些端点用 current_user 鉴权，但路径不在 PROTECTED_PREFIXES 里。\n"
            "浏览器带 cookie 时一切正常，拿 API token 的客户端会恒 401，\n"
            "而 401 的字面意思会把人引去查 token。\n"
            "把前缀加进 app.py 的 PROTECTED_PREFIXES：\n  "
            + "\n  ".join(uncovered),
        )

    def test_kg_prefix_is_bridged(self) -> None:
        # 上一条依赖 AST 探测；探测一旦失效会静默退化成空检查。
        # 这里钉住已知的那一个，作为探测本身还活着的证据。
        self.assertIn("/api/kg", _protected_prefixes())


if __name__ == "__main__":
    unittest.main()
