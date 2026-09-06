"""KJ 知识节点系统（2026-09-06 依《KJ知识点系统设计讨论》实施）。

分层：
- store.py    追加式事件账本（SQLite，唯一权威）+ 可重建投影
- compute.py  掌握度折叠 / 等级 / 准备度 / 前置成环校验
- register.py 格式化登记工具（按类型校验 → 追加事件 → 重算 → 重渲染 Markdown）
- query.py    渐进式查询（搜索 / 浏览 / 节点详情 / 邻域）
- markdown.py 节点 Markdown 页渲染（程序生成、可重建，不是数据来源）
- wikidata.py 公共目录（Wikidata 编号 / 标签 / 实体值关系）+ 自动关系
- anki_sync.py Anki 卡绑定与复习快照事件
- service.py  统一门面，CLI / Flask / 助手工具都从这里进
"""

CONTRACT = "kj-ledger/1"
DB_SCHEMA_VERSION = 1
