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
const ASSISTANT = readFileSync(
  new URL("../../_server_deploy/static/pdf/rc-assistant.js", import.meta.url),
  "utf8",
);
const VOICECALL = readFileSync(
  new URL("../../_server_deploy/static/pdf/rc-voicecall.js", import.meta.url),
  "utf8",
);
const VIDEOPLAYER = readFileSync(
  new URL("../../_server_deploy/static/pdf/rc-videoplayer.js", import.meta.url),
  "utf8",
);
const SIDEDRAWER = readFileSync(
  new URL("../../_server_deploy/static/pdf/rc-sidedrawer.js", import.meta.url),
  "utf8",
);
const PDF_STYLES = readFileSync(
  new URL("../../_server_deploy/static/pdf/pdf-styles.css", import.meta.url),
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

test("all settings surfaces cover cards, voice UI, and floating media", () => {
  const sharedSettings = zIndex(
    SETTINGS,
    /\.rc-set-mask\{[^']*z-index:(\d+)\}/,
    "shared settings mask",
  );
  const nativeSettings = zIndex(
    PDF_SHELL,
    /<div id="settings-mask"[^>]*z-index:(\d+)"/,
    "native settings mask",
  );
  const modelSettings = zIndex(
    ASSISTANT,
    /\.ams-mask\{[^']*z-index:(\d+)/,
    "assistant model settings",
  );
  const voicePanel = zIndex(
    VOICECALL,
    /#rc-vc\{[^']*z-index:(\d+)/,
    "voice panel",
  );
  const voiceDetail = zIndex(
    VOICECALL,
    /#vc-dtl\{[^']*z-index:(\d+)/,
    "voice tool detail",
  );
  const video = zIndex(
    VIDEOPLAYER,
    /#rc-vplayer\{[^']*z-index:(\d+)/,
    "floating video",
  );
  const sharedSideSettings = zIndex(
    SIDEDRAWER,
    /#ep-side\.rc-side-settings-open\{z-index:(\d+)\}/,
    "shared side settings",
  );
  const legacySideSettings = zIndex(
    PDF_STYLES,
    /#grammar-panel\.rc-side-settings-open\{z-index:(\d+)\}/,
    "legacy side settings",
  );

  for (const [name, layer] of [
    ["shared settings", sharedSettings],
    ["native settings", nativeSettings],
    ["model settings", modelSettings],
    ["shared side settings", sharedSideSettings],
    ["legacy side settings", legacySideSettings],
  ]) {
    assert.ok(layer > voicePanel, `${name} must cover the voice panel`);
    assert.ok(layer > voiceDetail, `${name} must cover voice tool details`);
    assert.ok(layer > video, `${name} must cover floating video`);
  }
});
