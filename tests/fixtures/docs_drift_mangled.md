# 回归夹具：2026-08-19 那 8 行被"只替换前缀"弄坏的真实原文

> 收紧误报判据之后，这 8 行必须**仍然全部被抓到**。
> 抓不到就说明收紧过头了 —— 检查器变干净的代价是它不再干活。

- ⚠ **前端 `_server_deploy/static/pdf/*`**（`reader.js`+`reader.src/*.js` 源、`rc-*.js` 共享控制层、`pdf-adapter.js`/`epub-html.js`/`epub2*.js` 各 adapter、`rc-userpages.js`/`rc-favorites.js`/`rc-stickynote.js` 等）⚠ **前端 `_server_deploy/static/pdf/*`**（`reader.js`+`reader.src/*.js` 源、`rc-*.js` 共享控制层、`pdf-adapter.js`/`epub-html.js` 各 adapter、`rc-userpages.js`/`rc-favorites.js`/`rc-stickynote.js`、`native-local-runtime.js` 等）有**两条互不相干的分发路径**：① 桌面/扩展表面由 nginx(443) 服务。

不依赖客户端 EXE 或 Obsidian。不依赖客户端 EXE 或 Obsidian。**数据权威在设备**：App 内高亮/便签/插入页/墨迹/阅读位置由 `native-local-runtime.js` 直接落本机存储，Pi 只在设备间同步中继时经手。草稿 → 浏览器 localStorage（per-device）。；草稿 → 浏览器 localStorage（per-device）。

改完 `cp` 三件套（py + html + reader.js）+ `systemctl restart webapp`。**⚠ 改 JS 逻辑改的是 `reader.src/NN-*.js`,不是 html**；流程：`改 reader.src/ → bash scripts/check_pdf_reader_js.sh → cp reader.js`（cache-bust 自动）。改 JS 逻辑改的是 `reader.src/NN-*.js`，然后 `bash scripts/check_pdf_reader_js.sh`（自动重建 reader.js + 校验）。**之后有三个互不相通的投递表面**：① Pi/旧网页表面走 `bash scripts/deploy_reader.sh`。

- - **需迁 19 项**(Pi 侧 `PAGE_ANCHOR_MIGRATIONS`，2026-08 复核)：pdf-highlights / reader-notes / pdf-ink / reader-favorites / reader-userpages / pdf-tr-sentences。⚠ **注册表有两份**：本机书的插/删页在 App 内本地跑，对应 `PDF_MUTATION_PAGE_ANCHOR_DOMAINS`。**新增任何按页存储必须两份都登记**，只改 Pi 那份对 App 无效。:pdf-highlights / reader-notes(便签 anchor.page,`u_*` 字符串跳过)/ pdf-ink / reader-favorites(本书 items)/ reader-userpages(真页 page + 旧 after 边界)/ pdf-tr-sentences。

- **静态由 nginx 服务**(测试/部署 gotcha):部署版 static JS 在 `/var/www/html/static`,**只有 nginx/443 服务它**;Flask `:5000` 是陈旧 static。- **静态由 nginx 服务**(仅限旧网页表面)：部署版 static JS 在 `/var/www/html/static`，只有 nginx/443 服务它；Flask `:5000` 是陈旧 static。⚠ **nginx 那份到不了 App 和扩展**——App 加载打进包的 ReaderBundle。

实现:核心四函数在 `assistant.py`(`_t_notes_query/_t_notes_read/_t_notes_create/_t_notes_edit`),PDF 助手 TOOLS 直接注册,EPUB 助手 `_etools` 复用。file 一律从会话 ctx 取(`ctx.file_rel`,同 see_figure),AI 不传 file。…file 一律从会话 ctx 取(`ctx.file_rel`,同 see_figure),AI 不传 file。数据层走 `_vb_notes(file_rel, ctx)`：**原生书读的是 App 随请求送上的 `native_local_state` 权威快照**。

