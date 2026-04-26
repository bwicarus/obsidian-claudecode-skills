"""
PDF 区域内容提取脚本

坐标系：Obsidian 使用 PDF 原生坐标（左下角原点，y 向上），脚本自动转换为 PyMuPDF 坐标。

用法一：直接传入 Obsidian 注解链接
  python pdf_extract.py --link "![[文件.pdf#page=15&rect=23,98,478,192&color=yellow]]"

用法二：手动传入各参数
  python pdf_extract.py --pdf <路径> --page <页码> --x1 <左> --y1 <下> --x2 <右> --y2 <上>

输出前缀：
  TEXT:<内容>   — 从文字层提取
  OCR:<内容>    — Tesseract OCR 识别
  VISION:<内容> — Claude Haiku 视觉分析（OCR 质量不足时）
"""

import argparse
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

VAULT_ROOT = r"C:\obsidian"

# 缓存每个 PDF 的字符修正表
_CORRECTION_CACHE = {}


# ── 字体编码分析 ──────────────────────────────────────────────────────────────

def _glyph_name_to_unicode(name):
    """PostScript 字形名 → Unicode 字符，失败返回 None。"""
    if not name:
        return None
    if len(name) == 1 and name.isascii() and name.isprintable():
        return name
    m = re.fullmatch(r'uni([0-9A-Fa-f]{4,6})', name)
    if m:
        return chr(int(m.group(1), 16))
    m = re.fullmatch(r'u([0-9A-Fa-f]{4,6})', name)
    if m:
        return chr(int(m.group(1), 16))
    try:
        import fitz
        code = fitz.unicode_from_glyph_name(name)
        if code > 0:
            return chr(code)
    except Exception:
        pass
    return None


def _parse_encoding_differences(doc, font_obj_str):
    """解析字体 Encoding Differences，返回 {char_code: glyph_name}。"""
    src = font_obj_str
    enc_ref = re.search(r'/Encoding\s+(\d+)\s+0\s+R', font_obj_str)
    if enc_ref:
        try:
            src = doc.xref_object(int(enc_ref.group(1)))
        except Exception:
            pass

    m = re.search(r'/Differences\s*\[(.*?)\]', src, re.DOTALL)
    if not m:
        return {}

    mapping = {}
    cur = None
    for tok in m.group(1).split():
        if re.fullmatch(r'\d+', tok):
            cur = int(tok)
        elif tok.startswith('/') and cur is not None:
            mapping[cur] = tok[1:]
            cur += 1
    return mapping


def build_char_corrections(pdf_path):
    """
    扫描 PDF 字体的 Encoding Differences，自动识别 ToUnicode 编码错误：

    根本原因：部分 PDF 的 ToUnicode CMap 用 UTF-16 代理对存储补充平面字符，
    某些条目解码失败，输出字符 = 正确字符 - 0x10000（落入韩文音节区间）。

    修正策略：从 Encoding 读取正确的字形名 → 正确 Unicode（如 U+1D45B 𝑛），
    推算出"错误字符"（U+D45B 푛），建立修正映射，不依赖任何硬编码。
    """
    if pdf_path in _CORRECTION_CACHE:
        return _CORRECTION_CACHE[pdf_path]

    import fitz
    doc = fitz.open(pdf_path)
    corrections = {}
    seen = set()

    for page in doc:
        for fi in page.get_fonts(full=True):
            xref = fi[0]
            if xref in seen:
                continue
            seen.add(xref)
            try:
                font_obj = doc.xref_object(xref)
                enc_map = _parse_encoding_differences(doc, font_obj)
                for glyph_name in enc_map.values():
                    correct_char = _glyph_name_to_unicode(glyph_name)
                    if correct_char is None:
                        continue
                    cp = ord(correct_char)
                    # 只处理数学字母符号块（U+1D000–U+1D7FF）
                    if not (0x1D000 <= cp <= 0x1D7FF):
                        continue
                    # ToUnicode 解码失败时输出 correct - 0x10000
                    wrong_cp = cp - 0x10000
                    # 安全检查：必须落在韩文音节区间才可能是误编码
                    if 0xAC00 <= wrong_cp <= 0xD7A3:
                        corrections[chr(wrong_cp)] = correct_char
            except Exception:
                pass

    _CORRECTION_CACHE[pdf_path] = corrections
    return corrections


def fix_chars(text, corrections):
    for wrong, correct in corrections.items():
        text = text.replace(wrong, correct)
    return text


# ── PDF 查找 & 链接解析 ───────────────────────────────────────────────────────

