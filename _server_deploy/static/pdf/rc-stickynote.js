/* rc-stickynote.js — 统一控制层:便签(Sticky Notes)共享组件(PDF/EPUB 通用,零底座耦合)。
 * 规格:references/sticky-notes-design.md「⚠️ 规格 v3」逐条;后端 /pdf/api/notes CRUD(anchor 不透明存储)。
 * 铁律(v4 修正):便签挂进内容容器内部(PDF=pw 内 absolute / EPUB=.ep-sec 内 absolute)→ 随内容滚动/缩放
 *   天然跟随,零 JS 跟滚。**定位机制归 host**:O.mount(anchor) 返回 {el,left,top}(容器内像素),组件只应用——
 *   PDF 锚仍 {kind:'pdf',page,x,y}(host 换算 x·clientWidth/y·clientHeight,行为等价旧 %);EPUB 锚 v4 升级为
 *   **内容锚** {kind:'epub',section,off,dx,dy}(off=最近可数文字偏移,同高亮 offsetOf 坐标系;dx/dy=相对该字符
 *   rect 的像素偏移;x/y 保留作纯图/无文字兜底),重排(侧栏开关/字号/栏宽)后字符位置变 → host 经
 *   repositionAll 重算,便签跟着字走零漂移。旧 x/y 锚由 host 在 mount 时懒迁移(返回 anchor 字段),组件 PATCH 落库。
 *   拖拽/左上缩放期间的 transform 是唯一暂态例外,松手统一经 reanchorAt→opts.anchorFromPoint 重取锚并 PATCH
 *   (handle 拖拽支持跨页/跨章;左上缩放 host 解析失败时退回同容器 dx/dy(内容锚)或 x/y(比例锚)位移补偿)。
 * 手势状态机 v3(v2 的 MOVE 移动模式 / STYLE 样式模式合并为唯一 EDIT 编辑模式):
 *   · 单击 body → 文字输入(textarea 常态可点可聚焦,无 readonly/pointer-events 门槛;失焦 PATCH text);
 *   · 长按便签任意部分(handle 或 body 同一入口;时长可调 lpMs():localStorage rc-note-longpress
 *     200–800ms 缺省 350,rc-settings「便签」tab 滑块写)→ EDIT 编辑模式,同时呈现:
 *     🗑(handle 旁)+ 色板(body 底部工具条)+ 左上/右下两个缩放手柄 + handle 可拖拽移动。
 *     浮起效果(.rc-note-lift)只在拖拽进行时;左上手柄=位置+尺寸同变(右下角为锚点固定,w/h 变化的
 *     补偿量换算回容器归一化并入 anchor 一并 PATCH;min 约束同右下);右下手柄=原有行为。
 *     进入时 blur 收键盘(handle/body 入口一致),模式内 textarea 暂 inert;移动/缩放/换色可自由连续
 *     操作不自动退出;点便签外退出(退出时保存文字/笔画待存项)。
 *   · 单击 handle → toggle body 折叠/展开(延迟 380ms 给双击让路,照 epub-html.js::_tapCount 380ms 窗先例;
 *     EDIT 模式内 handle=移动把手,不折叠);
 *   · 双击任意区域(380ms 窗)→ opts.onDoubleTap(note)(阶段3 AI 注入 hook)。例外:便签有笔画时,
 *     手指在 body 上的双击被「临时橡皮切换」优先吃掉(第二击 stopPropagation → 不进 AI 双击计数;
 *     handle 上的双击不受影响,仍走 AI hook)。
 * 手写 v2:pen 常态直写(任何时候、无需模式)。两个 reader 的页面 ink(pdf_reader.html::_ink* /
 *   epub-html.js::_epInk)在祖先层 capture 拦截 pen 并 stopPropagation —— 事件到不了本组件,由它们经
 *   penRoute/penBegin/penMove/penEnd 编程接口路由进来(跨界三段切割:笔尖实时位置决定写便签还是写页面,
 *   一条连续笔画在便签边界切段)。body 自己的 pen 拦截只是无页面 ink 底座时的独立后备(自管 document
 *   监听,出 body 即截断便签段,绝不画出便签)。手指快速双击 350ms/32px 且已有笔画 → 切临时橡皮
 *   quickErase(空闲 2.5s 自动回笔、擦完抬笔 0.9s 回笔,语义照搬 _epInk);鼠标不画(桌面=键盘文字)。
 *   橡皮=命中检测删除整条笔画(_inkHit 同款点-线段距离);笔画 {c,w,pts} 归一化到 body 宽高(后端
 *   /api/note-composite 合成图同一坐标系);resize 时 canvas 重设尺寸按归一化重绘;coalesced events + 抽稀。
 * iOS 文字隐形根因修复:PDF 的 .page-wrap 带 -webkit-user-select:none,WebKit 对 none 子树内的输入框
 *   在编辑期间不渲染字形/光标(失焦才出现,bugs.webkit.org #82692 族);光在 textarea 上设 text 不够,
 *   必须给 .rc-note-body 整个子树显式 -webkit-user-select:text 切断继承(handle 保持 none 供拖拽)。
 * 外观(2026-07-02,设置在 rc-settings「便签」tab,localStorage 设备级键,共享组件不挂 pdf- / eph- 前缀):
 *   · 磨砂玻璃:body 背景 = rgba(便签色, α) + backdrop-filter:blur(10px)(-webkit- 前缀 iOS 必须,CSS 常驻);
 *     α 读 rc-note-opacity(0.3–1,默认 0.72);handle 是操作件要醒目,α 取 max(0.85, 用户α)。
 *   · 自动对比色:rc-note-autocontrast(默认开)。按便签本色 W3C relative luminance(阈值 0.55,纯函数,
 *     α 不参与判定)决定前景:亮底→深(#1b1b1b)/暗底→浅(#f2f5f9),经 .rc-note-darkbg class 联动
 *     文字/placeholder/光标/handle 横杠/resize 角标;新笔画默认色同前景。已有笔画是用户数据,不改色。
 *     关=固定现状(深字 + INK 默认红)。色板含深色系(石墨/墨绿)让对比色有意义;旧便签颜色不在
 *     色板也正常渲染(applyColor 不依赖色板,hex 解析失败原样不透明兜底)。
 *   · 设置保存后 rc-settings 调 RC.stickynote.refreshStyle() → 对每个已挂载 ctl 重跑 applyColor 即时生效。
 * opts(per-reader 底座):
 *   file                  书相对路径(API 用)
 *   mount(anchor)         -> {el,left,top,anchor?}|null  容器 + 容器内像素位置(host 算;anchor=懒迁移升级后的
 *                            新锚,组件 PATCH 落库;未渲染/未加载 → null,稍后 mountPending 重试;
 *                            旧契约 {el,w,h} 兼容:无 left/top 时组件退回自算 x/y 百分比)
 *   anchorFromPoint(x,y)  -> anchor|null    视口坐标 → 锚点(PDF={kind,page,x,y};EPUB v4={kind,section,off,dx,dy,x,y})
 *   onDoubleTap(note)     -> bool           双击回调(true=已处理;阶段3 AI 注入)
 *   toast(msg)            可选,提示(缺省 RC.toast)
 */
