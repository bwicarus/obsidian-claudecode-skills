# 工具能力矩阵

按职责选择最短通道，不把每一层都串起来。

| 能力 | 首选实现 | 说明 |
|---|---|---|
| 当前页、选区、首次全文/后续窗口 | 本机 `reader_context_snapshot` | 只认 ready 的新鲜快照 |
| App/扩展合成图 | 本机 `reader_visual_image` | 图像按需取，不进文字快照 |
| 当前页面滚动与定位 | 本机 `reader_browser_control` | 只控制快照精确指向的来源 |
| 卡片、导航、工具状态 | 本机 `reader_command` | 严格 `BWREADER/1` 合同与回执 |
| 高亮 | 书里 `reader_highlight_range`／网页 `reader_web_highlight` | 带来源指纹，页面变了会被拒绝。`BWREADER/1 highlight` 已废弃；`reader_highlight_text` 不在工具清单里（老客户端兼容名） |
| 把卡片钉在正文某段（页面锚定/固定） | 本机 `reader_card` 的可选 `bind` | **书里和普通网页都支持**。序号取自 `reader_page_text` 的 `segments`；见 `cards.md` |
| 开放网络研究 | Codex 原生搜索/浏览工具 | 不经过 Reader 或旧 CLI |
| 查书、纸张、报告、已保存任务 | 已配置的服务 MCP | 先发现实时 schema；当前未暴露就明确失败 |
| 工作流与要求 | Codex Skill + 本能力文档 | 只加载当前任务所需的一份 |
| 独立多路证据 | Codex 原生子代理 | 仅并行有收益时使用，默认只读 |
| 插件 | 安装和分发 Skill/MCP | 不是每次请求的运行时跳板 |
| 旧 CLI worker | 兼容路径 | 实现不删除，但 Windows 原生路由不再启动它 |

MCP resource 是否被客户端自动注入不可假设；需要说明时调用 `reader_capability_guide` 读取一个
allowlist topic。普通单步请求不要为此增加一次工具往返。

---

<!-- 2026-09-18：以下内容自 ~/.codex/AGENTS.md 搬来。原来它每条新线程都全文注入（7551 字），而这些规则只在真用到时才需要，正是本指南「按需取」的形态。 -->

## 把一轮做过的事存成 skill —— 只由用户决定（2026-09-13）

工具面只提供原语，**你不判断、也不提议"值不值得存"**。用户按了侧栏的「保存为工具」
（通知会带 turn id），或口头说「把刚才那个存成工具/做成 skill」，就加载 `$organize-into-skill`
照它做：取真实轨迹 → 写 flow.json → `bw_skill_build.py` 校验+生成 run.js+用轨迹干跑 → 存。
默认静默完成，只有拿不准才问一句。运行器由套件生成、参数按工具 schema 校验、干跑过才算编好——
你只填 flow.json，不写运行器。以后用户说触发词，就把该 skill 的 run.js 原样放进 exec 跑，
每步进度侧栏自己会显示。
