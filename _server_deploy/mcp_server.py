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
import datetime
import json
import os
import sys
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ToolAnnotations

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


# Windows ReaderPC exposes the live Reader bridge as a local stdio MCP server.
# The public plugin stays a thin facade: it discovers that server's schemas and
# forwards calls without duplicating card, anchor, image, or snapshot logic.
_READER_PC_TOOL_NAMES = frozenset({
    "reader_context_snapshot",
    "reader_visual_image",
    "reader_camera_snap",
    "reader_capability_guide",
    "reader_browser_control",
    "reader_highlight_range",
    "reader_anki_draft",
    "reader_card",
    "reader_command",
    "reader_paper_start",
    "reader_page_cards",
    "reader_page_card_read",
    "reader_page_card_edit",
    "reader_page_card_delete",
    "reader_learning_cards",
    "reader_learning_card_read",
    "reader_learning_card_edit",
    "reader_learning_card_delete",
    "reader_review_current_card",
})


# 子进程要带过去的环境变量。MCP SDK 默认只传 HOME/PATH/SHELL/TERM —— Mac 上的桥是依赖框架的
# .NET 程序（运行时在 ~/.dotnet），不带 DOTNET_ROOT 就起不来；LOCALAPPDATA 决定它读哪份数据根。
_READER_PC_ENV_KEYS = ("DOTNET_ROOT", "LOCALAPPDATA", "USERPROFILE", "APPDATA", "READER_CONTEXT_MCP_STATE")


def _reader_pc_server_parameters() -> StdioServerParameters:
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    command = os.environ.get("READER_CONTEXT_MCP_COMMAND", "").strip()
    if not command:
        # Windows：ReaderPC 原生 EXE；Mac（2026-09-25 起的服务器）：同一份源码编出的 bw-reader-bridge，
        # 部署在 ~/BW/runtime/current/bridge（deploy_mac.py）。服务里通常已由环境变量指定，这里是退路。
        command = str(home / "bw-computer-voice-bridge" / "native-host" / "bw-computer-voice-audio.exe") \
            if os.name == "nt" else str(Path.home() / "BW" / "runtime" / "current" / "bridge" / "bw-reader-bridge")
    state = os.environ.get("READER_CONTEXT_MCP_STATE", "").strip()
    if not state:
        # Mac 上 ~/bw-computer-voice-bridge 链到 ~/BW/data/bridge（见 references/mac-server.md）。
        state = str(home / "bw-computer-voice-bridge" / "runtime" / "reader-context-snapshot.json")
    if not Path(command).is_file():
        raise FileNotFoundError(
            f"Reader MCP executable is unavailable (looked at {command}); set "
            "READER_CONTEXT_MCP_COMMAND on the ReaderPC host"
        )
    env = dict(get_default_environment())
    env.update({key: os.environ[key] for key in _READER_PC_ENV_KEYS if os.environ.get(key)})
    return StdioServerParameters(
        command=command,
        args=["--reader-context-mcp", "--state", state],
        env=env,
    )


def _reader_pc_error(ex: Exception) -> CallToolResult:
    return CallToolResult(
        isError=True,
        content=[{
            "type": "text",
            "text": f"READER_PC_UNAVAILABLE: {type(ex).__name__}: {ex}",
        }],
    )


async def _reader_pc_list_tools() -> dict:
    try:
        params = _reader_pc_server_parameters()
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.list_tools()
    except Exception as ex:
        return {
            "ok": False,
            "error": "READER_PC_UNAVAILABLE",
            "detail": f"{type(ex).__name__}: {ex}",
        }
    tools = []
    for tool in result.tools:
        if tool.name not in _READER_PC_TOOL_NAMES:
            continue
        tools.append({
            "name": tool.name,
            "description": tool.description or "",
            "inputSchema": tool.inputSchema,
            "annotations": (
                tool.annotations.model_dump(exclude_none=True)
                if tool.annotations is not None else None
            ),
        })
    tools.sort(key=lambda item: item["name"])
    return {
        "ok": True,
        "source": "windows-readerpc",
        "count": len(tools),
        "tools": tools,
    }


