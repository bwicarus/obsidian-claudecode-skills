from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))

from bridge_core import (  # noqa: E402
    BridgeError,
    BridgePaths,
    DIRECT_SERVE_SIBLING_PATHS,
    DIRECT_WSS_URL,
)
from control_plane import (  # noqa: E402
    ControlPaths,
    SERVE_PATH,
    SERVE_TARGET,
    SERVE_TCP_PORT,
    SERVE_WEB_HOST,
    SubprocessExactCommandRunner,
    TASK_DESCRIPTION_MARKER,
    TASK_NAME,
    TASK_TEMP_PREFIX,
    _decode_command_output,
    apply_tailscale_serve,
    build_task_command_plan,
    build_task_xml,
    classify_serve_status,
    inspect_bootstrap_task,
    inspect_tailscale_serve,
    install_bootstrap_task,
    remove_bootstrap_task,
    remove_tailscale_serve,
    run_bootstrap_task_if_owned,
    task_xml_is_owned,
)


USER_SID = "S-1-5-21-111111111-222222222-333333333-1001"
TASK_MISSING = "ERROR: The system cannot find the file specified.\n"


def result(
    returncode: int,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess((), returncode, stdout, stderr)


class ScriptedRunner:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, ...]] = []
        self.created_xml: bytes | None = None

    def run_exact(self, command, *, timeout_seconds):
        command = tuple(command)
        self.calls.append(command)
        if "/Create" in command:
            xml_path = Path(command[command.index("/XML") + 1])
            self.created_xml = xml_path.read_bytes()
        if not self.responses:
            raise AssertionError(f"unexpected command: {command}")
        return self.responses.pop(0)


