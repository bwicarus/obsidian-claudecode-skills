#!/usr/bin/env python3
"""Real Chromium regression for the shared contextual review queue.

The test always uses a random temporary browser profile.  It stubs only the
fixed review endpoint inside the extension service worker; the content script,
network allowlist, account-scoped extension storage and shared review UI are
the production files.
"""
from __future__ import annotations

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import time
from threading import Thread

from playwright.sync_api import sync_playwright


EXT = Path(
    os.environ.get("BW_EXTENSION_ROOT", Path(__file__).resolve().parent)
).resolve()
DEFAULT_CHROME = (
    Path.home() / ".cache/ms-playwright/chromium-1223/chrome-linux/chrome"
)
CHROME = Path(os.environ.get("BW_PLAYWRIGHT_CHROME", DEFAULT_CHROME))
NAMESPACE = "acct-v1-" + "b" * 64

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Candidate Contract</title></head>
<body>
  <main>
    <h1>Vector spaces and direct sums</h1>
    <p>The current page discusses subspaces, linear independence and direct sums.</p>
    <button id="host-button">host button</button>
  </main>
  <script>
    window.__hostClicks = 0;
    document.getElementById('host-button').addEventListener(
      'click', () => window.__hostClicks++
    );
  </script>
</body></html>"""


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *args: object) -> None:
        return


def wait_until(predicate, label: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
            if last:
                return
        except Exception as error:
            last = error
        time.sleep(0.08)
    raise AssertionError(f"timed out waiting for {label}: {last!r}")


def review_button_ready(page) -> bool:
    return bool(
        page.evaluate(
            """() => {
              const root = document.querySelector('#bw-reader-host')?.shadowRoot;
              return !!root?.querySelector('#ep-side-tabs [data-pane="asst"]') &&
                !!root?.querySelector('#asst-review-toggle');
            }"""
        )
    )


def open_review(page) -> None:
    wait_until(lambda: review_button_ready(page), "assistant review toggle")
    clicked = page.evaluate(
        """() => {
          const root = document.querySelector('#bw-reader-host')?.shadowRoot;
          const assistant = root?.querySelector(
            '#ep-side-tabs [data-pane="asst"]'
          );
          const review = root?.querySelector('#asst-review-toggle');
          if (!assistant || !review) return false;
          assistant.click();
          if (review.getAttribute('aria-pressed') !== 'true') review.click();
          return true;
        }"""
    )
    assert clicked
    page.wait_for_function(
        """() => document.querySelector('#bw-reader-host')
          ?.shadowRoot?.querySelector('#rc-review-body')
          ?.innerText?.includes('candidate browser card')""",
        timeout=15_000,
    )


def main() -> None:
    assert EXT.is_dir(), EXT
    assert (EXT / "manifest.json").is_file(), EXT
    assert CHROME.is_file(), CHROME

    with tempfile.TemporaryDirectory(prefix="bw-review-candidate-page-") as web:
        Path(web, "index.html").write_text(PAGE, "utf-8")
        handler = partial(QuietHandler, directory=web)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/index.html"
        try:
            with tempfile.TemporaryDirectory(
                prefix="bw-review-candidate-profile-"
            ) as profile:
                with sync_playwright() as playwright:
                    context = playwright.chromium.launch_persistent_context(
                        profile,
                        executable_path=str(CHROME),
                        headless=False,
                        viewport={"width": 1280, "height": 800},
                        args=[
                            f"--disable-extensions-except={EXT}",
                            f"--load-extension={EXT}",
                            "--no-sandbox",
                        ],
                    )
                    try:
                        workers = context.service_workers
                        worker = workers[0] if workers else context.wait_for_event(
                            "serviceworker", timeout=15_000
                        )
                        seeded = worker.evaluate(
                            """async ({namespace}) => {
                              await chrome.storage.local.set({
                                readerActiveVerifiedAccountV1: {
                                  schema: 1,
                                  namespace,
                                  verifiedAt: Date.now(),
                                  source: 'provider-ticket'
                                }
                              });
                              await ensurePersistentAccount();
                              const captured = await capturePersistentAccount();
                              await accountStorage.saveVerifiedToken(
                                captured.entry.accountContext,
                                captured.lease,
                                'review-candidate-test-token'
                              );
                              const originalFetch = globalThis.fetch.bind(globalThis);
                              globalThis.__candidateCalls = [];
                              globalThis.fetch = async (input, init = {}) => {
                                const requestUrl = new URL(
                                  typeof input === 'string' ? input : input.url
                                );
                                if (requestUrl.pathname === '/pdf/api/review-queue') {
                                  const method = String(init.method || 'GET').toUpperCase();
                                  let body = null;
                                  try { body = init.body ? JSON.parse(init.body) : null; }
                                  catch (_) {}
                                  globalThis.__candidateCalls.push({
                                    url: requestUrl.href,
                                    method,
                                    body
                                  });
                                  const payload = method === 'POST'
                                    ? {
                                        ok: true,
                                        contract: 'card-candidate-service/1',
                                        context_key: 'browser-context',
                                        due_total: 4,
                                        related_total: 2,
                                        cards: [{
                                          id: 10,
                                          note_id: 1010,
                                          question: '<div class="word"><b>candidate browser card</b></div><div class="phonetic">/candidate/</div><div class="url">📖 <a href="https://reader.example/pdf/view?file=books%2F000-note.pdf&page=7">legacy source notebook</a></div><div class="tags">legacy-vocab-tag</div>',
                                          answer: '<div class="word"><b>candidate browser card</b></div><div class="phonetic">/candidate/</div><div class="url">📖 <a href="https://reader.example/pdf/view?file=books%2F000-note.pdf&page=7">legacy source notebook</a></div><div class="tags">legacy-vocab-tag</div><hr><div class="actual-answer"><b>candidate answer</b><p>Definition and usage in the current reading context.</p><p><a class="answer-reference" href="https://docs.example/meaning">ordinary answer reference</a></p><div class="long-answer">' + '<p>Long answer paragraph used to prove that only the card face scrolls while permanent actions remain reachable.</p>'.repeat(18) + '</div><div class="refbox"><div class="example">A concise example remains part of the answer.</div><div class="more"><p>supplementary etymology and usage notes</p></div></div><div class="audio-line">[anki:play:a:0]</div><div class="url">📖 <a href="https://reader.example/pdf/view?file=books%2F000-note.pdf&page=7">legacy source notebook</a></div><div class="tags">legacy-vocab-tag</div></div>',
                                          deck: 'Contract',
                                          review_kind: 'related',
                                          candidate_reasons: ['当前内容来源'],
                                          was_due: false
                                        }, {
                                          id: 11,
                                          note_id: 1010,
                                          question: '<b>second candidate card</b>',
                                          answer: '<b>second candidate answer</b>',
                                          deck: 'Contract',
                                          review_kind: 'related',
                                          candidate_reasons: ['当前内容来源'],
                                          was_due: false,
                                          source_ref: 'note:000-note.md',
                                          source_url: 'obsidian://open?vault=Test&file=000-note'
                                        }]
                                      }
                                    : { ok: true, due_total: 0, cards: [] };
                                  return new Response(JSON.stringify(payload), {
                                    status: 200,
                                    headers: { 'Content-Type': 'application/json' }
                                  });
                                }
                                if (requestUrl.pathname === '/pdf/api/review-answer') {
                                  let body = null;
                                  try { body = init.body ? JSON.parse(init.body) : null; }
                                  catch (_) {}
                                  globalThis.__candidateCalls.push({
                                    url: requestUrl.href,
                                    method: String(init.method || 'GET').toUpperCase(),
                                    body
                                  });
                                  if (body && body.card_id === 11 &&
                                      globalThis.__holdReviewAnswer) {
                                    return await new Promise(resolve => {
                                      globalThis.__releaseReviewAnswer = () => {
                                        globalThis.__holdReviewAnswer = false;
                                        resolve(new Response(JSON.stringify({
                                          ok: true,
                                          next: {interval: 4}
                                        }), {
                                          status: 200,
                                          headers: {
                                            'Content-Type': 'application/json'
                                          }
                                        }));
                                      };
                                    });
                                  }
                                  return new Response(JSON.stringify({ok:true}), {
                                    status: 200,
                                    headers: { 'Content-Type': 'application/json' }
                                  });
                                }
                                return originalFetch(input, init);
                              };
                              return captured.lease.namespace === namespace;
                            }""",
                            {"namespace": NAMESPACE},
                        )
                        assert seeded

                        page = context.new_page()
                        page.goto(url, wait_until="domcontentloaded")
                        page.wait_for_selector(
                            "#bw-reader-host",
                            state="attached",
                            timeout=15_000,
                        )
                        page.locator("#host-button").click()
                        assert page.evaluate("() => window.__hostClicks") == 1
                        open_review(page)

                        calls = worker.evaluate("() => __candidateCalls.slice()")
                        assert len(calls) == 1, calls
                        assert calls[0]["method"] == "POST", calls
                        assert calls[0]["url"].endswith("/pdf/api/review-queue")
                        assert "visible_text" in calls[0]["body"]["context"]
                        assert "visible_text" not in calls[0]["url"]
                        initial = page.evaluate(
                            """() => {
                              const root = document.querySelector(
                                '#bw-reader-host'
                              ).shadowRoot;
                              const workspace = root.querySelector(
                                '#asst-review-workspace'
                              );
                              return {
                                text: workspace.innerText,
                                answerButton: !!workspace.querySelector(
                                  '.rv-review-card [data-fc="reveal"]'
                                ),
                                back: !!workspace.querySelector(
                                  '.rv-review-card .fc-back'
                                ),
                                vcCards: workspace.querySelectorAll(
                                  '.rv-review-card.vc-card'
                                ).length,
                                slides: [...workspace.querySelectorAll(
                                  '.rv-card-slide'
                                )].map((slide) => ({
                                  inert: slide.hasAttribute('inert'),
                                  hidden: slide.getAttribute('aria-hidden')
                                })),
                                dots: workspace.querySelectorAll(
                                  '.rv-card-dots .fc-dot'
                                ).length,
                                activeDot: workspace.querySelector(
                                  '.rv-card-dots .fc-dot.on'
                                )?.dataset.goto || '',
                                arrows: workspace.querySelectorAll(
                                  '.rv-prev,.rv-next'
                                ).length,
                                legacyFrames: workspace.querySelectorAll(
                                  '.rv-face,.rv-faces'
                                ).length,
                                cid: workspace.querySelector(
                                  '.rv-review-card'
                                )?.dataset.vcCid || '',
                                pinnable: workspace.querySelector(
                                  '.rv-review-card'
                                )?.classList.contains('vc-pinnable') || false,
                                deck: workspace.querySelector(
                                  '.rv-review-card .rv-meta'
                                )?.innerText || '',
                                improveHidden: workspace.querySelector(
                                  '.rv-improve-panel'
                                )?.hidden,
                                frontCount: (
                                  workspace.innerText.match(
                                    /candidate browser card/g
                                  ) || []
                                ).length,
                                hiddenLegacySource: !workspace.innerText.includes(
                                  'legacy source notebook'
                                ),
                                hiddenLegacyTag: !workspace.innerText.includes(
                                  'legacy-vocab-tag'
                                ),
                                hiddenLegacyAudio: !workspace.innerText.includes(
                                  '[anki:play:'
                                ),
                                rawPreserved: (() => {
                                  const raw = workspace.querySelector(
                                    '.rv-review-card .vc-card-bd'
                                  )?.__fc?.cards?.[0];
                                  return !!raw &&
                                    raw.back.includes(
                                      'legacy source notebook'
                                    ) &&
                                    raw.back.includes('legacy-vocab-tag') &&
                                    raw.back.includes('[anki:play:a:0]');
                                })(),
                                chatBelow: !!root.querySelector('#asst-thread') &&
                                  !!root.querySelector('#asst-input')
                              };
                            }"""
                        )
                        assert initial["answerButton"], initial
                        assert not initial["back"], initial
                        assert initial["vcCards"] == 2, initial
                        assert initial["slides"] == [
                            {"inert": False, "hidden": "false"},
                            {"inert": True, "hidden": "true"},
                        ], initial
                        assert initial["dots"] == 2, initial
                        assert initial["activeDot"] == "0", initial
                        assert initial["arrows"] == 0, initial
                        assert initial["legacyFrames"] == 0, initial
                        assert initial["cid"] == "anki_card_10", initial
                        assert initial["pinnable"], initial
                        assert "Contract" in initial["deck"], initial
                        assert initial["improveHidden"], initial
                        assert initial["chatBelow"], initial
                        assert initial["frontCount"] == 1, initial
                        assert initial["hiddenLegacySource"], initial
                        assert initial["hiddenLegacyTag"], initial
                        assert initial["hiddenLegacyAudio"], initial
                        assert initial["rawPreserved"], initial
                        assert "candidate answer" not in initial["text"], initial
                        assert "Local ID" not in initial["text"], initial
                        assert "当前内容来源" not in initial["text"], initial

                        # Review navigation reuses the same flashcard dots and
                        # scroll-snap controller; there are no separate arrows.
                        page.evaluate(
                            """() => document.querySelector('#bw-reader-host')
                              .shadowRoot.querySelector(
                                '.rv-card-dots .fc-dot[data-goto="1"]'
                              ).click()"""
                        )
                        page.wait_for_function(
                            """() => document.querySelector('#bw-reader-host')
                              ?.shadowRoot?.querySelector(
                                '.rv-card-dots .fc-dot.on'
                              )?.dataset.goto === '1'"""
                        )
                        page.evaluate(
                            """() => document.querySelector('#bw-reader-host')
                              .shadowRoot.querySelector(
                                '.rv-card-dots .fc-dot[data-goto="0"]'
                              ).click()"""
                        )
                        page.wait_for_function(
                            """() => document.querySelector('#bw-reader-host')
                              ?.shadowRoot?.querySelector(
                                '.rv-card-dots .fc-dot.on'
                              )?.dataset.goto === '0'"""
                        )

                        # The shared vc-card deliberately stops click bubbling
                        # at its boundary.  Review actions mounted inside that
                        # card must still reach the review controller.
                        page.evaluate(
                            """() => document.querySelector('#bw-reader-host')
                              .shadowRoot.querySelector(
                                '#rc-review-body [data-action="toggle-improve"]'
                              ).click()"""
                        )
                        page.evaluate(
                            """() => document.querySelector('#bw-reader-host')
                              .shadowRoot.querySelector(
                                '.rv-review-card [data-action="verbosity"]' +
                                '[data-mode="concise"]'
                              ).click()"""
                        )
                        improvement = page.evaluate(
                            """() => {
                              const workspace = document.querySelector(
                                '#bw-reader-host'
                              ).shadowRoot.querySelector(
                                '#asst-review-workspace'
                              );
                              return {
                                hidden: workspace.querySelector(
                                  '.rv-improve-panel'
                                )?.hidden,
                                concise: workspace.querySelector(
                                  '[data-action="verbosity"]' +
                                  '[data-mode="concise"]'
                                )?.getAttribute('aria-pressed')
                              };
                            }"""
                        )
                        assert not improvement["hidden"], improvement
                        assert improvement["concise"] == "true", improvement
                        screenshot = os.environ.get("BW_REVIEW_SCREENSHOT", "").strip()
                        if screenshot:
                            page.screenshot(path=screenshot, full_page=False)

                        wait_until(
                            lambda: worker.evaluate(
                                """async key => !!(await chrome.storage.local.get(key))[key]""",
                                f"reviewQueueV2:{NAMESPACE}",
                            ),
                            "account-scoped review snapshot",
                        )
                        keys = worker.evaluate(
                            """async () => Object.keys(await chrome.storage.local.get(null))
                              .filter(key => key.startsWith('reviewQueueV2'))"""
                        )
                        assert keys == [f"reviewQueueV2:{NAMESPACE}"], keys

                        # Before grading, an exact-context reload must reuse the
                        # account-scoped snapshot and avoid a second queue POST.
                        page.reload(wait_until="domcontentloaded")
                        open_review(page)
                        calls_after_reload = worker.evaluate(
                            "() => __candidateCalls.slice()"
                        )
                        assert len(calls_after_reload) == 1, calls_after_reload

                        page.evaluate(
                            """() => document.querySelector('#bw-reader-host')
                              .shadowRoot.querySelector(
                                '.rv-review-card [data-fc="reveal"]'
                              ).click()"""
                        )
                        revealed = page.evaluate(
                            """() => {
                              const root = document.querySelector(
                                '#bw-reader-host'
                              ).shadowRoot;
                              const back = root.querySelector(
                                '.rv-review-card .fc-back'
                              );
                              const card = root.querySelector(
                                '.rv-review-card .fc-review-card'
                              );
                              const scroll = card?.querySelector(
                                '.fc-review-scroll'
                              );
                              const footer = card?.querySelector(
                                '.fc-review-footer'
                              );
                              const extra = back?.querySelector(
                                'details.rv-card-extra'
                              );
                              const cardRect = card?.getBoundingClientRect();
                              const footerRect = footer?.getBoundingClientRect();
                              return {
                                text: back?.innerText || '',
                                cardText: card?.innerText || '',
                                frontCount: (
                                  card?.innerText.match(
                                    /candidate browser card/g
                                  ) || []
                                ).length,
                                answerCount: (
                                  card?.innerText.match(
                                    /candidate answer/g
                                  ) || []
                                ).length,
                                hiddenLegacySource: !card?.innerText.includes(
                                  'legacy source notebook'
                                ),
                                hiddenLegacyTag: !card?.innerText.includes(
                                  'legacy-vocab-tag'
                                ),
                                hiddenLegacyAudio: !card?.innerText.includes(
                                  '[anki:play:'
                                ),
                                ordinaryLink: back?.querySelector(
                                  'a.answer-reference'
                                )?.getAttribute('href') || '',
                                extra: {
                                  exists: !!extra,
                                  open: !!extra?.open,
                                  summary: extra?.querySelector(
                                    'summary'
                                  )?.innerText || '',
                                  rawText: extra?.textContent || ''
                                },
                                scrollOverflow: !!scroll &&
                                  scroll.scrollHeight > scroll.clientHeight,
                                footerOutsideScroll: !!footer && !!scroll &&
                                  !scroll.contains(footer),
                                footerVisible: !!footerRect && !!cardRect &&
                                  footerRect.height > 0 &&
                                  footerRect.top >= cardRect.top - 1 &&
                                  footerRect.bottom <= cardRect.bottom + 1,
                                eases: [...root.querySelectorAll(
                                  '.rv-review-card .fc-e'
                                )].map(node => [
                                  node.dataset.ease,
                                  node.textContent.trim()
                                ])
                              };
                            }"""
                        )
                        assert "candidate answer" in revealed["text"], revealed
                        assert "candidate browser card" not in revealed["text"], revealed
                        assert "来源：" not in revealed["text"], revealed
                        assert "Local ID" not in revealed["text"], revealed
                        assert revealed["frontCount"] == 1, revealed
                        assert revealed["answerCount"] == 1, revealed
                        assert revealed["hiddenLegacySource"], revealed
                        assert revealed["hiddenLegacyTag"], revealed
                        assert revealed["hiddenLegacyAudio"], revealed
                        assert revealed["ordinaryLink"] == (
                            "https://docs.example/meaning"
                        ), revealed
                        assert revealed["extra"] == {
                            "exists": True,
                            "open": False,
                            "summary": "补充信息",
                            "rawText": (
                                "补充信息"
                                "supplementary etymology and usage notes"
                            ),
                        }, revealed
                        assert revealed["scrollOverflow"], revealed
                        assert revealed["footerOutsideScroll"], revealed
                        assert revealed["footerVisible"], revealed
                        assert revealed["eases"] == [
                            ["1", "再来"],
                            ["2", "困难"],
                            ["3", "良好"],
                            ["4", "简单"],
                        ], revealed

                        # Long answer content may scroll, but both the rating
                        # footer and the expanded improvement controls stay in
                        # dedicated, reachable layers outside that scroll.
                        page.evaluate(
                            """() => document.querySelector('#bw-reader-host')
                              .shadowRoot.querySelector(
                                '#rc-review-body ' +
                                '[data-action="toggle-improve"]'
                              ).click()"""
                        )
                        fixed_actions = page.evaluate(
                            """() => {
                              const root = document.querySelector(
                                '#bw-reader-host'
                              ).shadowRoot;
                              const workspace = root.querySelector(
                                '#asst-review-workspace'
                              );
                              const scroll = workspace.querySelector(
                                '.fc-review-scroll'
                              );
                              const footer = workspace.querySelector(
                                '.fc-review-footer'
                              );
                              const improve = workspace.querySelector(
                                '.rv-improve-panel'
                              );
                              const actions = improve?.querySelector(
                                '.rv-actions'
                              );
                              const workspaceRect =
                                workspace.getBoundingClientRect();
                              const footerRect =
                                footer?.getBoundingClientRect();
                              const actionsRect =
                                actions?.getBoundingClientRect();
                              return {
                                improveOpen: !!improve && !improve.hidden,
                                improveOutsideCardScroll: !!improve &&
                                  !!scroll && !scroll.contains(improve),
                                actionsVisible: !!actionsRect &&
                                  actionsRect.height > 0 &&
                                  actionsRect.top < workspaceRect.bottom &&
                                  actionsRect.bottom > workspaceRect.top,
                                footerStillVisible: !!footerRect &&
                                  footerRect.height > 0 &&
                                  footerRect.top < workspaceRect.bottom &&
                                  footerRect.bottom > workspaceRect.top
                              };
                            }"""
                        )
                        assert fixed_actions == {
                            "improveOpen": True,
                            "improveOutsideCardScroll": True,
                            "actionsVisible": True,
                            "footerStillVisible": True,
                        }, fixed_actions

                        page.evaluate(
                            """() => document.querySelector('#bw-reader-host')
                              .shadowRoot.querySelector(
                                '.rv-review-card .fc-e[data-ease="3"]'
                              ).click()"""
                        )
                        wait_until(
                            lambda: len(worker.evaluate(
                                "() => __candidateCalls.slice()"
                            )) == 2,
                            "review answer write",
                        )
                        answer_call = worker.evaluate(
                            "() => __candidateCalls.slice()[1]"
                        )
                        assert answer_call["url"].endswith(
                            "/pdf/api/review-answer"
                        ), answer_call
                        assert answer_call["method"] == "POST", answer_call
                        assert answer_call["body"]["card_id"] == 10, answer_call
                        assert answer_call["body"]["ease"] == 3, answer_call

                        # A review card dragged onto the page remains the same
                        # Anki entity. Rating that copy must use exact card_id,
                        # immediately disable every live projection, and update
                        # the review queue only after the single request settles.
                        page.wait_for_function(
                            """() => document.querySelector('#bw-reader-host')
                              ?.shadowRoot?.querySelector(
                                '.rv-review-card'
                              )?.innerText?.includes(
                                'second candidate card'
                              )""",
                            timeout=10_000,
                        )
                        worker.evaluate(
                            "() => { __holdReviewAnswer = true; }"
                        )
                        review_handle = page.locator(
                            "#bw-reader-host"
                        ).locator(
                            ".rv-review-card .vc-card-hd"
                        )
                        review_box = review_handle.bounding_box()
                        assert review_box, "review card has no drag handle"
                        charging = page.evaluate(
                            """() => {
                              const root = document.querySelector(
                                '#bw-reader-host'
                              ).shadowRoot;
                              const handle = root.querySelector(
                                '.rv-review-card .vc-card-hd'
                              );
                              const card = root.querySelector(
                                '.rv-review-card'
                              );
                              const rect = handle.getBoundingClientRect();
                              const pointerId = 701;
                              window.__reviewDragPointer = {
                                pointerId,
                                startX: rect.left + 24,
                                startY: rect.top + 12
                              };
                              handle.dispatchEvent(new PointerEvent(
                                'pointerdown',
                                {
                                  pointerId,
                                  pointerType: 'mouse',
                                  button: 0,
                                  buttons: 1,
                                  bubbles: true,
                                  composed: true,
                                  cancelable: true,
                                  clientX: rect.left + 24,
                                  clientY: rect.top + 12
                                }
                              ));
                              return card.classList.contains(
                                'vc-drag-charging'
                              );
                            }"""
                        )
                        assert charging, "review card did not enter charged drag"
                        # Keep the production 420 ms charged-drag boundary.
                        # Use the same synthetic PointerEvent path as the
                        # dedicated card-drag browser test: native Xvfb mouse
                        # focus can emit a spurious window blur while held,
                        # which the production state machine correctly treats
                        # as a fail-closed cancellation.
                        # Wait for the runtime's observable ready state before
                        # moving, rather than weakening the hold threshold.
                        time.sleep(0.48)
                        page.wait_for_function(
                            """() => document.querySelector('#bw-reader-host')
                              ?.shadowRoot?.querySelector(
                                '.rv-review-card.vc-drag-ready'
                            ) !== null""",
                            timeout=5_000,
                        )
                        page.evaluate(
                            """() => {
                              const drag = window.__reviewDragPointer;
                              for (let step = 1; step <= 14; step++) {
                                const ratio = step / 14;
                                document.dispatchEvent(new PointerEvent(
                                  'pointermove',
                                  {
                                    pointerId: drag.pointerId,
                                    pointerType: 'mouse',
                                    button: 0,
                                    buttons: 1,
                                    bubbles: true,
                                    composed: true,
                                    cancelable: true,
                                    clientX: drag.startX +
                                      (280 - drag.startX) * ratio,
                                    clientY: drag.startY +
                                      (360 - drag.startY) * ratio
                                  }
                                ));
                              }
                              document.dispatchEvent(new PointerEvent(
                                'pointerup',
                                {
                                  pointerId: drag.pointerId,
                                  pointerType: 'mouse',
                                  button: 0,
                                  buttons: 0,
                                  bubbles: true,
                                  composed: true,
                                  cancelable: true,
                                  clientX: 280,
                                  clientY: 360
                                }
                              ));
                              delete window.__reviewDragPointer;
                            }"""
                        )
                        page_copy = page.locator(
                            "#bw-reader-pins"
                        ).locator(
                            ".bw-page-pin"
                        ).filter(
                            has_text="second candidate card"
                        )
                        page_copy.wait_for(state="visible", timeout=10_000)
                        page_copy.locator(
                            '[data-fc="reveal"]'
                        ).click()
                        page_copy.locator(
                            '.fc-e[data-ease="2"]'
                        ).click()
                        wait_until(
                            lambda: len(worker.evaluate(
                                "() => __candidateCalls.slice()"
                            )) == 3,
                            "page-copy review answer write",
                        )
                        pending = page.evaluate(
                            """() => {
                              const root = document.querySelector(
                                '#bw-reader-host'
                              ).shadowRoot;
                              const card = root.querySelector(
                                '.rv-review-card'
                              );
                              const pagePins = Array.from(
                                document.querySelector('#bw-reader-pins')
                                  .shadowRoot?.querySelectorAll(
                                    '.bw-page-pin'
                                  ) || []
                              );
                              return {
                                text: card?.innerText || '',
                                easeCount: card?.querySelectorAll(
                                  '.fc-e'
                                ).length || 0,
                                reviewCid: card?.dataset?.vcCid || '',
                                pagePins: pagePins.map(pin => ({
                                  text: pin.innerText,
                                  cid: pin.querySelector(
                                    '.vc-card'
                                  )?.dataset?.vcCid || ''
                                }))
                              };
                            }"""
                        )
                        assert pending["easeCount"] == 0, pending
                        assert "正在提交评分" in pending["text"], pending
                        assert len(worker.evaluate(
                            "() => __candidateCalls.slice()"
                        )) == 3
                        copy_answer_call = worker.evaluate(
                            "() => __candidateCalls.slice()[2]"
                        )
                        assert copy_answer_call["body"]["card_id"] == 11, (
                            copy_answer_call
                        )
                        assert "note_id" not in copy_answer_call["body"], (
                            copy_answer_call
                        )
                        worker.evaluate(
                            """() => {
                              if (typeof __releaseReviewAnswer !== 'function') {
                                throw new Error('missing answer release');
                              }
                              __releaseReviewAnswer();
                            }"""
                        )
                        page.wait_for_function(
                            """() => document.querySelector('#bw-reader-host')
                              ?.shadowRoot?.querySelector(
                                '#rc-review-body'
                              )?.innerText?.includes(
                                '当前这一批已经完成'
                              )""",
                            timeout=10_000,
                        )
                        assert len(worker.evaluate(
                            "() => __candidateCalls.slice()"
                        )) == 3
                    finally:
                        context.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    print(
        "PASS: contextual review uses POST, account-scoped extension cache, "
        "standard Anki reveal/rating, projected card HTML, exact-context "
        "reload cache and keeps host clicks working"
    )


if __name__ == "__main__":
    main()
