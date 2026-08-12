import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

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
const SETTINGS = read("_server_deploy/static/pdf/rc-settings.js");
const LOCAL_SERVER = read("ios/BWReader/App/ReaderLocalRuntimeServer.swift");
const PDF_READER = read("_server_deploy/static/pdf/pdf-tail.js");
const EPUB_READER = read("_server_deploy/static/pdf/epub-html.js");
const PENCIL_OVERLAY = read("ios/BWReader/App/NativePencilLiveOverlay.swift");

function nativeRealtimeToolHarness(options = {}) {
  const routing = VOICE.slice(
    VOICE.indexOf("function _rtcInkFingerprint"),
    VOICE.indexOf("var NATIVE_REALTIME_PI_AI_TOOLS"),
  );
  const responseCreate = VOICE.slice(
    VOICE.indexOf("function _rtcRespCreate"),
    VOICE.indexOf("// 创造物库清单"),
  );
  const pageTextHelpers = VOICE.slice(
    VOICE.indexOf("function _nativeRealtimeDOMPageText"),
    VOICE.indexOf("async function _rtcTool"),
  );
  const toolRun = VOICE.slice(
    VOICE.indexOf("async function _rtcTool"),
    VOICE.indexOf("// rtc 字幕队列", VOICE.indexOf("async function _rtcTool")),
  );
  const initial = {
    page: Number(options.page) || 7,
    total: Number(options.total) || 53,
    title: String(options.title || "料理师 part1"),
    visibleText: String(options.visibleText || ""),
    selection: String(options.selection || ""),
    question: String(options.question || ""),
    providerPages: options.providerPages || {},
  };
  const sandbox = {};
  vm.runInNewContext(`
    var initial = ${JSON.stringify(initial)};
    var snapshot = {
      page: initial.page,
      total: initial.total,
      title: initial.title,
      visible_text: initial.visibleText,
      selection: initial.selection
    };
    var providerCalls = [];
    var providerPages = initial.providerPages;
    var provider = {
      contract: 'reader-page-text-provider/1',
      pageChars: function (page) {
        page = Number(page) || 0;
        providerCalls.push(page);
        var text = String(providerPages[String(page)] || '');
        return Promise.resolve({
          state: text ? 'ready' : 'readyEmpty',
          source: 'pc-preprocess',
          revision: 'provider-rev-' + page,
          page: page,
          chars: Array.from(text).map(function (c) { return { c: c }; })
        });
      }
    };
    var BWReaderRuntime = { pageTextProvider: provider };
    var _rtc = {
      hasInk: false, inkVer: 0, inkSeenVer: 0, inkDirty: false,
      ctxFile: 'localbook:context-contract', ctxPage: initial.page,
      ctxTotal: initial.total, pendText: initial.visibleText,
      inkPages: Object.create(null), turnText: false, nativeDirect: true,
      activeInkPage: null, inkResponseAcks: Object.create(null), inkAckSeq: 0,
      turnEpoch: 0, visualTurnEpoch: null,
      callId: 'call', sidebandKey: 'sideband', recentTools: [],
      sel: initial.selection
    };
    var sent = [];
    var visualCalls = [];
    var visualResolvers = [];
    var holdVisual = false;
    var statuses = [];
    var _lastU = initial.question;
    function _voiceMode() { return 'sts'; }
    function _dcSend(value) { sent.push(value); return true; }
    function _rtcFetchPageText() {}
    function _rtcInterrupt() {}
    function _rtcFlushCtx() {}
    function capUser() {}
    function onToolStatus(value) { statuses.push(value); }
    function dispatch() {}
    function _nativeRealtimePiAITool() { return false; }
    function _visualStageError(stage, message) {
      var error = new Error(message); error.bwVisualStage = stage; return error;
    }
    function _captureView() { return Promise.resolve(null); }
    function _nativeRealtimeVisual(name, args) {
      visualCalls.push({ name: name, args: args });
      if (!holdVisual) return Promise.resolve({ native_delivered: true });
      return new Promise(function (resolve) { visualResolvers.push(resolve); });
    }
    var adapter = { getContext: function () { return Object.assign({}, snapshot); } };
    var RC = { adapter: function () { return adapter; } };
    var document = {
      title: initial.title,
      querySelector: function () { return null; },
      querySelectorAll: function () { return []; }
    };
    var window = {
      RC: RC,
      BWReaderRuntime: BWReaderRuntime,
      __BW_NATIVE_LOCAL_READER__: true,
      __BW_NATIVE_OPENAI_REALTIME__: true,
      __bwNativeRealtime: { request: function () {} }
    };
    var localStorage = { getItem: function () { return null; } };
    function setTimeout() { return 0; }
    function clearTimeout() {}
    ${routing}
    ${responseCreate}
    ${pageTextHelpers}
    ${toolRun}
    this.api = {
      tool: _rtcTool,
      beginTurn: _rtcBeginUserTurn,
      state: _rtc,
      sent: sent,
      statuses: statuses,
      visualCalls: visualCalls,
      providerCalls: providerCalls,
      holdVisual: function () { holdVisual = true; },
      releaseVisuals: function (value) {
        holdVisual = false;
        var pending = visualResolvers.splice(0);
        pending.forEach(function (resolve) {
          resolve(value || { native_delivered: true });
        });
      },
      visualWaiting: function () { return visualResolvers.length; },
      mutate: function (value) {
        value = value || {};
        if (Object.prototype.hasOwnProperty.call(value, 'page')) {
          snapshot.page = value.page; _rtc.ctxPage = value.page;
        }
        if (Object.prototype.hasOwnProperty.call(value, 'total')) {
          snapshot.total = value.total; _rtc.ctxTotal = value.total;
        }
        if (Object.prototype.hasOwnProperty.call(value, 'title')) {
          snapshot.title = value.title; document.title = value.title;
        }
        if (Object.prototype.hasOwnProperty.call(value, 'visibleText')) {
          snapshot.visible_text = value.visibleText; _rtc.pendText = value.visibleText;
        }
        if (Object.prototype.hasOwnProperty.call(value, 'selection')) {
          snapshot.selection = value.selection; _rtc.sel = value.selection;
        }
        if (Object.prototype.hasOwnProperty.call(value, 'question')) _lastU = value.question;
      },
      makeFresh: function (page) {
        var state = _rtcInkPageState(page, true);
        state.initialized = true;
        state.fp = 'fresh-' + page;
        state.strokes = [{ t: 'pen', p: [[0.1, 0.1], [0.2, 0.2]] }];
        state.hasInk = true;
        state.ver = 1;
        state.seenVer = 0;
        state.pending = false;
        state.pendingCount = 0;
        _rtc.activeInkPage = Number(page);
        _rtcUseInkPage(page);
      },
      clear: function () { sent.length = 0; visualCalls.length = 0; statuses.length = 0; }
    };
  `, sandbox);
  return sandbox.api;
}

