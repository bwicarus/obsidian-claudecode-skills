#!/usr/bin/env python3
"""账本 v3：client（哪个端）+ device（哪台机）。

活动账本设计稿 §3.3 / §3.4。调查（2026-08-24）把设计稿的两个用词都改了，
理由都很具体，写在这里免得有人改回去：

* **叫 client 不叫 surface** —— `event_mentions.surface` 在**同一个库里**
  已经是「命中的词形」。两个语义不同的 surface 共存，任何 SELECT/JOIN
  取错列**不报错、只算错**。
* **叫 device 不叫 place** —— 我们记的不是地点，是"哪台机"。真定位全仓
  零基础设施，IP 那条路也是死的（请求都经 nginx 反代，remote_addr 恒
  127.0.0.1；Tailscale 地址与所在网络无关）。叫 place 会让人以为有地理信息。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def _fresh_module():
    """每个用例一套独立的 CLAUDE_PROJECT，互不污染。"""
    tmp = tempfile.mkdtemp(prefix="bw-ledger-")
    os.environ["CLAUDE_PROJECT"] = tmp
    sys.path.insert(0, str(ROOT / "scripts"))
    # ⚠ config 也要一起丢掉：PROJECT_DIR 是它在 import 时从环境读的，
    #   只重载 attention_profile 会拿到缓存的旧路径 —— 于是所有用例
    #   共用同一个库，计数互相污染（第一次跑就撞上了）。
    for name in ("attention_profile", "config"):
        sys.modules.pop(name, None)
    import attention_profile as A  # noqa: E402
    A.ATT_DIR.mkdir(parents=True, exist_ok=True)
    return A


class LedgerFieldTests(unittest.TestCase):
    def test_client_and_device_reach_the_derived_index(self):
        """账本里有、查询里没有 —— 那是最容易被当成"功能没做"的一种沉默。

        extra / anchor / actor / session_id 今天就是那个状态（import_raw 不传），
        所以这两个字段必须**显式往下传**，并且要有断言盯着。
        """
        A = _fresh_module()
        A.append_raw("mutate", "删除了卡片 c_ab12", file="x.pdf", page=3,
                     client="native", device="ipad-1")
        c = A._db()
        A.import_raw(c)
        row = c.execute("SELECT client, device FROM events").fetchone()
        self.assertEqual(row, ("native", "ipad-1"),
                         "client/device 必须到得了 events 表，否则查不出来")

    def test_ledger_line_carries_v3_and_fields(self):
        A = _fresh_module()
        A.append_raw("read", "读了一页", file="y.pdf", page=7, client="extension")
        line = json.loads(Path(A.RAW).read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(line["v"], 3, "加字段就要 +1，读侧靠它兼容老行")
        self.assertEqual(line["client"], "extension")
        self.assertNotIn("device", line, "空值不写 —— 沿用既有的 omit 规则")

    def test_omitted_fields_stay_omitted(self):
        A = _fresh_module()
        A.append_raw("read", "没带端信息", file="z.pdf")
        line = json.loads(Path(A.RAW).read_text(encoding="utf-8").splitlines()[0])
        self.assertNotIn("client", line)
        self.assertNotIn("device", line)


class SrcKeyTests(unittest.TestCase):
    def test_src_key_must_not_include_client_or_device(self):
        """⚠⚠ 这是这次改动里最贵的一条。

        一旦把 client 掺进 src_key，raw-events.jsonl 里**全部历史行**会重算出
        新 key → INSERT OR REPLACE 匹配不到老行 → 老行留着、新行再进一遍，
        同一件事在画像里权重翻倍，而且没有任何地方会报错。
        """
        A = _fresh_module()
        source = Path(A.__file__).read_text(encoding="utf-8")
        at = source.index("key = hashlib.sha1(")
        expr = source[at:source.index("\n", at)]
        for forbidden in ("client", "device"):
            self.assertNotIn(
                forbidden, expr,
                f"src_key 里出现了 {forbidden} —— 存量账本会二次入库变重复事件")

    def test_same_event_from_two_clients_collapses_by_design(self):
        """上一条的代价：同一秒同一页的两条不同端事件会被合成一条。

        这是**有意接受**的取舍（把历史数据搞脏比丢一条并发事件贵得多）。
        钉住它，免得将来有人以为是 bug 而去改 src_key。
        """
        A = _fresh_module()
        A.append_raw("mutate", "同一件事", ts=1700000000, file="a.pdf", page=1,
                     client="native")
        A.append_raw("mutate", "同一件事", ts=1700000000, file="a.pdf", page=1,
                     client="extension")
        c = A._db()
        A.import_raw(c)
        n = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.assertEqual(
            n, 1, "同 src_key 合并是设计取舍；要改这条必须同时处理存量账本重导")


class TermGateTests(unittest.TestCase):
    def test_mutate_survives_the_term_gate(self):
        """`if not terms: return 0` 此前**完全无声**。

        对"删除了卡片 c_ab12"这类记账事件是致命的：文本里本来就没有可抽的词，
        于是导入计数看着正常、查询里却永远找不到它。
        """
        A = _fresh_module()
        A.append_raw("mutate", "c_ab12", file="a.pdf", page=1, client="native")
        c = A._db()
        A.import_raw(c)
        n = c.execute("SELECT COUNT(*) FROM events WHERE channel='mutate'").fetchone()[0]
        self.assertEqual(n, 1, "记账事件的价值在事件本身，不在词")

    def test_other_channels_still_gated(self):
        """豁免只给 TERMLESS_CHANNELS —— 其余渠道行为一个字都不变。"""
        A = _fresh_module()
        self.assertEqual(A.TERMLESS_CHANNELS, {"mutate"},
                         "扩大豁免面会让画像被无词噪声灌满")


class NamingTests(unittest.TestCase):
    def test_field_is_not_called_surface(self):
        """同库两义的 surface 是这次特意避开的坑。"""
        A = _fresh_module()
        source = Path(A.__file__).read_text(encoding="utf-8")
        at = source.index("def append_raw(")
        sig = source[at:source.index('"""', at)]
        self.assertIn("client=None", sig)
        self.assertIn("device=None", sig)
        self.assertNotIn("surface=", sig,
                         "event_mentions.surface 已占用这个词，含义是词形")
        self.assertNotIn("place=", sig,
                         "我们记的是哪台机，不是地点 —— 叫 place 会让人以为有地理信息")

    def test_events_table_still_has_the_word_surface_for_mentions(self):
        """反向确认撞名风险是真的：同一个库里确实另有一个 surface。"""
        A = _fresh_module()
        source = Path(A.__file__).read_text(encoding="utf-8")
        self.assertIn("event_mentions(", source)
        at = source.index("event_mentions(")
        self.assertIn("surface TEXT", source[at:at + 400],
                      "如果这条断言红了，说明撞名风险没了，可以重新考虑命名")


if __name__ == "__main__":
    unittest.main()
