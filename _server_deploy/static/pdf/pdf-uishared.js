/* pdf-uishared.js — 从 pdf_reader.html 抽出的 ui_shared 内联 JS(2026-07-06 架构优化:模板每开必下发 →
 * 静态 immutable 缓存 + 浏览器字节码缓存)。仅 ui_shared=1 时由模板引入,顺序=原 L1091-1117 + L1119-2174。
 * ⚠ 纯 JS 无 Jinja;动态值全走 window.__PDF_CFG。改这里 → 部署到 /var/www/html/static/pdf/ 生效。*/
// ⭐ 收藏当前页(不动 reader.src:当前 PDF 页 = 与视口交叠最多的 .page-wrap[data-page-num],覆盖视口中线者优先;
//   单页/连续/双页模式通吃)。收藏条目存 PDF 页号(1-based,同 /api/page-image 与 /pdf/view?page= 语义)。
//   ⭐ 按钮亮暗随当前页是否已收藏(rc-favorites.bindStar:收藏夹缓存 + 滚动节流刷新都在组件内)。
window._favCurTarget = function () {
  var page = 0, mid = window.innerHeight / 2, bestOv = -1;
  document.querySelectorAll('.page-wrap[data-page-num]').forEach(function (pw) {
    var r = pw.getBoundingClientRect(); if (!r.height) return;
    var ov = Math.min(r.bottom, window.innerHeight) - Math.max(r.top, 0);
    if (r.top <= mid && r.bottom >= mid) ov += 1e6;   // 盖住视口中线的页直接胜出
    if (ov > bestOv) { bestOv = ov; page = parseInt(pw.dataset.pageNum, 10) || 0; }
  });
  if (!page) page = (window.__PDF_CFG && __PDF_CFG.page) || 1;
  return { file: (window.__PDF_CFG && __PDF_CFG.file_rel) || '', kind: 'pdf', page: page };
};
window._favOpenPicker = function () {
  if (!(window.RC && RC.favorites)) return;
  RC.favorites.openPicker(_favCurTarget());
};
(function () {
  var b = document.querySelector('button[onclick*="_favOpenPicker"]');
  if (!b) return;
  var _f = (window.__PDF_CFG && __PDF_CFG.file_rel) || '';
  if (_f.indexOf('资源/收藏夹/') === 0) { b.style.display = 'none'; return; }   // 收藏夹物化书自身不出现 ⭐(禁自我收藏;规格 D)
  if (window.RC && RC.favorites && RC.favorites.bindStar) RC.favorites.bindStar(b, window._favCurTarget);
})();


