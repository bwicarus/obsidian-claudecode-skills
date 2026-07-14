"""mcp_server.py — 把整个自学 App 的能力封装成标准 **MCP 服务器**,供外部 agent 控制。

架构(薄门面,零业务逻辑):
  外部 agent(Claude Code / 任何 MCP 客户端)
      → 本服务器(MCP 工具)
      → webapp HTTP API(127.0.0.1:5000,gunicorn)
      → Bearer token 经 app.py 的 _bearer_user 桥认证成正式 session → 走跟浏览器完全相同的代码路径。

认证:env MCP_WEBAPP_TOKEN 或 ~/.config/mcp-webapp-token(app.db api_tokens 表,label='mcp-server')。
传输:默认 stdio(本机 agent);`--http [port]` 走 Streamable HTTP(默认 8765,给远程 MCP 客户端,须自行套 Tailscale/nginx)。

注册到 Claude Code:
  claude mcp add --scope user bwicarus-app -- /home/bwicarus/mcp-venv/bin/python /home/bwicarus/claude/_server_deploy/mcp_server.py

运行环境:/home/bwicarus/mcp-venv(mcp + httpx)。不 import webapp 代码,只走 HTTP → webapp 更新无需动本文件。
"""
import json
import os
import sys
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

BASE = os.environ.get("MCP_WEBAPP_BASE", "http://127.0.0.1:5000")


def _token() -> str:
    t = (os.environ.get("MCP_WEBAPP_TOKEN") or "").strip()
    if t:
        return t
    try:
        return Path("~/.config/mcp-webapp-token").expanduser().read_text().strip()
    except Exception:
        return ""


_client = httpx.Client(base_url=BASE, timeout=60.0,
                       headers={"Authorization": f"Bearer {_token()}"})

try:   # HTTP 模式经 nginx/Funnel 反代,Host 头是 ts.net → 关 SDK 的 DNS-rebinding Host 校验(否则 421;安全由 Bearer 门禁负责)
    from mcp.server.transport_security import TransportSecuritySettings
    _tsec = TransportSecuritySettings(enable_dns_rebinding_protection=False)
except Exception:
    _tsec = None

mcp = FastMCP("bwicarus-study-app",
              transport_security=_tsec,
              instructions="自学系统(Obsidian 笔记 + PDF/EPUB 阅读器 + 词汇 + Anki + 健身)的控制接口。"
                           "书籍文件路径(file 参数)一律用 list_books 返回条目的 rel 字段原样传入。\n"
                           "【编排模式】你可以临时充当这个 App 内置读书助手的最外层编排 agent:"
                           "① assistant_tools() 看内置工具目录(read_page/see_page/highlight/make_anki/notes…30+ 个);"
                           "② assistant_call_tool(name, args, file, page) 实际操作书本(与内置助手同一副身体);"
                           "③ 每轮对话结束用 assistant_log_chat 把你和用户的这轮对话写进助手会话历史——"
                           "阅读器侧栏会显示这些记录,内置助手接手时也有完整上下文。\n"
                           "前端动作类工具(goto_page 翻页等)会实时同步到用户**打开着的**PDF 阅读器页面"
                           "(经 SSE 总线);调用时 ctx 带上 file(书的 rel 路径)保证页码偏移正确。")


def _get(path: str, **params) -> dict:
    try:
        r = _client.get(path, params={k: v for k, v in params.items() if v not in (None, "")})
        return r.json()
    except Exception as ex:
        return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}


def _post(path: str, body: dict) -> dict:
    try:
        r = _client.post(path, json=body)
        return r.json()
    except Exception as ex:
        return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}


# ───────────────────────── 书籍 / 阅读 ─────────────────────────
@mcp.tool()
def list_books() -> dict:
    """列出书架上所有 PDF 书。每本的 **rel 字段**就是其他工具 file 参数要用的路径(原样传入,别改)。"""
    return _get("/pdf/api/list-pdfs")


@mcp.tool()
def read_page(file: str, page: int) -> dict:
    """读某书某页的纯文本(1-based 页码)。返回 {ok, page, total, text}。"""
    return _get("/pdf/api/page-text", file=file, page=page)


@mcp.tool()
def search_in_book(file: str, query: str, limit: int = 50) -> dict:
    """在**指定书**里全文搜索,返回命中页码+片段。"""
    return _get("/pdf/api/search", file=file, q=query, limit=limit)


@mcp.tool()
def search_all_books(query: str) -> dict:
    """跨**全部书**全文搜索(FTS 索引),返回各书命中数+片段。找"哪本书讲过 X"用这个。"""
    return _get("/pdf/api/global-search", q=query)


