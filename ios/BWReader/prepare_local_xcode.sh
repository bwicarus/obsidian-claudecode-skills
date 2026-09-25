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

# --dev：生成「开发签名」的工程（自动签名 + Apple Development），用于本机装到 iPad 调试。
#   不带时与 CI 相同（手动签名 + Apple Distribution + App Store 描述文件，用于 TestFlight）。
#   ⚠ 只在生成时临时改一份配置，project.yml 本身不动，CI 不受影响。
DEV_SIGNING=0
[ "${1:-}" = "--dev" ] && DEV_SIGNING=1

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
# 清空时保留仓库里的占位文件 .gitkeep，否则每次准备完工作区都显示它被删了
mkdir -p "$RESOURCES"
find "$RESOURCES" -mindepth 1 -maxdepth 1 ! -name .gitkeep -exec rm -rf {} +
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
SPEC="$HERE/project.yml"
if [ "$DEV_SIGNING" = 1 ]; then
  # 临时配置必须和 project.yml 同目录（里面的相对路径以配置文件所在目录为准）
  SPEC="$HERE/.project.dev-signing.yml"
  trap 'rm -f "$HERE/.project.dev-signing.yml"' EXIT
  "$PY" - "$HERE/project.yml" "$SPEC" <<'PY'
import sys
# 逐行改，不用正则：本段嵌在 shell heredoc 里，反斜杠转义一多就容易被改坏。
counts = [0, 0, 0]
out = []
for line in open(sys.argv[1], encoding="utf-8").read().splitlines(keepends=True):
    key = line.strip()
    if key.startswith("PROVISIONING_PROFILE_SPECIFIER:"):
        counts[2] += 1
        continue
    if key == "CODE_SIGN_STYLE: Manual":
        line = line.replace("Manual", "Automatic")
        counts[0] += 1
    elif key == "CODE_SIGN_IDENTITY: Apple Distribution":
        line = line.replace("Apple Distribution", "Apple Development")
        counts[1] += 1
    out.append(line)
open(sys.argv[2], "w", encoding="utf-8").write("".join(out))
print("开发签名：手动签名 %d 处→自动，发布证书 %d 处→开发证书，去掉描述文件 %d 处" % tuple(counts))
PY
fi
"$XCODEGEN" generate --spec "$SPEC" --project "$HERE"
xcodebuild -project "$HERE/BWReader.xcodeproj" -list | sed -n '1,30p'

printf '\n完成。用 Xcode 打开：open "%s"\n' "$HERE/BWReader.xcodeproj"
