#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查 pdf-styles.css 与 rc-*.js 之间的样式冲突（2026-09-20）。

为什么要有这个：两边**故意**用同一套类名 —— pdf-adapter.openSettings 里写着
「RC 不可用 → fallback 到原生模板面板」，那条路靠静态 CSS 上样式；而 rc-settings
建的面板复用同一套 id/类名，好让旧的回填/保存函数零改动复用。于是同一个元素被
两份规则同时命中，**谁最后生效谁说了算**。

一天之内因此漏了两个属性：flex-wrap:wrap 让分段控件折成两行、margin-bottom:-1px
把每个按钮往下拽 1px。都是"作用域覆盖没把旧规则的某个属性盖住"。

用法：python3 scripts/check_style_conflicts.py
      有真冲突时退出码 1，并逐条列出 css / js 两边的值。
"""
from __future__ import annotations

import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PDF = os.path.join(ROOT, "_server_deploy", "static", "pdf")

# 有意不同的，列在这里并写明理由 —— 不写理由就不该豁免。
EXPECTED = {
    (".set-tabs", "flex-wrap"): "旧的是下划线 tab（可折行），新的是分段控件（不折行）",
    (".set-tab", "border"): "旧的用 border-bottom 画下划线，新的是填充胶囊、无描边",
    (".set-tab", "padding"): "分段控件左右内边距略大",
}


def decls(body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in re.split(r";(?![^(]*\))", body):
        if ":" not in part:
            continue
        key, _, value = part.partition(":")
        out[key.strip()] = value.strip()
    return out


def norm(value: str) -> str:
    """把 var(--x, 任意兜底) 归一成 --x。

    ⚠ 括号要配对着吃：var(--x, rgba(1,2,3,.4)) 里的 rgba 有自己的右括号，
      用 [^)]* 会在第一个 ) 截断 —— 第一版就这么误报了一片。
    """
    out: list[str] = []
    i = 0
    while i < len(value):
        m = re.compile(r"var\((--[a-z-]+)").match(value, i)
        if not m:
            out.append(value[i])
            i += 1
            continue
        depth, j = 1, m.end()
        while j < len(value) and depth:
            if value[j] == "(":
                depth += 1
            elif value[j] == ")":
                depth -= 1
            j += 1
        out.append(m.group(1))
        i = j
    return "".join(out).strip()


def main() -> int:
    css = open(os.path.join(PDF, "pdf-styles.css"), encoding="utf-8").read()
    rules: dict[str, dict[str, str]] = {}
    for m in re.finditer(r"^(\.[a-zA-Z][a-zA-Z0-9_-]*)\s*\{([^}]*)\}", css, re.M):
        rules.setdefault(m.group(1), decls(m.group(2)))

    js = ""
    for path in sorted(glob.glob(os.path.join(PDF, "rc-*.js"))):
        js += open(path, encoding="utf-8").read()

    problems: list[str] = []
    for sel, cdecl in rules.items():
        m = (re.search(re.escape("'" + sel + "{") + r"([^}]*)\}", js)
             or re.search(r"[ ]" + re.escape(sel + "{") + r"([^}]*)\}", js))
        if not m:
            continue
        jdecl = decls(m.group(1))
        for key in cdecl:
            if key not in jdecl:
                continue
            if norm(cdecl[key]) == norm(jdecl[key]):
                continue
            if (sel, key) in EXPECTED:
                continue
            problems.append(
                "%s { %s }  css=%s  js=%s" % (sel, key, cdecl[key], jdecl[key]))

    if problems:
        print("样式冲突（同名选择器两边取值不同，谁最后生效谁说了算）:")
        for line in problems:
            print("  " + line)
        print()
        print("要么把两边对齐，要么在 EXPECTED 里写明为什么该不同。")
        return 1
    print("style conflicts: 0 new（%d 条已知差异已登记）" % len(EXPECTED))
    return 0


if __name__ == "__main__":
    sys.exit(main())
