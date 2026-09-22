#!/usr/bin/env python3
"""Laya 定型判断服务 —— 非自回归的 System 1 决策，本机一次前向，不烧 AI 额度。

Laya（convaiinnovations/laya）对任意 state 回答 choice / score / noul 三类
**定型问题**，不生成文本，所以没有解析和幻觉这两类问题；概率经 RLCD 校准，
可以按阈值自动放行 / 降级给 AI。

在独立 venv 里跑（Windows：C:\\claude\\laya-venv），被主项目脚本 subprocess 调用 ——
主 Python 装着整套项目依赖，往里塞 torch 风险太大，跟 spacy-venv 同一手法。

⚠ 它的定位是**闸，不是替代**：先验极度偏斜的判断（几百个候选里命中 0~3 个）
交给它先筛，只把拿不准的送给 AI。直接拿它替掉 AI 的判断会悄悄丢召回，
而丢了什么没有地方看得见 —— 见 references/evidence-quality-lessons.md。

用法：
    # 常驻（推荐）：启动即加载模型，stdin 每行一个请求
    python scripts/laya_decide.py --server [--device cuda|cpu] [--model english|multilingual|typed-decisions]
    # 单次（调试用，每次都要付模型加载的钱）
    echo '{"state":{...},"questions":{...}}' | python scripts/laya_decide.py

常驻协议（跟 spacy_parse.py --server 逐字同构，便于两处共用调用方那套锁/超时自愈）：
    启动后先回 {"ready":true,...}
    之后 stdin 每行一个 JSON：{"state":..., "questions":..., "model":可选}
    每行回一个 JSON + flush；空行跳过；EOF/管道断即退出。
"""
from __future__ import annotations

import json
import sys
import time

_ROUTER = None
_AGENTS: dict[str, object] = {}
_DEVICE = "cuda"


def _arg(name: str, default: str) -> str:
    """--name value / --name=value 都认。"""
    for i, a in enumerate(sys.argv):
        if a == name and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return default


def _load(device: str, preload: bool) -> None:
    """建 Router 并（可选）预载检查点。

    ⚠ 必须 preload。Laya 默认 max_loaded=1，语言一换就重建模型 ——
    它自己 README 实测：CPU 中位 7.4s、T4 10.3s。常驻服务不预载等于白常驻。
    """
    global _ROUTER, _DEVICE
    _DEVICE = device
    from laya import Router  # 延迟导入：没装时错误信息更清楚
    _ROUTER = Router(preload=preload, device=device) if preload else Router(device=device)


def decide(state, questions, model: str | None = None) -> dict:
    if _ROUTER is None:
        raise RuntimeError("模型未加载")
    began = time.perf_counter()
    result = _ROUTER.predict(state, questions, model=model) if model else _ROUTER.predict(state, questions)
    out = dict(result) if isinstance(result, dict) else {"answers": result}
    out["elapsed_ms"] = round((time.perf_counter() - began) * 1000, 2)
    return out


def serve(device: str) -> None:
    began = time.perf_counter()
    try:
        _load(device, preload=True)
    except Exception as ex:
        # ⚠ 出声。加载失败时一声不响地等 stdin，调用方只会看到"没反应"，
        #   而真实原因（venv 没装好 / 没显存 / 没网拉不到检查点）在别处看不出来。
        print(json.dumps({"ready": False, "error": f"{type(ex).__name__}: {ex}"},
                         ensure_ascii=False), flush=True)
        return
    print(json.dumps({"ready": True, "device": device,
                      "load_ms": round((time.perf_counter() - began) * 1000)},
                     ensure_ascii=False), flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line) or {}
            out = decide(request.get("state") or {},
                         request.get("questions") or {},
                         request.get("model"))
        except Exception as ex:
            out = {"error": f"{type(ex).__name__}: {ex}", "answers": {}}
        print(json.dumps(out, ensure_ascii=False), flush=True)


def main() -> None:
    device = _arg("--device", "cuda")
    if "--server" in sys.argv:
        serve(device)
        return
    payload = json.loads(sys.stdin.read() or "{}")
    try:
        _load(device, preload=False)
        out = decide(payload.get("state") or {}, payload.get("questions") or {}, payload.get("model"))
    except Exception as ex:
        out = {"error": f"{type(ex).__name__}: {ex}", "answers": {}}
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
