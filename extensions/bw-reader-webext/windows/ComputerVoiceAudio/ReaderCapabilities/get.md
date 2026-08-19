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

## 某一页的正文与字符序号

```json
{"tool":"reader_page_text","arguments":{"file":"<快照给出的 file>","page":12}}
```

返回 `{ ok, text, segments }`：

- `text` 是该页正文，最多 1,500 字符，用来读懂这一页写了什么。
- `segments` 是 `[{ from, to, text }]` —— 每项一个词组，`from`/`to` 是它在该页
  **字符层里的下标**（闭区间）。这是拿到字符序号的**唯一来源**。

要把卡片钉到正文的某一段（`cards.md` 的 `bind` / `page-chars`），就从这里取：
挑中你要讲的那几项，`from` 取第一项的、`to` 取最后一项的，并把这几项的 `text`
拼起来一起传过去。**别自己数字符** —— 空白在字符层里占序号但不成段，手数必错位。

两个边界：`segments` 最多 400 项，长页会被截断；**EPUB 表面恒为空数组**
（它走的是可见文本，没有字符层，也就没有可用的序号），所以 EPUB 上只能用
`upage-block` 形状的绑定，或者不绑。

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
