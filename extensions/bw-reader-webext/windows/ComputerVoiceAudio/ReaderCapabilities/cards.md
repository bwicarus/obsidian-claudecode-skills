# 卡片输出

Windows Codex 语音中，原生天气、新闻、图片、视频或事实类工具得到结构化结果后，必须在同一轮
调用 `reader_card`，把结果镜像到当前快照精确指向的 App 或扩展。文字聊天历史同步只携带用户和
助手文字，不携带卡片；不得从最终回答文字反推或补造卡片。

## 调用外壳

输入只能有 `card` 一个字段；`card` 必须且只能有 `kind`、`title`、`data` 三个字段：

```json
{
  "card": {
    "kind": "fact",
    "title": "标题",
    "data": { "answer": "结论", "detail": "补充" }
  }
}
```

- `kind` 只允许 `weather`、`news`、`images`、`videos`、`fact`、`general`。
- `title` 是必填字段，值为 `null` 或不超过 320 字符的字符串；空字符串也有效。
- 整个卡片 payload 的 UTF-8 编码不得超过 32 KiB，嵌套深度不得超过协议上限。
- 下文的 `text` 表示非空白、无 NUL、最多 2,000 字符的字符串；`scalar` 表示有限 JSON
  数字或符合 `text` 规则的字符串。标为可选的字段可以省略，但出现时不能为 `null`。
- 所有对象都拒绝未列出的字段和重复字段。

## 各 kind 的完整 data schema

### `weather`

```text
{
  "lo": scalar,
  "hi": scalar,
  "cond": text,
  "loc"?: text,
  "date"?: text,
  "precip"?: scalar,
  "tip"?: text
}
```

`lo`、`hi`、`cond` 必填；其余字段可省略。

### `news`

```text
{
  "items": [
    { "t": text, "s"?: text, "src"?: text }
  ]
}
```

`items` 必须有 1–20 项；每项的 `t` 必填。

### `images`

```text
{
  "items": [
    { "url": https-url, "title"?: text, "aid"?: text, "src"?: text }
  ]
}
```

`items` 必须有 1–20 项；每项的 `url` 必填。

### `videos`

```text
{
  "items": [
    {
      "title": text,
      "thumb"?: https-url,
      "url"?: https-url,
      "channel"?: text,
      "src"?: text
    }
  ]
}
```

`items` 必须有 1–20 项；每项的 `title` 必填，`thumb` 和 `url` 均可省略。

### `fact`

```text
{ "answer": text, "detail"?: text }
```

`answer` 必填，`detail` 可省略。

### `general`

```text
{ "text"?: text }
```

`data` 可以是空对象；若提供 `text`，它必须符合上述文字限制。

## URL 与兼容边界

`https-url` 最多 2,000 字符，必须是带非空 host 的绝对 HTTPS URL；前后不能有空白，不能含
userinfo 或反斜杠。图片 `url` 及视频 `thumb`/`url` 受此规则约束；`src` 只是来源文字，不会
被当作 URL。只引用 Reader 能加载的 HTTPS 资源，不传本地路径、`data:` URL、脚本或 HTML。

兼容调用仍可使用：

```text
BWREADER/1 card {"card":{"kind":"fact","title":"标题","data":{"answer":"结论"}}}
```

新调用应优先使用 typed `reader_card`。两条路径都复用现有 Realtime 卡片校验、精确 source
路由和回执；卡片进入当前轮次的既有容器，不创建另一套 UI，也不改变 Realtime 或 CLI 流程。
