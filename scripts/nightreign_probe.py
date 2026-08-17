"""黑夜君临(Nightreign)HP 探针 — 游戏信号采集试点。

只采集不分析:掉血成段写台账 + 落盘证据序列,分析(AI 裁定)后置。

═══ 2026-08-17 第二版:证据质量重构(用户指出首场三类误判后) ═══
首场暴露的问题不是阈值不准,而是"给 AI 的证据本身没价值",三个根因:
 ① 只采 HP 一条 → "血条归零(濒死)"和"整片 HUD 消失/压暗(菜单/过场/加载)"
    读数相同。实测三态可分:濒死=HP 独降而 FP/耐力照旧;菜单=三条同比例
    压暗;加载=三条全灭。故改为三条同采 + 同步性判据。
 ② 只存事发单帧 → 受击瞬间必然是最糟的一帧:动态模糊 + 特效遮挡,而且
    大型敌人贴脸时占满屏反而无法辨认。改为环形缓冲落盘"证据序列",覆盖
    事发前 8 秒(敌人从远处接近、轮廓完整)到事发后 2 秒。
 ③ 前帧是 1 秒粒度的滚动缓存 → 追溯到的常是无关时刻。改为按精确时间点
    取帧,且每个点在邻域里挑"最清晰"的一张(拉普拉斯方差)。
通用教训(适用于任何大规模采集):采集不可重来,所以证据的可判读性必须在
采集时保证 —— 时间覆盖要够宽(单帧不够)、要留清晰度余量(挑不模糊的)、
要多信号交叉(单一信号的歧义无法事后消解)。

用法:
  python nightreign_probe.py --self-test   # 纯逻辑自检(不需要游戏)
  python nightreign_probe.py --probe-bars  # 打印当前三条读数(标定核对)
  python nightreign_probe.py               # 采集循环

产物(每场一个 session 目录):
  session.json  元数据 | samples.csv 三条完整曲线 | ledger.jsonl 事件台账
  evidence/<eventId>/  证据序列(多帧 JPEG + manifest.json 含清晰度/相对时刻)
"""

from __future__ import annotations

import argparse
import ctypes
import io
import json
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from signal_audit import SelfDoubt, write_audit_entry

CONFIG_CONTRACT = "nightreign-probe-config/2"
LEDGER_CONTRACT = "game-ledger/2"
STATE_ROOT = Path(r"C:\claude\state\game-ledger\nightreign")
CONFIG_PATH = STATE_ROOT / "probe-config.json"

