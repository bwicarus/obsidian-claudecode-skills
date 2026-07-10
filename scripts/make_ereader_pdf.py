#!/usr/bin/env python3
"""把扫描书拼成「正常排版的电子版 PDF」——**交给 MuPDF 内置的 Story 排版引擎自动流式分页**。

纯电子版,不钉原始坐标;内容良好地灌进 PDF、行距自由:
  ① 从原书每页拿 OCR 文字块(带字号) + YOLO 框出的图/表/公式区;
  ② 文字按字号归档 → 映射成 **正文 / 小标题 / 大标题** 三种统一样式(HTML 的 <p>/<h2>/<h1>);
  ③ 把「文字段 + 区域裁图(<img>)」按阅读顺序拼成一份 **HTML**,交 `fitz.Story` +
     `DocumentWriter` 自动换行、自动分页 —— 引擎保证**不重叠、不丢内容**,CSS 控制行距;
  ④ 图/表/公式从原扫描按框裁图嵌入(保真)。
中文断行由 MuPDF 的 HTML 引擎原生处理;字体 Noto Sans CJK(中日+拉丁+数字全覆盖,CFF 也能嵌)。

用法: make_ereader_pdf.py <原PDF> <输出PDF> [--pages 22-56] [--cropzoom 2.0]
"""
import sys, re, json, hashlib, argparse, tempfile, shutil, subprocess
from pathlib import Path
from collections import defaultdict, Counter
from html import escape
import fitz

FONT_REG = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
FONT_BLD = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
FIG_DIR = Path(__file__).resolve().parent.parent / "state" / "pdf-figures"
# 字体子集化(把整套 Noto CJK 16MB 砍到本书用到的字形 ~1MB)。
# ⚠ MuPDF 的 Story 引擎按**原始 GID** 取字 → 必须 --retain-gids,否则字形全错位。
# ⚠ TTC 里 0=JP 1=KR 2=SC 3=TC 4=HK;共享码位的汉字字形按字面走,简体必须取 SC=2。
PYFTSUBSET = Path(__file__).resolve().parent.parent / ".venvs" / "fonttools" / "bin" / "pyftsubset"
SC_FACE = 2


