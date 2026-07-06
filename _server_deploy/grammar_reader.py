"""grammar_reader.py — 英语语法分析域(grammar KG 节点/跟踪/spacy worker/AI 分析/历史)。

grammar-nodes.json 三层节点 + per-PDF 跟踪 sidecar + 常驻 spacy worker(锁串行+超时自愈)
+ 语法分析(全量缓存/spacy 缓存/AI 兜底)+ grammar-stream(SSE + 回放缓存)+ 按书历史。
完整系统文档:references/grammar-analysis-system.md。

2026-07-06 结构拆分第 4 刀(单词本 tab 段仍在 pdf_reader.py,不属本域)。依赖经
register_grammar 注入;路由用 add_url_rule 挂载(函数体从 pdf_reader.py 逐行原样搬)。
用法(pdf_reader.py):
    from grammar_reader import register_grammar
    register_grammar(bp, claude_dir=CLAUDE_DIR, spacy_py=SPACY_PY, spacy_script=SPACY_SCRIPT,
                     ai_call=_ai_call, cstat=_cstat, spacy_available=_spacy_available,
                     start_ai_stream=_start_ai_stream, reader_uid=_reader_uid)
    from grammar_reader import _GRAMMAR_TRACKED_DIR   # 改名迁移 _mv 仍引用
部署:cp 本文件到 /home/bwicarus/webapp/(跟 pdf_reader.py 同目录)+ restart webapp。
"""
import json
import sys
import threading as _threading
from pathlib import Path

from flask import jsonify, request

# register_grammar 注入(模块 import 时为 None,注册后可用)
_GRAMMAR_NODES_PATH = None    # Path: _server_deploy/grammar-nodes.json(build_grammar_nodes.py 产物)
_GRAMMAR_TRACKED_DIR = None   # Path: state/grammar-tracked/(per-PDF 跟踪节点 sidecar)
_GRAMMAR_CACHE_DIR = None     # Path: state/grammar-cache/(sentence-only 分析缓存)
_GRAMMAR_HISTORY_DIR = None   # Path: state/grammar-history/(按书历史)
CLAUDE_DIR = None             # Path: 项目根(KG 目录/scripts 路径用)
SPACY_PY = None               # Path: spacy-venv python
SPACY_SCRIPT = None           # Path: scripts/spacy_parse.py
_ai_call = None               # callable: 同步 AI 调用(analyze 兜底)
_cstat = None                 # callable: 计数统计
_spacy_available = None       # callable: → bool(spacy-venv 是否可用)
_start_ai_stream = None       # callable: 抗断连流式 AI(rid 后台生成)
_reader_uid = None            # callable: → 当前用户 uid

_GRAMMAR_ZH_CACHE: dict = {}    # 语法分析 token→简明中文 memo(ECDICT 静态库,常驻进程跨请求复用)

# ─── 英语语法分析 ─────────────────────────────────────────────────────────



def _grammar_nodes() -> list[dict]:
    try:
        data = json.loads(_GRAMMAR_NODES_PATH.read_text("utf-8"))
        return data.get("nodes") or []
    except Exception:
        return []


def _tracked_path(file_rel: str) -> Path:
    import hashlib
    sha = hashlib.sha1(file_rel.encode("utf-8")).hexdigest()[:16]
    _GRAMMAR_TRACKED_DIR.mkdir(parents=True, exist_ok=True)
    return _GRAMMAR_TRACKED_DIR / f"{sha}.json"


def _tracked_get(file_rel: str) -> list[str]:
    p = _tracked_path(file_rel)
    if not p.exists(): return []
    try:
        return list(json.loads(p.read_text("utf-8")).get("tracked", []))
    except Exception:
        return []


def _tracked_set(file_rel: str, tracked: list[str]):
    p = _tracked_path(file_rel)
    p.write_text(json.dumps({"pdf_rel": file_rel, "tracked": tracked},
                            ensure_ascii=False, indent=2), "utf-8")