function realtimeToolOutputs(api) {
  return api.sent
    .filter((message) => message.type === "conversation.item.create" &&
      message.item && message.item.type === "function_call_output")
    .map((message) => JSON.parse(message.item.output));
}

async function waitForVisual(api) {
  for (let i = 0; i < 8 && !api.visualWaiting(); i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

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
  assert.match(CORE, /"event_id": eventID/);
  assert.match(CORE, /"id": itemID/);
  assert.match(CORE, /let eventID = "bwe_" \+ String\(eventNonce\.prefix\(28\)\)/);
  assert.match(CORE, /let itemID = "bwi_" \+ String\(itemNonce\.prefix\(28\)\)/);
  assert.equal("bwe_".length + 28, 32);
  assert.equal("bwi_".length + 28, 32);
  assert.match(CORE, /conversation\.item\.added/);
  assert.match(CORE, /conversation\.item\.done/);
  assert.match(CORE, /conversation\.item\.created/);
  assert.match(CORE, /item\?\["id"\] as\? String == itemID/);
  assert.match(CORE, /causingEventID != eventID/);
  assert.match(CORE, /private enum ReaderRealtimeVisualCache/);
  assert.match(CORE, /ReaderNativeBridgeContract\.appGroupIdentifier/);
  assert.match(
    CORE,
    /ReaderRealtimeVisualCache\.store\(imageData, mediaType: mediaType\)[\s\S]*wss:\/\/api\.openai\.com\/v1\/realtime/,
  );
  assert.match(CORE, /private static let maximumFiles = 12/);
});

