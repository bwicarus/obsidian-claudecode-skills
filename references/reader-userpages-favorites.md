# 阅读器:插入页(用户页)+ 收藏夹系统

> 2026-07-03 用户提出,2026-07-04 迭代收敛。遵守 `references/unified-control-layer.md` 顶部设计铁律。
> **本文是「最终态」叙述**(被后续推翻的中间结论已砍,只在各节末「演进史」小节压缩留档,标『已废,勿据此实现』)。改代码前读本文对应节 + 铁律。

## 用户规格(原话拆解)

**插入页**:点入口按钮,在目标页位置建立新页,**支持所有阅读器功能**(选词/查词/翻译/AI/高亮/手写)。
**收藏夹**:①页码/scrubber 旁 ⭐ → 收藏选择窗(已有夹**多选** + 新建);②阅读器收藏夹页面能看到并进入;③**收藏夹 = 一本书**:书架单独 tab、点进去用**同一个阅读器全套功能**,但**不记停留位置/不记「已开启」**(不污染阅读进度),可另开窗口与正在读的书搭配。用户重申两条关键:「收藏夹本身应被阅读器识别为一本书」「我们自己创建的插入页也能加入收藏夹」。

---

# 一、插入页(用户页)—— 最终态

## 核心口诀:页的存在 vs 页的内容 分离

- **页的存在**:PDF 里放一张**空白真页**(真改 PDF 页数,一次性重 job,保证页号/14 类锚迁移全对);EPUB 里插一个 `.ep-usec` DOM 段(不占章序,零迁移)。
- **页的内容**:文字存 sidecar `state/reader-userpages/<sha>.json` 的 `md` 字段(像手写墨迹),编辑=覆盖层就地打字 → **防抖即时存边车,不触发 PDF job、不 reload、无「保存」按钮**。
- **显示**:那张页上挂一层**常驻覆盖层**(`md→RC.md` 渲染 + MathJax + offset 高亮),**永远覆盖层态**——不再退化成普通 PDF 页(这是与「批次2 变普通页」相比最重要的方向反转,见演进史)。
- **后台同步**:完成编辑/关书时**异步**把边车 md 用 edit job 写回 PDF(delete_page + 同位重插,页号不变=零页号锚迁移)。**只为可移植 + 全文搜索 + 离线字符层,不影响前端显示**。

数据真源=边车;PDF 内文本是派生。选词/查词/AI/高亮走**覆盖层原生 DOM Selection**(复用 `html-reader.js` 那套单容器 offset 锚),不依赖 PDF 字符层。

## 数据模型(sidecar,一个 list 三种记录并存,缺 `mode` 按老语义走)

| 形态 | 判别 | 关键字段 | 语义 |
|---|---|---|---|
| v1 虚拟页(EPUB 沿用) | 无 `page`,有 `after` | `{id,after,title,md,h?}` | 纯 DOM 渲染,不进 PDF/EPUB 文件 |
| **overlay 真页**(PDF 现役) | `page:int` + `mode:"overlay"` | `{id,page,title,md,real:true,mode,md_ver,synced_ver}` | PDF 里是**空白真页**;文字真源在边车 `md`,覆盖层显示 |
| baked 真页(存量,不主动转) | `page:int` + `mode` 缺失/`"baked"` | `{id,page,title,md,real:true}` | 文字已烧进 PDF;编辑走重排(见下) |

- **脏标记用版本戳**:`md_ver` 每次写 md 自增;`synced_ver`=最后一次写进 PDF 的 `base_ver`(**单调 max**)。脏=`md_ver>synced_ver`。
- 端点:`/api/userpages`(GET/POST/PATCH/DELETE,边车 CRUD)+ `/api/pdf-insert-page`(POST 插空白真页 / PATCH 编辑或 overlay 同步 / DELETE 真删页,均返 job_id,轮询 `/api/job-status`)。

## PDF 插入页:乐观新建 + 全功能

- **乐观新建**(`_upCreate`):点 ➕ **立即**在插入点(`_upPlace`)插一个可编辑覆盖层(临时 `tmp_*` client id 的独立虚拟 `.pdf-upage`,**不占 `data-page-num`**),直接进即时编辑,用户马上打字;后台静默 `POST /api/pdf-insert-page` 插空白真页 + 右下角 `.up2-mini` 进度胶囊。job done → `_upWatchCreate` 取 `result.page` → `_upResolveNewId`(GET userpages 按新页号唯一匹配拿真 id)→ `_upBindTempToReal` 就地把 `rec.id` 改成真 id + 迁 `_upTextSnap/_upEditedIds` + 落库缓冲击键。**全程不 reload、不等待**。job error → `_upTempFail` 移除临时元素(缓冲的字仍在 `_upTextSnap`,不静默丢)。
- **即时编辑**(`_upTextSchedule/_upTextSave/_upTextFlushBeacon`,照手写墨迹三件套):防抖 600ms `PATCH /api/userpages` → `md_ver++`、不碰 PDF;`pagehide` keepalive PATCH 兜底(⚠ **不用 sendBeacon**:userpages POST=建新页,sendBeacon 只能 POST 会误建重复页)。取消「保存」改「完成」(仅收起面板,内容早已自动存)。
- **全功能**(临时虚拟元素与真 `.page-wrap` 一视同仁,`_upMountOverlay` 补齐):
  - **手写**:`_upEnsureInk(el)` 照 04-render 建 `.ink-layer` canvas(z7)+ 绑 capture `_inkPointerDown`,复用全部绘制逻辑;墨迹存 `el.__inkStrokes`,绑真 id 后 `POST /api/ink`。
  - **单击查词 / 精确高亮 / 多词工具栏**:覆盖层文字走 DOM Selection 胶水(`_ov*` 前缀,容器=`.up2-content-body`)——`_ovCaretFromPoint`+`_ovWordAt` → `RC.wordpop.show`;精确高亮 offset 锚存 sidecar 复合键 **`UP_FILE::rec.id`**(端点复用 `/api/html-highlights`,每 overlay 页独立 offset 空间),TreeWalker splitText 包 `<mark>`,`_ovTypesetThenHl` 在 MathJax typeset 后按 offset 复原;多词划选沿用原生 `#sel-toolbar`。char-layer 隔离:overlay 页 pw 打 `.pdf-upage-overlay` + CSS `char-layer{pointer-events:none}`。
