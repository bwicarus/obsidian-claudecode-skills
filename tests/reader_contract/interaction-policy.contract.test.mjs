import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";

const require = createRequire(import.meta.url);
const InteractionPolicy = require(
  "../../_server_deploy/static/reader-runtime/interaction-policy.js",
);

const OUTBOX_ACTION_IDS = [
  "vocabulary.mastery.set",
  "vocabulary.jp-mastery.set",
  "phrase.mastery.set",
  "phrase.favorite.add",
  "phrase.favorite.remove",
  "document.highlight.create",
  "document.highlight.update",
  "document.highlight.remove",
  "document.note.create",
  "document.note.update",
  "document.note.remove",
  "review.answer.submit",
  "reading.position.save",
  "learning.lookup.report",
  "anki.cards.enqueue",
  "entity.state.update",
];
const SERVICE_WORKER_STRATEGIES = new Set([
  "none",
  "private-cache-first",
  "private-swr",
  "private-network-fallback",
  "public-cache-first",
]);

function samplePath(match) {
  return match.path.replace(
    /\{([A-Za-z][A-Za-z0-9_]*)\}/g,
    (_placeholder, name) => (name === "id" ? "card_abc" : `sample_${name}`),
  );
}

test("interaction policy schema is valid and action ids/routes are unique", () => {
  assert.equal(InteractionPolicy.CONTRACT, "interaction-policy/1");
  assert.deepEqual(InteractionPolicy.validate(), {
    contract: "interaction-policy/1",
    ok: true,
    errors: [],
  });

  const policies = InteractionPolicy.policies();
  assert.equal(new Set(policies.map((item) => item.id)).size, policies.length);
  for (const policy of policies) {
    assert.ok(policy.matches.length > 0);
    assert.ok(policy.surfaces.length > 0);
    assert.deepEqual(
      Object.keys(policy.transport).sort(),
      ["extensionBridge", "outbox", "serviceWorker"],
    );
    assert.equal(typeof policy.transport.outbox, "boolean");
    assert.equal(typeof policy.transport.extensionBridge, "boolean");
    assert.ok(SERVICE_WORKER_STRATEGIES.has(policy.transport.serviceWorker));
    assert.equal("allow" in policy, false);
    if (policy.ui === "remote-required") {
      assert.ok(policy.reason.trim().length > 0);
      assert.equal(policy.transport.outbox, false);
    }
    if (policy.ui === "local-immediate") {
      assert.ok(policy.localEffectMs <= 50);
      assert.equal(policy.ack, "reconcile");
    }
  }
});

test("all current command-outbox routes resolve through the one policy registry", () => {
  const outboxPolicies = InteractionPolicy.policies().filter(
    (policy) => policy.transport.outbox,
  );
  assert.deepEqual(
    outboxPolicies.map((policy) => policy.id).sort(),
    [...OUTBOX_ACTION_IDS].sort(),
  );
  for (const expected of outboxPolicies) {
    for (const route of expected.matches) {
      for (const method of route.methods) {
        const path = samplePath(route);
        const actual = InteractionPolicy.match(path, method);
        assert.equal(actual?.id, expected.id, `${method} ${path}`);
        assert.equal(actual?.sync, "outbox");
        assert.equal(actual?.transport?.outbox, true);
      }
    }
  }
  assert.equal(
    InteractionPolicy.match("/pdf/api/translate", "POST")?.transport?.outbox,
    false,
  );
  assert.equal(
    InteractionPolicy.match("https://evil.invalid/pdf/api/vocab-mark", "POST")?.id,
    "vocabulary.mastery.set",
    "registry classifies paths; same-origin enforcement remains the transport's job",
  );
});

test("dynamic entity ids retain their character and length boundary", () => {
  const valid160 = "a".repeat(160);
  assert.equal(
    InteractionPolicy.match(`/pdf/api/entity/${valid160}`, "PATCH")?.id,
    "entity.state.update",
  );
  for (const invalid of [
    "",
    "a".repeat(161),
    "card.with-dot",
    "card%2Fchild",
    "card child",
  ]) {
    assert.equal(
      InteractionPolicy.match(`/pdf/api/entity/${invalid}`, "PATCH"),
      null,
      invalid,
    );
  }
});

