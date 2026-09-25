#!/usr/bin/env bash
# 在 Mac 上准备本机 Xcode 开发（2026-09-25 迁移到 Mac mini 后新增）。
#
# 做的事与 CI（.github/workflows/safari-extension-ios.yml）编译前的三步完全相同：
#   ① 打 Safari 扩展包，解到扩展目标的资源目录（Extension/Resources）
#   ② 生成 App 内置的本地阅读器 ReaderBundle（Generated/ReaderBundle）并校验
#   ③ 用固定版本的 XcodeGen 按 project.yml 生成 BWReader.xcodeproj
# 之后用 Xcode 打开 ios/BWReader/BWReader.xcodeproj 编译 / 装机 / 调试。
#
# ⚠ 改了 _server_deploy/static/pdf/*.js（阅读器前端）或扩展代码后要重跑本脚本：
#   App 用的是打进包里的 ReaderBundle，不重跑就还是旧的那份。
#   只改了 Swift 代码则不必重跑（除非新增 / 删除了文件：那要重跑 ③）。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
PY="${BW_PYTHON:-$HOME/BW/venv/server/bin/python}"
XCODEGEN="${XCODEGEN:-$HOME/.local/bin/xcodegen}"
XCODEGEN_VERSION="2.46.0"   # 与 CI 的 XCODEGEN_VERSION 保持一致
BUNDLE_ID="space.bwicarus.bwreader2"
APP_NAME="bwicarus-test"

step() { printf '\n== %s\n' "$1"; }

[ -x "$PY" ] || { echo "找不到 Python：$PY（设 BW_PYTHON 指定）"; exit 1; }
[ -x "$XCODEGEN" ] || { echo "找不到 XcodeGen：$XCODEGEN（设 XCODEGEN 指定）"; exit 1; }
"$XCODEGEN" --version | grep -qF "$XCODEGEN_VERSION" \
  || { echo "XcodeGen 版本不是 $XCODEGEN_VERSION（CI 固定用这一版，版本不同生成的工程会漂移）"; exit 1; }

step "① Safari 扩展包"
cd "$ROOT/extensions/bw-reader-webext"
OUTPUT="$("$PY" package_safari.py)"
printf '%s\n' "$OUTPUT" | tail -3
PACKAGE="$(printf '%s\n' "$OUTPUT" | sed -n 's/^package=//p')"
REPORTED_BUNDLE_ID="$(printf '%s\n' "$OUTPUT" | sed -n 's/^bundle_id=//p')"
[ -f "$PACKAGE" ] || { echo "package_safari.py 没有报出 package=…"; exit 1; }
[ "$REPORTED_BUNDLE_ID" = "$BUNDLE_ID" ] || { echo "扩展包 bundle ID 漂移：$REPORTED_BUNDLE_ID"; exit 1; }
RESOURCES="$HERE/Extension/Resources"
rm -rf "$RESOURCES"
mkdir -p "$RESOURCES"
unzip -q "$PACKAGE" -d "$RESOURCES"
for need in manifest.json background.js src vendor icons; do
  [ -e "$RESOURCES/$need" ] || { echo "扩展资源缺 $need"; exit 1; }
done
"$PY" - "$RESOURCES/manifest.json" "$APP_NAME" <<'PY'
import json, sys
name = json.load(open(sys.argv[1], encoding="utf-8")).get("name")
if name != sys.argv[2]:
    raise SystemExit(f"扩展 manifest 名称漂移：{name!r} != {sys.argv[2]!r}")
print("扩展 manifest 名称：", name)
PY

step "② 本地阅读器 ReaderBundle"
READER_BUNDLE="$HERE/Generated/ReaderBundle"
"$PY" "$HERE/package_local_reader.py" --output "$READER_BUNDLE"
"$PY" "$HERE/package_local_reader.py" --verify "$READER_BUNDLE"
[ -f "$READER_BUNDLE/bundle-manifest.json" ] || { echo "ReaderBundle 缺 bundle-manifest.json"; exit 1; }

step "③ 生成 Xcode 工程"
rm -rf "$HERE/BWReader.xcodeproj"
"$XCODEGEN" generate --spec "$HERE/project.yml" --project "$HERE"
xcodebuild -project "$HERE/BWReader.xcodeproj" -list | sed -n '1,30p'

printf '\n完成。用 Xcode 打开：open "%s"\n' "$HERE/BWReader.xcodeproj"
