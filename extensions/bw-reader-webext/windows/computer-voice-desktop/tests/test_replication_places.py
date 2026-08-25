from __future__ import annotations

import json
from pathlib import Path
import sys
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
