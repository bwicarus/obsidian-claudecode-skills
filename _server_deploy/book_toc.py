"""book_toc.py — 书籍目录(TOC)域:原生目录/AI 建目录/页偏移/章节 provenance。

有原生目录默认用原生;「建立目录」AI 整页识图抽出可覆盖。目录是 provenance
(图描述/助手都要知道「书的哪一章节」)的权威来源,也补「书没标准化分段」缺口。
sidecar: state/pdf-toc/<book-sha>.json = {range:{start,end}, entries:[{title,page,level}], built_at}
页偏移镜像: state/pdf-book-offset.json = {rel: PDF页-印刷页}

2026-07-06 结构拆分第 3 刀。依赖经 register_book_toc 注入(不 import pdf_reader,零循环);
⚠ register 必须在 pdf_reader 的 _JOBS/_job_set 定义之后调用(job 基建在源文件靠后)。
用法(pdf_reader.py 两个接点):
    # 原块位置:回导入供 _pam_toc/provenance/assistant.py(pdf._effective_toc)继续用
    from book_toc import _toc_path_abs, _page_offset_for, _book_location, _effective_toc
    # _JOBS/_job_set 定义之后:
    from book_toc import register_book_toc
    register_book_toc(bp, claude_dir=CLAUDE_DIR, book_sha=_book_sha,
                      safe_vault_path=_safe_vault_path, assistant=_assistant,
                      reader_uid=_reader_uid, job_set=_job_set, jobs=_JOBS)
路由:GET /pdf/api/toc、POST /pdf/api/page-offset、POST /pdf/api/build-toc、GET /pdf/api/build-toc-status
部署:cp 本文件到 /home/bwicarus/webapp/(跟 pdf_reader.py 同目录)+ restart webapp。
"""
import json
import threading
import time
from pathlib import Path

from flask import jsonify, request

# register_book_toc 注入(模块 import 时为 None,注册后可用;请求/后台 job 只在注册后发生)
_BOOK_TOC_DIR = None      # Path: state/pdf-toc/
_BOOK_OFFSET_PATH = None  # Path: state/pdf-book-offset.json
_book_sha = None          # callable: abs_path → 书内容 sha
_safe_vault_path = None   # callable: vault 相对路径 → 安全绝对 Path 或 None
_assistant = None         # callable: → assistant 模块(reader_vision 等)
_reader_uid = None        # callable: → 当前用户 uid
_job_set = None           # callable: (jid, **kw) 更新后台 job 状态
_JOBS = None              # dict: job 状态表(status 路由直读)


def _toc_path_abs(abs_path) -> Path:
    return _BOOK_TOC_DIR / f"{_book_sha(abs_path)}.json"

def _toc_load_abs(abs_path) -> dict:
    try:
        p = _toc_path_abs(abs_path)
        return json.loads(p.read_text("utf-8")) if p.exists() else {}
    except Exception:
        return {}

def _toc_save_abs(abs_path, data: dict):
    _BOOK_TOC_DIR.mkdir(parents=True, exist_ok=True)
    p = _toc_path_abs(abs_path)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(p)

def _native_toc_entries(abs_path) -> list:
    """PDF 自带目录 → [{title,page(印刷不可知→PDF页),level}]。get_toc 的 page 是 PDF 页(1基)。"""
    try:
        import fitz
        doc = fitz.open(str(abs_path))
        try:
            toc = doc.get_toc() or []
        finally:
            doc.close()
        return [{"title": str(t[1]).strip(), "page": int(t[2]), "level": int(t[0])} for t in toc if t[2]]
    except Exception:
        return []

def _page_offset_for(rel: str) -> int:
    """页偏移 PDF页-印刷页(前端 localStorage 的服务端镜像,供后台 describe/provenance 把目录印刷页对齐)。"""
    try:
        return int(json.loads(_BOOK_OFFSET_PATH.read_text("utf-8")).get(rel, 0) or 0)
    except Exception:
        return 0

def _effective_toc(abs_path, rel: str = "") -> tuple[list, str]:
    """有效目录(page 归一到**印刷页**):自定义(建过的,page 本就是印刷页)优先覆盖原生(get_toc 是 PDF 页→减 offset)。"""
    cust = _toc_load_abs(abs_path).get("entries")
    if cust:
        return cust, "custom"
    nat = _native_toc_entries(abs_path)
    if nat:
        off = _page_offset_for(rel) if rel else 0
        return [{**e, "page": e["page"] - off} for e in nat], "native"
    return [], "none"