def _subset_font(src_ttc, chars_file, out_path):
    subprocess.run([str(PYFTSUBSET), src_ttc, f"--font-number={SC_FACE}",
                    f"--text-file={chars_file}", f"--output-file={out_path}",
                    "--retain-gids", "--desubroutinize", "--notdef-outline"],
                   check=True, timeout=600,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


_RE_WS = re.compile(r"\s+")
_RE_SP_L = re.compile(r"(?<![A-Za-z]) ")   # 空格左邻不是英文字母 → 删
_RE_SP_R = re.compile(r" (?![A-Za-z])")    # 空格右邻不是英文字母 → 删


def clean_text(s):
    """中文段落基本不该有空格:只保留两侧都是英文字母的空格(免得 the box 粘成 thebox),其余全删。"""
    s = _RE_WS.sub(" ", s).strip()
    s = _RE_SP_L.sub("", s)
    s = _RE_SP_R.sub("", s)
    return s

CSS = """
@font-face { font-family: cjk; src: url(noto.ttc); }
@font-face { font-family: cjk; font-weight: bold; src: url(noto-b.ttc); }
* { font-family: cjk; }
body { font-size: 11pt; line-height: 1.7; color: #000; }
p  { margin: 0 0 8pt 0; text-indent: 2em; text-align: justify; }
h1 { font-size: 17pt; font-weight: bold; margin: 18pt 0 10pt; line-height: 1.4; }
h2 { font-size: 13.5pt; font-weight: bold; margin: 12pt 0 6pt; line-height: 1.4; }
img { display: block; margin: 8pt auto; }
img.m { display: inline; vertical-align: -0.12em; margin: 0 1pt; }
"""

OUT_BODY = 11.0   # 输出正文字号(pt),须与 CSS body font-size 一致;行内公式裁图按此缩放对齐


def book_sha(abs_path) -> str:
    return hashlib.sha1(str(Path(abs_path).resolve()).encode("utf-8")).hexdigest()[:16]


def load_regions(sha):
    p = FIG_DIR / f"{sha}.json"
    reg = defaultdict(list)
    if not p.exists():
        return reg
    d = json.loads(p.read_text("utf-8"))
    for f in (d.get("figures_geom") or d.get("figures") or []):
        bb = f.get("fbox") or f.get("bbox")
        if bb and f.get("page"):
            reg[int(f["page"])].append(bb)
    for f in (d.get("formulas") or []):
        bb = f.get("bbox")
        if bb and f.get("page"):
            reg[int(f["page"])].append(bb)
    return reg


def parse_pages(spec, n):
    if not spec:
        return list(range(1, n + 1))
    out = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-"); out += list(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return [p for p in out if 1 <= p <= n]


def collect_tiers(src, pages):
    """统计全书字号(按字数加权)→ 聚成几个等级。相邻 ≤8% 归一档,过滤占比 <1.5% 的噪声档。
    返回 [(字号, 占比)…],并标出占比最大的"正文档"。"""
    agg = defaultdict(float)
    for pno in pages:
        for blk in src[pno - 1].get_text("dict").get("blocks", []):
            if blk.get("type", 0) != 0:
                continue
            for ln in blk.get("lines", []):
                for sp in ln.get("spans", []):
                    t = (sp.get("text") or "").strip()
                    if t:
                        agg[round(sp.get("size", 10.0), 1)] += len(t)
    tiers = []
    for s, w in sorted(agg.items()):
        if tiers and s <= tiers[-1][0] * 1.08:
            c, tw = tiers[-1]; tiers[-1] = ((c * tw + s * w) / (tw + w), tw + w)
        else:
            tiers.append((s, w))
    total = sum(t[1] for t in tiers) or 1.0
    keep = [(round(c, 2), w / total) for c, w in tiers if w >= total * 0.015]
    return keep or [(10.0, 1.0)]


def block_info(blk):
    """(rawdict 块) 代表字号(众数·按字数加权)、整段文字、左边、**最后一行右边**、行数、
    以及逐字 [{c,size,bbox}…](供行内公式切片)。最后一行右边用于判断续行 vs 段落结束。"""
    cnt = Counter(); lines = []; chars = []
    bls = blk.get("lines", [])
    last_x1 = blk.get("bbox", (0, 0, 0, 0))[2]
    for ln in bls:
        lt = []
        for sp in ln.get("spans", []):
            ssz = round(sp.get("size", 10.0), 1)
            for ch in sp.get("chars", []):
                c = ch.get("c", "")
                lt.append(c)
                chars.append({"c": c, "size": ssz, "bbox": ch.get("bbox")})
                if c.strip():
                    cnt[ssz] += 1
        lines.append("".join(lt))
        if ln.get("spans"):
            last_x1 = ln.get("bbox", (0, 0, last_x1, 0))[2]
    bb = blk.get("bbox", (0, 0, 0, 0))
    size = cnt.most_common(1)[0][0] if cnt else 10.0
    return size, "".join(lines), bb[0], last_x1, len(bls), chars


def _is_cjk(c):
    o = ord(c)
    return 0x3400 <= o <= 0x9FFF or 0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFFEF


def cjk_ratio(s):
    """非空白字符里中日文(含中文标点/全角)占比。低 → 整块是公式/数字/单位行,不该信 OCR。"""
    cc = [c for c in s if not c.isspace()]
    if not cc:
        return 1.0
    return sum(1 for c in cc if _is_cjk(c) or c in "—…") / len(cc)


# 数学/符号字符:出现在「非中文 run」里就判定该 run 是公式 → 裁原图(不信 OCR)。
_MATH = set("×÷±∓·∘°′″‴∝√∛∜∫∮∑∏∞≈≅≡≠≤≥≮≯∈∉∋⊂⊃⊆⊇∪∩∅∂∇⊥∥∠∟⌒→←↑↓↔⇀⇒⇔↦"
            "½⅓⅔¼¾⅕⅖⅗⅘⅙⅛∙•※≪≫⟨⟩∴∵⁻⁺⁼⁽⁾ⁿ⁰¹²³⁴⁵⁶⁷⁸⁹₀₁₂₃₄₅₆₇₈₉∆Ω℃Å")


def _greek(c):
    o = ord(c)
    return 0x0391 <= o <= 0x03A9 or 0x03B1 <= o <= 0x03C9


def _run_is_formula(run):
    """非中文 run 是否当公式裁图(**只裁真公式**,纯数字/字母走文字——它们在字体里好好的):
    ① 含数学符号/希腊字母 → 裁;② 有真·上下标(run 内字符**基线高低差** > 35% 行高,如 10⁻⁸ 的指数
    位置偏上)→ 裁。**不**再用「字号忽大忽小」判(OCR 噪声会把普通数字误判成公式 → 过度裁图)。"""
    vis = [d for d in run if d["c"].strip() and d.get("bbox")]
    if not vis:
        return False
    if any(c in _MATH or _greek(c) for c in (d["c"] for d in vis)):
        return True
    if len(vis) >= 2:                                       # 上下标:字符竖直中心拉开
        ys = [(d["bbox"][1] + d["bbox"][3]) / 2 for d in vis]
        hs = sorted(d["bbox"][3] - d["bbox"][1] for d in vis)
        medh = hs[len(hs) // 2]
        if medh > 0 and (max(ys) - min(ys)) / medh > 0.35:
            return True
    return False


def segment_block(chars):
    """把一段(逐字)切成 [('t', 文本) | ('m', 裁图矩形)…]:中文/普通数字字母走文字,
    含数学符号或上下标的连续非中文 run → 裁原扫描图内联(保真,绕开 OCR 的数学字符错)。"""
    segs = []; tbuf = []; run = []

    def flush_text():
        if tbuf:
            segs.append(("t", "".join(tbuf))); tbuf.clear()

    def flush_run():
        if not run:
            return
        if _run_is_formula(run):
            flush_text()
            bx = [d["bbox"] for d in run if d["bbox"]]
            szs = [d["size"] for d in run if d["c"].strip() and d["size"] > 0]
            if bx:
                rep = max(szs) if szs else 10.0             # run 主字号(最大字),用于缩放到≈正文
                segs.append(("m", fitz.Rect(min(b[0] for b in bx), min(b[1] for b in bx),
                                            max(b[2] for b in bx), max(b[3] for b in bx)), rep))
        else:
            tbuf.append("".join(d["c"] for d in run))
        run.clear()

    for d in chars:
        c = d["c"]
        if not c:
            continue
        if _is_cjk(c):
            flush_run(); tbuf.append(c)
        elif c.isspace():
            if run:
                run.append(d)                               # 公式内的空格留着,中文间的丢
        else:
            run.append(d)
    flush_run(); flush_text()
    return segs


def make(src_path, out_path, pages_spec=None, cropzoom=2.0, progress_path=None):
    import os
    def _prog(done, total, status="running", phase="extract"):
        if not progress_path:
            return
        # 综合百分比:抽取 0-45% / 排版 45-90% / 收尾 90-100%(大书 pass2 也有进度,bar 不卡 99%)
        f = done / max(1, total)
        pct = {"extract": 45 * f, "layout": 45 + 45 * f}.get(phase, 90 + 10 * f)
        try:
            Path(progress_path).write_text(json.dumps(
                {"pid": os.getpid(), "done": int(done), "total": int(total), "phase": phase,
                 "percent": round(min(100.0, pct), 1), "status": status,
                 "ts": int(__import__("time").time())}), "utf-8")
        except Exception:
            pass
    src = fitz.open(src_path)
    reg = load_regions(book_sha(src_path))
    pages = parse_pages(pages_spec, src.page_count)
    _prog(0, len(pages))
    tinfo = collect_tiers(src, pages)
    tier_vals = [t[0] for t in tinfo]
    body_tier = sorted(tinfo, key=lambda t: -t[1])[0][0]    # 占比最大档 = 正文

    def tag(size):
        r = size / body_tier
        return "h1" if r >= 1.25 else ("h2" if r >= 1.08 else "p")

    MB = fitz.paper_rect("a4")                              # 595×842pt
    MARGIN = 54
    WHERE = fitz.Rect(MARGIN, MARGIN, MB.width - MARGIN, MB.height - MARGIN)
    FRAME_W = WHERE.width

    tmpdir = Path(tempfile.mkdtemp(prefix="ever_"))
    n_par = n_img = 0
    parts = []
    used = set()
    try:
        # ── Pass 1:抽全书条目流 ──
        # 元素: ('head', role, text) / ('p', text, cont) / ('crop', pno, rect)
        #   cont=True 表示该正文块最后一行顶到右边距 → 下一块是续行,不断段。
        stream = []
        for _i, pno in enumerate(pages):
            if _i % 5 == 0:
                _prog(_i, len(pages))
            sp = src[pno - 1]; R = sp.rect; H = R.height
            rects = []
            for bb in reg.get(pno, []):
                try:
                    r = fitz.Rect(bb[0] * R.width, bb[1] * R.height, bb[2] * R.width, bb[3] * R.height)
                    if r.width > 2 and r.height > 2:
                        rects.append(r)
                except Exception:
                    pass
            raw = []
            for blk in sp.get_text("rawdict").get("blocks", []):
                if blk.get("type", 0) != 0:
                    continue
                bb = blk.get("bbox")
                if not bb:
                    continue
                cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
                if any(r.x0 <= cx <= r.x1 and r.y0 <= cy <= r.y1 for r in rects):
                    continue                                # 落在图/公式区 → 裁图呈现
                size, txt, bx0, lx1, nl, chars = block_info(blk)
                ctxt = clean_text(txt)
                if not ctxt.strip():
                    continue
                raw.append((bb, size, ctxt, bx0, lx1, nl, chars))
            # 本页正文档的左右文字边距(判断续行用)
            bodyb = [t for t in raw if tag(t[1]) == "p"]
            bright = max((t[4] for t in bodyb), default=R.x1 - 50)
            bleft = min((t[3] for t in bodyb), default=R.x0 + 50)
            fw = max(1.0, bright - bleft)
            page_items = []                                 # (y0, kind, payload...)
            for bb, size, txt, bx0, lx1, nl, chars in raw:
                y0, y1 = bb[1], bb[3]
                if nl <= 1 and len(txt) < 45 and (y0 < 0.06 * H or y1 > 0.93 * H):
                    continue                                # 页眉/页脚/页码 → 丢
                role = tag(size)
                if role != "p":
                    page_items.append((y0, "head", role, txt))
                elif cjk_ratio(txt) < 0.5:
                    page_items.append((y0, "cropb", fitz.Rect(bb)))   # 整块公式/数字行 → 裁图
                else:
                    cont = lx1 >= bright - max(2.5 * size, 0.06 * fw)
                    psegs = []                              # 行内切片:文本 + 行内公式裁图
                    for seg in segment_block(chars):
                        if seg[0] == "t":
                            ct = clean_text(seg[1])
                            if ct:
                                psegs.append(("t", ct))
                        else:
                            psegs.append(("m", pno, seg[1], seg[2]))  # seg[2]=run 主字号,供按字号缩放
                    page_items.append((y0, "p", psegs, cont))
            for r in rects:
                page_items.append((r.y0, "cropr", r))
            page_items.sort(key=lambda d: d[0])
            for it in page_items:
                k = it[1]
                if k == "head":
                    stream.append(("head", it[2], it[3]))
                elif k == "p":
                    stream.append(("p", it[2], it[3]))      # it[2] = psegs 列表
                else:
                    stream.append(("crop", pno, it[2]))

        # ── Pass 2:连续正文按"续行"合并成自然段 + 裁图 + 组 HTML ──
        cur = []                                            # 累积中的段落(切片列表)

        def render_crop(pno, r, zoom, quality=82):
            name = f"f{n_img}.jpg"
            pm = src[pno - 1].get_pixmap(clip=r, matrix=fitz.Matrix(zoom, zoom), alpha=False)
            pm.save(str(tmpdir / name), jpg_quality=quality)
            return name

        def pad_rect(pno, r, top, bot, side):
            """把裁图框往外扩(裁切边距),并夹在页面内。
            OCR 文字层 bbox 常比真实墨迹**偏低**(顶部被切) → 顶部多留;不丢字。"""
            PR = src[pno - 1].rect
            return fitz.Rect(max(PR.x0, r.x0 - side), max(PR.y0, r.y0 - top),
                             min(PR.x1, r.x1 + side), min(PR.y1, r.y1 + bot))

        def crop_inline(pno, r, bsz):
            """行内公式裁图:四周**大扩**保证不切字,再**上下都自动裁到公式本行的墨迹**——
            从公式中心分别往上/往下找到与邻行之间的空白间隙,把间隙以外(邻行的半截)切掉,
            上下都贴着墨迹(无多余空白)。显示高度按裁后墨迹高定到≈正文数字大小(不靠 OCR 字号,避免忽大忽小)。
            返回 (jpg名, 显示宽pt, 显示高pt) 或 None。"""
            from PIL import Image
            core = max(2.0, r.height)
            zoom = 3.0   # 行内公式显示极小(~8pt),3× 够清晰且比 4× 快不少(整本几千 crop 累计省时)
            R2 = pad_rect(pno, r, core * 0.85, core * 0.85, core * 0.10)  # 上下都大扩(可能含邻行)
            pm = src[pno - 1].get_pixmap(clip=R2, matrix=fitz.Matrix(zoom, zoom), alpha=False)
            if pm.n < 3:
                return None
            img = Image.frombytes("RGB", (pm.width, pm.height), pm.samples)
            H = pm.height
            # 每行墨迹密度(0..255):二值化后把宽度压成 1 列(C 实现,快)
            prof = list(img.convert("L").point(lambda p: 255 if p < 160 else 0)
                        .resize((1, H), Image.BILINEAR).getdata())
            thr = 10                                  # >~4% 墨 算有字
            gap = max(3, int(core * zoom * 0.32))     # 行间空白判定:连续这么多空行=换行
            cy = int(((r.y0 + r.y1) / 2 - R2.y0) * zoom)
            cy = max(0, min(H - 1, cy))
            if prof[cy] <= thr:                       # 中心恰落在空隙 → 移到最近的有墨行
                for d in range(1, int(core * zoom)):
                    if cy - d >= 0 and prof[cy - d] > thr: cy -= d; break
                    if cy + d < H and prof[cy + d] > thr: cy += d; break
            top, bot = 0, H                           # 往上找本行真顶
            run = 0; y = cy
            while y > 0:
                y -= 1
                if prof[y] <= thr:
                    run += 1
                    if run >= gap: top = y + run; break
                else:
                    run = 0
            run = 0; y = cy                           # 往下找本行真底
            while y < H - 1:
                y += 1
                if prof[y] <= thr:
                    run += 1
                    if run >= gap: bot = y - run + 1; break
                else:
                    run = 0
            if not (0 <= top < bot <= H) or bot - top < 3:
                top, bot = 0, H
            img = img.crop((0, top, pm.width, bot))
            name = f"f{n_img}.jpg"; img.save(str(tmpdir / name), quality=85)
            band_pt = (bot - top) / zoom              # 裁后真实墨迹高(pt)
            # 按裁后墨迹高定显示大小:目标 ≈ 正文数字高(0.74em),跟两旁字一样大;不用 OCR 字号(忽大忽小)
            target = OUT_BODY * 0.74
            sc = target / band_pt
            if band_pt * sc > 1.45 * OUT_BODY:        # 带上下标的高公式封顶,别撑爆行
                sc = (1.45 * OUT_BODY) / band_pt
            return name, R2.width * sc, band_pt * sc

        def flush():
            nonlocal n_par, n_img
            if not cur:
                return
            html = ""
            for seg in cur:
                if seg[0] == "t":
                    html += escape(seg[1]); used.update(seg[1])
                else:                                       # ('m', pno, rect, bsize) 行内公式裁图
                    _, pno, r, bsz = seg
                    try:
                        ci = crop_inline(pno, r, bsz)         # 大扩+自动裁到本行墨迹(不切顶、不带上一行)
                        if ci:
                            nm, w, h = ci
                            html += f'<img class="m" src="{nm}" style="height:{h:.1f}pt;width:{w:.1f}pt;">'
                            n_img += 1
                    except Exception:
                        pass
            cur.clear()
            if html.strip():
                parts.append(f"<p>{html}</p>"); n_par += 1

        _ntot = len(stream)
        for _si, el in enumerate(stream):
            if _si % 40 == 0:
                _prog(_si, _ntot, phase="layout")
            if el[0] == "p":
                cur.extend(el[1])
                if not el[2]:                               # 没顶到右边距 → 段落到此结束
                    flush()
            elif el[0] == "head":
                flush()
                role, txt = el[1], el[2]
                parts.append(f"<{role}>{escape(txt)}</{role}>"); used.update(txt)
            else:                                           # 整块/区域裁图
                flush()
                pno, r = el[1], el[2]
                try:
                    z = cropzoom if r.height >= 60 else max(cropzoom, 3.0)
                    nm = render_crop(pno, r, z)
                    dw = min(FRAME_W, r.width * 1.3)
                    dh = r.height * (dw / r.width)
                    if dh > WHERE.height:
                        sc = WHERE.height / dh; dw *= sc; dh *= sc
                    parts.append(f'<img src="{nm}" style="width:{dw:.0f}pt;height:{dh:.0f}pt;">')
                    n_img += 1
                except Exception:
                    pass
        flush()

        _prog(0, 1, phase="finalize")   # 进入收尾(字体子集化 + 排版引擎 + 保存)
        html = "<html><body>" + "".join(parts) + "</body></html>"
        # ── 字体子集化:整套 Noto CJK ~16MB → 本书用到的字形 ~1MB(失败则回退整套) ──
        reg_font, bld_font, subset = FONT_REG, FONT_BLD, False
        try:
            if PYFTSUBSET.exists() and used:
                cf = tmpdir / "chars.txt"
                base = "".join(chr(c) for c in range(0x20, 0x7f))   # 总带上 ASCII
                cf.write_text("".join(sorted(used | set(base))), "utf-8")
                sr, sb = tmpdir / "sub-reg.otf", tmpdir / "sub-bold.otf"
                _subset_font(FONT_REG, cf, sr); _subset_font(FONT_BLD, cf, sb)
                if sr.exists() and sb.exists():
                    reg_font, bld_font, subset = str(sr), str(sb), True
        except Exception:
            pass
        arch = fitz.Archive()
        arch.add(reg_font, "noto.ttc"); arch.add(bld_font, "noto-b.ttc")
        for img in tmpdir.glob("*.jpg"):
            arch.add(str(img), img.name)
        story = fitz.Story(html=html, user_css=CSS, archive=arch)
        raw = str(tmpdir / "_raw.pdf")
        writer = fitz.DocumentWriter(raw)
        npg = 0; more = 1
        while more:
            dev = writer.begin_page(MB)
            more, _ = story.place(WHERE)
            story.draw(dev)
            writer.end_page()
            npg += 1
            if npg > 5000:
                break
        writer.close()
        d = fitz.open(raw)
        d.save(out_path, garbage=4, deflate=True, clean=True)
        d.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    osize = Path(src_path).stat().st_size; nsize = Path(out_path).stat().st_size
    src.close()
    _prog(1, 1, status="done", phase="finalize")
    print(f"✓ {out_path}")
    print(f"  字号等级: {tier_vals}  (正文档 {body_tier})  字体子集化: {'是' if subset else '否(回退整套)'}")
    print(f"  原 {len(pages)} 页 → 流式重排 {n_par} 段 + {n_img} 块裁图 → 电子版 {npg} 页(引擎自动分页)")
    print(f"  原(整本) {osize // 1024 // 1024}MB → 电子版(这些页) {nsize // 1024}KB")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("src"); ap.add_argument("out")
    ap.add_argument("--pages", default=None)
    ap.add_argument("--cropzoom", type=float, default=2.0)
    ap.add_argument("--progress", default=None)   # 进度文件路径(web 端轮询用)
    a = ap.parse_args()
    try:
        make(a.src, a.out, a.pages, a.cropzoom, progress_path=a.progress)
    except Exception as e:
        if a.progress:
            try:
                Path(a.progress).write_text(json.dumps({"status": "error", "error": str(e)[:200]}), "utf-8")
            except Exception:
                pass
        raise
