#!/usr/bin/env python3
"""把 PDF 重存为线性化(Fast Web View)——无损,只把页对象按阅读顺序重排 + 首页/xref 放文件头,
让 PDF.js 用 url 打开时取文件头即可渲首页、后续页 byte-range 流式更快(大书网络打开提速明显)。

用法: python linearize_pdf.py <pdf> [<pdf> ...]
      python linearize_pdf.py --vault-larger-than 20   # 批量:vault 里 ≥20MB 的 PDF 全线性化

实现:用 qpdf --linearize(本版 PyMuPDF/MuPDF 已移除 linear=True)。只重排不改内容,
字符层/页码/坐标全不变。原子替换(临时文件 + os.replace),失败不动原文件。
注:会改文件 mtime → pdf_reader 的 char 缓存 key 和前端 IndexedDB 缓存 key 随之失效、自动重建/重下(一次性)。
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path


def linearize(path: Path) -> bool:
    path = Path(path)
    if not path.exists() or path.suffix.lower() != ".pdf":
        print(f"✗ 跳过(不存在/非 PDF): {path}")
        return False
    if not shutil.which("qpdf"):
        print("✗ 需要 qpdf:  sudo apt install qpdf")
        return False
    tmp = path.with_suffix(".linearizing.tmp.pdf")
    try:
        before = path.stat().st_size
        rc = subprocess.run(["qpdf", "--linearize", str(path), str(tmp)],
                            capture_output=True, timeout=600).returncode
        if rc not in (0, 3) or not tmp.exists():   # 0=ok,3=warnings(仍产出有效文件)
            tmp.unlink(missing_ok=True)
            print(f"✗ {path.name}: qpdf rc={rc}")
            return False
        os.replace(str(tmp), str(path))   # 原子替换(同盘 rename)
        after = path.stat().st_size
        print(f"✓ {path.name}: {before/1048576:.1f}MB → {after/1048576:.1f}MB (已线性化)")
        return True
    except Exception as ex:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        print(f"✗ {path.name}: 线性化失败 {ex}")
        return False


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    if args[0] == "--vault-larger-than":
        mb = float(args[1]) if len(args) > 1 else 20.0
        vault = Path(os.environ.get("OBSIDIAN_VAULT", "/home/bwicarus/obsidian"))
        targets = [p for p in vault.rglob("*.pdf")
                   if p.is_file() and p.stat().st_size >= mb * 1048576]
        print(f"vault 里 ≥{mb}MB 的 PDF: {len(targets)} 个")
    else:
        targets = [Path(a) for a in args]
    ok = sum(1 for t in targets if linearize(t))
    print(f"\n完成: {ok}/{len(targets)} 线性化成功")
    return 0


if __name__ == "__main__":
    sys.exit(main())