def _book_location(abs_path, printed_page: int, rel: str = "") -> str:
    """provenance:该**印刷页**所属章节标题(取 page ≤ 当前的最深一条)。无目录 → 空串。"""
    entries, _ = _effective_toc(abs_path, rel)
    cands = [e for e in entries if e.get("page") and e["page"] <= printed_page]
    if not cands:
        return ""
    return (max(cands, key=lambda e: e["page"]).get("title") or "").strip()


def _claude_vision_pages(pngs: list, prompt: str, timeout=240):
    """把多张页面图 + 一段 prompt 发给视觉模型(脱壳 claude + Gemini 双后端,按「看图」预设 + 互为兜底),
    返回结果文本。建目录用。prompt 自包含 → 不另加系统提示(system="")。
    (2026-07:删掉从未生效的 model/effort 死参数,模型统一由「看图」action 预设决定。)"""
    import base64
    A = _assistant()
    images = [{"media_type": "image/png", "b64": base64.b64encode(png).decode("ascii")} for png in pngs]
    return A.reader_vision(images, prompt, action="vision", uid=_reader_uid(), system="", timeout=timeout)


def _build_toc_job(jid: str, abs_path, rel: str, start: int, end: int):
    """后台:渲染目录页(start..end,PDF 页)→ 整页图 + 文字层 → AI 抽目录 → 存 sidecar。"""
    import fitz
    try:
        doc = fitz.open(str(abs_path))
        npages = doc.page_count
        s, e = max(1, start), min(npages, end)
        pngs, texts = [], []
        for pg in range(s, e + 1):
            p = doc[pg - 1]
            z = min(2.0, 1600.0 / (max(p.rect.width, p.rect.height) or 1.0))
            pngs.append(p.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False).tobytes("png"))
            texts.append(f"[PDF第{pg}页文字层] " + " ".join((p.get_text("text") or "").split())[:1200])
        doc.close()
        _job_set(jid, status="running", step="AI 识别目录中…")
        prompt = (
            "下面是一本书**目录页**的扫描图(可能多张),附带各页 OCR 文字层(可能有错,以图为准)。\n"
            + "\n".join(texts) + "\n\n"
            "请把整个**目录(章节/小节标题 + 对应页码)**完整抽取出来。注意:\n"
            "- page 用目录里印的**书页码(印刷页码)**,原样填数字;\n"
            "- level 用层级(章=1,节=2,小节=3…,看缩进/编号判断);\n"
            "- 标题保留原文(含编号,如「1-1-2 共通フレーム」);别漏条目、别合并、别翻译。\n"
            "**只输出一个 JSON 数组**,每条 {\"title\":\"...\",\"page\":数字,\"level\":数字};"
            "不要 ``` 围栏、不要数组以外任何文字。"
        )
        raw = _claude_vision_pages(pngs, prompt)
        entries = _parse_toc(raw)
        if not entries:
            _job_set(jid, status="error", error="AI 没抽出目录(可识别页范围/换强模型重试)")
            return
        data = _toc_load_abs(abs_path)
        data.update({"entries": entries, "range": {"start": s, "end": e},
                     "built_at": int(time.time()), "source": "custom"})
        _toc_save_abs(abs_path, data)
        _job_set(jid, status="done", count=len(entries))
    except Exception as ex:
        _job_set(jid, status="error", error=str(ex))

def _parse_toc(raw):
    """解析 AI 回的目录 JSON 数组 → [{title,page,level}]。失败/空 → []。
    **顽强解析**:整体 json.loads 一旦因某一条目坏掉(最常见:title 里有未转义引号,如「§34-2 求"表观"运动」)就会全军覆没,
    所以坏了就退化到**逐对象**解析——单条坏的再用正则硬抠 title/page/level,一条坏数据绝不毁掉整本目录(实测 344 条只 1 条坏,旧逻辑 0 存活)。"""
    import re
    if not raw:
        return []
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else ""
        if s.endswith("```"):
            s = s[:-3]
    i, j = s.find("["), s.rfind("]")
    if i < 0 or j < 0:
        return []
    body = s[i:j + 1]
    arr = None
    try:
        arr = json.loads(body)
    except Exception:
        arr = None
    if not isinstance(arr, list):   # 整体坏 → 逐对象救
        arr = []
        for m in re.finditer(r'\{[^{}]*\}', body):
            o = m.group(0)
            try:
                arr.append(json.loads(o))
            except Exception:
                pm = re.search(r'"page"\s*:\s*(\d+)', o)
                lm = re.search(r'"level"\s*:\s*(\d+)', o)
                tm = re.search(r'"title"\s*:\s*"(.*)"\s*,\s*"page"', o, re.S)   # 贪婪到最后一个引号→容忍 title 内裸引号
                if pm and tm:
                    arr.append({"title": tm.group(1), "page": int(pm.group(1)),
                                "level": int(lm.group(1)) if lm else 1})
    out = []
    for e in (arr if isinstance(arr, list) else []):
        if not isinstance(e, dict):
            continue
        title = str(e.get("title") or "").strip()
        try:
            page = int(e.get("page"))
        except Exception:
            continue
        if not title or page < 1:
            continue
        try:
            level = max(1, int(e.get("level") or 1))
        except Exception:
            level = 1
        out.append({"title": title, "page": page, "level": level})
    return out


