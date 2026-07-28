#!/usr/bin/env python3
"""真实 Chromium 契约：最近 caret 不能等同于真实文字命中。"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[2]
UI_SOURCE = (ROOT / "_server_deploy/static/pdf/rc-ui.js").read_text(encoding="utf-8")
CHROME = Path.home() / ".cache/ms-playwright/chromium-1223/chrome-linux/chrome"


def main() -> int:
    with sync_playwright() as playwright:
        launch = {"headless": True}
        if CHROME.exists():
            launch["executable_path"] = str(CHROME)
        browser = playwright.chromium.launch(**launch)
        try:
            page = browser.new_page(viewport={"width": 800, "height": 400})
            page.set_content(
                """
                <style>
                  body { margin: 0 }
                  #text { box-sizing: border-box; width: 600px; height: 180px; margin: 20px;
                    padding: 0; background: #eee; font: 24px/32px sans-serif }
                </style>
                <p id="text">nearestword</p>
                """
            )
            page.add_script_tag(content=UI_SOURCE)
            result = page.evaluate(
                """
                () => {
                  const block = document.querySelector('#text');
                  const node = block.firstChild;
                  const word = document.createRange();
                  word.setStart(node, 0);
                  word.setEnd(node, node.nodeValue.length);
                  const wr = word.getBoundingClientRect();
                  const br = block.getBoundingClientRect();
                  const blankX = br.right - 30;
                  const blankY = br.bottom - 20;
                  const caret = document.caretRangeFromPoint(blankX, blankY);
                  return {
                    caretNodeIsWord: caret && caret.startContainer === node,
                    caretOffset: caret && caret.startOffset,
                    glyphMouse: RC.ui.rangeHitTest(
                      word, wr.left + wr.width / 2, wr.top + wr.height / 2,
                      {pointerType: 'mouse'}),
                    glyphTouch: RC.ui.rangeHitTest(
                      word, wr.left + wr.width / 2, wr.top + wr.height / 2,
                      {pointerType: 'touch'}),
                    blankMouse: RC.ui.rangeHitTest(
                      word, blankX, blankY, {pointerType: 'mouse'}),
                    blankTouch: RC.ui.rangeHitTest(
                      word, blankX, blankY, {pointerType: 'touch'}),
                  };
                }
                """
            )
        finally:
            browser.close()

    assert result["caretNodeIsWord"] is True
    assert result["caretOffset"] == len("nearestword")
    assert result["glyphMouse"] is True
    assert result["glyphTouch"] is True
    assert result["blankMouse"] is False
    assert result["blankTouch"] is False
    print("Text range hit browser contract: 6 assertions passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
