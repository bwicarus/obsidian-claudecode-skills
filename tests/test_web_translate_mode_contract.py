"""Contracts for ordinary-web AI translation mode selection.

These source-level checks protect the ownership boundary between the page and
the browser extension.  The page may discover its sentence count and report an
estimate, but only extension-owned preferences may select the outbound mode.
"""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
IMMERSIVE = ROOT / "_server_deploy/static/pdf/web-immersive.js"
SETTINGS = ROOT / "_server_deploy/static/pdf/rc-settings.js"


def _between(text: str, start: str, end: str) -> str:
    left = text.index(start)
    right = text.index(end, left + len(start))
    return text[left:right]


class WebTranslateModeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.immersive = IMMERSIVE.read_text(encoding="utf-8")
        cls.settings = SETTINGS.read_text(encoding="utf-8")

    def test_page_preferences_default_safely_and_clamp_the_auto_threshold(self):
        mode = _between(
            self.immersive,
            "function storedMode() {",
            "\n  function storedThreshold() {",
        )
        threshold = _between(
            self.immersive,
            "function storedThreshold() {",
            "\n  function validAiNamespace",
        )

        self.assertIn("'eph-web-tr-mode'", mode)
        self.assertIn("|| 'auto'", mode)
        self.assertIn("mode === 'session'", mode)
        self.assertIn("mode === 'stateless'", mode)
        self.assertIn("return 'auto'", mode)

        self.assertIn("'eph-web-tr-threshold'", threshold)
        self.assertIn("|| '50'", threshold)
        self.assertIn("Math.max(10, Math.min(500, n))", threshold)
        self.assertGreaterEqual(threshold.count("return 50"), 1)

    def test_initial_idle_discovery_counts_plain_text_without_sentence_rewrite(self):
        counter = _between(
            self.immersive,
            "function countTranslationBlock(el) {",
            "\n  function finishInitialDiscovery() {",
        )
        discovery = _between(
            self.immersive,
            "function scheduleBlockDiscovery(root) {",
            "\n  /** 生词提示独立于翻译开关",
        )

        self.assertIn("sentenceSpans(textMap(el).text).length", counter)
        self.assertNotIn("sentenceUnits(", counter)
        self.assertIn("__rcTranslationCounted", counter)

        self.assertIn("countTranslationBlock(el)", discovery)
        self.assertIn("_vric(slice)", discovery)
        self.assertIn("finishInitialDiscovery()", discovery)
        self.assertNotIn("sentenceUnits(", discovery)

    def test_auto_decision_uses_seventy_percent_once_then_freezes(self):
        decision = _between(
            self.immersive,
            "var PAGE_LENGTH = {",
            "\n  // ── 样式 ──",
        )

        self.assertIn("if (!PAGE_LENGTH.frozen)", decision)
        self.assertIn("PAGE_LENGTH.frozen = true", decision)
        self.assertIn(
            "PAGE_LENGTH.resolved = mode === 'session' ? 'session' : 'stateless'",
            decision,
        )
        self.assertIn("Math.round(PAGE_LENGTH.total * 0.7)", decision)
        self.assertIn("estimated > storedThreshold()", decision)
        self.assertNotIn("estimated * 0.7", decision)

        self.assertIn("if (!complete) return freezeTranslationMode('stateless')", decision)
        self.assertIn("if (PAGE_LENGTH.initialDone)", decision)
        self.assertIn("PAGE_LENGTH.waiters.push(done)", decision)
        self.assertIn("done(false)", decision)
        self.assertIn("1800", decision)

    def test_ai_request_carries_the_resolved_mode_and_uses_response_namespace(self):
        flush = _between(
            self.immersive,
            "function flush() {",
            "\n  // ── 未掌握词",
        )
        profile = _between(
            self.immersive,
            "function applyProfile(d) {",
            "\n  function ensureBackendProfile() {",
        )

        self.assertIn("payload.mode = ST.modeResolved", flush)
        self.assertIn("payload.estimatedUnits =", flush)
        self.assertIn("d.cacheNamespaces", flush)
        self.assertIn("d.modeResolved", flush)
        self.assertIn("ST.modeResolved = d.modeResolved", flush)
        self.assertIn("cacheFor(aiNs)[t]", flush)

        self.assertIn("d.cacheNamespaces", profile)
        self.assertIn("namespaces.stateless", profile)
        self.assertIn("namespaces.session", profile)
        self.assertIn("d.sessionSupported === true", profile)

    def test_settings_offer_all_three_modes_and_persist_the_threshold(self):
        fill = _between(
            self.settings,
            "function _fillWebPane() {",
            "\n  function _saveWebPane() {",
        )
        save = _between(
            self.settings,
            "function _saveWebPane() {",
            "\n  function _toggleSentAiRow()",
        )
        toggle = _between(
            self.settings,
            "function _toggleWebAiModeRows() {",
            "\n  function _fillWebPane() {",
        )

        self.assertIn('id="web-tr-mode"', self.settings)
        self.assertIn('<option value="auto">', self.settings)
        self.assertIn('<option value="stateless">', self.settings)
        self.assertIn('<option value="session">', self.settings)
        self.assertIn(
            'id="web-tr-threshold" type="number" min="10" max="500"',
            self.settings,
        )
        self.assertIn("预计阅读句数（总句数 × 70%）", self.settings)
        self.assertNotIn("短网页用短时会话，长网页用无状态批翻", self.settings)
        self.assertNotIn("≤ 此值时使用会话", self.settings)

        self.assertIn("'eph-web-tr-mode'", fill)
        self.assertIn("'eph-web-tr-threshold'", fill)
        self.assertIn("Math.max(10, Math.min(500, threshold))", fill)

        self.assertIn("lsSet('eph-web-tr-mode', newMode)", save)
        self.assertIn("lsSet('eph-web-tr-threshold', String(newThreshold))", save)
        self.assertIn("Math.max(10, Math.min(500, newThreshold))", save)

        self.assertIn("mode.value === 'auto'", toggle)
        self.assertIn("thresholdRow.style.display", toggle)


if __name__ == "__main__":
    unittest.main()