DEFAULT_CONFIG: dict = {
    "contract": CONFIG_CONTRACT,
    # 三条状态条矩形(物理像素,从 HUD 放大图直读并逐态验证)
    "bars": {
        "hp": {"x": 334, "y": 84, "w": 1400, "h": 9},
        "fp": {"x": 334, "y": 110, "w": 800, "h": 10},
        "stamina": {"x": 334, "y": 140, "w": 800, "h": 12},
    },
    # HP 按饱和度量长度(见 measure_bar);阈值来自四态实测
    "bar": {"skipLeft": 20, "refCols": 50, "tol": 55, "run": 20,
            "satSolid": 30, "satRatio": 1.8},
    "cool": {"minLevel": 60, "dominance": 15},  # FP/耐力存在性(青/绿)
    "pollHz": 8,
    "dropThresholdCols": 6,
    "hitDropCols": 25,
    "episodeQuietSeconds": 10.0,
    "stabilityTicks": 2,
    "stabilityToleranceCols": 4,
    "epOpenCols": 16,
    "healCloseCols": 25,
    "commitLossCols": 40,
    "hudGoneThreshold": 4,  # FP+耐力填充 ≤ 此值 = HUD 整体消失(加载/黑屏)
    "hudConfirmTicks": 3,  # HUD 状态需连续几帧一致才切换(去抖)
    # 证据序列
    "frameRingHz": 2.5,
    "frameRingSeconds": 14.0,
    "frameDownscale": 2,  # 4K→1080p 步长降采样(见 run() 里的性能注记)
    "frameQuality": 72,
    "evidenceOffsets": [-8.0, -5.0, -3.0, -1.5, -0.6, 0.0, 1.0, 2.0],
    "evidenceWindow": 0.7,  # 每个时间点在 ±该秒内挑最清晰帧
    "evidenceTailSeconds": 2.5,  # 事发后等这么久再落盘(等尾帧进缓冲)
    # 自审计(2026-08-17):探针怀疑自己,把"要人盯着试错"变成自动收敛。
    # 阈值来自实测分布(P99=3.2 P99.9=18/秒),取 8 标记约 0.3% 样本 —— 值得
    # 看一眼的量级,不是"一定错"。可疑读数照常进事件流,只是额外留证送审。
    "auditMaxRatePerSecond": 8.0,
    "auditReboundWindow": 1.0,
    "auditMinIntervalSeconds": 6.0,
    "windowTitleContains": ["NIGHTREIGN", "黑夜君临"],
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _set_dpi_aware() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception as exc:
        print(f"[warn] DPI aware 设置失败(截屏坐标可能被缩放): {exc}")


def _foreground_title() -> str:
    user32 = ctypes.windll.user32
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(user32.GetForegroundWindow(), buf, 256)
    return buf.value


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n", "utf-8"
        )
        return dict(DEFAULT_CONFIG)
    value = json.loads(CONFIG_PATH.read_text("utf-8"))
    merged = dict(DEFAULT_CONFIG)
    if value.get("contract") == CONFIG_CONTRACT:
        merged.update(value)
    else:
        # v1 配置只有单条 bar:保留它作为 hp,其余用新默认(出声,不静默降级)
        print(f"[warn] 配置是旧版 {value.get('contract')},仅沿用 HP 矩形,其余用默认")
        if isinstance(value.get("bar"), dict):
            merged["bars"] = dict(merged["bars"])
            merged["bars"]["hp"] = {
                k: value["bar"][k] for k in ("x", "y", "w", "h") if k in value["bar"]
            }
        CONFIG_PATH.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2) + "\n", "utf-8"
        )
    return merged


# ── 纯逻辑(可自检) ────────────────────────────────────────────────────────────

def measure_bar(
    bgra: bytes, width: int, height: int, *,
    skip_left: int, ref_cols: int, tol: int, run: int,
    sat_solid: int, **_legacy,
) -> tuple[int, int, int]:
    """量血条,返回 (实血, 余像, 总填充) 三个列数。

    第五版(2026-08-17,用户提出"对比最左侧与其它位置的差异"后定型):
    填充色是**开放集合**(橙红/变粉/中毒/雨淋/虚血…数不完),空槽也不能锚
    (实测半透明,背景透过来:雪地 lum102、夜沼 lum45)。唯一闭合的不变量是
    **一段填充内部同色**,所以判据是"与最左端(必为填充)的差异从哪里开始"。

    要求**连续性**:实测"统计与参考一致的列数"会被空槽里偶然相似的列污染
    (三个案例分别高估 51/93/147 列),必须是"第一处连续 run 列偏离"。

    逐段推进拿到三个量:
      段1(锚最左) = 实血      —— 有饱和度,无论什么色相
      段2(锚段1右侧) = 余像    —— 去饱和白影,亮度高于背景;它是"最近损失"
      其余 = 空槽/背景
    濒死态最左端本身就是余像(低饱和),此时实血记 0,整段计入余像。

    真实帧回归:满血 346/0、濒死 0/377、变粉 476/0(旧版误读成 0 伪造暴击)、
    商店遮罩 394 与遮罩前 394 一致、恶魔王子 408 + 余像、倒地 26/460。
    """
    import numpy as np

    a = np.frombuffer(bgra, np.uint8).reshape(height, width, 4)[:, :, 2::-1]
    cols = a.astype(np.float32).mean(axis=0)

    def segment(start: int) -> tuple[int, np.ndarray]:
        """从 start 起量一段同色区,返回 (段末位置, 该段参考色)。"""
        ref = np.median(cols[start:start + ref_cols], axis=0)
        dist = np.abs(cols - ref).sum(axis=1)
        miss = 0
        for x in range(start + ref_cols, len(cols)):
            if dist[x] > tol:
                miss += 1
                if miss >= run:
                    return x - run + 1, ref
            else:
                miss = 0
        return len(cols), ref

    if skip_left + ref_cols >= len(cols):
        return 0, 0, 0
    end1, ref1 = segment(skip_left)
    seg1 = end1 - skip_left
    sat1 = float(ref1.max() - ref1.min())

    if sat1 < sat_solid:
        # 最左端已是去饱和白影 = 濒死/刚被打空,实血为 0
        return 0, seg1, seg1

    ghost = 0
    if end1 + ref_cols < len(cols):
        end2, ref2 = segment(end1)
        sat2 = float(ref2.max() - ref2.min())
        lum2 = float(ref2[0] * 0.299 + ref2[1] * 0.587 + ref2[2] * 0.114)
        # 余像 = 去饱和且够亮的白影;暗背景不算
        if sat2 < sat_solid and lum2 >= 70:
            ghost = end2 - end1
    return seg1, ghost, seg1 + ghost


