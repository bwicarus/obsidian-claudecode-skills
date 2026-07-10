"""doubao_orchestrator.py — 用豆包(火山方舟 Ark)当**外部编排大脑**,经 MCP 操控整个自学 App。

这是「外部 AI 临时取代内置助手编排层」的第一个真实实现:
  用户 ↔ 豆包(大脑,OpenAI 兼容 function calling)
           │ 本脚本(编排循环)
           ├─ MCP stdio client → mcp_server.py 的 20 个工具(书/查词/翻译/vocab/健身/内置助手工具层)
           └─ 每轮对话末自动 assistant_log_chat → 写进阅读器助手会话历史(侧栏可见)

对 API 型 AI(豆包/DeepSeek/…)不需要 Funnel/公网暴露——方向是反的:我们调它,工具在本地执行。

用法(mcp-venv):
  /home/bwicarus/mcp-venv/bin/python scripts/doubao_orchestrator.py --ask "我最近在读什么书?"
  /home/bwicarus/mcp-venv/bin/python scripts/doubao_orchestrator.py            # 交互式对话
  可选 --model doubao-seed-1-6-251015(默认 flash 档)/ --list-tools(只看工具转换,不调豆包)
key:~/.config/doubao-api-key。模型须先在火山方舟控制台「开通管理」开通。
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ARK = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
DEFAULT_MODEL = "doubao-seed-1-6-flash-250828"
MCP_SERVER = StdioServerParameters(
    command="/home/bwicarus/mcp-venv/bin/python",
    args=["/home/bwicarus/claude/_server_deploy/mcp_server.py"],
)
SYSTEM = (
    "你是用户自学系统(PDF/EPUB 阅读器+词汇+Anki+健身)的外部编排助手,通过工具直接操控这个 App。"
    "规则:1) 书籍 file 参数一律用 list_books 返回条目的 rel 字段原样传入;"
    "2) 需要书内深度操作(看页面渲染图/高亮/便签/制卡)用 assistant_call_tool 调内置助手工具(先 assistant_tools 看目录);"
    "3) 回答用中文,简洁;工具结果是数据,别原样倾倒,提炼后回答。"
)


def _key() -> str:
    try:
        return Path("~/.config/doubao-api-key").expanduser().read_text().strip()
    except Exception:
        sys.exit("缺 ~/.config/doubao-api-key")


def _to_openai_tools(mcp_tools) -> list:
    """MCP 工具 → OpenAI function schema(inputSchema 本来就是 JSON Schema,直接搬)。"""
    out = []
    for t in mcp_tools:
        out.append({"type": "function", "function": {
            "name": t.name, "description": (t.description or "")[:1000],
            "parameters": t.inputSchema or {"type": "object", "properties": {}},
        }})
    return out


def _tool_result_text(res) -> str:
    try:
        txt = res.content[0].text if res.content else "{}"
    except Exception:
        txt = "{}"
    return txt[:4000]   # 截断防上下文爆(read_page 全文 8000 → 喂 4000 足够)


async def run(model: str, ask: str | None, list_tools_only: bool):
    async with stdio_client(MCP_SERVER) as (r, w):
        async with ClientSession(r, w) as mcp:
            await mcp.initialize()
            tools = (await mcp.list_tools()).tools
            oa_tools = _to_openai_tools(tools)
            if list_tools_only:
                for t in oa_tools:
                    f = t["function"]
                    print(f"{f['name']}: {f['description'][:70]}")
                print(f"\n共 {len(oa_tools)} 个工具已转换为 OpenAI function schema")
                return

            key = _key()
            messages = [{"role": "system", "content": SYSTEM}]

            async def one_round(user_text: str) -> str:
                messages.append({"role": "user", "content": user_text})
                async with httpx.AsyncClient(timeout=90) as hc:
                    for _ in range(12):   # 工具循环上限,防死循环
                        resp = await hc.post(ARK, headers={"Authorization": f"Bearer {key}"},
                                             json={"model": model, "messages": messages, "tools": oa_tools})
                        d = resp.json()
                        if d.get("error"):
                            return f"[豆包 API 错误] {d['error'].get('message', '')[:200]}"
                        msg = d["choices"][0]["message"]
                        tcs = msg.get("tool_calls")
                        if not tcs:
                            answer = (msg.get("content") or "").strip()
                            messages.append({"role": "assistant", "content": answer})
                            return answer
                        messages.append(msg)   # assistant(tool_calls) 消息原样入历史
                        for tc in tcs:
                            fn = tc["function"]["name"]
                            try:
                                targs = json.loads(tc["function"].get("arguments") or "{}")
                            except Exception:
                                targs = {}
                            print(f"  🔧 {fn}({json.dumps(targs, ensure_ascii=False)[:100]})", file=sys.stderr)
                            try:
                                res = await mcp.call_tool(fn, targs)
                                rtxt = _tool_result_text(res)
                            except Exception as ex:
                                rtxt = json.dumps({"error": str(ex)[:200]}, ensure_ascii=False)
                            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": rtxt})
                    return "[编排循环达到上限(12 轮工具调用),已停止]"

            async def log_round(u: str, a: str):
                try:   # 对话写进阅读器助手历史(via:'mcp',侧栏可见)——就用 MCP 自己的工具,同一条链路
                    await mcp.call_tool("assistant_log_chat", {"user_text": u, "assistant_text": a[:4000]})
                except Exception:
                    pass

            if ask:
                a = await one_round(ask)
                print(a)
                await log_round(ask, a)
                return
            print(f"豆包编排模式(model={model};输入 exit 退出)")
            while True:
                try:
                    u = input("\n你> ").strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not u or u.lower() in ("exit", "quit"):
                    break
                a = await one_round(u)
                print(f"\n豆包> {a}")
                await log_round(u, a)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ask", help="单发提问(不进交互循环)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--list-tools", action="store_true", help="只打印工具 schema 转换结果")
    a = ap.parse_args()
    asyncio.run(run(a.model, a.ask, a.list_tools))
