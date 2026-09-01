"""翻译服务的 Windows 侧入口（2026-09-01 用户拍板：翻译迁「Windows+本地」）。

stdin 一行 JSON：{"texts": ["...", ...], "target": "zh-CN"}
stdout 一行 JSON：{"ok": true, "results": ["...", ...]}（失败项为空串）

执行与缓存都在本机：复用 scripts/vocab/translate.py 的多源链
（gtranslate → deepl → mymemory），缓存写 C:\\claude\\state\\dict-cache
（永久，按内容哈希 —— 重复内容零 API 成本）。桥 C# 起子进程调用。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

MAX_TEXTS = 64
MAX_TEXT_CHARS = 4000


def main() -> int:
    project = Path(os.environ.get("CLAUDE_PROJECT", r"C:\claude"))
    os.environ["CLAUDE_PROJECT"] = str(project)
    vocab = project / "scripts" / "vocab"
    if not (vocab / "translate.py").is_file():
        # 打包运行时旁挂了一份（readerpc-runtime/scripts/vocab）。
        vocab = Path(__file__).resolve().parent / "scripts" / "vocab"
    sys.path.insert(0, str(vocab))
    try:
        import translate  # noqa: PLC0415
    except Exception as error:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"import: {error}"}))
        return 1

    try:
        request = json.loads(sys.stdin.readline() or "{}")
    except ValueError:
        print(json.dumps({"ok": False, "error": "bad json"}))
        return 1
    texts = request.get("texts")
    target = str(request.get("target") or "zh-CN")[:16]
    if not isinstance(texts, list) or not texts or len(texts) > MAX_TEXTS:
        print(json.dumps({"ok": False, "error": "texts must be 1..64"}))
        return 1

    results: list[str] = []
    for one in texts:
        text = str(one or "").strip()[:MAX_TEXT_CHARS]
        if not text:
            results.append("")
            continue
        try:
            results.append(translate.translate(text, target) or "")
        except Exception:  # noqa: BLE001
            results.append("")
    print(json.dumps({"ok": True, "results": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