def register_book_toc(bp, *, claude_dir, book_sha, safe_vault_path, assistant,
                      reader_uid, job_set, jobs):
    """挂 TOC 路由到 bp(url_prefix /pdf),并注入 pdf_reader 的依赖(见模块头)。"""
    global _BOOK_TOC_DIR, _BOOK_OFFSET_PATH, _book_sha, _safe_vault_path
    global _assistant, _reader_uid, _job_set, _JOBS
    _BOOK_TOC_DIR = claude_dir / "state" / "pdf-toc"
    _BOOK_OFFSET_PATH = claude_dir / "state" / "pdf-book-offset.json"
    _book_sha = book_sha
    _safe_vault_path = safe_vault_path
    _assistant = assistant
    _reader_uid = reader_uid
    _job_set = job_set
    _JOBS = jobs

    @bp.route("/api/toc")
    def pdf_api_toc_get():
        """GET ?file= → {ok, exists, source(custom|native|none), count, range}。前端据此显示『已存在目录』。"""
        rel = request.args.get("file", "")
        ap = _safe_vault_path(rel)
        if not ap:
            return jsonify({"ok": False, "error": "bad file"}), 400
        entries, source = _effective_toc(ap, rel)
        rng = (_toc_load_abs(ap).get("range")) or {}
        return jsonify({"ok": True, "exists": bool(entries), "source": source,
                        "count": len(entries), "range": rng})

    @bp.route("/api/page-offset", methods=["POST"])
    def pdf_api_page_offset_set():
        """前端页码对齐时把 PDF页-印刷页 偏移镜像到服务端(供后台 describe/provenance 用)。{file, offset}。"""
        data = request.get_json(silent=True) or {}
        rel = data.get("file", "")
        if not rel:
            return jsonify({"ok": False, "error": "no file"}), 400
        try:
            off = int(data.get("offset") or 0)
        except Exception:
            off = 0
        try:
            m = json.loads(_BOOK_OFFSET_PATH.read_text("utf-8")) if _BOOK_OFFSET_PATH.exists() else {}
        except Exception:
            m = {}
        m[rel] = off
        try:
            _BOOK_OFFSET_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = _BOOK_OFFSET_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(m, ensure_ascii=False), "utf-8")
            tmp.replace(_BOOK_OFFSET_PATH)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({"ok": True})

    @bp.route("/api/build-toc", methods=["POST"])
    def pdf_api_build_toc():
        """POST {file, start, end} → 后台 AI 整页识图建目录(覆盖原生)。返回 {ok, jid} 供轮询。"""
        data = request.get_json(silent=True) or {}
        rel = data.get("file", "")
        ap = _safe_vault_path(rel)
        if not ap:
            return jsonify({"ok": False, "error": "bad file"}), 400
        try:
            start = int(data.get("start")); end = int(data.get("end"))
        except Exception:
            return jsonify({"ok": False, "error": "目录起止页要填数字"}), 400
        if start < 1 or end < start or (end - start) > 30:
            return jsonify({"ok": False, "error": "起止页不合法(end≥start,且范围≤30页)"}), 400
        jid = "toc" + str(int(time.time() * 1000))
        _job_set(jid, status="running", step="渲染目录页…", ts=int(time.time()))
        threading.Thread(target=_build_toc_job, args=(jid, ap, rel, start, end), daemon=True).start()
        return jsonify({"ok": True, "jid": jid})

    @bp.route("/api/build-toc-status")
    def pdf_api_build_toc_status():
        """GET ?jid= → {status:running|done|error, step?, count?, error?}。"""
        j = dict(_JOBS.get(request.args.get("jid", "")) or {})
        if not j:
            return jsonify({"ok": False, "error": "no job"}), 404
        return jsonify({"ok": True, **{k: j.get(k) for k in ("status", "step", "count", "error")}})
