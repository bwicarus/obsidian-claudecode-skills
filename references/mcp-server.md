# MCP 服务器:App 能力标准化暴露给外部 agent

> 源码 `_server_deploy/mcp_server.py`,运行环境 `/home/bwicarus/mcp-venv`(mcp SDK + httpx)。
> 让任何 MCP 客户端(Claude Code、claude.ai、其他 agent)像操作一个 App 一样控制整个自学系统。

> Windows ReaderPC 另有一组实时 Reader MCP 工具，由电脑语音原生 EXE 的
> `--reader-context-mcp --state <path>` 提供。远程插件不复制这组工具的业务逻辑：
> `reader_pc_tools` 读取它的实时目录与 schema，`reader_pc_call_tool` 只对白名单内工具做
> 原样转调。因此其它电脑使用同一个远程插件时，页面快照、图片和写入仍以这台 Windows
> 为数据源；图片 content block、错误和 mutation 回执不会在门面层重解释。

## 架构(薄门面,零业务逻辑)

```
外部 agent(MCP 客户端)
    → mcp_server.py(FastMCP,22 个工具:下表 v1 的 14 个 + 编排模式 4 个 + 写操作封装 2 个 + Windows ReaderPC 代理 2 个)
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
| 书 | `reading_positions()`(已按最后阅读时间排序 + 直接给 `most_recent`) | GET /pdf/api/reading-pos。⚠ 该路由 owner=local:权威在 App 本地,Pi 这份是同步中继副本,可能落后于设备 |
| 语言 | `lookup_word(word,context,langs)` | GET /pdf/api/dict-quick(英 ECDICT/日 unidic 统一) |
| 语言 | `translate_text(text)` | POST /pdf/api/translate-sentence |
| 语言 | `mark_vocab(word,mark)` | POST vocab-mark / jp-vocab-mark(按字符自动路由) |
| 标注 | `list_highlights(file)` / `list_notes(file)` / `list_favorites()` | 对应 GET |
| 健身 | `fitness_plan()` / `fitness_recommend(ex)` / `fitness_log_set(...)` | /api/fitness/* |

## 编排模式(外部 AI 临时取代内置助手的编排层)

外部 AI 可以**成为"大脑"**,共享内置读书助手的"身体"(`assistant.py::TOOLS` 现 53 个书内工具 + 会话历史):

| MCP 工具 | 后端桥(assistant.py 新增) | 作用 |
|---|---|---|
| `assistant_tools()` | GET /api/assistant/tools | 内置工具目录(read_page/see_page/highlight/make_anki/notes…) |
| `assistant_call_tool(name,args,file,page,selection)` | POST /api/assistant/tool | 直接 dispatch `TOOLS[name](args, ctx)`,ctx 补 `_uid`;与侧栏助手同一副工具 |
| `assistant_history(limit)` | GET /api/assistant/history | 读会话历史(接管前了解上下文) |
| `assistant_log_chat(user,assistant,file,page)` | POST /api/assistant/log | 把外部对话写进历史(`via:'mcp'` + file_rel/page),侧栏可见、内置助手接手有上下文;写后 publish `assistant-history` SSE 事件 |

写操作便捷封装:`make_anki_card(text,file,page)`、`add_highlight(file,page,texts,color)`(texts 须页面原文逐字,先 read_page 照抄)。

## Windows ReaderPC 实时工具代理

| MCP 工具 | 作用 |
|---|---|
| `reader_pc_tools()` | 从这台 Windows 的本地 Reader MCP 读取允许远程使用的工具名称、说明、参数 schema 和 annotations |
| `reader_pc_call_tool(name,args)` | 白名单转调该工具，并原样返回文字、图片、错误或写入回执；不允许任意命令执行 |

调用方先发现再执行，不把 schema 手抄进插件。白名单覆盖当前 Codex Work 使用的
`reader_context_snapshot`、`reader_visual_image`、`reader_card`、页面卡片、学习卡片、
高亮、交互纸、浏览控制、摄像头等 Reader 工具。若 Windows ReaderPC 离线，返回
`READER_PC_UNAVAILABLE`；未知工具返回 `READER_PC_TOOL_NOT_ALLOWED`，均不得猜参数或盲目重试写操作。

**MCP 遥控前端(2026-07-13)**:外部 agent 调前端动作类工具(goto_page 等)时没有浏览器在等 `client_action`——之前"让它翻页但页面不动"。现在 `/api/assistant/tool` 桥执行后把 `client_action` 经**阅读器 SSE 总线**(`reader_events.publish("client-action", file, uid, extra={"action":{fn,args}})`)广播;**执行器在统一中间层**(rc-assistant.js 的 `RC.execRemote(action)`,PDF/EPUB 都加载):① `adapter.execAction(fn,args)` 精确翻译钩子(可选实现)→ ② `window[fn]` 原生全局(PDF 的 jumpWithBack,保留跳转带返回语义)→ ③ 跳转语义映射 `_host.asst.goTo`(EPUB=章跳,两 adapter 都实现了 goTo)。挂载=两阅读器各自的 reader-events SSE 监听各一行(仅 visible 页面;file 空=广播全部,非空=只匹配的书)。
⚠ `_convo_append` 的 meta 是**白名单字段**(page/book/file_rel/…/via)——加新 meta 字段要进白名单,字段名用 `file_rel` 不是 `file`。

## 注册 / 使用

```bash
# Claude Code(已注册,user scope;新会话自动可用)
claude mcp add --scope user bwicarus-app -- /home/bwicarus/mcp-venv/bin/python /home/bwicarus/claude/_server_deploy/mcp_server.py
```
stdio 模式由客户端按需拉起进程,不需要常驻服务。

**远程模式(2026-09-05 迁到 Windows)**：ReaderPC 托管 `mcp_server.py --http 8766`，只监听
`127.0.0.1:8766`；Windows Tailscale Serve 将 `/mcp`、`/oauth`、`/authorize`、`/token`、
`/.well-known` 暴露给 tailnet。公网 `https://bwicarus.space` 的 VPS nginx 只把这些 MCP/OAuth
路径经 tailnet 反代到 Windows，其它公网路由不因此开放。树莓派的旧 8766 不再是生产上游。

