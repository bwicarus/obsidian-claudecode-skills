#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把设置面板挂起来看（见同目录 README.md）。

每次启动都从 _server_deploy/static/pdf/ 重新拷贝，所以看到的一定是当前源码。
"""
from __future__ import annotations

import functools
import http.server
import os
import shutil
import socketserver
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
PDF = os.path.join(ROOT, "_server_deploy", "static", "pdf")
ASSETS = ("rc-ui.js", "rc-settings.js", "pdf-styles.css")
PORT = 8931

PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>设置面板预览</title>
<link rel="stylesheet" href="pdf-styles.css">
<style>body{margin:0;background:#101014;font-family:-apple-system,system-ui,sans-serif}</style>
</head><body>
<div id="lang-checks" style="display:none"></div>
<script src="rc-ui.js"></script>
<script src="rc-settings.js"></script>
<script>
// 最小桩：只为把面板建出来看布局/配色，不接真实数据源。
window.RC = window.RC || {};
RC.toast = function (t) { console.log('[toast]', t); };
RC.assistant = RC.assistant || {};
RC.assistant.renderModelSettings = function (host) {
  host.innerHTML = '<div class="ams-sub">（模型配置表：真实环境由 rc-assistant 渲染）</div>';
};
RC.grammar = RC.grammar || { renderTrackList: function () {} };
RC.stickynote = RC.stickynote || { refreshStyle: function () {} };
window.addEventListener('load', function () {
  try {
    RC.ui.inject();
    RC.settings.open({ host: 'pdf', ids: { mask: 'settings-mask', langChecks: 'lang-checks' },
                       keys: { tab: 'preview-set-tab' } });
    document.title = '设置面板预览 · 已打开';
  } catch (e) {
    document.body.insertAdjacentHTML('beforeend',
      '<pre style="color:#ff8080;padding:16px;white-space:pre-wrap">打开失败：'
      + (e && e.stack || e) + '</pre>');
  }
});
</script>
</body></html>
"""


def main() -> None:
    work = tempfile.mkdtemp(prefix="bw-ui-preview-")
    for name in ASSETS:
        shutil.copy2(os.path.join(PDF, name), os.path.join(work, name))
    with open(os.path.join(work, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(PAGE)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=work)
    print("设置面板预览: http://127.0.0.1:%d/index.html" % PORT)
    print("（资源取自 %s，改代码改那边）" % PDF)
    with socketserver.TCPServer(("127.0.0.1", PORT), handler) as srv:
        srv.serve_forever()


if __name__ == "__main__":
    main()
