import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const SOURCE = readFileSync(
  new URL("../../extensions/bw-reader-webext/background.js", import.meta.url),
  "utf8",
);
const ORIGIN = "https://bwicarus.taile44d0c.ts.net";

function loadRequestGate() {
  const start = SOURCE.indexOf("const BW_FETCH_ROUTE_METHODS =");
  const end = SOURCE.indexOf("const MAX_BW_FETCH_BINARY_BODY_BYTES", start);
  assert.notEqual(start, -1, "missing fixed route allowlist");
  assert.notEqual(end, -1, "missing request gate boundary");

  const sandbox = { URL };
  vm.runInNewContext(
    [
      `const ORIGIN = ${JSON.stringify(ORIGIN)};`,
      SOURCE.slice(start, end),
      "globalThis.checkedBwFetchRequestForTest = checkedBwFetchRequest;",
    ].join("\n"),
    sandbox,
  );
  return sandbox.checkedBwFetchRequestForTest;
}

const checkedRequest = loadRequestGate();

test("card improvement draft and commit allow only exact POST operations", () => {
  for (const path of [
    "/api/assistant/card-improvement-draft",
    "/api/assistant/card-improvement-commit",
  ]) {
    const checked = checkedRequest(`${ORIGIN}${path}?request_id=contract`, {
      method: "post",
    });
    assert.equal(checked.url.pathname, path);
    assert.equal(checked.method, "POST");

    for (const method of ["GET", "PUT", "PATCH", "DELETE", "OPTIONS"]) {
      assert.throws(
        () => checkedRequest(`${ORIGIN}${path}`, { method }),
        (error) =>
          error?.code === "BW_FETCH_OPERATION" &&
          error?.details?.path === path &&
          error?.details?.method === method,
        `${path} must reject ${method}`,
      );
    }
  }
});

test("card improvement proxy rejects neighboring paths and foreign origins", () => {
  for (const path of [
    "/api/assistant/card-improvement",
    "/api/assistant/card-improvement-draft/",
    "/api/assistant/card-improvement-draft-extra",
    "/api/assistant/card-improvement-commit/",
    "/api/assistant/card-improvement-commit/anything",
  ]) {
    assert.throws(
      () => checkedRequest(`${ORIGIN}${path}`, { method: "POST" }),
      (error) => error?.code === "BW_FETCH_OPERATION",
      `${path} must remain outside the allowlist`,
    );
  }

  for (const url of [
    "https://example.test/api/assistant/card-improvement-draft",
    "https://bwicarus.taile44d0c.ts.net.evil.test/api/assistant/card-improvement-commit",
    "https://user:password@bwicarus.taile44d0c.ts.net/api/assistant/card-improvement-draft",
  ]) {
    assert.throws(
      () => checkedRequest(url, { method: "POST" }),
      (error) => error?.code === "BW_FETCH_ORIGIN",
      `${url} must fail the origin fence`,
    );
  }
});

test("bw-fetch still requires a trusted top-level sender and captured account", () => {
  assert.match(
    SOURCE,
    /if\s*\(!isTopLevelOwnContentSender\(port\.sender\)\)\s*\{/,
  );
  assert.match(
    SOURCE,
    /captured\s*=\s*await capturePersistentAccountForContentSender\(port\.sender\)/,
  );
  assert.match(SOURCE, /fenceCapturedAccount\(captured\)/);
  assert.match(SOURCE, /headers\.set\("Authorization",\s*`Bearer \$\{token\}`\)/);
});
