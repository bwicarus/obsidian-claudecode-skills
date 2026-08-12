# 工具能力矩阵

按职责选择最短通道，不把每一层都串起来。

| 能力 | 首选实现 | 说明 |
|---|---|---|
| 当前页、选区、首次全文/后续窗口 | 本机 `reader_context_snapshot` | 只认 ready 的新鲜快照 |
| App/扩展合成图 | 本机 `reader_visual_image` | 图像按需取，不进文字快照 |
| 当前页面滚动与定位 | 本机 `reader_browser_control` | 只控制快照精确指向的来源 |
| 卡片、导航、高亮、工具状态 | 本机 `reader_command` | 严格 `BWREADER/1` 合同与回执 |
| 开放网络研究 | Codex 原生搜索/浏览工具 | 不经过 Reader 或旧 CLI |
| 查书、纸张、报告、已保存任务 | 已配置的服务 MCP | 先发现实时 schema；当前未暴露就明确失败 |
| 工作流与要求 | Codex Skill + 本能力文档 | 只加载当前任务所需的一份 |
| 独立多路证据 | Codex 原生子代理 | 仅并行有收益时使用，默认只读 |
| 插件 | 安装和分发 Skill/MCP | 不是每次请求的运行时跳板 |
| 旧 CLI worker | 兼容路径 | 实现不删除，但 Windows 原生路由不再启动它 |

MCP resource 是否被客户端自动注入不可假设；需要说明时调用 `reader_capability_guide` 读取一个
allowlist topic。普通单步请求不要为此增加一次工具往返。
