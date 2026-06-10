#!/bin/bash
# PDF 阅读器一键部署(Pi):重拼 bundle → 语法检查 → 部署(带 git 戳) → restart webapp → E2E 冒烟。
# 用法:  bash scripts/deploy_reader.sh [--no-e2e] [--pc]
#   --no-e2e  跳过 Playwright 冒烟(紧急用)
#   --pc      额外 scp 到 Windows PC(离线则警告跳过)
# 治三个反复踩的坑:① 忘了重拼 bundle ② 改模板忘 restart(gunicorn Jinja 缓存)③ 部署完不验证。
# 注:仓库里的 reader.js 保持「纯 cat reader.src/*.js」不变;git 戳只追加在**部署副本**末尾
#    (浏览器 console 看 window.__READER_GIT 即知用户跑的是哪个 commit)。
set -e
cd "$(dirname "$0")/.."

SRC=_server_deploy/static/pdf/reader.src
OUT=_server_deploy/static/pdf/reader.js

echo "── ① 重拼 bundle"
cat $(ls $SRC/*.js | sort) > "$OUT"
node --check "$OUT"
echo "   reader.js $(wc -c < "$OUT")B ✓"

echo "── ② 部署 Pi"
STAMP=";window.__READER_GIT='$(git rev-parse --short HEAD 2>/dev/null || echo dev)+$(git diff --quiet -- $SRC 2>/dev/null && echo clean || echo dirty)·$(date +%m%d-%H%M)';"
{ cat "$OUT"; echo "$STAMP"; } > /var/www/html/static/pdf/reader.js
cp _server_deploy/templates/pdf_reader.html _server_deploy/templates/pdf_index.html /home/bwicarus/webapp/templates/
cp _server_deploy/pdf_reader.py /home/bwicarus/webapp/pdf_reader.py
python3 -m py_compile /home/bwicarus/webapp/pdf_reader.py
sudo systemctl restart webapp
sleep 3
code=$(curl -s -o /dev/null -w '%{http_code}' -m 8 http://127.0.0.1:5000/login)
[ "$code" = "200" ] || { echo "❌ webapp 重启后 /login=$code"; exit 1; }
echo "   webapp active, /login 200 ✓  $STAMP"

if [[ "$*" == *--pc* ]]; then
  echo "── ②b 同步 PC"
  scp -o ConnectTimeout=10 "$OUT" bwicarus@100.99.9.124:C:/claude/_server_deploy/static/pdf/reader.js \
    && scp -o ConnectTimeout=10 $SRC/*.js bwicarus@100.99.9.124:C:/claude/_server_deploy/static/pdf/reader.src/ \
    && scp -o ConnectTimeout=10 _server_deploy/templates/pdf_reader.html _server_deploy/templates/pdf_index.html bwicarus@100.99.9.124:C:/claude/_server_deploy/templates/ \
    && scp -o ConnectTimeout=10 _server_deploy/pdf_reader.py bwicarus@100.99.9.124:C:/claude/_server_deploy/pdf_reader.py \
    && echo "   PC ✓" || echo "   ⚠ PC 离线/失败,稍后手动 scp 或 git pull"
fi

if [[ "$*" != *--no-e2e* ]]; then
  echo "── ③ E2E 冒烟"
  set -a; source .env 2>/dev/null || true; set +a
  python3 scripts/reader_e2e.py
fi
echo "✅ 部署完成"