def count_cool_pixels(bgra: bytes, min_level: int, dominance: int) -> int:
    """青/绿填充像素数(FP 与耐力条的颜色特征)。

    不用亮度:菜单遮罩自带亮色 UI,亮度反而高于濒死态,实测把三态判反了。
    """
    n = 0
    for i in range(0, len(bgra) - 3, 4):
        b, g, r = bgra[i], bgra[i + 1], bgra[i + 2]
        if (b >= min_level and b - r >= dominance) or (
            g >= min_level and g - r >= dominance
        ):
            n += 1
    return n


def classify_change(
    prev: int, cur: int, *, drop_threshold: int, **_ignored
) -> str | None:
    """HP 一次确认转换的归类。塌缩语义已移交 HudState(三条同步性)。"""
    if cur <= prev - drop_threshold:
        return "hp-drop"
    if cur >= prev + drop_threshold:
        return "hp-gain"
    return None


class StabilityFilter:
    """双帧确认:单帧闪烁(HUD 偶发不渲染)进不了事件层;慢漂逐帧通过。"""

    def __init__(self, need: int, tolerance: int) -> None:
        self.need = max(1, int(need))
        self.tol = int(tolerance)
        self.confirmed: int | None = None
        self.cand: int | None = None
        self.count = 0

    def feed(self, raw: int) -> tuple[int, int] | None:
        if self.cand is not None and abs(raw - self.cand) <= self.tol:
            self.count += 1
        else:
            self.count = 1
        self.cand = raw
        if self.count >= self.need:
            if self.confirmed is None:
                self.confirmed = raw
            elif raw != self.confirmed:
                prev, self.confirmed = self.confirmed, raw
                return (prev, raw)
        return None


class HudState:
    """判 HUD 是否整体消失。只做能做准的那一件事。

    实测(2026-08-17 四态对照)否掉了"按基线比例判遮罩"的设想:战斗中 FP/
    耐力本就被消耗,读数 1172-1812,与菜单遮罩的 1858 完全重叠 —— 像素层
    分不开"菜单压暗"和"低资源战斗"。但全灭是干净的:加载/黑屏恒为 0。
    故确定性层只判 gone;菜单/过场留给证据序列 + AI(首场 AI 正是靠画面
    正确认出了过场/训练场/装备菜单,它并不需要探针替它判)。
    三条读数如实写进事件与曲线,供分析层自行取用。
    """

    def __init__(self, gone_threshold: int = 4, confirm_ticks: int = 3) -> None:
        self.gone_threshold = int(gone_threshold)
        self.confirm = max(1, int(confirm_ticks))
        self.state = "on"
        self._cand = "on"
        self._count = 0

    def feed(self, fp_fill: int, st_fill: int) -> str:
        """需连续 confirm 帧一致才切换。首场 72 次转换里 28 次是 <1s 的来回抖
        (HUD 淡入淡出/伤害闪白的瞬间),不去抖会把台账淹掉。"""
        raw = "gone" if fp_fill + st_fill <= self.gone_threshold else "on"
        if raw == self._cand:
            self._count += 1
        else:
            self._cand, self._count = raw, 1
        if self._count >= self.confirm and self._cand != self.state:
            self.state = self._cand
        return self.state


