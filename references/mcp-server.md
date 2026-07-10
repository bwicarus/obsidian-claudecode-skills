# MCP 服务器:App 能力标准化暴露给外部 agent

> 源码 `_server_deploy/mcp_server.py`,运行环境 `/home/bwicarus/mcp-venv`(mcp SDK + httpx)。
> 让任何 MCP 客户端(Claude Code、claude.ai、其他 agent)像操作一个 App 一样控制整个自学系统。

## 架构(薄门面,零业务逻辑)

```
外部 agent(MCP 客户端)
    → mcp_server.py(FastMCP,14 个工具)
    → webapp HTTP API(127.0.0.1:5000,gunicorn)
    → Bearer token 经 app.py _bearer_user 桥认证成正式 session
    → 走跟浏览器完全相同的代码路径(权限/数据/副作用一致)
```

设计要点:
- **不 import webapp 代码,只走 HTTP** → webapp 随便改/重启,MCP 服务器零维护;工具实现 = 一行 `_get/_post`。
- **认证复用现有 API token 体系**(app.db `api_tokens` 表,label=`mcp-server`;token 文件 `~/.config/mcp-webapp-token`,600 权限)。app.py 的 Bearer 桥(before_request)会把有效 token 认证成该用户的 session → **所有** PROTECTED_PREFIXES 下的 API 全部可用,无需逐路由改造。
- `assistant.py` 的内部工具注册表(30+ 工具)是**给书内侧栏 agent 的**;MCP 是**给外部 agent 的**并行门面,两者共享同一批 HTTP API。

## 工具清单(v1,14 个)