async def _reader_pc_call(name: str, args: dict | None) -> CallToolResult:
    if name not in _READER_PC_TOOL_NAMES:
        return CallToolResult(
            isError=True,
            content=[{
                "type": "text",
                "text": (
                    f"READER_PC_TOOL_NOT_ALLOWED: {name}. "
                    "Call reader_pc_tools first and use one returned name."
                ),
            }],
        )
    try:
        params = _reader_pc_server_parameters()
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                return await session.call_tool(name, args or {})
    except Exception as ex:
        return _reader_pc_error(ex)

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
                           "② assistant_call_tool(name, args, file, page) 实际操作书本(与内置助手同一副身体)。\n"
                           # 这段 instructions 发给**每一个**连上来的客户端。原先这里写「③ 每轮对话结束都调
                           #   assistant_log_chat」——是强制指令,导致每个客户端每轮白烧一次工具调用回写内置助手
                           #   历史(实测我们自己的 worker 也照做,白费一整轮)。改成 opt-in 能力提示:多数编排根本
                           #   不需要回写,只有想让阅读器内置助手事后接手时才用。
                           "若希望阅读器内置助手事后能接手你这轮编排,可(非必需)在合适时机用 assistant_log_chat "
                           "回写要点——记录会显示在阅读器侧栏、内置助手接手时能看到;不要每轮都调。\n"
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
    """列出书架上所有书(PDF/EPUB)。每本的 **rel 字段**就是其他工具 file 参数要用的路径(原样传入,别改);
    kind=pdf|epub。列表已**按最近打开时间排好序**(books[0]=最近打开的那本;想要精确阅读位置用 reading_positions)。"""
    d = _get("/pdf/api/list-pdfs")
    if not d.get("ok"):
        return d
    # 只投影 agent 真正要用的字段。透传体里 comp_compressing/comp_exists/comp_percent 是压缩流水线的内部
    #   簿记、mtime/lastopen 是前端排序用的时间戳——对 agent 全无意义,纯占上下文(29 本 9.8KB → ~4KB)。
    #   rel 必留(其它工具的 file 参数);name 给可读标题,kind 决定哪些工具适用,size_kb 给大致篇幅。
    #   源端已按 lastopen 降序排好,保持原序即「最近在读在前」。
    books = [{"rel": b.get("rel"), "name": b.get("name"), "kind": b.get("kind"),
              "size_kb": int(round(b.get("size_kb") or 0))}
             for b in (d.get("pdfs") or [])]
    return {"ok": True, "count": len(books), "books": books}


@mcp.tool()
def read_page(file: str, page: int) -> dict:
    """读某书某页的纯文本(1-based 页码)。返回 {ok, page, total, text}。"""
    d = _get("/pdf/api/page-text", file=file, page=page)
    # KJ 页级分析块：未分析页附指示（先答后交 kj_page_submit）与 YOLO 框；已分析页附标注/节点掌握度/公式/图描述
    try:
        kb = _get("/kj/api/page/block", file=file, page=page)
        if isinstance(kb, dict) and kb.get("ok"):
            kb.pop("ok", None)
            d["kj_page"] = kb
        elif isinstance(kb, dict):
            d["kj_page"] = {"error": kb.get("error") or kb.get("code") or "unavailable"}
    except Exception as e:  # 页块失败不影响读页，但要出声
        d["kj_page"] = {"error": str(e)[:160]}
    return d


@mcp.tool()
def kj_page_submit(file: str, page: int, analysis: dict) -> dict:
    """交一页的 KJ 分析（read_page 返回 kj_page.status=unanalyzed 时，先回答用户再调）。
    analysis = {summary, kind[], notation[{symbol,meaning,concept}], concepts[{name,qid?,kind?,aliases?,role,definition?{text,uses[]}}],
    formulas[{idx,latex}], figures[{idx,desc}], exercises[{label,concepts[]}], pitfalls[{text,concept}]}。程序落账并打标记。"""
    body = dict(analysis or {})
    body.update({"file": file, "page": page})
    return _post("/kj/api/page/submit", body)


@mcp.tool()
def kj_page_brief(file: str, page: int) -> dict:
    """看某页的 KJ 分析结果（页标注、出现的节点及掌握度、公式 LaTeX、图描述）。"""
    return _get("/kj/api/page/brief", file=file, page=page)


@mcp.tool()
def kj_web_scope(action: str = "list", pattern: str = "", note: str = "", rule_id: str = "", url: str = "") -> dict:
    """网页分析范围规则表：只有命中的网页才当书页做 KJ 页级分析（默认维基百科词条、arXiv 摘要页）。
    action=list|add|remove|enable|disable|test；pattern 通配 *.site.org/path/* 或 re:正则；test 用 url 看在不在范围。"""
    if action == "list":
        return _get("/kj/api/web-scope")
    return _post("/kj/api/web-scope", {"action": action, "pattern": pattern or None, "note": note, "rule_id": rule_id or None, "url": url or None})


