# 统一 Reader 命令格式

所有写入 App/扩展的命令都通过 MCP 工具 `reader_command` 发送一个 `command` 字符串：

```text
BWREADER/1 <kind> <单个 JSON 对象>
```

允许的 `<kind>` 只有：`card`、`navigate`、`highlight`、`tool-status`。
字段必须与对应能力文件完全一致；多字段、未知动作、任意函数名、URL 或脚本都会被拒绝。

命令只投递给最新快照中同一个 `sourceInstanceId` 的在线 Reader，回执为：

- `applied`：该 App/扩展已执行或渲染；
- `replay`：相同命令已处理，未重复执行；
- 失败：明确错误码，不得假装成功或向另一页面回退。

普通回答不要放进命令。聊天同步由 Windows 本机历史同步器自动完成。
