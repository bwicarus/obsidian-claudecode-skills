from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from contextlib import contextmanager


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))

from reader_book_library import BookLibrary  # noqa: E402
from reader_sidecar_store import exclusive_lock  # noqa: E402
import reader_book_ocr  # noqa: E402
import reader_book_ocr_worker  # noqa: E402
from reader_book_ocr import ReaderBookOcrError, ReaderBookOcrService  # noqa: E402
from reader_book_ocr_worker import (  # noqa: E402
    _detect_ruled_table_grids,
    _manga_align_visual_segments,
    _manga_page,
    _manga_page_layout,
    _manga_line_char_boxes,
    _manga_table_cell_lines,
    _manga_vision_line_chars,
    _proven_table_layout_grids,
    _publish_attachments,
    _publish_release as _raw_publish_release,
    _tokenize_chars,
    _vision_page_layout,
)


PDF_A = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"


def _publish_release(args, job_dir, formula_path, final_job, **kwargs):
    """Exercise the real publication precondition in direct worker tests."""

    control_path = Path(job_dir) / "control.json"
    if not control_path.exists():
        control_path.write_text('{"desiredState":"running"}', "utf-8")
    return _raw_publish_release(args, job_dir, formula_path, final_job, **kwargs)


def _with_test_layout(page: dict) -> dict:
    value = dict(page)
    value["layout"] = reader_book_ocr_worker._vision_page_layout(
        value.get("chars") or [],
        page_w=float(value["page_w"]),
        page_h=float(value["page_h"]),
    )
    return value


class _FakeProcess:
    def __init__(self, pid: int | None = None) -> None:
        self.pid = int(pid or os.getpid())



