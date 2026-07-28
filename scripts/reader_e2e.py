#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PDF 阅读器 E2E 冒烟(Playwright,Pi 本机跑,~40s)。改阅读器必跑,跑过才部署。

断言链:登录(铸 cookie)→ 开 応用情報 p37 → charBoxes 就绪 → 真实点击「議」
(视觉坐标,含 crop translate)→ 选中=議事 → 词典自动弹出 → 书架浮层开/有书/关。
任何一步失败 exit 1。来历:2026-06-10「右缘点不中」盲修两轮,上真浏览器一轮见底
(pdf-reader.md §36),固化成回归套件。

用法:  python3 scripts/reader_e2e.py [--url-base https://bwicarus.taile44d0c.ts.net]
依赖:  pip install playwright && python -m playwright install chromium(Pi 已装)
"""
import os
import sys
import time
import argparse

BASE = "https://bwicarus.taile44d0c.ts.net"
BOOK = "%E8%B5%84%E6%BA%90%2Fbooks%2F%E5%BF%9C%E7%94%A8%E6%83%85%E5%A0%B1%E6%8A%80%E8%A1%93%E8%80%85.pdf"
DEPLOYMENT_PROBE_HEADER = "X-BW-Reader-Deployment-Probe"

def die(msg):
    print(f"❌ E2E FAIL: {msg}")
    sys.exit(1)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url-base", default=BASE)
    a = ap.parse_args()

    os.environ.setdefault("WEBAPP_DATA", "/home/bwicarus/webapp/data")
    sys.path.insert(0, "/home/bwicarus/webapp")
    for line in open("/home/bwicarus/webapp/.env"):
        if line.startswith("SECRET_KEY="):
            os.environ["SECRET_KEY"] = line.strip().split("=", 1)[1]
    from app import app
    from flask.sessions import SecureCookieSessionInterface
    cookie = SecureCookieSessionInterface().get_signing_serializer(app).dumps(
        {"user_id": 1, "username": "bwicarus"})

    from playwright.sync_api import sync_playwright
    url = f"{a.url_base}/pdf/view?file={BOOK}&page=37"
    errs = []
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-sandbox"])
        # 部署事务会在此 E2E 前后逐摘要核对 KG/派生状态。页面加载仍需
        # 真正走完插图路由，但缺失 badge 的懒补不能把一次只读健康探测
        # 变成生产写入，否则会触发 fail-closed 回滚。
        ctx = b.new_context(
            viewport={"width": 1180, "height": 820},
            extra_http_headers={DEPLOYMENT_PROBE_HEADER: "1"},
        )
        ctx.add_cookies([{"name": "session", "value": cookie, "url": a.url_base}])
        pg = ctx.new_page()
        pg.on("pageerror", lambda e: errs.append(str(e)[:120]))
        t0 = time.time()
        pg.goto(url, wait_until="domcontentloaded", timeout=60000)

        # 1) p37 charBoxes 就绪
        try:
            pg.wait_for_function(
                """() => { const w = document.querySelector('.page-wrap[data-page-num="37"]');
                           return w && w.__charBoxes && w.__charBoxes.length > 100; }""",
                timeout=60000)
        except Exception:
            die("p37 charBoxes 60s 未就绪(loadPdf/分批占位/页图链路坏了?)")
        print(f"✓ 打开+chars 就绪 {(time.time()-t0):.1f}s")

        pg.evaluate("""() => document.querySelector('.page-wrap[data-page-num="37"]').scrollIntoView({block:'start'})""")
        time.sleep(2.5)

        # 2) 视觉坐标点「議」(右缘词 = 历史死区;含 crop translate)
        d = pg.evaluate("""() => {
          const w = document.querySelector('.page-wrap[data-page-num="37"]');
          const cl = w.querySelector('.char-layer');
          if (!cl) return null;
          const tr = getComputedStyle(cl).transform;
          const m = new DOMMatrixReadOnly(tr === 'none' ? '' : tr);
          const cb = w.__charBoxes; let gi = -1;
          for (let i = 0; i < cb.length - 1; i++) {
            if (cb[i].c === '議' && cb[i+1].c === '事') { gi = i; break; }
          }
          if (gi < 0) return null;
          const c = cb[gi], r = w.getBoundingClientRect();
          const el = document.elementFromPoint(r.left + c.left + c.width/2 + m.e, r.top + c.top + c.height/2 + m.f);
          return { x: r.left + c.left + c.width/2 + m.e, y: r.top + c.top + c.height/2 + m.f,
                   atCl: el === cl || (el && cl.contains(el)) };
        }""")
        if not d:
            die("p37 找不到 議事 / char-layer 缺失")
        if not d["atCl"]:
            die("議 的视觉位置上不是 char-layer(去边叠层覆盖回归! 见 pdf-reader.md §36⑤)")
        pg.mouse.click(d["x"], d["y"])
        time.sleep(0.3)
        prev = pg.evaluate("() => document.querySelector('#sel-preview')?.textContent || ''")
        if "議事" not in prev:
            die(f"点击議未选中(preview={prev[:20]!r};命中/坐标链路回归)")
        print("✓ 右缘点词选中 議事")

        # 3) 词典弹出(快词直弹或慢词自动弹,≤12s)
        pop = ""
        for _ in range(24):
            time.sleep(0.5)
            pop = pg.evaluate("""() => { const p = document.getElementById('word-pop');
                return (p && p.style.display === 'block') ? p.textContent.slice(0, 40) : ''; }""")
            if pop:
                break
        if not pop:
            diag = pg.evaluate("""async () => {
              let quick = null;
              try { quick = await (await fetch('/pdf/api/dict-quick?word=' + encodeURIComponent('議事') + '&ctx=')).json(); }
              catch (e) { quick = {error: String(e)}; }
              const p = document.getElementById('word-pop');
              return {
                rootMarker: document.documentElement.dataset.bwReaderExtension || '',
                handoff: !!window.__BW_EXTENSION_HANDOFF__,
                uiShared: !!window.__uiShared,
                adapter: !!window.PdfAdapter,
                wordpop: !!(window.RC && RC.wordpop),
                popDisplay: p ? getComputedStyle(p).display : 'missing',
                popText: p ? p.textContent.slice(0, 100) : '',
                quick: quick,
              };
            }""")
            die("点词 12s 无词典弹框(dict-quick/自动弹出回归); diag=%s" % str(diag)[:500])
        print(f"✓ 词典弹出: {pop[:24]}…")

        # 4) 返回书架入口(2026-06-18 阅读器内浮层书单退役 → goPdfList 改 location.href='/pdf/';
        #    旧 E2E 在等已删除的 #bookshelf-ov 故永远红,改测真实存在的返回入口)
        if not pg.evaluate("() => typeof window.goPdfList === 'function'"):
            die("返回书架入口 goPdfList 丢失")
        print("✓ 返回书架入口 goPdfList→/pdf/ 就绪")

        # 5) 助手模型设置端点(三维 后端/型号/深度;登录态下应回 ok + 含 orchestrator/summarize/vision)
        prefs = pg.evaluate("""async () => { try { const r = await fetch('/api/assistant/action-prefs');
            return await r.json(); } catch (e) { return { error: String(e) }; } }""")
        if not (prefs and prefs.get('ok') and prefs.get('actions') and 'orchestrator' in prefs['actions']):
            die("/api/assistant/action-prefs 异常: %s" % str(prefs)[:80])
        print("✓ 模型设置端点 ok(%s)" % ",".join(prefs['actions'].keys()))

        b.close()
    real_errs = [e for e in errs if "FILE_REL" not in e]   # 已知模板 ink 历史问题,单独治
    if real_errs:
        die(f"页面 JS 错误: {real_errs[:3]}")
    print("✅ E2E 全过")

if __name__ == "__main__":
    main()
