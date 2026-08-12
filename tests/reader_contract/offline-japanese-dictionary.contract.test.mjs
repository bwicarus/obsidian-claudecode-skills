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
  };
  sandbox.window = sandbox;
  sandbox.RC = {};
  if (local) {
    sandbox.__bwOfflineJapaneseDictionaryBridge = {
      async fetchJson(path) {
        requests.push(path);
        if (!(path in resources)) throw new Error(`missing ${path}`);
        return structuredClone(resources[path]);
      },
    };
  }
  vm.runInNewContext(source, sandbox, { filename: "rc-offline-dictionary.js" });
  return { dictionary: sandbox.RC.offlineDictionary, requests };
}

test("offline dictionary restores Japanese stem without trusting the legacy AI overlay", async () => {
  const resources = {
    "manifest.json": {
      contract: "bw-jmdict-manifest/1",
      normalization: "NFC",
      shardAlgorithm: "utf8-prefix-2-kana-3/1",
      source: { release: "fixture" },
      posLabels: { v1: "Ichidan verb" },
      zhOverlay: { path: "zh-overlay.json" },
      shards: { e58f: { path: "shards/e58f.json" } },
    },
    "shards/e58f.json": {
      contract: "bw-jmdict-shard/1",
      key: "e58f",
      entries: [{
        id: "1",
        lemma: "取り寄せる",
        forms: ["取り寄せる"],
        readings: ["とりよせる"],
        pos: ["v1"],
        glosses: ["to order", "to have something sent"],
        common: true,
      }],
      exact: { "取り寄せる": [0] },
    },
    "zh-overlay.json": {
      contract: "bw-japanese-zh-overlay/1",
      entries: {
        "取り寄せる": {
          word: "取り寄せる",
          reading: "とりよせる",
          zh: "订购；调货；从外地寄来",
          pos: "一段动词",
          examples: [],
        },
      },
    },
  };
  const { dictionary, requests } = harness(resources);
  const result = await dictionary.lookupJapaneseLegacy("取り寄せ");
  assert.equal(result.ok, true);
  assert.equal(result.lemma, "取り寄せる");
  assert.equal(result.reading, "とりよせる");
  assert.equal(result.translation, "to order; to have something sent");
  assert.equal(result.inflect.base, "取り寄せる");
  assert.equal(result.source, "local-jmdict");
  assert.deepEqual(requests, [
    "manifest.json",
    "shards/e58f.json",
  ]);
});

test("legacy AI overlay cannot replace JMdict reading, part of speech, or meaning", async () => {
  const resources = {
    "manifest.json": {
      contract: "bw-jmdict-manifest/1",
      normalization: "NFC",
      shardAlgorithm: "utf8-prefix-2-kana-3/1",
      source: { release: "fixture" },
      posLabels: { n: "noun" },
      zhOverlay: { path: "zh-overlay.json", complete: false, authoritative: false },
      shards: { e689: { path: "shards/e689.json" } },
    },
    "shards/e689.json": {
      contract: "bw-jmdict-shard/1",
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
    "zh-overlay.json": {
      contract: "bw-japanese-zh-overlay/1",
      entries: { "手法": { reading: "てほう", pos: "wrong", zh: "wrong" } },
    },
  };
  const { dictionary, requests } = harness(resources);
  const result = await dictionary.lookupJapaneseLegacy("手法");
  assert.equal(result.reading, "しゅほう");
  assert.equal(result.pos, "noun");
  assert.equal(result.translation, "technique; method");
  assert.equal(result.local_zh, false);
  assert.deepEqual(requests, ["manifest.json", "shards/e689.json"]);
});

test("non-local PWA reports unavailable instead of silently contacting Pi", async () => {
  const { dictionary, requests } = harness({}, { local: false });
  const result = await dictionary.lookupJapanese("取り寄せ");
  assert.equal(result.ok, false);
  assert.equal(result.unavailable, true);
  assert.deepEqual(requests, []);
});

test("kana shards use the complete first kana while kanji keeps two bytes", () => {
  const { dictionary } = harness({});
  assert.equal(dictionary._shardKey("あう"), "e38182");
  assert.equal(dictionary._shardKey("取り寄せ"), "e58f");
});

test("App and extension load the offline module before lookup UI", () => {
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
  assert.match(project, /path: Extension\/Resources\/dictionary-data[\s\S]*?type: folder/);

  const pdfReader = read("_server_deploy/pdf_reader.py");
  const htmlReader = read("_server_deploy/html_reader.py");
  assert.equal((pdfReader.match(/"pdf\/rc-offline-dictionary\.js"/g) || []).length, 2);
  assert.equal((htmlReader.match(/"pdf\/rc-offline-dictionary\.js"/g) || []).length, 1);
});

test("Japanese UI keeps Pi AI as an explicit action", () => {
  const wordpop = read("_server_deploy/static/pdf/rc-wordpop.js");
  const phrasepop = read("_server_deploy/static/pdf/rc-phrasepop.js");
  assert.match(wordpop, /Pi AI 精释/);
  assert.doesNotMatch(wordpop, /暂无词典释义（可能是人名\/专有名词），已请 AI 讲解/);
  assert.match(phrasepop, /Pi AI 精释/);
});
