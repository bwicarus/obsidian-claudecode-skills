import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const BRIDGE = read("ios/BWReader/App/ReaderNativeAppPrefsBridge.swift");
const SURFACES = read("ios/BWReader/App/ReaderNativeSurfaceState.swift");
const WEBVIEW = read("ios/BWReader/App/ReaderWebView.swift");
const SETTINGS = read("_server_deploy/static/pdf/rc-settings.js");
const KEYVIEW = read("ios/BWReader/App/ReaderRealtimeKeyView.swift");

/// 去掉行注释与块注释。断言要守的是代码的行为 —— 一句解释性的注释里出现同样的
/// 字面量，不该让 doesNotMatch 假红，也不该让 match 假绿。
const codeOnly = (source) =>
  source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
const BRIDGE_CODE = codeOnly(BRIDGE);
const SURFACES_CODE = codeOnly(SURFACES);
const SETTINGS_CODE = codeOnly(SETTINGS);

// 用户 2026-08-18：两个原生按钮里的内容没好好分类，应该并进我们自己的设置 tab。
// 这个通道就是"并进来"所需要的那一层。以下三条是它的不变量。

test("桥先验来源再看内容 —— 内嵌的第三方子框不能调它", () => {
  // 此前这个桥**全程没有 frame/origin 校验**，而导航策略允许 youtube-nocookie /
  // player.bilibili.com 作为同页子框存在，addScriptMessageHandler 的 handler 对该
  // content world 的所有 frame 可见。当时暴露面只有 4 个偏好键；现在多了
  // "删词典 / 撤 Vault 授权 / 弹原生 picker"，这道闸就不能再缺。
  assert.match(BRIDGE, /var isTrustedFrame: \(\(WKScriptMessage\) -> Bool\)\?/);
  const guardAt = BRIDGE.indexOf("isTrustedFrame?(message) == true");
  const switchAt = BRIDGE.indexOf("switch action {");
  const bodyAt = BRIDGE.indexOf("message.body as? [String: Any]");
  assert.ok(guardAt >= 0, "桥没有来源闸");
  assert.ok(guardAt < bodyAt, "来源闸排在解析 body 之后 —— 顺序反了");
  assert.ok(guardAt < switchAt);

  // 闸的内容必须是双检，跟本地笔记桥同一套
  assert.match(WEBVIEW, /nativeAppPrefsBridge\.isTrustedFrame = \{/);
  const wiring = WEBVIEW.slice(
    WEBVIEW.indexOf("nativeAppPrefsBridge.isTrustedFrame = {"),
    WEBVIEW.indexOf("nativeAppPrefsBridge.surfacesProvider"),
  );
  assert.match(wiring, /message\.frameInfo\.isMainFrame/);
  assert.match(wiring, /isTrustedReaderURL\(self\.webView\.url\)/);
  assert.match(wiring, /isTrustedReaderURL\(message\.frameInfo\.request\.url\)/);
});

test("网页发的 action 与原生认识的 action 完全对齐", () => {
  // 两边名字漂开时不会有人报错 —— 网页那侧只会看到一个 rejected promise，
  // 而 catch 里通常只是一句 toast。所以把它钉成机器可查的事实。
  // 多值 case（case "a", "b", "c": 可能跨行）要整块取，不能只抓每行第一个。
  const nativeActions = new Set();
  for (const block of BRIDGE_CODE.matchAll(/case ((?:\s*"[a-zA-Z]+"\s*,?)+)\s*:/g)) {
    for (const m of block[1].matchAll(/"([a-zA-Z]+)"/g)) nativeActions.add(m[1]);
  }

  const webActions = new Set();
  for (const m of SETTINGS_CODE.matchAll(/action: '([a-zA-Z]+)'/g)) {
    webActions.add(m[1]);
  }
  for (const m of SETTINGS_CODE.matchAll(/_natDo\('([a-zA-Z]+)'/g)) {
    webActions.add(m[1]);
  }

  const unknown = [...webActions].filter((a) => !nativeActions.has(a));
  assert.deepEqual(
    unknown,
    [],
    `网页发了原生不认识的 action：${unknown.join(", ")}`,
  );
  // 反向不强制全等：原生可以先放行一个动作、网页稍后再用。
  for (const required of [
    "list", "get", "set", "surfaces",
    "dictDownload", "dictRemove",
    "vaultSetEnabled", "vaultClear", "realtimeClear",
    "openVaultPicker", "openRealtimeKey", "openPiLogin",
  ]) {
    assert.ok(nativeActions.has(required), `原生缺 action：${required}`);
  }
});

test("密钥、bookmark、文件路径永不经过这条通道", () => {
  // 这是边界，不是实现细节：Key 只经原生 SecureField 进 Keychain，
  // 文件夹只经原生 picker 产出的 security-scoped URL 做 bookmark。
  // 所以通道里既没有"保存 Key"的动作，回包里也不含这些东西。
  assert.doesNotMatch(BRIDGE_CODE, /realtimeSave|apiKey|folderBookmark|bookmarkData/);
  assert.doesNotMatch(SURFACES_CODE, /apiKey|bookmarkData|folderBookmark/);
  // 网页侧不得出现任何把 key 值发进通道的写法
  assert.doesNotMatch(SETTINGS_CODE, /action:\s*'realtimeSave'/);
  assert.doesNotMatch(SETTINGS_CODE, /_natDo\('realtimeSave'/);
  // 输入 Key 的 UI 必须还在原生
  assert.match(KEYVIEW, /SecureField\("输入现有 OpenAI Key/);
  assert.match(KEYVIEW, /saveExistingKey\(draft\)/);
  // folderName 给的是名字不是路径（ReaderLocalNotes 存的本来就是 lastPathComponent）
  assert.match(SURFACES_CODE, /"folderName": notes\.folderName/);
});

test("原生动作的失败原因要能到用户眼前", () => {
  // references/silent-failure-lessons.md：每个提前退出都要出声。
  // Vault 还有 pending 笔记时 clearFolder 会拒绝 —— 那句话必须传上去。
  assert.match(SURFACES, /return manager\.errorMessage/);
  // 词典在下载中删除会被原方法静默 return，这里前置说明原因
  assert.match(SURFACES, /正在下载，先暂停或等它完成再删除/);
  // 未知动作不能静默成功
  assert.match(SURFACES, /return "未知动作：\\\(action\)"/);
  // 网页侧把失败原样 toast 出来，不吞
  assert.match(SETTINGS, /var why = \(err && \(err\.message \|\| err\)\)/);
});

test("三项真的在设置 tab 里，而不是只有一个跳转按钮", () => {
  for (const id of [
    "rcset-nat-dict-dl", "rcset-nat-dict-rm",
    "rcset-nat-vault-on", "rcset-nat-vault-pick", "rcset-nat-vault-clr",
    "rcset-nat-key-set", "rcset-nat-key-clr", "rcset-nat-pi-login",
  ]) {
    assert.ok(SETTINGS.includes(id), `设置 tab 缺控件：${id}`);
  }
  // 隐私声明跟着内容一起搬过来 —— 它是对用户的承诺，不能在搬家时掉队
  assert.match(SETTINGS, /不进入书籍附件、Pi 或设置同步/);
  assert.match(SETTINGS, /都不连接 Pi/);
  // 每次打开面板都要重读状态（原生那侧会自己变，网页没有推送通道）
  assert.match(SETTINGS, /_fillNativePane\(\);\s+\/\/ 本机 tab/);
  // 下载进度的轮询必须在关面板时停掉
  assert.match(SETTINGS, /_natStopPoll\(\);\s+\/\/ 词典下载进度的轮询/);
});
