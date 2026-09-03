# 便签(Sticky Notes)系统设计 — PDF/EPUB 阅读器共享功能

> 2026-07-02 用户提出完整规格。本文档 = 规格固化 + 架构映射,实施前必读。
> 遵守 `references/unified-control-layer.md` 顶部设计铁律:共享层(rc-stickynote.js)只管策略/状态机,
> 锚定/挂载机制 per-reader 经 adapter hook;锚在内容坐标系(严禁 fixed+JS 跟滚)。

## ⚠️ 规格 v4(2026-07-02 第三轮:EPUB 便签改「内容锚」,根治侧边栏开关重排漂移)

**问题**:EPUB 锚是 {section, x, y 归一化比例}。侧边栏开关 → 正文被挤窄/放宽 → reflow 重排 → 同一比例点对应的**文字**变了 → 便签相对它标注的内容漂移。用户建议:绑定到最近的文字或图像元素上——正确,即高亮系统的锚定思路(高亮锚字符偏移,重排零漂移)。

**设计**:
1. EPUB anchor 升级:`{kind:'epub', section, off(最近文字的可数字符偏移,同高亮 offsetOf 坐标系), dx, dy(便签相对该字符 rect 的像素偏移)}`。创建/拖动松手时经 caretFromPoint→offsetOf 取锚;渲染时 `_domPosAtOffset(sec,off)`→Range rect→section 内像素定位。重排后字符位置变,便签跟着字走。
2. **重定位时机**:sidedrawer `onLayoutChange`(六连修已有钩子)/字号·行距·栏宽设置变更/window resize → `repositionAll()`。
3. **铁律修正**:组件现在自己设 left/top=x%/y%,这是"定位机制泄漏进共享层"——改为 host 的 `mount(anchor)` 返回 `{el, left, top}`(像素,host 算),组件只应用。PDF host 返回 x·w/y·h(等价现状,定宽页面比例锚本就稳,**PDF 锚不改**);EPUB host 返回内容锚换算像素。
4. **旧数据懒迁移**:mount 时发现 anchor 无 off → 按当前 x/y 找最近文字偏移升级并 PATCH。纯图/无文字 section → 保留 x/y 比例兜底。
5. 纯图元素附近:统一锚"最近可数文字";图旁通常有文字,不单独做 img 锚(降复杂度,兜底覆盖)。

### ✅ v4 实现状态(2026-07-02 实现,node --check + reader.js 重建校验过,待 iPad 复测)

改动文件:`rc-stickynote.js`(契约)+ `epub-html.js`(EPUB host)+ `reader.src/27-rc-adapter.js`(PDF host,reader.js 已重建)。后端零改动(`pdf_reader.py` PATCH anchor 只校验 `kind in (pdf,epub)`,off/dx/dy 不透明透传,已核实)。

- **契约(铁律修正)**:`O.mount(anchor)` 返回 `{el, left, top, anchor?}`——left/top 为容器内**像素**(host 算,组件只应用);`anchor` 字段 = host 懒迁移升级后的新锚,组件 `patchNote` 落库。旧契约 `{el,w,h}` 兼容(组件退回自算 x/y 百分比)。`ensureMounted` 兼管容器失效卸载(mount 返 null 且已挂 → remove 待补挂),`mountAll` 化简为逐条 ensureMounted(幂等),`repositionAll` = `mountPending` = mountAll 同一实现。
- **EPUB host**(epub-html.js 便签 init 区):
  - `_noteCharRect(sec,off)` off→字符 viewport rect(`_domPosAtOffset`+Range,collapsed 兜底 getClientRects);`_noteOffPos` → section 内像素 + dx/dy;
  - `anchorFromPoint(x,y)` 升级:caretFromPoint 在落点小范围候选点(8 个,±40px 内)找最近**可数**文字 → `{kind:'epub',section,off,dx,dy,x,y}`(x/y 保留:纯图兜底 + `_noteNearText`);落不到文字 → 旧比例锚;
  - **懒迁移** `_noteUpgradeAnchor`:纯几何搜(节点整体 rects 找最近节点 → 节内粗采样 ≤32 步 + 邻域细化),**不依赖 elementFromPoint,离屏 section 也能迁**;失败(无文字)→ null 保留 x/y;
  - `_countable` 新增排除 `.rc-note` 子树(便签 textarea/按钮文字挂在 .ep-sec 内,不能进偏移空间/不能锚到自己;便签追加节尾,不影响既有高亮偏移);
  - **重定位时机**:sidedrawer `onLayoutChange`(包装 `_keepReadPos` restore + repositionAll)/ `applyStyle` 末尾(字号/行距/栏宽/主题)/ window resize 防抖 250ms(sidedrawer 开关派发的合成 resize 也覆盖)。
- **PDF host**:`noteMount` 返回 `x·clientWidth / y·clientHeight` 像素(锚不改,行为等价旧 %);页重渲/缩放后 04-render 两处 `mountPending` 用新 clientWidth 重算(px 不再自适应,靠既有重挂时机覆盖)。
- **松手取锚统一**:`reanchorAt(ctl,px,py)`(pointer-events:none 穿透自己 → `O.anchorFromPoint`)。handle 拖拽照旧;**左上缩放**改走 reanchorAt(目标点=清 transform 后基准 rect + 最终 shift,防 rAF 取消致 transform 滞后一帧),host 解析失败兜底同容器位移补偿:内容锚 `dx/dy += shift`(像素精确等价),比例锚 x/y 换算。
- **妥协**:①纯图/无文字 section 仍 x/y 比例锚(重排在图周围漂移风险低);②PDF px 依赖 mountPending 时机(未加 PDF 侧 resize 钩子,超出「PDF 锚不改」范围);③迁移几何搜按「节点 rect 最近 → 节内采样」两级近似,极端多列/绝对定位排版可能选到视觉非最近字符,dx/dy 补偿保证**位置**仍精确,只影响跟随哪个字。

