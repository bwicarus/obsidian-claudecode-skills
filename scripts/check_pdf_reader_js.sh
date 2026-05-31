#!/usr/bin/env bash
# 提取 pdf_reader.html 的 <script type="module"> 块 → 去 Jinja {{ }}/{% %} → node --check
# 用法: bash scripts/check_pdf_reader_js.sh [可选 html 路径]
set -euo pipefail
HTML="${1:-_server_deploy/templates/pdf_reader.html}"
TMP="$(mktemp /tmp/pdfreader_XXXX.mjs)"
/usr/bin/python3 - "$HTML" "$TMP" <<'PY'
import re, sys
html = open(sys.argv[1], encoding="utf-8").read()
# 抓最后一个 <script type="module"> ... </script>（主逻辑块）
blocks = re.findall(r'<script type="module">(.*?)</script>', html, re.S)
if not blocks:
    print("NO module script found", file=sys.stderr); sys.exit(2)
js = max(blocks, key=len)
# 去 Jinja：{{ x|tojson }} → 占位字面量；{% ... %} → 删
js = re.sub(r'\{\{.*?\}\}', '0', js, flags=re.S)
js = re.sub(r'\{%.*?%\}', '', js, flags=re.S)
# top-level await 包进 async IIFE 才能让 node --check 不报 "await is only valid in async"
wrapped = "(async()=>{\n" + js + "\n})();"
open(sys.argv[2], "w", encoding="utf-8").write(wrapped)
PY
node --check "$TMP" && echo "✅ JS OK ($HTML)"
rm -f "$TMP"
