# 工具状态输出

工具状态复用 Realtime 现有工具 chip/轮次卡：

```text
BWREADER/1 tool-status {"status":"running","tool":"reader_task","label":"正在处理","detail":null}
BWREADER/1 tool-status {"status":"done","tool":"reader_task","label":"处理完成","detail":"简短结果"}
```

`status` 只允许 `running`、`done`、`error`、`aborted`。`tool` 是稳定短标识，
`label` 是用户可见文字，`detail` 可为 `null` 或简短结果。状态不替代最终回答。
