"""用本机 Qwen3.8-27B(llama.cpp)裁定危机段 — 语义层本地化。

═══ 为什么本地 ═══
2026-08-17 调研结论:血条这类**量化**任务 VLM 是公认弱项(VideoGameQA-Bench
UI 任务最好 40%),必须靠像素测量;但"谁打的、发生了什么、结果如何"是纯
**语义**任务,正是 VLM 的强项。之前用云端模型逐段裁定,成本随场次线性增长
且要联网;本机 4090 + Qwen3.8-27B(Q3_K_XL 12.5GB + mmproj 0.86GB)完全放得下,
零成本、零延迟顾虑、数据不出机。

用法:
  # 先起服务(桌面 llama 目录):
  #   llama-server.exe -m models/qwen3.8-q3/Qwen3.8-27B-UD-Q3_K_XL.gguf \
  #       --mmproj models/qwen3.8-q3/mmproj-F16.gguf -ngl 99 -c 8192 --port 8099
  python nightreign_local_verdict.py <session_dir> [--min-loss 150] [--limit N]

产物: <session>/refined/verdicts-local.json
"""

from __future__ import annotations

import argparse
import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "http://127.0.0.1:8099/v1/chat/completions"
VERDICT_CONTRACT = "nightreign-verdict-local/1"

SYSTEM = (
    "你在分析《艾尔登法环:黑夜君临(ELDEN RING NIGHTREIGN)》的游戏截图序列。"
    "画面 HUD:左上从上到下是玩家的 HP(橙红)/FP(蓝)/耐力(绿)三条,左侧是队友栏,"
    "屏幕下方居中的大血条是 Boss 或精英敌人并**带名字**。"
    "只描述你真正看到的东西;看不清就说看不清,不要编造。"
)

PROMPT = """《艾尔登法环:黑夜君临》一次玩家受伤事件的连续画面(方括号=相对首次受击的秒数)。

台账:持续 {dur:.1f} 秒,挨打 {drops} 次,损失约 {pct}% 血,段结束原因={ended}。

⚠ HUD 布局(务必分清,别把队友当敌人):
- 左上三条(橙/蓝/绿)= **玩家自己**的 HP/FP/耐力
- 左侧带头像的列表 = **队友**(Boochi、n1993r 这类玩家 ID),友方!
- 屏幕**下方居中**的横条 + 中文名 = **敌人**,这才是攻击者
- 敌人名只能从下方居中那条读,绝不能用队友 ID

输出 JSON(只输出 JSON):
{{"attacker":"敌人中文名(只从下方居中血条读;多条时选与玩家近身缠斗的那个)",
  "otherEnemies":"同时在场的其它敌人名",
  "source":"name-plate|visual-id|inferred|unknown",
  "confidence":"high|medium|low",
  "appearance":"外形:体型/颜色/材质/肢体/武器",
  "firstSeenAt":"第几秒首次看清它",
  "approach":"怎么进入战斗:方向、冲刺还是走近、距离变化",
  "attackMotion":"攻击动作分步:起手→中段→命中",
  "telegraph":"可辨识的攻击预兆",
  "effects":"伴随特效",
  "sceneContext":"地形环境",
  "playerResponse":"玩家当时在做什么、有没有躲",
  "counterplay":"应对建议",
  "outcome":"died|downed|survived|menu-or-other|unclear",
  "evidence":"判 outcome 的依据,指明哪一帧",
  "uncertain":"看不清只能推测的部分",
  "quality":"good|fair|poor"}}

判 outcome:"陷入濒死了"字样或紫色复活圈=downed;"夜渡失败"大字/画面变灰=died;
血条还有血=survived;画面是菜单/地图/加载=menu-or-other。
"""


def b64_image(path: Path, max_side: int | None = 1280) -> str:
    """max_side=None 表示**不缩放**。

    1280 是实测的清晰度拐点(读游戏内敌人名字):
        896px  → "负伤恶螳"(错)  1.2s
        1280px → "负伤恶魔"(对)  1.7s
        1920px → 对,但 3.6s,无额外收益
    另做过原生视频输入(llama-mtmd-cli --video)的对照:同为 1280px 时结论
    与静态帧**完全一致**,而视频要独占显存重载 13GB 模型(1-2 分钟) vs
    常驻 server 的 1.7 秒。故当前任务(读名字/判结果)不走视频路径;若将来
    要理解连续动作(boss 招式、操作序列),再回头评估视频。
    """
    from PIL import Image
    import io

    im = Image.open(path).convert("RGB")
    if max_side:
        im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode()


