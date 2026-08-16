#!/usr/bin/env python3
"""把公式 OCR/AI 校正结果(idx→latex)写回 pdf-figures sidecar 的 formulas[i].latex。

用法:
  python formula_writeback.py <sidecar.json|sha> <results.json> [--engine NAME]

results.json 形如:
  {"results":[{"idx":0,"latex":"...","kind":"math|text","conf":0.9}, ...]}
  或顶层就是 [{"idx":..,"latex":..}, ...]

原地更新 formulas[idx].latex(+ 可选 kind / latex_engine),写前备份到 <sidecar>.json.bak,原子替换。
保留 sidecar 原有紧凑格式(单行 JSON),不重排其它字段。
"""
import sys, os, json, argparse, shutil
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
SIDECAR_DIR = PROJ / "state" / "pdf-figures"


def resolve(arg: str) -> Path:
    p = Path(arg)
    if p.exists():
        return p
    c = SIDECAR_DIR / (arg if arg.endswith(".json") else arg + ".json")
    if c.exists():
        return c
    raise SystemExit("sidecar not found: " + arg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sidecar")
    ap.add_argument("results")
    ap.add_argument("--engine", default=None)
    a = ap.parse_args()

    sc = resolve(a.sidecar)
    data = json.loads(sc.read_text("utf-8"))
    res = json.loads(Path(a.results).read_text("utf-8"))
    rows = res["results"] if isinstance(res, dict) and "results" in res else res
    formulas = data.get("formulas") or []
    if not formulas:
        raise SystemExit("sidecar has no formulas")

    n = 0
    for r in rows:
        i = r.get("idx")
        if not isinstance(i, int) or not (0 <= i < len(formulas)):
            continue
        lx = r.get("latex")
        if lx is None:
            continue
        formulas[i]["latex"] = str(lx).strip()
        if r.get("kind"):
            formulas[i]["kind"] = r["kind"]
        if "multiline" in r:
            formulas[i]["multiline"] = bool(r["multiline"])   # 多行公式 → 注入时包 $$..$$(aligned 须 display 模式)
        if a.engine:
            formulas[i]["latex_engine"] = a.engine
        n += 1

    shutil.copy2(sc, sc.with_suffix(".json.bak"))
    # tmp 名带 pid:与 webapp/yolo/describe 共用同一 tmp 会写坏 sidecar。
    tmp = sc.with_suffix(f".json.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")  # indent=1 跟 _fig_save_abs 一致
    tmp.replace(sc)
    print(f"wrote {n}/{len(formulas)} latex into {sc.name} (backup .json.bak)")


if __name__ == "__main__":
    main()