- **公式**:覆盖层显示 MathJax **渲染后**的公式(整体不可选内部符号=所见即所得,正常);要看/改**源码**点左上角 `Aa` 进编辑态 textarea(`$..$` 原文)。**显示层=渲染、编辑层=源码**是合理分工。
- **高度/宽高比**:PDF 插入页高度=**邻真页高**(`.up2-new` 设 `aspectRatio=w/h` + `min-height:0` + `.pdf-upage{max-width:100%}`);开侧栏挤窄时随邻真页 06-layout refit 等比缩,始终跟原书页一致(不加拖动手柄,保持简单)。

## 后台同步 = 写回 PDF(edit job,零页号锚迁移)

- **触发**:完成编辑 + 关书 keepalive PATCH。**绝不每次击键、绝不 8~10s 自动防抖**——见下成本。串行队列 `_upSyncQ/_upSyncPump`(一次一个,应对 `_INSPAGE_ACTIVE` 409),右下角小胶囊,不阻塞不 reload。
- **🔴 BLOCKER 三修法(必守,否则后台同步用旧快照覆盖并发编辑=静默丢字)**:
  1. **per-rel `_upages_lock`**:userpages 边车所有 RMW(`/api/userpages` CRUD + job 的 `_up_collect_plans→_up_apply_plans` 整个迁移事务)串行化。单 worker 但后台 job 是独立 daemon 线程,不锁则 job phase1 读的旧快照在 phase2 落盘会覆盖 PATCH 刚写的新编辑。`doc.save`(慢)在锁外,锁只压几毫秒迁移。
  2. **overlay 记录 job 禁止回写 md/title**:边车是唯一真源,job 只 `synced_ver = max(synced_ver, base_ver)`(单调 max)。baked 记录保持原 update(写 md/title)语义。
  3. **sync 路由 record_op 只带 `base_ver`,不带 md/title**:job payload 另带 md/title(渲进 PDF 用),取自服务端**锁内原子快照**(md+md_ver 一起读,不信客户端);`md_ver<=synced_ver`(不脏)→ 直接 `{clean:true}` 免一次昂贵 `doc.save`。
- **自主路径文本真源**:未同步(脏)的 overlay 页 PDF 那页是空白 → `_page_text`(assistant.py,`_overlay_md_for_page`)、`build_search_index.py`(`_apply_overlay_supplement` + 把 overlay 边车 mtime 折进书变更签名)遇脏 overlay 页用边车 md 补 `get_text`,让 read_page/搜索/译页不落空;已同步页 PDF 已有文字,不重复补。
- **⚠ 成本(如实,别掩盖 Pi 代价)**:**每次后台同步 = `_inspage_job` 整本 `doc.save` + 刷 book_mtime → 全书页图/字符层缓存(按 mtime 键)全部失效**,用户随后滚到任意未编辑页都要重渲染(Pi ~几秒/页)。这正是同步频率**必须**压到「完成编辑 + 关书」的原因(8~10s 自动防抖会把「一次 reload」痛点换成「反复逐页重渲染」更碎)。不脏免同步(clean 短路)是关键省心。未来可评估 PyMuPDF 增量保存降低 mtime 影响。

## 真插 / 真删(改 PDF 页数)+ 锚迁移注册表 `PAGE_ANCHOR_MIGRATIONS`

新建插空白页(POST job)、删除真减页(DELETE job)**改的是「页的存在」,必须一次性落地并迁全书页锚**;只有 overlay 的**文字内容**走边车即时存不进这条重路。