@mcp.tool()
def search_in_book(file: str, query: str, limit: int = 50) -> dict:
    """在**指定书**里全文搜索,返回命中页码+片段。"""
    return _get("/pdf/api/search", file=file, q=query, limit=limit)


@mcp.tool()
def search_all_books(query: str, limit: int = 30) -> dict:
    """跨**全部书**全文搜索(FTS 索引),**按相关度**返回各书命中页+片段。找"哪本书讲过 X"用这个。
    limit=返回的命中页上限(取相关度最高的前 N;默认 30,要更全就调大)。"""
    d = _get("/pdf/api/global-search", q=query, limit=limit)
    if not d.get("ok"):
        return d
    # 透传会撑爆上下文:一次 "HTTP" 命中 254 页 ≈ 80KB。backend 已按 bm25 相关度排序并 truncate 到 limit,
    #   这里只做字段投影——pos/qlen 是前端加粗用的字符偏移、dir 与 file 重复,对 agent 全无意义。
    books = [{"file": b.get("file"), "name": b.get("name"), "hits": b.get("hits"),
              "pages": [{"page": p.get("page"), "snippet": p.get("snippet")}
                        for p in (b.get("pages") or [])]}
             for b in (d.get("books") or [])]
    out = {"ok": True, "q": d.get("q"), "books": books,
           "total_books": d.get("total_books"), "total_hits": d.get("total_hits"),
           "truncated": bool(d.get("truncated"))}
    if out["truncated"]:
        out["note"] = f"结果已截断到最相关的 {limit} 页;要更多请调大 limit,或用 search_in_book 在指定书里精确定位。"
    return out


@mcp.tool()
def reading_positions() -> dict:
    """用户各书的当前阅读位置,**按最后阅读时间从新到旧排好序**(第一条 = 最近在读的那本)。
    判断"用户最近在读什么/读到哪"用这个 —— 直接取 most_recent 或 books[0],不用自己比时间。"""
    try:
        response = _get("/pdf/api/reading-pos")
        if not response.get("ok"):
            return response
        raw = response.get("positions") or {}
        # 148:原先直接把无序 dict 原样吐出去 → 模型得自己逐个比 ts 找最大值,**实测真的挑错过书**
        #   (codex 挑了第二近的那本)。按 Anthropic「writing tools for agents」的 high-signal 原则:
        #   排好序 + 给人类可读时间 + 把结论(most_recent)直接摆出来,别让模型做本该我们做的事。
        rows = []
        for f, v in (raw.items() if isinstance(raw, dict) else []):
            if not isinstance(v, dict):
                continue
            ts = v.get("ts") or 0
            rows.append({"file": f,
                         "name": f.rsplit("/", 1)[-1].rsplit(".", 1)[0],
                         "kind": v.get("kind"),
                         "pos": v.get("pos"),
                         "last_read": (datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
                                       if ts else ""),
                         "ts": ts})
        rows.sort(key=lambda r: r["ts"], reverse=True)   # 新 → 旧
        return {"ok": True,
                "most_recent": rows[0] if rows else None,   # 「最近在读」的答案,直接给
                "books": rows}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}


# ───────────────────────── 用户现况（地点 / 设备 / 在做什么）─────────────────────────
def _bwreader_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "BWReader"


