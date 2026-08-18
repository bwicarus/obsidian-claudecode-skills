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

通用可选顶层:`title` `brief` `sources[{url,title}]` `bind`。

`bind` 把这张卡**钉到页面上的某个元素**（不给＝现在的浮层行为）。它同时决定两件事:
卡片在屏幕上的位置，以及卡片内容嵌入上下文时的位置。⚠ 跟同信封的 `anchor` 不是一回事 ——
`anchor` 说的是"这条结果属于哪本书哪一页"。

两种锚。① 自建页的格子块:
```json
{"kind": "upage-block", "upage": "<插入页 id>", "bid": "<block id>"}
```
② 书页正文的字符区间:
```json
{"kind": "page-chars", "page": 46, "from": 120, "to": 132,
 "text": "中国やフランス", "rev": "<产出时的字符层 revision>"}
```
`text` 与 `rev` 可省。三个一起带的理由:
- **只用文本不行** —— 同一个词在一页里会重复出现,光靠文本说不清是哪一处。
- **只用序号不行** —— 序号是某一份字符层里的下标,而同一本书可以有多份文字层
  (PDF 原文字层 / Pi / PC),用户在书库里能切换,换一份序号就全变。
- 所以:`rev` 对得上就按序号精确定位;对不上就按 `text` 重新找,再用原序号挑
  最接近的那一处消歧。两条都失败时 `text` 仍然说得清这张卡当初钉在哪句话上。

**锚定语义(沿用现状,不要改)**
- 书/卷/页/选区属**轮次或结果的元信息**,放 `anchor`,不塞进 payload。
- 合并书一律落到**真实卷 rel + 卷内页**。
- 高亮在正文**原位**呈现,正文不重复;卡片、便签等附属内容按各自锚定元素定位。

**硬约束**
- 渲染器画不出来的 `kind` 会被拒绝,错误里会指出是哪个字段。
- 不要在结果里塞聊天文本;不要要求接口再调用别的 AI。
