import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const EXT = "extensions/bw-reader-webext/";
const BACKGROUND = read(EXT + "background.js");
const POPUP_HTML = read(EXT + "popup.html");
const POPUP_JS = read(EXT + "popup.js");
const FACADE = read(EXT + "src/facade.js");
const WEB_ADAPTER = read(EXT + "src/web-adapter.js");
const MANIFEST = JSON.parse(read(EXT + "manifest.json"));
const HANDLER = read("ios/BWReader/Extension/SafariWebExtensionHandler.swift");
const CONTRACT = read("ios/BWReader/Shared/ReaderNativeBridgeContract.swift");
const STORE = read("ios/BWReader/Shared/ReaderAccountTokenCore.swift");
const PROVISIONER = read("ios/BWReader/App/ReaderAccountTokenProvisioner.swift");
const LOGIN_VIEW = read("ios/BWReader/App/ReaderPiLoginView.swift");
const APP = read("ios/BWReader/App/BWReaderNativeApp.swift");
const PROJECT = read("ios/BWReader/project.yml");

const WINDOWS = "https://bwicarus-2.taile44d0c.ts.net";

// 用户 2026-09-06：「现在版本网页扩展无法使用，就不能设计为 App 登录后扩展自动登录么」。
// 两件事：① 扩展的账户服务器还指着已停机的 Pi，所以令牌校验永远拿不到"账户证明"；
// ② App 登录后替扩展铸设备令牌、扩展经原生桥取走，用户不再需要去 /profile/ 复制。

test("扩展不再打 Pi：账户 ORIGIN、host_permissions、门面重写、旧阅读器链接全部指向 Windows 桥", () => {
  assert.match(BACKGROUND, /const ORIGIN = "https:\/\/bwicarus-2\.taile44d0c\.ts\.net";/);
  assert.doesNotMatch(BACKGROUND, /bwicarus\.taile44d0c/, "background 不许再出现 Pi 主机");
  assert.doesNotMatch(FACADE, /bwicarus\.taile44d0c/, "门面重写 /pdf/api/* 相对路径的目标要是 Windows");
  assert.doesNotMatch(WEB_ADAPTER, /bwicarus\.taile44d0c/);
  assert.deepEqual(MANIFEST.host_permissions, [WINDOWS + "/*", "https://api.openai.com/*"]);
  for (const script of MANIFEST.content_scripts) {
    for (const pattern of script.matches) {
      assert.doesNotMatch(pattern, /bwicarus\.taile44d0c/, pattern);
    }
  }
});