@mcp.tool()
def reading_positions() -> dict:
    """用户各书的当前阅读位置(页码/章节)。判断"用户最近在读什么/读到哪"用这个。"""
    try:
        p = Path("/home/bwicarus/claude/state/reader-positions.json")
        return {"ok": True, "positions": json.loads(p.read_text("utf-8")) if p.exists() else {}}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}


# ───────────────────────── 词汇 / 语言 ─────────────────────────
@mcp.tool()
def lookup_word(word: str, context: str = "", langs: str = "") -> dict:
    """查词典(英语 ECDICT / 日语 unidic+AI 统一入口):释义/读音/声调/变形/例句。langs 如 "ja" 或 "en,ja"。"""
    return _get("/pdf/api/dict-quick", word=word, context=context, langs=langs)


@mcp.tool()
def translate_text(text: str) -> dict:
    """把一句话翻译成中文(Google→AI 兜底链,含中日同形词处理)。"""
    return _post("/pdf/api/translate-sentence", {"text": text})


@mcp.tool()
def mark_vocab(word: str, mark: str = "known") -> dict:
    """标记词汇掌握态:mark='known'(已掌握,阅读器里不再画下划线/不注假名)或 'unknown'(重新学)。日语词自动走日语库。"""
    jp = any("぀" <= c <= "ヿ" or "一" <= c <= "鿿" for c in word)
    path = "/pdf/api/jp-vocab-mark" if jp else "/pdf/api/vocab-mark"
    return _post(path, {"word": word, "mark": mark})


# ───────────────────────── 高亮 / 便签 / 收藏 ─────────────────────────
@mcp.tool()
def list_highlights(file: str) -> dict:
    """列出某书的全部高亮(颜色/句子/备注/页码)。"""
    return _get("/pdf/api/highlights", file=file)


@mcp.tool()
def list_notes(file: str) -> dict:
    """列出某书的全部便签(文字内容+所在页;手写笔画不含)。"""
    return _get("/pdf/api/notes", file=file)


@mcp.tool()
def list_favorites() -> dict:
    """列出用户的收藏夹(页面/高亮/视频/插入页等收藏条目)。"""
    return _get("/pdf/api/favorites")


# ───────────────────────── 健身 ─────────────────────────
@mcp.tool()
def fitness_plan() -> dict:
    """当前训练计划(全身 3 天 A/B/C:动作/组数/次数区间/RIR/休息)。"""
    return _get("/api/fitness/plan")


@mcp.tool()
def fitness_recommend(exercise_id: str) -> dict:
    """某动作的下次训练推荐(双重渐进:基于上次记录给 重量×次数 建议)。exercise_id 来自 fitness_plan。"""
    return _get(f"/api/fitness/recommend/{exercise_id}")


@mcp.tool()
def fitness_log_set(date: str, day_id: str, exercise_id: str, set_no: int,
                    weight_kg: float | None = None, reps: int | None = None, note: str = "") -> dict:
    """录一组训练(date=YYYY-MM-DD;同 date+exercise+set_no 覆盖更新)。day_id 如 'fb_a'。"""
    return _post("/api/fitness/log", {"date": date, "day_id": day_id, "exercise_id": exercise_id,
                                      "set_no": set_no, "weight_kg": weight_kg, "reps": reps,
                                      "note": note or None})


# ───────────────────────── 编排模式:外部 AI 临时取代内置助手的编排层 ─────────────────────────
@mcp.tool()
def assistant_tools() -> dict:
    """内置读书助手的工具目录(30+ 个:read_page/see_page 看页面图/highlight/make_anki/notes/search_book/recall_notes…)。
    编排模式第一步:先看这个发现能力,再用 assistant_call_tool 实操。"""
    return _get("/api/assistant/tools")


@mcp.tool()
def assistant_call_tool(name: str, args: dict | None = None,
                        file: str = "", page: int = 0, selection: str = "") -> dict:
    """直接调用内置助手的某个工具(你=临时编排 agent,共享同一副工具身体)。
    name/args 见 assistant_tools;file(书的 rel 路径)/page/selection 会作为上下文传给工具
    (多数书内工具需要 file;高亮/制卡等写操作会真实落库并可在阅读器里撤销)。"""
    ctx = {}
    if file:
        ctx["file_rel"] = file
    if page:
        ctx["page"] = page
    if selection:
        ctx["selection"] = selection
    return _post("/api/assistant/tool", {"name": name, "args": args or {}, "ctx": ctx})


