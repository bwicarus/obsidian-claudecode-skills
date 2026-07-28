"""Review drawer → shared card-improvement workspace contract."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))

import pdf_reader  # noqa: E402
import html_reader  # noqa: E402


class CardImprovementActionContractTest(unittest.TestCase):
    def test_server_emits_transport_only_action(self) -> None:
        old = os.environ.get("QA_PUBLIC_URL")
        os.environ["QA_PUBLIC_URL"] = "https://bw.example/qa"
        try:
            meta = pdf_reader._anki_review_card_meta({
                "answer": (
                    "A<!--@src:book:资源/books/math.pdf#p12-->"
                    "<!--@entity:card_a1b2c3:2-->"
                ),
            })
        finally:
            if old is None:
                os.environ.pop("QA_PUBLIC_URL", None)
            else:
                os.environ["QA_PUBLIC_URL"] = old

        action = meta["improvement_action"]
        self.assertEqual(action["contract"], "card-improvement-action/1")
        self.assertEqual(action["delivery"], "workspace")
        self.assertEqual(action["selection"], "workspace")
        self.assertEqual(action["modes"], ["verbose", "concise"])
        self.assertEqual(action["entity_id"], "card_a1b2c3")
        self.assertEqual(action["entity_index"], 2)
        self.assertEqual(
            action["source_ref"],
            "book:资源/books/math.pdf#p12",
        )
        self.assertNotIn("prompt", action)
        self.assertNotIn("mutation", action)

    def test_browser_contract_preserves_identity_source_and_mode(self) -> None:
        contract = (
            ROOT
            / "_server_deploy"
            / "static"
            / "reader-runtime"
            / "card-improvement-actions.js"
        )
        node = f"""
