#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""规范库版本化发布器(任务书 A2)。

把 `reader-specs/` 打包成一个**带 manifest 的不可变版本**,原子切换 `current` 指针。
Windows 侧经既有 SSH 只读拉取,不需要 Pi 反向登录。

为什么要原子:Windows 同步器可能在任意时刻拉取。若直接往固定目录里逐个覆盖文件,
它会拿到"新 AGENTS.md + 旧 specs"这种半套组合,而 AGENTS.md 的路由表指向的文件版本
对不上——这类不一致极难在对话里发现。所以:先在临时目录写全 → 校验 → 再 rename 指针。

manifest 字段:version(内容哈希派生,同内容不产生新版本)、files[{path,sha256,bytes}]、
updatedAt、specVersion(人读的合同名)。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
SRC = ROOT / "reader-specs"
OUT = Path(os.environ.get("BW_SPECS_ROOT", str(ROOT / "state" / "reader-specs-dist")))
CONTRACT = "reader-specs/1"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# 规范库收录的文件类型:.md=人读规范;.jsonl=跨端事件 fixture(消费方照它写解析)。
# fixture 必须进 manifest,否则 Windows 拉不到带哈希的确定性副本 —— 那就失去了版本化的意义。
SPEC_SUFFIXES = (".md", ".jsonl")


def collect(src: Path) -> list[dict]:
    out = []
    for p in sorted(x for x in src.rglob("*") if x.suffix in SPEC_SUFFIXES):
        rel = p.relative_to(src).as_posix()
        out.append({"path": rel, "sha256": _sha(p), "bytes": p.stat().st_size})
    return out


def build_manifest(files: list[dict]) -> dict:
    # 版本 = 全部文件内容的稳定摘要 → 内容没变就不会产生新版本,同步器可据此跳过
    h = hashlib.sha256()
    for f in files:
        h.update(f["path"].encode("utf-8")); h.update(b"\0")
        h.update(f["sha256"].encode("ascii")); h.update(b"\0")
    return {
        "contract": CONTRACT,
        "version": h.hexdigest()[:16],
        "files": files,
        "updatedAt": int(time.time()),
        "entry": "AGENTS.md",
    }


def publish(src: Path = SRC, out: Path = OUT, dry: bool = False) -> dict:
    if not (src / "AGENTS.md").is_file():
        raise SystemExit(f"缺少入口文件:{src/'AGENTS.md'}")
    files = collect(src)
    man = build_manifest(files)
    if dry:
        return man
    out.mkdir(parents=True, exist_ok=True)
    rel_dir = out / "releases" / man["version"]
    if not rel_dir.exists():
        stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=str(out)))
        try:
            for f in files:
                dst = stage / f["path"]
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src / f["path"], dst)
            (stage / "manifest.json").write_text(
                json.dumps(man, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            # 落盘前自校验:哈希对不上就不发布(宁可没有新版本,也不发半套/错套)
            for f in files:
                if _sha(stage / f["path"]) != f["sha256"]:
                    raise SystemExit(f"暂存校验失败:{f['path']}")
            rel_dir.parent.mkdir(parents=True, exist_ok=True)
            os.rename(stage, rel_dir)
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise
    # 原子切换指针:先建临时符号链接再 rename 覆盖,读者永远看到完整一版
    cur, tmp = out / "current", out / ".current.new"
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    tmp.symlink_to(rel_dir.relative_to(out))
    os.replace(tmp, cur) if cur.is_symlink() or cur.exists() else tmp.rename(cur)
    return man


def main() -> int:
    ap = argparse.ArgumentParser(description="发布版本化规范库")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args()
    man = publish(out=Path(a.out), dry=a.dry_run)
    print(json.dumps({"version": man["version"], "files": len(man["files"]),
                      "entry": man["entry"], "dry": a.dry_run}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
