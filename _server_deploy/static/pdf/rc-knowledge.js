/* rc-knowledge.js — 统一控制层:右侧「知识点」抽屉 + 竖排把手(共享,本步只接 EPUB)。
 * 照搬 PDF 阅读器 #grammar-panel(磨砂玻璃抽屉)+ #side-handle(竖排把手)+ loadPageNodes 的 .kg-node 卡:
 *   状态左色条(mastered绿/unlockable蓝/locked灰)+ 点开 /skilltree/<book>/#f.<id> + grammar 节点 ☆跟踪。
 * 复用 PDF 的 .kg-node/.kg-track-btn 类名(EPUB 模板没定义,不冲突);容器 id 用 EPUB 专属 #ep-kg-panel / #ep-side-handle 防撞 PDF。
 * 底座耦合只走 opts:fetchNodes()→Promise<[nodes]>、onOpenNode(node)、onToggleTrack(node,btn)(都可省,内置默认走 skilltree)。
 * z-index 在 EPUB 梯度内排:抽屉 76 / 把手 77,都 < #ep-sel(78) → 选中工具栏永远盖在抽屉之上,不冲突。
 */
(function () {
  if (!window.RC) window.RC = {};
  if (window.RC.knowledge) return;

  var _opts = {};
  var _injected = false;
  var _loaded = false;          // 首次 load 标记(失败可重试)
  var _busy = false;            // load 进行中,防并发
  function esc(s) {
    if (window.RC && RC.esc) return RC.esc(s);
    var d = document.createElement('div'); d.textContent = (s == null ? '' : String(s)); return d.innerHTML;
  }
  function toast(m) { if (window.RC && RC.toast) { RC.toast(m); return; } try { console.log(m); } catch (e) {} }

  function injectCss() {
    if (_injected) return; _injected = true;
    var st = document.createElement('style'); st.id = 'rc-kg-css';
    // 把手/抽屉/卡片 CSS 照搬 PDF #grammar-panel + #side-handle + .kg-node,仅换容器 id + 挤压目标 + EPUB z-index 梯度。
    st.textContent = `
/* 右侧抽屉:仿仪表盘磨砂玻璃滑出(本书知识点)。默认挤压 → EPUB 正文留左侧可读 */
#ep-kg-panel{position:fixed;top:0;right:0;bottom:0;width:min(38vw,560px);display:flex;flex-direction:column;z-index:76;
  background:linear-gradient(135deg,rgba(255,255,255,0.08),rgba(255,255,255,0.03)),rgba(14,20,40,0.62);
  backdrop-filter:blur(var(--gp-blur,20px)) saturate(150%) brightness(1.05);-webkit-backdrop-filter:blur(var(--gp-blur,20px)) saturate(150%) brightness(1.05);
  border-left:1px solid rgba(255,255,255,0.20);
  box-shadow:-14px 0 48px rgba(0,0,0,0.45),inset 1px 0 0 rgba(255,255,255,0.16);
  transform:translateX(102%);transition:transform 0.4s cubic-bezier(.4,0,.2,1);touch-action:pan-y;padding-top:env(safe-area-inset-top)}
/* 磨砂颗粒(同仪表盘 / PDF 抽屉) */
#ep-kg-panel::before{content:'';position:absolute;inset:0;pointer-events:none;opacity:0.28;mix-blend-mode:overlay;border-radius:inherit;
  background-image:url("data:image/svg+xml;utf8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='180' height='180'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/%3E%3CfeColorMatrix values='0 0 0 0 1  0 0 0 0 1  0 0 0 0 1  0 0 0 0.5 0'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")}
/* iOS 层叠坑:磨砂 ::before 在内容下层,子元素必须建独立层叠并抬到其上(别漏) */
#ep-kg-panel>*{position:relative;z-index:1}
#ep-kg-panel.open{transform:translateX(0)}
/* 抽屉头部 */
#ep-kg-panel .ep-kg-head{flex:0 0 auto;padding:16px 14px 11px;font-size:14px;font-weight:600;color:#cfe6ff;border-bottom:1px solid #2a3550}
/* 节点列表(可滚动) */
#ep-kg-nodes{flex:1 1 auto;overflow-y:auto;overflow-x:hidden;min-height:0;padding:14px;overscroll-behavior:contain;touch-action:pan-y;-webkit-overflow-scrolling:touch}
/* 右边缘把手:仿仪表盘磨砂 grip pill;面板展开时移到抽屉左缘(点它关) */
#ep-side-handle{position:fixed;right:0;top:50%;transform:translateY(-50%);z-index:77;writing-mode:vertical-rl;
  background:linear-gradient(135deg,rgba(255,255,255,0.10),rgba(255,255,255,0.04)),rgba(99,102,241,0.18);
  backdrop-filter:blur(12px) saturate(150%) brightness(1.04);-webkit-backdrop-filter:blur(12px) saturate(150%) brightness(1.04);
  border:1px solid rgba(255,255,255,0.22);border-right:none;border-radius:14px 0 0 14px;
  color:rgba(255,255,255,0.85);padding:16px 7px;font-size:11px;letter-spacing:2px;cursor:pointer;
  box-shadow:inset 1px 0 0 rgba(255,255,255,0.16),-2px 0 12px rgba(0,0,0,.35);user-select:none;-webkit-user-select:none;-webkit-tap-highlight-color:transparent;
  transition:right 0.4s cubic-bezier(.4,0,.2,1),background .15s,color .15s}
#ep-side-handle:hover{color:#fff;background:linear-gradient(135deg,rgba(255,255,255,0.15),rgba(255,255,255,0.06)),rgba(99,102,241,0.28)}
body.ep-kg-open #ep-side-handle{right:min(38vw,560px)}
/* 抽屉展开时把 EPUB 正文(+顶栏)让出右侧空间(挤压,左侧仍可读,把手始终可见可关) */
body.ep-kg-open #ep-content{right:min(38vw,560px)}
body.ep-kg-open #ep-top{right:min(38vw,560px)}
@media (max-width:900px){
  /* 窄屏也挤压(不全屏盖住),左侧正文缩小后完整显示 */
  #ep-kg-panel{width:58vw;max-width:none}
  body.ep-kg-open #ep-content,body.ep-kg-open #ep-top{right:58vw}
  body.ep-kg-open #ep-side-handle{right:58vw}
}
/* 知识点节点卡(照搬 PDF .kg-node) */
#ep-kg-nodes .kg-node{background:#0d1322;border:1px solid #1f2740;border-radius:6px;padding:8px 10px;margin-bottom:6px;font-size:12px;display:flex;align-items:flex-start;gap:8px}
#ep-kg-nodes .kg-node .kg-node-main{flex:1;min-width:0;cursor:pointer}
#ep-kg-nodes .kg-node:hover{background:#162045;border-color:#3b6db5}
#ep-kg-nodes .kg-track-btn{flex:none;align-self:center;background:transparent;border:1px solid #3a4456;color:#8a9bb4;border-radius:5px;padding:3px 8px;font-size:11px;cursor:pointer;white-space:nowrap}
#ep-kg-nodes .kg-track-btn:hover{border-color:#34d399;color:#cfe6ff}
#ep-kg-nodes .kg-track-btn.on{background:#13351f;border-color:#34d399;color:#34d399}
#ep-kg-nodes .kg-node .lbl{font-weight:600;color:#cfe6ff}
#ep-kg-nodes .kg-node .sum{color:#8a9bb4;font-size:11px;margin-top:3px;line-height:1.5}
#ep-kg-nodes .kg-node.mastered{border-left:3px solid #34d399}
#ep-kg-nodes .kg-node.unlockable{border-left:3px solid #60a5fa}
#ep-kg-nodes .kg-node.locked{border-left:3px solid #3a4456;opacity:.7}
#ep-kg-nodes .kg-empty{color:#5a6680;font-size:12px}`;
    document.head.appendChild(st);
  }

  // ── 卡片 CSS 自注入(renderInto 用):作用域到传入容器 id(有 id → '#id .kg-node…'),无 id 退裸 '.kg-node…' 兜底。
  //    加固:renderInto 不走 init()/injectCss(),故不能只靠宿主模板静态 CSS —— 自己保证 .kg-node 卡有样式(值与 PDF 模板逐字一致 → 零视觉回归)。
  var _cardCssScopes = {};
  function injectCardCss(container) {
    var s = (container && container.id) ? ('#' + container.id + ' ') : '';
    if (_cardCssScopes[s]) return; _cardCssScopes[s] = true;
    var st = document.createElement('style'); st.className = 'rc-kg-card-css';
    st.textContent =
      s + '.kg-node{background:#0d1322;border:1px solid #1f2740;border-radius:6px;padding:8px 10px;margin-bottom:6px;font-size:12px;display:flex;align-items:flex-start;gap:8px}' +
      s + '.kg-node .kg-node-main{flex:1;min-width:0;cursor:pointer}' +
      s + '.kg-node:hover{background:#162045;border-color:#3b6db5}' +
      s + '.kg-track-btn{flex:none;align-self:center;background:transparent;border:1px solid #3a4456;color:#8a9bb4;border-radius:5px;padding:3px 8px;font-size:11px;cursor:pointer;white-space:nowrap}' +
      s + '.kg-track-btn:hover{border-color:#34d399;color:#cfe6ff}' +
      s + '.kg-track-btn.on{background:#13351f;border-color:#34d399;color:#34d399}' +
      s + '.kg-node .lbl{font-weight:600;color:#cfe6ff}' +
      s + '.kg-node .sum{color:#8a9bb4;font-size:11px;margin-top:3px;line-height:1.5}' +
      s + '.kg-node.mastered{border-left:3px solid #34d399}' +
      s + '.kg-node.unlockable{border-left:3px solid #60a5fa}' +
      s + '.kg-node.locked{border-left:3px solid #3a4456;opacity:.7}' +
      s + '.kg-empty{color:#5a6680;font-size:12px}';
    document.head.appendChild(st);
  }

  // ── 默认底座行为(opts 没给就用):跟 PDF loadPageNodes / toggleNodeTrack 一致 ──
  function _defaultOpenNode(n) {
    try { window.open('/skilltree/' + encodeURIComponent(n.book || '') + '/#' + encodeURIComponent('f.' + n.id), '_blank'); } catch (e) {}
  }
  function _defaultToggleTrack(n, btn) {
    btn.disabled = true;
    fetch('/skilltree/' + encodeURIComponent(n.book || '') + '/api/toggle-tracked', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ node_id: n.id })
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.ok) {
        n.tracked = !!d.tracked;
        btn.classList.toggle('on', n.tracked);
        btn.textContent = n.tracked ? '★ 跟踪中' : '☆ 跟踪';
        // 跟踪态变了 → 刷新语法启用/按钮可见性(等价 PDF toggleNodeTrack 后 loadGrammarTracked)
        try { if (typeof window.loadGrammarTracked === 'function') window.loadGrammarTracked(); } catch (_) {}
      } else { toast((d && d.error) || '操作失败'); }
    }).catch(function () { toast('网络错误'); }).then(function () { btn.disabled = false; });
  }

  // 一张知识点卡(照搬 loadPageNodes 的卡片结构 + 状态左色条 + 点开 skilltree + grammar ☆跟踪)
  // opts 默认 _opts(EPUB 既有调用零变化);renderInto 复用时传 per-call opts(PDF 阅读器路径)。
  function makeCard(n, opts) {
    opts = opts || _opts;
    var card = document.createElement('div');
    card.className = 'kg-node ' + (n.state || 'locked');
    var main = document.createElement('div'); main.className = 'kg-node-main';
    var lbl = document.createElement('div'); lbl.className = 'lbl';
    lbl.textContent = (n.numeric_label ? '[' + n.numeric_label + '] ' : '') + (n.name || '');
    var sum = document.createElement('div'); sum.className = 'sum'; sum.textContent = n.summary || '';
    main.appendChild(lbl); main.appendChild(sum);
    main.addEventListener('click', function () {
      try { (opts.onOpenNode || _defaultOpenNode)(n); } catch (e) {}
    });
    card.appendChild(main);
    // 只有 grammar KG 的节点能跟踪(跟技能树 toggle-tracked 规则一致)
    var isGrammar = !!(n.is_grammar || n.kind === 'grammar');
    if (isGrammar) {
      var btn = document.createElement('button');
      btn.className = 'kg-track-btn' + (n.tracked ? ' on' : '');
      btn.textContent = n.tracked ? '★ 跟踪中' : '☆ 跟踪';
      btn.title = '加入/取消语法跟踪';
      btn.addEventListener('click', function (ev) {
        ev.stopPropagation();
        (opts.onToggleTrack || _defaultToggleTrack)(n, btn);
      });
      card.appendChild(btn);
    }
    return card;
  }

  function render(list) {
    var c = document.getElementById('ep-kg-nodes');
    if (!c) return;
    c.innerHTML = '';
    if (!list || !list.length) {
      // 空态文案逐字对照 PDF reader.src/05-nav.js loadPageNodes
      c.innerHTML = '<div class="kg-empty">该页无 KG 节点（可能这本书没扫过/或本页不是知识点页）</div>';
      return;
    }
    for (var i = 0; i < list.length; i++) c.appendChild(makeCard(list[i]));
  }

  // ── 把节点卡渲进**宿主自带容器**(PDF 阶段4 复用入口)──────────────────────────
  // EPUB 自身只走 init/load → render 到固定 #ep-kg-nodes;此方法纯新增,EPUB 不调用 → 对 EPUB 零影响。
  // PDF 阅读器:容器=#kg-nodes(沿用 PDF 自己的 .kg-node CSS,视觉与原版逐字一致)、页作用域取数 + __lastPageNodes
  // 全由 PdfAdapter 负责,本方法只复用 makeCard 的卡片结构/状态色条/☆跟踪。
  // opts:{onOpenNode?, onToggleTrack?, emptyHtml?} 都可省(默认走 skilltree,与 PDF loadPageNodes/toggleNodeTrack 逐字一致)。
  function renderInto(container, list, opts) {
    if (typeof container === 'string') container = document.getElementById(container);
    if (!container) return;
    opts = opts || {};
    injectCardCss(container);   // 加固:自带 .kg-node 卡 CSS(作用域到本容器),不依赖宿主模板静态 CSS
    container.innerHTML = '';
    if (!list || !list.length) {
      container.innerHTML = opts.emptyHtml ||
        '<div class="kg-empty">该页无 KG 节点（可能这本书没扫过/或本页不是知识点页）</div>';
      return;
    }
    for (var i = 0; i < list.length; i++) container.appendChild(makeCard(list[i], opts));
  }

  function load() {
    var c = document.getElementById('ep-kg-nodes');
    if (!c || _busy) return;
    if (typeof _opts.fetchNodes !== 'function') { render([]); return; }
    _busy = true;
    c.innerHTML = '<div class="kg-empty">加载中…</div>';
    Promise.resolve().then(function () { return _opts.fetchNodes(); }).then(function (nodes) {
      _loaded = true;
      window.__lastBookNodes = nodes || [];   // 给语音助手/上下文复用
      render(nodes || []);
    }).catch(function () {
      c.innerHTML = '<div class="kg-empty" style="color:#c00">加载失败</div>';
    }).then(function () { _busy = false; });
  }

  function isOpen() {
    var p = document.getElementById('ep-kg-panel');
    return !!(p && p.classList.contains('open'));
  }
  function open() {
    var p = document.getElementById('ep-kg-panel');
    if (!p) return;
    p.classList.add('open');
    document.body.classList.add('ep-kg-open');
    load();   // 每次打开都刷新(本书节点 + 最新 tracked 态),单次请求,廉价
  }
  function close() {
    var p = document.getElementById('ep-kg-panel');
    if (p) p.classList.remove('open');
    document.body.classList.remove('ep-kg-open');
  }
  function toggle() { if (isOpen()) close(); else open(); }

  function init(opts) {
    _opts = opts || {};
    // 合并抽屉模式(EPUB rc-sidedrawer):不自建 #ep-kg-panel/#ep-side-handle、不注 chrome CSS;
    // 节点卡由 rc-sidedrawer 提供的 #ep-kg-nodes 承载,本模块只保留 load()/render()。opt-in,默认 false → 旧调用零变化、PDF 不引故零影响。
    if (_opts.embedded) { return window.RC.knowledge; }
    injectCss();
    if (!document.getElementById('ep-side-handle')) {
      var h = document.createElement('div');
      h.id = 'ep-side-handle';
      h.textContent = '知识点';
      h.title = '展开侧栏:本书知识点';
      h.addEventListener('click', toggle);
      document.body.appendChild(h);
    }
    if (!document.getElementById('ep-kg-panel')) {
      var p = document.createElement('aside');
      p.id = 'ep-kg-panel';
      p.innerHTML = '<div class="ep-kg-head">本书知识点</div><div id="ep-kg-nodes"><div class="kg-empty">加载中…</div></div>';
      document.body.appendChild(p);
    }
    return window.RC.knowledge;
  }

  window.RC.knowledge = {
    init: init,
    toggle: toggle,
    open: open,
    close: close,
    load: load,
    isOpen: isOpen,
    renderInto: renderInto   // 阶段4:PDF 阅读器把页节点渲进自己的 #kg-nodes 容器(EPUB 不调用)
  };
})();
