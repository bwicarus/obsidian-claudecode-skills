#!/usr/bin/env python3
"""kg/gen_page_brief.py — 生成「本页知识点简述」元数据(页面简述系统 Phase 1 生成器)。

配方**改自** build_nodes.py::extract_l2_from_page(逐页 render + 抽 char 文字 → AI → 知识点),
但按用户拍板改造:
- **不依赖 TOC**:直接从页文字层 + 图描述现算,覆盖扫描/日语/无 KG 书;
- 输出改为 {"brief":"1-3句≤60字要点", "tags":[3-5标签],
  "concepts":[{"name":"概念","evidence":"原文逐字证据"}], "page_type":..., "subtype":...};
- 读页高频 → 用**轻模型**(gemini-3.5-flash,省额度+快;无 key/失败回退 claude haiku);
- 顺手让 AI 判定 page_type/subtype 当基本属性元数据(反正已读这页,零额外调用)。

输入:--file <vault 相对路径(list_books 的 rel)> --page <1-based 页>
输出:打印一行 JSON 到 stdout(供 pdf_reader 后台子进程捕获后写 sidecar):
  {"brief":"...", "tags":["..."], "concepts":[...], "page_type":"knowledge|skip|userpage",
   "subtype":"text|figure|formula|mixed|toc|front|back|blank|", "model":"gemini|haiku"}

用法:
  python3 scripts/kg/gen_page_brief.py --file "资源/books/xxx.pdf" --page 12
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

import fitz  # PyMuPDF

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
_CODE_ROOT = Path(__file__).resolve().parents[2]
_PROJECT_DIR = Path(config.PROJECT_DIR)
sys.path.insert(0, str(_CODE_ROOT / "_client" / "core"))
from ai_backends import _gemini_chat, make_backend  # noqa: E402

# 提示词/生成配方版本;改配方就 +1,并同步 pdf_reader.py 的 _BRIEF_VER → 旧简述全失效重生。
BRIEF_PROMPT_VER = 2

VAULT = config.VAULT_ROOT
STATE = _PROJECT_DIR / "state"
CHAR_CACHE = STATE / "pdf-char-cache"
FIG_DIR = STATE / "pdf-figures"

_ALLOWED_TYPE = {"knowledge", "skip", "userpage"}
_ALLOWED_SUB = {"text", "figure", "formula", "mixed", "toc", "front", "back", "blank"}
# 插图边框被 OCR 认成的竖线串(跟 pdf_reader._OCR_LINE_NOISE_RE 同款,文字层兜底清洗用)
_OCR_LINE_NOISE_RE = re.compile(r"(?:[|│丨︱‖∥┃┆┇┊┋╎╏]\s*){2,}")


def _abs_from_rel(rel: str) -> Path | None:
    """rel → vault 内绝对路径(防 traversal;镜像 pdf_reader._safe_vault_path)。"""
    rel = (rel or "").lstrip("/")
    if not rel or ".." in rel.split("/"):
        return None
    ap = (VAULT / rel).resolve()
    try:
        ap.relative_to(VAULT.resolve())
    except ValueError:
        return None
    return ap if ap.exists() and ap.is_file() else None


def _book_sha(abs_path: Path) -> str:
    """跟 pdf_reader._book_sha / describe_figures.pdf_sha 同键规则(图注 sidecar 互通)。"""
    return hashlib.sha1(str(Path(abs_path).resolve()).encode("utf-8")).hexdigest()[:16]


def _char_cache_text(rel: str, page: int, mtime: int) -> str:
    """**复用 pdf_reader 已算好的 char-cache**(state/pdf-char-cache/<sha>-p<page>-<mtime>-<lang>.json):
    按 y 中心重建阅读序文本、跳 ruby 注音(镜像 _page_text_clean 的 chars→text 逻辑)。
    只接受当前 PDF mtime 的缓存；缺失/损坏时返回 ""，由调用方读取当前 PDF 文字层。
    绝不能退回同页任意旧 mtime 缓存，否则换书/re-OCR 后会把陈旧证据写入 KG。"""
    sha = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]
    if not CHAR_CACHE.exists() or mtime <= 0:
        return ""
    cands = sorted(CHAR_CACHE.glob(f"{sha}-p{page}-{mtime}-*.json"))
    for cp in cands:
        try:
            d = json.loads(cp.read_text("utf-8"))
        except Exception:
            continue
        chars = d.get("chars") or []
        if not chars:
            continue
        hs = sorted((c["y1"] - c["y0"]) for c in chars if not c.get("sp") and c.get("c", "").strip())
        med = hs[len(hs) // 2] if hs else 0
        out, prev = [], None
        for c in chars:
            if c.get("sp"):
                out.append(" ")
                continue
            ch = c.get("c", "")
            if med and (c["y1"] - c["y0"]) < med * 0.60 and re.match(r"^[ぁ-んァ-ヶー]$", ch):
                continue  # ruby 注音不进 AI 文本
            if prev is not None:
                cy = (c["y0"] + c["y1"]) / 2
                py = (prev["y0"] + prev["y1"]) / 2
                if abs(cy - py) > max(c["y1"] - c["y0"], prev["y1"] - prev["y0"]) * 0.6:
                    out.append("\n")
            out.append(ch)
            prev = c
        txt = re.sub(r"[ \t]+", " ", "".join(out))
        txt = re.sub(r" ?\n ?", "\n", txt).strip()
        if txt:
            return txt
    return ""


def _page_text(abs_path: Path, rel: str, page: int) -> str:
    """本页干净文字:优先复用 char-cache(阅读器算好的剔噪去注音),否则 fitz 裸文字 + OCR 竖线噪声清洗。"""
    try:
        mt = int(os.path.getmtime(str(abs_path)))
    except Exception:
        mt = 0
    t = _char_cache_text(rel, page, mt)
    if t:
        return t[:3500]
    try:
        doc = fitz.open(str(abs_path))
        try:
            idx = max(0, min(page - 1, doc.page_count - 1))
            raw = doc[idx].get_text("text") or ""
        finally:
            doc.close()
        return _OCR_LINE_NOISE_RE.sub(" ", raw).strip()[:3500]
    except Exception:
        return ""


def _page_figures(abs_path: Path, page: int) -> list[str]:
    """本页图描述(pdf-figures sidecar,_book_sha 键与 describe_figures.py 互通)。缺 → 空。"""
    p = FIG_DIR / f"{_book_sha(abs_path)}.json"
    try:
        d = json.loads(p.read_text("utf-8"))
    except Exception:
        return []
    out = []
    for f in (d.get("figures") or []):
        if f.get("page") == page:
            cap = (f.get("caption") or "").strip()
            desc = (f.get("desc") or "").strip()
            seg = (cap + ":" + desc) if (cap and desc) else (desc or cap)
            if seg:
                out.append(seg)
    return out


def _render_png(abs_path: Path, page: int) -> bytes | None:
    """渲染该页 PNG(文字太少的扫描/图为主页,附图让轻模型视觉判读)。"""
    try:
        doc = fitz.open(str(abs_path))
        try:
            p = doc[max(0, min(page - 1, doc.page_count - 1))]
            z = min(2.0, 1540.0 / (max(p.rect.width, p.rect.height) or 1.0))
            return p.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False).tobytes("png")
        finally:
            doc.close()
    except Exception:
        return None


_PROMPT = """你在给一页教材/技术书生成「知识点简述」元数据(供语音伴读省 token、知识树复用)。这是第 {page} 页。

