#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""摄像头取图（Pi 侧传感器）。

职责只有一件：**拍一张能看的照片，交出去**。它不判断画面内容，也不知道
调用方要拿它干什么 —— 判断归 Windows 那边的 AI（它本来就能看图）。

这样分工是用户 2026-08-27 定的：「只是给 codex 那边的 ai 提供一个获取他
想要的资讯的手段罢了，也就是说目前只需要给他获取图像的一个接口」。

输出是一个 JSON 信封（图片走 base64）。用信封而不是裸二进制，是因为
调用方在 Windows，中间隔着 ssh 和 subprocess 两层管道，裸二进制被文本
模式改一个字节就得到一张坏图、而且**看起来像成功**。多出来的 33% 体积
在一张 150KB 的图上不值一提。

留好的扩展位（用户说后面会换带云台、补光的高清摄像头）：
- `--size`   已经通着，换更高分辨率不用改形状
- `--device` 一台机器接多个摄像头时用
- 云台/补光将来作为新子命令加（`snap` 之外的 `pan` / `light`），
  不影响现有调用方
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import glob
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

DEFAULT_DEVICE = "/dev/video0"
DEFAULT_SIZE = "1280x720"
# 实测：一次 ffmpeg 里抓 8 帧和抓 1 帧都是 ~1250ms —— 开设备与 UVC 预热
# 占了全部开销，多抓几帧是**免费的**。既然免费就多给挑帧一点余地：
# UVC 摄像头刚开机那几帧常常在对焦/调增益，糊的居多。
DEFAULT_FRAMES = 8
CAPTURE_TIMEOUT_SECONDS = 60
# 摄像头装歪时把画面转正。ffmpeg 的 transpose 滤镜没有 0 度这一档,
# 所以 0 走"不加滤镜"分支。
_TRANSPOSE = {
    90: "transpose=1",    # 顺时针 90
    180: "transpose=1,transpose=1",
    270: "transpose=2",   # 逆时针 90
}
DEVICE_WAIT_SECONDS = 90


class SnapError(RuntimeError):
    """取图失败。调用方会把它原样转给 AI，所以消息要写给人看。"""


@contextlib.contextmanager
def _device_lock(device: str):
    """让并发的抓帧排队，而不是互相撞掉。

    摄像头是**独占设备**：AI 手动要一张、快照页同时点了刷新，两个 ffmpeg
    撞上就是 "Device or resource busy"。锁只在 Linux 真的生效（fcntl），
    别的平台退化成不加锁 —— 这脚本本来只跑在 Pi 上，但导入做测试时不该炸。
    """
    try:
        import fcntl
    except ImportError:
        yield
        return
    path = pathlib.Path(tempfile.gettempdir()) / (
        "bw-camera-%s.lock" % device.strip("/").replace("/", "-"))
    handle = open(path, "w")
    deadline = time.time() + DEVICE_WAIT_SECONDS
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() >= deadline:
                    raise SnapError(
                        "等了 %.0f 秒摄像头也没空出来（另一个取图还在跑）"
                        % DEVICE_WAIT_SECONDS)
                time.sleep(0.5)
        yield
    finally:
        with contextlib.suppress(Exception):
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def capture_burst(
    directory: pathlib.Path,
    device: str = DEFAULT_DEVICE,
    frames: int = DEFAULT_FRAMES,
    size: str = DEFAULT_SIZE,
    rotate: int = 0,
) -> list[pathlib.Path]:
    directory.mkdir(parents=True, exist_ok=True)
    pattern = str(directory / "frame_%02d.jpg")
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "v4l2", "-input_format", "mjpeg",
        "-video_size", size, "-i", device,
        "-frames:v", str(frames),
    ]
    # 摄像头装歪了是固定属性（实测这台是侧装的，画面转了 90°）。在这里
    # 转正，是因为再往下每一个消费方 —— AI、快照页、将来的云台预览 ——
    # 都得各自补偿一次，而它们谁也不知道这台是怎么装的。
    if rotate:
        command += ["-vf", _TRANSPOSE[rotate]]
    command += ["-y", pattern]
    stderr = ""
    with _device_lock(device):
        # 即使前一个 ffmpeg 已经退出，UVC 驱动释放设备也要一会儿（实测过），
        # 所以拿到锁之后仍要对"忙"重试。
        for attempt in range(4):
            try:
                result = subprocess.run(
                    command, capture_output=True, text=True,
                    timeout=CAPTURE_TIMEOUT_SECONDS)
            except FileNotFoundError as error:
                raise SnapError("这台机器上没有 ffmpeg") from error
            except subprocess.TimeoutExpired as error:
                raise SnapError(
                    "抓帧超时（%d 秒）——摄像头卡住或被拔了"
                    % CAPTURE_TIMEOUT_SECONDS) from error
            shots = sorted(
                pathlib.Path(p)
                for p in glob.glob(pattern.replace("%02d", "*")))
            if shots:
                return shots
            stderr = (result.stderr or "ffmpeg 没有输出").strip()
            if "busy" not in stderr.lower():
                break
            time.sleep(1.5 * (attempt + 1))
    raise SnapError("一帧都没抓到：%s" % stderr[:300])


