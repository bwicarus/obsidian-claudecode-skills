# 高亮输出

用**工具**，不要用命令串。

| 情况 | 用哪个 |
|---|---|
| 书里（PDF/EPUB），快照给了 `currentPage.highlightSource` | `reader_highlight_range` |
| 宿主没有标记表 | `reader_highlight_text`（给逐字原文） |
| 网页 | `reader_web_highlight` |

颜色只允许 `yellow`、`green`、`blue`、`pink`。没有当前选区或当前宿主不支持高亮时
必须返回失败，不能猜测范围，也不能改成整页标记。

## 为什么不用 `BWREADER/1 highlight`

那条命令串（`highlight.save` 动作）**已废弃，别再用**。它按"当前稳定选区"落笔，
不带 `sourceDigest` —— 从你读到快照到命令送达之间，用户可能已经翻页或改了选区，
而这条路没有任何办法发现这件事，只会安静地划错地方。`reader_highlight_range`
带着来源指纹，页面变了阅读器会**拒绝**，不会将错就错。

运行时仍然收这条命令（别的调用方还在用），所以它不会报错 —— 这正是不该由你来用
的理由：出了偏差你也看不见。
