# 建待办的规则 —— 给建它的那一方（Codex）

> 用户 2026-08-29 定的形状：**你从闭集里挑参数，按格式发到指定入口，
> 后面的自动化由入口负责；失败会原样回报给你；你自己定不了的，回来问用户。**
>
> 实现：`extensions/bw-reader-webext/windows/computer-voice-desktop/replication_notifications.py`
> 测试：同目录 `tests/test_replication_notifications.py`（33 条）

---

## 0. 一句话

```bash
python replication_notifications.py create \
  --kind user-todo --title "倒垃圾" --audience user \
  --at-place 家 --end "expires:1787990400000"
```

`--end` 是**唯一一个你必须想清楚才能填的**。其余都有合理默认。

---

## 1. ⚠ 先记住这三件事，其余都是细节

### ① 「什么时候提醒」和「什么时候结束」是两件事

这是本系统出过的最贵的一次错（2026-08-29）：08-27 和 08-28 的垃圾提醒
到 08-29 还挂在提醒事项里，因为它们只设了 `--at-place 家`。

```
--at-place / --due-at   决定**什么时候提醒**
--end                   决定**什么时候结束**
```

两者看起来很像，但设了前者不等于设了后者。**`--due-at` 也不是终止条件** ——
到点响过之后，条目照样挂着。

### ② 定不了就回来问，别猜

`--end` 填错**查不出来**：填短了待办悄悄提前消失，填 never 了它永远堆着，
两种都不报错、都要几天后才显形。

> **「不知道」是合法的结果，随便填一个不是。**

用户没给足信息（比如没说这事什么时候算过期）时，**直接问他**。

### ③ 报错是写给你照做的

每一条报错都列出了可选项和下一步。读完再改，不要换个参数重试。

---

## 2. 必填：`--end` 三选一

| 模式 | 参数 | 什么时候用 |
|---|---|---|
| `expires:<毫秒时刻>` | 时刻 | 过了就没意义的事（垃圾回收日、当天的票） |
| `auto:<条件>` | 见下表 | 有客观完成信号的事 |
| `never` | 无 | 确实要一直留着（明知而选） |

`auto:` 的条件是闭集，只有三个：

| 条件 | 命中时机 | **必须同时给** |
|---|---|---|
| `item-mutated` | 账本里出现该条目的操作 | `--auto-item <itemId>` |
| `card-reviewed` | 该卡片被复习 | `--auto-card <cardId>` |
| `place-arrived` | **到达**某个已命名地点（判的是到达这个事件，不是此刻在不在） | `--auto-place <地点名>` |

⚠ **第三列不是可选的。** 一个条件不绑到具体对象上就永远不会命中 ——
建出来的是一条**永不结束**的待办，而链路上没有一处会说出来。
现在缺了会当场报错（2026-08-30 之前只有 `place-arrived` 会拦，
另外两个照样「已创建」，那期间建的这类条目要人工确认一下）。

**填不出那个 id 就别用 `auto:`** —— 改用 `expires:`，或者回去问用户。
猜一个 id 比不写更糟：它让条目看起来是有终点的。

⚠ `--expires-hours <小时>` 是 `expires:` 的便捷写法，同样满足要求。

⚠ 这条规矩**只对 `--audience user` 生效**。给 AI 自己看的条目（默认）
不需要终止条件 —— 它们靠同 `--dedupe-key` 覆盖、靠下一轮对账退场。

---

## 3. 其余参数

| 参数 | 说明 |
|---|---|
| `--kind` | 类别（`user-todo` / `trip` / …），≤40 字 |
| `--title` | 一句话，用户会在提醒事项里看到 |
| `--body` | 细节。会成为提醒的备注 |
| `--audience` | `ai`（默认，进你的快照）/ `user`（进侧边栏 tab 和苹果提醒） |
| `--dedupe-key` | 同 key 覆盖而不是新增。**重复类待办必带** |
| `--due-at` | `'YYYY-MM-DD HH:MM'` 本地时间。行程/赶车类必带 |
| `--activate-date` | `YYYY-MM-DD`，当天 00:00 起可见。持续待办用这个，不要另开定时任务 |
| `--activate-in-hours` | 同上的相对写法 |
| `--at-place` | 已命名地点（`家` / `工作地点`），到达时由系统提醒触发 |
| `--on-leave` | 改成离开时触发 |
| `--auto-item` / `--auto-card` / `--auto-place` | `--end auto:` 的绑定对象，见第 2 节。**用了 `auto:` 就必须给** |
| `--source` | 这条是谁提的（默认 `system`），出现在条目里 |
| `--deliver` | 怎么送到他面前，见下 |

