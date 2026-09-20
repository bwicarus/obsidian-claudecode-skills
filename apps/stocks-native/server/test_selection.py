"""Behavioral checks for screening semantics and account mutation boundaries."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from data import StockDataStore
from selection import (SelectionConflict, SelectionError, SelectionService,
                       criterion_checks, DEFAULT_PARAMETERS)


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.market = self.root / "market"
        self.market.mkdir()
        with sqlite3.connect(self.market / "stocks.db") as db:
            db.executescript("""
                CREATE TABLE daily_quotes(trade_date TEXT,code TEXT,name TEXT,price REAL,turnover_rate REAL,change_pct REAL,turnover REAL);
                CREATE TABLE daily_feature_groups(trade_date TEXT,code TEXT,feature_group TEXT,checks_json TEXT,metrics_json TEXT);
                CREATE TABLE sector_membership(trade_date TEXT,code TEXT,sector TEXT,is_hot_sector INT,is_sector_leader INT);
                CREATE TABLE daily_limit(trade_date TEXT,code TEXT,up_limit REAL,down_limit REAL);
            """)
            db.executemany("INSERT INTO daily_quotes VALUES ('2026-09-18',?,?,?,?,?,?)", [
                ("000001", "甲", 10, 5, 1, 100), ("000002", "乙", 20, 6, -1, 200),
                ("688001", "丙", 100, 7, 20, 300), ("920001", "丁", 5, None, None, 50)])
            features = [
                ("000001", "technical", {"above_ma5": True, "kdj_recent_cross": True}, {"profit_ratio": .9, "chip_concentration": 8, "kdj_cross_days_ago": 4}),
                ("000002", "technical", {"above_ma5": False}, {"profit_ratio": .3, "chip_concentration": 20}),
                ("688001", "technical", {"above_ma5": True}, {"profit_ratio": .95}),
                ("000001", "fund", {"main_fund_5d_inflow": True}, {}),
            ]
            for code, kind, checks, metrics in features:
                db.execute("INSERT INTO daily_feature_groups VALUES ('2026-09-18',?,?,?,?)", (code, kind, json.dumps(checks), json.dumps(metrics)))
            db.execute("INSERT INTO sector_membership VALUES ('2026-09-18','000001','金融',1,1)")
        db.close()
        self.service = SelectionService(StockDataStore(self.market), self.root / "state")
        self.owner = "apple:test-one"

    def tearDown(self):
        self.temp.cleanup()

    def query(self, groups, **extra):
        return self.service.evaluate(self.owner, {"groups": groups, **extra})

    def mutate(self, operation, payload, owner=None, revision=None, request_id=None):
        owner = owner or self.owner
        revision = self.service.load_library(owner)["revision"] if revision is None else revision
        return self.service.mutate(owner, {"requestId": request_id or f"r-{revision}", "expectedRevision": revision,
                                          "operation": operation, "payload": payload})

    def test_catalog_has_all_twenty_five_legacy_conditions(self):
        catalog = self.service.catalog()
        ids = {item["id"] for item in catalog["criteria"]}
        self.assertEqual(len(ids), 25)
        self.assertTrue({"main_force_present", "volume_contracting", "break_60d_high", "monthly_up", "chip_concentration_below"} <= ids)

    def test_or_groups_and_exclusion_are_strict_with_missing_data(self):
        result = self.query([{"id": "a", "and": ["price_below_limit", "above_ma5"], "not": []},
                             {"id": "b", "and": [], "not": ["above_ma5"]}])
        self.assertEqual([r["code"] for r in result["items"]], ["000001", "000002"])
        self.assertEqual(result["unknown"], 1)  # 920001 must not pass NOT unknown.

    def test_effect_is_added_count_from_removing_one_constraint(self):
        result = self.query([{"id": "a", "and": ["price_below_limit", "above_ma5"], "not": []}])
        self.assertEqual(result["groups"][0]["baselinePassed"], 1)
        self.assertEqual(result["groups"][0]["impacts"], {"price_below_limit": 1, "above_ma5": 2})

    def test_disabled_criterion_is_scoped_to_one_group(self):
        groups = [{"id": "a", "and": ["price_below_limit", "above_ma5"], "not": []},
                  {"id": "b", "and": ["above_ma5"], "not": ["price_below_limit"]}]
        result = self.query(groups, disabled=["a|above_ma5"])
        self.assertEqual(result["passed"], 4)
        self.assertEqual(result["groups"][1]["baselinePassed"], 1)

    def test_explicit_no_conditions_is_unfiltered(self):
        for groups in ([], [{"id": "empty", "and": [], "not": []}]):
            with self.subTest(groups=groups):
                result = self.query(groups)
                self.assertTrue(result["unfiltered"])
                self.assertFalse(result["disabledAll"])
                self.assertEqual(result["passed"], 4)

    def test_all_configured_conditions_disabled_returns_no_matches(self):
        cases = [
            ([{"id": "a", "and": ["price_below_limit"], "not": ["above_ma5"]}],
             ["a|price_below_limit", "a|not:above_ma5"]),
            ([{"id": "a", "and": ["price_below_limit"], "enabled": False}], []),
            ([{"id": "a", "and": ["price_below_limit"], "enabled": False},
              {"id": "b", "not": ["above_ma5"]}, {"id": "empty"}], ["b|not:above_ma5"]),
        ]
        for groups, disabled in cases:
            with self.subTest(groups=groups, disabled=disabled):
                result = self.query(groups, disabled=disabled)
                self.assertTrue(result["disabledAll"])
                self.assertFalse(result["unfiltered"])
                self.assertEqual(result["passed"], 0)
                self.assertEqual(result["items"], [])

    def test_restoring_one_chip_filters_without_reenabling_the_other_conditions(self):
        groups = [{"id": "a", "and": ["price_below_limit", "above_ma5"]},
                  {"id": "b", "not": ["above_ma5"]}]
        disabled = ["a|price_below_limit", "a|above_ma5", "b|not:above_ma5"]
        self.assertEqual(self.query(groups, disabled=disabled)["passed"], 0)
        disabled.remove("a|above_ma5")
        result = self.query(groups, disabled=disabled)
        self.assertFalse(result["disabledAll"])
        self.assertFalse(result["unfiltered"])
        self.assertEqual([item["code"] for item in result["items"]], ["000001", "688001"])

    def test_unknown_condition_and_bad_disabled_are_rejected(self):
        for request in ({"groups": [{"and": ["not_a_feature"]}]}, {"disabled": ["g1|above_ma5"]}):
            with self.assertRaises(SelectionError):
                self.service.evaluate(self.owner, request)

    def test_live_parameter_changes_use_raw_metric_and_zero_is_not_default(self):
        group = [{"id": "a", "and": ["profit_ratio_below"], "not": []}]
        self.assertEqual(self.query(group, parameters={"profit_ratio_below_value": 0})["passed"], 0)
        self.assertEqual(self.query(group, parameters={"profit_ratio_below_value": 100})["passed"], 3)
        cross = [{"id": "a", "and": ["kdj_recent_cross"], "not": []}]
        self.assertEqual(self.query(cross, parameters={"kdj_cross_days": 4})["passed"], 0)
        self.assertEqual(self.query(cross, parameters={"kdj_cross_days": 5})["passed"], 1)

    def test_board_threshold_precedes_st_name(self):
        checks, _ = criterion_checks({"code": "301001", "name": "ST示例", "price": 10, "change_pct": 6}, {}, {}, {}, DEFAULT_PARAMETERS)
        self.assertIs(checks["is_limit_up"], False)

    def test_failed_group_creation_rolls_back_all_changes(self):
        with self.assertRaises(SelectionError):
            self.mutate("group.create", {"name": "坏规则", "kind": "smart", "rules": {"attrs": ["unknown"]}})
        self.assertEqual(self.service.load_library(self.owner)["revision"], 0)
        self.assertEqual(self.service.load_library(self.owner)["groups"], [])

    def test_owner_isolation_revision_conflict_and_replay(self):
        request = {"requestId": "create-one", "expectedRevision": 0, "operation": "group.create", "payload": {"name": "自选"}}
        first = self.service.mutate(self.owner, request)
        replay = self.service.mutate(self.owner, request)
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["groupId"], replay["groupId"])
        self.assertEqual(len(replay["library"]["groups"]), 1)
        self.assertEqual(self.service.load_library("apple:test-two")["groups"], [])
        with self.assertRaises(SelectionConflict) as cm:
            self.mutate("group.create", {"name": "旧版本"}, revision=0, request_id="different")
        self.assertEqual(cm.exception.revision, 1)
        with self.assertRaises(SelectionError) as cm:
            self.service.mutate(self.owner, {**request, "payload": {"name": "替换"}})
        self.assertEqual(cm.exception.code, "request_id_conflict")

    def test_batch_add_is_atomic_when_one_target_is_smart(self):
        manual = self.mutate("group.create", {"name": "手动"})["groupId"]
        smart = self.mutate("group.create", {"name": "热门", "kind": "smart", "rules": {"attrs": ["hot_sector"]}})["groupId"]
        with self.assertRaises(SelectionError):
            self.mutate("group.add", {"groupIds": [manual, smart], "codes": ["000001"]})
        self.assertEqual(self.service.load_library(self.owner)["groups"][0]["codes"], [])

    def test_manual_members_deduplicate_and_can_belong_to_multiple_groups(self):
        a = self.mutate("group.create", {"name": "甲"})["groupId"]
        b = self.mutate("group.create", {"name": "乙"})["groupId"]
        result = self.mutate("group.add", {"groupIds": [a, b], "codes": ["000001", "000001"]})
        self.assertEqual([g["codes"] for g in result["library"]["groups"]], [["000001"], ["000001"]])

    def test_smart_legacy_ai_rule_is_never_dropped_from_any_expression(self):
        result = self.mutate("group.create", {"name": "旧智能", "kind": "smart", "rules": {"match": "any", "attrs": ["ai_a", "hot_sector"]}})
        group = result["library"]["groups"][0]
        self.assertEqual(group["status"], "needs_migration")
        self.assertEqual(group["codes"], [])
        self.assertIn("smart_attribute_unavailable:ai_a", group["warnings"])

    def test_smart_definition_recomputes_after_source_update(self):
        result = self.mutate("group.create", {"name": "低价", "kind": "smart", "rules": {"attrs": [], "definition": {
            "groups": [{"id": "g", "and": ["price_below_limit"]}], "parameters": {"max_price": 6}}}})
        self.assertEqual(result["library"]["groups"][0]["codes"], ["920001"])
        with sqlite3.connect(self.market / "stocks.db") as db:
            db.execute("UPDATE daily_quotes SET price=4 WHERE code='000001'")
        db.close()
        self.assertEqual(self.service.load_library(self.owner)["groups"][0]["codes"], ["000001", "920001"])

    def test_empty_smart_definition_is_rejected(self):
        with self.assertRaises(SelectionError):
            self.mutate("group.create", {"name": "空", "kind": "smart", "rules": {"definition": {"groups": []}}})

    def test_preset_save_and_run_keep_definition_and_receipt(self):
        preset = self.mutate("preset.save", {"name": "低价", "definition": {"groups": [{"id": "g", "and": ["price_below_limit"]}]}})["presetId"]
        result = self.mutate("preset.run", {"id": preset})
        self.assertEqual(result["evaluation"]["passed"], 3)
        self.assertEqual(result["library"]["lastRun"]["presetId"], preset)

    def test_preset_save_ignores_temporary_disabled_chips_without_mutating_the_draft(self):
        definition = {"groups": [{"id": "a", "and": ["price_below_limit", "above_ma5"]}],
                      "disabled": ["a|above_ma5"]}
        self.assertEqual(self.service.evaluate(self.owner, definition)["passed"], 3)
        preset_id = None
        for _ in range(2):
            receipt = self.mutate("preset.save", {"id": preset_id, "name": "基础方案", "definition": definition})
            preset_id = receipt["presetId"]
            saved = next(p for p in receipt["library"]["presets"] if p["id"] == preset_id)
            self.assertEqual(saved["definition"]["disabled"], [])
            self.assertEqual(saved["definition"]["groups"][0]["and"], definition["groups"][0]["and"])
            self.assertEqual(definition["disabled"], ["a|above_ma5"])
            self.assertEqual(self.mutate("preset.run", {"id": preset_id})["evaluation"]["passed"], 1)

    def test_legacy_import_appends_and_retains_unmigrated_rules(self):
        self.mutate("group.create", {"name": "原生自选"})
        payload = {"settings": {"max_price": 10, "criteria_groups": [["price_below_limit"]]},
            "configs": {"未知条件": {"criteria_groups": [["unknown_old"]]}},
            "watchlist": {"tabs": [{"id": "old", "name": "旧AI", "smart": True,
                "smart_rules": {"match": "any", "attrs": ["ai_a", "hot_sector"]}, "codes": ["000001"]}]}}
        result = self.service.import_legacy(self.owner, payload, "legacy-1")
        self.assertEqual(len(result["library"]["groups"]), 2)
        self.assertEqual(result["library"]["groups"][1]["legacySource"]["codes"], ["000001"])
        self.assertEqual(result["library"]["presets"][1]["status"], "needs_migration")
        again = self.service.import_legacy(self.owner, payload, "legacy-1")
        self.assertTrue(again["replayed"])
        with self.assertRaises(SelectionError):
            self.mutate("legacy.import", payload)

    def test_moving_into_nonempty_account_never_overwrites(self):
        self.mutate("group.create", {"name": "设备自选"}, owner="device:one")
        self.mutate("group.create", {"name": "账户自选"})
        with self.assertRaises(SelectionConflict):
            self.service.move_library("device:one", self.owner)
        self.assertEqual(self.service.load_library(self.owner)["groups"][0]["name"], "账户自选")
        result = self.service.move_library("device:one", "apple:empty")
        self.assertTrue(result["copied"])
        self.assertEqual(self.service.load_library("device:one")["groups"][0]["name"], "设备自选")

    def test_queries_do_not_create_native_state_database(self):
        self.service.load_library(self.owner)
        self.query([])
        self.assertFalse(self.service.db_path.exists())

    def test_group_history_uses_next_day_only_and_omits_unrealized_last_day(self):
        with sqlite3.connect(self.market / "stocks.db") as db:
            db.execute("INSERT INTO daily_quotes SELECT '2026-09-21',code,name,CASE WHEN code='000001' THEN 11 ELSE price END,turnover_rate,change_pct,turnover FROM daily_quotes WHERE trade_date='2026-09-18'")
        db.close()
        result = self.query([{"id": "a", "and": ["above_ma5"]}], includeHistory=True)
        history = result["history"]
        self.assertEqual(history["days"], 1)
        self.assertEqual(history["groups"][0]["hits"], 2)
        self.assertAlmostEqual(history["groups"][0]["meanExcessPct"], 2.5)
        self.assertEqual(history["groups"][0]["sampleDays"], 1)


if __name__ == "__main__":
    unittest.main()
