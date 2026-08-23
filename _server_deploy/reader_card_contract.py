#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工具卡 / 轮次 part 的**唯一契约来源**。

为什么存在(2026-07-27):在此之前同一份"允许哪些卡"散在三处手写白名单——
`assistant.py` 的 web_search 网关、`assistant.py::_EXT_CARD_KINDS`、以及前端渲染器的
分支判断。三处各自漂移:渲染器早就会画 images/videos 了,web_search 网关却只放行
weather/news/fact/general。外部桥接再加第四份白名单只会让漂移更快。

本模块的做法:**kind 不是写死的,是从统一渲染器里解析出来的**——
  - part kind  ← `rc-turncard.js::renderPart` 的 `p.kind === '…'` 分支
  - card kind  ← `rc-voicecall.js::_infoHtml` 的 `k === '…'` 分支(+ else 兜底 general)
渲染器画得出来的就是合法的,画不出来的一律拒绝。字段规格只在本文件声明一次;
若渲染器新增了一种 kind 而这里还没有对应字段规格,校验**fail-closed 并明确报出缺口**,
而不是放行一个前端渲染不出来的东西。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# 渲染器 JS 的位置在**仓库和生产上不一样**:仓库里它就在本文件旁边(`_server_deploy/static/pdf/`),
# 生产上 webapp 目录下**没有** static/pdf —— 静态资源只由 nginx 从 /var/www/html/static 服务。
# 2026-07-27 首次上线就踩了这个:模块本身部署成功,但一读渲染器就 FileNotFoundError。
# 所以按候选根依次找,并允许用环境变量覆盖(测试/别的部署形态)。
_STATIC_CANDIDATES = [
    Path(os.environ["BW_READER_STATIC_PDF"]) if os.environ.get("BW_READER_STATIC_PDF") else None,
    _HERE / "static" / "pdf",                      # 仓库 / 开发树
    Path("/var/www/html/static/pdf"),              # Pi 生产(nginx 静态根)
]


def _static_root() -> Path:
    for c in _STATIC_CANDIDATES:
        if c and (c / "rc-turncard.js").is_file() and (c / "rc-voicecall.js").is_file():
            return c
    raise ContractError(
        "找不到统一渲染器 JS(rc-turncard.js / rc-voicecall.js)。已找过:"
        + ", ".join(str(c) for c in _STATIC_CANDIDATES if c)
        + "。契约以渲染器为唯一来源,读不到就不放行任何卡(设 BW_READER_STATIC_PDF 可覆盖)。")


def _js(name: str) -> Path:
    return _static_root() / name

# `_infoHtml` 的 else 分支:任何未识别 kind 都按"综合"渲染成 data.text/brief。
# 它是渲染器里唯一一个没有 `k === '…'` 字面量的合法 kind,所以只能在这里显式登记。
FALLBACK_CARD_KIND = "general"

_MAX_TEXT = 8000
_MAX_ITEMS = 24
_MAX_STR = 2000


class ContractError(ValueError):
    """契约自身有缺口(渲染器有这种 kind,但本文件没给字段规格)。"""


def _slice_fn(src: str, header: str) -> str:
    """截出某个函数体(到下一个同缩进 `function ` 为止),避免匹配到别处的同名比较。"""
    i = src.find(header)
    if i < 0:
        return ""
    j = src.find("\n  function ", i + len(header))
    return src[i: j if j > 0 else len(src)]


def _kinds_from(path: Path, header: str, var: str) -> list[str]:
    body = _slice_fn(path.read_text("utf-8"), header)
    if not body:
        raise ContractError(f"渲染器契约锚点丢失:{path.name} 里找不到 {header!r}")
    seen, out = set(), []
    for m in re.finditer(re.escape(var) + r"\.kind === '([a-z][a-z0-9_-]*)'"
                         if var else r"\bk === '([a-z][a-z0-9_-]*)'", body):
        k = m.group(1)
        if k not in seen:
            seen.add(k)
            out.append(k)
    if not out:
        raise ContractError(f"渲染器契约锚点没解析出任何 kind:{path.name} {header!r}")
    return out


_CACHE: dict = {}


def _cached(key: str, fn):
    """按 (路径, mtime, 大小) 缓存解析结果:rc-voicecall.js 近 300KB,每次写入都重读太浪费;
    文件一变(部署/热改)缓存键自然失效,不会读到旧契约。"""
    path = _js("rc-turncard.js" if key == "part" else "rc-voicecall.js")
    st = path.stat()
    sig = (str(path), st.st_mtime_ns, st.st_size)
    hit = _CACHE.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    val = fn(path)
    _CACHE[key] = (sig, val)
    return val


