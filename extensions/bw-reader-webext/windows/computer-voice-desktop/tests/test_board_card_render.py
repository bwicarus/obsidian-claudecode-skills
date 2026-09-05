# -*- coding: utf-8 -*-
"""board_card_render 的纯函数部分（不起浏览器）。

钉的是 2026-09-05 第二版的三件事：每张卡两种形状、文件名带形状、旧格式的清理时机。
渲染本身（Playwright）不在这里测 —— 那要装 Chromium，属于安装后的现场验收。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import board_card_render as bcr  # noqa: E402


def _store(root: Path, cards: list[tuple[str, str]]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / bcr.STORE_NAME).write_text(json.dumps({
        "contract": bcr.CONTRACT,
        "boards": [{
            "code": "bd_test",
            "cards": [{"id": cid, "html": html, "sha": bcr.sha_of(html)}
                      for cid, html in cards],
        }],
    }, ensure_ascii=False), encoding="utf-8")


class ShapesTest(unittest.TestCase):
    def test_two_shapes_square_and_wide(self) -> None:
        self.assertEqual(set(bcr.SHAPES), {"square", "wide"})
        self.assertEqual(bcr.SHAPES["square"], (320, 320))
        self.assertEqual(bcr.SHAPES["wide"], (640, 320))

    def test_png_name_carries_shape(self) -> None:
        root = Path("/tmp/x")
        self.assertEqual(bcr.card_png_path("ab" * 8, "wide", root).name, "ab" * 8 + ".wide.png")
        self.assertEqual(bcr.card_png_path("ab" * 8, root=root).name, "ab" * 8 + ".square.png")
        with self.assertRaises(ValueError):
            bcr.card_png_path("ab" * 8, "tall", root)

    def test_fit_script_only_scales_up(self) -> None:
        # 只放大不缩小，且有上限 —— 缩小等于替 AI 把字缩小，违背"写不下就拆卡"。
        self.assertIn("if (!fits(1))", bcr._FIT_SCRIPT)
        self.assertIn("style.zoom", bcr._FIT_SCRIPT)
        self.assertGreaterEqual(bcr.MAX_ZOOM, 2.0)
        self.assertIn('id="bw"', bcr._document("<b>x</b>"))


class PendingTest(unittest.TestCase):
    def test_pending_lists_missing_shapes_per_card(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _store(root, [("a", "<b>A</b>"), ("b", "<b>B</b>")])
            sha_a = bcr.sha_of("<b>A</b>")
            pending = bcr.pending_cards(root)
            self.assertEqual(sorted((p[0], sorted(p[2])) for p in pending),
                             sorted([(bcr.sha_of("<b>A</b>"), ["square", "wide"]),
                                     (bcr.sha_of("<b>B</b>"), ["square", "wide"])]))
            # 方卡渲好了，就只差宽卡。
            bcr.cache_dir(root).mkdir(parents=True)
            bcr.card_png_path(sha_a, "square", root).write_bytes(b"png")
            pending = {p[0]: p[2] for p in bcr.pending_cards(root)}
            self.assertEqual(pending[sha_a], ["wide"])

    def test_unreadable_store_means_nothing_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(bcr.pending_cards(Path(tmp)), [])


class PruneTest(unittest.TestCase):
    def test_legacy_png_removed_only_after_both_new_shapes_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _store(root, [("a", "<b>A</b>")])
            sha = bcr.sha_of("<b>A</b>")
            bcr.cache_dir(root).mkdir(parents=True)
            legacy = bcr.legacy_png_path(sha, root)
            legacy.write_bytes(b"old")
            # 新图还没齐：旧图留着（渲染失败时不能连旧图也没了）。
            bcr.card_png_path(sha, "square", root).write_bytes(b"png")
            self.assertEqual(bcr.prune(root), 0)
            self.assertTrue(legacy.exists())
            bcr.card_png_path(sha, "wide", root).write_bytes(b"png")
            self.assertEqual(bcr.prune(root), 1)
            self.assertFalse(legacy.exists())
            self.assertTrue(bcr.card_png_path(sha, "square", root).exists())
            self.assertTrue(bcr.card_png_path(sha, "wide", root).exists())

    def test_orphan_shapes_removed_but_live_ones_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _store(root, [("a", "<b>A</b>")])
            bcr.cache_dir(root).mkdir(parents=True)
            gone = "0" * 16
            bcr.card_png_path(gone, "square", root).write_bytes(b"png")
            bcr.card_png_path(gone, "wide", root).write_bytes(b"png")
            live = bcr.sha_of("<b>A</b>")
            bcr.card_png_path(live, "wide", root).write_bytes(b"png")
            self.assertEqual(bcr.prune(root), 2)
            self.assertTrue(bcr.card_png_path(live, "wide", root).exists())

    def test_prune_refuses_when_store_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bcr.cache_dir(root).mkdir(parents=True)
            bcr.card_png_path("f" * 16, "square", root).write_bytes(b"png")
            self.assertEqual(bcr.prune(root), 0)
            self.assertTrue(bcr.card_png_path("f" * 16, "square", root).exists())


if __name__ == "__main__":
    unittest.main()
