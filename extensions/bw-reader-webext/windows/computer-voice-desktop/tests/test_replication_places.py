from __future__ import annotations

import json
from pathlib import Path
import sys
import shutil
import tempfile
import time
import unittest

SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))

import replication_places  # noqa: E402


def _dwell_line(at_ms: int, lat: float, lon: float,
                secs: int = 60, name: str = "") -> str:
    return json.dumps({
        "receivedAtUtcMs": at_ms,
        "mutationId": "mut-v2-" + "0" * 32,
        "deviceId": "native-app-v1-" + "a" * 32,
        "body": {"kind": "dwell", "file": "localbook:x",
                 "entries": [{"page": 1, "secs": secs}],
                 "loc": {"lat": lat, "lon": lon, "at": at_ms // 1000,
                         **({"name": name} if name else {})}},
    }, ensure_ascii=False)


class PlacesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.book = self.root / "replication-data" / ("repbook-" + "a" * 32)
        self.book.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write(self, lines: list[str]) -> None:
        (self.book / "activity-dwell.jsonl").write_text(
            "\n".join(lines) + "\n", "utf-8")

    def test_micro_drift_stays_one_cluster(self) -> None:
        now = int(time.time() * 1000)
        # 三个点相距 <120m（约 0.0005 度纬度 ≈ 55m）
        self._write([
            _dwell_line(now - 3000, 35.0000, 139.0000, 100),
            _dwell_line(now - 2000, 35.0005, 139.0000, 200),
            _dwell_line(now - 1000, 35.0003, 139.0004, 300, "図書館"),
        ])
        clusters = replication_places.cluster(
            replication_places._load_located_dwell(self.root))
        self.assertEqual(len(clusters), 1, "微小漂移不分裂成多个位置")
        self.assertEqual(clusters[0]["seconds"], 600)
        self.assertEqual(clusters[0]["geoName"], "図書館")

    def test_distinct_places_split_and_alias_resolves(self) -> None:
        now = int(time.time() * 1000)
        self._write([
            _dwell_line(now - 2000, 35.0, 139.0, 500),
            _dwell_line(now - 1000, 35.1, 139.1, 100),  # ~14km 外
        ])
        clusters = replication_places.cluster(
            replication_places._load_located_dwell(self.root))
        self.assertEqual(len(clusters), 2)
        replication_places.save_alias(self.root, "家", 35.0, 139.0)
        self.assertEqual(
            replication_places.resolve_alias(self.root, 35.0004, 139.0),
            "家", "别名按坐标就近命中（200m 内）")
        self.assertIsNone(
            replication_places.resolve_alias(self.root, 35.1, 139.1))

    def test_current_place_export_and_staleness(self) -> None:
        now = int(time.time() * 1000)
        self._write([
            _dwell_line(now - 60_000, 35.0, 139.0, 60, "会社"),
        ])
        replication_places.save_alias(self.root, "公司", 35.0, 139.0)
        export = self.root / "runtime" / "current-place.json"
        value = replication_places.export_current_place(self.root, export)
        self.assertEqual(value["alias"], "公司", "别名优先")
        self.assertEqual(value["geoName"], "会社")
        # 只剩过期位置 → 导出文件删除（缺席=不知道在哪）
        self._write([
            _dwell_line(now - 2 * 3600_000, 35.0, 139.0, 60),
        ])
        gone = replication_places.export_current_place(self.root, export)
        self.assertIsNone(gone)
        self.assertFalse(export.exists(),
                         "旧位置绝不冒充当前 —— 文件删除表达缺席")


if __name__ == "__main__":
    unittest.main()


class ManualNamingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.book = self.root / "replication-data" / ("repbook-" + "a" * 32)
        self.book.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_name_latest(self) -> None:
        now = int(time.time() * 1000)
        (self.book / "activity-dwell.jsonl").write_text(
            _dwell_line(now - 1000, 35.0, 139.0) + "\n", "utf-8")
        hit = replication_places.name_latest(self.root, "家")
        self.assertIsNotNone(hit)
        self.assertEqual(
            replication_places.resolve_alias(self.root, 35.0, 139.0), "家")

    def test_pending_name_binds_first_arrival_within_ttl(self) -> None:
        replication_places.set_pending_name(self.root, "家")
        now = int(time.time() * 1000)
        (self.book / "activity-dwell.jsonl").write_text(
            _dwell_line(now - 1000, 35.0, 139.0) + "\n", "utf-8")
        export = self.root / "runtime" / "current-place.json"
        value = replication_places.export_current_place(self.root, export)
        self.assertEqual(value["alias"], "家", "首条定位自动绑定")
        self.assertFalse(
            (self.root / "place-pending-name.json").exists(),
            "挂起标记消费即删")
        # 完成通知已生成
        notes = json.loads(
            (self.root / "notifications.json").read_text("utf-8"))
        self.assertEqual(notes["items"][0]["kind"], "place-named")

    def test_stale_pending_never_binds(self) -> None:
        import replication_places as rp
        (self.root / rp.PENDING_NAME_FILE_NAME).write_text(json.dumps({
            "name": "家",
            "createdAtUtcMs": int(time.time() * 1000)
            - rp.PENDING_NAME_TTL_MS - 1,
        }), "utf-8")
        now = int(time.time() * 1000)
        (self.book / "activity-dwell.jsonl").write_text(
            _dwell_line(now - 1000, 35.0, 139.0) + "\n", "utf-8")
        export = self.root / "runtime" / "current-place.json"
        value = replication_places.export_current_place(self.root, export)
        self.assertIsNone(value["alias"], "过时挂起绝不错绑")
        self.assertFalse(
            (self.root / rp.PENDING_NAME_FILE_NAME).exists())


class PlaceStateTests(unittest.TestCase):
    """位置 → 状态。这层存在的理由是下游要的不是坐标，是「该怎么打扰他」。"""

    def test_known_aliases_map_to_states(self) -> None:
        self.assertEqual(replication_places.place_state("家"), "home")
        self.assertEqual(replication_places.place_state("工作地点"), "work")

    def test_unknown_alias_is_elsewhere_not_a_guess(self) -> None:
        # ⚠ 不认识就是 elsewhere。猜的话会在「在咖啡店」时按在家处理，
        # 而那正是会出声打扰人的那一档。
        self.assertEqual(replication_places.place_state("星巴克"), "elsewhere")

    def test_no_alias_is_elsewhere(self) -> None:
        self.assertEqual(replication_places.place_state(None), "elsewhere")
        self.assertEqual(replication_places.place_state(""), "elsewhere")

    def test_export_carries_state(self) -> None:
        # 归类要在导出时就做好 —— 让每个下游自己去认那几个名字，
        # 迟早认岔（改了别名却只改了一处）。
        # ⚠ 夹具必须按 _load_located_dwell 真正读的那个形状写
        # （replication-data/<book>/<活动文件>，body.kind=dwell + body.loc）。
        # 第一版我写成了另一种形状，于是用例自己 skip 掉了 ——
        # 一个 skip 就是一个静默的洞：它看起来是绿的，其实什么都没验。
        import replication_activity
        root = Path(tempfile.mkdtemp())
        try:
            replication_places.save_alias(root, "家", 35.65318, 139.31593)
            book = root / "replication-data" / "bk"
            book.mkdir(parents=True, exist_ok=True)
            record = {
                "receivedAtUtcMs": int(time.time() * 1000),
                "body": {
                    "kind": "dwell",
                    "loc": {
                        "lat": 35.65318,
                        "lon": 139.31593,
                        "name": "散田町",
                    },
                    "entries": [{"secs": 600}],
                },
            }
            (book / replication_activity.ACTIVITY_FILE_NAME).write_text(
                json.dumps(record, ensure_ascii=False) + "\n",
                encoding="utf-8")
            export = root / "runtime" / "current-place.json"
            value = replication_places.export_current_place(root, export)
            self.assertIsNotNone(
                value,
                "夹具没被 _load_located_dwell 认出来 —— 这是测试写错了")
            self.assertEqual(value["alias"], "家")
            self.assertEqual(value["state"], "home")
        finally:
            shutil.rmtree(root, ignore_errors=True)
