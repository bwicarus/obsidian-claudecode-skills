"""
config_schema.validate_partial 防止控制面板 POST 静默接受拼错的字段名。
另外测 schema_for_ui 跟 SCHEMA 不 drift（FIELD_META 里写的字段都得在 SCHEMA 里）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from config_schema import validate_partial, schema_for_ui, SCHEMA, FIELD_META


class ConfigSchemaTest(unittest.TestCase):

    def test_known_top_level_field_accepted(self) -> None:
        clean, errors = validate_partial({"qa_remote_daemon": True})
        self.assertEqual(clean, {"qa_remote_daemon": True})
        self.assertEqual(errors, [])

    def test_known_nested_field_accepted(self) -> None:
        clean, errors = validate_partial({"anki": {"auto_restart": False}})
        self.assertEqual(clean, {"anki": {"auto_restart": False}})
        self.assertEqual(errors, [])

    def test_unknown_field_rejected(self) -> None:
        clean, errors = validate_partial({"qa_remote_demon": True})  # 拼错 daemon → demon
        self.assertEqual(clean, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("qa_remote_demon", errors[0])

    def test_wrong_type_rejected(self) -> None:
        clean, errors = validate_partial({"qa_remote_daemon": "yes"})  # 应该 bool
        self.assertEqual(clean, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("bool", errors[0])

    def test_bool_vs_int_strict(self) -> None:
        """bool 是 int 的子类，但 schema 期望 bool 时不接受 0/1。"""
        clean, errors = validate_partial({"qa_remote_daemon": 1})
        self.assertEqual(clean, {})
        self.assertTrue(any("bool" in e for e in errors))

    def test_mixed_valid_and_invalid(self) -> None:
        """合法字段保留，非法字段进 errors。"""
        clean, errors = validate_partial({
            "qa_remote_daemon": True,         # OK
            "anki": {"auto_restart": True},   # OK
            "unknown_top":      "x",          # reject (unknown)
            "scheduled_register": {
                "wake_anki":  False,           # OK
                "wak_anki":   True,            # reject (拼错)
            },
        })
        # clean 应该有 2 个合法叶子
        self.assertTrue(clean.get("qa_remote_daemon") is True)
        self.assertEqual(clean.get("anki"), {"auto_restart": True})
        self.assertEqual(clean.get("scheduled_register"), {"wake_anki": False})
        # errors 应该报 unknown_top 和 wak_anki
        self.assertEqual(len(errors), 2)
        self.assertTrue(any("unknown_top" in e for e in errors))
        self.assertTrue(any("wak_anki" in e for e in errors))

    def test_non_dict_input(self) -> None:
        clean, errors = validate_partial([1, 2, 3])  # type: ignore[arg-type]
        self.assertEqual(clean, {})
        self.assertEqual(len(errors), 1)


class SchemaForUITest(unittest.TestCase):
    """schema_for_ui() 返回前端用的字段列表。
    跟 SCHEMA 不 drift：FIELD_META 里每个字段必须也在 SCHEMA 里声明。"""

    def test_all_fields_have_known_schema_entry(self) -> None:
        for path in FIELD_META:
            self.assertIn(path, SCHEMA, f"FIELD_META[{path}] 没有对应的 SCHEMA 类型声明")

    def test_returns_list_of_dicts_with_required_keys(self) -> None:
        fields = schema_for_ui()
        self.assertIsInstance(fields, list)
        self.assertTrue(len(fields) > 0)
        required = {"path", "type", "group", "label"}
        for f in fields:
            self.assertTrue(required.issubset(f.keys()),
                            f"字段缺 key: {required - f.keys()}")

    def test_type_field_is_python_type_name(self) -> None:
        for f in schema_for_ui():
            self.assertIn(f["type"], {"bool", "str", "int", "float"},
                          f"未支持的类型 {f['type']} (字段 {f['path']})")


if __name__ == "__main__":
    unittest.main()