后端 `/api/page-overlay` 返回 `vocab_marks: [{word, lemma, mastery, label_slug, rects:[[x0,y0,x1,y1],...]}]`（`_build_vocab_marks`，用 PDF pt 坐标 rect 列表，不依赖 char idx —— 跟前端 chars sort 与否无关）；`/api/page-chars` 自 2026-06-07 拆分后**不再内嵌** vocab_marks。⚠ 归属不同：page-chars 在 App 内是本地实现（改服务端无效）。...]}]`（`_build_vocab_marks`，用 PDF pt 坐标 rect 列表，不依赖 char idx —— 跟前端 chars sort 与否无关）。另有轻量路由 `/api/page-vocab-marks`：

| **阅读器共享功能**（PDF+EPUB） | 同上两个阅读器 | 经 `window.RC` 统一控制层（`references/unified-control-layer.md`）共享：**插入页/用户页**（`/api/userpages`+`/api/pdf-insert-page`）、**收藏夹**（`/api/favorites`→`state/reader-favorites.json`）、| **阅读器共享功能**（PDF+EPUB） | 同上两个阅读器 | 经 `window.RC` 统一控制层（`references/unified-control-layer.md`）共享：**插入页/用户页**（`/pdf/api/userpages`+`/pdf/api/pdf-insert-page`）、**收藏夹**（`/pdf/api/favorites`，owner=pi）、**便签**（`rc-stickynote.js`，`/pdf/api/notes`）。|

## 对照组：以下都**不该被报**（正常文档里天然的重复）

| 环境 | 判定 | 项目根 | Vault | 服务管理 |
|---|---|---|---|---|
| **Pi** | Linux + 存在 `/home/bwicarus/claude` | `/home/bwicarus/claude` | `/home/bwicarus/obsidian` | systemd |
| **VPS** | Linux + 存在 `/root/claude` | `/root/claude` | `/root/obsidian` | systemd |

完整迁移见 [`references/epub-on-epubjs-migration.md`](references/epub-on-epubjs-migration.md) 与 [`references/unified-control-layer.md`](references/unified-control-layer.md)。

| 路由 | 用途 | 归属 |
|---|---|---|
| `/pdf/api/highlights` | 高亮 CRUD | local |
| `/pdf/api/notes` | 便签 CRUD | local |

### 真实误报（2026-08-19 第二轮审计跑出来的 4 条，全部经人工确认是正常写法：
同一路径在一句里出现两次、两个共享后缀的函数名。判据调整后这些**必须不被报**）

- ⚠ **前端 `_server_deploy/static/pdf/*`**（`reader.js`+`reader.src/*.js` 源、`rc-*.js` 共享控制层、`pdf-adapter.js`/`epub-html.js` 各 adapter、`rc-userpages.js`/`rc-favorites.js`/`rc-stickynote.js`、`native-local-runtime.js` 等）有**三条互不相干的分发路径**：① 桌面/旧网页表面由 nginx(443) 从 `/var/www/html/static/pdf/` 服务（`immutable` 缓存），`127.0.0.1:5000` 的 Flask `/static/` 是**陈旧副本**，改前端要到桌面表面必须 `bash scripts/deploy_reader.sh` 部署到 nginx 静态目录，直连 :5000 看到的是旧的；② **App 不走 nginx** —— 同一批文件被 `ios/BWReader/package_local_reader.py` 烤进 ReaderBundle 随包发，要到 iPad 只能出 TestFlight 新构建；③ **浏览器扩展也不走 nginx** —— 它按 `manifest.json` 加载自带副本 `extensions/bw-reader-webext/vendor/rc-*.js`（由 `extensions/bw-reader-webext/build.py` 从这批源文件生成），要到扩展必须重跑 build.py 再打包，部署 nginx 对扩展无效。

- `scripts/daily_anki_status.py::main` 现在跑 **22 步**（2026-08-19 核实；开头另有总开关 `server-config` 的 `daily.enabled`，false 则 timer 照常触发但脚本 `write_run("skipped")` 空跑退出）。下面这份编号列表是 2026-05 的旧子集，**权威流程直接读 `scripts/daily_anki_status.py::main`**——它比下表多出：通用语停用词 / 停用词复活赛（受 `stopword_gov.enabled`、`ai_judge` 控制）、领域词典 / 融合权重学习 / 跨语言概念归一（`attention_profile.py --domain-dict|--fit|--concepts`）、学习近况（`learning_situations.py --daily`）、错误模式元画像（`error_meta_profile.py --gen`）；概念网三步已拆到独立的 `concept-graph.timer`（02:30）：

- **更新到笔记**：POST `/api/card-update` `{target:'note', verbosity:'verbose'|'concise'}` → 后台 `_prepare_legacy_card_draft()`（qa_browser.py:1122）**只出草稿、一个字都不写盘** → 前端渲染「草稿预览（尚未写入）」→ 用户确认后 POST `/api/card-update-commit` → `_commit_legacy_card_draft()`（qa_browser.py:1343）才真正改写源笔记

该文件在 `scripts/reader_deploy_manifest.py` 清单内(dest `webapp/voice_realtime_relay.py`),**必须走 `scripts/deploy_reader.sh`**——脚本自带原子安装、`assert_voice_runtime_stable` 稳定性检查,并在装完断言 `systemctl cat voice-rt` 的 ExecStart 指向 `/home/bwicarus/webapp/voice_realtime_relay.py`(deploy_reader.sh:1493);不要手工 `cp` + `sudo systemctl restart voice-rt`。⚠ 重启会掐断进行中的通话,发布前仍需自行确认当前无人在通话中(原文引用的 `[[restart-voice-rt-check-active-call]]` 程序化 gate 全仓查无此物,是悬空链接,别指望它挡)。