- **安全落盘**:磁盘空间守卫 → 备份 `state/pdf-page-backups/<sha>/<ts>.pdf`(留最近 2 份)→ PyMuPDF 改页 → tmp 同目录 save(点开头文件名避开 Obsidian Sync;<40MB `garbage=3` 否则 `garbage=1`+deflate)→ 重开断言页数 → **journal**(写于 `os.replace` 前、迁移全部完成后删;残留 → 后续操作 409 拒绝,防「替换成功但迁移没跑」带伤续写)→ `os.replace` 原子替换 → **锚迁移事务**。任何异常回滚不动原书 = 全成或全不成。
- **锚迁移注册表**(⚠ **未来任何新增按页存储必须在此登记**):每项 `(name, fn(ctx)->plans)`。插入=page>after 则 +1、删除=反向 -1(被删页锚丢弃)、编辑=0。**事务两阶段**:phase1 全迁移器纯内存算出 write/rename/unlink 计划(读原始 JSON,绕过 loader 的 mtime 清空守卫)→ phase2 统一落盘,任一步失败逆序回滚 + copy 备份恢复 PDF。
- **需迁 14 项**(2026-07-03 全项目 grep 盘点):pdf-highlights / reader-notes(便签 anchor.page,`u_*` 字符串跳过)/ pdf-ink / reader-favorites(本书 items)/ reader-userpages(真页 page + 旧 after 边界)/ pdf-tr-sentences / pdf-char-offset / pdf-toc(仅 range,entries 是印刷页天然稳)/ sentence-cards(页号在 sha1 键 → 重键)/ vocab-exposure / **pdf-figures + figures_geom + formulas**(各 page + `_none_pages`,并把 `book_mtime` 刷新 → loader mtime 守卫不触发,贵的 AI 图描述/YOLO 框全保留)/ pdf-ocr-fix(同款 mtime 刷新)/ pdf-page-ocr(页号在文件名 → 改名)/ mokuro+google-vision OCR checkpoint(`p%04d` 0-based → +1 降序改名防撞)。
- **免迁**(键机制天然失效,已逐一读代码确认):页图 `sha-p{p}-w{w}-{mtime}.jpg` / 字符层 `sha16-p{p}-{mtime}-{lang}.json` / 振假名·整本文本(文件名含 mtime → 替换即换键)/ FTS 搜索(meta.mtime 变 → quick_sync ≤15min 自动整书重建)/ lastopen / grammar 缓存 / assistant 会话 / vocab-lookups / pdf-prefs 阅读位置(客户端 localStorage 为真源)。
- **已知妥协**:pdf-book-offset 印刷页偏移是全书标量,插入点之后印刷页显示差 1(设置可重对齐);vault 笔记/Anki 里历史「第N页」散文引用不迁。

## EPUB 插入页(`.ep-usec` DOM 覆盖层,镜像 PDF overlay 但更简单)

EPUB 插入段本就是 `#ep-col` 内的 DOM 文本 → 选词/查词/AI/高亮**直接走 EPUB 正文那套 DOM Selection**(现成),**永远覆盖层、天然全功能**;无 PDF 那套字符层隔离/同步进文件/变普通页/后台 job/临时 id 绑定(EPUB 无「写进文件」概念)。改动仅 `rc-userpages.js`(`opts.instant` 门控)+ `epub-html.js`(加法式,`.ep-sec` 路径逐字不变)+ `pdf_reader.py`(`/api/epub-ink` 放行 `u_*` 字符串 idx)。

- **整页所见即所得**:`.ep-usec` = `min-height:86vh` + 纸底阴影(视觉=多出一页,跟主题变量);左上角 `Aa` 毛玻璃钮进即时编辑;显示层 `RC.md` + `onRender` host 钩子(MathJax typeset + 按 offset 复原高亮 + 复原墨迹)。
- **即时保存三件套**:照搬 PDF(防抖 600ms `PATCH /api/userpages`,离页 keepalive PATCH)。**乐观新建**:点 ➕ → `createInstant` 直接 `POST /api/userpages`(建记录=一次本地边车写,~毫秒,**无后台 job**)→ 回来即本地插入 + 进编辑。
- **选区/高亮 string section 锚**:`captureSel` 遇 `secOf` 返 null 且选区在 `.ep-usec .rc-up-body` → `secInfo={el:body, idx:usec.dataset.uid}`(`u_*` 字符串);`_secElOf`/`saveHl`/`loadHls`/`markHighlight` 统一走它;offset 空间=用户页正文 body(排除 Aa/标题 chrome)。单击查词/多选/翻译逐字复用正文路径。
- **手写**:`_inkPointerDown`/`_inkBeginPageSegAt` 的 `closest('.ep-sec')` → `'.ep-sec, .ep-usec'`;`_inkIdxOf`(有 `dataset.uid`→u_* 字符串键,否则 `parseInt(dataset.idx)`);`_epUpApplyInk` 在 onRender 建 canvas 重绘。
- **可拖高度**(用户:「EPUB 编辑模式能手动调高、记住」):`.ep-up-rz` 下边缘 16px 窄条**只编辑态露出**,pointer 事件统一鼠标/触摸 + `setPointerCapture`,实时 `--uh`(px)驱动高度;存**绝对 px** 到 `userpages` 记录新字段 `h`(用 `min-height` 而非 `height`,内容超过 `h` 自然增长永不裁文字),防抖 300ms `PATCH {file,id,h}`。`p.h` 有值→**手动模式**(`.uh-set`,不跑 keepRatio);无值→默认 **keepRatio 等比**(ResizeObserver 最大宽度自愈,`natW/natH` 算 aspectRatio,侧栏挤窄等比缩)。clamp 交互 120~300vh、服务端 60~30000px。后端 `pdf_api_userpages` PATCH **EPUB 虚拟页分支**放行 `h`(PDF overlay 真页分支不加 `h`,永不发)。

## PDF ⇄ EPUB 插入页 异同