class EpisodeTracker:
    """危机段:首次掉血开段,回血/塌缩/静默兜底关段。连招不重复开段。"""

    def __init__(self, quiet_seconds: float) -> None:
        self.quiet = float(quiet_seconds)
        self.active = False
        self.start_px = 0
        self.min_px = 0
        self.started = 0.0
        self.last_decrease = 0.0
        self.drops = 0

    def on_decrease(self, px_before: int, px_after: int, now_ts: float) -> bool:
        self.last_decrease = now_ts
        self.drops += 1
        if self.active:
            self.min_px = min(self.min_px, px_after)
            return False
        self.active = True
        self.start_px = px_before
        self.min_px = px_after
        self.started = now_ts
        self.drops = 1
        return True

    def quiet_elapsed(self, now_ts: float) -> bool:
        return self.active and (now_ts - self.last_decrease) >= self.quiet

    def close(self, cur_px: int, now_ts: float, ended_by: str) -> dict:
        summary = {
            "pxBefore": self.start_px,
            "pxAfter": cur_px,
            "pxMin": self.min_px,
            "lossPx": self.start_px - self.min_px,
            "durationMs": int((now_ts - self.started) * 1000),
            "drops": self.drops,
            "endedBy": ended_by,
        }
        self.active = False
        self.drops = 0
        return summary


def pick_evidence_frames(
    ring: list[tuple[float, bytes, float]],
    anchor_ts: float,
    offsets: list[float],
    window: float,
) -> list[dict]:
    """按相对时刻取帧,每点在 ±window 内挑最清晰的一张;同一帧不重复取。

    这是"证据可判读性"的核心:事发帧必然模糊且敌人贴脸,靠前几秒的帧才有
    完整轮廓,靠后的帧能看到结果(倒地提示/死亡画面)。
    """
    picked: list[dict] = []
    used: set[float] = set()
    for off in offsets:
        target = anchor_ts + off
        cands = [
            f for f in ring if abs(f[0] - target) <= window and f[0] not in used
        ]
        if not cands:
            continue
        best = max(cands, key=lambda f: f[2])
        used.add(best[0])
        picked.append(
            {"offset": round(best[0] - anchor_ts, 2), "sharpness": round(best[2], 1),
             "_ts": best[0], "_jpg": best[1]}
        )
    return picked


