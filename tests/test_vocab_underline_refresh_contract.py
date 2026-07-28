"""Regression contract for ordinary-web vocabulary underline refreshes.

The visual failure was caused by clearing every CSS Highlight before an
asynchronous `/web-vocab` rescan.  These assertions deliberately guard the
source-level ownership boundaries as well as the user-visible invariants.
"""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
IMMERSIVE = ROOT / "_server_deploy/static/pdf/web-immersive.js"
WORDPOP = ROOT / "_server_deploy/static/pdf/rc-wordpop.js"


def _between(text: str, start: str, end: str) -> str:
    left = text.index(start)
    right = text.index(end, left + len(start))
    return text[left:right]


class VocabUnderlineRefreshContractTests(unittest.TestCase):
    def test_full_refresh_keeps_old_ranges_until_each_sentence_is_reconciled(self):
        src = IMMERSIVE.read_text(encoding="utf-8")
        refresh = _between(
            src,
            "window.refreshVocabUnderlinesForAllPages = function () {",
            "\n  // ── 语言筛选",
        )
        underline = _between(
            src,
            "function underlineWords(el, occ) {",
            "\n  // ── 标记掌握",
        )

        self.assertNotIn(".clear(", refresh)
        self.assertIn("__rcVocab = 0", refresh)
        self.assertIn("vocabPass();", refresh)
        self.assertNotIn("if (!occ || !occ.length) return", underline)
        self.assertIn("next.forEach", underline)
        self.assertIn("(el.__rcVocabRanges || []).forEach", underline)
        self.assertLess(
            underline.index("next.forEach"),
            underline.index("(el.__rcVocabRanges || []).forEach"),
        )

    def test_mastery_removal_is_lemma_aware_and_blocks_late_scan_results(self):
        src = IMMERSIVE.read_text(encoding="utf-8")
        drop = _between(
            src,
            "function _dropVocabUnderline() {",
            "\n  // 乐观分支",
        )
        commit = _between(
            src,
            "function commitVocabItems(items, els, reqs, done) {",
            "\n  function scanVocab(list) {",
        )
        scan = _between(src, "function scanVocab(list) {", "\n  function report()")

        self.assertIn("p.lemma", drop)
        self.assertIn("data-rc-vocab-lemma", drop)
        self.assertIn("VMASTER", src)
        self.assertIn("vocabOccurrenceHidden", src)
        self.assertIn("el.__rcVocabReq !== reqs[m.i]", commit)
        self.assertIn("commitVocabItems(items, els, reqs, resolve)", scan)

    def test_lookup_refreshes_are_debounced_and_full_modal_mastery_is_targeted(self):
        src = WORDPOP.read_text(encoding="utf-8")

        self.assertIn("function _scheduleUnderlineRefresh(delay)", src)
        self.assertIn("_scheduleUnderlineRefresh(80)", src)
        self.assertIn("_scheduleUnderlineRefresh(120)", src)
        self.assertIn("_refreshUnderlines(lemma, true, meta)", src)
        self.assertIn("state.setMastered(", src)
        self.assertIn("本地已保存，服务器同步失败", src)
        self.assertNotIn("s.mastered = prev", src)
        self.assertNotIn(
            "setTimeout(function () { _refreshUnderlines(); }, 1800)", src
        )
        self.assertNotIn(
            "setTimeout(function () { _refreshUnderlines(); }, 3500)", src
        )
        self.assertNotIn(
            "setTimeout(function () { _refreshUnderlines(); }, 1500)", src
        )

    def test_preexisting_pwa_word_pop_always_gets_action_delegation(self):
        """PDF/EPUB templates own #word-pop; shared controls must bind it too."""
        src = WORDPOP.read_text(encoding="utf-8")
        ensure = _between(
            src,
            "function _ensurePop() {",
            "\n\n  // ── 待查词呼吸高亮",
        )

        self.assertIn("var p = document.getElementById('word-pop')", ensure)
        self.assertIn("_bindWpDelegate(p);", ensure)
        self.assertGreater(
            ensure.index("_bindWpDelegate(p);"),
            ensure.index("if (!p)"),
        )
        # The binding must not remain inside the creation-only branch.
        self.assertIn(
            "\n    }\n",
            ensure[ensure.index("if (!p)") : ensure.index("_bindWpDelegate(p);")],
        )


if __name__ == "__main__":
    unittest.main()
