from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "deploy_reader.sh"
SCRIPT = SCRIPT_PATH.read_text("utf-8")


def _function_source(name: str) -> str:
    match = re.search(
        rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n",
        SCRIPT,
        flags=re.MULTILINE | re.DOTALL,
    )
    if not match:
        raise AssertionError(f"missing shell function: {name}")
    return match.group(0)


_STATE_FUNCTIONS = "\n".join(
    _function_source(name)
    for name in (
        "unit_active_state",
        "wait_unit_still",
        "wait_unit_active",
        "confirm_units_still",
        "stop_units_and_confirm",
        "freeze_writers",
    )
)


class _FakeSystemd:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.states = self.root / "states"
        self.bin.mkdir()
        self.states.mkdir()
        self.log = self.root / "systemctl.log"
        (self.bin / "sudo").write_text(
            "#!/bin/sh\nexec \"$@\"\n",
            encoding="utf-8",
        )
        (self.bin / "sleep").write_text(
            "#!/bin/sh\nexit 0\n",
            encoding="utf-8",
        )
        (self.bin / "systemctl").write_text(
            textwrap.dedent(
                """\
                #!/bin/sh
                set -eu
                printf '%s\\n' "$*" >> "$FAKE_SYSTEMCTL_LOG"
                command_name="${1:-}"
                shift || true
                case "$command_name" in
                  show)
                    property=""
                    unit=""
                    for value in "$@"; do
                      case "$value" in
                        --property=*) property="${value#--property=}" ;;
                        --value) ;;
                        *) unit="$value" ;;
                      esac
                    done
                    file="$FAKE_SYSTEMD_STATE/$unit.$property"
                    [ -f "$file" ] || exit 4
                    first="$(sed -n '1p' "$file")"
                    lines="$(wc -l < "$file")"
                    if [ "$lines" -gt 1 ]; then
                      sed '1d' "$file" > "$file.next"
                      mv "$file.next" "$file"
                    fi
                    printf '%s\\n' "$first"
                    ;;
                  stop)
                    for unit in "$@"; do
                      [ -f "$FAKE_SYSTEMD_STATE/$unit.sticky" ] && continue
                      printf 'inactive\\n' \
                        > "$FAKE_SYSTEMD_STATE/$unit.ActiveState"
                    done
                    ;;
                  start)
                    for unit in "$@"; do
                      printf 'active\\n' \
                        > "$FAKE_SYSTEMD_STATE/$unit.ActiveState"
                    done
                    ;;
                  daemon-reload) ;;
                  *) exit 5 ;;
                esac
                """
            ),
            encoding="utf-8",
        )
        for path in self.bin.iterdir():
            path.chmod(0o755)

    def close(self):
        self.tmp.cleanup()

    def state(self, unit: str, value: str, *, sticky: bool = False):
        (self.states / f"{unit}.ActiveState").write_text(
            value + "\n",
            encoding="utf-8",
        )
        if sticky:
            (self.states / f"{unit}.sticky").touch()

    def property_sequence(self, unit: str, prop: str, values: list[str]):
        (self.states / f"{unit}.{prop}").write_text(
            "\n".join(values) + "\n",
            encoding="utf-8",
        )

    def run(self, body: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{self.bin}:{env['PATH']}",
                "FAKE_SYSTEMD_STATE": str(self.states),
                "FAKE_SYSTEMCTL_LOG": str(self.log),
            }
        )
        return subprocess.run(
            ["bash", "-c", body],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )


def _state_harness(body: str) -> str:
    return (
        "set -u\n"
        "WRITER_WAIT_SECONDS=1\n"
        "WRITER_TIMERS=(quick.timer daily.timer concept.timer)\n"
        "WRITER_SERVICES=(quick.service daily.service concept.service)\n"
        "MANAGED_SERVICES=(webapp.service voice.service)\n"
        "WRITERS_FROZEN=0\n"
        + _STATE_FUNCTIONS
        + "\n"
        + body
    )


