#!/usr/bin/env python3
# 阶段1 切分协议对抗单测:验 _parse_batch(纯解析)+ ai_translate_batch 编排(mock _ai_batch_call/gtranslate_batch/_cache_put)
import sys
sys.path.insert(0, '/home/bwicarus/claude/scripts/vocab')
import translate as T

fails = []
def ck(name, cond, got=None):
    if cond: print(f"  ✓ {name}")
    else: print(f"  ✗ {name}  got={got!r}"); fails.append(name)

print("== _parse_batch 对抗 ==")
# 1 正常 + 术语块
r, g = T._parse_batch("⟦1⟧ 你好\n⟦2⟧ 世界\n⟦G⟧\nAlice => 爱丽丝")
ck("正常段", r == {1: "你好", 2: "世界"}, r)
ck("术语块", g == {"Alice": "爱丽丝"}, g)
# 2 译文内换行(吃到下一标记)
r, _ = T._parse_batch("⟦1⟧ 第一行\n第二行\n⟦2⟧ 世界\n⟦G⟧")
ck("译文内换行不断段", r == {1: "第一行\n第二行", 2: "世界"}, r)
# 3 前导废话丢弃
r, _ = T._parse_batch("好的,我来翻译:\n⟦1⟧ 你好\n⟦2⟧ 世界")
ck("前导废话丢弃", r == {1: "你好", 2: "世界"}, r)
# 4 漏段(缺号)
r, _ = T._parse_batch("⟦1⟧ 你好\n⟦3⟧ 三")
ck("漏段=缺号", r == {1: "你好", 3: "三"}, r)
# 5 重排(按 id 映射,顺序无关)
r, _ = T._parse_batch("⟦2⟧ 二\n⟦1⟧ 一\n⟦3⟧ 三")
ck("重排按id映射", r == {1: "一", 2: "二", 3: "三"}, r)
# 6 无术语块
r, g = T._parse_batch("⟦1⟧ 你好\n⟦2⟧ 世界")
ck("无术语块", g == {}, g)
# 7 拒绝识别→该段miss
r, _ = T._parse_batch("⟦1⟧ 我无法翻译这个内容\n⟦2⟧ 世界")
ck("拒绝段判miss", (1 not in r and r.get(2) == "世界"), r)
# 8 只有 ⟦G⟧(全空术语)
r, g = T._parse_batch("⟦1⟧ 你好\n⟦G⟧")
ck("空术语块", (r == {1: "你好"} and g == {}), (r, g))
# 9 术语块含 => 多行
r, g = T._parse_batch("⟦1⟧ x\n⟦G⟧\nFoucault => 福柯\nbiopolitics => 生命政治")
ck("多术语", g == {"Foucault": "福柯", "biopolitics": "生命政治"}, g)

print("== _san_seg ==")
ck("源自带标记sanitize", T._san_seg("a⟦b⟧c") == "a[b]c", T._san_seg("a⟦b⟧c"))

print("== ai_translate_batch 编排(mock) ==")
_orig_call, _orig_g, _orig_cp = T._ai_batch_call, T.gtranslate_batch, T._cache_put
T._cache_put = lambda *a, **k: None  # 别写盘

# A 缺段重试:首批3段返回1,2(缺3);重试(局部id 3)返回3
def fake_retry(numbered, target, model, effort, glossary):
    ids = [i for i, _ in numbered]
    if ids == [1, 2, 3]: return {1: "一", 2: "二"}, {"Foo": "福"}
    if ids == [3]:       return {3: "三"}, {}
    return {}, {}
T._ai_batch_call = fake_retry
out, glo = T.ai_translate_batch(["a", "b", "c"], model="sonnet")
ck("缺段重试补齐", out == ["一", "二", "三"], out)
ck("术语合并", glo.get("Foo") == "福", glo)

# B 全崩→降级 Google
T._ai_batch_call = lambda *a, **k: ({}, {})
T.gtranslate_batch = lambda texts, target="zh-CN": ["G" + t for t in texts]
out, _ = T.ai_translate_batch(["a", "b"], model="sonnet")
ck("全崩降级Google", out == ["Ga", "Gb"], out)