class ControlPlaneTests(unittest.TestCase):
    def test_command_output_decoder_prefers_utf8_then_system_encoding(
        self,
    ) -> None:
        self.assertEqual(
            _decode_command_output(
                "直连正常".encode("utf-8"),
                fallback_encoding="cp936",
            ),
            "直连正常",
        )
        self.assertEqual(
            _decode_command_output(
                "错误".encode("cp936"),
                fallback_encoding="cp936",
            ),
            "错误",
        )
        with self.assertRaisesRegex(
            BridgeError,
            "控制命令输出编码无效",
        ):
            _decode_command_output(
                b"\xff",
                fallback_encoding="ascii",
            )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.paths = BridgePaths.for_root(self.root)
        self.paths.desktop_launcher.parent.mkdir(parents=True)
        self.paths.desktop_launcher.write_bytes(b"noconsole placeholder")
        tools = self.root / "tools"
        tools.mkdir()
        self.control = ControlPaths(
            schtasks_exe=tools / "schtasks.exe",
            whoami_exe=tools / "whoami.exe",
            tailscale_exe=tools / "tailscale.exe",
        )
        for executable in (
            self.control.schtasks_exe,
            self.control.whoami_exe,
            self.control.tailscale_exe,
        ):
            executable.write_bytes(b"tool placeholder")
        self.identity = result(
            0,
            f'"DESKTOP\\\\reader","{USER_SID}"\n',
        )
        self.owned_xml = build_task_xml(
            self.paths,
            USER_SID,
        ).decode("utf-16")
        self.serve_json = json.dumps(
            {
                "TCP": {
                    SERVE_TCP_PORT: {
                        "HTTPS": True,
                    },
                },
                "Web": {
                    SERVE_WEB_HOST: {
                        "Handlers": {
                            SERVE_PATH: {"Proxy": SERVE_TARGET}
                        }
                    }
                }
            }
        )
        # ⚠ 上面那份是**理想形状**：只有我们这一条 handler。
        # 现实里这台机上有七条 —— 桥自己的其它路由是历次按文档手工
        # `tailscale serve --set-path=...` 加的（见 web-context-snapshot-handoff.md）。
        # 判定器原来用整份全等比较，于是恒判 foreign，apply/off 全线瘫痪；
        # 而测试因为只见过单 handler 的世界，一直是绿的。
        # **这份 fixture 存在的意义就是让测试看见现实。**
        self.sibling_paths = list(DIRECT_SERVE_SIBLING_PATHS)
        self.serve_real_json = json.dumps(self._serve_config(
            [SERVE_PATH] + self.sibling_paths))
        self.serve_siblings_only_json = json.dumps(
            self._serve_config(self.sibling_paths))

    @staticmethod
    def _serve_config(paths: list[str]) -> dict:
        return {
            "TCP": {SERVE_TCP_PORT: {"HTTPS": True}},
            "Web": {
                SERVE_WEB_HOST: {
                    "Handlers": {
                        path: {
                            "Proxy": "http://127.0.0.1:43128" + path
                        }
                        for path in paths
                    }
                }
            },
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_endpoint_is_fixed_and_uses_canonical_path(self) -> None:
        self.assertEqual(
            DIRECT_WSS_URL,
            "wss://bwicarus-2.taile44d0c.ts.net"
            "/reader-computer-voice/v1",
        )

    def test_default_control_runner_uses_exact_argv_without_shell(self) -> None:
        command = (
            str(self.control.schtasks_exe.resolve()),
            "/Query",
            "/TN",
            TASK_NAME,
            "/XML",
        )
        completed = result(0, "<Task />")
        with mock.patch(
            "control_plane.subprocess.run",
            return_value=completed,
        ) as run:
            actual = SubprocessExactCommandRunner().run_exact(
                command,
                timeout_seconds=7.0,
            )

        self.assertIs(actual, completed)
        run.assert_called_once_with(
            list(command),
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=False,
            timeout=7.0,
            shell=False,
            creationflags=mock.ANY,
        )

    def test_task_xml_is_interactive_hidden_and_restartable(self) -> None:
        xml = self.owned_xml
        self.assertIn(TASK_DESCRIPTION_MARKER, xml)
        self.assertIn("<LogonType>InteractiveToken</LogonType>", xml)
        self.assertIn("<RunLevel>LeastPrivilege</RunLevel>", xml)
        self.assertIn("<Hidden>true</Hidden>", xml)
        self.assertIn("<Interval>PT1M</Interval>", xml)
        self.assertIn("<Count>3</Count>", xml)
        self.assertIn("<Arguments>--bootstrap</Arguments>", xml)
        self.assertNotIn("--direct-serve", xml)
        self.assertTrue(task_xml_is_owned(xml, self.paths, USER_SID))

    def test_task_ownership_accepts_only_windows_canonical_user_defaults(
        self,
    ) -> None:
        namespace = (
            "http://schemas.microsoft.com/windows/2004/02/mit/task"
        )
        root = ET.fromstring(self.owned_xml)
        trigger = root.find(
            f"./{{{namespace}}}Triggers/"
            f"{{{namespace}}}LogonTrigger"
        )
        principal = root.find(
            f"./{{{namespace}}}Principals/"
            f"{{{namespace}}}Principal"
        )
        self.assertIsNotNone(trigger)
        self.assertIsNotNone(principal)
        trigger.find(f"{{{namespace}}}UserId").text = "DESKTOP\\reader"
        trigger.remove(trigger.find(f"{{{namespace}}}Enabled"))
        principal.remove(principal.find(f"{{{namespace}}}RunLevel"))
        canonical = ET.tostring(root, encoding="unicode")

        self.assertTrue(
            task_xml_is_owned(
                canonical,
                self.paths,
                USER_SID,
                user_name="desktop\\READER",
            )
        )
        self.assertFalse(
            task_xml_is_owned(
                canonical,
                self.paths,
                USER_SID,
                user_name="DESKTOP\\another-user",
            )
        )

    def test_task_ownership_rejects_any_extra_trigger_or_action(self) -> None:
        namespace = (
            "http://schemas.microsoft.com/windows/2004/02/mit/task"
        )
        for parent_name, child_name in (
            ("Triggers", "BootTrigger"),
            ("Actions", "ComHandler"),
        ):
            with self.subTest(child=child_name):
                root = ET.fromstring(self.owned_xml)
                parent = root.find(f"{{{namespace}}}{parent_name}")
                self.assertIsNotNone(parent)
                ET.SubElement(parent, f"{{{namespace}}}{child_name}")
                self.assertFalse(
                    task_xml_is_owned(
                        ET.tostring(root, encoding="unicode"),
                        self.paths,
                        USER_SID,
                    )
                )

    def test_install_uses_temp_xml_and_never_overwrites(self) -> None:
        runner = ScriptedRunner(
            [
                self.identity,
                result(1, "", TASK_MISSING),
                result(0),
                result(0, self.owned_xml),
            ]
        )
        self.assertTrue(
            install_bootstrap_task(
                self.paths,
                self.control,
                runner,
            )
        )
        create = next(
            command for command in runner.calls if "/Create" in command
        )
        self.assertNotIn("/F", create)
        self.assertEqual(
            create[create.index("/TN") + 1],
            TASK_NAME,
        )
        xml_path = Path(create[create.index("/XML") + 1])
        self.assertTrue(xml_path.parent.name.startswith(TASK_TEMP_PREFIX))
        self.assertFalse(xml_path.exists())
        self.assertEqual(runner.created_xml, build_task_xml(self.paths, USER_SID))

    def test_install_refuses_any_existing_same_name_without_mutation(self) -> None:
        runner = ScriptedRunner(
            [
                self.identity,
                result(0, "<Task>unknown owner</Task>"),
            ]
        )
        with self.assertRaises(BridgeError):
            install_bootstrap_task(self.paths, self.control, runner)
        self.assertEqual(len(runner.calls), 2)
        self.assertFalse(any("/Create" in call for call in runner.calls))

    def test_install_never_rolls_back_unknown_post_create_task(self) -> None:
        runner = ScriptedRunner(
            [
                self.identity,
                result(1, "", TASK_MISSING),
                result(0),
                result(0, "<Task>replaced concurrently</Task>"),
            ]
        )
        with self.assertRaises(BridgeError):
            install_bootstrap_task(self.paths, self.control, runner)
        self.assertTrue(any("/Create" in call for call in runner.calls))
        self.assertFalse(any("/Delete" in call for call in runner.calls))

    def test_delete_only_removes_owned_exact_task(self) -> None:
        runner = ScriptedRunner(
            [
                self.identity,
                result(0, self.owned_xml),
                result(0),
                result(0),
                result(1, "", TASK_MISSING),
            ]
        )
        self.assertTrue(
            remove_bootstrap_task(
                self.paths,
                self.control,
                runner,
            )
        )
        self.assertTrue(any("/End" in call for call in runner.calls))
        deletion = next(
            call for call in runner.calls if "/Delete" in call
        )
        self.assertEqual(
            deletion[deletion.index("/TN") + 1],
            TASK_NAME,
        )

        unknown = ScriptedRunner(
            [
                self.identity,
                result(0, "<Task>unknown owner</Task>"),
            ]
        )
        with self.assertRaises(BridgeError):
            remove_bootstrap_task(
                self.paths,
                self.control,
                unknown,
            )
        self.assertFalse(any("/Delete" in call for call in unknown.calls))

    def test_task_query_is_read_only(self) -> None:
        runner = ScriptedRunner(
            [self.identity, result(1, "", TASK_MISSING)]
        )
        inspection = inspect_bootstrap_task(
            self.paths,
            self.control,
            runner,
        )
        self.assertFalse(inspection.exists)
        self.assertFalse(
            any(
                "/Create" in call
                or "/Delete" in call
                or "/End" in call
                for call in runner.calls
            )
        )

    def test_task_query_rc1_without_not_found_proof_fails_closed(self) -> None:
        runner = ScriptedRunner(
            [self.identity, result(1, "", "ERROR: Access is denied.")]
        )
        with self.assertRaises(BridgeError):
            inspect_bootstrap_task(
                self.paths,
                self.control,
                runner,
            )
        self.assertFalse(
            any(
                "/Create" in call
                or "/Run" in call
                or "/Delete" in call
                for call in runner.calls
            )
        )

    def test_task_plan_has_exact_fixed_name_and_xml_only_create(self) -> None:
        xml_path = (
            self.root
            / TASK_TEMP_PREFIX
            / "computer-voice-bootstrap-task.xml"
        )
        plan = build_task_command_plan(self.control, xml_path)
        self.assertEqual(plan.query[3], TASK_NAME)
        self.assertEqual(plan.create[3], TASK_NAME)
        self.assertEqual(plan.run[3], TASK_NAME)
        self.assertEqual(plan.delete[3], TASK_NAME)
        self.assertNotIn("/F", plan.create)
        self.assertIn("/F", plan.delete)

    def test_task_run_requires_fresh_exact_ownership_proof(self) -> None:
        owned = ScriptedRunner(
            [
                self.identity,
                result(0, self.owned_xml),
                result(0),
            ]
        )
        self.assertTrue(
            run_bootstrap_task_if_owned(
                self.paths,
                self.control,
                owned,
            )
        )
        run = owned.calls[-1]
        self.assertEqual(run[1:], ("/Run", "/TN", TASK_NAME))

        unknown = ScriptedRunner(
            [
                self.identity,
                result(0, "<Task>unknown</Task>"),
            ]
        )
        with self.assertRaises(BridgeError):
            run_bootstrap_task_if_owned(
                self.paths,
                self.control,
                unknown,
            )
        self.assertFalse(any("/Run" in call for call in unknown.calls))

        missing = ScriptedRunner(
            [self.identity, result(1, "", TASK_MISSING)]
        )
        self.assertFalse(
            run_bootstrap_task_if_owned(
                self.paths,
                self.control,
                missing,
            )
        )
        self.assertFalse(any("/Run" in call for call in missing.calls))

    def test_serve_apply_and_off_are_path_scoped_and_verified(self) -> None:
        apply_runner = ScriptedRunner(
            [
                result(0, "{}"),
                result(0),
                result(0, self.serve_json),
            ]
        )
        self.assertTrue(
            apply_tailscale_serve(self.control, apply_runner)
        )
        mutation = apply_runner.calls[1]
        self.assertIn("--yes", mutation)
        self.assertIn("--set-path=/reader-computer-voice/v1", mutation)
        self.assertIn(SERVE_TARGET, mutation)
        self.assertEqual(
            SERVE_TARGET,
            "http://127.0.0.1:43128/reader-computer-voice/v1",
        )

        off_runner = ScriptedRunner(
            [
                result(0, self.serve_json),
                result(0),
                result(0, "{}"),
            ]
        )
        self.assertTrue(
            remove_tailscale_serve(self.control, off_runner)
        )
        off = off_runner.calls[1]
        self.assertIn("--yes", off)
        self.assertIn("--set-path=/reader-computer-voice/v1", off)
        self.assertEqual(off[-1], "off")

    def test_status_classifier_accepts_the_real_multi_handler_config(self) -> None:
        """线上七条 handler 必须判 ours。

        ⚠ 这条是 2026-08-27 的回归闸：原判定用整份全等比较、期望唯一
        handler，于是这台机上的真实配置恒判 foreign，控制面的 apply/off
        全线拒绝。**但测试一直是绿的** —— 因为 fixture 只有一条 handler。
        测试没见过的世界，测试保护不了。
        """
        inspection = classify_serve_status(
            json.loads(self.serve_real_json))
        self.assertEqual("ours", inspection.state)
        self.assertEqual(
            1 + len(self.sibling_paths), len(inspection.handlers))

    def test_serve_apply_and_off_keep_sibling_mounts_intact(self) -> None:
        """多 handler 世界里 apply/off 仍然只动我们那一条。"""
        apply_runner = ScriptedRunner(
            [
                result(0, self.serve_siblings_only_json),
                result(0),
                result(0, self.serve_real_json),
            ]
        )
        self.assertTrue(apply_tailscale_serve(self.control, apply_runner))
        self.assertIn(
            "--set-path=/reader-computer-voice/v1", apply_runner.calls[1])

        off_runner = ScriptedRunner(
            [
                result(0, self.serve_real_json),
                result(0),
                result(0, self.serve_siblings_only_json),
            ]
        )
        self.assertTrue(remove_tailscale_serve(self.control, off_runner))
        self.assertEqual("off", off_runner.calls[1][-1])

    def test_serve_off_rejects_when_a_sibling_mount_disappeared(self) -> None:
        """撤自己那条时**顺手少了别人一条**，必须报错。

        原来的后置是「关闭后必须是空配置」，在七条并存的现实里它永远失败；
        换成挂载集合差分之后，它才真正在检查「有没有殃及别人」——
        这是从隐含假设变成显式断言。
        """
        damaged = json.dumps(self._serve_config(self.sibling_paths[1:]))
        runner = ScriptedRunner(
            [
                result(0, self.serve_real_json),
                result(0),
                result(0, damaged),
            ]
        )
        with self.assertRaises(BridgeError):
            remove_tailscale_serve(self.control, runner)

    def test_serve_never_overwrites_or_removes_foreign_config(self) -> None:
        foreign_json = json.dumps(
            {
                "Web": {
                    "host:443": {
                        "Handlers": {
                            "/other": {
                                "Proxy": "http://127.0.0.1:9999"
                            }
                        }
                    }
                }
            }
        )
        for action in (apply_tailscale_serve, remove_tailscale_serve):
            runner = ScriptedRunner([result(0, foreign_json)])
            with self.assertRaises(BridgeError):
                action(self.control, runner)
            self.assertEqual(len(runner.calls), 1)

    def test_serve_failed_postcheck_never_turns_off_mixed_state(self) -> None:
        mixed = json.dumps(
            {
                "TCP": {
                    SERVE_TCP_PORT: {
                        "HTTPS": True,
                    },
                },
                "Web": {
                    SERVE_WEB_HOST: {
                        "Handlers": {
                            SERVE_PATH: {"Proxy": SERVE_TARGET},
                            "/other": {
                                "Proxy": "http://127.0.0.1:9999"
                            },
                        }
                    }
                }
            }
        )
        runner = ScriptedRunner(
            [
                result(0, "{}"),
                result(0),
                result(0, mixed),
            ]
        )
        with self.assertRaises(BridgeError):
            apply_tailscale_serve(self.control, runner)
        self.assertFalse(
            any(call[-1:] == ("off",) for call in runner.calls)
        )

        replaced = ScriptedRunner(
            [
                result(0, "{}"),
                result(0),
                result(
                    0,
                    json.dumps(
                        {
                            "Web": {
                                "host:443": {
                                    "Handlers": {
                                        SERVE_PATH: {
                                            "Proxy":
                                                "http://127.0.0.1:9999"
                                        }
                                    }
                                }
                            }
                        }
                    ),
                ),
            ]
        )
        with self.assertRaises(BridgeError):
            apply_tailscale_serve(self.control, replaced)
        self.assertFalse(
            any(call[-1:] == ("off",) for call in replaced.calls)
        )

    def test_serve_status_refresh_is_read_only(self) -> None:
        runner = ScriptedRunner([result(0, self.serve_json)])
        inspection = inspect_tailscale_serve(self.control, runner)
        self.assertEqual(inspection.state, "ours")
        self.assertEqual(len(runner.calls), 1)
        self.assertIn("status", runner.calls[0])
        self.assertFalse(
            any(
                "--bg" in call or call[-1:] == ("off",)
                for call in runner.calls
            )
        )

    def test_status_classifier_requires_exact_path_and_backend(self) -> None:
        self.assertEqual(classify_serve_status({}).state, "empty")
        self.assertEqual(
            classify_serve_status(json.loads(self.serve_json)).state,
            "ours",
        )
        self.assertEqual(
            classify_serve_status(
                {
                    "Web": {
                        "host": {
                            "Handlers": {
                                SERVE_PATH: {
                                    "Proxy": "http://127.0.0.1:9999"
                                }
                            }
                        }
                    }
                }
            ).state,
            "foreign",
        )

    def test_status_classifier_rejects_wrong_host_tcp_funnel_and_unknown(self) -> None:
        exact = json.loads(self.serve_json)
        cases = {
            "wrong-host": {
                **exact,
                "Web": {
                    "other-node.example.ts.net:443": exact["Web"][
                        SERVE_WEB_HOST
                    ],
                },
            },
            "wrong-tcp": {
                **exact,
                "TCP": {
                    SERVE_TCP_PORT: {
                        "TCPForward": "127.0.0.1:43128",
                    },
                },
            },
            "funnel": {
                **exact,
                "AllowFunnel": {
                    SERVE_WEB_HOST: True,
                },
            },
            "foreground": {
                **exact,
                "Foreground": {
                    "foreign-session": {},
                },
            },
            "extra-handler": {
                **exact,
                "Web": {
                    SERVE_WEB_HOST: {
                        "Handlers": {
                            **exact["Web"][SERVE_WEB_HOST]["Handlers"],
                            "/other": {
                                "Proxy": "http://127.0.0.1:9999",
                            },
                        },
                    },
                },
            },
        }
        for label, value in cases.items():
            with self.subTest(label=label):
                self.assertEqual(
                    classify_serve_status(value).state,
                    "foreign",
                )

    def test_serve_never_applies_or_turns_off_funnel_mixed_config(self) -> None:
        mixed = json.loads(self.serve_json)
        mixed["AllowFunnel"] = {SERVE_WEB_HOST: True}
        payload = json.dumps(mixed)
        for action in (apply_tailscale_serve, remove_tailscale_serve):
            with self.subTest(action=action.__name__):
                runner = ScriptedRunner([result(0, payload)])
                with self.assertRaises(BridgeError):
                    action(self.control, runner)
                self.assertEqual(len(runner.calls), 1)
                self.assertFalse(
                    any(
                        "--bg" in call or call[-1:] == ("off",)
                        for call in runner.calls
                    )
                )


if __name__ == "__main__":
    unittest.main()
