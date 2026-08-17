"""信号自审计 — 让探针怀疑自己,把"需要人盯着试错"变成自动收敛。

═══ 为什么需要它 ═══
2026-08-17 的游戏探针试点里,测量层改了五版,每一版的缺陷都是**用户看出来**
的:中毒刷屏、回血才是分界、归零与消失没区分、填充色是开放集合、差异要求
连续性。而事后每一条我都能用**已有数据**验证或证伪 —— 答案一直在数据里,
只是没人去问。

这种"每遇到新问题就要人在旁边监视试错"的模式不可扩展。真正的缺陷不是
某个阈值,而是:**探针只会报告,不会怀疑自己**。

═══ 三条自审机制(都不需要人) ═══
① 物理不可能性:现实世界的量有变化率上限。血量不可能一帧内从满到空再回满;
   体重不可能一小时涨 10kg。超出上限的读数,先怀疑测量而不是相信现实。
② 交叉矛盾:同源的多个信号应当协同变化。血量突变而 FP/耐力纹丝不动,是
   测量口径出了问题的强信号。
③ 时序回弹:归零后立刻恢复、突降后立刻复原 —— 这类 A→B→A 的瞬时往返几乎
   都是渲染/遮挡假象,不是真实事件。

可疑读数不丢弃(它可能是真的),而是**打标 + 留证**,攒成审计队列交给 AI
批量看。人只需要看结论,不需要盯着过程。

═══ 通用性 ═══
这套机制不绑定游戏:任何"从连续信号里提取离散语义"的采集都适用 ——
摄像头→行为、屏幕→活动、传感器→状态、日志→故障。只需给出该量的物理
变化率上限和同源信号列表。
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


AUDIT_CONTRACT = "signal-audit/1"


@dataclass
class Suspicion:
    """一条可疑读数。kind 是机制类别,detail 给人和 AI 看的原因。"""

    kind: str
    detail: str
    value: float
    previous: float
    dt: float


@dataclass
class SelfDoubt:
    """读数自审器。喂进每次读数,吐出可疑原因(没有则 None)。

    参数都以"被测量的物理意义"表达,不是像素细节:
      max_rate_per_second: 该量每秒最大可能变化(占满量程比例)
      full_scale: 满量程读数(用于把绝对值换算成比例)
      rebound_window: 判定"瞬时往返"的时间窗(秒)
      rebound_ratio: 往返幅度达到量程该比例才算可疑
    """

    full_scale: float
    max_rate_per_second: float = 1.5
    rebound_window: float = 1.0
    rebound_ratio: float = 0.5
    history: deque = field(default_factory=lambda: deque(maxlen=64))
    peak: float = 0.0

    def check(
        self,
        value: float,
        ts: float,
        *,
        companions: dict[str, float] | None = None,
        companion_tolerance: float = 0.02,
    ) -> Suspicion | None:
        """喂一次读数。companions 是同源信号(用于交叉矛盾检测)。"""
        prev = self.history[-1] if self.history else None
        self.history.append((value, ts, dict(companions or {})))
        if prev is None:
            self.peak = max(self.peak, value)
            return None
        pv, pts, pc = prev
        dt = max(1e-3, ts - pts)
        scale = self.full_scale or 1.0
        rate = abs(value - pv) / scale / dt

        # ① 物理不可能性
        if rate > self.max_rate_per_second:
            return Suspicion(
                "impossible-rate",
                f"{dt*1000:.0f}ms 内变化 {abs(value-pv)/scale*100:.0f}% 量程"
                f"(上限 {self.max_rate_per_second*100:.0f}%/秒)",
                value, pv, dt,
            )

        # ② 交叉矛盾:主信号剧变而同源信号纹丝不动
        if companions and pc and abs(value - pv) / scale > 0.35:
            frozen = [
                k for k, v in companions.items()
                if k in pc and abs(v - pc[k]) <= abs(pc[k]) * companion_tolerance
            ]
            if frozen and len(frozen) == len(companions):
                return Suspicion(
                    "cross-conflict",
                    f"主信号变 {abs(value-pv)/scale*100:.0f}% 而同源信号"
                    f"{'/'.join(frozen)} 未动",
                    value, pv, dt,
                )

        # ③ 时序回弹:窗口内 A→B→A
        for old_v, old_ts, _ in reversed(self.history):
            if ts - old_ts > self.rebound_window:
                break
            if (abs(old_v - value) / scale < 0.05
                    and abs(old_v - pv) / scale >= self.rebound_ratio):
                return Suspicion(
                    "rebound",
                    f"{ts-old_ts:.1f}s 内 {old_v:.0f}→{pv:.0f}→{value:.0f} 往返",
                    value, pv, dt,
                )

        # 超过历史峰值也值得看一眼(量程变了?测量漂了?)
        if self.peak and value > self.peak * 1.15:
            self.peak = value
            return Suspicion(
                "above-peak", f"读数 {value:.0f} 超历史峰值 15%", value, pv, dt
            )
        self.peak = max(self.peak, value)
        return None


def write_audit_entry(
    audit_dir: Path, entry_id: str, suspicion: Suspicion, extra: dict[str, Any]
) -> None:
    """把一条可疑读数记进审计队列(不丢弃原读数,只是打标留证)。"""
    audit_dir.mkdir(parents=True, exist_ok=True)
    line = {
        "contract": AUDIT_CONTRACT,
        "id": entry_id,
        "kind": suspicion.kind,
        "detail": suspicion.detail,
        "value": suspicion.value,
        "previous": suspicion.previous,
        "dtMs": int(suspicion.dt * 1000),
        **extra,
    }
    with (audit_dir / "suspicions.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


def mine_extremes(
    rows: list[dict], metrics: dict[str, str], per_metric: int = 3
) -> list[dict]:
    """从一批样本里挖极端态,作为回归测试集。

    人工挑"典型案例"必然漏掉真正的坑 —— 坑都在分布的两端。metrics 形如
    {"字段名": "high"|"low"|"both"}。
    """
    picked: dict[int, dict] = {}
    for field_name, direction in metrics.items():
        usable = [r for r in rows if isinstance(r.get(field_name), (int, float))]
        if not usable:
            continue
        ordered = sorted(usable, key=lambda r: r[field_name])
        ends = []
        if direction in ("low", "both"):
            ends += [(r, f"{field_name} 最低") for r in ordered[:per_metric]]
        if direction in ("high", "both"):
            ends += [(r, f"{field_name} 最高") for r in ordered[-per_metric:]]
        for row, why in ends:
            key = id(row)
            if key in picked:
                picked[key]["_why"] += f" / {why}"
            else:
                picked[key] = {**row, "_why": why}
    return list(picked.values())


def self_test() -> int:
    d = SelfDoubt(full_scale=100.0, max_rate_per_second=1.5)
    assert d.check(100, 0.0) is None, "首个读数无从判断"
    assert d.check(98, 0.1) is None, "正常小变化"
    s = d.check(10, 0.2)
    assert s and s.kind == "impossible-rate", s   # 100ms 掉 88% 量程
    d2 = SelfDoubt(full_scale=100.0, max_rate_per_second=99.0)
    d2.check(100, 0.0)
    d2.check(20, 0.1)
    s2 = d2.check(100, 0.2)
    assert s2 and s2.kind == "rebound", s2        # 100→20→100 往返

    # 交叉矛盾:主信号腰斩而同源信号完全没动
    d3 = SelfDoubt(full_scale=100.0, max_rate_per_second=99.0, rebound_ratio=9.9)
    d3.check(100, 0.0, companions={"fp": 50.0, "st": 40.0})
    s3 = d3.check(40, 0.5, companions={"fp": 50.0, "st": 40.0})
    assert s3 and s3.kind == "cross-conflict", s3
    # 同源信号也在动 → 不算矛盾(真实战斗就是这样)
    d4 = SelfDoubt(full_scale=100.0, max_rate_per_second=99.0, rebound_ratio=9.9)
    d4.check(100, 0.0, companions={"fp": 50.0, "st": 40.0})
    assert d4.check(40, 0.5, companions={"fp": 30.0, "st": 12.0}) is None

    rows = [{"a": i, "b": -i} for i in range(10)]
    ex = mine_extremes(rows, {"a": "both", "b": "high"}, per_metric=2)
    vals = sorted(r["a"] for r in ex)
    # a 的两端 + b 最高(=a 最低,与前者重叠) → 同一行只出现一次,理由合并
    assert vals == [0, 1, 8, 9], vals
    assert any("/" in r["_why"] for r in ex), "重叠样本应合并入选理由"
    print("signal_audit self-test OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test())
