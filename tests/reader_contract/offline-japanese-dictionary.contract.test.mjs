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

test("offline rich dictionary restores Japanese stem and exposes pronunciation, examples and kanji", async () => {
  const resources = {
    "manifest.json": {
      contract: "bw-jmdict-manifest/3",
      normalization: "NFC",
      shardAlgorithm: "utf8-prefix-2-kana-3/1",
      source: { release: "fixture" },
      posLabels: { v1: "Ichidan verb" },
      shards: { e58f: { path: "shards/e58f.json" } },
    },
    "shards/e58f.json": {
      contract: "bw-jmdict-shard/3",
      key: "e58f",
      entries: [{
        id: "1",
        lemma: "取り寄せる",
        // The exact index legitimately contains surface forms.  Matching the
        // selected surface first must not erase the lemma/current-form row.
        forms: ["取り寄せ", "取り寄せる"],
        readings: ["とりよせる"],
        readingKata: "トリヨセル",
        accent: 0,
        pos: ["v1"],
        glosses: ["to order", "to have something sent"],
        zhGlosses: [
          "订购；调货；从外地寄来",
          "取り寄せる toriyoseru alt-of",
          "取り寄せるcontinuative取り寄せるstem",
          "to order",
        ],
        zhSenses: [
          { glosses: ["订购", "调货"] },
          { pos: "non-lemma", glosses: ["取り寄せる alternative form"] },
        ],
        examples: [
          { ja: "商品を取り寄せた。", en: "I ordered the product.", source: "tanaka" },
          { ja: "商品を取り寄せる。", zh: "订购商品。", source: "fixture" },
        ],
        etymology: ["取る＋寄せる"],
        synonyms: ["注文する"],
        sourceUrls: ["https://example.invalid/取り寄せる"],
        common: true,
      }],
      exact: { "取り寄せ": [0], "取り寄せる": [0] },
    },
    "kanji.json": {
      "取": { kanji: "取", on: ["シュ"], kun: ["と.る"], meanings: ["take"], meanings_zh: "取；拿" },
      "寄": { kanji: "寄", on: ["キ"], kun: ["よ.る"], meanings: ["approach"], meanings_zh: "靠近" },
    },
  };
  const { dictionary, requests } = harness(resources);
  const result = await dictionary.lookupJapaneseLegacy("取り寄せ");
  assert.equal(result.ok, true);
  assert.equal(result.lemma, "取り寄せる");
  assert.equal(result.reading, "とりよせる");
  assert.equal(result.reading_kata, "トリヨセル");
  assert.equal(result.accent, 0);
  assert.equal(result.mora, 5);
  assert.equal(result.romaji, "toriyoseru");
  assert.equal(result.zh, "订购；调货；从外地寄来");
  assert.equal(result.translation, "订购；调货；从外地寄来");
  assert.equal(result.local_zh, true);
  assert.equal(result.meaning_language, "zh");
  assert.equal(result.english_fallback, false);
  assert.equal(result.inflect.base, "取り寄せる");
  assert.equal(result.inflect.surface, "取り寄せ");
  assert.deepEqual(JSON.parse(JSON.stringify(result.inflect.marks)), ["活用→原形"]);
  assert.equal(result.source, "local-jmdict");
  assert.deepEqual(result.examples, [
    { ja: "商品を取り寄せた。", en: "I ordered the product.", source: "tanaka" },
    { ja: "商品を取り寄せる。", zh: "订购商品。", source: "fixture" },
  ]);
  assert.deepEqual(JSON.parse(JSON.stringify(result.zh_senses)), [{ glosses: ["订购", "调货"] }]);
  assert.deepEqual(result.etymology, ["取る＋寄せる"]);
  assert.deepEqual(result.synonyms, ["注文する"]);
  assert.deepEqual(result.source_urls, ["https://example.invalid/取り寄せる"]);
  assert.deepEqual(JSON.parse(JSON.stringify(result.kanji)), [
    { kanji: "取", on: ["シュ"], kun: ["と.る"], meanings: ["take"], meanings_zh: "取；拿" },
    { kanji: "寄", on: ["キ"], kun: ["よ.る"], meanings: ["approach"], meanings_zh: "靠近" },
  ]);
  assert.deepEqual(requests, [
    "manifest.json",
    "shards/e58f.json",
    "kanji.json",
  ]);
});

