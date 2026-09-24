const fs = require('node:fs');
const D = require('../../../../_server_deploy/static/reader-runtime/data-store.js');
const registry = require('../../../../_server_deploy/static/reader-runtime/data-registry.js');
(async () => {
  const entries = registry.settingMigrations();
  const store = D.createDataStore({ deviceId: 'prefs-test', clock: () => 1234,
    causalCollections: registry.syncCollections() });
  const cases = [];
  for (const entry of entries) {
    const id = 'setting:' + entry.semanticKey;
    for (const raw of ['1', '日本語\n"escaped"\u0000🧪', null, '0']) {
      const mutation = 'preference-' + cases.length;
      const value = { id, legacyKey: entry.legacyKey, semanticKey: entry.semanticKey,
        codec: entry.codec, rawValue: raw, migration: 'preference-store-v1' };
      const record = raw === null
        ? await store.remove(entry.collection, id, { mutationId: mutation })
        : await store.put(entry.collection, value, { id, mutationId: mutation });
      cases.push({ key: entry.legacyKey, raw, mutation, record });
    }
  }
  fs.writeFileSync(process.argv[2], JSON.stringify({ catalog: {contract: 'reader-native-preferences/1', entries}, cases }));
})().catch(error => { console.error(error); process.exitCode = 1; });
