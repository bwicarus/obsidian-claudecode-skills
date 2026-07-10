"""二期验证:Gemini 能否胜任 orchestrator 的工具循环(我们手搓 JSON 工具协议)。

复用生产的 _sys_static / _ctx_block / _parse_tool / TOOLS,只把「claude 进程多轮」换成
「Gemini 多轮 contents」。跑几个真实请求,量化:
  ① 工具调用 JSON 格式正确率(_parse_tool 解析得出来吗)
  ② 工具选对(read_page / translate / goto_page / summarize_section / make_note …)
  ③ 多轮收敛到 final answer(不超步、不空转)
  ④ 复合任务能否拆成多步
不改生产代码;写类工具(make_*/highlight)stub 掉防副作用。烧少量 Gemini token。

用法:python3 scripts/verify_gemini_orchestrator.py [gemini-2.5-flash|gemini-2.5-pro] [--nothink]
"""
import sys, json, glob, time

sys.path.insert(0, "/home/bwicarus/webapp")
sys.path.insert(0, "/home/bwicarus/claude/scripts")
import assistant as A  # noqa: E402

GEM_MODEL = next((a for a in sys.argv[1:] if not a.startswith("--")), "gemini-2.5-flash")
THINK = "--nothink" not in sys.argv
WRITE_TOOLS = {"make_note", "make_anki", "add_vocab", "highlight"}   # 副作用工具:验证只看"选对"不真写

_stats = {"steps": 0, "malformed": 0, "gemini_err": 0, "toks": 0}


def gemini_chat(system, contents, model=GEM_MODEL, think=THINK, timeout=90):
    """Gemini 多轮 generateContent。contents=[{role,parts}]。返回 (text|None, usage_total)。"""
    import requests
    keys = A._gemini_keys()
    if not keys:
        return None, 0
    key = keys[0][0]   # 免费优先
    cfg = {"temperature": 0.3, "maxOutputTokens": 2000}
    if not think:
        cfg["thinkingConfig"] = {"thinkingBudget": 0}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents, "generationConfig": cfg}
    try:
        r = requests.post(url, json=body, timeout=timeout)
    except Exception as e:
        return f"__ERR__:{str(e)[:120]}", 0
    if r.status_code != 200:
        return f"__HTTP_{r.status_code}__:{r.text[:160]}", 0
    j = r.json(); us = A._gemini_usage(j)
    cand = (j.get("candidates") or [{}])[0]
    out = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []))
    return out.strip(), us["total"]


def run_turn(message, ctx, max_steps=8):
    system = A._sys_static()
    first = f"{A._ctx_block(ctx)}\n\n【用户】{message}\n\n现在开始(调工具就只输出 JSON,能答就直接答):"
    contents = [{"role": "user", "parts": [{"text": first}]}]
    steps = []
    for _ in range(max_steps):
        text, toks = gemini_chat(system, contents)
        _stats["toks"] += toks
        if text is None:
            steps.append({"err": "gemini key 缺失/冷却中"}); _stats["gemini_err"] += 1; break
        if text.startswith("__HTTP_") or text.startswith("__ERR__"):
            steps.append({"err": text}); _stats["gemini_err"] += 1; break
        _stats["steps"] += 1
        tool = A._parse_tool(text)
        if tool and tool.get("tool") in A.TOOLS:
            name = tool["tool"]
            targs = tool.get("args") if isinstance(tool.get("args"), dict) else {}
            if name in WRITE_TOOLS:
                res = {"ok": True, "task_id": "VERIFY-STUB", "note": "(验证模式:写操作已跳过)"}
            else:
                try:
                    res = A.TOOLS[name][1](targs, ctx) or {}
                except Exception as e:
                    res = {"error": str(e)[:160]}
            if isinstance(res, dict):
                for k in ("_vision", "_gen_model", "_gen_action"):
                    res.pop(k, None)
            steps.append({"tool": name, "args": targs,
                          "res_keys": list(res.keys())[:8] if isinstance(res, dict) else None})
            contents.append({"role": "model", "parts": [{"text": text}]})
            contents.append({"role": "user", "parts": [{"text": "【工具结果】"
                            + json.dumps(res, ensure_ascii=False)[:6000]
                            + "\n\n继续(调工具只输出 JSON,能答就直接答):"}]})
            continue
        looks_json = text.strip().startswith("{") and '"tool"' in text[:400]
        if looks_json and not tool:
            _stats["malformed"] += 1
        steps.append({"final": text, "malformed_toolcall": bool(looks_json and not tool)})
        break
    else:
        steps.append({"err": f"未收敛(>{max_steps} 步)"})
    return steps


def main():
    vault = str(A.VAULT_ROOT)
    import fitz
    pick = None
    for p in sorted(glob.glob(vault + "/**/*.pdf", recursive=True)):
        try:
            d = fitz.open(p)
            for pg in range(min(10, d.page_count)):
                if len((d[pg].get_text("text") or "").strip()) > 250:
                    pick = (p, pg + 1, d.page_count); break
            d.close()
            if pick:
                break
        except Exception:
            continue
    if not pick:
        print("✗ vault 里没找到有文字层的 PDF,无法验证"); return
    path, page, npages = pick
    file_rel = path[len(vault) + 1:]
    ctx = {"file_rel": file_rel, "page": page, "pages": [page],
           "book_name": file_rel.split("/")[-1][:-4], "page_offset": 0,
           "_uid": "_verify", "_base": "http://127.0.0.1:5000"}
    print(f"靶书: {file_rel}")
    print(f"页 {page}/{npages}  模型 {GEM_MODEL}  thinking={'on' if THINK else 'off'}")
    print("=" * 72)
    tests = [
        "这页主要讲什么?简要说",
        "把这一页翻译成中文",
        "跳到第 3 页",
        "总结这一节的要点,再把要点整理成一条笔记",
    ]
    for q in tests:
        print(f"\n▶ 「{q}」")
        t0 = time.time()
        steps = run_turn(q, dict(ctx))
        dt = round(time.time() - t0, 1)
        for s in steps:
            if "tool" in s:
                print(f"   🔧 {s['tool']}({json.dumps(s['args'], ensure_ascii=False)[:70]}) → {s['res_keys']}")
            elif "final" in s:
                flag = "  ⚠️JSON坏" if s.get("malformed_toolcall") else ""
                print(f"   💬 final{flag}: {s['final'][:160]}")
            elif "err" in s:
                print(f"   ❌ {s['err']}")
        print(f"   ⏱ {dt}s · {len([s for s in steps if 'tool' in s])} 个工具调用")
    print("\n" + "=" * 72)
    print(f"汇总: 模型决策步 {_stats['steps']} · JSON坏 {_stats['malformed']} · Gemini错 {_stats['gemini_err']} · 共 {_stats['toks']} token")
    verdict = "✅ 稳" if (_stats["malformed"] == 0 and _stats["gemini_err"] == 0) else "⚠️ 有问题,看上面"
    print(f"判定: {verdict}")


if __name__ == "__main__":
    main()
