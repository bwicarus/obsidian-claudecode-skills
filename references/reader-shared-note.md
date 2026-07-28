# Reader 实时共享便签

共享便签是现有 Reader Web 应用内的账户级 Markdown 草稿，不是独立服务，也不替代
`reader-collaboration-status.md`、文档批注或 sync-v3 数据。

## 入口与认证

- 页面：`GET /pdf/shared-note`
- 读取：`GET /pdf/api/shared-note`
- 写入：`POST /pdf/api/shared-note`

三者都复用 `/pdf` 认证。浏览器用现有 session；Codex、Claude 或桥接器使用个人页签发的
`Authorization: Bearer …`。不得把 token 写入便签、日志或命令示例。

## 写入合同

所有写入都使用 `reader-shared-note/1`，必须先读取当前 `revision`，再把它作为
`baseRevision` 发送。`source` 是 1–80 字符的调用来源；`updateId` 可选，建议每次逻辑写入
固定生成一次，网络重试时原样复用。

全文替换：

```json
{
  "contract": "reader-shared-note/1",
  "operation": "replace",
  "baseRevision": 3,
  "source": "codex",
  "updateId": "codex:20260727:context-1",
  "content": "# 完整正文\n"
}
```

尾部追加把 `content` 改为 `"text"`，并使用 `"operation": "append"`。指定片段更新使用
`"operation": "replace-text"`、`"oldText"` 与 `"newText"`；`oldText` 必须在当前正文中
逐字、连续且只出现一次，否则接口拒绝猜测位置。

成功回执包含 `result`、`revision`、`updatedAt`、`source`、`updateId` 和实时通知结果。
相同 `updateId` 加完全相同请求返回 `idempotent-replay`；同一 ID 换内容、旧 revision、
缺失/不唯一的替换目标都返回 HTTP 409，并给出机器可读错误码。版本冲突响应还会带当前权威
`note`，调用方必须读取、明确合并后再以新 `baseRevision` 写入。

## 实时与草稿边界

写入成功后只通过既有 `/pdf/api/reader-events` 广播一条不含正文、版本、来源、账户或
updateId 的 `shared-note` 失效通知。页面收到后重新读取自己账户的权威正文。若编辑器没有
本地修改就自动更新；若有未保存内容，页面保留本地草稿并显示比较、载入远端或明确以远端版本
为基线继续的选项，不会静默覆盖。

持久化采用账户 namespace 派生的不可逆目录键、进程/文件双锁与同目录原子替换。模块、模板与
`nav.js` 已登记到唯一 Reader 部署 manifest，但在完成用户人工浏览器验收并执行正式原子发布
前，功能仍只属于本地候选。
