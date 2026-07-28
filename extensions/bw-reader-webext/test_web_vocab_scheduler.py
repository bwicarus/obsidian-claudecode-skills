#!/usr/bin/env python3
"""Real-Chromium regression for ordinary-web vocabulary scheduling.

The test loads the canonical ``web-immersive.js`` source in a small ordinary
web page.  Only the two fixed server endpoints are stubbed; sentence discovery,
IntersectionObserver delivery, idle scheduling, animation-frame commits and
the click handler all run in Chromium.

It protects three user-visible invariants:

* a queue larger than two scan batches drains completely without another
  scroll (the trailing 40 of 120 sentences cannot be starved);
* scrolling does not restart a synchronous whole-document DOM collection;
* a warmed ``译 N`` sentence renders in the same click task and does not issue
  another translation request.
"""

from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[2]
IMMERSIVE = ROOT / "_server_deploy" / "static" / "pdf" / "web-immersive.js"
CHROME = Path.home() / ".cache/ms-playwright/chromium-1223/chrome-linux/chrome"
URL = "http://web-vocab-scheduler.test/"
SENTENCE_COUNT = 120


def sentence(index: int) -> str:
    return (
        f"Scheduler sentence {index:03d} contains enough distinct vocabulary "
        "tokens to satisfy the ordinary web unknown word threshold today."
    )


def html() -> str:
    paragraphs = "\n".join(
        f'<p data-sentence="{index}">{sentence(index)}</p>'
        for index in range(1, SENTENCE_COUNT + 1)
    )
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Web vocabulary scheduler contract</title>
  <style>
    html, body {{ margin: 0; padding: 0; }}
    #article {{
      width: 1300px;
      font: 10px/14px sans-serif;
    }}
    #article p {{
      margin: 0;
      min-height: 14px;
      white-space: nowrap;
    }}
    #scroll-space {{ height: 5000px; }}
  </style>
</head>
<body>
  <main id="article">{paragraphs}</main>
  <div id="scroll-space" aria-hidden="true"></div>
  <script>
  (() => {{
    const originalTreeWalker = document.createTreeWalker.bind(document);
    const originalRect = Element.prototype.getBoundingClientRect;
    const state = window.__schedulerProbe = {{
      treeWalkCalls: 0,
      rectCalls: 0,
      vocabRequests: [],
      translateRequests: []
    }};

    // These counters make the former scroll -> autoVocab -> collect(body)
    // regression directly observable.  The scheduler may use them while
    // preparing sentences, but a settled scroll must not increase either.
    document.createTreeWalker = function (...args) {{
      state.treeWalkCalls += 1;
      return originalTreeWalker(...args);
    }};
    Element.prototype.getBoundingClientRect = function (...args) {{
      state.rectCalls += 1;
      return originalRect.apply(this, args);
    }};

    window.__bwTranslationCacheGet = async cacheNamespace => ({{
      cacheNamespace,
      items: {{}}
    }});
    window.__rcRawFetch = async (url, init = {{}}) => {{
      const body = init.body ? JSON.parse(init.body) : {{}};
      if (String(url).endsWith('/pdf/api/web-vocab')) {{
        state.vocabRequests.push(body);
        return {{
          async json() {{
            return {{
              ok: true,
              items: (body.texts || []).map((text, i) => ({{
                i,
                hot: true,
                count: 3,
                words: ['scheduler', 'vocabulary', 'threshold'],
                occurrences: []
              }}))
            }};
          }}
        }};
      }}
      if (String(url).endsWith('/pdf/api/web-translate')) {{
        state.translateRequests.push(body);
        return {{
          async json() {{
            return {{
              ok: true,
              zh: (body.texts || []).map(text => '已预热：' + text),
              sources: (body.texts || []).map(() => 'google'),
              cacheNamespace: 'web-google-v1-gtranslate-v2',
              googleCacheNamespace: 'web-google-v1-gtranslate-v2'
            }};
          }}
        }};
      }}
      throw new Error('unexpected request: ' + url);
    }};
  }})();
  </script>
