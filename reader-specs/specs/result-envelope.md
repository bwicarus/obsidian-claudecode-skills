# 结构化结果 envelope

一切结果走同一个信封,类型差异体现在 `kind` + `payload`,**不为每种结果另造协议**。

```
{
  "envelope": "reader-result/1",
  "correlation": "<与命令相同的关联编号>",
  "kind": "weather|news|images|videos|fact|general|cards",
  "payload": { ... 按 kind 而定 ... },
  "anchor": { "file": "<真实卷 rel>", "page": <页码>, "selection": "<可选>" }
}
```

**各 kind 的 payload 最小字段(以前端渲染器实际支持为准)**

| kind | 必填 | 可选 |
|---|---|---|
| `weather` | `lo` `hi` `cond` | `loc` `date` `precip` `tip` |
| `news` | `items[].t` | `items[].s` `items[].src` |
| `images` | `items[].url` | `title` `aid` `src` |
| `videos` | `items[].title` | `thumb` `url` `channel` `src` |
| `fact` | `answer` | `detail` |
| `general` | — | `text` |
| `cards` | `cards[]`(每条含 `front`/`cloze`/`text` 之一) | `draft` |

通用可选顶层:`title` `brief` `sources[{url,title}]`。

**锚定语义(沿用现状,不要改)**
- 书/卷/页/选区属**轮次或结果的元信息**,放 `anchor`,不塞进 payload。
- 合并书一律落到**真实卷 rel + 卷内页**。
- 高亮在正文**原位**呈现,正文不重复;卡片、便签等附属内容按各自锚定元素定位。

**硬约束**
- 渲染器画不出来的 `kind` 会被拒绝,错误里会指出是哪个字段。
- 不要在结果里塞聊天文本;不要要求接口再调用别的 AI。
