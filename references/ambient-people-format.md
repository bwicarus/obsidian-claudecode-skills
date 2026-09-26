# 旁听人物与时间轴 · 共用记录格式（2026-09-26）

用户要求：多人说话的时间轴、每个声音块能点开看人物资料（名字 / 自定义介绍 / AI 整理 / 对话历史），
都能直接编辑；**不同的块设成同一个名字就是同一个人**（两边的 KJ 节点跟着合并）；
文字资料格式化后写进 Obsidian（和 KJ 节点联动）；以后的独立 App 读写**同一份格式**。

代码：`_server_deploy/ambient_people.py`（存储）+ `_server_deploy/ambient_jev.py`（HTTP）。
iPad 页面：设置 →「旁听与降噪」→「对话时间轴与人物」。

## 两处存储，各存各擅长的

### 1. KJ（文字 → Obsidian `KJ/`）

| 字段 | KJ 里的位置 | 谁写 |
|---|---|---|
| 名字、别名 | 节点 `name` / `aliases`，`kind = person` | 用户（改名 / 合并） |
| 自定义介绍（jev 线索） | 节点 `summary` | 用户 |
| AI 整理（关系 / 商量过 / 近况） | 定义 `context_key = "ambient-profile"`，永远只留一条有效（新的 supersede 全部旧的） | AI（攒够 5 段新对话自动重写，或手动「让 AI 整理」）；用户可直接改 |
| 对话历史 | 记录 `kind = conversation`，每个有他在场的窗口一条（`dedupe_key = ambient:<windowId>:<personId>`） | 服务器自动 |

「我」是保留 id `me`，不建 KJ 节点。KJ 里的书中人物（也是 `person`）不会出现在旁听人物列表里——
只列在旁听里出现过（有声纹或声音块）的人。

### 2. 旁听存储 `state/ambient/`（向量与时间）

`speakers.json`（contract `bw-ambient-speakers/1`）：

```json
{
  "contract": "bw-ambient-speakers/1",
  "persons": { "<personId>": { "voiceprints": [ {"vector": [256 个浮点], "from": "<slotKey>", "addedAt": 1700000000000} ], "updatedAt": 0 } },
  "slots":   { "<slotKey>": { "personId": "<personId>|null", "firstSeen": 0, "lastSeen": 0, "utterances": 3, "vector": [...] } }
}
```

`timeline/<YYYY-MM-DD>.jsonl`（contract `bw-utterance/1`，一句一行）：

```json
{"contract":"bw-utterance/1","id":"…","t0":1700000003000,"t1":1700000005000,"windowId":"amb-…","source":"ipad-mic","slotKey":"<会话>:<槽位>","isUser":false,"label":"说话人2","text":"…"}
```

- `slotKey` = App 的「分离器会话 id : 槽位」= 时间轴上的一种颜色的块。
- **名字永远在读的时候解析**：slot → personId →（跟随 KJ 合并）→ KJ 名字。所以事后起名 / 改名 / 合并，
  全部历史自动跟着变，从不回写 jsonl。`label` 只是当时 App 显示的名字，仅在未定人时兜底。
- 声纹库每人最多 12 条（新的挤掉旧的），来源是被定为此人的声音块的向量；App 比对时用全部条目。

## 合并规则（「设成同一个名字就是同一个人」）

- 给块起名：已有同名 / 同别名的 `person` → 就是他；否则新建 KJ 人物节点。块的向量并入其声纹库。
- 给人改名成另一个已有的人：KJ `merge_node`（记录 / 定义 / 关系 / 卡片随之搬过去）+ 补齐 KJ 不搬的：
  介绍拼接、旧名字变别名、两条 AI 整理收拢成一条（KJ 定义的 `supersedes` 支持 id 列表）；
  声纹与声音块都转给目标人。
- App 靠声纹比对认出某块是某人（窗口 `speakers[].personId`）：块自动归他，这次的向量补进声纹库（越认越准）。

## HTTP（设备令牌 Bearer，`/api/ambient/*`）

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/judge` | 窗口（utterances 带 `slotKey`；`speakers: [{slotKey, personId?, vector?}]`）→ 判断 + 记时间轴；回 `names: {slotKey: {personId, name}}` |
| GET | `/timeline?from=&to=` | 时间轴（≤7 天）：`utterances`（带解析后的 `personId`/`name`）+ `speakers` |
| GET | `/people` | 人物列表（首位是「我」） |
| GET | `/people/<id>` | 资料 + 对话历史（按窗口，含同窗口里所有人的话，新的在前；按窗口里最后一句排序，重转补记的句子按分钟各自成段） |
| PATCH | `/people/<id>` | 改 `name` / `intro` / `profile`（改成已有名字 = 合并） |
| POST | `/translate` | 精翻：`{lines:[{speaker,text,personId?}]}` → `{translations:[…]}`（与输入等长、按序号对齐，缺的留空）；出场人物的介绍 / AI 整理附在提示里；走 `ai_client`（Claude 登录失效自动改走 Codex） |
| POST | `/slots/delete` | `{slotKey}` 删掉一个声音块（未定人的「说话人N」）的全部句子 |
| DELETE | `/people/<id>` | `?purge=1` 连同他在时间轴上的全部句子一起删；否则只从旁听里删掉：声纹删、名下声音块退回未定人、列表隐藏（`speakers.json` 的 `hidden`）；KJ 页与记录保留（只增账本）；再被定回来就重新出现。不能删「我」 |
| POST | `/people/<id>/merge` | `{into}` 显式合并 |
| POST | `/people/<id>/summarize` | 让 AI 按历史重写整理 |
| POST | `/revise` | `{slotKey, t0, t1, text, lang, langConfirmed}` App 空闲时逐段重转后修正时间轴那一段（替换该块在该时段的句子，标 `revised`） |
| POST | `/slots/assign` | `{slotKey, name|personId, vector?}` 给块定人 |
| POST | `/slots/unassign` | `{slotKey}` 取消 |
| GET | `/voiceprints` | 每人名字 + 全部声纹向量（App 比对用） |

## 给 jev 的线索

判断时，本段里出现的熟人的「介绍 + AI 整理」作为「在场熟人的资料」拼进 jev 状态（每人 ≤500 字，最多 6 人）。