test("English remains an explicit fallback when Chinese data has no safe match", async () => {
  const resources = {
    "manifest.json": {
      contract: "bw-jmdict-manifest/3",
      normalization: "NFC",
      shardAlgorithm: "utf8-prefix-2-kana-3/1",
      source: { release: "fixture" },
      posLabels: { n: "noun" },
      shards: { e689: { path: "shards/e689.json" } },
    },
    "shards/e689.json": {
      contract: "bw-jmdict-shard/3",
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
    "kanji.json": {
      "手": { kanji: "手", on: ["シュ"], kun: ["て"], meanings: ["hand"] },
      "法": { kanji: "法", on: ["ホウ"], kun: [], meanings: ["method"] },
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
  assert.deepEqual(requests, ["manifest.json", "shards/e689.json", "kanji.json"]);
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

test("mora counting keeps sokuon and long vowels as full morae", () => {
  const { dictionary } = harness({});
  assert.equal(dictionary._moraCount("がっこう"), 4);
  assert.equal(dictionary._moraCount("コーヒー"), 4);
  assert.equal(dictionary._moraCount("きょう"), 2);
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

test("词典留在设备上：可与同包 Safari 扩展共享，但不进 iCloud / 服务器 / 设置同步", () => {
  const core = read("ios/BWReader/Shared/ReaderOfflineDictionaryCore.swift");
  const installer = read("ios/BWReader/App/ReaderOfflineDictionary.swift");
  const localServer = read("ios/BWReader/App/ReaderLocalRuntimeServer.swift");
  const settings = read("ios/BWReader/App/NativeReaderToolsView.swift");
  const webSettings = read("_server_deploy/static/pdf/rc-settings.js");

  // ── 边界改了，但只改了一条 ──────────────────────────────────────
  //
  // 2026-08-19 用户拍板（C 组 #19）：允许词典放进 App Group 共享容器，让同一个包
  // 里的 Safari 扩展能离线查词。那句承诺的本意是"不外流"，而 App 组只在
  // App + 它自己的扩展 + Widget 之间共享 —— 不进 iCloud、不到 Pi、不随设置同步。
  //
  // 所以这条守卫**不再**禁止 App Group，但下面每一条仍然要守住：真正会让数据
  // 离开这台设备的路径，一条都不许开。
  assert.match(
    core,
    /containerURL\(\s*forSecurityApplicationGroupIdentifier:\s*\n?\s*ReaderNativeBridgeContract\.appGroupIdentifier/,
    "词典应当放在 App Group 共享容器（扩展要读得到）",
  );
  assert.match(core, /OfflineJapaneseDictionary/);
  // 仍然排除 iCloud 备份 —— 几百 MB 的词典不该占用户的 iCloud 空间。
  // （这条在 Core：它属于"准备目录"，跟存储路径一起走，不属于下载。）
  assert.match(core, /isExcludedFromBackup = true/);
  // 下载源不变（sourceBaseURL 也是只读常量，在 Core）
  assert.match(core, /raw\.githubusercontent\.com/);
  // Installer 确实还在 App 那边
  assert.match(installer, /private enum ReaderOfflineDictionaryInstaller/);
  // App 内运行时仍从本地读（不因为共享而改走网络）
  assert.match(localServer, /native-api\/offline-dictionary\//);

  // ── 承诺的文案必须跟着实现走 ────────────────────────────────────
  //
  // 两处都要改：原生表里那份，和 2026-08-19 搬进设置面板「本机」tab 的那份。
  // 实现变了而话没变 = 对用户撒谎；话变了而实现没变 = 白白削弱承诺。
  assert.match(settings, /下载离线日语词典/);
  for (const source of [settings, webSettings]) {
    assert.match(source, /不进入书籍附件、服务器或设置同步/);
    assert.match(source, /Safari 扩展共享/);
    // 旧话不能残留 —— 它现在是错的
    assert.doesNotMatch(source, /不进入书籍附件、Pi、Safari 扩展或设置同步/);
  }
});

test("Japanese UI queries the App dictionary first and restores the old explicit expand-to-AI fallback", () => {
  const wordpop = read("_server_deploy/static/pdf/rc-wordpop.js");
  const phrasepop = read("_server_deploy/static/pdf/rc-phrasepop.js");
  const computerVoice = read("_server_deploy/static/pdf/rc-computer-voice.js");
  const nativeRuntime = read("_server_deploy/static/pdf/native-local-runtime.js");
  const protocol = read(
    "extensions/bw-reader-webext/windows/ComputerVoiceAudio/DirectBridgeProtocol.cs",
  );
  assert.match(wordpop, /暂无词典释义\(可能是复合词\/专有名词\)。点此展开,让 AI 结合上下文讲解/);
  assert.match(wordpop, /try \{ jpAiDeep\(word\); \} catch \(_\) \{\}/);
  assert.doesNotMatch(wordpop, /改用 Pi 深度解释（可选）|手动使用 Pi 深度解释/);
  assert.match(wordpop, /_lookupJapaneseLocalFirst/);
  assert.match(phrasepop, /_lookupPhraseLocalFirst/);
  assert.doesNotMatch(wordpop, /lookupJapaneseFallback\s*\(/);
  // 词组:本地词典缺中文时改向已连接的 ReaderPC 要句境释义(2026-08-18 用户拍板:
  // 「字典里没有的就直接闪烁等待就好了啊」)。英文 gloss 一律不得进视觉层 ——
  // 它此前被当成中文显示,还挂着「App 本地中文词典」的落款。
  assert.match(phrasepop, /lookupJapaneseFallback\(\{/);
  assert.match(phrasepop, /_readerPCLinked\(\)/);
  assert.match(phrasepop, /function _localChinese/);
  assert.doesNotMatch(
    phrasepop,
    /zh:\s*localResult\.zh\s*\|\|\s*localResult\.definition/,
    "英文兜底字段不得再被当作中文释义",
  );
  assert.doesNotMatch(phrasepop, /fetch\('\/pdf\/api\/dict-jp\?word=/);
  // 词典未命中 → 自动 AI 查询（呼吸高亮期间完成），不再停在红字未命中让用户再点一次。
  assert.match(phrasepop, /'\/pdf\/api\/translate-sentence'/);
  assert.match(phrasepop, /@interaction ai\.translate\.compute/);
  assert.match(wordpop, /return local\.lookupJapaneseLegacy\(word\)/);
  assert.match(phrasepop, /local\.lookupJapaneseLegacy\(text\)/);
  assert.match(computerVoice, /lookupJapaneseFallback/);
  assert.match(computerVoice, /"dictionary-lookup"/);
  assert.match(nativeRuntime, /dictionaryFallbackCache/);
  assert.match(protocol, /HandleDictionaryLookupAsync/);
  assert.match(wordpop, /暂无词典释义（可能是人名\/专有名词），已请 AI 讲解/);
  assert.match(phrasepop, /点这里展开完整字典/);   // 2026-09-03 词组框与单词框统一版式:展开完整字典 = 原「改用旧版精释」
});
