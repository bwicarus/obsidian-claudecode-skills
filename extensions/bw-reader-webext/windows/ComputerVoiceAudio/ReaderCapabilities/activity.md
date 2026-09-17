# activity
---

<!-- 2026-09-18：以下内容自 ~/.codex/AGENTS.md 搬来。原来它每条新线程都全文注入（7551 字），而这些规则只在真用到时才需要，正是本指南「按需取」的形态。 -->

## 学习活动记录查询（2026-08-25）

用户问到「我今天/最近学了什么、在哪学的、改了什么、某张卡片内容」这类
学习活动/历史问题时，直接跑（数据就在本机，零传输）：

```
python C:\Users\bwica\AppData\Local\BWReader\replication_activity.py --today
```

读取纪律（内建于默认参数，照默认用即可）：
- 默认 = 最近 1 天摘要；近 48 小时的条目自带内容，更早折叠为编号。
- 用户追问某条 → `--id <编号>`（当前内容全量 + 操作历史）。
- 范围筛选 → `--since N`（天）、`--kind highlight,note,userpage,ink,dwell`、
  `--verbosity ids|summary|full`。机器可读加 `--json`。
- 不要绕过它去整读账本 SQLite / jsonl 原始层。