def _bridge_runtime() -> Path:
    state = os.environ.get("READER_CONTEXT_MCP_STATE", "").strip()
    if state:
        return Path(state).parent
    return Path(os.environ.get("USERPROFILE") or Path.home()) / "bw-computer-voice-bridge" / "runtime"


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _minutes_ago(ms, now_ms: int):
    return round((now_ms - ms) / 60000, 1) if isinstance(ms, (int, float)) and ms > 0 else None


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
))
def user_situation() -> dict:
    """用户**此刻**的现况：在哪（地点）、用什么设备、在做什么（读哪本书第几页 / 复习 / 语音），
    以及是否醒着、多久没操作。回答「他现在在干嘛/在哪/方便说话吗」先调这个。

    每项都带 known：known=false 表示**不知道**（附 why），不等于「否」—— 读不到位置
    不代表不在家。ageMinutes 是该信息距今多久，旧信息请按旧处理。
    信号与自动触发规则共用同一套判断（situation_signals），这里只读不改。"""
    import importlib
    now_ms = int(datetime.datetime.now().timestamp() * 1000)
    root, runtime = _bwreader_root(), _bridge_runtime()
    gaps: list[str] = []
    try:
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        signals_module = importlib.import_module("situation_signals")
        signals = signals_module.read_all(root, runtime)["signals"]
    except Exception as ex:  # 信号模块缺失/报错：说清楚，别给一份看似完整的空结果
        signals = {}
        gaps.append(f"情境信号读取失败：{ex}")

    def sig(name):
        value = signals.get(name) or {"known": False, "why": "信号不可用"}
        if not value.get("known"):
            gaps.append(f"{name}：{value.get('why') or '不知道'}")
        return value

    place, alias = sig("place"), sig("place_alias")
    presence = _read_json(root / "presence-signal.json") or {}
    snapshot = _read_json(runtime / "reader-context-snapshot.json") or {}
    active = snapshot.get("activeReading") if isinstance(snapshot.get("activeReading"), dict) else None

    # 桥按设备各存一份 presence-signal-<设备>.json；旧桥只有「最后一台」那一份。
    reports = [r for r in (_read_json(p) for p in sorted(root.glob("presence-signal-*.json"))) if isinstance(r, dict)]
    if not reports and presence.get("device"):
        reports = [dict(presence, _singleSlot=True)]
    latest_ms = max((r.get("atMs") or 0 for r in reports), default=0)
    devices = []
    for report in sorted(reports, key=lambda r: r.get("atMs") or 0, reverse=True):
        entry = {
            "device": report.get("device"),
            "appForeground": report.get("foreground"),
            "audioRoute": report.get("audioRoute"),
            "lastReportMinutesAgo": _minutes_ago(report.get("atMs"), now_ms),
            "mostRecent": report.get("atMs") == latest_ms,
        }
        if report.get("_singleSlot"):
            entry["note"] = "只有最后一台上报的设备（桥未升级到分设备记录）"
        devices.append(entry)
    if not devices:
        gaps.append("设备：App 没报过在场信号")

    reading = None
    if active and active.get("title"):
        observed = active.get("observedAtEpochMs")
        reading = {
            "title": active.get("title"), "kind": active.get("kind"), "page": active.get("page"),
            "fresh": active.get("fresh"), "observedMinutesAgo": _minutes_ago(observed, now_ms),
            "device": None,
            "deviceWhy": "阅读上报里还没有设备字段（多台设备时无法区分是哪台在读）",
        }
    else:
        gaps.append("阅读：快照里没有当前阅读")

    return {
        "ok": True,
        "now": datetime.datetime.fromtimestamp(now_ms / 1000).strftime("%Y-%m-%d %H:%M"),
        "place": {"state": place.get("value"), "alias": alias.get("value"),
                  "known": bool(place.get("known")), "ageMinutes": place.get("ageMinutes"),
                  "why": place.get("why")},
        "devices": devices,
        "activity": {
            "reading": reading,
            "voiceLinked": sig("voice_linked").get("value"),
            "reviewing": sig("reviewing").get("value"),
            "reviewRemaining": signals.get("review_remaining", {}).get("value"),
            "reviewDue": signals.get("review_due", {}).get("value"),
            "reviewNew": signals.get("review_new", {}).get("value"),
            "headphones": sig("headphones").get("value"),
        },
        "body": {
            "awake": sig("awake").get("value"),
            "wokeAtHour": signals.get("woke_at_hour", {}).get("value"),
            "idleMinutes": signals.get("idle_minutes", {}).get("value"),
            "localHour": signals.get("local_hour", {}).get("value"),
        },
        "unknown": gaps,
    }


# ───────────────────────── 语音开场：一次拿全（GPT 普通聊天语音）─────────────────────────
# 2026-09-26 用户提议：ChatGPT 普通聊天的语音也能调 MCP 了，但那里没有 jev 判断、也没有任何上下文注入
# —— 模型只能自己来问。所以给它一个「一次拿全」的入口：现况（在哪 / 什么设备 / 在做什么）
# + 阅读器全量快照（旧版那种带各种状态信息的完整快照，复用 ReaderPC 的 reader_context_snapshot，
# 不在这里另拼一份）。少一次往返，慢速语音里就少一段沉默。
_VOICE_BRIEF_SNAPSHOT_LIMIT = 12000


