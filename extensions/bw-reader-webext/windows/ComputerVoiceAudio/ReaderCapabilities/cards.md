# 卡片输出

使用 `reader_command`，格式：

```text
BWREADER/1 card {"card":{"kind":"fact","title":"标题","data":{"answer":"结论","detail":"补充"}}}
```

`kind` 只允许 `weather`、`news`、`images`、`videos`、`fact`、`general`。`title` 可为 `null`，
`data` 必须符合 Reader 现有 Realtime 卡片的数据形状。卡片进入同一个轮次容器，
不创建另一套卡片 UI。

图片或视频卡只引用受支持的 HTTPS 资源；不要把本地路径、脚本或 HTML 当成卡片数据。