def ask(frames: list[tuple[float, Path]], meta: dict, timeout: float) -> dict:
    content: list[dict] = [{"type": "text", "text": PROMPT.format(**meta)}]
    # 名字条裁图先送且不缩放 —— 名字的权威来源,避免模型拿队友 ID 顶包
    for off, path in frames:
        plate = path.with_name(path.stem + "_plate.jpg")
        if plate.exists():
            content.append({"type": "text",
                            "text": "【敌人名字条(原尺寸,名字以此为准)】"})
            content.append({"type": "image_url", "image_url": {
                "url": f"data:image/jpeg;base64,{b64_image(plate, None)}"}})
            break
    for off, path in frames:
        content.append({"type": "text", "text": f"[{off:+.1f}s]"})
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64_image(path)}"},
        })

    body = json.dumps({
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": content}],
        "temperature": 0.2,
        "max_tokens": 1600,
        # Qwen3.8 默认 thinking:正式答案会跑进 reasoning_content 而 content 为空,
        # 且思考先把 token 预算吃光导致截断。语义裁定不需要长链思考,直接关掉。
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = urllib.request.Request(
        ENDPOINT, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        out = json.loads(resp.read())
    text = out["choices"][0]["message"]["content"]
    # 模型可能包在 ```json 里或带思考前缀:取第一个完整 JSON 对象
    start = text.find("{")
    if start < 0:
        return {"_raw": text, "_parse": "no-json"}
    depth, end = 0, None
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        return {"_raw": text, "_parse": "truncated"}
    try:
        return json.loads(text[start:end])
    except json.JSONDecodeError as exc:
        return {"_raw": text, "_parse": f"invalid: {exc}"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("session")
    ap.add_argument("--min-loss", type=int, default=150)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-frames", type=int, default=6)
    ap.add_argument("--timeout", type=float, default=300.0)
    a = ap.parse_args()

    sess = Path(a.session)
    work_path = sess / "refined" / "work.json"
    if not work_path.exists():
        import subprocess, sys as _s
        subprocess.run([_s.executable,
                        str(Path(__file__).with_name("nightreign_extract_evidence.py")),
                        str(sess), "--min-loss", str(a.min_loss)], check=True)
    work = json.loads(work_path.read_text("utf-8"))
    items = sorted(work["items"], key=lambda w: -w["lossPx"])
    if a.limit:
        items = items[: a.limit]
    print(f"裁定 {len(items)} 段(本机 Qwen3.8-27B)")

    full = max((w["pxBefore"] for w in work["items"]), default=1) or 1
    results = []
    for i, w in enumerate(items, 1):
        frames = [(f["offset"], Path(f["path"])) for f in w["frames"]]
        if len(frames) > a.max_frames:  # 均匀抽,保住首尾
            step = (len(frames) - 1) / (a.max_frames - 1)
            frames = [frames[round(k * step)] for k in range(a.max_frames)]
        meta = {"dur": w["durationMs"] / 1000, "drops": w["drops"],
                "pct": round(w["lossPx"] / full * 100), "ended": w["endedBy"]}
        t0 = time.monotonic()
        try:
            v = ask(frames, meta, a.timeout)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"  [{i}/{len(items)}] {w['id']} 失败: {exc}")
            v = {"_error": str(exc)}
        dt = time.monotonic() - t0
        results.append({"id": w["id"], "ts": w["ts"], "lossPx": w["lossPx"],
                        "frames": len(frames), "seconds": round(dt, 1), **v})
        print(f"  [{i}/{len(items)}] {w['id']} {dt:5.1f}s  "
              f"{str(v.get('attacker', v.get('_parse', v.get('_error','?'))))[:24]:24s} "
              f"{v.get('outcome','')}/{v.get('confidence','')}  "
              f"{str(v.get('appearance',''))[:30]}")

    out = sess / "refined" / "verdicts-local.json"
    out.write_text(json.dumps(
        {"contract": VERDICT_CONTRACT, "model": "Qwen3.8-27B-UD-Q3_K_XL",
         "items": results}, ensure_ascii=False, indent=2), "utf-8")
    print(f"\n→ {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