def score_frame(path: pathlib.Path) -> dict[str, float]:
    """清晰度（拉普拉斯方差）与亮度（灰度均值）。实测约 22ms/帧。

    清晰度用来挑帧；亮度用来**告诉调用方画面有多暗** —— 注意这里不做
    任何"太暗就不给"的判断：AI 自己看得见暗，替它下结论只会挡路。
    我们只负责把数字如实报上去。
    """
    import numpy
    from PIL import Image
    with Image.open(path) as image:
        gray = numpy.asarray(image.convert("L"), dtype=numpy.float32)
    laplacian = (
        gray[:-2, 1:-1] + gray[2:, 1:-1]
        + gray[1:-1, :-2] + gray[1:-1, 2:]
        - 4 * gray[1:-1, 1:-1]
    )
    return {
        "sharpness": float(laplacian.var()),
        "brightness": float(gray.mean()),
    }


def snap(
    device: str = DEFAULT_DEVICE,
    frames: int = DEFAULT_FRAMES,
    size: str = DEFAULT_SIZE,
    rotate: int = 0,
) -> dict:
    """抓一小段，挑最清楚的一帧交出去。"""
    workspace = pathlib.Path(tempfile.mkdtemp(prefix="bw-snap-"))
    try:
        started = time.time()
        shots = capture_burst(
            workspace, device=device, frames=frames, size=size,
            rotate=rotate)
        try:
            scored = [(path, score_frame(path)) for path in shots]
            best, quality = max(scored, key=lambda row: row[1]["sharpness"])
        except ImportError:
            # numpy/PIL 缺席不该让取图整个失败 —— 退化成"最后一帧"
            # （UVC 的前几帧通常还在对焦，最后一帧通常最好），并**说出来**，
            # 免得以为挑帧生效了其实没有。
            best, quality = shots[-1], {"sharpness": -1.0, "brightness": -1.0}
        data = best.read_bytes()
        width = height = 0
        with contextlib.suppress(Exception):
            from PIL import Image
            with Image.open(best) as image:
                width, height = image.size
        return {
            "ok": True,
            "capturedAtUtcMs": int(time.time() * 1000),
            "device": device,
            "requestedSize": size,
            "rotate": rotate,
            "width": width,
            "height": height,
            "frames": len(shots),
            "sharpness": round(quality["sharpness"], 1),
            "brightness": round(quality["brightness"], 1),
            "elapsedMs": int((time.time() - started) * 1000),
            "bytes": len(data),
            "jpegBase64": base64.b64encode(data).decode("ascii"),
        }
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")

    snap_command = sub.add_parser("snap", help="拍一张（默认子命令）")
    snap_command.add_argument("--device", default=DEFAULT_DEVICE)
    snap_command.add_argument("--size", default=DEFAULT_SIZE,
                              help="分辨率，如 1920x1080。本机支持的模式用 "
                                   "`v4l2-ctl --list-formats-ext` 查")
    snap_command.add_argument("--frames", type=int, default=DEFAULT_FRAMES)
    snap_command.add_argument("--rotate", type=int, default=0,
                              choices=(0, 90, 180, 270),
                              help="摄像头装歪时把画面转正（度，顺时针）")
    snap_command.add_argument("--out", default=None,
                              help="把 JPEG 直接写到这个文件（不走 base64）")

    sub.add_parser("modes", help="列出摄像头支持的分辨率")

    # 不给子命令时默认 snap —— 调用方最常做的就是这件事。
    argv = sys.argv[1:]
    if not argv or argv[0].startswith("-"):
        argv = ["snap"] + argv
    args = parser.parse_args(argv)

    try:
        if args.command == "modes":
            result = subprocess.run(
                ["v4l2-ctl", "--list-formats-ext", "-d", DEFAULT_DEVICE],
                capture_output=True, text=True, timeout=15)
            print(json.dumps(
                {"ok": True, "modes": result.stdout}, ensure_ascii=False))
            return 0

        payload = snap(device=args.device, frames=args.frames,
                       size=args.size, rotate=args.rotate)
        if args.out:
            pathlib.Path(args.out).write_bytes(
                base64.b64decode(payload.pop("jpegBase64")))
            payload["path"] = args.out
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    except SnapError as error:
        # 失败也走 JSON —— 调用方永远只需要解析一种形状，而且这段文字
        # 会被原样转给 AI，所以它得说人话。
        print(json.dumps({"ok": False, "error": str(error)},
                         ensure_ascii=False))
        return 2
    except Exception as error:  # noqa: BLE001
        print(json.dumps(
            {"ok": False, "error": "取图时出了意料之外的错：%s: %s"
                                   % (type(error).__name__, error)},
            ensure_ascii=False))
        return 3


if __name__ == "__main__":
    sys.exit(main())