def _call_result_text(result) -> tuple[str, bool]:
    texts = []
    for item in getattr(result, "content", None) or []:
        kind = item.get("type") if isinstance(item, dict) else getattr(item, "type", "")
        if kind == "text":
            texts.append(item.get("text", "") if isinstance(item, dict) else getattr(item, "text", ""))
    return "\n".join(t for t in texts if t), bool(getattr(result, "isError", False))


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
))
async def voice_brief() -> dict:
    """语音对话的开场工具：**一次**拿到用户此刻的现况与阅读器全量快照。
    适用于没有上下文注入的语音（如 ChatGPT 普通聊天的语音模式）：对话开始、或用户提到
    「这本书 / 这一页 / 这句 / 我在干嘛」时先调它，不要分几次去问。
    返回 situation（在哪、什么设备、在读哪本第几页、是否在复习、是否醒着；known=false = 不知道，不等于否）
    与 reader（阅读器快照原文：当前书页、选区、页上卡片、复习卡等；过长会截断）。
    要操作书本（高亮、制卡、翻页…）再用 reader_pc_tools / reader_pc_call_tool。
    回答用户时口语化、简短 —— 这是语音。"""
    situation = user_situation()
    reader = await _reader_pc_call("reader_context_snapshot", {})
    text, failed = _call_result_text(reader)
    truncated = len(text) > _VOICE_BRIEF_SNAPSHOT_LIMIT
    brief = {
        "ok": True,
        "situation": situation,
        "reader": {
            "ok": not failed and bool(text),
            "snapshot": text[:_VOICE_BRIEF_SNAPSHOT_LIMIT],
            "truncated": truncated,
        },
    }
    if failed or not text:
        brief["reader"]["why"] = text or "阅读器快照为空"
    return brief


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
def list_highlights(file: str, limit: int = 100) -> dict:
    """列出某书的高亮,**按页排好序**(页码 + 高亮文字 + 备注 + 颜色)。limit=返回条数上限。"""
    d = _get("/pdf/api/highlights", file=file)
    if not d.get("ok"):
        return d
    # rects(高亮矩形几何)/page_w/page_h/time/kind 是渲染与内部簿记,agent 用不到 → 只留 页/文字/颜色,
    #   外加非空的 note/sentence/body(用户实际写的内容)。按页排序,省得模型自己整理。
    rows = []
    for h in (d.get("highlights") or []):
        r = {"page": h.get("page"), "text": h.get("text") or "", "color": h.get("color")}
        for k in ("note", "sentence", "body"):
            v = (h.get(k) or "").strip()
            if v:
                r[k] = v
        rows.append(r)
    rows.sort(key=lambda x: (x.get("page") or 0))
    total = len(rows)
    out = {"ok": True, "count": min(total, limit), "highlights": rows[:limit]}
    if total > limit:
        out["truncated"] = True
        out["note"] = f"共 {total} 条,只返回前 {limit} 条(按页序);要更多请调大 limit。"
    return out


@mcp.tool()
def list_notes(file: str, limit: int = 100) -> dict:
    """列出某书的便签,**按位置排好序**(位置 loc + 文字;手写笔画只给 has_ink 标记,不含坐标)。limit=返回条数上限。"""
    d = _get("/pdf/api/notes", file=file)
    if not d.get("ok"):
        return d
    # anchor 坐标 / strokes 笔画点 / w/h/collapsed/iar 都是渲染态,agent 用不到 → 只给位置(pdf=p.页 /
    #   epub=§节)+ 文字 + has_ink;按位置排序。
    rows = []
    for n in (d.get("notes") or []):
        a = n.get("anchor") or {}
        if a.get("kind") == "epub":
            loc, sk = f"§{a.get('section')}", (1, a.get("section") or 0)
        else:
            loc, sk = f"p.{a.get('page')}", (0, a.get("page") or 0)
        rows.append({"_k": sk, "loc": loc, "text": n.get("text") or "",
                     "has_ink": bool(n.get("strokes"))})
    rows.sort(key=lambda x: x["_k"])
    for r in rows:
        r.pop("_k", None)
    total = len(rows)
    out = {"ok": True, "count": min(total, limit), "notes": rows[:limit]}
    if total > limit:
        out["truncated"] = True
        out["note"] = f"共 {total} 条,只返回前 {limit} 条;要更多请调大 limit。"
    return out


