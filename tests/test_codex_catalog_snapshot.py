"""Offline contracts for Codex model-catalog generation snapshots.

No test in this module starts Codex or reads the developer's credential files.
"""

from __future__ import annotations

import copy
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "_server_deploy"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import assistant  # noqa: E402


def _bootstrap(generation: str):
    return {
        "generation": generation,
        "changed": False,
        "error": "",
    }


def _row(model: str, *, priority: bool):
    return {
        "id": model,
        "hidden": False,
        "supportedReasoningEfforts": [
            {"reasoningEffort": "low"},
            {"reasoningEffort": "high"},
        ],
        "serviceTiers": ([{"id": "priority"}] if priority else []),
    }


class _AliveProcess:
    def poll(self):
        return None


class CodexAppModelCatalogSnapshotTest(unittest.TestCase):
    def test_paginated_rows_return_the_bound_process_generation(self):
        client = assistant._CodexApp()
        client._p = _AliveProcess()
        client._auth_generation = "auth-bound-process"
        client._ensure = Mock(return_value="auth-bound-process")
        pages = iter((
            {
                "data": [_row("gpt-page-one", priority=False)],
                "nextCursor": "page-two",
            },
            {
                "data": [_row("gpt-page-two", priority=True)],
                "nextCursor": None,
            },
        ))
        calls = []

        def rpc(method, params, timeout=20):
            calls.append((method, dict(params), timeout))
            return next(pages)

        client._rpc = rpc

        rows, generation = client.model_catalog()

        self.assertEqual(
            [row["id"] for row in rows],
            ["gpt-page-one", "gpt-page-two"],
        )
        self.assertEqual(generation, "auth-bound-process")
        self.assertEqual(
            calls,
            [
                ("model/list", {"limit": 100}, 20),
                (
                    "model/list",
                    {"limit": 100, "cursor": "page-two"},
                    20,
                ),
            ],
        )


class CodexCatalogGenerationSnapshotTest(unittest.TestCase):
    def setUp(self):
        self._old_cache = copy.deepcopy(assistant._codex_catalog_cache)
        assistant._codex_catalog_cache.clear()
        assistant._codex_catalog_cache.update({
            "ts": 0.0,
            "models": {},
            "verified": False,
            "error": "",
            "auth_generation": "",
        })

    def tearDown(self):
        assistant._codex_catalog_cache.clear()
        assistant._codex_catalog_cache.update(self._old_cache)

    def test_rows_are_tagged_with_answering_process_not_preprobe_auth(self):
        rows = [_row("gpt-5.6-sol", priority=True)]
        with (
            patch.object(
                assistant,
                "_codex_rc_bootstrap",
                return_value=_bootstrap("auth-before-list"),
            ),
            patch.object(
                assistant._codex_app,
                "model_catalog",
                return_value=(rows, "auth-that-answered-list"),
            ),
        ):
            snapshot = assistant._codex_catalog(force=True)

        self.assertEqual(
            snapshot["auth_generation"],
            "auth-that-answered-list",
        )
        self.assertEqual(
            assistant._codex_catalog_cache["auth_generation"],
            "auth-that-answered-list",
        )
        self.assertTrue(snapshot["models"]["gpt-5.6-sol"]["fast"])

    def test_concurrent_forced_reads_publish_only_complete_snapshots(self):
        worker_count = 10
        start = threading.Barrier(worker_count)
        call_lock = threading.Lock()
        calls = 0
        results = []
        errors = []
        result_lock = threading.Lock()

        def model_catalog():
            nonlocal calls
            with call_lock:
                calls += 1
                number = calls
            # Widen the interleaving window.  _codex_catalog must still return
            # one whole locally-built snapshot to every caller.
            time.sleep(0.003)
            suffix = "a" if number % 2 else "b"
            model = f"gpt-snapshot-{suffix}"
            return [_row(model, priority=(suffix == "a"))], f"auth-{suffix}"

        def worker():
            try:
                start.wait(timeout=3)
                snapshot = assistant._codex_catalog(force=True)
                with result_lock:
                    results.append(snapshot)
            except BaseException as error:
                with result_lock:
                    errors.append(error)

        with (
            patch.object(
                assistant,
                "_codex_rc_bootstrap",
                return_value=_bootstrap("auth-preprobe"),
            ),
            patch.object(
                assistant._codex_app,
                "model_catalog",
                side_effect=model_catalog,
            ),
        ):
            workers = [
                threading.Thread(target=worker)
                for _ in range(worker_count)
            ]
            for thread in workers:
                thread.start()
            for thread in workers:
                thread.join(timeout=5)

        self.assertFalse(any(thread.is_alive() for thread in workers))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), worker_count)
        for snapshot in results:
            dynamic = [
                model for model in snapshot["models"]
                if model.startswith("gpt-snapshot-")
            ]
            self.assertEqual(len(dynamic), 1)
            suffix = dynamic[0].rsplit("-", 1)[-1]
            self.assertEqual(snapshot["auth_generation"], f"auth-{suffix}")
            self.assertIs(
                snapshot["models"][dynamic[0]]["fast"],
                suffix == "a",
            )

        # Returned values must not alias the published cache.
        results[0]["models"].clear()
        self.assertTrue(assistant._codex_catalog_cache["models"])

    def test_auth_change_during_active_thread_fails_fast_closed(self):
        verified = {
            "ts": 0.0,
            "models": {
                "gpt-5.6-sol": {
                    "available": True,
                    "selectable": True,
                    "catalog_advertised": True,
                    "depths": ["low"],
                    "depths_verified": True,
                    "service_tiers": ["priority"],
                    "priority": True,
                    "fast": True,
                }
            },
            "verified": True,
            "error": "",
            "auth_generation": "auth-old",
        }
        assistant._codex_catalog_cache.clear()
        assistant._codex_catalog_cache.update(verified)
        generations = iter(("auth-old", "auth-new"))

        with (
            patch.object(
                assistant,
                "_codex_rc_bootstrap",
                side_effect=lambda: _bootstrap(next(generations)),
            ),
            patch.object(
                assistant._codex_app,
                "model_catalog",
                side_effect=RuntimeError(
                    "Codex 认证已更新，等待当前多轮会话结束后切换"
                ),
            ),
        ):
            snapshot = assistant._codex_catalog(force=True)

        self.assertEqual(snapshot["auth_generation"], "auth-new")
        self.assertFalse(snapshot["verified"])
        self.assertFalse(
            snapshot["models"]["gpt-5.6-sol"]["fast"]
        )
        spark = snapshot["models"]["gpt-5.3-codex-spark"]
        self.assertTrue(spark["selectable"])
        self.assertFalse(spark["fast"])


if __name__ == "__main__":
    unittest.main()