### `--deliver`：四档，默认 `auto`

| 档 | 行为 |
|---|---|
| `auto` | **默认**。按位置决定：在家可出声，在工作只静音 |
| `silent` | 一定不出声 |
| `voice` | 一定出声（明确要打断时才用） |
| `call` | **打一通真电话**，穿透静音与专注模式，接通后你直接开口说 |

⚠ `call` 的两条代价都不是可以调的选项，选之前先掂量：

1. iOS 规定每个 VoIP 推送**都必须真的响铃** —— 它不能当"更响一点的
   通知"用，也不能拿来试探设备在不在线，推一次就是响一次；
2. 他一按接听，**iPad 会强制切到 BWReader 前台**，不管他当时在干什么。
   这是 CallKit 自 iOS 10 起的固定行为，没有任何 API 能阻止
   （2026-08-30 实测确认）。

所以这一档不只是吵，它会**打断他手上的事**。只用在必须现在让他知道的
事上。真要打电话时不要自己拼推送，用 `voip_push.py call`：它包办响铃、
等待和重拨，你只等它返回一行 JSON。

⚠ 如果它返回 `{"outcome": "blocked"}`，说明 ReaderPC 的「语音功能」关着
或处在桥接模式 —— **铃一声都没响过**。改走通知，并把 `reason` 原话转告
用户，那是他要动手改的东西。

⚠ `--at-place` 的名字必须与 `replication_places.py` 里已命名的一致。
名字对不上会导出 `null` 而不是报错 —— 拿不准就先 `replication_places.py`
看一眼有哪些名字。

⚠ 带 `--due-at` 的条目**必须** `--audience user`：设备侧只投影用户方向的
条目，ai 方向的到点时刻没有任何消费端。

---

## 4. 发出去之后会自动发生什么

你**不需要**做下面任何一件，也不需要在 body 里重复说明：

1. 同步到 iPad
2. 在提醒事项里建一条（列表名「BW 待办」），带地理围栏
3. 新 pending 弹一条本地横幅
4. 带 `--due-at` 的排到点通知 + 系统闹钟
5. 镜像到 Apple Watch
6. 写入小组件数据

**回流也是自动的**：用户在提醒事项里打勾 → 回到 Windows 自动 resolve。
**不需要你去销账**，也不要因为"怕它忘了"而额外建提醒。

---

## 5. 改和取消

条目有唯一编号（`ntf-` + 12 位 hex）。

⚠ **编号是位置参数，不是 `--id`。**（我第一版把它写成 `--id` 了，
而且是在说完"逐条核实过"之后 —— 实测时当场报 `unrecognized arguments`。）

```bash
python replication_notifications.py ack     ntf-xxxx           # 我看到了
python replication_notifications.py resolve ntf-xxxx [--note …] # 完成
python replication_notifications.py cancel  ntf-xxxx [--note …] # 撤销
python replication_notifications.py update  ntf-xxxx \
    [--title …] [--body …] [--activate-date …] [--expires-hours …]
python replication_notifications.py list                        # 看现在有哪些
```

⚠ `update` **只能改这四项**（title / body / activate-date / expires-hours）。
要改地点、终止方式、到点时刻，只能 `cancel` 掉再 `create` 一条新的 ——
那会是一个新的 `ntf-` 编号，用同一个 `--dedupe-key` 保持它在用户眼里
是"同一件事"。

---

## 6. 失败了怎么办

报错会原样回到你手里。三类：

| 报错说的 | 你该做什么 |
|---|---|
| 「没有终止条件」并列出三个模式 | 选一个；**选不出来就问用户** |
| 「到点时刻已经过去了」 | 给未来的时刻，或去掉 `--due-at` |
| 「end=auto: 的条件必须是 … 之一」 | 只能用那三个，不要自创 |

**不要靠换参数重试绕过报错。** 每一条报错都对应一个真实的失败形态 ——
它们是从「创建成功、却永远不响 / 永远不结束」这类事故里一条条加出来的。

---

## 7. 还没有的能力（别写这些参数）

以下在设计里但**尚未实现**，写了会报错：

- `trigger`（何时该说，区别于何时该结束）
- `on_complete`（达成后执行什么）
- 重复规则（`repeat`）—— 重复由你自己决定，每次建一条新的，用
  `--dedupe-key` 防重