def pdf_api_grammar_nodes():
    """所有可跟踪的语法节点 list（旧 demo 数据；保留兼容）"""
    return jsonify({"ok": True, "nodes": _grammar_nodes()})


def pdf_api_grammar_books():
    """列出所有 kind=grammar 的 KG，含每本书 tracked 节点数。
    返回 {ok, books: [{book, title, total_l2, tracked_count}]}"""
    kg_dir = CLAUDE_DIR / "knowledge_graph"
    out = []
    for kg_f in kg_dir.glob("*.json"):
        if kg_f.name.endswith(".bak.json"): continue
        try:
            kg = json.loads(kg_f.read_text("utf-8"))
        except Exception:
            continue
        if kg.get("kind") != "grammar":
            continue
        nodes = kg.get("nodes") or []
        l2 = [n for n in nodes if n.get("level") == 2]
        tracked = [n for n in l2 if n.get("tracked")]
        out.append({
            "book": kg.get("book") or kg_f.stem,
            "title": kg.get("title") or kg.get("book") or kg_f.stem,
            "total_l2": len(l2),
            "tracked_count": len(tracked),
        })
    out.sort(key=lambda x: x["book"])
    return jsonify({"ok": True, "books": out})


def _collect_grammar_tracked_nodes(enabled_books: list[str]) -> list[dict]:
    """汇总指定 KG 中所有 tracked level-2 节点（合并视图给 AI 用）。"""
    kg_dir = CLAUDE_DIR / "knowledge_graph"
    out = []
    for b in enabled_books:
        kg_f = kg_dir / f"{b}.json"
        if not kg_f.exists(): continue
        try:
            kg = json.loads(kg_f.read_text("utf-8"))
        except Exception:
            continue
        if kg.get("kind") != "grammar": continue
        for n in kg.get("nodes") or []:
            if n.get("level") == 2 and n.get("tracked"):
                out.append({
                    "id": n["id"], "name": n.get("name", ""),
                    "summary": n.get("summary", ""),
                    "book": b,
                })
    return out


def pdf_api_grammar_tracked():
    """新语义：per-PDF 启用的 grammar KG 书列表。
    用户在技能树页面 toggle 节点 tracked；PDF reader 这里勾选哪些书启用。
    分析时合并所有启用书的 tracked 节点。
    GET ?file=<rel> → {ok, enabled_books: [...]}
    POST {file, enabled_books: [...]} → {ok}"""
    if request.method == "GET":
        rel = (request.args.get("file") or "").strip()
        if not rel: return jsonify({"ok": False, "error": "no file"}), 400
        data = _load_grammar_enabled(rel)
        return jsonify({"ok": True, "enabled_books": data})
    data = request.get_json(silent=True) or {}
    rel = (data.get("file") or "").strip()
    books = data.get("enabled_books") or []
    if not rel or not isinstance(books, list):
        return jsonify({"ok": False, "error": "invalid"}), 400
    _save_grammar_enabled(rel, [str(b) for b in books])
    return jsonify({"ok": True})


def _load_grammar_enabled(file_rel: str) -> list[str]:
    p = _tracked_path(file_rel)
    if not p.exists(): return []
    try:
        d = json.loads(p.read_text("utf-8"))
        # 兼容老格式（tracked 是 node ids）+ 新格式（enabled_books）
        if "enabled_books" in d:
            return list(d["enabled_books"])
        return []
    except Exception:
        return []


def _save_grammar_enabled(file_rel: str, books: list[str]):
    p = _tracked_path(file_rel)
    p.write_text(json.dumps({"pdf_rel": file_rel, "enabled_books": books},
                            ensure_ascii=False, indent=2), "utf-8")


_SPACY_LOCK = _threading.Lock()
_SPACY_PROC: list = [None]    # 常驻 spacy worker 进程盒(2026-06-10:原每句 spawn 子进程,固定付 ~3.3s 进程+模型加载税)


