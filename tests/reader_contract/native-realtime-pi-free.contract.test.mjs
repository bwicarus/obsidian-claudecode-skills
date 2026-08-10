import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");

const SERVER = read("_server_deploy/assistant.py");
const VOICE = read("_server_deploy/static/pdf/rc-voicecall.js");
const BRIDGE = read("ios/BWReader/App/ReaderNativeRealtimeBridge.swift");
const CORE = read("ios/BWReader/Shared/ReaderRealtimeCredentialCore.swift");
const CONTRACT = read("ios/BWReader/Shared/ReaderNativeBridgeContract.swift");
const HANDLER = read("ios/BWReader/Extension/SafariWebExtensionHandler.swift");
const APP_ENTITLEMENTS = read("ios/BWReader/App/BWReader.entitlements");
const EXT_ENTITLEMENTS = read(
  "ios/BWReader/Extension/BWReaderSafariExtension.entitlements",
);
const BACKGROUND = read("extensions/bw-reader-webext/background.js");
const FACADE = read("extensions/bw-reader-webext/src/facade.js");
const WEB_VIEW = read("ios/BWReader/App/ReaderWebView.swift");
const TOOLS = read("ios/BWReader/App/NativeReaderToolsView.swift");
const MANIFEST = read("ios/BWReader/native_reader_interface_manifest.json");