test("local-first, cached, network-first, and remote-required policies stay distinct", () => {
  assert.equal(
    InteractionPolicy.match("/pdf/api/vocab-mark", "POST")?.ui,
    "local-immediate",
  );
  assert.equal(
    InteractionPolicy.match("/pdf/api/dict-quick?word=be", "GET")?.ui,
    "cache-first",
  );
  assert.equal(
    InteractionPolicy.match("/pdf/api/vocab-mastery-map?all=1", "GET")?.ui,
    "cache-first",
  );
  assert.equal(
    InteractionPolicy.match("/pdf/api/highlights?file=book.pdf", "GET")?.local?.cache,
    "local-snapshot-with-dirty-overlay",
  );
  assert.equal(
    InteractionPolicy.match("/api/assistant/chat", "POST")?.ui,
    "remote-required",
  );
  const computerVoice = InteractionPolicy.match(
    "/api/reader/computer-voice/devices/device-1/start",
    "POST",
  );
  assert.equal(computerVoice?.id, "computer-voice.bridge.request");
  assert.equal(computerVoice?.ui, "remote-required");
  assert.equal(computerVoice?.offline, "unavailable");
  assert.equal(computerVoice?.sync, "direct");
  assert.equal(computerVoice?.transport?.outbox, false);
  assert.equal(
    InteractionPolicy.match("/static/pdf/reader.css?v=1", "GET")?.id,
    "reader.shell.read",
  );
  assert.equal(InteractionPolicy.match("/not-registered", "GET"), null);
});

test("UI policy and concrete transports remain orthogonal and fail closed", () => {
  const dictionary = InteractionPolicy.policy("dictionary.quick.read");
  assert.equal(dictionary.ui, "cache-first");
  assert.deepEqual(dictionary.transport, {
    outbox: false,
    extensionBridge: true,
    serviceWorker: "private-swr",
  });

  const pageImage = InteractionPolicy.policy("document.page-image.read");
  assert.equal(pageImage.ui, "cache-first");
  assert.deepEqual(pageImage.transport, {
    outbox: false,
    extensionBridge: false,
    serviceWorker: "private-cache-first",
  });

  const overlay = InteractionPolicy.policy("document.page-overlay.read");
  assert.equal(overlay.ui, "network-first");
  assert.equal(overlay.transport.serviceWorker, "private-network-fallback");

  const local = InteractionPolicy.policy("vocabulary.mastery.set");
  assert.equal(local.ui, "local-immediate");
  assert.equal(local.transport.serviceWorker, "none");
});

test("registry accessors return copies, so consumers cannot rewrite global policy", () => {
  const first = InteractionPolicy.policy("vocabulary.mastery.set");
  first.ui = "remote-required";
  first.matches[0].methods.push("DELETE");
  const second = InteractionPolicy.policy("vocabulary.mastery.set");
  assert.equal(second.ui, "local-immediate");
  assert.deepEqual(second.matches[0].methods, ["POST"]);
});

test("validator rejects remote-required without a reason and duplicate routes", () => {
  const policies = InteractionPolicy.policies();
  const remote = structuredClone(
    policies.find((item) => item.id === "ai.explain.compute"),
  );
  remote.reason = "";
  const local = structuredClone(
    policies.find((item) => item.id === "vocabulary.mastery.set"),
  );
  local.id = "vocabulary.mastery.duplicate";
  const checked = InteractionPolicy.validate([remote, local, local]);
  assert.equal(checked.ok, false);
  assert.ok(checked.errors.some((message) => message.includes(".reason")));
  assert.ok(checked.errors.some((message) => message.includes("duplicates")));
});

test("validator rejects missing/invalid transport and unconstrained param metadata", () => {
  const missingTransport = InteractionPolicy.policy("ai.explain.compute");
  delete missingTransport.transport;
  const invalidServiceWorker = InteractionPolicy.policy("dictionary.quick.read");
  invalidServiceWorker.transport.serviceWorker = "cache-sometimes";
  const invalidParam = InteractionPolicy.policy("entity.state.update");
  invalidParam.matches[0].params.extra = { pattern: ".+" };
  const checked = InteractionPolicy.validate([
    missingTransport,
    invalidServiceWorker,
    invalidParam,
  ]);
  assert.equal(checked.ok, false);
  assert.ok(checked.errors.some((message) => message.includes(".transport")));
  assert.ok(
    checked.errors.some((message) => message.includes("serviceWorker")),
  );
  assert.ok(
    checked.errors.some((message) => message.includes("no path placeholder")),
  );
});

test("all book PWA templates load policy before outbox and business scripts", () => {
  for (const name of [
    "pdf_reader.html",
    "epub_html_reader.html",
    "html_reader.html",
  ]) {
    const source = readFileSync(
      new URL(`../../_server_deploy/templates/${name}`, import.meta.url),
      "utf8",
    );
    const policyAt = source.indexOf(
      "/static/reader-runtime/interaction-policy.js",
    );
    const coreAt = source.indexOf("/static/pdf/rc-core.js");
    const outboxAt = source.indexOf("/static/pdf/rc-outbox.js");
    const businessAt = source.indexOf("/static/pdf/rc-flashcard.js");
    assert.ok(policyAt > coreAt, `${name}: policy follows the RC bootstrap`);
    assert.ok(policyAt < businessAt, `${name}: policy precedes business code`);
    if (outboxAt >= 0) {
      assert.ok(policyAt < outboxAt, `${name}: policy precedes outbox`);
    }
  }
});