def _spacy_worker_kill():
    p = _SPACY_PROC[0]
    try:
        if p:
            p.kill()
    except Exception:
        pass


import atexit as _atexit
_atexit.register(_spacy_worker_kill)   # gunicorn 重启不留孤儿


def _spacy_worker_request(sentence: str, timeout: float = 10.0) -> str | None:
    """向常驻 spacy worker(spacy_parse.py --server)发一行 JSON、读一行响应。
    锁覆盖完整收发(8 gthread 下防响应错配);进程没起/死了就地拉起(首次含模型加载 ~3s);
    超时/异常 → kill+置空下次重拉,返回 None(调用方走既有 AI 兜底,语义不变)。"""
    import subprocess
    with _SPACY_LOCK:
        first = _SPACY_PROC[0] is None
        out: list = []

        def _io():
            try:
                p = _SPACY_PROC[0]
                if p is None or p.poll() is not None:
                    p = subprocess.Popen(
                        [str(SPACY_PY), str(SPACY_SCRIPT), "--server"],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, text=True, bufsize=1)
                    _SPACY_PROC[0] = p          # 先存(超时路径才能 kill 到),再等 ready
                    if not p.stdout.readline():  # {"ready":true}
                        raise RuntimeError("worker no ready")
                p.stdin.write(json.dumps({"text": sentence}, ensure_ascii=False) + "\n")
                p.stdin.flush()
                out.append(p.stdout.readline())
            except Exception:
                pass

        t = _threading.Thread(target=_io, daemon=True)
        t.start()
        t.join(timeout + (8.0 if first else 0.0))   # 首次多给模型加载时间
        if t.is_alive() or not out or not (out[0] or "").strip():
            _spacy_worker_kill()
            _SPACY_PROC[0] = None
            return None
        return out[0]


def _spacy_grammar(sentence: str) -> dict | None:
    """用 spaCy 常驻 worker 做词性 + 依存分析；ECDICT 补每词中文义、MyMemory 译整句。
    返回 {tokens, deps, sentence_zh}；失败返回 None（调用方回退 AI）。"""
    line = _spacy_worker_request(sentence)
    if not line:
        return None
    try:
        parsed = json.loads(line)
    except Exception:
        return None
    if parsed.get("error"):
        return None
    tokens = parsed.get("tokens") or []
    deps = parsed.get("deps") or []
    clauses = parsed.get("clauses") or []
    components = parsed.get("components") or []
    clause_tree = parsed.get("clause_tree")
    if not tokens:
        return None
    # ECDICT 补每个词的简明中文义（离线、毫秒级）；主 tokens + 各子句 tokens 共用一份缓存
    try:
        vp = str(CLAUDE_DIR / "scripts" / "vocab")
        if vp not in sys.path:
            sys.path.insert(0, vp)
        import dict_sources  # type: ignore
        _zh_cache = _GRAMMAR_ZH_CACHE   # 模块级 memo:ECDICT 是静态库,跨请求复用(含 miss 空串负缓存)
        if len(_zh_cache) > 20000:
            _zh_cache.clear()
        def _zh(w: str) -> str:
            w = (w or "").strip()
            if not w or not w[0].isalpha():
                return ""
            key = w.lower()
            if key in _zh_cache:
                return _zh_cache[key]
            z = ""
            try:
                ec = dict_sources.lookup_ecdict(w)
                if ec:
                    for d in dict_sources._ec_definitions(ec):
                        if d.get("zh"):
                            z = d["zh"][:30]
                            break
            except Exception:
                pass
            _zh_cache[key] = z
            return z
        for tk in tokens:
            tk["zh"] = _zh(tk.get("text", ""))
        for c in clauses:
            for tk in c.get("tokens", []):
                tk["zh"] = _zh(tk.get("text", ""))
        # 嵌套子句树的节点也补中文（占位节点 ref 不补），供可展开弧线图点词看翻译
        def _fill_tree_zh(node):
            if not node:
                return
            for nd in node.get("nodes", []):
                if nd.get("ref") is None:
                    nd["zh"] = _zh(nd.get("text", ""))
            for ch in node.get("children", []):
                _fill_tree_zh(ch)
        _fill_tree_zh(clause_tree)
    except Exception:
        pass
    # 整句翻译 + 语法点讲解交给 AI 流式（/api/grammar-stream，翻译标志先出）
    # 这里只出词性 + 依存 + 子句切分，秒级零 AI
    return {"tokens": tokens, "deps": deps, "clauses": clauses, "components": components, "clause_tree": clause_tree, "sentence_zh": ""}


