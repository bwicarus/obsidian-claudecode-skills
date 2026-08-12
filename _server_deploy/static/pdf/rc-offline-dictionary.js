/* rc-offline-dictionary.js — App / extension bundled Japanese dictionary.
 *
 * The dictionary is intentionally independent from Pi.  App pages read the
 * immutable ReaderBundle files through the local runtime; the extension asks
 * its service worker for the same packaged JSON.  Pi AI is a separate,
 * explicitly-invoked refinement action owned by rc-wordpop/rc-phrasepop.
 */
(function () {
  if (!window.RC) window.RC = {};
  if (window.RC.offlineDictionary) return;

  var MANIFEST_CONTRACT = 'bw-jmdict-manifest/1';
  var SHARD_CONTRACT = 'bw-jmdict-shard/1';
  var BASE = '/static/pdf/dictionary-data/';
  var bridge = window.__bwOfflineJapaneseDictionaryBridge || null;
  var localMode = !!(bridge && typeof bridge.fetchJson === 'function') ||
    !!window.__BW_NATIVE_LOCAL_BASE_PATH__ ||
    window.__BW_NATIVE_OPENAI_REALTIME__ === true;
  var manifestPromise = null;
  var shardPromises = new Map();
  var encoder = new TextEncoder();

  function normalize(value) {
    var text = String(value == null ? '' : value).trim();
    try { return text.normalize('NFC'); } catch (_) { return text; }
  }

  function shardKey(term) {
    var bytes = encoder.encode(normalize(term));
    var kanaPrefix = bytes.length >= 2 && bytes[0] === 0xe3 &&
      (bytes[1] === 0x81 || bytes[1] === 0x82 || bytes[1] === 0x83);
    var length = Math.min(kanaPrefix ? 3 : 2, bytes.length);
    var key = '';
    for (var i = 0; i < length; i++) key += bytes[i].toString(16).padStart(2, '0');
    return key;
  }

  function safeRelativePath(value) {
    var path = String(value || '').replace(/^\/+/, '');
    if (!/^(?:manifest\.json|shards\/[a-f0-9]{1,6}\.json)$/.test(path)) {
      throw new Error('离线词典资源路径无效');
    }
    return path;
  }

  async function loadJson(relativePath) {
    var path = safeRelativePath(relativePath);
    if (bridge && typeof bridge.fetchJson === 'function') return bridge.fetchJson(path);
    var response = await fetch(BASE + path, { cache: 'force-cache', credentials: 'same-origin' });
    if (!response.ok) throw new Error('离线词典资源 HTTP ' + response.status);
    return response.json();
  }

  function loadManifest() {
    if (!manifestPromise) {
      manifestPromise = loadJson('manifest.json').then(function (manifest) {
        if (!manifest || manifest.contract !== MANIFEST_CONTRACT ||
            manifest.shardAlgorithm !== 'utf8-prefix-2-kana-3/1' || !manifest.shards) {
          throw new Error('离线词典清单版本不兼容');
        }
        return manifest;
      }).catch(function (error) {
        manifestPromise = null;
        throw error;
      });
    }
    return manifestPromise;
  }

  function loadShard(manifest, key) {
    if (!key || !manifest.shards[key]) return Promise.resolve(null);
    if (!shardPromises.has(key)) {
      var path = manifest.shards[key].path || ('shards/' + key + '.json');
      var promise = loadJson(path).then(function (shard) {
        if (!shard || shard.contract !== SHARD_CONTRACT || shard.key !== key ||
            !Array.isArray(shard.entries) || !shard.exact) {
          throw new Error('离线词典分片损坏: ' + key);
        }
        return shard;
      }).catch(function (error) {
        shardPromises.delete(key);
        throw error;
      });
      shardPromises.set(key, promise);
    }
    return shardPromises.get(key);
  }

  function exactEntries(shard, term) {
    if (!shard || !shard.exact) return [];
    var indexes = shard.exact[term];
    if (!Array.isArray(indexes)) return [];
    return indexes.map(function (index) { return shard.entries[index]; }).filter(Boolean);
  }

  function candidateForms(term) {
    var value = normalize(term);
    var result = [value];
    var seen = new Set(result);
    function add(candidate, mark) {
      candidate = normalize(candidate);
      if (!candidate || seen.has(candidate)) return;
      seen.add(candidate);
      result.push({ term: candidate, mark: mark || '词形还原' });
    }
    if (/せ$/.test(value)) add(value + 'る', '连用形→原形');
    if (/して(?:いる|いた|いて)?$/.test(value)) add(value.replace(/して(?:いる|いた|いて)?$/, 'する'), 'サ变→原形');
    if (/した$/.test(value)) { add(value.replace(/した$/, 'する'), 'サ变过去式→原形'); add(value.replace(/した$/, 'す'), '过去式→原形'); }
    if (/かった$/.test(value)) add(value.replace(/かった$/, 'い'), '形容词过去式→原形');
    if (/く(?:ない|て)$/.test(value)) add(value.replace(/く(?:ない|て)$/, 'い'), '形容词活用→原形');
    if (/った$/.test(value)) ['う', 'つ', 'る'].forEach(function (end) { add(value.replace(/った$/, end), '过去式→原形'); });
    if (/んだ$/.test(value)) ['む', 'ぶ', 'ぬ'].forEach(function (end) { add(value.replace(/んだ$/, end), '过去式→原形'); });
    if (/いた$/.test(value)) add(value.replace(/いた$/, 'く'), '过去式→原形');
    if (/いだ$/.test(value)) add(value.replace(/いだ$/, 'ぐ'), '过去式→原形');
    if (/[てた]$/.test(value)) add(value.slice(0, -1) + 'る', '活用→原形');
    if (/ない$/.test(value)) {
      var stem = value.slice(0, -2);
      var table = { 'わ':'う', 'か':'く', 'が':'ぐ', 'さ':'す', 'た':'つ', 'な':'ぬ', 'ば':'ぶ', 'ま':'む', 'ら':'る' };
      var last = stem.slice(-1);
      if (table[last]) add(stem.slice(0, -1) + table[last], '否定形→原形');
      add(stem + 'る', '否定形→原形');
    }
    if (/ます$/.test(value)) {
      var masuStem = value.slice(0, -2);
      var masu = { 'い':'う', 'き':'く', 'ぎ':'ぐ', 'し':'す', 'ち':'つ', 'に':'ぬ', 'び':'ぶ', 'み':'む', 'り':'る' };
      var masuLast = masuStem.slice(-1);
      if (masu[masuLast]) add(masuStem.slice(0, -1) + masu[masuLast], 'ます形→原形');
      add(masuStem + 'る', 'ます形→原形');
    }
    return result.slice(0, 18).map(function (item) {
      return typeof item === 'string' ? { term: item, mark: '' } : item;
    });
  }

  async function lookupJapanese(term) {
    var query = normalize(term);
    if (!query) return { ok: false, code: 'BW_OFFLINE_DICTIONARY_EMPTY', source: 'local-jmdict' };
    if (!localMode) return { ok: false, unavailable: true, code: 'BW_OFFLINE_DICTIONARY_NOT_LOCAL' };
    var manifest = await loadManifest();
    var forms = candidateForms(query);
    var found = [];
    var matched = '';
    var mark = '';
    for (var i = 0; i < forms.length; i++) {
      var form = forms[i];
      var shard = await loadShard(manifest, shardKey(form.term));
      found = exactEntries(shard, form.term);
      if (found.length) { matched = form.term; mark = form.mark; break; }
    }
    if (!found.length) {
      return { ok: false, source: 'local-jmdict', query: query, code: 'BW_OFFLINE_DICTIONARY_NO_MATCH' };
    }
    var entry = found[0];
    return {
      ok: true,
      source: 'local-jmdict',
      query: query,
      matchedTerm: matched,
      inflectionMark: mark,
      entry: entry,
      candidates: found,
      // The packaged overlay is a retained migration artifact from a small
      // context-sensitive AI cache.  Its own manifest marks it incomplete and
      // non-authoritative, so it must never override JMdict pronunciation,
      // part of speech, or meaning in the automatic dictionary result.
      zhRecord: null,
      posLabels: manifest.posLabels || {},
      sourceVersion: manifest.source &&
        (manifest.source.release || manifest.source.dictionaryVersion || manifest.source.version) || ''
    };
  }

  function asLegacy(result, original) {
    if (!result || !result.ok) return result || { ok: false, source: 'local-jmdict' };
    var entry = result.entry || {};
    var glosses = Array.isArray(entry.glosses) ? entry.glosses.filter(Boolean) : [];
    var forms = Array.isArray(entry.forms) ? entry.forms.filter(Boolean) : [];
    var readings = Array.isArray(entry.readings) ? entry.readings.filter(Boolean) : [];
    var labels = result.posLabels || {};
    var pos = Array.isArray(entry.pos)
      ? entry.pos.map(function (code) { return labels[code] || code; }).join(' / ')
      : String(entry.pos || '');
    var lemma = entry.lemma || result.matchedTerm || original;
    return {
      ok: true,
      jp: true,
      word: original,
      lemma: lemma,
      forms: forms,
      reading: readings[0] || '',
      pos: pos,
      zh: '',
      translation: glosses.slice(0, 3).join('; '),
      definition: glosses.slice(0, 6).join('; '),
      examples: [],
      inflect: result.inflectionMark ? { base: lemma, marks: [result.inflectionMark] } : null,
      local_candidates: result.candidates || [],
      source: 'local-jmdict',
      source_version: result.sourceVersion || '',
      local_zh: false
    };
  }

  window.RC.offlineDictionary = Object.freeze({
    CONTRACT: 'bw-offline-dictionary/1',
    isLocalMode: function () { return localMode; },
    lookupJapanese: lookupJapanese,
    lookupJapaneseLegacy: function (term) {
      return lookupJapanese(term).then(function (result) { return asLegacy(result, term); });
    },
    _candidateForms: candidateForms,
    _shardKey: shardKey
  });
})();
