"""扫描本地目录并逐个 POST 到服务端 /api/upload/<dataset>。

只传"网页内容"，不传"网页代码"：
  - 数据文件（.json）+ 用户媒体（图片）→ 上传
  - HTML/CSS/JS 等前端代码 → 客户端不碰（admin 通过其他渠道发布）
"""
from __future__ import annotations

from pathlib import Path
from typing import Generator

ALLOWED_SUFFIX = {".json", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
SKIP_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
MAX_FILE_BYTES = 20 * 1024 * 1024  # 20MB


def upload_dataset(client, root: Path, dataset: str) -> Generator[str, None, None]:
    if not root.exists() or not root.is_dir():
        yield f"✗ {dataset}：目录不存在 {root}"
        return

    files: list[Path] = []
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in ALLOWED_SUFFIX and p.name not in SKIP_NAMES:
            if p.stat().st_size <= MAX_FILE_BYTES:
                files.append(p)

    if not files:
        yield f"⊘ {dataset}：没有可上传的文件 ({root})"
        return

    yield f"开始上传 {dataset} → {len(files)} 个文件"
    ok = 0
    failed: list[tuple[Path, str]] = []
    for i, p in enumerate(files, 1):
        rel = p.relative_to(root).as_posix()
        try:
            data = p.read_bytes()
            client.upload(dataset, rel, data)
            ok += 1
            yield f"  [{i}/{len(files)}] ✓ {rel}  ({len(data):,} bytes)"
        except Exception as e:
            failed.append((p, str(e)))
            yield f"  [{i}/{len(files)}] ✗ {rel}  {e}"

    if failed:
        yield f"{dataset} 完成：{ok}/{len(files)} 成功，{len(failed)} 失败"
    else:
        yield f"{dataset} 完成：{ok}/{len(files)} 全部成功 ✓"


def upload_dashboard(client, root: Path) -> Generator[str, None, None]:
    """向后兼容的 thin wrapper。"""
    yield from upload_dataset(client, root, "dashboard")