【本页文字层】
————
{page_text}
————
{figs}
请**只针对这一页实际内容**,先判定页面类型,再写简述,输出**一个 JSON 对象**:
- "page_type":本页主类,必出其一——
    "knowledge"(有实质学习内容的正文页) /
    "skip"(目录/版权/扉页/索引/参考文献/空白等无学习内容的页) /
    "userpage"(用户手写/作答的插入页;拿不准别硬判,归 knowledge 或 skip)
- "subtype":细分,拿不准可空串——
    knowledge 下:"text"(纯文字) / "figure"(重要插图为主) / "formula"(公式推导为主) / "mixed"
    skip 下:"toc"(目录) / "front"(版权扉页) / "back"(索引参考文献) / "blank"(空白/几乎无文字)
- "brief":中文 1-3 句(总共≤60 字)概括本页核心知识点/在讲什么,直接说要点、别写"本页介绍了";
    **skip 页别硬凑知识点**,brief 写很短的说明即可(如"目录页,无学习内容"),或空串 ""。
- "tags":3-5 个知识点标签(每个 2-8 字的中文名词短语,如"矩阵乘法""链式法则"),对应本页出现的概念;
    skip 页 / 无实质知识点 → 空数组 []。
- "concepts":可安全用于知识图谱的概念候选数组,每项必须是
    {{"name":"原文术语","evidence":"本页文字层中逐字出现的一句或连续短语"}}。
    name 和 evidence 都必须逐字来自上面的文字层；name 必须包含在 evidence 中。
    不能把英文/日文术语翻译成中文 name，不能改写 evidence，也不能引用图片里未转成文字的内容；
    找不到逐字证据时不要为该 tag 生成 concept。最多 5 项；skip 页必须为空数组。