test("fresh ink is page-versioned, immediate, and consumed only after one delivered visual answer", async () => {
  const routing = VOICE.slice(
    VOICE.indexOf("function _rtcInkFingerprint"),
    VOICE.indexOf("var NATIVE_REALTIME_PI_AI_TOOLS"),
  );
  const handleUp = VOICE.slice(
    VOICE.indexOf("function _rtcHandleUp"),
    VOICE.indexOf("// ── 65", VOICE.indexOf("function _rtcHandleUp")),
  );
  const responseCreate = VOICE.slice(
    VOICE.indexOf("function _rtcRespCreate"),
    VOICE.indexOf("// 创造物库清单"),
  );
  const toolRun = VOICE.slice(
    VOICE.indexOf("async function _rtcTool"),
    VOICE.indexOf("// rtc 字幕队列", VOICE.indexOf("async function _rtcTool")),
  );
  const sandbox = {};
  vm.runInNewContext(`
    var _rtc = {
      hasInk: false, inkVer: 0, inkSeenVer: 0, inkDirty: false, ctxPage: 1,
      inkPages: Object.create(null), turnText: false, nativeDirect: true,
      activeInkPage: null, inkResponseAcks: Object.create(null), inkAckSeq: 0,
      turnEpoch: 0, visualTurnEpoch: -1,
      callId: 'call', sidebandKey: 'sideband', recentTools: [],
      ctxTotal: 53, pendText: 'existing sandbox page text', sel: ''
    };
    var sent = [];
    var sendResults = [];
    var visualCalls = [];
    var visualResolver = null;
    var holdVisual = false;
    var statuses = [];
    var _lastU = '';
    function _voiceMode() { return 'sts'; }
    function _dcSend(value) {
      sent.push(value);
      return sendResults.length ? sendResults.shift() : true;
    }
    function _rtcFetchPageText() {}
    function _rtcInterrupt() {}
    function _rtcFlushCtx() {}
    function capUser() {}
    function onToolStatus(value) { statuses.push(value); }
    function dispatch() {}
    function _nativeRealtimePiAITool() { return false; }
    function _nativeRealtimePageText() { return String(_rtc.pendText || ''); }
    function _nativeRealtimeContextSnapshot(page) {
      return {
        title: 'sandbox book', file: 'sandbox.pdf',
        page: Number(page) || _rtc.ctxPage, total: _rtc.ctxTotal,
        visible_text: String(_rtc.pendText || ''), selection: String(_rtc.sel || ''),
        selection_context: '', user_question: String(_lastU || '')
      };
    }
    function _nativeRealtimePageContext(page, snapshot) {
      snapshot = snapshot || _nativeRealtimeContextSnapshot(page);
      return Promise.resolve({
        contract: 'reader-realtime-page-context/1',
        title: snapshot.title, page: snapshot.page, total: snapshot.total,
        before: '', visible_text: snapshot.visible_text,
        current_page_text: snapshot.visible_text, after: '',
        selection: snapshot.selection, selection_context: snapshot.selection_context,
        source: 'sandbox'
      });
    }
    function _visualStageError(stage, message) {
      var error = new Error(message); error.bwVisualStage = stage; return error;
    }
    function _captureView() { return Promise.resolve(null); }
    function _nativeRealtimeVisual(name, args) {
      visualCalls.push({ name: name, args: args });
      if (!holdVisual) return Promise.resolve({ native_delivered: true });
      return new Promise(function (resolve) { visualResolver = resolve; });
    }
    var window = {
      __BW_NATIVE_LOCAL_READER__: true,
      __BW_NATIVE_OPENAI_REALTIME__: true,
      __bwNativeRealtime: { request: function () {} }
    };
    var localStorage = { getItem: function () { return null; } };
    function setTimeout() { return 0; }
    function clearTimeout() {}
    ${routing}
    ${responseCreate}
    ${handleUp}
    ${toolRun}
    this.api = {
      fp: _rtcInkFingerprint,
      effective: _rtcEffectiveTool,
      fresh: _rtcHasFreshInk,
      freshPage: _rtcFreshInkPage,
      mark: _rtcMarkInkSeen,
      pending: _rtcSetInkPending,
      handle: _rtcHandleUp,
      pageState: _rtcInkPageState,
      response: _rtcRespCreate,
      complete: _rtcCompleteToolTurn,
      finishAck: _rtcFinishInkAck,
      beginTurn: _rtcBeginUserTurn,
      tool: _rtcTool,
      state: _rtc,
      sent: sent,
      statuses: statuses,
      visualCalls: visualCalls,
      clearSent: function () { sent.length = 0; sendResults.length = 0; },
      clearVisualCalls: function () { visualCalls.length = 0; },
      failSends: function (values) { sendResults = values.slice(); },
      holdVisual: function () { holdVisual = true; visualResolver = null; },
      visualWaiting: function () { return !!visualResolver; },
      resolveVisual: function (value) {
        holdVisual = false;
        var resolve = visualResolver; visualResolver = null; resolve(value);
      }
    };
  `, sandbox);

  const first = [{ t: "pen", p: [[0.1, 0.2], [0.3, 0.4]] }];
  const second = [{ t: "pen", p: [[0.4, 0.3], [0.2, 0.1]] }];
  const third = [{ t: "pen", p: [[0.2, 0.3], [0.4, 0.1]] }];
  assert.equal(JSON.stringify(first).length, JSON.stringify(second).length);
  assert.notEqual(sandbox.api.fp(1, first), sandbox.api.fp(1, second));

  sandbox.api.handle({
    type: "ink", page: 1, strokes: first,
    revision: sandbox.api.fp(1, first),
  });
  assert.equal(sandbox.api.pageState(1).initialized, true);
  assert.equal(sandbox.api.pageState(1).ver, 0);
  assert.equal(sandbox.api.fresh(1), false, "first non-empty frame is old-ink baseline");

  sandbox.api.handle({
    type: "ink", page: 1, strokes: second,
    revision: sandbox.api.fp(1, second),
    changed: true,
  });
  assert.equal(sandbox.api.pageState(1).ver, 1);
  assert.equal(sandbox.api.fresh(1), true);

  sandbox.api.handle({
    type: "ink", page: 2, strokes: second,
    revision: sandbox.api.fp(2, second),
  });
  assert.equal(sandbox.api.pageState(2).ver, 0);
  assert.equal(sandbox.api.fresh(2), false, "page 2 establishes its own baseline");
  sandbox.api.handle({
    type: "ink", page: 2, strokes: first,
    revision: sandbox.api.fp(2, first),
    changed: true,
  });
  assert.equal(sandbox.api.fresh(2), true);
  sandbox.api.handle({ type: "page", page: 1 });
  sandbox.api.handle({ type: "page", page: 2 });
  sandbox.api.handle({ type: "page", page: 1 });
  assert.equal(sandbox.api.fresh(1), true, "page switching must not consume page 1");
  assert.equal(sandbox.api.fresh(2), true, "page switching must not consume page 2");

  for (const name of ["read_selection", "read_page", "see_page", "see_figure"]) {
    assert.equal(sandbox.api.effective(name, true), "see_ink");
    assert.equal(sandbox.api.effective(name, false), name);
  }
  assert.equal(sandbox.api.effective("web_search", true), "web_search");

  sandbox.api.response("user");
  assert.equal(sandbox.api.sent.length, 1);
  assert.equal(sandbox.api.sent[0].response.tool_choice.type, "function");
  assert.equal(sandbox.api.sent[0].response.tool_choice.name, "see_ink");

  sandbox.api.clearSent();
  await sandbox.api.tool(
    "see_ink",
    { page: 2, selectionId: "selection-on-page-2", scope: "selection" },
    "explicit-ink-target",
  );
  assert.equal(sandbox.api.visualCalls.length, 1);
  assert.equal(sandbox.api.visualCalls[0].name, "see_ink");
  assert.equal(
    sandbox.api.visualCalls[0].args.page,
    2,
    "an explicit see_ink page must not be replaced by the current fresh page",
  );
  assert.equal(sandbox.api.visualCalls[0].args.selectionId, "selection-on-page-2");
  assert.equal(sandbox.api.visualCalls[0].args.scope, "selection");
  sandbox.api.clearSent();
  sandbox.api.clearVisualCalls();

  sandbox.api.complete("see_page", true, 1, 1, "see-page", "{}", false, false);
  assert.equal(sandbox.api.pageState(1).seenVer, 0, "see_page must not consume ink");
  sandbox.api.clearSent();
  sandbox.api.complete("see_ink", false, 1, 1, "failed-ink", "{}", false, false);
  assert.equal(sandbox.api.pageState(1).seenVer, 0, "failed see_ink must not consume ink");

  sandbox.api.beginTurn();
  sandbox.api.clearSent();
  sandbox.api.holdVisual();
  const toolPromise = sandbox.api.tool(
    "see_page",
    { page: 99, selectionId: "must-not-survive" },
    "visual-call",
  );
  for (let i = 0; i < 5 && !sandbox.api.visualWaiting(); i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.equal(
    sandbox.api.visualWaiting(),
    true,
    JSON.stringify({
      sent: sandbox.api.sent,
      statuses: sandbox.api.statuses,
      visualCalls: sandbox.api.visualCalls,
      currentPage: sandbox.api.state.ctxPage,
      page: sandbox.api.pageState(1),
    }),
  );
  assert.equal(sandbox.api.visualCalls.length, 1);
  assert.equal(sandbox.api.visualCalls[0].name, "see_ink");
  assert.equal(sandbox.api.visualCalls[0].args.page, 1);
  assert.equal(sandbox.api.visualCalls[0].args.scope, "drawing-nearby");
  assert.equal("selectionId" in sandbox.api.visualCalls[0].args, false);

  sandbox.api.handle({
    type: "ink", page: 1, strokes: third,
    revision: sandbox.api.fp(1, third),
    changed: true,
  });
  assert.equal(sandbox.api.pageState(1).ver, 2);
  sandbox.api.resolveVisual({ native_delivered: true });
  await toolPromise;

  const outputs = sandbox.api.sent.filter(
    (message) => message.type === "conversation.item.create" &&
      message.item && message.item.type === "function_call_output",
  );
  const responses = sandbox.api.sent.filter((message) => message.type === "response.create");
  assert.equal(outputs.length, 1, "one visual call has one tool output");
  assert.equal(responses.length, 1, "one visual call has one final answer request");
  assert.equal(responses[0].response.tool_choice, "none");
  const completedAck = responses[0].response.metadata.bw_ink_ack;
  assert.match(completedAck, /^ink_/);
  const output = JSON.parse(outputs[0].item.output);
  assert.equal(output.requested_tool, "see_page");
  assert.equal(output.resolved_as, "see_ink");
  assert.equal(
    sandbox.api.pageState(1).seenVer,
    0,
    "sending response.create is not proof that the final answer completed",
  );
  sandbox.api.finishAck({
    type: "response.done",
    response: {
      status: "completed",
      metadata: { bw_ink_ack: completedAck },
    },
  });
  assert.equal(sandbox.api.pageState(1).seenVer, 1);
  assert.equal(
    sandbox.api.fresh(1),
    true,
    "ink committed while the screenshot was pending must remain fresh",
  );
  assert.equal(sandbox.api.fresh(2), true, "page 2 remains independently fresh");

  sandbox.api.clearSent();
  sandbox.api.failSends([false]);
  sandbox.api.complete("see_ink", true, 2, 1, "output-fails", "{}", false, false);
  assert.equal(sandbox.api.pageState(1).seenVer, 1, "failed tool-output send must not consume");
  sandbox.api.clearSent();
  sandbox.api.failSends([true, false]);
  sandbox.api.complete("see_ink", true, 2, 1, "response-fails", "{}", false, false);
  assert.equal(sandbox.api.pageState(1).seenVer, 1, "failed final-response send must not consume");

  sandbox.api.clearSent();
  sandbox.api.complete(
    "see_ink", true, 2, 1, "answer-fails", "{}", false, false,
    sandbox.api.state.turnEpoch,
  );
  const failedAck = sandbox.api.sent.find(
    (message) => message.type === "response.create",
  ).response.metadata.bw_ink_ack;
  sandbox.api.finishAck({
    type: "response.done",
    response: { status: "failed", metadata: { bw_ink_ack: failedAck } },
  });
  assert.equal(sandbox.api.pageState(1).seenVer, 1, "failed response.done must not consume");

  sandbox.api.clearSent();
  const oldEpoch = sandbox.api.state.turnEpoch;
  sandbox.api.complete(
    "see_ink", true, 2, 1, "old-epoch-answer", "{}", false, false, oldEpoch,
  );
  const oldEpochAck = sandbox.api.sent.find(
    (message) => message.type === "response.create",
  ).response.metadata.bw_ink_ack;
  sandbox.api.beginTurn();
  sandbox.api.finishAck({
    type: "response.done",
    response: { status: "completed", metadata: { bw_ink_ack: oldEpochAck } },
  });
  assert.equal(sandbox.api.pageState(1).seenVer, 1, "an old turn's late ACK must not consume");

  sandbox.api.clearSent();
  sandbox.api.holdVisual();
  const staleTool = sandbox.api.tool(
    "see_page",
    { page: 77, selectionId: "stale" },
    "stale-visual-call",
  );
  for (let i = 0; i < 5 && !sandbox.api.visualWaiting(); i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.equal(sandbox.api.visualWaiting(), true);
  sandbox.api.beginTurn();
  const staleVisualItemID = `bwi_${"a".repeat(28)}`;
  sandbox.api.resolveVisual({ native_delivered: true, item_id: staleVisualItemID });
  await staleTool;
  const staleDeletes = sandbox.api.sent.filter(
    (message) => message.type === "conversation.item.delete",
  );
  assert.equal(staleDeletes.length, 1, "a directly delivered stale image is deleted exactly once");
  assert.equal(staleDeletes[0].item_id, staleVisualItemID);
  assert.equal(
    sandbox.api.sent.filter((message) =>
      message.type === "conversation.item.create" &&
      message.item && message.item.type === "function_call_output"
    ).length,
    1,
    "a stale tool call is still closed exactly once",
  );
  assert.equal(
    sandbox.api.sent.filter((message) => message.type === "response.create").length,
    0,
    "a stale tool must not create an answer for the replacement user turn",
  );
  assert.equal(sandbox.api.pageState(1).seenVer, 1, "stale tool completion must not consume");

  const rtcEvents = VOICE.slice(
    VOICE.indexOf("function _rtcOnEvent"),
    VOICE.indexOf("async function _openDirectRtcCall", VOICE.indexOf("function _rtcOnEvent")),
  );
  assert.match(rtcEvents, /t === 'response\.done'[\s\S]{0,100}_rtcFinishInkAck\(e\)/);
  const speech = { actions: [] };
  vm.runInNewContext(`
    var _rtc = { ctxFile: '', ctxPage: 1, aEnd: 1, aStart: 1 };
    var curAText = '', curAEl = null, _lastU = '';
    var window = { __asstVoiceMsg: function () {} };
    function _rtcBeginUserTurn() { actions.push('begin'); }
    function _rtcInterrupt() { actions.push('interrupt'); }
    function _requestSyncNow() { actions.push('sync'); }
    function _rtcFlushCtx() { actions.push('flush'); }
    function _recFinish() { return ''; }
    function _rtcCapReset() {}
    function capClear() {}
    function _ttsOn() { return false; }
    function bargeIn() {}
    ${rtcEvents}
    this.api = { event: _rtcOnEvent };
  `, speech);
  speech.api.event({ type: "input_audio_buffer.speech_started" });
  assert.deepEqual(
    speech.actions,
    ["begin", "interrupt", "sync", "flush"],
    "speech_started interrupts the old answer before syncing and injecting the new turn",
  );

  const syncInkSource = VOICE.slice(
    VOICE.indexOf("function syncInk"),
    VOICE.indexOf("// ── 入口按钮", VOICE.indexOf("function syncInk")),
  );
  const adapterSyncSource = VOICE.slice(
    VOICE.indexOf("function _syncAdapterNow"),
    VOICE.indexOf("setInterval(function ()", VOICE.indexOf("function _syncAdapterNow")),
  );
  const transport = {};
  vm.runInNewContext(`
    var events = Object.create(null);
    var sent = [];
    var captureCalls = 0;
    var voiceInkCalls = 0;
    var strokes = ${JSON.stringify(first)};
    var mode = 's2s';
    var _connecting = false;
    var _inkFp = '';
    var _rtc = {
      on: true, nativeDirect: true, ctxPage: 7, inkPages: Object.create(null),
      activeInkPage: null, ink: null, hasInk: false,
      inkVer: 0, inkSeenVer: 0, inkDirty: false
    };
    function setTimeout() { return 0; }
    function clearTimeout() {}
    function _rtcFetchPageText() {}
    function _rtcInterrupt() {}
    function _rtcFlushCtx() {}
    function capUser() {}
    function _rtcRespCreate() { return true; }
    function setSt() {}
    function setPage() {}
    function syncState() {}
    var adapter = {
      getContext: function () { return { page: 7, selection: '' }; },
      getVoiceInk: function () {
        voiceInkCalls += 1;
        return { page: 7, strokes: strokes };
      }
    };
    var RC = {
      adapter: function () { return adapter; },
      captureInkRegion: function () { captureCalls += 1; return new Promise(function () {}); },
      captureView: function () { captureCalls += 1; return new Promise(function () {}); }
    };
    var window = {
      RC: RC,
      __vcSyncNow: function () {},
      addEventListener: function (name, handler) { events[name] = handler; }
    };
    var ws = {
      readyState: 1,
      send: function (raw) {
        var value = JSON.parse(raw);
        sent.push(value);
        _rtcHandleUp(value);
      }
    };
    ${routing}
    ${handleUp}
    ${syncInkSource}
    ${adapterSyncSource}
    this.api = {
      events: events,
      sent: sent,
      syncNow: _syncAdapterNow,
      fresh: _rtcHasFreshInk,
      freshPage: _rtcFreshInkPage,
      pageState: _rtcInkPageState,
      setStrokes: function (value) { strokes = value; },
      captureCalls: function () { return captureCalls; },
      voiceInkCalls: function () { return voiceInkCalls; }
    };
  `, transport);

  transport.api.syncNow();
  assert.equal(transport.api.voiceInkCalls(), 1, "EPUB adapter ink reaches syncInk");
  assert.equal(transport.api.sent.length, 1);
  assert.equal(transport.api.fresh(7), false, "initial adapter frame is only baseline");
  assert.equal(transport.api.captureCalls(), 0, "nativeDirect sends state without awaiting capture");

  transport.api.events["rc:inkpending"]({
    detail: {
      source: "native-pencil",
      opId: "op-a",
      surfaceIds: ["section:0", "section:1"],
    },
  });
  transport.api.events["rc:inkpending"]({
    detail: { source: "native-pencil", opId: "op-b", surfaceIds: ["section:1"] },
  });
  transport.api.events["rc:inkpending"]({
    detail: { source: "native-pencil", opId: "op-b", surfaceIds: ["section:1"] },
  });
  assert.equal(transport.api.pageState(1).pendingCount, 1);
  assert.equal(transport.api.pageState(2).pendingCount, 2);

  transport.api.events["rc:inkchange"]({
    detail: {
      source: "native-pencil",
      opId: "op-a",
      changes: [
        { page: 1, strokes: first },
        { page: 2, strokes: second },
      ],
    },
  });
  assert.equal(transport.api.voiceInkCalls(), 1, "event payload avoids a stale adapter reread");
  assert.equal(transport.api.sent.length, 3, "two changed pages sync immediately after baseline");
  assert.equal(transport.api.sent[1].page, 1);
  assert.equal(transport.api.sent[2].page, 2);
  assert.equal(transport.api.sent[1].changed, true);
  assert.equal(transport.api.sent[2].changed, true);
  assert.equal(transport.api.pageState(1).pending, false);
  assert.equal(transport.api.pageState(2).pending, true, "second page still has op-b in flight");
  assert.equal(transport.api.pageState(2).pendingCount, 1);
  assert.equal(transport.api.fresh(1), true);
  assert.equal(transport.api.fresh(2), true);

  transport.api.events["rc:inkcancel"]({
    detail: { source: "native-pencil", opId: "not-op-b", surfaceIds: ["section:1"] },
  });
  assert.equal(transport.api.pageState(2).pendingCount, 1, "unknown cancel cannot consume another op");
  transport.api.events["rc:inkcancel"]({
    detail: { source: "native-pencil", opId: "op-b", surfaceIds: ["section:1"] },
  });
  assert.equal(transport.api.pageState(2).pendingCount, 0);
  assert.equal(transport.api.pageState(2).pending, false);
  assert.equal(transport.api.freshPage(), 2, "the most recently changed page remains the visual target");
  assert.equal(transport.api.captureCalls(), 0, "nativeDirect never starts the EPUB screenshot promise");

  for (const source of [PDF_READER, EPUB_READER]) {
    assert.match(
      source,
      /_inkScheduleSave\([^;]+;[\s\S]{0,240}rc:inkchange[\s\S]{0,120}source: 'web-ink'/,
    );
  }
  assert.match(
    PENCIL_OVERLAY,
    /private func enqueue\(_ operation: NativeInkOperation\) \{[\s\S]{0,240}reader\.signalNativePencilOperationPending\(operation\)/,
  );
  assert.match(
    PENCIL_OVERLAY,
    /signalNativePencilOperationPending\(_ operation: NativeInkOperation\)[\s\S]{0,220}event: "rc:inkpending"/,
  );
  assert.match(
    PENCIL_OVERLAY,
    /signalNativePencilOperationCancelled\(_ operation: NativeInkOperation\)[\s\S]{0,220}event: "rc:inkcancel"/,
  );
  assert.match(
    PENCIL_OVERLAY,
    /window\.dispatchEvent\(new CustomEvent[\s\S]{0,180}source: 'native-pencil'[\s\S]{0,120}opId:/,
  );
  const epubVoiceInkStart = EPUB_READER.indexOf("getVoiceInk: function");
  const epubVoiceInk = EPUB_READER.slice(
    epubVoiceInkStart,
    EPUB_READER.indexOf("// 图 +", epubVoiceInkStart),
  );
  assert.ok(epubVoiceInkStart >= 0);
  assert.match(epubVoiceInk, /return \{ page: [^,]+, strokes: strokes \|\| \[\] \}/);
  const epubVoicePage = EPUB_READER.slice(
    EPUB_READER.indexOf("function _voicePageOfInkEl"),
    EPUB_READER.indexOf("// 插入页", EPUB_READER.indexOf("function _voicePageOfInkEl")),
  );
  const epubExact = {};
  vm.runInNewContext(`
    var _curTopIdx = 0;
    function makeSection(idx, strokes, top) {
      return {
        dataset: { idx: String(idx) },
        __inkStrokes: strokes,
        matches: function () { return true; },
        closest: function () { return this; },
        getBoundingClientRect: function () { return { top: top, bottom: top + 100 }; }
      };
    }
    var secEls = [
      makeSection(0, ${JSON.stringify(first)}, 0),
      makeSection(1, ${JSON.stringify(second)}, 2000),
      makeSection(2, ${JSON.stringify(third)}, 3000)
    ];
    var _epInk = { lastEl: secEls[0], data: Object.create(null) };
    function _favUpElIn() { return null; }
    function _inkIdxOf(el) { return parseInt(el.dataset.idx, 10); }
    var document = { body: { contains: function () { return true; } } };
    var window = { innerHeight: 900 };
    ${epubVoicePage}
    var adapter = { ${epubVoiceInk} };
    this.api = { adapter: adapter };
  `, epubExact);
  const exactPage2 = epubExact.api.adapter.getVoiceInk(2);
  assert.equal(exactPage2.page, 2, "EPUB page hints are 1-based");
  assert.equal(
    JSON.stringify(exactPage2.strokes),
    JSON.stringify(second),
    "an exact page hint reads that page even when it is offscreen",
  );
  assert.equal(epubExact.api.adapter.getVoiceInk(3).page, 3);

  assert.match(CORE, /指代优先级固定为：当前显示页在本次通话中尚未看过的新笔迹变化 > 当前明确选区 > 当前可见文字 > 整页图像/);
  assert.match(CORE, /同一用户轮只选择一个视觉目标/);
});

test("native read_page awaits the App page-text provider and returns real nearby text", async () => {
  const before = "CTX_BEFORE_食物禁忌的来历";
  const current = "CTX_CURRENT_和尚正在吃饭并说明肉食禁忌";
  const after = "CTX_AFTER_下一格继续解释规则";
  const api = nativeRealtimeToolHarness({
    page: 7,
    total: 53,
    title: "料理师 part1",
    providerPages: { 6: before, 7: current, 8: after },
  });

  await api.tool("read_page", {}, "read-native-page");

  assert.deepEqual(
    [...new Set(Array.from(api.providerCalls))].sort((a, b) => a - b),
    [6, 7, 8],
    "read_page must ask the native provider for the current page and its neighbors",
  );
  const outputs = realtimeToolOutputs(api);
  assert.equal(outputs.length, 1);
  assert.equal(outputs[0].text, current);
  assert.equal(outputs[0].page_context.contract, "reader-realtime-page-context/1");
  assert.equal(outputs[0].page_context.title, "料理师 part1");
  assert.equal(outputs[0].page_context.page, 7);
  assert.equal(outputs[0].page_context.total, 53);
  assert.equal(outputs[0].page_context.before, before);
  assert.equal(outputs[0].page_context.current_page_text, current);
  assert.equal(outputs[0].page_context.after, after);
  assert.equal(outputs[0].page_context.source, "pc-preprocess");
  assert.equal(
    api.sent.filter((message) => message.type === "response.create").length,
    1,
  );
});

test("visual tool output freezes page text, selection, and question before one final answer", async () => {
  const original = {
    before: "VISUAL_BEFORE_前一页人物背景",
    current: "VISUAL_CURRENT_当前页台词说明人物身份",
    after: "VISUAL_AFTER_后一页继续情节",
    visible: "VISUAL_VISIBLE_当前视口文字",
    selection: "VISUAL_SELECTION_圈内人物",
    question: "VISUAL_QUESTION_他是什么人？",
  };

  for (const tool of ["see_ink", "see_page"]) {
    const api = nativeRealtimeToolHarness({
      page: 7,
      total: 53,
      title: "料理师 part1",
      visibleText: original.visible,
      selection: original.selection,
      question: original.question,
      providerPages: {
        6: original.before,
        7: original.current,
        8: original.after,
      },
    });
    api.holdVisual();
    const pending = api.tool(tool, { page: 7 }, `context-${tool}`);
    await waitForVisual(api);
    assert.equal(api.visualWaiting(), 1, `${tool} must reach exactly one native capture`);

    api.mutate({
      page: 8,
      total: 99,
      title: "MUTATED_TITLE",
      visibleText: "MUTATED_VISIBLE",
      selection: "MUTATED_SELECTION",
      question: "MUTATED_QUESTION",
    });
    api.releaseVisuals();
    await pending;

    const outputs = realtimeToolOutputs(api);
    assert.equal(outputs.length, 1);
    const output = outputs[0];
    assert.equal(output.user_question, original.question);
    assert.equal(output.page_context.contract, "reader-realtime-page-context/1");
    assert.equal(output.page_context.title, "料理师 part1");
    assert.equal(output.page_context.page, 7);
    assert.equal(output.page_context.total, 53);
    assert.equal(output.page_context.before, original.before);
    assert.equal(output.page_context.visible_text, original.visible);
    assert.equal(output.page_context.current_page_text, original.current);
    assert.equal(output.page_context.after, original.after);
    assert.equal(output.page_context.selection, original.selection);
    assert.doesNotMatch(JSON.stringify(output), /MUTATED_/);
    assert.match(output.instruction, /当前用户问题/);
    assert.match(output.instruction, /此前对话/);
    assert.match(output.instruction, /page_context/);
    assert.match(output.instruction, /合成图/);
    assert.match(output.instruction, /综合/);
    assert.match(output.instruction, /不得只描述图片/);

    const outputIndex = api.sent.findIndex((message) =>
      message.type === "conversation.item.create" &&
      message.item && message.item.type === "function_call_output"
    );
    const responses = api.sent.filter((message) => message.type === "response.create");
    const responseIndex = api.sent.findIndex((message) => message.type === "response.create");
    assert.equal(responses.length, 1, `${tool} may request only one final answer`);
    assert.equal(responses[0].response.tool_choice, "none");
    assert.ok(outputIndex >= 0 && outputIndex < responseIndex,
      "the frozen page context must arrive before the final answer request");
  }
});

test("one turn admits only one concurrent visual capture and one answer", async () => {
  const api = nativeRealtimeToolHarness({
    page: 7,
    total: 53,
    visibleText: "并发视觉上下文",
    selection: "当前圈选",
    question: "这里是什么？",
    providerPages: {
      6: "前页",
      7: "当前页",
      8: "后页",
    },
  });
  api.makeFresh(7);
  api.holdVisual();

  const first = api.tool("see_page", { page: 99 }, "queued-page");
  const second = api.tool("see_ink", { page: 7 }, "queued-ink");
  await waitForVisual(api);
  const capturesBeforeRelease = api.visualCalls.length;
  api.releaseVisuals();
  await Promise.all([first, second]);

  assert.equal(capturesBeforeRelease, 1,
    "the visual turn must be claimed before the first await");
  assert.equal(api.visualCalls.length, 1);
  assert.equal(api.visualCalls[0].name, "see_ink",
    "fresh ink upgrades the first queued page request to the sole visual target");
  assert.equal(api.visualCalls[0].args.page, 7);
  assert.equal(api.visualCalls[0].args.scope, "drawing-nearby");

  const outputs = realtimeToolOutputs(api);
  assert.equal(outputs.length, 2, "both model function calls must be closed");
  const suppressed = outputs.filter((output) =>
    output.suppressed === true && output.no_additional_answer === true
  );
  assert.equal(suppressed.length, 1,
    "the duplicate visual call is closed explicitly without another answer");
  assert.equal(outputs.filter((output) => output.page_context).length, 1);
  const responses = api.sent.filter((message) => message.type === "response.create");
  assert.equal(responses.length, 1);
  assert.equal(responses[0].response.tool_choice, "none");

  api.clear();
  api.beginTurn();
  await api.tool("see_page", {}, "next-turn-page");
  assert.equal(api.visualCalls.length, 1,
    "a new user turn may claim a new visual target");
  assert.equal(
    api.sent.filter((message) => message.type === "response.create").length,
    1,
  );
});

test("every new native Realtime call invalidates the injected-context fingerprint", () => {
  const start = VOICE.slice(
    VOICE.indexOf("async function rtcStart"),
    VOICE.indexOf("function toggle(opts)", VOICE.indexOf("async function rtcStart")),
  );
  assert.match(start, /_rtc\.visualTurnEpoch = -1/);
  assert.match(start, /_rtc\._pageFp = ''; _rtc\._sentCtxFp = ''/);
});

test("see_ink failures preserve the actual composition, identity, storage, and transport stage", () => {
  const visual = VOICE.slice(
    VOICE.indexOf("function _visualStageError"),
    VOICE.indexOf("function _nativeRealtimePageText"),
  );
  assert.match(visual, /看图失败\[' \+ stage \+ '\]/);
  assert.match(visual, /attempted\.push\('笔迹裁图'\)/);
  assert.match(visual, /attempted\.push\('整页合成'\)/);
  assert.match(visual, /attempted\.push\('视口截图'\)/);
  assert.match(visual, /_visualStageError\('call 身份'/);
  assert.match(visual, /_visualStageError\('sideband'/);
  assert.match(visual, /_visualStageError\('原生直投'/);
  assert.match(visual, /_rethrowVisualStage/);
  assert.match(visual, /_visualStageError\('本地保存\/传输'/);
  assert.match(visual, /reply && reply\.ok === false/);

  const injection = CORE.slice(
    CORE.indexOf("static func injectImage"),
    CORE.indexOf("static func hangup", CORE.indexOf("static func injectImage")),
  );
  assert.match(injection, /通话标识无效/);
  assert.match(injection, /旁路密钥无效/);
  assert.match(injection, /不支持的图像类型/);
  assert.match(injection, /编码后体积越界/);
  assert.match(injection, /图像 base64 解码失败/);
  assert.match(injection, /解码后体积越界/);
  assert.match(injection, /本地保存合成图失败/);
  assert.doesNotMatch(injection, /throw ReaderRealtimeCredentialError\.imageTooLarge/);
});

test("visual-tool failures keep their route and stage visible in the rendered tool flow", () => {
  const toolRun = VOICE.slice(
    VOICE.indexOf("async function _rtcTool"),
    VOICE.indexOf("function _rtcCapReset"),
  );
  assert.match(
    toolRun,
    /!_rtc\.nativeDirect && \/\^\(see_ink\|see_page\|see_figure\)\$\//,
  );
  assert.match(toolRun, /'模型工具触发\/路由'/);
  assert.match(toolRun, /route: route/);
  assert.match(toolRun, /stage: stage \|\| undefined/);
  assert.match(toolRun, /native_local: window\.__BW_NATIVE_LOCAL_READER__ === true/);
  assert.match(toolRun, /native_flag: window\.__BW_NATIVE_OPENAI_REALTIME__ === true/);
  assert.match(toolRun, /native_bridge: !!\(window\.__bwNativeRealtime/);

  const chipEnd = VOICE.slice(
    VOICE.indexOf("function _chipEnd"),
    VOICE.indexOf("function onToolStatus"),
  );
  const errorBranch = chipEnd.slice(
    chipEnd.indexOf("if (p.status === 'error')"),
    chipEnd.indexOf("// ④", chipEnd.indexOf("if (p.status === 'error')")),
  );
  assert.match(errorBranch, /errorDetail = String\(p\.rag \|\| p\.result_brief/);
  assert.match(errorBranch, /result: errorDetail/);
  assert.match(errorBranch, /error: errorDetail/);
});

test("visual-tool waits are bounded and every host exposes the visible step log first", async () => {
  const visual = VOICE.slice(
    VOICE.indexOf("function _visualStageError"),
    VOICE.indexOf("function _nativeRealtimePageText"),
  );
  assert.match(visual, /window\.dlog\('see: ' \+ text/);
  assert.match(visual, /RC\.captureInkRegion\(target\), 26000/);
  assert.match(visual, /RC\.capturePageComposite[\s\S]*26000, '原生取图并直投\/整页合成'/);
  assert.match(
    visual,
    /compositeTarget = name === 'see_page'[\s\S]*\? target[\s\S]*scope: 'viewport-context'/,
    "see_page must request the complete logical page while see_figure keeps viewport semantics",
  );
  assert.match(visual, /_captureView\(delivery\), 26000, '原生取图并直投\/视口截图'/);
  assert.match(visual, /_nativeRealtimeRequest\([\s\S]*15000, '本地保存\/传输'/);
  assert.ok(
    visual.indexOf("if (reply && reply.ok === false)") <
      visual.indexOf("_visualStep('原生通道已接受')"),
    "the log must not claim acceptance before checking the native reply",
  );
  assert.match(
    VOICE,
    /bridge=' \+ String\(!!\(window\.__bwNativeRealtime &&[\s\S]*typeof window\.__bwNativeRealtime\.request === 'function'\)\)/,
  );

  assert.match(SETTINGS, /if \(typeof window\.dlog !== 'function'\)/);
  for (const template of [
    "_server_deploy/templates/pdf_reader.html",
    "_server_deploy/templates/epub_html_reader.html",
    "_server_deploy/templates/html_reader.html",
  ]) {
    const html = read(template);
    assert.ok(
      html.indexOf('<script src="/static/pdf/rc-settings.js') <
        html.indexOf('<script src="/static/pdf/rc-voicecall.js'),
      `${template} must install the visible log before Realtime tools`,
    );
  }

  const definitions = VOICE.slice(
    VOICE.indexOf("function _visualStageError"),
    VOICE.indexOf("async function _nativeRealtimeVisual"),
  );
  const sandbox = { setTimeout, clearTimeout, Promise, Error };
  vm.runInNewContext(
    `${definitions}; this.withVisualTimeout = _withVisualTimeout;`,
    sandbox,
  );
  await assert.rejects(
    sandbox.withVisualTimeout(new Promise(() => {}), 5, "probe-stage"),
    (error) => {
      assert.equal(error.bwVisualStage, "probe-stage");
      assert.match(error.message, /等待 5ms 无响应/);
      return true;
    },
  );
  assert.equal(
    await sandbox.withVisualTimeout(Promise.resolve("ok"), 50, "fast"),
    "ok",
  );
  const lateRejection = new Promise((resolve, reject) => {
    setTimeout(() => reject(new Error("late")), 20);
  });
  await assert.rejects(
    sandbox.withVisualTimeout(lateRejection, 5, "late-stage"),
    /等待 5ms 无响应/,
  );
  await new Promise((resolve) => setTimeout(resolve, 30));
});

test("App-native visuals stay native and are delivered to Realtime without a JPEG round trip through JS", () => {
  const capture = VOICE.slice(
    VOICE.indexOf("async function _nativeCapture"),
    VOICE.indexOf("function _viewportRectFromPageRect"),
  );
  assert.match(capture, /q \+= '&deliver=realtime'/);
  assert.match(capture, /request\.method = 'POST'/);
  assert.match(capture, /call_id: delivery\.call_id/);
  assert.match(capture, /client_secret: delivery\.client_secret/);
  assert.match(capture, /native_delivered: true/);
  assert.match(capture, /directItemID = \/\^bwi_/);
  assert.match(capture, /item_id: directItemID/);
  assert.match(capture, /resp\.arrayBuffer\(\)/);
  assert.ok(
    capture.indexOf("if (delivery)") < capture.indexOf("resp.arrayBuffer()"),
    "direct delivery must return its small receipt before reading image bytes",
  );

  const visual = VOICE.slice(
    VOICE.indexOf("async function _nativeRealtimeVisual"),
    VOICE.indexOf("function _nativeRealtimePageText"),
  );
  assert.match(visual, /if \(shot\.native_delivered\)/);
  assert.match(visual, /图像已在原生层直接送入当前 Realtime 会话/);
  assert.ok(
    visual.indexOf("if (shot.native_delivered)") <
      visual.indexOf("action: 'image'"),
    "a native delivery receipt must bypass the legacy b64 bridge request",
  );

  const tool = VOICE.slice(
    VOICE.indexOf("async function _rtcTool"),
    VOICE.indexOf("function _rtcCapReset"),
  );
  assert.match(
    tool,
    /vision = nativeShot && nativeShot\.native_delivered \? null : \[nativeShot\]/,
  );

  const route = LOCAL_SERVER.slice(
    LOCAL_SERVER.indexOf("private func serveNativeVisualCapture"),
    LOCAL_SERVER.indexOf("private func serveNativePageImage"),
  );
  assert.match(route, /request\.query\["deliver"\] == "realtime"/);
  assert.match(route, /request\.method == \.POST/);
  assert.match(route, /request\.bodyData/);
  assert.match(route, /Set\(object\.keys\) == \["call_id", "client_secret", "tool"\]/);
  assert.match(route, /imageData: capture\.jpegData/);
  assert.match(route, /let itemID = try await ReaderRealtimeOpenAIClient\.injectImage/);
  assert.match(route, /"item_id": itemID/);
  assert.match(route, /"delivered": true/);
  assert.match(route, /"bytes": capture\.jpegData\.count/);
  assert.match(route, /BW_NATIVE_VISUAL_DELIVERY_FAILED/);

  const rawInjection = CORE.slice(
    CORE.indexOf("static func injectImage(\n        callID: String,\n        clientSecret: String,\n        mediaType: String,\n        imageData: Data"),
    CORE.indexOf("private static func injectPreparedImage"),
  );
  assert.match(rawInjection, /\) async throws -> String/);
  assert.match(rawInjection, /imageData\.base64EncodedString\(\)/);
  assert.match(rawInjection, /return try await injectPreparedImage/);
  assert.match(rawInjection, /imageData: imageData/);
  assert.doesNotMatch(rawInjection, /Data\(base64Encoded:/);
  const preparedInjection = CORE.slice(
    CORE.indexOf("private static func injectPreparedImage"),
    CORE.indexOf("static func hangup", CORE.indexOf("private static func injectPreparedImage")),
  );
  assert.match(preparedInjection, /let itemID = "bwi_"/);
  assert.match(preparedInjection, /return itemID/);
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
