"""Source contract for ordinary-web vocabulary scheduling.

The vocabulary pass used to be coupled to scrolling and could repeatedly do
DOM collection, sentence wrapping, and a whole response commit on the main
thread.  These tests intentionally guard the scheduling shape without fixing
implementation names, so refactors remain possible while the responsiveness
invariants stay explicit.
"""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
IMMERSIVE = ROOT / "_server_deploy/static/pdf/web-immersive.js"


def _between(text: str, start: str, end: str) -> str:
    left = text.index(start)
    right = text.index(end, left + len(start))
    return text[left:right]


def _early_vertical_margin(value: str) -> bool:
    """Return whether a root margin begins work materially before visibility."""

    parts = value.split()
    if not parts:
        return False
    vertical = [parts[0]]
    if len(parts) >= 3:
        vertical.append(parts[2])
    for token in vertical:
        match = re.fullmatch(r"(-?\d+(?:\.\d+)?)(px|%|vh)", token)
        if not match:
            continue
        size, unit = float(match.group(1)), match.group(2)
        if (unit == "px" and size >= 300) or (
            unit in {"%", "vh"} and size >= 25
        ):
            return True
    return False


def _vocab_observer_margins(source: str) -> list[str]:
    """Collect root margins from observers whose callback schedules vocab work."""

    margins: list[str] = []
    for found in re.finditer(r"new\s+IntersectionObserver\s*\(", source):
        tail = source[found.end() : found.end() + 3500]
        options = re.search(
            r"\}\s*,\s*\{\s*rootMargin\s*:\s*(['\"])(.*?)\1",
            tail,
            re.DOTALL,
        )
        if not options:
            continue
        callback = tail[: options.start()]
        if re.search(
            r"(?:vocab|scanVocab|queueVocab|scheduleVocab)",
            callback,
            re.IGNORECASE,
        ):
            margins.append(options.group(2))
    return margins


def _filtered_before_batch(source: str) -> bool:
    """Recognize either a filter/slice chain or a nearby named filtered queue."""

    for found in re.finditer(r"\.filter\s*\(", source):
        window = source[found.start() : found.start() + 900]
        state = window.find("__rcVocab")
        if state < 0:
            continue
        batches = [
            pos
            for pos in (
                window.find(".slice(", state),
                window.find(".splice(", state),
            )
            if pos >= 0
        ]
        if batches:
            return True
    return False


class WebVocabSchedulerContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = IMMERSIVE.read_text(encoding="utf-8")

    def test_scroll_does_not_debounce_into_a_full_vocab_pass(self):
        self.assertNotRegex(
            self.source,
            r"addEventListener\(\s*['\"]scroll['\"][\s\S]{0,700}"
            r"\b(?:autoVocab|vocabPass)\s*\(",
        )
        self.assertNotIn(
            "setTimeout(function () { t = null; autoVocab(); }, 700)",
            self.source,
        )

    def test_vocab_has_its_own_early_viewport_observer(self):
        margins = _vocab_observer_margins(self.source)
        self.assertTrue(
            margins,
            "expected a vocabulary-specific IntersectionObserver",
        )
        self.assertTrue(
            any(_early_vertical_margin(value) for value in margins),
            f"vocabulary observer must scan ahead of the viewport, got {margins}",
        )

    def test_unscanned_units_are_filtered_before_the_scan_batch_is_cut(self):
        self.assertNotRegex(
            self.source,
            r"scanVocab\s*\(\s*(?:vis|units|list)\s*\.slice\s*\(",
        )
        self.assertTrue(
            _filtered_before_batch(self.source),
            "filter !__rcVocab before slice/splice so scanned leading units "
            "cannot starve later sentences",
        )

    def test_vocab_results_commit_in_small_animation_frame_batches(self):
        commit = _between(
            self.source,
            "function commitVocabItems(items, els, reqs, done) {",
            "\n  function scanVocab(list) {",
        )
        size = re.search(
            r"Math\.min\(\s*at\s*\+\s*(\d+)\s*,\s*items\.length\s*\)",
            commit,
        )
        self.assertIsNotNone(size, "commit helper must explicitly cap each frame")
        self.assertLessEqual(int(size.group(1)), 24)
        self.assertGreaterEqual(commit.count("_raf(frame)"), 2)
        self.assertIn("underlineWords(el, m.occurrences || [])", commit)

    def test_sentence_translate_button_has_an_explicit_immediate_path(self):
        click = _between(
            self.source,
            "b.onclick = function (ev) {",
            "\n        };",
        )
        self.assertNotIn("setTimeout", click)
        self.assertNotRegex(click, r"\b(?:60|220)\b")
        self.assertIn("showHotTranslation(this.parentElement, this)", click)

        hot = _between(
            self.source,
            "function showHotTranslation(el, button) {",
            "\n  function commitVocabItems(",
        )
        self.assertIn("cacheFor(GOOGLE_NS)[text]", hot)
        self.assertIn("attach(el, hit)", hot)
        self.assertIn("loadingSlot(el)", hot)
        self.assertIn("enqueue(el, true)", hot)

        enqueue = _between(
            self.source,
            "function enqueue(el, force) {",
            "\n  function flush() {",
        )
        force = _between(enqueue, "if (force) {", "\n    }")
        self.assertIn("clearTimeout(PENDT)", force)
        self.assertIn("flush()", force)
        self.assertNotIn("setTimeout", force)


if __name__ == "__main__":
    unittest.main()
