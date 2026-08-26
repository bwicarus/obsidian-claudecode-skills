#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""摄像头取图（传感器层，Linux 与 Windows 共用一份）。

职责只有一件：**拍一张能看的照片，交出去**。它不判断画面内容，也不知道
调用方要拿它干什么 —— 判断归 AI（它本来就能看图）。

这样分工是用户 2026-08-27 定的：「只是给 codex 那边的 ai 提供一个获取他
想要的资讯的手段罢了，也就是说目前只需要给他获取图像的一个接口」。

**为什么一份而不是两份**：摄像头现在同时挂在 Pi（v4l2）和这台 Windows
（DirectShow）上，之后还会更多。两个平台真正不同的只有"怎么打开设备"
这一句 ffmpeg 参数；挑帧、转正、信封格式、错误措辞全都一样。拆成两份
的话，这些共同部分会各自慢慢漂移，而漂移的那天没有任何地方会报错。

输出是一个 JSON 信封（图片走 base64）。用信封而不是裸二进制，是因为
调用方可能在另一台机器上，中间隔着 ssh 和 subprocess 两层管道，裸二进制
被文本模式改一个字节就得到一张坏图、而且**看起来像成功**。多出来的
33% 体积在一张 30KB 的图上不值一提。

留好的扩展位（用户说后面会换带云台、补光的高清摄像头）：
- `--size`   已经通着，换更高分辨率不用改形状
- `--rotate` 摄像头装歪时转正（每台的固有属性）
- 云台/补光将来作为新子命令加（`snap` 之外的 `pan` / `light`），
  不影响现有调用方
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import glob
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

WINDOWS = sys.platform == "win32"
DEFAULT_SIZE = "1280x720"
# 实测两个平台同一个结论：**开设备的开销占了全部**，多抓几帧几乎免费。
#   Pi   /dev/video0 ：1 帧 1254ms，10 帧 1254ms
#   本机 C920 (dshow)：1 帧 3323ms， 8 帧 3932ms
# 既然免费就多给挑帧一点余地 —— UVC 摄像头刚开机那几帧常常还在对焦或
# 调增益，糊的居多。
DEFAULT_FRAMES = 8
CAPTURE_TIMEOUT_SECONDS = 90
DEVICE_WAIT_SECONDS = 120
# 摄像头装歪时把画面转正。ffmpeg 的 transpose 没有 0 度这一档，
# 所以 0 走"不加滤镜"分支。
_TRANSPOSE = {
    90: "transpose=1",    # 顺时针 90
    180: "transpose=1,transpose=1",
    270: "transpose=2",   # 逆时针 90
}
# winget 装的 ffmpeg 不一定在服务进程的 PATH 里。找不到时按这里兜底 ——
# 兜底失败要说清是"没装"还是"装了但没找到"。
_FFMPEG_FALLBACKS = [
    pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet"
    / "Packages"
    / "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
    / "ffmpeg-9.0-full_build" / "bin" / "ffmpeg.exe",
]


class SnapError(RuntimeError):
    """取图失败。消息会一路转到 AI 和用户面前，所以要说人话。"""


def ffmpeg_path() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in _FFMPEG_FALLBACKS:
        if candidate.is_file():
            return str(candidate)
    raise SnapError(
        "这台机器上找不到 ffmpeg（PATH 里没有，常见安装位置也没有）")


# ── 设备 ──

def _device_input_arguments(device: str) -> list[str]:
    """把设备名翻译成 ffmpeg 的输入参数。**两个平台唯一真正的差别。**"""
    if WINDOWS:
        # dshow 认两种写法：友好名 video=<名字>，或 @device_pnp_... 全名。
        # 全名唯一但难写，友好名好写但可能重名 —— 两种都放行，由登记表定。
        source = device if device.startswith("@device") else "video=" + device
        return ["-f", "dshow", "-i", source]
    return ["-f", "v4l2", "-input_format", "mjpeg", "-i", device]


