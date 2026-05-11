#!/usr/bin/env python3
"""历史查看器：本地浏览截图问答所有对话记录"""

import os, json, sqlite3, re, socket, subprocess, webbrowser, threading, ctypes
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path

_CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]
CHROME_EXE = next((p for p in _CHROME_CANDIDATES if Path(p).exists()), None)

_localappdata = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
STORE_DIR    = Path(_localappdata) / "截图问答"
DB_PATH      = STORE_DIR / "history.db"
HIST_IMG_DIR = STORE_DIR / "images"

# ─── 数据库 ────────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def find_free_port():
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]

# ─── HTML ──────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>问答历史</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#f0f2f5;min-height:100vh;font-size:14px;color:#1a1a1a}
#header{background:#fff;border-bottom:1px solid #e5e7eb;padding:9px 18px;display:flex;align-items:center;gap:10px;position:sticky;top:0;z-index:10;flex-wrap:wrap}
#header h1{font-size:15px;font-weight:600;color:#111;flex-shrink:0}
#tabs{display:flex;gap:2px;background:#f3f4f6;border-radius:8px;padding:3px;flex-shrink:0}
.tab{background:none;border:none;border-radius:6px;padding:4px 11px;font-size:12px;cursor:pointer;color:#6b7280;transition:all .15s;white-space:nowrap}
.tab.active{background:#fff;color:#1a1a1a;font-weight:500;box-shadow:0 1px 3px rgba(0,0,0,.1)}
.tab-count{color:#bbb;margin-left:3px}
.tab.active .tab-count{color:#9ca3af}
#search{flex:1;min-width:120px;max-width:240px;border:1px solid #e5e7eb;border-radius:8px;padding:5px 10px;font-size:13px;outline:none;font-family:inherit;transition:border-color .15s;margin-left:auto}
#search:focus{border-color:#0078d4;box-shadow:0 0 0 2px #e3f0fd}
#content{padding:12px 16px;max-width:860px;margin:0 auto;display:flex;flex-direction:column;gap:7px}
#empty{padding:60px 20px;text-align:center;color:#9ca3af;font-size:14px}
/* ─── 卡片 ─── */
.card{background:#fff;border-radius:10px;border:1px solid #e5e7eb;overflow:hidden;transition:box-shadow .15s}
.card:hover{box-shadow:0 2px 10px rgba(0,0,0,.07)}
.card.wrong{border-left:3px solid #e74c3c}
.card-head{display:flex;align-items:center;gap:10px;padding:10px 12px;cursor:pointer;user-select:none}
.card-thumb{width:76px;height:57px;object-fit:cover;border-radius:5px;border:1px solid #e8e8e8;flex-shrink:0;background:#f5f5f5}
.card-thumb-blank{width:76px;height:57px;border-radius:5px;background:#f0f2f5;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:22px;color:#d1d5db}
.card-meta{flex:1;min-width:0}
.card-ts{font-size:11px;color:#9ca3af;margin-bottom:3px;display:flex;align-items:center;gap:5px}
.wrong-badge{font-size:10px;background:#fde8e8;color:#c0392b;border-radius:3px;padding:1px 5px;font-weight:600}
.card-note{font-size:13px;color:#374151;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:2px}
.card-count{font-size:11px;color:#9ca3af}
.card-actions{display:flex;align-items:center;gap:2px;flex-shrink:0}
.card-del{background:none;border:none;color:#d1d5db;cursor:pointer;font-size:14px;padding:4px 6px;border-radius:5px;transition:all .15s;line-height:1}
.card-del:hover{color:#e74c3c;background:#fef2f2}
.card-toggle{background:none;border:none;color:#9ca3af;cursor:pointer;font-size:12px;padding:4px 6px;border-radius:5px;transition:all .15s;line-height:1}
.card-toggle:hover{color:#374151;background:#f5f5f5}
/* ─── 展开内容 ─── */
.card-body{display:none;border-top:1px solid #f3f4f6;padding:14px 16px}
.card-body.open{display:block}
.card-img-full{max-width:100%;border-radius:6px;border:1px solid #e5e7eb;margin-bottom:14px;display:block}
.msg-block{margin-bottom:12px}
.msg-block:last-child{margin-bottom:0}
.msg-label{font-size:11px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;margin-bottom:5px;padding:2px 7px;border-radius:4px;display:inline-block}
.msg-label.user{background:#eff6ff;color:#1d4ed8}
.msg-label.assistant{background:#f0fdf4;color:#166534}
.msg-text{line-height:1.75;color:#374151;padding-left:2px}
.msg-divider{border:none;border-top:1px solid #f3f4f6;margin:12px 0}
/* ─── Markdown ─── */
.md p{margin:.35em 0}.md p:first-child{margin-top:0}.md p:last-child{margin-bottom:0}
.md h1,.md h2,.md h3,.md h4{margin:.5em 0 .2em;font-weight:600;font-size:1em}
.md h1{font-size:1.1em}.md h2{font-size:1.05em}
.md ul,.md ol{padding-left:1.5em;margin:.35em 0}.md li{margin:.1em 0}
.md pre{background:#f6f8fa;border:1px solid #e1e4e8;border-radius:6px;padding:8px 12px;overflow-x:auto;margin:.35em 0;font-size:.86em}
.md code{font-family:'Cascadia Code',Consolas,monospace;font-size:.86em;background:#f0f0f0;padding:1px 4px;border-radius:3px}
.md pre code{background:none;padding:0;font-size:inherit}
.md blockquote{border-left:3px solid #0078d4;padding-left:10px;color:#6b7280;margin:.35em 0}
.md strong{font-weight:600}.md em{font-style:italic}
.md table{border-collapse:collapse;margin:.35em 0;font-size:.9em}
.md td,.md th{border:1px solid #d0d0d0;padding:4px 10px}.md th{background:#f6f8fa;font-weight:600}
.md hr{border:none;border-top:1px solid #e0e0e0;margin:.5em 0}
</style>
</head>
<body>
<div id="header">
  <h1>问答历史</h1>
  <div id="tabs">
    <button class="tab active" data-type="all"    onclick="setTab('all')">全部 <span class="tab-count" id="cnt-all"></span></button>
    <button class="tab"        data-type="normal" onclick="setTab('normal')">普通 <span class="tab-count" id="cnt-normal"></span></button>
    <button class="tab"        data-type="wrong"  onclick="setTab('wrong')">错题 <span class="tab-count" id="cnt-wrong"></span></button>
  </div>
  <input id="search" type="search" placeholder="搜索笔记或内容…" oninput="onSearch()">
</div>
<div id="content"></div>

<script>
window.MathJax = {
  tex:{inlineMath:[['$','$'],['\\(','\\)']],displayMath:[['$$','$$'],['\\[','\\]']],processEscapes:true},
  options:{skipHtmlTags:['script','noscript','style','textarea','pre']},
  startup:{typeset:false},
};
</script>
<script src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml.js" async id="MathJax-script"></script>
<script src="https://cdn.jsdelivr.net/npm/marked@9/marked.min.js"></script>
<script>
marked.use({ breaks: true, gfm: true });

function renderMd(text) {
  const saved = [];
  const ph = m => { saved.push(m); return '\x02M' + (saved.length-1) + '\x02'; };
  let s = text
    .replace(/\$\$[\s\S]*?\$\$/g, ph).replace(/\\\[[\s\S]*?\\\]/g, ph)
    .replace(/\\\([\s\S]*?\\\)/g, ph)
    .replace(/(?<!\$)\$(?!\$)[^\n$]*?\$(?!\$)/g, ph);
  s = marked.parse(s);
  return s.replace(/\x02M(\d+)\x02/g, (_, i) => saved[+i]);
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

let allData = [];
let currentType = 'all';
let searchQ = '';
let searchTimer = null;

async function load() {
  document.getElementById('content').innerHTML = '<div id="empty">加载中…</div>';
  try {
    const d = await fetch('/api/history').then(r => r.json());
    allData = d.entries || [];
    const s = d.stats || {};
    document.getElementById('cnt-all').textContent    = s.total  || 0;
    document.getElementById('cnt-normal').textContent = s.normal || 0;
    document.getElementById('cnt-wrong').textContent  = s.wrong  || 0;
    renderList();
  } catch(e) {
    document.getElementById('content').innerHTML = `<div id="empty" style="color:#c00">加载失败：${esc(e.message)}</div>`;
  }
}

function setTab(type) {
  currentType = type;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.type === type));
  renderList();
}

function onSearch() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    searchQ = document.getElementById('search').value.toLowerCase().trim();
    renderList();
  }, 220);
}

function filterEntry(e) {
  if (currentType === 'normal' && e.record_type === 'wrong') return false;
  if (currentType === 'wrong'  && e.record_type !== 'wrong') return false;
  if (searchQ) {
    const hay = (e.note + ' ' + (e.messages || []).map(m => m.text).join(' ')).toLowerCase();
    if (!hay.includes(searchQ)) return false;
  }
  return true;
}

function noteShort(note) {
  return (note || '')
    .replace(/^已保存\([^)]*\)\s*→\s*.*?[\\\/]/, '')
    .replace(/^已保存\s*→\s*.*?[\\\/]/, '')
    .replace(/^已保存[^→]*→\s*/, '')
    || note || '（未保存）';
}

function renderList() {
  const content = document.getElementById('content');
  const filtered = allData.filter(filterEntry);
  if (!filtered.length) {
    content.innerHTML = '<div id="empty">暂无记录</div>';
    return;
  }
  content.innerHTML = '';
  filtered.forEach(e => {
    const card = document.createElement('div');
    card.className = 'card' + (e.record_type === 'wrong' ? ' wrong' : '');
    card.dataset.id = e.id;

    const thumb = e.img_fname
      ? `<img class="card-thumb" src="/img/${e.img_fname}" loading="lazy" onerror="this.outerHTML='<div class=card-thumb-blank>📷</div>'">`
      : `<div class="card-thumb-blank">📷</div>`;
    const badge = e.record_type === 'wrong' ? '<span class="wrong-badge">错题</span>' : '';

    card.innerHTML = `
      <div class="card-head" onclick="toggleCard('${e.id}')">
        ${thumb}
        <div class="card-meta">
          <div class="card-ts">${esc(e.timestamp)}${badge}</div>
          <div class="card-note" title="${esc(e.note)}">${esc(noteShort(e.note))}</div>
          <div class="card-count">${e.msg_count} 条消息</div>
        </div>
        <div class="card-actions" onclick="event.stopPropagation()">
          <button class="card-del" title="删除" onclick="delCard('${e.id}')">🗑</button>
        </div>
        <button class="card-toggle" id="tog-${e.id}">▼</button>
      </div>
      <div class="card-body" id="body-${e.id}" data-rendered="0"></div>
    `;
    card._entry = e;
    content.appendChild(card);
  });
}

function toggleCard(id) {
  const body = document.getElementById('body-' + id);
  const tog  = document.getElementById('tog-'  + id);
  if (!body) return;
  const open = body.classList.toggle('open');
  tog.textContent = open ? '▲' : '▼';
  if (open && body.dataset.rendered === '0') {
    body.dataset.rendered = '1';
    const e = allData.find(x => x.id === id);
    if (!e) return;
    let html = '';
    if (e.img_fname) html += `<img class="card-img-full" src="/img/${e.img_fname}" loading="lazy">`;
    (e.messages || []).forEach((m, i) => {
      if (i > 0) html += '<hr class="msg-divider">';
      const labelCls = m.role === 'user' ? 'user' : 'assistant';
      const labelTxt = m.role === 'user' ? '用户' : 'Claude';
      const textHtml = m.role === 'assistant'
        ? `<div class="md">${renderMd(m.text)}</div>`
        : `<div>${esc(m.text)}</div>`;
      html += `<div class="msg-block">
        <span class="msg-label ${labelCls}">${labelTxt}</span>
        <div class="msg-text">${textHtml}</div>
      </div>`;
    });
    body.innerHTML = html;
    if (window.MathJax) MathJax.typesetPromise([body]).catch(() => {});
  }
}

async function delCard(id) {
  const card = document.querySelector(`.card[data-id="${id}"]`);
  if (!card) return;
  card.style.opacity = '0.4';
  try {
    const r = await fetch('/api/delete', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id})
    }).then(r => r.json());
    if (r.ok) {
      card.remove();
      allData = allData.filter(e => e.id !== id);
      const total  = allData.length;
      const wrong  = allData.filter(e => e.record_type === 'wrong').length;
      const normal = total - wrong;
      document.getElementById('cnt-all').textContent    = total;
      document.getElementById('cnt-normal').textContent = normal;
      document.getElementById('cnt-wrong').textContent  = wrong;
      if (!document.querySelector('.card'))
        document.getElementById('content').innerHTML = '<div id="empty">暂无记录</div>';
    } else { card.style.opacity = ''; }
  } catch(_) { card.style.opacity = ''; }
}

load();
</script>
</body>
</html>"""

# ─── HTTP 服务器 ────────────────────────────────────────────────────────────────

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/api/history":
            with get_db() as conn:
                rows = conn.execute(
                    "SELECT id, timestamp, img_fname, note, messages, record_type "
                    "FROM conversations ORDER BY timestamp DESC"
                ).fetchall()
                total = len(rows)
                wrong = sum(1 for r in rows if (r["record_type"] or "normal") == "wrong")
            entries = []
            for r in rows:
                msgs = json.loads(r["messages"] or "[]")
                entries.append({
                    "id":          r["id"],
                    "timestamp":   r["timestamp"],
                    "img_fname":   r["img_fname"] or "",
                    "note":        r["note"] or "",
                    "msg_count":   len(msgs),
                    "record_type": r["record_type"] or "normal",
                    "messages":    msgs,
                })
            self.send_json({
                "entries": entries,
                "stats": {"total": total, "normal": total - wrong, "wrong": wrong},
            })

        elif self.path.startswith("/img/"):
            fname = self.path[5:].split("?")[0]
            if re.match(r'^[A-Za-z0-9._-]+$', fname):
                img_path = HIST_IMG_DIR / fname
                if img_path.exists():
                    data = img_path.read_bytes()
                    ct   = "image/png" if img_path.suffix == ".png" else "image/jpeg"
                    self.send_response(200)
                    self.send_header("Content-Type", ct)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
            self.send_response(404); self.end_headers()

        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length) or b"{}")

        if self.path == "/api/delete":
            hid = body.get("id", "")
            if re.match(r'^[A-Za-z0-9_-]+$', hid):
                with get_db() as conn:
                    row = conn.execute(
                        "SELECT img_fname FROM conversations WHERE id=?", (hid,)
                    ).fetchone()
                    conn.execute("DELETE FROM conversations WHERE id=?", (hid,))
                if row and row["img_fname"]:
                    img = HIST_IMG_DIR / row["img_fname"]
                    if img.exists():
                        img.unlink()
                self.send_json({"ok": True})
            else:
                self.send_json({"ok": False})

        else:
            self.send_response(404); self.end_headers()

# ─── 主流程 ────────────────────────────────────────────────────────────────────

def main():
    if not DB_PATH.exists():
        ctypes.windll.user32.MessageBoxW(
            0, "未找到历史数据库，请先使用截图问答功能保存一条记录。", "历史查看器", 0x40
        )
        return

    port   = find_free_port()
    server = ThreadedHTTPServer(("localhost", port), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    url = f"http://localhost:{port}"
    if CHROME_EXE:
        subprocess.Popen([CHROME_EXE, f"--app={url}", "--window-size=920,720"])
    else:
        webbrowser.open(url)

    try:
        t.join()
    except KeyboardInterrupt:
        server.shutdown()

if __name__ == "__main__":
    main()
