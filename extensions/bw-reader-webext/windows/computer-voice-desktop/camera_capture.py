# -*- coding: utf-8 -*-
"""摄像头取图（Windows 侧）—— AI 要看一眼现场时走这里。

用户 2026-08-27 定的形状：

- 摄像头有的挂在别的机器上（Pi），有的就接在这台 Windows 上。无论哪种，
  **图像最终都落到 Windows 本地**，于是 AI 在本地跑任务时直接读文件就
  能看见。
- 快照页会有一个「摄像头」tab，但那**只是给用户看的显示口**。
  ⚠ 图像绝不进快照载荷 —— AI 不该每取一次快照就被塞一张照片，
  它只在自己决定要看的时候来拍。这条是用户明说的。
- 摄像头是通用测试设备，不是为某个场景（垃圾桶）造的。这里不做任何
  画面判断，判断归 AI。

**给 AI 的用法**（也写在 AGENTS.md 里）::

    python %LOCALAPPDATA%\\BWReader\\camera_capture.py snap pi

打印一行 JSON，`path` 就是刚拍的照片。读它即可。

留好的扩展位（用户说后面会换带云台、补光的高清摄像头）：

- 摄像头登记在 `camera-sources.json`，一台一条，`kind` 决定怎么取图。
  现在有 `local`（就接在这台机器上）和 `ssh`（在别的机器上，ssh 过去拍）；
  将来的网络摄像头加一个 `http` kind 即可，调用方那句 `snap <id>` 一个字
  都不用改。两条路跑的是**同一个取图脚本**（scripts/camera_snap.py），
  只有"怎么打开设备"那一句不同。
- `--size` 已经通到底层，换高分辨率不用改形状。
- 云台/补光将来作为新子命令（`pan` / `light`），与 `snap` 平级。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SOURCES_FILE_NAME = "camera-sources.json"
FRAMES_DIR_NAME = "camera"
SOURCES_CONTRACT = "camera-sources/1"
# 每台摄像头留最近多少张。够回看"刚才那下是什么"，又不会慢慢吃满盘。
KEEP_FRAMES = 20
SSH_TIMEOUT_SECONDS = 120
# 本机 dshow 打开设备就要 ~3.3 秒（实测），比 Pi 的 v4l2 慢不少。
LOCAL_TIMEOUT_SECONDS = 120

# 第一台摄像头。找不到登记文件时按这个建，省得用户手写 JSON。
_DEFAULT_SOURCES = [
    # 摄像头装歪时把 rotate 填 90/180/270 —— 在取图那一层转正，下游
    # （AI、快照页）就都不必各自补偿。装正了就留 0。
    # label 按位置改比按型号好（"玄关""厨房"），等角度定下来再改。
    {
        "id": "pi",
        "label": "Pi 摄像头",
        "kind": "ssh",
        "host": "pi",
        "script": "/home/bwicarus/claude/scripts/camera_snap.py",
        "device": "/dev/video0",
        "size": "1280x720",
        "rotate": 0,
    },
    {
        "id": "c920",
        "label": "C920（外接）",
        "kind": "local",
        "device": "HD Pro Webcam C920",
        "size": "1280x720",
        "rotate": 0,
    },
    # ⚠ 下面两台是机身自带的（ASUS ROG Flow Z13），2026-08-27 实测**出全黑帧**。
    # 排查过程记在这里，免得以后再走一遍：设备在、驱动 ProblemCode=0、
    # Windows 隐私策略三级全 Allow、Frame Server 在跑；ffmpeg 的 dshow 直接
    # I/O error，OpenCV 的 dshow 后端能打开、能出帧，但**亮度恒为 0 且每帧
    # 读取要 1 秒**（超时）；MSMF 后端打得开读不出。同一时刻外接的 C920 一切正常。
    # 内外之别而非 API 之别 —— 是 ASUS 机身摄像头被硬件/固件级关掉了
    # （管这个开关的 ArmouryCrateControlInterface / ASUSOptimization 服务当时都是停的）。
    # 软件侧绕不过去。开关打开后它们应当自动可用，不需要改这里。
    {
        "id": "builtin",
        "label": "机身后置（13MP）",
        "kind": "local",
        "device": "OV13B10",
        "size": "1280x720",
        "rotate": 0,
    },
    {
        "id": "usb5m",
        "label": "机身前置（5MP）",
        "kind": "local",
        "device": "USB2.0 5M UVC WebCam",
        "size": "1280x720",
        "rotate": 0,
    },
]


class CameraError(RuntimeError):
    """取图失败。消息会原样转给 AI 和快照页，所以要说人话。"""


def default_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "BWReader"


def sources_path(root: Path | None = None) -> Path:
    return (root or default_root()) / SOURCES_FILE_NAME


def load_sources(root: Path | None = None) -> list[dict[str, Any]]:
    """读摄像头登记表；没有就按默认建一份。

    ⚠ 文件坏了要**报错**而不是悄悄退回默认值 —— 用户可能刚在里面登记了
    第二台摄像头，静默重置会让那台凭空消失，而且哪儿都不会说。
    """
    path = sources_path(root)
    if not path.is_file():
        payload = {"contract": SOURCES_CONTRACT, "sources": _DEFAULT_SOURCES}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1),
            encoding="utf-8")
        return list(_DEFAULT_SOURCES)
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as error:
        raise CameraError(
            "摄像头登记表 JSON 坏了（%s），拒绝静默重置：%s" % (path, error)
        ) from error
    if not isinstance(value, dict) or not isinstance(value.get("sources"), list):
        raise CameraError("摄像头登记表 contract 不符：%s" % path)
    return value["sources"]


def find_source(camera_id: str, root: Path | None = None) -> dict[str, Any]:
    sources = load_sources(root)
    for source in sources:
        if str(source.get("id")) == camera_id:
            return source
    known = "、".join(str(s.get("id")) for s in sources) or "一台都没有"
    raise CameraError(
        "没有叫「%s」的摄像头（已登记：%s）" % (camera_id, known))


def frames_dir(camera_id: str, root: Path | None = None) -> Path:
    return (root or default_root()) / FRAMES_DIR_NAME / camera_id


def _prune(directory: Path) -> None:
    frames = sorted(directory.glob("*.jpg"))
    for stale in frames[:-KEEP_FRAMES]:
        try:
            stale.unlink()
        except OSError:
            pass


def _capture_ssh(source: dict[str, Any], size: str | None) -> dict:
    """ssh 到摄像头所在的机器跑取图脚本，拿回 JSON 信封。

    ⚠ ssh 把 argv **拼成一条字符串交给远端 shell**。这里的参数目前都来自
    登记表（不是用户自由文本），但登记表将来会被 AI 或用户编辑 ——
    逐个 shlex.quote，别留这个口子。
    """
    host = str(source.get("host") or "")
    script = str(source.get("script") or "")
    if not host or not script:
        raise CameraError(
            "摄像头「%s」的登记缺 host 或 script" % source.get("id"))
    remote_argv = ["python3", script, "snap",
                   "--device", str(source.get("device") or "/dev/video0"),
                   "--size", str(size or source.get("size") or "1280x720")]
    rotate = int(source.get("rotate") or 0)
    if rotate:
        remote_argv += ["--rotate", str(rotate)]
    remote = " ".join(shlex.quote(part) for part in remote_argv)
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
               host, remote]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True,
            timeout=SSH_TIMEOUT_SECONDS)
    except FileNotFoundError as error:
        raise CameraError("这台机器上没有 ssh") from error
    except subprocess.TimeoutExpired as error:
        raise CameraError(
            "连「%s」取图超时（%d 秒）——那台机器睡了或网络断了"
            % (host, SSH_TIMEOUT_SECONDS)) from error

    return _parse_envelope(
        result.stdout, result.stderr, result.returncode, "「%s」" % host)


def _snap_script_path() -> Path:
    """取图脚本（与 Pi 上是同一份源码，见 scripts/camera_snap.py）。

    ⚠ 一份而不是两份：两个平台真正不同的只有"怎么打开设备"那一句 ffmpeg
    参数，挑帧、转正、信封格式、错误措辞全都一样。拆开的话这些共同部分
    会各自慢慢漂移，而漂移那天没有任何地方会报错。
    """
    return default_root() / "camera_snap.py"


def _capture_local(source: dict[str, Any], size: str | None) -> dict:
    """这台机器上直接接着的摄像头（Windows DirectShow）。

    与 ssh 那条路唯一的区别是不出网 —— 跑的是同一个脚本、拿的是同一种
    信封，所以下游一个字都不用改。
    """
    script = _snap_script_path()
    if not script.is_file():
        raise CameraError(
            "找不到取图脚本 %s（桌面端还没部署过？）" % script)
    device = str(source.get("device") or "")
    if not device:
        raise CameraError(
            "摄像头「%s」的登记缺 device" % source.get("id"))
    argv = [sys.executable, str(script), "snap",
            "--device", device,
            "--size", str(size or source.get("size") or "1280x720")]
    rotate = int(source.get("rotate") or 0)
    if rotate:
        argv += ["--rotate", str(rotate)]
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8",
            timeout=LOCAL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        raise CameraError(
            "本机取图超时（%d 秒）——摄像头被别的程序占着？"
            % LOCAL_TIMEOUT_SECONDS) from error
    return _parse_envelope(
        result.stdout, result.stderr, result.returncode, "本机")


def _parse_envelope(
    stdout: str | None, stderr: str | None, code: int, who: str
) -> dict:
    """两条取图路共用的信封解析。**失败必须带上原因** —— 这段文字是
    AI 唯一能告诉用户"为什么没拍到"的依据。"""
    text = (stdout or "").strip()
    if not text:
        raise CameraError(
            "%s取图没有任何输出（退出码 %s）：%s"
            % (who, code, (stderr or "").strip()[:300]))
    try:
        payload = json.loads(text.splitlines()[-1])
    except json.JSONDecodeError as error:
        raise CameraError(
            "%s取图的输出不是 JSON：%s" % (who, text[:300])) from error
    if not payload.get("ok"):
        raise CameraError(str(payload.get("error") or "取图失败但没说原因"))
    return payload


_CAPTURERS = {
    "ssh": _capture_ssh,
    "local": _capture_local,
    # 将来的网络摄像头挂这里：{"kind": "http", "url": ...} → _capture_http。
    # 调用方那句 snap <id> 一个字都不用改。
}


def snap(
    camera_id: str, size: str | None = None, root: Path | None = None
) -> dict[str, Any]:
    """拍一张，落到本地磁盘，返回带 path 的元数据。"""
    source = find_source(camera_id, root)
    kind = str(source.get("kind") or "")
    capturer = _CAPTURERS.get(kind)
    if capturer is None:
        raise CameraError(
            "不认识的摄像头类型「%s」（支持：%s）"
            % (kind, "、".join(_CAPTURERS)))

    payload = capturer(source, size)
    encoded = payload.pop("jpegBase64", None)
    if not encoded:
        raise CameraError("对方没有回图片数据")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as error:
        raise CameraError("图片数据解不开（传输中被改动了？）") from error
    if not data.startswith(b"\xff\xd8"):
        # 拿到的不是 JPEG。不检查的话会写下一个坏文件，然后 AI 打开时
        # 得到一个跟"摄像头没插"完全不同却同样没用的错误。
        raise CameraError("拿回来的不是 JPEG（开头 %r）" % data[:8])

    directory = frames_dir(camera_id, root)
    directory.mkdir(parents=True, exist_ok=True)
    captured_ms = int(payload.get("capturedAtUtcMs") or time.time() * 1000)
    frame_path = directory / ("%d.jpg" % captured_ms)
    frame_path.write_bytes(data)
    _prune(directory)

    meta = dict(payload)
    meta.update({
        "id": camera_id,
        "label": source.get("label") or camera_id,
        # 花名册随每次拍照带回。AI 没有别的途径知道有哪几台、各自对着
        # 哪儿，而这个名单会变（用户会调角度、改名字、加摄像头）——
        # 与其在说明书里写一份会过期的清单，不如每次如实报当前的。
        "cameras": [
            {"id": str(s.get("id")), "label": s.get("label") or s.get("id")}
            for s in load_sources(root)
        ],
        "path": str(frame_path),
        "bytes": len(data),
        "capturedAtUtcMs": captured_ms,
    })
    (directory / "latest.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return meta


def latest(camera_id: str, root: Path | None = None) -> dict[str, Any] | None:
    """最近一张的元数据；从没拍过就是 None（不是错误）。"""
    path = frames_dir(camera_id, root) / "latest.json"
    if not path.is_file():
        return None
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not Path(str(meta.get("path") or "")).is_file():
        # 元数据在但图没了（被清理/被删）。当成没有，别让调用方拿到一个
        # 指向空气的路径。
        return None
    meta["ageSeconds"] = int(
        time.time() - int(meta.get("capturedAtUtcMs") or 0) / 1000)
    return meta


def probe_all(root: Path | None = None) -> list[dict[str, Any]]:
    """逐台试拍，报告谁能用谁不能用。

    ⚠ 这是这套东西的**诊断出口**（silent-failure-lessons 规则4：新功能
    落地时诊断出口必须已在）。摄像头会被拔、被别的程序占、被系统的隐私
    策略挡 —— 没有这条命令，表现就只是"AI 说它看不到"，而没人知道是哪
    一环。实测就用它发现了两台设备**能被枚举但读不出**。
    """
    rows = []
    for source in load_sources(root):
        camera_id = str(source.get("id"))
        row = {
            "id": camera_id,
            "label": source.get("label") or camera_id,
            "kind": source.get("kind"),
            "device": source.get("device"),
        }
        started = time.time()
        try:
            meta = snap(camera_id, root=root)
            row["ok"] = True
            row["elapsedMs"] = int((time.time() - started) * 1000)
            for key in ("width", "height", "brightness", "sharpness"):
                if key in meta:
                    row[key] = meta[key]
        except CameraError as error:
            row["ok"] = False
            row["error"] = str(error)[:300]
        rows.append(row)
    return rows


def describe_all(root: Path | None = None) -> list[dict[str, Any]]:
    """给快照页用：每台摄像头 + 它最近一张的情况。"""
    rows = []
    for source in load_sources(root):
        camera_id = str(source.get("id"))
        rows.append({
            "id": camera_id,
            "label": source.get("label") or camera_id,
            "kind": source.get("kind"),
            "host": source.get("host"),
            "latest": latest(camera_id, root),
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=None,
                        help="数据根目录（默认 %%LOCALAPPDATA%%\\BWReader）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="列出已登记的摄像头及各自最近一张")
    sub.add_parser("probe", help="逐台试拍，看谁能用（诊断用）")

    snap_command = sub.add_parser("snap", help="拍一张，打印本地路径")
    snap_command.add_argument("id", nargs="?", default="pi")
    snap_command.add_argument("--size", default=None,
                              help="分辨率，如 1920x1080（不给用登记表里的）")

    latest_command = sub.add_parser(
        "latest", help="最近一张的路径（不重新拍）")
    latest_command.add_argument("id", nargs="?", default="pi")

    args = parser.parse_args()
    root = Path(args.root) if args.root else None

    try:
        if args.command == "list":
            print(json.dumps(
                {"ok": True, "cameras": describe_all(root)},
                ensure_ascii=False, indent=1))
        elif args.command == "probe":
            print(json.dumps({"ok": True, "cameras": probe_all(root)},
                             ensure_ascii=False, indent=1))
        elif args.command == "snap":
            print(json.dumps(
                {"ok": True, **snap(args.id, size=args.size, root=root)},
                ensure_ascii=False))
        else:
            meta = latest(args.id, root)
            print(json.dumps(
                {"ok": True, "latest": meta} if meta else
                {"ok": False, "error": "「%s」还没拍过（先跑 snap）" % args.id},
                ensure_ascii=False))
            return 0 if meta else 2
        return 0
    except CameraError as error:
        print(json.dumps({"ok": False, "error": str(error)},
                         ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
