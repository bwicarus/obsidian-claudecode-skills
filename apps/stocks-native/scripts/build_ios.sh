#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-simulator}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
PROJECT_DIR="$PROJECT_ROOT/ios/StocksNative"
PROJECT_FILE="$PROJECT_DIR/StocksNative.xcodeproj"
BUILD_ROOT="${STOCKS_BUILD_ROOT:-$PROJECT_ROOT/.build/ios}"
BUILD_NUMBER="${STOCKS_BUILD_NUMBER:-1}"
BUNDLE_ID="space.bwicarus.stocksnative"

case "$MODE" in simulator|archive) ;; *) echo "Expected simulator or archive" >&2; exit 2 ;; esac
[[ "$BUILD_NUMBER" =~ ^[0-9]+$ ]] || { echo "Build number must be numeric" >&2; exit 2; }
test "$(uname)" = Darwin || { echo "Xcode builds require macOS" >&2; exit 2; }
mkdir -p "$BUILD_ROOT/logs"
xcodegen generate --spec "$PROJECT_DIR/project.yml" --project "$PROJECT_DIR"

if [[ "$MODE" == simulator ]]; then
  swiftc -swift-version 5 -parse-as-library \
    "$PROJECT_DIR/Sources/StockWorkspaceLayout.swift" \
    "$PROJECT_DIR/Sources/WorkspaceGridEngine.swift" \
    "$PROJECT_DIR/../Tests/WorkspaceGridChecks.swift" \
    -o "$BUILD_ROOT/workspace-grid-checks"
  "$BUILD_ROOT/workspace-grid-checks"
  swiftc -swift-version 5 -parse-as-library \
    "$PROJECT_DIR/Sources/StockSelectionModels.swift" \
    "$PROJECT_DIR/../Tests/SelectionDragChecks.swift" \
    -o "$BUILD_ROOT/selection-drag-checks"
  "$BUILD_ROOT/selection-drag-checks"
  swiftc -swift-version 5 -parse-as-library \
    "$PROJECT_DIR/Sources/StockTimelineState.swift" \
    "$PROJECT_DIR/../Tests/StockTimelineChecks.swift" \
    -o "$BUILD_ROOT/stock-timeline-checks"
  "$BUILD_ROOT/stock-timeline-checks"
  xcodebuild build -project "$PROJECT_FILE" -scheme StocksNative \
    -configuration Debug -destination 'generic/platform=iOS Simulator' \
    -derivedDataPath "$BUILD_ROOT/DerivedData-simulator" \
    CURRENT_PROJECT_VERSION="$BUILD_NUMBER" \
    CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO \
    | tee "$BUILD_ROOT/logs/simulator.log"
  exit 0
fi

: "${TEAM_ID:?TEAM_ID required}"
: "${PROFILE_UUID:?PROFILE_UUID required}"
xcodebuild archive -project "$PROJECT_FILE" -scheme StocksNative \
  -configuration Release -destination 'generic/platform=iOS' \
  -archivePath "$BUILD_ROOT/StocksNative.xcarchive" \
  -derivedDataPath "$BUILD_ROOT/DerivedData-device" \
  DEVELOPMENT_TEAM="$TEAM_ID" CODE_SIGN_STYLE=Manual \
  CODE_SIGN_IDENTITY='Apple Distribution' \
  PROVISIONING_PROFILE_SPECIFIER="$PROFILE_UUID" \
  CURRENT_PROJECT_VERSION="$BUILD_NUMBER" \
  | tee "$BUILD_ROOT/logs/archive.log"

APP_PATH="$BUILD_ROOT/StocksNative.xcarchive/Products/Applications/StocksNative.app"
test -d "$APP_PATH"
APP_PATH="$APP_PATH" BUILD_ROOT="$BUILD_ROOT" BUNDLE_ID="$BUNDLE_ID" python3 <<'PY'
import os, plistlib
from pathlib import Path
app = Path(os.environ['APP_PATH'])
with (app / 'Info.plist').open('rb') as stream:
    info = plistlib.load(stream)
assert info['CFBundleIdentifier'] == os.environ['BUNDLE_ID'], 'Unexpected app bundle'
assert 2 in info['UIDeviceFamily'], 'iPad support missing'
assert info.get('NSMicrophoneUsageDescription'), 'Microphone purpose missing'
assert {'audio', 'voip', 'remote-notification'} <= set(info.get('UIBackgroundModes', [])), 'Call background modes missing'
assert not (app / 'PlugIns').exists(), 'MVP must not embed Reader extensions'
assert not (app / 'Watch').exists(), 'MVP must not embed Reader Watch app'
options = {
    'method': 'app-store-connect', 'destination': 'export',
    'teamID': os.environ['TEAM_ID'], 'signingStyle': 'manual',
    'signingCertificate': 'Apple Distribution', 'uploadSymbols': True,
    'provisioningProfiles': {os.environ['BUNDLE_ID']: os.environ['PROFILE_UUID']},
}
with (Path(os.environ['BUILD_ROOT']) / 'ExportOptions.plist').open('wb') as stream:
    plistlib.dump(options, stream)
print('Validated StocksNative archive identity, iPad support and microphone purpose')
PY
codesign --verify --deep --strict "$APP_PATH"
xcodebuild -exportArchive -archivePath "$BUILD_ROOT/StocksNative.xcarchive" \
  -exportOptionsPlist "$BUILD_ROOT/ExportOptions.plist" \
  -exportPath "$BUILD_ROOT/export" | tee "$BUILD_ROOT/logs/export.log"
test -f "$BUILD_ROOT/export/StocksNative.ipa"
shasum -a 256 "$BUILD_ROOT/export/StocksNative.ipa"
