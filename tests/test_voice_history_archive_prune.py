"""归档到顶要裁最老的,不是抛错停摆(2026-09-08 用户实锤:侧栏聊天记录静止)。"""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "extensions" / "bw-reader-webext" / "windows" / "computer-voice-desktop"
if str(DESKTOP) not in sys.path:
    sys.path.insert(0, str(DESKTOP))

import voice_history_sidebar_sync as sync  # noqa: E402


def thread(n, gaps=None):
    return {"items": [{"role": "user", "text": f"t{i}"} for i in range(n)],
            "gaps": list(gaps or [])}


class ArchivePruneTests(unittest.TestCase):
    def test_under_limit_is_untouched(self):
        archive = {"threads": {"a": thread(10), "b": thread(20)}}
        self.assertEqual(sync._prune_archive(archive), 0)
        self.assertEqual(len(archive["threads"]["a"]["items"]), 10)

    def test_prunes_biggest_thread_down_to_target_and_keeps_the_tail(self):
        archive = {"threads": {"big": thread(9500), "active": thread(600)}}
        removed = sync._prune_archive(archive)
        total = sum(len(t["items"]) for t in archive["threads"].values())
        self.assertEqual(total, sync.ARCHIVE_PRUNE_TARGET)
        self.assertEqual(removed, 10100 - sync.ARCHIVE_PRUNE_TARGET)
        # 正在进行的那条一条都不该丢
        self.assertEqual(len(archive["threads"]["active"]["items"]), 600)
        # 裁的是头部:尾部原样保留,去重比对才认得出重叠
        self.assertEqual(archive["threads"]["big"]["items"][-1]["text"], "t9499")

    def test_gap_indexes_follow_the_cut(self):
        t = thread(300, gaps=[10, 250])
        sync._drop_thread_head(t, 100)
        self.assertEqual(len(t["items"]), 200)
        self.assertEqual(t["items"][0]["text"], "t100")
        self.assertEqual(t["gaps"], [150], "落在被裁段里的断点丢弃,其余左移")

    def test_每条对话都留够尾部才停手(self):
        many = {f"t{i}": thread(sync.MIN_THREAD_TAIL_ITEMS) for i in range(60)}
        archive = {"threads": many}
        sync._prune_archive(archive)   # 全都在保底线上 → 裁不动也不能死循环
        for t in archive["threads"].values():
            self.assertEqual(len(t["items"]), sync.MIN_THREAD_TAIL_ITEMS)

    def test_merge_recent_no_longer_raises_at_the_ceiling(self):
        t = thread(sync.MAX_ARCHIVE_ITEMS)
        sync._merge_recent(t, [{"role": "user", "text": "new"}])
        self.assertLessEqual(len(t["items"]), sync.ARCHIVE_PRUNE_TARGET + 1)
        self.assertEqual(t["items"][-1]["text"], "new")


if __name__ == "__main__":
    unittest.main()
