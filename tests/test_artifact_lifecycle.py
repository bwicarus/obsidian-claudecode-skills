#!/usr/bin/env python3
"""生成物生命周期的判定与归档。

这个脚本会碰**用户数据**，所以两条最重要的性质要钉死：

* 默认 dry-run —— 不带 --archive 时不动任何文件；
* 归档不是删除 —— 冷条目原样进 `state/cold-archive/`，随时可取回。

以及那条最容易出错的：**引用判定少认一处就会误归档用户还在用的东西**。
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import artifact_lifecycle as AL  # noqa: E402


NOW = 1_756_000_000
DAY = 86400


def _reg(entries: dict) -> dict:
    return {"acct-v1-" + "a" * 8: entries}


class ClassifyTests(unittest.TestCase):
    def test_pinned_is_kept_however_old(self):
        """已贴页 = 用户把它固定下来了。多老都不能碰。"""
        reg = _reg({"img_aaa": {"kind": "img", "ts": NOW - 999 * DAY, "local": "x.png"}})
        keep, cold = AL.classify(reg, "", 90, now=NOW)
        self.assertEqual(cold, [])
        self.assertEqual(keep[0]["why"], "已贴页（local）")

    def test_referenced_is_kept_however_old(self):
        """被便签/高亮引用着的，同样不能碰。"""
        reg = _reg({"img_bbb": {"kind": "img", "ts": NOW - 999 * DAY, "local": ""}})
        corpus = "看这张图 #img_bbb 讲得很清楚"
        keep, cold = AL.classify(reg, corpus, 90, now=NOW)
        self.assertEqual(cold, [])
        self.assertEqual(keep[0]["why"], "被持久记录引用")

    def test_fresh_is_kept(self):
        reg = _reg({"img_ccc": {"kind": "img", "ts": NOW - 3 * DAY, "local": ""}})
        keep, cold = AL.classify(reg, "", 90, now=NOW)
        self.assertEqual(cold, [])
        self.assertEqual(keep[0]["why"], "还不够老")

    def test_only_all_three_misses_goes_cold(self):
        """三条判据全不命中才算冷 —— 这是唯一会被归档的情形。"""
        reg = _reg({"img_ddd": {"kind": "img", "ts": NOW - 200 * DAY, "local": ""}})
        keep, cold = AL.classify(reg, "", 90, now=NOW)
        self.assertEqual(keep, [])
        self.assertEqual(len(cold), 1)
        self.assertEqual(cold[0]["id"], "img_ddd")

    def test_flat_and_partitioned_registries_both_read(self):
        """注册表历史上有平铺和按 identity 分区两种形态，都要认。"""
        flat = {"img_eee": {"kind": "img", "ts": NOW - 200 * DAY, "local": ""}}
        _keep, cold = AL.classify(flat, "", 90, now=NOW)
        self.assertEqual(len(cold), 1, "平铺形态没被识别 —— 会漏判整份注册表")


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bw-lifecycle-"))
        self.reg_path = self.tmp / "registry.json"
        self.archive_root = self.tmp / "cold-archive"
        self.entries = {
            "img_keep": {"kind": "img", "ts": NOW, "local": "a.png"},
            "img_cold": {"kind": "img", "ts": NOW - 200 * DAY, "local": ""},
        }
        self.reg_path.write_text(
            json.dumps(_reg(self.entries), ensure_ascii=False), encoding="utf-8")

    def test_archive_moves_cold_and_keeps_the_rest(self):
        reg = AL._load_registry(self.reg_path)
        _keep, cold = AL.classify(reg, "", 90, now=NOW)
        out = AL.archive(cold, reg_path=self.reg_path,
                         archive_root=self.archive_root, now=NOW)
        self.assertIsNotNone(out)

        live = AL._load_registry(self.reg_path)
        live_ids = {aid for aid, _ in AL._iter_entries(live)}
        self.assertIn("img_keep", live_ids, "保留项不能被摘掉")
        self.assertNotIn("img_cold", live_ids, "冷项应当从活的那份里移出")

    def test_archive_never_deletes(self):
        """⚠ 最重要的一条：冷条目原样躺在归档里，随时可取回。"""
        reg = AL._load_registry(self.reg_path)
        _keep, cold = AL.classify(reg, "", 90, now=NOW)
        out = AL.archive(cold, reg_path=self.reg_path,
                         archive_root=self.archive_root, now=NOW)
        moved = json.loads((out / "cold.json").read_text(encoding="utf-8"))
        self.assertEqual(
            moved["img_cold"], self.entries["img_cold"],
            "归档里必须是**原样**的条目 —— 采集不可重来，删错了没有第二次机会")

    def test_archive_writes_forensic_snapshot_before_pruning(self):
        """先存整份快照再摘 —— 崩在中间时宁可多一份副本，不要活的那份已经少了。"""
        reg = AL._load_registry(self.reg_path)
        _keep, cold = AL.classify(reg, "", 90, now=NOW)
        out = AL.archive(cold, reg_path=self.reg_path,
                         archive_root=self.archive_root, now=NOW)
        before = json.loads((out / "registry.before.json").read_text(encoding="utf-8"))
        ids = {aid for aid, _ in AL._iter_entries(before)}
        self.assertEqual(ids, {"img_keep", "img_cold"}, "快照必须是摘之前的全量")

    def test_nothing_cold_means_nothing_touched(self):
        out = AL.archive([], reg_path=self.reg_path,
                         archive_root=self.archive_root, now=NOW)
        self.assertIsNone(out)
        self.assertFalse(self.archive_root.exists(), "没有冷条目时不该建归档目录")


class ContractTests(unittest.TestCase):
    def test_reference_sources_cover_every_persistent_kind(self):
        """⚠ 这张表少一处就会误归档用户还在用的东西。

        代价完全不对称：少认一处 = 删（归档）掉在用的；多认一处 = 少归档一点。
        所以新增一类持久记录时必须同时加进来。
        """
        labels = {label for label, _ in AL.REFERENCE_SOURCES}
        for must in ("便签", "插入页", "收藏夹", "高亮", "墨迹", "卡片"):
            self.assertIn(must, labels, f"引用来源少了「{must}」—— 会误归档")

    def test_default_is_dry_run(self):
        source = Path(AL.__file__).read_text(encoding="utf-8")
        self.assertIn('action="store_true"', source)
        self.assertIn("只报告，什么都没动", source,
                      "默认必须是只读的，并且要明确告诉用户什么都没动")


if __name__ == "__main__":
    unittest.main()