| 维度 | PDF overlay | EPUB 插入页 |
|---|---|---|
| 页的存在 | 真插进 PDF 文件(多秒 job + 14 类锚迁移 + 空白真页) | `.ep-usec` DOM 覆盖层 + 边车(无 job/无迁移/无 reload) |
| 编辑/保存 | `_upTextSchedule/Save/FlushBeacon` + Aa 钮 + 无保存按钮 | **逐字照搬**(移进 rc-userpages instant 模式) |
| 选词/查词/高亮 | 覆盖层关字符层 + 复用 html-reader offset 胶水 | 直接走 EPUB 正文 DOM Selection(string section 锚) |
| 手写 | `_upEnsureInk` 补虚拟页 canvas(踩过 `currentPage` ReferenceError) | 复用 `_epInk`(按 section idx,给 u_* 字符串 idx) |
| 新建 | 乐观临时 id + job 轮询 + 绑定 + 缓冲击键 | POST 建记录即插(无 job → 不要临时 id 复杂度) |
| 高度 | 邻真页高(不可调) | 编辑态可拖 + 持久化 `h` / 默认 keepRatio |

## 关键踩坑(现役,盲改必踩)

- **🔴 `currentPage` ReferenceError**:`_inkPointerDown`(模板内联 script,**非 ES module**)里 `parseInt(pw.dataset.pageNum) || currentPage`——`currentPage` 是 reader.js 模块级 `let`,内联取不到。真页有 `data-page-num` → 真值短路从不求值(故真页手写一直好);**临时元素无 pageNum → `NaN || currentPage` 求值抛错**中断绘制。修法:`|| (typeof currentPage !== 'undefined' ? currentPage : 0)`(`typeof` 对不可解析标识符返 'undefined' 不抛)。**不能给临时元素塞哨兵 `data-page-num`**:会撞裸 `[data-page-num="N"]` 选择器(渲染/搜索/助手跳页)。墨迹落盘 `_inkScheduleSave` 顶部守卫 `if (pw.__upRec) { _upInkPersist(pw); return; }`(虚拟元素不走 num-keyed POST 污染别页;守卫只对有 `__upRec` 的元素,真页/legacy 零影响)。
- **iOS 输入框隐形**:`.page-wrap`/`.rc-upage` 家族 `-webkit-user-select:none`,子树内 textarea 编辑期不渲染字形/光标。修法:`.up2-inline .rc-up-ti/ta`、`.up2-content .up2-content-body`、EPUB 编辑链**自给全套样式** + 显式 `-webkit-text-fill-color`/`user-select:text`/`appearance:none`(滚动容器内加 `translateZ(0)`)。同 [[sticky-notes-design]] 便签教训。
- **点色板时机**:点色板前 mousedown 可能已折叠原生选区 → 不能现读 `getSelection`,改为**选区时**(`_selDone`)捕获 `_ovLastSelInfo` 快照,色板拦截用快照 + document capture 作废机制。
- **退路=整页 reload 是唯一可靠对账**:canvas 模式(小 PDF 整本进 PDF.js)的 `pdfDoc` 是开书时旧 buffer,单页重渲会渲出旧空白页;客户端做不到服务端「全成或全不成」的迁移事务保证。故不做在线全书重编号(insert 之后每页正文全错到 reload)。乐观/覆盖层的价值=**在保留 reload 当兜底前提下把它变无感/没必要**,而非挑战这条底线。

## 演进史(已废,勿据此实现)

插入页经历:**v1 虚拟页**(不进文件,EPUB 沿用)→ **v2 真插 PDF**(改页数)→ UX 三连改(就地编辑替屏幕中央弹窗、乐观非阻塞替全屏遮罩、编辑期禁阅读器功能)→ 大厂范式 P0/P1(删除改 Gmail 式撤销条替 `confirm()`、保存后立即显示内容)→ **v4 即时编辑**(文字拆出 PDF 存边车,后台异步写回)。**关键反转**:v4 批次2 曾定「完成同步→重开变回普通 PDF 页」退路,但用户实测发现高亮不持久、公式重开不渲染(`insert_htmlbox` 阶段1把公式降级 `$..$`)、offset 高亮在普通页态休眠 → **改为 overlay 页永远覆盖层态**(公式永远 MathJax、offset 高亮永远持久),同步进 PDF 保留但**不再影响前端显示**。「变普通页」`_upIsSyncedAtLoad` 分流已删。

---

# 二、收藏夹 —— 最终态

## 数据模型 + ⭐ 入口