def self_test() -> int:
    assert count_cool_pixels(
        bytes([190, 150, 60, 255] + [80, 170, 60, 255] + [20, 20, 40, 255]), 60, 15
    ) == 2, "冷色只认青条和绿条"

    # measure_bar:三段分割(实血/余像/空槽),锚在填充段自身
    def bar(solid_n, ghost_n, slot_n, solid=(200, 60, 50)):
        r, g, b = solid
        return bytes(
            [b, g, r, 255] * solid_n            # 实血:有饱和度
            + [205, 200, 210, 255] * ghost_n    # 余像:去饱和白影
            + [40, 55, 90, 255] * slot_n        # 空槽:背景色(任意)
        )
    kw = dict(skip_left=2, ref_cols=6, tol=55, run=4, sat_solid=30)
    n = 40 - kw["skip_left"]  # 读数天然少 skip_left 列(那是边框装饰)
    assert measure_bar(bar(40, 0, 30), 70, 1, **kw) == (n, 0, n), "满血无余像"
    assert measure_bar(bar(0, 40, 30), 70, 1, **kw) == (0, n, n), "濒死:实血 0"
    hp, gh, tot = measure_bar(bar(30, 20, 30), 80, 1, **kw)
    assert hp == 30 - kw["skip_left"] and gh == 20 and tot == hp + gh, (hp, gh, tot)
    # 变色(粉紫/中毒绿/冰蓝)都不影响 —— 正是为这个开放集合设计
    for tint in ((210, 60, 190), (90, 200, 70), (70, 120, 210)):
        assert measure_bar(bar(40, 0, 30, tint), 70, 1, **kw)[0] == n, f"变色 {tint}"

    f = StabilityFilter(2, 8)
    assert f.feed(6932) is None and f.feed(6932) is None
    assert f.feed(0) is None and f.feed(6932) is None, "单帧闪烁不发布"
    assert f.feed(6932) is None and f.confirmed == 6932, "确认值未被闪烁污染"
    assert f.feed(4500) is None and f.feed(4500) == (6932, 4500), "站稳才发布"
    assert f.feed(4498) == (4500, 4498), "慢漂逐帧通过"

    h = HudState(4, 3)
    assert h.feed(3754, 3899) == "on", "濒死实测值:HUD 在"
    assert h.feed(1242, 616) == "on", "菜单遮罩:像素层判不出,不冒充判定"
    assert h.feed(1394, 0) == "on", "低资源战斗:同样只算在场"
    assert h.feed(0, 0) == "on" and h.feed(0, 0) == "on", "单帧全灭不切(去抖)"
    assert h.feed(0, 0) == "gone", "连续三帧全灭才认定 HUD 消失"
    assert h.feed(2000, 2000) == "gone", "回来也要连续三帧才切"
    assert h.feed(2000, 2000) == "gone" and h.feed(2000, 2000) == "on", "确认后恢复"

    e = EpisodeTracker(10.0)
    assert e.on_decrease(1000, 950, 10.0) is True, "首掉开段"
    assert e.on_decrease(950, 900, 10.5) is False, "连招不开新段"
    assert e.on_decrease(900, 750, 11.0) is False
    assert not e.quiet_elapsed(15.0) and e.quiet_elapsed(21.5)
    s = e.close(760, 21.5, "quiet")
    assert s["lossPx"] == 250 and s["pxMin"] == 750 and s["drops"] == 3, s
    assert e.on_decrease(700, 650, 30.0) is True, "关段后再掉血是新一轮"

    ring = [(100.0 + i * 0.33, b"", 10.0 + (i % 5)) for i in range(45)]
    picks = pick_evidence_frames(ring, 110.0, [-8.0, -3.0, 0.0, 2.0], 0.7)
    assert len(picks) == 4, picks
    assert [p["offset"] for p in picks] == sorted(p["offset"] for p in picks)
    assert all(abs(p["offset"] - o) <= 0.7 for p, o in zip(picks, [-8, -3, 0, 2]))
    assert len({p["_ts"] for p in picks}) == 4, "不该重复取同一帧"
    print("self-test OK")
    return 0


# ── 采集 ─────────────────────────────────────────────────────────────────────

def _sharpness(gray) -> float:
    """拉普拉斯方差:动态模糊帧显著偏低,用来在候选里挑可判读的一张。"""
    import numpy as np

    a = gray.astype("float32")
    lap = (
        -4 * a[1:-1, 1:-1] + a[:-2, 1:-1] + a[2:, 1:-1] + a[1:-1, :-2] + a[1:-1, 2:]
    )
    return float(np.var(lap))


def probe_bars_once() -> int:
    """打印当前三条读数,用于核对标定。"""
    import mss

    _set_dpi_aware()
    cfg = load_config()
    with mss.mss() as sct:
        for name, b in cfg["bars"].items():
            raw = bytes(
                sct.grab({"left": b["x"], "top": b["y"], "width": b["w"], "height": b["h"]}).raw
            )
            bc = cfg["bar"]
            fill, ghost, total = measure_bar(
                raw, b["w"], b["h"], skip_left=bc["skipLeft"],
                ref_cols=bc["refCols"], tol=bc["tol"], run=bc["run"],
                sat_solid=bc["satSolid"])
            print(f"{name:8s} 实血={fill:5d} 余像={ghost:5d} 总={total:5d}列  冷填充="
                  f"{count_cool_pixels(raw, cfg['cool']['minLevel'], cfg['cool']['dominance']):5d}")
    print(f"前台窗口: {_foreground_title()!r}")
    return 0


