import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const APP = read("ios/BWReader/App/BWReaderNativeApp.swift");
const WEB = read("ios/BWReader/App/ReaderWebView.swift");
const SERVER = read("ios/BWReader/App/ReaderLocalRuntimeServer.swift");
const LIBRARY_VIEW = read("ios/BWReader/App/ReaderLocalLibraryView.swift");

function section(source, start, end) {
  const from = source.indexOf(start);
  const to = source.indexOf(end, from + start.length);
  assert.ok(from >= 0 && to > from, `missing source section: ${start}`);
  return source.slice(from, to);
}

test("startup restoration stores only stable library/book identity after didFinish", () => {
  const reference = section(
    WEB,
    "struct ReaderLastLocalBookReference",
    "struct ReaderLastLocalBookStore",
  );
  assert.match(reference, /let libraryID: String/);
  assert.match(reference, /let bookID: String/);
  assert.doesNotMatch(reference, /relativePath|fileURL|position|page|offset|title/);

  assert.match(WEB, /private var pendingLocalBookNavigation/);
  assert.match(WEB, /pending\.navigation === navigation/);
  assert.match(WEB, /isFinishedLocalBookURL\(webView\.url, bookID: pending\.bookID\)/);
  assert.equal(
    [...WEB.matchAll(/ReaderLastLocalBookStore\.shared\.save\(/g)].length,
    1,
    "the completed navigation delegate is the sole last-book writer",
  );
  const didFinish = section(WEB, "didFinish navigation:", "didFail navigation:");
  assert.match(didFinish, /ReaderLastLocalBookStore\.shared\.save\(/);
  const open = section(WEB, "private func openLocalBook(", "func setReaderScenePhase");
  assert.doesNotMatch(open, /ReaderLastLocalBookStore\.shared\.save\(/);
});

test("startup restores from the persisted index and visibly falls back on mismatch", () => {
  assert.match(APP, /@State private var showsLibrary = false/);
  assert.match(APP, /private func restoreInitialLocalBook\(\) async/);
  assert.match(APP, /reference\.libraryID == library\.stableLibraryID/);
  assert.match(APP, /library\.books\.first/);
  assert.match(APP, /await reader\.restoreLocalBook\(book, library: library\)/);
  assert.match(APP, /ReaderLastLocalBookStore\.shared\.clear\(\)/);
  assert.match(APP, /startupNotice: libraryStartupNotice/);
  assert.match(APP, /已移动、删除或尚未进入本机索引/);
  assert.match(WEB, /func finishInitialBookDecision\(\)/);
  assert.match(WEB, /guard !waitsForInitialBookDecision, webView\.url == nil/);
});

test("bundle integrity remains fail closed but full hashing is off the main actor", () => {
  assert.match(SERVER, /Task\.detached\(priority: \.utility\)/);
  assert.match(SERVER, /ReaderLocalBundleIntegrity\.verify/);
  assert.match(SERVER, /SHA256\.hash\(data: data\)/);
  assert.match(SERVER, /validatesDigests: !cacheHit/);
  assert.match(SERVER, /bundleIdentity/);
  assert.match(SERVER, /manifestDigest/);
  assert.match(SERVER, /try await bundleVerificationTask\.value/);
  assert.match(SERVER, /throw ReaderLocalRuntimeError\.bundleUnavailable/);

  const initializer = section(
    SERVER,
    "init(bundle: Bundle = .main) throws",
    "deinit {",
  );
  assert.doesNotMatch(initializer, /Data\(contentsOf: fileURL, options: \.mappedIfSafe\)/);
});

test("shells use normal navigation caching and bundle-revisioned immutable assets", () => {
  assert.doesNotMatch(WEB, /reloadIgnoringLocalCacheData|reloadFromOrigin/);
  assert.ok(
    [...WEB.matchAll(/cachePolicy: \.useProtocolCachePolicy/g)].length >= 2,
    "welcome and local-book shell navigation both use normal cache policy",
  );
  assert.match(SERVER, /staticRevision: String\(manifestDigest\.prefix\(24\)\)/);
  assert.match(SERVER, /"\/static\/\\\(staticRevision\)\/"/);
  assert.match(SERVER, /public, max-age=31536000, immutable/);
  assert.match(SERVER, /: "no-cache"/);
});

test("library shows its persisted index before TTL refresh and collapsed OCR is passive", () => {
  assert.match(
    LIBRARY_VIEW,
    /@State private var selectedSource = ReaderLibrarySource\.local/,
  );
  assert.match(LIBRARY_VIEW, /\.task \{ await refreshIfStale\(\) \}/);
  assert.match(
    LIBRARY_VIEW,
    /\.onChange\(of: selectedSource\)[\s\S]*source != \.local[\s\S]*refreshRemoteIfStale\(\)/,
  );
  assert.match(LIBRARY_VIEW, /let localTTL: TimeInterval = 15 \* 60/);
  assert.match(LIBRARY_VIEW, /let remoteTTL: TimeInterval = 5 \* 60/);
  assert.match(
    LIBRARY_VIEW,
    /if selectedSource != \.local \{ await refreshRemoteIfStale\(\) \}/,
  );
  assert.doesNotMatch(
    section(LIBRARY_VIEW, "case .success(let url):", "case .failure(let error):"),
    /refreshRemote/,
  );
  assert.match(LIBRARY_VIEW, /await refresh\(force: true\)/);

  const localRow = section(
    LIBRARY_VIEW,
    "private func localBookRow",
    "private func remoteBookRow",
  );
  const remoteRow = section(
    LIBRARY_VIEW,
    "private func remoteBookRow",
    "private func preprocessingPanel",
  );
  for (const row of [localRow, remoteRow]) {
    assert.match(row, /if expandedPreprocessingBookIDs\.contains/);
    assert.match(row, /preprocessingPanel[\s\S]*\.task\(id: piStatusTaskIdentity/);
    assert.doesNotMatch(
      row.slice(row.lastIndexOf(".padding(.vertical, 2)")),
      /\.task\(/,
      "a collapsed row must not own a Pi status task",
    );
  }
  assert.match(
    LIBRARY_VIEW,
    /private func refreshPiStatus[\s\S]*matchingLocalIdentity/,
  );
});