def find_pdf_in_vault(filename):
    if os.path.isabs(filename) and os.path.exists(filename):
        return filename
    direct = os.path.join(VAULT_ROOT, filename)
    if os.path.exists(direct):
        return direct
    target = os.path.basename(filename)
    for root, dirs, files in os.walk(VAULT_ROOT):
        dirs[:] = [d for d in dirs if d != "claude"]
        if target in files:
            return os.path.join(root, target)
    raise FileNotFoundError(f"在 vault 中找不到文件：{filename}")


def parse_obsidian_link(link):
    link = link.strip()
    match = re.match(r'^!?\[\[(.+?\.pdf)#(.+?)\]\]$', link)
    if not match:
        raise ValueError(f"无法解析链接格式：{link}")
    filename = match.group(1)
    params = dict(p.split("=", 1) for p in match.group(2).split("&") if "=" in p)
    page = int(params.get("page", 1))
    rect_str = params.get("rect", "")
    if not rect_str:
        raise ValueError("链接中缺少 rect 参数")
    coords = [float(v) for v in rect_str.split(",")]
    if len(coords) != 4:
        raise ValueError(f"rect 应有 4 个坐标值：{rect_str}")
    return find_pdf_in_vault(filename), page, *coords


# ── 提取方法 ─────────────────────────────────────────────────────────────────

def extract_text_layer(page, rect, corrections):
    return fix_chars(page.get_text("text", clip=rect).strip(), corrections)


def extract_via_ocr(page, rect, corrections, dpi=150):
    import io, fitz
    import pytesseract
    from PIL import Image
    mat = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), clip=rect)
    img = Image.open(io.BytesIO(mat.tobytes("png")))
    return fix_chars(pytesseract.image_to_string(img, lang="chi_sim+eng+jpn").strip(), corrections)


def extract_via_claude(page, rect, corrections, dpi=150):
    import base64, io, fitz, anthropic
    mat = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), clip=rect)
    img_b64 = base64.standard_b64encode(mat.tobytes("png")).decode()
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
            {"type": "text", "text": "请提取并输出图片中的所有文字内容，保持原有排版结构。只输出文字，不要添加任何解释。"},
        ]}],
    )
    return fix_chars(response.content[0].text.strip(), corrections)


# ── 主程序 ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="提取 PDF 指定区域内容")
    parser.add_argument("--link", help="Obsidian PDF 注解链接")
    parser.add_argument("--pdf")
    parser.add_argument("--page", type=int)
    parser.add_argument("--x1", type=float)
    parser.add_argument("--y1", type=float)
    parser.add_argument("--x2", type=float)
    parser.add_argument("--y2", type=float)
    parser.add_argument("--dpi", type=int, default=150)
    args = parser.parse_args()

    try:
        import fitz
    except ImportError:
        print("ERROR: 缺少 PyMuPDF，请运行 pip install pymupdf", file=sys.stderr)
        sys.exit(1)

    if args.link:
        try:
            pdf_path, page, x1, y1, x2, y2 = parse_obsidian_link(args.link)
        except (ValueError, FileNotFoundError) as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.pdf and args.page is not None and None not in (args.x1, args.y1, args.x2, args.y2):
        pdf_path, page, x1, y1, x2, y2 = args.pdf, args.page, args.x1, args.y1, args.x2, args.y2
    else:
        print("ERROR: 请提供 --link 或完整的 --pdf --page --x1 --y1 --x2 --y2", file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(pdf_path):
        print(f"ERROR: 文件不存在：{pdf_path}", file=sys.stderr)
        sys.exit(1)

    corrections = build_char_corrections(pdf_path)

    doc = fitz.open(pdf_path)
    if page < 1 or page > len(doc):
        print(f"ERROR: 页码 {page} 超出范围（共 {len(doc)} 页）", file=sys.stderr)
        sys.exit(1)

    fitz_page = doc[page - 1]
    H = fitz_page.rect.height
    rect = fitz.Rect(x1, H - y2, x2, H - y1)

    text = extract_text_layer(fitz_page, rect, corrections)
    if text:
        print(f"TEXT:{text}")
        return

    try:
        text = extract_via_ocr(fitz_page, rect, corrections, dpi=args.dpi)
        if text and len(text) > 3:
            print(f"OCR:{text}")
            return
    except Exception:
        pass

    try:
        text = extract_via_claude(fitz_page, rect, corrections, dpi=args.dpi)
        print(f"VISION:{text}")
    except Exception as e:
        print(f"ERROR: 所有提取方式均失败：{e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