## ⚠️ 规格 v3(2026-07-02 第二轮实测反馈,覆盖 v2 中冲突的条目)

1. **统一编辑模式**:不再区分"handle 长按=移动模式 / body 长按=样式模式"——**便签任意部分长按**进入同一个编辑模式,内含:
   - **左上角 + 右下角**两个缩放手柄(左上角拖动=位置+尺寸同变,锚点在右下角;右下角=原有行为);
   - handle 旁 **🗑 删除按钮**;
   - 下方 **颜色板**;
   - handle 拖拽 = 移动便签(编辑模式内可移动,浮起效果照旧)。
   点便签外退出(退出时保存文字/笔画待存项)。
2. **长按时长可调**:默认缩短(500ms → 350ms),设置「便签」tab 加滑块(200–800ms),localStorage 键 `rc-note-longpress`(共享组件设备级键,同 opacity/autocontrast 惯例)。
3. 不变:单击 handle=折叠/展开、单击 body=文字输入、pen=常态直写+跨界切割、双击=AI hook、手指双击=临时橡皮。

### ✅ v3 实现状态(2026-07-02 实现,Chromium 无头 44 项验收全过,待 iPad 复测)

改动文件:`rc-stickynote.js`(状态机合并)+ `rc-settings.js`(便签 tab 加长按滑块)。两 reader 底座/后端零改动。

- **状态机**:`MOVE`/`STYLE` 两个模式变量删除,合并为唯一 `EDIT`;`enterMove/exitMove/enterStyle/exitStyle` 四函数合并为 `enterEdit/exitEdit`(进入=blur 收键盘+textarea inert+展开折叠便签,handle/body 入口共用;退出=保存文字+flush 笔画)。CSS `.rc-note-moving`/`.rc-note-styling` 合并为 `.rc-note-editing`(🗑/色板/双缩放手柄/textarea inert 全挂它);浮起效果拆成独立 `.rc-note-lift`,只在 handle 拖拽进行时(startDrag 加、松手/取消/退出撤),EDIT 静止不浮。EDIT 内 handle 按下即拖(原 MOVE 语义)、单击 handle 不折叠。
- **左上缩放手柄** `.rc-note-rs-tl`(视觉同右下角标,cursor:nwse-resize):拖动 = `w=clamp(w0-dx,120,720)`、`h=clamp(h0-dy,80,720)`,位移补偿 `shift=(w0-w,h0-h)` 拖动期间走 transform 暂态(锚定铁律既有例外),松手 `anchor += shift/容器rect` (clamp 0..1)与 w/h **一并 PATCH**——右下角钉死,碰到 min 后 shift 停增。同容器内补偿,不经 anchorFromPoint(左上缩放不跨页)。
- **长按可调**:`LP_MS=500` 常量删,改 `lpMs()` 每次长按现读 localStorage `rc-note-longpress`(钳 200–800,缺省 **350**);rc-settings「便签」tab 滑块(200–800 步进 50,拖动只更新数值显示),回填 `_fillNotePane`/保存 `_saveNotePane` 跟 opacity 同一套 host 无关两段式,保存后下次长按自然生效(无需 refreshStyle)。
- 顺手修:v2 `removeAll` teardown 漏清 resize 进行中手势(document 监听泄漏),现 `_rz`/`_rzTL` 都收尾。
- 验收:body/handle 长按同入 EDIT 四件套齐、EDIT 内换色/移动/缩放连续操作不退出、左上手柄右下角钉死+anchor 补偿精确(0.2→0.25/0.23)+min 约束、拖拽时 lift 松手撤、点外退出、350/200/800 三档时长、单击输入不回归、滑块回填/联动/取消丢弃/保存落盘。`backdrop-filter` 磨砂与 Apple Pencil 路径仍需 iPad 真机。

## ⚠️ 规格 v2(2026-07-02 用户实测后修正,覆盖下方 v1 中冲突的条目;与 v3 冲突处以 v3 为准)

1. **文字输入 = 常态单击**:单击便签 body → 直接进入文字输入(聚焦,可打字)。不再需要长按才能输入。
2. **长按 body = 仅样式调整**:只出现 颜色板 + resize 手柄(大小/颜色调整模式),不再承担"启用输入/手写"的职责。
3. **手写 = 常态笔触**:笔(pen)只要落在便签 body 上方就**直接写入便签**——任何时候,不需要任何模式。
4. **跨界笔画三段切割**(分层语义:笔到哪层写哪层,按笔尖实时位置路由):一条线从便签外起笔、横跨便签、出便签 → 书页上留左、右两段笔迹,便签上留中间一段。反方向(便签内起笔出界)同理。便签是覆盖在页面上的独立书写层。
5. **实测 bug**:①输入时文字不可见(隐形);②(v1 实现)手写穿透便签写到下方页面——由规格3的常态拦截根治。

### ✅ v2 实现状态(2026-07-02 实现,待 iPad 复测)

