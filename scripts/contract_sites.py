#!/usr/bin/env python3
"""这条约定有几份副本、分别在哪？—— 改白名单之前先问一句。

## 为什么需要它

2026-08-19：同一个模式一天咬了五次 —— `_normalize_pc_page` 的允许字段、
MCP 的 `additionalProperties`、`localFileQuery` 的精确参数表、Xcode target 的
sources 清单、卡片顶层字段白名单。全是「清单类的东西改了一处，以为改好了」。

最后那次让用户来回问了四轮，因为我是一层一层撞出来的：
先补 MCP schema（AI 看不见）→ 再补能力指南（指南写着"必须且只能有三个字段"，
主动禁止）→ 再补发现层（助手扫功能列表就下结论，根本不会打开指南）→
最后才发现阅读器入站闸压根没放行。**每一层都长得像"就差这一处了"。**

根因不是哪一处写错了，是**没有一个地方能回答"一共有几处"**。这个脚本就是
那个地方。

## 它不做什么

不做"所有站点必须都有所有字段"的通用校验。实测那是错的：`cid` 在服务端契约
里有、在阅读器入站闸里没有 —— 各道闸的字段范围**本来就不同**，因为它们在
不同的信任边界上，管的东西不一样。照那个做只会得到一个满口误报的检查，
而按这个仓库自己的教训（`references/silent-failure-lessons.md`），
误报比没有检查更糟：它训练人忽略输出。

所以它只报事实：**哪些站点提到了这个词，哪些没有**。判断留给人。
要 pass/fail 的，写成针对性契约测试（如
`tests/reader_contract/card-bind-whitelist-parity.contract.test.mjs`）。

## 用法

    python scripts/contract_sites.py                       # 列出所有登记的约定
    python scripts/contract_sites.py card-top-fields       # 看这条约定的全部站点
    python scripts/contract_sites.py card-top-fields bind  # 逐站点查这个词在不在
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "reader-specs" / "contract-sites.json"


def _load() -> dict:
    return json.loads(REGISTRY.read_text("utf-8"))["contracts"]


def _list_all(contracts: dict) -> int:
    print("已登记的多副本约定：\n")
    for name, spec in contracts.items():
        print(f"  {name}")
        print(f"      {spec['what']}")
        print(f"      {len(spec['sites'])} 处站点"
              + (f"，{len(spec['tests'])} 个契约测试" if spec["tests"] else "，**还没有契约测试**"))
        print()
    print("看某条：python scripts/contract_sites.py <名字> [要查的词]")
    print("\n⚠ 这份登记表是人维护的，可能不全。没登记 ≠ 不存在副本 ——")
    print("   动手前仍要自己 grep 一遍，查到新的就补进 reader-specs/contract-sites.json。")
    return 0


def _show(name: str, spec: dict, probe: str | None) -> int:
    print(f"{name} —— {spec['what']}")
    print(f"权威定义：{spec['canonical']}")
    if spec["tests"]:
        for t in spec["tests"]:
            print(f"契约测试：{t}")
    else:
        print("契约测试：**无** —— 改动后没有任何东西会红")
    print(f"\n{len(spec['sites'])} 处站点"
          + (f"，逐处查「{probe}」：" if probe else "：") + "\n")

    missing = []
    for site in spec["sites"]:
        path = ROOT / site["file"]
        mark = " "
        if probe:
            if not path.exists():
                mark = "?"
            elif probe in path.read_text("utf-8", errors="replace"):
                mark = "✅"
            else:
                mark = "❌"
                missing.append(site)
        print(f"  {mark} [{site['role']}] {site['file']}")
        print(f"        {site['note']}")
    if probe:
        print()
        if missing:
            print(f"有 {len(missing)} 处没提到「{probe}」。")
            print("这**不一定**是 bug —— 各道闸的字段范围本来就不同"
                  "（比如 cid 在服务端契约里有、在入站闸里没有）。")
            print("要判断的是：这个词所在的那条调用路径，是否经过这些站点。")
        else:
            print(f"全部 {len(spec['sites'])} 处都提到了「{probe}」。")
            print("⚠ 提到 ≠ 生效：入站闸那两处是**重建**卡片对象的，"
                  "放行了还要显式把字段搬过去。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("contract", nargs="?", help="约定名字，省略则列出全部")
    parser.add_argument("probe", nargs="?", help="要逐站点查的词，如 bind")
    args = parser.parse_args()

    contracts = _load()
    if not args.contract:
        return _list_all(contracts)
    spec = contracts.get(args.contract)
    if spec is None:
        print(f"没有登记「{args.contract}」。已登记：{', '.join(contracts)}")
        return 1
    return _show(args.contract, spec, args.probe)


if __name__ == "__main__":
    sys.exit(main())