(function () {
  if (!window.RC) window.RC = {};
  if (window.RC.stickynote) return;
  var RC = window.RC;

  var API = '/pdf/api/notes';
  var COLORS = [
    { c: '#ffffff', n: '白' },
    { c: '#fff8c5', n: '黄' },
    { c: '#cfe3ff', n: '蓝' },
    { c: '#d5f2d9', n: '绿' },
    { c: '#ffd9e8', n: '粉' },
    { c: '#2d3440', n: '石墨' },   // 深色系:让自动对比色有意义(浅字/浅笔画)
    { c: '#1f3a2e', n: '墨绿' }
  ];
  var DEFAULT_COLOR = '#ffffff';        // 用户规格1:默认白色便签
  var INK = { color: '#e74c3c', width: 2 };   // 便签手写笔色/粗细(独立于页面 ink 的当前笔色,各用各的)
  var FG_DARK = '#1b1b1b', FG_LIGHT = '#f2f5f9';   // 自动对比色两档前景(亮底→深 / 暗底→浅)
  var LSK_OPACITY = 'rc-note-opacity', LSK_AUTOC = 'rc-note-autocontrast';   // 设备级设置键(rc-settings「便签」tab 写)
  var LSK_LP = 'rc-note-longpress';     // 长按时长设置键(毫秒;rc-settings「便签」tab 滑块写)
  var TAP_WIN = 380, TAP_TOL = 10;      // 双击窗口/tap 位移容差(照 _tapCount 先例)
  var LP_TOL = 10;                      // 长按移动取消容差
  var CARD_DRAG_HOLD_MS = 420;          // 卡头蓄力：与侧栏宽度把手采用同一成熟节奏
  var CARD_DRAG_TOL = 8;                // 蓄力前超过容差即取消，快划不会误拖或切形态

  var O = null;        // opts
  var notes = [];      // 服务端便签列表(本地镜像)
  var ctls = {};       // id -> controller {note,root,handle,body,ta,cv,tools,rs,rsTL,...}
  var EDIT = null;     // 编辑模式(长按便签任意部分){note, ctl}:🗑+色板+双缩放手柄+handle 拖拽移动
  var NINK = { tool: 'pen', quickErase: false, _revertT: null, _lastTap: null };  // 便签手写工具态(全局一支笔,同页面 ink 惯例)
  // 原生 PencilKit 的共享工具态。region 只属于书页：便签记住自己最后一支
  // pen/eraser，但在共享工具为 region 时拒绝笔路由，绝不偷偷创建私有选区。
  var SHARED_INK_TOOL = 'pen';
  var SHARED_INK_STYLE_ACTIVE = false;
  var draw = null;     // 手写进行中 {ctl, stroke|eraser, raf}
  var _selfDraw = false; // 无页面 ink 底座时的后备自管手势进行中
  var _hd = null;      // handle 手势 {ctl, sx, sy, lp, dragging, moved, rect0}
  var _bd = null;      // body 长按 {ctl, sx, sy, lp}
  var _rz = null;      // 右下 resize {ctl, sx, sy, w0, h0, raf}
  var _rzTL = null;    // 左上 resize {ctl, sx, sy, w0, h0, shiftX, shiftY, raf}(右下角锚定:位置+尺寸同变)
  var _docBound = false;
  // 存储边界：PWA 未迁移时仍走 legacy HTTP；扩展传入 scoped repository 后，
  // 本组件的所有便签 I/O 只经 repository，绝不再碰 /pdf/api/notes。
  var _generation = 0;
  var _unsubscribe = null;
  var _writeQueues = Object.create(null);   // noteId -> Promise；同一便签严格串行写
  var _seenRevs = Object.create(null);      // CHANGE 先于 LIST/RESULT 时阻止旧快照回灌
  var _mutationSeq = 0;

  function toastMsg(m) {
    try { if (O && O.toast) return O.toast(m); } catch (e) {}
    try { if (RC.toast) return RC.toast(m); } catch (e2) {}
    try { console.log('[rc-stickynote]', m); } catch (e3) {}
  }

  // ─────────────────────────── 外观设置读取 + 对比色纯函数 ───────────────────────────
  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function noteOpacity() {   // body 底色不透明度(rc-settings「便签」tab 滑块写 0.3–1;缺省 0.72)
    var v = parseFloat(lsGet(LSK_OPACITY));
    if (isNaN(v)) v = 0.72;
    return Math.max(0.3, Math.min(1, v));
  }
  function autoContrast() { return lsGet(LSK_AUTOC) !== '0'; }   // 自动对比色开关(缺省开)
  function noteBlur() {   // 磨砂强度(rc-settings「便签」tab 滑块写 0–24px;缺省 10;0=纯半透明不模糊)
    var v = parseInt(lsGet('rc-note-blur'), 10);
    if (isNaN(v)) v = 10;
    return Math.max(0, Math.min(24, v));
  }
  function lpMs() {   // 长按进入 EDIT 的时长(rc-settings「便签」tab 滑块写 200–800ms;缺省 350,规格 v3 缩短)
    var v = parseInt(lsGet(LSK_LP), 10);
    if (isNaN(v)) v = 350;
    return Math.max(200, Math.min(800, v));
  }
  // hex → {r,g,b}|null(容错 #rgb/#rrggbb;非法/非 hex 返回 null → 调用方原样兜底,旧数据不炸)
  function hexRgb(hex) {
    if (typeof hex !== 'string') return null;
    var m = hex.trim().match(/^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/);
    if (!m) return null;
    var h = m[1];
    if (h.length === 3) h = h.charAt(0) + h.charAt(0) + h.charAt(1) + h.charAt(1) + h.charAt(2) + h.charAt(2);
    return { r: parseInt(h.slice(0, 2), 16), g: parseInt(h.slice(2, 4), 16), b: parseInt(h.slice(4, 6), 16) };
  }
  // W3C relative luminance(sRGB 线性化加权;按便签**本色**算,透明度不参与判定)
  function relLum(rgb) {
    var f = function (v) { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(rgb.r) + 0.7152 * f(rgb.g) + 0.0722 * f(rgb.b);
  }
  // 便签色是否深底(阈值 0.55;hex 解析失败按浅底 → 深字,即现状语义)
  function isDarkBg(hex) {
    var rgb = hexRgb(hex);
    return !!rgb && relLum(rgb) < 0.55;
  }
  // 新笔画颜色:自动对比色开=按便签本色取对比前景;关=固定 INK 默认红(现状)。已有笔画存的 c 不动。
  function strokeColor(ctl) {
    // App 原生工具条给出的颜色是显式用户选择，优先于便签的自动对比默认值。
    if (SHARED_INK_STYLE_ACTIVE) return INK.color;
    if (!autoContrast()) return INK.color;
    return isDarkBg(ctl.note.color || DEFAULT_COLOR) ? FG_LIGHT : FG_DARK;
  }

  // ─────────────────────────── CSS(注入一次,rc-note-* 独立类名)───────────────────────────
  var _cssIn = false;
  function injectCss() {
    if (_cssIn) return; _cssIn = true;
    var css = document.createElement('style'); css.id = 'rc-stickynote-css';
    css.textContent = [
      // 根:absolute 挂内容容器内(pw / .ep-sec 均 position:relative),left/top=锚点百分比 → 随内容滚动
      // user-select:text 在 root 就显式设(不只 body):iOS WebKit 对 none 祖先链内 textarea 的渲染 bug,
      // 中间层显式 text 有时仍不够,全链切断最稳(handle 自己再设回 none 供拖拽)。
      '.rc-note{position:absolute;z-index:40;touch-action:manipulation;-webkit-tap-highlight-color:transparent;transform-origin:top left;-webkit-user-select:text;user-select:text}',
      // 展开态置顶:盖过两侧栏(#grammar-panel/#ep-side=120)、把手(130)、PDF 选中框(135)与相邻页;仍低于模态遮罩(≥200)。
      //   祖先(#main / .page-wrap / .ep-sec)稳态无 z-index/zoom → 不成层叠上下文,便签在根上下文里抬高即生效。
      //   折叠态(仅手柄)保持低层(40),侧栏开着时不挡。
      '.rc-note:not(.rc-note-collapsed){z-index:140}',
      '.rc-note.rc-note-active{z-index:150}',
      // portaled 便签在 pointer-events:none 的叠加层里 → 须显式 auto(pointer-events 会继承)才收得到点击;
      //   否则整张便签"点穿"到下方页面,按钮全失灵。内部显式 none 的层(ink canvas / 编辑态 textarea)不受影响。
      '.rc-note.rc-note-portaled{pointer-events:auto}',
      // handle:短矩形,常驻,显示便签色;::before 扩触控区 ≥32px(视觉小、命中大)
      '.rc-note-handle{position:relative;width:56px;height:20px;border-radius:7px;border:1px solid rgba(0,0,0,.28);box-shadow:0 2px 6px rgba(0,0,0,.3);cursor:grab;touch-action:none;-webkit-touch-callout:none;-webkit-user-select:none;user-select:none}',
      '.rc-note-handle::before{content:"";position:absolute;left:-8px;right:-8px;top:-8px;bottom:-8px}',
      '.rc-note-handle::after{content:"";position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:22px;height:4px;border-radius:2px;background:rgba(0,0,0,.22)}',
      // 浮起特效(阴影加深+微放大+轻微透明):只在 handle 拖拽进行时(EDIT 模式内静止不浮)
      '.rc-note.rc-note-lift{transform:scale(1.03);transform-origin:0 0;opacity:.92}',
      '.rc-note.rc-note-lift .rc-note-handle{cursor:grabbing;box-shadow:0 10px 26px rgba(0,0,0,.5)}',
      '.rc-note.rc-note-lift .rc-note-body{box-shadow:0 12px 30px rgba(0,0,0,.45)}',
      '.rc-note.rc-card-drag-charging .rc-note-handle{box-shadow:inset 0 -2px 0 rgba(125,211,252,.38)!important}',
      '.rc-note.rc-card-drag-charging .rc-note-handle::before{content:"";position:absolute;left:0;bottom:0;height:2px;width:100%;border-radius:2px;background:#7dd3fc;transform-origin:left;animation:rc-card-drag-charge .42s linear both}',
      '.rc-note.rc-card-drag-ready .rc-note-handle{box-shadow:inset 0 -2px 0 rgba(125,211,252,.9)!important}',
      '@keyframes rc-card-drag-charge{from{transform:scaleX(0);opacity:.35}to{transform:scaleX(1);opacity:1}}',
      /* 删除键:Apple 简约风——右上角小圆角标,毛玻璃深底 + 白色细线 ✕(不再红底红边飘右侧;确认弹窗才是危险动作) */
      '.rc-note-del{position:absolute;top:-10px;right:-10px;z-index:6;width:24px;height:24px;border-radius:50%;border:none;background:rgba(28,28,30,.62);-webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px);color:#fff;line-height:0;display:none;align-items:center;justify-content:center;cursor:pointer;padding:0;box-shadow:0 1px 5px rgba(0,0,0,.32);-webkit-tap-highlight-color:transparent;transition:transform .12s ease,background .12s ease}',
      '.rc-note-del:hover{background:rgba(40,40,44,.72)}',
      '.rc-note-del:active{transform:scale(.86);background:rgba(20,20,22,.85)}',
      '.rc-note.rc-note-editing .rc-note-del{display:flex}',
      // body:折叠/展开的记录区(底=rgba(便签色,α) 由 applyColor 内联;磨砂 blur 在这常驻,
      //   -webkit-backdrop-filter iOS 必须;文字 textarea + 手写 canvas 叠放,可重叠)。
      // ⚠ user-select:text 必须显式给整个子树:PDF 挂载容器 .page-wrap 是 -webkit-user-select:none,
      //   iOS WebKit 对 none 子树内的 textarea 编辑期间不渲染字形/光标(输入隐形 bug 根因)。
      // blur 不再 CSS 常驻:磨砂强度可调(rc-note-blur 0-24px),由 applyColor 内联设置(refreshStyle 即时生效)
      '.rc-note-body{position:relative;margin-top:6px;border-radius:10px;border:1px solid rgba(0,0,0,.22);box-shadow:0 4px 12px rgba(0,0,0,.28);overflow:visible;-webkit-touch-callout:none;-webkit-user-select:text;user-select:text}',
      '.rc-note.rc-note-collapsed .rc-note-body{display:none}',
      // 文字层:16px 防 iOS 聚焦自动缩放;常态可点可聚焦(单击=输入,v2 规格1)
      // -webkit-text-fill-color 显式设(它优先于 color,堵祖先继承);translateZ(0) 强制独立合成层——
      // iOS 在 -webkit-overflow-scrolling:touch 滚动容器内的 absolute textarea 有"文字不渲染"的合成层老坑;
      // appearance:none 去 iOS 原生皮肤怪癖。均为无副作用加固(2026-07-02 文字隐形第二轮修复)。
      '.rc-note-text{position:absolute;left:0;top:0;width:100%;height:100%;box-sizing:border-box;padding:8px 10px;background:transparent;border:none;outline:none;resize:none;font-size:16px;line-height:1.45;color:#1b1b1b;-webkit-text-fill-color:#1b1b1b;caret-color:#1b1b1b;opacity:1;-webkit-appearance:none;appearance:none;transform:translateZ(0);font-family:inherit;overflow-y:auto;-webkit-user-select:text;user-select:text}',
      '.rc-note-text::placeholder{color:rgba(0,0,0,.32)}',
      // EDIT 模式:textarea 暂 inert(长按松手不误聚焦弹键盘;taps 归 body/色板/手柄),文字留出工具条高度
      '.rc-note.rc-note-editing .rc-note-text{pointer-events:none;padding-bottom:46px}',
      // 手写层:铺满 body,永远 pointer-events:none(纯显示;绘制经页面 ink 路由或 body 后备拦截)
      '.rc-note-ink{position:absolute;left:0;top:0;width:100%;height:100%;pointer-events:none;background:transparent;z-index:2}',
      // 样式工具条(EDIT 模式浮 body 底部):颜色板 + 笔/橡皮(7 色后允许换行,窄便签不溢出)
      '.rc-note-tools{position:absolute;left:0;right:0;bottom:0;display:none;align-items:center;flex-wrap:wrap;gap:8px;padding:7px 10px;background:rgba(0,0,0,.08);z-index:3}',
      '.rc-note.rc-note-editing .rc-note-tools{display:flex}',
      '.rc-note-swatch{position:relative;width:22px;height:22px;border-radius:50%;border:1px solid rgba(0,0,0,.3);padding:0;cursor:pointer;flex-shrink:0}',
      '.rc-note-swatch::before{content:"";position:absolute;left:-6px;right:-6px;top:-6px;bottom:-6px}',
      '.rc-note-swatch.on{box-shadow:0 0 0 2px #2f6fce}',
      '.rc-note-tool{position:relative;margin-left:auto;width:32px;height:28px;border-radius:7px;border:1px solid rgba(0,0,0,.25);background:rgba(255,255,255,.6);font-size:14px;line-height:1;cursor:pointer;padding:0;flex-shrink:0}',
      '.rc-note-tool.on{background:#ffe2a8;border-color:#c98a2b}',
      // resize 手柄(EDIT 模式,右下角,34px 触控)+ 左上角手柄(视觉同款角标,位置+尺寸同变)
      // resize 手柄贴便签**外侧边缘**(负偏移露到 body 外,body overflow:visible),不再占内部空间跟工具条/文字冲突。
      // 手柄本体做成圆形小抓点(半透明白+描边),视觉像 iOS 悬浮调整点。
      '.rc-note-rs{position:absolute;right:-11px;bottom:-11px;width:26px;height:26px;display:none;z-index:6;cursor:nwse-resize;touch-action:none;border-radius:50%;background:rgba(255,255,255,.92);border:1px solid rgba(0,0,0,.28);box-shadow:0 2px 6px rgba(0,0,0,.3)}',
      '.rc-note.rc-note-editing .rc-note-rs{display:block}',
      '.rc-note-rs::after{content:"";position:absolute;right:7px;bottom:7px;width:9px;height:9px;border-right:2.2px solid rgba(0,0,0,.5);border-bottom:2.2px solid rgba(0,0,0,.5);border-radius:2px}',
      '.rc-note-rs-tl{position:absolute;left:-11px;top:-11px;width:26px;height:26px;display:none;z-index:6;cursor:nwse-resize;touch-action:none;border-radius:50%;background:rgba(255,255,255,.92);border:1px solid rgba(0,0,0,.28);box-shadow:0 2px 6px rgba(0,0,0,.3)}',
      '.rc-note.rc-note-editing .rc-note-rs-tl{display:block}',
      '.rc-note-rs-tl::after{content:"";position:absolute;left:7px;top:7px;width:9px;height:9px;border-left:2.2px solid rgba(0,0,0,.5);border-top:2.2px solid rgba(0,0,0,.5);border-radius:2px}',
      // 视频便签:工具条的笔/橡皮/视频按钮**默认不显示**(但画笔功能仍在:Apple Pencil / 手指双击切橡皮照常可画,笔画画在文字备注区);视频区自己裁圆角
      '.rc-note.rc-note-hasvideo .rc-note-tool{display:none}',
      // ⚠ 折叠优先:hasvideo 的 body{display:flex} 会盖过 collapsed 的 display:none → 视频便签折不了。加更高特异性规则修回
      '.rc-note.rc-note-hasvideo.rc-note-collapsed .rc-note-body{display:none}',
      '.rc-note-card{display:none}',
      '.rc-note.rc-note-hascard .rc-note-card{display:block;padding:2px 2px 4px}',
      '.rc-note.rc-note-hascard .rc-note-text,.rc-note.rc-note-hascard .rc-note-tools,.rc-note.rc-note-hascard .rc-note-ink{display:none!important}',
      '.rc-note.rc-note-hascard.rc-note-collapsed .rc-note-card{display:none}',
      // 卡片式便签(hascard/hashtml)= **真 vc-card**(用户拍板:和字幕浮层卡一模一样,便签只当锚定壳):
      //   便签壳全透明(背景/边框/阴影全清),body 里渲 .vc-card.vc-pinned(rc-voicecall 同一套 CSS);
      //   handle 变透明层覆盖卡头区=拖动把手(拖动逻辑零改动)
      '.rc-note.rc-note-hascard .rc-note-handle,.rc-note.rc-note-hashtml .rc-note-handle{position:absolute;left:0;top:0;width:100%;height:36px;background:transparent!important;box-shadow:none!important;z-index:5;border-radius:16px 16px 0 0}',
      '.rc-note.rc-note-hascard .rc-note-handle::after,.rc-note.rc-note-hashtml .rc-note-handle::after{display:none}',
      '.rc-note.rc-note-hascard .rc-note-body,.rc-note.rc-note-hashtml .rc-note-body{margin-top:0;border:none!important;box-shadow:none!important;background:transparent!important;border-radius:16px}',
      '.rc-note.rc-note-hascard .rc-note-rs,.rc-note.rc-note-hascard .rc-note-rs-tl,.rc-note.rc-note-hascard .rc-note-del,.rc-note.rc-note-hashtml .rc-note-rs,.rc-note.rc-note-hashtml .rc-note-rs-tl,.rc-note.rc-note-hashtml .rc-note-del{display:none!important}',   // 白圆手柄/外部✕不属于卡观感；删卡只走拖到左上角大投放区
      // 拖动锚定反馈(用户设计 2026-07-21 #51):拖动时实时标出将绑定的位置——
      //   光带=命中内容(锚到这段);横线=空白/clamp(内容插入位置,排到上方内容之后)。iOS 蓝,美观优先。
      '.rc-anchor-fx{position:absolute;pointer-events:none;z-index:60;transition:top .06s linear,left .06s linear,width .06s linear}',
      '.rc-anchor-fx.rc-afx-word{border-radius:4px;background:rgba(10,132,255,.16);box-shadow:inset 0 0 0 1.5px rgba(10,132,255,.55)}',
      '.rc-anchor-fx.rc-afx-line{height:0;border-top:2px solid #0a84ff;border-radius:0;background:none;box-shadow:0 0 6px rgba(10,132,255,.5)}',
      '.rc-anchor-fx.rc-afx-line::before{content:"";position:absolute;left:-4px;top:-5px;width:8px;height:8px;border-radius:50%;background:#0a84ff}',
      '.rc-anchor-fx.rc-afx-line::after{content:"";position:absolute;right:-4px;top:-5px;width:8px;height:8px;border-radius:50%;background:#0a84ff}',
      '.rc-note-html{display:none}',
      '.rc-note.rc-note-hashtml .rc-note-html{display:block;padding:3px 5px 5px;font-size:14px;line-height:1.6;color:#e6e6f0;max-height:min(50vh,340px);overflow-y:auto;-webkit-overflow-scrolling:touch}',
      // ★ 壳里装的是整张 .vc-card 时,**尺寸与滚动全部交给卡片**。
      //   上面那条给普通 HTML 便签用的 padding + 限高 + 独立滚动,套在卡片外面会出三件事:
      //   ① padding 把卡片顶向右下(左 5px/上 3px),超出的右边被壳裁掉 —— 就是"右边变直角";
      //   ② 壳的 max-height 让整张卡在壳里再滚一层(卡片自己已经有 overflow-y:auto);
      //   ③ 壳的圆角与卡片圆角不同心,于是看着像卡片外面浮了一层错位的框。
      //   用户诊断:"整个卡片都放在一个透明底框中甚至可以在其中滚动，这就是所有问题的根源"。
      '.rc-note.rc-note-hashtml .rc-note-html.rc-note-html-card{padding:0;max-height:none;overflow:visible;background:transparent}',
      // ★ 卡片收成球时，壳必须**完全隐形** —— 球自己就是全部视觉。
      //   用户报了四轮"球外面还有一圈框/边线"，每轮我都去猜是哪一层留下的
      //   （壳背景？body 边框？handle？html 容器？），猜一层修一层，没有尽头。
      //   这里改成不问来源：dot 态下把壳这一整套装饰(背景/边框/阴影/描边)一次清零，
      //   任何一层想画点什么都画不出来。展开态不受影响。
      //   :has() 在 iOS 15.4+ 可用；万一不支持，退化成现状(有框)，不会更糟。
      '.rc-note:has(.vc-card.vc-dot),' +
      '.rc-note:has(.vc-card.vc-dot) .rc-note-body,' +
      '.rc-note:has(.vc-card.vc-dot) .rc-note-handle,' +
      '.rc-note:has(.vc-card.vc-dot) .rc-note-html' +
      '{background:transparent!important;background-image:none!important;border:none!important;' +
      'box-shadow:none!important;outline:none!important;backdrop-filter:none!important;' +
      '-webkit-backdrop-filter:none!important}',
      // 壳的尺寸也跟着球收 —— 否则壳还占着方块那么大一片，手指点旁边空白也算点中它。
      '.rc-note:has(.vc-card.vc-dot){width:auto!important;height:auto!important;min-width:0!important;min-height:0!important}',
      '.rc-note.rc-note-hashtml .rc-note-text,.rc-note.rc-note-hashtml .rc-note-tools,.rc-note.rc-note-hashtml .rc-note-ink{display:none!important}',
      '.rc-note.rc-note-hashtml.rc-note-collapsed .rc-note-html{display:none}',
      '.rc-note.rc-note-hasvideo .rc-vid-embed{border-radius:9px 9px 0 0;overflow:hidden}',
      '.rc-vc-rm{margin-left:auto;border:1px solid rgba(0,0,0,.2);background:rgba(255,255,255,.6);border-radius:5px;width:22px;height:20px;line-height:1;font-size:12px;cursor:pointer;color:#a33;padding:0}',
      // 暗底自动对比色(applyColor 按便签本色亮度 toggle .rc-note-darkbg):文字/placeholder/光标 →
      // 浅色系;handle 横杠/resize 角标/工具条底 同步翻浅,深色便签上操作件才可见
      '.rc-note.rc-note-darkbg .rc-note-text{color:' + FG_LIGHT + ';-webkit-text-fill-color:' + FG_LIGHT + ';caret-color:' + FG_LIGHT + '}',
      '.rc-note.rc-note-darkbg .rc-note-text::placeholder{color:rgba(242,245,249,.45)}',
      '.rc-note.rc-note-darkbg .rc-note-handle::after{background:rgba(255,255,255,.42)}',
      '.rc-note.rc-note-darkbg .rc-note-rs::after{border-right-color:rgba(255,255,255,.6);border-bottom-color:rgba(255,255,255,.6)}',
      '.rc-note.rc-note-darkbg .rc-note-rs-tl::after{border-left-color:rgba(255,255,255,.6);border-top-color:rgba(255,255,255,.6)}',
      '.rc-note.rc-note-darkbg .rc-note-tools{background:rgba(255,255,255,.12)}',
      /* ── 视频便签:有 video 时 body 改 flex 列(播放器 + 控件 + 文字备注),隐藏手写层 ── */
      '.rc-note-video{display:none}',
      '.rc-note.rc-note-hasvideo .rc-note-body{display:flex;flex-direction:column;padding:0}',
      // 视频区显式抬到手写层(z-index:2)与磨砂层之上,永不被白底/canvas 盖住(PDF zoom 祖先下 backdrop 失效时尤甚)
      '.rc-note.rc-note-hasvideo .rc-note-video{display:block;flex:none;position:relative;z-index:5;background:#000;border-radius:9px 9px 0 0;overflow:hidden}',
      // 手写层保留(用户要:视频便签也能画笔,只是不显示笔按钮)——canvas 铺满 body 叠在文字备注区上,视频 iframe 区会吞笔无妨
      '.rc-note.rc-note-hasvideo .rc-note-text{position:relative;left:auto;top:auto;width:100%;height:auto;flex:1;min-height:30px;font-size:13px;padding:6px 9px;background:transparent}',
      // aspect-ratio 放在图片/iframe(替换元素)上,而非容器——flex 布局(尤其 iOS Safari)里容器 aspect-ratio 常失效
      //   导致 height:100% 的图被便签比例拉变形。img/iframe 自己锁 16:9 → 任何便签宽高比都不变形,embed 高度跟随。
      '.rc-vid-embed{position:relative;width:100%;background:#000;cursor:pointer}',
      '.rc-vid-embed img{width:100%;height:auto;aspect-ratio:16/9;object-fit:cover;display:block}',
      '.rc-vid-no-thumb{width:100%;aspect-ratio:16/9;display:flex;align-items:center;justify-content:center;color:#7d8db0;font-size:11px;background:#10182b}',
      '.rc-vid-if{width:100%;height:auto;aspect-ratio:16/9;border:0;display:block}',
      '.rc-vid-go{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:42px;height:42px;border-radius:50%;border:none;background:rgba(0,0,0,.55);color:#fff;font-size:15px;cursor:pointer;padding-left:2px}',
      '.rc-vid-embed:hover .rc-vid-go{background:rgba(220,40,40,.92)}',
      '.rc-vid-ctrls{display:flex;flex-wrap:wrap;gap:4px 8px;align-items:center;padding:5px 8px;font-size:11.5px;color:#2a2a2a;background:rgba(255,255,255,.42);border-top:1px solid rgba(0,0,0,.08)}',
      '.rc-note-darkbg .rc-vid-ctrls{color:#e8eefc;background:rgba(0,0,0,.22);border-top-color:rgba(255,255,255,.12)}',
      '.rc-vid-ctrls label{display:inline-flex;align-items:center;gap:3px;white-space:nowrap}',
      '.rc-vid-ctrls .rc-vc-sm,.rc-vid-ctrls .rc-vc-ss,.rc-vid-ctrls .rc-vc-em,.rc-vid-ctrls .rc-vc-es{width:26px;text-align:center;border:1px solid rgba(0,0,0,.22);border-radius:4px;padding:2px 3px;font-size:11.5px;background:rgba(255,255,255,.75);color:#222}',
      '.rc-vid-ctrls .rc-vc-cn{margin:0 1px;opacity:.6}',
      '.rc-vc-now{margin-left:4px;border:1px solid rgba(0,0,0,.2);background:rgba(255,255,255,.6);border-radius:4px;width:22px;height:20px;line-height:1;font-size:11px;cursor:pointer;padding:0}',
      '.rc-note-darkbg .rc-vc-now{background:rgba(255,255,255,.14);border-color:rgba(255,255,255,.24)}',
      '.rc-vid-ctrls select{border:1px solid rgba(0,0,0,.22);border-radius:4px;padding:1px 3px;font-size:11.5px;background:rgba(255,255,255,.75);color:#222}',
      '.rc-note-darkbg .rc-vid-ctrls .rc-vc-sm,.rc-note-darkbg .rc-vid-ctrls .rc-vc-ss,.rc-note-darkbg .rc-vid-ctrls .rc-vc-em,.rc-note-darkbg .rc-vid-ctrls .rc-vc-es,.rc-note-darkbg .rc-vid-ctrls select{background:rgba(255,255,255,.14);color:#eef;border-color:rgba(255,255,255,.24)}',
      '.rc-vc-ck input{margin:0 2px 0 0}'
    ].join('\n');
    document.head.appendChild(css);
  }

  // ─────────────────────────── 唯一 I/O 边界 ───────────────────────────
  function repoMode() { return !!(O && O.repository); }
  function repoReady() {
    if (!repoMode()) return false;
    var r = O.repository;
    return !!(
      O.documentId &&
      typeof r.newId === 'function' &&
      typeof r.list === 'function' &&
      typeof r.get === 'function' &&
      typeof r.create === 'function' &&
      typeof r.patch === 'function' &&
      typeof r.remove === 'function' &&
      typeof r.subscribe === 'function'
    );
  }
  function staleError() {
    var e = new Error('便签页面身份已经变化');
    e.code = 'BW_NOTE_STALE_GENERATION';
    return e;
  }
  function ioError(error, prefix) {
    if (error && error.code === 'BW_NOTE_STALE_GENERATION') return;
    var detail = String(error && error.message || error || '').trim();
    toastMsg('✗ ' + (prefix || '便签保存失败') + (detail ? '：' + detail : ''));
  }
  function cloneValue(value) {
    if (value == null) return value;
    try { return JSON.parse(JSON.stringify(value)); } catch (_) { return value; }
  }
  function mutationId(kind, noteId) {
    var random = '';
    try {
      if (window.crypto && typeof window.crypto.randomUUID === 'function') random = window.crypto.randomUUID();
    } catch (_) {}
    if (!random) random = Date.now().toString(36) + '-' + (++_mutationSeq).toString(36);
    return 'rc-note:' + kind + ':' + String(noteId || 'new') + ':' + random;
  }
  function noteIdOf(note) { return String(note && (note.noteId || note.id) || ''); }
  function noteIndex(noteId) {
    for (var i = 0; i < notes.length; i++) if (noteIdOf(notes[i]) === noteId) return i;
    return -1;
  }
  function currentNote(noteId) {
    var index = noteIndex(noteId);
    return index >= 0 ? notes[index] : null;
  }
  function removeLocal(noteId) {
    var ctl = ctls[noteId];
    if (ctl) {
      try { if (EDIT && EDIT.ctl === ctl) exitEdit(); } catch (_) {}
      // 词锚的描边和序号在另一个层里,光删便签自己的 DOM 不够 ——
      // 不撤会永远留在页上,而且**后面所有序号都错位**(序号是位置不是身份)。
      try {
        var _b = ctl.note && ctl.note.card && ctl.note.card.bind;
        if (_b && window.__pageBindRemove) window.__pageBindRemove(_b);
      } catch (_) {}
      try { ctl.root.remove(); } catch (_) {}
      try { if (ctl._ph) ctl._ph.remove(); } catch (_) {}
      delete ctls[noteId];
    }
    var index = noteIndex(noteId);
    if (index >= 0) notes.splice(index, 1);
  }
  // LIST、CHANGE 与本地操作 RESULT 共用同一增量投影。相同 rev 是同一次写的
  // 重放，不重建 DOM；更旧 rev 永远不能覆盖。删除 CHANGE 即使先于旧 LIST
  // 到达，也由 _seenRevs 留住 tombstone 的版本栅栏。
  function upsertRecord(note, generation) {
    if (generation !== _generation || !note) return null;
    var id = noteIdOf(note), rev = Number(note.rev) || 0;
    if (!id) return null;
    var seen = Number(_seenRevs[id]) || 0;
    if (rev < seen) return currentNote(id);
    if (note.deleted === true) {
      _seenRevs[id] = Math.max(seen, rev);
      removeLocal(id);
      return null;
    }
    var index = noteIndex(id), existing = index >= 0 ? notes[index] : null;
    var existingRev = Number(existing && existing.rev) || 0;
    if (existing && rev <= existingRev) {
      _seenRevs[id] = Math.max(seen, existingRev);
      return existing;
    }
    if (!existing && rev < seen) return null;
    var next = cloneValue(note);
    next.id = id; next.noteId = id;
    _seenRevs[id] = Math.max(seen, rev);
    if (index < 0) notes.push(next);
    else notes[index] = next;
    var ctl = ctls[id];
    if (ctl) {
      ctl.note = next;
      syncCtl(ctl);
      ensureMounted(next);
    } else {
      ensureMounted(next);
    }
    return next;
  }
  function eventData(event) {
    if (!event) return null;
    if (event.data && typeof event.data === 'object') return event.data;
    return event;
  }
  function onRepositoryChange(event, generation) {
    if (generation !== _generation) return;
    var data = eventData(event);
    if (!data) return;
    if (data.error) {
      ioError(data.error, '便签同步失败');
      return;
    }
    var note = data.note || data.record || null;
    if (!note) return;
    if (note.documentId && note.documentId !== O.documentId) return;
    upsertRecord(note, generation);
  }
  function enqueueWrite(noteId, generation, work) {
    var key = String(noteId || '');
    var previous = _writeQueues[key] || Promise.resolve();
    var operation = previous.catch(function () {}).then(function () {
      if (generation !== _generation) throw staleError();
      return work();
    });
    _writeQueues[key] = operation;
    operation.then(function () {
      if (_writeQueues[key] === operation) delete _writeQueues[key];
    }, function () {
      if (_writeQueues[key] === operation) delete _writeQueues[key];
    });
    return operation;
  }
  function legacyJson(method, body) {
    return fetch(API, {
      method: method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (response) {
      if (!response.ok) throw new Error('HTTP ' + response.status);
      return response.json();
    });
  }
  function ioList(generation) {
    if (repoMode()) {
      if (!repoReady()) return Promise.reject(new Error('便签 repository 合同不完整'));
      var all = [], offset = 0, pageSize = 200, pageCount = 0;
      var nextPage = function () {
        return Promise.resolve(O.repository.list({
          includeDeleted: false,
          offset: offset,
          limit: pageSize
        })).then(function (result) {
          if (generation !== _generation) throw staleError();
          var items = Array.isArray(result) ? result :
            (result && Array.isArray(result.notes) ? result.notes : []);
          for (var i = 0; i < items.length; i++) all.push(items[i]);
          pageCount += 1;
          if (items.length < pageSize) return all;
          // 防损坏 facade 恒返同页造成无限循环；正常仓库远低于此上限。
          if (pageCount >= 100) throw new Error('便签分页超过 20000 条安全上限');
          offset += items.length;
          return nextPage();
        });
      };
      return nextPage();
    }
    if (!O || !O.file) return Promise.resolve([]);
    return fetch(API + '?file=' + encodeURIComponent(O.file)).then(function (response) {
      if (!response.ok) throw new Error('HTTP ' + response.status);
      return response.json();
    }).then(function (data) {
      if (!data || !data.ok) throw new Error('便签列表响应无效');
      return data.notes || [];
    });
  }
  function ioCreate(fields, generation) {
    if (repoMode()) {
      if (!repoReady()) return Promise.reject(new Error('便签 repository 合同不完整'));
      return Promise.resolve(O.repository.newId()).then(function (noteId) {
        if (generation !== _generation) throw staleError();
        var input = { documentId: O.documentId, noteId: noteId };
        for (var key in fields) if (Object.prototype.hasOwnProperty.call(fields, key)) input[key] = cloneValue(fields[key]);
        var mid = mutationId('create', noteId);
        return O.repository.create(input, { mutationId: mid });
      });
    }
    var body = { file: O.file };
    for (var key in fields) if (Object.prototype.hasOwnProperty.call(fields, key)) body[key] = fields[key];
    return legacyJson('POST', body).then(function (data) {
      if (!data || !data.ok || !data.note) throw new Error('便签创建响应无效');
      return data.note;
    });
  }
  function patchNote(note, fields, cb) {
    if (!O || !note || !noteIdOf(note)) return Promise.resolve(null);
    var generation = _generation;
    var id = noteIdOf(note);
    var payload = cloneValue(fields || {});
    var mid = mutationId('patch', id);   // 入队时生成，重放/重试始终同一个 ID
    var operation;
    if (repoMode()) {
      operation = enqueueWrite(id, generation, function () {
        if (!repoReady()) throw new Error('便签 repository 合同不完整');
        var latest = currentNote(id);
        if (!latest) throw new Error('便签已经不存在');
        return O.repository.patch(id, payload, {
          ifRev: Number(latest.rev) || 0,
          mutationId: mid
        });
      }).then(function (result) {
        return upsertRecord(result, generation) || currentNote(id);
      });
    } else {
      var body = { file: O.file, id: id };
      for (var key in payload) if (Object.prototype.hasOwnProperty.call(payload, key)) body[key] = payload[key];
      operation = legacyJson('PATCH', body).then(function (data) {
        if (data && data.note) return data.note;
        return note;
      });
    }
    operation.then(function (result) {
      if (cb) cb({ ok: true, note: result });
    }).catch(function (error) {
      ioError(error, '便签保存失败');
      if (cb) cb(null);
    });
    return operation;
  }
  function deleteNote(note, successMessage) {
    if (!O || !note || !noteIdOf(note)) return Promise.resolve(false);
    var generation = _generation;
    var id = noteIdOf(note);
    var mid = mutationId('remove', id);
    var operation;
    if (repoMode()) {
      operation = enqueueWrite(id, generation, function () {
        if (!repoReady()) throw new Error('便签 repository 合同不完整');
        var latest = currentNote(id);
        if (!latest) throw new Error('便签已经不存在');
        return O.repository.remove(id, {
          ifRev: Number(latest.rev) || 0,
          mutationId: mid
        });
      }).then(function (result) {
        upsertRecord(result, generation);
        return true;
      });
    } else {
      // @interaction document.note.remove
      operation = fetch(
        API + '?file=' + encodeURIComponent(O.file) + '&id=' + encodeURIComponent(id),
        { method: 'DELETE' }
      ).then(function (response) {
        if (!response.ok) throw new Error('HTTP ' + response.status);
        removeLocal(id);
        return true;
      });
    }
    operation.then(function () {
      if (successMessage) toastMsg(successMessage);
    }).catch(function (error) {
      ioError(error, '便签删除失败');
    });
    return operation;
  }

  // ─────────────────────────── 笔画绘制/命中 = 共享核心 rc-ink.js(RCInk)───────────────────────────
  // 便签笔画字段 pts、无 t(RCInk 按 pen 处理);默认色/宽经 defs 跟随用户当前 INK 选择,行为与原实现一致。
  function drawStroke(ctx, s, W, H, dpr) {
    RCInk.drawStroke(ctx, s, W, H, dpr, { color: INK.color, width: INK.width });
  }
  // 保持手写宽高比(iar=首次落笔时的便签 w/h)的内接绘制区(letterbox):便签任意 resize 笔画不变形。
  // 存量便签无 iar → 退回整幅(老行为)。W/H 单位随调用方(canvas 像素 或 CSS 像素)。
  function inkBox(iar, W, H) {
    if (!iar || iar <= 0) return { ox: 0, oy: 0, w: W, h: H };
    var car = W / H, w = W, h = H, ox = 0, oy = 0;
    if (car > iar) { w = H * iar; ox = (W - w) / 2; }   // 容器更宽 → 左右留白
    else { h = W / iar; oy = (H - h) / 2; }             // 容器更高 → 上下留白
    return { ox: ox, oy: oy, w: w, h: h };
  }
  function redrawInk(ctl) {
    var cv = ctl.cv, ctx = cv.getContext('2d');
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, cv.width, cv.height);
    var arr = ctl.note.strokes || [], dpr = window.devicePixelRatio || 1;
    var b = inkBox(ctl.note.iar, cv.width, cv.height);
    ctx.save(); ctx.translate(b.ox, b.oy);
    for (var i = 0; i < arr.length; i++) drawStroke(ctx, arr[i], b.w, b.h, dpr);
    ctx.restore();
    try { ctl._strokeSig = JSON.stringify(arr); } catch (_) { ctl._strokeSig = ''; }
  }
  function strokeHit(s, pt, thr) { return RCInk.hit(s, pt, thr); }

  // ─────────────────────────── controller 构建 ───────────────────────────
  function buildCtl(note) {
    var root = document.createElement('div');
    root.className = 'rc-note';
    root.dataset.noteId = note.id;
    root.innerHTML =
      '<div class="rc-note-handle"></div>' +
      '<div class="rc-note-body">' +
        '<div class="rc-note-video"></div>' +
        '<div class="rc-note-card"></div>' +
        '<div class="rc-note-html"></div>' +
        '<textarea class="rc-note-text" placeholder="输入文字…(笔=手写)"></textarea>' +
        '<canvas class="rc-note-ink"></canvas>' +
        '<div class="rc-note-tools"></div>' +
        '<div class="rc-note-rs" title="拖动调整大小"></div>' +
        '<div class="rc-note-rs-tl" title="拖动调整大小(右下角固定)"></div>' +
      '</div>' +
      // 删除键挂 root(不在小把手内)→ 定位在便签**整体**右上角外部
      '<button class="rc-note-del" title="删除便签"><svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></button>';
    var ctl = {
      note: note, root: root,
      handle: root.querySelector('.rc-note-handle'),
      del: root.querySelector('.rc-note-del'),
      body: root.querySelector('.rc-note-body'),
      ta: root.querySelector('.rc-note-text'),
      cv: root.querySelector('.rc-note-ink'),
      video: root.querySelector('.rc-note-video'),
      card: root.querySelector('.rc-note-card'),
      html: root.querySelector('.rc-note-html'),
      tools: root.querySelector('.rc-note-tools'),
      rs: root.querySelector('.rc-note-rs'),
      rsTL: root.querySelector('.rc-note-rs-tl'),
      _tapT: 0, _tapCount: 0, _tapPt: null, _toggleT: null,
      _downPt: null, _suppressTap: 0, _strokeT: null, _strokeDirty: false
    };
    buildTools(ctl);
    bindCtl(ctl);
    return ctl;
  }
  function buildTools(ctl) {
    var h = '';
    for (var i = 0; i < COLORS.length; i++) {
      h += '<button class="rc-note-swatch" data-c="' + COLORS[i].c + '" style="background:' + COLORS[i].c + '" title="' + COLORS[i].n + '"></button>';
    }
    h += '<button class="rc-note-tool" data-t="eraser" title="橡皮(点/划笔画删除整条;手指快速双击也可临时切换)">✒️</button>';
    h += '<button class="rc-note-tool" data-t="video" title="加入/编辑视频(粘贴 YouTube 链接)">🎬</button>';
    ctl.tools.innerHTML = h;
    ctl.tools.addEventListener('click', function (e) {
      var b = e.target && e.target.closest ? e.target.closest('button') : null; if (!b) return;
      e.stopPropagation(); e.preventDefault();
      if (b.dataset.c) setColor(ctl, b.dataset.c);
      else if (b.dataset.t === 'eraser') setTool(ctl, NINK.tool === 'eraser' ? 'pen' : 'eraser', false);
      else if (b.dataset.t === 'video') promptAddVideo(ctl);
    });
  }

  // ─────────────────────────── 视频便签(YouTube 嵌入 + 起止/速度/循环/字幕控件)───────────────────────────
  function ytIdOf(s) {
    s = String(s || '').trim();
    var m = s.match(/(?:youtu\.be\/|youtube(?:-nocookie)?\.com\/(?:watch\?v=|embed\/|v\/|shorts\/|live\/))([A-Za-z0-9_-]{11})/) || s.match(/^([A-Za-z0-9_-]{11})$/);
    return m ? m[1] : '';
  }
  // 来源识别:显式 src 优先(拖放时存);无 src(旧便签)按 bvid 兜底(BV+10=12 字符,不误判 11 位 YT id)。
  function _isBili(v) { return !!(v && (v.src ? v.src === 'bili' : /^BV[0-9A-Za-z]{10}/.test(v.id || ''))); }
  function _ytThumb(id) { return 'https://i.ytimg.com/vi/' + id + '/mqdefault.jpg'; }   // 仅 YT 用;B站缩略图无 id→URL 规律,须存 v.thumb
  function _videoThumbURL(v) {
    var raw = String((v && v.thumb) || (!_isBili(v) && v && v.id ? _ytThumb(v.id) : '')).trim();
    if (!raw) return '';
    try {
      var parsed = new URL(raw);
      if (parsed.protocol !== 'https:' || !parsed.hostname || parsed.username || parsed.password || parsed.hash) return '';
      return '/pdf/api/img-proxy?url=' + encodeURIComponent(parsed.href);
    } catch (e) { return ''; }
  }
  function vEmbedSrc(v) {
    if (_isBili(v)) {
      var bp = ['bvid=' + encodeURIComponent(v.id), 'autoplay=1', 'danmaku=0', 'high_quality=1', 'p=1'];
      if (v.start) bp.push('t=' + Math.max(0, v.start | 0));
      return 'https://player.bilibili.com/player.html?' + bp.join('&');
    }
    var p = ['enablejsapi=1', 'playsinline=1', 'rel=0', 'autoplay=1', 'cc_lang_pref=zh-Hans'];
    if (v.start) p.push('start=' + Math.max(0, v.start | 0));
    if (v.end) p.push('end=' + Math.max(0, v.end | 0));
    if (v.loop) { p.push('loop=1'); p.push('playlist=' + v.id); }
    p.push('cc_load_policy=' + (v.cc !== false ? 1 : 0));
    return 'https://www.youtube-nocookie.com/embed/' + v.id + '?' + p.join('&');
  }
  function secToMMSS(s) { s = Math.max(0, s | 0); return Math.floor(s / 60) + ':' + ('0' + (s % 60)).slice(-2); }
  function mmssToSec(str) {
    str = String(str || '').trim(); if (!str) return 0;
    if (/^\d+$/.test(str)) return parseInt(str, 10);
    var m = str.match(/^(\d+):(\d{1,2})$/); return m ? (parseInt(m[1], 10) * 60 + parseInt(m[2], 10)) : 0;
  }
  function setRate(ctl, r) {
    // enablejsapi=1 后经 postMessage 设倍速(YT player ready 才响应 → 载入后多次尝试)
    var f = ctl.__vif; if (!f || !f.contentWindow) return;
    [200, 700, 1500].forEach(function (d) {
      setTimeout(function () { try { f.contentWindow.postMessage(JSON.stringify({ event: 'command', func: 'setPlaybackRate', args: [r] }), '*'); } catch (e) {} }, d);
    });
  }
  // YouTube iframe(enablejsapi=1)收到 {event:listening} 后周期推 infoDelivery,含 currentTime → 存到对应便签 ctl.__vcur(供「⏱ 设为当前」读)。
  function _hookVidMsg() {
    if (window.__rcVidMsg) return; window.__rcVidMsg = 1;
    window.addEventListener('message', function (e) {
      if (typeof e.data !== 'string' || e.data.indexOf('"infoDelivery"') < 0) return;
      var d; try { d = JSON.parse(e.data); } catch (_) { return; }
      if (!d || d.event !== 'infoDelivery' || !d.info || typeof d.info.currentTime !== 'number') return;
      for (var id in ctls) { var c = ctls[id]; if (c && c.__vif && c.__vif.contentWindow === e.source) { c.__vcur = d.info.currentTime; break; } }
    });
  }
  function _removeNoteVideo(ctl) {   // 移除视频(从浮层 🗑 或旧入口调):便签只有视频→删整张;否则变回普通便签
    var onlyVideo = !(ctl.note.text || '').trim() && !(ctl.note.strokes && ctl.note.strokes.length);
    if (onlyVideo) {
      if (!window.confirm('这张便签只有视频,移除视频将删除整张便签,确定?')) return;
      deleteNote(ctl.note, '🗑 视频便签已删除');
      return;
    }
    if (!window.confirm('移除这个视频?(便签变回普通便签,保留文字/手写)')) return;
    ctl.note.video = null; patchNote(ctl.note, { video: null }); renderNoteVideo(ctl);
  }
  function renderNoteVideo(ctl) {
    var v = ctl.note.video, box = ctl.video; if (!box) return;
    if (!v || !v.id) { ctl.root.classList.remove('rc-note-hasvideo'); box.innerHTML = ''; box.__sig = ''; ctl.__vif = null; return; }
    ctl.root.classList.add('rc-note-hasvideo');
    var sig = JSON.stringify(v);
    if (box.__sig === sig) return;   // 无变化不重建
    box.__sig = sig; ctl.__vif = null;
    // 便签视频区 = 纯缩略图 + ▶(点开共享浮层播放器 RC.videoPlayer;起止/循环/倍速/字幕等控制都在浮层里,内联控制条已退役)
    // 缩略图:B站存 v.thumb(bvid 无 URL 规律);YT 用 v.id 拼 ytimg。播放/兜底 URL 按来源分流。
    var _bili = _isBili(v);
    var _thumb = _videoThumbURL(v);
    box.innerHTML = '<div class="rc-vid-embed">' + (_thumb ? '<img loading="lazy" referrerpolicy="same-origin" src="' + _thumb + '" alt="">' : '<span class="rc-vid-no-thumb">无预览图</span>') + '<button class="rc-vid-go" aria-label="播放">▶</button></div>';
    var openPlayer = function () {
      if (!(window.RC && RC.videoPlayer)) { window.open((_bili ? 'https://www.bilibili.com/video/' : 'https://www.youtube.com/watch?v=') + encodeURIComponent(v.id), '_blank'); return; }
      RC.videoPlayer.open({
        id: v.id, src: (_bili ? 'bili' : 'yt'), thumb: v.thumb, start: v.start, end: v.end, loop: v.loop, rate: v.rate, cc: v.cc,
        title: ((ctl.note.text || '').trim().slice(0, 40)) || '视频便签',
        noteId: ctl.note.id,
        onChange: function (nv) {   // 浮层改起止/循环/倍速/字幕 → 回写 note.video(签名同步防 syncCtl 回灌重建)
          for (var k in nv) v[k] = nv[k];
          ctl.note.video = v; box.__sig = JSON.stringify(v);
          patchNote(ctl.note, { video: v });
        },
        onRemove: function () { _removeNoteVideo(ctl); },
      });
    };
    var _thumbImage = box.querySelector('.rc-vid-embed img');
    if (_thumbImage) _thumbImage.addEventListener('error', function () {
      _thumbImage.style.display = 'none';
      if (!box.querySelector('.rc-vid-no-thumb')) {
        var empty = document.createElement('span'); empty.className = 'rc-vid-no-thumb'; empty.textContent = '封面加载失败';
        box.querySelector('.rc-vid-embed').insertBefore(empty, box.querySelector('.rc-vid-go'));
      }
    });
    box.querySelector('.rc-vid-embed').addEventListener('click', function (e) { e.stopPropagation(); openPlayer(); });
  }
  function cardContextText(cards) {
    // AI 文本与结构化元数据必须来自同一份 live card snapshot。文本只做可读投影；
    // 完整字段（含 Anki/source/entity/state）由 bindCardSelection 的 meta.cards 保留。
    return (Array.isArray(cards) ? cards : []).map(function (c, index) {
      c = c || {};
      var front = c.front != null ? c.front : (c.q != null ? c.q : (c.cloze != null ? c.cloze : c.text));
      var back = c.back != null ? c.back : c.a;
      var lines = ['卡片 ' + (index + 1)];
      if (front != null && String(front).trim()) lines.push('正面：' + String(front));
      if (back != null && String(back).trim()) lines.push('背面：' + String(back));
      return lines.join('\n');
    }).join('\n\n');
  }
  function resolveCardSnapshot(cardsOrGetter) {
    var cards = cardsOrGetter;
    try {
      if (typeof cardsOrGetter === 'function') cards = cardsOrGetter();
    } catch (_) {
      cards = [];
    }
    return Array.isArray(cards) ? cards : [];
  }
  function bindCardSelection(el, cardsOrGetter, gid, hostKind) {
    gid = String(gid || '');
    if (!el || !gid || !resolveCardSnapshot(cardsOrGetter).length ||
        !(window.RC && RC.voiceCard && RC.voiceCard.pinBind)) return false;
    // placement 自己有 noteId/wp_*，但上下文身份只能使用业务卡 gid；
    // 同一张卡在侧栏、收藏、PWA 页和普通网页页因此永远命中 card:<gid>。
    try { if (RC.voiceCard.pinReg) RC.voiceCard.pinReg(el, gid); } catch (e) {}
    // owner 仍是整张卡（稳定 cid 与处处高亮），但长按监听只挂展开正文。
    // 正面的 reveal 是可短点翻面的正文，不再用 [data-fc] 围栏挡掉长按；
    // pinBind 自己排除真正的 button/link/input，并在长按成功后吞掉随后 click。
    var pressTarget = null;
    try {
      pressTarget = el.querySelector && (
        el.querySelector('.vc-card-bd') ||
        el.querySelector('.fc-track')
      );
    } catch (e2) {}
    pressTarget = pressTarget || el;
    var selectionSpec = {
      id: 'card:' + gid,
      kind: 'card',
      source: { cid: gid, gid: gid },
      meta: {
        contract: 'anki-card-context/1',
        host: String(hostKind || 'page-placement'),
        cards: []
      }
    };
    RC.voiceCard.pinBind(el, '学习卡片', function () {
      // mountState 会复制 placement 输入；每次真正选中前重新读取 body.__fc.cards，
      // 让评分/离线队列/回滚后的 live 状态进入文本和完整结构化快照。
      var live = resolveCardSnapshot(cardsOrGetter);
      selectionSpec.meta.cards = live;
      return cardContextText(live);
    }, selectionSpec, pressTarget);
    return true;
  }
  function resolveHtmlSnapshot(htmlOrGetter) {
    var value = htmlOrGetter;
    try {
      if (typeof htmlOrGetter === 'function') value = htmlOrGetter();
    } catch (_) {
      value = null;
    }
    return value && typeof value === 'object' ? value : {};
  }
  function htmlCardContextText(el, html) {
    var bodyText = '';
    try {
      var body = el && el.querySelector && (
        el.querySelector('.vc-card-bd') || el
      );
      bodyText = String(body && body.textContent || '')
        .replace(/\s+/g, ' ').trim();
    } catch (_) {}
    if (bodyText) return bodyText;
    var content = String(html && html.content || '');
    if (!html || !html.isHtml) return content;
    try {
      var tmp = document.createElement('div');
      tmp.innerHTML = content;
      return String(tmp.textContent || '').replace(/\s+/g, ' ').trim();
    } catch (_) {
      return content.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
    }
  }
  function bindHtmlCardSelection(el, htmlOrGetter, cid, hostKind) {
    cid = String(cid || '');
    if (!el || !cid ||
        !(window.RC && RC.voiceCard && RC.voiceCard.pinBind)) return false;
    try { if (RC.voiceCard.pinReg) RC.voiceCard.pinReg(el, cid); } catch (e) {}
    var pressTarget = null;
    try {
      pressTarget = el.querySelector && el.querySelector('.vc-card-bd');
    } catch (_) {}
    pressTarget = pressTarget || el;
    var selectionSpec = {
      id: 'card:' + cid,
      kind: 'card',
      source: { cid: cid },
      meta: {
        contract: 'tool-card-context/1',
        host: String(hostKind || 'page-placement'),
        card: {}
      }
    };
    RC.voiceCard.pinBind(el, '工具卡片', function () {
      var live = resolveHtmlSnapshot(htmlOrGetter);
      selectionSpec.source.cid = cid;
      selectionSpec.source.tool = String(live.tool || live.kind || '');
      try { selectionSpec.meta.card = cloneValue(live); }
      catch (_) { selectionSpec.meta.card = {}; }
      return htmlCardContextText(el, live);
    }, selectionSpec, pressTarget);
    return true;
  }
  function renderNoteCard(ctl) {
    var card = ctl.note.card, box = ctl.card; if (!box) return;
    if (!card || !card.cards || !card.cards.length || !(window.RC && RC.flashcard && RC.flashcard.mountState)) {
      if (ctl.root.classList.contains('rc-note-hascard')) { ctl.root.classList.remove('rc-note-hascard'); try { ctl.cv.style.display = ''; } catch (e) {} }
      box.innerHTML = ''; box.__sig = ''; return;
    }
    var _idDirty = false;
    if (!card.gid) { card.gid = card.cid || ('fcg_' + ((window.RC && RC.voiceCard && RC.voiceCard.mkCid) ? RC.voiceCard.mkCid() : Date.now().toString(36))); _idDirty = true; }
    if (card.cid !== card.gid) { card.cid = card.gid; _idDirty = true; }   // 学习状态主键和外壳主键必须是同一号
    if (_idDirty) { ctl.note.card = card; patchNote(ctl.note, { card: card }); }   // 存量卡一次性补稳定编号
    ctl.root.classList.add('rc-note-hascard');
    try { ctl.cv.style.display = 'none'; ctl.body.style.background = 'transparent'; ctl.body.style.backdropFilter = ''; ctl.body.style.webkitBackdropFilter = ''; ctl.handle.style.background = 'transparent'; } catch (e) {}   // 便签壳透明(卡自己有玻璃);ink canvas 内联白块
    var sig = JSON.stringify(card, function (k, v) { return k === 'form' ? undefined : v; });   // form 变不重建(收纳循环别丢卡状态)
    try { ctl.body.style.height = 'auto'; ctl.body.style.width = _formW(ctl, card.form); } catch (e) {}   // 壳跟卡形态收缩(否则收成标记后壳还横在页上挡内容)
    if (box.__sig === sig) return;   // 无变化不重建(防 syncCtl 反复重挂丢状态)
    box.__sig = sig; box.innerHTML = '';
    // 直接复用字幕浮层卡渲染代码(用户拍板):RC.voiceCard.renderInto = _cardPush 同一段 _cardDom
    var done = false, cardEl = null, stateBody = null;
    var onPlacementStateChange = function (snapshot, reason) {
      // review-pending 也必须立即落盘：请求结果未知时刷新若恢复成未答，
      // 用户会生成新 aid 再评一次。accepted/queued/reverted 再用后续快照覆盖。
      if (!Array.isArray(snapshot) || !snapshot.length) return;
      card.cards = cloneValue(snapshot);
      ctl.note.card = card;
      patchNote(ctl.note, { card: card });
    };
    try {
      if (window.RC && RC.voiceCard && RC.voiceCard.renderInto)
        cardEl = RC.voiceCard.renderInto(box, { text: null, label: '🎴 卡片' + (card.cards.length > 1 ? '×' + card.cards.length : ''), isHtml: false, type: card.type || '#b9a8ff', icon: '🎴', form: card.form, cid: card.cid || card.gid,
          onSize: function (size) {
            ctl._cardPresentationSize = size || null;
            try { ctl.body.style.width = _formW(ctl, card.form); } catch (_) {}
          },
          mount: function (bd) {
            stateBody = bd;
            RC.flashcard.mountState(bd, card.cards, { bare: true, gid: card.gid, nopin: true, onStateChange: onPlacementStateChange });
            if (card.gid && /^card_/.test(card.gid)) {   // 统一编号协议:全局编号 → 服务端 states 还原(跨会话"一张卡一种状态";先渲快照防闪空)
              fetch('/pdf/api/entity/' + card.gid).then(function (r) { return r.json(); }).then(function (d) {
                if (!(d && d.ok && d.kind === 'cards' && (d.cards || []).length)) return;
                var merged = d.cards.map(function (c0, i2) { return Object.assign({}, c0, (d.states || {})[String(i2)] || {}); });
                var fc = bd.__fc;
                if (!fc || !fc.cards) return;
                if (JSON.stringify(merged.map(function (c) { return c._st; })) === JSON.stringify(fc.cards.map(function (c) { return c._st; }))) return;   // 状态一致不重挂
                merged.forEach(function (mc, i2) {   // ⚠ 共享对象**就地更新**(rc-flashcard register 复用同 gid 共享数组,换引用会被换回旧的)
                  var cc = fc.cards[i2]; if (!cc) return;
                  ['_st', '_nid', '_next', '_showBack'].forEach(function (k) { if (mc[k] !== undefined) cc[k] = mc[k]; });
                });
                RC.flashcard.mountState(bd, fc.cards, { bare: true, gid: card.gid, nopin: true, onStateChange: onPlacementStateChange });   // 重挂渲新状态(register 复用已更新的共享对象)
              }).catch(function () {});
            }
          },
          onClose: function () { try { ctl.del.click(); } catch (e) {} },
          onForm: function (f) { try { card.form = f; ctl.note.card = card; ctl.body.style.width = _formW(ctl, f); patchNote(ctl.note, { card: card }); } catch (e) {} } });
      done = !!cardEl;
    } catch (e) {}
    if (!done) { try { stateBody = box; RC.flashcard.mountState(box, card.cards, { bare: true, gid: card.gid, nopin: true, onStateChange: onPlacementStateChange }); } catch (e) {} }   // voiceCard 未载兜底
    // 卡头由 rc-stickynote 的透明 handle 独占蓄力拖动；展开正文才负责整卡长按选中。
    // 真正的按钮/链接由 pinBind 自身排除，正面 reveal 则保留“短点翻面、长按选中”。
    bindCardSelection(cardEl || box.querySelector('.fc-wrap') || box, function () {
      return (stateBody && stateBody.__fc && stateBody.__fc.cards) || card.cards;
    }, card.gid, 'pwa-page-placement');
  }
  function renderNoteHtml(ctl) {
    var h = ctl.note.html, box = ctl.html; if (!box) return;
    if (!h || !h.content) { if (ctl.root.classList.contains('rc-note-hashtml')) { ctl.root.classList.remove('rc-note-hashtml'); try { ctl.cv.style.display = ''; } catch (e) {} } box.innerHTML = ''; box.__sig = ''; return; }
    if (!h.cid) { h.cid = (window.RC && RC.voiceCard && RC.voiceCard.mkCid) ? RC.voiceCard.mkCid() : ('c' + Date.now().toString(36)); ctl.note.html = h; patchNote(ctl.note, { html: h }); }   // 存量 HTML 卡补号后立即持久化
    ctl.root.classList.add('rc-note-hashtml');
    try { ctl.cv.style.display = 'none'; ctl.body.style.background = 'transparent'; ctl.body.style.backdropFilter = ''; ctl.body.style.webkitBackdropFilter = ''; ctl.handle.style.background = 'transparent'; } catch (e) {}   // 便签壳透明(卡自己有玻璃)
    var sig = JSON.stringify(h, function (k, v) { return k === 'form' ? undefined : v; });   // form 变不重建
    try { ctl.body.style.height = 'auto'; ctl.body.style.width = _formW(ctl, h.form); } catch (e) {}   // 壳跟卡形态收缩
    if (box.__sig === sig) {
      bindHtmlCardSelection(
        box.querySelector('.vc-card') || box,
        function () { return ctl.note.html || h; },
        h.cid,
        'pwa-page-placement'
      );
      return;
    }
    box.__sig = sig; box.innerHTML = '';
    // 直接复用字幕浮层卡渲染代码(用户拍板):同一 _cardDom → 卡头/排版/样式与浮层卡永远一致;
    //   content 自带 vc-if-hd 标题条时 renderInto 会剥掉(否则双标题)
    var done = false, el2 = null;
    try {
      if (window.RC && RC.voiceCard && RC.voiceCard.renderInto)
        el2 = RC.voiceCard.renderInto(box, { text: h.content, label: h.label || '卡片', isHtml: !!h.isHtml, type: h.type, icon: h.icon, form: h.form, cid: h.cid,
          onSize: function (size) {
            ctl._cardPresentationSize = size || null;
            try {
              ctl.body.style.width = _formW(ctl, h.form);
              // 装的是卡片 → 壳不限高(卡片自带 max-height 与滚动);inline 值要清掉,
              // 否则上一次普通便签留下的 maxHeight 会继续裁着卡片。
              ctl.html.style.maxHeight = '';
            } catch (_) {}
          },
          onClose: function () { try { ctl.del.click(); } catch (e) {} },
          onForm: function (f) { try { h.form = f; ctl.note.html = h; ctl.body.style.width = _formW(ctl, f); patchNote(ctl.note, { html: h }); } catch (e) {} } });
      done = !!el2;
      // 成功装进真 .vc-card 才打这个标记:回退分支(下面的 fb)仍是普通 HTML,
      // 那时壳的 padding/限高/滚动照旧需要。
      try { box.classList.toggle('rc-note-html-card', done); } catch (e2) {}
    } catch (e) {}
    if (!done) { var fb = document.createElement('div'); if (h.isHtml) fb.innerHTML = h.content; else fb.textContent = h.content; box.appendChild(fb); }
    bindHtmlCardSelection(
      el2 || box.querySelector('.vc-card') || box,
      function () { return ctl.note.html || h; },
      h.cid,
      'pwa-page-placement'
    );
    try { window.RC && RC.typeset && RC.typeset(box); } catch (e) {}
  }
  function setNoteVideo(ctl, id) {   // 供拖放/入口共用:给便签设视频(保留已有起止等设置)
    var v = (ctl.note.video && ctl.note.video.id === id) ? ctl.note.video : { id: id, start: 0, end: 0, rate: 1, loop: false, cc: true };
    v.id = id; ctl.note.video = v;
    if (ctl.note.collapsed) { ctl.note.collapsed = false; ctl.root.classList.remove('rc-note-collapsed'); patchNote(ctl.note, { collapsed: false }); }
    patchNote(ctl.note, { video: v });
    renderNoteVideo(ctl);
  }
  window.__rcNoteSetVideo = function (noteId, id) { var c = ctls[noteId]; if (c) setNoteVideo(c, id); };   // 拖放(阶段B)用
  function promptAddVideo(ctl) {
    var cur = ctl.note.video && ctl.note.video.id;
    var url = window.prompt('粘贴 YouTube 链接或视频 ID' + (cur ? '(留空=移除视频)' : '') + '：', cur || '');
    if (url === null) return;
    url = url.trim();
    if (!url) { if (cur) { ctl.note.video = null; patchNote(ctl.note, { video: null }); renderNoteVideo(ctl); } return; }
    var id = ytIdOf(url);
    if (!id) { alert('没识别出 YouTube 视频 ID'); return; }
    setNoteVideo(ctl, id);
  }

  function bindCtl(ctl) {
    // 🗑 删除(EDIT 模式出现;confirm 后 DELETE)
    ctl.del.addEventListener('pointerdown', function (e) { e.stopPropagation(); });
    ctl.del.addEventListener('click', function (e) {
      e.stopPropagation(); e.preventDefault();
      if (!window.confirm('删除这张便签？')) return;
      deleteNote(ctl.note, '🗑 便签已删除');
    });
    // handle:长按 lpMs() 进 EDIT(与 body 长按同一入口);EDIT 模式下按下即拖(移动便签)
    ctl.handle.addEventListener('pointerdown', function (e) { onHandleDown(ctl, e); });
    ctl.handle.addEventListener('lostpointercapture', function (e) {
      if (_hd && _hd.ctl === ctl) onHandleCancel(e);
    });
    // body:pen 后备直写/手指双击切橡皮/长按 lpMs() 进 EDIT(capture:抢在 textarea 前看一眼;
    //   手指单击不拦截 → 穿透给 textarea 聚焦输入)
    ctl.body.addEventListener('pointerdown', function (e) { onBodyDown(ctl, e); }, true);
    // 缩放手柄(右下=原有;左上=位置+尺寸同变,右下角固定)
    ctl.rs.addEventListener('pointerdown', function (e) { onResizeDown(ctl, e); });
    ctl.rsTL.addEventListener('pointerdown', function (e) { onResizeTLDown(ctl, e); });
    // tap 计数(单击 handle 折叠 / 双击任意区域 → onDoubleTap)
    ctl.root.addEventListener('pointerdown', function (e) { ctl._downPt = { x: e.clientX, y: e.clientY }; });
    ctl.root.addEventListener('pointerup', function (e) { onRootUp(ctl, e); });
    // 文字失焦 → 存文字(v2 规格1:保存语义=失焦 PATCH)
    ctl.ta.addEventListener('blur', function () { saveText(ctl); });
    // iOS:Apple Pencil 触摸阻止默认滚动(照搬 _inkBlockStylusScroll;pen 常态直写 → 常开;手指放行照常滚)
    function blockStylus(e) {
      for (var i = 0; i < e.touches.length; i++) { if (e.touches[i].touchType === 'stylus') { e.preventDefault(); break; } }
    }
    ctl.body.addEventListener('touchstart', blockStylus, { passive: false });
    ctl.body.addEventListener('touchmove', blockStylus, { passive: false });
  }

  // ─────────────────────────── 挂载(锚定进内容容器;幂等,可反复重挂)───────────────────────────
  // v4 契约(铁律修正:定位**机制**归 host,组件只应用**策略**):O.mount(anchor) 返回
  //   {el, left, top, anchor?} —— left/top 为容器内**像素**(PDF=x·clientWidth/y·clientHeight,行为等价旧 %;
  //   EPUB=内容锚 off→字符 rect→section 内像素+dx/dy);anchor 字段=host 懒迁移升级后的新锚(旧 x/y 比例锚 →
  //   内容锚),组件负责 PATCH 落库。旧契约({el,w,h} 无 left/top)兼容:组件退回自算 x/y 百分比。
  // ─────────────────────────── 展开态置顶浮层(portal 到滚动容器内的叠加层)───────────────────────────
  // 根因:PDF 单页/连续缩放靠祖先 CSS zoom 撑大,zoom≠1 在 Chrome/Safari 造层叠上下文,把 absolute-in-page-wrap
  //   的便签困死其中 → z-index 再高也冲不出侧栏(120)。
  // 解法:展开时把便签搬到「滚动容器(PDF #main / EPUB 滚动祖先)内部」的叠加层 .rc-note-overlay
  //   (position:absolute,z-index 190:#main 非层叠上下文→根上下文里盖过侧栏 120;低于模态遮罩 200/210)。
  //   便签在叠加层里 absolute 定位 → 随容器**原生滚动**(零 JS 跟滚抖动),又因叠加层不在被 zoom 的 page-container 内
  //   而逃出层叠陷阱。page-wrap 内留 0×0 占位:reflow/缩放/侧栏开关后 repositionPortaled 读占位屏幕位重算便签坐标
  //   (原生滚动不需此步,故不绑 scroll)。折叠即回内容锚。
  function scrollAncestor(el) {
    for (var p = el && el.parentElement; p && p !== document.body && p !== document.documentElement; p = p.parentElement) {
      var s; try { s = getComputedStyle(p); } catch (e) { continue; }
      if (/(auto|scroll|overlay)/.test((s.overflowY || '') + ' ' + (s.overflow || ''))) return p;
    }
    return document.scrollingElement || document.documentElement;
  }
  function overlayFor(scrollEl) {
    if (!scrollEl) return null;
    if (scrollEl.__rcNoteOverlay && scrollEl.__rcNoteOverlay.isConnected) return scrollEl.__rcNoteOverlay;
    try { if (getComputedStyle(scrollEl).position === 'static') scrollEl.style.position = 'relative'; } catch (e) {}   // abs 子锚到它
    var ov = document.createElement('div');
    ov.className = 'rc-note-overlay';
    ov.style.cssText = 'position:absolute;left:0;top:0;width:0;height:0;z-index:190;pointer-events:none';
    scrollEl.appendChild(ov);
    scrollEl.__rcNoteOverlay = ov;
    return ov;
  }
  // 屏幕坐标 → 滚动容器内容坐标(叠加层里 absolute 定位用;含 border 偏移 clientLeft/Top)
  function _contentXY(scrollEl, screenLeft, screenTop) {
    var r = scrollEl.getBoundingClientRect();
    return { x: Math.round(screenLeft - r.left - scrollEl.clientLeft + scrollEl.scrollLeft),
             y: Math.round(screenTop - r.top - scrollEl.clientTop + scrollEl.scrollTop) };
  }
  // 在 note 当前锚容器里补一个 0×0 占位(供 repositionPortaled 读位;页容器重渲被冲掉时也用它补挂)
  function attachPlaceholder(ctl) {
    if (ctl._ph) { try { ctl._ph.remove(); } catch (e) {} ctl._ph = null; }
    var m = null; try { m = O.mount(ctl.note.anchor); } catch (e) {}
    if (!m || !m.el) return;
    var a = ctl.note.anchor || {};
    var l = (typeof m.left === 'number') ? m.left + 'px' : ((Math.max(0, Math.min(1, a.x || 0)) * 100) + '%');
    var t = (typeof m.top === 'number') ? m.top + 'px' : ((Math.max(0, Math.min(1, a.y || 0)) * 100) + '%');
    var ph = document.createElement('div');
    ph.style.cssText = 'position:absolute;width:0;height:0;pointer-events:none;left:' + l + ';top:' + t;
    m.el.appendChild(ph);
    ctl._ph = ph; ctl._scrollEl = scrollAncestor(m.el);
  }
  // 视频便签守则(用户规格):播放/暂停只归视频自己——iframe 一被 reparent 浏览器就强制重载(=播放中断丢进度),
  // 所以带活 iframe 的便签不做自动迁回(点外降层/折叠都不搬 DOM);折叠改用 postMessage 暂停(enablejsapi 已开,进度保留)。
  function _hasLiveVid(ctl) { try { return !!(ctl && ctl.root && ctl.root.querySelector('.rc-vid-if')); } catch (e) { return false; } }
  function _vidPause(ctl) {
    try {
      ctl.root.querySelectorAll('.rc-vid-if').forEach(function (f) {
        try { f.contentWindow.postMessage(JSON.stringify({ event: 'command', func: 'pauseVideo', args: '' }), '*'); } catch (e) {}
      });
    } catch (e) {}
  }
  function portalIn(ctl) {
    if (O && O.disablePortal) return;   // 浏览器扩展 shadow 根本身就是全视口浮层；禁止搬到宿主页滚动层
    if (!ctl || ctl.portaled || ctl.note.collapsed || !ctl.root.isConnected) return;
    var home = ctl.root.parentElement;
    var scrollEl = scrollAncestor(home);
    var ov = overlayFor(scrollEl);
    if (!ov) return;
    var noteRect = ctl.root.getBoundingClientRect();
    if (!noteRect.width && !noteRect.height) return;   // 不可见 → 不 portal
    attachPlaceholder(ctl);   // 先在 page-wrap 留占位(读得到锚的真实屏幕位,含缩放)
    ctl.portaled = true; ctl._scrollEl = scrollEl;
    var c = _contentXY(scrollEl, noteRect.left, noteRect.top);
    ov.appendChild(ctl.root);   // 搬进滚动容器叠加层 → 随容器原生滚动 + 逃出 zoom 陷阱
    ctl.root.style.position = 'absolute';
    ctl.root.style.left = c.x + 'px'; ctl.root.style.top = c.y + 'px';
    ctl.root.classList.add('rc-note-portaled');
  }
  function portalOut(ctl) {
    if (!ctl || !ctl.portaled) return;
    ctl.portaled = false;
    ctl.root.classList.remove('rc-note-portaled');
    ctl.root.style.position = ''; ctl.root.style.left = ''; ctl.root.style.top = '';
    if (ctl._ph) { try { ctl._ph.remove(); } catch (e) {} ctl._ph = null; }
    ctl._scrollEl = null;
    try { ensureMounted(ctl.note); } catch (e) {}   // 回内容锚(容器没了→卸下待 mountPending 补挂)
  }
  // reflow/缩放/侧栏开关后:portaled 便签读占位当前屏幕位 → 重算叠加层坐标
  function repositionPortaled() {
    for (var id in ctls) {
      var ctl = ctls[id];
      if (!ctl || !ctl.portaled || !ctl._scrollEl) continue;
      if (_hd && _hd.dragging && _hd.ctl === ctl) continue;   // #51 S1:拖动中的便签不被异步重渲重定位(消除"拖动期间 root.left 被 mountAll 改写"→松手落点偏移的竞态)
      if (!ctl._ph || !ctl._ph.isConnected) { attachPlaceholder(ctl); }   // 页重渲冲掉占位 → 在新容器补挂
      if (!ctl._ph || !ctl._ph.isConnected) continue;   // 锚页未挂载(滚出) → 保持原坐标,展开浮层不消失
      var pr = ctl._ph.getBoundingClientRect();
      var c = _contentXY(ctl._scrollEl, pr.left, pr.top);
      ctl.root.style.left = c.x + 'px'; ctl.root.style.top = c.y + 'px';
    }
  }
  function ensureMounted(note) {
    var ctlP = ctls[note.id];
    if (ctlP && ctlP.portaled) return true;   // 展开态在叠加层:重定位/重挂交给 portal(repositionPortaled),不回插内容容器
    var m = null;
    try { m = O.mount(note.anchor); } catch (e) {}
    var ctl = ctls[note.id];
    if (!m || !m.el) {   // 容器未就绪/已失效(PDF 单页翻页 pw 复用换页 / 章重渲)→ 卸下,留待 mountPending 补挂
      if (ctl && ctl.root.isConnected) { try { ctl.root.remove(); } catch (e2) {} }
      return false;
    }
    if (!ctl) { ctl = buildCtl(note); ctls[note.id] = ctl; }
    ctl.note = note;
    if (m.anchor) {   // host 懒迁移(v4:旧 x/y 锚就地升级成内容锚)→ 落库,之后直接走内容锚
      note.anchor = m.anchor;
      patchNote(note, { anchor: m.anchor });
    }
    if (ctl.root.parentElement !== m.el) m.el.appendChild(ctl.root);
    if (typeof m.left === 'number' && typeof m.top === 'number') {
      ctl.root.style.left = m.left + 'px';
      ctl.root.style.top = m.top + 'px';
    } else {   // 旧 host 契约兜底:组件自算比例(x/y 锚)
      ctl.root.style.left = (Math.max(0, Math.min(1, note.anchor.x || 0)) * 100) + '%';
      ctl.root.style.top = (Math.max(0, Math.min(1, note.anchor.y || 0)) * 100) + '%';
    }
    syncCtl(ctl);
    _applyWordBind(ctl);
    return true;
  }

  /// 拖动落点重新求词锚。返回 true 表示 card.bind 换了（调用方要把 card 一起落库）。
  /// 拿不到词（拖到空白/图区/页缝）就**撤掉词锚退回普通便签** —— 比留一个
  /// 指向别处的旧 bind 好：旧 bind 会让标记停在原地，看着像卡片分身。
  /// ⚠ cx/cy 必须是**加过 shift 的落点**，跟同一处 reanchorAt 用的是同一个点：
  ///   r0 是撤掉 transform 之后的矩形（= 拖动前的位置），直接拿它去探测
  ///   等于锚回原处，而 anchor 已经按落点更新了 —— 两者从此指向不同的地方。
  function _rebindWord(ctl, cx, cy) {
    var old = ctl.note && ctl.note.card && ctl.note.card.bind;
    if (!old) return false;   // 本来就不是词锚卡，不管
    if (cx == null || cy == null) return false;   // 没真拖动（shift=0）→ 不重锚
    var wr = null;
    try {
      wr = _probeHidden(ctl.root, function () {
        return O.noteWordRect ? O.noteWordRect(cx, cy) : null;
      });
    } catch (e) {}
    var nb = (wr && wr.page > 0 && wr.text && (wr.dist == null || wr.dist <= 48))
      ? { kind: 'page-chars', page: wr.page, from: wr.from, to: wr.to,
          text: String(wr.text).slice(0, 200) }
      : null;
    if (nb && nb.page === old.page && nb.from === old.from && nb.to === old.to) return false;
    try { if (window.__pageBindRemove) window.__pageBindRemove(old); } catch (e) {}
    ctl.note.card.bind = nb;
    ctl._bindMarked = false;
    if (!nb) ctl.root.style.display = '';   // 退回普通便签，别把卡藏没了
    return true;
  }

  // ── 词锚便签：正文里显示成「词描边 + 右上角序号」，点词才展开真卡 ──────
  //   用户 2026-08-20 定的形态：「插入后自动锁定到前方的分词元素，高亮整个分词，
  //   右上角加上数字，然后点击这个词直接展开卡片」。动机是圆球太多会挡住正文。
  //
  //   实现上**不换一套便签**：卡片壳、学习状态、拖动、删除全是原来那份，
  //   这里只做两件事 —— 画标记、把便签默认收起来。绑不上时（页没渲染出来、
  //   文字层换过）**原样显示便签**，不制造「卡不见了」。
  function _applyWordBind(ctl) {
    var b = ctl.note && ctl.note.card && ctl.note.card.bind;
    if (!b || !window.__pageBindCard) return;
    var res = null;
    try {
      res = window.__pageBindCard(b, {
        label: '🎴 卡片',
        onToggle: function () {
          ctl._bindOpen = !ctl._bindOpen;
          ctl.root.style.display = ctl._bindOpen ? '' : 'none';
        }
      });
    } catch (e) {}
    if (res && res.ok) {
      ctl._bindMarked = true;
      ctl.root.style.display = ctl._bindOpen ? '' : 'none';
    } else {
      // 回退到老浮层是对的（宁可看到老样子，也不要卡片消失），但**必须留痕**：
      // 圆球本来就是老形态，退回去看着像「这卡就是这样的」，没人会去查。
      // 见 references/silent-failure-lessons.md 第五节。
      if (ctl._bindWhy !== (res && res.why)) {
        ctl._bindWhy = res && res.why;
        try { console.warn('[bind] 词锚没画上，退回浮层便签', ctl.note.id, res); } catch (e) {}
      }
    }
    if (!(res && res.ok) && ctl._bindMarked) {
      // 之前标上过、这次没标上（换页/重渲）——先回到可见，别把卡片藏没了
      ctl._bindMarked = false;
      ctl.root.style.display = '';
    }
  }
  // 全量重挂/重定位:ensureMounted 幂等(容器校验/换容器重挂/位置重算全在里面)。
  // 亦是 v4 repositionAll 的实现——重排(侧栏开关/字号/栏宽/resize)后 host 重算像素位置。
  function mountAll() {
    if (!O) return;
    for (var i = 0; i < notes.length; i++) ensureMounted(notes[i]);
    repositionPortaled();   // 展开态浮层便签:reflow/缩放/侧栏开关后按 page-wrap 占位重算叠加层坐标(原生滚动不需)
  }
  function removeAllEls() {
    for (var id in ctls) {
      try { ctls[id].root.remove(); } catch (e) {}
      try { if (ctls[id]._ph) ctls[id]._ph.remove(); } catch (e2) {}   // 清占位,防叠加层/页容器残留孤儿
    }
    ctls = {};
  }
  function syncCtl(ctl) {
    var n = ctl.note;
    applyColor(ctl);
    applySize(ctl);
    // applySize 只有 canvas 尺寸变化时会重画；跨标签 CHANGE 可能只更新
    // strokes 而尺寸不变，需按内容签名补画，否则本地数据已更新但画面仍旧。
    var strokeSig = '';
    try { strokeSig = JSON.stringify(n.strokes || []); } catch (_) {}
    if (ctl._strokeSig !== strokeSig) redrawInk(ctl);
    ctl.root.classList.toggle('rc-note-collapsed', !!n.collapsed);
    renderNoteVideo(ctl);   // 视频便签:有 video 则渲染播放器+控件(签名去重,无变化不重建)
    renderNoteCard(ctl);    // 卡片便签:有 card 则 mount rc-flashcard(同 gid → 与侧栏/浮层原卡联动)
    renderNoteHtml(ctl);    // 通用卡便签:天气/搜索/图等 vc-card 的 HTML 快照
    // 正在输入时(聚焦)不回灌服务端文字,防清掉未保存输入
    if (document.activeElement !== ctl.ta && ctl.ta.value !== (n.text || '')) ctl.ta.value = n.text || '';
  }
  function applyColor(ctl) {
    var c = ctl.note.color || DEFAULT_COLOR;
    var rgb = hexRgb(c), a = noteOpacity();
    if (rgb) {
      // 磨砂玻璃:body=rgba(便签色,α);handle 是操作件要醒目,α≥0.85
      ctl.body.style.background = 'rgba(' + rgb.r + ',' + rgb.g + ',' + rgb.b + ',' + a + ')';
      ctl.handle.style.background = 'rgba(' + rgb.r + ',' + rgb.g + ',' + rgb.b + ',' + Math.max(0.85, a) + ')';
    } else {
      // 非 hex 的旧数据/异常值:原样不透明兜底(不在色板的颜色也要正常显示)
      ctl.body.style.background = c;
      ctl.handle.style.background = c;
    }
    // 磨砂强度可调(0=不模糊纯半透明,置空省 GPU;refreshStyle 重跑本函数即时生效)
    var bl = noteBlur();
    var bf = bl > 0 ? 'blur(' + bl + 'px)' : '';
    ctl.body.style.backdropFilter = bf;
    ctl.body.style.webkitBackdropFilter = bf;
    // 自动对比色:按便签本色亮度(α 不参与)切前景;开关关 → 恒深字(现状)
    ctl.root.classList.toggle('rc-note-darkbg', autoContrast() && isDarkBg(c));
    updateSwatchUI(ctl);
  }
  function applySize(ctl) {
    ctl.body.style.width = (ctl.note.w || 260) + 'px';
    ctl.body.style.height = (ctl.note.h || 180) + 'px';
    if (sizeCanvas(ctl)) redrawInk(ctl);   // 尺寸变了 → 按归一化坐标重绘笔画
  }
  function sizeCanvas(ctl) {
    var w = ctl.note.w || 260, h = ctl.note.h || 180, dpr = window.devicePixelRatio || 1;
    var W = Math.max(1, Math.floor(w * dpr)), H = Math.max(1, Math.floor(h * dpr));
    if (ctl.cv.width === W && ctl.cv.height === H) return false;
    ctl.cv.width = W; ctl.cv.height = H;
    return true;
  }

  // ─────────────────────────── tap 状态机(单击折叠 / 双击 AI hook)───────────────────────────
  function onRootUp(ctl, e) {
    if (Date.now() - (ctl._suppressTap || 0) < 80) return;   // 刚结束拖拽/绘制/resize 的抬手不算 tap
    if (draw || _rz || _rzTL) return;
    var d0 = ctl._downPt; ctl._downPt = null;
    if (!d0 || Math.hypot(e.clientX - d0.x, e.clientY - d0.y) > TAP_TOL) return;
    if (e.target && e.target.closest && (e.target.closest('.rc-note-del') || e.target.closest('.rc-note-tools') || e.target.closest('.rc-note-rs') || e.target.closest('.rc-note-rs-tl'))) return;
    // 卡片正文的双击/短点属于 flashcard / 工具卡自己的交互；不能落进旧便签
    // EDIT。否则卡片一旦误进 EDIT，卡头会走“编辑态立即拖动”，绕过 420ms
    // 蓄力，而且短点切换形态也会失效。
    if (_isCardNote(ctl)) { ctl._tapCount = 0; return; }
    var now = Date.now();
    var near = ctl._tapPt && Math.hypot(e.clientX - ctl._tapPt.x, e.clientY - ctl._tapPt.y) < 32;
    ctl._tapCount = (now - ctl._tapT < TAP_WIN && near) ? ctl._tapCount + 1 : 1;
    ctl._tapT = now; ctl._tapPt = { x: e.clientX, y: e.clientY };
    if (ctl._tapCount >= 2) {
      // 双击 = 进 EDIT 编辑模式(用户方案 2026-07-21:双击=编辑;长按原地松手=加入上下文/注入AI,长按+拖动=移动便签)。
      // 注:body 上聚焦文字的 tap 照常进计数;橡皮双击(有笔画时)在 onBodyDown 已 stopPropagation,到不了这里。
      ctl._tapCount = 0;
      if (ctl._toggleT) { clearTimeout(ctl._toggleT); ctl._toggleT = null; }
      try { enterEdit(ctl); } catch (err) {}
      return;
    }
    // 单击 handle → 延迟 380ms toggle(双击窗口内被第二击撤销;EDIT 模式下 handle=移动把手,不折叠)
    if (!EDIT && e.target && e.target.closest && e.target.closest('.rc-note-handle')) {
      clearTimeout(ctl._toggleT);
      ctl._toggleT = setTimeout(function () { ctl._toggleT = null; toggleCollapsed(ctl); }, TAP_WIN);
    }
  }
  function toggleCollapsed(ctl) {
    ctl.note.collapsed = !ctl.note.collapsed;
    ctl.root.classList.toggle('rc-note-collapsed', ctl.note.collapsed);
    patchNote(ctl.note, { collapsed: ctl.note.collapsed });
    if (ctl.note.collapsed) {
      _vidPause(ctl);                         // 折叠=暂停播放(postMessage,进度保留;用户规格)
      if (!_hasLiveVid(ctl)) portalOut(ctl);  // 有活视频不迁回(reparent 强制重载 iframe);折叠小条留浮层视觉无差,展开时 portalIn 幂等早退不再搬
    } else portalIn(ctl);                     // 展开→置顶浮层;折叠→回内容锚
  }

  // ─────────────────────────── handle 手势(长按 → EDIT;EDIT 内按下即拖 = 移动便签)───────────────────────────
  function _isCardNote(ctl) { return !!(ctl && ctl.note && (ctl.note.card || ctl.note.html)); }
  function _formW(ctl, f) {   // 便签壳宽跟卡形态(dot 标记 40/长条 300/方块 note.w);方块按页宽**等比缩放**(用户:页面缩放时定死像素会挡内容)
    if (f === 'dot') return '40px';
    if (f === 'min') return '300px';
    if (ctl._cardPresentationSize && ctl._cardPresentationSize.w) {
      return Math.max(180, Math.min(720, Math.round(ctl._cardPresentationSize.w))) + 'px';
    }
    var w0 = ctl.note.w || 300;
    var bw = ((ctl.note.card || {}).base_w) || ((ctl.note.html || {}).base_w) || 0;
    try { var cw = ctl.root.parentElement ? ctl.root.parentElement.clientWidth : 0; if (bw > 0 && cw > 0) return Math.max(140, Math.round(w0 * cw / bw)) + 'px'; } catch (e) {}
    return w0 + 'px';
  }
  function _hardDelete(ctl) {   // 拖到左上角删除(无 confirm,浮层拖删同手感)
    deleteNote(ctl.note, '已删除');
  }
  function _favoriteCard(ctl) {
    try {
      if (!(window.RC && RC.voiceCard && RC.voiceCard.favorite)) return false;
      var n = ctl.note || {}, rec;
      if (n.html) rec = { label: n.html.label || '工具卡片', raw: n.html.content || '', isHtml: !!n.html.isHtml, text: n.html.content || '', kind: 'tool', cid: n.html.cid || '' };
      else {
        var cards = (n.card && n.card.cards) || [];
        rec = { label: '学习卡片', raw: JSON.stringify(cards), isHtml: false, text: cardContextText(cards), kind: 'cards', cid: (n.card && (n.card.cid || n.card.gid)) || '', gid: (n.card && n.card.gid) || '' };
      }
      return RC.voiceCard.favorite.save(rec);
    } catch (e) { return false; }
  }
  function onHandleDown(ctl, e) {
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    // 一次手势严格只归最先按下的 pointer；第二根手指/笔尖不得抢占或结束它。
    if (_hd) return;
    e.preventDefault();   // handle touch-action:none;再挡 iOS 文本选择/callout
    if (!ctl.note.collapsed) portalIn(ctl);   // 展开态一碰即置顶(单击/长按/拖动统一,idempotent;折叠态由 toggleCollapsed 展开后再置顶)
    _hd = {
      ctl: ctl, sx: e.clientX, sy: e.clientY, lp: null,
      pointerId: e.pointerId, captureEl: ctl.handle,
      dragging: false, moved: false, rect0: null,
      cardArmed: false, slopCancelled: false
    };
    // 普通便签的既有“EDIT 内立即拖动”语义保持不变；卡片即使来自热更新前
    // 遗留的 EDIT 状态，也被显式排除在这条捷径外，始终进入下面的蓄力链。
    if (EDIT && EDIT.ctl === ctl && !_isCardNote(ctl)) startDrag(ctl);
    else if (_isCardNote(ctl)) {
      ctl.root.classList.add('rc-card-drag-charging');
      _hd.lp = setTimeout(function () {
        if (!_hd || _hd.ctl !== ctl || _hd.slopCancelled) return;
        _hd.lp = null;
        _hd.longFired = true;
        _hd.cardArmed = true;
        ctl.root.classList.remove('rc-card-drag-charging');
        ctl.root.classList.add('rc-card-drag-ready');
        try { if (navigator.vibrate) navigator.vibrate(8); } catch (_) {}
        startDrag(ctl);
      }, CARD_DRAG_HOLD_MS);
    }
    else _hd.lp = setTimeout(function () { if (_hd && _hd.ctl === ctl) { _hd.longFired = true; startDrag(ctl); } }, lpMs());   // 长按=可拖候选(不进EDIT):拖了→移动便签,原地松手→注入AI(用户方案,见 onHandleUp 的 !moved 分支)
    document.addEventListener('pointermove', onHandleMove, true);
    document.addEventListener('pointerup', onHandleUp, true);
    document.addEventListener('pointercancel', onHandleCancel, true);
    try { ctl.handle.setPointerCapture(e.pointerId); } catch (err) {}
  }
  function startDrag(ctl) {
    if (!_hd) return;
    _hd.dragging = true;
    ctl.root.classList.remove('rc-card-drag-charging');
    ctl.root.style.transformOrigin = '0 0';   // #51:scale 以左上角为原点——拖动中视觉左上=translate 位置=松手最终位置(中心放大会外扩≈1.5%,松手跳位根因之一)
    _hd.rect0 = ctl.root.getBoundingClientRect();
    // #51 用户真机实锤'拖动时渲染位置出错':页面缩放(zoom/适应)下便签在被缩放坐标系里,translate(手指px)被
    // scale 放大/缩小=不跟手。位移按**元素有效缩放**(视觉宽/布局宽)换算→任何缩放环境 1:1 跟手(拖拽库标准做法)
    _hd.scale = (ctl.root.offsetWidth ? _hd.rect0.width / ctl.root.offsetWidth : 1) || 1;
    ctl.root.classList.add('rc-note-lift');   // 浮起效果:只在拖拽进行时(松手/取消即撤)
  }
  function onHandleMove(e) {
    if (!_hd || !sameHandlePointer(_hd, e)) return;
    var dx = e.clientX - _hd.sx, dy = e.clientY - _hd.sy;
    if (!_hd.dragging) {
      var tolerance = _isCardNote(_hd.ctl) ? CARD_DRAG_TOL : LP_TOL;
      if (Math.hypot(dx, dy) > tolerance) {
        clearTimeout(_hd.lp); _hd.lp = null; _hd.slopCancelled = true;
        _hd.ctl.root.classList.remove('rc-card-drag-charging', 'rc-card-drag-ready');
      }   // 蓄力前快划=取消，不会突然追上手指，也不会误切形态
      return;
    }
    if (!_hd.moved && Math.hypot(dx, dy) <= 4) return;
    e.preventDefault();
    _hd.moved = true;
    var _s9 = _hd.scale || 1;
    _hd.ctl.root.style.transform = 'translate(' + (dx / _s9) + 'px,' + (dy / _s9) + 'px) scale(1.03)';
    if (_isCardNote(_hd.ctl) && window.RC && RC.voiceCard) {
      if (RC.voiceCard.trash) { RC.voiceCard.trash.show(true); RC.voiceCard.trash.hot(RC.voiceCard.trash.inZone(e.clientX, e.clientY)); }
      if (RC.voiceCard.favorite) RC.voiceCard.favorite.hint(RC.voiceCard.favorite.inZone(e.clientX, e.clientY));
    }   // 6A：卡便签拖起同时启用左上删除区与底边收藏区
    try { var _rr9 = _hd.ctl.root.getBoundingClientRect(); anchorFxShow(_rr9.left + 1, _rr9.top + 1, _hd.ctl.root); } catch (e2) {}   // #51:探测点=**卡左上角**(=钉入点,用户拍板);隐自身穿透(拖已钉便签恒横线的根因)
  }
  function onHandleUp(e) {
    var g = _hd; if (!g || !sameHandlePointer(g, e)) return;
    cleanupHandleListeners();
    clearTimeout(g.lp);
    _hd = null;
    clearHandleDropUi();
    releaseHandleCapture(g);
    g.ctl.root.classList.remove('rc-card-drag-charging', 'rc-card-drag-ready');
    if (!g.dragging) {
      if (_isCardNote(g.ctl)) g.ctl._suppressTap = Date.now();
      if (_isCardNote(g.ctl) && !g.slopCancelled) {
        try {
          var _tapEl = g.ctl.body.querySelector('.vc-card');
          var _tapBtn = _tapEl && (_tapEl.classList.contains('vc-dot') ? _tapEl.querySelector('.vc-card-dot') : _tapEl.querySelector('.vc-card-hd'));
          if (_tapBtn) _tapBtn.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        } catch (_) {}
      }
      return;
    }
    if (g.dragging) {
      g.ctl.root.classList.remove('rc-note-lift');   // 浮起只随拖拽;EDIT 模式(🗑/色板/手柄)继续保持
      g.ctl._suppressTap = Date.now();
      if (_isCardNote(g.ctl) && window.RC && RC.voiceCard) {
        var _trashDrop = !!(g.moved && RC.voiceCard.trash && RC.voiceCard.trash.inZone(e.clientX, e.clientY));
        var _favDrop = !!(g.moved && RC.voiceCard.favorite && RC.voiceCard.favorite.inZone(e.clientX, e.clientY));
        if (RC.voiceCard.trash) RC.voiceCard.trash.show(false);
        if (RC.voiceCard.favorite) RC.voiceCard.favorite.hint(false);
        if (_trashDrop) { _hardDelete(g.ctl); return; }   // 拖到左上角松手=删除(浮层同交互,无叉叉按钮)
        if (_favDrop) { _favoriteCard(g.ctl); g.ctl.root.style.transform = ''; return; }   // 收藏是复制，原页卡回原位
      }
      if (g.moved) dropNote(g.ctl, g.rect0, e.clientX - g.sx, e.clientY - g.sy);
      else {
        g.ctl.root.style.transform = '';   // 长按未拖:清暂态
        if (g.longFired && !_isCardNote(g.ctl)) {   // 长按原地松手(位移未超阈值)= 加入上下文/注入AI(用户方案:松手位移分辨长按vs拖动)
          try { saveText(g.ctl); } catch (e0) {}   // 冲掉未失焦文字改动(hook 拿最新 text + 合成图最新 sidecar)
          try { O && O.onDoubleTap && O.onDoubleTap(g.ctl.note); } catch (e1) {}
        }
      }
    }
  }
  function onHandleCancel(e) {
    var g = _hd; if (!g || !sameHandlePointer(g, e)) return;
    cleanupHandleListeners();
    clearTimeout(g.lp);
    _hd = null;
    clearHandleDropUi();
    releaseHandleCapture(g);
    g.ctl.root.classList.remove('rc-card-drag-charging', 'rc-card-drag-ready');
    if (g.dragging) { g.ctl.root.classList.remove('rc-note-lift'); g.ctl.root.style.transform = ''; g.ctl._suppressTap = Date.now(); }
  }
  function sameHandlePointer(g, e) {
    return !!(g && (!e || e.pointerId == null || g.pointerId == null || e.pointerId === g.pointerId));
  }
  function releaseHandleCapture(g) {
    if (!g || !g.captureEl || g.pointerId == null || !g.captureEl.releasePointerCapture) return;
    try {
      if (!g.captureEl.hasPointerCapture || g.captureEl.hasPointerCapture(g.pointerId))
        g.captureEl.releasePointerCapture(g.pointerId);
    } catch (e) {}
  }
  function clearHandleDropUi() {
    try { anchorFxHide(); } catch (e) {}
    try {
      if (window.RC && RC.voiceCard) {
        if (RC.voiceCard.trash) {
          RC.voiceCard.trash.hot(false);
          RC.voiceCard.trash.show(false);
        }
        if (RC.voiceCard.favorite) RC.voiceCard.favorite.hint(false);
      }
    } catch (e2) {}
  }
  function cleanupHandleListeners() {
    document.removeEventListener('pointermove', onHandleMove, true);
    document.removeEventListener('pointerup', onHandleUp, true);
    document.removeEventListener('pointercancel', onHandleCancel, true);
  }
  function cancelHandleGesture() {
    if (!_hd) return;
    var g = _hd;
    cleanupHandleListeners();
    clearTimeout(g.lp);
    _hd = null;
    clearHandleDropUi();
    releaseHandleCapture(g);
    g.ctl.root.classList.remove('rc-card-drag-charging', 'rc-card-drag-ready');
    if (g.dragging) {
      g.ctl.root.classList.remove('rc-note-lift');
      g.ctl.root.style.transform = '';
      g.ctl._suppressTap = Date.now();
    }
  }
  // 「松手算锚」统一入口(v4):视口点 → host anchorFromPoint(EPUB 返回内容锚 off/dx/dy,PDF 返回页比例 x/y;
  // 机制在 host)。期间便签自身 pointer-events:none,让 elementFromPoint/caretFromPoint 穿过它命中下方内容。
  function reanchorAt(ctl, px, py) {
    var anchor = null;
    var oldPe = ctl.root.style.pointerEvents;
    ctl.root.style.pointerEvents = 'none';
    try { anchor = O.anchorFromPoint(px, py); } catch (e) {}
    ctl.root.style.pointerEvents = oldPe || '';
    return anchor;
  }
  function dropNote(ctl, rect0, dx, dy) {
    // 松手:便签左上角 → anchorFromPoint 重解析目标容器(支持拖过页/章边界)→ PATCH。
    // #51 S1:探测点用**松手瞬间实时 BCR**(transform 未清,transform-origin '0 0' 下左上角=视觉落点),
    //   不再用 rect0+指针位移——后者在"拖动中异步页渲染/滚动改了 root.left"时与实际视觉位置分叉
    //   (拖动反馈 anchorFxShow 一直用实时 BCR,两链对齐=拖动显示与松手落点一致)。
    var wasPortaled = ctl.portaled;
    var _lr = ctl.root.getBoundingClientRect();
    var anchor = _probeHidden(ctl.root, function () { return reanchorAt(ctl, _lr.left + 1, _lr.top + 1); });   // #51:穿透自身探测;实时 BCR 左上 +1
    ctl.root.style.transform = '';
    if (!anchor) { toastMsg('放不到这里(不在内容页上),已弹回'); return; }   // 清 transform→回原位(portaled 回 fixed 原点)
    ctl.note.anchor = anchor;
    patchNote(ctl.note, { anchor: anchor });
    if (wasPortaled) {   // 先解除 portal,ensureMounted 才会真正换容器重挂(否则 portaled 守卫早退)
      ctl.portaled = false; ctl.root.classList.remove('rc-note-portaled');
      ctl.root.style.position = ''; ctl.root.style.left = ''; ctl.root.style.top = '';
      if (ctl._ph) { try { ctl._ph.remove(); } catch (e) {} ctl._ph = null; }
    }
    ensureMounted(ctl.note);   // 跨页/跨章 → 换容器重挂 + 新锚绝对定位
    if (wasPortaled && !ctl.note.collapsed) portalIn(ctl);   // 回置顶浮层(落点新位置,重建占位)
  }
  // ─────────────────────────── EDIT 编辑模式(长按便签任意部分,handle/body 同一入口)───────────────────────────
  // 同时呈现:🗑 + 色板工具条 + 左上/右下缩放手柄 + handle 拖拽移动;移动/缩放/换色可连续操作不自动退出。
  function enterEdit(ctl) {
    if (EDIT && EDIT.ctl !== ctl) exitEdit();
    if (EDIT) return;
    if (ctl.note.collapsed) { ctl.note.collapsed = false; ctl.root.classList.remove('rc-note-collapsed'); patchNote(ctl.note, { collapsed: false }); }
    EDIT = { note: ctl.note, ctl: ctl };
    ctl.root.classList.add('rc-note-editing', 'rc-note-active');
    portalIn(ctl);   // 进编辑=展开:置顶浮层(逃出被困层叠上下文,盖过侧栏/相邻页)
    try { ctl.ta.blur(); } catch (e) {}   // 收键盘/选词态(blur 顺带 saveText);模式内 textarea pointer-events:none。handle/body 入口一致。
    updateToolUI(ctl); updateSwatchUI(ctl);
    try { if (navigator.vibrate) navigator.vibrate(10); } catch (e2) {}
  }
  function exitEdit() {   // 点便签外退出(onDocDown);退出时保存文字/笔画待存项
    if (!EDIT) return;
    var ctl = EDIT.ctl;
    EDIT = null;
    saveText(ctl);
    flushStrokeSave(ctl);
    ctl.root.classList.remove('rc-note-editing', 'rc-note-active', 'rc-note-lift');
    ctl.root.style.transform = '';
  }

  // ─────────────────────────── body 手势(v3:单击=文字 / 长按=EDIT / pen=后备直写 / 手指双击=橡皮)───────────────────────────
  function onBodyDown(ctl, e) {
    if (!ctl.note.collapsed) portalIn(ctl);   // 单击正文即置顶(展开态一碰就浮到最上层;idempotent,已 portaled 则 no-op)
    if (e.target && e.target.closest && (e.target.closest('.rc-note-tools') || e.target.closest('.rc-note-rs') || e.target.closest('.rc-note-rs-tl') || e.target.closest('.rc-note-del'))) return;
    // 学习卡自己的正文长按统一交给 RC.voiceCard.pinBind。若继续武装便签 body
    // 长按，同一次手势会同时触发旧 onDoubleTap 和 ContextSelectionRegistry，造成双注入。
    // 卡头不在此分支（透明 handle 独占拖动），翻面/评分等控件仍由卡片自身处理。
    if (_isCardNote(ctl) && e.target && e.target.closest &&
        e.target.closest('.rc-note-card,.rc-note-html')) return;
    // 手指快速双击 → 临时橡皮(照搬 _epInk 350ms/32px;gate=已有笔画或已是橡皮 → 没写过字的便签双击留给 AI hook)。
    // 第一击不拦截(照常穿透聚焦文字/进 root 双击计数);第二击才吃掉(stopPropagation → 不聚焦、不进 AI 计数)。
    if (e.pointerType === 'touch' && !draw && ((ctl.note.strokes && ctl.note.strokes.length) || NINK.tool === 'eraser')) {
      var now = Date.now(), lt = NINK._lastTap;
      if (lt && now - lt.t < 350 && Math.hypot(e.clientX - lt.x, e.clientY - lt.y) < 32) {
        e.preventDefault(); e.stopPropagation();
        NINK._lastTap = null;
        quickEraseSwitch(ctl);
        return;
      }
      NINK._lastTap = { t: now, x: e.clientX, y: e.clientY };
    }
    // pen 常态直写(v2 规格3,无需任何模式)。两个 reader 的页面 ink 在祖先层 capture 拦截 pen 并
    // stopPropagation → 正常情况下事件到不了这里(由它们经 penBegin/penMove/penEnd 路由,含跨界切割)。
    // 这里是无页面 ink 底座时的独立后备:自管 document 监听,出 body 即截断便签段(绝不画出便签)。
    if (e.pointerType === 'pen') {
      e.preventDefault(); e.stopPropagation();
      try { ctl.body.setPointerCapture(e.pointerId); } catch (err) {}
      penBegin(e, {});
      _selfDraw = true;
      document.addEventListener('pointermove', onSelfMove, true);
      document.addEventListener('pointerup', onSelfUp, true);
      document.addEventListener('pointercancel', onSelfUp, true);
      return;
    }
    if (EDIT && EDIT.ctl === ctl) return;   // 已在本便签 EDIT:body taps 归色板/手柄/双击计数,不再武装长按
    // 手指/鼠标:不拦截 → 事件继续走到 textarea(单击=聚焦输入,v2 规格1);同时武装长按 → EDIT。
    // 长按取消条件=移动超容差/抬手;故意不监听 pointercancel:iOS 在文本上长按会启动选词接管并发
    // pointercancel,若取消则文本区域上的长按永远进不了 EDIT;真滚动必先超容差,已被 move 分支拦下。
    if (_bd) cancelBodyLP();
    _bd = {
      ctl: ctl, sx: e.clientX, sy: e.clientY,
      lp: setTimeout(function () { var b = _bd; cancelBodyLP(); if (b) { try { saveText(b.ctl); } catch (_e) {} try { O && O.onDoubleTap && O.onDoubleTap(b.ctl.note); } catch (_e2) {} } }, lpMs())   // body 长按原地=加入上下文/注入AI(用户方案;body 不含拖动,拖动走 handle 把手)
    };
    document.addEventListener('pointermove', onBodyLPMove, true);
    document.addEventListener('pointerup', cancelBodyLP, true);
  }
  function onBodyLPMove(e) {
    if (!_bd) return;
    if (Math.hypot(e.clientX - _bd.sx, e.clientY - _bd.sy) > LP_TOL) cancelBodyLP();
  }
  function cancelBodyLP() {
    if (!_bd) return;
    clearTimeout(_bd.lp);
    _bd = null;
    document.removeEventListener('pointermove', onBodyLPMove, true);
    document.removeEventListener('pointerup', cancelBodyLP, true);
  }
  function saveText(ctl) {
    var v = ctl.ta.value || '';
    if (v === (ctl.note.text || '')) return;
    ctl.note.text = v;
    patchNote(ctl.note, { text: v });
  }
  function setColor(ctl, c) {
    ctl.note.color = c;
    applyColor(ctl);
    patchNote(ctl.note, { color: c });
  }
  function updateSwatchUI(ctl) {
    var cur = ctl.note.color || DEFAULT_COLOR;
    var sws = ctl.tools.querySelectorAll('.rc-note-swatch');
    for (var i = 0; i < sws.length; i++) sws[i].classList.toggle('on', sws[i].dataset.c === cur);
  }

  // ─────────────────────────── resize(EDIT 模式:右下手柄=原有;左上手柄=位置+尺寸同变)───────────────────────────
  function onResizeDown(ctl, e) {
    if (!(EDIT && EDIT.ctl === ctl)) return;
    e.preventDefault(); e.stopPropagation();
    _rz = { ctl: ctl, sx: e.clientX, sy: e.clientY, w0: ctl.note.w || 260, h0: ctl.note.h || 180, raf: null };
    try { ctl.rs.setPointerCapture(e.pointerId); } catch (err) {}
    document.addEventListener('pointermove', onResizeMove, true);
    document.addEventListener('pointerup', onResizeUp, true);
    document.addEventListener('pointercancel', onResizeUp, true);
  }
  function onResizeMove(e) {
    if (!_rz) return;
    e.preventDefault();
    _rz.ctl.note.w = Math.max(120, Math.min(720, Math.round(_rz.w0 + e.clientX - _rz.sx)));
    _rz.ctl.note.h = Math.max(80, Math.min(720, Math.round(_rz.h0 + e.clientY - _rz.sy)));
    if (!_rz.raf) _rz.raf = requestAnimationFrame(function () { if (!_rz) return; _rz.raf = null; applySize(_rz.ctl); });
  }
  function onResizeUp() {
    var r = _rz; if (!r) return;
    _rz = null;
    if (r.raf) cancelAnimationFrame(r.raf);
    document.removeEventListener('pointermove', onResizeMove, true);
    document.removeEventListener('pointerup', onResizeUp, true);
    document.removeEventListener('pointercancel', onResizeUp, true);
    applySize(r.ctl);
    r.ctl._suppressTap = Date.now();
    patchNote(r.ctl.note, { w: r.ctl.note.w, h: r.ctl.note.h });
  }
  // 左上手柄:拖动=尺寸变化 + 便签整体位移(右下角为锚点固定)。w/h 按拖动量反向变(min/max 同右下),
  // 位移补偿 shift = (w0-w, h0-h):碰到 min 后 shift 停增 → 右下角保持钉死。拖动期间用 transform 暂态
  // (锚定铁律的既有例外),松手把 shift 换算回容器归一化并入 anchor,与 w/h 一并 PATCH(同容器内,无需
  // anchorFromPoint 重解析;anchor clamp 0..1 同 ensureMounted 渲染端)。
  function onResizeTLDown(ctl, e) {
    if (!(EDIT && EDIT.ctl === ctl)) return;
    e.preventDefault(); e.stopPropagation();
    _rzTL = { ctl: ctl, sx: e.clientX, sy: e.clientY, w0: ctl.note.w || 260, h0: ctl.note.h || 180, shiftX: 0, shiftY: 0, raf: null };
    try { ctl.rsTL.setPointerCapture(e.pointerId); } catch (err) {}
    document.addEventListener('pointermove', onResizeTLMove, true);
    document.addEventListener('pointerup', onResizeTLUp, true);
    document.addEventListener('pointercancel', onResizeTLUp, true);
  }
  function onResizeTLMove(e) {
    var g = _rzTL; if (!g) return;
    e.preventDefault();
    g.ctl.note.w = Math.max(120, Math.min(720, Math.round(g.w0 - (e.clientX - g.sx))));
    g.ctl.note.h = Math.max(80, Math.min(720, Math.round(g.h0 - (e.clientY - g.sy))));
    g.shiftX = g.w0 - g.ctl.note.w; g.shiftY = g.h0 - g.ctl.note.h;
    if (!g.raf) g.raf = requestAnimationFrame(function () {
      if (_rzTL !== g) return;
      g.raf = null;
      applySize(g.ctl);
      g.ctl.root.style.transform = 'translate(' + g.shiftX + 'px,' + g.shiftY + 'px)';
    });
  }
  function onResizeTLUp() {
    var g = _rzTL; if (!g) return;
    _rzTL = null;
    if (g.raf) cancelAnimationFrame(g.raf);
    document.removeEventListener('pointermove', onResizeTLMove, true);
    document.removeEventListener('pointerup', onResizeTLUp, true);
    document.removeEventListener('pointercancel', onResizeTLUp, true);
    applySize(g.ctl);
    // v4:松手算锚统一走 host anchorFromPoint。目标点=基准位置(清掉 transform 后的 rect,即手势起点)
    // + 最终 shift(transform 本身可能因 rAF 被取消而滞后一帧,不能直接用带 transform 的 rect)。
    // host 解析失败(极端:落点提不出锚)→ 兜底同容器位移补偿:内容锚改 dx/dy(像素,精确等价),旧比例锚改 x/y。
    g.ctl.root.style.transform = '';
    var r0 = null;
    try { r0 = g.ctl.root.getBoundingClientRect(); } catch (e) {}
    var anchor = (r0 && (g.shiftX || g.shiftY)) ? reanchorAt(g.ctl, r0.left + g.shiftX + 4, r0.top + g.shiftY + 4) : null;
    if (anchor) {
      g.ctl.note.anchor = anchor;
    } else if (g.shiftX || g.shiftY) {
      var a = g.ctl.note.anchor, par = g.ctl.root.parentElement;
      if (a && a.off != null) { a.dx = (a.dx || 0) + g.shiftX; a.dy = (a.dy || 0) + g.shiftY; }
      else if (a && par) {
        var r = par.getBoundingClientRect();
        if (r.width && r.height) {
          a.x = Math.max(0, Math.min(1, (a.x || 0) + g.shiftX / r.width));
          a.y = Math.max(0, Math.min(1, (a.y || 0) + g.shiftY / r.height));
        }
      }
    }
    if (g.ctl.portaled) {   // 顶层叠加层(absolute):左上缩放位移并进 left/top(右下角钉死),ensureMounted 早退不动它
      var _cl = parseFloat(g.ctl.root.style.left) || 0, _ct = parseFloat(g.ctl.root.style.top) || 0;
      g.ctl.root.style.left = (_cl + g.shiftX) + 'px';
      g.ctl.root.style.top = (_ct + g.shiftY) + 'px';
      attachPlaceholder(g.ctl);   // 锚已更新 → 占位移到新锚,避免下次 reflow 把便签拉回旧位
    }
    // 词锚卡:拖动 = 重新锚定(沿用这套的原语义,也是用户要的「手动和自动形成
    //   相同的效果」)。只更新 anchor 不更新 card.bind 的话,标记会**留在旧词上**
    //   而卡片跑到别处 —— 又是一个"看着像正常"的错位。
    var _rb = (r0 && (g.shiftX || g.shiftY))
      ? _rebindWord(g.ctl, r0.left + g.shiftX + 4, r0.top + g.shiftY + 4)
      : false;
    ensureMounted(g.ctl.note);   // host 按新锚重算像素位置(左上缩放不跨容器,原地重挂幂等;portaled 时早退)
    g.ctl._suppressTap = Date.now();
    var _pf = { anchor: g.ctl.note.anchor, w: g.ctl.note.w, h: g.ctl.note.h };
    if (_rb) _pf.card = g.ctl.note.card;
    patchNote(g.ctl.note, _pf);
  }

  // ─────────────────────────── 手写:编程式笔路由 API(页面 ink 层调用;跨界三段切割的便签侧)───────────────────────────
  // 页面 ink(pdf_reader.html::_ink* / epub-html.js::_epInk)是整条笔手势的唯一主人:pointerdown/move 时
  // 用 penRoute 查笔尖是否在某展开便签 body 上,进入 → penBegin,期间 → penMove,离开/抬笔 → penEnd。
  // 坐标由本组件归一化到 body 宽高;笔画生命周期与 PATCH 全部内部管理。
  function normPt(ctl, cx, cy) {
    var r = ctl.body.getBoundingClientRect();
    if (!r.width || !r.height) return null;
    var b = inkBox(ctl.note.iar, r.width, r.height);   // 输入坐标同样过 letterbox,与 redrawInk 一致
    return [(cx - r.left - b.ox) / b.w, (cy - r.top - b.oy) / b.h];
  }
  // 视口点命中哪个便签 body(展开态) → note id | null。倒序遍历:后创建的视觉在上,优先命中。
  function penRoute(x, y) {
    if (SHARED_INK_TOOL === 'region') return null;
    if (x && typeof x === 'object') { y = x.clientY; x = x.clientX; }
    for (var i = notes.length - 1; i >= 0; i--) {
      var ctl = ctls[notes[i].id];
      if (!ctl || !ctl.root.isConnected || ctl.note.collapsed) continue;
      var r = ctl.body.getBoundingClientRect();
      if (r.width && r.height && x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) return ctl.note.id;
    }
    return null;
  }
  // 在命中便签上开一段笔画/橡皮。opts.eraser=页面 ink 当前是橡皮(便签侧自身 NINK 为橡皮时同样生效)。
  function penBegin(e, opts) {
    if (SHARED_INK_TOOL === 'region') return false;
    var id = penRoute(e.clientX, e.clientY); if (!id) return false;
    var ctl = ctls[id]; if (!ctl) return false;
    var pt = normPt(ctl, e.clientX, e.clientY); if (!pt) return false;
    if (draw) penEnd();   // 防御:上一段未收尾
    if (!ctl.note.strokes) ctl.note.strokes = [];
    var eraser = !!(opts && opts.eraser) || NINK.tool === 'eraser';
    if (eraser) {
      if (NINK.quickErase) clearTimeout(NINK._revertT);   // 正在擦 → 暂停自动回笔计时(抬笔再重启,照搬)
      eraseAt(ctl, pt);
      draw = { ctl: ctl, eraser: true, raf: null };
    } else {
      // 首次落笔锚定当前宽高比:之后 resize 按此比例 letterbox 绘制,笔画不随便签比例变形。
      // pt 已按当前(尚未变形)几何算出,此刻 iar==w/h → letterbox 为整幅,首点坐标不受影响。
      if (!ctl.note.strokes.length && !ctl.note.iar) {
        ctl.note.iar = (ctl.note.w || 260) / (ctl.note.h || 180);
        patchNote(ctl.note, { iar: ctl.note.iar });
      }
      var s = { c: strokeColor(ctl), w: INK.width, pts: [pt] };   // 新笔画色:自动对比色开=按便签底色取对比前景
      ctl.note.strokes.push(s);
      draw = { ctl: ctl, stroke: s, raf: null };
      redrawInk(ctl);
    }
    return true;
  }
  function penMove(e) {
    var d = draw; if (!d) return;
    // 笔尖滑进了另一张便签(相邻/重叠)→ 内部切段:页面 ink 只分「便签/页面」,哪张便签由这里管
    var id = penRoute(e.clientX, e.clientY);
    if (id && id !== d.ctl.note.id) {
      var er = !!d.eraser;
      penEnd({ boundary: true });
      penBegin(e, { eraser: er });
      return;
    }
    if (d.eraser) { var p = normPt(d.ctl, e.clientX, e.clientY); if (p) eraseAt(d.ctl, p); return; }
    var evs = e.getCoalescedEvents ? e.getCoalescedEvents() : [e];   // 高频笔合并采样(照搬 _epInk)
    for (var i = 0; i < evs.length; i++) {
      var p2 = normPt(d.ctl, evs[i].clientX, evs[i].clientY); if (!p2) continue;
      var lp = d.stroke.pts[d.stroke.pts.length - 1];
      if (lp) { var dx = p2[0] - lp[0], dy = p2[1] - lp[1]; if (dx * dx + dy * dy < 6e-6) continue; }   // 抽稀近点(防曲线发毛)
      d.stroke.pts.push(p2);
    }
    if (!d.raf) d.raf = requestAnimationFrame(function () { d.raf = null; if (draw === d) redrawInk(d.ctl); });   // rAF 节流(便签 canvas 小,全量重绘廉价)
  }
  // 收尾当前便签段。opts.boundary=跨界切段调用(擦边单点毛段丢弃);抬笔调用不传(单点=有意的点)。
  function penEnd(opts) {
    var d = draw; if (!d) return;
    draw = null;
    if (d.raf) cancelAnimationFrame(d.raf);
    if (d.stroke && opts && opts.boundary && d.stroke.pts.length < 2) {
      var arr = d.ctl.note.strokes || [], i = arr.indexOf(d.stroke);
      if (i >= 0) arr.splice(i, 1);
    }
    redrawInk(d.ctl);
    scheduleStrokeSave(d.ctl);
    d.ctl._suppressTap = Date.now();
    if (d.eraser && NINK.quickErase) armRevert(900);   // 擦完抬笔 0.9s 没再擦 → 自动回笔(照搬)
  }
  // 无页面 ink 底座时的后备自管手势:出 body 截断,回 body(或滑进别的便签)开新段;界外不落墨。
  function onSelfMove(e) {
    if (!_selfDraw) return;
    e.preventDefault();
    var id = penRoute(e.clientX, e.clientY);
    if (draw) {
      if (id === draw.ctl.note.id) { penMove(e); return; }
      penEnd({ boundary: true });
    }
    if (id) penBegin(e, {});
  }
  function onSelfUp() {
    _selfDraw = false;
    document.removeEventListener('pointermove', onSelfMove, true);
    document.removeEventListener('pointerup', onSelfUp, true);
    document.removeEventListener('pointercancel', onSelfUp, true);
    penEnd();
  }
  function eraseAt(ctl, pt) {
    var arr = ctl.note.strokes || [], removed = false;
    var r = ctl.body.getBoundingClientRect();
    var thr = Math.max(0.014, 10 / Math.max(r.width || 1, r.height || 1));   // _epInk 阈值 0.014;便签小 → 保底 10px 命中半径
    for (var i = arr.length - 1; i >= 0; i--) if (strokeHit(arr[i], pt, thr)) { arr.splice(i, 1); removed = true; }
    if (removed) { redrawInk(ctl); scheduleStrokeSave(ctl); }
  }
  function scheduleStrokeSave(ctl) {
    ctl._strokeDirty = true;
    clearTimeout(ctl._strokeT);
    ctl._strokeT = setTimeout(function () { flushStrokeSave(ctl); }, 800);
  }
  function flushStrokeSave(ctl) {
    if (!ctl._strokeDirty) return;
    ctl._strokeDirty = false;
    clearTimeout(ctl._strokeT);
    patchNote(ctl.note, { strokes: ctl.note.strokes || [] });
  }
  // ⚠ 800ms 防抖窗口内关页/切后台会丢最后一批笔画 → 卸载期对所有 pending 便签立即 PATCH
  //   repository 模式直接写扩展本地库；legacy PWA 仍由同一 patch helper 发送。
  function flushAllStrokes() {
    if (!O || (!repoMode() && !O.file)) return;
    for (var id in ctls) {
      var ctl = ctls[id];
      if (!ctl || !ctl._strokeDirty) continue;
      ctl._strokeDirty = false;
      clearTimeout(ctl._strokeT);
      patchNote(ctl.note, { strokes: ctl.note.strokes || [] });
    }
  }
  window.addEventListener('pagehide', flushAllStrokes);
  document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'hidden') flushAllStrokes(); });
  // 工具切换(工具条按钮=持久切;手指双击=临时橡皮 quickErase,空闲自动回笔 —— 照搬 _epInk 语义;
  // NINK 全局一支笔:所有便签共享工具态,同页面 ink 惯例)
  function setTool(ctl, t, temp) {
    clearTimeout(NINK._revertT); NINK._revertT = null;
    NINK.tool = t;
    NINK.quickErase = !!temp && t === 'eraser';
    updateToolUI(ctl);
  }
  function quickEraseSwitch(ctl) {
    if (NINK.tool === 'eraser') { setTool(ctl, 'pen', false); toastMsg('✏️ 已回到笔'); }   // 再次双击 → 立刻回笔
    else { NINK.tool = 'eraser'; NINK.quickErase = true; updateToolUI(ctl); armRevert(2500); toastMsg('🧹 临时橡皮(空闲自动回笔)'); }
  }
  function armRevert(ms) {
    clearTimeout(NINK._revertT);
    NINK._revertT = setTimeout(function () {
      if (draw && draw.eraser) { armRevert(400); return; }   // 正在擦 → 顺延(照搬)
      if (NINK.quickErase) { NINK.quickErase = false; NINK.tool = 'pen'; updateToolUI(null); toastMsg('✏️ 已回到笔'); }
    }, ms);
  }
  function updateToolUI(ctl) {
    var region = SHARED_INK_TOOL === 'region';
    var er = !region && NINK.tool === 'eraser';
    var upd = function (c) {
      var b = c.tools.querySelector('.rc-note-tool');
      if (!b) return;
      b.classList.toggle('on', er);
      b.disabled = region;
      b.setAttribute('aria-disabled', region ? 'true' : 'false');
      b.textContent = region ? '□' : (er ? '🧹' : '✒️');
      b.title = region ? '选区笔只作用于书页，便签不接收本次笔迹' : (er ? '橡皮中(点回笔;划过笔画删除整条)' : '笔(点切橡皮;手指快速双击也可临时切换)');
    };
    if (ctl) upd(ctl);
    for (var id in ctls) if (!ctl || ctls[id] !== ctl) upd(ctls[id]);   // NINK 全局 → 所有便签按钮同步
  }

  // 原生画布 -> 便签的有界同步入口。三个参数必须一次性全部通过校验，
  // 非法调用不允许留下“工具改了但颜色没改”之类的半状态。
  function synchronizeInkToolStyle(tool, color, width) {
    if (arguments.length !== 3) return false;
    if (tool !== 'pen' && tool !== 'eraser' && tool !== 'region') return false;
    if (typeof color !== 'string' || !/^#[0-9a-fA-F]{6}$/.test(color)) return false;
    if (typeof width !== 'number' || !isFinite(width) || width < 1 || width > 16) return false;

    SHARED_INK_TOOL = tool;
    SHARED_INK_STYLE_ACTIVE = true;
    INK.color = color.toLowerCase();
    INK.width = width;
    clearTimeout(NINK._revertT);
    NINK._revertT = null;
    NINK.quickErase = false;
    if (tool === 'pen' || tool === 'eraser') NINK.tool = tool;
    updateToolUI(null);
    return true;
  }

  // ─────────────────────────── 点便签外 → 退出 EDIT 编辑模式(保存待存项)───────────────────────────
  function onDocDown(e) {
    var t = e.target;
    var insideRoot = (t && t.closest) ? t.closest('.rc-note') : null;
    if (EDIT && insideRoot !== EDIT.ctl.root) exitEdit();
    // 点便签外(或点到另一张便签)→ 把「不是当前被点的」portaled 便签放回下层(点别处即降层;只留被点那张在顶)。
    //   capture 阶段先于 onBodyDown/onHandleDown 跑:被点那张稍后由其 pointerdown 再 portalIn(不受影响)。
    for (var id in ctls) {
      var c = ctls[id];
      if (c && c.portaled && c.root !== insideRoot && !_hasLiveVid(c)) portalOut(c);   // 有活视频不降层:portalOut 的 reparent 会重载 iframe=播放中断(用户规格:点外面不碰播放)
    }
  }

  // ─────────────────────────── 创建 ───────────────────────────
  function createRecord(anchor, fields, pendingMessage, mountedMessage) {
    var generation = _generation;
    fields = fields || {};
    fields.anchor = anchor;
    ioCreate(fields, generation).then(function (note) {
      if (generation !== _generation) return;
      // legacy vbook 响应锚可能使用成员局部页；当前视图应继续使用请求时锚。
      if (note && note.anchor && anchor) note.anchor = anchor;
      var mounted = !!upsertRecord(note, generation);
      if (!mounted || !ctls[noteIdOf(note)] || !ctls[noteIdOf(note)].root.isConnected) {
        if (pendingMessage) toastMsg(pendingMessage);
      } else if (mountedMessage) {
        toastMsg(mountedMessage);
      }
    }).catch(function (error) {
      ioError(error, '便签创建失败');
    });
  }
  function createAt(anchor) {
    if (!O) return;
    if (!anchor) { toastMsg('这里放不了便签(不在内容上)'); return; }
    createRecord(
      anchor,
      { color: DEFAULT_COLOR, w: 260, h: 180 },
      '便签已创建(所在页尚未渲染,渲染后出现)'
    );
  }
  /* 带内容地在视野中央建一张便签 —— 给助手用。
   *
   * 助手没有指针，指不出"贴在哪"，但它知道要写什么。所以位置由这里用
   * 与用户点"新建便签"完全相同的落点逻辑决定（中央优先，落在页缝/空白
   * 就试附近候选），内容由调用方给。
   *
   * 走 createRecord 而不是另写一条持久化：便签的锚定、代次校验、渲染与
   * 失败提示都在那条路上，绕过去就会得到一张存下来但不显示、
   * 或者显示了却没存的便签。
   */
  function createAtCenterWithText(text) {
    var body = String(text == null ? '' : text);
    if (!body.trim()) { toastMsg('便签内容为空'); return false; }
    if (body.length > 4000) { toastMsg('便签内容过长'); return false; }
    if (!O || !O.anchorFromPoint) { toastMsg('当前界面不支持便签'); return false; }
    var w = window.innerWidth || 1024, h = window.innerHeight || 768;
    var cands = [[w / 2, h / 2], [w / 2, h * 0.42], [w / 2, h * 0.58], [w / 2, h * 0.33], [w / 2, h * 0.66], [w * 0.4, h / 2], [w * 0.6, h / 2]];
    var anchor = null;
    for (var i = 0; i < cands.length && !anchor; i++) {
      try { anchor = O.anchorFromPoint(cands[i][0], cands[i][1]); } catch (e) {}
    }
    // 落不下就说出来。悄悄不建的话，助手会以为写成功了并这样告诉用户。
    if (!anchor) { toastMsg('这里放不了便签(把页面滚到正文上再试)'); return false; }
    createRecord(
      anchor,
      { color: DEFAULT_COLOR, w: 260, h: 180, text: body },
      '便签已创建(所在页尚未渲染,渲染后出现)'
    );
    return true;
  }

  function createAtCenter() {
    if (!O || !O.anchorFromPoint) return;
    var w = window.innerWidth || 1024, h = window.innerHeight || 768;
    // 视野中央落点;中央可能落在页缝/空白 → 附近候选点依次重试
    var cands = [[w / 2, h / 2], [w / 2, h * 0.42], [w / 2, h * 0.58], [w / 2, h * 0.33], [w / 2, h * 0.66], [w * 0.4, h / 2], [w * 0.6, h / 2]];
    var anchor = null;
    for (var i = 0; i < cands.length && !anchor; i++) {
      try { anchor = O.anchorFromPoint(cands[i][0], cands[i][1]); } catch (e) {}
    }
    createAt(anchor);
  }
  // 阶段 B:从助手视频卡拖到书页某点 → 在该点建**视频便签**(锚点就近重试,复用 note.video 全套控件)
  function createVideoAt(clientX, clientY, videoId, meta) {
    if (!O || !O.anchorFromPoint || !videoId) return false;
    meta = meta || {};
    var cands = [[clientX, clientY], [clientX, clientY - 22], [clientX, clientY + 22], [clientX - 30, clientY], [clientX + 30, clientY]];
    var anchor = null;
    for (var i = 0; i < cands.length && !anchor; i++) { try { anchor = O.anchorFromPoint(cands[i][0], cands[i][1]); } catch (e) {} }
    if (!anchor) { toastMsg('这里放不了(把视频拖到正文上再松手)'); return false; }
    // 视频对象带 thumb+src(B站缩略图无 URL 规律须存;src 决定播放器/embed 走 B站还是 YT)
    var _vid = { id: videoId, start: 0, end: 0, rate: 1, loop: false, cc: true };
    if (meta.thumb) _vid.thumb = meta.thumb;
    if (meta.src) _vid.src = meta.src;
    createRecord(
      anchor,
      { color: DEFAULT_COLOR, w: 300, h: 210, collapsed: false, video: _vid },
      '视频便签已建(所在页尚未渲染,渲染后出现)',
      '✅ 视频已放进书页便签'
    );
    return true;
  }

  // 卡片便签:从制卡卡 📌 钉页 / 真机圆球拖出 → 在该屏幕点建带 card 的便签(复用 rc-flashcard 全套状态机)
  function createCardAt(clientX, clientY, cards, gid) {
    if (!O || !O.anchorFromPoint || !cards || !cards.length) return false;
    var cands = [[clientX, clientY], [clientX, clientY - 22], [clientX, clientY + 22], [clientX - 30, clientY], [clientX + 30, clientY]];
    var anchor = null;
    for (var i = 0; i < cands.length && !anchor; i++) { try { anchor = O.anchorFromPoint(cands[i][0], cands[i][1]); } catch (e) {} }
    if (!anchor) { toastMsg('这里放不了(把卡片放到正文上再松手)'); return false; }
    // ── 词锚（用户设计 2026-08-20）：落点吸附到最近的分词元素 ──────────
    //   手动钉和 AI 自动钉要产出**同一种标记**，所以这里也解析出词区间。
    //   吸附范围沿用拖动反馈那条（≤48px），两者必须一致，否则「看到框但钉别处」。
    //
    //   ⚠ 词锚放进 **card 载荷**里，不是放进 anchor —— anchor 在
    //     native-local-runtime.js::normalizedNoteAnchor 里是**逐字段重建**的，
    //     加新字段会被静默 strip（本次会话看不出来，下次开书才发现锚退化了）；
    //     而 card 是 boundedCanonicalJSON 整块不透明存，加字段两侧都不用改。
    var _wb = null;
    try {
      var _wr = O.noteWordRect ? O.noteWordRect(clientX, clientY) : null;
      if (_wr && _wr.page > 0 && _wr.text && (_wr.dist == null || _wr.dist <= 48)) {
        _wb = { kind: 'page-chars', page: _wr.page,
                from: _wr.from, to: _wr.to, text: String(_wr.text).slice(0, 200) };
      }
    } catch (e) {}
    var cid0 = gid || ((window.RC && RC.voiceCard && RC.voiceCard.mkCid) ? ('fcg_' + RC.voiceCard.mkCid()) : ('fcg_' + Date.now().toString(36)));
    gid = gid || cid0;
    var w0 = 300, bw0 = 0; try { var mm = O.mount(anchor); if (mm && mm.el && mm.el.clientWidth) { bw0 = mm.el.clientWidth; w0 = Math.max(240, Math.min(480, Math.round(bw0 * 0.44))); } } catch (e) {}   // 卡宽按页面宽自适应+记创建时页宽(缩放等比跟随,用户拍板)
    // gid/cid 原样携带，同一学习卡组跨宿主共享；repository 不重编号业务卡。
    createRecord(
      anchor,
      { color: '#0d1322', w: w0, h: 210, collapsed: false, card: { cards: cards, gid: gid, cid: cid0, base_w: bw0, bind: _wb } },
      '卡片便签已建(所在页尚未渲染,渲染后出现)',
      _wb ? ('✅ 已钉到「' + _wb.text.slice(0, 12) + '」') : '✅ 卡片已钉到书页'
    );
    return true;
  }

  // 通用卡便签:天气/搜索/图/文字等 vc-card 的 HTML 快照 → 钉页。
  // 允许的交互由共享模块按自描述 data-* 属性做全局委托；便签不持久化闭包。
  function createHtmlAt(clientX, clientY, htmlObj) {
    if (!O || !O.anchorFromPoint || !htmlObj || !htmlObj.content) return false;
    var cands = [[clientX, clientY], [clientX, clientY - 22], [clientX, clientY + 22], [clientX - 30, clientY], [clientX + 30, clientY]];
    var anchor = null;
    for (var i = 0; i < cands.length && !anchor; i++) { try { anchor = O.anchorFromPoint(cands[i][0], cands[i][1]); } catch (e) {} }
    if (!anchor) { toastMsg('这里放不了(把卡片放到正文上再松手)'); return false; }
    var cid1 = htmlObj.cid || ((window.RC && RC.voiceCard && RC.voiceCard.mkCid) ? RC.voiceCard.mkCid() : ('c' + Date.now().toString(36)));
    var w1 = 300, bw1 = 0; try { var mh = O.mount(anchor); if (mh && mh.el && mh.el.clientWidth) { bw1 = mh.el.clientWidth; w1 = Math.max(240, Math.min(480, Math.round(bw1 * 0.44))); } } catch (e) {}   // 卡宽按页面宽自适应+记创建时页宽
    // cid 原样持久化，重开不换号。
    createRecord(
      anchor,
      { color: '#0d1322', w: w1, h: 210, collapsed: false, html: { content: htmlObj.content, isHtml: !!htmlObj.isHtml, label: htmlObj.label || '', type: htmlObj.type || '', icon: htmlObj.icon || '', cid: cid1, base_w: bw1 } },
      '卡片便签已建(所在页尚未渲染,渲染后出现)',
      '✅ 卡片已钉到书页'
    );
    return true;
  }

  // ── 拖动锚定反馈层(#51 用户设计,所有拖动统一挂:便签拖动/单图拖出/侧栏拖出/钉子卡拖动)──
  function _probeHidden(el, fn) {
    // 探测穿透(#51 修:拖动物自己压住内容,elementFromPoint/caretFromPoint 全命中它→恒横线/EPUB 甚至锚到便签自身文字)
    // 同步 隐藏→探测→恢复 在同一帧内完成,不产生可见闪烁(拖拽库标准做法)。
    if (!el) return fn();
    var pv = el.style.visibility;
    el.style.visibility = 'hidden';
    try { return fn(); } finally { el.style.visibility = pv || ''; }
  }
  var _afx = null;
  function anchorFxShow(cx, cy, ignoreEl) {
    // #51 粒度=单词(用户设计):近文字(词中心 ≤48px)→**单词精确框**(锚定=绑到这个词,注入=词所在句后方);
    //   超范围/空白/clamp → 横线(内容插入位置=排到上方内容/段落之后)。旧 20px 光带退役。
    if (!O) return;
    if (!_afx) { _afx = document.createElement('div'); _afx.className = 'rc-anchor-fx'; }
    var wr = null, _aa = null;
    try {
      _probeHidden(ignoreEl, function () {
        try { wr = O.noteWordRect ? O.noteWordRect(cx, cy) : null; } catch (e) {}
        if (!wr) { try { _aa = O.anchorFromPoint ? O.anchorFromPoint(cx, cy) : null; } catch (e) {} }
      });
    } catch (e) {}
    if (wr && wr.el && (wr.dist == null || wr.dist <= 48)) {
      _afx.classList.add('rc-afx-word'); _afx.classList.remove('rc-afx-line');
      if (_afx.parentElement !== wr.el) wr.el.appendChild(_afx);
      _afx.style.left = (wr.left - 3) + 'px'; _afx.style.right = 'auto';
      _afx.style.top = (wr.top - 2) + 'px';
      _afx.style.width = (wr.width + 6) + 'px'; _afx.style.height = (wr.height + 4) + 'px';
      _afx.style.display = 'block';
      return;
    }
    var a = _aa;
    if (!a) { anchorFxHide(); return; }
    var m = null; try { m = O.mount(a); } catch (e) {}
    if (!m || !m.el || typeof m.top !== 'number') { anchorFxHide(); return; }
    _afx.classList.add('rc-afx-line'); _afx.classList.remove('rc-afx-word');
    if (_afx.parentElement !== m.el) m.el.appendChild(_afx);
    _afx.style.left = '8px'; _afx.style.right = '8px'; _afx.style.width = 'auto'; _afx.style.height = '0';
    _afx.style.top = Math.max(0, m.top - 1) + 'px';
    _afx.style.display = 'block';
  }
  function anchorFxHide() { if (_afx) _afx.style.display = 'none'; }

  function teardownStorage(clearOptions) {
    var priorOptions = O;
    _generation += 1;
    if (_unsubscribe) {
      try { _unsubscribe(); } catch (error) { ioError(error, '便签订阅关闭失败'); }
      _unsubscribe = null;
    }
    _writeQueues = Object.create(null);
    _seenRevs = Object.create(null);
    // teardown 只撤销当前交互，不允许 resize/pen 的收尾 PATCH 在 O 被下一页替换后
    // 错写到新 document；正常 pagehide/visibilitychange 已有独立 flush。
    O = null;
    exitEdit();
    cancelHandleGesture();
    cancelBodyLP();
    if (_rz) onResizeUp();
    if (_rzTL) onResizeTLUp();
    if (draw) penEnd();
    removeAllEls();
    notes = [];
    O = clearOptions ? null : priorOptions;
  }

  // ─────────────────────────── 公开 API ───────────────────────────
  RC.stickynote = {
    // opts:
    // legacy PWA: {file,...} → /pdf/api/notes；
    // 扩展/已迁移宿主: {documentId,repository,...}，repository 是 identity-scoped
    // {newId,list,get,create,patch,remove,subscribe}，此模式绝不访问 notes HTTP。
    init: function (opts) {
      teardownStorage(false);
      O = opts || {};
      var generation = _generation;
      injectCss();
      if (!_docBound) { _docBound = true; document.addEventListener('pointerdown', onDocDown, true); }
      if (repoMode()) {
        if (!repoReady()) {
          ioError(new Error('缺少 documentId 或 repository 方法'), '便签初始化失败');
          return;
        }
        try {
          var subscribed = O.repository.subscribe(function (event) {
            onRepositoryChange(event, generation);
          });
          Promise.resolve(subscribed).then(function (unsubscribe) {
            if (generation !== _generation) {
              if (typeof unsubscribe === 'function') try { unsubscribe(); } catch (_) {}
              return;
            }
            if (typeof unsubscribe !== 'function') {
              ioError(new Error('subscribe 没有返回 unsubscribe'), '便签初始化失败');
              return;
            }
            _unsubscribe = unsubscribe;
          }).catch(function (error) {
            if (generation === _generation) ioError(error, '便签订阅失败');
          });
        } catch (error) {
          ioError(error, '便签订阅失败');
        }
      }
      this.loadAll();
    },
    // LIST 全部便签。repository 模式增量合并（CHANGE 可先于 LIST）；
    // legacy PWA 保持旧的全量替换语义。
    loadAll: function () {
      if (!O || (!repoMode() && !O.file)) return;
      var generation = _generation;
      // 只允许清理“加载开始时已经存在、且整个 LIST 期间 revision 没变”的
      // absent note。CHANGE 在分页途中创建/更新的记录不在快照或 rev 已提高，
      // 不会被旧 LIST 误删。
      var before = Object.create(null);
      if (repoMode()) {
        for (var n = 0; n < notes.length; n++) {
          before[noteIdOf(notes[n])] = Number(notes[n].rev) || 0;
        }
      }
      ioList(generation).then(function (items) {
        if (generation !== _generation) return;
        if (repoMode()) {
          var returned = Object.create(null);
          for (var i = 0; i < items.length; i++) {
            returned[noteIdOf(items[i])] = true;
            upsertRecord(items[i], generation);
          }
          var reconcile = [];
          Object.keys(before).forEach(function (id) {
            if (returned[id]) return;
            var current = currentNote(id);
            if (current && (Number(current.rev) || 0) === before[id]) {
              // 分页过程中插入/删除会移动 offset，单凭“本轮 LIST 缺失”
              // 不能判定已删除；对少量 absent 候选做一次点查，避免误删仍存在
              // 的便签，同时补回错位分页漏项。
              reconcile.push(Promise.resolve(O.repository.get(id, {
                includeDeleted: true
              })).then(function (latest) {
                if (generation !== _generation) return;
                var now = currentNote(id);
                if (!now || (Number(now.rev) || 0) !== before[id]) return;
                if (!latest || latest.deleted === true) {
                  if (latest) upsertRecord(latest, generation);
                  else {
                    _seenRevs[id] = Math.max(Number(_seenRevs[id]) || 0, before[id]);
                    removeLocal(id);
                  }
                } else {
                  upsertRecord(latest, generation);
                }
              }).catch(function (error) {
                // 点查失败时宁可保留旧 UI，也绝不能把存在的便签误删。
                ioError(error, '便签列表核对失败');
              }));
            }
          });
          return Promise.all(reconcile).then(function () {
            if (generation === _generation) mountAll();
          });
        } else {
          removeAllEls();
          notes = items || [];
          mountAll();
        }
      }).catch(function (error) {
        ioError(error, '便签加载失败');
      });
    },
    // 容器就绪(EPUB 章节加载完 / PDF 页渲染完)时由 reader 调:幂等重挂未挂/被重渲清掉的便签
    mountPending: mountAll,
    // v4 重定位:重排(侧栏开关/字号/行距/栏宽/window resize)后由 host 调,对每个已挂载 ctl 重跑
    // host mount 定位(内容锚字符位置变了 → 便签跟着字走)。与 mountPending 同一实现(ensureMounted 幂等)。
    repositionAll: mountAll,
    // 指定锚点建默认便签(anchor 已归一化;null → toast 提示)
    createAt: createAt,
    // 视野中央建默认便签(顶栏 🗒 按钮入口;经 opts.anchorFromPoint 解析,页缝自动就近重试)
    createAtCenter: createAtCenter,
    createAtCenterWithText: createAtCenterWithText,
    // 阶段 B:拖放建视频便签(助手视频卡长按拖到书页 → 该屏幕点建带 video 的便签)
    createVideoAt: createVideoAt,
    createCardAt: createCardAt,   // 卡片便签(制卡卡 📌 钉页 / 真机拖出复用)
    createHtmlAt: createHtmlAt,   // 通用卡便签(天气/搜索/图等 vc-card 钉页)
    cardContextText: cardContextText,   // 收藏/上下文共用正面+背面可读投影；raw/meta 仍保留完整卡记录
    bindCardSelection: bindCardSelection,   // 固定学习卡整卡长按：PWA/普通网页共用同一语义与完整快照
    bindHtmlCardSelection: bindHtmlCardSelection,   // 固定工具/HTML 卡正文长按：同 cid 处处同步，不占用卡头拖拽
    anchorFx: { show: anchorFxShow, hide: anchorFxHide },   // 拖动锚定反馈(#51:光带=绑定内容/横线=插入位置)
    // ── 笔路由接口(页面 ink 层用,跨界三段切割;见上「编程式笔路由 API」注释)──
    penRoute: penRoute,     // (x,y|event) -> noteId|null  笔尖是否在某展开便签 body 上
    penBegin: penBegin,     // (event, {eraser?}) -> bool  在命中便签开一段(坐标归一化/PATCH 内部管理)
    penMove: penMove,       // (event)                     追加点(coalesced+抽稀+rAF 重绘)
    penEnd: penEnd,         // ({boundary?})               收尾当前段(boundary=跨界切段,擦边单点丢弃)
    penActive: function () { return !!draw; },
    // 原生 PencilKit 共享工具同步；region 仅使便签让出笔路由，不创建便签私有选区。
    synchronizeInkToolStyle: synchronizeInkToolStyle,
    // 外观设置(rc-note-opacity / rc-note-autocontrast)变更后即时应用:对每个已挂载 ctl 重跑 applyColor
    // (rc-settings「便签」tab 保存时调;已有笔画是数据不改色,只影响底色/前景/新笔画)
    refreshStyle: function () { for (var id in ctls) { try { applyColor(ctls[id]); } catch (e) {} } },
    // 卸载全部便签 DOM + 清状态(teardown，包括 repository subscribe)
    removeAll: function () { teardownStorage(true); },
    notes: function () { return notes; }
  };
})();