// ➕ 插入页:**规格 v2 = PDF 真插入**(改 PDF 文件本身;设计 references/reader-userpages-favorites.md「⚠️ 规格 v2」)。
//   · 新建/✏️编辑/🗑删除 → /pdf/api/pdf-insert-page 异步 job(备份→PyMuPDF 改页→原子替换→全套锚迁移)→
//     等待浮层轮询 /pdf/api/job-status(照 prewarm/建目录 job 先例)→ done 后整页 reload(按新文件重初始化,
//     mtime 变化让页图/字符层缓存自然重建)。
//   · 真实页记录(sidecar 带 page 字段)在对应 .page-wrap 渲「📝 我的页」角标(pw 内 absolute,照便签挂载先例),
//     点角标弹 ✏️/🗑 菜单。插入的页是真文本(insert_htmlbox)→ 字符层/选词/多选/高亮/AI 全套功能天然可用。
//   · **旧版虚拟页(v1 after 语义存量)保留并存**:RC.userpages 经 O.filter 只 mount 无 page 字段的记录,
//     其 ✏️/🗑 走旧 /api/userpages(不改 PDF);after 边界由服务端迁移器随真插入/删除同步移位。新建一律真插入。
//   · 以下 _upPlace/_upPatchRemode/_upWrapNoteHost 是 v1 虚拟页的渲染机制,原样保留(锚定铁律注释见 git 历史)。
(function () {
  if (!(window.RC && RC.userpages)) return;
  var UP_FILE = (window.__PDF_CFG && __PDF_CFG.file_rel) || '';
  var UP_API = '/pdf/api/pdf-insert-page';       // 新建/删除/baked编辑 = 真插页 job
  var UP_TEXT_API = '/pdf/api/userpages';        // v4 overlay 文字即时存边车(不 job、不 reload)
  // 本机导入书(localbook:,字节只在设备、Pi 无此书):服务端任务链对它必然失败,
  // 造纸走下方 _lp* 本地分支(2026-08-16 用户架构拍板:纸=类型化数据,接收端全权处理)。
  var UP_LOCAL = String(UP_FILE).indexOf('localbook:') === 0;
  var _upCss = document.createElement('style');
  _upCss.textContent =
    '.up2-content-hd .up2-lb{opacity:.72;font-variant-numeric:tabular-nums;font-size:.92em}' +
    '.up2-badge{position:absolute;left:6px;top:6px;z-index:40;background:rgba(26,37,64,.88);color:#9fcbff;border:1px solid rgba(91,118,184,.6);border-radius:999px;padding:2px 10px;font-size:12px;line-height:1.5;cursor:pointer;-webkit-user-select:none;user-select:none;box-shadow:0 1px 5px rgba(0,0,0,.35)}' +
    /* 就地编辑覆盖层:absolute 盖满目标 .page-wrap / 临时白纸页;磨砂深色底 + 内嵌编辑卡。
       对其下页面手势 stopPropagation(选词/ink/双击缩放点不到那一页)= 编辑期禁用阅读器功能 */
    '.up2-inline{position:absolute;inset:0;z-index:60;display:flex;flex-direction:column;padding:14px;box-sizing:border-box;' +
    'background:rgba(9,14,26,.86);-webkit-backdrop-filter:blur(3px);backdrop-filter:blur(3px);border-radius:6px;-webkit-user-select:text;user-select:text}' +
    '.up2-inline .up2-ihd{flex:0 0 auto;font-size:12.5px;color:#9fb4dc;margin-bottom:8px;display:flex;align-items:center;gap:8px}' +
    '.up2-inline .up2-ihd .tag{background:rgba(59,109,181,.25);border:1px solid rgba(91,118,184,.55);border-radius:999px;padding:1px 9px;color:#bcd6ff}' +
    /* 输入框自给全套样式(编辑现有页时面板在 .page-wrap 内、无 .rc-upage 祖先 → 不能靠 rc-userpages 的祖先选择器;
       iOS 文字防隐形三件套 -webkit-text-fill-color/user-select:text/appearance:none 必须自带,同便签教训)*/
    '.up2-inline .rc-up-ti{width:100%;box-sizing:border-box;padding:9px 11px;border-radius:8px;border:1px solid rgba(110,135,195,.5);font-size:14px;font-weight:600;outline:none;background:rgba(255,255,255,.94);color:#16233c;-webkit-text-fill-color:#16233c;caret-color:#16233c;-webkit-user-select:text;user-select:text;-webkit-appearance:none;appearance:none}' +
    '.up2-inline .rc-up-ta{width:100%;box-sizing:border-box;flex:1 1 auto;min-height:120px;resize:none;padding:11px;border-radius:8px;border:1px solid rgba(110,135,195,.5);font:14px/1.6 ui-monospace,Menlo,Consolas,monospace;outline:none;background:rgba(255,255,255,.94);color:#16233c;-webkit-text-fill-color:#16233c;caret-color:#16233c;-webkit-user-select:text;user-select:text;-webkit-appearance:none;appearance:none}' +
    '.up2-inline .up2-ibar{flex:0 0 auto;display:flex;gap:10px;justify-content:flex-end;margin-top:10px}' +
    '.up2-inline .up2-ibar button{background:#1a2540;border:1px solid #3b6db5;color:#9fcbff;border-radius:8px;padding:8px 16px;font-size:13px;cursor:pointer;touch-action:manipulation}' +
    '.up2-inline .up2-ibar .up2-cancel{background:transparent;border-color:rgba(110,135,195,.4);color:#aab6cf;margin-right:auto}' +
    '.up2-inline .up2-ibar .up2-del{background:transparent;border-color:rgba(200,90,90,.5);color:#ff9d9d}' +
    /* 新建时的临时白纸页(占位,一屏高,视觉=书里多出的一页;job 完成刷新后被真 PDF 页替代)*/
    '.pdf-upage.up2-new{min-height:80vh;position:relative;display:block}' +
    '.up2-newph{padding:24px;color:#7a8bb0;font-size:14px;text-align:left;line-height:1.7}' +
    /* 顶部非阻塞横幅:细条,不遮阅读区,可继续读 */
    '#up2-banner,#up2-del-ind{position:fixed;left:50%;transform:translateX(-50%);top:calc(env(safe-area-inset-top,0px) + 8px);z-index:2500;' +
    'max-width:92vw;display:flex;align-items:center;gap:10px;background:#12203c;border:1px solid #33507f;border-radius:999px;' +
    'padding:8px 16px;font-size:13px;color:#dbe7ff;box-shadow:0 6px 22px rgba(0,0,0,.45);cursor:default}' +
    '#up2-banner.ok{background:#123324;border-color:#2f6d45;color:#c7f0d6;cursor:pointer}' +
    '#up2-banner.err{background:#33161a;border-color:#7a2f36;color:#ffc7cd}' +
    '#up2-banner .up2-spin,#up2-del-ind .up2-spin{width:15px;height:15px;flex:0 0 auto;border:2px solid rgba(110,160,255,.3);border-top-color:#7fb0ff;border-radius:50%;animation:up2spin .9s linear infinite}' +
    '#up2-banner .up2-x{flex:0 0 auto;margin-left:4px;color:inherit;opacity:.7;cursor:pointer;font-size:15px;line-height:1}' +
    '@keyframes up2spin{to{transform:rotate(360deg)}}' +
    /* 编辑期兜底:藏掉可能已弹出的选中工具栏/词典弹窗(覆盖层拦截是主防线,这是二道保险)*/
    'body.up-editing #sel-toolbar,body.up-editing #word-pop,body.up-editing #phrase-pop{display:none !important}' +
    /* 删除撤销条(Gmail 式:先删 + 撤销小条 + 延后提交;撤销窗口内不碰 PDF/页码)*/
    '#up2-undo-stack{position:fixed;left:50%;transform:translateX(-50%);bottom:calc(env(safe-area-inset-bottom,0px) + 18px);z-index:2600;display:flex;flex-direction:column-reverse;gap:8px}' +
    '.up2-undo{position:relative;overflow:hidden;display:flex;align-items:center;gap:14px;background:#1c2740;border:1px solid #3a4d78;border-radius:12px;padding:11px 16px;color:#dfe8fa;box-shadow:0 8px 28px rgba(0,0,0,.5);font-size:13.5px;white-space:nowrap}' +
    '.up2-undo .up2-undo-btn{background:transparent;border:none;color:#7fd3ff;font-weight:600;font-size:13.5px;cursor:pointer;padding:2px 4px;touch-action:manipulation}' +
    '.up2-undo .up2-undo-bar{position:absolute;left:0;bottom:0;height:3px;background:#7fd3ff;border-radius:0 0 12px 12px;width:100%;animation:up2undobar linear forwards}' +
    '@keyframes up2undobar{from{width:100%}to{width:0}}' +
    /* 删除待撤销:那一页蒙层 */
    '.up2-delveil{position:absolute;inset:0;z-index:55;display:flex;align-items:center;justify-content:center;background:rgba(12,16,26,.72);color:#c7d4ee;font-size:14px;border-radius:6px;-webkit-backdrop-filter:blur(2px);backdrop-filter:blur(2px);-webkit-user-select:none;user-select:none}' +
    /* 乐观内容层:保存后立即把用户内容渲染在页上(替掉转圈),盖住旧页图直到 reload 拿到真页 */
    '.up2-optim{position:absolute;inset:0;z-index:50;overflow:auto;-webkit-overflow-scrolling:touch;background:#fff;color:#1b1b1b;border-radius:6px}' +
    '.up2-optim .up2-optim-hd{padding:8px 16px;font-size:12.5px;color:#5b76b8;border-bottom:1px dashed rgba(110,135,195,.4)}' +
    '.up2-optim .up2-optim-body{padding:16px 20px;font-size:15px;line-height:1.65}' +
    '.up2-optim .up2-optim-body p{margin:0 0 .8em}' +
    '.up2-optim .up2-optim-body h1,.up2-optim .up2-optim-body h2,.up2-optim .up2-optim-body h3{line-height:1.35;margin:.9em 0 .45em}' +
    '.up2-optim .up2-optim-body ul,.up2-optim .up2-optim-body ol{margin:0 0 .8em;padding-left:1.6em}' +
    /* v4 overlay 常驻可编辑覆盖层:显示态渲 RC.md;点击进 textarea 即时编辑(自动存边车,无保存按钮) */
    /* 显示态:z=6(选词层 char z4 之上 → 文字可选中/接阅读器功能;手写 ink z7 仍在其上 → 笔画可见)。
       pen 走 page-wrap 捕获阶段归手写,手指 tap/拖选落到覆盖层文字归选词,两者不打架;左对齐 */
    '.up2-content{position:absolute;inset:0;z-index:6;overflow:auto;-webkit-overflow-scrolling:touch;background:#fff;color:#1b1b1b;border-radius:6px;text-align:left;-webkit-user-select:text;user-select:text}' +
    /* 左上角编辑按钮:Apple 简约(毛玻璃+SF),图标 "Aa" 区分手写 ✏️;唯一进编辑入口 */
    '.up2-edit-btn{position:absolute;left:8px;top:8px;z-index:44;height:29px;padding:0 12px;display:inline-flex;align-items:center;justify-content:center;background:rgba(255,255,255,.72);-webkit-backdrop-filter:saturate(180%) blur(20px);backdrop-filter:saturate(180%) blur(20px);border:.5px solid rgba(0,0,0,.13);border-radius:9px;font:600 14px/1 -apple-system,BlinkMacSystemFont,"SF Pro Text",system-ui,sans-serif;color:#1d1d1f;letter-spacing:.02em;cursor:pointer;pointer-events:auto;box-shadow:0 1px 3px rgba(0,0,0,.1);-webkit-user-select:none;user-select:none;transition:transform .1s,background .12s}' +
    '.up2-edit-btn:active{background:rgba(255,255,255,.95);transform:scale(.96)}' +
    /* 右上角 ⭐:收藏这一页(自己创建的插入页)到收藏夹。显示态覆盖层穿透;编辑态被 .up2-content.editing(z52)盖住自然隐藏 */
    '.up2-fav-btn{position:absolute;right:8px;top:8px;z-index:44;height:29px;padding:0 10px;display:inline-flex;align-items:center;justify-content:center;background:rgba(255,255,255,.72);-webkit-backdrop-filter:saturate(180%) blur(20px);backdrop-filter:saturate(180%) blur(20px);border:.5px solid rgba(0,0,0,.13);border-radius:9px;font-size:15px;line-height:1;cursor:pointer;pointer-events:auto;box-shadow:0 1px 3px rgba(0,0,0,.1);-webkit-user-select:none;user-select:none;transition:transform .1s,background .12s}' +
    '.up2-fav-btn:active{background:rgba(255,255,255,.95);transform:scale(.96)}' +
    '.up2-content .up2-content-hd{padding:8px 16px;font-size:12.5px;color:#5b76b8;border-bottom:1px dashed rgba(110,135,195,.4);position:sticky;top:0;background:#fff}' +
    '.up2-content .up2-content-body{padding:16px 20px;font-size:15px;line-height:1.65;min-height:60px}' +
    '.up2-content .up2-content-body.empty{color:#9aa5bd;font-style:italic}' +
    '.up2-content .up2-content-body p{margin:0 0 .8em}' +
    // 任务运行时:页面块(references/adr-task-runtime.md)。纸是白底(PDF 真页) → 用深色文字。
    // ★ 格子布局(paper.py):块用**服务端算好的归一化 rect** 绝对定位 —— 前端不再自己量。
    //   容器必须 position:relative(绝对定位的基准);绝不能用自由流式 CSS(会跟服务端算的 bbox 对不上)。
    '.up2-blocks{position:absolute;inset:0}' +
    '.up2-b{position:absolute;box-sizing:border-box;display:flex;align-items:center}' +
    // ⚠ 这里**不能**写 font-size:!important —— 字号由 _upFitText 按格子算(h1 的 1.25 倍已经
    //   算在 __fr 里)。曾经这条 !important 把算好的值整个盖掉,标题既被重复放大又脱离格子体系。
    '.up2-b.up2-h1{font-weight:700;color:#1e2a44;align-items:flex-end;padding-bottom:4px}' +
    '.up2-b-blank{align-items:flex-end;gap:6px;flex-wrap:wrap}' +   // 长标签(整道题在 label 里)换行,作答线落到末行
    '.up2-b-lab{flex:none;color:#5b76b8;opacity:.75;white-space:normal;max-width:100%}' +
    // 手写就写在这条线上。线只是**视觉参考**;bbox 是算出来的,手写层(ink canvas)在更上层,天然共存。
    '.up2-b-box{flex:1 1 8em;align-self:stretch;min-height:1.1em;border-bottom:1.5px solid #c3cee6;margin-bottom:3px}' +
    /* 选择题(choice):题干 / 选项 / 作答线 三段纵排;选项 flex wrap 自动装行 —— 治"题干+ABCD 塞一行被截断" */
    '.up2-b-choice{flex-direction:column;align-items:stretch;justify-content:space-between;row-gap:2px}' +
    '.up2-c-q{color:#1e2a44}' +
    '.up2-c-opts{display:flex;flex-wrap:wrap;column-gap:1.2em;row-gap:2px;color:#33436a}' +
    '.up2-c-opts span{cursor:pointer;padding:0 .3em;border-radius:4px;transition:background .1s}' +
    '.up2-c-opts span.up2-c-sel{background:#2563eb;color:#fff}' +
    '.up2-c-ans{display:flex;align-items:flex-end;gap:6px}' +
    '.up2-b-ck{flex:none;width:1em;height:1em;border:1.5px solid #8fa2c8;border-radius:3px;margin-right:6px}' +
    '.up2-b-lab2{color:#33436a}' +
    // 纸张底纹:横线(听写/笔记)/ 方格(数学演草)—— 由 paper 预设的 rule 决定
    '.up2-rule-line{background-image:repeating-linear-gradient(to bottom,transparent 0,transparent calc(var(--lh) - 1px),rgba(120,150,200,.16) calc(var(--lh) - 1px),rgba(120,150,200,.16) var(--lh))}' +
    '.up2-rule-grid{background-image:repeating-linear-gradient(to bottom,transparent 0,transparent calc(var(--lh) - 1px),rgba(120,150,200,.13) calc(var(--lh) - 1px),rgba(120,150,200,.13) var(--lh)),repeating-linear-gradient(to right,transparent 0,transparent calc(var(--cw) - 1px),rgba(120,150,200,.13) calc(var(--cw) - 1px),rgba(120,150,200,.13) var(--cw))}' +
    // <span role=button>(不是 <button>):memory ios-button-white-block —— Safari 会用原生外观盖掉一切
    // 字号继承块(= 一格宽),内边距用 em —— 写死 px 的话按钮不跟着页面缩放,
    // 在大页面上缩成一粒、小页面上撑出格子。后端给按钮留的宽是"文字 + 3 格",
    // 这里 1.1em×2 的横向内边距正好落在那 3 格里。
    '.up2-b-btn{display:inline-flex;align-items:center;justify-content:center;padding:.4em 1.1em;' +
      'border-radius:.6em;background:#3b6fd4;color:#fff;font-weight:600;cursor:pointer;line-height:1.2;' +
      '-webkit-appearance:none;appearance:none;user-select:none;margin-right:.5em}' +
    '.up2-b-btn:active{transform:scale(.96)}' +
    '.up2-b-btn.up2-b-off{background:#9aa7c4;opacity:.55;cursor:not-allowed}' +   // #36 禁用态
    '.up2-b-btn.up2-b-off:active{transform:none}' +
    '.up2-b-card{background:rgba(123,108,255,.06);border:1px solid rgba(123,108,255,.28);border-radius:10px;' +   // #50 贴上来的卡
      'padding:8px 10px;overflow:auto;font-size:13px;line-height:1.5;color:#2a2f3a}' +
    '.up2-b-card img{max-width:100%}' +
    // 收起成球：占同一个格子的左上角，其余留白让出书页内容。
    '.up2-b-card.up2-b-card-dot{width:40px!important;height:40px!important;min-height:0;padding:0;overflow:hidden;border-radius:13px;background:rgba(123,108,255,.16);border-color:rgba(123,108,255,.5);cursor:pointer;display:flex;align-items:center;justify-content:center;transition:width .34s cubic-bezier(.32,.72,.36,1),height .34s cubic-bezier(.32,.72,.36,1)}' +
    '.up2-b-card.up2-b-card-dot > *{display:none}' +
    '.up2-b-card.up2-b-card-dot::after{content:"\1F3B4";font-size:17px;line-height:1}' +
    '.up2-run-hint{padding:6px 22px 14px;font-size:12.5px;color:#5b76b8;min-height:14px}' +
    '.up2-run-result{position:absolute;left:0;right:0;bottom:0;max-height:45%;display:flex;flex-direction:column;background:rgba(255,255,255,.97);border-top:1px solid rgba(91,118,184,.25);font-size:13.5px;line-height:1.6;color:#1b2740;box-shadow:0 -4px 16px rgba(0,0,0,.08)}' +
    '.up2-rr-hd{display:flex;align-items:center;gap:8px;padding:7px 16px;border-bottom:1px solid rgba(91,118,184,.16);flex:0 0 auto;background:rgba(245,248,253,.98)}' +   // 收起头(不挡内容:收起后只剩这一条)
    '.up2-rr-tt{font-weight:600;font-size:13px}.up2-rr-sp{flex:1}' +
    '.up2-rr-ask,.up2-rr-tog{-webkit-appearance:none;appearance:none;border:1px solid rgba(91,118,184,.35);background:#fff;color:#31518f;font-size:12px;padding:3px 10px;border-radius:8px;cursor:pointer;white-space:nowrap}' +
    '.up2-rr-ask{background:#3b6fd4;color:#fff;border-color:#3b6fd4}' +
    '.up2-rr-bd{padding:10px 20px 14px;overflow:auto;-webkit-overflow-scrolling:touch}' +
    '.up2-run-result.collapsed{max-height:none}.up2-run-result.collapsed .up2-rr-bd{display:none}' +   // 收起=藏正文,只留头条,不挡纸
    '.up2-rr-bd h3{font-size:15px;margin:0 0 6px}.up2-rr-bd p{margin:0 0 .4em}' +
    '.up2-rr-bd img{max-width:100%;max-height:220px;border-radius:6px;cursor:zoom-in;margin:4px 6px 0 0;vertical-align:top}' +   // #1 判分依据图(点击放大)
    '.up2-rr-bd hr{border:none;border-top:1px solid rgba(91,118,184,.25);margin:8px 0}' +
    '.up2-content .up2-content-body h1,.up2-content .up2-content-body h2,.up2-content .up2-content-body h3{line-height:1.35;margin:.9em 0 .45em}' +
    '.up2-content .up2-content-body ul,.up2-content .up2-content-body ol{margin:0 0 .8em;padding-left:1.6em}' +
    '.up2-content.editing{z-index:52;pointer-events:auto;cursor:default;display:flex;flex-direction:column}' +   /* 编辑态才抬到最上+全拦(禁手写/选词) */
    '.up2-content .up2-content-ebar{flex:0 0 auto;display:flex;gap:8px;align-items:center;padding:8px 12px;border-bottom:1px dashed rgba(110,135,195,.4);position:sticky;top:0;background:#fff;z-index:1}' +
    '.up2-content .up2-title{flex:1;min-width:0;box-sizing:border-box;padding:6px 9px;border:1px solid rgba(110,135,195,.4);border-radius:7px;font-size:14px;font-weight:600;outline:none;background:#fff;color:#16233c;-webkit-text-fill-color:#16233c;caret-color:#16233c;-webkit-user-select:text;user-select:text;-webkit-appearance:none;appearance:none}' +
    '.up2-content .up2-del2{flex:0 0 auto;background:transparent;border:none;color:#ff3b30;border-radius:8px;padding:6px 10px;font:500 13px -apple-system,BlinkMacSystemFont,"SF Pro Text",system-ui,sans-serif;cursor:pointer;touch-action:manipulation}' +
    '.up2-content .up2-done{flex:0 0 auto;background:#007aff;border:none;color:#fff;border-radius:8px;padding:7px 16px;font:600 13px -apple-system,BlinkMacSystemFont,"SF Pro Text",system-ui,sans-serif;cursor:pointer;touch-action:manipulation;transition:background .12s}' +
    '.up2-content .up2-done:active{background:#0062cc}' +
    '.up2-content .up2-ta{flex:1 1 auto;width:100%;box-sizing:border-box;min-height:140px;border:none;outline:none;resize:none;padding:14px 18px;font:15px/1.65 ui-monospace,Menlo,Consolas,monospace;background:#fff;color:#1b1b1b;-webkit-text-fill-color:#1b1b1b;caret-color:#1b1b1b;-webkit-user-select:text;user-select:text;-webkit-appearance:none;appearance:none;transform:translateZ(0)}' +
    '.up2-content .up2-savehint{flex:0 0 auto;font-size:11px;color:#8fa4cc;padding:4px 14px 8px;text-align:right}' +
    /* 后台同步小胶囊(不阻塞、右下角、自动消失):完成编辑 → 把这页写回 PDF 的静默进度提示 */
    '.up2-mini{position:fixed;right:calc(env(safe-area-inset-right,0px) + 12px);bottom:calc(env(safe-area-inset-bottom,0px) + 14px);z-index:2400;display:flex;align-items:center;gap:8px;background:#12203c;border:1px solid #33507f;border-radius:999px;padding:6px 13px;font-size:12px;color:#cfe0ff;box-shadow:0 4px 16px rgba(0,0,0,.4);opacity:.96}' +
    '.up2-mini.ok{background:#123324;border-color:#2f6d45;color:#c7f0d6}' +
    '.up2-mini.err{background:#33161a;border-color:#7a2f36;color:#ffc7cd}' +
    '.up2-mini .up2-spin{width:12px;height:12px;flex:0 0 auto;border:2px solid rgba(110,160,255,.3);border-top-color:#7fb0ff;border-radius:50%;animation:up2spin .9s linear infinite}';
  document.head.appendChild(_upCss);

  // ── 就地编辑面板(覆盖在目标页/临时白纸页上,替代屏幕中央弹窗)──
  //   anchorEl=承载页(编辑现有真实页=那一页 .page-wrap;新建=插入点后的临时 .up2-new 白纸页)。
  //   面板对 pointerdown/dblclick/click stopPropagation → 其下页面手势(选词/ink/双击缩放)全部够不着
  //   = 用户要的「编辑模式禁用所有阅读器功能」;body.up-editing 再兜底藏选中工具栏/词典弹窗。
  //   iOS 输入链 user-select:text(rc-upage/up2-inline 已在 CSS 钉死),复用 rc-up-ti/ta 类。
  // 滚动时间戳(_upBanner 空闲自动刷新判据)。**删除撤销已去掉**(用户要求)—— 删除立即执行(见 _upDelReal)。
  var _upScrolledAt = 0;
  try { window.addEventListener('scroll', function () { _upScrolledAt = Date.now(); }, { capture: true, passive: true }); } catch (_) {}
  var _upDeleting = {};   // id -> 1(删除进行中,防重复触发)

  var _upEditing = null;   // 当前就地编辑面板 {el, anchorEl, onClose}
  function _upCloseInline() {
    if (!_upEditing) return;
    try { _upEditing.el.remove(); } catch (_) {}
    document.body.classList.remove('up-editing');
    var oc = _upEditing.onClose; _upEditing = null;
    if (oc) try { oc(); } catch (_) {}
  }
  function _upInlineEdit(anchorEl, opts, onCommit) {
    if (_upEditing) _upCloseInline();
    var el = document.createElement('div'); el.className = 'up2-inline';
    el.innerHTML =
      '<div class="up2-ihd"><span class="tag">📝 我的页</span><span class="ttl"></span></div>' +
      '<input class="rc-up-ti" type="text" maxlength="120" placeholder="标题(可空)">' +
      '<div style="height:8px"></div>' +
      '<textarea class="rc-up-ta" placeholder="正文…(markdown:# 标题 / 列表 / **粗体** / $..$ 公式)"></textarea>' +
      '<div class="up2-ibar">' +
        '<button class="up2-cancel">✕ 取消</button>' +
        (opts.canDelete ? '<button class="up2-del">🗑 删除</button>' : '') +
        '<button class="up2-save">✔ 保存</button>' +
      '</div>';
    el.querySelector('.up2-ihd .ttl').textContent = opts.heading || '';
    var ti = el.querySelector('.rc-up-ti'), ta = el.querySelector('.rc-up-ta');
    ti.value = opts.title || ''; ta.value = opts.md || '';
    // 拦其下页面手势:**冒泡阶段** stopPropagation。按钮/输入框自身事件已先在 target 触发(不受影响),
    // 只阻止继续冒泡到页面的选词/工具栏监听。⚠ 绝不能用捕获阶段(true)——那会在事件到达内部按钮前就
    // 拦停,导致保存/取消/删除点了没反应。字符层被本覆盖层遮住(target 不在其内)→ 选词天然不触发;
    // .page-wrap 捕获阶段的手写 ink 另在 _inkPointerDown 里按 body.up-editing gate。
    ['pointerdown', 'mousedown', 'touchstart', 'click', 'dblclick'].forEach(function (ev) {
      el.addEventListener(ev, function (e) { e.stopPropagation(); });
    });
    el.querySelector('.up2-cancel').addEventListener('click', function () {
      var oc = opts.onCancel; _upCloseInline(); if (oc) try { oc(); } catch (_) {}
    });
    if (opts.canDelete) el.querySelector('.up2-del').addEventListener('click', function () {
      if (opts.onDelete) opts.onDelete();
    });
    el.querySelector('.up2-save').addEventListener('click', function () {
      var b = this; b.disabled = true; b.textContent = '保存中…';
      onCommit(ti.value.trim(), ta.value, function fail(msg) {
        b.disabled = false; b.textContent = '✔ 保存'; alert(msg);
      });
    });
    anchorEl.style.position = anchorEl.style.position || 'relative';
    anchorEl.appendChild(el);
    _upEditing = { el: el, anchorEl: anchorEl, onClose: null };
    document.body.classList.add('up-editing');
    setTimeout(function () { try { (opts.title ? ta : ti).focus(); } catch (_) {} }, 80);
    return el;
  }

  // ── 乐观非阻塞横幅:保存后立即出现(不遮阅读区,可继续读),后台轮询 job-status。
  //   done → 变绿可点「轻点查看新页」(用户掌控刷新时机,reload 靠阅读位置服务端化回位,绝不打断);
  //   error → 变红提示(原书有备份)。job 期间不干预任何其它页(内容不变,可正常阅读/触控/选词)。
  function _upBanner(jobId, opts) {
    opts = opts || {};
    var old = document.getElementById('up2-banner'); if (old) old.remove();
    var bn = document.createElement('div'); bn.id = 'up2-banner';
    bn.innerHTML = '<span class="up2-spin"></span><span class="up2-msg">正在写入 PDF…(可继续阅读)</span>';
    document.body.appendChild(bn);
    var msg = bn.querySelector('.up2-msg');
    // 不动其它页:job 期间已渲染页内容不变(仍是各自本来的内容),继续正常阅读/触控/选词;
    // 页图缓存按 mtime 键,前端此刻用旧 mtime 请求会命中旧缓存(正确内容),错位窗口几乎不存在 →
    // 真状态靠 done 后用户可控的一次 reload 拿到。(曾给后页加 opacity+pointer-events:none 变灰锁死,与"可继续阅读"矛盾,已撤。)
    function addX(handler) {
      var x = document.createElement('span'); x.className = 'up2-x'; x.textContent = '✕';
      x.addEventListener('click', function (e) { e.stopPropagation(); handler(); });
      bn.appendChild(x);
    }
    var t = setInterval(function () {
      fetch('/pdf/api/job-status?id=' + encodeURIComponent(jobId), { cache: 'no-store' })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          if (j.status === 'done') {
            clearInterval(t);
            var w = (j.result && j.result.warnings) || [];
            var sp = bn.querySelector('.up2-spin'); if (sp) sp.remove();
            bn.className = 'ok';
            msg.textContent = '✔ ' + (opts.doneMsg || '已写入新页') + (w.length ? '(' + w.join(';') + ')' : '') + ' · 轻点查看';
            bn.addEventListener('click', function () { location.reload(); });
            addX(function () { bn.remove(); });   // 不想现在看:关掉,下次开书自然是新状态
            // P1-b:空闲(无选中/无编辑/近 3s 没滚动/无待撤销)且无警告 → 延迟 1.2s 二次确认仍空闲后静默 reload
            //   (阅读位置服务端化回位,用户几乎无感);活跃时保留可点横幅当兜底,不打断。
            function _idle() {
              return (!window.getSelection || !String(window.getSelection())) &&
                     !document.body.classList.contains('up-editing') &&
                     (Date.now() - _upScrolledAt > 3000) &&
                     !document.getElementById('up2-undo-stack');
            }
            if (!w.length && !opts.manualOnly && _idle()) setTimeout(function () { if (_idle()) location.reload(); }, 1200);
          } else if (j.status === 'error' || j.status === 'unknown') {
            clearInterval(t);
            var sp2 = bn.querySelector('.up2-spin'); if (sp2) sp2.remove();
            bn.className = 'err';
            msg.textContent = '✕ 写入失败:' + (j.error || '任务状态丢失(服务可能重启);原书有备份,刷新后重试');
            addX(function () { bn.remove(); });
          } else if (j.step) { msg.textContent = j.step + '…(可继续阅读)'; }
        }).catch(function () {});   // 网络抖动:下一轮再试
    }, 1000);
  }

  // 自建页的位置标签(46-a)。用户 2026-08-23:「给他个前一页加上字母的虚拟页码」。
  // ⚠ 只用于**显示**。绝不进 bind.page / ?page= —— parseInt('46-a')===46
  //   会把它静默当成真实的第 46 页。
  function _upLabel(rec) {
    try {
      var l = RC.userpages && RC.userpages.label && RC.userpages.label(rec);
      return l || '';
    } catch (_) { return ''; }
  }

  // ── 真实页角标(📝 我的页)+ ✏️/🗑 菜单 ──
  function _upRealPages() {
    var out = [];
    try {
      (RC.userpages.pages() || []).forEach(function (p) { if (typeof p.page === 'number') out.push(p); });
    } catch (_) {}
    return out;
  }
  // 渲染层（04-render 去边豁免）用：该页号是否用户插入的真实页。
  window._upIsRealPage = function (pageNum) {
    var recs = _upRealPages();
    for (var i = 0; i < recs.length; i++) if (recs[i].page === pageNum) return true;
    return false;
  };
  // 编辑现有真实页 = 就地在那一页覆盖编辑面板(删除按钮内嵌);保存/删除都走乐观横幅(不阻塞、不整页硬刷新)
  function _upEditReal(rec) {
    var pw = document.querySelector('.page-wrap[data-page-num="' + rec.page + '"]');
    if (!pw) { alert('这一页还没渲染出来,翻到第 ' + rec.page + ' 页再编辑'); return; }
    if (rec.mode === 'overlay') { _upOverlayEnterEdit(rec, pw); return; }   // v4 即时编辑:存边车,不 job、不 reload
    // baked 老页(内容已烧进 PDF):保持 v2 就地编辑 + 重排 job
    _upInlineEdit(pw, {
      heading: '第 ' + rec.page + ' 页 · 保存后重排进 PDF', title: rec.title, md: rec.md, canDelete: true,
      onDelete: function () { _upCloseInline(); _upDelReal(rec); }
    }, function (title, md, fail) {
      RC.reqJson('PATCH', UP_API, { file: UP_FILE, id: rec.id, title: title, md: md }).then(function (d) {
        if (!(d && d.ok && d.job_id)) { fail('保存失败:' + ((d && d.error) || '?')); return; }
        _upCloseInline();
        _upOptimShow(rec.page, title, md);   // P0:立即把新内容显示在那一页(替掉旧页图),不等 reload
        _upBanner(d.job_id, { afterPage: rec.page });
      }).catch(function () { fail('网络错误,没保存上'); });
    });
  }
  // P0:在某页覆盖乐观内容层(渲染 md,盖住旧页图/占位),job done reload 后被真页替代
  function _upOptimShow(page, title, md) {
    var pw = document.querySelector('.page-wrap[data-page-num="' + page + '"]');
    if (!pw) return;
    var ov = pw.querySelector('.up2-optim'); if (ov) ov.remove();
    ov = document.createElement('div'); ov.className = 'up2-optim';
    ov.innerHTML = '<div class="up2-optim-hd">📝 ' + (title ? RC.esc(title) : '我的页') + ' · 写入中…</div><div class="up2-optim-body"></div>';
    var body = ov.querySelector('.up2-optim-body');
    if (body) { body.innerHTML = (window.RC && RC.md) ? RC.md(md || '') : RC.esc(md || ''); try { if (RC.typeset) RC.typeset(body); } catch (_) {} }
    pw.style.position = pw.style.position || 'relative';
    pw.appendChild(ov);
  }

  // ── v4 overlay 即时文字编辑:存边车(/api/userpages PATCH),不碰 PDF、不 job、无保存按钮 ──
  var _upTextTimers = {}, _upTextDirty = {}, _upTextSnap = {};
  // 本会话「编辑过」的 overlay 页 id 集合:关书 keepalive 同步(_upSyncFlushKeepalive)据此挑要写回 PDF 的页。
  //   (方案调整 2026-07-04:不再用于渲染分流 —— overlay 页始终挂覆盖层,见 _upMountBadges。)
  var _upEditedIds = {};
  // ── 乐观新建:临时页 client 态(job 未完成前用临时 id 缓冲击键,done 后绑真 id 一次性落库,评审 #8)──
  var _upTempEls = {};        // tempId -> 临时虚拟元素(.pdf-upage);绑真 id 后改 dataset.uid
  var _upTempCancelled = {};  // tempId -> true:job 完成前用户已取消(删)→ 绑定时清理那张空白真页
  function _upIsTempId(id) { return /^tmp_/.test(String(id || '')); }
  function _upTextSchedule(id, title, md) {
    _upTextDirty[id] = true; _upTextSnap[id] = { title: title, md: md };   // 快照防延迟读(墨迹 FILE_REL/快照教训)
    _upEditedIds[id] = true;
    clearTimeout(_upTextTimers[id]);
    if (_upIsTempId(id)) return;   // 乐观新建临时页:尚无真 id,击键先本地缓冲(_upTextSnap),job done 绑真 id 后一次性落库
    _upTextTimers[id] = setTimeout(function () { _upTextSave(id); }, 600);
  }
  function _upTextSave(id) {
    var snap = _upTextSnap[id]; if (!snap || !UP_FILE) return Promise.resolve();
    if (_upIsTempId(id)) return Promise.resolve();   // 临时页只缓冲不落库(绑真 id 后 _upBindTempToReal 再存)
    delete _upTextDirty[id];
    return RC.reqJson('PATCH', UP_TEXT_API, { file: UP_FILE, id: id, title: snap.title, md: snap.md })
      .then(function (d) { if (!(d && d.ok)) _upTextDirty[id] = true; return d; })
      .catch(function () { _upTextDirty[id] = true; });
  }
  function _upTextFlush(id) { clearTimeout(_upTextTimers[id]); return _upTextSave(id); }

  // ── v4 批次2:后台同步(把 sidecar md 写回 PDF)= 完成编辑 / 关书 时触发,静默、不 reload、串行队列 ──
  //   频率只压在**完成编辑**(用户主动)+ pagehide(关书),绝不每次击键(每次同步=整本 doc.save+刷 mtime
  //   → 全书页图/字符层缓存作废,成本高;见 references 风险登记)。同步是「让 PDF 最终可移植 + 字符层可用」,
  //   覆盖层已显示正确 md → 用户无需等待、不 reload。
  var _upSyncQ = [], _upSyncBusy = false;
  function _upEnqueueSync(id) {
    if (!id || !UP_FILE || _upIsTempId(id)) return;   // 临时页无真 id,不同步(绑真 id 后按需再入队)
    if (_upSyncQ.indexOf(id) >= 0) return;   // 按 id 去重
    _upSyncQ.push(id); _upSyncPump();
  }
  function _upSyncPump() {
    if (_upSyncBusy || !_upSyncQ.length || !UP_FILE) return;
    var id = _upSyncQ.shift();
    _upSyncBusy = true;
    RC.reqJson('PATCH', UP_API, { file: UP_FILE, id: id }).then(function (d) {
      if (d && d.ok && d.job_id) { _upWatchSync(d.job_id, id); }                 // 脏 → 起 edit job,静默轮询
      else if (d && !d.ok && /进行中/.test(d.error || '')) {                     // 撞并发改页 job(新建/删除)→ 稍后重排
        _upSyncQ.push(id); _upSyncBusy = false; setTimeout(_upSyncPump, 2500);
      } else { _upSyncBusy = false; _upSyncPump(); }                             // clean / 其它 → 直接下一个
    }).catch(function () { _upSyncBusy = false; setTimeout(_upSyncPump, 2500); });
  }
  function _upWatchSync(jobId, id) {
    var bn = _upMini('正在把这页写回 PDF…');
    var t = setInterval(function () {
      fetch('/pdf/api/job-status?id=' + encodeURIComponent(jobId), { cache: 'no-store' })
        .then(function (r) { return r.json(); }).then(function (j) {
          if (j.status === 'done') { clearInterval(t); _upMiniEnd(bn, '✔ 已同步到 PDF'); _upSyncBusy = false; _upSyncPump(); }
          else if (j.status === 'error' || j.status === 'unknown') {
            clearInterval(t); _upMiniEnd(bn, '同步待恢复(稍后重试)', true); _upSyncBusy = false; _upSyncPump();
          }
        }).catch(function () {});   // 网抖:下一轮
    }, 1200);
  }
  function _upMini(text) {
    var b = document.createElement('div'); b.className = 'up2-mini';
    b.innerHTML = '<span class="up2-spin"></span><span class="up2-mini-t"></span>';
    b.querySelector('.up2-mini-t').textContent = text; document.body.appendChild(b); return b;
  }
  function _upMiniEnd(b, text, err) {
    if (!b) return;
    var sp = b.querySelector('.up2-spin'); if (sp) sp.remove();
    var t = b.querySelector('.up2-mini-t'); if (t) t.textContent = text;
    b.classList.add(err ? 'err' : 'ok');
    setTimeout(function () { try { b.remove(); } catch (_) {} }, err ? 3400 : 1500);
  }
  // 关书:对本会话编辑过的页发 keepalive 同步(fire-and-forget;PATCH 支持 keepalive,sendBeacon 只能 POST 不能用)。
  //   服务端不脏则自然 no-op;脏则起 job 异步写回(用户已离开,无感)。边车文字已由 _upTextFlushBeacon 保住。
  function _upSyncFlushKeepalive() {
    if (!UP_FILE) return;
    for (var id in _upEditedIds) {
      if (_upIsTempId(id)) continue;   // 临时页无真 id,跳过(避免 404;绑真 id 后才有得同步)
      try {
        fetch(UP_API, { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ file: UP_FILE, id: id }), keepalive: true });
      } catch (_) {}
    }
  }
  window.addEventListener('pagehide', _upSyncFlushKeepalive);
  function _upTextFlushBeacon() {
    if (!UP_FILE) return;
    for (var id in _upTextDirty) {
      if (!_upTextDirty[id]) continue;
      if (_upIsTempId(id)) continue;   // 临时页无真 id → 无法落库(边界:创建 job 未完成就关书,缓冲的字会丢;见 references)
      var snap = _upTextSnap[id]; if (!snap) continue;
      clearTimeout(_upTextTimers[id]);
      try {
        navigator.sendBeacon && navigator.sendBeacon(UP_TEXT_API,
          new Blob([JSON.stringify({ file: UP_FILE, id: id, title: snap.title, md: snap.md })], { type: 'application/json' }));
      } catch (_) {}
    }
    _upTextDirty = {};
  }
  window.addEventListener('pagehide', _upTextFlushBeacon);
  document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'hidden') _upTextFlushBeacon(); });
  // #1 检查结果里的「判分依据图」点击放大(复用 .fig-lightbox)
  document.addEventListener('click', function (e) {
    var im = e.target && e.target.closest && e.target.closest('.up2-run-result img');
    if (!im) return;
    e.stopPropagation();
    var mask = document.createElement('div'); mask.className = 'fig-lightbox';
    var big = document.createElement('img'); big.src = im.src; big.alt = '';
    mask.appendChild(big); document.body.appendChild(mask);
    mask.addEventListener('click', function () { mask.remove(); });
  });

  // ══════════════ v4 批次3:overlay 覆盖层「过渡态」DOM 选词/查词/精确高亮(复用 html-reader.js 那套 offset 锚胶水)══════════════
  //   覆盖层 .up2-content 显示态是网页原生文本(RC.md 渲染),不走 PDF 字符层。选词/查词/高亮全用 DOM Selection +
  //   字符偏移锚(相对 .up2-content-body),逐段照搬 html-reader.js:_charOffset/_markRange/caretFromPoint/wordAt/高亮 offset CRUD。
  //   · 单击词 → RC.wordpop.show(同普通页单击查词);· 选一段 + 点工具栏 🖌 色板 → 建 offset 高亮(存 html-highlights sidecar,每页独立 key)。
  //   · 多词翻译/解释/对话/复制沿用原生 #sel-toolbar(checkSelection 捕获覆盖层原生选区,section F:开箱即用),批次3 只补「单击查词 + 精确高亮」两处缺口。
  //   · 与 char-layer 隔离:脏 overlay 页 pw 打 .pdf-upage-overlay + CSS 关该页空白 char-layer 指针(见样式;只对脏 overlay 页,不误伤 baked/普通/已同步页)。
  //   · overlay 页(不论是否已同步)始终挂覆盖层 → 全部走本逻辑(方案调整 2026-07-04);baked 页走原生 char-layer,不进本逻辑(门控:mode==='overlay')。
  var _ovStyle = document.createElement('style');
  _ovStyle.textContent =
    '.pdf-upage-overlay .char-layer{pointer-events:none}' +   /* 脏 overlay 页:空白 char-layer 不接指针,单击/选区全落覆盖层(覆盖层 z6 已在其上,这是二道保险)*/
    '.up2-content .up2-content-body{-webkit-user-select:text;user-select:text}' +   /* iOS:.page-wrap 家族 user-select:none,覆盖层全链显式放行(便签教训)*/
    '.up2-content mark.rc-html-hl{color:inherit;background:#fff59d;border-radius:2px;padding:0 .5px;cursor:pointer;-webkit-user-select:text;user-select:text}';
  document.head.appendChild(_ovStyle);

  var _ovHlCache = {};        // rec.id -> [highlight]（内存缓存,重挂覆盖层按 offset 复原）
  var _ovLastSelInfo = null;  // 最近一次 overlay 选区快照(选区时捕获,供工具栏色板高亮 —— 点色板时原生选区可能已被 mousedown 折叠,不能现读)
  var _ovLastDictTs = 0;

  function _ovHlColors() {
    return (window.RC && RC.settings && RC.settings.hlColors) ? RC.settings.hlColors() : ['#fff59d', '#a7f3d0', '#a3d4ff', '#fda4af'];
  }
  function _ovLangs() {   // 书语言(决定英/日分流);[] 让 RC.wordpop 按字符自动判英/日
    try { if (window.PdfAdapter && PdfAdapter.bookLangs) return PdfAdapter.bookLangs() || []; } catch (_) {}
    return [];
  }
  function _ovHlKey(rec) { return UP_FILE + '::' + rec.id; }   // 每 overlay 页独立 offset 空间(端点按 key 的 sha 分文件,零后端改动)
  function _ovRecOf(pw) {   // 由 .page-wrap / 虚拟 .pdf-upage 反查 overlay 记录(mode==='overlay' 才认;baked/普通页返 null)
    if (!pw) return null;
    if (pw.__upRec && pw.__upRec.mode === 'overlay') return pw.__upRec;   // 乐观新建/绑定后的虚拟 .pdf-upage 元素(无 data-page-num)
    if (!pw.dataset) return null;
    var pn = parseInt(pw.dataset.pageNum, 10); if (!pn) return null;
    var recs = _upRealPages();
    for (var i = 0; i < recs.length; i++) if (recs[i].page === pn && recs[i].mode === 'overlay') return recs[i];
    return null;
  }
  function _ovElOfRec(rec) {   // overlay 记录的承载元素:优先本会话虚拟 .pdf-upage(乐观新建/绑定后仍显示),否则真 .page-wrap(重开后)
    var el = rec && _upTempEls[rec.id];
    if (el && el.isConnected) return el;
    return document.querySelector('.page-wrap[data-page-num="' + (rec && rec.page) + '"]');
  }
  function _ovBodyOfRec(rec) {
    var el = _ovElOfRec(rec);
    return el ? el.querySelector('.up2-content-body') : null;
  }
  function _ovHlById(rec, id) { var a = _ovHlCache[rec.id] || []; for (var i = 0; i < a.length; i++) if (a[i].id === id) return a[i]; return null; }
  function _ovDictGate() { var now = Date.now(); if (now - _ovLastDictTs < 500) return false; _ovLastDictTs = now; return true; }
  function _ovPointFromEvent(e) {
    if (e && e.changedTouches && e.changedTouches[0]) { var t = e.changedTouches[0]; return { x: t.clientX, y: t.clientY, pointerType: 'touch' }; }
    if (e && typeof e.clientX === 'number') return { x: e.clientX, y: e.clientY, pointerType: e.pointerType || 'mouse' };
    return null;
  }
  function _ovCloseWordPop() { try { var wp = document.getElementById('word-pop'); if (wp && wp.style.display !== 'none') wp.style.display = 'none'; } catch (_) {} }

  // ── 字符偏移工具(相对指定容器 = .up2-content-body;照搬 html-reader _charOffset/_markRange:offset=Range.toString().length,还原=TreeWalker 累计 nodeValue,口径一致)──
  function _ovCharOffset(container, node, offset) {
    try { var r = document.createRange(); r.setStart(container, 0); r.setEnd(node, offset); return r.toString().length; } catch (_) { return 0; }
  }
  function _ovMarkRange(container, h) {
    var start = h.start, end = h.end; if (end <= start) return;
    var walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, null);
    var pos = 0, tn, segs = [];
    while ((tn = walker.nextNode())) {
      var len = tn.nodeValue.length, a = pos, b = pos + len;
      if (b > start && a < end) segs.push({ node: tn, s: Math.max(0, start - a), e: Math.min(len, end - a) });
      pos = b; if (pos >= end) break;
    }
    segs.forEach(function (seg) {
      var node = seg.node, s = seg.s, e = seg.e; if (e <= s) return;
      try {
        var mid = (s > 0) ? node.splitText(s) : node;
        if ((e - s) < mid.nodeValue.length) mid.splitText(e - s);
        var mk = document.createElement('mark');
        mk.className = 'rc-html-hl'; mk.setAttribute('data-hid', h.id);
        if (h.color) mk.style.background = h.color;
        mid.parentNode.insertBefore(mk, mid); mk.appendChild(mid);
      } catch (_) {}
    });
  }
  function _ovMarksOf(container, hid) { return container.querySelectorAll('mark.rc-html-hl[data-hid="' + hid + '"]'); }
  function _ovUnwrapMarks(container, hid) {
    Array.prototype.forEach.call(_ovMarksOf(container, hid), function (m) {
      var p = m.parentNode; if (!p) return; while (m.firstChild) p.insertBefore(m.firstChild, m); p.removeChild(m); try { p.normalize(); } catch (_) {}
    });
  }

  // ── 单击词查词(caretFromPoint + wordAt → RC.wordpop.show;非英/日词关框,照搬 html-reader clickWord)──
  function _ovCaretFromPoint(x, y) {
    if (document.caretRangeFromPoint) { var r = document.caretRangeFromPoint(x, y); return r ? { node: r.startContainer, offset: r.startOffset } : null; }
    if (document.caretPositionFromPoint) { var p = document.caretPositionFromPoint(x, y); return p ? { node: p.offsetNode, offset: p.offset } : null; }
    return null;
  }
  function _ovWordAt(node, off) {
    var s = node.nodeValue || ''; if (!s) return null;
    var isW = function (c) { return /[A-Za-z0-9'’\-]/.test(c) || /[぀-ヿ㐀-鿿가-힯一-鿿]/.test(c); };
    var i = off; if (i >= s.length) i = s.length - 1; if (i < 0) return null;
    if (!isW(s[i])) { if (i > 0 && isW(s[i - 1])) i--; else return null; }
    var lo = i, hi = i + 1;
    while (lo > 0 && isW(s[lo - 1])) lo--;
    while (hi < s.length && isW(s[hi])) hi++;
    return { node: node, start: lo, end: hi, text: s.slice(lo, hi) };
  }
  function _ovClickWord(x, y, rec, pointerType) {
    if (!(window.RC && RC.wordpop)) { _ovCloseWordPop(); return; }
    var pos = _ovCaretFromPoint(x, y);
    if (!pos || !pos.node || pos.node.nodeType !== 3) { _ovCloseWordPop(); return; }
    var w = _ovWordAt(pos.node, pos.offset);
    if (!w || !w.text) { _ovCloseWordPop(); return; }
    var t = w.text, isEn = /^[A-Za-z][A-Za-z'’\-]*$/.test(t), isJa = /[぀-ヿ]/.test(t);
    if (!isEn && !isJa) { _ovCloseWordPop(); return; }   // 纯中文等 → 不弹词典(同 html-reader)
    var rng = document.createRange();
    try { rng.setStart(w.node, w.start); rng.setEnd(w.node, w.end); } catch (_) { return; }
    if (!(RC.ui && RC.ui.rangeHitTest && RC.ui.rangeHitTest(rng, x, y, { pointerType: pointerType || 'mouse' }))) {
      _ovCloseWordPop(); return;   // 覆盖层大片空白不能被 caret 最近点吸附到正文末词
    }
    if (!_ovDictGate()) return;
    var rr = rng.getBoundingClientRect();
    var rect = { left: rr.left, top: rr.top, right: rr.right, bottom: rr.bottom };
    var pblk = w.node.parentElement && w.node.parentElement.closest ? w.node.parentElement.closest('p,li,td,blockquote,h1,h2,h3,h4,div') : null;
    var pctx = (pblk ? (pblk.textContent || '') : '').trim().slice(0, 1200);
    RC.wordpop.show({ word: t, rect: rect, ctx: pctx, file: UP_FILE, page: (rec && rec.page) || 0, langs: _ovLangs(),
      onFallback: function (word) { if (window.RC && RC.result) RC.result.aiCall('/pdf/api/translate', { text: word, target_lang: '中文' }, '🌐 翻译', _ovResultOpts(pctx)); } });
  }
  function _ovResultOpts(ctx) {
    return {
      kind: 'note',
      aiParams: function () { return (window.RC && RC.settings && RC.settings.aiParams) ? RC.settings.aiParams() : {}; },
      ankiSource: function () { return { file: UP_FILE, sentence: ctx || '', sourceUrl: location.origin + '/pdf/view?file=' + encodeURIComponent(UP_FILE) }; },
      markHighlight: function (text, bodyTxt, sent, k) {
        if (_ovLastSelInfo) _ovCreateHighlight(_ovLastSelInfo, (localStorage.getItem('pdf-hl-active') || _ovHlColors()[0]), (bodyTxt || sent || '').slice(0, 400));
        else if (window.RC && RC.toast) RC.toast('请先选中要标记的文字');
      }
    };
  }

  // ── 高亮 CRUD(offset sidecar,复用 /pdf/api/html-highlights;file 键=UP_FILE::rec.id 每页独立;照搬 html-reader 高亮胶水)──
  function _ovCreateHighlight(info, color, note) {
    if (!info || !info.anchor) { if (window.RC && RC.toast) RC.toast('无法定位选区'); return; }
    RC.reqJson('POST', '/pdf/api/html-highlights', { file: _ovHlKey(info.rec), start: info.anchor.start, end: info.anchor.end, text: info.text || '', color: color, sentence: info.ctx || '', note: note || '' })
      .then(function (d) {
        if (d && d.ok && d.highlight) {
          (_ovHlCache[info.rec.id] = _ovHlCache[info.rec.id] || []).push(d.highlight);
          var body = (info.body && info.body.isConnected) ? info.body : _ovBodyOfRec(info.rec);
          if (body) _ovMarkRange(body, d.highlight);
          if (window.RC && RC.toast) RC.toast('已高亮');
          _ovClearNativeSel();
        } else if (window.RC && RC.toast) RC.toast('高亮失败:' + ((d && d.error) || '?'));
      }).catch(function () { if (window.RC && RC.toast) RC.toast('高亮失败'); });
  }
  function _ovClearNativeSel() {
    try { var s = window.getSelection(); if (s) s.removeAllRanges(); } catch (_) {}
    try { var tb = document.getElementById('sel-toolbar'); if (tb) tb.classList.remove('open'); } catch (_) {}
    try { document.querySelectorAll('.sel-overlay').forEach(function (o) { o.innerHTML = ''; }); } catch (_) {}
    _ovLastSelInfo = null;
  }
  function _ovLoadHls(rec, cb) {
    if (_ovHlCache[rec.id]) { cb(_ovHlCache[rec.id]); return; }
    RC.reqJson('GET', '/pdf/api/html-highlights?file=' + encodeURIComponent(_ovHlKey(rec))).then(function (d) {
      _ovHlCache[rec.id] = (d && d.highlights) || []; cb(_ovHlCache[rec.id]);
    }).catch(function () { _ovHlCache[rec.id] = _ovHlCache[rec.id] || []; cb(_ovHlCache[rec.id]); });
  }
  function _ovApplyHls(body, rec) {
    if (!body || !rec) return;
    _ovLoadHls(rec, function (hls) {
      if (!body.isConnected) return;
      hls.slice().sort(function (a, b) { return a.start - b.start; }).forEach(function (h) { try { _ovMarkRange(body, h); } catch (_) {} });
    });
  }
  // MathJax 排版会把 $..$ 文本换成容器 → 改字符偏移口径;建高亮 与 重挂高亮 都在**排版后**做,保证口径一致(照 html-reader whenMathReady 思路)
  function _ovTypesetThenHl(body, rec) {
    var done = function () { try { _ovApplyHls(body, rec); } catch (_) {} };
    try { if (body && window.MathJax && MathJax.typesetPromise) { MathJax.typesetPromise([body]).then(done).catch(done); return; } } catch (_) {}
    done();
  }
  function _ovPatchHl(rec, h, f) {
    return RC.reqJson('PATCH', '/pdf/api/html-highlights', Object.assign({ file: _ovHlKey(rec), id: h.id }, f)).then(function (d) {
      if (!(d && d.ok && d.highlight)) {
        if (window.RC && RC.toast) RC.toast('高亮保存失败：' + ((d && d.error) || '服务未确认'));
        return false;
      }
      var body = _ovBodyOfRec(rec);
      if ('color' in f) { h.color = d.highlight.color; if (body) Array.prototype.forEach.call(_ovMarksOf(body, h.id), function (m) { m.style.background = h.color; }); }
      if ('note' in f) h.note = d.highlight.note;
      return true;
    }).catch(function (e) {
      if (window.RC && RC.toast) RC.toast('高亮保存失败：' + ((e && e.message) || '网络错误'));
      return false;
    });
  }
  function _ovDelHl(rec, h) {
    return RC.reqJson('DELETE', '/pdf/api/html-highlights?file=' + encodeURIComponent(_ovHlKey(rec)) + '&id=' + encodeURIComponent(h.id), null)
      .then(function (d) {
        if (!(d && d.ok)) {
          if (window.RC && RC.toast) RC.toast('删除失败：' + ((d && d.error) || '服务未确认'));
          return false;
        }
        var body = _ovBodyOfRec(rec); if (body) _ovUnwrapMarks(body, h.id);
        _ovHlCache[rec.id] = (_ovHlCache[rec.id] || []).filter(function (x) { return x.id !== h.id; });
        if (window.RC && RC.toast) RC.toast('已删除');
        return true;
      }).catch(function (e) {
        if (window.RC && RC.toast) RC.toast('删除失败：' + ((e && e.message) || '网络错误'));
        return false;
      });
  }
  function _ovOpenHlEditor(rec, h) {   // 点高亮 → RC.highlight.openEditor(改色/备注/删,host 无关)
    if (!(window.RC && RC.highlight)) { if (window.RC && RC.toast) RC.toast('编辑层未就绪'); return; }
    RC.highlight.openEditor({
      colors: _ovHlColors(), current: h.color, note: h.note || '', preview: h.text || '', sentence: h.sentence || '',
      onColor: function (c) { return _ovPatchHl(rec, h, { color: c }); },
      onNote: function (t) { return _ovPatchHl(rec, h, { note: t }); },
      onDelete: function () { return _ovDelHl(rec, h); }
    });
  }
  // 乐观新建绑真 id 时:临时期(tempId 键)的 offset 高亮迁到 realId 键(每 overlay 页独立 sidecar),重挂 mark。
  //   复用 html-highlights 端点,零后端改动;高亮建/改/删在临时期照常工作(rec.id=tempId),绑定时统一搬键。
  function _upMigrateOvHls(tempId, realId, rec) {
    if (!UP_FILE) { delete _ovHlCache[tempId]; return; }
    if (!_ovHlCache[tempId] || !_ovHlCache[tempId].length) { delete _ovHlCache[tempId]; return; }   // 临时期没建过高亮 → 免一次 GET(常态)
    var tempKey = UP_FILE + '::' + tempId, realKey = UP_FILE + '::' + realId;
    RC.reqJson('GET', '/pdf/api/html-highlights?file=' + encodeURIComponent(tempKey), null).then(function (d) {
      var hls = (d && d.highlights) || [];
      if (!hls.length) { delete _ovHlCache[tempId]; return; }
      Promise.all(hls.map(function (h) {
        return RC.reqJson('POST', '/pdf/api/html-highlights', { file: realKey, start: h.start, end: h.end, text: h.text || '', color: h.color, sentence: h.sentence || '', note: h.note || '' })
          .then(function (r) { return (r && r.highlight) || null; }).catch(function () { return null; });
      })).then(function (results) {
        var saved = results.filter(Boolean);
        _ovHlCache[realId] = saved; delete _ovHlCache[tempId];
        hls.forEach(function (h) { RC.reqJson('DELETE', '/pdf/api/html-highlights?file=' + encodeURIComponent(tempKey) + '&id=' + encodeURIComponent(h.id), null).catch(function () {}); });
        var body = _ovBodyOfRec(rec);
        if (body) { hls.forEach(function (h) { _ovUnwrapMarks(body, h.id); }); saved.forEach(function (h) { _ovMarkRange(body, h); }); }
      });
    }).catch(function () { delete _ovHlCache[tempId]; });
  }

  // ── 当前 overlay 选区信息(offset 锚 + 上下文);非 overlay/编辑态/collapsed 返 null(不干扰 char-layer 高亮)──
  function _ovActiveSelInfo() {
    try {
      var sel = window.getSelection();
      if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return null;
      var rng = sel.getRangeAt(0);
      var node = rng.commonAncestorContainer;
      var el = node.nodeType === 3 ? node.parentElement : node;
      var body = el && el.closest ? el.closest('.up2-content-body') : null;
      if (!body) return null;
      var ov = body.closest ? body.closest('.up2-content') : null;
      if (!ov || ov.classList.contains('editing')) return null;
      if (!body.contains(rng.startContainer) || !body.contains(rng.endContainer)) return null;
      var pw = ov.closest ? ov.closest('.page-wrap, .pdf-upage') : null;   // .pdf-upage=乐观新建虚拟页(offset 高亮同真页)
      var rec = _ovRecOf(pw); if (!rec) return null;
      var txt = (sel.toString() || '').trim(); if (!txt) return null;
      var start = _ovCharOffset(body, rng.startContainer, rng.startOffset);
      var end = _ovCharOffset(body, rng.endContainer, rng.endOffset);
      if (end < start) { var tmp = start; start = end; end = tmp; }
      if (end <= start) return null;
      var blk = rng.startContainer.nodeType === 3 ? rng.startContainer.parentElement : rng.startContainer;
      blk = blk && blk.closest ? blk.closest('p,li,td,blockquote,div,section,h1,h2,h3,h4') : null;
      var ctx = (blk ? (blk.textContent || '') : (body.textContent || '')).trim().slice(0, 1200);
      return { body: body, rec: rec, anchor: { start: start, end: end }, text: txt, ctx: ctx };
    } catch (_) { return null; }
  }

  // ── 工具栏 🖌 色板高亮入口拦截:overlay 选区点色板 → offset 高亮(拦掉原生 onPickColor 的 char-layer 分支;
  //    capture 早于 swatch 自身 onclick[bubble],stopImmediatePropagation 拦停;非 overlay 选区放行原生)──
  (function _ovBindPicker() {
    var picker = document.getElementById('hl-color-picker');
    if (!picker) return;
    picker.addEventListener('click', function (e) {
      var sw = e.target && e.target.closest ? e.target.closest('.swatch') : null;
      if (!sw) return;
      var info = _ovLastSelInfo;
      if (!info || !info.body || !info.body.isConnected) return;   // 非 overlay 选区(或已失效)→ 放行原生 char-layer 高亮
      e.stopImmediatePropagation(); e.preventDefault();
      var swatches = picker.querySelectorAll('.swatch');
      var idx = Array.prototype.indexOf.call(swatches, sw);
      var colors = _ovHlColors();
      var color = colors[idx] || colors[0] || '#fff59d';
      try { localStorage.setItem('pdf-hl-active', color); } catch (_) {}
      _ovCreateHighlight(info, color);
    }, true);
  })();
  // 非 overlay/非工具栏区域按下 → 作废 overlay 选区快照(防选完 overlay 又去别处选 char-layer 时色板误判为 overlay 高亮)
  function _ovMaybeClearSel(e) {
    var t = e && e.target;
    if (t && t.closest && (t.closest('.up2-content') || t.closest('#sel-toolbar') || t.closest('#word-pop') || t.closest('#hl-popover'))) return;
    _ovLastSelInfo = null;
  }
  document.addEventListener('mousedown', _ovMaybeClearSel, true);
  document.addEventListener('touchstart', _ovMaybeClearSel, true);

  // ── 手写(画图):虚拟 .pdf-upage 元素补 __inkCanvas + 绑 _inkPointerDown(照 04-render 对普通页做法)= 手写天然生效 ──
  //   真 .page-wrap 由 04-render 建 canvas(有 __inkCanvas → 跳过);只给虚拟元素(乐观新建/绑定后本会话显示)补。
  //   墨迹存 el.__inkStrokes(同真页 pw.__inkStrokes);临时页(未绑真 id)只缓冲不 POST,绑真 id 后写 realPage(见 _upInkPersist)。
  function _upEnsureInk(el) {
    if (!el || el.__inkCanvas) return;   // 真 .page-wrap 已有 canvas → 跳过(不建第二张);只补虚拟元素
    var cw = Math.max(1, el.clientWidth || Math.round(el.getBoundingClientRect().width) || 300);
    var ch = Math.max(1, el.clientHeight || Math.round(el.getBoundingClientRect().height) || 400);
    var cv = document.createElement('canvas'); cv.className = 'ink-layer';   // z7,pointer-events:none(CSS);绘制靠 pw capture 拦 pointerdown
    // ⚠ 不写 style.width/height:.ink-layer 的 CSS 本来就是 position:absolute + inset:0,
    //   会自动铺满纸;一旦写死 px 就把这份自动跟随覆盖掉 —— 页面一缩放,纸变了而墨迹层
    //   停在旧尺寸(用户实测:自定页上的绘图不跟着缩放)。这里只管**位图分辨率**,
    //   CSS 尺寸交给 inset:0,缩放时立刻跟上,随后由 ResizeObserver 校准清晰度。
    var dpr = window.devicePixelRatio || 1;
    cv.width = Math.floor(cw * dpr); cv.height = Math.floor(ch * dpr);
    el.style.position = el.style.position || 'relative';
    el.appendChild(cv); el.__inkCanvas = cv;
    if (!el.__inkStrokes) el.__inkStrokes = [];
    if (!el.__inkBound) {   // 与 04-render 普通页同款:capture pointerdown → _inkPointerDown(pen 才画);Pencil 触摸阻默认滚动
      el.addEventListener('pointerdown', function (e) { if (window._inkPointerDown) window._inkPointerDown(e); }, true);
      var _blk = function (e) { for (var i = 0; i < e.touches.length; i++) { if (e.touches[i].touchType === 'stylus') { e.preventDefault(); break; } } };
      el.addEventListener('touchstart', _blk, { passive: false });
      el.addEventListener('touchmove', _blk, { passive: false });
      el.__inkBound = true;
    }
  }
  function _upResizeInk(el) {   // 显示态渲染后按元素真实尺寸校准 canvas 背景分辨率(内容高度可能变);坐标用 getBoundingClientRect 恒准,只影响清晰度
    var cv = el && el.__inkCanvas; if (!cv) return;
    var cw = Math.max(1, el.clientWidth || Math.round(el.getBoundingClientRect().width));
    var ch = Math.max(1, el.clientHeight || Math.round(el.getBoundingClientRect().height));
    // 只调位图分辨率;CSS 尺寸由 .ink-layer 的 inset:0 自动跟随(不再写 style.width/height,
    // 那会覆盖掉自动跟随)。因此比较的是**位图**是否已是想要的尺寸,不再读 style。
    var dpr = window.devicePixelRatio || 1;
    var wantW = Math.floor(cw * dpr), wantH = Math.floor(ch * dpr);
    if (cv.width === wantW && cv.height === wantH) return;
    cv.width = wantW; cv.height = wantH;
    if (window._inkRedraw) window._inkRedraw(el);
  }
  // 虚拟 .pdf-upage 墨迹落盘:绑真 id 后 POST 到 realPage(/api/ink 按整数页键);未绑(临时)则只留 el.__inkStrokes,绑定时补。
  //   不写 _ink.byPage[realPage](本会话那页号仍是被 stale 的原页,写它会让 stale 页误显本页墨迹);下次开书 _inkLoadAll 从服务端拿。
  window._upInkPersist = function (el) {
    var rec = el && el.__upRec; if (!rec || _upIsTempId(rec.id) || !UP_FILE) return;
    try {
      // 55:记自回声指纹(与 pdf-tail _ink.echo 同一本账)——插入页存墨迹触发的 SSE 广播会被本端收到,
      // 按页号命中"尚未重编号的旧同名页"(=插入页的下一页)把墨迹串过去;3s 抑制窗兜住
      try { var _ik = window._ink || (window._ink = {}); (_ik.echo = _ik.echo || {})[rec.page] = Date.now(); } catch (e) {}
      fetch('/pdf/api/ink', { method: 'POST', headers: { 'Content-Type': 'application/json' }, keepalive: true,
        body: JSON.stringify({ file: UP_FILE, page: rec.page, strokes: el.__inkStrokes || [] }) });
    } catch (_) {}
  };

  // overlay 页常驻覆盖层:显示态渲 RC.md(空则提示);点击进 textarea 即时编辑
  // 这一页刚渲染出来 → 把"曾经绑不上、因为那时它还没渲染"的卡接回去。
  //   放在 _upRenderOverlay 里而不是逐个挂载点，是因为挂载路径有好几条
  //   （lp 本机纸 / 服务端纸 / 重渲染），但它们都收敛到这里。
  function _upBindRetryFor(rec) {
    try {
      if (rec && rec.id && typeof window.__upBindRetry === 'function') {
        window.__upBindRetry(rec.id);
      }
    } catch (e) {}
  }
  function _upRenderOverlay(ov, rec) {
    ov.classList.remove('editing');
    _upBindRetryFor(rec);   // 这一页出现了 → 把等着绑到它上面的卡接回去
    // ★ 任务运行时(references/adr-task-runtime.md):这一页有结构化块 → 走块渲染;否则原 md 路径不动。
    if (rec.blocks && rec.blocks.length) { _upRenderBlocks(ov, rec); return; }
    var md = (rec.md || '').trim();
    var _ovLb = _upLabel(rec);
    ov.innerHTML = '<div class="up2-content-hd">📝 ' +
      (_ovLb ? ('<span class="up2-lb">' + RC.esc(_ovLb) + '</span> ') : '') +
      (rec.title ? RC.esc(rec.title) : '我的页') + '</div>' +
                   '<div class="up2-content-body' + (md ? '' : ' empty') + '"></div>';
    var body = ov.querySelector('.up2-content-body');
    if (md) { body.innerHTML = (window.RC && RC.md) ? RC.md(rec.md) : RC.esc(rec.md); _ovTypesetThenHl(body, rec); }   // 批次3:typeset 后按 offset 复原高亮(口径一致)
    else { body.textContent = '（点左上角 ✏️ 写笔记 · 自动保存,无需按钮）'; }
    var host = ov.closest ? ov.closest('.pdf-upage') : null; if (host && host.__inkCanvas) _upResizeInk(host);   // 虚拟页:显示态校准手写层尺寸(真 .page-wrap 无 .pdf-upage 祖先 → 跳过)
  }
  // 任务运行时:AI 建的插入页是**真往 PDF 文件里插了一页** —— 浏览器里加载的 PDF 已经过时
  //   (页数都变了),光 jumpWithBack 只会跳到**旧文档**的那一页,新纸根本不在里面。必须重载。
  //   ⚠ 必须挂 window:client_action 是 `window[fn].apply(...)` 找函数的(模块内的声明它够不到)。
  // ★ AI 遥控前端**建一张任务纸**(听写等)。
  //   ⚠ 建页**必须走前端已有的乐观新建链路**:立刻插一个虚拟页、马上可用,PDF 写回在后台异步跑,
  //     **不刷新、不卡顿**(CLAUDE.md「乐观新建即全功能页」)。
  //     我一开始让后端**同步**插页再整本书重载 —— 大书上要卡好几秒,而且把浏览器里那份 PDF 作废了。
  //     用户拍板:前端才是建页的执行者,AI 只负责遥控 + 注入内容。
  // ── 检查结果面板(共享渲染)──────────────────────────────────────────────
  //   可**收起**(默认展开;收起后只剩一条头,不再挡纸内容)—— 收起态按纸 id 记住,跨重画不丢。
  //   **不放「让 AI 讲讲」按钮**(用户设计):上下文只告知"最近有检查报告《名》",用户直接问,
  //   AI 调 read_check_report(一个带报告上下文、能自己查书核实的子 agent)作答。这里只 stash 报告名/得分。
  var _upResCollapsed = {};   // upage id → true(收起)
  function _upResKey(ov, fallback) {
    try { var h = ov.closest('[data-uid]'); if (h && h.getAttribute('data-uid')) return h.getAttribute('data-uid'); } catch (e) {}
    return fallback || '_';
  }
  function _upRenderResult(ov, md, key, name, score) {
    var r = ov.querySelector('.up2-run-result');
    if (!r) { r = document.createElement('div'); r.className = 'up2-run-result'; ov.appendChild(r); }
    var collapsed = !!_upResCollapsed[key];
    r.classList.toggle('collapsed', collapsed);
    var bodyHtml; try { bodyHtml = (window.RC && RC.md) ? RC.md(md) : RC.esc(md); } catch (e) { bodyHtml = RC.esc(md); }
    var hd = document.createElement('div'); hd.className = 'up2-rr-hd';
    var tt = document.createElement('span'); tt.className = 'up2-rr-tt'; tt.textContent = '🔍 检查结果';
    var sp = document.createElement('span'); sp.className = 'up2-rr-sp';
    var tog = document.createElement('button'); tog.type = 'button'; tog.className = 'up2-rr-tog'; tog.textContent = collapsed ? '展开 ▸' : '收起 ▾';
    var rej = document.createElement('button'); rej.type = 'button'; rej.className = 'up2-rr-tog'; rej.textContent = '↻ 重判';
    rej.title = '对判分有异议/改了答案 → 重新截图、更仔细地复判';
    rej.addEventListener('click', function (e) {
      e.stopPropagation();
      var host = ov.closest ? ov.closest('.pdf-upage, .page-wrap') : null;
      var rec2 = host && host.__upRec;
      if (rec2) _upRunEvent(rec2, 'check:__recheck__');
    });
    hd.appendChild(tt); hd.appendChild(sp); hd.appendChild(rej); hd.appendChild(tog);
    var bd = document.createElement('div'); bd.className = 'up2-rr-bd'; bd.innerHTML = bodyHtml;
    r.innerHTML = ''; r.appendChild(hd); r.appendChild(bd);
    tog.addEventListener('click', function (e) {
      e.stopPropagation();
      _upResCollapsed[key] = !_upResCollapsed[key];
      var c = !!_upResCollapsed[key];
      r.classList.toggle('collapsed', c); tog.textContent = c ? '展开 ▸' : '收起 ▾';
    });
    // (旧 __lastCheckResult 专线已退役:上下文告知统一走服务端创造物库,文字=_sys_prompt 直读、语音=creations-brief 端点。)
  }

  // 任务运行时:按 upage id 重画那张纸(检查结果写回 sidecar 后,SSE text 事件触发)。
  window.__upRerender = function (upId) {
    try {
      RC.reqJson('GET', UP_TEXT_API + '?file=' + encodeURIComponent(UP_FILE)).then(function (g) {
        var fresh = ((g && (g.pages || g.items)) || []).filter(function (x) { return x.id === upId; })[0];
        if (!fresh) return;
        // 找到这张纸的覆盖层重画(它已挂在某个 page-wrap / pdf-upage 上)
        var el = document.querySelector('[data-uid="' + upId + '"]') ||
                 (fresh.page ? document.querySelector('.page-wrap[data-page-num="' + fresh.page + '"]') : null);
        var ov = el ? el.querySelector('.up2-content') : null;
        if (!ov && fresh.page) { var pw = document.querySelector('[data-page-num="' + fresh.page + '"]'); ov = pw ? pw.querySelector('.up2-content') : null; }
        if (ov) _upRenderOverlay(ov, fresh);
      }).catch(function () {});
    } catch (e) {}
  };
  // 进度:更新那张纸卡片**内**的提示行(在标题下方,不在卡上方)。
  window.__upRunProgress = function (run) {
    try {
      if (!run || !run.rid) return;
      var ov = document.querySelector('[data-up-run="' + run.rid + '"]');
      if (!ov) return;
      var h = ov.querySelector('.up2-run-hint');
      if (h) h.textContent = run.hint || '';
      // 检查/批改结果(AI 回复)显示在**卡片内**(纸的下方),渲成 markdown。不塞进纸格子(会撑破)。
      if (run.result_md) _upRenderResult(ov, run.result_md, _upResKey(ov, run.rid), run.check_name, run.check_score);
    } catch (e) {}
  };

  // 建一张任务纸(乐观新建),绑到真页号后回调 onReady(rec, tmpEl)。多纸时反复调它。
  //   afterEl(可选):把新页**紧邻这个 DOM 元素之后**插入 —— 多纸溢出必须用它,否则按页号 _upPlace
  //   会命中"乐观插入未重编号的陈旧同号真页"→ 溢出页落到它后面 = 两页中间隔一页(用户实测 #1)。
  // ═══ 本机书本地造纸(_lp*)═══════════════════════════════════════════════════
  // 布局算术移植自 paper.py(spec/default_span/layout)——**单源在服务端**,这里是
  // 刻意的最小副本(改语义两边同步,rect 直接复用 _upGridRect=同一算术)。持久化
  // localStorage['lp:'+UP_FILE];渲染/交互 100% 复用服务端纸的 _upRenderOverlay。
  var _lpPAPERS = { dictation: { bg: '#fffdf7', rows: 20, cols: 24, margin: .05, font_ratio: .42, rule: 'line' },
                    exam: { bg: '#ffffff', rows: 30, cols: 34, margin: .05, font_ratio: .5, rule: 'none' },
                    math: { bg: '#fbfdff', rows: 32, cols: 30, margin: .04, font_ratio: .5, rule: 'grid' },
                    draw: { bg: '#fffefa', rows: 24, cols: 26, margin: .03, font_ratio: .5, rule: 'none' },
                    note: { bg: '#fffdf7', rows: 26, cols: 28, margin: .05, font_ratio: .45, rule: 'line' } };
  function _lpSpec(kind) {   // A4 固定 595×842:与服务端打不开书时的 fallback 同款,插入页无需跟书页同比例
    var p = _lpPAPERS[kind] || _lpPAPERS.note, pw = 595, ph = 842, mx = pw * p.margin, my = ph * p.margin;
    return { kind: _lpPAPERS[kind] ? kind : 'note', bg: p.bg, rule: p.rule, rows: p.rows, cols: p.cols,
             font_ratio: p.font_ratio, mx: mx, my: my, page_w: pw, page_h: ph,
             char_w: (pw - 2 * mx) / p.cols, line_h: (ph - 2 * my) / p.rows };
  }
  function _lpWide(s) { var n = 0, t = String(s || ''); for (var i = 0; i < t.length; i++) n += t.charCodeAt(i) < 0x2E80 ? .5 : 1; return Math.ceil(n); }
  function _lpSpan(b, sp) {
    var k = b.kind, C = sp.cols;
    if (k === 'text') { var w = (b.cols | 0) || C, need = Math.max(1, Math.ceil(_lpWide(b.text) / Math.max(1, w))); if (b.style === 'h1') need += 1; return [need, w]; }
    if (k === 'blank') return [Math.max(1, Math.ceil((_lpWide(b.label) + 8) / C)), C];
    if (k === 'choice') {
      var q = Math.max(1, Math.ceil(_lpWide(b.text || b.label) / C)), opts = b.options || [], orows = opts.length ? 1 : 0, used = 0;
      for (var i = 0; i < opts.length; i++) { var w2 = Math.min(C, _lpWide('A. ' + opts[i]) + 2); if (used && used + w2 > C) { orows += 1; used = 0; } used += w2; }
      return [q + orows + 1, C];
    }
    if (k === 'button') return [1, Math.min(C, _lpWide(b.label) + 3)];
    if (k === 'checkbox') return [1, Math.min(C, _lpWide(b.label) + 4)];
    return [1, C];   // hr 及其它:1 行整宽
  }
  function _lpHit(occ, r, c, h, w) { for (var i = 0; i < occ.length; i++) { var o = occ[i]; if (!(r + h <= o[0] || o[0] + o[2] <= r || c + w <= o[1] || o[1] + o[3] <= c)) return true; } return false; }
  function _lpLayout(blocks, sp) {   // 碰撞安全 + 自动补页(铁律:每个元素独占位置,冲突往后挪)
    var ROWS = sp.rows, COLS = sp.cols, pages = [], cur = [], occ = [], r = 0, c = 0;
    function np() { if (cur.length) pages.push(cur); cur = []; occ = []; r = 0; c = 0; }
    (blocks || []).forEach(function (b0) {
      var b = {}; for (var k in b0) b[k] = b0[k];
      var s = b.span || _lpSpan(b, sp), h = Math.max(1, Math.min(s[0] | 0, ROWS)), w = Math.max(1, Math.min(s[1] | 0, COLS));
      if (b.at) {
        var ar = Math.max(0, b.at[0] | 0), ac = Math.max(0, Math.min(b.at[1] | 0, COLS - w));
        while (ar + h <= ROWS && _lpHit(occ, ar, ac, h, w)) ar += 1;
        if (ar + h > ROWS) { np(); ar = 0; ac = Math.max(0, Math.min(b.at[1] | 0, COLS - w)); while (ar + h <= ROWS && _lpHit(occ, ar, ac, h, w)) ar += 1; }
        b.at = [ar, ac]; b.span = [h, w]; b.rect = _upGridRect(sp, ar, ac, h, w); occ.push([ar, ac, h, w]); cur.push(b); return;
      }
      if (c + w > COLS) { r += 1; c = 0; }
      for (;;) { if (c + w > COLS) { r += 1; c = 0; } if (r + h > ROWS) np(); if (!_lpHit(occ, r, c, h, w)) break; c += 1; }
      b.at = [r, c]; b.span = [h, w]; b.rect = _upGridRect(sp, r, c, h, w); occ.push([r, c, h, w]); cur.push(b);
      if (w >= COLS) { r += h; c = 0; } else { c += w; if (c >= COLS) { r += h; c = 0; } }
    });
    if (cur.length) pages.push(cur);
    return pages.length ? pages : [[]];
  }
  function _lpNorm(blocks) {   // paper.normalize_blocks 的最小副本:只保"渲染不出空壳"
    var out = [];
    (Array.isArray(blocks) ? blocks : []).forEach(function (a) {
      if (!a || typeof a !== 'object') return;
      var k = a.kind; if (['text', 'blank', 'choice', 'checkbox', 'button', 'hr'].indexOf(k) < 0) return;
      var b = {}; for (var key in a) b[key] = a[key];
      var t = String(b.text || '').trim(), l = String(b.label || '').trim();
      if (k === 'text' || k === 'hr' || k === 'choice') { if (!t && l) { b.text = b.label; delete b.label; } }
      else if (!l && t) { b.label = b.text; delete b.text; }
      if (k === 'choice') {
        var o = b.options; if (typeof o === 'string') o = o.split('/').map(function (x) { return x.trim(); }).filter(Boolean);
        if (!Array.isArray(o) || !o.length) { b.kind = 'text'; delete b.options; }
        else { b.options = o.slice(0, 6).map(function (x) { return String(x).replace(/^[A-Da-d][\.、\)]\s*/, '').trim(); });
               if (b.answer) b.answer = String(b.answer).trim().toUpperCase().slice(0, 1); }
      }
      if (!b.id) b.id = 'b' + out.length;
      out.push(b);
    });
    // 不自动补「让 AI 检查」按钮(2026-08-17 用户拍板):批改=让语音 AI 直接看纸,
    // 不需要页内按钮与截图上传管道。按钮仍合法,模型按需显式放。
    return out.slice(0, 48);
  }
  function _lpLoad() { try { var d = JSON.parse(localStorage.getItem('lp:' + UP_FILE) || 'null'); return (d && d.v === 1 && Array.isArray(d.papers)) ? d.papers : []; } catch (e) { return []; } }
  var _lpPapers = null;   // 内存权威:渲染器/交互直接改 rec,落盘统一走 _lpSaveSoon
  function _lpAll() { if (!_lpPapers) _lpPapers = _lpLoad(); return _lpPapers; }
  var _lpSaveT = 0;
  function _lpSaveSoon() {
    clearTimeout(_lpSaveT);
    _lpSaveT = setTimeout(function () {
      try { localStorage.setItem('lp:' + UP_FILE, JSON.stringify({ v: 1, papers: _lpAll() })); }
      catch (e) { try { console.warn('[lp] 本地纸保存失败', e); } catch (e2) {} }
    }, 400);
  }
  var _lpMounted = {};
  function _lpMountOne(rec, afterEl) {
    var tmp = document.createElement('div');
    tmp.className = 'rc-upage pdf-upage up2-new'; tmp.dataset.uid = rec.id;
    tmp.style.aspectRatio = '595/842'; tmp.style.minHeight = '0';
    var placed = false;
    if (afterEl && afterEl.isConnected) {
      try { if (afterEl.style.width) tmp.style.width = afterEl.style.width; afterEl.parentNode.insertBefore(tmp, afterEl.nextSibling); placed = true; } catch (e) {}
    }
    if (!placed && !_upPlace(tmp, rec.after || 0, null)) return null;
    _lpMounted[rec.id] = tmp;
    _upMountOverlay(rec, tmp);
    var ov = tmp.querySelector('.up2-content');
    if (ov) {
      _upRenderOverlay(ov, rec);
      ov.addEventListener('click', _lpSaveSoon, true);   // capture:选择题 picked 等改动落盘(按钮的 stopPropagation 在冒泡段,不挡这里)
    }
    // 删除走设计内入口:每张覆盖页都有左上角 Aa → 编辑面板 → 🗑(up2-del2)→
    // _upDelReal —— lp 分支已让终点认识本机纸(整组删)。不另造删除按钮。
    return tmp;
  }
  function _lpSyncWidths() {
    // 缩放/侧栏挤压后真实页宽度变了:镜像 _upPlace 的对齐规则(宽度对齐邻页,
    // aspect-ratio 驱动高度)。lp 页挂载后不再经 _upPlace,靠本函数在每次
    // _userpagesMount 幂等钩子里跟排——否则"新建页不跟其它页一起缩放"。
    Object.keys(_lpMounted).forEach(function (id) {
      var el = _lpMounted[id];
      if (!el || !el.isConnected) return;
      var sib = el.previousElementSibling;
      while (sib && !(sib.classList && sib.classList.contains('page-wrap'))) sib = sib.previousElementSibling;
      var ref = sib || document.querySelector('.page-wrap[data-page-num]');
      if (!ref) return;
      var w = ref.getBoundingClientRect().width;
      if (w > 40 && Math.abs((parseFloat(el.style.width) || 0) - w) > 1) el.style.width = Math.round(w) + 'px';
    });
  }
  function _lpRestore() {   // 幂等恢复:页 DOM 未就绪时 _upPlace 失败,靠调用方重试补挂
    var prevByGid = {};
    _lpAll().forEach(function (rec) {
      if (_lpMounted[rec.id] && _lpMounted[rec.id].isConnected) { prevByGid[rec.gid] = _lpMounted[rec.id]; return; }
      var el = _lpMountOne(rec, rec.idx > 0 ? prevByGid[rec.gid] : null);
      if (el) prevByGid[rec.gid] = el;
    });
    _lpSyncWidths();
  }
  function _lpStartTask(spec) {
    var sp = _lpSpec(spec.paper || (spec.params && spec.params.paper) || 'note');
    var blocks = _lpNorm((spec.params && spec.params.blocks) || []);
    if (!blocks.length) { alert('这张纸没有可用元素'); return; }
    var pages = _lpLayout(blocks, sp);
    var after = 0; try { after = _upCurPage() | 0; } catch (e) {}
    var gid = 'lp_' + Date.now().toString(36), prev = null, title = spec.title || '练习纸';
    pages.forEach(function (pageBlocks, i) {
      var rec = { id: gid + '_' + i, gid: gid, idx: i, after: after, mode: 'overlay',
                  title: i ? title + ' · 第 ' + (i + 1) + ' 页' : title,
                  run_id: gid + '_' + i,   // 本地纸 run_id=自身 id(lp_ 前缀)→ _upRunEvent 分流到本地语义
                  paper: sp, blocks: pageBlocks, created: Date.now() };
      _lpAll().push(rec);
      var el = _lpMountOne(rec, prev);
      if (el && !i) { try { el.scrollIntoView({ block: 'center', behavior: 'auto' }); } catch (e) {} }
      if (el) prev = el;
    });
    _lpSaveSoon();
  }
  function _lpRunEvent(rec, ev) {   // 本地事件语义:能本地做的本地做,做不到的诚实说,绝不静默
    var sep = String(ev || '').indexOf(':'), act = sep < 0 ? String(ev || '') : ev.slice(0, sep), arg = sep < 0 ? '' : ev.slice(sep + 1);
    function rerender() { var el = _lpMounted[rec.id], ov = el && el.querySelector('.up2-content'); if (ov) _upRenderOverlay(ov, rec); _lpSaveSoon(); }
    if ((act === 'reveal' || act === 'hide') && arg) { (rec.blocks || []).forEach(function (b) { if (b.id === arg) b.hidden = (act === 'hide'); }); rerender(); return; }
    if ((act === 'set_enabled' || act === 'enable' || act === 'disable') && arg) { (rec.blocks || []).forEach(function (b) { if (b.id === arg) b.enabled = (act !== 'disable'); }); rerender(); return; }
    if (act === 'goto' && /^\d+$/.test(arg)) { try { if (window.jumpWithBack) window.jumpWithBack(parseInt(arg, 10)); } catch (e) {} return; }
    if (act === 'say' && arg) { try { var u = new SpeechSynthesisUtterance(arg); u.lang = /[぀-ヿ]/.test(arg) ? 'ja-JP' : (/[一-鿿]/.test(arg) ? 'zh-CN' : 'en-US'); speechSynthesis.speak(u); } catch (e) {} return; }
    if (act === 'check') { _upRunHint(rec, '本机书的 AI 批改还没接通(即将上线);你的作答已保存在本机。'); return; }
    _upRunHint(rec, '这个按钮的动作(' + ev + ')在本机纸上暂不支持。');
  }
  // ═══ 本机书本地造纸(_lp*)结束 ═══════════════════════════════════════════════

  function _upSpawnTaskPage(after, title, onReady, afterEl) {
    if (after < 0) after = 0;
    var tempId = 'tmp_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
    var rec = { id: tempId, page: 0, title: title || '任务', md: '', mode: 'overlay',
                md_ver: 0, synced_ver: 0, _temp: true };
    var tmp = document.createElement('div');
    tmp.className = 'rc-upage pdf-upage up2-new'; tmp.dataset.uid = tempId;
    var placed = false;
    if (afterEl && afterEl.parentNode && afterEl.isConnected) {
      try {
        if (afterEl.style.width) tmp.style.width = afterEl.style.width;                 // 跟前一张同宽
        if (afterEl.style.aspectRatio) { tmp.style.aspectRatio = afterEl.style.aspectRatio; tmp.style.minHeight = '0'; }
        afterEl.parentNode.insertBefore(tmp, afterEl.nextSibling);                      // 紧邻前一张之后
        placed = true;
      } catch (e) {}
    }
    if (!placed && !_upPlace(tmp, after, null)) { alert('请先翻到要插纸的位置附近'); return; }
    try { tmp.scrollIntoView({ block: 'center', behavior: 'auto' }); } catch (e) {}
    _upTempEls[tempId] = tmp;
    _upMountOverlay(rec, tmp);   // 不进编辑态,先显示"生成中"
    var mini = _upMini('正在生成' + (title || '任务纸') + '…');
    RC.reqJson('POST', UP_API, { file: UP_FILE, after: after, title: title || '', md: '' })
      .then(function (d) {
        if (!(d && d.ok && d.job_id)) { _upMiniEnd(mini, '新增失败', true); _upTempFail(tempId); return; }
        _upWatchCreate(d.job_id, tempId, rec, mini);
        var iv = setInterval(function () {   // 绑到真 id+页号后才回调(运行时要真页号才能按 bbox 裁图)
          if (String(rec.id).indexOf('tmp_') === 0 || !rec.page) return;
          clearInterval(iv);
          try { onReady(rec, tmp); } catch (e) {}
        }, 400);
        setTimeout(function () { clearInterval(iv); }, 120000);
      })
      .catch(function () { _upMiniEnd(mini, '网络错误', true); _upTempFail(tempId); });
  }
  // 服务端已把 blocks 写进 sidecar → 重新拉回渲染这一页
  function _upRenderTaskPage(rec, tmp) {
    RC.reqJson('GET', UP_TEXT_API + '?file=' + encodeURIComponent(UP_FILE)).then(function (g) {
      var fresh = ((g && (g.pages || g.items)) || []).filter(function (x) { return x.id === rec.id; })[0];
      if (fresh) { for (var k in fresh) rec[k] = fresh[k]; }
      var ov = tmp.querySelector('.up2-content') || (document.querySelector('[data-page-num="' + rec.page + '"] .up2-content'));
      if (ov) _upRenderOverlay(ov, rec);
    }).catch(function () {});
  }
  // 多纸自动补页(#33):建第 index 张溢出页 → attach 写块 → 渲染 → 递归下一张。
  //   afterEl=前一张的 DOM 元素:紧邻它插入(#1 修复:不按会碰撞陈旧同号页的页号)。
  function _upSpawnOverflow(rid, afterPage, afterEl, n, index) {
    if (index >= n) return;
    _upSpawnTaskPage(afterPage, '第 ' + (index + 1) + ' 页', function (rec, tmp) {
      RC.reqJson('POST', '/pdf/api/run-attach', { rid: rid, upage: rec.id, page: rec.page, index: index })
        .then(function (a) {
          rec.run_id = rid;
          _upRenderTaskPage(rec, tmp);
          _upSpawnOverflow(rid, rec.page, tmp, n, index + 1);   // 下一张紧邻这张
        }).catch(function () {});
    }, afterEl);
  }

  window.__upStartTask = function (spec) {
    try {
      spec = spec || {};
      if (_upEditing || document.body.classList.contains('up-editing')) { alert('先完成当前正在编辑的页'); return; }
      if (UP_LOCAL) { _lpStartTask(spec); return; }   // 本机书:数据已在手,布局/存储/渲染全本地走完
      var after = 0;
      try { after = _upCurPage() | 0; } catch (e) {}
      _upSpawnTaskPage(after, spec.title || '任务纸', function (rec, tmp) {
        RC.reqJson('POST', '/pdf/api/run-start',
          { file: UP_FILE, kind: spec.kind || 'dictation', upage: rec.id, page: rec.page,
            params: spec.params || {} })
          .then(function (r) {
            if (!(r && r.ok)) { _upRunHint(rec, '起任务失败:' + ((r && r.error) || '?')); return; }
            _upRenderTaskPage(rec, tmp);
            if ((r.n_pages || 1) > 1) _upSpawnOverflow(r.rid, rec.page, tmp, r.n_pages, 1);   // 溢出 → 自动补页(紧邻第 1 张)
          }).catch(function () {});
      });
    } catch (e) { try { console.warn('[task] __upStartTask 失败', e); } catch (e2) {} }
  };

  // #50 卡片贴到自建页:格子 → 页归一化 bbox(与 paper._rect 同一算术,前后端一致)。
  function _upGridRect(sp, r, c, h, w) {
    var pw = sp.page_w || 595, ph = sp.page_h || 842, mx = sp.mx || 0, my = sp.my || 0,
        cw = sp.char_w || (pw / (sp.cols || 28)), lh = sp.line_h || (ph / (sp.rows || 26));
    var f = function (v) { return Math.max(0, Math.min(1, Math.round(v * 1e4) / 1e4)); };
    return [f((mx + c * cw) / pw), f((my + r * lh) / ph), f((mx + (c + w) * cw) / pw), f((my + (r + h) * lh) / ph)];
  }
  // 把一张卡(payload={label, raw|text, isHtml})贴到自建页 pageEl 的落点,吸附到最近行列交叉点,持久化。
  window.__upPasteCard = function (pageEl, clientX, clientY, payload) {
    try {
      var rec = pageEl && pageEl.__upRec; if (!rec || !UP_FILE) return;
      var sp = rec.paper || {};
      var rows = sp.rows || 26, cols = sp.cols || 28;
      var host = pageEl.querySelector('.up2-content') || pageEl;
      var rc = host.getBoundingClientRect();
      var pw = sp.page_w || rc.width, ph = sp.page_h || rc.height, mx = sp.mx || 0, my = sp.my || 0,
          cw = sp.char_w || (pw / cols), lh = sp.line_h || (ph / rows);
      var nx = Math.max(0, Math.min(1, (clientX - rc.left) / Math.max(1, rc.width)));
      var ny = Math.max(0, Math.min(1, (clientY - rc.top) / Math.max(1, rc.height)));
      var w = Math.max(4, Math.round(cols * 0.9)), h = 8;                 // 卡默认 8 行 × 90% 页宽
      var col = Math.max(0, Math.min(Math.round((nx * pw - mx) / cw), cols - w));   // 吸附最近列
      var row = Math.max(0, Math.min(Math.round((ny * ph - my) / lh), rows - h));   // 吸附最近行
      var html = payload.isHtml ? (payload.raw || '') : RC.esc(payload.text || payload.raw || '');
      var blk = { kind: 'card', id: 'card_' + Date.now().toString(36), html: html, label: payload.label || '',
                  at: [row, col], span: [h, w], rect: _upGridRect(sp, row, col, h, w) };
      var blocks = (rec.blocks || []).slice(); blocks.push(blk); rec.blocks = blocks;   // 独立副本(存快照)
      RC.reqJson('PATCH', UP_TEXT_API, { file: UP_FILE, id: rec.id, blocks: blocks }).then(function () {
        var ov = pageEl.querySelector('.up2-content'); if (ov) _upRenderOverlay(ov, rec);
      }).catch(function () {});
    } catch (e) { try { console.warn('[paste] __upPasteCard 失败', e); } catch (e2) {} }
  };

  // ★ 按 block id 把卡片**绑定**到自建页的某个格子块（卡片协议的 bind 字段，用户设计 2026-08-18）。
  //
  //   跟 __upPasteCard 的区别：那个按屏幕坐标吸附到最近的格子，这个按目标块的位置插在它**之后**。
  //   之所以能同时满足用户那两条要求（"保证自己的位置"和"保证自身数据嵌入上下文的位置"），
  //   是因为自建页的 blocks 本来就是**有序数组**：插进目标块后面，屏幕位置和内容序列位置
  //   是同一件事，不需要额外发明一套锚。
  //
  //   找不到目标块时返回 false —— 调用方据此退回浮层，而不是把卡片丢掉。
  window.__upBindCard = function (upageId, bid, payload) {
    try {
      if (!UP_FILE || !upageId || !bid) return false;
      var pageEl = null, rec = null;
      var pages = document.querySelectorAll('.pdf-upage');
      for (var i = 0; i < pages.length; i++) {
        var r = pages[i].__upRec;
        if (r && String(r.id) === String(upageId)) { pageEl = pages[i]; rec = r; break; }
      }
      if (!rec) return false;                       // 那一页没在视图里（没渲染/别的书）
      var blocks = (rec.blocks || []).slice();
      var idx = -1;
      for (var j = 0; j < blocks.length; j++) {
        if (blocks[j] && String(blocks[j].id) === String(bid)) { idx = j; break; }
      }
      if (idx < 0) return false;                    // 目标块不存在（可能已被删）
      var sp = rec.paper || {};
      var rows = sp.rows || 26, cols = sp.cols || 28;
      var tgt = blocks[idx];
      var at = tgt.at || [0, 0], span = tgt.span || [1, cols];
      var w = Math.max(4, Math.round(cols * 0.9));
      var h = 8;
      var row = Math.max(0, Math.min((at[0] | 0) + (span[0] | 0), Math.max(0, rows - h)));
      var col = Math.max(0, Math.min(at[1] | 0, Math.max(0, cols - w)));
      var html = payload.isHtml ? (payload.raw || '') : RC.esc(payload.text || payload.raw || '');
      var blk = { kind: 'card', id: 'card_' + Date.now().toString(36),
                  html: html, label: payload.label || '', boundTo: String(bid),
                  at: [row, col], span: [h, w], rect: _upGridRect(sp, row, col, h, w) };
      blocks.splice(idx + 1, 0, blk);               // 插在目标块**之后** = 内容序列上的位置
      rec.blocks = blocks;
      // @interaction document.upage.bind-card
      RC.reqJson('PATCH', UP_TEXT_API, { file: UP_FILE, id: rec.id, blocks: blocks }).then(function () {
        var ov = pageEl.querySelector('.up2-content'); if (ov) _upRenderOverlay(ov, rec);
      }).catch(function () {});
      return true;
    } catch (e) {
      try { console.warn('[bind] __upBindCard 失败', e); } catch (e2) {}
      return false;
    }
  };

  // ══════════ 任务运行时:页面块渲染(text / blank / button)══════════
  //   设计见 references/adr-task-runtime.md。三个坑一次绕开:
  //   ① 覆盖层拦手势用**冒泡非捕获**(memory overlay-gate-use-bubble-not-capture:
  //      捕获阶段 stopPropagation 会吞掉内部按钮事件 —— 插入页保存键失灵就是这么来的)
  //   ② iOS 的 AudioContext **必须在点击的同步栈里** warm(__vcTtsWarm),否则听写第一个词无声
  //   ③ SSE 在页面不可见时会被**直接丢弃** → 回前台必须拉 run-status **对齐状态机**,不能只靠推送
  // 用户点子:检查时**截前端渲染好的整页**(题目+手写所见即所得)发后端,比服务端拼图准。
  function _upH2C() {
    if (window.html2canvas) return Promise.resolve(window.html2canvas);
    return new Promise(function (res, rej) {
      var s = document.createElement('script'); s.src = '/static/pdf/html2canvas.min.js';
      s.onload = function () { res(window.html2canvas); }; s.onerror = rej;
      document.head.appendChild(s);
    });
  }
  function _upShotEl(el) {
    // 统一走共享原语 RC.captureEl(通用截图);未就绪才本地兜底(html2canvas 直调)。
    if (window.RC && RC.captureEl) return RC.captureEl(el).then(function (r) { return (r && r.b64) || ''; });
    return _upH2C().then(function (h2c) {
      return h2c(el, { useCORS: true, logging: false, backgroundColor: '#ffffff',
                       scale: Math.min(2, window.devicePixelRatio || 1) });
    }).then(function (canvas) {
      var b64 = '', qs = [0.85, 0.7, 0.5];
      for (var q = 0; q < qs.length; q++) { b64 = (canvas.toDataURL('image/jpeg', qs[q]).split(',')[1]) || ''; if (b64.length <= 900000) break; }
      return b64.length > 3000 ? b64 : '';
    });
  }
  function _upCaptureRunShots(rec) {   // 截本 run 的每一页(按页号排序=图序=页序)
    var els = Array.prototype.slice.call(document.querySelectorAll('.pdf-upage'))
      .filter(function (el) { return el.__upRec && el.__upRec.run_id === rec.run_id; });
    if (!els.length && rec && _upTempEls[rec.id]) els = [_upTempEls[rec.id]];
    els.sort(function (a, b) { return ((a.__upRec && a.__upRec.page) || 0) - ((b.__upRec && b.__upRec.page) || 0); });
    var shots = [];
    return els.reduce(function (chain, el) {
      return chain.then(function () {
        return _upShotEl(el).then(function (b64) {
          if (b64) shots.push({ page: (el.__upRec || {}).page, media_type: 'image/jpeg', b64: b64 });
        }).catch(function () {});
      });
    }, Promise.resolve()).then(function () { return shots; });
  }
  function _upRunResp(rec, d) {   // 统一处理 run-event 响应:后端复活了 run(旧 rid 过期)→ 换绑新 rid
    if (!d) return;
    if (d.rid && d.rid !== rec.run_id) {
      var old = rec.run_id; rec.run_id = d.rid;
      try { var el = document.querySelector('[data-up-run="' + old + '"]'); if (el) el.setAttribute('data-up-run', d.rid); } catch (e) {}
    }
    if (d.hint) _upRunHint(rec, d.hint);
  }
  function _upRunEvent(rec, ev) {
    if (!rec.run_id) return;
    if (String(rec.run_id).indexOf('lp_') === 0) { _lpRunEvent(rec, ev); return; }   // 本机纸:本地语义,不打服务端
    try { if (window.__vcTtsWarm) window.__vcTtsWarm(); } catch (e) {}   // ② 必须在点击同步栈里
    if (ev === 'check' || ev.indexOf('check:') === 0) {   // 检查/复判:先截整页(所见即所得)再连截图一起发。★截图**加 5s 超时**:卡住也照发纯事件
      _upRunHint(rec, '正在截图检查…');   //   (后端回退服务端拼图)→ 绝不因截图挂住而"点检查没反应"(用户实测根因之一)。
      var _fired = false;
      function _fire(shots) {
        if (_fired) return; _fired = true;
        var body = { rid: rec.run_id, event: ev, file: UP_FILE, upage: rec.id };   // file/upage:run 过期时后端按纸复活
        if (shots && shots.length) body.shots = shots;
        var picks = {};   // 1b:点选的选择题答案 {block_id: 字母}(本纸;多纸未点选的走 AI)
        (rec.blocks || []).forEach(function (b) { if (b.kind === 'choice' && b.picked && b.id) picks[b.id] = b.picked; });
        if (Object.keys(picks).length) body.picks = picks;
        RC.reqJson('POST', '/pdf/api/run-event', body)
          .then(function (d) { _upRunResp(rec, d); }).catch(function () {});
      }
      setTimeout(function () { _fire(null); }, 5000);
      _upCaptureRunShots(rec).then(function (shots) { _fire(shots); }).catch(function () { _fire(null); });
      return;
    }
    RC.reqJson('POST', '/pdf/api/run-event', { rid: rec.run_id, event: ev, file: UP_FILE, upage: rec.id })   // ⚠ 签名是 (method,url,body)
      .then(function (d) { _upRunResp(rec, d); })
      .catch(function () {});
  }
  function _upRunHint(rec, txt) {
    try {
      var el = document.querySelector('[data-up-run="' + rec.run_id + '"] .up2-run-hint');
      if (el) el.textContent = txt || '';
    } catch (e) {}
  }
  // ★ 字号 = 一格的**宽**,行高 = 一格的**高** —— 网格模型的两个方向都要锁住。
  //
  //   曾经只锁了字号、而且锚错了方向:字号 = 格高 × font_ratio(一个拍出来的常数 0.5)。
  //   两个后果:
  //   ① 块内换行走 CSS 默认行高(≈1.2em ≈ 0.6 格),文字行不落在格子行上;
  //   ② CJK 全角字宽 = font-size,字号 0.5 格高 ≈ 0.8 格宽 → 一行塞得下 41 个字,
  //      而后端按 cols=34 估行数。前后端对不上,长内容就溢出被裁、看着互相压。
  //   要让"一行 = cols 个字"成立,font_ratio 必须等于 char_w/line_h —— 那是随页面
  //   宽高比变的量,根本不该是常数。所以字号直接取格宽,font_ratio 不再参与。
  //
  //   锁住之后:一行文字 = 一格,一个全角字 = 一格,后端 ceil(字数/列数) 算出的行数
  //   == 前端真实占用的行数,服务端算的 bbox 才真的等于屏幕上的位置(批改裁图靠它)。
  function _upFitText(body) {
    var bh = body.offsetHeight || 0, bw = body.offsetWidth || 0;
    if (!bh || !bw) return;
    body.querySelectorAll('.up2-b').forEach(function (el) {
      if (el.classList.contains('up2-b-card')) { el.style.overflow = 'auto'; return; }   // #50 卡片自带字号/样式,不锁字号
      if (!el.__cwr) return;
      // ⚠ 规格残缺(paper 字段被写成字符串而不是 spec 对象)时**不能**回退到块自身高度 ——
      //   那正是字号爆炸的老路(3 格高的块字号立刻 3 倍)。用保守均分兜底,并且出声。
      var rowPx = (el.__lhr && bh) ? bh * el.__lhr : bh / 26;
      var colPx = bw * el.__cwr;
      if (!el.__lhr && !body.__warnedLhr) {
        body.__warnedLhr = 1;
        try { console.warn('[paper] 缺 line_h/page_h,行高按 26 行兜底;检查 upage.paper 是否为完整 spec 对象'); } catch (e) {}
      }
      // 不设上限、不取整:页面放多大字就该多大(旧代码 min(64) 会在大页面上把比例钳死),
      // 整数舍入在小格子上会累积成可见偏移,浏览器本来就支持小数 px。
      el.style.fontSize = Math.max(1, colPx * (el.classList.contains('up2-h1') ? 1.25 : 1)) + 'px';
      el.style.lineHeight = rowPx + 'px';
      el.style.overflow = 'hidden';
    });
  }
  // 绑定卡的形态：展开 <-> 球。语义与浮层卡的三态一致，区别是这里的卡是自建页的
  //   一个 block（它同时定住了屏幕位置和内容序列位置），所以"收起"不是关闭，
  //   只是把同一个格子换一种画法。形态存进 block.form，跨会话保留。
  // 与浮层卡**同一个**设置口径（rc-voicecall.js:4079-4080）。以前这里写死 20000，
  //   于是用户把自动收起关掉、或改成 60s，绑定卡完全不理 —— 提交注释说"与浮层卡默认
  //   一致"，可只有默认值一致。返回 null = 用户关掉了自动收起，永不排表。
  var UP_BIND_IDLE_MS = 20000;   // 兜底默认（localStorage 读不到时用）
  function _upBindIdleMs() {
    try {
      if (localStorage.getItem('rc-voice-card-hide') === '0') return null;
      var v = parseInt(localStorage.getItem('rc-voice-card-secs') || '20', 10) || 20;
      return Math.max(5, Math.min(60, v)) * 1000;
    } catch (e) { return UP_BIND_IDLE_MS; }
  }

  // 收起状态的落盘按 rec 合并成一次 PATCH。
  //   同页多张绑定卡是同时渲染的，也就会**同时**到点；每张各发一个整数组 PATCH，
  //   后写会覆盖先写，并发的 form 变更互相吞。合并窗口内只发最后一次。
  var _upBindSaveTimers = new WeakMap();
  function _upBindSaveSoon(rec) {
    try {
      clearTimeout(_upBindSaveTimers.get(rec));
      _upBindSaveTimers.set(rec, setTimeout(function () {
        // @interaction document.upage.bind-card
        RC.reqJson('PATCH', UP_TEXT_API, { file: UP_FILE, id: rec.id, blocks: rec.blocks })
          .catch(function () {});
      }, 300));
    } catch (e) {}
  }

  // 绑定卡的形态：展开 <-> 球。语义与浮层卡的三态一致，区别是这里的卡是自建页的
  //   一个 block（它同时定住了屏幕位置和内容序列位置），所以"收起"不是关闭，
  //   只是把同一个格子换一种画法。形态存进 block.form，跨会话保留。
  //
  // ⚠ 计时**只在这张卡真的被看见时**才走。自建页的元素一旦挂进滚动容器就常驻
  //   （不虚拟化），所以裸 setTimeout 会让"绑在第 12 页的卡"在你读第 3 页时自己
  //   收成球，全程没被看见过 —— 浮层那层早就有这个修复（_cardsVisSync:「卡片被藏
  //   起来的这段时间不该计时」），块这层一直没有对等物。
  function _upBindCardForm(el, blk, rec) {
    // 同一块的旧实例先拆干净：_upRenderBlocks 每次都重建 innerHTML 并重跑这里，
    //   旧闭包的 timer 从来没人 clear，它仍会在已脱离的元素上触发，并改**同一个**
    //   blk 对象写 form:'dot' —— 表现为"刚展开的卡被自己收起来了"。
    try { if (el.__bwBindTeardown) el.__bwBindTeardown(); } catch (e) {}

    var collapsed = (blk.form === 'dot');
    var timer = null;
    var io = null;
    var visible = false;

    function paint() {
      el.classList.toggle('up2-b-card-dot', collapsed);
      el.title = collapsed ? '展开这张卡' : '';
    }
    function save() {
      try {
        blk.form = collapsed ? 'dot' : 'full';
        _upBindSaveSoon(rec);
      } catch (e) {}
    }
    function stop() { try { clearTimeout(timer); } catch (e) {} timer = null; }
    function arm() {
      stop();
      if (collapsed) return;
      if (!visible) return;                       // 看不见就不该计时
      if (document.visibilityState === 'hidden') return;   // 切后台同理
      var ms = _upBindIdleMs();
      if (ms === null) return;                    // 用户关掉了自动收起
      timer = setTimeout(function () { collapsed = true; paint(); save(); }, ms);
    }
    function onVis() { if (document.visibilityState === 'visible') arm(); else stop(); }

    el.addEventListener('click', function (ev) {
      if (!collapsed) { arm(); return; }   // 展开态点内容不收起，只重排计时
      ev.stopPropagation();
      collapsed = false; paint(); save(); arm();
    });

    try {
      if (window.IntersectionObserver) {
        io = new IntersectionObserver(function (entries) {
          for (var i = 0; i < entries.length; i++) {
            visible = entries[i].isIntersecting;
            if (visible) arm(); else stop();
          }
        }, { threshold: 0.15 });
        io.observe(el);
      } else {
        visible = true;   // 没有 IO 就退回旧行为，总比不计时好
        arm();
      }
    } catch (e) { visible = true; arm(); }
    document.addEventListener('visibilitychange', onVis);

    el.__bwBindTeardown = function () {
      stop();
      try { if (io) io.disconnect(); } catch (e) {}
      document.removeEventListener('visibilitychange', onVis);
      el.__bwBindTeardown = null;
    };
    paint();
  }
  // ★ 严格按**格子绝对定位**渲染 —— 服务端用 (row,col,span) 纯算术算出的 rect,
  //   只有前端也按同一套格子摆,那个 rect 才等于屏幕上的真实位置(批改按它裁图)。
  //   ⚠ 绝不能用自由流式 CSS:那样服务端算的 bbox 就对不上了。
  function _upRenderBlocks(ov, rec) {
    var sp = rec.paper || {};
    ov.setAttribute('data-up-run', rec.run_id || '');
    // 这里是唯一销毁块 DOM 的地方 —— 也就是唯一正确的拆卸点。不拆的话旧闭包的
    //   timer/observer 会活到下一轮，在已脱离的元素上继续改 blk。
    try {
      ov.querySelectorAll('.up2-b-card').forEach(function (old) {
        if (old.__bwBindTeardown) old.__bwBindTeardown();
      });
    } catch (e) {}
    ov.innerHTML = '<div class="up2-blocks"></div><div class="up2-run-hint"></div>';
    var body = ov.querySelector('.up2-blocks');
    if (sp.bg) ov.style.background = sp.bg;
    body.classList.toggle('up2-rule-line', sp.rule === 'line');
    body.classList.toggle('up2-rule-grid', sp.rule === 'grid');
    if (sp.line_h) body.style.setProperty('--lh', sp.line_h + 'px');
    if (sp.char_w) body.style.setProperty('--cw', sp.char_w + 'px');

    (rec.blocks || []).forEach(function (b) {
      if (!b.rect || b.hidden) return;   // hidden:reveal/hide 内置动作控制显隐
      var d = document.createElement('div');
      d.className = 'up2-b up2-b-' + (b.kind || 'text');
      d.setAttribute('data-bid', b.id || '');
      // 位置/尺寸**直接用服务端算好的归一化 rect**(单一真相源;前端不再自己量、不再写回)
      d.style.cssText = 'position:absolute;left:' + (b.rect[0] * 100) + '%;top:' + (b.rect[1] * 100) + '%;' +
                        'width:' + ((b.rect[2] - b.rect[0]) * 100) + '%;' +
                        'height:' + ((b.rect[3] - b.rect[1]) * 100) + '%;';
      // 字号 = 这一格**渲染后的真实高度** × font_ratio。不能写死 px:PDF 页尺寸千差万别
      //   (A4 595pt vs 超大扫描件 2230pt),写死会让字比蚂蚁小或撑破格子(实测那次就是这么翻的)。
      // 字号锁定**单行格高**(不是块的 offsetHeight —— 内容多的块会被撑高,× ratio 就字号爆炸,
      //   用户实测检查结果撑破整页的根因)。行高比例 = line_h / page_h,乘页元素像素高。
      d.__lhr = (sp.line_h && sp.page_h) ? (sp.line_h / sp.page_h) : 0;   // 一格占页高的比例 → 行高
      d.__cwr = (sp.char_w && sp.page_w) ? (sp.char_w / sp.page_w) : 0;   // 一格占页宽的比例 → 字号
      if (b.kind === 'text') {
        d.textContent = b.text || b.label || '';   // 容错:AI 把题目误放进 label 也能显示(后端 _norm_block 已纠,这里兜旧数据)
        if (b.style === 'h1') d.classList.add('up2-h1');
      } else if (b.kind === 'blank') {
        d.innerHTML = '<span class="up2-b-lab">' + RC.esc(b.label || '') + '</span><span class="up2-b-box"></span>';
      } else if (b.kind === 'choice') {
        var cq = document.createElement('div'); cq.className = 'up2-c-q'; cq.textContent = b.text || b.label || '';
        var co = document.createElement('div'); co.className = 'up2-c-opts';
        var ca = document.createElement('div'); ca.className = 'up2-c-ans';
        ca.innerHTML = '<span class="up2-b-lab">答:</span><span class="up2-b-box" style="flex:0 1 10em"></span>';
        var _ansBox = ca.querySelector('.up2-b-box');
        // 1b:选项可点选 → 客观判分依据(不点而手写则回退 AI 识别)
        (b.options || []).forEach(function (o, oi) {
          var letter = String.fromCharCode(65 + oi);
          var it = document.createElement('span'); it.textContent = letter + '. ' + o;
          it.setAttribute('role', 'button'); it.setAttribute('tabindex', '0');
          if ((b.picked || '') === letter) it.classList.add('up2-c-sel');
          it.addEventListener('click', function (ev) {
            ev.stopPropagation();                                  // memory overlay-gate-use-bubble-not-capture
            b.picked = letter;                                     // 存进块对象;check 时 _fire 收集进载荷
            co.querySelectorAll('span').forEach(function (sp) { sp.classList.remove('up2-c-sel'); });
            it.classList.add('up2-c-sel');
            if (_ansBox) _ansBox.textContent = letter;
          });
          co.appendChild(it);
        });
        if (b.picked && _ansBox) _ansBox.textContent = b.picked;
        d.appendChild(cq); d.appendChild(co); d.appendChild(ca);
      } else if (b.kind === 'checkbox') {
        d.innerHTML = '<span class="up2-b-ck"></span><span class="up2-b-lab2">' + RC.esc(b.label || '') + '</span>';
      } else if (b.kind === 'button') {
        // <span role=button>(不是 <button>):memory ios-button-white-block —— Safari 会用原生外观盖掉一切
        var sb = document.createElement('span');
        sb.className = 'up2-b-btn'; sb.setAttribute('role', 'button'); sb.setAttribute('tabindex', '0');
        sb.textContent = b.label || '按钮';
        // #36 显示状态:enabled:false → 禁用/不可点(灰);状态机 set_enabled 事件可动态打开。
        var _on = (b.enabled !== false);
        if (!_on) { sb.classList.add('up2-b-off'); sb.setAttribute('aria-disabled', 'true'); }
        // ⚠ 冒泡阶段 stopPropagation(memory overlay-gate-use-bubble-not-capture:
        //   捕获阶段拦会**吞掉内部按钮事件** —— 插入页保存键失灵就是这么来的)
        sb.addEventListener('click', function (ev) {
          ev.stopPropagation();
          if (sb.classList.contains('up2-b-off')) return;   // 禁用态不响应
          _upRunEvent(rec, b.event || 'next');
        });
        d.appendChild(sb);
      } else if (b.kind === 'card') {
        // #50 贴到页面的卡片:独立副本(存的是快照 html),按格子定位,内部可滚。
        d.classList.add('up2-b-card');
        try { d.innerHTML = b.html || RC.esc(b.label || '卡片'); } catch (e) { d.textContent = b.label || '卡片'; }
        // 绑定卡的形态与计时(用户设计)：刚落下时保持打开并走非活跃计时，
        // 到点**收起成球留在原位**而不是消失 —— 卡片的位置本身就是信息，
        // 关掉等于把"这一处有过一次纠正"也一起丢了。球仍占同一个格子，点它展开回来。
        if (b.boundTo) _upBindCardForm(d, b, rec);
      } else { return; }
      body.appendChild(d);
    });
    // 定位完成后按各块真实高度算字号(offsetHeight 此刻可读)
    requestAnimationFrame(function () {
      // 检查结果(持久化在 sidecar 的 result_md)→ 渲进卡内结果区(不塞格子)
      if (rec.result_md) _upRenderResult(ov, rec.result_md, _upResKey(ov, rec.id), rec.check_name, rec.check_score);
      _upFitText(body);
      // ★ 跟随页面缩放实时重算。只在渲染时算一次的话,PDF 一缩放 px 就不再对应格子,
      //   字相对纸整个飘掉(用户实测:放大后字号明显不跟随)。容器是 absolute inset:0,
      //   尺寸只由页面决定、不受内部字号影响 → 回调里改字号不会自激。
      try {
        if (window.ResizeObserver) {
          var ro = new ResizeObserver(function () {
            _upFitText(body);
            // 手写层同理:_upResizeInk 给 canvas 设的是**固定 px**,只在渲染时校准一次 ——
            // 页面一缩放,元素尺寸变了而 canvas 还停在旧尺寸,墨迹就不跟着纸走。
            // 笔画本身是归一化坐标,所以尺寸校准 + 重绘之后位置自然对上。
            try {
              var h = ov.closest ? ov.closest('.pdf-upage') : null;
              if (h && h.__inkCanvas) _upResizeInk(h);
            } catch (e2) {}
          });
          ro.observe(body);
          body.__ro = ro;
        }
      } catch (e) {}
    });
    var host = ov.closest ? ov.closest('.pdf-upage') : null; if (host && host.__inkCanvas) _upResizeInk(host);
  }
  // 141:_upSyncRects(前端量 bbox 再 PATCH 写回)已**删除**。
  //   有了格子模型之后,bbox 由服务端从 (row,col,span) **纯算术**算出(paper.py),
  //   前端只要**严格按同一套格子绝对定位**渲染就必然对齐 —— 量和写回这一环又丑又不可靠,没了。
  // ③ 回前台对齐状态机(SSE 不可见时丢事件 → 不能只靠推送)
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState !== 'visible') return;
    document.querySelectorAll('[data-up-run]').forEach(function (ov) {
      var rid = ov.getAttribute('data-up-run');
      if (!rid) return;
      RC.reqJson('GET', '/pdf/api/run-status?rid=' + encodeURIComponent(rid))
        .then(function (d) { if (d && d.ok) { var h = ov.querySelector('.up2-run-hint'); if (h) h.textContent = d.hint || ''; } })
        .catch(function () {});
    });
  });

  // 左上角小 Aa 编辑按钮(唯一进编辑入口),显示态覆盖层穿透。无论覆盖层挂不挂(脏/已同步)都放,是「再次编辑」入口。
  function _upEnsureEditBtn(rec, pw) {
    if (pw.querySelector('.up2-edit-btn')) return;
    var eb = document.createElement('div'); eb.className = 'up2-edit-btn';
    eb.textContent = 'Aa'; eb.title = '编辑这一页的文字';   // Aa=文字编辑,区分手写 ✏️
    eb.addEventListener('click', function (e) { e.stopPropagation(); _upOverlayEnterEdit(rec, pw); });
    pw.appendChild(eb);
  }
  // 右上角 ⭐ 收藏钮:把「自己创建的插入页」收进收藏夹(复用 rc-favorites 弹窗;kind=userpage,id=该插入页记录 id)。
  //   临时页(乐观新建未绑真 id)不给(绑定后重挂时会补);收藏夹物化书自身页面禁自我收藏(前缀判断,双保险)。
  function _upEnsureFavBtn(rec, pw) {
    if (!(window.RC && RC.favorites) || pw.querySelector('.up2-fav-btn')) return;
    if (!rec || _upIsTempId(rec.id)) return;
    if (UP_FILE.indexOf('资源/收藏夹/') === 0) return;
    var fb = document.createElement('div'); fb.className = 'up2-fav-btn';
    fb.textContent = '⭐'; fb.title = '收藏这一页到收藏夹';
    fb.addEventListener('click', function (e) { e.stopPropagation(); RC.favorites.openPicker({ file: UP_FILE, kind: 'userpage', id: rec.id }); });
    pw.appendChild(fb);
  }
  // (方案调整 2026-07-04:删掉 _upIsSyncedAtLoad —— overlay 页不再有「已同步→当普通页」退路,始终挂覆盖层。见 _upMountBadges。)
  function _upMountOverlay(rec, pw) {
    pw.classList.add('pdf-upage-overlay');   // overlay 页标记 → CSS 关该页 char-layer 指针(隔离,始终生效)
    if (pw.classList.contains('pdf-upage')) {   // 虚拟 .pdf-upage 元素(乐观新建/绑定后本会话显示):补齐真 .page-wrap 才有的手写层
      pw.__upRec = rec;                          //   → _ovRecOf/_ov*(查词/精确高亮/点空白关框)按 __upRec 认它;ink 存边路径按 __upRec 分流(_inkScheduleSave 守卫)
      _upEnsureInk(pw);                          //   建 __inkCanvas + 绑 _inkPointerDown = 手写(画图)天然生效,同普通 overlay 页
    }
    var ov = pw.querySelector('.up2-content');
    if (!ov) {
      ov = document.createElement('div'); ov.className = 'up2-content';
      pw.style.position = pw.style.position || 'relative';
      pw.appendChild(ov);
      // ⚠ stopPropagation 只在**编辑态**拦手势;显示态必须放行,否则点覆盖层空白冒泡不到 document → 弹框/工具栏关不掉。
      //   一次绑定 + 运行时查 editing class(旧版每次 enterEdit 重复 addEventListener 且退出不移除 → 显示态残留拦截,弹框关不掉)。
      ['pointerdown', 'mousedown', 'touchstart', 'click', 'dblclick'].forEach(function (ev) {
        ov.addEventListener(ev, function (e) { if (ov.classList.contains('editing')) e.stopPropagation(); });
      });
      // 显示态选中覆盖层文字 → 钉进右侧助手焦点卡(__setFocusSel,助手开着才显示);__voiceContext 发消息时靠
      //   getSelection 回退拿覆盖层原生选区(设废 __lastSelMeta 让 char-layer 新鲜度校验失败,强制走回退)。
      //   批次3:同时(a)无选中单击词 → _ovClickWord 弹字典;(b)有选中 → 记 _ovLastSelInfo 供工具栏色板建 offset 高亮。
      function _selDone(e) {
        if (ov.classList.contains('editing')) return;
        var pt = _ovPointFromEvent(e);
        setTimeout(function () {
          var t = (window.getSelection ? getSelection().toString() : '').trim();
          if (!t) {   // 点覆盖层空白/单击词无选中 → 关工具栏/词组框 + 试单击查词
            //   (覆盖层非 char-layer,document 的 _shouldCloseToolbar 在 mousedown 时读到未清的旧选区 → 判定"还选着"不关)
            _ovLastSelInfo = null;
            try {
              var tb = document.getElementById('sel-toolbar'); if (tb) tb.classList.remove('open');
              document.querySelectorAll('.sel-overlay').forEach(function (o) { o.innerHTML = ''; });
              var pp = document.getElementById('phrase-pop'); if (pp) pp.style.display = 'none';
            } catch (_) {}
            if (pt) {
              var tgt = document.elementFromPoint(pt.x, pt.y);
              if (tgt && tgt.closest && tgt.closest('mark.rc-html-hl')) return;   // 点高亮 → 交给 mark click 开编辑,不查词
              _ovClickWord(pt.x, pt.y, _ovRecOf(pw), pt.pointerType);             // 单击词 → 弹字典;非词/空白 → 内部关框
            } else { _ovCloseWordPop(); }
            return;
          }
          try { window.__lastSelMeta = { page: -999, t: 0 }; } catch (_) {}
          try { window.__setFocusSel && window.__setFocusSel(t, 'text'); } catch (_) {}
          _ovLastSelInfo = _ovActiveSelInfo();   // 批次3:选区时捕获快照(点色板时原生选区可能已折叠,不能现读)
        }, 10);
      }
      ov.addEventListener('mouseup', _selDone);
      ov.addEventListener('touchend', _selDone);
      // 批次3:点高亮 mark → RC.highlight 编辑浮层(改色/备注/删)。绑在 ov(常驻),body 每次重渲无所谓
      ov.addEventListener('click', function (e) {
        if (ov.classList.contains('editing')) return;
        var mk = e.target && e.target.closest ? e.target.closest('mark.rc-html-hl') : null;
        if (!mk) return;
        e.stopPropagation();
        var rec2 = _ovRecOf(ov.closest ? ov.closest('.page-wrap, .pdf-upage') : null); if (!rec2) return;
        var h = _ovHlById(rec2, mk.getAttribute('data-hid')); if (h) _ovOpenHlEditor(rec2, h);
      });
    }
    if (!ov.classList.contains('editing')) _upRenderOverlay(ov, rec);
    _upEnsureEditBtn(rec, pw);   // 显示态覆盖层穿透,不点整页进编辑,手写/滚动/阅读器功能照常
    _upEnsureFavBtn(rec, pw);    // 右上角 ⭐ 收藏这一页
  }
  function _upOverlayEnterEdit(rec, pw) {
    var ov = pw.querySelector('.up2-content');
    if (!ov) { _upMountOverlay(rec, pw); ov = pw.querySelector('.up2-content'); }
    if (!ov || ov.classList.contains('editing')) return;
    ov.classList.add('editing');
    document.body.classList.add('up-editing');   // 禁用页面手势(选词/ink/双击缩放),同 _upInlineEdit
    ov.innerHTML =
      '<div class="up2-content-ebar"><input class="up2-title" maxlength="120" placeholder="标题(可空)">' +
      '<button class="up2-del2" title="删除这一页">🗑</button><button class="up2-done">✓ 完成</button></div>' +
      '<textarea class="up2-ta" placeholder="正文…(markdown:# 标题/列表/**粗体**/$..$ 公式)"></textarea>' +
      '<div class="up2-savehint">自动保存,无需手动</div>';
    var ti = ov.querySelector('.up2-title'), ta = ov.querySelector('.up2-ta');
    ti.value = rec.title || ''; ta.value = rec.md || '';
    // (拦手势的 stopPropagation 已在 _upMountOverlay 一次绑定 + 按 editing class 生效,这里不再重复加,避免退出编辑后残留)
    function onInput() { rec.title = ti.value; rec.md = ta.value; _upTextSchedule(rec.id, ti.value.trim(), ta.value); }
    ti.addEventListener('input', onInput); ta.addEventListener('input', onInput);
    ov.querySelector('.up2-del2').addEventListener('click', function (e) {
      e.stopPropagation();
      document.body.classList.remove('up-editing');
      _upDelReal(rec);   // overlay 删除 = 撤销条 + 真减页 job(同 baked)
    });
    ov.querySelector('.up2-done').addEventListener('click', function (e) {
      e.stopPropagation();
      document.body.classList.remove('up-editing'); _upRenderOverlay(ov, rec);
      if (rec._temp) { rec._syncOnBind = true; return; }   // 乐观新建临时页:内容已缓冲,job done 绑真 id 后自动落库+同步
      // 完成编辑 → 先把边车存完(await),再触发后台同步写回 PDF(静默、不 reload、串行队列)。
      _upTextFlush(rec.id).then(function () { _upEnqueueSync(rec.id); });
    });
    setTimeout(function () { try { ta.focus(); } catch (_) {} }, 80);
  }
  // 删除 = **立即执行**(用户要求去掉撤销)。关键三步防"删了还显示"/"删除失败":
  //   ① 立刻从本地 RC.userpages 列表剔除(否则 _upMountBadges 从陈旧列表重挂已删页 = 删了还显示的根因);
  //   ② 立刻抹掉这页覆盖层 + 盖"正在删除"蒙层(视觉立即消失;真减页/重编号靠 job done 后 reload);
  //   ③ DELETE 改页 job 撞并发锁(409「进行中」)时**排队重试**,不再直接弹"删除失败"。
  // 删除 = 乐观**整页立即消失**(不再盖蒙层等 reload、不再"轻点查看")+ 串行后台真删(连续删不撞锁、
  //   不被中途 reload 打断)+ 全删完自动对齐一次(reading-pos 服务端化 → 无感回位,用户零操作)。
  var _upDelQueue = [], _upDelBusy = false, _upDelWarns = [], _upDelInd = null;
  function _upDelIndicator(txt) {
    if (!txt) { if (_upDelInd) { try { _upDelInd.remove(); } catch (_) {} _upDelInd = null; } return; }
    if (!_upDelInd) { _upDelInd = document.createElement('div'); _upDelInd.id = 'up2-del-ind'; document.body.appendChild(_upDelInd); }
    _upDelInd.className = ''; _upDelInd.innerHTML = '<span class="up2-spin"></span><span class="up2-msg">' + RC.esc(txt) + '</span>';
  }
  function _upDelReal(rec) {
    if (String(rec.id || '').indexOf('lp_') === 0) {
      // 本机纸:只存 localStorage,不存在于任何服务端/原生 sidecar——老路径的三连
      // (临时表/按 page 找 DOM/DELETE API)对它全部落空,实测表现"点删除删不掉"。
      // 纸是一个整体(gid 一组),删任何一页=删整张(留孤儿页比删多了更困惑)。
      var gid = rec.gid || rec.id, all = _lpAll(), n = 0;
      for (var li = all.length - 1; li >= 0; li--) {
        if ((all[li].gid || all[li].id) !== gid) continue;
        var lel = _lpMounted[all[li].id];
        if (lel) { try { lel.remove(); } catch (_) {} }
        delete _lpMounted[all[li].id];
        all.splice(li, 1); n += 1;
      }
      _lpSaveSoon();
      if (window.RC && RC.toast) RC.toast(n > 1 ? ('已删除整张练习纸(' + n + ' 页)') : '本机练习纸已删除');
      return;
    }
    if (rec._temp) {   // 乐观新建 job 还没完成就删 → 移除临时元素;绑真 id 时再清理那张空白真页(_upBindTempToReal)
      _upTempCancelled[rec.id] = true;
      var tel = _upTempEls[rec.id]; if (tel) { try { tel.remove(); } catch (_) {} }
      delete _upTempEls[rec.id];
      try { if (window.RC && RC.userpages && RC.userpages.removeLocal) RC.userpages.removeLocal(rec.id); } catch (_) {}
      if (window.RC && RC.toast) RC.toast('已取消新增');
      return;
    }
    if (_upDeleting[rec.id]) return;   // 已在删,忽略重复
    _upDeleting[rec.id] = true;
    try { (window._upJustDeleted = window._upJustDeleted || {})[rec.id] = Date.now(); } catch (_) {}   // reconcile 自愈:僵尸元素按此名单清
    try { if (window.RC && RC.userpages && RC.userpages.removeLocal) RC.userpages.removeLocal(rec.id); } catch (_) {}   // 本地列表立刻剔除
    var tempEl = _upTempEls[rec.id];
    var pw = tempEl || document.querySelector('.page-wrap[data-page-num="' + rec.page + '"]');
    if (pw) {
      // ★乐观:**整页立即移除**(书就地合拢)。删的页完全在视口上方 → 补偿 scrollTop 防内容跳动。
      try {
        var mainEl = document.getElementById('main');
        var r = pw.getBoundingClientRect();
        if (mainEl && r.bottom <= 0) mainEl.scrollTop -= (pw.offsetHeight || r.height || 0);
      } catch (_) {}
      try { pw.__inkStrokes = []; } catch (_) {}
      try { if (window._upClaimed) delete window._upClaimed[rec.page]; } catch (_) {}
      try { pw.remove(); } catch (_) {}   // 窗口内不重编号其余页:它们的页号仍匹配旧文件 → 页图/字符层/高亮全对
    }
    if (tempEl) delete _upTempEls[rec.id];
    _upDelQueue.push(rec);
    _upDrainDeletes();
  }
  function _upDrainDeletes() {
    if (_upDelBusy) return;
    var rec = _upDelQueue.shift();
    if (!rec) { _upDelFinish(); return; }
    _upDelBusy = true;
    _upDelIndicator('🗑 删除中…' + (_upDelQueue.length ? ('(还剩 ' + (_upDelQueue.length + 1) + ')') : ''));
    _upSendDelete(rec, 0);
  }
  function _upSendDelete(rec, attempt) {   // 串行:DELETE → 等这张改页 job 完成 → 下一张(不撞并发锁、不中途 reload)
    RC.reqJson('DELETE', UP_API + '?file=' + encodeURIComponent(UP_FILE) + '&id=' + encodeURIComponent(rec.id), null)
      .then(function (d) {
        if (d && d.ok && d.job_id) {
          _upWaitJob(d.job_id, function (ok, warns) {
            if (warns && warns.length) _upDelWarns = _upDelWarns.concat(warns);
            delete _upDeleting[rec.id]; _upDelBusy = false; _upDrainDeletes();
          });
          return;
        }
        if (d && !d.ok && /进行中/.test(d.error || '') && attempt < 40) { setTimeout(function () { _upSendDelete(rec, attempt + 1); }, 1500); return; }   // 撞别的改页 job → 排队重试
        if (!(d && !d.ok && /未找到|not\s*found/i.test(d.error || ''))) { if (d && !d.ok) _upDelWarns.push('第' + rec.page + '页:' + ((d && d.error) || '失败')); }   // 404=已删,忽略;其它记一下
        delete _upDeleting[rec.id]; _upDelBusy = false; _upDrainDeletes();
      }).catch(function () {
        if (attempt < 40) { setTimeout(function () { _upSendDelete(rec, attempt + 1); }, 2000); return; }
        _upDelWarns.push('第' + rec.page + '页:网络错误'); delete _upDeleting[rec.id]; _upDelBusy = false; _upDrainDeletes();
      });
  }
  function _upWaitJob(jobId, cb) {   // 等改页 job 完成(**不 reload**),再处理下一张
    var t = setInterval(function () {
      fetch('/pdf/api/job-status?id=' + encodeURIComponent(jobId), { cache: 'no-store' })
        .then(function (r) { return r.json(); }).then(function (j) {
          if (j.status === 'done') { clearInterval(t); cb(true, (j.result && j.result.warnings) || []); }
          else if (j.status === 'error' || j.status === 'unknown') { clearInterval(t); cb(false, [j.error || '删除失败']); }
        }).catch(function () {});   // 网络抖动:下一轮再试
    }, 800);
  }
  function _upDelFinish(attempt) {   // 全部删完 → 就地 reconcile(不刷新);失败**隔 900ms 重试一次**(异步挂载churn落定)再退 reload。
    if (!attempt) {
      var warns = _upDelWarns; _upDelWarns = [];
      _upDelIndicator('');
      if (warns.length && window.RC && RC.toast) RC.toast('部分删除失败:' + warns.join(';'));
    }
    fetch('/pdf/api/book-meta?file=' + encodeURIComponent(UP_FILE), { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (meta) {
        var ok = false;
        try { if (meta && meta.ok && window.__upReconcileDelete) ok = window.__upReconcileDelete(meta); }
        catch (e) { try { localStorage.setItem('_recon_dbg', ((localStorage.getItem('_recon_dbg') || '') + '|throw:' + e.message).slice(-1500)); } catch (_) {} }
        if (!ok) {
          if (!attempt) { setTimeout(function () { _upDelFinish(1); }, 900); return; }   // 自愈第二枪
          try { location.reload(); } catch (_) {}
        }
      })
      .catch(function () {
        if (!attempt) { setTimeout(function () { _upDelFinish(1); }, 900); return; }
        try { location.reload(); } catch (_) {}
      });
  }
  function _upMountBadges() {
    _upRealPages().forEach(function (p) {
      var pw = document.querySelector('.page-wrap[data-page-num="' + p.page + '"]');
      if (!pw) return;
      if (p.mode === 'overlay') {
        // 方案调整(2026-07-04):overlay 页**始终**挂覆盖层显示 sidecar md,无论 synced_ver 是否追上 md_ver。
        //   ← 反转批次2 的「已同步→当普通页(撤覆盖层)」退路:那退路让重开后公式降级为 $..$ 原文(不 MathJax)、
        //     offset 高亮休眠(改走字符层)。覆盖层态下:公式永远 RC.typeset 渲染、offset 高亮永远按 sidecar 复原、
        //     单击查词/精确高亮/划选 AI/工具栏由批次3 胶水对齐普通页;char-layer 隔离(.pdf-upage-overlay)始终生效。
        //   后台同步仍把 md 写回 PDF(为可移植 + 全文搜索),但**不再影响前端显示**(不撤覆盖层)。
        _upMountOverlay(p, pw);
        return;
      }
      // baked 老页:角标(点它走重排 job 编辑)
      if (pw.querySelector('.up2-badge')) return;
      var b = document.createElement('div'); b.className = 'up2-badge';
      var _lb = _upLabel(p);
      b.textContent = '📝 ' + (_lb || '我的页');
      b.title = (p.title || '我的页') + (_lb ? ('（' + _lb + '）') : '') + '(点击编辑)';
      b.addEventListener('click', function (e) {
        e.stopPropagation();
        var rec = _upRealPages().filter(function (x) { return x.id === p.id; })[0] || p;
        _upEditReal(rec);
      });
      pw.appendChild(b);
      _upEnsureFavBtn(p, pw);   // baked 老页也可收藏(物化读 sidecar md;右上角 ⭐)
    });
  }
  function _upCurPage() {   // 当前页 = 与视口交叠最多的 .page-wrap(同 _favOpenPicker 口径)
    var page = 0, mid = window.innerHeight / 2, bestOv = -1;
    document.querySelectorAll('.page-wrap[data-page-num]').forEach(function (pw) {
      var r = pw.getBoundingClientRect(); if (!r.height) return;
      var ov = Math.min(r.bottom, window.innerHeight) - Math.max(r.top, 0);
      if (r.top <= mid && r.bottom >= mid) ov += 1e6;
      if (ov > bestOv) { bestOv = ov; page = parseInt(pw.dataset.pageNum, 10) || 0; }
    });
    return page || (window.__PDF_CFG && __PDF_CFG.page) || 1;
  }
  function _upPlace(el, after, refEl) {
    var pc = document.getElementById('page-container'); if (!pc) return false;
    var anchorPage = after > 0 ? after : 1;   // after=0 书首 = 插在第 1 页之前
    var pw = pc.querySelector('.page-wrap[data-page-num="' + anchorPage + '"]');
    if (!pw) { if (el.isConnected) el.remove(); return false; }   // 目标页不在(单页模式看别的页/占位未建)→ 摘下待重试
    var row = (pw.closest && pw.closest('.spread-row')) || pw;    // 双页:插在整行之后,不拆行
    var _pr = pw.getBoundingClientRect();
    var w = _pr.width, h = _pr.height;
    if (w > 40) el.style.width = Math.round(w) + 'px';            // 宽度对齐页宽(缩放后下次 mount 自动刷新)
    // 临时新建白纸页(.up2-new)保持邻页的宽高比:侧栏(助手/知识点)挤窄主区时,靠 aspect-ratio+max-width:100%
    // 让高度随宽度等比缩小,而非固定 min-height:80vh 导致「变瘦」失真(邻真页本身走 refit 重渲已等比,这里让白纸与之一致)。
    // 只对 .up2-new(临时白纸)生效;legacy 虚拟 .pdf-upage 内容卡按内容自适应高度,不套比例(零回归)。
    if (el.classList && el.classList.contains('up2-new') && w > 40 && h > 40) {
      el.style.aspectRatio = Math.round(w) + ' / ' + Math.round(h);
      el.style.minHeight = '0';                                  // 覆盖 CSS 的 min-height:80vh,让 aspect-ratio 独占驱动高度
    }
    if (after > 0) {
      var ref = refEl || row;
      if (el.previousElementSibling === ref && el.parentNode === ref.parentNode) return true;   // 已在位
      ref.parentNode.insertBefore(el, ref.nextSibling);
    } else {
      if (refEl) {   // 同为书首的第 2+ 页:接在前一个用户页后面
        if (el.previousElementSibling === refEl && el.parentNode === refEl.parentNode) return true;
        refEl.parentNode.insertBefore(el, refEl.nextSibling);
      } else {
        if (el.nextElementSibling === row && el.parentNode === row.parentNode) return true;
        row.parentNode.insertBefore(el, row);
      }
    }
    return true;
  }
  // 模式切换(连续↔双页)走 _remodeListInPlace:container.innerHTML='' 只回填 .page-wrap → 兄弟元素被丢。
  // 包一层(reader.js 是 module,加载晚于本脚本 → 首次 mount 时再补),重排完幂等补挂。不动 reader.src。
  var _remodePatched = false;
  function _upPatchRemode() {
    if (_remodePatched || !window._remodeListInPlace) return;
    _remodePatched = true;
    var orig = window._remodeListInPlace;
    window._remodeListInPlace = function () {
      var r = orig.apply(this, arguments);
      try { if (window.RC && RC.userpages) RC.userpages.mountAll(); } catch (_) {}
      return r;
    };
  }
  // 便签可贴在用户页上:PDF 便签锚 page 支持 u_* 字符串。27-rc-adapter 的 stickynote init 是
  // `mount:(a)=>PdfAdapter._host.noteMount(a)`(调用时动态取 _host 方法)→ 这里包 _host 两个方法即可,零 reader.src 改动。
  var _noteHostWrapped = false;
  function _upWrapNoteHost() {
    if (_noteHostWrapped) return;
    var h = window.PdfAdapter && PdfAdapter._host;
    if (!h || !h.noteMount || !h.noteAnchorFromPoint) return;
    _noteHostWrapped = true;
    var m0 = h.noteMount, a0 = h.noteAnchorFromPoint;
    h.noteMount = function (anchor) {
      if (anchor && anchor.kind === 'pdf' && typeof anchor.page === 'string') {   // 用户页:比例锚,容器=.pdf-upage
        var el = (window.RC && RC.userpages && RC.userpages.elOf) ? RC.userpages.elOf(anchor.page) : null;
        if (!el || !el.isConnected) return null;
        var x = Math.max(0, Math.min(1, anchor.x || 0)), y = Math.max(0, Math.min(1, anchor.y || 0));
        return { el: el, left: x * el.clientWidth, top: y * el.clientHeight };
      }
      return m0.apply(this, arguments);
    };
    h.noteAnchorFromPoint = function (x, y) {
      var t = document.elementFromPoint(x, y);
      var up = t && t.closest ? t.closest('.pdf-upage') : null;
      if (up && up.dataset.uid) {
        var r = up.getBoundingClientRect();
        if (!r.width || !r.height) return null;
        return { kind: 'pdf', page: up.dataset.uid, x: (x - r.left) / r.width, y: (y - r.top) / r.height };
      }
      return a0.apply(this, arguments);
    };
  }
  // 幂等挂载 hook:04-render 每页渲染完调;setTimeout 去抖(连续模式多页并发渲染)。
  // 顺带渲真实页角标(_unloadFarPages/模式切换丢了也会在下次渲染补挂,幂等)。
  var _upSched = false;
  window._userpagesMount = function () {
    if (_upSched) return; _upSched = true;
    setTimeout(function () {
      _upSched = false;
      try { _upPatchRemode(); _upWrapNoteHost(); if (window.RC && RC.userpages) RC.userpages.mountAll(); _upMountBadges(); } catch (_) {}
      if (UP_LOCAL) { try { _lpRestore(); } catch (_) {} }   // 本机纸随页渲染幂等补挂
    }, 0);
  };
  RC.userpages.init({
    file: UP_FILE,
    cls: 'pdf-upage',
    filter: function (p) { return typeof p.page !== 'number'; },   // 真实页记录不 mount DOM(host 渲角标),只旧虚拟页
    place: _upPlace,
    afterCurrent: _upCurPage,   // 「在当前页之后」(PDF 页号 1-based)
    posLabel: function (a) { return a > 0 ? ('第 ' + (window._dispPage ? window._dispPage(a) : a) + ' 页') : '书首'; },
    scrollTo: function (el) { try { el.scrollIntoView({ block: 'start', behavior: 'auto' }); } catch (_) {} }
  });
  RC.userpages.load();   // 列表先到手;虚拟页等 _userpagesMount 幂等补挂
  // load 是异步的:开书时页可能先渲染完(hook 已过)→ 启动期重试几轮补角标
  var _upBootTries = 0;
  var _upBootT = setInterval(function () {
    try { _upMountBadges(); } catch (_) {}
    if (UP_LOCAL) { try { _lpRestore(); } catch (_) {} }   // 本机纸恢复:页区就绪前 _upPlace 失败,重试幂等补挂
    if (++_upBootTries >= 12) clearInterval(_upBootT);
  }, 500);
  // ➕ = 乐观新建(方案调整 2026-07-04):点 ➕ 立刻在插入点插一个**可编辑覆盖层**(临时 client id,马上打字),
  //   后台静默 POST 插空白 PDF 页;job done → 绑真 id + 落库缓冲击键 + 就地升级为正式 overlay 页,全程不 reload、不等待。
  //   临时元素是独立虚拟元素(.pdf-upage,不占 data-page-num,after 定位)—— 本会话显示它;下次开书那页作为真
  //   .page-wrap + 覆盖层 mount(始终覆盖层,同 _upMountBadges)。视觉都是覆盖层,一致。
  //   击键缓冲(评审 #8):job 未 done 前记录尚无真 id,用户打的字先本地缓冲(_upTextSnap[tempId]),done 拿真 id 后一次性 PATCH 落库,别丢。
  function _upResolveNewId(realPage, cb) {   // job done 后由服务端 sidecar 反查刚建 overlay 记录的真 id(按新页号唯一匹配)
    RC.reqJson('GET', UP_TEXT_API + '?file=' + encodeURIComponent(UP_FILE), null).then(function (d) {
      var arr = (d && d.pages) || [];
      for (var i = 0; i < arr.length; i++) { if (arr[i].mode === 'overlay' && arr[i].page === realPage) { cb(arr[i].id, arr[i]); return; } }
      cb(null, null);
    }).catch(function () { cb(null, null); });
  }
  function _upBindTempToReal(tempId, realId, realPage, realRec, rec) {
    if (_upTempCancelled[tempId]) {   // 用户在 job 完成前已取消(删)→ 删掉那张空白真页(清理孤儿页)
      delete _upTempCancelled[tempId];
      RC.reqJson('DELETE', UP_API + '?file=' + encodeURIComponent(UP_FILE) + '&id=' + encodeURIComponent(realId), null).catch(function () {});
      return;
    }
    var snap = _upTextSnap[tempId];
    rec.id = realId; rec.page = realPage; rec._temp = false;   // 就地升级:同一 rec 对象 → 覆盖层闭包后续用真 id
    // ★#4:登记"这个页号本会话归插入页占用"。乐观插入不重编号 DOM → 同号还有张陈旧真页;
    //   _upClaimed 让墨迹渲染路径(_inkLoadAll / SSE / 04-render)绝不把插入页墨迹贴到陈旧同号页。
    try { (window._upClaimed = window._upClaimed || {})[realPage] = realId; } catch (e) {}
    if (realRec) { rec.md_ver = +(realRec.md_ver || 0); rec.synced_ver = +(realRec.synced_ver || 0); }
    var el = _upTempEls[tempId];
    if (el) {
      el.dataset.uid = realId; delete _upTempEls[tempId]; _upTempEls[realId] = el;   // 便签比例锚 u_* 跟着换;__upRec 是同一对象(rec.id 已更新)
      if (el.__inkStrokes && el.__inkStrokes.length) window._upInkPersist(el);         // 临时期手写落库到 realPage(/api/ink);此刻 rec 已绑真 id
    }
    // 新建页做好后**自动翻到那一页**(用户要求)——仅当它此刻不在视口才滚,已经看得见就不打断。
    try {
      var _nel = _ovElOfRec(rec);
      if (_nel) {
        var _r = _nel.getBoundingClientRect();
        if (!(_r.bottom > 60 && _r.top < (window.innerHeight || 0) - 60)) _nel.scrollIntoView({ block: 'center', behavior: 'smooth' });
      }
    } catch (e) {}
    _upMigrateOvHls(tempId, realId, rec);   // 临时期精确 offset 高亮(tempId 键 sidecar)迁到 realId 键 + 重挂 mark
    if (_upEditedIds[tempId]) { _upEditedIds[realId] = true; delete _upEditedIds[tempId]; }
    delete _upTextTimers[tempId]; delete _upTextDirty[tempId];
    if (snap && ((snap.md && snap.md.trim()) || (snap.title && snap.title.trim()))) {   // 落库缓冲击键(一次 PATCH)
      _upTextSnap[realId] = snap; delete _upTextSnap[tempId];
      var pr = _upTextSave(realId);
      if (rec._syncOnBind) { try { pr.then(function () { _upEnqueueSync(realId); }); } catch (_) {} }   // 已按过「完成」→ 顺带后台同步写回 PDF
    } else { delete _upTextSnap[tempId]; }
  }
  function _upTempFail(tempId) {   // job error:移除临时元素(缓冲的字仍在 _upTextSnap[tempId],不静默丢)
    var el = _upTempEls[tempId]; if (el) { try { el.remove(); } catch (_) {} }
    delete _upTempEls[tempId];
  }
  function _upWatchCreate(jobId, tempId, rec, mini) {
    var t = setInterval(function () {
      fetch('/pdf/api/job-status?id=' + encodeURIComponent(jobId), { cache: 'no-store' })
        .then(function (r) { return r.json(); }).then(function (j) {
          if (j.status === 'done') {
            clearInterval(t);
            var realPage = (j.result && j.result.page) || 0;
            _upMiniEnd(mini, _upTempCancelled[tempId] ? '已取消新增' : '✔ 已新增页', false);
            if (!realPage) return;   // 拿不到页号(异常)→ 临时元素留着,下次开书真页自然 mount
            _upResolveNewId(realPage, function (realId, realRec) {   // cancelled 时 bind 内部会对真 id 发 DELETE 清理空白页
              if (realId) _upBindTempToReal(tempId, realId, realPage, realRec, rec);
            });
          } else if (j.status === 'error' || j.status === 'unknown') {
            clearInterval(t); _upMiniEnd(mini, '新增失败(原书有备份)', true); _upTempFail(tempId);
          } else if (j.step && mini) { var mt = mini.querySelector('.up2-mini-t'); if (mt) mt.textContent = j.step + '…'; }
        }).catch(function () {});
    }, 1000);
  }
  window._upCreate = function () {
    if (_upEditing || document.body.classList.contains('up-editing')) { alert('先完成当前正在编辑的页'); return; }
    var after = 0;
    try { after = _upCurPage() | 0; } catch (_) {}
    if (after < 0) after = 0;
    var tempId = 'tmp_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
    var rec = { id: tempId, page: 0, title: '', md: '', mode: 'overlay', md_ver: 0, synced_ver: 0, _temp: true };
    var tmp = document.createElement('div');
    tmp.className = 'rc-upage pdf-upage up2-new'; tmp.dataset.uid = tempId;
    if (!_upPlace(tmp, after, null)) { alert('请先翻到第 ' + (after || 1) + ' 页附近,再新建插入页'); return; }
    try { tmp.scrollIntoView({ block: 'center', behavior: 'auto' }); } catch (_) {}
    _upTempEls[tempId] = tmp;
    _upOverlayEnterEdit(rec, tmp);   // 立即进即时编辑(textarea 聚焦),用户马上打字 → 缓冲(临时 id)
    var mini = _upMini('正在后台新增 PDF 页…');
    RC.reqJson('POST', UP_API, { file: UP_FILE, after: after, title: '', md: '' }).then(function (d) {
      if (!(d && d.ok && d.job_id)) { _upMiniEnd(mini, '新增失败', true); _upTempFail(tempId); alert('创建失败:' + ((d && d.error) || '?')); return; }
      _upWatchCreate(d.job_id, tempId, rec, mini);
    }).catch(function () { _upMiniEnd(mini, '网络错误', true); _upTempFail(tempId); alert('网络错误,没创建上'); });
  };
})();
