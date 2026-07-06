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

  var O = null;        // opts
  var notes = [];      // 服务端便签列表(本地镜像)
  var ctls = {};       // id -> controller {note,root,handle,body,ta,cv,tools,rs,rsTL,...}
  var EDIT = null;     // 编辑模式(长按便签任意部分){note, ctl}:🗑+色板+双缩放手柄+handle 拖拽移动
  var NINK = { tool: 'pen', quickErase: false, _revertT: null, _lastTap: null };  // 便签手写工具态(全局一支笔,同页面 ink 惯例)
  var draw = null;     // 手写进行中 {ctl, stroke|eraser, raf}
  var _selfDraw = false; // 无页面 ink 底座时的后备自管手势进行中
  var _hd = null;      // handle 手势 {ctl, sx, sy, lp, dragging, moved, rect0}
  var _bd = null;      // body 长按 {ctl, sx, sy, lp}
  var _rz = null;      // 右下 resize {ctl, sx, sy, w0, h0, raf}
  var _rzTL = null;    // 左上 resize {ctl, sx, sy, w0, h0, shiftX, shiftY, raf}(右下角锚定:位置+尺寸同变)
  var _docBound = false;

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
      // handle:短矩形,常驻,显示便签色;::before 扩触控区 ≥32px(视觉小、命中大)
      '.rc-note-handle{position:relative;width:56px;height:20px;border-radius:7px;border:1px solid rgba(0,0,0,.28);box-shadow:0 2px 6px rgba(0,0,0,.3);cursor:grab;touch-action:none;-webkit-touch-callout:none;-webkit-user-select:none;user-select:none}',
      '.rc-note-handle::before{content:"";position:absolute;left:-8px;right:-8px;top:-8px;bottom:-8px}',
      '.rc-note-handle::after{content:"";position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:22px;height:4px;border-radius:2px;background:rgba(0,0,0,.22)}',
      // 浮起特效(阴影加深+微放大+轻微透明):只在 handle 拖拽进行时(EDIT 模式内静止不浮)
      '.rc-note.rc-note-lift{transform:scale(1.03);opacity:.92}',
      '.rc-note.rc-note-lift .rc-note-handle{cursor:grabbing;box-shadow:0 10px 26px rgba(0,0,0,.5)}',
      '.rc-note.rc-note-lift .rc-note-body{box-shadow:0 12px 30px rgba(0,0,0,.45)}',
      '.rc-note-del{position:absolute;left:100%;top:50%;transform:translate(12px,-50%);width:34px;height:34px;border-radius:50%;border:1px solid #e05a5a;background:#fff2f2;color:#c62828;font-size:15px;line-height:1;display:none;align-items:center;justify-content:center;cursor:pointer;box-shadow:0 4px 10px rgba(0,0,0,.35);padding:0}',
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

  // ─────────────────────────── API ───────────────────────────
  function req(method, body, cb) {
    fetch(API, { method: method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(function (r) { return r.json(); })
      .then(function (d) { if (cb) cb(d); })
      .catch(function () { if (cb) cb(null); });
  }
  function patchNote(note, fields, cb) {
    var b = { file: O.file, id: note.id };
    for (var k in fields) if (Object.prototype.hasOwnProperty.call(fields, k)) b[k] = fields[k];
    req('PATCH', b, cb);
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
  }
  function strokeHit(s, pt, thr) { return RCInk.hit(s, pt, thr); }

  // ─────────────────────────── controller 构建 ───────────────────────────
  function buildCtl(note) {
    var root = document.createElement('div');
    root.className = 'rc-note';
    root.dataset.noteId = note.id;
    root.innerHTML =
      '<div class="rc-note-handle"><button class="rc-note-del" title="删除便签">🗑</button></div>' +
      '<div class="rc-note-body">' +
        '<div class="rc-note-video"></div>' +
        '<textarea class="rc-note-text" placeholder="输入文字…(笔=手写)"></textarea>' +
        '<canvas class="rc-note-ink"></canvas>' +
        '<div class="rc-note-tools"></div>' +
        '<div class="rc-note-rs" title="拖动调整大小"></div>' +
        '<div class="rc-note-rs-tl" title="拖动调整大小(右下角固定)"></div>' +
      '</div>';
    var ctl = {
      note: note, root: root,
      handle: root.querySelector('.rc-note-handle'),
      del: root.querySelector('.rc-note-del'),
      body: root.querySelector('.rc-note-body'),
      ta: root.querySelector('.rc-note-text'),
      cv: root.querySelector('.rc-note-ink'),
      video: root.querySelector('.rc-note-video'),
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
  function vEmbedSrc(v) {
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
  function renderNoteVideo(ctl) {
    var v = ctl.note.video, box = ctl.video; if (!box) return;
    if (!v || !v.id) { ctl.root.classList.remove('rc-note-hasvideo'); box.innerHTML = ''; box.__sig = ''; ctl.__vif = null; return; }
    ctl.root.classList.add('rc-note-hasvideo');
    var sig = JSON.stringify(v);
    if (box.__sig === sig) return;   // 无变化不重建(防控件输入时闪 iframe)
    box.__sig = sig; ctl.__vif = null;
    box.innerHTML =
      '<div class="rc-vid-embed"><img loading="lazy" src="https://i.ytimg.com/vi/' + v.id + '/mqdefault.jpg" alt=""><button class="rc-vid-go" aria-label="播放">▶</button></div>' +
      '<div class="rc-vid-ctrls">' +
        '<label>起<input class="rc-vc-sm" inputmode="numeric" maxlength="3" placeholder="0">'+'<span class="rc-vc-cn">:</span>'+'<input class="rc-vc-ss" inputmode="numeric" maxlength="2" placeholder="00">'+'<button class="rc-vc-now" data-w="start" title="设为当前播放位置">⏱</button></label>' +
        '<label>止<input class="rc-vc-em" inputmode="numeric" maxlength="3" placeholder="—">'+'<span class="rc-vc-cn">:</span>'+'<input class="rc-vc-es" inputmode="numeric" maxlength="2" placeholder="00">'+'<button class="rc-vc-now" data-w="end" title="设为当前播放位置">⏱</button></label>' +
        '<label>速<select class="rc-vc-rate"><option>0.5</option><option>0.75</option><option>1</option><option>1.25</option><option>1.5</option><option>2</option></select></label>' +
        '<label class="rc-vc-ck"><input type="checkbox" class="rc-vc-loop">循环</label>' +
        '<label class="rc-vc-ck"><input type="checkbox" class="rc-vc-cc">字幕</label>' +
        '<button class="rc-vc-rm" title="移除视频(变回普通便签)">✕</button>' +
      '</div>';
    var emb = box.querySelector('.rc-vid-embed');
    var _fillT = function (mSel, sSel, secs) { box.querySelector(mSel).value = secs ? Math.floor(secs / 60) : ''; box.querySelector(sSel).value = secs ? ('0' + (secs % 60)).slice(-2) : ''; };
    _fillT('.rc-vc-sm', '.rc-vc-ss', v.start || 0);
    _fillT('.rc-vc-em', '.rc-vc-es', v.end || 0);
    box.querySelector('.rc-vc-rate').value = String(v.rate || 1);
    box.querySelector('.rc-vc-loop').checked = !!v.loop;
    box.querySelector('.rc-vc-cc').checked = v.cc !== false;
    var loadFrame = function () {
      if (emb.querySelector('iframe')) return;
      var f = document.createElement('iframe'); f.className = 'rc-vid-if';
      f.allow = 'autoplay; encrypted-media; picture-in-picture; fullscreen'; f.setAttribute('allowfullscreen', '');
      f.src = vEmbedSrc(v);
      emb.innerHTML = ''; emb.appendChild(f); ctl.__vif = f; _hookVidMsg();
      f.addEventListener('load', function () {
        setRate(ctl, v.rate || 1);
        try { f.contentWindow.postMessage('{"event":"listening"}', '*'); } catch (e) {}   // 注册 → YT 周期推 infoDelivery(含 currentTime)
      });
    };
    emb.querySelector('.rc-vid-go').addEventListener('click', loadFrame);
    // 控件改动 → 更新 note.video → 存后端 → 起止/循环/字幕改 URL 参数须重载 iframe;速度用 postMessage
    var applyPatch = function (patch, needReload) {
      for (var k in patch) v[k] = patch[k];
      box.__sig = JSON.stringify(v);   // 自己改的:更新签名,避免 syncCtl 回灌重建
      patchNote(ctl.note, { video: v });
      if (needReload && ctl.__vif) { ctl.__vif.src = vEmbedSrc(v); ctl.__vif.addEventListener('load', function () { setRate(ctl, v.rate || 1); }, { once: true }); }
    };
    var _readT = function (mSel, sSel) { var m = parseInt(box.querySelector(mSel).value || '0', 10) || 0; var s2 = parseInt(box.querySelector(sSel).value || '0', 10) || 0; return m * 60 + Math.max(0, Math.min(59, s2)); };
    box.querySelector('.rc-vc-sm').addEventListener('change', function () { applyPatch({ start: _readT('.rc-vc-sm', '.rc-vc-ss') }, true); });
    box.querySelector('.rc-vc-ss').addEventListener('change', function () { applyPatch({ start: _readT('.rc-vc-sm', '.rc-vc-ss') }, true); });
    box.querySelector('.rc-vc-em').addEventListener('change', function () { applyPatch({ end: _readT('.rc-vc-em', '.rc-vc-es') }, true); });
    box.querySelector('.rc-vc-es').addEventListener('change', function () { applyPatch({ end: _readT('.rc-vc-em', '.rc-vc-es') }, true); });
    box.querySelector('.rc-vc-rate').addEventListener('change', function () { var r = parseFloat(this.value) || 1; applyPatch({ rate: r }, false); setRate(ctl, r); });
    box.querySelector('.rc-vc-loop').addEventListener('change', function () { applyPatch({ loop: this.checked }, true); });
    box.querySelector('.rc-vc-cc').addEventListener('change', function () { applyPatch({ cc: this.checked }, true); });
    Array.prototype.forEach.call(box.querySelectorAll('.rc-vc-now'), function (btn) {
      btn.addEventListener('click', function (e) {
        e.stopPropagation(); e.preventDefault();
        if (!ctl.__vif) { alert('先点播放、播到想要的位置,再点这个标记'); return; }
        var sec = Math.max(0, Math.floor(ctl.__vcur || 0));
        if (btn.dataset.w === 'start') { _fillT('.rc-vc-sm', '.rc-vc-ss', sec); applyPatch({ start: sec }, false); }
        else { _fillT('.rc-vc-em', '.rc-vc-es', sec); applyPatch({ end: sec }, false); }
      });
    });
    box.querySelector('.rc-vc-rm').addEventListener('click', function (e) {
      e.stopPropagation(); e.preventDefault();
      // 便签只有视频(无文字、无手写)→ 移除视频 = 删整张便签;否则只变回普通便签(留文字/手写)
      var onlyVideo = !(ctl.note.text || '').trim() && !(ctl.note.strokes && ctl.note.strokes.length);
      if (onlyVideo) {
        if (!window.confirm('这张便签只有视频,移除视频将**删除整张便签**,确定?')) return;
        fetch(API + '?file=' + encodeURIComponent(O.file) + '&id=' + encodeURIComponent(ctl.note.id), { method: 'DELETE' }).catch(function () {});
        try { exitEdit(); } catch (e2) {}
        try { ctl.root.remove(); } catch (e2) {}
        delete ctls[ctl.note.id];
        for (var i = notes.length - 1; i >= 0; i--) if (notes[i].id === ctl.note.id) notes.splice(i, 1);
        toastMsg('🗑 视频便签已删除');
        return;
      }
      if (!window.confirm('移除这个视频?(便签变回普通便签,保留文字/手写)')) return;
      ctl.note.video = null; patchNote(ctl.note, { video: null }); renderNoteVideo(ctl);
    });
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
      fetch(API + '?file=' + encodeURIComponent(O.file) + '&id=' + encodeURIComponent(ctl.note.id), { method: 'DELETE' }).catch(function () {});
      exitEdit();
      try { ctl.root.remove(); } catch (e2) {}
      delete ctls[ctl.note.id];
      for (var i = notes.length - 1; i >= 0; i--) if (notes[i].id === ctl.note.id) notes.splice(i, 1);
      toastMsg('🗑 便签已删除');
    });
    // handle:长按 lpMs() 进 EDIT(与 body 长按同一入口);EDIT 模式下按下即拖(移动便签)
    ctl.handle.addEventListener('pointerdown', function (e) { onHandleDown(ctl, e); });
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
  function ensureMounted(note) {
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
    return true;
  }
  // 全量重挂/重定位:ensureMounted 幂等(容器校验/换容器重挂/位置重算全在里面)。
  // 亦是 v4 repositionAll 的实现——重排(侧栏开关/字号/栏宽/resize)后 host 重算像素位置。
  function mountAll() {
    if (!O) return;
    for (var i = 0; i < notes.length; i++) ensureMounted(notes[i]);
  }
  function removeAllEls() {
    for (var id in ctls) { try { ctls[id].root.remove(); } catch (e) {} }
    ctls = {};
  }
  function syncCtl(ctl) {
    var n = ctl.note;
    applyColor(ctl);
    applySize(ctl);
    ctl.root.classList.toggle('rc-note-collapsed', !!n.collapsed);
    renderNoteVideo(ctl);   // 视频便签:有 video 则渲染播放器+控件(签名去重,无变化不重建)
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
    var now = Date.now();
    var near = ctl._tapPt && Math.hypot(e.clientX - ctl._tapPt.x, e.clientY - ctl._tapPt.y) < 32;
    ctl._tapCount = (now - ctl._tapT < TAP_WIN && near) ? ctl._tapCount + 1 : 1;
    ctl._tapT = now; ctl._tapPt = { x: e.clientX, y: e.clientY };
    if (ctl._tapCount >= 2) {
      // 双击:撤掉第一击排队的折叠 toggle,交给 AI hook(阶段3)。
      // 注:body 上聚焦文字的 tap 照常进计数(聚焦不吃事件);橡皮双击在 onBodyDown 已 stopPropagation,到不了这里。
      ctl._tapCount = 0;
      if (ctl._toggleT) { clearTimeout(ctl._toggleT); ctl._toggleT = null; }
      try { saveText(ctl); } catch (err0) {}   // 先冲掉未失焦的文字改动(hook 拿到最新 text;服务端合成图也要最新 sidecar)
      var handled = false;
      try { handled = !!(O && O.onDoubleTap && O.onDoubleTap(ctl.note)); } catch (err) {}
      if (!handled) { try { console.log('[rc-stickynote] 双击便签(host 未处理:助手未开着或未接线)', ctl.note.id); } catch (err2) {} }
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
  }

  // ─────────────────────────── handle 手势(长按 → EDIT;EDIT 内按下即拖 = 移动便签)───────────────────────────
  function onHandleDown(ctl, e) {
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    if (_hd) cancelHandleGesture(true);
    e.preventDefault();   // handle touch-action:none;再挡 iOS 文本选择/callout
    _hd = { ctl: ctl, sx: e.clientX, sy: e.clientY, lp: null, dragging: false, moved: false, rect0: null };
    if (EDIT && EDIT.ctl === ctl) startDrag(ctl);   // 已在 EDIT → 按下即拖
    else _hd.lp = setTimeout(function () { if (_hd && _hd.ctl === ctl) { enterEdit(ctl); startDrag(ctl); } }, lpMs());
    document.addEventListener('pointermove', onHandleMove, true);
    document.addEventListener('pointerup', onHandleUp, true);
    document.addEventListener('pointercancel', onHandleCancel, true);
    try { ctl.handle.setPointerCapture(e.pointerId); } catch (err) {}
  }
  function startDrag(ctl) {
    if (!_hd) return;
    _hd.dragging = true;
    _hd.rect0 = ctl.root.getBoundingClientRect();
    ctl.root.classList.add('rc-note-lift');   // 浮起效果:只在拖拽进行时(松手/取消即撤)
  }
  function onHandleMove(e) {
    if (!_hd) return;
    var dx = e.clientX - _hd.sx, dy = e.clientY - _hd.sy;
    if (!_hd.dragging) {
      if (Math.hypot(dx, dy) > LP_TOL) { clearTimeout(_hd.lp); _hd.lp = null; }   // 动了 → 不算长按
      return;
    }
    e.preventDefault();
    _hd.moved = true;
    _hd.ctl.root.style.transform = 'translate(' + dx + 'px,' + dy + 'px) scale(1.03)';
  }
  function onHandleUp(e) {
    var g = _hd; if (!g) return;
    cleanupHandleListeners();
    clearTimeout(g.lp);
    _hd = null;
    if (g.dragging) {
      g.ctl.root.classList.remove('rc-note-lift');   // 浮起只随拖拽;EDIT 模式(🗑/色板/手柄)继续保持
      g.ctl._suppressTap = Date.now();
      if (g.moved) dropNote(g.ctl, g.rect0, e.clientX - g.sx, e.clientY - g.sy);
      else g.ctl.root.style.transform = '';   // 长按未拖:清暂态
    }
  }
  function onHandleCancel() {
    var g = _hd; if (!g) return;
    cleanupHandleListeners();
    clearTimeout(g.lp);
    _hd = null;
    if (g.dragging) { g.ctl.root.classList.remove('rc-note-lift'); g.ctl.root.style.transform = ''; g.ctl._suppressTap = Date.now(); }
  }
  function cleanupHandleListeners() {
    document.removeEventListener('pointermove', onHandleMove, true);
    document.removeEventListener('pointerup', onHandleUp, true);
    document.removeEventListener('pointercancel', onHandleCancel, true);
  }
  function cancelHandleGesture() {
    if (!_hd) return;
    cleanupHandleListeners();
    clearTimeout(_hd.lp);
    if (_hd.dragging) { _hd.ctl.root.classList.remove('rc-note-lift'); _hd.ctl.root.style.transform = ''; }
    _hd = null;
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
    // 松手:便签左上角(+4px 进容器内)→ anchorFromPoint 重解析目标容器(支持拖过页/章边界)→ PATCH
    var anchor = reanchorAt(ctl, rect0.left + dx + 4, rect0.top + dy + 4);
    ctl.root.style.transform = '';
    if (!anchor) { toastMsg('放不到这里(不在内容页上),已弹回'); return; }
    ctl.note.anchor = anchor;
    patchNote(ctl.note, { anchor: anchor });
    ensureMounted(ctl.note);   // 可能跨页/跨章 → 换容器重挂 + 新锚定位
  }
  // ─────────────────────────── EDIT 编辑模式(长按便签任意部分,handle/body 同一入口)───────────────────────────
  // 同时呈现:🗑 + 色板工具条 + 左上/右下缩放手柄 + handle 拖拽移动;移动/缩放/换色可连续操作不自动退出。
  function enterEdit(ctl) {
    if (EDIT && EDIT.ctl !== ctl) exitEdit();
    if (EDIT) return;
    if (ctl.note.collapsed) { ctl.note.collapsed = false; ctl.root.classList.remove('rc-note-collapsed'); patchNote(ctl.note, { collapsed: false }); }
    EDIT = { note: ctl.note, ctl: ctl };
    ctl.root.classList.add('rc-note-editing', 'rc-note-active');
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
    if (e.target && e.target.closest && (e.target.closest('.rc-note-tools') || e.target.closest('.rc-note-rs') || e.target.closest('.rc-note-rs-tl') || e.target.closest('.rc-note-del'))) return;
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
      lp: setTimeout(function () { var b = _bd; cancelBodyLP(); if (b) enterEdit(b.ctl); }, lpMs())
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
    ensureMounted(g.ctl.note);   // host 按新锚重算像素位置(左上缩放不跨容器,原地重挂幂等)
    g.ctl._suppressTap = Date.now();
    patchNote(g.ctl.note, { anchor: g.ctl.note.anchor, w: g.ctl.note.w, h: g.ctl.note.h });
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
  //   (PATCH 走不了 sendBeacon,用 keepalive fetch;hidden 时页面还活着,普通发送也大多来得及)
  function flushAllStrokes() {
    if (!O || !O.file) return;
    for (var id in ctls) {
      var ctl = ctls[id];
      if (!ctl || !ctl._strokeDirty) continue;
      ctl._strokeDirty = false;
      clearTimeout(ctl._strokeT);
      try {
        fetch(API, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' }, keepalive: true,
          body: JSON.stringify({ file: O.file, id: ctl.note.id, strokes: ctl.note.strokes || [] }),
        });
      } catch (e) {}
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
    var er = NINK.tool === 'eraser';
    var upd = function (c) {
      var b = c.tools.querySelector('.rc-note-tool');
      if (!b) return;
      b.classList.toggle('on', er);
      b.textContent = er ? '🧹' : '✒️';
      b.title = er ? '橡皮中(点回笔;划过笔画删除整条)' : '笔(点切橡皮;手指快速双击也可临时切换)';
    };
    if (ctl) upd(ctl);
    for (var id in ctls) if (!ctl || ctls[id] !== ctl) upd(ctls[id]);   // NINK 全局 → 所有便签按钮同步
  }

  // ─────────────────────────── 点便签外 → 退出 EDIT 编辑模式(保存待存项)───────────────────────────
  function onDocDown(e) {
    var t = e.target;
    var insideRoot = (t && t.closest) ? t.closest('.rc-note') : null;
    if (EDIT && insideRoot !== EDIT.ctl.root) exitEdit();
  }

  // ─────────────────────────── 创建 ───────────────────────────
  function createAt(anchor) {
    if (!O) return;
    if (!anchor) { toastMsg('这里放不了便签(不在内容上)'); return; }
    req('POST', { file: O.file, anchor: anchor, color: DEFAULT_COLOR, w: 260, h: 180 }, function (d) {
      if (!d || !d.ok || !d.note) { toastMsg('✗ 便签创建失败'); return; }
      notes.push(d.note);
      if (!ensureMounted(d.note)) toastMsg('便签已创建(所在页尚未渲染,渲染后出现)');
    });
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
  function createVideoAt(clientX, clientY, videoId) {
    if (!O || !O.anchorFromPoint || !videoId) return false;
    var cands = [[clientX, clientY], [clientX, clientY - 22], [clientX, clientY + 22], [clientX - 30, clientY], [clientX + 30, clientY]];
    var anchor = null;
    for (var i = 0; i < cands.length && !anchor; i++) { try { anchor = O.anchorFromPoint(cands[i][0], cands[i][1]); } catch (e) {} }
    if (!anchor) { toastMsg('这里放不了(把视频拖到正文上再松手)'); return false; }
    req('POST', { file: O.file, anchor: anchor, color: DEFAULT_COLOR, w: 300, h: 210, collapsed: false,
                  video: { id: videoId, start: 0, end: 0, rate: 1, loop: false, cc: true } }, function (d) {
      if (!d || !d.ok || !d.note) { toastMsg('✗ 便签创建失败'); return; }
      notes.push(d.note);
      if (!ensureMounted(d.note)) toastMsg('视频便签已建(所在页尚未渲染,渲染后出现)');
      else toastMsg('✅ 视频已放进书页便签');
    });
    return true;
  }

  // ─────────────────────────── 公开 API ───────────────────────────
  RC.stickynote = {
    // opts: {file, mount(anchor)->{el,w,h}|null, anchorFromPoint(x,y)->anchor|null, onDoubleTap(note)->bool, toast?}
    init: function (opts) {
      O = opts || {};
      injectCss();
      if (!_docBound) { _docBound = true; document.addEventListener('pointerdown', onDocDown, true); }
      this.loadAll();
    },
    // GET 全部便签 → 重挂(容器没就绪的留给 mountPending)
    loadAll: function () {
      if (!O || !O.file) return;
      fetch(API + '?file=' + encodeURIComponent(O.file))
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d || !d.ok) return;
          removeAllEls();
          notes = d.notes || [];
          mountAll();
        }).catch(function () {});
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
    // 阶段 B:拖放建视频便签(助手视频卡长按拖到书页 → 该屏幕点建带 video 的便签)
    createVideoAt: createVideoAt,
    // ── 笔路由接口(页面 ink 层用,跨界三段切割;见上「编程式笔路由 API」注释)──
    penRoute: penRoute,     // (x,y|event) -> noteId|null  笔尖是否在某展开便签 body 上
    penBegin: penBegin,     // (event, {eraser?}) -> bool  在命中便签开一段(坐标归一化/PATCH 内部管理)
    penMove: penMove,       // (event)                     追加点(coalesced+抽稀+rAF 重绘)
    penEnd: penEnd,         // ({boundary?})               收尾当前段(boundary=跨界切段,擦边单点丢弃)
    penActive: function () { return !!draw; },
    // 外观设置(rc-note-opacity / rc-note-autocontrast)变更后即时应用:对每个已挂载 ctl 重跑 applyColor
    // (rc-settings「便签」tab 保存时调;已有笔画是数据不改色,只影响底色/前景/新笔画)
    refreshStyle: function () { for (var id in ctls) { try { applyColor(ctls[id]); } catch (e) {} } },
    // 卸载全部便签 DOM + 清状态(teardown)
    removeAll: function () { exitEdit(); cancelHandleGesture(); cancelBodyLP(); if (_rz) onResizeUp(); if (_rzTL) onResizeTLUp(); if (draw) penEnd(); removeAllEls(); notes = []; },
    notes: function () { return notes; }
  };
})();