test("App 登录后铸令牌：登录成功那一刻与启动时都调 ensureToken，令牌进共享 Keychain", () => {
  assert.match(LOGIN_VIEW, /ReaderAccountTokenProvisioner\.shared\.ensureToken\(/, "登录成功回调里铸");
  assert.match(APP, /ReaderAccountTokenProvisioner\.shared\.ensureToken\(/, "启动时补铸（幂等）");
  assert.match(PROVISIONER, /appendingPathComponent\("api\/tokens"\)/, "用服务端现成的 /api/tokens");
  assert.match(PROVISIONER, /request\.httpShouldHandleCookies = false/, "cookie 显式从 App 的网站数据存储取，不靠默认 jar");
  assert.match(PROVISIONER, /ReaderAccountTokenStore\.shared\.save\(/);
  assert.match(PROVISIONER, /loadIfPresent\(\)/, "已有令牌就不再铸");
  assert.match(PROVISIONER, /throw ReaderAccountTokenError\.notLoggedIn/, "302 到登录页当「没登录」，不当服务器坏了");
  assert.match(STORE, /kSecAttrAccessGroup as String:\s*\n\s*ReaderNativeBridgeContract\.realtimeKeychainAccessGroup/,
    "与 Realtime 凭证同一个签名访问组，扩展原生进程才读得到");
  assert.match(STORE, /kSecAttrAccessibleWhenUnlockedThisDeviceOnly/);
  assert.match(STORE, /space\.bwicarus\.bwreader2\.account-token/);
  // Shared/ 下的文件每个 target 都要单独列（project.yml 里的老教训）。
  assert.equal((PROJECT.match(/Shared\/ReaderAccountTokenCore\.swift/g) ?? []).length, 2, "App 与 Extension 两个 target 都要列");
});

test("原生桥新增 account.token：契约清单、handler 分支、扩展白名单三处一致", () => {
  assert.match(CONTRACT, /"account\.token",/);
  assert.match(HANDLER, /case "account\.token":/);
  assert.match(HANDLER, /ReaderAccountTokenStore\.shared\.loadIfPresent\(\)/);
  assert.match(HANDLER, /code: "BW_ACCOUNT_APP_NOT_LOGGED_IN"/, "App 没登录要有专门的码，弹窗据此指路");
  assert.match(BACKGROUND, /"account\.token"\n\]\);/, "扩展 NATIVE_APP_ACTIONS 也要放行");
});

test("扩展取令牌走与手工粘贴相同的路径：token-owner 证明 → 账户上下文 → 凭据存储", () => {
  assert.match(BACKGROUND, /async function fetchTokenOwnerNamespace\(token, fence = \(\) => \{\}\)/);
  assert.match(BACKGROUND, /async function adoptAppAccountToken\(reason\)/);
  const adopt = BACKGROUND.slice(
    BACKGROUND.indexOf("async function adoptAppAccountToken(reason)"),
    BACKGROUND.indexOf("async function adoptAppAccountTokenIfNeeded(reason)"));
  assert.match(adopt, /action: "account\.token"/);
  assert.match(adopt, /!== ORIGIN/, "App 登录的服务器必须与扩展配置同一台");
  assert.match(adopt, /await fetchTokenOwnerNamespace\(token\)/, "App 给的令牌同样要向服务器换账户证明");
  assert.match(adopt, /rememberVerifiedAccount\(namespace, "", "app-token"\)/);
  assert.match(adopt, /accountStorage\.saveVerifiedToken\(/);
  assert.match(BACKGROUND, /"BW_ACCOUNT_FROM_APP",\n  "BW_ACCOUNT_STATUS",/, "弹窗消息进账户消息集合，走 popup 专属通道");
  assert.match(BACKGROUND, /if \(error\?\.code !== "BW_ACCOUNT_CONTEXT_UNAVAILABLE"\) throw error;\s*\n\s*captured = await adoptAppAccountToken\("app-token-auto"\);/,
    "没有账户上下文时先问 App，而不是让用户去开 PWA");
  assert.match(BACKGROUND, /adoptAppAccountTokenSilently\("worker-start"\)/, "启动时静默尝试");
  for (const code of ["BW_ACCOUNT_APP_NOT_LOGGED_IN", "BW_ACCOUNT_APP_ORIGIN_MISMATCH", "BW_NATIVE_APP_UNAVAILABLE"]) {
    assert.match(BACKGROUND, new RegExp(`"${code}",`), `${code} 要在可透传给弹窗的错误码里`);
  }
  assert.doesNotMatch(BACKGROUND, /请先打开一次已登录的 BW 书籍 PWA/, "PWA 已退役，不能再让用户去开它");
});

test("弹窗：先问 App、手工粘贴退为备用，文案不再提 PWA", () => {
  assert.match(POPUP_HTML, /id="from-app"/);
  assert.match(POPUP_HTML, /<details class="manual">/, "手工粘贴收进折叠区");
  assert.doesNotMatch(POPUP_HTML, /PWA/);
  assert.match(POPUP_JS, /accountMessage\("BW_ACCOUNT_FROM_APP"\)/);
  assert.match(POPUP_JS, /if \(!data\?\.credential\?\.configured\) await adoptFromApp\(true\);/, "打开弹窗发现没令牌就自动去要");
});
