# 高亮输出

用**工具**，不要用命令串。

| 情况 | 用哪个 |
|---|---|
| 书里（PDF/EPUB），快照给了 `currentPage.highlightSource` | `reader_highlight_range` |
| 网页 | `reader_web_highlight`（给逐字原文；同一句出现多次时补 prefix/suffix） |

颜色只允许 `yellow`、`green`、`blue`、`pink`。没有当前选区或当前宿主不支持高亮时
必须返回失败，不能猜测范围，也不能改成整页标记。

## 两条别走的路

**`BWREADER/1 highlight` 命令串**（`highlight.save` 动作）已废弃。它按"当前稳定
选区"落笔、不带 `sourceDigest` —— 从你读到快照到命令送达之间，用户可能已经翻页或
改了选区，而这条路没有任何办法发现，只会安静地划错地方。`reader_highlight_range`
带来源指纹，页面变了阅读器会**拒绝**。运行时仍然收这条命令（别的调用方还在用），
所以它不会报错 —— 这正是不该由你来用的理由：出了偏差你也看不见。

**`reader_highlight_text`** 不在 `tools/list` 里，你调不到它。它只是给老客户端
留的兼容名，工具清单有意只登记 marker range 那条。看到别处提到它，按上表走。