def run() -> int:
    import mss
    import numpy as np
    from PIL import Image

    _set_dpi_aware()
    cfg = load_config()
    bars = cfg["bars"]
    barcfg, cool = cfg["bar"], cfg["cool"]
    poll = 1.0 / float(cfg["pollHz"])
    title_keys = [str(k).upper() for k in cfg["windowTitleContains"]]

    session_dir = STATE_ROOT / datetime.now().strftime("%Y%m%d-%H%M%S")
    (session_dir / "evidence").mkdir(parents=True, exist_ok=True)
    ledger = (session_dir / "ledger.jsonl").open("a", encoding="utf-8")
    samples = (session_dir / "samples.csv").open("a", encoding="utf-8")
    samples.write("ts,hp_cols,ghost_cols,total_cols,fp,stamina,hud\n")
    (session_dir / "session.json").write_text(
        json.dumps(
            {"contract": LEDGER_CONTRACT, "game": "nightreign", "channel": "hud",
             "startedAt": _now_iso(), "config": cfg},
            ensure_ascii=False, indent=2),
        "utf-8")
    print(f"session: {session_dir}")

    seq = [0]

    def emit(kind: str, px_before: int, px_after: int, extra: dict | None = None) -> str:
        seq[0] += 1
        event_id = f"e{seq[0]:04d}"
        line = {
            "contract": LEDGER_CONTRACT, "eventId": event_id, "ts": _now_iso(),
            "game": "nightreign", "channel": "hud", "kind": kind,
            "pxBefore": px_before, "pxAfter": px_after, "delta": px_after - px_before,
        }
        if extra:
            line.update(extra)
        ledger.write(json.dumps(line, ensure_ascii=False) + "\n")
        ledger.flush()
        print(f"[{line['ts'][11:23]}] {event_id} {kind} {px_before}->{px_after}"
              + (f" {extra.get('endedBy','')}" if extra else ""))
        return event_id

    def save_evidence(event_id: str, anchor: float, ring_snapshot: list) -> None:
        picks = pick_evidence_frames(
            ring_snapshot, anchor, list(cfg["evidenceOffsets"]),
            float(cfg["evidenceWindow"]))
        if not picks:
            print(f"[warn] {event_id} 无可用证据帧(缓冲空?)")
            return
        d = session_dir / "evidence" / event_id
        d.mkdir(parents=True, exist_ok=True)
        manifest = []
        for i, p in enumerate(picks):
            name = f"{i:02d}_{p['offset']:+.1f}s.jpg"
            (d / name).write_bytes(p["_jpg"])
            manifest.append({"file": name, "offset": p["offset"],
                             "sharpness": p["sharpness"]})
        (d / "manifest.json").write_text(
            json.dumps({"eventId": event_id, "anchorTs": _now_iso(),
                        "frames": manifest}, ensure_ascii=False, indent=2), "utf-8")
        print(f"   └ 证据 {len(manifest)} 帧 → evidence/{event_id}")

    hit_px = int(cfg["hitDropCols"])
    ep_open_px = int(cfg["epOpenCols"])
    heal_close_px = int(cfg["healCloseCols"])
    commit_loss_px = int(cfg["commitLossCols"])
    episode = EpisodeTracker(float(cfg["episodeQuietSeconds"]))
    stability = StabilityFilter(int(cfg["stabilityTicks"]), int(cfg["stabilityToleranceCols"]))
    hud = HudState(int(cfg["hudGoneThreshold"]), int(cfg["hudConfirmTicks"]))

    ring: deque[tuple[float, bytes, float]] = deque(
        maxlen=int(float(cfg["frameRingHz"]) * float(cfg["frameRingSeconds"])) + 4)
    ring_interval = 1.0 / float(cfg["frameRingHz"])
    next_ring = 0.0
    pending_ev: list[dict] = []  # 等尾帧成熟后落盘
    ep_pending: dict | None = None
    doubt = SelfDoubt(
        full_scale=1.0,  # 首个满量程读数进来后校准
        max_rate_per_second=float(cfg["auditMaxRatePerSecond"]),
        rebound_window=float(cfg["auditReboundWindow"]),
    )
    audit_dir = session_dir / "audit"
    audit_n = [0]
    last_audit = 0.0
    game_front = None
    last_hud = "on"
    last_sample: tuple = ()
    heartbeat_at = time.monotonic()
    # 坐标健康检查(2026-08-17):全自动校准试过,实测会把大片天空误当状态条,
    # 且调研显示社区工具同样是"学一次缓存起来"。所以不假装能自动 —— 改为
    # 廉价检测坐标失效的稳定表征:读数长期贴死在 0 或轨道满格。
    health_window: deque[int] = deque(maxlen=240)
    health_warned = False

    rects = {n: {"left": b["x"], "top": b["y"], "width": b["w"], "height": b["h"]}
             for n, b in bars.items()}

    with mss.mss() as sct:
        mon = sct.monitors[1]
        try:
            while True:
                tick_start = time.monotonic()
                fg = _foreground_title().upper()
                front = any(k in fg for k in title_keys)
                if front != game_front:
                    game_front = front
                    print("采集中(游戏前台)" if front else "暂停(游戏不在前台)")
                if not front:
                    ring.clear()  # 切出去的画面不是证据
                    time.sleep(1.0)
                    continue

                now = time.monotonic()
                hp_rect = bars["hp"]
                hp_raw, hp_ghost, hp_total = measure_bar(
                    bytes(sct.grab(rects["hp"]).raw), hp_rect["w"], hp_rect["h"],
                    skip_left=barcfg["skipLeft"], ref_cols=barcfg["refCols"],
                    tol=barcfg["tol"], run=barcfg["run"],
                    sat_solid=barcfg["satSolid"])
                fp_lit = count_cool_pixels(
                    bytes(sct.grab(rects["fp"]).raw), cool["minLevel"], cool["dominance"])
                st_lit = count_cool_pixels(
                    bytes(sct.grab(rects["stamina"]).raw), cool["minLevel"], cool["dominance"])
                hud_state = hud.feed(fp_lit, st_lit)

                # 帧环形缓冲(比采样慢,只在游戏前台时填)
                if now >= next_ring:
                    next_ring = now + ring_interval
                    shot = sct.grab(mon)
                    # numpy 步长降采样 + BGRA→RGB:PIL 的 LANCZOS 缩放实测
                    # 47.8ms,这条路径 7.7ms 且分辨率更高(整帧 137→64ms)。
                    step = int(cfg["frameDownscale"])
                    arr = np.frombuffer(shot.raw, np.uint8).reshape(
                        shot.height, shot.width, 4)[::step, ::step, 2::-1]
                    im = Image.fromarray(arr)
                    buf = io.BytesIO()
                    im.save(buf, "JPEG", quality=int(cfg["frameQuality"]))
                    sharp = _sharpness(np.asarray(im.convert("L").resize((320, 180))))
                    ring.append((now, buf.getvalue(), sharp))

                # 自审计:物理不可能 / 交叉矛盾 / 时序回弹 → 打标留证送审
                doubt.full_scale = max(doubt.full_scale, float(hp_total), 1.0)
                sus = doubt.check(
                    float(hp_raw), now,
                    companions={"fp": float(fp_lit), "stamina": float(st_lit)})
                if sus is not None and now - last_audit >= cfg["auditMinIntervalSeconds"]:
                    last_audit = now
                    audit_n[0] += 1
                    aid = f"a{audit_n[0]:04d}"
                    write_audit_entry(
                        audit_dir, aid, sus,
                        {"ts": _now_iso(), "hud": hud_state, "ghostCols": hp_ghost,
                         "totalCols": hp_total, "fp": fp_lit, "stamina": st_lit})
                    pending_ev.append({"id": f"audit-{aid}", "anchor": now,
                                       "due": now + float(cfg["evidenceTailSeconds"])})
                    print(f"[audit] {aid} {sus.kind}: {sus.detail}")

                cur_sample = (hp_raw, hp_ghost // 8, fp_lit // 50, st_lit // 50, hud_state)
                if cur_sample != last_sample:
                    samples.write(
                        f"{_now_iso()},{hp_raw},{hp_ghost},{hp_total},"
                        f"{fp_lit},{st_lit},{hud_state}\n")
                    samples.flush()
                    last_sample = cur_sample

                # HUD 处境转换本身是事件(菜单/加载都在这里被正确归类,不再冒充死亡)
                if hud_state != last_hud:
                    emit(f"hud-{hud_state}", 0, 0, {"fp": fp_lit, "stamina": st_lit})
                    if hud_state == "gone" and episode.active:
                        s = episode.close(stability.confirmed or hp_raw, now, hud_state)
                        if ep_pending is None:
                            emit("episode-end", s["pxBefore"], s["pxAfter"], s)
                        else:
                            ep_pending = None
                    last_hud = hud_state

                # HUD 全灭时 HP 读数无意义,不进事件层(加载/黑屏类假死的根因)
                if hud_state == "on":
                    transition = stability.feed(hp_raw)
                    if transition is not None:
                        prev_c, cur_c = transition
                        kind = classify_change(
                            prev_c, cur_c, drop_threshold=int(cfg["dropThresholdCols"]))
                        if kind == "hp-drop":
                            delta_abs = prev_c - cur_c
                            was_active = episode.active
                            if was_active:
                                episode.on_decrease(prev_c, cur_c, now)
                            elif delta_abs >= ep_open_px:
                                episode.on_decrease(prev_c, cur_c, now)
                                ep_pending = {"anchor": now, "pxBefore": prev_c,
                                              "pxAfter": cur_c}
                            if (episode.active and ep_pending is not None
                                    and (episode.start_px - cur_c >= commit_loss_px
                                         or episode.drops >= 2)):
                                eid = emit("episode-start", ep_pending["pxBefore"],
                                           ep_pending["pxAfter"],
                                           {"fp": fp_lit, "stamina": st_lit,
                                            "ghostCols": hp_ghost,
                                            "totalCols": hp_total})
                                pending_ev.append(
                                    {"id": eid, "anchor": ep_pending["anchor"],
                                     "due": ep_pending["anchor"]
                                     + float(cfg["evidenceTailSeconds"])})
                                ep_pending = None
                            elif was_active and ep_pending is None and delta_abs >= hit_px:
                                emit("hp-drop", prev_c, cur_c)
                        elif kind == "hp-gain" and cur_c - prev_c >= heal_close_px:
                            if episode.active:
                                s = episode.close(prev_c, now, "heal")
                                if ep_pending is None:
                                    emit("episode-end", s["pxBefore"], prev_c, s)
                                else:
                                    ep_pending = None
                            emit("hp-gain", prev_c, cur_c)

                if episode.quiet_elapsed(now):
                    cur_c = stability.confirmed if stability.confirmed is not None else hp_raw
                    s = episode.close(cur_c, now, "quiet")
                    if ep_pending is None:
                        emit("episode-end", s["pxBefore"], cur_c, s)
                    else:
                        ep_pending = None

                # 尾帧成熟的证据落盘
                if pending_ev and pending_ev[0]["due"] <= now:
                    job = pending_ev.pop(0)
                    save_evidence(job["id"], job["anchor"], list(ring))

                health_window.append(hp_raw)
                if not health_warned and len(health_window) == health_window.maxlen:
                    track = bars["hp"]["w"]
                    if max(health_window) <= 2:
                        health_warned = True
                        print("[health] HP 读数长期为 0 —— 血条坐标可能已失效"
                              "(换了分辨率/窗口模式?)。跑 nightreign_calibrate_hud.py 重新标定。")
                    elif min(health_window) >= track * 0.95:
                        health_warned = True
                        print("[health] HP 读数长期贴满轨道 —— 矩形可能没对准血条。"
                              "跑 nightreign_calibrate_hud.py 重新标定。")

                if now - heartbeat_at >= 120.0:
                    heartbeat_at = now
                    print(f"[heartbeat] hp={hp_raw}列 余像={hp_ghost} 总={hp_total} "
                          f"fp={fp_lit} st={st_lit} "
                          f"hud={hud_state} 缓冲={len(ring)}帧")

                elapsed = time.monotonic() - tick_start
                if elapsed < poll:
                    time.sleep(poll - elapsed)
        except KeyboardInterrupt:
            print("停止采集")
        finally:
            for job in pending_ev:
                save_evidence(job["id"], job["anchor"], list(ring))
            meta = json.loads((session_dir / "session.json").read_text("utf-8"))
            meta["endedAt"] = _now_iso()
            (session_dir / "session.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")
            ledger.close()
            samples.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--probe-bars", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if args.probe_bars:
        return probe_bars_once()
    return run()


if __name__ == "__main__":
    sys.exit(main())
