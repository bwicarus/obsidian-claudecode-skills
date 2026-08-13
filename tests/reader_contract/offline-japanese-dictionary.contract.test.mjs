import test from "node:test";
import assert from "node:assert/strict";
import vm from "node:vm";
import { readFileSync } from "node:fs";
import { TextEncoder } from "node:util";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const source = read("_server_deploy/static/pdf/rc-offline-dictionary.js");

function harness(resources, { local = true } = {}) {
  const requests = [];
  const sandbox = {
    TextEncoder,
    Map,
    Set,
    Promise,
    console,
    async fetch(url) {
      const prefix = "/r/fixture/native-api/offline-dictionary/";
      const path = String(url).startsWith(prefix)
        ? String(url).slice(prefix.length)
        : String(url);
      requests.push(path);
      if (!(path in resources)) {
        return { ok: false, status: 404, async json() { return {}; } };
      }
      return {
        ok: true,
        status: 200,
        async json() { return structuredClone(resources[path]); },
      };
    },
  };
  sandbox.window = sandbox;
  sandbox.RC = {};
  if (local) {
    sandbox.__BW_NATIVE_LOCAL_BASE_PATH__ = "/r/fixture";
  }
  vm.runInNewContext(source, sandbox, { filename: "rc-offline-dictionary.js" });
  return { dictionary: sandbox.RC.offlineDictionary, requests };
}

test("offline dictionary restores Japanese stem and prefers packaged Chinese gloss", async () => {
  const resources = {
    "manifest.json": {
      contract: "bw-jmdict-manifest/2",
      normalization: "NFC",
      shardAlgorithm: "utf8-prefix-2-kana-3/1",
      source: { release: "fixture" },
      posLabels: { v1: "Ichidan verb" },
      shards: { e58f: { path: "shards/e58f.json" } },
    },
    "shards/e58f.json": {
      contract: "bw-jmdict-shard/2",
      key: "e58f",
      entries: [{
        id: "1",
        lemma: "取り寄せる",
        forms: ["取り寄せる"],
        readings: ["とりよせる"],
        pos: ["v1"],
        glosses: ["to order", "to have something sent"],
        zhGlosses: ["订购；调货；从外地寄来"],
        common: true,
      }],
      exact: { "取り寄せる": [0] },
    },
  };
  const { dictionary, requests } = harness(resources);
  const result = await dictionary.lookupJapaneseLegacy("取り寄せ");
  assert.equal(result.ok, true);
  assert.equal(result.lemma, "取り寄せる");
  assert.equal(result.reading, "とりよせる");
  assert.equal(result.zh, "订购；调货；从外地寄来");
  assert.equal(result.translation, "订购；调货；从外地寄来");
  assert.equal(result.local_zh, true);
  assert.equal(result.meaning_language, "zh");
  assert.equal(result.english_fallback, false);
  assert.equal(result.inflect.base, "取り寄せる");
  assert.equal(result.source, "local-jmdict");
  assert.deepEqual(requests, [
    "manifest.json",
    "shards/e58f.json",
  ]);
});

test("English remains an explicit fallback when Chinese data has no safe match", async () => {
  const resources = {
    "manifest.json": {
      contract: "bw-jmdict-manifest/2",
      normalization: "NFC",
      shardAlgorithm: "utf8-prefix-2-kana-3/1",
      source: { release: "fixture" },
      posLabels: { n: "noun" },
      shards: { e689: { path: "shards/e689.json" } },
    },
    "shards/e689.json": {
      contract: "bw-jmdict-shard/2",
      key: "e689",
      entries: [{
        id: "2",
        lemma: "手法",
        forms: ["手法"],
        readings: ["しゅほう"],
        pos: ["n"],
        glosses: ["technique", "method"],
        common: true,
      }],
      exact: { "手法": [0] },
    },
  };
  const { dictionary, requests } = harness(resources);
  const result = await dictionary.lookupJapaneseLegacy("手法");
  assert.equal(result.reading, "しゅほう");
  assert.equal(result.pos, "noun");
  assert.equal(result.translation, "technique; method");
  assert.equal(result.local_zh, false);
  assert.equal(result.meaning_language, "en");
  assert.equal(result.english_fallback, true);
  assert.deepEqual(requests, ["manifest.json", "shards/e689.json"]);
});

test("non-local PWA reports unavailable instead of silently contacting Pi", async () => {
  const { dictionary, requests } = harness({}, { local: false });
  const result = await dictionary.lookupJapanese("取り寄せ");
  assert.equal(result.ok, false);
  assert.equal(result.unavailable, true);
  assert.deepEqual(requests, []);
});

