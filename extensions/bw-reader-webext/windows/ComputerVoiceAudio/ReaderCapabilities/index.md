# Reader 能力索引

只读取当前任务需要的文件，不要一次加载全部说明。

- 读取当前页、当前显示区域、选区、全文或合成图：`get.md`
- 把普通对话同步进 App/扩展现有对话记录：`conversation.md`
- 展示天气、事实、新闻、图片或视频卡：`cards.md`
- 翻动视口、定位文字/标题/选区、跳页/章节：`navigation.md`
- 把当前选区保存为高亮：`highlight.md`
- 展示工具进行中、完成、失败或中止状态：`tool-status.md`
- 统一命令外壳、回执和失败规则：`command-format.md`

现有 Realtime 与 CLI 委托链路保持不变。这些能力是 Reader 本机通道的补充，
不要求也不允许经 Pi PWA 中转。