def list_devices() -> list[dict]:
    """这台机器上有哪些摄像头。用于登记新摄像头时对照名字。"""
    if WINDOWS:
        result = subprocess.run(
            [ffmpeg_path(), "-hide_banner", "-list_devices", "true",
             "-f", "dshow", "-i", "dummy"],
            capture_output=True, text=True, timeout=60)
        # ffmpeg 把设备列表打在 stderr 上，且**退出码非 0**（它确实没能
        # 打开叫 dummy 的设备）—— 这里不能按退出码判失败。
        rows = []
        for line in (result.stderr or "").splitlines():
            match = re.search(r'"([^"]+)"\s*\(video\)', line)
            if match:
                rows.append({"device": match.group(1), "backend": "dshow"})
        return rows
    rows = []
    for node in sorted(glob.glob("/dev/video*")):
        label = node
        try:
            info = subprocess.run(
                ["v4l2-ctl", "-d", node, "--info"],
                capture_output=True, text=True, timeout=10)
            match = re.search(r"Card type\s*:\s*(.+)", info.stdout or "")
            if match:
                label = match.group(1).strip()
        except (OSError, subprocess.SubprocessError):
            pass
        rows.append({"device": node, "label": label, "backend": "v4l2"})
    return rows


@contextlib.contextmanager
def _device_lock(device: str):
    """让并发的抓帧排队，而不是互相撞掉。

    摄像头是**独占设备**：AI 手动要一张、快照页同时点了刷新，两个 ffmpeg
    撞上就是"设备忙"。用 O_EXCL 锁文件而不是 fcntl —— 后者在 Windows 上
    没有，而现在两个平台都真的会抓帧。持锁进程崩了留下的陈锁按 mtime
    超时强夺，否则一次崩溃就把这台摄像头永久锁死。
    """
    safe = re.sub(r"[^A-Za-z0-9]+", "-", device)[:60] or "camera"
    path = pathlib.Path(tempfile.gettempdir()) / ("bw-camera-%s.lock" % safe)
    deadline = time.time() + DEVICE_WAIT_SECONDS
    while True:
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(handle)
            break
        except FileExistsError:
            with contextlib.suppress(OSError):
                if time.time() - path.stat().st_mtime > CAPTURE_TIMEOUT_SECONDS:
                    path.unlink(missing_ok=True)
                    continue
            if time.time() >= deadline:
                raise SnapError(
                    "等了 %.0f 秒摄像头也没空出来（另一个取图还在跑）"
                    % DEVICE_WAIT_SECONDS)
            time.sleep(0.4)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


def capture_burst(
    directory: pathlib.Path,
    device: str,
    frames: int = DEFAULT_FRAMES,
    size: str = DEFAULT_SIZE,
    rotate: int = 0,
) -> list[pathlib.Path]:
    directory.mkdir(parents=True, exist_ok=True)
    pattern = str(directory / "frame_%02d.jpg")
    command = [ffmpeg_path(), "-hide_banner", "-loglevel", "error"]
    if size:
        command += ["-video_size", size]
    command += _device_input_arguments(device)
    command += ["-frames:v", str(frames)]
    # 摄像头装歪了是每台固有的属性。在这里转正，是因为再往下每一个消费方
    # —— AI、快照页、将来的云台预览 —— 都得各自补偿一次，而它们谁也不
    # 知道这台是怎么装的。
    if rotate:
        command += ["-vf", _TRANSPOSE[rotate]]
    command += ["-y", pattern]

    stderr = ""
    with _device_lock(device):
        # 即使前一个 ffmpeg 已经退出，驱动释放设备也要一会儿（两个平台
        # 都实测到过），所以拿到锁之后仍要对"忙"重试。
        for attempt in range(4):
            try:
                result = subprocess.run(
                    command, capture_output=True, text=True,
                    timeout=CAPTURE_TIMEOUT_SECONDS)
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
            lowered = stderr.lower()
            if "busy" not in lowered and "in use" not in lowered:
                break
            time.sleep(1.5 * (attempt + 1))
    raise SnapError("一帧都没抓到：%s" % stderr[:300])