test("App without a downloaded dictionary reports the install state", async () => {
  const { dictionary, requests } = harness({});
  const result = await dictionary.lookupJapanese("取り寄せ");
  assert.equal(result.ok, false);
  assert.equal(result.unavailable, true);
  assert.equal(result.code, "BW_OFFLINE_DICTIONARY_NOT_INSTALLED");
  assert.deepEqual(requests, ["manifest.json"]);
});

test("kana shards use the complete first kana while kanji keeps two bytes", () => {
  const { dictionary } = harness({});
  assert.equal(dictionary._shardKey("あう"), "e38182");
  assert.equal(dictionary._shardKey("取り寄せ"), "e58f");
});

test("lookup code ships everywhere but dictionary bytes ship nowhere", () => {
  for (const path of [
    "_server_deploy/templates/pdf_reader.html",
    "_server_deploy/templates/epub_html_reader.html",
    "_server_deploy/templates/html_reader.html",
    "_server_deploy/templates/web_live.html",
  ]) {
    const html = read(path);
    assert.ok(
      html.indexOf("rc-offline-dictionary.js") < html.indexOf("rc-wordpop.js"),
      path,
    );
  }
  const manifest = JSON.parse(read("extensions/bw-reader-webext/manifest.json"));
  const scripts = manifest.content_scripts.find((item) =>
    item.js?.includes("vendor/rc-wordpop.js"))?.js || [];
  assert.ok(scripts.indexOf("vendor/rc-offline-dictionary.js") >= 0);
  assert.ok(
    scripts.indexOf("vendor/rc-offline-dictionary.js") <
      scripts.indexOf("vendor/rc-wordpop.js"),
  );

  const project = read("ios/BWReader/project.yml");
  assert.doesNotMatch(project, /Extension\/Resources\/dictionary-data/);

  const safariPackager = read("extensions/bw-reader-webext/package_safari.py");
  assert.match(safariPackager, /ROOT_DIRS = \("src", "vendor", "icons"\)/);
  assert.doesNotMatch(safariPackager, /ROOT_DIRS = .*dictionary-data/);

  const facade = read("extensions/bw-reader-webext/src/facade.js");
  const background = read("extensions/bw-reader-webext/background.js");
  assert.doesNotMatch(facade, /__bwOfflineJapaneseDictionaryBridge/);
  assert.doesNotMatch(background, /BW_OFFLINE_DICTIONARY_JSON/);

  const pdfReader = read("_server_deploy/pdf_reader.py");
  const htmlReader = read("_server_deploy/html_reader.py");
  assert.equal((pdfReader.match(/"pdf\/rc-offline-dictionary\.js"/g) || []).length, 2);
  assert.equal((htmlReader.match(/"pdf\/rc-offline-dictionary\.js"/g) || []).length, 1);
});

test("App download stays in private Application Support and outside backup/sync", () => {
  const nativeStore = read("ios/BWReader/App/ReaderOfflineDictionary.swift");
  const localServer = read("ios/BWReader/App/ReaderLocalRuntimeServer.swift");
  const settings = read("ios/BWReader/App/NativeReaderToolsView.swift");
  assert.match(nativeStore, /\.applicationSupportDirectory/);
  assert.match(nativeStore, /OfflineJapaneseDictionary/);
  assert.match(nativeStore, /isExcludedFromBackup = true/);
  assert.match(nativeStore, /raw\.githubusercontent\.com/);
  assert.doesNotMatch(nativeStore, /containerURL\(forSecurityApplicationGroupIdentifier/);
  assert.match(localServer, /native-api\/offline-dictionary\//);
  assert.match(settings, /下载离线日语词典/);
  assert.match(settings, /不进入书籍附件、Pi、Safari 扩展或设置同步/);
});

test("Japanese UI uses ReaderPC CLI first and keeps Pi only as an explicit legacy fallback", () => {
  const wordpop = read("_server_deploy/static/pdf/rc-wordpop.js");
  const phrasepop = read("_server_deploy/static/pdf/rc-phrasepop.js");
  const computerVoice = read("_server_deploy/static/pdf/rc-computer-voice.js");
  const nativeRuntime = read("_server_deploy/static/pdf/native-local-runtime.js");
  const protocol = read(
    "extensions/bw-reader-webext/windows/ComputerVoiceAudio/DirectBridgeProtocol.cs",
  );
  assert.match(wordpop, /电脑 CLI 精释/);
  assert.match(wordpop, /改用 Pi 旧版精释/);
  assert.match(wordpop, /_lookupJapaneseLocalFirst/);
  assert.match(computerVoice, /lookupJapaneseFallback/);
  assert.match(computerVoice, /"dictionary-lookup"/);
  assert.match(nativeRuntime, /dictionaryFallbackCache/);
  assert.match(protocol, /HandleDictionaryLookupAsync/);
  assert.doesNotMatch(wordpop, /暂无词典释义（可能是人名\/专有名词），已请 AI 讲解/);
  assert.match(phrasepop, /Pi AI 精释/);
});