def pdf_api_grammar_analyze():
    """spaCy（默认，零 AI）/ AI（兜底）分析句子词性+依存，相对 tracked 语法节点。
    body: {text, sentence?, file, tracked_ids?, model?, effort?}
    返回 {ok, analyses: [{node_id, node_name, point, explanation, examples}]}
    缓存：state/grammar-cache/<sha1(text + sorted(ids))>.json
    """
    import hashlib
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    sentence = (data.get("sentence") or text).strip()
    rel = (data.get("file") or "").strip()
    if not text or len(text) > 2000:
        return jsonify({"ok": False, "error": "no text / too long"}), 400
    enabled_books = data.get("enabled_books") or _load_grammar_enabled(rel)
    if not enabled_books:
        return jsonify({"ok": False, "error": "no enabled grammar KGs; enable in settings + track nodes in skilltree"}), 400
    tracked_nodes = _collect_grammar_tracked_nodes(enabled_books)
    if not tracked_nodes:
        return jsonify({"ok": False, "error": "no tracked level-2 nodes in enabled KGs (toggle 跟踪 in skilltree page)"}), 400

    tracked_ids = [n["id"] for n in tracked_nodes]
    node_by_id = {n["id"]: n for n in tracked_nodes}
    cache_key = hashlib.sha1((sentence + "||" + text + "||" + ",".join(sorted(tracked_ids))).encode("utf-8")).hexdigest()[:20]
    _GRAMMAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_p = _GRAMMAR_CACHE_DIR / f"{cache_key}.json"
    if cache_p.exists():
        try:
            _cd = json.loads(cache_p.read_text("utf-8"))
            # 同键文件可能被 grammar-stream 先建(只含 ai_stream_full)→ 必须有真分析内容才算命中
            if _cd.get("tokens") or _cd.get("analyses"):
                _cstat("grammar.full_hit")
                return jsonify({"ok": True, "from_cache": True, **_cd})
        except Exception:
            pass
    # spaCy 输出只依赖 sentence(不看 text/tracked_ids)→ 单独的 sentence-only 键:
    # 换选中焦点词/toggle 任一跟踪节点不再全量重付分析(2026-06-10)。先查全键(兼容存量
    # 含 AI 结果的条目),miss 再查 sp 键。若将来 spaCy 路径用 tracked_ids 做规则匹配,键需加回。
    sp_key = hashlib.sha1(("spacy||" + sentence).encode("utf-8")).hexdigest()[:20]
    sp_p = _GRAMMAR_CACHE_DIR / f"{sp_key}.json"
    if sp_p.exists():
        try:
            _out = json.loads(sp_p.read_text("utf-8"))
            _cstat("grammar.sp_hit")
            return jsonify({"ok": True, "from_cache": True, **_out})
        except Exception:
            pass

    # ── 优先 spaCy 本地分析（词性 + 依存，零 AI、秒级）──
    if _spacy_available():
        _cstat("grammar.spacy_run")
        sp = _spacy_grammar(sentence)
        if sp is not None:
            out = {
                "sentence_zh": sp.get("sentence_zh", ""),
                "tokens":      sp.get("tokens", []),
                "deps":        sp.get("deps", []),
                "clauses":     sp.get("clauses", []),   # 长句按从句切段
                "components":  sp.get("components", []), # 句子成分分块
                "clause_tree": sp.get("clause_tree"),    # 嵌套子句树(可展开弧线)
                "analyses":    [],   # 语法点匹配暂不在 spaCy 路径做（后续可加规则）
                "engine":      "spacy",
            }
            try:
                sp_p.write_text(json.dumps(out, ensure_ascii=False, indent=2), "utf-8")
            except Exception:
                pass
            return jsonify({"ok": True, **out})
        # spaCy 失败则继续走下面 AI 兜底

    # 构 prompt
    nodes_block = "\n".join(
        f"- [{n['id']}] **{n['name']}** ({n.get('book','')}): {n.get('summary','')}"
        for n in tracked_nodes
    )
    prompt = f"""你是英语句子结构分析助手。请对下面这句做依存句法分析，输出可用于画依存关系图的结构化数据。

【待分析句子】
{sentence}

【用户特别关注的片段（句子内的子串）】
{text}

【跟踪的语法点】
{nodes_block}

【任务】
1. sentence_zh：整句自然中文翻译。
2. tokens：把句子按词切分（标点也算一个 token），**严格保持原句顺序**。每个词给：
   - text：原文 token（跟原句一致，含大小写）
   - pos：词性，**只能用这些小写英文之一**：noun, verb, adj, adv, pron, prep, det, conj, aux, num, punct, part, intj
   - zh：该词在本句中的简明中文义（标点或虚词可留空字符串）
3. deps：依存关系弧的数组。每条 {{"head": <int>, "child": <int>, "label": "<中文关系名>"}}：
   - head / child 都是 tokens 数组下标（0-based 整数）。head 是支配词，child 是从属词。
   - label 用简短中文：主语 / 宾语 / 间接宾语 / 定语 / 状语 / 介词宾语 / 介词 / 系动词 / 并列 / 连词 / 补语 / 限定 / 同位 / 主句谓语标记 等。
   - 整句的核心（一般是主要动词）作为根，不必为它列入边。
   - 不要画自环，head ≠ child。
4. analyses：句中命中的跟踪语法点（仅限上面列表，不要凭空加）。每条 {{node_id, phrase, explanation, examples}}。没有命中则 []。

【输出 JSON（仅输出 JSON，不要任何额外文字）】
{{
  "sentence_zh": "<整句中文翻译>",
  "tokens": [{{"text": "The", "pos": "det", "zh": "（定冠词）"}}, {{"text": "cat", "pos": "noun", "zh": "猫"}}],
  "deps": [{{"head": 1, "child": 0, "label": "限定"}}],
  "analyses": [{{"node_id": "<id>", "phrase": "<语法实例>", "explanation": "<简明解释>", "examples": ["..."]}}]
}}
"""
    _cstat("grammar.ai_run")
    try:
        zh = _ai_call(prompt, "grammar")   # 2026-07:语法分析拆出独立 action(原并在 explain 里)
    except Exception as ex:
        return jsonify({"ok": False, "error": f"AI call failed: {ex}"}), 500
    # 解析 JSON（AI 可能裹在 ```json 或夹解释文字里）
    import re as _re
    j = None
    # 优先匹配整段 JSON 对象
    m = _re.search(r"\{[\s\S]*\}", zh)
    if m:
        try:
            j = json.loads(m.group(0))
        except Exception:
            # AI 偶尔吐 ```json...``` 包裹，剥一层再试
            cleaned = _re.sub(r"^```(?:json)?|```$", "", m.group(0).strip(), flags=_re.M).strip()
            try:
                j = json.loads(cleaned)
            except Exception:
                pass
    if not j:
        return jsonify({"ok": True, "sentence_zh": "", "tokens": [], "deps": [], "analyses": [], "raw": zh[:1500]})
    # 补节点名
    for a in (j.get("analyses") or []):
        nid = a.get("node_id")
        if nid in node_by_id:
            a["node_name"] = node_by_id[nid]["name"]
    # 清洗 tokens / deps：保证 index 合法、无自环
    tokens = []
    for t in (j.get("tokens") or []):
        if isinstance(t, dict) and t.get("text") is not None:
            tokens.append({
                "text": str(t.get("text", "")),
                "pos":  str(t.get("pos", "")).lower(),
                "zh":   str(t.get("zh", "")),
            })
    n = len(tokens)
    deps = []
    for d in (j.get("deps") or []):
        try:
            h, c = int(d.get("head")), int(d.get("child"))
        except Exception:
            continue
        if 0 <= h < n and 0 <= c < n and h != c:
            deps.append({"head": h, "child": c, "label": str(d.get("label", ""))})
    out = {
        "sentence_zh": j.get("sentence_zh") or "",
        "tokens":      tokens,
        "deps":        deps,
        "analyses":    j.get("analyses") or [],
    }
    try:   # merge:别覆盖 grammar-stream 已存进同键文件的 ai_stream_full
        _old = json.loads(cache_p.read_text("utf-8")) if cache_p.exists() else {}
    except Exception:
        _old = {}
    cache_p.write_text(json.dumps({**_old, **out}, ensure_ascii=False, indent=2), "utf-8")
    return jsonify({"ok": True, **out})