@mcp.tool()
def assistant_history(limit: int = 30) -> dict:
    """读内置助手的会话历史(含侧栏对话 + 外部编排对话)。接管编排前先看这个了解上下文。"""
    d = _get("/api/assistant/history")
    if d.get("ok") and isinstance(d.get("messages"), list):
        d["messages"] = d["messages"][-max(1, min(100, limit)):]
    return d


@mcp.tool()
def assistant_log_chat(user_text: str = "", assistant_text: str = "",
                       file: str = "", page: int = 0) -> dict:
    """把你(外部编排 AI)和用户的一轮对话写进助手会话历史(标 via:'mcp')。
    每轮对话结束都调一次——阅读器侧栏会显示,内置助手接手时有完整上下文。"""
    return _post("/api/assistant/log", {"user": user_text, "assistant": assistant_text,
                                        "file": file, "page": page or None})


# ───────────────────────── 写操作便捷封装 ─────────────────────────
@mcp.tool()
def make_anki_card(text: str, file: str = "", page: int = 0) -> dict:
    """把一段内容做成 Anki 卡(后台任务,带原文链接)。等价 assistant_call_tool('make_anki')。"""
    return _post("/api/assistant/tool", {"name": "make_anki", "args": {"text": text},
                                         "ctx": ({"file_rel": file, "page": page} if file else {})})


@mcp.tool()
def add_highlight(file: str, page: int, texts: list[str], color: str = "") -> dict:
    """在某书某页给句子画高亮(texts 必须是该页**原文逐字**,先 read_page 照抄;可撤销)。"""
    args: dict = {"texts": texts, "page": page}
    if color:
        args["color"] = color
    return _post("/api/assistant/tool", {"name": "highlight", "args": args,
                                         "ctx": {"file_rel": file, "page": page}})


if __name__ == "__main__":
    if "--http" in sys.argv:
        try:
            i = sys.argv.index("--http")
            port = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 and sys.argv[i + 1].isdigit() else 8765
        except Exception:
            port = 8765
        try:
            http_token = Path("~/.config/mcp-http-token").expanduser().read_text().strip()
        except Exception:
            http_token = ""
        if not http_token:
            print("HTTP 模式必须有 ~/.config/mcp-http-token(客户端门禁),拒绝无认证启动", file=sys.stderr)
            sys.exit(1)
        import uvicorn
        # 门禁+OAuth 面都在 mcp_oauth(静态 token 与 OAuth token 并行;官方 app 走 OAuth 发现流程)
        from mcp_oauth import build_asgi
        public_base = os.environ.get("MCP_PUBLIC_BASE", "https://bwicarus.taile44d0c.ts.net:8443")
        # 144(2026-07-14 实测):**OpenAI Realtime 的 MCP 客户端跟 stateful(SSE 会话)模式对不上** ——
        #   日志里每次请求都 "Created new transport"、夹一个 400,tools/call 我们这侧 0.9s 就跑完并返回了
        #   (直连公网 URL 验证过:200 + 完整结果 10.8KB),但 OpenAI 那边 mcp_call.output 恒为 None、
        #   模型一直"还在等",最后干脆编书名。改 stateless + json_response(每个 POST 自包含、纯 JSON、
        #   不依赖 SSE 长连接会话)——穿 nginx/代理最稳,也是 hosted MCP 客户端的通用形态。
        #   ⚠ 可回退:systemd 里设 MCP_STATELESS=0 即回老行为(claude.ai 连接器若出问题先试这个)。
        _stateless = os.environ.get("MCP_STATELESS", "1") != "0"
        mcp.settings.stateless_http = _stateless
        mcp.settings.json_response = _stateless
        print(f"[mcp] streamable-http stateless={_stateless} json_response={_stateless}", flush=True)
        app = build_asgi(mcp.streamable_http_app(), static_token=http_token, public_base=public_base)
        # host 默认只听本机;Pi unit 设 0.0.0.0(VPS nginx 经 tailnet 反代进来,claude.ai 连接器
        # 的服务端请求不走非 443 端口——8443 Funnel 授权页能开但 token/mcp 全连不上,2026-07-13 实测)
        host = os.environ.get("MCP_HTTP_HOST", "127.0.0.1")
        uvicorn.run(app, host=host, port=port, log_level="info")   # 入口=nginx /mcp(tailnet)+ Funnel 8443 + VPS bwicarus.space 443;info=开访问日志(排查连接器全靠它)
    else:
        mcp.run()   # stdio(默认):由 MCP 客户端(claude mcp add)按需拉起,无 HTTP 面