**HTTP 模式强制 Bearer 门禁**:所有请求须带 `Authorization: Bearer <token>`,认**两类** token(无 token → 401;静态 token 文件缺失服务拒绝启动):
- 静态 token `~/.config/mcp-http-token`(Claude Code / 脚本客户端,`--header` 直填);
- **OAuth access token**(官方 app 走 OAuth 流程签发,见下节)。

**两把基础 token 职责别混**:`mcp-http-token`=客户端→MCP 服务器的门禁;`mcp-webapp-token`=MCP 服务器→webapp 的用户身份。SDK 的 DNS-rebinding Host 校验已关(经反代 Host=ts.net 会 421;安全由门禁负责)。

**接入方式**:
- tailnet 内：`https://bwicarus-2.taile44d0c.ts.net/mcp` + Authorization header。
- 公网官方插件：`https://bwicarus.space/mcp`，标准 443；VPS 只做 TLS/反代，Reader 数据和 MCP
  执行均留在 Windows。不要把 Windows 的 443 从 Serve 改成 Funnel：同端口改为 Funnel 会使该
  端口上的其它 Reader 路径也公开。

## OAuth 2.1 层(mcp_oauth.py,2026-07-13)——接 claude.ai / ChatGPT 官方连接器

claude.ai 自定义连接器**只支持 OAuth**(UI 无静态 Bearer/自定义 header 字段;无鉴权服务器的 connect flow 也有已知 issue),所以公网面提供标准 OAuth:

- **端点**:RFC 9728 PRM(`/.well-known/oauth-protected-resource[/mcp]`)+ RFC 8414 AS metadata(`/.well-known/oauth-authorization-server[/mcp]` + `openid-configuration` 别名)+ RFC 7591 DCR(`POST /oauth/register`,public client)+ `GET/POST /oauth/authorize` + `POST /oauth/token`(PKCE S256 强制;authorization_code + refresh_token 轮换)。
- **发现流程**:客户端无 token 打 /mcp → 401 带 `WWW-Authenticate: Bearer resource_metadata=...` → 客户端顺藤摸瓜 DCR→authorize→token(claude.ai 全自动,用户只见授权页)。
- **信任模型**:授权页要求**配对密码**(`~/.config/mcp-oauth-pass`,600)——URL 公开无妨,没密码拿不到 token。consent-phishing 缓解=页面显示回调域名+警示「只在自己主动添加连接器时输入」。密码输错限速(10min 5 次/IP)。
- **token**:access 7 天 / refresh 180 天(轮换,旧 refresh 即废),存 `~/.config/mcp-oauth-store.json`(600,原子写);前缀 `mat_`/`mrt_`/`mac_`(access/refresh/code)、`mcl_`(client)。静态 token 并行有效。
- **public base**：Windows `.env.local` 固定 `MCP_PUBLIC_BASE=https://bwicarus.space`；401 challenge、
  PRM、AS metadata 和 issuer 必须全部返回这一公网域名。ReaderPC 主服务启动时读取该环境文件，
  修改后须重启主服务，不能只重启 MCP 子进程。
- ⚠ **lifespan 桥接坑**:外层 Starlette 组装(Route+Mount)会消费 lifespan 不传子 app → FastMCP 的 session manager 不启动,/mcp 全 500 且 worker 连接复位。mcp_oauth.build_asgi 里手动 `async with mcp_app.router.lifespan_context(mcp_app)` 桥接。
- **官方插件添加/刷新**：URL 填 `https://bwicarus.space/mcp`，Advanced 的 Client ID/Secret 留空，
  走 DCR 后在授权页输入配对密码。MCP 工具或 metadata 更新后刷新插件，使客户端重新读取 descriptors。

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
