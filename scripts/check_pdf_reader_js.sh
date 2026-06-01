#!/usr/bin/env bash
# PDF 阅读器主模块 JS 校验。2026-06 起 reader.js 由 reader.src/*.js 按 NN- 顺序拼接而成
# (运行时仍是一个 ES module)。本脚本:① 若有分块源则先重建 reader.js(保证校验/部署的是最新拼接)
# ② 把 top-level await 包 async IIFE 后 node --check。用法: bash scripts/check_pdf_reader_js.sh [可选 js 路径]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIR="$ROOT/_server_deploy/static/pdf"
# ① 有分块源 → 重建(= build_pdf_reader_js.sh),让校验的就是即将部署的内容
if compgen -G "$DIR/reader.src/*.js" > /dev/null 2>&1; then
  cat "$DIR"/reader.src/*.js > "$DIR/reader.js"
fi
JS_SRC="${1:-$DIR/reader.js}"
TMP="$(mktemp /tmp/pdfreader_XXXX.mjs)"
/usr/bin/python3 - "$JS_SRC" "$TMP" <<'PY'
import re, sys
js = open(sys.argv[1], encoding="utf-8").read()
js = re.sub(r'\{\{.*?\}\}', '0', js, flags=re.S)   # 兜底去 Jinja(正常已无;配置走 window.__PDF_CFG)
js = re.sub(r'\{%.*?%\}', '', js, flags=re.S)
open(sys.argv[2], "w", encoding="utf-8").write("(async()=>{\n" + js + "\n})();")
PY
node --check "$TMP" && echo "✅ JS OK ($JS_SRC)"
rm -f "$TMP"
