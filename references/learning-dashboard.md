# 学习数据看板（/insights）

跨 Anki / 生词 / 知识图谱 / PDF 阅读 的统一学习分析页。**区别于既有 `/dashboard`**(那是 Anki 复习优先级 + KG 审计 + 词云 + D3 图谱)：`/insights` 补的是**时间序列维度**(活跃度热力图、留存曲线)+ 既有仪表盘完全没碰的**生词/阅读/KG 进度**，并且 **Anki 统计只算本工作流创建的卡、忽略旧卡片**。

创建于 2026-06-03（任务「学习数据看板」3/4）。

## 架构
自包含 blueprint(`register_insights(app)` 风格,同 control/skilltree/pdf_reader/fitness)，**请求时实时聚合 + 120s 进程缓存**(不耦合 daily 流水线 → 永远新鲜、不拖垮夜间任务)。所有数据本机文件/SQLite，聚合 ~1-2s，缓存后秒开。

- 后端 `_server_deploy/insights.py` → 部署 `/home/bwicarus/webapp/insights.py`
- 前端 `_server_deploy/templates/insights.html`(暗色、移动优先、**手搓 SVG 图表**不依赖 CDN，契合离线 PWA 方向)→ 部署 `/home/bwicarus/webapp/templates/insights.html`
- app.py 三处接入：`from insights import register_insights; register_insights(app)`(app.run 之前) + `PROTECTED_PREFIXES` 加 `/insights`(鉴权) + `NAV_INJECT_PREFIXES` 加 `/insights`(注 nav.js+PWA)
- Pi nginx：`/etc/nginx/sites-available/bwicarus` 两个 server 块(80+443)各加一行 `location /insights { proxy_pass http://127.0.0.1:5000; ... }`(Pi 的 `location /` 是静态 404 兜底不反代，必须显式加；改完 `nginx -t && systemctl restart nginx` 不是 reload)
- 入口：nav.js DEFAULT_LINKS + 用户 nav-links.json 都加了「学习数据看板」→ `/insights/`；`/dashboard` 页头也有按钮
- 部署：`cp insights.py app.py /home/bwicarus/webapp/ && cp templates/insights.html /home/bwicarus/webapp/templates/ && sudo systemctl restart webapp`

## 路由
- `GET /insights/` → 渲染 insights.html
- `GET /insights/api/data` → 完整 JSON(见下)，**按 session username 分键缓存**(多用户不串数据)

## payload 结构(`_build_payload`)
- `kpis`：streak_days(连续学习天)/today_lookups/today_reviews/due_today/overdue/total_vocab/total_cards/mastered_nodes/week_*
- `activity`：近 182 天 `[{date,reviews,lookups,total}]` → GitHub 风格日历热力图(reviews 已排除旧卡 + 查词)
- `anki`：retention_curve(按 lastIvl 分桶 pass 率)/retention_young/mature/funnel(queue 映射)/due_forecast(未来14天)/due_today/overdue/stability(FSRS s 分布)/per_deck/total_cards/**excluded_legacy_decks**
- `vocab`：total/by_lang(en,ja)/mastery_hist(10桶)/by_label/cumulative(词汇量增长)/lookups_per_day/top_books
- `kg`：books[](每本 l2_total/mastered/unlockable/locked/tracked/is_grammar/pct)/recently_mastered/next_up/total_*
- `reading`：books[](title/page/total/pct/ts，按最后阅读时间，总页数来自 pdf-search.db meta)
- `notes`：by_lapse/by_retention/subjects(全部来自既有 dashboard.json 的 notes[]，即本工作流创建的笔记卡)

## 数据源(全部只读)
- **Anki** `~/.local/share/Anki2/User 1/collection.anki2`(`?immutable=1` 只读铁律，Anki headless 在跑也安全)：revlog(留存/活跃度)、cards(漏斗/到期/FSRS data 的 s)、decks
- **vocab** vault `资源/vocab/*/*.md` frontmatter(mastery/lang/mastery_label/first_seen)+ `state/vocab-lookups.jsonl`(查词事件)
- **KG** `knowledge_graph/*.json`(排除 .bak)
- **PDF** `state/pdf-prefs/<user>.json` 的 pdf-last-positions(注意 localStorage dump，值是字符串要二次 json.loads)+ `state/pdf-search.db` meta(总页数)
- **notes** `dashboard/dashboard.json` 的 notes[]

## ⚠ 忽略旧卡片（核心设计，用户明确要求）
用户的 Anki 里有大批量**早就存在的日语沉浸牌组**(新 2126卡/27653复习、漢字 1898/13990、意味 476/2774，合计 ~4500卡/44k 复习)，会把真实新学习淹没。看板**所有 Anki 统计都过滤掉这些旧牌组**，只算本工作流的学习(数学/CS/生词/语法/项目…，剩 1254 卡/6838 复习)。
- 实现：`_LEGACY_DECKS`(默认 `新,漢字,意味`，env `INSIGHTS_LEGACY_DECKS` 可覆盖)→ `_legacy_deck_ids()` 取这些顶层牌组(含子牌组)的 did → 每条 Anki 查询加 `AND c.did NOT IN (...)`(decks.name 用 `\x1f` 分层，按顶层名匹配)
- 前端 Anki 面板显示「已忽略旧牌组：新 / 漢字 / 意味」
- 备选方案(未采用)：只算 pipeline 建的卡(anki/records note_id + vocab anki_card_id)→ 仅 185 卡/524 复习，太稀疏，Anki 分析会空掉

## 踩坑
- **留存/活跃度只统计本工作流卡** → mature 桶(2-6m/6m+)很稀疏甚至 0(新材料卡还没到长间隔)，young/mature 留存率代表性偏弱；前端已标注「只统计本工作流创建的卡」。留存曲线只用 `revlog.type=1`(review 阶段)，relearn(type=2)不计。
- **forecast 查询占位符顺序坑**：`due-?` 出现多次 + 中间插 `NOT IN` 片段会让绑定错位 → 改成 `GROUP BY due` 在 Python 里减 today_off。
- **stability 分桶下界必须=0**：FSRS s 单位是天，学习阶段卡 s 普遍 0<s<1，下界写 1 会静默丢掉 ~39% 的卡。
- **缓存必须按 username 分键**：payload 含 per-user 阅读进度，全局单缓存会跨用户泄露(多用户实例)。
- **时区**：显式 +9h(JST_OFF=32400)算日历天，不依赖服务器 TZ。reviews/lookups/today/streak 全用同一口径(JST 午夜，非 Anki 的 04:00 rollover，差约 3.2% 边界复习，可接受且让 reviews+lookups 对齐)。
- `due_today` 含逾期(`due<=today_off`)，前端拆成「待复习(逾期 M)」。
- `_safe()` 包装每个数据域：某源失败(如 Anki 离线)只那块降级返回 `{_error}`，整页仍出；前端对每块 `if(A.xxx)` 守卫。
- 手搓 SVG：heatmap 周对齐 `pad=(getDay()+6)%7`(周一列首)；vbars/area/hbars 复用；tooltip 用全局 `event`(iPad Safari OK)。
