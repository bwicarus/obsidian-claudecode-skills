#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 vault 里 >200MB 的文件(超 Obsidian Sync Plus 单文件上限)从 Pi 推送到 PC。

背景:Obsidian Sync(Plus)单文件上限 200MB,≤200MB 的笔记/PDF 它自己双向同步;
>200MB 的(目前全 vault 仅 応用情報技術者.pdf 318M)它永远不同 → 走这条旁路。

为什么 Pi 主动推、而不是 PC 拉:PC 会睡眠/间歇掉 Tailscale,Pi 永远在线。
Pi push「PC 可达才推、不可达静默跳过、下次 timer 再补」对 PC 的不稳定最鲁棒,
且 Pi→PC 免密 SSH 本来就通(见 memory pc-ssh-access),不用配新密钥。

由 Pi 的 systemd timer(push-big-files.timer)定时跑。幂等:同名同大小就跳过,不重传。
"""
from __future__ import annotations

import base64
import subprocess
import sys
from pathlib import Path

PC = "bwicarus@100.99.9.124"
VAULT = Path("/home/bwicarus/obsidian")
PC_VAULT = "C:/obsidian"
THRESHOLD = 200 * 1024 * 1024   # >200MiB 才走旁路(Obsidian 管 ≤200MB)


def _ps(cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
    enc = base64.b64encode(cmd.encode("utf-16-le")).decode()
    return subprocess.run(
        ["ssh", "-o", "ConnectTimeout=15", PC, "powershell -NoProfile -EncodedCommand " + enc],
        capture_output=True, text=True, timeout=timeout)


def pc_reachable() -> bool:
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", PC, "cmd /c echo OK"],
                           capture_output=True, text=True, timeout=20)
        return "OK" in r.stdout
    except Exception:
        return False


def pc_size(win_path: str) -> int:
    """PC 上该文件大小(字节);不存在返回 -1。"""
    try:
        r = _ps(f"$p='{win_path}'; if(Test-Path -LiteralPath $p){{(Get-Item -LiteralPath $p).Length}}else{{-1}}")
        for tok in r.stdout.split():
            t = tok.strip()
            if t.lstrip("-").isdigit():
                return int(t)
    except Exception:
        pass
    return -1


def main() -> int:
    if not pc_reachable():
        print("PC 不可达(睡眠/离线),跳过 —— 下次 timer 再补", flush=True)
        return 0
    big = [f for f in VAULT.rglob("*")
           if f.is_file() and not f.name.startswith(".") and "/." not in f.as_posix()
           and f.stat().st_size > THRESHOLD]
    print(f"vault 中 >200MB 文件:{len(big)}", flush=True)
    pushed = skipped = failed = 0
    for f in big:
        rel = f.relative_to(VAULT)
        size = f.stat().st_size
        win_native = (PC_VAULT + "/" + str(rel).replace("\\", "/")).replace("/", "\\")
        if pc_size(win_native) == size:
            print(f"=  已同步  {rel} ({size//1048576}MB)", flush=True)
            skipped += 1
            continue
        # 建目标目录(New-Item -Force 建中间层)+ scp
        parent = "\\".join(win_native.split("\\")[:-1])
        _ps(f"New-Item -ItemType Directory -Force -Path '{parent}' | Out-Null")
        dst_dir = str(rel.parent).replace("\\", "/")
        dst = f"{PC}:{PC_VAULT}/" + (dst_dir + "/" if dst_dir != "." else "")
        try:
            r = subprocess.run(["scp", "-o", "ConnectTimeout=20", str(f), dst],
                               capture_output=True, text=True, timeout=3600)
            ok = r.returncode == 0
        except Exception as e:
            ok = False
            r = type("X", (), {"stderr": str(e)})()
        if ok:
            print(f"✓  推送    {rel} ({size//1048576}MB)", flush=True)
            pushed += 1
        else:
            print(f"✗  失败    {rel}: {getattr(r, 'stderr', '')[:160]}", flush=True)
            failed += 1
    print(f"完成:推送 {pushed}、已同步 {skipped}、失败 {failed}", flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
