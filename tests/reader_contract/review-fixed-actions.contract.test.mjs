import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const FLASHCARD = fs.readFileSync(
  new URL("../../_server_deploy/static/pdf/rc-flashcard.js", import.meta.url),
  "utf8",
);
const REVIEW = fs.readFileSync(
  new URL("../../_server_deploy/static/pdf/rc-review.js", import.meta.url),
  "utf8",
);

test("every learning Anki projection separates the scrollable face from the fixed rating footer", () => {
  const start = FLASHCARD.indexOf("function cardHtml(st, c, i)");
  const end = FLASHCARD.indexOf("function bindSlide", start);
  const renderer = FLASHCARD.slice(start, end);

  assert.match(
    renderer,
    /<div class="fc-card fc-review-card">[\s\S]*?<div class="fc-review-scroll">/,
  );
  assert.match(
    renderer,
    /var footerControls = c\._showBack[\s\S]*?\? answerControls[\s\S]*?fc-reveal[\s\S]*?data-fc="reveal"[\s\S]*?显示答案/,
  );
  assert.match(
    renderer,
    /<div class="fc-review-footer">['"]?\s*\+\s*footerControls/,
    "the card footer must exist before and after answer reveal",
  );
  assert.ok(
    renderer.indexOf("fc-review-scroll") <
      renderer.indexOf("fc-review-footer"),
    "rating controls must be a sibling after the scrollable face",
  );
  assert.match(
    FLASHCARD,
    /\.fc-review-scroll\{[^}]*overflow-y:auto[^}]*\}/,
  );
  assert.match(
    FLASHCARD,
    /\.fc-review-footer\{[^}]*flex:0 0 auto[^}]*\}/,
  );
  assert.match(
    FLASHCARD,
    /\.fc-reveal\{[^}]*width:100%[^}]*\}/,
    "the pre-answer action must occupy the fixed footer",
  );
  assert.doesNotMatch(
    FLASHCARD,
    /\.fc-review-footer\{[^}]*overflow-y:auto/,
  );
  assert.doesNotMatch(
    renderer,
    /return '<div class="fc-card">' \+ body/,
    "ordinary learning cards must not fall back to the old scrolling action layout",
  );
});

test("controlled reveal supports answer-only replacement without changing other modes", () => {
  const start = FLASHCARD.indexOf("function cardHtml(st, c, i)");
  const end = FLASHCARD.indexOf("function bindSlide", start);
  const renderer = FLASHCARD.slice(start, end);

  assert.match(
    renderer,
    /c\._revealMode === 'replace'[\s\S]*?fc-answer-only/,
  );
  assert.match(
    renderer,
    /fc-answer-only[^+]*['"]\s*\+\s*back/,
  );
  assert.ok(
    renderer.includes(
      `: '<div class="fc-face">' + front + '<div class="fc-back">' + back + '</div></div>'`,
    ),
    "append remains the default controlled-review reveal",
  );
  assert.match(
    renderer,
    /if \(c\._st === 'preview'\)[\s\S]*?<div class="fc-lbl">正面/,
    "preview rendering must remain outside the controlled learn branch",
  );
});

test("review workspace has one bounded face scroller and a fixed bottom improvement dock", () => {
  assert.match(
    REVIEW,
    /#asst-review-workspace\{[^}]*height:min\(54vh,520px\)[^}]*overflow:hidden/,
  );
  assert.match(
    REVIEW,
    /#rc-review-body\{[^}]*height:100%[^}]*overflow:hidden/,
  );
  assert.match(
    REVIEW,
    /#asst-review-workspace\.rv-card-collapsed,#asst-review-workspace\.rv-card-empty\{[^}]*height:auto/,
    "collapsing the card must not reserve an empty fixed-height workspace",
  );
  assert.match(
    REVIEW,
    /\.rv-card-panel\{[^}]*display:flex[^}]*min-height:0[^}]*overflow:hidden/,
  );
  assert.match(
    REVIEW,
    /\.rv-review-card \.vc-card-bd\.rv-review-layout\{[^}]*display:flex[^}]*overflow:hidden!important/,
  );
  assert.match(
    REVIEW,
    /\.rv-review-controls\{[^}]*flex:0 1 45%[^}]*max-height:45%[^}]*overflow:hidden/,
  );
  assert.match(
    REVIEW,
    /\.rv-review-controls>\.rv-improve-toggle\{[^}]*flex:0 0 auto[^}]*width:100%/,
  );
  assert.match(
    REVIEW,
    /\.rv-improve-panel\{[^}]*flex:1 1 auto[^}]*overflow-y:auto/,
  );
  assert.match(
    REVIEW,
    /rendered\.bd\.classList\.add\('rv-review-layout'\)/,
  );
  assert.match(
    REVIEW,
    /panel\.classList\.toggle\('rv-improve-open', _improveExpanded\)/,
  );
  const pagerMount = REVIEW.indexOf(
    "if (RC.flashcard && typeof RC.flashcard.bindPager === 'function')",
  );
  const dockMount = REVIEW.indexOf(
    "reviewControls.className = 'rv-review-controls'",
    pagerMount,
  );
  assert.ok(
    dockMount > pagerMount,
    "the improvement dock must be mounted after the card pager at the bottom",
  );
  assert.match(
    REVIEW.slice(dockMount, REVIEW.indexOf("function _queueCards", dockMount)),
    /reviewControls\.appendChild\(improveToggle\)[\s\S]*?_appendImprovePanel\(reviewControls, card\)[\s\S]*?panel\.appendChild\(reviewControls\)/,
  );
});
