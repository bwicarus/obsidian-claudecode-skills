import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const SETTINGS = readFileSync(
  new URL("../../_server_deploy/static/pdf/rc-settings.js", import.meta.url),
  "utf8",
);
const PDF_SHELL = readFileSync(
  new URL("../../_server_deploy/templates/pdf_reader.html", import.meta.url),
  "utf8",
);

function zIndex(source, pattern, label) {
  const match = source.match(pattern);
  assert.ok(match, `${label} z-index must remain explicit`);
  return Number(match[1]);
}

test("debug overlays stay below settings so their controls remain tappable", () => {
  const epubDebug = zIndex(
    SETTINGS,
    /el\.style\.cssText = 'position:fixed;left:10px;bottom:10px;[^']*z-index:(\d+)'/,
    "EPUB debug overlay",
  );
  const sharedSettings = zIndex(
    SETTINGS,
    /\.rc-set-mask\{[^']*z-index:(\d+)\}/,
    "shared settings mask",
  );
  const pdfDebug = zIndex(
    PDF_SHELL,
    /<div id="debug-log"[^>]*z-index:(\d+)"/,
    "PDF debug overlay",
  );
  const nativeSettings = zIndex(
    PDF_SHELL,
    /<div id="settings-mask"[^>]*z-index:(\d+)"/,
    "native settings mask",
  );

  assert.ok(epubDebug < sharedSettings, "EPUB debug overlay must not cover shared settings");
  assert.ok(pdfDebug < sharedSettings, "PDF debug overlay must not cover shared settings");
  assert.ok(pdfDebug < nativeSettings, "PDF debug overlay must not cover native settings");
});
