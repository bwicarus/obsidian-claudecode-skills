# GET：读取 Reader 信息

读取操作不修改页面，使用已有 MCP 工具；不要把全部信息预先塞进每轮上下文。

## 当前页、显示窗口、位置和选区

```json
{"tool":"reader_context_snapshot","arguments":{}}
```

先看 `contextStatus`。只有 `ready` 才可使用 `currentPage`。当前选区在
`currentPage.selection` / `selectionRegions`，当前显示区域在 `readingWindow`。
同一对话首次读取某个文档时，工具会按既有读账本附上全文；之后只返回阅读窗口，
不重复挤占上下文。

## App/扩展当前合成图

```json
{"tool":"reader_visual_image","arguments":{"scope":"viewport-context"}}
{"tool":"reader_visual_image","arguments":{"scope":"drawing-nearby"}}
{"tool":"reader_visual_image","arguments":{"scope":"selection-near","selectionId":"<快照给出的 ID>"}}
```

只能使用当前快照公布的 `selectionId`。结果是即时内联 JPEG，不依赖 Pi。

## 浏览位置控制后的新内容

先按 `navigation.md` 执行命令或使用既有 `reader_browser_control`，然后重新调用
`reader_context_snapshot`。不得把控制前的文本当成控制后的页面。