def renderer_part_kinds() -> list[str]:
    """轮次 part 的合法 kind(实时与回放同一个 renderPart,所以它就是真值)。"""
    return _cached("part", lambda p: _kinds_from(p, "function renderPart(t, p) {", "p"))


def renderer_card_kinds() -> list[str]:
    """结果卡的合法 kind(_infoHtml 分派 + else 兜底)。"""
    def _parse(p):
        ks = _kinds_from(p, "function _infoHtml(card) {", "")
        return ks + ([FALLBACK_CARD_KIND] if FALLBACK_CARD_KIND not in ks else [])
    return _cached("card", _parse)


# ── 字段规格:唯一声明处。req=必填,opt=可选;list_of 表示该字段是对象数组 ──────────
# 字段名逐条对应渲染器实际消费的字段(见 _infoHtml / renderPart),多余字段一律剔除,
# 免得外部塞进渲染器根本不看、却会被原样存库回放的垃圾。
CARD_FIELD_SPECS: dict[str, dict] = {
    "weather": {"data_req": ("lo", "hi", "cond"), "data_opt": ("loc", "date", "precip", "tip")},
    "news":    {"items_req": ("t",), "items_opt": ("s", "src")},
    "images":  {"items_req": ("url",), "items_opt": ("title", "aid", "src", "_gone")},
    "videos":  {"items_req": ("title",), "items_opt": ("thumb", "url", "channel", "src", "_gone")},
    "fact":    {"data_req": ("answer",), "data_opt": ("detail",)},
    "general": {"data_req": (), "data_opt": ("text",)},
}
_CARD_TOP_OPT = ("title", "brief", "cid", "sources", "bind")

# bind：把卡片**钉到页面上某个元素**的可选绑定（用户设计 2026-08-18）。
#   ⚠ 不叫 anchor：reader-result/1 信封里已经有一个 anchor，那是"这条结果属于
#     哪本书哪一页"(file/page)，跟"钉到页面哪个元素"是两回事。同名会把两种
#     语义混在一起，日后谁都说不清某处的 anchor 指的是哪个。
#   两件事由它同时决定：卡片在屏幕上的位置，以及卡片自身内容嵌入上下文时的位置。
#   所以它不能只是一个像素框——必须能定出"在正文的哪个位置"。
#   两种锚：
#   ① 自建页的格子块（paper.py 的 block 已经有 id + 服务端纯算术算出的归一化 rect，
#      前端按 data-bid 绝对定位，贴卡机制 __upPasteCard 也已在用它）：
#        {"kind": "upage-block", "upage": <插入页 id>, "bid": <block id>}
#   ② 书页正文的字符区间（2026-08-19 补齐）：
#        {"kind": "page-chars", "page": <页码>, "from": <起>, "to": <止>,
#         "text": <那段文字>, "rev": <产出时的字符层 revision，可空>}
#
#   为什么锚是"序号 + 文本 + revision"而不是其中任何一个单独用：
#   - 只用文本 → 用户明确提过的那个问题：**同一个词在一页里会重复出现**，
#     光靠文本找不出是哪一处。
#   - 只用序号 → 序号是某一份字符层里的下标。用户可以切换文字层
#     （PDF 原文字层 / Pi / PC 各一份，见书库的「当前使用」），换一份序号就全变。
#   - 所以两个都带：revision 对得上就直接用序号（精确）；对不上就用文本重新找，
#     并用原序号挑最接近的那一处（消歧）。text 同时也是"这张卡当初钉在哪句话上"
#     的人类可读记录 —— 即便两种定位都失败，它仍然说得清曾经指向什么。
_BIND_KINDS = {"upage-block", "page-chars"}
_BIND_FIELDS = {
    "upage-block": ("upage", "bid"),
    # ⚠ page-chars 只有 page 是无条件必需的。序号与原文**二选一即可**：
    #   带了 from/to 就精确定位；只给 text 由阅读器自己按文本解析。
    #   （用户 2026-08-23：「ai 需要多进行一次思考和判断不是么，而我之前
    #     提出的方案就是依靠自动可靠的位置换算省掉 ai 的这一步」。
    #     换算能力阅读器本来就有 —— locate() 在 rev 失效时走的就是按文本找，
    #     是契约在逼助手先自己算一遍。）
    "page-chars": ("page",),
}
# 可空字段：缺了不影响锚成立，只是降级成"按文本找"或"没有原文记录"。
_BIND_OPTIONAL = {"page-chars": ("from", "to", "text", "rev", "block")}