规格 v2 五条已全部落地。改动文件:`rc-stickynote.js`(交互状态机重构)、`epub-html.js` `_epInk` 区(便签 gate+跨界路由)、`pdf_reader.html` 内联 `_ink*` 块(同上,PDF 的页面 ink 在模板内联,**不在 reader.src**)。

- **bug① 根因**:PDF 便签挂载容器 `.page-wrap` 带 `-webkit-user-select:none`,iOS WebKit 对 none 子树内的 textarea **编辑期间不渲染字形/光标**(失焦才出现;bugs.webkit.org #82692 族)。v1 只在 `.rc-note-text` 上设 `user-select:text` 不够——修法:`.rc-note-body` 整个子树显式 `-webkit-user-select:text` 切断继承(handle 保持 none 供拖拽)。
- **bug② 根因**:两个 reader 的页面 ink 委托监听都在**祖先层 capture**(EPUB=`#ep-col`、PDF=每个 page-wrap),pen 分支 `stopPropagation` → 便签 body 自己的 capture 监听根本收不到事件,v1 的组件内拦截形同虚设。修法:**gate 加在页面 ink 侧**(`_inkPointerDown` 开头),这也是跨界路由的天然入口。
- **手势表 v2**:body 单击=聚焦输入(textarea 常态可点,无 readonly;失焦 PATCH)/ body 长按 500ms=样式模式(色板+resize;进入时 blur 收键盘,模式内 textarea 暂 inert)/ body 手指双击=临时橡皮(仅当有笔画或已是橡皮;第一击照常聚焦,第二击才吃掉且不进 AI 双击计数)/ pen 落 body=直写便签(常态)/ handle 单击=折叠、长按=移动+🗑、双击=AI hook(不受橡皮双击影响)。
- **跨界路由架构**:页面 ink 是整条笔手势的唯一主人;rc-stickynote 暴露 `penRoute(x,y)->id|null / penBegin(e,{eraser}) / penMove(e) / penEnd({boundary})`。down/move 时按笔尖实时位置切段:页面段 end(`_inkEndPageSeg`,擦边单点段丢弃)→ 便签段 begin → 出界便签段 end → 页面新段 begin(`_inkBeginPageSegAt`,elementFromPoint 重找页/章锚,支持跨页跨章;落不到页上=悬空 dangling 后续再试)。**两个方向都已实现**,便签→便签(相邻/重叠)由 `penMove` 内部自切段。组件内另有无页面 ink 底座时的后备自管手势(出 body 即截断,不画出便签)。
- **已知妥协**:形状工具(line/arrow/rect)不参与切割(几何无法切段,起笔在页面则整条留页面,可能视觉上穿便签下方);从便签内起笔且页面当前工具是形状时,出界的页面段降级为 pen;长按 body 进样式模式故意不监听 pointercancel(iOS 在文本上长按会启动选词接管并发 cancel,监听则文本区永远进不了样式模式),极端情况(便签上双指捏合)可能误进样式模式;手指双击橡皮优先于 AI 双击(仅 body 上、仅有笔画时;handle 双击仍走 AI)。

### ✅ 外观 v3(2026-07-02 第二轮:磨砂玻璃 + 自动对比色 + 设置 tab,已实现待 iPad 复测)

用户增补规格:便签半透明磨砂、透明度进设置、改色时 body 要真变色、文字/笔自动对比色(带开关)。

- **磨砂玻璃**:body 背景 = `rgba(便签色→rgb, α)` 内联(applyColor)+ `backdrop-filter:blur(Npx)`(`-webkit-` 前缀 iOS 必须);**模糊强度 N 现是运行时可调**(`noteBlur()` 读 localStorage `rc-note-blur`,钳 0–24 缺省 10,`applyColor` 内联设 body 的 `backdropFilter`/`webkitBackdropFilter`——不再是写死的 CSS `blur(10px)`);文字/笔迹/工具条不透明清晰。handle 是操作件保持醒目:α 取 `max(0.85, 用户α)`。
- **设置面板「便签」tab**:rc-settings 第 5 个 tab(PDF/EPUB 都显示,不 gate):**透明度滑块 30–100%**(默认 72%)+ **磨砂强度滑块 0–24px**(step 2,默认 10,控件 `rcset-note-blur`)+「自动对比色」checkbox + **长按时长滑块 200–800ms**(默认 350,见 v3)。拖动只更新旁边数值显示;遵循面板「取消/保存」两段式;**保存/回填都是 host 无关**路径(PDF host 的 onFill=原生 `_fillSettings`、onSave=原生 `saveSettings` 都不认识这些控件——回填挂在 `open()` 照 `_renderAiInline` 先例,保存挂在 `rcset-save` handler 顶部先于 host 分发)。落盘后调 `RC.stickynote.refreshStyle()`(对每个已挂载 ctl 重跑 applyColor,即时生效)。
- **存储**:localStorage 设备级 `rc-note-opacity`(0.3–1)/**`rc-note-blur`(0–24,默认 10)**/`rc-note-autocontrast`('1' 默认开)/`rc-note-longpress`(200–800,默认 350);共享组件自己的键,不挂 pdf-/eph- 前缀,rc-stickynote 读取端给默认值。
- **笔画防丢**:`scheduleStrokeSave`(800ms 防抖)+ `flushAllStrokes()`(`pagehide` / `visibilitychange:hidden` 用 `keepalive` PATCH,不用 sendBeacon)——切后台/关页不丢防抖窗口内未落盘的笔画。
- **自动对比色**:纯函数 hex→W3C relative luminance(阈值 0.55,按便签**本色**算,α 不参与)→ 亮底=深前景 `#1b1b1b` / 暗底=浅前景 `#f2f5f9`,经 `.rc-note-darkbg` class 联动 文字/placeholder/光标/handle 横杠/resize 角标/工具条底。**新笔画**默认色=同前景;**已有笔画是用户数据不改色**(文字颜色是渲染属性可即时切)。开关关=固定现状(深字+INK 默认红)。色板扩 2 个深色(石墨 `#2d3440`/墨绿 `#1f3a2e`,工具条加 flex-wrap 防窄便签溢出);applyColor 不依赖色板,旧便签颜色不在色板照常渲染(hexRgb 解析失败→原样不透明兜底)。
- **改色 body 不变 bug 复盘**:本轮排查时工作区源码的 applyColor 已同时设 handle+body(Chromium 实测变色成功、部署副本与源码一致)——用户实测命中的是当天更早的迭代构建(文件多次编辑,18:30 有过一次编辑+部署),叠加旧色板全是极浅色视觉差微小。本轮把 body 背景收口到 rgba 磨砂单一路径 + 深色系色板,变色肉眼可辨,彻底根治。
- 19 项 Chromium 无头功能验收全过(磨砂 α/对比色/笔色/两段式/容错);backdrop-filter 的实际磨砂效果需 iPad 真机确认。

## 用户规格 v1(原话拆解;被 v2 覆盖的条目以 v2 为准)

1. **入口**:顶栏新增「🗒 便签」按钮 → 按下在当前视野中央创建一个默认白色便签。
2. **结构**:便签 = 上方**操作短矩形**(handle,常驻) + 下方**折叠/展开的记录区域**(body)。
3. **单击 handle** → toggle body 展开/闭合。
4. **长按 handle** → 出现"可移动"特效(浮起阴影/微放大) → 拖拽 handle 移动整个便签 → 松手后 handle 固定在对应**页面位置**(锚定进内容,随内容滚动)。长按后同时出现**删除按钮**(🗑),点它删除整个便签;点别处退出移动模式。
5. **长按 body** → 进入**编辑模式**:可选便签颜色 + 拖右下角手柄调整 body 尺寸。
6. **编辑区输入**:默认文字输入;**手写笔**(pointerType=pen)在编辑区书写 → 记录手写笔画,文字与手写**可重叠**(canvas 叠在文字上,按 pointerType 分流事件:pen→canvas,触摸/鼠标→文字层)。
7. **删除粒度**:手写内容 → 只能用笔的**橡皮模式**擦;文字 → 只能键盘输入删改;整个便签 → 长按 handle 出的删除按钮。
8. **AI 集成(双击)**:AI 助手面板开启状态下,**快速双击便签任何区域** → 便签内容 + 便签插入位置的**上下文**(锚点附近正文)放入 AI 对话上下文。
   - 有手写内容 → 便签整体(文字+绘图层)**合成一张图片**发给 AI(可能是文字+笔标记叠加,必须整体成图);
   - 无手写 → 只发文字内容。
9. **AI 工具**:开放给助手 agentic 工具循环:
   - 查询:按**颜色** / **位置**(某页/某章附近) / **关键词** 检索便签并查看内容;
   - **插入新便签**(指定位置+内容+颜色);
   - **编辑既有便签**(改文字/颜色)。
   (写操作按 [[assistant-write-action-undo-cards]]:自动生成 详情/撤销/重做 按钮组并持久化进聊天记录。)

## 架构映射

### 分层(铁律)
- **`rc-stickynote.js`(新,共享)**:便签 DOM 结构/CSS、状态机(折叠/展开/移动模式/编辑模式/双击检测)、颜色板、resize、手写 canvas 的笔画记录与橡皮(几何归一化到 body 尺寸)、双击收集与注入回调。零底座耦合。
- **adapter hook(per-reader)**:
  - `mount(anchor) -> containerEl`:PDF = 对应 `pw`(page-wrap,absolute 定位,坐标=页面归一化);EPUB = 对应 `.ep-sec`(absolute,坐标=相对 section 宽高归一化)。**便签元素挂进内容容器内部 → 天然随内容滚动/缩放,零 JS 跟滚**(同手写墨迹层 `.ep-ink-canvas` 的锚定技术)。
  - `anchorFromDrop(x, y) -> anchor`:松手视口坐标 → {page,x,y}(PDF)/{section,x,y}(EPUB),归一化。
  - `contextAt(anchor) -> string`:锚点附近正文(PDF=该页文本切片;EPUB=该 section `_countableText` 切片,取锚点 y 比例附近 ±600 字)。
  - `endpoints` / `file`。
- - **后端(两份实现，改行为要同步)**：App 内 `/pdf/api/notes` 与 `/pdf/api/note-composite` 是 owner=local，由 `static/pdf/native-local-runtime.js` 的 `localNotes` / `localNoteComposite` 就地处理（便签存本机 store，合成图 `renderLocalNoteComposite` 在设备上画，无 PIL），本地实现带精确字段白名单且不接受任何查询参数；`pdf_reader.py` 的便签 sidecar CRUD + PIL 合成端点只服务旧网页表面与 Pi 中继。(照 epub-highlights 模式,按书 JSON,PDF/EPUB 同一套路由——anchor 结构不透明存储);合成图片端点(服务端 PIL 重绘文字+笔画,避免前端 html2canvas 依赖)。
- **AI 工具**:`assistant.py` 工具循环注册 `notes_query/notes_read/notes_create/notes_edit`(沙盒注册机制照 task#210/211 现有工具)。

### 数据模型(sidecar,按书一个 JSON)
```json
{ "id": "n_...", "anchor": {"kind":"pdf","page":12,"x":0.7,"y":0.3} | {"kind":"epub","section":5,"x":0.7,"y":0.3},
  "color": "#fff8c5", "w": 260, "h": 180, "collapsed": false,
  "text": "……", "strokes": [{"color":"#e33","w":2,"pts":[[x,y],…]}],   // pts 归一化到 body 宽高
  "created": 0, "updated": 0 }
```

### 手写层要点(复用既有 ink 技术)
- body 内叠 `<canvas>`(absolute 铺满 body),`pointer-events` 动态:非编辑模式 none(双击/滚动穿透);编辑模式按事件 pointerType 分流(pen 捕获画,touch/mouse 穿给 contenteditable)。
- 笔画记录/重绘/橡皮(命中检测擦除)照 `epub-html.js::_epInk` 与 PDF ink 的既有实现语义;resize 时按归一化坐标重绘。
- 橡皮进入方式对齐既有 ink 工具:双击笔尾/工具栏切橡皮(读 `_epInk` 现状为准)。

### 双击注入 AI(链路复用)✅ 已实现(2026-07-02,实际落地形态如下)
- 双击检测:rc-stickynote 自建时间窗(`_tapCount` 380ms 先例);双击时先 `saveText` 冲掉未失焦文字再进 `onDoubleTap(note)` hook(hook/服务端合成图都要最新文字)。
- host 侧回调:PDF=`27-rc-adapter.js` → `window.__noteInject`(实现在 `25-assistant.js`);EPUB=`epub-html.js::noteInject`。**助手 pane 开着(`__asstOpen()`)才注入**,否则 return false 维持现状。
- 注入进 `window.__noteAttached`(独立便签 chip 行 `#asst-note-chips`/`#ep-asst-note-chips`,挨图附件条同款视觉:🗒/合成图缩略 + 摘要 + ✕ 移除;重复双击不叠加,只刷新内容 + toast「已在对话上下文」)。发送时定格进 context、发完即清(同图附件条语义)。
- 无笔画(文本通道):`context.notes = [{id,text,near,page|section}]`,`near`=contextAt(锚点附近正文:PDF=页字符层里离锚点 y 最近字符 ±600 字;EPUB=该 section `_countableText` 按 y 比例 ±600 字);服务端 `_sys_prompt`/`_esys_prompt` 拼「用户双击带入了自己写的便签…」块(**没走 __setFocusSel**:焦点 slot 只有一个且服务端截 sel[:200],装不下便签+上下文)。
- 有笔画(视觉通道):`kind:'note', note_id` 条目并入 `context.figures` 走现有图附件通道;服务端 `_t_see_figure`(assistant.py/epub_assistant.py 都认 kind)按 note_id 调 `pdf_reader._note_composite_png` **现场重合成**(不传 data URL,永远最新 sidecar);prompt fig 块里给便签文字+位置正文,笔画内容按需 see_figure。chip 缩略图另经 `/api/note-composite` 取 data_url 仅前端显示(`_thumb` 字段不随请求发)。

### AI 工具协议(assistant.py)✅ 已实现(2026-07-03,四工具 + 撤销卡,两侧行为一致)
实现:核心四函数在 `assistant.py`(`_t_notes_query/_t_notes_read/_t_notes_create/_t_notes_edit`,`kind='pdf'|'epub'` 参数定位置口径),PDF 助手 TOOLS 直接注册,EPUB 助手 `_etools` 以 `lambda a,c: _A()._t_notes_*(a,c,kind="epub")` 复用。file 一律从会话 ctx 取(`ctx.file_rel`,同 see_figure),AI 不传 file。数据层走 `_vb_notes(file_rel, ctx)`：**原生书读的是 App 随请求送上的 `native_local_state` 权威快照**（快照由 `native-local-runtime.js::nativePDFRequestBody` 注入，Pi 侧 `assistant.py::_native_pdf_state` 校验合同），**只有旧书才落到 `pdf_reader._notes_load/_notes_save`**。

- `notes_query {color?, keyword?, page?|section?}` → {count, notes:[{id, color(色名), 位置(第N页/第N章), text 摘要≤60, has_ink}]} 上限 50;三过滤可组合,全空=列全部。color 收色名(白/黄/蓝/绿/粉/石墨/墨绿,同 rc-stickynote 色板)或 hex;PDF 的 page 是印刷页(`_to_pdf` 换算)。
- `notes_read {id}` → 全文 + 位置 + color;有笔画时 note 提示「含手写内容,调 see_figure({note_id})看合成图」——**see_figure 两侧已加 args.note_id 支持**(不依赖用户双击带入,直接按 id 走 `_note_composite_png`)。
- `notes_create {text, page?|section?, x?, y?, color?}` → id。anchor 组装 {kind,page/section,x,y} 比例锚(x/y 缺省 0.72/0.25,EPUB 前端 mount 时懒迁移升级内容锚);页/章缺省当前;色缺省白(#ffffff,同前端 DEFAULT_COLOR)。
- `notes_edit {id, text?, color?}` → ok。**工具层硬拦**:实现只读 text/color 两个参数,strokes/anchor/w/h 传了也不进(白名单,非 prompt 约束);写前存旧值快照。

**撤销卡(系统自动,非 AI 生成)**:
- EPUB 侧:res["action"] → SSE `action` 事件 → `_epShowAction` 持久 [查看详情/↩撤销/↪重做] 卡(`_echat_worker` 收集落库 meta.actions,刷新后回放)。undo/redo 走 `/pdf/api/epub-action` 新增 op:`sticky_delete`(删)/`sticky_create`(快照原 id 重建,幂等)/`sticky_set`(text/color 白名单恢复)。`_epActClientFx` 对 notes_* kind 调 `notesReload`。
- PDF 侧:res["client_action"] {fn:'_assistEdit', args:[{type:'note', op:'create'|'edit', file, items}]} → 25-assistant.js 便签版「跳转 + 撤销⇄重做」会话卡(同高亮 `_t_highlight` 先例;create 撤=DELETE/重做=POST 快照重建拿新 id,edit 撤=PATCH old/重做=PATCH new)。⚠ PDF 侧卡**不持久化**(PDF 助手对话无 meta.actions 基建,现状与高亮卡一致)。
- 两侧写操作都另记 `voice._undo_record`(kind `sticky`/`sticky_edit`,`_undo_do` 已加分支)→ 聊天说「撤销刚才那个」的 undo_last 也能撤,且 undo_last 撤到便签时自动带 `client_action notesReload` 刷新页面。

**前端刷新**:两 reader 各定义 `window.notesReload()` = `RC.stickynote.loadAll()`(幂等全量重挂);工具建/改后经 client_action(EPUB)或 `_assistEdit` 卡内首行(PDF)触发,撤销/重做后同样刷。

## 实施阶段
- **阶段1 核心**:后端 CRUD + rc-stickynote.js(结构/折叠/长按移动/删除/编辑模式/颜色/resize/文字)+ 两 reader 挂载 hook + 顶栏按钮。
- **阶段2 手写**:canvas 叠层 + pen 分流 + 橡皮 + 归一化持久化。
- **阶段3 AI**:双击注入(文字/合成图)✅(2026-07-02,两侧都接;详见上「双击注入 AI」节)+ 服务端合成端点 ✅(`/api/note-composite`,PIL 逻辑抽成 `_note_composite_png` 与 see_figure 共用)+ assistant 四工具 + 撤销卡 ✅(2026-07-03,两侧都注册;详见上「AI 工具协议」节)。**阶段3 全部完成,便签系统收尾。**

## 冲突提示
设置面板统一 agent(2026-07-02 在跑)正在改 rc-settings.js/epub-html.js/21-misc-ai.js/两模板——**阶段1 前端必须等它完成合并后再动**;后端(pdf_reader.py notes CRUD、assistant.py 工具)无冲突可先行。

## 视频便签(2026-07-06,阶段 A)
便签支持嵌 YouTube 视频 + 学习向控件。这是「拖视频到书里」大功能的地基(拖放/持久化/收藏为后续阶段)。
- **数据**:note 加 `video` 字段 `{id, start, end, rate, loop, cc}`(后端 notes CRUD 的 POST/PATCH 支持,PATCH 传 `video:null` 移除)。
- **渲染**(rc-stickynote.js `renderNoteVideo`):有 video 时给 root 加 `.rc-note-hasvideo` → body 改 flex 列(播放器 16:9 + 控件行 + 文字备注),隐藏手写 canvas。lite-embed(缩略图 → 点▶ 加载 iframe)。签名(JSON.stringify(video))去重,防控件输入时闪 iframe。
- **控件**:起/止时间(**分:秒两框**,数字键盘、秒 clamp 0-59)+ **⏱「设为当前播放位置」**(iframe enablejsapi 收 infoDelivery 拿 currentTime → 填框存后端、不重载不打断当前播放)、速度、循环、字幕、✕移除。起止/循环/字幕 → URL 参数(重载 iframe);速度 → `enablejsapi=1` + postMessage `setPlaybackRate`(player ready 才响应 → 载入后 200/700/1500ms 多次尝试)。字幕默认中文(`cc_lang_pref=zh-Hans`)。
- **入口**:便签工具栏 🎬 按钮 → prompt 贴 YouTube 链接/ID(`ytIdOf` 解析 watch/youtu.be/embed/shorts/live/裸 id);留空移除。`window.__rcNoteSetVideo(noteId, ytid)` 供阶段 B 拖放复用。
- **阶段 B 拖放建便签**(完成,手柄式根治触摸冲突):视频卡缩略图角上一个**拖动手柄 ⠿**,只在手柄 pointerdown 拖(不长按计时)→ 拖影跟手 → 松手在书页(不在助手面板内)→ `RC.stickynote.createVideoAt(x,y,vid)` 按 `anchorFromPoint` 就近建带 video 的便签。移动超阈值取消长按(=滚动)。
- **阶段 C 视频卡持久化**(完成):EPUB 助手 worker 收 `actions` 事件时提取 `renderVideos` 的 videos → 存进 assistant 消息 meta.videos(`_econvo_append` 白名单加 videos);`loadHistory` 回放 assistant 消息后 `renderVideos(m.videos)` → 刷新不丢、清空才没。
- **阶段 D 收藏视频**(完成):视频卡 ☆ → `/api/favorites` 加 `kind:'video'` 条目(无 file、用 `video:<vid>` 合成 key,收第一个夹/无则建「⭐我的收藏」);`_fav_norm_item`+`_fav_video_item` 物化成 section(收藏夹 section 走 raw 不消毒 → `.fav-video[data-yt]` + 缩略图保留);rc-video.js document 委托:收藏夹里点 `.fav-video` 缩略图 → 换 youtube-nocookie iframe 播放。

## ⚠️ 去边(crop)模式坐标统一(2026-07-21,用户实测 vbook 扫描书拖动偏移)

**症状**:开「去边」时,便签/卡片**拖动显示位置 ≠ 松手落点**,偏 -(cropL,cropT)(3% 去边≈十几~二十几 CSS px);关去边就正常。

**根因**:去边靠 CSS `.crop-on>* { transform: translate(-cropL,-cropT) }`(pdf-styles.css)平移**所有**子层——包括便签 `.rc-note`、反馈层 `.rc-anchor-fx`。但 PDF host(`reader.src/27-rc-adapter.js`)原 `noteMount`/`noteAnchorFromPoint`/`noteWordRect` 用**裁后** `pw.clientWidth`(=可见窄宽)算锚/还原像素,没算这个 translate → 落点系统性偏 -(cropL,cropT)。三条链(重拖已钉便签 / 浮层卡钉入 / 侧栏拖出)都经这三个 host 函数,全偏。

**修法**(统一基准,一处修好三条链):三个 host 函数改用 `_pdfNoteGeom(pw)` —— 以 **char-layer(`pw.__charLayer`,撑满整页 `--full-w`、与便签同吃 translate)的实时 `getBoundingClientRect()` + 整页布局宽**为基准。crop/zoom/祖先缩放全部自动含在 BCR 里,便签 append 进吃 translate 的 pw 自然对齐。char-layer 未就绪时回退 `pw BCR + --crop-l/--full-w` 手算。`reader.src/08-charlayer.js` 的 `__charsBaseW` 也改存**整页布局宽**(去边下=`--full-w`;charBox 本就是整页坐标)。anchor.x/y 语义从"裁后比例"变"整页比例"——非去边下两者恒等(char-layer=pw 全宽),旧便签零影响;去边旧便签本就偏,自动修正。⚠ 改 `reader.src/*` 后必须重建 `reader.js`（`bash scripts/check_pdf_reader_js.sh`），再按表面分别投递：Pi/旧网页走 `bash scripts/deploy_reader.sh`（唯一生产写入口，别手工 sudo cp）；**iPad App 加载的是打进包的 ReaderBundle**（`ios/BWReader/package_local_reader.py`），只能靠新的 TestFlight 构建到达；Safari 扩展加载 `extensions/bw-reader-webext/vendor/` 自带副本，需同步并重新打包。。

**顺带修 S1**(外部诊断确认的竞态,`rc-stickynote.js`):`dropNote` 探测点从 `rect0+指针位移` 改为**松手瞬间实时 BCR**(transform-origin '0 0' 下左上=视觉落点),与拖动反馈 `anchorFxShow` 一致;`repositionPortaled` 跳过拖动中的便签(防异步页渲染改写 `root.left`)。

**验证**(headless Playwright,探测点→anchorFromPoint→noteMount→探针 BCR 闭环):单本 crop 开/关 **0.0px**、真实 vbook(料理师)**0.0px**、词框反馈 8/8 dist=0 覆盖字符、真实拖动松手前后 delta=(0,0)。

**铁律**:今后任何"屏幕坐标 ↔ 页锚"的新功能,**必须走 char-layer BCR 基准**(或 `_pdfNoteGeom`),严禁直接用 `pw.clientWidth`(去边下是裁后窄宽,必偏)。

---

## 卡片便签的「词锚」形态（2026-08-20 用户拍板，PDF 阅读器）

原来卡片便签钉在页上是一个**圆球**，用户的问题是「圆球过多会遮挡视野」。
新形态：

> 「插入后自动锁定到前方的分词元素，高亮整个分词，右上角加上数字，
> 然后点击这个词直接展开卡片」

即：卡片不在页上占地方，**被绑定的那个词本身**就是它的把手。

### 不是另造一套便签

卡片壳、学习状态、拖动、删除、落库全是原来那份。词锚只加两件事：

1. 画标记（`__pageBindCard` → `.pgmark` 描边 + `.pgmark-n` 角标）
2. 把便签默认 `display:none`，点标记才显出来

因此**手动钉和 AI 自动钉产出完全相同的标记**（两条路进同一个
`__pageBindCard`），页内序号也是同一套。

### 存在哪

`note.card.bind = { kind:'page-chars', page, from, to, text }`。

⚠ **不能存进 `note.anchor`**：anchor 在 `native-local-runtime.js` 的
`normalizedNoteAnchor` 里是**逐字段重建**的，加字段会被静默 strip——当次
会话看不出来，下次开书才发现锚退化了。`card` 是 `boundedCanonicalJSON`
整块不透明存，所以服务端和 App runtime **一行都不用改**。

### 五个必须一起做的点（少一个都是"看着像正常"的错位）

| 时机 | 做什么 | 不做的表现 |
|---|---|---|
| 建卡 | 落点吸附最近词（≤48px，跟拖动反馈同一阈值） | 「看到框、松手却钉到别处」 |
| **charBoxes 就绪** | `RC.stickynote.repositionAll()` | 首次开页必然退回圆球（挂载跑在 charBoxes 之前） |
| 删卡 | `__pageBindRemove` + 整页重排序号 | 框留在页上，且**后面所有序号错位** |
| 拖动 | 用**加过 shift 的落点**重求词锚 | 标记留在旧词、卡片跑到别处 |
| 绑不上 | 退回浮层 **并 `console.warn`** | 圆球本来就是老形态，退回去看着像"本来就该这样" |

序号是**位置不是身份** —— 加卡删卡都整页重算（`_renumberMarks`），
不维护自增计数器。这样人和 AI 说的「第 3 个」才是同一张。

### 还没做

AI **读**页面时看不见卡片编号（`buildLocalPageContext` 吐的是纯文本）。
所以「把第三个删掉」这个用途目前只有人这一侧成立。要补的是
`⟦CARD:n …⟧` 内联标记，格式见
[`card-anchor-footnote-design.md`](card-anchor-footnote-design.md) §7.3。

⚠ EPUB 没有 `__pageBindCard`，词锚在 EPUB 上静默回落到老浮层便签。

## 词 → 卡片 反向索引（2026-09-03 重做，用户拍板）

用户原话：「字典的内容还是保存在原本的字典内，卡片的内容还是保存在卡片内，只是锁定后
进行一次标记……解绑等动作发生时需要将其解除……卡片被解绑为自由卡、移动到其他地方、
绑定新元素，都要立刻体现」。

**病根**：旧索引（device state `word-card-index`）存的是卡片内容的**副本**，由语音建卡时
写入（`rc-voicecall._registerWordCard`），之后解绑/改锁/移动都不碰它，所以单词框里
嵌的卡永远是建卡那一刻的样子。

**现行设计**（`native-local-runtime.js`）：

- **只有一份真相**：卡片内容只在便签 `html.content`，锁定只是 `html.bind`（`page-chars`）。
- **标记与便签同批写入**：`deriveWordBindings(notes)` 派生每书一条 `word-bindings`
  记录（`{cid, key, text, page, label, at, order}`，**无内容**），与 notes / card-placements /
  entity-references 在 `mutateDocumentStateNow` / `writeNotesAndIndexes` 的**同一批事务**里落盘，
  因此不可能有过期副本。键 = 去空白小写的绑定文本。
- **内容现读**：`/pdf/api/word-card-index?lemma=&word=`（或 `?cid=`）→ `listAllWordBindings`
  扫全部 `native-word-bindings` 记录 → `liveWordCards` 分书读便签取当前内容；便签没了或
  绑定键不再匹配就当它不存在（索引自愈）。POST 只为兼容旧调用方保留，返回 `deprecated`。
- **事件**：事务提交后广播 `bw:native-word-bindings-changed`（契约
  `reader-word-bindings-changed/1`，detail 含 `keys` / `changes[{cid,before,after}]`）。
  `rc-wordpop` 订阅它，正开着的小框按键集合命中就重取卡片段。电脑端上下文快照不需要新
  接线：绑定本来就是一次便签写入，既有 `bw:native-document-notes-changed` 已让它重发。
- **一次性迁移**：`ensureWordBindingsRebuilt` 首次查询时扫全部便签记录补齐标记，
  device state `word-bindings-rebuilt` 记一次。
- **词卡整理**（`wordCardsConsolidate`）：直接把内容写回各书便签本体
  （`applyWordCardContents` → `writeNotesAndIndexesFor`），撤销用 `word-card-consolidations`
  单层 prev；`rc-stickynote.reconcileConsolidatedWordCard`（开卷对账）随之删除。

契约：`tests/reader_contract/word-card-bindings.contract.test.mjs`。

### 卡内词典的查词链与快照锚词（2026-09-04）

- **卡内词典与查词框走同一条查词链**：`RC.wordpop.lookupData(word, ctx)` = 内存缓存 → 设备持久缓存
  （`localStorage` `rc-wordpop-dict-cache-v1`，600 上限 LRU）→ 本地 JMdict → 服务器 dict-quick → ReaderPC Codex 兜底。
  此前卡内词典只打服务器 dict-quick（prewarm），外来语（パンセオ）查词框有释义、卡里却是空的。
  写入方式不变：仍是写进卡片 content 末尾的 `<div class="rc-note-dict vc-dict-sec" data-dict-word="…">`，
  不是另起一张卡；改锁到别的词先摘旧段再写新段。
- **查过的词再点不再闪烁重查**：查到中文义的结果落设备持久缓存；合成兜底词条（「暂无词典释义…」）不落盘，
  免得永久短路真查询；`mastered` 不随词条缓存（掌握态另有权威）。
- **快照卡片标记带锚词**：`⟦CARD_START n=".." … label=".." anchor="被锚定的词"⟧正文⟦CARD_END⟧`（未锚定卡无 anchor）。
  卡插在锚词之后，此前只靠位置看不出钉在哪个词上。PC 快照查看器改为带四边框的卡块，标题
  「🎴 卡片 #n · label · 锚「词」」。消费方（C# 两处解析器、MCP、typist）都把属性当整串保存，加属性向后兼容。
- **三次踩坑的契约**：`word-card-bindings.contract.test.mjs` 扫卡内词典链路里每个被调用的名字都有定义
  （`bindWordTextOf` → `_dictLineCache` 两次都是"使用处在、定义被删、ReferenceError 被空 catch 吞掉"）。
