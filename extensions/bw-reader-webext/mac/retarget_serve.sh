#!/bin/bash
# 把 tailscale serve 里所有指向 127.0.0.1:5000（webapp 旧端口）的路由改到新端口。
# 2026-09-26：5000 被 macOS「隔空播放接收器」占着，iPad 请求会随机收到它的 403，webapp 改用 5055。
# 用法：extensions/bw-reader-webext/mac/retarget_serve.sh [新端口，默认 5055]
# 先部署（deploy_mac.py 会让 webapp 监听新端口），再跑这个。只改 5000 的那些行，桥 / MCP 等其它路由不动。
set -euo pipefail
PORT="${1:-5055}"
TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
[ -x "$TS" ] || { echo "找不到 $TS" >&2; exit 1; }

if ! curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/login" | grep -qE '^(200|302)$'; then
  echo "webapp 还没在 127.0.0.1:$PORT 上响应 —— 先跑 deploy_mac.py --only webapp 再来" >&2
  exit 1
fi

changed=0
while read -r path target; do
  [ -n "$path" ] || continue
  new="${target/127.0.0.1:5000/127.0.0.1:$PORT}"
  echo "$path  →  $new"
  "$TS" serve --bg --set-path "$path" "$new" >/dev/null
  changed=$((changed + 1))
done < <("$TS" serve status | awk '$3 == "proxy" && $4 ~ /^http:\/\/127\.0\.0\.1:5000/ {print $2, $4}')

echo "改了 $changed 条。现在指向旧端口 5000 的还剩：$("$TS" serve status | grep -c '127.0.0.1:5000' || true) 条"