def _norm_bind(value, kind: str) -> dict:
    """绑定规范化。形状不对就整条丢掉（返回 None）而不是抛错——
    卡片本身仍应显示，只是退回浮层；因为"钉不上去"不该让整张卡消失。"""
    if not isinstance(value, dict):
        return None
    akind = str(value.get("kind") or "").strip()
    if akind not in _BIND_KINDS:
        return None
    out = {"kind": akind}
    for f in _BIND_FIELDS[akind]:
        v = value.get(f)
        if not isinstance(v, (str, int)) or not str(v).strip():
            return None
        out[f] = _clip(str(v))
    for f in _BIND_OPTIONAL.get(akind, ()):
        v = value.get(f)
        if isinstance(v, (str, int)) and str(v).strip():
            out[f] = _clip(str(v))
    if akind == "page-chars":
        has_range = "from" in out and "to" in out
        if has_range:
            # 序号必须是能比大小的数，且 from <= to。歪掉的区间不如没有 ——
            # 让它退回浮层，别在页面上定出一个荒唐的位置。
            try:
                lo, hi = int(out["from"]), int(out["to"])
            except (TypeError, ValueError):
                return None
            if lo < 0 or hi < lo:
                return None
            out["from"], out["to"] = str(lo), str(hi)
        elif "from" in out or "to" in out:
            # 只给一半是**发错了**，不是"想按文本找"。放过去会得到一个
            # 看起来合法却半截的锚，比整条拒掉难查得多。
            return None
        elif not out.get("text"):
            # 既没有序号也没有原文 —— 这个锚指不向任何地方。
            return None
        if "block" in out:
            # 块号：把"按文本找"的范围限定在某一块里。
            #
            # 用户 2026-08-23 定的寻址方式：「ai 其实要做的就是用 markdown 里的
            # 分区加上分区里的内容进行锁定…系统要做到利用生成 markdown 时的
            # 对应关系，将这段命令文本换算成原先的对应范围」。
            #
            # 它比"只给原文"严格更准：同一句话在页内出现多次时，**块把范围锁住了**，
            # 不必再报 matchCount 让助手把引用加长（那是多一轮）。
            # 而块号助手本来就在读（Markdown 里印着 [NN]），说出来零成本。
            #
            # ⚠ 块号是**位置**不是身份：页面重排、换一份 OCR 之后同一个 [03]
            #   可能指向别的内容。所以它只用来缩小搜索范围，最终仍靠 text 命中；
            #   块内找不到就退回全页找，并在回执里说明走了哪条。
            try:
                blk = int(out["block"])
            except (TypeError, ValueError):
                return None
            if blk < 1:
                return None
            if not out.get("text"):
                # 块号必须配原文。只给块号等于"钉在第 3 块的某处" ——
                # 那不是一个位置，放过去只会得到一个看起来合法的假锚。
                return None
            out["block"] = str(blk)
    return out

PART_FIELD_SPECS: dict[str, dict] = {
    "text":   {"req": ("text",), "opt": ()},
    "card":   {"req": ("card",), "opt": ("seq",)},
    "cards":  {"req": ("cards",), "opt": ("draft", "gid", "seq")},
    "hlcard": {"req": ("file", "items"), "opt": ("seq",)},
    "tool":   {"req": (), "opt": ("tool", "label", "args", "result", "ms", "seq")},
    "meta":   {"req": (), "opt": ("meta", "seq")},
}


def contract_gaps() -> list[str]:
    """渲染器支持、但本文件没给字段规格的 kind。非空 = 契约有缺口,必须补齐后才放行。"""
    gaps = [f"card:{k}" for k in renderer_card_kinds() if k not in CARD_FIELD_SPECS]
    gaps += [f"part:{k}" for k in renderer_part_kinds() if k not in PART_FIELD_SPECS]
    return gaps


def _pick(src: dict, req, opt, where: str) -> dict:
    out = {}
    for f in req:
        v = src.get(f)
        if v in (None, ""):
            raise ValueError(f"{where}:缺必填字段 {f}")
        out[f] = v
    for f in opt:
        if src.get(f) is not None:
            out[f] = src[f]
    return out