| 类 | 工具 | 后端 |
|---|---|---|
| 书 | `list_books`(rel 字段=file 参数) | GET /pdf/api/list-pdfs |
| 书 | `read_page(file,page)` | GET /pdf/api/page-text(为 MCP 新加的轻端点) |
| 书 | `search_in_book(file,query)` | GET /pdf/api/search |
| 书 | `search_all_books(query)` | GET /pdf/api/global-search(FTS) |
| 书 | `reading_positions()` | 直读 state/reader-positions.json |
| 语言 | `lookup_word(word,context,langs)` | GET /pdf/api/dict-quick(英 ECDICT/日 unidic 统一) |
| 语言 | `translate_text(text)` | POST /pdf/api/translate-sentence |
| 语言 | `mark_vocab(word,mark)` | POST vocab-mark / jp-vocab-mark(按字符自动路由) |
| 标注 | `list_highlights(file)` / `list_notes(file)` / `list_favorites()` | 对应 GET |
| 健身 | `fitness_plan()` / `fitness_recommend(ex)` / `fitness_log_set(...)` | /api/fitness/* |

## 编排模式(外部 AI 临时取代内置助手的编排层)

外部 AI 可以**成为"大脑"**,共享内置读书助手的"身体"(29 个书内工具 + 会话历史):

| MCP 工具 | 后端桥(assistant.py 新增) | 作用 |
|---|---|---|
| `assistant_tools()` | GET /api/assistant/tools | 内置工具目录(read_page/see_page/highlight/make_anki/notes…) |
| `assistant_call_tool(name,args,file,page,selection)` | POST /api/assistant/tool | 直接 dispatch `TOOLS[name](args, ctx)`,ctx 补 `_uid`;与侧栏助手同一副工具 |
| `assistant_history(limit)` | GET /api/assistant/history | 读会话历史(接管前了解上下文) |
| `assistant_log_chat(user,assistant,file,page)` | POST /api/assistant/log | 把外部对话写进历史(`via:'mcp'` + file_rel/page),侧栏可见、内置助手接手有上下文;写后 publish `assistant-history` SSE 事件 |

写操作便捷封装:`make_anki_card(text,file,page)`、`add_highlight(file,page,texts,color)`(texts 须页面原文逐字,先 read_page 照抄)。
⚠ `_convo_append` 的 meta 是**白名单字段**(page/book/file_rel/…/via)——加新 meta 字段要进白名单,字段名用 `file_rel` 不是 `file`。

## 注册 / 使用

```bash
# Claude Code(已注册,user scope;新会话自动可用)
claude mcp add --scope user bwicarus-app -- /home/bwicarus/mcp-venv/bin/python /home/bwicarus/claude/_server_deploy/mcp_server.py
```
stdio 模式由客户端按需拉起进程,不需要常驻服务。

**远程模式(已启用)**:`mcp-server.service`(systemd,`--http 8766` Streamable HTTP,只听 127.0.0.1)+ Pi nginx 两个 server 块(80/443)各加了 `location /mcp`(proxy_buffering off + 3600s 超时;⚠ Pi nginx 是手工 patch,备份在 `sites-available/bwicarus.bak-mcp`)。unit 副本 `references/systemd/mcp-server.service`。

**HTTP 模式强制 Bearer 门禁**:所有请求须带 `Authorization: Bearer <~/.config/mcp-http-token>`(无 token → 401;token 文件缺失服务拒绝启动)。**两把 token 职责别混**:`mcp-http-token`=客户端→MCP 服务器的门禁;`mcp-webapp-token`=MCP 服务器→webapp 的用户身份。SDK 的 DNS-rebinding Host 校验已关(经反代 Host=ts.net 会 421;安全由门禁负责)。

**接入方式**:
- tailnet 内:`https://bwicarus.taile44d0c.ts.net/mcp` + Authorization header。
- **公网(tailnet 外的 AI)= Tailscale Funnel**(待用户执行,见下)——Tailscale 官方入口反代,自动 TLS,无需开路由器端口:
  ```bash
  sudo tailscale funnel --bg --https=8443 --set-path=/mcp http://127.0.0.1:8766/mcp
  ```
  首次可能要在 admin console 开 Funnel 权限(命令会给链接)。之后公网地址 `https://bwicarus.taile44d0c.ts.net:8443/mcp`(仍需 Bearer 门禁)。⚠ 必须用 8443,**绝不能** `--https=443`(tailscaled 会接管 tailnet 侧 443,遮蔽 nginx 全站)。
  claude.ai 远程连接器若只支持 OAuth/无鉴权(不支持自定义 header),接它需另加 OAuth 层;Claude Code(`claude mcp add --transport http URL --header ...`)/SDK 客户端都支持 header。

## 踩坑(HTTP 模式)

- **⚠ 8765 是 AnkiConnect 的默认端口**——最初选它撞车,curl 探活 200 全是 AnkiConnect 的假阳性(响应体 "AnkiConnect v.6"),mcp-server 实际 bind 失败循环重启。**探活别只看状态码,要看响应体**。现用 8766。

## 加新工具(三行套路)

```python
@mcp.tool()
def my_tool(arg: str) -> dict:
    """给 agent 看的中文描述(何时用/参数含义)。"""
    return _get("/pdf/api/xxx", param=arg)   # 或 _post(path, body)
```
写操作类(建 Anki 卡/加高亮/建便签)按需追加——端点都是现成的,照上表模式接即可。

## 冒烟测试

`mcp-venv/bin/python` 跑真 stdio 客户端:initialize → list_tools → call。样例脚本见 scratchpad `mcp_smoke.py` 模式(ClientSession + stdio_client)。首次验收 6/6:29 本书、read_page 返回料理师part2 p3 文本、衛生→えいせい、健身计划 3 天、全局搜索命中。

## 踩坑

- **book 条目的路径字段是 `rel`**(不是 path/file)——工具描述里已注明,外部 agent 别取错。
- token 直插 app.db 生成(与 /api/tokens 端点同逻辑,secrets.token_urlsafe(32));泄露 = 该用户全量 API 权限,Pi 是 Tailscale-only 风险可控,但别把 token 写进 git。
- Debian 系统 pip 是 externally-managed → 必须 venv(/home/bwicarus/mcp-venv)。
