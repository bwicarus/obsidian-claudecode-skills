# -*- coding: utf-8 -*-
"""摄像头取图的测试。

守的不是"能拍照"，而是几条一旦破了就很难事后发现的性质：
坏图不能被当成好图写下去、认不出的摄像头要报出可用的有哪些、
以及**图像绝不进快照载荷**（用户 2026-08-27 明说：快照那个 tab 只是
显示口，AI 不该每取一次快照就被塞一张家里的照片）。
"""
import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import camera_capture  # noqa: E402

# 一个最小的合法 JPEG（1x1）。测的是"开头是不是 JPEG"这条闸，
# 所以只要头对、能被 base64 往返即可。
_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300"
    "0806060706050807070709090807"
) + b"\xff\xd9"


def _root() -> Path:
    return Path(tempfile.mkdtemp(prefix="bw-camtest-"))


def _envelope(**overrides):
    payload = {
        "ok": True,
        "capturedAtUtcMs": 1700000000000,
        "width": 1280,
        "height": 720,
        "brightness": 60.0,
        "jpegBase64": base64.b64encode(_JPEG).decode("ascii"),
    }
    payload.update(overrides)
    return payload


class RegistryTests(unittest.TestCase):
    def test_missing_registry_is_seeded(self):
        root = _root()
        sources = camera_capture.load_sources(root)
        self.assertTrue(sources)
        self.assertTrue(camera_capture.sources_path(root).is_file())

    def test_corrupt_registry_raises_instead_of_resetting(self):
        """静默重置会让用户刚登记的第二台摄像头凭空消失，而且哪儿都不说。"""
        root = _root()
        camera_capture.sources_path(root).parent.mkdir(
            parents=True, exist_ok=True)
        camera_capture.sources_path(root).write_text("{ 坏的", encoding="utf-8")
        with self.assertRaises(camera_capture.CameraError):
            camera_capture.load_sources(root)

    def test_unknown_id_lists_what_exists(self):
        root = _root()
        with self.assertRaises(camera_capture.CameraError) as caught:
            camera_capture.find_source("nope", root)
        self.assertIn("pi", str(caught.exception),
                      "只说'没有这台'而不说有哪些，等于让人去猜")


class SnapTests(unittest.TestCase):
    def _patch(self, envelope):
        # ⚠ 必须换掉**字典里那一项**：_CAPTURERS 在导入时就抓住了函数对象，
        # patch 模块属性对已经建好的字典无效（第一版就是这么错的，
        # 症状是测试悄悄跑去连真的 Pi）。
        return mock.patch.dict(
            camera_capture._CAPTURERS,
            {"ssh-v4l2": lambda source, size: dict(envelope)})

    def test_snap_writes_a_file_and_returns_its_path(self):
        root = _root()
        with self._patch(_envelope()):
            meta = camera_capture.snap("pi", root=root)
        path = Path(meta["path"])
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_bytes(), _JPEG)

    def test_non_jpeg_is_refused(self):
        """拿回来的不是图片时就得停下 —— 写下去的话，AI 打开时得到的是
        一个跟'摄像头没插'完全不同、却同样没用的错误。"""
        root = _root()
        bad = base64.b64encode(b"<html>404</html>").decode("ascii")
        with self._patch(_envelope(jpegBase64=bad)):
            with self.assertRaises(camera_capture.CameraError) as caught:
                camera_capture.snap("pi", root=root)
        self.assertIn("JPEG", str(caught.exception))

    def test_corrupt_base64_is_refused(self):
        root = _root()
        with self._patch(_envelope(jpegBase64="!!!not base64!!!")):
            with self.assertRaises(camera_capture.CameraError):
                camera_capture.snap("pi", root=root)

    def test_missing_image_data_is_refused(self):
        root = _root()
        envelope = _envelope()
        envelope.pop("jpegBase64")
        with self._patch(envelope):
            with self.assertRaises(camera_capture.CameraError):
                camera_capture.snap("pi", root=root)

    def test_old_frames_are_pruned(self):
        root = _root()
        for index in range(camera_capture.KEEP_FRAMES + 5):
            with self._patch(_envelope(
                    capturedAtUtcMs=1700000000000 + index * 1000)):
                camera_capture.snap("pi", root=root)
        frames = list(camera_capture.frames_dir("pi", root).glob("*.jpg"))
        self.assertEqual(len(frames), camera_capture.KEEP_FRAMES,
                         "不清理的话会慢慢把盘吃满")