- **真源**:`state/reader-favorites.json`(全局,不分用户),`{folders:[{id:'f_...', name, items:[…], content_sig, built_sig, built_ver}]}`。
- **item 三类**:`{file, kind:'pdf', page}` / `{file, kind:'epub', section}` / `{file, kind:'userpage', id:'u_...'}`(自己创建的插入页;`file`=该页所属书 rel,`id`=userpages 记录 id)。
- **⭐ picker**(`rc-favorites.js`,共享,injectCss):`RC.favorites.openPicker({file,kind,page|section|id})` → 弹窗 checkbox 多选(已在的夹打勾,勾/取消即时 POST/PATCH,失败回滚)+ 底部新建行(回车即建并收当前页)。`normItem`/`sameItem`/`itemLabel` 认三种 kind(userpage 缺 id → toast 拒)。
- **亮暗星**(`bindStar`):未收藏=去色暗星,当前页/章已在任一夹=原色亮星+微光;`_folders` 内存缓存 + document capture scroll 节流 350ms 刷新 + visibilitychange 回前台重拉。PDF=模板 `_favCurTarget`(视口交叠最多的 `.page-wrap`)、EPUB=`_curTopIdx` getter。
- **禁自我收藏**:对 `资源/收藏夹/` 前缀书隐藏 ⭐(`bindStar`/顶栏入口,PDF+EPUB 通吃)+ `_fav_norm_item` 后端拒该前缀 file(双保险)。
- **收藏入口(userpage 页)**:PDF overlay 页右上角 `.up2-fav-btn`(`_upEnsureFavBtn`,z44,编辑态被覆盖层盖住)/ EPUB 插入段右上角 `.ep-up-favbtn`(`buildInstant`,z12,编辑态 `display:none`)。
- CRUD 路由 `/pdf/api/favorites`(GET/POST/PATCH/DELETE)。GET/PATCH 不调 `_lastopen_touch`。

## 物化成一本真 EPUB(现役核心)

**架构**:收藏夹**真源仍是 item 列表**,由它**物化(derive)**出一本标准 EPUB3 zip `state/reader-fav-epub/<fid>.epub`,用**完整 EPUB 阅读器**(`epub-html.js` + 全套 rc-*)打开 → 选词/查词/AI/侧栏助手/高亮/手写/生词/振假名/语法/插入页 **全部天然可用**。

- **合成 rel 键 + resolver**:EPUB 阅读器/高亮/墨迹 sidecar 用**合成 rel** `资源/收藏夹/<fid>.epub`(`_fav_epub_rel`,**不是 vault 文件**);`_resolve_epub_book(rel)` 把该前缀解析回 `state/reader-fav-epub/`,其余走 vault。epub-css/manifest/section/search/`_epub_section_paragraphs` 5 处 `_safe_vault_path→_resolve_epub_book`。**epub-html.js / epub_html_reader.html 逐字零改**(选型=物化真 EPUB,不改前端造虚拟书入口)。
- **按 fid 命名**(不按夹名):所有 sidecar 键是 sha(合成 rel),按夹名命名则改名=换 sha=孤儿掉所有高亮/便签/墨迹。按 fid → **改名只改 `name`,零文件移动、零孤儿**;标题栏显示 `f_xxx` 是已知小瑕疵(批次3 映射)。
- **`_fav_write_epub`**(EPUB3 zip:`mimetype` 首个 + ZIP_STORED 不压缩 → container/OPF/nav/`fav.css` → 各条目一个 `sec_NNNN.xhtml`,**严格 1 item=1 section=1 spine**):
  - **PDF 条目**=`_fav_pdf_page_jpg`(PyMuPDF **只读原书**渲原分辨率 JPEG w=1520)打包 `img/pdf_NNN.jpg` + `_fav_pdf_overlay_spans` 生成**透明可选文字层**。
  - **EPUB 条目**=`_epub_section_cached`(原书**只读**)消毒 HTML → 图**读字节打包**进 zip(byte-identical 不压)+ src 改 zip 内相对路径。
  - **userpage 条目**=`_fav_userpage_item`:`_upages_load(file)` 找 id 记录(**只读**)→ md 经 `_fav_render_userpage_md`(复用 `_up_md_html`:标题/列表/加粗;数学 `$..$`/`$$..$$` 先抠占位再 HTML 转义还原;图片/链接先抠占位)→ `_fav_pack_userpage_imgs` 打包本地图、远程/`data:` 图原样留。
  - 条目间**分隔条**(《书名》·页/章名 或 「📝我的页·标题·出自《…》」 + 「打开原书 ↗」站内深链)+ `nav.xhtml` TOC + `set_metadata(title=夹名)`。
- **PDF 条目选词=完整可用(v7,非退化)**:`/api/epub-section` 对**收藏夹前缀走 raw**(`_fav_epub_raw_section`:服务端亲手生成的可信 HTML,**不消毒**,只把 zip 内相对 img src 改代理 URL)→ 透明词层行内 style + 「打开原书」链接全保住(`_sanitize_epub_section` 会剥行内 style 与站内 `/pdf/` 链,故必须 raw)。raw 分支**只对收藏夹前缀**,普通书仍全量消毒(零回归)。
- **透明词层结构(v7)**:`_fav_pdf_overlay_spans` 按视觉行分组——① 相邻同 `w` 合成词(**行感知 guard**:同 `w` 但下一字竖直中心错开当前词 y 带→断开,防 `w` 退化 `-1` 把上行末词与下行首词粘成跨行 token);② 同 `bk`+竖直中心相近归**视觉行**;③ 每行一个 `.fav-pdf-line` 行盒(absolute 定位在行 bbox),行内词 `inline-block` 排在正常行内流。`left/top/width/height`=**%**、`font-size`=**cqh**(`.fav-pdf-page{container-type:size}` → 随页图/reflow 列宽等比缩,免 JS),容器 `aspect-ratio=page_w/page_h`。文字节点顺序=reading order → offset 锚/高亮/便签口径与旧一致。

## PDF 条目 char-layer 式自定义选择(v7,根治 iOS 乱跨行)

