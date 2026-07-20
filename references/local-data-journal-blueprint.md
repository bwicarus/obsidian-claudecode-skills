# 本地数据手账界面 · 数据源蓝图(2026-07-20 盘点)

> 6-agent workflow 盘点浏览器/扩展/SW/服务端全部存储层的产物。用户诉求:"新建一个手账一样的界面来观察记录保存到本地的各种数据"。

## ⭐ 关键真相:数据其实存哪(诚实版)

当前**不是**"全在本地"。分三档:

| 档 | 含义 | 属于此档的数据 |
|---|---|---|
| **真本地** | 权威副本在设备、断网不丢 | 每页足迹 `pdf-cv:*`、阅读器内提问史 `pdf-qhist-*`(不同步)、AI草稿 `pdf-drafts`、待同步队列 `rc-outbox-v1`、整本 PDF 字节(IndexedDB `pdf-blob-cache` ≤360MB)、页图/字典缓存(CacheStorage)、代码壳、扩展 `dictCache`(跨站查词 LRU≤800)、`apiToken` |
| **本地镜像** | 设备有一份但权威在 Pi | EPUB 续读 `eph-pos:*`、已掌握词 `vocab-mastered-v1`、复习队列 `review-queue-v1`、PDF 侧偏好(已同步)、各 sidecar 的离线镜像 |
| **仅 Pi** | 浏览器不常驻,权威在服务端 | PDF 续读 `reader-positions.json`、**生词库** `资源/vocab/*.md`(1370词)、**高亮** *-highlights、**便签** `reader-notes/`、插入页、收藏夹、Anki records(不外露)、注意力事件层 `attention/`(2851事件/10467mention)+ focus/dwell |

**一句话**:用户产出类(位置/高亮/便签/生词/收藏/制卡/注意力)**权威全在 Pi**;浏览器本地只留偏好+镜像+离线兜底队列+整本书缓存。→ 真正 local-first"本地权威"还没到,当前是"读快取本地、写 outbox 攒批、权威仍 Pi"。

## 手账值得呈现的 9 类(按"读到哪/查了啥/做了几张卡/有啥没同步"排序)

1. **续读书架**(卡片流+进度条)← reader-positions + eph-pos + pdf-lastopen
2. **今日/本周阅读时长热力**(GitHub 热力格)← attention/dwell.jsonl + events.db
3. **查词流水·生词墙**(按天时间线)← 资源/vocab(权威,带 first_seen/last_lookup/count);离线兜底 outbox lkevt + 扩展 dictCache + pdf-qhist
4. **词汇量增长曲线 + mastery 分布** ← 资源/vocab mastery;本地速览 vocab-mastered-v1
5. **划线摘录·本周金句**(卡片流,原文句+备注) ← *-highlights sidecar
6. **便签疑问·批注时间线**(引原话+定位书页) ← reader-notes/(最能体现主动思考)
7. **产出·今天做了几张卡/复习几张** ← Anki本体/`/dashboard`(**不读 anki/records 原始 json**);内联复习兜底 outbox fcadd/rev + review-queue-v1
8. **焦点热词·这阵子在钻什么**(标签云/burst榜) ← attention/focus.json
9. **待同步徽标·还有啥没回传 Pi**(顶栏小徽标,非空才亮) ← rc-outbox-v1 `RC.outbox.size()`(纯本地零请求)

次级(拉页):草稿箱(pdf/epub-drafts)、本机藏书(pdf-blob-cache 枚举+MB)、收藏夹(需过滤沙盒测试条目)。

## 版面草案(仿实体手账)

```
书脊顶栏:日期 │ ⏳待同步N │ 🔥连续N天
├─ 📖 今日页(Daily Log)      │ 📅 月历侧栏(活跃热力,点某天→今日页)
│   续读卡 / 查词流水 / 划线金句 / 便签疑问 / 产出+N卡
├─ 📈 成长跨页:词汇增长曲线 · mastery分布 · 焦点热词云
├─ 🔖 收藏拉页(折叠):草稿箱 · 收藏夹 · 本机藏书
└─ ✅ 待办栏(Outbox):未同步写操作明细,可手动重投
```

## 取数三档

- **纯本地零请求**:localStorage.getItem / `Object.keys(localStorage).filter(k=>k.startsWith('pdf-cv:'))`(足迹)/ `caches.open('pdf-cache-v3').keys()` / IndexedDB `pdf-blob-cache` getAllKeys+byteLength(本机藏书)/ `RC.outbox.size()`
- **需问 Pi(权威)**:`/api/reading-pos`、`/pdf/api/highlights`、`/api/notes`、`/api/favorites`、`/api/userpages`、服务端 vocab+lookup 日志、focus/dwell、`/dashboard`(Anki);MCP 通道 `reading_positions`/`list_highlights`/`list_notes`/`list_favorites` 已就绪
- **扩展专属**:`chrome.storage.local.get(['dictCache'])`(跨站查词时间线);apiToken 只判"已连 Pi"绝不回显
- **原则**:同信号本地有镜像+Pi有权威 → 展示读 Pi(全+跨设备一致),本地镜像仅离线降级+"未同步待办"

## MVP(先 3 块最小可用)

1. **续读书架**(卡片流+进度条)— 一个 /api/reading-pos + 渲染即成型,"读到哪了"门面,视觉回报最高
2. **查词流水时间线 + 词汇量数字** — 读服务端 vocab(按 last_lookup 排序+count),生词是最硬学习足迹,数据现成
3. **待同步徽标** — 纯本地 RC.outbox.size(),几乎零成本,诚实展示 local-first

第二批:划线金句(highlights)、便签时间线(notes)、活跃热力(dwell/events)。

## 落地

**建议部署在 webapp 内 `/journal/`**:复用现有登录 + `/insights` 聚合器 + reader `/api`,按用户分键缓存。与 `/insights` 分工:insights 偏统计图表,journal 偏"每日叙事流水 + 手账版面"。
**无扩展的网页端能完整跑**(一等素材权威全在 Pi,经 /api 拿全;纯本地部分网页也能读);扩展只给"跨站查词"支线补第一手离线数据,非手账运行前提。