global.window = {{
  RC: {{}},
  location: {{href: 'https://reader.example/pdf/view'}}
}};
require({json.dumps(str(contract))});
const url = window.RC.cardImprovementActions.workspaceUrl({{
  improvement_action: {{
    contract: 'card-improvement-action/1',
    workspace_url: 'https://bw.example/qa/?card=old',
    entity_id: 'card_a1b2c3',
    entity_index: 2,
    source_ref: 'book:资源/books/math.pdf#p12'
  }}
}}, 'concise');
process.stdout.write(url);
"""
        result = subprocess.run(
            ["node", "-e", node],
            check=True,
            capture_output=True,
            text=True,
        )
        from urllib.parse import parse_qs, urlsplit

        params = parse_qs(urlsplit(result.stdout).query)
        self.assertEqual(params["card"], ["card_a1b2c3"])
        self.assertEqual(params["index"], ["2"])
        self.assertEqual(params["source"], ["book:资源/books/math.pdf#p12"])
        self.assertEqual(params["verbosity"], ["concise"])
        self.assertEqual(params["entry"], ["review"])

    def test_review_ui_is_embedded_in_assistant_and_uses_draft_commit(self) -> None:
        source = (
            ROOT / "_server_deploy" / "static" / "pdf" / "rc-review.js"
        ).read_text("utf-8")
        flashcard_source = (
            ROOT / "_server_deploy" / "static" / "pdf" / "rc-flashcard.js"
        ).read_text("utf-8")

        # 复习是助手 pane 内的模式：卡片区插在完整聊天 thread 上方，
        # 不再创建第五个独立 tab/pane。
        self.assertIn("document.getElementById('side-pane-asst')", source)
        self.assertIn("document.getElementById('asst-quick')", source)
        self.assertIn("document.getElementById('asst-thread')", source)
        self.assertIn("workspace.id = 'asst-review-workspace'", source)
        self.assertIn("pane.insertBefore(workspace, thread)", source)
        self.assertNotIn("side-tab-review", source)
        self.assertNotIn("side-pane-review", source)

        # 复习保留标准 Anki 流程：初始只显示正面，用户显式揭示后才
        # 显示背面和四档评分。改进写入仍必须
        # prepare → preview → explicit commit，不在浏览器里直接改旧卡。
        # 队列外壳只负责共享 pager；每张 slide 仍通过唯一的
        # flashcard.renderEntity 入口渲染，不能退回 review 私有卡面。
        self.assertIn("_renderReviewSlide(slide, queuedCard, index)", source)
        self.assertIn("RC.flashcard.renderEntity(host", source)
        self.assertIn("pager: false", source)
        self.assertIn("RC.flashcard.bindPager(panel", source)
        self.assertIn("slides: slides", source)
        self.assertIn("dots: dots", source)
        self.assertNotIn("RC.flashcard.renderEntity(panel", source)
        self.assertIn("mode: 'review'", source)
        self.assertRegex(
            source,
            r"onReveal:\s*function \(\) \{\s*"
            r"if \(index !== _idx\) \{\s*"
            r"_selectCard\(index, 'card-reveal'\);\s*return;\s*\}\s*"
            r"_showAnswer\(\);\s*\}",
        )
        self.assertRegex(
            source,
            r"onRate:\s*function \(ease\) \{\s*"
            r"if \(index === _idx\) _answerCurrent\(ease\);\s*\}",
        )
        self.assertNotIn("function _appendFace(", source)
        self.assertIn("function mountReview(", flashcard_source)
        self.assertIn("function renderEntity(", flashcard_source)
        for label in (
            "打开原笔记",
            "📝 详细",
            "✂️ 精炼",
            "更新到笔记",
            "根据此改进 Anki",
            "全部更新",
            "草稿预览（尚未写入）",
        ):
            self.assertIn(label, source)
        for label in ("显示答案", "'再来'", "'困难'", "'良好'", "'简单'"):
            self.assertIn(label, flashcard_source)
        self.assertIn("/api/assistant/card-improvement-draft", source)
        self.assertIn("/api/assistant/card-improvement-commit", source)
        self.assertIn("window.confirm(", source)
        self.assertIn("确认写入 Anki 新卡", source)
        self.assertIn("确认更新原笔记", source)
        self.assertNotIn("api/card-update", source)
        self.assertNotIn("RC.cardImprovementActions.openWorkspace", source)

        # 评分必须走真实 review-answer，网络失败才进入耐久 outbox；
        # HTTP/业务拒绝则恢复乐观移出的卡，不能伪装成离线成功。
        self.assertIn("!_showingAnswer || ease < 1 || ease > 4", source)
        self.assertIn("data-ease", flashcard_source)
        self.assertIn("'/pdf/api/review-answer'", source)
        self.assertIn("function _restoreRejectedAnswer(", source)
        self.assertIn("error.name === 'TypeError'", source)
        self.assertIn("RC.outbox.send(", source)
        self.assertIn("'rev'", source)

    def test_review_selection_contract_preserves_parent_coverage(self) -> None:
        source = (
            ROOT / "_server_deploy" / "static" / "pdf" / "rc-review.js"
        ).read_text("utf-8")
        self.assertIn("answer.querySelectorAll('p,li,h1,h2,h3,h4')", source)
        self.assertIn("parentId: answerId", source)
        self.assertIn("covers: childIds", source)
        self.assertIn("registry.snapshot({ maxText: 20000, limit: 120 })", source)
        self.assertIn("item.kind === 'review-answer'", source)
        self.assertIn("item.kind === 'review-answer-segment'", source)

    def test_review_buttons_use_delegated_events_in_extension_safe_path(self) -> None:
        source = (
            ROOT / "_server_deploy" / "static" / "pdf" / "rc-review.js"
        ).read_text("utf-8")
        self.assertIn("quick.addEventListener('click'", source)
        self.assertIn("workspace.addEventListener('click', _workspaceClick)", source)
        self.assertIn("thread.addEventListener('click', _threadClick)", source)
        self.assertIn("button.setAttribute('data-action', action)", source)
        self.assertIsNone(re.search(r"\bonclick\s*=", source))
        self.assertNotIn("setAttribute('onclick'", source)

    def test_assistant_frontend_separates_mode_history_clear_and_stale_io(self) -> None:
        source = (
            ROOT / "_server_deploy" / "static" / "pdf" / "rc-assistant.js"
        ).read_text("utf-8")

        self.assertIn(
            "'/api/assistant/history?assistant_mode=review'",
            source,
        )
        self.assertIn(
            "function _clearUrl(mode) { return _modeNorm(mode || "
            "_assistantMode) === 'review' ? '/api/assistant/clear' : "
            "_NORMAL_CLEARURL; }",
            source,
        )
        self.assertIn("c.assistant_mode = _assistantMode", source)
        self.assertIn("assistant_mode: turnMode", source)
        self.assertIn("assistant_mode: 'review'", source)

        # 模式切换清掉共享 thread 的旧 DOM，再只加载新域历史；两个 epoch
        # 同时围住历史回包与在途流，旧响应不能污染当前模式。
        self.assertIn("_modeEpoch++; _historyEpoch++", source)
        self.assertIn("thread.innerHTML = ''", source)
        self.assertIn("loadHistory(mode)", source)
        self.assertIn("if (turnEpoch !== _modeEpoch)", source)
        self.assertIn(
            "histToken !== _historyEpoch || modeEpoch !== _modeEpoch || "
            "mode !== _assistantMode",
            source,
        )
        self.assertIn("var clearMode = _assistantMode", source)
        self.assertIn("fetch(_clearUrl(clearMode), clearOpts)", source)
        self.assertIn("只清空复习对话（普通助手记录保留）", source)
        self.assertIn("只清空普通助手对话（复习记录保留）", source)
        self.assertIn(
            "RC.review.mode && RC.review.mode() === 'review'",
            source,
        )

        review = (
            ROOT / "_server_deploy" / "static" / "pdf" / "rc-review.js"
        ).read_text("utf-8")
        self.assertIn("'rc:assistant-mode-request'", review)
        self.assertIn("'rc:assistant-mode-changed'", review)

        # 复习域的清空不能触发普通语音助手的长期记忆 cutoff 或 S2S
        # 会话重置；这两个旁听器也必须检查当前助手模式。
        voice = (
            ROOT / "_server_deploy" / "static" / "pdf" / "rc-voicecall.js"
        ).read_text("utf-8")
        self.assertGreaterEqual(
            voice.count("RC.assistant.getMode() === 'review'"),
            2,
        )

    def test_old_workspace_accepts_review_mode_and_untrusted_source_hint(self) -> None:
        source = (ROOT / "_client" / "core" / "qa_browser.py").read_text("utf-8")
        self.assertIn("get('verbosity') === 'concise'", source)
        self.assertIn("c.entry_source_ref = entrySource", source)
        self.assertIn("服务端按 entity_id/index", source)
        self.assertNotIn("s-delete-original", source)
        self.assertNotIn("card_qa.delete_original", source)
        self.assertIn("卡片改进始终保留原卡", source)

    def test_load_order_places_contract_before_review(self) -> None:
        manifest = json.loads(
            (ROOT / "extensions" / "bw-reader-webext" / "manifest.json")
            .read_text("utf-8")
        )
        scripts = manifest["content_scripts"][1]["js"]
        self.assertLess(
            scripts.index("vendor/reader-runtime-card-improvement-actions.js"),
            scripts.index("vendor/rc-review.js"),
        )
        template = (
            ROOT / "_server_deploy" / "templates" / "pdf_reader.html"
        ).read_text("utf-8")
        self.assertLess(
            template.index("card-improvement-actions.js"),
            template.index("rc-review.js"),
        )
        epub_template = (
            ROOT / "_server_deploy" / "templates" / "epub_html_reader.html"
        ).read_text("utf-8")
        self.assertIn("rc-review.js", epub_template)
        self.assertLess(
            epub_template.index("rc-review.js"),
            epub_template.index("rc-assistant.js"),
        )
        html_template = (
            ROOT / "_server_deploy" / "templates" / "html_reader.html"
        ).read_text("utf-8")
        self.assertIn("rc-review.js", html_template)
        self.assertLess(
            html_template.index("rc-review.js"),
            html_template.index("rc-assistant.js"),
        )
        self.assertIn("pdf/rc-review.js", pdf_reader._EPUB_CACHE_ASSETS)
        self.assertIn("pdf/rc-review.js", html_reader._HTML_CACHE_ASSETS)


if __name__ == "__main__":
    unittest.main()