class ReaderDeployTransactionContractTest(unittest.TestCase):
    def test_preflight_builds_reader_only_in_temporary_stage(self):
        self.assertIn("--preflight-only", SCRIPT)
        self.assertIn(
            'cat "${READER_PARTS[@]}" > "$STAGE_DIR/generated/reader.js"',
            SCRIPT,
        )
        self.assertNotIn('cat "${READER_PARTS[@]}" > "$OUT"', SCRIPT)
        self.assertIn("无副作用预检通过", SCRIPT)

    def test_candidate_and_helpers_are_pinned_before_release_actions(self):
        self.assertIn('CANDIDATE_ROOT="$STAGE_DIR/candidate"', SCRIPT)
        self.assertIn(
            'RELEASE_HELPER="$CANDIDATE_ROOT/scripts/reader_kg_release.py"',
            SCRIPT,
        )
        self.assertIn("run_manifest_helper", SCRIPT)
        self.assertIn('run_manifest_helper "$CANDIDATE_ROOT"', SCRIPT)
        self.assertIn('CANDIDATE_DIGEST="$(hash_candidate_tree)"', SCRIPT)
        self.assertGreaterEqual(SCRIPT.count("verify_candidate_digest"), 5)
        self.assertNotIn(
            "python3 -B scripts/reader_kg_release.py ",
            SCRIPT,
        )
        self.assertIn('"candidateDigest": sys.argv[6] or None', SCRIPT)
        self.assertIn('"payloadDigest": sys.argv[7] or None', SCRIPT)

    def test_deploy_lock_precedes_candidate_capture_and_preflight(self):
        lock_at = SCRIPT.index('flock -n 9')
        build_at = SCRIPT.index(
            'echo "── ① 构建临时 bundle 与完整发布预检"'
        )
        candidate_at = SCRIPT.index(
            'install -D -m 0444 -- \\\n'
            '    "$PROJECT_ROOT/$helper" "$CANDIDATE_ROOT/$helper"'
        )
        self.assertLess(lock_at, build_at)
        self.assertLess(lock_at, candidate_at)
        self.assertIn(
            'if [ "$PREFLIGHT_ONLY" != "1" ]; then',
            SCRIPT[:build_at],
        )

    def test_final_payload_is_sealed_hashed_and_rechecked_before_install(self):
        self.assertIn("hash_deploy_payload", SCRIPT)
        self.assertIn('PAYLOAD_DIGEST="$(hash_deploy_payload)"', SCRIPT)
        self.assertIn(
            'find "$payload_dir" -type f -exec chmod 0444 {} +',
            SCRIPT,
        )
        install_at = SCRIPT.index('echo "── ③ 原子安装普通文件并切换 KG current"')
        last_verify_before_install = SCRIPT.rfind(
            "verify_deploy_payload_digest",
            0,
            install_at,
        )
        self.assertGreater(last_verify_before_install, 0)
        self.assertGreaterEqual(
            SCRIPT.count("verify_deploy_payload_digest"),
            6,
        )

    def test_payload_tamper_is_rejected_by_executable_digest_contract(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for name in ("webapp", "static", "systemd", "kg_runtime"):
                directory = root / name
                directory.mkdir()
                (directory / "payload.txt").write_text(
                    f"{name}\n",
                    encoding="utf-8",
                )
            source = "\n".join(
                (
                    _function_source("hash_deploy_payload"),
                    _function_source("verify_deploy_payload_digest"),
                )
            )
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    "set -u\n"
                    f"STAGE_DIR={root!s}\n"
                    + source
                    + "\nPAYLOAD_DIGEST=\"$(hash_deploy_payload)\"\n"
                    + f"python3 -c \"open('{root / 'webapp' / 'payload.txt'}',"
                    + "'ab').write(b'tamper')\"\n"
                    + "set +e\n"
                    + "if verify_deploy_payload_digest; then rc=0; else rc=$?; fi\n"
                    + "printf 'rc=%s\\n' \"$rc\"\n",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("rc=2", result.stdout)
            self.assertIn("payload 摘要漂移", result.stderr)

    def test_mutable_validation_inputs_and_candidate_sources_are_fenced(self):
        self.assertIn("hash_validation_inputs", SCRIPT)
        self.assertIn(
            'VALIDATION_DIGEST="$(hash_validation_inputs)"',
            SCRIPT,
        )
        self.assertGreaterEqual(SCRIPT.count("verify_validation_digest"), 3)
        self.assertGreaterEqual(
            SCRIPT.count("verify_checkout_inputs_match_candidate"),
            4,
        )
        self.assertIn(
            '"$PROJECT_ROOT/$source_rel" \\\n'
            '      "$CANDIDATE_ROOT/$source_rel"',
            SCRIPT,
        )
        for path in (
            "scripts/deploy_reader.sh",
            "scripts/reader_deploy_manifest.py",
            "scripts/reader_kg_release.py",
            "scripts/reader_e2e.py",
        ):
            with self.subTest(path=path):
                self.assertIn(path, SCRIPT)
        self.assertIn(
            'python3 -B "$CANDIDATE_ROOT/scripts/reader_e2e.py"',
            SCRIPT,
        )
        e2e_at = SCRIPT.index(
            'python3 -B "$CANDIDATE_ROOT/scripts/reader_e2e.py"'
        )
        final_validation_at = SCRIPT.find(
            "verify_validation_digest",
            e2e_at,
        )
        self.assertGreater(final_validation_at, e2e_at)

    def test_validation_input_tamper_is_rejected_by_executable_contract(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            required = (
                "tests/example.py",
                "scripts/deploy_reader.sh",
                "scripts/reader_deploy_manifest.py",
                "scripts/reader_kg_release.py",
                "scripts/reader_e2e.py",
                "scripts/audit_reader_network.py",
                "scripts/reader_network_audit_baseline.json",
                "scripts/vocab/test_batch_protocol.py",
            )
            for relative in required:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(relative + "\n", encoding="utf-8")
            source = "\n".join(
                (
                    _function_source("hash_validation_inputs"),
                    _function_source("verify_validation_digest"),
                )
            )
            target = root / "scripts" / "reader_e2e.py"
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    "set -u\n"
                    f"PROJECT_ROOT={root!s}\n"
                    + source
                    + "\nVALIDATION_DIGEST=\"$(hash_validation_inputs)\"\n"
                    + f"python3 -c \"open('{target}', 'ab').write(b'tamper')\"\n"
                    + "set +e\n"
                    + "if verify_validation_digest; then rc=0; else rc=$?; fi\n"
                    + "printf 'rc=%s\\n' \"$rc\"\n",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("rc=2", result.stdout)
            self.assertIn("验证合同/夹具", result.stderr)

    def test_kg_runtime_is_published_and_switched_as_one_tree(self):
        self.assertIn('"$RELEASE_HELPER" publish', SCRIPT)
        self.assertIn('"$RELEASE_HELPER" switch', SCRIPT)
        self.assertIn("CURRENT_SWITCHED=1", SCRIPT)
        self.assertIn("restore_kg_pointer", SCRIPT)
        self.assertIn("flock -n 9", SCRIPT)
        self.assertIn("deploy-in-progress.json", SCRIPT)

    def test_only_inactive_or_failed_oneshot_is_accepted(self):
        fake = _FakeSystemd()
        self.addCleanup(fake.close)
        for unit in (
            "quick.timer",
            "daily.timer",
            "concept.timer",
            "quick.service",
            "daily.service",
            "concept.service",
            "webapp.service",
            "voice.service",
        ):
            fake.state(unit, "inactive")

        ok = fake.run(
            _state_harness(
                'freeze_writers; printf "rc=%s frozen=%s\\n" "$?" '
                '"$WRITERS_FROZEN"\n'
            )
        )
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn("rc=0 frozen=1", ok.stdout)

        for transient in ("activating", "deactivating", "active"):
            with self.subTest(transient=transient):
                fake.state("quick.service", transient, sticky=True)
                result = fake.run(
                    _state_harness(
                        'set +e; freeze_writers; rc=$?; '
                        'printf "rc=%s frozen=%s\\n" "$rc" '
                        '"$WRITERS_FROZEN"; exit 0\n'
                    )
                )
                self.assertIn("rc=2 frozen=0", result.stdout)
                self.assertIn("KG writer 仍在运行", result.stderr)
                (fake.states / "quick.service.sticky").unlink()
                fake.state("quick.service", "inactive")

    def test_rollback_does_not_repoint_when_freeze_cannot_be_proven(self):
        fake = _FakeSystemd()
        self.addCleanup(fake.close)
        for unit in (
            "quick.timer",
            "daily.timer",
            "concept.timer",
            "quick.service",
            "daily.service",
            "concept.service",
            "webapp.service",
            "voice.service",
        ):
            fake.state(unit, "inactive")
        fake.state("concept.service", "activating", sticky=True)
        marker = fake.root / "deploy-in-progress.json"
        marker.write_text("{}\n", encoding="utf-8")
        events = fake.root / "events"
        rollback_source = _function_source("rollback_deploy")
        result = fake.run(
            _state_harness(
                f"""
                STAGE_DIR={fake.root!s}
                BACKUP_DIR={fake.root!s}
                KG_STATE_BEFORE={fake.root / "missing-state"}
                ACTIVE_MARKER={marker!s}
                KG_MUTABLE_PATHS=(knowledge_graph state/attention)
                restore_kg_pointer() {{ echo pointer >> {events!s}; }}
                restore_backup() {{ echo files >> {events!s}; }}
                hash_kg_state() {{ return 0; }}
                restore_active_units() {{ echo active >> {events!s}; }}
                write_json_atomic() {{ echo "$2" >> {events!s}; }}
                {rollback_source}
                set +e
                if rollback_deploy; then
                  rc=0
                else
                  rc=$?
                fi
                printf 'rc=%s marker=%s\\n' "$rc" "$(
                  [ -f {marker!s} ] && echo present || echo missing
                )"
                exit 0
                """
            )
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("rc=1 marker=present", result.stdout)
        self.assertNotIn("pointer", events.read_text("utf-8"))
        self.assertNotIn("files", events.read_text("utf-8"))
        self.assertIn("rollback_blocked", events.read_text("utf-8"))

    def test_rollback_repoints_only_after_every_unit_is_proven_still(self):
        fake = _FakeSystemd()
        self.addCleanup(fake.close)
        for unit in (
            "quick.timer",
            "daily.timer",
            "concept.timer",
            "quick.service",
            "daily.service",
            "concept.service",
            "webapp.service",
            "voice.service",
        ):
            fake.state(unit, "inactive")
        marker = fake.root / "deploy-in-progress.json"
        marker.write_text("{}\n", encoding="utf-8")
        events = fake.root / "events"
        rollback_source = _function_source("rollback_deploy")
        result = fake.run(
            _state_harness(
                f"""
                STAGE_DIR={fake.root!s}
                BACKUP_DIR={fake.root!s}
                KG_STATE_BEFORE={fake.root / "missing-state"}
                ACTIVE_MARKER={marker!s}
                KG_MUTABLE_PATHS=(knowledge_graph state/attention)
                KG_EXTERNAL_MUTABLE_PATHS=({fake.root / "concepts"})
                restore_kg_pointer() {{ echo pointer >> {events!s}; }}
                restore_backup() {{ echo files >> {events!s}; }}
                hash_kg_state() {{ return 0; }}
                restore_active_units() {{ echo active >> {events!s}; }}
                write_json_atomic() {{ echo "$2" >> {events!s}; }}
                {rollback_source}
                if rollback_deploy; then
                  rc=0
                else
                  rc=$?
                fi
                printf 'rc=%s marker=%s\\n' "$rc" "$(
                  [ -f {marker!s} ] && echo present || echo missing
                )"
                exit 0
                """
            )
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("rc=0 marker=missing", result.stdout)
        self.assertEqual(
            events.read_text("utf-8").splitlines(),
            ["pointer", "files", "active", "rolled_back"],
        )

    def test_rollback_does_not_restore_files_after_pointer_cas_failure(self):
        fake = _FakeSystemd()
        self.addCleanup(fake.close)
        for unit in (
            "quick.timer",
            "daily.timer",
            "concept.timer",
            "quick.service",
            "daily.service",
            "concept.service",
            "webapp.service",
            "voice.service",
        ):
            fake.state(unit, "inactive")
        marker = fake.root / "deploy-in-progress.json"
        marker.write_text("{}\n", encoding="utf-8")
        events = fake.root / "events"
        rollback_source = _function_source("rollback_deploy")
        result = fake.run(
            _state_harness(
                f"""
                STAGE_DIR={fake.root!s}
                BACKUP_DIR={fake.root!s}
                KG_STATE_BEFORE={fake.root / "missing-state"}
                ACTIVE_MARKER={marker!s}
                KG_MUTABLE_PATHS=(knowledge_graph state/attention)
                KG_EXTERNAL_MUTABLE_PATHS=({fake.root / "concepts"})
                restore_kg_pointer() {{ echo pointer >> {events!s}; return 9; }}
                restore_backup() {{ echo files >> {events!s}; }}
                hash_kg_state() {{ return 0; }}
                restore_active_units() {{ echo active >> {events!s}; }}
                write_json_atomic() {{ echo "$2" >> {events!s}; }}
                {rollback_source}
                set +e
                if rollback_deploy; then
                  rc=0
                else
                  rc=$?
                fi
                printf 'rc=%s marker=%s\\n' "$rc" "$(
                  [ -f {marker!s} ] && echo present || echo missing
                )"
                exit 0
                """
            )
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("rc=1 marker=present", result.stdout)
        self.assertEqual(
            events.read_text("utf-8").splitlines(),
            ["pointer", "rollback_blocked"],
        )

    def test_success_is_not_declared_before_durable_result_and_marker_cleanup(self):
        restore_at = SCRIPT.rfind("\nrestore_active_units\n")
        result_at = SCRIPT.rfind(
            'write_json_atomic "$BACKUP_DIR/result.json" "complete"'
        )
        marker_at = SCRIPT.rfind('sudo rm -f -- "$ACTIVE_MARKER"')
        finished_at = SCRIPT.rfind("DEPLOY_FINISHED=1")
        self.assertGreater(restore_at, 0)
        self.assertLess(restore_at, result_at)
        self.assertLess(result_at, marker_at)
        self.assertLess(marker_at, finished_at)

    def test_voice_stability_rejects_pid_or_restart_drift(self):
        fake = _FakeSystemd()
        self.addCleanup(fake.close)
        fake.state("voice.service", "active")
        fake.property_sequence("voice.service", "MainPID", ["101", "202"])
        fake.property_sequence("voice.service", "NRestarts", ["0", "1"])
        source = "\n".join(
            (
                _function_source("unit_active_state"),
                _function_source("wait_unit_active"),
                _function_source("assert_voice_runtime_stable"),
            )
        )
        result = fake.run(
            "set -u\n"
            "VOICE_RT_UNIT=voice.service\n"
            "VOICE_STABILITY_SECONDS=0\n"
            "voice_tcp_probe() { return 0; }\n"
            + source
            + "\nset +e; assert_voice_runtime_stable; "
            'printf "rc=%s\\n" "$?"; exit 0\n'
        )
        self.assertIn("rc=2", result.stdout)
        self.assertIn("稳定窗口内重启", result.stderr)

    def test_child_err_handler_never_performs_deployment_rollback(self):
        source = _function_source("on_error")
        with tempfile.TemporaryDirectory() as raw:
            events = Path(raw) / "events"
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    "set -u\n"
                    "ROOT_SHELL_BASHPID=\"$BASHPID\"\n"
                    "DEPLOY_STARTED=1\n"
                    "DEPLOY_FINISHED=0\n"
                    "ORIGINAL_EXIT=0\n"
                    f"rollback_deploy() {{ echo rollback >> {events!s}; }}\n"
                    + source
                    + "\nset +e\n"
                    + "( false; on_error )\n"
                    + "rc=$?\n"
                    + "printf 'rc=%s\\n' \"$rc\"\n"
                    + "exit 0\n",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("rc=1", result.stdout)
            self.assertFalse(events.exists())

    def test_health_checks_cover_voice_lazy_cards_and_runtime_imports(self):
        self.assertIn("assert_voice_runtime_stable", SCRIPT)
        self.assertIn("assert_webapp_runtime_stable", SCRIPT)
        self.assertIn("wait_webapp_http 30", SCRIPT)
        self.assertIn("wait_voice_tcp 30", SCRIPT)
        self.assertIn('HTTP/1.1 101', SCRIPT)
        self.assertIn("VOICE_RT_PORT=8767", SCRIPT)
        self.assertIn("WEBAPP_PORT=5000", SCRIPT)
        self.assertIn("pdf_reader._review_candidate_service()", SCRIPT)
        self.assertIn(
            'PYTHONPATH="$STAGE_DIR/webapp"',
            SCRIPT,
        )
        self.assertIn(
            "staged card candidate lazy service contract mismatch",
            SCRIPT,
        )
        self.assertIn('kg_runtime.import_module(name)', SCRIPT)
        self.assertIn("production KG current mismatch", SCRIPT)
        self.assertIn(
            '/usr/bin/python3 -B - "$NEW_KG_ID"',
            SCRIPT,
        )
        self.assertNotIn(
            '/home/bwicarus/mcp-venv/bin/python -B - "$NEW_KG_ID"',
            SCRIPT,
        )
        self.assertIn("concept_graph_daily.py", SCRIPT)
        self.assertIn("--gate-only", SCRIPT)

    def test_hash_and_snapshot_share_one_explicit_inventory(self):
        self.assertIn("KG_MUTABLE_PATHS=(", SCRIPT)
        self.assertIn('"knowledge_graph"', SCRIPT)
        self.assertIn('"state/attention"', SCRIPT)
        self.assertIn(
            'hash_kg_state "$KG_STATE_BEFORE" \\\n'
            '  "${KG_MUTABLE_PATHS[@]}" "${KG_EXTERNAL_MUTABLE_PATHS[@]}"',
            SCRIPT,
        )
        self.assertIn(
            'for state_path in "${KG_MUTABLE_PATHS[@]}"; do',
            SCRIPT,
        )
        self.assertIn('"$OBSIDIAN_VAULT_ROOT/资源/概念"', SCRIPT)
        self.assertIn("kg-vault-concepts.tar", SCRIPT)
        self.assertIn("KG 状态取证快照", SCRIPT)

    def test_deploy_never_bulk_deletes_production_roots(self):
        forbidden = (
            'rm -rf -- "$WEBAPP_ROOT"',
            'rm -rf -- "$STATIC_ROOT"',
            'rm -rf -- "$KG_RUNTIME_ROOT"',
            "rsync --delete",
        )
        for value in forbidden:
            with self.subTest(value=value):
                self.assertNotIn(value, SCRIPT)


if __name__ == "__main__":
    unittest.main()