def _clip(v):
    if isinstance(v, str):
        return v[:_MAX_STR]
    if isinstance(v, (int, float, bool)) or v is None:
        return v
    return str(v)[:_MAX_STR]


def validate_card(card) -> dict:
    """校验并**规范化**一张结果卡。不合规抛 ValueError(消息含具体字段/kind)。"""
    if not isinstance(card, dict):
        raise ValueError("card 必须是对象")
    kind = str(card.get("kind") or "").strip()
    allowed = renderer_card_kinds()
    if kind not in allowed:
        raise ValueError(f"card.kind={kind!r} 渲染器画不出来;当前支持:{sorted(allowed)}")
    spec = CARD_FIELD_SPECS.get(kind)
    if spec is None:
        raise ContractError(
            f"契约缺口:渲染器支持 card.kind={kind!r},但 CARD_FIELD_SPECS 里没有它的字段规格 —— "
            "请在 reader_card_contract.py 补一条(fail-closed,不放行渲染不出的卡)")
    out = {"kind": kind, "data": {}}
    if "items_req" in spec:
        items = card.get("data", {}).get("items") if isinstance(card.get("data"), dict) else None
        if not isinstance(items, list) or not items:
            raise ValueError(f"card[{kind}].data.items 必须是非空数组")
        norm = []
        for i, it in enumerate(items[:_MAX_ITEMS]):
            if not isinstance(it, dict):
                raise ValueError(f"card[{kind}].data.items[{i}] 必须是对象")
            try:
                one = _pick(it, spec["items_req"], spec["items_opt"], f"card[{kind}].items[{i}]")
            except ValueError as e:
                raise ValueError(str(e)) from None
            norm.append({k: _clip(v) for k, v in one.items()})
        out["data"]["items"] = norm
    else:
        data = card.get("data") if isinstance(card.get("data"), dict) else {}
        one = _pick(data, spec["data_req"], spec["data_opt"], f"card[{kind}].data")
        out["data"] = {k: _clip(v) for k, v in one.items()}
    for f in _CARD_TOP_OPT:
        v = card.get(f)
        if v is None:
            continue
        if f == "bind":
            b = _norm_bind(v, kind)
            if b:
                out["bind"] = b
        elif f == "sources":
            if not isinstance(v, list):
                raise ValueError(f"card[{kind}].sources 必须是数组")
            out["sources"] = [{"url": _clip(s.get("url")), "title": _clip(s.get("title"))}
                              for s in v[:5] if isinstance(s, dict) and s.get("url")]
        else:
            out[f] = _clip(v)
    return out


def validate_parts(parts) -> list[dict]:
    """校验并规范化一个 parts 数组(轮次的正文)。空数组合法——纯文本轮不该被强迫造卡。"""
    gaps = contract_gaps()
    if gaps:
        raise ContractError(f"契约缺口未补齐,拒绝写入:{gaps}")
    if parts is None:
        return []
    if not isinstance(parts, list):
        raise ValueError("parts 必须是数组")
    allowed = renderer_part_kinds()
    out = []
    for i, p in enumerate(parts):
        if not isinstance(p, dict):
            raise ValueError(f"parts[{i}] 必须是对象")
        kind = str(p.get("kind") or "").strip()
        if kind not in allowed:
            raise ValueError(f"parts[{i}].kind={kind!r} 渲染器画不出来;当前支持:{sorted(allowed)}")
        spec = PART_FIELD_SPECS[kind]
        one = _pick(p, spec["req"], spec["opt"], f"parts[{i}]({kind})")
        if kind == "text":
            t = str(one["text"]).strip()
            if not t:
                raise ValueError(f"parts[{i}](text):text 不能为空白")
            one["text"] = t[:_MAX_TEXT]
        elif kind == "card":
            one["card"] = validate_card(one["card"])
        elif kind == "cards":
            if not isinstance(one["cards"], list) or not one["cards"]:
                raise ValueError(f"parts[{i}](cards):cards 必须是非空数组")
            one["cards"] = one["cards"][:_MAX_ITEMS]
        elif kind == "hlcard":
            if not isinstance(one["items"], list) or not one["items"]:
                raise ValueError(f"parts[{i}](hlcard):items 必须是非空数组")
            one["items"] = one["items"][:_MAX_ITEMS]
        out.append({"kind": kind, **one})
    return out
