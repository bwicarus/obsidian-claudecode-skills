#!/usr/bin/env bash
# Xcode 编译前自动重新生成 ReaderBundle（2026-09-26）。
#
# 为什么要有它：App 用的是打进包里的 ReaderBundle（阅读器网页部分），xcodebuild 只是把
# Generated/ReaderBundle 原样拷进包，**不会**自己重新打包。改了 _server_deploy/static/pdf/*.js
# 却忘了重跑 package_local_reader.py，装上去的就是旧网页代码 —— 编译全绿、毫无提示
# （2026-09-26 连装两版都是这样）。用户要能在 Xcode 里随时按 Run 装机，所以放进编译步骤。
#
# CI 编译前已经自己生成并校验过（safari-extension-ios.yml），这里直接跳过。
# 失败就让编译失败：宁可装不上，也不要悄悄装一份旧的。
set -euo pipefail
if [ "${CI:-}" = "true" ] || [ -n "${GITHUB_ACTIONS:-}" ]; then
  echo "CI 已生成 ReaderBundle，跳过"; exit 0
fi
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${BW_PYTHON:-$HOME/BW/venv/server/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "error: 找不到 Python，无法重新生成 ReaderBundle（设 BW_PYTHON）"; exit 1; }
"$PY" "$HERE/package_local_reader.py" --output "$HERE/Generated/ReaderBundle" | tail -2
