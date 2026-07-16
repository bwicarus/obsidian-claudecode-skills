# 创造物库 Creation Store(summary-plus-handle / just-in-time context)

> 用户设计(2026-07-17):**所有非操作型工具调用的最终结果 = 一个创造物**,以时间做 id 标记,
> 统一以自动生成的告知回复作为工具回复;上下文只注入告知+句柄,AI 按需取回全文。
> 一并解决:拿错纸(按物定位)、天气/网页搜索跨轮引用、上下文膨胀、新工具回报接入(登记即接入,零专线)。

## 业界对应(经三路研究核实,2026-07-17)

| 厂商 | 设计 | 来源 |
|---|---|---|
| LangChain | `content_and_artifact`:工具返回 (content 给模型, artifact 应用层持有 "not meant to be sent to the model") | reference.langchain.com ToolMessage.artifact |
| Anthropic | **just-in-time context**(上下文只放轻量标识符,工具按引用加载)+ tool-result-clearing(清除留占位告知)+ memory tool | anthropic.com/engineering/effective-context-engineering |
| OpenAI cookbook | portfolio-collaboration:inline 只回摘要+文件名(=id)+schema,`read_file`(预览)/`list_output_files` 按需取;memory-compaction:"cited facts 住 artifact,上下文只是工作集" | 本地 refs/openai-cookbook |
| MemGPT/Letta | recall memory:结果入库带 id/时间,上下文放摘要,agent 主动调检索工具 | arXiv 2310.08560 |
| Manus | **restorable compression**:内容可丢,指针(URL/路径/id)绝不能丢 | manus.im blog |

三条铁律(业界共识):①句柄无损保留 ②省略要**显式告知**模型(否则拿残片当全量会说谎)③取回=agent **主动**工具调用,不是隐式塞。

## 协议

- **存储**:`state/assistant-creations/<uid>.json`(环形 40 条)。条目:
  `{id: c_<hex ts>_<rand>, kind, brief(告知一行,含实际查询词), query, content(≤8000)|ref(引用型), anchor:{file,page}, ts}`
- **登记点**(全自动):
  - 编排三循环(claude/gemini/codex)工具 done → `_creation_register`(白名单 `_CREATION_KINDS`:web_search/search_video/search_image/translate/summarize_section/search_all_books;error 不登;**read_page 不登**——可随时重读,登了只添噪);
  - 纸:`task_runtime.start()` → kind:'paper', ref={upage,file,page}(本体在 userpages sidecar,不复制;"未检查/已检查"清单时实时判);
  - 检查报告:`_save_check_report` → kind:'check_report', ref={name}(本体在报告库);
  - CLI 任务:`voice._task_agent` done → kind:'cli_task', brief=流程摘要, content=answer。
- **上下文注入**(唯一入口,替代 recent_check 等专线):`_sys_prompt` 直读 `_creations_recent_line(uid)`——6 条,纸/报告优先保留、其余同 kind 去重,`#id brief · 相对时间`。前端不再拼(pdf-adapter 的 recent_check 已删)。
- **取回**:工具 `recall_creation(id?|kind?|query?)`;query 命中多条 → **paper 优先**(纸=本体,报告是它的侧面);paper 条目返回 题目+标准答案(`_upage_read_text` 实时)+ 最近检查报告(经纸的 check_name 反查)。

## 语音侧(P2 待统一)

语音实时链路的告知仍走前端 `_rtcFlushCtx` 的 rcHint(读 `window.__lastCheckResult`,文案已指向 recall_creation);
`recall_creation` 经统一 TOOLS 注册表自动进语音工具目录。P2:relay 侧状态事件改读创造物库(需动 voice-rt,注意通话中不可 restart)。

## 踩坑

- 2026-07-17 实现时把 `_save_check_report` 的落盘三行误吞进 `except: pass` 死码(报告不写盘),单测 `save→load` 抓回——**改函数尾部时必须最小复现 save/load**。
