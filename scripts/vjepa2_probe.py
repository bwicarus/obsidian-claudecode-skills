"""V-JEPA 2 可行性验证 — 它能不能"读懂运动"?

═══ 为什么试这个 ═══
2026-08-17 调研结论:能说话的模型(Qwen3-VL/Gemini/llama.cpp)全是逐帧编码,
**架构上没有运动通道**(SpookyBench 上所有 VLM 得 0 分)。而 V-JEPA 2 用
3D tubelet tokenization,16 帧联合编码,时间是第一类维度 —— 它在 SSv2
(专测"从左推到右"vs"从右推到左"这种纯运动语义)拿到 77.3%。

代价:它**不会说话**,只输出 embedding。所以正确用法是"它做感知、VLM 做
表达",跟我们已有的"血条测 when、VLM 解释 why"是同一个结构。

═══ 这个脚本验证什么 ═══
不做分类训练,只做**最基础的证伪测试**:同一段视频正放 vs 倒放,它的
embedding 会不会不同?

  · 如果 **几乎相同** → 它跟 VLM 一样对时间方向无感,这条路白走
  · 如果 **明显不同** → 它确实编码了时间方向,值得投入标注

这是最便宜的判据:不需要标注、不需要训练、一次前向就能出结论。
对照组是同一段视频抽两批不同的帧(内容相同、顺序相同)——它们的距离
是"噪声地板",正倒放的距离必须显著高于这个地板才算数。

用法:
  python vjepa2_probe.py --frames DIR              # 用一批帧
  python vjepa2_probe.py --session SESS --event e0155
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

MODEL_ID = "facebook/vjepa2-vitl-fpc64-256"   # ViT-L,16GB 可跑;ViT-g 需量化
FRAMES_PER_CLIP = 16


def load_frames(paths: list[Path], size: int = 256):
    import numpy as np
    from PIL import Image

    out = []
    for p in paths:
        im = Image.open(p).convert("RGB")
        # 中心裁成正方形再缩放,避免拉伸改变运动方向的表观
        w, h = im.size
        s = min(w, h)
        im = im.crop(((w - s) // 2, (h - s) // 2, (w + s) // 2, (h + s) // 2))
        out.append(np.asarray(im.resize((size, size))))
    return np.stack(out)


def embed(model, processor, frames, device):
    """一段 clip → 一个向量(对所有 patch token 做平均池化)。"""
    import torch

    inputs = processor(frames, return_tensors="pt").to(device)
    with torch.no_grad():
        out = model.get_vision_features(**inputs)
    return out.mean(dim=1).squeeze(0).float().cpu()


def cosine(a, b) -> float:
    import torch

    return float(torch.nn.functional.cosine_similarity(a, b, dim=0))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--frames", help="含连续帧的目录")
    ap.add_argument("--session")
    ap.add_argument("--event")
    ap.add_argument("--device", default="auto")
    a = ap.parse_args()

    if a.session:
        d = Path(a.session) / "evidence" / (a.event or "")
        cand = d / "all"
        src = cand if cand.is_dir() else d
    elif a.frames:
        src = Path(a.frames)
    else:
        raise SystemExit("需要 --frames 或 --session/--event")

    files = sorted(f for f in src.glob("*.jpg") if "_plate" not in f.name)
    if len(files) < FRAMES_PER_CLIP:
        print(f"[warn] 只有 {len(files)} 帧,不足 {FRAMES_PER_CLIP};将重复采样补齐")
        if not files:
            raise SystemExit(f"{src} 里没有帧")
        files = (files * ((FRAMES_PER_CLIP // len(files)) + 1))[:FRAMES_PER_CLIP]
    else:
        # 均匀取 16 帧,覆盖整段
        step = (len(files) - 1) / (FRAMES_PER_CLIP - 1)
        files = [files[round(i * step)] for i in range(FRAMES_PER_CLIP)]

    import torch
    from transformers import AutoModel, AutoVideoProcessor

    device = (a.device if a.device != "auto"
              else ("cuda" if torch.cuda.is_available() else "cpu"))
    print(f"设备 {device} | 模型 {MODEL_ID}")
    print(f"素材 {src}  取 {len(files)} 帧: "
          f"{files[0].name} … {files[-1].name}")

    model = AutoModel.from_pretrained(
        MODEL_ID, torch_dtype=torch.float16 if device == "cuda" else torch.float32
    ).to(device).eval()
    processor = AutoVideoProcessor.from_pretrained(MODEL_ID)

    fwd = load_frames(files)
    rev = fwd[::-1].copy()
    # 噪声地板:同内容同顺序,只是像素级微扰(重编码抖动的量级)
    import numpy as np
    jitter = np.clip(fwd.astype(np.int16) + 2, 0, 255).astype(np.uint8)

    e_fwd = embed(model, processor, fwd, device)
    e_rev = embed(model, processor, rev, device)
    e_jit = embed(model, processor, jitter, device)

    sim_rev = cosine(e_fwd, e_rev)
    sim_floor = cosine(e_fwd, e_jit)
    print("\n" + "=" * 56)
    print(f"正放 vs 倒放      余弦相似度 = {sim_rev:.4f}")
    print(f"正放 vs 微扰(地板) 余弦相似度 = {sim_floor:.4f}")
    gap = sim_floor - sim_rev
    print(f"差距 = {gap:.4f}")
    print("=" * 56)
    if gap > 0.05:
        print("✓ 倒放明显改变了表示 —— 它确实编码了时间方向,值得投入")
    elif gap > 0.01:
        print("△ 有差异但不大 —— 可能素材本身运动不显著,换一段重测")
    else:
        print("✗ 倒放几乎不改变表示 —— 对这批素材它读不出时间方向")
    return 0


if __name__ == "__main__":
    sys.exit(main())
