#!/usr/bin/env python3
"""我改的这些文件，要投递到哪些表面才算到达？

## 为什么需要它

2026-08-19：同一个错犯了三次 —— 改完阅读器前端就部署 Pi，然后告诉用户"好了"。
可用户在 **App** 上，而 App 加载的是打进包的 ReaderBundle，**根本不走 nginx**：
部署 Pi 对他完全无效。用户第三次指出来时的原话是「你又部署到 pi 是什么意思，
这不是 windows 和 app 的链接么」。

讽刺的是 CLAUDE.md 里那条「三条互不相干的投递路径」正是我当天早些时候刚改准的，
然后自己没照着做。**写进文档不等于会被执行**；能跑的判据才会。

## 判据从哪来

不硬编码文件清单（那样它自己就会过期）：
  · Pi/nginx      ← `scripts/reader_deploy_manifest.py` 的输出
  · App           ← `ios/BWReader/package_local_reader.py` 实际烤进 ReaderBundle 的东西
  · Safari 扩展   ← `extensions/bw-reader-webext/build.py` 的 FILES + 扩展自身文件
  · Windows 桥    ← `extensions/bw-reader-webext/windows/` 下的 C#/打包脚本

## 用法

    python scripts/where_does_this_file_go.py                    # 看当前未提交的改动
    python scripts/where_does_this_file_go.py --since HEAD~1     # 看上一个提交改了什么
    python scripts/where_does_this_file_go.py a.js b.py          # 指定文件
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(args: list[str]) -> str:
    try:
        res = subprocess.run(args, cwd=ROOT, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=180)
    except (OSError, subprocess.SubprocessError):
        return ""
    return res.stdout if res.returncode == 0 else ""


def _deploy_manifest() -> set[str]:
    out = _run([sys.executable, str(ROOT / "scripts" / "reader_deploy_manifest.py")])
    return {ln.split("\t")[0] for ln in out.splitlines() if "\t" in ln}


def _vendor_files() -> set[str]:
    """build.py 的 FILES = 会被同步进扩展 vendor 的共享层文件。"""
    src = (ROOT / "extensions" / "bw-reader-webext" / "build.py").read_text(
        "utf-8", errors="replace")
    block = re.search(r"FILES\s*=\s*\[(.*?)\]", src, re.S)
    if not block:
        return set()
    return {f"_server_deploy/static/pdf/{name}"
            for name in re.findall(r'"([^"]+\.js)"', block.group(1))}


def _changed(since: str | None) -> list[str]:
    if since:
        out = _run(["git", "diff", "--name-only", since])
    else:
        out = _run(["git", "status", "--porcelain"])
        return sorted({ln[3:].strip() for ln in out.splitlines() if ln[3:].strip()})
    return sorted({ln.strip() for ln in out.splitlines() if ln.strip()})


def surfaces_for(rel: str, manifest: set[str], vendor: set[str]) -> list[tuple[str, str]]:
    """→ [(表面, 怎么到达)]。空 = 这个文件不投递到任何设备表面。"""

    out: list[tuple[str, str]] = []
    p = rel.replace("\\", "/")

    if p in manifest:
        out.append(("桌面 / 旧网页表面", "bash scripts/deploy_reader.sh（Windows: "
                                        "powershell -ExecutionPolicy Bypass -File "
                                        "scripts\\deploy_from_windows.ps1）"))
    # 阅读器前端会被烤进 App 的 ReaderBundle
    if p.startswith("_server_deploy/static/pdf/") or p.startswith("_server_deploy/templates/"):
        out.append(("iPad App", "gh workflow run safari-extension-ios.yml "
                                "--ref <分支> -f upload=true —— **只能靠新的 TestFlight 构建**"))
    if p in vendor or p.startswith("extensions/bw-reader-webext/"):
        if "/windows/" not in p:
            out.append(("Safari 扩展", "python extensions/bw-reader-webext/build.py "
                                       "重新生成 vendor/，然后随 iOS 构建打包"))
    if "/windows/ComputerVoiceAudio/" in p or "/windows/" in p:
        out.append(("Windows 桥", "python extensions/bw-reader-webext/windows/"
                                  "package_computer_voice_direct.py --build <版本> "
                                  "→ --self-test → --install"))
    if p.startswith("_server_deploy/") and p.endswith(".py") and p not in manifest:
        out.append(("Pi（清单外）", "手工 cp 到 webapp + systemctl restart webapp"))
    if p.startswith("ios/"):
        out.append(("iPad App", "gh workflow run safari-extension-ios.yml "
                                "--ref <分支> -f upload=true"))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("files", nargs="*", help="文件路径；省略则看当前未提交改动")
    parser.add_argument("--since", help="改成看 git diff --name-only <ref>")
    args = parser.parse_args()

    files = args.files or _changed(args.since)
    if not files:
        print("没有改动。")
        return 0

    manifest = _deploy_manifest()
    vendor = _vendor_files()
    if not manifest:
        print("⚠ 读不到部署清单（reader_deploy_manifest.py 跑不起来），"
              "「桌面表面」这一列会缺 —— 别把它当成「不需要部署」。\n")

    by_surface: dict[str, set[str]] = {}
    inert: list[str] = []
    for rel in files:
        hits = surfaces_for(rel, manifest, vendor)
        if not hits:
            inert.append(rel)
            continue
        for surface, how in hits:
            by_surface.setdefault(f"{surface}\n      {how}", set()).add(rel)

    print(f"{len(files)} 个改动文件 → {len(by_surface)} 个投递表面\n")
    for key, rels in sorted(by_surface.items()):
        print(f"  ▸ {key}")
        for rel in sorted(rels):
            print(f"        {rel}")
        print()
    if inert:
        print(f"  不投递到设备表面（测试/脚本/文档等）：{len(inert)} 个")
        for rel in sorted(inert)[:8]:
            print(f"        {rel}")
        if len(inert) > 8:
            print(f"        …另 {len(inert) - 8} 个")
    print("\n⚠ 一个文件出现在多个表面时，**做一个不等于做完**："
          "\n   部署 Pi 只到桌面，App 要新构建，扩展要重新打包。三条互不相干。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
