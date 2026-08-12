# 导航输出

使用 `reader_command`：

```text
BWREADER/1 navigate {"action":"next-viewport","target":null,"selectionId":null}
BWREADER/1 navigate {"action":"previous-viewport","target":null,"selectionId":null}
BWREADER/1 navigate {"action":"scroll-to-text","target":"要定位的原文","selectionId":null}
BWREADER/1 navigate {"action":"scroll-to-heading","target":"标题","selectionId":null}
BWREADER/1 navigate {"action":"scroll-to-selection","target":null,"selectionId":"<快照公布的 ID>"}
BWREADER/1 navigate {"action":"go-to-page","target":12,"selectionId":null}
BWREADER/1 navigate {"action":"go-to-section","target":4,"selectionId":null}
```

普通网页使用前五种；本地 PDF/EPUB 可使用视口动作和页/章节动作。
只能定位当前快照公布的选区，不接受任意 CSS、JavaScript 或 URL。
成功后重新 GET 快照，以新阅读窗口为准。