def score_frame(path: pathlib.Path) -> dict[str, float]:
    """清晰度（拉普拉斯方差）与亮度（灰度均值）。实测约 22ms/帧。

    清晰度用来挑帧；亮度用来**告诉调用方画面有多暗**。注意这里不做任何
    "太暗就不给"的判断：AI 自己看得见暗，替它下结论只会挡路。我们只负责
    把数字如实报上去。
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
    device: str,
    frames: int = DEFAULT_FRAMES,
    size: str = DEFAULT_SIZE,
    rotate: int = 0,
) -> dict:
    """抓一小段，挑最清楚的一帧交出去。"""
    workspace = pathlib.Path(tempfile.mkdtemp(prefix="bw-snap-"))
    try:
        started = time.time()
        shots = capture_burst(
            workspace, device=device, frames=frames, size=size, rotate=rotate)
        picked = "sharpest"
        try:
            scored = [(path, score_frame(path)) for path in shots]
            best, quality = max(scored, key=lambda row: row[1]["sharpness"])
        except ImportError:
            # numpy/PIL 缺席不该让取图整个失败 —— 退化成"最后一帧"（UVC
            # 的前几帧通常还在对焦）。**必须说出来**：否则调用方会以为
            # 挑帧生效了，而 sharpness 那个字段其实是编的。
            best, quality = shots[-1], {}
            picked = "last-frame-no-numpy"
        data = best.read_bytes()
        width = height = 0
        with contextlib.suppress(Exception):
            from PIL import Image
            with Image.open(best) as image:
                width, height = image.size
        payload = {
            "ok": True,
            "capturedAtUtcMs": int(time.time() * 1000),
            "device": device,
            "backend": "dshow" if WINDOWS else "v4l2",
            "requestedSize": size,
            "rotate": rotate,
            "width": width,
            "height": height,
            "frames": len(shots),
            "picked": picked,
            "elapsedMs": int((time.time() - started) * 1000),
            "bytes": len(data),
            "jpegBase64": base64.b64encode(data).decode("ascii"),
        }
        # 量不出来就**不给这两个字段**，而不是塞一个 -1 让下游去猜。
        if quality:
            payload["sharpness"] = round(quality["sharpness"], 1)
            payload["brightness"] = round(quality["brightness"], 1)
        return payload
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def _default_device() -> str:
    return "" if WINDOWS else "/dev/video0"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")

    snap_command = sub.add_parser("snap", help="拍一张（默认子命令）")
    snap_command.add_argument("--device", default=_default_device(),
                              help="Linux 是 /dev/videoN；Windows 是 dshow "
                                   "设备名（用 list 子命令查）")
    snap_command.add_argument("--size", default=DEFAULT_SIZE,
                              help="分辨率，如 1920x1080")
    snap_command.add_argument("--frames", type=int, default=DEFAULT_FRAMES)
    snap_command.add_argument("--rotate", type=int, default=0,
                              choices=(0, 90, 180, 270),
                              help="摄像头装歪时把画面转正（度，顺时针）")
    snap_command.add_argument("--out", default=None,
                              help="把 JPEG 直接写到这个文件（不走 base64）")

    sub.add_parser("list", help="列出这台机器上的摄像头")

    argv = sys.argv[1:]
    if not argv or argv[0].startswith("-"):
        argv = ["snap"] + argv   # 最常做的就是拍一张
    args = parser.parse_args(argv)

    try:
        if args.command == "list":
            print(json.dumps(
                {"ok": True, "devices": list_devices()}, ensure_ascii=False))
            return 0
        if not args.device:
            raise SnapError(
                "没给 --device。这台机器上有：%s"
                % json.dumps(list_devices(), ensure_ascii=False))
        payload = snap(device=args.device, frames=args.frames,
                       size=args.size, rotate=args.rotate)
        if args.out:
            pathlib.Path(args.out).write_bytes(
                base64.b64decode(payload.pop("jpegBase64")))
            payload["path"] = args.out
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    except SnapError as error:
        # 失败也走 JSON —— 调用方永远只需要解析一种形状，而这段文字会被
        # 原样转给 AI，所以它得说人话。
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
