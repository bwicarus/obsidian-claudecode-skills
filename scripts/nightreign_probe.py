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

CONFIG_CONTRACT = "nightreign-probe-config/2"
LEDGER_CONTRACT = "game-ledger/2"
STATE_ROOT = Path(r"C:\claude\state\game-ledger\nightreign")
CONFIG_PATH = STATE_ROOT / "probe-config.json"

DEFAULT_CONFIG: dict = {
    "contract": CONFIG_CONTRACT,
    # 三条状态条矩形(物理像素,从 HUD 放大图直读并逐态验证)
    "bars": {
        "hp": {"x": 334, "y": 84, "w": 800, "h": 9},
        "fp": {"x": 334, "y": 110, "w": 800, "h": 10},
        "stamina": {"x": 334, "y": 140, "w": 800, "h": 12},
    },
    "red": {"rMin": 90, "dominance": 25},  # HP 填充判定
    "cool": {"minLevel": 60, "dominance": 15},  # FP/耐力填充判定(青/绿)
    "pollHz": 8,
    "dropThresholdPx": 4,
    "hitDropPx": 80,
    "episodeQuietSeconds": 10.0,
    "stabilityTicks": 2,
    "stabilityTolerancePx": 8,
    "epOpenPx": 40,
    "healClosePx": 80,
    "commitLossPx": 150,
    "hudGoneThreshold": 4,  # FP+耐力填充 ≤ 此值 = HUD 整体消失(加载/黑屏)
    # 证据序列
    "frameRingHz": 2.5,
    "frameRingSeconds": 14.0,
    "frameDownscale": 2,  # 4K→1080p 步长降采样(见 run() 里的性能注记)
    "frameQuality": 72,
    "evidenceOffsets": [-8.0, -5.0, -3.0, -1.5, -0.6, 0.0, 1.0, 2.0],
    "evidenceWindow": 0.7,  # 每个时间点在 ±该秒内挑最清晰帧
    "evidenceTailSeconds": 2.5,  # 事发后等这么久再落盘(等尾帧进缓冲)
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

def count_fill_pixels(bgra: bytes, r_min: int, dominance: int) -> int:
    n = 0
    for i in range(0, len(bgra) - 3, 4):
        b, g, r = bgra[i], bgra[i + 1], bgra[i + 2]
        if r >= r_min and (r - (g if g > b else b)) >= dominance:
            n += 1
    return n


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

    def __init__(self, gone_threshold: int = 4) -> None:
        self.gone_threshold = int(gone_threshold)

    def feed(self, fp_fill: int, st_fill: int) -> str:
        return "gone" if fp_fill + st_fill <= self.gone_threshold else "on"


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
    # BGRA 序:红填充 ×2 / 浅黄残影 / 暗背景 / FP 青 / 耐力绿
    px = bytes(
        [0, 0, 200, 255] * 2
        + [140, 200, 220, 255]
        + [20, 20, 40, 255]
        + [190, 150, 60, 255]
        + [80, 170, 60, 255]
    )
    assert count_fill_pixels(px, 90, 25) == 2, "HP 红填充只认前两个"
    assert count_cool_pixels(px, 60, 15) == 2, "冷色只认青条和绿条"
    kw = dict(drop_threshold=4)
    assert classify_change(100, 97, **kw) is None, "小抖动不该报事件"
    assert classify_change(100, 90, **kw) == "hp-drop"
    assert classify_change(90, 100, **kw) == "hp-gain"

    f = StabilityFilter(2, 8)
    assert f.feed(6932) is None and f.feed(6932) is None
    assert f.feed(0) is None and f.feed(6932) is None, "单帧闪烁不发布"
    assert f.feed(6932) is None and f.confirmed == 6932, "确认值未被闪烁污染"
    assert f.feed(4500) is None and f.feed(4500) == (6932, 4500), "站稳才发布"
    assert f.feed(4498) == (4500, 4498), "慢漂逐帧通过"

    h = HudState(4)
    assert h.feed(3754, 3899) == "on", "濒死实测值:HUD 在"
    assert h.feed(1242, 616) == "on", "菜单遮罩:像素层判不出,不冒充判定"
    assert h.feed(1394, 0) == "on", "低资源战斗:同样只算在场"
    assert h.feed(0, 0) == "gone", "加载黑屏:全灭(唯一能判准的一态)"

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
            print(
                f"{name:8s} 红填充={count_fill_pixels(raw, cfg['red']['rMin'], cfg['red']['dominance']):5d}"
                f"  冷填充={count_cool_pixels(raw, cfg['cool']['minLevel'], cfg['cool']['dominance']):5d}"
            )
    print(f"前台窗口: {_foreground_title()!r}")
    return 0


def run() -> int:
    import mss
    import numpy as np
    from PIL import Image

    _set_dpi_aware()
    cfg = load_config()
    bars = cfg["bars"]
    red, cool = cfg["red"], cfg["cool"]
    poll = 1.0 / float(cfg["pollHz"])
    title_keys = [str(k).upper() for k in cfg["windowTitleContains"]]

    session_dir = STATE_ROOT / datetime.now().strftime("%Y%m%d-%H%M%S")
    (session_dir / "evidence").mkdir(parents=True, exist_ok=True)
    ledger = (session_dir / "ledger.jsonl").open("a", encoding="utf-8")
    samples = (session_dir / "samples.csv").open("a", encoding="utf-8")
    samples.write("ts,hp,fp_lit,st_lit,hud\n")
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

    hit_px = int(cfg["hitDropPx"])
    ep_open_px = int(cfg["epOpenPx"])
    heal_close_px = int(cfg["healClosePx"])
    commit_loss_px = int(cfg["commitLossPx"])
    episode = EpisodeTracker(float(cfg["episodeQuietSeconds"]))
    stability = StabilityFilter(int(cfg["stabilityTicks"]), int(cfg["stabilityTolerancePx"]))
    hud = HudState(int(cfg["hudGoneThreshold"]))

    ring: deque[tuple[float, bytes, float]] = deque(
        maxlen=int(float(cfg["frameRingHz"]) * float(cfg["frameRingSeconds"])) + 4)
    ring_interval = 1.0 / float(cfg["frameRingHz"])
    next_ring = 0.0
    pending_ev: list[dict] = []  # 等尾帧成熟后落盘
    ep_pending: dict | None = None
    game_front = None
    last_hud = "on"
    last_sample: tuple = ()
    heartbeat_at = time.monotonic()

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
                hp_raw = count_fill_pixels(
                    bytes(sct.grab(rects["hp"]).raw), red["rMin"], red["dominance"])
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

                cur_sample = (hp_raw, fp_lit // 50, st_lit // 50, hud_state)
                if cur_sample != last_sample:
                    samples.write(f"{_now_iso()},{hp_raw},{fp_lit},{st_lit},{hud_state}\n")
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
                            prev_c, cur_c, drop_threshold=int(cfg["dropThresholdPx"]))
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
                                           {"fp": fp_lit, "stamina": st_lit})
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

                if now - heartbeat_at >= 120.0:
                    heartbeat_at = now
                    print(f"[heartbeat] hp={hp_raw} fp={fp_lit} st={st_lit} "
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
