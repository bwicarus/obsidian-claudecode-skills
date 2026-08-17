"""把运动"画"进帧里 — 让 VLM 看得见时间。

═══ 为什么(2026 年最有价值的一条视频理解改进)═══
CVPR 2026 的 SpookyBench 做了个极端实验:信息**只**编码在时序里(逐帧看是
噪声)。结果:人类准确率 >98%,**所有 VLM 全是 0%** —— 包括 GPT-4o 和 Gemini。

但同一篇论文的缓解实验给出了转折:用经典 Farneback 光流算出 motion
boundary、**叠加到帧上**再喂同一个模型:
    Qwen2-VL-7B   0% → 51.54%
    GPT-4o        0% → 59.10%

结论很硬:**不是模型不行,是运动信息从来没有到达模型**。而修复方式不是换
架构、不是换模型 —— 是改渲染。这也解释了我们反复撞到的"看不出是抓取还是
横扫":那是运动信息,静态帧里根本不存在。

═══ 三种渲染(可组合)═══
  flow    Farneback 光流 → 运动幅度上色叠加(论文验证过的方案)
  rgb     t-Δ/t/t+Δ 三帧灰度塞进 R/G/B 通道 —— 静止物体呈灰,运动物体出彩色
          拖影,零依赖、一眼看出"谁在动、往哪动"
  diff    帧差热力图(最轻,只标"哪里变了")

用法:
  python motion_overlay.py PREV.jpg CUR.jpg OUT.jpg [--mode flow|rgb|diff]
  python motion_overlay.py --dir FRAME_DIR   # 对目录里的连续帧批量生成
"""

from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np


def _read(path: Path) -> np.ndarray:
    img = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"读不了 {path}")
    return img


def _write(path: Path, img: np.ndarray) -> None:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise RuntimeError(f"编码失败 {path}")
    buf.tofile(str(path))


def flow_overlay(prev: np.ndarray, cur: np.ndarray, *,
                 alpha: float = 0.75, min_mag: float = 2.5,
                 compensate_camera: bool = True) -> np.ndarray:
    """Farneback 光流 → 运动幅度上色后叠加(论文验证过的做法)。

    ⚠ 游戏视频特有的坑(2026-08-17 实测):**镜头本身在动**。玩家转视角时
    全画面都有位移,直接叠光流会把整屏染色,局部的怪物动作反被淹没 —— 论文
    用的是固定摄像头素材,游戏不是。故默认做**全局运动补偿**:取光流的中位数
    当作镜头位移减掉,剩下的才是物体相对运动。
    """
    g0 = cv2.cvtColor(prev, cv2.COLOR_BGR2GRAY)
    g1 = cv2.cvtColor(cur, cv2.COLOR_BGR2GRAY)
    flow = cv2.calcOpticalFlowFarneback(
        g0, g1, None, pyr_scale=0.5, levels=3, winsize=21,
        iterations=3, poly_n=5, poly_sigma=1.2, flags=0)
    if compensate_camera:
        # 中位数比均值稳:画面里少数快速物体不会带偏对镜头位移的估计
        flow = flow - np.median(flow.reshape(-1, 2), axis=0)
    mag, ang = cv2.cartToPolar(flow[..., 0], flow[..., 1])
    hsv = np.zeros_like(cur)
    hsv[..., 0] = (ang * 180 / np.pi / 2).astype(np.uint8)      # 方向→色相
    hsv[..., 1] = 255
    # 幅度按分位裁剪再归一,避免个别极值把其余运动压成黑色
    hi = float(np.percentile(mag, 99.0)) or 1.0
    hsv[..., 2] = np.clip(mag / hi * 255, 0, 255).astype(np.uint8)
    colored = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
    # 只叠"动得明显"的区域,且边缘羽化,避免整屏染色淹没画面内容
    mask = cv2.GaussianBlur((mag > min_mag).astype(np.float32), (9, 9), 0)
    mask = np.clip(mask, 0, 1)[..., None]
    return (cur * (1 - mask * alpha) + colored * (mask * alpha)).astype(np.uint8)


def rgb_stack(before: np.ndarray, cur: np.ndarray,
              after: np.ndarray) -> np.ndarray:
    """三帧灰度塞进 B/G/R 通道:静止=灰,运动=彩色拖影。零依赖、最直观。"""
    shape = cur.shape[:2][::-1]
    g = [cv2.cvtColor(cv2.resize(x, shape), cv2.COLOR_BGR2GRAY)
         for x in (before, cur, after)]
    return cv2.merge([g[0], g[1], g[2]])


def diff_heat(prev: np.ndarray, cur: np.ndarray, *,
              alpha: float = 0.5) -> np.ndarray:
    """帧差热力图叠加(最轻,只回答"哪里变了")。"""
    d = cv2.absdiff(cv2.cvtColor(prev, cv2.COLOR_BGR2GRAY),
                    cv2.cvtColor(cur, cv2.COLOR_BGR2GRAY))
    d = cv2.GaussianBlur(d, (7, 7), 0)
    heat = cv2.applyColorMap(cv2.normalize(d, None, 0, 255, cv2.NORM_MINMAX),
                             cv2.COLORMAP_JET)
    mask = (d > 12).astype(np.float32)[..., None]
    return (cur * (1 - mask * alpha) + heat * (mask * alpha)).astype(np.uint8)


def render(mode: str, frames: list[Path], out: Path) -> Path:
    imgs = [_read(f) for f in frames]
    if mode == "rgb":
        if len(imgs) < 3:
            imgs = [imgs[0]] + imgs + [imgs[-1]]
        result = rgb_stack(imgs[0], imgs[len(imgs) // 2], imgs[-1])
    elif mode == "diff":
        result = diff_heat(imgs[0], imgs[-1])
    else:
        result = flow_overlay(imgs[0], imgs[-1])
    _write(out, result)
    return out


def batch_dir(frame_dir: Path, mode: str) -> int:
    """对目录里按名字排序的连续帧,两两生成运动增强图到 motion/。"""
    frames = sorted(f for f in frame_dir.glob("*.jpg")
                    if "_plate" not in f.name and "_motion" not in f.name)
    if len(frames) < 2:
        print(f"{frame_dir} 帧不足")
        return 1
    out_dir = frame_dir / "motion"
    out_dir.mkdir(exist_ok=True)
    made = 0
    for i in range(1, len(frames)):
        window = ([frames[i - 1], frames[i], frames[min(i + 1, len(frames) - 1)]]
                  if mode == "rgb" else [frames[i - 1], frames[i]])
        out = out_dir / f"{frames[i].stem}_motion.jpg"
        try:
            render(mode, window, out)
            made += 1
        except RuntimeError as exc:
            print(f"[warn] {frames[i].name}: {exc}")
    print(f"生成 {made} 张运动增强帧 → {out_dir}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("frames", nargs="*")
    ap.add_argument("--dir")
    ap.add_argument("--mode", default="flow", choices=("flow", "rgb", "diff"))
    a = ap.parse_args()
    if a.dir:
        return batch_dir(Path(a.dir), a.mode)
    if len(a.frames) < 3:
        raise SystemExit("需要 PREV CUR OUT 三个参数,或用 --dir")
    render(a.mode, [Path(a.frames[0]), Path(a.frames[1])], Path(a.frames[2]))
    print(f"→ {a.frames[2]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