@mcp.tool()
def list_favorites(limit: int = 100) -> dict:
    """列出用户的收藏夹,按文件夹分组(每条:kind + 定位信息)。limit=返回条目总数上限。"""
    d = _get("/pdf/api/favorites")
    if not d.get("ok"):
        return d
    # built_sig/content_sig/built_ts/built_ver 是「收藏夹物化成 EPUB」的内部簿记、thumb 是缩略图 URL,
    #   agent 都用不到 → 每条只留 kind + 定位。定位字段随 kind 变(见 favorites 归一化):
    #   pdf=file+page、epub=file+section、userpage=file+id、video=vid+src+title——section/id 少一个就
    #   定位不到具体章节/插入页,必须一并保留。
    folders, shown, truncated = [], 0, False
    for f in (d.get("folders") or []):
        items = []
        for it in (f.get("items") or []):
            if shown >= limit:
                truncated = True
                break
            row = {"kind": it.get("kind")}
            for k in ("title", "file", "page", "section", "id", "vid", "src"):
                if it.get(k) not in (None, ""):
                    row[k] = it.get(k)
            items.append(row)
            shown += 1
        folders.append({"name": f.get("name"), "items": items})
        if truncated:
            break
    out = {"ok": True, "folders": folders}
    if truncated:
        out["truncated"] = True
        out["note"] = f"收藏条目超过 {limit} 已截断;要更多请调大 limit。"
    return out


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


# ───────────────────────── Windows ReaderPC 实时工具代理 ─────────────────────────
@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
))
async def reader_pc_tools() -> dict:
    """列出这台 Windows ReaderPC 当前可供远程插件调用的 Reader 工具及完整参数说明。

    需要读取当前页、看页面/选区图、创建或改绑图片卡、管理页面卡片、操作学习卡
    或复习当前卡片时，先调用本工具发现准确名称与 inputSchema，再用
    reader_pc_call_tool 调用。目录直接来自 Windows 上正在使用的 Reader MCP，插件不复制业务逻辑。
    """
    return await _reader_pc_list_tools()


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=False,
))
async def reader_pc_call_tool(name: str, args: dict | None = None) -> CallToolResult:
    """按 reader_pc_tools 返回的名称和参数，调用这台 Windows 上的实时 Reader 工具。

    返回内容、图片、错误状态和写入回执均由原始 Reader MCP 原样透传；不要根据旧记忆猜参数，
    也不要在结果不确定时自动重复写操作。该入口只允许已登记的 Reader 工具，不能执行任意命令。
    """
    return await _reader_pc_call(name, args)


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
    **可选、非必需**:仅当希望阅读器内置助手事后接手时能看到上下文,才在合适时机回写要点——别每轮都调。"""
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
        # 145(2026-07-14,交叉实验断因后**改回原样**):MCP 的 transport 模式**根本不影响** OpenAI Realtime。
        #   我一度以为 stateful(SSE 会话)跟 OpenAI 的 MCP 客户端不兼容(日志里每次都 "Created new transport"
        #   + 夹一个 400,而 mcp_call.output 恒为 None)→ 改成 stateless+json_response 后"立刻通了"。
        #   **那是误判**:真因是 mcp_call 走**异步**生命周期(response.done 先结束,工具 1.9s 后才完成),
        #   我在 response.done 就断开了 WS,自然永远看不到结果。GPT 指出我同时改了两个变量、不能断因,
        #   于是做了 stateless×json_response 四格交叉实验 —— **四种组合全部正常**(mcp_list_tools ✅ + mcp_call 有结果)。
        #   → 默认回到原生 stateful(claude.ai 连接器一直跑在这上面,没理由为一个不存在的问题动它)。
        #   env 可覆盖:MCP_STATELESS=1 / MCP_JSON=1(留着,万一将来真遇到代理不兼容)。
        _stateless = os.environ.get("MCP_STATELESS", "0") != "0"
        _json = os.environ.get("MCP_JSON", "0") != "0"
        mcp.settings.stateless_http = _stateless
        mcp.settings.json_response = _json
        print(f"[mcp] streamable-http stateless={_stateless} json_response={_json}", flush=True)
        app = build_asgi(mcp.streamable_http_app(), static_token=http_token, public_base=public_base)
        # host 默认只听本机;Pi unit 设 0.0.0.0(VPS nginx 经 tailnet 反代进来,claude.ai 连接器
        # 的服务端请求不走非 443 端口——8443 Funnel 授权页能开但 token/mcp 全连不上,2026-07-13 实测)
        host = os.environ.get("MCP_HTTP_HOST", "127.0.0.1")
        uvicorn.run(app, host=host, port=port, log_level="info")   # 入口=nginx /mcp(tailnet)+ Funnel 8443 + VPS bwicarus.space 443;info=开访问日志(排查连接器全靠它)
    else:
        mcp.run()   # stdio(默认):由 MCP 客户端(claude mcp add)按需拉起,无 HTTP 面
