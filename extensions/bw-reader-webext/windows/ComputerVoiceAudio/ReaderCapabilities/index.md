# Reader 能力索引

只读取当前任务需要的文件，不要一次加载全部说明。

- 读取当前页、当前显示区域、选区、全文或合成图：`get.md`
- 把普通对话同步进 App/扩展现有对话记录：`conversation.md`
- 展示天气、事实、新闻、图片或视频卡：`cards.md`
- **把卡片钉在正文某一段上（页面锚定 / 固定 / 绑定元素，不随轮次消失）**：`cards.md`
  的「钉住内容（`bind`）」一节；字符序号从 `get.md` 的 `reader_page_text` → `segments` 取
- 用实体摄像头看一眼现实世界（当场拍，不在快照里）：`camera.md`
- 翻动视口、定位文字/标题/选区、跳页/章节：`navigation.md`
- 把当前选区保存为高亮：`highlight.md`
- 展示工具进行中、完成、失败或中止状态：`tool-status.md`
- **在 iOS 小组件上留一块分区展示板**（反复看状态的任务：每日新闻/发布盯梢/长任务进展）：
  `boards.md`。⚠ 只有用户在任务里明确说了要用才用，不要自行判断是否开启
- 统一命令外壳、回执和失败规则：`command-format.md`
- 判断直接调用、原生子代理和兼容回退：`task-routing.md`
- 多步研究、跨书或联网核实：`research-task.md`
- 生成可手写作答的交互练习纸：`interactive-paper.md`
- 读取练习纸检查报告或查书核实：`check-report.md`
- 重新运行已保存的生成型任务：`saved-task.md`
- 确认本机 MCP、服务 MCP、Skill、插件和 CLI 的职责：`capability-matrix.md`

Windows Codex 语音主路由由当前 Codex 会话直接使用 Skill 与 MCP，不再为复杂任务启动
另一个 CLI 进程。现有 Realtime 调用方式和旧 CLI 实现保持不变，只作为兼容路径；
Reader 本机通道不经 Pi PWA 中转。