test("the App owns both the project key and the Realtime session without Pi", () => {
  assert.doesNotMatch(SERVER, /native-realtime-config/);
  assert.match(CORE, /kSecClassGenericPassword/);
  assert.match(CORE, /kSecAttrAccessibleWhenUnlockedThisDeviceOnly/);
  assert.match(CORE, /kSecAttrAccessGroup/);
  assert.match(CORE, /reader-native-realtime-keychain\/3/);
  assert.match(CORE, /reader-native-realtime-keychain\/2/);
  assert.doesNotMatch(CORE, /UserDefaults/);
  assert.doesNotMatch(CORE, /native-realtime-config|HTTPCookie|taile44d0c/);
  assert.match(CORE, /static let model = "gpt-realtime-2\.1-mini"/);
  assert.match(CORE, /private static func localSessionConfiguration\(\)/);
  assert.match(CORE, /v1\/realtime\/client_secrets/);
  assert.match(
    CORE,
    /"retention_ratio": NSDecimalNumber\([\s\S]*mantissa: 8,[\s\S]*exponent: -1/,
  );
  assert.match(CORE, /options: \[\.sortedKeys\]/);
  assert.match(CORE, /json\.contains\(#""retention_ratio":0\.8,"#/);
  assert.doesNotMatch(CORE, /"retention_ratio": 0\.8,/);
  assert.match(CORE, /static func openCall\(sdp: String\) async throws/);
  assert.match(CORE, /appendingPathComponent\("v1\/realtime\/calls"\)/);
  assert.match(CORE, /multipart\/form-data; boundary=/);
  assert.match(CORE, /Content-Disposition: form-data; name=\\"sdp\\"/);
  assert.match(CORE, /Content-Disposition: form-data; name=\\"session\\"/);
  assert.match(CORE, /value\(forHTTPHeaderField: "Location"\)/);
  assert.match(CORE, /"OpenAI-Safety-Identifier"/);
  assert.match(CORE, /"read_selection"/);
  assert.match(CORE, /"see_ink"/);
  assert.match(CORE, /"see_page"/);
  assert.match(CORE, /"make_note"/);
  assert.match(CORE, /"make_anki"/);
  assert.match(CORE, /"web_search"/);
  assert.match(CORE, /"deep_think"/);
  assert.match(CORE, /"do_task"/);
  assert.match(TOOLS, /SecureField\("输入现有 OpenAI Key/);
  assert.match(TOOLS, /保存 App Key/);
  assert.match(TOOLS, /App 保存、启动通话、注入选区与发送笔迹\/视口合成图都不连接 Pi/);
  assert.match(TOOLS, /已存入 Apple Keychain（尚未验证）/);
  assert.match(TOOLS, /Keychain 与 OpenAI 均已验证/);
  assert.match(TOOLS, /清除这台 iPad 中的 Key/);
  assert.match(BRIDGE, /func saveExistingKey\(_ apiKey: String\) async/);
  assert.match(BRIDGE, /try await ReaderRealtimeOpenAIClient\.mintClientSecret\(\)/);
  assert.match(BRIDGE, /OpenAI Realtime 联通验证/);

  // The credential export is intentionally unavailable to Reader JS and the
  // Safari extension's Pi manifest. Only the native settings client owns it.
  assert.doesNotMatch(MANIFEST, /native-realtime-config/);
});

test("the local Reader page receives only an opaque native Realtime capability", () => {
  assert.match(BRIDGE, /static let messageName = "bwNativeRealtime"/);
  assert.match(BRIDGE, /message\.frameInfo\.isMainFrame/);
  assert.match(BRIDGE, /message\.webView === webView/);
  assert.match(BRIDGE, /isTrustedLocalURL\(webView\.url\)/);
  assert.match(BRIDGE, /case "call"/);
  assert.match(BRIDGE, /ReaderRealtimeOpenAIClient\.openCall/);
  assert.match(BRIDGE, /"sdp": call\.answerSDP/);
  assert.match(BRIDGE, /"call_id": call\.callID/);
  assert.match(BRIDGE, /case "mint"/);
  assert.match(BRIDGE, /mintClientSecret\(\)/);
  assert.match(
    BRIDGE,
    /"client_secret": minted\.clientSecret/,
  );
  assert.doesNotMatch(
    BRIDGE.slice(BRIDGE.indexOf("case \"mint\"")),
    /"api_key"\s*:/,
  );

  assert.match(
    WEB_VIEW,
    /ReaderNativeRealtimeBridge\(\s*webView: webView,\s*trustedBaseURL: localRuntimeServer\.baseURL/s,
  );
  assert.match(
    WEB_VIEW,
    /addScriptMessageHandler\(\s*nativeRealtimeBridge,\s*contentWorld: \.page,\s*name: ReaderNativeRealtimeBridge\.messageName/s,
  );
  assert.match(WEB_VIEW, /location\.origin !== "http:\/\/127\.0\.0\.1:43129"/);
  assert.match(WEB_VIEW, /Object\.defineProperty\(window, "__bwNativeRealtime"/);
});

test("App-created calls keep one project credential domain for images and hangup", () => {
  const openCall = CORE.slice(
    CORE.indexOf("static func openCall"),
    CORE.indexOf("private static func localSessionConfiguration"),
  );
  assert.match(openCall, /ReaderRealtimeCredentialStore\.shared\.load\(\)/);
  assert.match(openCall, /"Bearer \\\(stored\.apiKey\)"/);
  assert.match(openCall, /callRequestBody\(/);
  assert.match(openCall, /ReaderRealtimeProjectCallRegistry\.shared/);
  assert.match(openCall, /callURL\?\.path == "\/v1\/realtime\/calls\/\\\(callID\)"/);
  assert.match(openCall, /clientSecret: capability/);
  assert.doesNotMatch(openCall, /clientSecret: minted\.clientSecret/);

  assert.match(CORE, /private actor ReaderRealtimeProjectCallRegistry/);
  assert.match(CORE, /private let maximumEntries = 8/);
  assert.match(CORE, /private let maximumAge: TimeInterval = 12 \* 60 \* 60/);
  assert.match(CORE, /authorizationKey\([\s\S]*entry\.capability == capability/);
  assert.match(CORE, /capability\.hasPrefix\("ek_bwreader_"\)[\s\S]*nil/);
  assert.match(
    CORE,
    /realtimeAuthorizationKey\([\s\S]*ephemeralFallback: clientSecret/,
  );
  assert.match(
    CORE,
    /injectImage\([\s\S]*let authorizationKey = try await realtimeAuthorizationKey/,
  );
  assert.match(
    CORE,
    /hangup\([\s\S]*let authorizationKey = try await realtimeAuthorizationKey/,
  );
  assert.match(CORE, /"Bearer \\\(authorizationKey\)"/);
  assert.doesNotMatch(BRIDGE, /"apiKey"\s*:/);
  assert.doesNotMatch(FACADE, /"apiKey"\s*:/);
});

test("App opens the call natively while Safari mints and hangs up through native code", () => {
  const direct = VOICE.slice(
    VOICE.indexOf("async function _openDirectRtcCall"),
    VOICE.indexOf("async function rtcStart"),
  );
  assert.match(direct, /action: 'call', sdp: sdp/);
  assert.match(direct, /action: 'mint'/);
  assert.match(direct, /native_direct: nativeDirect/);
  assert.match(direct, /if \(!nativeDirect\)[\s\S]*fetch\('\/api\/assistant\/rtc-bind'/);
  assert.match(direct, /var appRequiresNative = window\.__BW_NATIVE_LOCAL_READER__ === true/);
  assert.match(direct, /if \(appRequiresNative\)[\s\S]*App 原生 Realtime 桥尚未就绪/);
  assert.match(direct, /fetch\('\/api\/assistant\/rtc-client-secret'/);
  const nativeMintStart = direct.indexOf(
    "if (window.__BW_NATIVE_OPENAI_REALTIME__ === true)",
    direct.indexOf("action: 'call'"),
  );
  const nativeMintBranch = direct.slice(
    nativeMintStart,
    direct.indexOf("    } else {", nativeMintStart),
  );
  assert.doesNotMatch(nativeMintBranch, /rtc-client-secret|rtc-bind/);
  const appNativeBranch = direct.slice(
    direct.indexOf("if (appRequiresNative)"),
    direct.indexOf(
      "if (window.__BW_NATIVE_OPENAI_REALTIME__ === true)",
      direct.indexOf("action: 'call'"),
    ),
  );
  assert.match(appNativeBranch, /action: 'call'/);
  assert.doesNotMatch(appNativeBranch, /fetch\(|headers\.get\('Location'\)/);

  const connect = VOICE.slice(
    VOICE.indexOf("toggle._connect = function"),
    VOICE.indexOf("function toggle(opts)"),
  );
  assert.match(
    connect,
    /__BW_NATIVE_LOCAL_READER__[\s\S]*__BW_NATIVE_OPENAI_REALTIME__[\s\S]*rtcStart\(opts\);[\s\S]*return;/,
  );
  assert.ok(
    connect.indexOf("__BW_NATIVE_LOCAL_READER__") <
      connect.indexOf("fetch('/api/assistant/voice-config')"),
  );

  const control = VOICE.slice(
    VOICE.indexOf("function _ctlOpen"),
    VOICE.indexOf("function _rtcRequestHangup"),
  );
  assert.match(
    control,
    /if \(_rtc\.nativeDirect\)[\s\S]*_rtc\.ctl = false;[\s\S]*return;/,
  );
  assert.match(VOICE, /action: 'hangup', call_id: callId/);
  assert.match(VOICE, /_rtc\.nativeDirect = !!cres\.native_direct/);
  assert.match(VOICE, /语音启动失败（' \+ startupStage/);
  assert.match(VOICE, /RC\.toast\(startupMessage\)/);
  assert.match(VOICE, /startupStage = '申请麦克风权限'/);
  assert.match(VOICE, /startupStage = directRtc \? '连接 OpenAI Realtime'/);

  assert.match(CONTRACT, /"realtime\.mint"/);
  assert.match(CONTRACT, /"realtime\.image"/);
  assert.match(CONTRACT, /"realtime\.hangup"/);
  assert.match(APP_ENTITLEMENTS, /keychain-access-groups/);
  assert.match(EXT_ENTITLEMENTS, /keychain-access-groups/);
  assert.match(
    APP_ENTITLEMENTS,
    /\$\(AppIdentifierPrefix\)space\.bwicarus\.bwreader2\.realtime/,
  );
  assert.match(
    EXT_ENTITLEMENTS,
    /\$\(AppIdentifierPrefix\)space\.bwicarus\.bwreader2\.realtime/,
  );
  assert.match(HANDLER, /case "realtime\.mint"/);
  assert.match(HANDLER, /ReaderRealtimeOpenAIClient[\s\S]*mintClientSecret/);
  assert.match(HANDLER, /case "realtime\.image"/);
  assert.match(HANDLER, /case "realtime\.hangup"/);
  assert.doesNotMatch(HANDLER, /"apiKey"\s*:/);
  assert.match(BACKGROUND, /"realtime\.mint"/);
  assert.match(BACKGROUND, /NATIVE_APP_REALTIME_SECRET_RE/);
  assert.match(FACADE, /Object\.defineProperty\(window, '__bwNativeRealtime'/);
  assert.match(FACADE, /client_secret: data\.clientSecret/);
  assert.doesNotMatch(FACADE, /sk-[A-Za-z0-9]/);
});

test("selection is injected on the user's turn and visual tools send the real composite", () => {
  const flush = VOICE.slice(
    VOICE.indexOf("function _rtcFlushCtx"),
    VOICE.indexOf("async function _rtcDeep"),
  );
  assert.match(flush, /var sel = String\(_rtc\.sel \|\| ''\)\.trim\(\)/);
  assert.match(flush, /他当前明确选中了这段文字/);
  assert.match(flush, /sel\.length \+ ':' \+ sel\.slice\(0, 40\)/);

  assert.match(VOICE, /async function _nativeRealtimeVisual/);
  assert.match(VOICE, /RC\.captureInkRegion\(target\)/);
  assert.match(VOICE, /RC\.capturePageComposite/);
  assert.match(VOICE, /action: 'image', call_id: _rtc\.callId/);
  assert.match(
    VOICE,
    /_rtc\.nativeDirect && \/\^\(see_ink\|see_page\|see_figure\)\$\//,
  );
  assert.match(VOICE, /vision: vision \|\| undefined/);

  assert.match(BRIDGE, /case "image"/);
  assert.match(CORE, /wss:\/\/api\.openai\.com\/v1\/realtime/);
  assert.match(CORE, /"type": "input_image"/);
  assert.match(CORE, /"detail": "high"/);
  assert.match(CORE, /waitForImageConfirmation/);
  assert.match(CORE, /conversation\.item\.created/);
  assert.match(CORE, /private enum ReaderRealtimeVisualCache/);
  assert.match(CORE, /ReaderNativeBridgeContract\.appGroupIdentifier/);
  assert.match(
    CORE,
    /ReaderRealtimeVisualCache\.store\(decoded, mediaType: mediaType\)[\s\S]*wss:\/\/api\.openai\.com\/v1\/realtime/,
  );
  assert.match(CORE, /private static let maximumFiles = 12/);
});

test("native direct keeps local work in App and exposes only explicit Pi AI tools", () => {
  assert.match(VOICE, /function _rtcCreFetch\(\) \{\s*if \(_rtc\.nativeDirect\) return;/);
  assert.match(VOICE, /function _rtcFetchPageText\(pk\)[\s\S]{0,180}if \(_rtc\.nativeDirect\) return;/);
  assert.match(VOICE, /async function _rtcCompactNow\(urgent\) \{\s*if \(_rtc\.nativeDirect\) return;/);
  assert.match(VOICE, /async function _rtcInjectHistory\(\)[\s\S]{0,220}if \(_rtc\.nativeDirect\) return;/);
  assert.match(VOICE, /if \(!_rtc\.nativeDirect\) fetch\('\/api\/assistant\/rtc-usage'/);
  assert.match(VOICE, /if \(!wasNativeRealtime\)[\s\S]*fetch\('\/api\/assistant\/compact-history'/);
  assert.match(VOICE, /_rtc\.nativeDirect && name === 'read_page'/);
  assert.match(VOICE, /_rtc\.nativeDirect && name === 'make_note'/);
  assert.match(VOICE, /fetch\('\/pdf\/api\/to-note'/);
  assert.match(VOICE, /owner: 'native-app'/);
  assert.match(VOICE, /_rtc\.nativeDirect && !_nativeRealtimePiAITool\(name\)/);
  assert.match(VOICE, /App 本机直连模式未开放该工具/);
  assert.match(VOICE, /!_rtc\.nativeDirect && !_rtc\.turnTool/);

  const allowlistBlock = VOICE.slice(
    VOICE.indexOf("var NATIVE_REALTIME_PI_AI_TOOLS"),
    VOICE.indexOf("function _nativeRealtimePiAITool"),
  );
  assert.deepEqual(
    [...allowlistBlock.matchAll(/'([a-z_]+)'/g)].map((match) => match[1]),
    [
      "make_anki", "web_search", "search_image", "search_video",
      "deep_think", "do_task", "make_paper", "route_to_text",
    ],
  );

  const routeToText = VOICE.slice(
    VOICE.indexOf("if (name === 'route_to_text')"),
    VOICE.indexOf("_rtc.turnTool = true", VOICE.indexOf("if (name === 'route_to_text')")),
  );
  assert.doesNotMatch(routeToText, /本机直连模式不提供服务器文字路由/);
});
