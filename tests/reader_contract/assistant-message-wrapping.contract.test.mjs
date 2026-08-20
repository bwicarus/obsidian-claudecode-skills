import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const SHARED = readFileSync(
  new URL("../../_server_deploy/static/pdf/rc-assistant.js", import.meta.url),
  "utf8",
);
const LEGACY = readFileSync(
  new URL("../../_server_deploy/static/pdf/reader.src/25-assistant.js", import.meta.url),
  "utf8",
);

for (const [name, source] of [["shared", SHARED], ["legacy", LEGACY]]) {
  test(`${name} assistant contains long card markers inside the message bubble`, () => {
    assert.match(
      source,
      /#asst-thread\{[^']*min-width:0;[^']*max-width:100%;[^']*box-sizing:border-box/,
    );
    assert.match(
      source,
      /\.asst-msg\{[^']*box-sizing:border-box;min-width:0;max-width:92%;[^']*overflow-wrap:anywhere;word-break:break-word/,
    );
    assert.match(
      source,
      /\.asst-msg \.rc-turn-bd,[^']*\.rc-part-text,[^']*\.vc-card-bd\{[^']*min-width:0;max-width:100%;overflow-wrap:anywhere/,
    );
    assert.match(
      source,
      /\.asst-msg code,[^']*white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word/,
    );
    assert.match(
      source,
      /\.asst-msg pre\{[^']*max-width:100%;white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;overflow-x:auto/,
    );
  });
}
