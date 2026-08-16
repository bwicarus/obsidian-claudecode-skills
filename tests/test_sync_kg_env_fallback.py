"""`.env` 回退：凭据读不到时必须看得出来是读不到。

Windows 上没有任何东西会把 `.env` 读进环境变量（Linux 那边是 systemd /
profile.d 干的）。所以「token 写进 .env 就行了」这个直觉在这台机器上是错的，
而失败长得像凭据无效 —— 会把人引去查 token。
"""
from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "sync_kg_from_pi", ROOT / "scripts" / "sync_kg_from_pi.py")
sync_kg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync_kg)


class EnvFileFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = os.environ.get("CLAUDE_PROJECT")

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop("CLAUDE_PROJECT", None)
        else:
            os.environ["CLAUDE_PROJECT"] = self._saved

    def _with_env_file(self, body: str, tmp: Path) -> str:
        (tmp / ".env").write_text(body, encoding="utf-8")
        os.environ["CLAUDE_PROJECT"] = str(tmp)
        return sync_kg._env_file_value("BW_PI_TOKEN")

    def test_reads_plain_value(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                self._with_env_file("BW_PI_TOKEN=abc123\n", Path(d)), "abc123")

    def test_strips_quotes_and_ignores_comments(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            body = "# 注释里也有 BW_PI_TOKEN=wrong\nBW_PI_TOKEN=\"q u o t e d\"\n"
            self.assertEqual(self._with_env_file(body, Path(d)), "q u o t e d")

    def test_absent_key_returns_empty_not_crash(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self._with_env_file("OTHER=1\n", Path(d)), "")

    def test_missing_file_returns_empty(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            os.environ["CLAUDE_PROJECT"] = str(Path(d) / "nope")
            self.assertEqual(sync_kg._env_file_value("BW_PI_TOKEN"), "")

    def test_prefix_match_does_not_count(self) -> None:
        # BW_PI_TOKEN_OLD 不是 BW_PI_TOKEN。用 startswith 写就会中招，
        # 而中招的表现是拿到一个过期 token，仍然报「凭据无效」。
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            body = "BW_PI_TOKEN_OLD=stale\nBW_PI_TOKEN=fresh\n"
            self.assertEqual(self._with_env_file(body, Path(d)), "fresh")


if __name__ == "__main__":
    unittest.main()
