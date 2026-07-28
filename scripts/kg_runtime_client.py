"""Stable client used by non-webapp orchestrators to pin KG executable code.

Linux production resolves through the deployed webapp copy of ``kg_runtime``;
it never imports KG algorithms from the mutable checkout.  Windows remains an
explicit development environment and may execute its checked-out KG scripts.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys


DEFAULT_PRODUCTION_RESOLVER = Path("/home/bwicarus/webapp/kg_runtime.py")
_SAFE_KG_FILE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\.py")


class KgRuntimeClientError(RuntimeError):
    pass


class _WindowsDevelopmentRelease:
    def __init__(self, project_root: Path):
        self.release = project_root.resolve()
        self.deploy_id = "windows-development-checkout"

    def runtime_file(self, relative: str) -> Path:
        path = PurePosixPath(str(relative))
        if (
            path.parts[:2] != ("scripts", "kg")
            or len(path.parts) != 3
            or not _SAFE_KG_FILE.fullmatch(path.name)
        ):
            raise KgRuntimeClientError("Windows KG 开发路径无效")
        target = self.release.joinpath(*path.parts)
        try:
            mode = target.lstat().st_mode
        except OSError as exc:
            raise KgRuntimeClientError("Windows KG 开发脚本缺失") from exc
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise KgRuntimeClientError("Windows KG 开发脚本不是实体普通文件")
        return target


def _load_production_resolver(path: Path):
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise KgRuntimeClientError(
            f"KG runtime resolver 不可用: {path}"
        ) from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise KgRuntimeClientError("KG runtime resolver 必须是实体普通文件")
    module_name = "_bw_reader_deployed_kg_runtime"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise KgRuntimeClientError("KG runtime resolver 无法加载")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    actual = Path(getattr(module, "__file__", "")).resolve()
    if actual != path.resolve():
        raise KgRuntimeClientError("KG runtime resolver 加载身份不一致")
    if not callable(getattr(module, "pin_release", None)):
        raise KgRuntimeClientError("KG runtime resolver 缺少 pin_release")
    return module


def pin(*, project_root: Path | None = None):
    """Pin one release for a complete register/daily KG batch."""

    root = Path(
        project_root
        or os.environ.get("CLAUDE_PROJECT")
        or Path(__file__).resolve().parents[1]
    )
    if os.name == "nt":
        return _WindowsDevelopmentRelease(root)
    resolver = Path(
        os.environ.get(
            "BW_READER_KG_RESOLVER",
            str(DEFAULT_PRODUCTION_RESOLVER),
        )
    )
    try:
        return _load_production_resolver(resolver).pin_release()
    except Exception as exc:
        if isinstance(exc, KgRuntimeClientError):
            raise
        raise KgRuntimeClientError(
            f"KG runtime 无法固定: {exc}"
        ) from exc


__all__ = [
    "DEFAULT_PRODUCTION_RESOLVER",
    "KgRuntimeClientError",
    "pin",
]