</body>
</html>"""


def main() -> None:
    assert IMMERSIVE.is_file(), IMMERSIVE
    assert CHROME.is_file(), CHROME
    all_sentences = [sentence(index) for index in range(1, SENTENCE_COUNT + 1)]
    trailing = all_sentences[-40:]
    hot_target = trailing[-1]

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(CHROME),
            headless=True,
            args=["--no-sandbox"],
        )
        try:
            context = browser.new_context(viewport={"width": 1600, "height": 800})
            context.route(
                URL,
                lambda route: route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body=html(),
                ),
            )
            page = context.new_page()
            page_errors: list[str] = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.goto(URL, wait_until="domcontentloaded")
            page.add_script_tag(path=str(IMMERSIVE))

            # 120 entries exceed two 48-item network batches.  Seeing all
            # trailing 40 in requests and committed buttons proves that the
            # filtered FIFO drains without needing a second scroll wake-up.
            page.wait_for_function(
                """trailing => {
                  const state = window.__schedulerProbe;
                  const queued = new Set(
                    state.vocabRequests.flatMap(request => request.texts || [])
                  );
                  const committed = new Set(
                    [...document.querySelectorAll('.rc-vocab-unit')]
                      .filter(unit => unit.querySelector('.rc-vocab-btn'))
                      .map(unit => unit.__rcText)
                  );
                  return (
                    trailing.every(text => queued.has(text)) &&
                    trailing.every(text => committed.has(text))
                  );
                }""",
                arg=trailing,
                timeout=20_000,
            )
            queue_state = page.evaluate(
                """allTexts => {
                  const state = window.__schedulerProbe;
                  const queued = new Set(
                    state.vocabRequests.flatMap(request => request.texts || [])
                  );
                  return {
                    scrollY: window.scrollY,
                    requestSizes: state.vocabRequests.map(
                      request => (request.texts || []).length
                    ),
                    queuedCount: allTexts.filter(text => queued.has(text)).length,
                    committedButtons:
                      document.querySelectorAll('.rc-vocab-btn').length,
                  };
                }""",
                all_sentences,
            )
            assert queue_state["scrollY"] == 0, queue_state
            assert queue_state["queuedCount"] == SENTENCE_COUNT, queue_state
            assert queue_state["committedButtons"] == SENTENCE_COUNT, queue_state
            assert len(queue_state["requestSizes"]) >= 3, queue_state
            assert max(queue_state["requestSizes"]) <= 48, queue_state

            # Wait for the target's Google prewarm promise to settle into the
            # in-memory namespace before testing the synchronous click path.
            page.wait_for_function(
                """target => window.__schedulerProbe.translateRequests.some(
                  request => (request.texts || []).includes(target)
                )""",
                arg=hot_target,
                timeout=10_000,
            )
            page.wait_for_timeout(50)

            baseline = page.evaluate(
                """() => ({
                  treeWalkCalls: window.__schedulerProbe.treeWalkCalls,
                  rectCalls: window.__schedulerProbe.rectCalls,
                  vocabRequests: window.__schedulerProbe.vocabRequests.length,
                  units: document.querySelectorAll('.rc-vocab-unit').length
                })"""
            )
            page.evaluate(
                """() => {
                  for (let i = 0; i < 12; i += 1) {
                    window.scrollTo(0, i % 2 ? 0 : 1800);
                    dispatchEvent(new Event('scroll'));
                  }
                }"""
            )
            # Covers the old 700 ms scroll debounce as well as the new short
            # animation-pause timer.
            page.wait_for_timeout(1_050)
            after_scroll = page.evaluate(
                """() => ({
                  treeWalkCalls: window.__schedulerProbe.treeWalkCalls,
                  rectCalls: window.__schedulerProbe.rectCalls,
                  vocabRequests: window.__schedulerProbe.vocabRequests.length,
                  units: document.querySelectorAll('.rc-vocab-unit').length
                })"""
            )
            assert after_scroll == baseline, {
                "before_scroll": baseline,
                "after_scroll": after_scroll,
            }

            request_count = page.evaluate(
                "() => window.__schedulerProbe.translateRequests.length"
            )
            immediate = page.evaluate(
                """target => {
                  const unit = [...document.querySelectorAll('.rc-vocab-unit')]
                    .find(candidate => candidate.__rcText === target);
                  const button = unit && unit.querySelector('.rc-vocab-btn');
                  if (!button) return { found: false };
                  button.click();
                  const block = unit.nextElementSibling;
                  return {
                    found: true,
                    sameTask: !!block && block.classList.contains('rc-tr-block'),
                    text: block ? block.textContent : '',
                    buttonRemoved: !unit.querySelector('.rc-vocab-btn')
                  };
                }""",
                hot_target,
            )
            assert immediate == {
                "found": True,
                "sameTask": True,
                "text": "已预热：" + hot_target,
                "buttonRemoved": True,
            }, immediate
            page.wait_for_timeout(250)
            assert (
                page.evaluate(
                    "() => window.__schedulerProbe.translateRequests.length"
                )
                == request_count
            ), "warm 译N click issued an unnecessary /web-translate request"
            assert not page_errors, page_errors

            print(
                "OK: 120-sentence FIFO drained without scroll; scroll did not "
                "re-collect the document; warmed 译N rendered synchronously "
                "without another translation request"
            )
        finally:
            browser.close()


if __name__ == "__main__":
    main()