class ReaderBookOcrReleaseIndexTest(unittest.TestCase):
    """多份预处理结果：并存、带日期、可切换、可删除。

    用户 2026-08-18：「我希望书库里能够删除预处理的结果，还有预处理的结果标记上
    日期用以区分，而不是覆盖或者拒绝进行多次预处理」。
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.vault = self.base / "vault"
        self.vault.mkdir()
        (self.vault / "A.pdf").write_bytes(PDF_A)
        self.library = BookLibrary(self.vault, self.base / "catalog")
        self.entry = self.library.catalog()[0]
        self.service = ReaderBookOcrService(
            self.library,
            self.base / "ocr",
            self.base / "project",
            launcher=lambda *args: _FakeProcess(1),
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
        )
        self.version = self.service._version_dir(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.run_sequence = 0

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _publish(
        self,
        engine: str,
        char: str,
        *,
        run_id: str | None = None,
        updated_at: int | None = None,
    ) -> str:
        self.run_sequence += 1
        run_id = run_id or f"ocrrun_{self.run_sequence:016x}"
        updated_at = updated_at if updated_at is not None else self.run_sequence
        job_dir = self.version / engine
        pages = job_dir / "pages"
        pages.mkdir(parents=True, exist_ok=True)
        (pages / "p000001.json").write_text(json.dumps(_with_test_layout({
            "schema": "reader-page-chars/1",
            "bookId": self.entry["bookId"],
            "contentSha256": self.entry["contentSha256"],
            "engine": engine,
            "pageNumber": 1,
            "page_w": 10,
            "page_h": 20,
            "chars": [{"c": char, "x0": 1, "y0": 1, "x1": 2, "y1": 2}],
            "furigana": [],
        })), "utf-8")
        formula_path = job_dir / "formula-source.json"
        formula_path.write_text('{"formulas":[]}', "utf-8")
        final_job = {
            "contract": "reader-library-ocr/1",
            "jobId": f"ocrjob_{engine}_{self.run_sequence}",
            "runId": run_id,
            "bookId": self.entry["bookId"],
            "contentSha256": self.entry["contentSha256"],
            "engine": engine,
            "state": "succeeded",
            "totalPages": 1,
            "successfulPages": 1,
            "formulaState": "unavailable",
            "formulaTotal": 0,
            "resultAvailable": True,
            "createdAtEpochMs": updated_at,
            "updatedAtEpochMs": updated_at,
        }
        (job_dir / "job.json").write_text(json.dumps(final_job), "utf-8")
        return _publish_release(
            SimpleNamespace(
                book_id=self.entry["bookId"],
                content_sha256=self.entry["contentSha256"],
                engine=engine,
                max_bytes=1024 * 1024,
            ),
            job_dir,
            formula_path,
            final_job,
            source_path=self.vault / "A.pdf",
        )

    def _list(self) -> dict:
        return self.service.list_releases(
            self.entry["bookId"], self.entry["contentSha256"]
        )

    def test_two_runs_coexist_with_dates_and_one_is_active(self) -> None:
        first = self._publish("vision", "V")
        second = self._publish("manga", "M")
        # The detached Pi publication path must commit the ledger immediately;
        # list_releases() must not be required to repair split-brain state.
        committed = json.loads(
            (self.version / "releases-index.json").read_text("utf-8")
        )
        self.assertEqual(
            {run["revision"] for run in committed["runs"]}, {first, second}
        )
        self.assertEqual(
            next(
                run["revision"]
                for run in committed["runs"]
                if run["runId"] == committed["activeRunId"]
            ),
            second,
        )
        self.assertEqual(
            json.loads((self.version / "publication.json").read_text("utf-8"))["revision"],
            second,
        )
        self.assertEqual(
            json.loads((self.version / "current.json").read_text("utf-8"))["revision"],
            second,
        )
        self.assertTrue(self.service.lock_path.is_file())
        self.assertFalse((self.version / ".jobs.lock").exists())
        listing = self._list()
        revisions = {run["revision"] for run in listing["runs"]}
        self.assertEqual(revisions, {first, second})
        # 磁盘上本来就并存了；缺的只是枚举 + 日期 + 切换。
        self.assertEqual(len(listing["runs"]), 2)
        for run in listing["runs"]:
            self.assertRegex(run["runId"], r"^ocrrun_[0-9a-f]{16}$")
            # 日期不编造：取不到就是 None，UI 显示"日期未知"。
            self.assertTrue(
                run["publishedAtEpochMs"] is None
                or isinstance(run["publishedAtEpochMs"], int)
            )
        active = [run for run in listing["runs"] if run["isActive"]]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["revision"], second)

    def test_same_revision_runs_remain_distinct_and_share_physical_release(self) -> None:
        first_run = "ocrrun_00000000000000a1"
        second_run = "ocrrun_00000000000000a2"
        first_revision = self._publish(
            "vision", "V", run_id=first_run, updated_at=100
        )
        second_revision = self._publish(
            "vision", "V", run_id=second_run, updated_at=200
        )
        self.assertEqual(first_revision, second_revision)

        listing = self._list()
        self.assertEqual(
            {run["runId"] for run in listing["runs"]},
            {first_run, second_run},
        )
        self.assertEqual(listing["activeRunId"], second_run)
        status = self.service.status(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertEqual(status["runId"], second_run)
        self.assertEqual(status["jobId"], "ocrjob_vision_2")
        mutable_job = json.loads(
            (self.version / "vision" / "job.json").read_text("utf-8")
        )
        self.assertEqual(mutable_job["runId"], second_run)
        self.assertEqual(mutable_job["jobId"], "ocrjob_vision_2")
        self.assertEqual(
            next(run for run in listing["runs"] if run["runId"] == first_run)[
                "sameRevisionRunIds"
            ],
            [second_run],
        )

        switched = self.service.activate_run(
            self.entry["bookId"], self.entry["contentSha256"], first_run
        )
        self.assertEqual(switched["activeRunId"], first_run)
        after_one_delete = self.service.delete_run(
            self.entry["bookId"], self.entry["contentSha256"], second_run
        )
        self.assertEqual([run["runId"] for run in after_one_delete["runs"]], [first_run])
        self.assertTrue(
            (self.version / "releases" / first_revision).is_dir(),
            "the shared immutable release must survive while one run still references it",
        )
        after_last_delete = self.service.delete_run(
            self.entry["bookId"],
            self.entry["contentSha256"],
            first_run,
            allow_deactivate=True,
        )
        self.assertEqual(after_last_delete["runs"], [])
        self.assertIsNone(after_last_delete["activeRunId"])
        self.assertFalse((self.version / "releases" / first_revision).exists())

    def test_status_repairs_same_revision_run_after_index_terminal_crash(self) -> None:
        self._publish(
            "vision", "V", run_id="ocrrun_00000000000000a1", updated_at=100
        )
        job_dir = self.version / "vision"
        job_path = job_dir / "job.json"
        second = {
            "contract": "reader-library-ocr/1",
            "jobId": "ocrjob_same_revision_second",
            "runId": "ocrrun_00000000000000a2",
            "workerGeneration": "ocrgen_same_revision_second",
            "bookId": self.entry["bookId"],
            "contentSha256": self.entry["contentSha256"],
            "engine": "vision",
            "state": "running",
            "totalPages": 1,
            "successfulPages": 1,
            "formulaState": "unavailable",
            "formulaTotal": 0,
            "resultAvailable": False,
            "createdAtEpochMs": 200,
            "updatedAtEpochMs": 200,
        }
        job_path.write_text(json.dumps(second), "utf-8")
        terminal = {
            **second,
            "state": "succeeded",
            "resultAvailable": True,
        }
        index_path = self.version / "releases-index.json"
        real_atomic = reader_book_ocr_worker._atomic_json

        def crash_before_terminal(path, value):
            if Path(path) == job_path and index_path.exists():
                index = json.loads(index_path.read_text("utf-8"))
                if index.get("activeRunId") == second["runId"]:
                    raise OSError("crash before mutable terminal replace")
            return real_atomic(path, value)

        with patch.object(
            reader_book_ocr_worker,
            "_atomic_json",
            side_effect=crash_before_terminal,
        ):
            with self.assertRaises(OSError):
                _raw_publish_release(
                    SimpleNamespace(
                        book_id=self.entry["bookId"],
                        content_sha256=self.entry["contentSha256"],
                        engine="vision",
                        max_bytes=1024 * 1024,
                    ),
                    job_dir,
                    job_dir / "formula-source.json",
                    terminal,
                    source_path=self.vault / "A.pdf",
                    terminal_job=terminal,
                )

        committed = json.loads(index_path.read_text("utf-8"))
        self.assertEqual(committed["activeRunId"], second["runId"])
        self.assertEqual(json.loads(job_path.read_text("utf-8"))["state"], "running")
        status = self.service.status(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertEqual(status["state"], "succeeded")
        self.assertEqual(status["runId"], second["runId"])
        self.assertEqual(status["jobId"], second["jobId"])

    def test_reconcile_migrates_split_publication_once_then_index_stays_truth(self) -> None:
        first_revision = self._publish("vision", "A")
        first_fence = json.loads((self.version / "publication.json").read_text("utf-8"))
        first_run = next(
            run
            for run in json.loads(
                (self.version / "releases-index.json").read_text("utf-8")
            )["runs"]
            if run["revision"] == first_revision
        )
        second_revision = self._publish("manga", "B")
        second_fence = json.loads((self.version / "publication.json").read_text("utf-8"))

        # Production split shape: the immutable v6 release and publication
        # exist, but the old v5-only ledger has never heard of that release.
        stale_index = {
            "contract": reader_book_ocr.RELEASE_INDEX_CONTRACT,
            "bookId": self.entry["bookId"],
            "contentSha256": self.entry["contentSha256"],
            "generation": 7,
            "activeRunId": first_run["runId"],
            "runs": [first_run],
        }
        (self.version / "releases-index.json").write_text(
            json.dumps(stale_index), "utf-8"
        )
        (self.version / "publication.json").write_text(
            json.dumps(second_fence), "utf-8"
        )
        migrated = self._list()
        self.assertEqual({run["revision"] for run in migrated["runs"]}, {
            first_revision,
            second_revision,
        })
        active_revision = next(
            run["revision"] for run in migrated["runs"] if run["isActive"]
        )
        self.assertEqual(active_revision, second_revision)

        # Once both releases are represented, a stale compatibility mirror is
        # no longer allowed to move activeRunId back to v5.
        (self.version / "publication.json").write_text(
            json.dumps(first_fence), "utf-8"
        )
        stable = self._list()
        self.assertEqual(
            next(run["revision"] for run in stable["runs"] if run["isActive"]),
            second_revision,
        )
        self.assertEqual(
            json.loads((self.version / "publication.json").read_text("utf-8"))[
                "revision"
            ],
            second_revision,
        )

    def test_existing_corrupt_or_wrong_identity_index_fails_closed(self) -> None:
        self._publish("vision", "V")
        index_path = self.version / "releases-index.json"
        valid = json.loads(index_path.read_text("utf-8"))
        cases = (
            b"{not-json",
            json.dumps({**valid, "contentSha256": "0" * 64}).encode("utf-8"),
            json.dumps({**valid, "bookId": "book_" + "f" * 32}).encode("utf-8"),
            json.dumps(
                {**valid, "activeRunId": "ocrrun_ffffffffffffffff"}
            ).encode("utf-8"),
        )
        for payload in cases:
            with self.subTest(payload=payload[:24]):
                index_path.write_bytes(payload)
                before = index_path.read_bytes()
                with self.assertRaises(ReaderBookOcrError) as caught:
                    self._list()
                self.assertEqual(caught.exception.code, "ocr-publication-invalid")
                self.assertEqual(index_path.read_bytes(), before)
                index_path.write_text(json.dumps(valid), "utf-8")

    def test_delete_index_failure_restores_release_before_unlock(self) -> None:
        first = self._publish("vision", "A")
        self._publish("manga", "B")
        listing = self._list()
        victim = next(run for run in listing["runs"] if run["revision"] == first)
        release_dir = self.version / "releases" / first

        with patch.object(
            self.service, "_write_index_locked", side_effect=OSError("index failed")
        ):
            with self.assertRaises(OSError):
                self.service.delete_run(
                    self.entry["bookId"],
                    self.entry["contentSha256"],
                    victim["runId"],
                )
        self.assertTrue(release_dir.is_dir())
        self.assertFalse(any(self.version.glob(".trash-delete-*")))
        self.assertIn(victim["runId"], {run["runId"] for run in self._list()["runs"]})

    def test_delete_commit_crash_trash_never_reenters_migration(self) -> None:
        self._publish("vision", "A")
        active = next(run for run in self._list()["runs"] if run["isActive"])
        real_rmtree = shutil.rmtree

        def leave_delete_trash(path, *args, **kwargs):
            if Path(path).name.startswith(".trash-delete-"):
                return None
            return real_rmtree(path, *args, **kwargs)

        with patch.object(
            self.service, "_repair_active_mirrors_locked", return_value=None
        ), patch.object(shutil, "rmtree", side_effect=leave_delete_trash):
            deleted = self.service.delete_run(
                self.entry["bookId"],
                self.entry["contentSha256"],
                active["runId"],
                allow_deactivate=True,
            )
        self.assertEqual(deleted["runs"], [])
        leftovers = list(self.version.glob(".trash-delete-*"))
        self.assertEqual(len(leftovers), 1)
        malformed = self.version / ".trash-delete-vision-not-a-revision-deadbeef"
        malformed.mkdir()
        real_lock = reader_book_ocr.exclusive_lock
        real_rmtree = shutil.rmtree
        lock_depth = 0

        @contextmanager
        def tracked_jobs_lock(path):
            nonlocal lock_depth
            with real_lock(path):
                lock_depth += 1
                try:
                    yield
                finally:
                    lock_depth -= 1

        def checked_gc(path, *args, **kwargs):
            if Path(path).name.startswith(".trash-delete-"):
                self.assertEqual(
                    lock_depth, 0, "delete-trash GC must run outside jobs.lock"
                )
            return real_rmtree(path, *args, **kwargs)

        with patch.object(
            reader_book_ocr, "exclusive_lock", side_effect=tracked_jobs_lock
        ), patch.object(shutil, "rmtree", side_effect=checked_gc):
            after_restart = self._list()
        self.assertEqual(after_restart["runs"], [])
        self.assertIsNone(after_restart["activeRunId"])
        self.assertFalse(leftovers[0].exists())
        self.assertTrue(malformed.is_dir())

    def test_delete_rename_crash_before_index_commit_is_recovered(self) -> None:
        revision = self._publish("vision", "A")
        before = self._list()
        release_dir = self.version / "releases" / revision
        trash = self.version / (
            f".trash-delete-vision-{revision}-{'f' * 32}"
        )
        os.replace(release_dir, trash)

        recovered = self._list()
        self.assertEqual(recovered["activeRunId"], before["activeRunId"])
        self.assertTrue(release_dir.is_dir())
        self.assertFalse(trash.exists())

    def test_activate_validates_target_before_committing_active_run(self) -> None:
        first_revision = self._publish("vision", "A")
        self._publish("manga", "B")
        before = self._list()
        target = next(run for run in before["runs"] if run["revision"] == first_revision)
        release_result = self.version / "releases" / first_revision / "result.json"
        damaged = json.loads(release_result.read_text("utf-8"))
        damaged.pop("sourceIdentity", None)
        release_result.write_text(json.dumps(damaged), "utf-8")

        with self.assertRaises(ReaderBookOcrError):
            self.service.activate_run(
                self.entry["bookId"], self.entry["contentSha256"], target["runId"]
            )
        committed = json.loads(
            (self.version / "releases-index.json").read_text("utf-8")
        )
        self.assertEqual(committed["activeRunId"], before["activeRunId"])

    def test_none_active_repairs_stale_release_mirrors(self) -> None:
        self._publish("vision", "V")
        index_path = self.version / "releases-index.json"
        index = json.loads(index_path.read_text("utf-8"))
        index["activeRunId"] = None
        index_path.write_text(json.dumps(index), "utf-8")
        for name in ("publication.json", "result.json", "current.json"):
            self.assertTrue((self.version / name).exists())

        listing = self._list()
        self.assertIsNone(listing["activeRunId"])
        for name in ("publication.json", "result.json", "current.json"):
            self.assertFalse((self.version / name).exists())

    def test_activate_switches_the_published_result(self) -> None:
        first = self._publish("vision", "V")
        self._publish("manga", "M")
        target = next(
            run for run in self._list()["runs"] if run["revision"] == first
        )
        after = self.service.activate_run(
            self.entry["bookId"], self.entry["contentSha256"], target["runId"]
        )
        self.assertEqual(after["activeRunId"], target["runId"])
        status = self.service.status(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertEqual(status["engine"], "vision")

    def test_deleting_a_non_active_run_keeps_the_active_one_working(self) -> None:
        first = self._publish("vision", "V")
        self._publish("manga", "M")
        victim = next(
            run for run in self._list()["runs"] if run["revision"] == first
        )
        after = self.service.delete_run(
            self.entry["bookId"], self.entry["contentSha256"], victim["runId"]
        )
        self.assertEqual(len(after["runs"]), 1)
        self.assertFalse(
            (self.version / "releases" / first).exists(),
            "物理目录应当已被删除",
        )
        status = self.service.status(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertEqual(status["engine"], "manga")

    def test_deleting_the_active_run_requires_explicit_confirmation(self) -> None:
        self._publish("vision", "V")
        listing = self._list()
        active = next(run for run in listing["runs"] if run["isActive"])
        with self.assertRaises(ReaderBookOcrError) as caught:
            self.service.delete_run(
                self.entry["bookId"], self.entry["contentSha256"], active["runId"]
            )
        self.assertEqual(caught.exception.code, "ocr-run-active")
        self.assertEqual(caught.exception.status, 409)

    def test_deleting_the_last_run_does_not_brick_the_book(self) -> None:
        """删完最后一份之后，status 必须还能查 —— 否则连重跑都发不出去。

        旧的 _published_snapshot 对任何缺件一律抛 500，而 status() 无条件调它：
        删除一旦上线，这本书就会被焊死。这条测试钉住那个自愈分支。
        """

        self._publish("vision", "V")
        active = next(run for run in self._list()["runs"] if run["isActive"])
        after = self.service.delete_run(
            self.entry["bookId"],
            self.entry["contentSha256"],
            active["runId"],
            allow_deactivate=True,
        )
        self.assertEqual(after["runs"], [])
        self.assertIsNone(after["activeRunId"])
        status = self.service.status(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertEqual(status["state"], "idle")

    def test_stale_fence_pointing_at_a_removed_release_is_not_fatal(self) -> None:
        """围栏还在、结果目录没了（删除中途崩溃）时，也不能把书焊死。"""

        revision = self._publish("vision", "V")
        shutil.rmtree(self.version / "releases" / revision)
        self.assertTrue((self.version / "publication.json").exists())
        # 要害是**不再 500**：旧行为会在这里抛 ocr-publication-invalid，
        # 而 status() 是无条件调 _published_snapshot 的，于是整本书连状态都查不了。
        status = self.service.status(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertIn("state", status)
        # 而且必须还能重新发起预处理 —— 否则"查得到状态"也只是好看而已。
        job, _already = self.service.start(
            self.entry["bookId"], self.entry["contentSha256"], "vision"
        )
        self.assertEqual(job["state"], "queued")
        # ⚠ 已知不足：此时 status 会报 failed / ocr-publication-incomplete，
        #   因为上一次的可变暂存还指着已被删的那一版。正常删除路径会把它一并归档
        #   （_archive_stale_staging_locked），只有"删到一半崩了"才会看到这个提示。
        #   它有误导性但不阻塞操作，留作后续。

    def test_results_survive_a_book_id_change(self) -> None:
        """书被重新登记（bookId 变了）之后，旧结果必须还找得回来。

        2026-08-19 Pi 上实测：三本书的预处理结果**全部**挂在已经不在 catalog 里的
        bookId 下，而 catalog 里都有同 contentSha256、不同 bookId 的条目。
        结果既列不出来也用不上，看起来就像从来没跑过。
        结果本来就是内容寻址的，所以按内容找回来是安全的。
        """

        revision = self._publish("vision", "V")
        # 模拟重新登记：把结果目录挪到另一个 bookId 下。
        stale = self.service.state_root / ("book_" + "9" * 32)
        stale.mkdir(parents=True, exist_ok=True)
        shutil.move(str(self.version), str(stale / self.entry["contentSha256"]))
        # A content-addressed alias may retain the directory owner's historical
        # bookId in the ledger; both that owner and the requested catalog id are
        # legal, while unrelated ids still fail closed.
        aliased_version = stale / self.entry["contentSha256"]
        aliased_index_path = aliased_version / "releases-index.json"
        aliased_index = json.loads(aliased_index_path.read_text("utf-8"))
        aliased_index["bookId"] = stale.name
        aliased_index_path.write_text(json.dumps(aliased_index), "utf-8")
        listing = self._list()
        self.assertEqual(len(listing["runs"]), 1)
        self.assertEqual(listing["runs"][0]["revision"], revision)
        # 而且要真能用 —— 不只是列得出来。
        status = self.service.status(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertEqual(status["engine"], "vision")

    def test_run_ids_are_stable_across_repeated_listing(self) -> None:
        """回填是按需重跑的；runId 必须确定性派生，否则每次列举都变成"新的一条"。"""

        self._publish("vision", "V")
        first = {run["runId"] for run in self._list()["runs"]}
        second = {run["runId"] for run in self._list()["runs"]}
        self.assertEqual(first, second)



class ReaderBookOcrVisionGeometryTest(unittest.TestCase):
    """送 Vision 的图必须保住有效 DPI，否则文字层的框会高到吃掉行距。

    用户 2026-08-19：「pc预处理vision文字区的高度太高导致选择下方时会一起选择上方，
    而且这里明显分词也有问题」。量到的实据（同一套代码、三份已有结果）：

        595x890pt  -> 400dpi -> 框高/行距 0.51，行重叠 0/28
        515x731pt  -> 300dpi -> 框高/行距 0.64，行重叠 2/38
        1684x2405pt-> 120dpi -> 框高/行距 1.34，行重叠 31/56   <- 就是这本

    第三本被固定的长边像素封顶压到了 120dpi。页面越大压得越狠，这是封顶的
    数学必然，跟引擎、执行者都无关。
    """

    def setUp(self) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_server_deploy"))
        import reader_book_ocr_worker  # type: ignore

        self.worker = reader_book_ocr_worker

    def _page(self, width_pt: float, height_pt: float):
        import fitz  # type: ignore

        document = fitz.open()
        document.new_page(width=width_pt, height=height_pt)
        return document, document[0]

    def test_oversized_page_still_reaches_the_dpi_floor(self) -> None:
        """1684x2405pt 那本必须不再掉到 120dpi。"""

        document, page = self._page(1684.0, 2405.0)
        try:
            _image, image_w, _image_h, dpi = self.worker._vision_render(page)
        finally:
            document.close()
        self.assertGreaterEqual(
            dpi,
            self.worker.VISION_MIN_DPI,
            "超大页面被压到保底 DPI 以下 —— 框会重新开始糊",
        )
        # 有效 DPI 就是"每页面英寸多少像素"，不是别的什么估计值。
        self.assertAlmostEqual(dpi, image_w / 1684.0 * 72.0, places=1)

    def test_normal_page_gets_the_target_dpi(self) -> None:
        document, page = self._page(515.0, 731.0)
        try:
            _image, _w, _h, dpi = self.worker._vision_render(page)
        finally:
            document.close()
        self.assertAlmostEqual(dpi, self.worker.VISION_TARGET_DPI, delta=1.0)

    def test_upload_stays_within_the_request_budget(self) -> None:
        """封顶换成字节预算之后，它必须真的封住 —— 否则 Vision 会拒收整页。

        空白页在任何分辨率下都是几十 KB，拿它测预算等于什么都没测。
        把预算压到必然触发回退的量级，才看得出回退是不是真的在工作。
        """

        document, page = self._page(1684.0, 2405.0)
        try:
            _image, _w, _h, uncapped_dpi = self.worker._vision_render(page)
            budget = 40_000
            with patch.object(self.worker, "VISION_MAX_UPLOAD_BYTES", budget):
                image, _w2, _h2, capped_dpi = self.worker._vision_render(page)
        finally:
            document.close()
        self.assertLess(
            capped_dpi,
            uncapped_dpi,
            "预算压到 40KB 都没降分辨率 —— 回退根本没触发",
        )
        self.assertLess(len(image), len(_image), "降了分辨率字节却没变小")
        # 故意不断言"一定进预算"：大片均匀区域的 JPEG 几乎不随分辨率变小，
        # 硬要进预算就得把页面压成糊的。预算是我们保守的自我约束，不是 Vision
        # 的硬限制 —— 压到地板还超就照发，这是有意的选择。

    def test_budget_pressure_does_not_stop_at_the_floor_forever(self) -> None:
        """预算实在塞不下时，宁可降到保底以下也要把请求发出去 —— 但要留痕。

        `_vision_render` 返回真实有效 DPI，调用方据此写 visionDpiShortfall；
        这里钉住的是"不会因为够不到保底就卡死或抛"。
        """

        document, page = self._page(1684.0, 2405.0)
        try:
            with patch.object(self.worker, "VISION_MAX_UPLOAD_BYTES", 1_000):
                image, _w, _h, dpi = self.worker._vision_render(page)
        finally:
            document.close()
        self.assertGreater(dpi, 0.0)
        self.assertGreater(len(image), 0)

    def test_pixel_cap_no_longer_decides_resolution(self) -> None:
        """同一本书的两倍尺寸版本不该拿到一半的 DPI。

        这条直接钉住旧行为的形状：固定长边封顶下，页面翻倍 = DPI 减半。
        """

        small_doc, small_page = self._page(842.0, 1202.0)
        big_doc, big_page = self._page(1684.0, 2405.0)
        try:
            _i, _w, _h, small_dpi = self.worker._vision_render(small_page)
            _i2, _w2, _h2, big_dpi = self.worker._vision_render(big_page)
        finally:
            small_doc.close()
            big_doc.close()
        self.assertGreater(
            big_dpi,
            small_dpi * 0.6,
            "页面变大就掉 DPI —— 固定像素封顶又回来了",
        )


class ReaderBookOcrJapaneseTokenizationTest(unittest.TestCase):
    """日文页一律用 fugashi 重新分词，不拿 Vision 的 word 分组当分词。

    Vision 对日文的分组实测是碎的（每组中位 1-2 字，「フランス」「受けている」
    都会被切开）。旧条件是"只要 Vision 给了 word id 就跳过分词"，等于把日文的
    分词权交给了一个不做日文分词的东西。
    """

    def setUp(self) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_server_deploy"))
        import reader_book_ocr_worker  # type: ignore

        self.worker = reader_book_ocr_worker
        self.temp = tempfile.TemporaryDirectory()
        self.job_dir = Path(self.temp.name)
        (self.job_dir / "pages").mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_page(self, text: str) -> Path:
        # Vision 的形状：每个字都带 word id（>= 0），且逐字成词。
        chars = [
            {
                "c": character,
                "x0": float(index),
                "y0": 0.0,
                "x1": float(index) + 1.0,
                "y1": 1.0,
                "w": index,
                "bk": 1,
                "b": 0,
            }
            for index, character in enumerate(text)
        ]
        path = self.job_dir / "pages" / "p000001.json"
        path.write_text(json.dumps({
            "schema": "reader-page-chars/1",
            "engine": "vision",
            "pageNumber": 1,
            "page_w": 100.0,
            "page_h": 100.0,
            "chars": chars,
            "furigana": [],
        }), "utf-8")
        return path

    def test_japanese_vision_page_is_retokenized(self) -> None:
        path = self._write_page("中国やフランスの影響を受けている")
        self.worker._tokenize_directory(self.job_dir)
        chars = json.loads(path.read_text("utf-8"))["chars"]
        groups: dict[int, str] = {}
        for char in chars:
            groups.setdefault(int(char["w"]), "")
            groups[int(char["w"])] += char["c"]
        words = list(groups.values())
        self.assertIn("フランス", words, f"「フランス」仍被切开：{words}")
        self.assertIn("中国", words, f"「中国」仍被切开：{words}")
        self.assertLess(
            len(words),
            len("中国やフランスの影響を受けている"),
            "还是逐字成词 —— Vision 的分组没有被换掉",
        )

    def test_non_japanese_vision_page_keeps_vision_grouping(self) -> None:
        """英文页 Vision 的空格分词是可靠的，不该被推翻。"""

        path = self._write_page("hello")
        before = json.loads(path.read_text("utf-8"))["chars"]
        self.worker._tokenize_directory(self.job_dir)
        after = json.loads(path.read_text("utf-8"))["chars"]
        self.assertEqual(
            [char["w"] for char in before],
            [char["w"] for char in after],
        )



class ReaderBookOcrForcedRerunTest(unittest.TestCase):
    """`force=True` 才真的再跑一份；默认仍然复用已发布结果。

    用户 2026-08-18：「而不是覆盖或者拒绝进行多次预处理」。台账把"多份并存"
    做出来了，但 start() 仍然在已有结果时直接返回旧的那一份 —— 于是修好了
    OCR 参数也重跑不出来，改进到不了用户手上。
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.vault = self.base / "vault"
        self.vault.mkdir()
        (self.vault / "A.pdf").write_bytes(PDF_A)
        self.library = BookLibrary(self.vault, self.base / "catalog")
        self.entry = self.library.catalog()[0]
        self.service = ReaderBookOcrService(
            self.library,
            self.base / "ocr",
            self.base / "project",
            launcher=lambda *args: _FakeProcess(1),
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
        )
        self.version = self.service._version_dir(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self._publish()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _publish(self) -> str:
        job_dir = self.version / "vision"
        pages = job_dir / "pages"
        pages.mkdir(parents=True, exist_ok=True)
        (pages / "p000001.json").write_text(json.dumps(_with_test_layout({
            "schema": "reader-page-chars/1",
            "bookId": self.entry["bookId"],
            "contentSha256": self.entry["contentSha256"],
            "engine": "vision",
            "executor": "pi",
            "processingProfile": "pi-default-v5",
            "pageNumber": 1,
            "page_w": 10,
            "page_h": 20,
            "chars": [{"c": "x", "x0": 1, "y0": 1, "x1": 2, "y1": 2}],
            "furigana": [],
        })), "utf-8")
        formula_path = job_dir / "formula-source.json"
        formula_path.write_text('{"formulas":[]}', "utf-8")
        final_job = {
            "contract": "reader-library-ocr/1",
            "jobId": "ocrjob_seed",
            "runId": "ocrrun_0000000000000001",
            "bookId": self.entry["bookId"],
            "contentSha256": self.entry["contentSha256"],
            "engine": "vision",
            "executor": "pi",
            "processingProfile": "pi-default-v5",
            "state": "succeeded",
            "totalPages": 1,
            "successfulPages": 1,
            "formulaState": "unavailable",
            "formulaTotal": 0,
            "resultAvailable": True,
            "updatedAtEpochMs": 1,
        }
        (job_dir / "job.json").write_text(json.dumps(final_job), "utf-8")
        return _publish_release(
            SimpleNamespace(
                book_id=self.entry["bookId"],
                content_sha256=self.entry["contentSha256"],
                engine="vision",
                max_bytes=1024 * 1024,
            ),
            job_dir,
            formula_path,
            final_job,
            source_path=self.vault / "A.pdf",
        )

    def test_default_start_still_reuses_the_published_result(self) -> None:
        """默认不重跑 —— 那是省时省钱的正确默认，别把它一起改掉。"""

        job, already = self.service.start(
            self.entry["bookId"], self.entry["contentSha256"], "vision"
        )
        self.assertTrue(already)
        self.assertEqual(job["state"], "succeeded")

    def test_forced_start_queues_a_new_run(self) -> None:
        job, already = self.service.start(
            self.entry["bookId"], self.entry["contentSha256"], "vision",
            force=True,
        )
        self.assertFalse(already, "force=True 仍然返回了旧结果")
        self.assertEqual(job["state"], "queued")

    def test_each_forced_start_gets_a_stable_unique_run_id(self) -> None:
        self.service.start(
            self.entry["bookId"], self.entry["contentSha256"], "vision", force=True
        )
        job_path = self.version / "vision" / "job.json"
        first = json.loads(job_path.read_text("utf-8"))
        self.assertRegex(first["runId"], r"^ocrrun_[0-9a-f]{16}$")

        # End this generation without publishing, then explicitly start a new
        # run.  Archiving/retry must not reuse the previous run identity.
        job_path.write_text(
            json.dumps({**first, "state": "failed", "resultAvailable": False}),
            "utf-8",
        )
        self.service.start(
            self.entry["bookId"], self.entry["contentSha256"], "vision", force=True
        )
        second = json.loads(job_path.read_text("utf-8"))
        self.assertRegex(second["runId"], r"^ocrrun_[0-9a-f]{16}$")
        self.assertNotEqual(second["runId"], first["runId"])

    def test_forced_start_does_not_resume_the_previous_staging(self) -> None:
        """新参数只作用于"还没做过的页"是最隐蔽的失败形态。

        断点续跑会把上一轮的 successfulPages 继承下来，于是重跑只补剩下的页，
        已经做过的页仍是旧参数的产物 —— 看起来跑了，其实大部分没变。
        """

        job, _already = self.service.start(
            self.entry["bookId"], self.entry["contentSha256"], "vision",
            force=True,
        )
        self.assertEqual(job["processedPages"], 0)
        self.assertEqual(job["successfulPages"], 0)

    def test_forced_rerun_keeps_the_existing_release(self) -> None:
        """重跑期间旧结果必须还在 —— 用户随时可以切回去。"""

        before = self.service.list_releases(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.service.start(
            self.entry["bookId"], self.entry["contentSha256"], "vision",
            force=True,
        )
        after = self.service.list_releases(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertEqual(
            [run["revision"] for run in before["runs"]],
            [run["revision"] for run in after["runs"]],
        )



class ReaderBookOcrPageSchemaContractTest(unittest.TestCase):
    """worker 产的页字段必须全部在服务端白名单里。

    2026-08-19：给页加了 visionEffectiveDpi 却忘了在 `_normalize_pc_page` 放行，
    结果 PC 传上来的**每一页**都被 400 拒，整本 0/53 失败，UI 只显示"PC 预处理
    失败；可重试"—— 重试多少次都一样。

    白名单是拒绝式的（`set(page) - allowed` 非空即拒），这是对的：多出来的字段
    可能是攻击面。但"拒绝式"意味着两边必须同步，而同步靠人记是记不住的 ——
    所以这里直接把两个 worker 的 sidecar 键抓出来跟白名单对。
    """

    @staticmethod
    def _allowed_fields() -> set[str]:
        source = (
            Path(__file__).resolve().parents[1]
            / "_server_deploy" / "reader_book_ocr.py"
        ).read_text("utf-8")
        start = source.index("def _normalize_pc_page(")
        block = source[start:source.index("}", source.index("allowed = {", start))]
        return set(re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"', block))

    @staticmethod
    def _worker_page_fields(relative: str) -> set[str]:
        source = (
            Path(__file__).resolve().parents[1] / relative
        ).read_text("utf-8")
        fields: set[str] = set()
        # 页字典的字面量键：`"schema": PAGE_SCHEMA,` 这种。
        for match in re.finditer(r'"(\w+)":\s*(?![\s\S]{0,3}$)', source):
            fields.add(match.group(1))
        # 事后补写的键：`sidecar["visionEffectiveDpi"] = ...`
        fields |= set(re.findall(r'sidecar\["(\w+)"\]\s*=', source))
        return fields

    def test_new_page_fields_are_allowed_by_the_server(self) -> None:
        allowed = self._allowed_fields()
        # 只挑真正会随页上传的那几个 —— 全量比对会把无关的字面量键也算进来。
        for field in (
            "schema", "bookId", "contentSha256", "engine", "pageNumber",
            "page_w", "page_h", "imageWidth", "imageHeight", "chars",
            "furigana", "textCharCount", "generatedAtEpochMs", "tokenized",
            "visionEffectiveDpi", "visionDpiShortfall", "layout",
        ):
            self.assertIn(field, allowed, f"服务端白名单少了 {field}，PC 传页会被 400 拒")

    def test_pc_worker_sidecar_keys_are_all_allowed(self) -> None:
        """PC worker 显式补写到 sidecar 上的键，必须都能过白名单。"""

        allowed = self._allowed_fields()
        source = (
            Path(__file__).resolve().parents[1]
            / "scripts" / "reader_pc_preprocess_worker.py"
        ).read_text("utf-8")
        written = set(re.findall(r'sidecar\["(\w+)"\]\s*=', source))
        self.assertTrue(written, "没抓到 PC worker 往 sidecar 写的键；正则该修了")
        self.assertEqual(
            written - allowed,
            set(),
            "PC worker 写了服务端不认的页字段 —— 每一页都会被 400 拒",
        )

    def test_a_page_carrying_the_new_fields_passes_normalization(self) -> None:
        """端到端一点的验证：带新字段的页真的能过 `_normalize_pc_page`。"""

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        vault = base / "vault"
        vault.mkdir()
        (vault / "A.pdf").write_bytes(PDF_A)
        library = BookLibrary(vault, base / "catalog")
        entry = library.catalog()[0]
        service = ReaderBookOcrService(
            library, base / "ocr", base / "project",
            launcher=lambda *args: _FakeProcess(1),
            max_pdf_bytes=1024 * 1024, max_pages=100,
        )
        job = {
            "bookId": entry["bookId"],
            "contentSha256": entry["contentSha256"],
            "engine": "vision",
            "executor": "pc",
            "processingProfile": "quality-first-v6",
            "totalPages": 1,
        }
        page = {
            "schema": "reader-page-chars/1",
            "bookId": entry["bookId"],
            "contentSha256": entry["contentSha256"],
            "engine": "vision",
            "pageNumber": 1,
            "page_w": 10.0,
            "page_h": 20.0,
            "imageWidth": 100,
            "imageHeight": 200,
            "chars": [{"c": "x", "x0": 1, "y0": 1, "x1": 2, "y1": 2}],
            "layout": {
                "schema": "reader-page-layout/1",
                "textSource": "vision",
                "layoutSource": "vision",
                "mode": "vision",
                "readingDirection": "ltr",
                "confidence": "high",
                "gridColumns": 1,
                "gridRows": 1,
                "regions": [{
                    "id": 0,
                    "kind": "vision-block",
                    "order": 0,
                    "bounds": [1, 1, 2, 2],
                    "ranges": [[0, 0]],
                    "gridRow": 0,
                    "gridColumn": 0,
                    "rowSpan": 1,
                    "columnSpan": 1,
                    "vertical": False,
                    "tableId": None,
                    "row": None,
                    "column": None,
                }],
                "tables": [],
            },
            "furigana": [],
            "textCharCount": 1,
            "generatedAtEpochMs": 1,
            "tokenized": True,
            "visionEffectiveDpi": 238.4,
            "visionDpiShortfall": False,
        }
        normalized, _payload = service._normalize_pc_page(page, 1, job)
        self.assertEqual(normalized["visionEffectiveDpi"], 238.4)
        self.assertEqual(normalized["layout"]["regions"][0]["ranges"], [[0, 0]])
        without_layout = {key: value for key, value in page.items() if key != "layout"}
        with self.assertRaises(ReaderBookOcrError):
            service._normalize_pc_page(without_layout, 1, job)
        old_job = {**job, "processingProfile": "quality-first-v5"}
        old_page, _payload = service._normalize_pc_page(
            without_layout, 1, old_job
        )
        self.assertNotIn("layout", old_page)

    def test_layout_rejects_missing_or_overlapping_char_indices(self) -> None:
        chars = [
            {"c": "a", "x0": 1, "y0": 1, "x1": 2, "y1": 2},
            {"c": " ", "sp": 1, "x0": 1, "y0": 1, "x1": 2, "y1": 2},
        ]
        layout = {
            "schema": "reader-page-layout/1",
            "textSource": "vision",
            "layoutSource": "vision",
            "mode": "vision",
            "readingDirection": "ltr",
            "confidence": "high",
            "gridColumns": 1,
            "gridRows": 1,
            "regions": [{
                "id": 0, "kind": "vision-block", "order": 0,
                "bounds": [1, 1, 2, 2], "ranges": [[0, 0]],
                "gridRow": 0, "gridColumn": 0,
                "rowSpan": 1, "columnSpan": 1, "vertical": False,
                "tableId": None, "row": None, "column": None,
            }],
            "tables": [],
        }
        with self.assertRaises(ReaderBookOcrError) as missing:
            reader_book_ocr._normalize_page_layout(
                layout, chars, 10, 10,
                code="invalid-worker-page", status=400,
            )
        self.assertEqual(missing.exception.code, "invalid-worker-page")
        invalid_manga_grid = {
            **layout,
            "layoutSource": "manga",
            "mode": "manga",
            "gridColumns": 3,
            "regions": [{
                **layout["regions"][0],
                "kind": "manga-region",
                "ranges": [[0, 1]],
            }],
        }
        with self.assertRaises(ReaderBookOcrError):
            reader_book_ocr._normalize_page_layout(
                invalid_manga_grid, chars, 10, 10,
                code="invalid-worker-page", status=400,
            )
        layout["regions"].append({
            **layout["regions"][0],
            "id": 1,
            "order": 1,
            "ranges": [[0, 1]],
        })
        with self.assertRaises(ReaderBookOcrError):
            reader_book_ocr._normalize_page_layout(
                layout, chars, 10, 10,
                code="invalid-worker-page", status=400,
            )

    def test_layout_rejects_cross_platform_collection_overflow(self) -> None:
        table_layout = {
            "schema": "reader-page-layout/1",
            "textSource": "vision",
            "layoutSource": "ruled-table",
            "mode": "table",
            "readingDirection": "ltr",
            "confidence": "high",
            "gridColumns": 1,
            "gridRows": 1,
            "regions": [],
            "tables": [
                {
                    "id": index,
                    "rows": 1,
                    "columns": 2,
                    "xEdges": [0, 5, 10],
                    "yEdges": [0, 10],
                }
                for index in range(65)
            ],
        }
        with self.assertRaises(ReaderBookOcrError):
            reader_book_ocr._normalize_page_layout(
                table_layout, [], 10, 10,
                code="invalid-worker-page", status=400,
            )

        table_cell_overflow = {
            **table_layout,
            "tables": [
                {
                    "id": index,
                    "rows": 100,
                    "columns": 100,
                    "xEdges": list(range(101)),
                    "yEdges": list(range(101)),
                }
                for index in range(2)
            ],
        }
        with self.assertRaises(ReaderBookOcrError):
            reader_book_ocr._normalize_page_layout(
                table_cell_overflow, [], 100, 100,
                code="invalid-worker-page", status=400,
            )

        chars = [
            {"c": "x", "x0": 0, "y0": 0, "x1": 1, "y1": 1}
            for _index in range(4097)
        ]
        region_layout = {
            **table_layout,
            "layoutSource": "vision",
            "mode": "vision",
            "gridColumns": 1,
            "gridRows": 4096,
            "tables": [],
            "regions": [
                {
                    "id": index,
                    "kind": "vision-block",
                    "order": index,
                    "bounds": [0, 0, 1, 1],
                    "ranges": [[index, index]],
                    "gridRow": min(index, 4095),
                    "gridColumn": 0,
                    "rowSpan": 1,
                    "columnSpan": 1,
                    "vertical": False,
                    "tableId": None,
                    "row": None,
                    "column": None,
                }
                for index in range(4097)
            ],
        }
        with self.assertRaises(ReaderBookOcrError):
            reader_book_ocr._normalize_page_layout(
                region_layout, chars, 10, 10,
                code="invalid-worker-page", status=400,
            )


class ReaderBookOcrFormulaWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        (self.project / "scripts").mkdir()
        (self.project / "scripts" / "yolo_figures.py").write_text("", "utf-8")
        self.pdf = self.project / "book.pdf"
        self.pdf.write_bytes(PDF_A)
        self.job_path = self.project / "job.json"
        self.job_path.write_text(json.dumps({
            "jobId": "ocrjob_formula_test",
            "workerGeneration": "ocrgen_formula_test",
            "totalPages": 588,
        }), "utf-8")
        self.control_path = self.project / "control.json"
        self.control_path.write_text(
            json.dumps({"desiredState": "running"}), "utf-8"
        )
        self.args = SimpleNamespace(
            project=str(self.project),
            pdf=str(self.pdf),
        )
        reader_book_ocr_worker._set_worker_identity(None, None)

    def tearDown(self) -> None:
        reader_book_ocr_worker._set_worker_identity(None, None)
        self.temp.cleanup()

    @staticmethod
    def _completed_child():
        class Child:
            returncode = 0

            @staticmethod
            def poll():
                return 0

        return Child()

    def _rewrite_formula_sidecar(self) -> None:
        formula_path = reader_book_ocr_worker._formula_path(
            self.project, self.pdf
        )
        value = json.loads(formula_path.read_text("utf-8"))
        value["geom_at"] = int(time.time())
        reader_book_ocr_worker._atomic_json(formula_path, value)

    def test_formula_detector_success_requires_this_run_to_rewrite_target(self) -> None:
        captured = {}

        def launch(_cmd, **kwargs):
            captured.update(kwargs)
            kwargs["stdout"].write(b"processing 1 sidecar(s)...\ndone\n")
            kwargs["stdout"].flush()
            self._rewrite_formula_sidecar()
            return self._completed_child()

        with patch.object(
            reader_book_ocr_worker.subprocess, "Popen", side_effect=launch
        ):
            result = reader_book_ocr_worker._run_formula_pipeline(
                self.args, self.job_path, self.control_path
            )

        self.assertEqual(result, 0)
        self.assertIs(captured["stderr"], reader_book_ocr_worker.subprocess.STDOUT)
        self.assertTrue(captured["stdout"].closed)
        logs = list(
            (self.project / "state" / "pdf-figures" / "formula-detect-logs")
            .glob("*.log")
        )
        self.assertEqual(len(logs), 1)
        self.assertIn("ocrjob_formula_test", logs[0].name)
        self.assertIn("processing 1 sidecar", logs[0].read_text("utf-8"))
        if os.name == "posix":
            self.assertEqual(logs[0].stat().st_mode & 0o777, 0o600)

    def test_formula_detector_stdout_error_cannot_be_reported_as_success(self) -> None:
        captured_handle = None

        def launch(_cmd, **kwargs):
            nonlocal captured_handle
            captured_handle = kwargs["stdout"]
            captured_handle.write(
                b"processing 1 sidecar(s)...\n"
                b"  ERROR fake-sidecar.json: missing doclayout_yolo\n"
                b"done\n"
            )
            captured_handle.flush()
            self._rewrite_formula_sidecar()
            return self._completed_child()

        with patch.object(
            reader_book_ocr_worker.subprocess, "Popen", side_effect=launch
        ):
            with self.assertRaisesRegex(RuntimeError, "reported ERROR"):
                reader_book_ocr_worker._run_formula_pipeline(
                    self.args, self.job_path, self.control_path
                )
        self.assertTrue(captured_handle.closed)

    def test_formula_detector_zero_match_and_unchanged_target_fail_closed(self) -> None:
        for output, expected in (
            (b"processing 0 sidecar(s)...\ndone\n", "matched no target sidecar"),
            (b"processing 1 sidecar(s)...\ndone\n", "did not update the target"),
        ):
            with self.subTest(expected=expected):
                def launch(_cmd, **kwargs):
                    kwargs["stdout"].write(output)
                    kwargs["stdout"].flush()
                    return self._completed_child()

                with patch.object(
                    reader_book_ocr_worker.subprocess, "Popen", side_effect=launch
                ):
                    with self.assertRaisesRegex(RuntimeError, expected):
                        reader_book_ocr_worker._run_formula_pipeline(
                            self.args, self.job_path, self.control_path
                        )

    def test_formula_log_open_failure_is_visible_and_does_not_launch(self) -> None:
        with patch.object(
            reader_book_ocr_worker,
            "_open_formula_detect_log",
            side_effect=PermissionError("log path denied"),
        ), patch.object(reader_book_ocr_worker.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(RuntimeError, "log could not open"):
                reader_book_ocr_worker._run_formula_pipeline(
                    self.args, self.job_path, self.control_path
                )
        launch.assert_not_called()

    def test_formula_log_handle_closes_when_detector_cannot_start(self) -> None:
        log_handle = io.BytesIO()
        with patch.object(
            reader_book_ocr_worker,
            "_open_formula_detect_log",
            return_value=log_handle,
        ), patch.object(
            reader_book_ocr_worker.subprocess,
            "Popen",
            side_effect=FileNotFoundError("detector missing"),
        ):
            with self.assertRaisesRegex(RuntimeError, "could not start"):
                reader_book_ocr_worker._run_formula_pipeline(
                    self.args, self.job_path, self.control_path
                )
        self.assertTrue(log_handle.closed)

    def test_formula_log_handle_closes_and_child_stops_on_monitor_failure(self) -> None:
        log_handle = io.BytesIO()

        class Child:
            returncode = None
            terminated = False

            def poll(self):
                return self.returncode

            def terminate(self):
                self.terminated = True
                self.returncode = -15

            def wait(self, timeout):
                return self.returncode

            def kill(self):
                raise AssertionError("terminate should have stopped the child")

        child = Child()
        with patch.object(
            reader_book_ocr_worker,
            "_open_formula_detect_log",
            return_value=log_handle,
        ), patch.object(
            reader_book_ocr_worker.subprocess, "Popen", return_value=child
        ), patch.object(
            reader_book_ocr_worker,
            "_run_controlled",
            side_effect=RuntimeError("job update failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "job update failed"):
                reader_book_ocr_worker._run_formula_pipeline(
                    self.args, self.job_path, self.control_path
                )
        self.assertTrue(child.terminated)
        self.assertTrue(log_handle.closed)

    def test_formula_log_tail_read_is_bounded(self) -> None:
        path = self.project / "large.log"
        path.write_bytes(b"x" * 100_000 + b"TAIL")
        self.assertEqual(
            reader_book_ocr_worker._read_formula_detect_log_tail(path, 8),
            "xxxxTAIL",
        )


class ReaderBookOcrServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.vault = self.base / "vault"
        self.vault.mkdir()
        (self.vault / "A.pdf").write_bytes(PDF_A)
        self.library = BookLibrary(self.vault, self.base / "catalog")
        self.entry = self.library.catalog()[0]
        self.launches = []
        self.fake_worker_pid = 424242
        self.pid_alive_patcher = patch.object(
            reader_book_ocr,
            "_pid_alive",
            side_effect=lambda pid: int(pid or 0) == self.fake_worker_pid,
        )
        self.pid_alive_patcher.start()

        def launch(job_dir, source_path, job):
            self.launches.append((job_dir, source_path, dict(job)))
            return _FakeProcess(self.fake_worker_pid)

        self.service = ReaderBookOcrService(
            self.library,
            self.base / "ocr",
            ROOT,
            launcher=launch,
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
        )

    def tearDown(self) -> None:
        self.pid_alive_patcher.stop()
        self.temp.cleanup()

    def _publish_pc_release(
        self,
        service: ReaderBookOcrService,
        *,
        engine: str,
        profile: str,
        text: str,
    ) -> str:
        version_dir = service._version_dir(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        job_dir = version_dir / engine
        pages_dir = job_dir / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)
        page = _with_test_layout({
            "schema": "reader-page-chars/1",
            "bookId": self.entry["bookId"],
            "contentSha256": self.entry["contentSha256"],
            "engine": engine,
            "executor": "pc",
            "processingProfile": profile,
            "pageNumber": 1,
            "page_w": 10,
            "page_h": 20,
            "chars": [{"c": text, "x0": 1, "y0": 1, "x1": 2, "y1": 2}],
            "furigana": [],
        })
        (pages_dir / "p000001.json").write_text(json.dumps(page), "utf-8")
        formula_path = job_dir / "formula.json"
        formula_path.write_text(json.dumps({
            "schema": "reader-formula-regions/1",
            "bookId": self.entry["bookId"],
            "contentSha256": self.entry["contentSha256"],
            "formulas": [],
        }), "utf-8")
        job = {
            "contract": "reader-library-ocr/1",
            "jobId": "ocrjob_existing_" + engine,
            "bookId": self.entry["bookId"],
            "contentSha256": self.entry["contentSha256"],
            "engine": engine,
            "executor": "pc",
            "processingProfile": profile,
            "state": "succeeded",
            "totalPages": 1,
            "successfulPages": 1,
            "formulaState": "succeeded",
            "formulaTotal": 0,
            "resultAvailable": True,
            "createdAtEpochMs": 50,
            "updatedAtEpochMs": 100,
        }
        (job_dir / "job.json").write_text(json.dumps(job), "utf-8")
        return _publish_release(
            SimpleNamespace(
                book_id=self.entry["bookId"],
                content_sha256=self.entry["contentSha256"],
                engine=engine,
                max_bytes=1024 * 1024,
            ),
            job_dir,
            formula_path,
            job,
            source_path=self.vault / "A.pdf",
        )

    def _prepare_pc_completion(
        self, service: ReaderBookOcrService, *, engine: str = "vision"
    ) -> tuple[dict, Path]:
        job, already = service.start(
            self.entry["bookId"],
            self.entry["contentSha256"],
            engine,
            "pc",
            force=True,
        )
        self.assertFalse(already)
        claimed = service.claim_pc_worker("pc_test", {
            "engines": [engine],
            "maxPdfBytes": 1024 * 1024,
            "maxPageBytes": 1024 * 1024,
            "processingProfile": "quality-first-v6",
        })
        self.assertIsNotNone(claimed)
        identity = {
            "contract": reader_book_ocr.WORKER_CONTRACT,
            "workerId": "pc_test",
            "bookId": claimed["job"]["bookId"],
            "contentSha256": claimed["job"]["contentSha256"],
            "jobId": claimed["job"]["jobId"],
            "generation": claimed["job"]["generation"],
            "leaseId": claimed["lease"]["leaseId"],
        }
        page = _with_test_layout({
            "schema": "reader-page-chars/1",
            "bookId": self.entry["bookId"],
            "contentSha256": self.entry["contentSha256"],
            "engine": engine,
            "pageNumber": 1,
            "page_w": 10,
            "page_h": 20,
            "chars": [],
            "furigana": [],
        })
        service.upload_pc_page(1, {**identity, "page": page})
        service.upload_pc_formulas({
            **identity,
            "formula": {
                "schema": "reader-formula-regions/1",
                "bookId": self.entry["bookId"],
                "contentSha256": self.entry["contentSha256"],
                "formulas": [],
            },
            "formulaState": "succeeded",
        })
        return identity, service._job_dir(
            self.entry["bookId"], self.entry["contentSha256"], engine
        )

    def test_pc_finalizer_owner_is_persistent_and_control_is_not_swallowed(self) -> None:
        service = ReaderBookOcrService(
            self.library,
            self.base / "pc-finalizer-ocr",
            ROOT,
            launcher=lambda *_args: self.fail("PC finalizer must not spawn Pi"),
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 1,
        )
        version_dir = service._version_dir(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        job_dir = version_dir / "vision"
        job_dir.mkdir(parents=True)
        (version_dir / "current.json").write_text(
            json.dumps({
                "engine": "vision",
                "executor": "pc",
                "processingProfile": "quality-first-v6",
            }),
            "utf-8",
        )
        (job_dir / "control.json").write_text(
            json.dumps({"desiredState": "running"}), "utf-8"
        )
        finalizing = {
            "contract": "reader-library-ocr/1",
            "jobId": "ocrjob_" + "1" * 32,
            "runId": "ocrrun_00000000000000f1",
            "workerGeneration": "ocrgen_" + "2" * 32,
            "bookId": self.entry["bookId"],
            "contentSha256": self.entry["contentSha256"],
            "engine": "vision",
            "executor": "pc",
            "processingProfile": "quality-first-v6",
            "state": "running",
            "phase": "finalizing",
            "leaseId": "ocrlease_" + "3" * 32,
            "leaseWorkerId": "pc_test",
            "leaseExpiresAtEpochMs": 1,
            "finalizerOwnerToken": "ocrfinal_" + "4" * 32,
            "finalizerPid": self.fake_worker_pid,
            "finalizerProcessStartToken": None,
            "canPause": True,
            "canCancel": True,
            "resultAvailable": False,
        }
        job_path = job_dir / "job.json"
        job_path.write_text(json.dumps(finalizing), "utf-8")

        # Lease expiry alone cannot reassign work while the persisted server
        # finalizer process is still alive.
        with exclusive_lock(service.lock_path):
            _engine, owned = service._current_job_locked(version_dir)
        self.assertEqual(owned["state"], "running")
        self.assertEqual(owned["finalizerOwnerToken"], finalizing["finalizerOwnerToken"])

        identity = {
            "contract": reader_book_ocr.WORKER_CONTRACT,
            "workerId": finalizing["leaseWorkerId"],
            "bookId": finalizing["bookId"],
            "contentSha256": finalizing["contentSha256"],
            "jobId": finalizing["jobId"],
            "generation": finalizing["workerGeneration"],
            "leaseId": finalizing["leaseId"],
        }
        pages_dir = job_dir / "pages"
        pages_dir.mkdir()
        page_path = pages_dir / "p000001.json"
        page_path.write_bytes(b"existing-page")
        formula_path = job_dir / "pc-formulas.json"
        formula_path.write_bytes(b"existing-formulas")
        for operation in (
            lambda: service.upload_pc_page(1, {**identity, "page": {}}),
            lambda: service.upload_pc_formulas({
                **identity,
                "formula": {},
                "formulaState": "succeeded",
            }),
        ):
            with self.assertRaises(ReaderBookOcrError) as frozen:
                operation()
            self.assertEqual(frozen.exception.code, "ocr-publication-finalizing")
        self.assertEqual(page_path.read_bytes(), b"existing-page")
        self.assertEqual(formula_path.read_bytes(), b"existing-formulas")
        lease = service.pc_worker_heartbeat(identity)
        self.assertEqual(lease["desiredState"], "running")
        self.assertEqual(
            json.loads(job_path.read_text("utf-8"))["finalizerOwnerToken"],
            finalizing["finalizerOwnerToken"],
        )

        paused = service.pause(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertEqual(paused["state"], "pause-requested")
        self.assertEqual(
            json.loads((job_dir / "control.json").read_text("utf-8"))["desiredState"],
            "paused",
        )

        # Once the owning process is gone, a different server process can
        # recover the persisted finalizing generation instead of waiting for a
        # guessed long lease.
        stopped = json.loads(job_path.read_text("utf-8"))
        stopped["finalizerPid"] = 999999999
        job_path.write_text(json.dumps(stopped), "utf-8")
        with exclusive_lock(service.lock_path):
            _engine, recovered = service._current_job_locked(version_dir)
        self.assertEqual(recovered["state"], "paused")
        self.assertNotIn("finalizerOwnerToken", recovered)

    def test_current_profile_requires_layout_but_previous_profile_remains_readable(self) -> None:
        page_path = self.base / "page.json"
        page = {
            "schema": "reader-page-chars/1",
            "bookId": self.entry["bookId"],
            "contentSha256": self.entry["contentSha256"],
            "engine": "vision",
            "executor": "pc",
            "processingProfile": "quality-first-v5",
            "pageNumber": 1,
            "page_w": 10,
            "page_h": 20,
            "chars": [{"c": "A", "x0": 1, "y0": 1, "x1": 2, "y1": 2}],
            "furigana": [],
        }
        page_path.write_text(json.dumps(page), "utf-8")

        def snapshot(profile: str) -> dict:
            return {
                "engine": "vision",
                "job": {"executor": "pc", "processingProfile": profile},
                "attachmentPaths": {"ocr-page-000001": page_path},
            }

        with patch.object(
            self.service,
            "_published_snapshot",
            return_value=snapshot("quality-first-v5"),
        ):
            readable, _path = self.service.read_page(
                self.entry["bookId"], self.entry["contentSha256"], 1
            )
        self.assertNotIn("layout", readable)

        with patch.object(
            self.service,
            "_published_snapshot",
            return_value=snapshot("quality-first-v6"),
        ):
            with self.assertRaises(ReaderBookOcrError) as missing:
                self.service.read_page(
                    self.entry["bookId"], self.entry["contentSha256"], 1
                )
        self.assertEqual(missing.exception.code, "ocr-sidecar-invalid")

    def test_start_is_content_addressed_path_free_and_idempotent(self) -> None:
        job, already = self.service.start(
            self.entry["bookId"], self.entry["contentSha256"], "vision"
        )
        self.assertFalse(already)
        self.assertEqual(job["state"], "queued")
        self.assertEqual(job["pauseMode"], "checkpoint-restart")
        self.assertNotIn(str(self.vault), json.dumps(job))
        stored = json.loads((self.launches[0][0] / "job.json").read_text("utf-8"))
        self.assertNotIn("sourcePath", stored)
        self.assertNotIn(str(self.vault), json.dumps(stored))
        self.assertEqual(stored["workerPid"], self.fake_worker_pid)
        self.assertEqual(stored["pid"], self.fake_worker_pid)
        self.assertEqual(
            stored["workerGeneration"], self.launches[0][2]["workerGeneration"]
        )

        # A model may spend a long time importing before its first progress
        # update.  The synchronous spawn handshake already owns the generation,
        # so age alone must never turn this queued job into a retryable failure.
        stored["updatedAtEpochMs"] = 1
        (self.launches[0][0] / "job.json").write_text(json.dumps(stored), "utf-8")
        slow_start = self.service.status(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertEqual(slow_start["state"], "queued")

        repeated, already = self.service.start(
            self.entry["bookId"], self.entry["contentSha256"], "vision"
        )
        self.assertTrue(already)
        self.assertEqual(repeated["jobId"], job["jobId"])
        self.assertEqual(len(self.launches), 1)

    def test_pc_executor_claim_upload_and_common_publication(self) -> None:
        service = ReaderBookOcrService(
            self.library,
            self.base / "pc-ocr",
            ROOT,
            launcher=lambda *_args: self.fail("PC executor must not spawn a Pi worker"),
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 1,
            pc_lease_seconds=60,
            pc_online_seconds=60,
        )
        old_revision = self._publish_pc_release(
            service,
            engine="vision",
            profile="quality-first-v5",
            text="V",
        )
        version_dir = service._version_dir(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        index_path = version_dir / "releases-index.json"
        old_index = json.loads(index_path.read_text("utf-8"))
        self.assertEqual(
            next(
                run["revision"]
                for run in old_index["runs"]
                if run["runId"] == old_index["activeRunId"]
            ),
            old_revision,
        )
        job, already = service.start(
            self.entry["bookId"], self.entry["contentSha256"], "manga", "pc"
        )
        self.assertFalse(already)
        self.assertEqual(job["executor"], "pc")
        self.assertEqual(job["totalPages"], 1)

        claimed = service.claim_pc_worker("pc_test", {
            "engines": ["manga"],
            "maxPdfBytes": 1024 * 1024,
            "maxPageBytes": 1024 * 1024,
            "processingProfile": "quality-first-v6",
        })
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["job"]["completedPages"], [])
        pc_status = next(
            item for item in service.executor_status() if item["executor"] == "pc"
        )
        self.assertTrue(pc_status["online"])
        identity = {
            "contract": reader_book_ocr.WORKER_CONTRACT,
            "workerId": "pc_test",
            "bookId": claimed["job"]["bookId"],
            "contentSha256": claimed["job"]["contentSha256"],
            "jobId": claimed["job"]["jobId"],
            "generation": claimed["job"]["generation"],
            "leaseId": claimed["lease"]["leaseId"],
        }
        _entry, source, lease = service.pc_worker_source(identity)
        self.assertEqual(source, self.vault / "A.pdf")
        self.assertEqual(lease["desiredState"], "running")

        page = {
            "schema": "reader-page-chars/1",
            "bookId": self.entry["bookId"],
            "contentSha256": self.entry["contentSha256"],
            "engine": "manga",
            "pageNumber": 1,
            "page_w": 10,
            "page_h": 20,
            "imageWidth": 100,
            "imageHeight": 200,
            "chars": [{
                "c": "A", "x0": 1, "y0": 1, "x1": 2, "y1": 2,
                "w": 1, "bk": 0, "b": 0, "line": 0, "vertical": True,
            }],
            "furigana": [],
            "textCharCount": 1,
            "tokenized": True,
        }
        page = _with_test_layout(page)
        uploaded = service.upload_pc_page(1, {**identity, "page": page})
        self.assertTrue(uploaded["accepted"])
        self.assertEqual(uploaded["job"]["successfulPages"], 1)

        formula = {
            "schema": "reader-formula-regions/1",
            "bookId": self.entry["bookId"],
            "contentSha256": self.entry["contentSha256"],
            "formulas": [],
        }
        with self.assertRaises(ReaderBookOcrError) as pending_formula:
            service.upload_pc_formulas({
                **identity,
                "formula": formula,
                "formulaState": "pending",
                "formulaReason": "formula-model-unavailable",
            })
        self.assertEqual(
            pending_formula.exception.code, "worker-formula-not-publishable"
        )
        inconsistent_formula = {
            **formula,
            "formulas": [{
                "page": 1,
                "bbox": [0.1, 0.1, 0.2, 0.2],
                "latex": "x",
            }],
        }
        with self.assertRaises(ReaderBookOcrError) as inconsistent:
            service.upload_pc_formulas({
                **identity,
                "formula": inconsistent_formula,
                "formulaState": "unavailable",
                "formulaReason": "formula-model-unavailable",
            })
        self.assertEqual(
            inconsistent.exception.code, "worker-formula-inconsistent"
        )
        formula_result = service.upload_pc_formulas({
            **identity,
            "formula": formula,
            "formulaState": "unavailable",
            "formulaReason": "formula-model-unavailable",
        })
        self.assertEqual(formula_result["job"]["formulaState"], "unavailable")
        self.assertEqual(
            formula_result["job"]["formulaReason"], "formula-model-unavailable"
        )
        self.assertEqual(formula_result["job"]["formulaProgress"]["completed"], 0)
        self.assertEqual(formula_result["job"]["formulaProgress"]["unavailable"], 1)

        job_path = service._job_dir(
            self.entry["bookId"], self.entry["contentSha256"], "manga"
        ) / "job.json"
        publishable_job = json.loads(job_path.read_text("utf-8"))
        tampered_job = {**publishable_job, "formulaState": "failed"}
        job_path.write_text(json.dumps(tampered_job), "utf-8")
        with self.assertRaises(ReaderBookOcrError) as failed_completion:
            service.complete_pc_worker({**identity, "totalPages": 1})
        self.assertEqual(
            failed_completion.exception.code, "worker-formula-not-publishable"
        )
        job_path.write_text(json.dumps(publishable_job), "utf-8")

        real_lock = reader_book_ocr.exclusive_lock
        real_page_done = service._page_for_pc_done
        real_read_optional = service._read_optional
        formula_path = job_path.parent / "pc-formulas.json"
        lock_depth = 0

        @contextmanager
        def tracked_jobs_lock(path):
            nonlocal lock_depth
            with real_lock(path):
                lock_depth += 1
                try:
                    yield
                finally:
                    lock_depth -= 1

        def checked_page_preflight(*args, **kwargs):
            self.assertEqual(
                lock_depth, 0, "page JSON preflight must run outside jobs.lock"
            )
            return real_page_done(*args, **kwargs)

        def checked_optional_read(path):
            if Path(path) == formula_path:
                self.assertEqual(
                    lock_depth, 0, "formula JSON preflight must run outside jobs.lock"
                )
            return real_read_optional(path)

        with patch.object(
            reader_book_ocr, "exclusive_lock", side_effect=tracked_jobs_lock
        ), patch.object(
            service, "_page_for_pc_done", side_effect=checked_page_preflight
        ), patch.object(
            service, "_read_optional", side_effect=checked_optional_read
        ), patch.object(
            service,
            "_published_snapshot",
            side_effect=AssertionError(
                "PC completion tail must not rescan published attachments"
            ),
        ):
            completed = service.complete_pc_worker({
                **identity,
                "totalPages": 1,
            })
        self.assertTrue(completed["published"])
        self.assertRegex(completed["revision"], r"^ocr_[0-9a-f]{20}$")
        committed_index = json.loads(index_path.read_text("utf-8"))
        self.assertEqual(
            {run["revision"] for run in committed_index["runs"]},
            {old_revision, completed["revision"]},
        )
        active_run = next(
            run
            for run in committed_index["runs"]
            if run["runId"] == committed_index["activeRunId"]
        )
        self.assertEqual(active_run["revision"], completed["revision"])
        self.assertEqual(active_run["processingProfile"], "quality-first-v6")
        self.assertEqual(committed_index["generation"], old_index["generation"] + 1)
        self.assertEqual(
            json.loads((version_dir / "publication.json").read_text("utf-8"))["revision"],
            completed["revision"],
        )
        self.assertEqual(
            json.loads((version_dir / "current.json").read_text("utf-8"))["revision"],
            completed["revision"],
        )
        status = service.status(self.entry["bookId"], self.entry["contentSha256"])
        self.assertEqual(status["state"], "succeeded")
        self.assertEqual(status["executor"], "pc")
        self.assertEqual(status["formulaState"], "unavailable")
        self.assertEqual(status["formulaProgress"]["completed"], 0)
        self.assertEqual(status["formulaProgress"]["unavailable"], 1)
        self.assertIn("公式不可用", status["message"])
        manifest = service.attachment_manifest(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertEqual(manifest["revision"], completed["revision"])
        self.assertEqual(manifest["formulaReason"], "formula-model-unavailable")
        self.assertEqual(manifest["executor"], "pc")
        self.assertEqual(manifest["processingProfile"], "quality-first-v6")
        snapshot = service._published_snapshot(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertEqual(snapshot["result"]["executor"], "pc")
        self.assertEqual(
            snapshot["result"]["processingProfile"], "quality-first-v6"
        )
        listing = service.list_releases(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertEqual(
            next(run["revision"] for run in listing["runs"] if run["isActive"]),
            completed["revision"],
        )

    def test_pc_post_commit_terminal_failure_uses_index_outside_lock(self) -> None:
        service = ReaderBookOcrService(
            self.library,
            self.base / "pc-post-commit-ocr",
            ROOT,
            launcher=lambda *_args: self.fail("PC executor must not spawn a Pi worker"),
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 1,
        )
        identity, job_dir = self._prepare_pc_completion(service)
        job_path = job_dir / "job.json"
        real_atomic = reader_book_ocr_worker._atomic_json
        real_lock = reader_book_ocr.exclusive_lock
        real_index_lookup = service._indexed_run_for_job
        lock_depth = 0

        def fail_terminal(path, value):
            if Path(path) == job_path and value.get("state") == "succeeded":
                raise OSError("fault after release-index commit")
            return real_atomic(path, value)

        @contextmanager
        def tracked_jobs_lock(path):
            nonlocal lock_depth
            with real_lock(path):
                lock_depth += 1
                try:
                    yield
                finally:
                    lock_depth -= 1

        def checked_index_lookup(*args, **kwargs):
            self.assertEqual(
                lock_depth,
                0,
                "post-publish index validation must run outside jobs.lock",
            )
            return real_index_lookup(*args, **kwargs)

        with patch.object(
            reader_book_ocr_worker, "_atomic_json", side_effect=fail_terminal
        ), patch.object(
            reader_book_ocr, "exclusive_lock", side_effect=tracked_jobs_lock
        ), patch.object(
            service, "_indexed_run_for_job", side_effect=checked_index_lookup
        ), patch.object(
            service,
            "_published_snapshot",
            side_effect=AssertionError(
                "post-commit recovery must not rescan published attachments"
            ),
        ):
            completed = service.complete_pc_worker({
                **identity,
                "totalPages": 1,
            })

        self.assertTrue(completed["published"])
        stored = json.loads(job_path.read_text("utf-8"))
        self.assertEqual(stored["state"], "succeeded")
        self.assertEqual(stored["jobId"], identity["jobId"])
        self.assertEqual(stored["pageCharsRevision"], completed["revision"])
        self.assertNotIn("finalizerOwnerToken", stored)

    def test_pc_success_tail_preserves_post_commit_replacement(self) -> None:
        service = ReaderBookOcrService(
            self.library,
            self.base / "pc-replacement-ocr",
            ROOT,
            launcher=lambda *_args: self.fail("PC executor must not spawn a Pi worker"),
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 1,
        )
        identity, job_dir = self._prepare_pc_completion(service)
        real_publish = reader_book_ocr_worker._publish_release
        replacement: dict[str, object] = {}

        def publish_then_replace(*args, **kwargs):
            revision = real_publish(*args, **kwargs)
            replacement_job, already = service.start(
                self.entry["bookId"],
                self.entry["contentSha256"],
                "vision",
                "pc",
                force=True,
            )
            self.assertFalse(already)
            replacement.update({
                "job": replacement_job,
                "bytes": (job_dir / "job.json").read_bytes(),
            })
            return revision

        with patch.object(
            reader_book_ocr_worker,
            "_publish_release",
            side_effect=publish_then_replace,
        ), patch.object(
            service,
            "_published_snapshot",
            wraps=service._published_snapshot,
        ) as snapshot:
            completed = service.complete_pc_worker({
                **identity,
                "totalPages": 1,
            })

        self.assertTrue(completed["published"])
        self.assertEqual(completed["job"]["jobId"], identity["jobId"])
        self.assertEqual((job_dir / "job.json").read_bytes(), replacement["bytes"])
        self.assertEqual(
            json.loads((job_dir / "job.json").read_text("utf-8"))["jobId"],
            replacement["job"]["jobId"],
        )
        # start(force=True) performs its own published-result lookup.  The
        # completion tail must not add a second scan or activation afterward.
        self.assertEqual(snapshot.call_count, 1)

    def test_pc_expired_lease_is_reclaimable_and_old_upload_is_rejected(self) -> None:
        service = ReaderBookOcrService(
            self.library,
            self.base / "pc-lease-ocr",
            ROOT,
            launcher=lambda *_args: self.fail("PC executor must not spawn a Pi worker"),
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 1,
            pc_lease_seconds=60,
        )
        service.start(
            self.entry["bookId"], self.entry["contentSha256"], "vision", "pc"
        )
        capabilities = {
            "engines": ["vision"],
            "maxPdfBytes": 1024 * 1024,
            "maxPageBytes": 1024 * 1024,
            "processingProfile": "quality-first-v6",
        }
        first = service.claim_pc_worker("pc_first", capabilities)
        version_dir = service._version_dir(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        job_path = version_dir / "vision" / "job.json"
        stored = json.loads(job_path.read_text("utf-8"))
        stored["leaseExpiresAtEpochMs"] = 1
        job_path.write_text(json.dumps(stored), "utf-8")

        second = service.claim_pc_worker("pc_second", capabilities)
        self.assertIsNotNone(second)
        self.assertNotEqual(first["lease"]["leaseId"], second["lease"]["leaseId"])
        old_identity = {
            "contract": reader_book_ocr.WORKER_CONTRACT,
            "workerId": "pc_first",
            "bookId": first["job"]["bookId"],
            "contentSha256": first["job"]["contentSha256"],
            "jobId": first["job"]["jobId"],
            "generation": first["job"]["generation"],
            "leaseId": first["lease"]["leaseId"],
        }
        with self.assertRaises(ReaderBookOcrError) as stale:
            service.pc_worker_heartbeat(old_identity)
        self.assertEqual(stale.exception.code, "ocr-worker-lease-stale")
        second_identity = {
            "contract": reader_book_ocr.WORKER_CONTRACT,
            "workerId": "pc_second",
            "bookId": second["job"]["bookId"],
            "contentSha256": second["job"]["contentSha256"],
            "jobId": second["job"]["jobId"],
            "generation": second["job"]["generation"],
            "leaseId": second["lease"]["leaseId"],
        }
        requested = service.pause(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertEqual(requested["state"], "pause-requested")
        stopping = service.pc_worker_heartbeat({
            **second_identity, "state": "running", "currentPage": None,
        })
        self.assertEqual(stopping["desiredState"], "paused")
        stopped = service.pc_worker_heartbeat({
            **second_identity, "state": "paused", "currentPage": None,
        })
        self.assertEqual(stopped["job"]["state"], "paused")

    def test_source_fingerprint_ignores_windows_ctime_rounding_only(self) -> None:
        common = {
            "st_dev": 7,
            "st_ino": 11,
            "st_size": len(PDF_A),
            "st_mtime_ns": 123_456_789,
        }
        fd_stat = SimpleNamespace(**common, st_ctime_ns=800_000_000)
        path_stat = SimpleNamespace(**common, st_ctime_ns=800_001_000)
        self.assertEqual(
            self.service._source_fingerprint(fd_stat),
            self.service._source_fingerprint(path_stat),
        )

    def test_worker_errors_redact_sensitive_url_query_values(self) -> None:
        raw = (
            "fetch failed https://pc.invalid/run?token=token-secret"
            "&api_key=api-secret&key=key-secret&safe=visible"
        )
        public = reader_book_ocr._safe_public_job({"error": raw})["error"]
        stored = self.service._sanitize_worker_error(raw)
        for value in (public, stored):
            self.assertNotIn("token-secret", value)
            self.assertNotIn("api-secret", value)
            self.assertNotIn("key-secret", value)
            self.assertIn("safe=visible", value)
            self.assertGreaterEqual(value.count("<redacted>"), 3)

    def test_switching_pi_publication_to_pc_archives_only_mutable_staging(self) -> None:
        launches = []
        service = ReaderBookOcrService(
            self.library,
            self.base / "identity-ocr",
            ROOT,
            launcher=lambda job_dir, source_path, job: (
                launches.append((job_dir, source_path, dict(job)))
                or _FakeProcess(self.fake_worker_pid)
            ),
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 1,
            pc_lease_seconds=60,
        )
        pi_job, _already = service.start(
            self.entry["bookId"], self.entry["contentSha256"], "vision", "pi"
        )
        self.assertEqual(pi_job["processingProfile"], "pi-default-v5")
        job_dir = launches[0][0]
        page = {
            "schema": "reader-page-chars/1",
            "bookId": self.entry["bookId"],
            "contentSha256": self.entry["contentSha256"],
            "engine": "vision",
            "executor": "pi",
            "processingProfile": "pi-default-v5",
            "pageNumber": 1,
            "page_w": 10,
            "page_h": 20,
            "chars": [{
                "c": "P", "x0": 1, "y0": 1, "x1": 2, "y1": 2,
                "w": 1, "bk": 0, "b": 0,
            }],
            "furigana": [],
        }
        page = _with_test_layout(page)
        (job_dir / "pages").mkdir(parents=True, exist_ok=True)
        (job_dir / "pages" / "p000001.json").write_text(
            json.dumps(page), "utf-8"
        )
        formula_path = job_dir / "formula-source.json"
        formula_path.write_text(json.dumps({"formulas": []}), "utf-8")
        stored = json.loads((job_dir / "job.json").read_text("utf-8"))
        final_job = {
            **stored,
            "state": "succeeded",
            "totalPages": 1,
            "processedPages": 1,
            "successfulPages": 1,
            "recognizedPages": 1,
            "formulaState": "succeeded",
            "formulaReason": None,
            "formulaTotal": 0,
            "formulaRecognized": 0,
            "resultAvailable": True,
            "updatedAtEpochMs": 1,
        }
        (job_dir / "job.json").write_text(json.dumps(final_job), "utf-8")
        args = SimpleNamespace(
            book_id=self.entry["bookId"],
            content_sha256=self.entry["contentSha256"],
            engine="vision",
            max_bytes=1024 * 1024,
        )
        reader_book_ocr_worker._set_worker_identity(None, None)
        revision = _publish_release(
            args,
            job_dir,
            formula_path,
            final_job,
            source_path=self.vault / "A.pdf",
        )
        final_job["pageCharsRevision"] = revision
        (job_dir / "job.json").write_text(json.dumps(final_job), "utf-8")
        version_dir = service._version_dir(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        release_dir = version_dir / "releases" / revision
        self.assertTrue(release_dir.is_dir())
        manifest = json.loads((release_dir / "attachments.json").read_text("utf-8"))
        self.assertEqual(manifest["executor"], "pi")
        self.assertEqual(manifest["processingProfile"], "pi-default-v5")

        pc_job, already = service.start(
            self.entry["bookId"], self.entry["contentSha256"], "vision", "pc"
        )
        self.assertFalse(already)
        self.assertEqual(pc_job["executor"], "pc")
        self.assertEqual(pc_job["processingProfile"], "quality-first-v6")
        self.assertEqual(pc_job["successfulPages"], 0)
        self.assertTrue(release_dir.is_dir(), "immutable Pi release must remain")
        self.assertFalse((job_dir / "pages" / "p000001.json").exists())
        archives = list((version_dir / "staging-archive").glob("*"))
        self.assertEqual(len(archives), 1)
        self.assertTrue(archives[0].is_dir())
        claimed = service.claim_pc_worker("pc_identity", {
            "engines": ["vision"],
            "maxPdfBytes": 1024 * 1024,
            "maxPageBytes": 1024 * 1024,
            "processingProfile": "quality-first-v6",
        })
        self.assertEqual(claimed["job"]["completedPages"], [])
        self.assertEqual(
            claimed["job"]["processingProfile"], "quality-first-v6"
        )

    def test_historical_pc_v1_publication_remains_readable_and_can_restart_as_v5(self) -> None:
        service = ReaderBookOcrService(
            self.library,
            self.base / "pc-profile-upgrade-ocr",
            ROOT,
            launcher=lambda *_args: self.fail("PC executor must not spawn a Pi worker"),
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 1,
        )
        _job, _already = service.start(
            self.entry["bookId"], self.entry["contentSha256"], "manga", "pc"
        )
        version_dir = service._version_dir(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        job_dir = version_dir / "manga"
        stored = json.loads((job_dir / "job.json").read_text("utf-8"))
        historical_profile = "quality-first-v1"
        stored["processingProfile"] = historical_profile
        page = {
            "schema": "reader-page-chars/1",
            "bookId": self.entry["bookId"],
            "contentSha256": self.entry["contentSha256"],
            "engine": "manga",
            "executor": "pc",
            "processingProfile": historical_profile,
            "pageNumber": 1,
            "page_w": 10,
            "page_h": 20,
            "imageWidth": 100,
            "imageHeight": 200,
            "chars": [{
                "c": "旧", "x0": 1, "y0": 1, "x1": 2, "y1": 2,
                "w": 1, "bk": 0, "b": 0, "line": 0,
            }],
            "furigana": [],
        }
        (job_dir / "pages").mkdir(parents=True, exist_ok=True)
        (job_dir / "pages" / "p000001.json").write_text(
            json.dumps(page), "utf-8"
        )
        formula_path = job_dir / "pc-formulas.json"
        formula_path.write_text(json.dumps({"formulas": []}), "utf-8")
        final_job = {
            **stored,
            "state": "succeeded",
            "totalPages": 1,
            "processedPages": 1,
            "successfulPages": 1,
            "recognizedPages": 1,
            "formulaState": "succeeded",
            "formulaReason": None,
            "formulaTotal": 0,
            "formulaRecognized": 0,
            "resultAvailable": True,
            "updatedAtEpochMs": 1,
        }
        (job_dir / "job.json").write_text(json.dumps(final_job), "utf-8")
        args = SimpleNamespace(
            book_id=self.entry["bookId"],
            content_sha256=self.entry["contentSha256"],
            engine="manga",
            max_bytes=1024 * 1024,
        )
        reader_book_ocr_worker._set_worker_identity(None, None)
        revision = _publish_release(
            args,
            job_dir,
            formula_path,
            final_job,
            source_path=self.vault / "A.pdf",
        )

        manifest = service.attachment_manifest(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertEqual(manifest["processingProfile"], historical_profile)
        self.assertTrue((version_dir / "releases" / revision).is_dir())

        restarted, already = service.start(
            self.entry["bookId"], self.entry["contentSha256"], "manga", "pc"
        )
        self.assertFalse(already)
        self.assertEqual(restarted["executor"], "pc")
        self.assertEqual(restarted["processingProfile"], "quality-first-v6")
        self.assertEqual(restarted["state"], "queued")
        self.assertTrue((version_dir / "releases" / revision).is_dir())
        self.assertEqual(len(list((version_dir / "staging-archive").glob("*"))), 1)

    def test_version_kind_and_unknown_fields_fail_closed(self) -> None:
        self.assertEqual(
            reader_book_ocr._safe_public_job({})["processingProfile"],
            "pi-default-v5",
        )
        self.assertEqual(
            reader_book_ocr._safe_public_job({
                "executor": "pi",
                "state": "succeeded",
            })["processingProfile"],
            "pi-default-v1",
        )
        self.assertEqual(
            self.service._processing_identity({"executor": "pi"}),
            ("pi", "pi-default-v1"),
        )
        self.assertEqual(
            self.service._processing_identity({"executor": "pc"}),
            ("pc", "quality-first-v1"),
        )
        self.assertNotEqual(
            self.service._processing_identity({"executor": "pi"}),
            ("pi", reader_book_ocr.PROCESSING_PROFILES["pi"]),
        )
        with self.assertRaises(ReaderBookOcrError) as changed:
            self.service.start(self.entry["bookId"], "0" * 64, "vision")
        self.assertEqual(changed.exception.code, "book-version-changed")
        with self.assertRaises(ReaderBookOcrError) as engine:
            self.service.start(self.entry["bookId"], self.entry["contentSha256"], "other")
        self.assertEqual(engine.exception.code, "invalid-engine")
        with self.assertRaises(ReaderBookOcrError) as profile:
            self.service.start(
                self.entry["bookId"],
                self.entry["contentSha256"],
                "vision",
                "pc",
                "pi-default-v2",
            )
        self.assertEqual(profile.exception.code, "invalid-processing-profile")

        self.assertEqual(
            reader_book_ocr.READABLE_PROCESSING_PROFILES["pi"],
            frozenset((
                "pi-default-v1", "pi-default-v2", "pi-default-v3", "pi-default-v4",
                "pi-default-v5",
            )),
        )
        self.assertEqual(
            reader_book_ocr.READABLE_PROCESSING_PROFILES["pc"],
            frozenset((
                "quality-first-v1", "quality-first-v2", "quality-first-v3",
                "quality-first-v4", "quality-first-v5", "quality-first-v6",
            )),
        )

        epub = self.vault / "B.epub"
        epub.write_bytes(b"not-needed-for-catalog")
        epub_entry = next(item for item in self.library.catalog() if item["kind"] == "epub")
        with self.assertRaises(ReaderBookOcrError) as unsupported:
            self.service.start(epub_entry["bookId"], epub_entry["contentSha256"], "vision")
        self.assertEqual(unsupported.exception.code, "unsupported-book-kind")

    def test_pause_resume_cancel_and_retry_are_checkpoint_actions(self) -> None:
        self.service.start(self.entry["bookId"], self.entry["contentSha256"], "vision")
        paused = self.service.pause(self.entry["bookId"], self.entry["contentSha256"])
        self.assertEqual(paused["state"], "pause-requested")
        self.assertIn("当前页可能", paused["message"])
        control = self.launches[0][0] / "control.json"
        self.assertEqual(json.loads(control.read_text("utf-8"))["desiredState"], "paused")

        # Simulate the worker acknowledging the page-boundary checkpoint.
        job_path = self.launches[0][0] / "job.json"
        stored = json.loads(job_path.read_text("utf-8"))
        stored["state"] = "paused"
        job_path.write_text(json.dumps(stored), "utf-8")
        resumed = self.service.resume(self.entry["bookId"], self.entry["contentSha256"])
        self.assertEqual(resumed["state"], "queued")
        self.assertEqual(len(self.launches), 2)

        cancelled = self.service.cancel(self.entry["bookId"], self.entry["contentSha256"])
        self.assertEqual(cancelled["state"], "cancel-requested")
        stored = json.loads(job_path.read_text("utf-8"))
        stored["state"] = "cancelled"
        job_path.write_text(json.dumps(stored), "utf-8")
        retried = self.service.retry(self.entry["bookId"], self.entry["contentSha256"])
        self.assertEqual(retried["state"], "queued")
        self.assertEqual(len(self.launches), 3)

    @unittest.skipUnless(os.name == "posix", "process-group cleanup is POSIX-only")
    def test_dead_worker_cleanup_waits_for_the_entire_generation_group(self) -> None:
        job = {
            "workerPid": 424242,
            "processGroupId": 424242,
            "processStartToken": "start-a",
            "workerGeneration": "ocrgen_a",
        }
        with (
            patch.object(reader_book_ocr, "_process_start_token", return_value=None),
            patch.object(
                self.service,
                "_process_group_alive",
                side_effect=[True, True, False],
            ),
            patch.object(reader_book_ocr.os, "killpg") as killpg,
            patch.object(reader_book_ocr.time, "sleep", return_value=None),
        ):
            self.assertTrue(self.service._terminate_worker_generation(job))
        killpg.assert_called_once_with(424242, reader_book_ocr.signal.SIGTERM)

    def test_legacy_adoption_is_previewable_copy_only_and_idempotent(self) -> None:
        project = self.base / "legacy-project"
        rel_key = hashlib.sha1(self.entry["rel"].encode("utf-8")).hexdigest()[:16]

        def layer(text: str) -> dict:
            return {
                "chars": [{
                    "c": text, "x0": 1, "y0": 2, "x1": 3, "y1": 4,
                    "sp": 0, "w": 7, "b": 0, "bk": 1,
                }],
                "page_w": 100,
                "page_h": 200,
                "furigana": [],
            }

        override = project / "state" / "pdf-page-ocr" / f"{rel_key}-p1.json"
        override.parent.mkdir(parents=True)
        override.write_text(json.dumps({
            **layer("override"),
            "contentSha256": self.entry["contentSha256"],
        }), "utf-8")
        mtime = int((self.vault / "A.pdf").stat().st_mtime)
        cache = (
            project / "state" / "pdf-char-cache"
            / f"{rel_key}-p2-{mtime}-ja.json"
        )
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps({
            **layer("cache"),
            "cver": 11,
            "sourceContentSha256": self.entry["contentSha256"],
        }), "utf-8")
        lower_priority_cache = (
            project / "state" / "pdf-char-cache"
            / f"{rel_key}-p1-{mtime}-ja.json"
        )
        lower_priority_cache.write_text(
            json.dumps({
                **layer("must-not-win"),
                "cver": 11,
                "sourceContentSha256": self.entry["contentSha256"],
            }), "utf-8"
        )
        formula_key = hashlib.sha1(
            str((self.vault / "A.pdf").resolve()).encode("utf-8")
        ).hexdigest()[:16]
        formula = project / "state" / "pdf-figures" / f"{formula_key}.json"
        formula.parent.mkdir(parents=True)
        formula.write_text('{"formulas": []}\n216\n}', "utf-8")
        old_bytes = {
            override: override.read_bytes(),
            cache: cache.read_bytes(),
            lower_priority_cache: lower_priority_cache.read_bytes(),
            formula: formula.read_bytes(),
            self.vault / "A.pdf": (self.vault / "A.pdf").read_bytes(),
        }
        state_root = self.base / "adopted-ocr"
        service = ReaderBookOcrService(
            self.library,
            state_root,
            project,
            launcher=lambda *_args: self.fail("adoption must not launch OCR"),
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 3,
            legacy_embedded_page_reader=(
                lambda _path, _rel, page: layer("embedded") if page == 3 else None
            ),
            legacy_language_resolver=lambda _rel: "ja",
            legacy_char_cache_version=11,
        )

        preview = service.preview_adoption(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertFalse(state_root.exists())
        self.assertTrue(preview["available"])
        self.assertEqual(
            preview["pageSources"],
            {"override": 1, "char-cache": 1, "embedded": 1, "missing": 0},
        )
        self.assertEqual(preview["formula"]["state"], "pending")
        self.assertEqual(
            preview["formula"]["reason"], "legacy-formulas-invalid-json"
        )
        self.assertNotIn(str(self.vault), json.dumps(preview))

        job, adoption, already = service.adopt_legacy(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertFalse(already)
        self.assertEqual(job["state"], "succeeded")
        self.assertEqual(job["engine"], "legacy")
        self.assertEqual(job["formulaState"], "pending")
        self.assertTrue(adoption["revision"].startswith("ocr_"))
        self.assertEqual(service.read_page(
            self.entry["bookId"], self.entry["contentSha256"], 1
        )[0]["legacySource"], "override")
        self.assertEqual(service.read_page(
            self.entry["bookId"], self.entry["contentSha256"], 2
        )[0]["legacySource"], "char-cache")
        self.assertEqual(service.read_page(
            self.entry["bookId"], self.entry["contentSha256"], 3
        )[0]["legacySource"], "embedded")
        self.assertEqual(service.read_formulas(
            self.entry["bookId"], self.entry["contentSha256"]
        ), [])
        manifest = service.attachment_manifest(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertEqual(manifest["formulaState"], "pending")
        self.assertEqual(manifest["formulaReason"], "legacy-formulas-invalid-json")
        for path, original in old_bytes.items():
            self.assertEqual(path.read_bytes(), original)

        repeated_job, repeated, already = service.adopt_legacy(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertTrue(already)
        self.assertEqual(repeated_job["state"], "succeeded")
        self.assertEqual(repeated["revision"], adoption["revision"])

    def test_legacy_adoption_fails_closed_when_a_page_is_missing(self) -> None:
        service = ReaderBookOcrService(
            self.library,
            self.base / "missing-ocr",
            self.base / "missing-project",
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 2,
            legacy_embedded_page_reader=lambda _path, _rel, page: (
                {
                    "chars": [{
                        "c": "A", "x0": 1, "y0": 1, "x1": 2, "y1": 2,
                    }],
                    "page_w": 10,
                    "page_h": 10,
                    "furigana": [],
                }
                if page == 1 else None
            ),
        )
        preview = service.preview_adoption(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertFalse(preview["available"])
        self.assertEqual(preview["missingPages"], [2])
        with self.assertRaises(ReaderBookOcrError) as missing:
            service.adopt_legacy(self.entry["bookId"], self.entry["contentSha256"])
        self.assertEqual(missing.exception.code, "legacy-result-incomplete")
        self.assertFalse(any((self.base / "missing-ocr").rglob("publication.json")))

    def test_legacy_adoption_hashes_actual_pdf_not_only_catalog_metadata(self) -> None:
        path = self.vault / "A.pdf"
        path.write_bytes(PDF_A + b"changed")

        class StaleCatalog:
            def resolve(_self, _book_id):
                return dict(self.entry), path

        service = ReaderBookOcrService(
            StaleCatalog(),
            self.base / "stale-ocr",
            self.base / "stale-project",
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 1,
        )
        with self.assertRaises(ReaderBookOcrError) as changed:
            service.preview_adoption(
                self.entry["bookId"], self.entry["contentSha256"]
            )
        self.assertEqual(changed.exception.code, "book-version-changed")
        self.assertFalse((self.base / "stale-ocr").exists())

    def test_legacy_sources_require_full_sha_and_exact_language(self) -> None:
        project = self.base / "provenance-project"
        rel_key = hashlib.sha1(self.entry["rel"].encode("utf-8")).hexdigest()[:16]
        mtime = int((self.vault / "A.pdf").stat().st_mtime)

        def layer(text: str) -> dict:
            return {
                "chars": [{"c": text, "x0": 1, "y0": 1, "x1": 2, "y1": 2}],
                "page_w": 10, "page_h": 20, "furigana": [],
            }

        override = project / "state" / "pdf-page-ocr" / f"{rel_key}-p1.json"
        override.parent.mkdir(parents=True)
        override.write_text(json.dumps(layer("unbound-override")), "utf-8")
        cache_dir = project / "state" / "pdf-char-cache"
        cache_dir.mkdir(parents=True)
        (cache_dir / f"{rel_key}-p1-{mtime}-zh.json").write_text(json.dumps({
            **layer("wrong-language"),
            "cver": 11,
            "sourceContentSha256": self.entry["contentSha256"],
        }), "utf-8")
        (cache_dir / f"{rel_key}-p1-{mtime}-ja.json").write_text(json.dumps({
            **layer("unbound-cache"), "cver": 11,
        }), "utf-8")
        formula_key = hashlib.sha1(
            str((self.vault / "A.pdf").resolve()).encode("utf-8")
        ).hexdigest()[:16]
        formula = project / "state" / "pdf-figures" / f"{formula_key}.json"
        formula.parent.mkdir(parents=True)
        formula.write_text(json.dumps({
            "pdf": str((self.vault / "A.pdf").resolve()),
            "book_mtime": mtime,
            "formulas": [{"page": 1, "bbox": [0, 0, 1, 1], "latex": "x"}],
        }), "utf-8")
        embedded_calls = []
        service = ReaderBookOcrService(
            self.library,
            self.base / "provenance-ocr",
            project,
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 1,
            legacy_embedded_page_reader=lambda *_args: (
                embedded_calls.append(_args) or layer("embedded")
            ),
            legacy_language_resolver=lambda _rel: "ja",
            legacy_char_cache_version=11,
        )
        preview = service.preview_adoption(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertEqual(
            preview["pageSources"],
            {"override": 0, "char-cache": 0, "embedded": 1, "missing": 0},
        )
        self.assertEqual(preview["formula"], {
            "state": "pending", "count": 0, "reason": "legacy-formulas-unbound",
        })
        self.assertEqual(len(embedded_calls), 1)

    def test_adoption_mirror_failure_keeps_index_truth_and_retry_ignores_residue(self) -> None:
        state_root = self.base / "fence-ocr"
        service = ReaderBookOcrService(
            self.library,
            state_root,
            self.base / "fence-project",
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 1,
            legacy_embedded_page_reader=lambda *_args: {
                "chars": [{"c": "A", "x0": 1, "y0": 1, "x1": 2, "y1": 2}],
                "page_w": 10, "page_h": 20, "furigana": [],
            },
        )
        real_atomic_write = reader_book_ocr.atomic_write_json

        def fail_publication(path, *args, **kwargs):
            if Path(path).name == "publication.json":
                raise OSError("fault before publication fence")
            return real_atomic_write(path, *args, **kwargs)

        with patch.object(reader_book_ocr, "atomic_write_json", side_effect=fail_publication):
            first_job, first_adoption, first_already = service.adopt_legacy(
                self.entry["bookId"], self.entry["contentSha256"]
            )
        self.assertFalse(first_already)
        self.assertEqual(first_job["state"], "succeeded")
        self.assertTrue(first_adoption["alreadyAdopted"])
        version = service._version_dir(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertFalse((version / "publication.json").exists())
        status = service.status(self.entry["bookId"], self.entry["contentSha256"])
        self.assertEqual(status["state"], "succeeded")
        index = json.loads((version / "releases-index.json").read_text("utf-8"))
        self.assertIsNotNone(index["activeRunId"])
        self.assertTrue((version / "publication.json").exists())
        page, _path = service.read_page(
            self.entry["bookId"], self.entry["contentSha256"], 1
        )
        self.assertEqual(page["pageNumber"], 1)

        residue = state_root / ".adopt-staging-residue" / "pages"
        residue.mkdir(parents=True)
        (residue / "p999999.json").write_text("{}", "utf-8")
        job, adoption, already = service.adopt_legacy(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertTrue(already)
        self.assertTrue(adoption["alreadyAdopted"])
        self.assertEqual(job["state"], "succeeded")
        manifest = service.attachment_manifest(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertEqual(
            [item["attachmentId"] for item in manifest["files"]],
            ["ocr-page-000001", "ocr-formulas"],
        )

    def test_adoption_source_replaced_during_index_write_is_rolled_back(self) -> None:
        state_root = self.base / "source-swap-adoption-ocr"
        service = ReaderBookOcrService(
            self.library,
            state_root,
            self.base / "source-swap-project",
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 1,
            legacy_embedded_page_reader=lambda *_args: {
                "chars": [{"c": "A", "x0": 1, "y0": 1, "x1": 2, "y1": 2}],
                "page_w": 10, "page_h": 20, "furigana": [],
            },
        )
        source = self.vault / "A.pdf"
        version = service._version_dir(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        fence_path = version / "publication.json"
        index_path = version / "releases-index.json"
        real_atomic_write = reader_book_ocr.atomic_write_json
        swapped = False

        def replace_source_at_index(path, *args, **kwargs):
            nonlocal swapped
            if Path(path) == index_path and not swapped:
                swapped = True
                replacement = self.vault / "replacement.pdf"
                replacement.write_bytes(PDF_A + b"different")
                if os.name == "nt":
                    # Windows denies replacing a path whose source guard is
                    # open; an in-place mutation exercises the same post-fence
                    # check.  POSIX exercises the original atomic-replace race.
                    source.write_bytes(replacement.read_bytes())
                    replacement.unlink()
                else:
                    os.replace(replacement, source)
            return real_atomic_write(path, *args, **kwargs)

        with patch.object(
            reader_book_ocr, "atomic_write_json", side_effect=replace_source_at_index
        ):
            with self.assertRaises(ReaderBookOcrError) as changed:
                service.adopt_legacy(
                    self.entry["bookId"], self.entry["contentSha256"]
                )
        self.assertTrue(swapped)
        self.assertEqual(changed.exception.code, "book-version-changed")
        self.assertFalse(fence_path.exists())

    def test_adoption_same_metadata_source_change_at_index_is_rolled_back(self) -> None:
        state_root = self.base / "same-metadata-adoption-ocr"
        service = ReaderBookOcrService(
            self.library,
            state_root,
            self.base / "same-metadata-adoption-project",
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 1,
            legacy_embedded_page_reader=lambda *_args: {
                "chars": [{"c": "A", "x0": 1, "y0": 1, "x1": 2, "y1": 2}],
                "page_w": 10, "page_h": 20, "furigana": [],
            },
        )
        source = self.vault / "A.pdf"
        original_stat = source.stat()
        original_identity = service._source_identity(original_stat)
        changed_bytes = bytearray(PDF_A)
        changed_bytes[-1] ^= 1
        version = service._version_dir(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        fence_path = version / "publication.json"
        index_path = version / "releases-index.json"
        real_atomic_write = reader_book_ocr.atomic_write_json
        swapped = False

        def change_source_at_index(path, *args, **kwargs):
            nonlocal swapped
            if Path(path) == index_path and not swapped:
                swapped = True
                source.write_bytes(bytes(changed_bytes))
                os.utime(
                    source,
                    ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
                )
                self.assertEqual(
                    service._source_identity(source.stat()), original_identity
                )
            return real_atomic_write(path, *args, **kwargs)

        with patch.object(
            reader_book_ocr, "atomic_write_json", side_effect=change_source_at_index
        ):
            with self.assertRaises(ReaderBookOcrError) as changed:
                service.adopt_legacy(
                    self.entry["bookId"], self.entry["contentSha256"]
                )
        self.assertTrue(swapped)
        self.assertEqual(changed.exception.code, "book-version-changed")
        self.assertNotEqual(
            hashlib.sha256(source.read_bytes()).hexdigest(),
            self.entry["contentSha256"],
        )
        self.assertFalse(fence_path.exists())

    def test_same_byte_atomic_source_replacement_keeps_old_revision_readable(self) -> None:
        service = ReaderBookOcrService(
            self.library,
            self.base / "same-byte-ocr",
            self.base / "same-byte-project",
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 1,
            legacy_embedded_page_reader=lambda *_args: {
                "chars": [{"c": "A", "x0": 1, "y0": 1, "x1": 2, "y1": 2}],
                "page_w": 10, "page_h": 20, "furigana": [],
            },
        )
        _job, adoption, _already = service.adopt_legacy(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        source = self.vault / "A.pdf"
        replacement = self.vault / "same-bytes.pdf"
        replacement.write_bytes(PDF_A)
        os.replace(replacement, source)

        entry, path, manifest = service.read_attachment(
            self.entry["bookId"],
            self.entry["contentSha256"],
            "ocr-page-000001",
            expected_revision=adoption["revision"],
        )
        self.assertEqual(manifest["revision"], adoption["revision"])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), entry["sha256"])

    def test_non_adoption_publication_is_not_reported_as_adopted_and_formula_failed_stays_failed(self) -> None:
        state_root = self.base / "normal-publication-ocr"
        launches = []
        service = ReaderBookOcrService(
            self.library,
            state_root,
            self.base / "normal-project",
            launcher=lambda *args: (
                launches.append(args) or _FakeProcess(self.fake_worker_pid)
            ),
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 1,
            legacy_embedded_page_reader=lambda *_args: {
                "chars": [{"c": "E", "x0": 1, "y0": 1, "x1": 2, "y1": 2}],
                "page_w": 10, "page_h": 20, "furigana": [],
            },
        )
        version = service._version_dir(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        job_dir = version / "vision"
        pages = job_dir / "pages"
        pages.mkdir(parents=True)
        (pages / "p000001.json").write_text(json.dumps(_with_test_layout({
            "schema": "reader-page-chars/1",
            "bookId": self.entry["bookId"],
            "contentSha256": self.entry["contentSha256"],
            "engine": "vision",
            "pageNumber": 1,
            "page_w": 10,
            "page_h": 20,
            "chars": [{"c": "V", "x0": 1, "y0": 1, "x1": 2, "y1": 2}],
            "furigana": [],
        })), "utf-8")
        formula_path = job_dir / "formula-source.json"
        formula_path.write_text('{"formulas":[]}', "utf-8")
        final_job = {
            "contract": "reader-library-ocr/1",
            "jobId": "ocrjob_normal",
            "bookId": self.entry["bookId"],
            "contentSha256": self.entry["contentSha256"],
            "engine": "vision",
            "state": "succeeded",
            "totalPages": 1,
            "successfulPages": 1,
            "formulaState": "failed",
            "formulaTotal": 0,
            "resultAvailable": True,
            "updatedAtEpochMs": 1,
        }
        (job_dir / "job.json").write_text(json.dumps(final_job), "utf-8")
        vision_revision = _publish_release(
            SimpleNamespace(
                book_id=self.entry["bookId"],
                content_sha256=self.entry["contentSha256"],
                engine="vision",
                max_bytes=1024 * 1024,
            ),
            job_dir,
            formula_path,
            final_job,
            source_path=self.vault / "A.pdf",
        )
        preview = service.preview_adoption(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertFalse(preview["alreadyAdopted"])
        self.assertEqual(preview["pageSources"]["embedded"], 1)
        published_status = service.status(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertEqual(published_status["engine"], "vision")
        self.assertEqual(published_status["formulaState"], "failed")
        old_entry, old_path, _old_manifest = service.read_attachment(
            self.entry["bookId"],
            self.entry["contentSha256"],
            "ocr-page-000001",
            expected_revision=vision_revision,
        )
        old_payload = old_path.read_bytes()

        (job_dir / "job.json").write_text(json.dumps({
            **final_job,
            "jobId": "ocrjob_killed_before_fence",
            "state": "running",
            "phase": "finalizing",
            "pid": 999999999,
            "resultAvailable": False,
            "pageCharsRevision": None,
        }), "utf-8")
        killed = service.status(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertEqual(killed["state"], "failed")
        self.assertEqual(killed["errorCode"], "worker-stopped")
        self.assertTrue(killed["canRetry"])

        (job_dir / "job.json").write_text(json.dumps({
            **final_job,
            "jobId": "ocrjob_unpublished_same_engine",
            "pageCharsRevision": None,
        }), "utf-8")
        mismatched = service.status(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertEqual(mismatched["state"], "failed")
        self.assertEqual(mismatched["errorCode"], "ocr-publication-incomplete")
        restored, already = service.start(
            self.entry["bookId"], self.entry["contentSha256"], "vision"
        )
        self.assertFalse(already)
        self.assertEqual(restored["state"], "queued")
        self.assertEqual(restored["processingProfile"], "pi-default-v5")
        self.assertNotEqual(restored["jobId"], final_job["jobId"])

        current_vision = json.loads((job_dir / "job.json").read_text("utf-8"))
        current_vision["state"] = "failed"
        (job_dir / "job.json").write_text(json.dumps(current_vision), "utf-8")

        manga_dir = version / "manga"
        manga_dir.mkdir(parents=True)
        (manga_dir / "job.json").write_text(json.dumps({
            **final_job,
            "jobId": "ocrjob_stale_manga",
            "engine": "manga",
        }), "utf-8")
        switched, already = service.start(
            self.entry["bookId"], self.entry["contentSha256"], "manga"
        )
        self.assertFalse(already)
        self.assertEqual(switched["state"], "queued")
        self.assertEqual(switched["engine"], "manga")
        self.assertEqual(len(launches), 2)
        self.assertEqual(
            service.attachment_manifest(
                self.entry["bookId"], self.entry["contentSha256"]
            )["engine"],
            "vision",
        )
        self.assertEqual(
            service.read_page(
                self.entry["bookId"], self.entry["contentSha256"], 1
            )[0]["engine"],
            "vision",
        )

        manga_pages = manga_dir / "pages"
        manga_pages.mkdir()
        (manga_pages / "p000001.json").write_text(json.dumps(_with_test_layout({
            "schema": "reader-page-chars/1",
            "bookId": self.entry["bookId"],
            "contentSha256": self.entry["contentSha256"],
            "engine": "manga",
            "executor": switched["executor"],
            "processingProfile": switched["processingProfile"],
            "pageNumber": 1,
            "page_w": 10,
            "page_h": 20,
            "chars": [{"c": "M", "x0": 1, "y0": 1, "x1": 2, "y1": 2}],
            "furigana": [],
        })), "utf-8")
        manga_formula = manga_dir / "formula-source.json"
        manga_formula.write_text('{"formulas":[]}', "utf-8")
        manga_final = {
            **switched,
            "state": "succeeded",
            "totalPages": 1,
            "successfulPages": 1,
            "formulaState": "failed",
            "formulaTotal": 0,
            "resultAvailable": True,
        }
        manga_revision = _publish_release(
            SimpleNamespace(
                book_id=self.entry["bookId"],
                content_sha256=self.entry["contentSha256"],
                engine="manga",
                max_bytes=1024 * 1024,
            ),
            manga_dir,
            manga_formula,
            manga_final,
            source_path=self.vault / "A.pdf",
        )
        self.assertNotEqual(manga_revision, vision_revision)
        self.assertEqual(
            service.attachment_manifest(
                self.entry["bookId"], self.entry["contentSha256"]
            )["engine"],
            "manga",
        )
        retained_entry, retained_path, retained_manifest = service.read_attachment(
            self.entry["bookId"],
            self.entry["contentSha256"],
            "ocr-page-000001",
            expected_revision=vision_revision,
        )
        self.assertEqual(retained_manifest["revision"], vision_revision)
        self.assertEqual(retained_entry["sha256"], old_entry["sha256"])
        self.assertEqual(retained_path.read_bytes(), old_payload)

    def test_partial_release_manifest_is_rejected_even_with_updated_fence_digest(self) -> None:
        service = ReaderBookOcrService(
            self.library,
            self.base / "partial-ocr",
            self.base / "partial-project",
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 2,
            legacy_embedded_page_reader=lambda _path, _rel, page: {
                "chars": [{"c": str(page), "x0": 1, "y0": 1, "x1": 2, "y1": 2}],
                "page_w": 10, "page_h": 20, "furigana": [],
            },
        )
        _job, adoption, _already = service.adopt_legacy(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        version = service._version_dir(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        release = version / "legacy" / "releases" / adoption["revision"]
        manifest_path = release / "attachments.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["files"] = [
            item for item in manifest["files"]
            if item["attachmentId"] != "ocr-page-000002"
        ]
        manifest_path.write_text(json.dumps(manifest), "utf-8")
        fence_path = version / "publication.json"
        fence = json.loads(fence_path.read_text("utf-8"))
        fence["manifestSha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        fence_path.write_text(json.dumps(fence), "utf-8")
        with self.assertRaises(ReaderBookOcrError) as invalid:
            service.attachment_manifest(
                self.entry["bookId"], self.entry["contentSha256"]
            )
        self.assertEqual(invalid.exception.code, "ocr-publication-invalid")

    def test_attachment_manifest_is_immutable_and_path_whitelisted(self) -> None:
        project = self.base / "manifest-project"
        formula_key = hashlib.sha1(
            str((self.vault / "A.pdf").resolve()).encode("utf-8")
        ).hexdigest()[:16]
        formula_source = project / "state" / "pdf-figures" / f"{formula_key}.json"
        formula_source.parent.mkdir(parents=True)
        formula_source.write_text(json.dumps({
            "sourceContentSha256": self.entry["contentSha256"],
            "pdf": str((self.vault / "A.pdf").resolve()),
            "book_mtime": int((self.vault / "A.pdf").stat().st_mtime),
            "formulas": [{"page": 1, "bbox": [0, 0, 0.5, 0.5], "latex": "x"}],
        }), "utf-8")
        service = ReaderBookOcrService(
            self.library,
            self.base / "manifest-ocr",
            project,
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 1,
            legacy_embedded_page_reader=lambda *_args: {
                "chars": [{"c": "A", "x0": 1, "y0": 1, "x1": 2, "y1": 2}],
                "page_w": 10, "page_h": 20, "furigana": [],
            },
        )
        _job, adoption, _already = service.adopt_legacy(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        loaded = service.attachment_manifest(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertEqual(loaded["category"], "derived")
        self.assertEqual(loaded["revision"], adoption["revision"])
        self.assertEqual(service.read_formulas(
            self.entry["bookId"], self.entry["contentSha256"]
        )[0]["latex"], "x")
        entry, resolved, manifest = service.read_attachment(
            self.entry["bookId"], self.entry["contentSha256"], "ocr-page-000001"
        )
        self.assertEqual(entry["sha256"], hashlib.sha256(resolved.read_bytes()).hexdigest())
        with self.assertRaises(ReaderBookOcrError):
            service.read_attachment(
                self.entry["bookId"], self.entry["contentSha256"], "../../A.pdf"
            )

        manifest["files"][0]["downloadUrl"] = "../../A.pdf"
        resolved.parent.parent.joinpath("attachments.json").write_text(
            json.dumps(manifest), "utf-8"
        )
        with self.assertRaises(ReaderBookOcrError) as invalid_manifest:
            service.attachment_manifest(
                self.entry["bookId"], self.entry["contentSha256"]
            )
        self.assertEqual(invalid_manifest.exception.code, "ocr-publication-invalid")


class ReaderBookOcrWorkerContractTest(unittest.TestCase):
    def test_empty_vision_layout_fails_closed(self) -> None:
        self.assertEqual(
            _vision_page_layout([], page_w=100, page_h=200),
            reader_book_ocr_worker._unavailable_page_layout(),
        )

    def test_layout_table_is_row_major_and_conserves_final_char_indices(self) -> None:
        chars = []
        # Deliberately keep a non-row-major Vision source order. Layout ranges
        # must point into this exact array while region.order becomes row-major.
        for row, column, text in (
            (1, 2, "F"), (0, 0, "A"), (2, 1, "H"),
            (0, 2, "C"), (1, 0, "D"), (2, 2, "I"),
            (0, 1, "B"), (1, 1, "E"), (2, 0, "G"),
        ):
            chars.append({
                "c": text,
                "x0": column * 100 + 20,
                "y0": row * 100 + 120,
                "x1": column * 100 + 40,
                "y1": row * 100 + 150,
                "w": -1,
                "bk": row,
                "b": 0,
            })
        # Spaces are real sidecar entries and must be conserved too.
        chars.append({
            "c": " ", "sp": 1,
            "x0": 120, "y0": 120, "x1": 140, "y1": 150,
            "w": -1, "bk": 0, "b": 0,
        })
        original = [dict(item) for item in chars]
        layout = _manga_page_layout(
            chars,
            [],
            [{
                "xEdges": [0, 100, 200, 300],
                "yEdges": [100, 200, 300, 400],
            }],
            page_w=400,
            page_h=500,
        )

        self.assertEqual(chars, original)
        self.assertEqual(layout["schema"], "reader-page-layout/1")
        self.assertEqual(layout["mode"], "table")
        self.assertEqual(layout["confidence"], "high")
        cells = sorted(layout["regions"], key=lambda item: item["order"])
        positions = [(item["row"], item["column"]) for item in cells]
        self.assertEqual(
            positions,
            sorted(positions),
            "multiple continuous runs in one cell must remain adjacent and row-major",
        )
        self.assertEqual(
            set(positions),
            {(row, column) for row in range(3) for column in range(3)},
        )
        covered = [
            index
            for region in cells
            for start, end in region["ranges"]
            for index in range(start, end + 1)
        ]
        self.assertEqual(sorted(covered), list(range(len(chars))))
        self.assertEqual(len(covered), len(set(covered)))
        self.assertIn(9, covered, "sp entries are part of the conserved index space")

    def test_table_cell_regions_restore_geometry_after_source_index_wrap(self) -> None:
        chars = [
            {"c": "D", "x0": 40, "y0": 20, "x1": 48, "y1": 40, "w": -1, "bk": 0, "b": 0},
            {"c": "E", "x0": 50, "y0": 20, "x1": 58, "y1": 40, "w": -1, "bk": 0, "b": 0},
            {"c": "F", "x0": 60, "y0": 20, "x1": 68, "y1": 40, "w": -1, "bk": 0, "b": 0},
            {"c": " ", "sp": 1, "x0": 69, "y0": 20, "x1": 72, "y1": 40, "w": -1, "bk": 0, "b": 0},
            {"c": "X", "x0": 110, "y0": 20, "x1": 118, "y1": 40, "w": -1, "bk": 1, "b": 0},
            {"c": "Y", "x0": 10, "y0": 120, "x1": 18, "y1": 140, "w": -1, "bk": 2, "b": 0},
            {"c": "A", "x0": 10, "y0": 20, "x1": 18, "y1": 40, "w": -1, "bk": 0, "b": 0},
            {"c": "B", "x0": 20, "y0": 20, "x1": 28, "y1": 40, "w": -1, "bk": 0, "b": 0},
            {"c": "C", "x0": 30, "y0": 20, "x1": 38, "y1": 40, "w": -1, "bk": 0, "b": 0},
            {"c": "Z", "x0": 110, "y0": 120, "x1": 118, "y1": 140, "w": -1, "bk": 3, "b": 0},
            {"c": "G", "x0": 10, "y0": 60, "x1": 18, "y1": 80, "w": -1, "bk": 0, "b": 0},
            {"c": "H", "x0": 20, "y0": 60, "x1": 28, "y1": 80, "w": -1, "bk": 0, "b": 0},
        ]
        original = [dict(item) for item in chars]
        layout = _manga_page_layout(
            chars,
            [],
            [{"xEdges": [0, 100, 200, 300], "yEdges": [0, 100, 200]}],
            page_w=300,
            page_h=200,
        )

        target_regions = [
            region
            for region in sorted(layout["regions"], key=lambda item: item["order"])
            if region["kind"] == "table-cell"
            and region["row"] == 0
            and region["column"] == 0
        ]
        rendered = [
            "".join(
                chars[index]["c"]
                for start, end in region["ranges"]
                for index in range(start, end + 1)
            )
            for region in target_regions
        ]
        covered = sorted(
            index
            for region in layout["regions"]
            for start, end in region["ranges"]
            for index in range(start, end + 1)
        )
        self.assertEqual(rendered, ["ABC", "DEF ", "GH"])
        self.assertLess(
            max(target_regions[0]["bounds"][1], target_regions[1]["bounds"][1]),
            min(target_regions[0]["bounds"][3], target_regions[1]["bounds"][3]),
            "same visual line source-wrap runs must overlap vertically",
        )
        self.assertLessEqual(
            target_regions[0]["bounds"][3],
            target_regions[2]["bounds"][1],
            "different visual lines must expose distinct vertical bounds",
        )
        self.assertEqual(chars, original)
        self.assertEqual(covered, list(range(len(chars))))
        self.assertEqual(len(covered), len(set(covered)))

    def test_table_cell_regions_split_same_line_source_index_gaps(self) -> None:
        chars = [
            {"c": "A", "x0": 10, "y0": 20, "x1": 18, "y1": 40, "w": -1, "bk": 0, "b": 0},
            {"c": "X", "x0": 110, "y0": 20, "x1": 118, "y1": 40, "w": -1, "bk": 1, "b": 0},
            {"c": "C", "x0": 30, "y0": 20, "x1": 38, "y1": 40, "w": -1, "bk": 0, "b": 0},
            {"c": "Y", "x0": 10, "y0": 120, "x1": 18, "y1": 140, "w": -1, "bk": 2, "b": 0},
            {"c": "Z", "x0": 110, "y0": 120, "x1": 118, "y1": 140, "w": -1, "bk": 3, "b": 0},
        ]
        layout = _manga_page_layout(
            chars,
            [],
            [{"xEdges": [0, 100, 200, 300], "yEdges": [0, 100, 200]}],
            page_w=300,
            page_h=200,
        )
        target_regions = [
            region
            for region in sorted(layout["regions"], key=lambda item: item["order"])
            if region["kind"] == "table-cell"
            and region["row"] == 0
            and region["column"] == 0
        ]
        rendered = [
            "".join(
                chars[index]["c"]
                for start, end in region["ranges"]
                for index in range(start, end + 1)
            )
            for region in target_regions
        ]
        covered = sorted(
            index
            for region in layout["regions"]
            for start, end in region["ranges"]
            for index in range(start, end + 1)
        )
        self.assertEqual(rendered, ["A", "C"])
        self.assertTrue(all(len(region["ranges"]) == 1 for region in layout["regions"]))
        self.assertLess(
            max(target_regions[0]["bounds"][1], target_regions[1]["bounds"][1]),
            min(target_regions[0]["bounds"][3], target_regions[1]["bounds"][3]),
        )
        self.assertEqual(covered, list(range(len(chars))))
        self.assertEqual(len(covered), len(set(covered)))

    def test_layout_two_by_two_grid_does_not_misclassify_manga_panel(self) -> None:
        chars = [
            {"c": "右", "x0": 220, "y0": 20, "x1": 240, "y1": 50, "w": -1, "bk": 0, "b": 0},
            {"c": "左", "x0": 20, "y0": 120, "x1": 40, "y1": 150, "w": -1, "bk": 1, "b": 0},
        ]
        lines = [
            {"bounds": (210, 10, 250, 60), "bk": 0, "vertical": True},
            {"bounds": (10, 110, 50, 160), "bk": 1, "vertical": True},
        ]
        layout = _manga_page_layout(
            chars,
            lines,
            [{"xEdges": [0, 150, 300], "yEdges": [0, 100, 200]}],
            page_w=300,
            page_h=200,
        )
        self.assertEqual(layout["mode"], "manga")
        self.assertEqual(layout["tables"], [])
        self.assertEqual(layout["gridColumns"], 4)

    def test_layout_rejects_every_two_column_ruled_grid(self) -> None:
        chars = [
            {"c": "右", "x0": 220, "y0": 20, "x1": 240, "y1": 50, "w": -1, "bk": 0, "b": 0},
            {"c": "左", "x0": 20, "y0": 120, "x1": 40, "y1": 150, "w": -1, "bk": 1, "b": 0},
        ]
        lines = [
            {"bounds": (210, 10, 250, 60), "bk": 0, "vertical": True},
            {"bounds": (10, 110, 50, 160), "bk": 1, "vertical": True},
        ]
        layout = _manga_page_layout(
            chars,
            lines,
            [{"xEdges": [0, 150, 300], "yEdges": [0, 100, 200, 300]}],
            page_w=300,
            page_h=300,
        )
        self.assertEqual(layout["mode"], "manga")
        self.assertEqual(layout["tables"], [])

    def test_layout_manga_uses_vision_only_and_adds_supplement(self) -> None:
        chars = [
            {"c": "右", "x0": 320, "y0": 20, "x1": 340, "y1": 50, "w": 1, "bk": 7, "b": 0},
            {"c": " ", "sp": 1, "x0": 320, "y0": 20, "x1": 340, "y1": 50, "w": 1, "bk": 7, "b": 0},
            {"c": "左", "x0": 20, "y0": 220, "x1": 40, "y1": 250, "w": 2, "bk": 8, "b": 0},
            {"c": "外", "x0": 190, "y0": 390, "x1": 210, "y1": 420, "w": 3, "bk": 9, "b": 0},
        ]
        layout = _manga_page_layout(
            chars,
            [
                {"bounds": (300, 0, 360, 80), "bk": 0, "vertical": True},
                {"bounds": (0, 200, 70, 280), "bk": 1, "vertical": True},
            ],
            [],
            page_w=400,
            page_h=500,
        )
        self.assertEqual(layout["textSource"], "vision")
        self.assertEqual(layout["layoutSource"], "manga")
        self.assertEqual(layout["readingDirection"], "rtl")
        self.assertEqual(layout["gridColumns"], 4)
        self.assertEqual(layout["confidence"], "low")
        self.assertEqual(
            sum(region["kind"] == "vision-supplement" for region in layout["regions"]),
            1,
        )
        covered = sorted(
            index
            for region in layout["regions"]
            for start, end in region["ranges"]
            for index in range(start, end + 1)
        )
        self.assertEqual(covered, list(range(len(chars))))

    def test_table_confidence_uses_structured_visible_coverage(self) -> None:
        table_chars = [
            {"c": text, "x0": x, "y0": y, "x1": x + 10, "y1": y + 10,
             "w": -1, "bk": index, "b": 0}
            for index, (text, x, y) in enumerate((
                ("A", 10, 110), ("B", 110, 110),
                ("C", 10, 210), ("D", 110, 210),
            ))
        ]
        supplements = [
            {"c": chr(97 + index), "x0": 310 + index, "y0": 20,
             "x1": 311 + index, "y1": 30, "w": -1, "bk": 10 + index, "b": 0}
            for index in range(12)
        ]
        boundary = _manga_page_layout(
            [*table_chars, *supplements],
            [],
            [{"xEdges": [0, 100, 200, 300], "yEdges": [100, 200, 300]}],
            page_w=400,
            page_h=400,
        )
        self.assertEqual(boundary["mode"], "table")
        self.assertEqual(boundary["confidence"], "high")
        below_boundary = _manga_page_layout(
            [
                *table_chars,
                *supplements,
                {"c": "!", "x0": 390, "y0": 20, "x1": 391, "y1": 30,
                 "w": -1, "bk": 99, "b": 0},
            ],
            [],
            [{"xEdges": [0, 100, 200, 300], "yEdges": [100, 200, 300]}],
            page_w=400,
            page_h=400,
        )
        self.assertEqual(below_boundary["confidence"], "low")

    def test_table_and_ordinary_same_band_respect_rtl_column_order(self) -> None:
        chars = [
            {"c": "左", "x0": 20, "y0": 20, "x1": 40, "y1": 50, "w": -1, "bk": 0, "b": 0},
            {"c": "右", "x0": 350, "y0": 20, "x1": 370, "y1": 50, "w": -1, "bk": 1, "b": 0},
            {"c": "A", "x0": 10, "y0": 110, "x1": 20, "y1": 120, "w": -1, "bk": 2, "b": 0},
            {"c": "B", "x0": 110, "y0": 110, "x1": 120, "y1": 120, "w": -1, "bk": 3, "b": 0},
            {"c": "C", "x0": 10, "y0": 210, "x1": 20, "y1": 220, "w": -1, "bk": 4, "b": 0},
            {"c": "D", "x0": 110, "y0": 210, "x1": 120, "y1": 220, "w": -1, "bk": 5, "b": 0},
        ]
        layout = _manga_page_layout(
            chars,
            [
                {"bounds": (10, 10, 50, 60), "bk": 0, "vertical": True},
                {"bounds": (340, 10, 380, 60), "bk": 1, "vertical": True},
            ],
            [{"xEdges": [0, 100, 200, 300], "yEdges": [100, 200, 300]}],
            page_w=400,
            page_h=400,
        )
        ordinary = [
            region for region in sorted(layout["regions"], key=lambda item: item["order"])
            if region["kind"] == "manga-region"
        ]
        self.assertEqual(layout["readingDirection"], "rtl")
        self.assertEqual([region["ranges"] for region in ordinary], [[[1, 1]], [[0, 0]]])

    def test_table_producer_caps_total_declared_cells(self) -> None:
        first_y = list(range(0, 3001))
        second_y = list(range(3001, 6002))
        chars = [
            {"c": "A", "x0": 10, "y0": 10, "x1": 20, "y1": 20, "w": -1, "bk": 0, "b": 0},
            {"c": "B", "x0": 110, "y0": 10, "x1": 120, "y1": 20, "w": -1, "bk": 1, "b": 0},
            {"c": "C", "x0": 10, "y0": 1010, "x1": 20, "y1": 1020, "w": -1, "bk": 2, "b": 0},
            {"c": "D", "x0": 110, "y0": 1010, "x1": 120, "y1": 1020, "w": -1, "bk": 3, "b": 0},
            {"c": "E", "x0": 10, "y0": 3011, "x1": 20, "y1": 3021, "w": -1, "bk": 4, "b": 0},
            {"c": "F", "x0": 110, "y0": 3011, "x1": 120, "y1": 3021, "w": -1, "bk": 5, "b": 0},
            {"c": "G", "x0": 10, "y0": 4011, "x1": 20, "y1": 4021, "w": -1, "bk": 6, "b": 0},
            {"c": "H", "x0": 110, "y0": 4011, "x1": 120, "y1": 4021, "w": -1, "bk": 7, "b": 0},
        ]
        layout = _manga_page_layout(
            chars,
            [],
            [
                {"xEdges": [0, 100, 200, 300], "yEdges": first_y},
                {"xEdges": [0, 100, 200, 300], "yEdges": second_y},
            ],
            page_w=400,
            page_h=6001,
        )
        self.assertEqual(len(layout["tables"]), 1)
        self.assertLessEqual(
            sum(table["rows"] * table["columns"] for table in layout["tables"]),
            16_384,
        )

    def test_ruled_table_uses_cell_order_and_exact_vision_boxes(self) -> None:
        try:
            import cv2
            import numpy as np
        except ImportError:
            self.skipTest("OpenCV table detector unavailable")
        image = np.full((600, 900), 255, dtype=np.uint8)
        for y in (200, 250, 300, 350):
            cv2.line(image, (100, y), (800, y), 0, 3)
        for x in (300, 520):
            cv2.line(image, (x, 200), (x, 350), 0, 3)
        cv2.line(image, (0, 400), (899, 400), 0, 3)
        grids = _detect_ruled_table_grids(image, sx=1.0, sy=1.0)
        self.assertEqual(len(grids), 1)
        self.assertEqual(len(grids[0]["xEdges"]), 4)
        self.assertEqual(len(grids[0]["yEdges"]), 4)
        self.assertAlmostEqual(grids[0]["xEdges"][1], 300, delta=5)
        self.assertAlmostEqual(grids[0]["xEdges"][2], 520, delta=5)
        self.assertAlmostEqual(grids[0]["yEdges"][-1], 350, delta=5)

        prepared = [
            {"text": "表の前", "bounds": (100, 100, 250, 140), "bk": 0,
             "line": 0, "vertical": False},
            {"text": "国名特徴主な料理", "bounds": (120, 210, 780, 240), "bk": 1,
             "line": 1, "vertical": False},
            {"text": "韓国医食同源キムチ", "bounds": (120, 260, 780, 290), "bk": 2,
             "line": 2, "vertical": False},
            {"text": "印度香辛料ナン", "bounds": (120, 310, 780, 340), "bk": 3,
             "line": 3, "vertical": False},
        ]
        values = [
            (0, 0, "国名", 130, 212), (0, 1, "特徴", 330, 212),
            (0, 2, "主な料理", 550, 212),
            (1, 0, "韓国", 130, 262), (1, 1, "医食同源", 330, 262),
            (1, 2, "キムチ", 550, 262),
            (2, 0, "印度", 130, 312), (2, 1, "香辛料", 330, 312),
            (2, 2, "ナン", 550, 312),
        ]
        vision = []
        expected_x = {}
        for row, column, text, start_x, start_y in values:
            for offset, character in enumerate(text):
                x0 = start_x + offset * 18
                vision.append({
                    "c": character, "x0": x0, "y0": start_y,
                    "x1": x0 + 14, "y1": start_y + 24, "w": -1,
                })
                expected_x[(row, column, offset)] = x0
        for row in range(3):
            prepared[row + 1]["cells"] = [
                (
                    character,
                    start_x + offset * 18,
                    start_y,
                    start_x + offset * 18 + 14,
                    start_y + 24,
                )
                for value_row, _column, text, start_x, start_y in values
                if value_row == row
                for offset, character in enumerate(text)
            ]
            prepared[row + 1]["polygon"] = None
        split = _manga_table_cell_lines(prepared, vision, grids)
        self.assertEqual(split[0]["text"], "表の前")
        table = split[1:]
        self.assertEqual(
            [line["text"] for line in table],
            [value[2] for value in values],
        )
        self.assertEqual(table[-1]["vision_chars"][0]["x0"], 550)
        self.assertEqual(
            len({line["bk"] for line in table}),
            len(table),
            "each populated cell must keep an independent selection block",
        )
        self.assertTrue(all(line["vertical"] is False for line in table))
        self.assertEqual(
            _proven_table_layout_grids(
                prepared, vision, grids, sx=1.0, sy=1.0
            ),
            grids,
        )

    def test_borderless_layout_does_not_trigger_table_reordering(self) -> None:
        try:
            import numpy as np
        except ImportError:
            self.skipTest("NumPy unavailable")
        image = np.full((400, 700), 255, dtype=np.uint8)
        self.assertEqual(
            _detect_ruled_table_grids(image, sx=1.0, sy=1.0),
            [],
        )

    def test_two_by_two_manga_panel_grid_is_not_a_ruled_table(self) -> None:
        try:
            import cv2
            import numpy as np
        except ImportError:
            self.skipTest("OpenCV table detector unavailable")
        image = np.full((800, 900), 255, dtype=np.uint8)
        for y in (100, 170, 240):
            cv2.line(image, (100, y), (800, y), 0, 3)
        for x in (100, 450, 800):
            cv2.line(image, (x, 100), (x, 240), 0, 3)

        self.assertEqual(
            _detect_ruled_table_grids(image, sx=1.0, sy=1.0),
            [],
        )

    def test_parallel_tables_fail_closed_instead_of_becoming_one_grid(self) -> None:
        try:
            import cv2
            import numpy as np
        except ImportError:
            self.skipTest("OpenCV table detector unavailable")
        image = np.full((800, 1200), 255, dtype=np.uint8)
        for x0, separator, x1 in ((50, 250, 450), (650, 850, 1050)):
            for y in (150, 210, 270):
                cv2.line(image, (x0, y), (x1, y), 0, 3)
            for x in (x0, separator, x1):
                cv2.line(image, (x, 150), (x, 270), 0, 3)

        self.assertEqual(
            _detect_ruled_table_grids(image, sx=1.0, sy=1.0),
            [],
        )

    def test_ruled_table_keeps_manga_when_vision_misses_a_populated_cell(self) -> None:
        grids = [{
            "xEdges": [0, 100, 200, 300],
            "yEdges": [0, 100, 200, 300],
        }]
        prepared = []
        vision = []
        values = [
            ["AAAA", "BBBB", "CCCC"],
            ["DDDD", "EEEE", "FFFF"],
            ["GGGG", "HHHH", "IIII"],
        ]
        for row, row_values in enumerate(values):
            cells = []
            for column, text in enumerate(row_values):
                for offset, character in enumerate(text):
                    x0 = 10 + column * 100 + offset * 10
                    y0 = 10 + row * 100
                    cells.append((character, x0, y0, x0 + 8, y0 + 20))
                    if (row, column) != (2, 2):
                        vision.append({
                            "c": character,
                            "x0": x0,
                            "y0": y0,
                            "x1": x0 + 8,
                            "y1": y0 + 20,
                            "w": -1,
                            "b": 0,
                        })
            prepared.append({
                "text": "".join(row_values),
                "bounds": (10, 10 + row * 100, 290, 90 + row * 100),
                "bk": row,
                "line": row,
                "vertical": False,
                "cells": cells,
                "polygon": None,
            })

        split = _manga_table_cell_lines(prepared, vision, grids)

        self.assertEqual(split, prepared)
        self.assertIn("IIII", "".join(line["text"] for line in split))

    def test_ruled_table_keeps_manga_when_vision_partially_misses_a_cell(self) -> None:
        grids = [{
            "xEdges": [0, 100, 200, 300],
            "yEdges": [0, 100, 200, 300],
        }]
        prepared = []
        vision = []
        values = [
            ["AAAA", "BBBB", "CCCC"],
            ["DDDD", "EEEE", "FFFF"],
            ["GGGG", "HHHH", "IJKL"],
        ]
        for row, row_values in enumerate(values):
            cells = []
            for column, text in enumerate(row_values):
                for offset, character in enumerate(text):
                    x0 = 10 + column * 100 + offset * 10
                    y0 = 10 + row * 100
                    cells.append((character, x0, y0, x0 + 8, y0 + 20))
                    if (row, column) != (2, 2) or offset != 1:
                        vision.append({
                            "c": character,
                            "x0": x0,
                            "y0": y0,
                            "x1": x0 + 8,
                            "y1": y0 + 20,
                            "w": -1,
                            "b": 0,
                        })
            prepared.append({
                "text": "".join(row_values),
                "bounds": (10, 10 + row * 100, 290, 90 + row * 100),
                "bk": row,
                "line": row,
                "vertical": False,
                "cells": cells,
                "polygon": None,
            })

        split = _manga_table_cell_lines(prepared, vision, grids)

        self.assertEqual(split, prepared)
        self.assertIn("IJKL", "".join(line["text"] for line in split))

    def test_ruled_table_never_overrides_vertical_manga_regions(self) -> None:
        grids = [{
            "xEdges": [0, 100, 200, 300],
            "yEdges": [0, 100, 200, 300],
        }]
        prepared = []
        vision = []
        for row in range(3):
            cells = []
            text = "".join(chr(65 + row * 3 + column) * 2 for column in range(3))
            for column in range(3):
                for offset in range(2):
                    character = chr(65 + row * 3 + column)
                    x0 = 10 + column * 100 + offset * 12
                    y0 = 10 + row * 100
                    cells.append((character, x0, y0, x0 + 9, y0 + 20))
                    vision.append({
                        "c": character,
                        "x0": x0,
                        "y0": y0,
                        "x1": x0 + 9,
                        "y1": y0 + 20,
                        "w": -1,
                        "b": 0,
                    })
            prepared.append({
                "text": text,
                "bounds": (10, 10 + row * 100, 290, 90 + row * 100),
                "bk": row,
                "line": row,
                "vertical": True,
                "cells": cells,
                "polygon": None,
            })

        self.assertEqual(
            _manga_table_cell_lines(prepared, vision, grids),
            prepared,
        )

    def test_ruled_table_stays_after_nearby_heading_despite_manga_block_order(self) -> None:
        grids = [{
            "xEdges": [100, 300, 500, 700],
            "yEdges": [200, 250, 300, 350],
        }]
        table_lines = [
            {
                "text": "甲甲乙乙庚庚",
                "bounds": (120, 210, 680, 240),
                "bk": 0,
                "line": 0,
                "vertical": False,
                "cells": [("甲", 130, 212, 145, 235),
                          ("甲", 150, 212, 165, 235),
                          ("乙", 330, 212, 345, 235),
                          ("乙", 350, 212, 365, 235),
                          ("庚", 530, 212, 545, 235),
                          ("庚", 550, 212, 565, 235)],
                "polygon": None,
            },
            {
                "text": "标题",
                "bounds": (120, 160, 280, 190),
                "bk": 1,
                "line": 1,
                "vertical": False,
                "cells": [],
                "polygon": None,
            },
            {
                "text": "丙丙丁丁辛辛",
                "bounds": (120, 260, 680, 290),
                "bk": 2,
                "line": 2,
                "vertical": False,
                "cells": [("丙", 130, 262, 145, 285),
                          ("丙", 150, 262, 165, 285),
                          ("丁", 330, 262, 345, 285),
                          ("丁", 350, 262, 365, 285),
                          ("辛", 530, 262, 545, 285),
                          ("辛", 550, 262, 565, 285)],
                "polygon": None,
            },
            {
                "text": "戊戊己己壬壬",
                "bounds": (120, 310, 680, 340),
                "bk": 3,
                "line": 3,
                "vertical": False,
                "cells": [("戊", 130, 312, 145, 335),
                          ("戊", 150, 312, 165, 335),
                          ("己", 330, 312, 345, 335),
                          ("己", 350, 312, 365, 335),
                          ("壬", 530, 312, 545, 335),
                          ("壬", 550, 312, 565, 335)],
                "polygon": None,
            },
        ]
        vision = [
            {"c": character, "x0": x0, "y0": y0,
             "x1": x0 + 15, "y1": y0 + 23, "w": -1, "b": 0}
            for character, x0, y0 in (
                ("甲", 130, 212), ("甲", 150, 212),
                ("乙", 330, 212), ("乙", 350, 212),
                ("庚", 530, 212), ("庚", 550, 212),
                ("丙", 130, 262), ("丙", 150, 262),
                ("丁", 330, 262), ("丁", 350, 262),
                ("辛", 530, 262), ("辛", 550, 262),
                ("戊", 130, 312), ("戊", 150, 312),
                ("己", 330, 312), ("己", 350, 312),
                ("壬", 530, 312), ("壬", 550, 312),
            )
        ]

        split = _manga_table_cell_lines(table_lines, vision, grids)

        self.assertEqual(
            [line["text"] for line in split],
            [
                "标题", "甲甲", "乙乙", "庚庚",
                "丙丙", "丁丁", "辛辛",
                "戊戊", "己己", "壬壬",
            ],
        )
        self.assertTrue(all(
            line.get("vision_chars")
            for line in split[1:]
        ))

    def test_manga_regions_use_vision_text_and_symbol_boxes(self) -> None:
        manga_text = (
            "次の表は、日本で言うう代表的なエスニック料理をまとめたものだ。試験に"
        )
        vision_text = (
            "次の表は、日本で言う代表的なエスニック料理をまとめたものだ。試験に"
        )
        vision_chars = []
        for index, character in enumerate(vision_text):
            x0 = 350.0 + index * 37.0
            vision_chars.append({
                "c": character,
                "x0": x0,
                "y0": 100.0,
                "x1": x0 + 30.0,
                "y1": 140.0,
            })
        lines = [{
            "text": manga_text,
            "bounds": (300.0, 90.0, 1800.0, 150.0),
            "bk": 12,
            "line": 16,
            "vertical": False,
        }]
        fused = _manga_vision_line_chars(lines, vision_chars)[0]
        fused_text = "".join(item["c"] for item in fused)
        self.assertEqual(fused_text, vision_text)
        self.assertNotIn("言うう", fused_text)
        target = vision_text.index("まとめた")
        self.assertEqual(
            [item["x0"] for item in fused[target:target + 4]],
            [350.0 + index * 37.0 for index in range(target, target + 4)],
        )
        self.assertTrue(all(item["bk"] == 12 for item in fused))
        self.assertTrue(all(item["line"] == 16 for item in fused))
        self.assertTrue(all(item["vertical"] is False for item in fused))

    def test_manga_vision_line_rejects_low_agreement_and_large_omissions(self) -> None:
        line = {
            "text": "これは十分に長い漫画の一行です",
            "bounds": (0.0, 0.0, 500.0, 50.0),
            "bk": 1,
            "line": 2,
            "vertical": False,
        }
        unrelated = [
            {"c": c, "x0": i * 20, "y0": 5, "x1": i * 20 + 15, "y1": 45}
            for i, c in enumerate("無関係")
        ]
        self.assertEqual(_manga_vision_line_chars([line], unrelated), {})

    def test_manga_vision_line_rejects_high_agreement_partial_prefix(self) -> None:
        line = {
            "text": "ABCDEFGH",
            "bounds": (0.0, 0.0, 200.0, 20.0),
            "bk": 1,
            "line": 2,
            "vertical": False,
        }
        partial = [
            {"c": c, "x0": i * 20, "y0": 2, "x1": i * 20 + 15, "y1": 18}
            for i, c in enumerate("ABCDEF")
        ]
        self.assertEqual(_manga_vision_line_chars([line], partial), {})

    def test_manga_vision_symbol_belongs_to_only_one_adjacent_region(self) -> None:
        lines = [
            {"text": "共通", "bounds": (0, 0, 100, 24), "bk": 1, "line": 1,
             "vertical": False},
            {"text": "共通", "bounds": (0, 18, 100, 42), "bk": 2, "line": 2,
             "vertical": False},
        ]
        symbols = [
            {"c": "共", "x0": 10, "y0": 16, "x1": 30, "y1": 30},
            {"c": "通", "x0": 35, "y0": 16, "x1": 55, "y1": 30},
        ]
        fused = _manga_vision_line_chars(lines, symbols)
        self.assertEqual(sum(len(items) for items in fused.values()), 2)
        self.assertEqual(len(fused), 1)

    def test_manga_vision_slanted_polygon_prevents_aabb_cross_region(self) -> None:
        lines = [
            {
                "text": "共通",
                "bounds": (0, 0, 100, 40),
                "polygon": [(0, 0), (100, 30), (100, 40), (0, 10)],
                "bk": 1,
                "line": 1,
                "vertical": False,
            },
            {
                "text": "共通",
                "bounds": (0, 20, 100, 60),
                "polygon": [(0, 20), (100, 50), (100, 60), (0, 30)],
                "bk": 2,
                "line": 2,
                "vertical": False,
            },
        ]
        # Both symbols are in the lower polygon.  Their y centers are closer to
        # the upper AABB center, which used to assign them to the wrong region.
        symbols = [
            {"c": "共", "x0": 2, "y0": 20, "x1": 12, "y1": 27},
            {"c": "通", "x0": 14, "y0": 23, "x1": 24, "y1": 30},
        ]
        fused = _manga_vision_line_chars(lines, symbols)
        self.assertEqual(set(fused), {1})
        self.assertTrue(all(item["bk"] == 2 for item in fused[1]))

    def test_manga_vision_vertical_line_sorts_symbols_top_to_bottom(self) -> None:
        lines = [{
            "text": "縦書",
            "bounds": (10, 0, 40, 120),
            "bk": 3,
            "line": 4,
            "vertical": True,
        }]
        symbols = [
            {"c": "書", "x0": 12, "y0": 60, "x1": 38, "y1": 100},
            {"c": "縦", "x0": 12, "y0": 10, "x1": 38, "y1": 50},
        ]
        fused = _manga_vision_line_chars(lines, symbols)[0]
        self.assertEqual("".join(item["c"] for item in fused), "縦書")
        self.assertTrue(all(item["vertical"] is True for item in fused))

    def test_manga_alignment_handles_split_glyph_and_missing_quote(self) -> None:
        # Real p28 geometry from the reported regression.  Manga OCR omitted
        # the closing quote after る, while the two strokes of い arrived as two
        # visual runs.  Equal line division moved 美味しい about 23 page units
        # to the right; monotonic ink alignment must merge and skip in place.
        text = list(
            "そうだろ。調理師の役割は「美味しい料理をつくるだけじゃなく、栄養面"
        )
        intervals = [
            (284.626, 315.345), (323.265, 349.663),
            (359.743, 391.661), (396.941, 425.980),
            (432.219, 442.299), (461.978, 496.296),
            (496.296, 530.615), (535.895, 567.333),
            (573.093, 604.291), (610.051, 644.129),
            (644.129, 678.448), (685.647, 715.886),
            (727.645, 736.045), (741.325, 776.603),
            (776.603, 812.121), (821.241, 848.120),
            (855.079, 868.279), (875.718, 884.118),
            (890.118, 925.156), (925.156, 960.434),
            (966.674, 995.233), (1003.872, 1035.071),
            (1043.470, 1067.709), (1080.909, 1108.987),
            (1116.427, 1124.827),  # visual closing quote, absent from OCR
            (1138.746, 1170.904), (1176.904, 1206.423),
            (1217.942, 1246.021), (1255.620, 1280.579),
            (1290.659, 1321.137), (1331.217, 1356.176),
            (1368.895, 1371.055), (1394.334, 1426.492),
            (1431.772, 1465.130), (1470.410, 1502.809),
        ]
        aligned = _manga_align_visual_segments(
            text,
            [(start, end, 0.0, 100.0) for start, end in intervals],
        )
        self.assertEqual([item[0] for item in aligned], text)
        word = aligned[text.index("美"):text.index("美") + 4]
        self.assertAlmostEqual(word[0][1], 741.325, places=3)
        self.assertAlmostEqual(word[-1][2], 884.118, places=3)
        self.assertEqual((word[-1][1], word[-1][2]), (855.079, 884.118))
        after_quote = text.index("だ", text.index("る") + 1)
        self.assertEqual(
            (aligned[after_quote][1], aligned[after_quote][2]),
            (1138.746, 1170.904),
        )

    def test_manga_alignment_rejects_implausible_noise_geometry(self) -> None:
        self.assertEqual(
            _manga_align_visual_segments(
                list("美味"),
                [(0.0, 1.0, 0.0, 20.0), (2.0, 1002.0, 0.0, 20.0)],
            ),
            [],
        )

    def test_manga_optical_geometry_crops_detector_line_padding(self) -> None:
        import numpy as np

        image = np.full((120, 500), 255, dtype=np.uint8)
        image[30:80, 40:140] = 0
        image[30:80, 240:340] = 0
        # A narrow omitted visual mark spans almost the whole detector line.
        # It may be skipped by OCR/DP, but must not inflate both glyph boxes.
        image[5:115, 430:435] = 0
        boxes = _manga_line_char_boxes(
            "美味",
            [[0, 0], [500, 0], [500, 120], [0, 120]],
            vertical=False,
            image_gray=image,
        )
        self.assertEqual([item[0] for item in boxes], ["美", "味"])
        self.assertTrue(all(25 <= item[2] <= 30 for item in boxes))
        self.assertTrue(all(80 <= item[4] <= 85 for item in boxes))
        self.assertTrue(all(item[4] - item[2] < 70 for item in boxes))

    def test_manga_optical_geometry_keeps_vertical_line_direction(self) -> None:
        import numpy as np

        image = np.full((500, 120), 255, dtype=np.uint8)
        image[40:140, 30:80] = 0
        image[240:340, 30:80] = 0
        boxes = _manga_line_char_boxes(
            "縦書",
            [[0, 0], [120, 0], [120, 500], [0, 500]],
            vertical=True,
            image_gray=image,
        )
        self.assertEqual([item[0] for item in boxes], ["縦", "書"])
        self.assertTrue(all(25 <= item[1] <= 30 for item in boxes))
        self.assertTrue(all(80 <= item[3] <= 85 for item in boxes))
        self.assertLess(boxes[0][2], boxes[1][2])
        self.assertTrue(all(item[3] - item[1] < 70 for item in boxes))

    def test_manga_optical_geometry_inverse_maps_skewed_horizontal_line(self) -> None:
        import cv2
        import numpy as np

        rectified = np.full((80, 300), 255, dtype=np.uint8)
        rectified[15:65, 20:100] = 0
        rectified[15:65, 200:280] = 0
        polygon = np.asarray(
            [[40, 40], [350, 80], [330, 170], [20, 130]],
            dtype=np.float32,
        )
        transform = cv2.getPerspectiveTransform(
            np.asarray([[0, 0], [300, 0], [300, 80], [0, 80]], dtype=np.float32),
            polygon,
        )
        page = cv2.warpPerspective(
            rectified,
            transform,
            (420, 220),
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=255,
        )
        boxes = _manga_line_char_boxes(
            "美味",
            polygon.tolist(),
            vertical=False,
            image_gray=page,
        )
        expected = cv2.perspectiveTransform(
            np.asarray([[[60, 40], [240, 40]]], dtype=np.float32),
            transform,
        )[0]
        centers = [
            ((item[1] + item[3]) / 2.0, (item[2] + item[4]) / 2.0)
            for item in boxes
        ]
        self.assertEqual([item[0] for item in boxes], ["美", "味"])
        for actual, target in zip(centers, expected):
            self.assertAlmostEqual(actual[0], float(target[0]), delta=1.0)
            self.assertAlmostEqual(actual[1], float(target[1]), delta=1.0)

    def test_manga_optical_geometry_inverse_maps_rotated_vertical_line(self) -> None:
        import cv2
        import numpy as np

        rectified = np.full((300, 80), 255, dtype=np.uint8)
        rectified[20:100, 15:65] = 0
        rectified[200:280, 15:65] = 0
        polygon = np.asarray(
            [[250, 20], [340, 50], [250, 360], [160, 330]],
            dtype=np.float32,
        )
        transform = cv2.getPerspectiveTransform(
            np.asarray([[0, 0], [80, 0], [80, 300], [0, 300]], dtype=np.float32),
            polygon,
        )
        page = cv2.warpPerspective(
            rectified,
            transform,
            (420, 400),
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=255,
        )
        boxes = _manga_line_char_boxes(
            "縦書",
            polygon.tolist(),
            vertical=True,
            image_gray=page,
        )
        expected = cv2.perspectiveTransform(
            np.asarray([[[40, 60], [40, 240]]], dtype=np.float32),
            transform,
        )[0]
        centers = [
            ((item[1] + item[3]) / 2.0, (item[2] + item[4]) / 2.0)
            for item in boxes
        ]
        self.assertEqual([item[0] for item in boxes], ["縦", "書"])
        for actual, target in zip(centers, expected):
            self.assertAlmostEqual(actual[0], float(target[0]), delta=1.0)
            self.assertAlmostEqual(actual[1], float(target[1]), delta=1.0)

    def test_pi_page_cache_rejects_previous_geometry_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            page_path = Path(temp_dir) / "p000001.json"
            page = {
                "schema": reader_book_ocr_worker.PAGE_SCHEMA,
                "bookId": "book_test",
                "contentSha256": "a" * 64,
                "engine": "manga",
                "processingProfile": "pi-default-v2",
                "chars": [],
            }
            page_path.write_text(json.dumps(page), "utf-8")
            self.assertFalse(reader_book_ocr_worker._page_done(
                page_path,
                "book_test",
                "a" * 64,
                "manga",
                "pi-default-v5",
            ))
            page["processingProfile"] = "pi-default-v5"
            page_path.write_text(json.dumps(page), "utf-8")
            self.assertTrue(reader_book_ocr_worker._page_done(
                page_path,
                "book_test",
                "a" * 64,
                "manga",
                "pi-default-v5",
            ))

    def test_manga_page_preserves_authoritative_vertical_direction(self) -> None:
        class FakePixmap:
            width = 100
            height = 100

            def save(self, path) -> None:
                Path(path).write_bytes(b"png")

        page = SimpleNamespace(
            rect=SimpleNamespace(width=100, height=100),
            get_pixmap=lambda **_kwargs: FakePixmap(),
        )
        engine = lambda _path: {"blocks": [{
            "vertical": True,
            "lines": ["縦書"],
            "lines_coords": [[[10, 10], [30, 10], [30, 70], [10, 70]]],
        }]}
        chars, _text, _image_w, _image_h = _manga_page(page, engine)
        self.assertEqual([char.get("vertical") for char in chars], [True, True])
        _chars, _text, _image_w, _image_h, layout = _manga_page(
            page, engine, include_layout=True
        )
        self.assertEqual(layout["textSource"], "unavailable")
        self.assertEqual(layout["mode"], "fallback")
        self.assertEqual(layout["regions"], [])

    def test_manga_page_with_vision_preserves_source_chars_and_emits_layout(self) -> None:
        class FakePixmap:
            width = 100
            height = 100
            n = 0
            samples = b""

            def save(self, path) -> None:
                Path(path).write_bytes(b"png")

        page = SimpleNamespace(
            rect=SimpleNamespace(width=100, height=100),
            get_pixmap=lambda **_kwargs: FakePixmap(),
        )
        engine = lambda _path: {"blocks": [{
            "vertical": True,
            "lines": ["漫画誤字"],
            "lines_coords": [[[60, 5], [95, 5], [95, 80], [60, 80]]],
        }]}
        vision = [
            {"c": "正", "x0": 70, "y0": 10, "x1": 85, "y1": 30, "w": 4, "bk": 9, "b": 0},
            {"c": " ", "sp": 1, "x0": 70, "y0": 10, "x1": 85, "y1": 30, "w": 4, "bk": 9, "b": 0},
            {"c": "文", "x0": 70, "y0": 40, "x1": 85, "y1": 60, "w": 5, "bk": 9, "b": 0},
        ]
        original = [dict(item) for item in vision]
        chars, _text, _image_w, _image_h, layout = _manga_page(
            page,
            engine,
            vision_chars=vision,
            include_layout=True,
        )
        self.assertEqual(chars, original)
        self.assertEqual([item["c"] for item in chars], ["正", " ", "文"])
        self.assertEqual(layout["textSource"], "vision")
        covered = sorted(
            index
            for region in layout["regions"]
            for start, end in region["ranges"]
            for index in range(start, end + 1)
        )
        self.assertEqual(covered, [0, 1, 2])

    def test_manga_line_geometry_follows_vertical_japanese_writing(self) -> None:
        boxes = _manga_line_char_boxes(
            "取り寄せ",
            [[100, 20], [120, 20], [120, 180], [100, 180]],
            vertical=True,
        )
        self.assertEqual([item[0] for item in boxes], list("取り寄せ"))
        self.assertTrue(all(item[1] == 100 and item[3] == 120 for item in boxes))
        self.assertEqual([item[2] for item in boxes], [20, 60, 100, 140])
        self.assertEqual([item[4] for item in boxes], [60, 100, 140, 180])

    def test_manga_line_geometry_keeps_horizontal_text_on_x_axis(self) -> None:
        boxes = _manga_line_char_boxes(
            "HTTP",
            [[10, 30], [210, 30], [210, 50], [10, 50]],
            vertical=False,
        )
        self.assertEqual([item[1] for item in boxes], [10, 60, 110, 160])
        self.assertTrue(all(item[2] == 30 and item[4] == 50 for item in boxes))

    def test_manga_line_geometry_uses_authoritative_direction_for_square_box(self) -> None:
        boxes = _manga_line_char_boxes(
            "縦書",
            [[10, 10], [50, 10], [50, 50], [10, 50]],
            vertical=True,
        )
        self.assertEqual([(item[2], item[4]) for item in boxes], [(10, 30), (30, 50)])
        self.assertTrue(all(item[1] == 10 and item[3] == 50 for item in boxes))

    def test_manga_line_geometry_follows_skewed_polygon(self) -> None:
        boxes = _manga_line_char_boxes(
            "AB",
            [[10, 10], [50, 20], [46, 40], [6, 30]],
            vertical=False,
        )
        self.assertEqual(boxes[0][1:], ("A", 6.0, 10.0, 30.0, 35.0)[1:])
        self.assertEqual(boxes[1][1:], (26.0, 15.0, 50.0, 40.0))

    def test_manga_line_geometry_never_inverts_dense_character_boxes(self) -> None:
        boxes = _manga_line_char_boxes(
            "1234567890",
            [[0, 0], [2, 0], [2, 1], [0, 1]],
            vertical=False,
        )
        self.assertEqual(len(boxes), 10)
        self.assertTrue(all(item[1] < item[3] and item[2] < item[4] for item in boxes))

    def test_non_japanese_tokenization_uses_real_boundaries(self) -> None:
        chars = [
            {"c": "A", "w": -1, "bk": 0, "line": 0},
            {"c": "B", "w": -1, "bk": 0, "line": 0},
            {"c": "中", "w": -1, "bk": 0, "line": 0},
            {"c": "文", "w": -1, "bk": 0, "line": 0},
        ]
        out = _tokenize_chars(chars)
        self.assertEqual(out[0]["w"], out[1]["w"])
        self.assertNotEqual(out[1]["w"], out[2]["w"])
        self.assertNotEqual(out[2]["w"], out[3]["w"])

    def test_published_manifest_lists_only_derived_immutable_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            version = Path(temp) / ("a" * 64)
            job_dir = version / "vision"
            pages = job_dir / "pages"
            pages.mkdir(parents=True)
            (pages / "p000001.json").write_text("{}", "utf-8")
            formula = Path(temp) / "global-formulas.json"
            formula.write_text(json.dumps({"formulas": [{
                "page": 1,
                "bbox": [0, 0, 1, 1],
                "latex": "x",
                "multiline": True,
            }]}), "utf-8")
            args = SimpleNamespace(
                book_id="book_" + "a" * 32,
                content_sha256="b" * 64,
            )
            revision, manifest = _publish_attachments(args, job_dir, formula)
            self.assertTrue(revision.startswith("ocr_"))
            self.assertEqual(manifest["category"], "derived")
            self.assertEqual(manifest["mergePolicy"], "immutable")
            self.assertEqual({item["category"] for item in manifest["files"]}, {"derived"})
            self.assertTrue(all("revision=" + revision in item["downloadUrl"] for item in manifest["files"]))
            self.assertNotIn(temp, json.dumps(manifest))
            exported = json.loads((version / "formulas.json").read_text("utf-8"))
            self.assertTrue(exported["formulas"][0]["multiline"])

    def test_revision_addresses_provenance_and_formula_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            job_dir = base / "job"
            pages = job_dir / "pages"
            pages.mkdir(parents=True)
            (pages / "p000001.json").write_text("{}", "utf-8")
            args = SimpleNamespace(
                book_id="book_" + "a" * 32,
                content_sha256="b" * 64,
            )
            common = {
                "adoptionContract": "reader-library-ocr-adoption/1",
                "source": "legacy-sidecars",
                "formulaState": "pending",
                "formulaCount": 0,
                "engine": "legacy",
                "totalPages": 1,
            }
            first_revision, _ = _publish_attachments(
                args,
                job_dir,
                formula_records=[],
                manifest_metadata={
                    **common,
                    "pageSources": {"embedded": 1},
                    "formulaReason": "legacy-formulas-missing",
                },
                publish_manifest=False,
                output_dir=base / "first",
                generated_at_epoch_ms=0,
            )
            second_revision, _ = _publish_attachments(
                args,
                job_dir,
                formula_records=[],
                manifest_metadata={
                    **common,
                    "pageSources": {"override": 1},
                    "formulaReason": "legacy-formulas-unbound",
                },
                publish_manifest=False,
                output_dir=base / "second",
                generated_at_epoch_ms=0,
            )
            self.assertNotEqual(first_revision, second_revision)

    def test_stale_worker_generation_cannot_update_the_replacement_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            job_path = Path(temp) / "job.json"
            job_path.write_text(json.dumps({
                "jobId": "ocrjob_new",
                "workerGeneration": "ocrgen_new",
                "state": "queued",
            }), "utf-8")
            reader_book_ocr_worker._set_worker_identity(
                "ocrjob_old", "ocrgen_old"
            )
            try:
                with self.assertRaisesRegex(RuntimeError, "no longer current"):
                    reader_book_ocr_worker._update_job(job_path, state="running")
            finally:
                reader_book_ocr_worker._set_worker_identity(None, None)
            self.assertEqual(
                json.loads(job_path.read_text("utf-8"))["state"], "queued"
            )

    def test_controlled_child_termination_does_not_treat_child_pid_as_pgid(self) -> None:
        class Child:
            pid = 987654

            def __init__(self):
                self.terminated = False
                self.waited = False

            def poll(self):
                return None

            def terminate(self):
                self.terminated = True

            def wait(self, timeout):
                self.waited = timeout == 8
                return 0

            def kill(self):
                raise AssertionError("graceful termination should have succeeded")

        child = Child()
        reader_book_ocr_worker._terminate(child)
        self.assertTrue(child.terminated)
        self.assertTrue(child.waited)

    def test_release_rejects_wrong_page_formula_range_and_changed_source_before_fence(self) -> None:
        for case in ("wrong-engine", "formula-out-of-range", "source-changed"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                base = Path(temp)
                source = base / "source.pdf"
                source.write_bytes(PDF_A)
                content_sha = hashlib.sha256(PDF_A).hexdigest()
                job_dir = base / "state" / "vision"
                pages = job_dir / "pages"
                pages.mkdir(parents=True)
                page_engine = "manga" if case == "wrong-engine" else "vision"
                (pages / "p000001.json").write_text(json.dumps({
                    "schema": "reader-page-chars/1",
                    "bookId": "book_" + "a" * 32,
                    "contentSha256": content_sha,
                    "engine": page_engine,
                    "pageNumber": 1,
                    "page_w": 10,
                    "page_h": 20,
                    "chars": [],
                    "furigana": [],
                }), "utf-8")
                formula = job_dir / "formula.json"
                formula_records = (
                    [{"page": 2, "bbox": [0, 0, 1, 1], "latex": "x"}]
                    if case == "formula-out-of-range" else []
                )
                formula.write_text(json.dumps({"formulas": formula_records}), "utf-8")
                final_job = {
                    "contract": "reader-library-ocr/1",
                    "jobId": "ocrjob_test",
                    "bookId": "book_" + "a" * 32,
                    "contentSha256": content_sha,
                    "engine": "vision",
                    "state": "succeeded",
                    "totalPages": 1,
                    "successfulPages": 1,
                    "formulaState": "succeeded",
                    "formulaTotal": len(formula_records),
                    "resultAvailable": True,
                }
                args = SimpleNamespace(
                    book_id="book_" + "a" * 32,
                    content_sha256=content_sha,
                    engine="vision",
                    max_bytes=1024 * 1024,
                )
                if case == "source-changed":
                    source.write_bytes(PDF_A + b"changed")
                with self.assertRaises(RuntimeError):
                    _publish_release(
                        args,
                        job_dir,
                        formula,
                        final_job,
                        source_path=source,
                    )
                self.assertFalse((job_dir.parent / "publication.json").exists())

    def test_worker_source_replaced_during_index_commit_is_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source.pdf"
            source.write_bytes(PDF_A)
            content_sha = hashlib.sha256(PDF_A).hexdigest()
            job_dir = base / "state" / "vision"
            pages = job_dir / "pages"
            pages.mkdir(parents=True)
            book_id = "book_" + "a" * 32
            (pages / "p000001.json").write_text(json.dumps({
                "schema": "reader-page-chars/1",
                "bookId": book_id,
                "contentSha256": content_sha,
                "engine": "vision",
                "pageNumber": 1,
                "page_w": 10,
                "page_h": 20,
                "chars": [],
                "furigana": [],
            }), "utf-8")
            formula = job_dir / "formula.json"
            formula.write_text('{"formulas":[]}', "utf-8")
            final_job = {
                "contract": "reader-library-ocr/1",
                "jobId": "ocrjob_test",
                "bookId": book_id,
                "contentSha256": content_sha,
                "engine": "vision",
                "state": "succeeded",
                "totalPages": 1,
                "successfulPages": 1,
                "formulaState": "succeeded",
                "formulaTotal": 0,
                "resultAvailable": True,
            }
            (job_dir / "job.json").write_text(json.dumps(final_job), "utf-8")
            args = SimpleNamespace(
                book_id=book_id,
                content_sha256=content_sha,
                engine="vision",
                max_bytes=1024 * 1024,
            )
            fence_path = job_dir.parent / "publication.json"
            index_path = job_dir.parent / "releases-index.json"
            real_atomic = reader_book_ocr_worker._atomic_json
            swapped = False

            def replace_source_at_index(path, value):
                nonlocal swapped
                if Path(path) == index_path and not swapped:
                    swapped = True
                    replacement = base / "replacement.pdf"
                    replacement.write_bytes(PDF_A + b"different")
                    if os.name == "nt":
                        source.write_bytes(replacement.read_bytes())
                        replacement.unlink()
                    else:
                        os.replace(replacement, source)
                return real_atomic(path, value)

            with patch.object(
                reader_book_ocr_worker,
                "_atomic_json",
                side_effect=replace_source_at_index,
            ):
                with self.assertRaisesRegex(RuntimeError, "changed before"):
                    _publish_release(
                        args,
                        job_dir,
                        formula,
                        final_job,
                        source_path=source,
                    )
            self.assertTrue(swapped)
            self.assertFalse(fence_path.exists())
            self.assertFalse(index_path.exists())

    def test_worker_full_rehash_detects_same_metadata_change_before_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source.pdf"
            source.write_bytes(PDF_A)
            original_stat = source.stat()
            original_identity = reader_book_ocr_worker._source_identity(original_stat)
            changed_bytes = bytearray(PDF_A)
            changed_bytes[-1] ^= 1
            content_sha = hashlib.sha256(PDF_A).hexdigest()
            job_dir = base / "state" / "vision"
            pages = job_dir / "pages"
            pages.mkdir(parents=True)
            book_id = "book_" + "a" * 32
            (pages / "p000001.json").write_text(json.dumps({
                "schema": "reader-page-chars/1",
                "bookId": book_id,
                "contentSha256": content_sha,
                "engine": "vision",
                "pageNumber": 1,
                "page_w": 10,
                "page_h": 20,
                "chars": [],
                "furigana": [],
            }), "utf-8")
            formula = job_dir / "formula.json"
            formula.write_text('{"formulas":[]}', "utf-8")
            final_job = {
                "contract": "reader-library-ocr/1",
                "jobId": "ocrjob_test",
                "workerGeneration": "ocrgen_test",
                "bookId": book_id,
                "contentSha256": content_sha,
                "engine": "vision",
                "state": "succeeded",
                "totalPages": 1,
                "successfulPages": 1,
                "formulaState": "succeeded",
                "formulaTotal": 0,
                "resultAvailable": True,
            }
            (job_dir / "job.json").write_text(json.dumps(final_job), "utf-8")
            args = SimpleNamespace(
                book_id=book_id,
                content_sha256=content_sha,
                engine="vision",
                max_bytes=1024 * 1024,
            )
            fence_path = job_dir.parent / "publication.json"
            index_path = job_dir.parent / "releases-index.json"
            real_assert = reader_book_ocr_worker._assert_source_guard
            swapped = False

            def change_source_before_full_hash(guard, *, rehash):
                nonlocal swapped
                if rehash and not swapped:
                    swapped = True
                    source.write_bytes(bytes(changed_bytes))
                    os.utime(
                        source,
                        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
                    )
                    self.assertEqual(
                        reader_book_ocr_worker._source_identity(source.stat()),
                        original_identity,
                    )
                return real_assert(guard, rehash=rehash)

            reader_book_ocr_worker._set_worker_identity(
                final_job["jobId"], final_job["workerGeneration"]
            )
            try:
                with patch.object(
                    reader_book_ocr_worker,
                    "_assert_source_guard",
                    side_effect=change_source_before_full_hash,
                ):
                    with self.assertRaisesRegex(RuntimeError, "changed before"):
                        _publish_release(
                            args,
                            job_dir,
                            formula,
                            final_job,
                            source_path=source,
                        )
            finally:
                reader_book_ocr_worker._set_worker_identity(None, None)
            self.assertTrue(swapped)
            self.assertNotEqual(hashlib.sha256(source.read_bytes()).hexdigest(), content_sha)
            self.assertFalse(fence_path.exists())
            self.assertFalse(index_path.exists())

    def test_stale_worker_generation_at_index_commit_restores_previous_truth(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source.pdf"
            source.write_bytes(PDF_A)
            content_sha = hashlib.sha256(PDF_A).hexdigest()
            book_id = "book_" + "a" * 32
            job_dir = base / "state" / "vision"
            pages = job_dir / "pages"
            pages.mkdir(parents=True)

            def write_page(text: str) -> None:
                (pages / "p000001.json").write_text(json.dumps({
                    "schema": "reader-page-chars/1",
                    "bookId": book_id,
                    "contentSha256": content_sha,
                    "engine": "vision",
                    "pageNumber": 1,
                    "page_w": 10,
                    "page_h": 20,
                    "chars": [{"c": text}],
                    "furigana": [],
                }), "utf-8")

            formula = job_dir / "formula.json"
            formula.write_text('{"formulas":[]}', "utf-8")
            args = SimpleNamespace(
                book_id=book_id,
                content_sha256=content_sha,
                engine="vision",
                max_bytes=1024 * 1024,
            )
            first_job = {
                "jobId": "ocrjob_first",
                "workerGeneration": "ocrgen_first",
                "bookId": book_id,
                "contentSha256": content_sha,
                "engine": "vision",
                "state": "succeeded",
                "totalPages": 1,
                "successfulPages": 1,
                "formulaState": "succeeded",
                "formulaTotal": 0,
                "resultAvailable": True,
            }
            write_page("A")
            (job_dir / "job.json").write_text(json.dumps(first_job), "utf-8")
            reader_book_ocr_worker._set_worker_identity(
                first_job["jobId"], first_job["workerGeneration"]
            )
            try:
                _publish_release(
                    args, job_dir, formula, first_job, source_path=source
                )
            finally:
                reader_book_ocr_worker._set_worker_identity(None, None)
            fence_path = job_dir.parent / "publication.json"
            index_path = job_dir.parent / "releases-index.json"
            previous_fence = fence_path.read_bytes()
            previous_index = index_path.read_bytes()

            stale_job = {
                **first_job,
                "jobId": "ocrjob_stale",
                "workerGeneration": "ocrgen_stale",
            }
            replacement_job = {
                **first_job,
                "jobId": "ocrjob_replacement",
                "workerGeneration": "ocrgen_replacement",
                "state": "queued",
                "resultAvailable": False,
            }
            write_page("B")
            (job_dir / "job.json").write_text(json.dumps(stale_job), "utf-8")
            real_atomic = reader_book_ocr_worker._atomic_json
            swapped = False

            def replace_generation_at_index(path, value):
                nonlocal swapped
                if Path(path) == index_path and not swapped:
                    swapped = True
                    real_atomic(job_dir / "job.json", replacement_job)
                return real_atomic(path, value)

            reader_book_ocr_worker._set_worker_identity(
                stale_job["jobId"], stale_job["workerGeneration"]
            )
            try:
                with patch.object(
                    reader_book_ocr_worker,
                    "_atomic_json",
                    side_effect=replace_generation_at_index,
                ):
                    with self.assertRaisesRegex(RuntimeError, "no longer current"):
                        _publish_release(
                            args,
                            job_dir,
                            formula,
                            stale_job,
                            source_path=source,
                        )
            finally:
                reader_book_ocr_worker._set_worker_identity(None, None)
            self.assertTrue(swapped)
            self.assertEqual(fence_path.read_bytes(), previous_fence)
            self.assertEqual(index_path.read_bytes(), previous_index)
            self.assertEqual(
                json.loads((job_dir / "job.json").read_text("utf-8"))["jobId"],
                replacement_job["jobId"],
            )

    def test_index_failure_rolls_back_but_mirror_failure_keeps_new_truth(self) -> None:
        for failed_name in (
            "releases-index.json",
            "publication.json",
            "result.json",
            "current.json",
        ):
            with self.subTest(failed_name=failed_name), tempfile.TemporaryDirectory() as temp:
                base = Path(temp)
                book_id = "book_" + "a" * 32
                source = base / "source.pdf"
                source.write_bytes(PDF_A)
                content_sha = hashlib.sha256(PDF_A).hexdigest()

                def publish(engine: str, text: str, job_id: str) -> str:
                    job_dir = base / engine
                    pages = job_dir / "pages"
                    pages.mkdir(parents=True, exist_ok=True)
                    (pages / "p000001.json").write_text(json.dumps({
                        "schema": "reader-page-chars/1",
                        "bookId": book_id,
                        "contentSha256": content_sha,
                        "engine": engine,
                        "pageNumber": 1,
                        "page_w": 10,
                        "page_h": 20,
                        "chars": [{"c": text}],
                        "furigana": [],
                    }), "utf-8")
                    formula = job_dir / "formula.json"
                    formula.write_text('{"formulas":[]}', "utf-8")
                    return _publish_release(
                        SimpleNamespace(
                            book_id=book_id,
                            content_sha256=content_sha,
                            engine=engine,
                            max_bytes=1024 * 1024,
                        ),
                        job_dir,
                        formula,
                        {
                            "jobId": job_id,
                            "bookId": book_id,
                            "contentSha256": content_sha,
                            "engine": engine,
                            "state": "succeeded",
                            "totalPages": 1,
                            "successfulPages": 1,
                            "formulaState": "succeeded",
                            "formulaTotal": 0,
                            "resultAvailable": True,
                        },
                        source_path=source,
                    )

                first_revision = publish("vision", "A", "ocrjob_a")
                fence_path = base / "publication.json"
                index_path = base / "releases-index.json"
                first_fence = fence_path.read_bytes()
                first_index = index_path.read_bytes()
                failed_path = base / failed_name
                real_atomic = reader_book_ocr_worker._atomic_json

                def fail_commit_file(path, value):
                    if Path(path) == failed_path:
                        raise OSError("fault before commit-file replace")
                    return real_atomic(path, value)

                with patch.object(
                    reader_book_ocr_worker,
                    "_atomic_json",
                    side_effect=fail_commit_file,
                ):
                    if failed_name == "releases-index.json":
                        with self.assertRaises(OSError):
                            publish("manga", "B", "ocrjob_b")
                        second_revision = None
                    else:
                        second_revision = publish("manga", "B", "ocrjob_b")
                if failed_name == "releases-index.json":
                    self.assertEqual(fence_path.read_bytes(), first_fence)
                    self.assertEqual(index_path.read_bytes(), first_index)
                    self.assertEqual(json.loads(first_fence)["revision"], first_revision)
                    continue

                committed_index = json.loads(index_path.read_text("utf-8"))
                active = next(
                    run
                    for run in committed_index["runs"]
                    if run["runId"] == committed_index["activeRunId"]
                )
                self.assertEqual(active["revision"], second_revision)
                if failed_name == "publication.json":
                    self.assertEqual(fence_path.read_bytes(), first_fence)

    def test_same_revision_new_inode_reuses_release_and_cleans_staging_after_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source_a = base / "source-a.pdf"
            source_b = base / "source-b.pdf"
            source_a.write_bytes(PDF_A)
            source_b.write_bytes(PDF_A)
            content_sha = hashlib.sha256(PDF_A).hexdigest()
            book_id = "book_" + "a" * 32
            job_dir = base / "vision"
            pages = job_dir / "pages"
            pages.mkdir(parents=True)
            (pages / "p000001.json").write_text(json.dumps({
                "schema": "reader-page-chars/1",
                "bookId": book_id,
                "contentSha256": content_sha,
                "engine": "vision",
                "pageNumber": 1,
                "page_w": 10,
                "page_h": 20,
                "chars": [],
                "furigana": [],
            }), "utf-8")
            formula = job_dir / "formula.json"
            formula.write_text('{"formulas":[]}', "utf-8")
            args = SimpleNamespace(
                book_id=book_id,
                content_sha256=content_sha,
                engine="vision",
                max_bytes=1024 * 1024,
            )

            def job(run_id: str, job_id: str) -> dict:
                return {
                    "jobId": job_id,
                    "runId": run_id,
                    "bookId": book_id,
                    "contentSha256": content_sha,
                    "engine": "vision",
                    "state": "succeeded",
                    "totalPages": 1,
                    "successfulPages": 1,
                    "formulaState": "succeeded",
                    "formulaTotal": 0,
                    "resultAvailable": True,
                }

            first = job("ocrrun_00000000000000a1", "ocrjob_first_inode")
            (job_dir / "job.json").write_text(json.dumps(first), "utf-8")
            first_revision = _publish_release(
                args, job_dir, formula, first, source_path=source_a
            )
            first_result = json.loads(
                (base / "releases" / first_revision / "result.json").read_text("utf-8")
            )

            second = job("ocrrun_00000000000000a2", "ocrjob_second_inode")
            (job_dir / "job.json").write_text(json.dumps(second), "utf-8")
            lock_held = False
            real_rmtree = reader_book_ocr_worker.shutil.rmtree

            @contextmanager
            def tracked_lock(_path):
                nonlocal lock_held
                lock_held = True
                try:
                    yield
                finally:
                    lock_held = False

            def checked_rmtree(path, *args, **kwargs):
                if Path(path).name.startswith(".release-staging-"):
                    self.assertFalse(lock_held)
                return real_rmtree(path, *args, **kwargs)

            import reader_sidecar_store

            with patch.object(
                reader_sidecar_store, "exclusive_lock", side_effect=tracked_lock
            ), patch.object(
                reader_book_ocr_worker.shutil,
                "rmtree",
                side_effect=checked_rmtree,
            ):
                second_revision = _publish_release(
                    args, job_dir, formula, second, source_path=source_b
                )
            self.assertEqual(second_revision, first_revision)
            index = json.loads((base / "releases-index.json").read_text("utf-8"))
            self.assertEqual(index["activeRunId"], second["runId"])
            self.assertEqual(
                {item["jobId"] for item in index["runs"]},
                {first["jobId"], second["jobId"]},
            )
            fence = json.loads((base / "publication.json").read_text("utf-8"))
            self.assertEqual(fence["sourceIdentity"], first_result["sourceIdentity"])

    def test_post_publication_replacement_generation_blocks_old_terminal_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source.pdf"
            source.write_bytes(PDF_A)
            content_sha = hashlib.sha256(PDF_A).hexdigest()
            book_id = "book_" + "a" * 32
            job_dir = base / "state" / "vision"
            pages = job_dir / "pages"
            pages.mkdir(parents=True)
            (pages / "p000001.json").write_text(json.dumps({
                "schema": "reader-page-chars/1",
                "bookId": book_id,
                "contentSha256": content_sha,
                "engine": "vision",
                "pageNumber": 1,
                "page_w": 10,
                "page_h": 20,
                "chars": [],
                "furigana": [],
            }), "utf-8")
            formula = job_dir / "formula.json"
            formula.write_text('{"formulas":[]}', "utf-8")
            args = SimpleNamespace(
                book_id=book_id,
                content_sha256=content_sha,
                engine="vision",
                max_bytes=1024 * 1024,
            )
            old_job = {
                "jobId": "ocrjob_old",
                "workerGeneration": "ocrgen_old",
                "runId": "ocrrun_00000000000000f1",
                "bookId": book_id,
                "contentSha256": content_sha,
                "engine": "vision",
                "state": "running",
                "totalPages": 1,
                "successfulPages": 1,
                "formulaState": "succeeded",
                "formulaTotal": 0,
                "resultAvailable": False,
            }
            job_path = job_dir / "job.json"
            job_path.write_text(json.dumps(old_job), "utf-8")
            terminal = {
                **old_job,
                "state": "succeeded",
                "resultAvailable": True,
                "updatedAtEpochMs": 100,
            }
            reader_book_ocr_worker._set_worker_identity(
                old_job["jobId"], old_job["workerGeneration"]
            )
            try:
                # Publication has returned successfully.  The generation is
                # replaced in the exact window before run() writes terminal
                # success (or its outer except tries to write failure).
                _publish_release(
                    args, job_dir, formula, terminal, source_path=source
                )
                replacement = {
                    **old_job,
                    "jobId": "ocrjob_replacement",
                    "workerGeneration": "ocrgen_replacement",
                    "runId": "ocrrun_00000000000000f2",
                    "state": "queued",
                }
                job_path.write_text(json.dumps(replacement), "utf-8")
                replacement_bytes = job_path.read_bytes()

                with self.assertRaisesRegex(RuntimeError, "no longer current"):
                    reader_book_ocr_worker._write_terminal_job(
                        args, job_dir, terminal
                    )
                self.assertEqual(job_path.read_bytes(), replacement_bytes)
                self.assertFalse(
                    reader_book_ocr_worker._record_worker_failure(
                        args,
                        job_dir,
                        RuntimeError("late failure"),
                        source,
                        base,
                    )
                )
                self.assertEqual(job_path.read_bytes(), replacement_bytes)
            finally:
                reader_book_ocr_worker._set_worker_identity(None, None)


if __name__ == "__main__":
    unittest.main()
