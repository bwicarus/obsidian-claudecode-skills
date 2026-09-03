import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";


const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const SWIFT = fs.readFileSync(
  path.join(ROOT, "ios/BWReader/App/ReaderBookUserStatePackage.swift"),
  "utf8"
);
const PYTHON = fs.readFileSync(
  path.join(ROOT, "_server_deploy/reader_book_user_state.py"),
  "utf8"
);
const WEB_ADAPTER = fs.readFileSync(
  path.join(ROOT, "ios/BWReader/App/ReaderBookUserStateWebAdapter.swift"),
  "utf8"
);


test("server and App agree on the complete book user-state domain set", () => {
  const names = [
    "reading-position",
    "highlights",
    "ink",
    "closed-regions",
    "notes",
    "user-pages",
    "card-placements",
    "entity-references",
  ];
  assert.match(PYTHON, /CONTRACT = "reader-book-user-state\/1"/);
  assert.match(SWIFT, /currentContract = "reader-book-user-state\/1"/);
  for (const name of names) {
    assert.ok(PYTHON.includes(`"${name}"`), `server missing ${name}`);
    assert.ok(SWIFT.includes(`"${name}"`) || SWIFT.includes(`case ${name}`),
      `App missing ${name}`);
  }
  assert.match(PYTHON, /DOMAIN_NAMES: tuple\[str, \.\.\.\]/);
  assert.match(SWIFT,
    /package\.domains\.map\(\\\.name\) == ReaderBookUserStateDomainName\.allCases/);
});

test("the package carries canonical payload bytes, never account credentials", () => {
  assert.match(PYTHON, /"payloadJson": payload_bytes\.decode\("utf-8"\)/);
  assert.match(PYTHON, /hashlib\.sha256\(payload_bytes\)\.hexdigest\(\)/);
  assert.match(SWIFT, /sha256\(payload\) == domain\.digest/);
  assert.match(SWIFT, /JSONSerialization\.jsonObject/);
  assert.match(PYTHON, /_SENSITIVE_KEYS/);
  assert.match(SWIFT, /sensitiveKeys/);
  assert.doesNotMatch(PYTHON, /"owner"\s*:/);
  assert.doesNotMatch(PYTHON, /"storageNamespace"\s*:/);
  assert.doesNotMatch(SWIFT, /let ownerToken:/);
  assert.doesNotMatch(SWIFT, /let absolutePath:/);
});

test("all Pi-newer domains are one atomic renderer transaction", () => {
  assert.match(SWIFT,
    /protocol ReaderBookUserStateAtomicApplying[\s\S]*func applyAtomically\(/);
  assert.match(SWIFT,
    /let importedDomains = prepared\.package\.domains\.filter/);
  assert.match(SWIFT,
    /applier\.applyAtomically\(transaction\)/);
  assert.match(SWIFT,
    /receipt\.domainDigests == expected/);
  assert.match(SWIFT,
    /let expectedLocalHeaders: \[String: ReaderBookUserStateDomainHeader\]/);
  assert.match(SWIFT,
    /Set\(localHeaders\.keys\)[\s\S]*== Set\(ReaderBookUserStateDomainName\.allCases\)/);
  assert.match(WEB_ADAPTER,
    /Set\(transaction\.expectedLocalHeaders\.keys\)[\s\S]*Set\(transaction\.domains/);
  assert.match(SWIFT,
    /A partial commit must be reported as an[\s\S]*error, never as a receipt/);
});

test("both-changed state is retained locally and not imported", () => {
  assert.match(PYTHON,
    /"conflict", "keep", "local and Pi both changed since baseline"/);
  assert.match(SWIFT,
    /classification = \.conflict[\s\S]*action = \.keep[\s\S]*本机与服务器都在基线后发生变化/);
  assert.match(SWIFT,
    /decision\.action == \.import \? decision\.name : nil/);
  assert.match(SWIFT,
    /Local-newer\/conflict state keeps its old[\s\S]*baseline/);
  assert.match(PYTHON,
    /pi_header\.revision < baseline\.remote_revision[\s\S]*"conflict", "keep"/);
  assert.match(SWIFT,
    /domain\.revision < based\.piRevision[\s\S]*classification = \.conflict/);
});

test("the service requires verified account identity and uses private metadata", () => {
  assert.match(PYTHON,
    /def _validate_identity\(identity: ReaderStorageIdentity\)/);
  assert.match(PYTHON, /verified ReaderStorageIdentity required/);
  assert.match(PYTHON,
    /self\.sidecar_store\.account_path\([\s\S]*"reader-book-user-state"/);
  assert.match(PYTHON,
    /with self\.sidecar_store\.lock\(identity, "reader-book-user-state", lock_key\)/);
});

test("the App reconciliation baseline is account-scoped without exposing account data", () => {
  assert.match(SWIFT, /let accountScopeDigest: String/);
  assert.ok(SWIFT.includes(
    '"\\(accountScopeDigest)\\u{0}\\(localBookId)\\u{0}\\(remoteBookId)".utf8'
  ));
  assert.match(SWIFT,
    /accountScopeDigest\.range\([\s\S]*\^\[a-f0-9\]\{64\}\$/);
  assert.doesNotMatch(WEB_ADAPTER, /accountScopeDigest/);
});

test("the Web adapter is main-frame, capability-origin and context bound", () => {
  assert.match(WEB_ADAPTER,
    /final class ReaderBookUserStateWebAdapter: ReaderBookUserStateAtomicApplying/);
  assert.match(WEB_ADAPTER, /window\.top !== window/);
  assert.match(WEB_ADAPTER,
    /location\.protocol\.toLowerCase\(\) === expectedScheme/);
  assert.match(WEB_ADAPTER,
    /location\.pathname\.startsWith\(expectedBasePath\)/);
  assert.match(WEB_ADAPTER,
    /webView\.url == initialURL/);
  assert.match(WEB_ADAPTER,
    /generation == contextGeneration/);
  assert.match(WEB_ADAPTER,
    /contentWorld: \.page/);
  assert.match(WEB_ADAPTER,
    /window\.BWReaderRuntime\?\.nativeLocalRuntime\?\.bookUserState/);
});

test("the Web adapter accepts only strict snapshot and commit receipts", () => {
  assert.match(WEB_ADAPTER,
    /Set\(response\.keys\) == Set\(\[[\s\S]*"headers"/);
  assert.match(WEB_ADAPTER,
    /values\.count == ReaderBookUserStateDomainName\.allCases\.count/);
  assert.match(WEB_ADAPTER,
    /Set\(result\.keys\) == Set\(ReaderBookUserStateDomainName\.allCases\)/);
  assert.match(WEB_ADAPTER,
    /receipt\["committed"\] as\? Bool == true/);
  assert.match(WEB_ADAPTER, /digests == expected/);
  assert.match(WEB_ADAPTER,
    /ReaderBookUserStatePackageCodec[\s\S]*\.validateDomainPayload\(domain\)/);
  assert.match(SWIFT, /sha256\(payload\) == domain\.digest/);
});