严格只输出这个 JSON 对象,无任何额外文字、说明或代码围栏。
例:{{"page_type":"knowledge","subtype":"mixed","brief":"定义线性映射及其矩阵表示,证明矩阵乘法对应映射复合。","tags":["线性映射","矩阵表示","映射复合"],"concepts":[{{"name":"线性映射","evidence":"线性映射是保持向量加法与标量乘法的映射。"}}]}}
"""


def _parse(raw: str) -> dict:
    """解析 AI JSON；concept evidence 之后还会对真实页文字做逐字校验。"""
    s = (raw or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n", "", s)
        s = re.sub(r"\n```\s*$", "", s)
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return {"brief": "", "tags": [], "concepts": [], "page_type": "", "subtype": ""}
    try:
        d = json.loads(s[i:j + 1])
    except Exception:
        return {"brief": "", "tags": [], "concepts": [], "page_type": "", "subtype": ""}
    brief = str(d.get("brief") or "").strip()[:120]
    tags = d.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    tags = [str(t).strip()[:20] for t in tags if str(t).strip()][:5]
    concepts = []
    raw_concepts = d.get("concepts") or []
    if isinstance(raw_concepts, list):
        for item in raw_concepts[:5]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()[:40]
            evidence = str(item.get("evidence") or "").strip()[:500]
            if name and evidence:
                concepts.append({"name": name, "evidence": evidence})
    pt = str(d.get("page_type") or "").strip().lower()
    if pt not in _ALLOWED_TYPE:
        # 只有模型明确给出的 skip 才能成为可持久化的“无需处理”结论。
        # 缺失/非法页型且内容全空通常意味着模型给了空心 JSON；保留空页型，
        # 让 pdf_reader 的全空门禁把它当作临时失败并在下次读页重试。
        pt = "knowledge" if (brief or tags) else ""
    st = str(d.get("subtype") or "").strip().lower()
    if st not in _ALLOWED_SUB:
        st = ""
    if pt != "knowledge":
        concepts = []
    return {
        "brief": brief,
        "tags": tags,
        "concepts": concepts,
        "page_type": pt,
        "subtype": st,
    }


def _evidence_key(value: str) -> str:
    return re.sub(
        r"\s+",
        "",
        __import__("unicodedata").normalize("NFKC", str(value or "")),
    ).casefold()


def _verified_concepts(concepts: list[dict], page_text: str) -> list[dict]:
    """只保留能在真实文字层逐字复核的证据；AI 改写/翻译一律 fail closed。"""
    page_key = _evidence_key(page_text)
    if not page_key:
        return []
    out = []
    seen = set()
    for item in concepts or []:
        name = str(item.get("name") or "").strip()
        evidence = str(item.get("evidence") or "").strip()
        name_key = _evidence_key(name)
        evidence_key = _evidence_key(evidence)
        meaningful = re.sub(r"[^\w\u3400-\u9fff\u3040-\u30ff]", "", evidence_key)
        if (
            not name_key
            or len(meaningful) < 4
            or name_key not in evidence_key
            or evidence_key not in page_key
        ):
            continue
        identity = (name.casefold(), evidence_key)
        if identity in seen:
            continue
        seen.add(identity)
        out.append({"name": name[:40], "evidence": evidence[:500]})
        if len(out) >= 5:
            break
    return out


def gen_brief(rel: str, page: int) -> dict:
    abs_path = _abs_from_rel(rel)
    if not abs_path:
        return {
            "brief": "", "tags": [], "concepts": [],
            "page_type": "", "subtype": "", "model": "",
            "error": "file_not_found",
        }
    text = _page_text(abs_path, rel, page)
    figs = _page_figures(abs_path, page)
    figs_block = ("【本页插图描述】\n" + "\n".join("· " + f for f in figs) + "\n") if figs else ""
    # 有文字层 → 纯文本喂轻模型(省 token/快);文字太少(扫描页/图为主)→ 附页图让视觉判读
    png = _render_png(abs_path, page) if len((text or "").strip()) < 120 else None
    prompt = _PROMPT.format(
        page=page,
        page_text=(text[:3500] if text else "(无文字层,靠附图识别)"),
        figs=figs_block,
    )
    model = "gemini"
    raw = ""
    img_path = None
    try:
        if png:
            fd, img_path = tempfile.mkstemp(suffix=".png", prefix="brief-")
            os.close(fd)
            Path(img_path).write_bytes(png)
        raw = _gemini_chat(prompt, image_path=img_path, model="gemini-3.5-flash")
    except Exception:
        raw = ""
    finally:
        if img_path:
            try:
                Path(img_path).unlink(missing_ok=True)
            except Exception:
                pass
    if not (raw or "").strip():        # gemini 无 key / 失败 → claude haiku 兜底
        try:
            be = make_backend("claude_cli", settings={
                "command": os.environ.get("APP_CLAUDE") or "claude",
                "model": "haiku", "gemini_model": "gemini-3.5-flash"})
            raw = be.chat([{"role": "user", "content": prompt}], image=png)
            model = "haiku"
        except Exception:
            raw = ""
    obj = _parse(raw)
    obj["concepts"] = _verified_concepts(obj.get("concepts") or [], text)
    obj["model"] = model if (obj.get("page_type") or obj.get("brief") or obj.get("tags")) else ""
    return obj


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="vault 相对路径(list_books 的 rel)")
    ap.add_argument("--page", type=int, required=True, help="1-based 页码")
    args = ap.parse_args()
    print(json.dumps(gen_brief(args.file, args.page), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
