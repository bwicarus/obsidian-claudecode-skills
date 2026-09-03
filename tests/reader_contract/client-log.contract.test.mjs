import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const RUNTIME = read("_server_deploy/static/pdf/native-local-runtime.js");
const SERVER = read("_server_deploy/pdf_reader.py");
const POLICY = read("_server_deploy/static/reader-runtime/interaction-policy.js");
const MANIFEST = JSON.parse(read("ios/BWReader/native_reader_interface_manifest.json"));
const VOICE = read("_server_deploy/static/pdf/rc-computer-voice.js");

const bodyOf = (source, name) => {
  const start = source.indexOf(`function ${name}(`);
  if (start < 0) return "";
  const next = source.slice(start + 1).search(/\n {2}function /);
  return next < 0 ? source.slice(start) : source.slice(start, start + 1 + next);
};

// 用户建议（2026-09-03）：「做新的功能时都要有详细的报错日志……直接把报错写到某个文件
// 这样我遇到问题你就可以去查看了」。这里锁住四处同步（路由白名单有几份副本就要改几份）。

test("路由四处同步：服务端实现、manifest、interaction-policy、运行时上报器", () => {
  assert.match(SERVER, /@bp\.route\("\/api\/client-log", methods=\["POST"\]\)/);
  assert.match(SERVER, /_CLIENT_LOG_DIR = CLAUDE_DIR \/ "state" \/ "reader-client-log"/);
  const routes = (MANIFEST.routes || MANIFEST.interfaces || []).filter?.((r) => r.path === "/pdf/api/client-log")
    ?? [];
  const found = JSON.stringify(MANIFEST).includes('"/pdf/api/client-log"');
  assert.equal(found, true, "manifest 必须声明 /pdf/api/client-log(owner=pi)");
  assert.match(POLICY, /'diagnostics\.client-log\.report',\n\s*'\/pdf\/api\/client-log',\n\s*\['POST'\]/);
  // 诊断不能反过来制造积压：离线丢弃、不进 outbox
  assert.match(POLICY, /'diagnostics\.client-log\.report'[\s\S]{0,300}offline: 'drop'/);
  assert.match(RUNTIME, /root\.fetch\(localBasePath\(\) \+ '\/pdf\/api\/client-log'/);
  void routes;
});

test("上报器：包住 dlog、接住未捕获异常、批量而有上限", () => {
  const install = bodyOf(RUNTIME, "installClientLogReporter");
  assert.match(install, /Object\.defineProperty\(root, 'dlog'/);
  // 声明式全局(PDF 页 function dlog)不可重定义时退回赋值包一层
  assert.match(install, /if \(!trapped\) root\.dlog = current;/);
  assert.match(install, /root\.addEventListener\('error'/);
  assert.match(install, /root\.addEventListener\('unhandledrejection'/);
  assert.match(install, /root\.addEventListener\('pagehide'/);
  const push = bodyOf(RUNTIME, "clientLogPush");
  assert.match(push, /if \(clientLogBuffer\.length > 400\) clientLogBuffer\.splice\(0, clientLogBuffer\.length - 400\);/);
  const flush = bodyOf(RUNTIME, "clientLogFlush");
  assert.match(flush, /var batch = clientLogBuffer\.splice\(0, 200\);/);
  // 失败放回去、不制造重试风暴
  assert.match(flush, /clientLogBuffer = batch\.concat\(clientLogBuffer\)\.slice\(-400\);/);
  assert.match(RUNTIME, /installClientLogReporter\(\);\n\n\s*runtimeRoot\.nativeLocalRuntime = api;/);
});

test("服务端：按用户写 JSONL、20MB 轮转、每批/每行/整包三重上限", () => {
  const route = SERVER.slice(SERVER.indexOf("def pdf_api_client_log("), SERVER.indexOf("@bp.route(\"/api/lookup-event\""));
  assert.match(route, /if len\(raw\) > 256 \* 1024:/);
  assert.match(route, /for item in lines\[:200\]:/);
  assert.match(route, /\[:2000\]/);
  assert.match(route, /_CLIENT_LOG_MAX_BYTES/);
  assert.match(route, /f"\{user\}\.jsonl"/);
});

// 同日实锤：一张失配的已锚卡曾让整页正文永远不上报。快照的每个提前退出现在都出声。
test("快照：失配已锚卡跳过并 dlog，正文构建有超时栅栏", () => {
  const projection = bodyOf(VOICE, "buildLocalPageCardProjection");
  assert.match(projection, /unresolvedCards\.push\(\{ id: card\.id, label: card\.label \}\)/);
  assert.match(projection, /window\.dlog\("快照:第 " \+ page \+ " 页 " \+ unresolvedCards\.length/);
  assert.doesNotMatch(projection, /throw new Error\("已锚定卡片几何暂不可解析"\)/);
  assert.match(VOICE, /var LOCAL_PAGE_CONTEXT_BUILD_TIMEOUT_MS = 20 \* 1000;/);
  const publish = bodyOf(VOICE, "maybePublishLocalPageContext");
  assert.match(publish, /reject\(new Error\("本机正文构建超时\(" \+ LOCAL_PAGE_CONTEXT_BUILD_TIMEOUT_MS \+ "ms\)"\)\)/);
  // 绑定自带字符索引集合时优先按它解析
  const resolve = bodyOf(VOICE, "resolveLocalCardRange");
  assert.match(resolve, /if \(Array\.isArray\(want && want\.ois\) && want\.ois\.length\)/);
});