# C 部分成功但<50%→不重试,直接降级(2段只返回1个=50%边界,这里3段返回1个<50%)
def fake_low(numbered, target, model, effort, glossary):
    ids = [i for i, _ in numbered]
    if len(ids) == 3: return {1: "一"}, {}   # 命中<50% → 不重试整体降级
    return {}, {}
T._ai_batch_call = fake_low
T.gtranslate_batch = lambda texts, target="zh-CN": ["G" + t for t in texts]
out, _ = T.ai_translate_batch(["a", "b", "c"], model="sonnet")
# 1 采纳AI"一",2/3降级Google("Gb"/"Gc")
ck("低命中率整体降级", out == ["一", "Gb", "Gc"], out)

# D 空原文占位:texts含空串,输出对齐(空串位置留空)
T._ai_batch_call = lambda numbered, *a, **k: ({i: "译" + str(i) for i, _ in numbered}, {})
T.gtranslate_batch = _orig_g
out, _ = T.ai_translate_batch(["a", "", "c"], model="sonnet")
ck("空原文位置对齐留空", (out[1] == "" and out[0] and out[2]), out)

# E 服务端注入 text-only generator；协议模块不读取 uid/action，缓存按调用方 namespace。
T._ai_batch_call = _orig_call
cache_writes = []
T._cache_put = lambda text, target, tr, source, ns="": cache_writes.append((text, tr, source, ns))
seen_prompt = {}
def injected_generator(system, user):
    seen_prompt["system"] = system
    seen_prompt["user"] = user
    return "⟦2⟧ 乙\n⟦1⟧ 甲\n⟦G⟧\nAlpha => 阿尔法"
out, glo, meta = T.ai_translate_batch(
    ["a", "b"], model="safe-model", generator=injected_generator,
    cache_ns="web-ai-v1-safe-model", fallback_cache_ns="web-google-v1",
    with_meta=True,
)
ck("注入generator仍按id映射", out == ["甲", "乙"], out)
ck("敌对网页提示进入system", "不可信数据" in seen_prompt.get("system", ""), seen_prompt)
ck("AI缓存使用调用方namespace",
   bool(cache_writes) and all(x[3] == "web-ai-v1-safe-model" for x in cache_writes),
   cache_writes)
ck("来源元数据可区分降级", meta["sources"] == ["ai", "ai"] and meta["google"] == 0, meta)
ck("注入generator术语回传", glo.get("Alpha") == "阿尔法", glo)

# F 会话生成:禁止 AI 文本缓存，但 Google fallback 仍按 Google namespace 落盘。
cache_writes.clear()
out, _, meta = T.ai_translate_batch(
    ["a"], model="safe-model",
    generator=lambda _system, _user: "⟦1⟧ 会话译文\n⟦G⟧",
    cache_ns="web-ai-v2-safe-session",
    fallback_cache_ns="web-google-v1",
    with_meta=True,
    cache_ai=False,
)
ck("session AI译文不写服务器文本缓存",
   out == ["会话译文"] and cache_writes == [] and meta["sources"] == ["ai"],
   (out, cache_writes, meta))

cache_writes.clear()
T.gtranslate_batch = lambda texts, target="zh-CN": ["G" + t for t in texts]
out, _, meta = T.ai_translate_batch(
    ["a"], model="safe-model",
    generator=lambda _system, _user: "",
    cache_ns="web-ai-v2-safe-session",
    fallback_cache_ns="web-google-v1",
    with_meta=True,
    cache_ai=False,
)
ck("session Google兜底仍写Google缓存",
   out == ["Ga"] and meta["sources"] == ["google"]
   and len(cache_writes) == 1 and cache_writes[0][3] == "web-google-v1",
   (out, cache_writes, meta))

T._ai_batch_call, T.gtranslate_batch, T._cache_put = _orig_call, _orig_g, _orig_cp
print()
print("FAILED:" + ",".join(fails) if fails else "ALL PASS ✓")
sys.exit(1 if fails else 0)