def pdf_api_grammar_stream():
    """AI 流式：先输出整句翻译（[[TRANS]]..[[/TRANS]] 标志先到先显示），
    再输出语法点讲解（[[POINTS]] JSON [[/POINTS]]）。配合 spaCy 出的依存图用。
    依存图本身不在这里——spaCy 已经出了，这里只补「翻译 + 语法点讲解」。"""
    from flask import Response, stream_with_context
    data = request.get_json(silent=True) or {}
    sentence = (data.get("sentence") or "").strip()
    text = (data.get("text") or "").strip()
    rel = (data.get("file") or "").strip()
    if not sentence:
        return jsonify({"ok": False, "error": "no sentence"}), 400
    enabled_books = data.get("enabled_books") or _load_grammar_enabled(rel)
    tracked_nodes = _collect_grammar_tracked_nodes(enabled_books) if enabled_books else []
    # 服务端回放缓存(2026-06-10):同 (sentence,text,tracked_ids) 讲解过一次就存进
    # grammar-cache 同键文件(grammar-forget 已 unlink 同文件,级联免费)。命中 → 按
    # dict-jp-ai 同款 SSE 一次性回放全文(前端对累计文本抠 [[TRANS]]/[[POINTS]],天然兼容)。
    # prompt 变更需 bump ai_v。
    import hashlib
    tracked_ids = [n["id"] for n in tracked_nodes]
    _gs_key = hashlib.sha1((sentence + "||" + text + "||" + ",".join(sorted(tracked_ids))).encode("utf-8")).hexdigest()[:20]
    _GRAMMAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _gs_p = _GRAMMAR_CACHE_DIR / f"{_gs_key}.json"
    try:
        _gs_old = json.loads(_gs_p.read_text("utf-8")) if _gs_p.exists() else {}
    except Exception:
        _gs_old = {}
    if _gs_old.get("ai_stream_full") and _gs_old.get("ai_v") == 1:
        def _gs_replay(md=_gs_old["ai_stream_full"]):
            yield "event: start\ndata: {}\n\n"
            yield f"data: {json.dumps({'text': md}, ensure_ascii=False)}\n\n"
            yield "event: done\ndata: {}\n\n"
        return Response(stream_with_context(_gs_replay()), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    nodes_block = "\n".join(
        f"- {n['name']}：{n.get('summary','')}" for n in tracked_nodes
    ) or "（无跟踪语法点，[[POINTS]] 直接输出 []）"
    prompt = f"""你是英语句子分析助手。严格按下面顺序、用标志输出两部分，标志必须原样出现、不要加代码块围栏。

第一部分——整句中文翻译（自然通顺）：
[[TRANS]]这里写整句翻译[[/TRANS]]

第二部分——语法点讲解，只讲【跟踪语法点】里命中的，JSON 数组：
[[POINTS]][{{"point":"语法点名称","phrase":"句中实例短语","explanation":"针对该句的简明讲解","examples":["1-2个相似例句"]}}][[/POINTS]]
没有命中的语法点就输出 [[POINTS]][][[/POINTS]]。

【待分析句子】
{sentence}

【用户特别关注的片段】
{text}

【跟踪语法点】
{nodes_block}

只输出上面两段（含标志），先翻译后语法点，不要任何额外说明。"""

    def _gs_save(full, _p=_gs_p):
        # 只缓存完整输出(两个闭合标志都在,防报错/截断永久缓存);merge 不覆盖同键已有字段(AI 依存分析)
        if "[[/TRANS]]" not in full or "[[POINTS]]" not in full:
            return
        try:
            old = json.loads(_p.read_text("utf-8")) if _p.exists() else {}
        except Exception:
            old = {}
        old["ai_stream_full"] = full
        old["ai_v"] = 1
        try:
            _p.write_text(json.dumps(old, ensure_ascii=False, indent=2), "utf-8")
        except Exception:
            pass

    return _start_ai_stream(prompt, "grammar", _reader_uid(), (data.get("rid") or "").strip(), on_done=_gs_save)   # 2026-07:语法分析独立 action

# ── 语法分析历史：按 PDF 分文件持久保存（state/grammar-history/<sha>.json）──


def _grammar_hist_path(file_rel: str) -> Path:
    import hashlib
    h = hashlib.sha1((file_rel or "_").encode("utf-8")).hexdigest()[:16]
    return _GRAMMAR_HISTORY_DIR / f"{h}.json"


def _grammar_hist_load(file_rel: str) -> dict:
    p = _grammar_hist_path(file_rel)
    if p.exists():
        try:
            return json.loads(p.read_text("utf-8"))
        except Exception:
            pass
    return {"pdf": file_rel, "items": []}


def _grammar_hist_write(file_rel: str, data: dict):
    _GRAMMAR_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    _grammar_hist_path(file_rel).write_text(json.dumps(data, ensure_ascii=False), "utf-8")


def pdf_api_grammar_history():
    """返回某 PDF 的语法分析历史（按时间倒序，新的在前）。"""
    file = (request.args.get("file") or "").strip()
    hist = _grammar_hist_load(file)
    items = sorted(hist.get("items", []), key=lambda x: x.get("ts", 0), reverse=True)
    return jsonify({"ok": True, "items": items})


def pdf_api_grammar_history_save():
    """保存一条语法分析结果到该 PDF 的历史（同句去重更新，限 200 条）。"""
    import time
    data = request.get_json(silent=True) or {}
    file = (data.get("file") or "").strip()
    item = data.get("item") or {}
    sentence = (item.get("sentence") or "").strip()
    if not file or not sentence:
        return jsonify({"ok": False, "error": "no file/sentence"}), 400
    text = item.get("text") or ""
    hist = _grammar_hist_load(file)
    hist["items"] = [x for x in hist.get("items", [])
                     if not (x.get("sentence") == sentence and (x.get("text") or "") == text)]
    item["ts"] = int(time.time())
    hist["items"].append(item)
    hist["items"] = hist["items"][-200:]
    _grammar_hist_write(file, hist)
    return jsonify({"ok": True, "total": len(hist["items"])})


def pdf_api_grammar_forget():
    """删除某句的语法分析缓存 + 历史（删卡时调），下次分析同句从头生成。"""
    import hashlib
    data = request.get_json(silent=True) or {}
    sentence = (data.get("sentence") or "").strip()
    text = (data.get("text") or "").strip()
    rel = (data.get("file") or "").strip()
    if not sentence:
        return jsonify({"ok": True, "removed": 0})
    enabled_books = data.get("enabled_books") or _load_grammar_enabled(rel)
    tracked_nodes = _collect_grammar_tracked_nodes(enabled_books) if enabled_books else []
    tracked_ids = [n["id"] for n in tracked_nodes]
    # 跟 grammar-analyze 完全一致的 cache_key 算法
    cache_key = hashlib.sha1(
        (sentence + "||" + text + "||" + ",".join(sorted(tracked_ids))).encode("utf-8")
    ).hexdigest()[:20]
    removed = 0
    p = _GRAMMAR_CACHE_DIR / f"{cache_key}.json"
    if p.exists():
        try:
            p.unlink(); removed = 1
        except Exception:
            pass
    # 级联删 spaCy sentence-only 键(保住「删卡→同句从头生成」语义)
    sp_p = _GRAMMAR_CACHE_DIR / (hashlib.sha1(("spacy||" + sentence).encode("utf-8")).hexdigest()[:20] + ".json")
    if sp_p.exists():
        try:
            sp_p.unlink(); removed += 1
        except Exception:
            pass
    # 从历史删
    try:
        hist = _grammar_hist_load(rel)
        before = len(hist.get("items", []))
        hist["items"] = [x for x in hist.get("items", [])
                         if not (x.get("sentence") == sentence and (x.get("text") or "") == text)]
        if len(hist["items"]) != before:
            _grammar_hist_write(rel, hist)
    except Exception:
        pass
    return jsonify({"ok": True, "removed": removed})

def register_grammar(bp, *, claude_dir, spacy_py, spacy_script, ai_call, cstat,
                     spacy_available, start_ai_stream, reader_uid):
    """挂语法域 8 条路由到 bp(url_prefix /pdf),并注入 pdf_reader 依赖(见模块头)。"""
    global _GRAMMAR_NODES_PATH, _GRAMMAR_TRACKED_DIR, _GRAMMAR_CACHE_DIR, _GRAMMAR_HISTORY_DIR
    global CLAUDE_DIR, SPACY_PY, SPACY_SCRIPT, _ai_call
    global _cstat, _spacy_available, _start_ai_stream, _reader_uid
    CLAUDE_DIR = claude_dir
    SPACY_PY = spacy_py
    SPACY_SCRIPT = spacy_script
    _ai_call = ai_call
    _GRAMMAR_NODES_PATH = claude_dir / "_server_deploy" / "grammar-nodes.json"
    _GRAMMAR_TRACKED_DIR = claude_dir / "state" / "grammar-tracked"
    _GRAMMAR_CACHE_DIR = claude_dir / "state" / "grammar-cache"
    _GRAMMAR_HISTORY_DIR = claude_dir / "state" / "grammar-history"
    _cstat = cstat
    _spacy_available = spacy_available
    _start_ai_stream = start_ai_stream
    _reader_uid = reader_uid
    for rule, func, methods in (
        ("/api/grammar-nodes", pdf_api_grammar_nodes, ['GET']),
        ("/api/grammar-books", pdf_api_grammar_books, ['GET']),
        ("/api/grammar-tracked", pdf_api_grammar_tracked, ['GET', 'POST']),
        ("/api/grammar-analyze", pdf_api_grammar_analyze, ['POST']),
        ("/api/grammar-stream", pdf_api_grammar_stream, ['POST']),
        ("/api/grammar-history", pdf_api_grammar_history, ['GET']),
        ("/api/grammar-history-save", pdf_api_grammar_history_save, ['POST']),
        ("/api/grammar-forget", pdf_api_grammar_forget, ['POST']),
    ):
        bp.add_url_rule(rule, view_func=func, methods=methods)
