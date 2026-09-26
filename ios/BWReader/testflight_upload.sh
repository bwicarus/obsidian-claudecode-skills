#!/bin/bash
# 本机出 TestFlight（Apple 官方流程：xcodebuild archive → exportArchive 直接上传）。
# 签名与构建号都交给 Xcode 登录的账号自动管理（signingStyle=automatic、
# manageAppVersionAndBuildNumber=true：构建号自动取下一个可用号）。
# 必须在 Mac 桌面会话里跑（远程 SSH 会话不能签名）。
set -euo pipefail
cd "$(dirname "$0")"
OUT="${HOME}/BW/xcode-derived/archive"
rm -rf "$OUT"; mkdir -p "$OUT"
cat > "$OUT/ExportOptions.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>method</key><string>app-store-connect</string>
<key>destination</key><string>upload</string>
<key>signingStyle</key><string>automatic</string>
<key>teamID</key><string>7MDVSLPV8F</string>
<key>manageAppVersionAndBuildNumber</key><true/>
<key>uploadSymbols</key><true/>
</dict></plist>
PLIST
xcodebuild archive -project BWReader.xcodeproj -scheme BWReader -destination generic/platform=iOS \
  -archivePath "$OUT/app.xcarchive" -allowProvisioningUpdates | grep -E "error:|ARCHIVE (SUCCEEDED|FAILED)"
xcodebuild -exportArchive -archivePath "$OUT/app.xcarchive" -exportOptionsPlist "$OUT/ExportOptions.plist" \
  -exportPath "$OUT/export" -allowProvisioningUpdates 2>&1 | grep -E "error|Upload succeeded|EXPORT (SUCCEEDED|FAILED)"
