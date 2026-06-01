#!/usr/bin/env bash
# 把 reader.src/*.js 按 NN- 数字前缀顺序拼接成单文件 reader.js(运行时仍是一个 ES module)。
# 改前端 = 改 reader.src/ 里对应功能文件 → 跑本脚本重建 → cp reader.js 到静态目录。
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)/_server_deploy/static/pdf"
cat "$DIR"/reader.src/*.js > "$DIR/reader.js"
echo "built $DIR/reader.js ($(wc -l < "$DIR/reader.js") 行 from $(ls "$DIR"/reader.src/*.js | wc -l) 个源文件)"