**坑**:收藏夹 PDF 透明词层若走**原生 DOM Selection**,iOS Safari 对嵌套 absolute span/无行盒文本会整块乱选(与 pdf.js textLayer iOS 老 bug 同源:mozilla/pdf.js #14243/#20017)。EPUB 正文/PDF 阅读器 char-layer 都不出问题——前者有行盒,后者 `user-select:none`+自建 caret 从不走原生选区。**桌面 Chromium 复现不出(WebKit 专有)**。

**根治(选「中」:epub-html.js 里新写一小段照 13-selection 思路,只挂收藏夹 PDF 条目,不动 reader.src/EPUB 正文)**:
1. CSS:`.fav-pdf-line`/`span` `user-select:none`(**彻底关原生选区,iOS 长按拖选引擎压根不启动**)+ 自绘高亮层 `.fav-pdf-sel`(`pointer-events:none`,`.sel-overlay` 等价物)。
2. epub-html.js 新模块(仅 `IS_FAV_BOOK`=FREL 前缀生效,普通书完全惰性):`_favWords`(词 span→bbox 缓存)/`_favHit`(bbox→同行最近→Manhattan,照 `_findCharAt`)/`_favPaintWords`(选中词合并成行矩形画进 `.fav-pdf-sel`)/`_favStart|Move|End`(照 `_bindCharLayer` 触摸方向锁:竖滑=滚动放弃、横拖=选字 preventDefault),`_favBindSection` 在 `_fetchSection` 空闲帧挂。
3. `_favEnd`=**手动组 cur + showSel**(不碰原生 selection):`offsetOf/_countableText` 算 offset 锚 + 选中文本(与高亮/便签同口径)。⚠ **两个非显然坑**:(a) `user-select:none` 下程序化 `addRange` 后 `getSelection().toString()` **恒空** → 必须手动 cur;(b) content 的 **capture 相** `mouseup/touchend→captureSel`(空选区→hideSel)会清掉手动 toolbar → 真拖选(moved)时 `_favEnd` 后 `stopPropagation` 拦住(**零改 captureSel**)。单击查词仍走既有 content 单击(`caretRangeFromPoint`,none 下照常命中)。

## AI 认收藏集(v8 meta + `epub_assistant.py`)

收藏夹自 v5 起物化成 EPUB → AI 走 **`epub_assistant.py`**(不是 assistant.py)。普通 EPUB 书/PDF 阅读器 AI 一律不变(全以 `_fav_fid()==None` gate,fav 内容全在**动态**块不入静态缓存)。

- **数据源 meta**:`_fav_write_epub` 每条目建 `items[]`(section idx 对齐 spine)→ 写 `state/reader-fav-meta/<fid>.json`:`{section, kind, src_file, src_name, src_page|src_section|id, label, snippet(内容首句 ≤80字), adj_prev, missing}`;夹删/build 同步清 meta。
- **① 识别**(`_fav_fid`):`ctx.file_rel` 匹配 `^资源/收藏夹/(f_…)\.epub$` → fid。
- **② system prompt + ③ 目录概览**(`_fav_sys_block`,拼在 `_esys_prompt` 的【当前章节】之后=动态块):注入「这是**收藏集**、条目不一定连续、各标原书出处、要原书上下文用 read_source_page」声明 + **【收藏集目录】**(每行 `[section idx]《原书》第N页/节 + 首句`,标 `↳接上条` / `←当前` / `(原书缺失)`,cap 60 条)。收藏集时跳过原 `toc_line`。
- **④ 相邻性截断**(`_t_read_section`):只有下一条 `adj_prev==true`(同书连续)才带下一章预览,否则不带(它是另一本无关书的页,拼进去=污染)。`adj_prev` 严格=同 file 且 `page/section==prev+1`(逆序/重复/跳页/跨 PDF↔EPUB 一律 false,保守定义)。
- **⑤ `read_source_page` 工具**(**收藏集专用**):`item=条目 section idx`(经 meta 解析原书 src_file+页/节)或 `book+page/section`,`offset` 读前后。PDF 源走 `assistant.py::_page_text`(吃任意 vault rel);EPUB 源走本模块读原书某节。**工具集 gate**:不进 `_etools`(否则普通书静态工具目录也列它),放 `_efav_tools` 由 `_tool_fn(name,ctx)` **仅收藏集时**并入执行注册表 → 普通书零影响。
- **NotebookLM 3 按钮**(`epub-html.js` ui 块 `_favNotebookEntries`,**仅 `IS_FAV_BOOK`**,纯前端后端零改):助手快捷区 `#ep-asst-quick` 置顶注入 📋 总结整本 / 🔗 串联要点 / 💡 找共同点(紫调,幂等),走既有 `data-send` → `sendChat()`。措辞显式点名【收藏集目录】+ read_source_page → AI 拿全貌 + 按需翻原书查证。

## 服务器自动后台 build + 秒开兜底

- **真源=item,EPUB=派生缓存**:folder 加 `content_sig`(=`sha1(json(items))`;userpage 条目**额外折入该记录 `[md_ver,updated,title,md]`** → 收藏后又编辑了插入页,sig 变,下次开重建;非 userpage 折算前后字节一致=存量零影响)+ `built_sig` + `built_ver`。**脏 = `content_sig!=built_sig` 或 `built_ver!=_FAV_BUILD_VER(=8)` 或 .epub 不存在**。
- **CRUD 变条目 → 服务器自动后台 build**:`_fav_trigger_build(fid)` daemon 线程 fire-and-forget 起 `_fav_build_job`,不阻塞 CRUD、不等 job(改名不触发)。**去重**:`_FAV_BUILD_ACTIVE[fid]` 复用其 jid(`_INSPAGE_MUTEX` 原子判定);**合并 last-write-wins**:job 收尾 re-check dirty,期间又改了再起一次直到追平(`built_ok` 仅成功且未删置真,失败/删夹不重试防死循环)。
- **`/fav/open?id=`**:`_fav_trigger_build` → 不脏(常态=后台已 build 完)秒开 302 到 EPUB 阅读器;仍脏 → 返回 `_FAV_WAIT_HTML` 等待页轮询 job-status,done reload。`/fav/view`(退役)→ 302 到 `/fav/open`。
- **版本迁移**:`_FAV_BUILD_VER` bump → 存量夹 `built_ver` 不匹配=脏 → 下次打开懒重建(不做部署时全量重建=避免 thundering herd)。
- **删夹并发安全**:build 期间夹被删 → job step-3 `_fav_folder` 返 None → unlink 掉刚 `os.replace` 出来的孤儿派生 EPUB + 解包目录(DELETE 自身也 unlink,双保险)。

## 零进度 / 静态由 nginx 服务 / 系统边界特判

- **零进度**(收藏夹书排除出进度系统):`_fav_serve_reader` 传 `server_pos=None`、不调 `_lastopen_touch`(不进「最近打开」);`reading-pos` 对 `资源/收藏夹/` 前缀提前拒收(服务端更稳)。`fav-drafts` 草稿 key 已随精简页退役。
- **静态由 nginx 服务**(测试/部署 gotcha):部署版 static JS 在 `/var/www/html/static`,**只有 nginx/443 服务它**;Flask `:5000` 是陈旧 static。真机/Playwright 验证收藏夹务必走 nginx 而非 `:5000`。
- **系统边界**:产物在 `state/reader-fav-epub`(**非 vault** → 无 Obsidian Sync churn、天然不进书架/搜索)。`_FAV_BOOK_PREFIX="资源/收藏夹/"` 现是**合成 rel 键**,仍作为锚用于:`_resolve_epub_book` 解析、零进度、禁自我收藏、以及**防御性**排除(`_list_vault_pdfs`:108 + `build_search_index.py::_list_pdfs` 仍 `startswith` 跳过,防 v3 残留的 vault PDF 混进书架/双份命中)。register/KG/制卡(只扫 `[0-9A-Fa-f]{3}-*.md`)、push_big_files、backup 均顺其自然。

## 退役清单

- **阶段A 精简查看页**:`/fav/view` 路由 → 302;`fav_reader.html` / `fav-reader.js` / `_fav_js_v` 留盘不再链接(注释标退役)。文件仍在 `static/pdf/fav-reader.js`。
- **v3 固定页 PDF** `资源/收藏夹/<fid>.pdf`、**v4 流式 HTML** `state/reader-fav-html/<fid>.html`:build 成功 `unlink`(别删原书),删夹双清。`_FAV_HTML_DIR` 仅用于清理残留。
- `rc-favorites.js` 的 ⭐picker/bindStar **保留不动**(收藏入口不变,写的还是 `favorites.json`)。

## 风险与妥协(如实)

- **PDF 条目可视高亮不支持**:透明 `<mark>` 不可视;文本操作/查词/翻译/offset 高亮(回原书也在)全可用,**可视高亮请点「打开原书」画**;EPUB 条目高亮天然可视。
- **收藏夹 EPUB 自身标注对账**:用户能在收藏夹书里画高亮/手写(sidecar keyed by 合成 rel sha + section idx)。加/删/**重排**条目触发重建=**任意排列**(不是单点 +1/-1),标注按老 idx **best-effort**(可能错位)。缓解:多数人在原书标注,收藏夹是复习/串读面。根治需 item-diff → 排列 mv → 驱动 `PAGE_ANCHOR_MIGRATIONS`(registry 的 `mv:page->new_or_None` 能表达任意排列),**未做**。
- **标注不回写原书**:v5 物化后收藏夹书是独立真书,标注写它自己的 sidecar,不自动回写原书(「当成真书」的自然代价;阶段A 精简页曾承诺回写原书,已随退役作废)。
- **EPUB 长节强制排一页 / 公式 `$..$` 阶段1**;**原书改名/删除**后收藏夹里那页/节仍是**已打包静态副本**(照样能开能选词),重建时该 item 渲占位页(`missing`),「打开原书」链失效前端 toast。
- **MathJax 破例**:普通书 section 阅读器不 typeset,收藏 EPUB 的「我的页」含 `$..$` → `epub-html.js _fetchSection` 加**收藏夹前缀 + 仅 `.fav-item-userpage` 元素** typeset(v5「epub-html.js 零改」的唯一破例,scoped 到收藏夹书,零普通书影响)。
- 多设备同编=last-write-wins 静默丢;大收藏夹一次性建 EPUB 几秒(后台 job 不阻塞);PDF 旋转页 overlay 坐标可能微偏(罕见);userpage 手写墨迹本批不物化(canvas 数据嵌静态 EPUB 困难,「打开原书」看)。

## 演进史(已废,勿据此实现)

收藏夹「打开方式」经历:**阶段A 精简查看页**(`/fav/view` 手搭只读页,PDF 条目无字符层不能选词)→ **v3 物化成固定页 PDF**(`资源/收藏夹/<fid>.pdf`,`insert_pdf`/`insert_htmlbox` 每 item 一页;痛点=EPUB 章硬塞一页文字/图被压小、截断)→ **v4 流式 HTML**(`state/reader-fav-html`,html-reader.js 不定高度不压缩;痛点=html-reader 功能少,无手写/agentic 助手/图徽标/grammar/vocab)→ **v5 物化成真 EPUB + 完整 EPUB 阅读器**(现役,全功能)。PDF 条目选择:**v6 行盒分组**(缓解 iOS 乱跨行)→ **v7 char-layer 式自定义选择**(根治)。演进主线=用户反复要「更全功能 + 不压缩文字」,最终落到「物化真 EPUB,epub-html.js 零改」。

---

## 附录:Gemini 免费额度恢复自动切回(小功能,已实现)

免费 429 的临时冷却(`_gemini_off`)到期本就自动回免费;用户点过 `@paid` 的 action 预设是永久直连。设计:调用路径上**节流探测**(30min 至多一次)——`assistant.py::_paid_probe_due`(ts 持久化 `state/gemini-paid-probe.json`)+ `_paid_recover_check(uid, action)`:free key **1-token generateContent、3s 超时同步**(不用 countTokens——它是独立配额桶,generate 免费耗尽时照样 200,必然误判)。成功 → `_ap_set` 原地存回裸 variant(摘 `@paid`)+ 记 `ok_at` 短窗(5min,其它 @paid action 免请求一起切回)+ 本次回复附一次 `gemini-paid {recovered:true}` 事件;失败 → 429/403 顺手 `_gemini_cooldown("free")` 静默继续付费。接线:`_agent_run`/`_eagent_run`(有 SSE 通道 → 绿色提示条「✅ 免费额度已恢复」)+ reader_ask/stream/vision + 4 处 summarize 叶子(无 SSE,静默切回)。前端 rc-assistant.js `paidNotice` 认 `data.recovered`。

## 「同一张纸」终局(2026-07-05/06)——收藏夹⇄原书全双向,唯一代码

**模型**:插入页/收藏页=中间层里带 uid 的共享对象,PDF/EPUB/收藏夹都是**视图**;谁编辑都写同一份数据,SSE 让所有打开的视图实时收敛。

- **正文+编辑器唯一代码**:`rc-userpages.js` 加 `_fileOf(p)=(p.__file)||O.file`(per-page 文件路由:存/删/改高/beacon 全走它)+ `mountOne(container,{file,id})`(把单页挂成真 `.ep-usec`,复用 buildInstant 全套:Aa 即时编辑/下边缘改高/keepRatio/自动保存)。收藏夹 `_favUpMount` 对 `.fav-item-userpage[data-uid][data-ink-file]` 调 mountOne;`load()` 保留 `__mounted` 页;`mountAll` 跳过它们。
- **墨迹**:EPUB 自建页键=uid(fileOf[uid]=原书);**收藏的 PDF 页键=`pdf|原书|页号`**,存取自动路由 `/api/ink`(画/存/beacon/清空/撤销全链)。收藏夹打开自动拉原书墨迹(`_favUpInkLoad`/`_favPdfInkLoad`,dirty 不覆盖);**favPdfLoaded 起笔门槛**(整页替换语义下,原书墨迹没落地前禁画,防空底覆盖)。工具栏目标 `_favUpElIn` 认内层 `.ep-usec`/`.fav-pdf-page`。
- **删除闭环**:统一 🗑(收藏夹删=删原书那页,确认文案明示)→ 后端 `_fav_cascade_userpage_delete` 级联清各收藏夹条目 + `userpage-del` SSE(所有视图当场移除元素)+ `_fav_prune_dead_userpages`(open/预建时清存量墓碑;sidecar 存在且查无 id 才删)。
- **结构真·增量**:fav CRUD→`fav-changed`;重建完→`fav-built`;打开着的收藏夹 `_favReconcile` 拉 `/api/fav-meta` 按条目 key diff:保留节复用 DOM 只改 idx、移除节消失、新增节建占位懒加载(滚动/墨迹 canvas 不动)。
- **来源条**(v13):`《书名》·页/节 | 打开原书↗ | ☆取消收藏`(`_fav_sep_html3` 带 data-fitem→PATCH remove_item,乐观隐藏+reconcile 收尾);间距紧凑(sep 8/5、item 2)。
- **收藏夹工程**:后台预建(`_fav_prebuild_loop` 45s 后+每 15min,串行防并发压 Pi;线程起失败回滚占位);独立 PWA(`/fav/manifest`+`/fav/icon` 金星,start_url 固定本夹,零状态不进「最近打开」)。
- **实时同步**:内容 ~1s(SSE)、结构=重建耗时(几秒);正在画/编辑的页永不被打断/覆盖(drawing/dirty 守卫,响应落地复查)。
