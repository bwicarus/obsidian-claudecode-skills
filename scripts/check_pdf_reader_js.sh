#!/usr/bin/env bash
# 语法校验 PDF 阅读器主模块 JS。2026-06 起主逻辑从 pdf_reader.html 内联 <script type="module">
# 抽成独立文件 _server_deploy/static/pdf/reader.js;把 top-level await 包进 async IIFE 后 node --check。
# 用法: bash scripts/check_pdf_reader_js.sh [可选 js 路径]
set -euo pipefail
JS_SRC="${1:-_server_deploy/static/pdf/reader.js}"
TMP="$(mktemp /tmp/pdfreader_XXXX.mjs)"
/usr/bin/python3 - "$JS_SRC" "$TMP" <<'PY'
import re, sys
js = open(sys.argv[1], encoding="utf-8").read()
# 兜底去 Jinja(正常 reader.js 已无;配置走 window.__PDF_CFG)
js = re.sub(r'\{\{.*?\}\}', '0', js, flags=re.S)
js = re.sub(r'\{%.*?%\}', '', js, flags=re.S)
# top-level await 包进 async IIFE,让 node --check 不报 "await is only valid in async"
open(sys.argv[2], "w", encoding="utf-8").write("(async()=>{\n" + js + "\n})();")
PY
node --check "$TMP" && echo "✅ JS OK ($JS_SRC)"
rm -f "$TMP"