class LatestTests(unittest.TestCase):
    def test_no_capture_yet_is_none_not_an_error(self):
        self.assertIsNone(camera_capture.latest("pi", _root()))

    def test_dangling_path_reads_as_no_capture(self):
        """元数据在但图被清理了。返回一个指向空气的路径比返回 None 糟得多 ——
        调用方会拿着它去打开一个不存在的文件。"""
        root = _root()
        with mock.patch.dict(
                camera_capture._CAPTURERS,
                {"ssh-v4l2": lambda source, size: _envelope()}):
            meta = camera_capture.snap("pi", root=root)
        Path(meta["path"]).unlink()
        self.assertIsNone(camera_capture.latest("pi", root))


class InjectionTests(unittest.TestCase):
    """登记表将来会被 AI 或用户编辑，而 ssh 把参数拼成一条远程 shell 命令。"""

    def test_remote_arguments_are_quoted(self):
        import shlex
        captured = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            return mock.Mock(
                stdout=json.dumps(_envelope()), stderr="", returncode=0)

        source = {
            "id": "x", "kind": "ssh-v4l2", "host": "pi",
            "script": "/tmp/s.py",
            "device": "/dev/video0; rm -rf ~",
        }
        with mock.patch.object(camera_capture.subprocess, "run", fake_run):
            camera_capture._capture_ssh_v4l2(source, None)
        remote = shlex.split(captured["command"][-1])
        self.assertEqual(
            remote[remote.index("--device") + 1], "/dev/video0; rm -rf ~")
        self.assertNotIn("rm", remote, "参数没被引用，远端 shell 会执行它")


class FailureTests(unittest.TestCase):
    def _run(self, **kwargs):
        return mock.Mock(**kwargs)

    def test_remote_error_is_passed_through_verbatim(self):
        """Pi 那边说了原因就原样带上来 —— 这段文字最终会给到 AI 和用户。"""
        source = {"id": "x", "kind": "ssh-v4l2", "host": "pi",
                  "script": "/tmp/s.py"}
        payload = json.dumps({"ok": False, "error": "这台机器上没有 ffmpeg"})
        with mock.patch.object(
                camera_capture.subprocess, "run",
                return_value=self._run(stdout=payload, stderr="",
                                       returncode=2)):
            with self.assertRaises(camera_capture.CameraError) as caught:
                camera_capture._capture_ssh_v4l2(source, None)
        self.assertIn("ffmpeg", str(caught.exception))

    def test_silent_remote_reports_the_exit_code_and_stderr(self):
        """一个字都没有时最容易被写成'取图失败'四个字。那样就没法查了。"""
        source = {"id": "x", "kind": "ssh-v4l2", "host": "pi",
                  "script": "/tmp/s.py"}
        with mock.patch.object(
                camera_capture.subprocess, "run",
                return_value=self._run(
                    stdout="", stderr="ssh: Could not resolve hostname pi",
                    returncode=255)):
            with self.assertRaises(camera_capture.CameraError) as caught:
                camera_capture._capture_ssh_v4l2(source, None)
        self.assertIn("resolve hostname", str(caught.exception))

    def test_unknown_kind_names_what_is_supported(self):
        root = _root()
        path = camera_capture.sources_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "contract": camera_capture.SOURCES_CONTRACT,
            "sources": [{"id": "x", "kind": "onvif"}],
        }), encoding="utf-8")
        with self.assertRaises(camera_capture.CameraError) as caught:
            camera_capture.snap("x", root=root)
        self.assertIn("ssh-v4l2", str(caught.exception))


class SnapshotIsolationTests(unittest.TestCase):
    """用户 2026-08-27 明说：快照那个 tab **只是显示口**，
    AI 不该每取一次快照就被塞一张家里的照片。"""

    def test_context_snapshot_does_not_carry_camera_frames(self):
        source = (Path(__file__).resolve().parents[2]
                  / "ComputerVoiceAudio" / "DirectContextSnapshot.cs")
        text = source.read_text(encoding="utf-8", errors="replace").lower()
        for forbidden in ("camera", "jpegbase64"):
            self.assertNotIn(
                forbidden, text,
                "快照投影里出现了 %r —— 摄像头画面正在被塞进 AI 的上下文"
                % forbidden)


if __name__ == "__main__":
    unittest.main()
