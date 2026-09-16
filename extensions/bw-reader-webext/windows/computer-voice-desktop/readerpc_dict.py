# -*- coding: utf-8 -*-
"""词典服务（Windows 本地）—— 把 Pi 上的查词能力搬过来（2026-09-17 用户拍板「迁到 windows」）。

背景：日语/英语词典一直靠 Pi 的 webapp。Pi 退出阅读线之后那台机器上的 webapp 被停用，
而 App 的词典请求（`/pdf/api/dict*`）经桥打到 Windows、桥又没有这些路由 → 404，
于是点词只剩一张「只有翻译」的降级卡片（用户 2026-09-17 报）。

做法不是重写，而是**原样跑 `_server_deploy/pdf_reader.py` 的蓝图** ——
那些路由只是薄包装，真正的逻辑在 `scripts/vocab/dict_sources.py`，
而它的数据根由 `CLAUDE_PROJECT` 决定，把数据搬到 Windows 就地跑即可。实测零改动通过。

数据（已从 Pi 搬来，约 950MB）：
  <root>/data/ecdict.db        340 万条英文词
  <root>/data/tanaka.db        14.8 万日语例句
  <root>/data/kanjidic.json    12633 个汉字
  <root>/state/dict-cache/     21390 条已生成的词条缓存
  <root>/state/jp-vocab.json   生词掌握度

只监听 127.0.0.1：对外由桥转发，别把词典直接暴露到 Tailscale 上。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

#: 数据根。默认放在 BWReader 的本地目录下 —— 850MB 的 ecdict 不该进 git 仓库。
DEFAULT_ROOT = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "BWReader" / "dict"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("BW_DICT_PORT", "43134"))


def _repo_root() -> Path:
    """找到带 _server_deploy 的仓库根。装机后代码与数据分开放，别硬编码。"""
    env = os.environ.get("BW_READER_REPO")
    if env and (Path(env) / "_server_deploy" / "pdf_reader.py").exists():
        return Path(env)
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "_server_deploy" / "pdf_reader.py").exists():
            return parent
    raise RuntimeError("找不到 _server_deploy/pdf_reader.py；用 BW_READER_REPO 指一下")


def build_app(root: Path | None = None):
    """造一个只挂词典路由的 Flask app。"""
    root = Path(root or os.environ.get("CLAUDE_PROJECT") or DEFAULT_ROOT)
    # dict_sources / pdf_reader 都按这个环境变量找 data/ 与 state/
    os.environ["CLAUDE_PROJECT"] = str(root)
    repo = _repo_root()
    for sub in ("_server_deploy", "scripts", "scripts/vocab"):
        p = str(repo / sub)
        if p not in sys.path:
            sys.path.insert(0, p)

    from flask import Flask
    from pdf_reader import register_pdf_reader

    app = Flask(__name__)
    app.config["JSON_AS_ASCII"] = False
    register_pdf_reader(app)

    @app.get("/healthz")
    def _healthz():
        from flask import jsonify
        return jsonify({"ok": True, "root": str(root),
                        "ecdict": (root / "data" / "ecdict.db").exists(),
                        "cache": len(list((root / "state" / "dict-cache").glob("*")))
                        if (root / "state" / "dict-cache").is_dir() else 0})

    return app


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="ReaderPC 本地词典服务")
    ap.add_argument("--root", default=None, help="数据根（含 data/ 与 state/）")
    ap.add_argument("--port", type=int, default=LISTEN_PORT)
    ap.add_argument("--routes", action="store_true", help="只打印挂上的词典路由后退出")
    a = ap.parse_args(argv)
    app = build_app(a.root)
    if a.routes:
        for r in sorted(app.url_map.iter_rules(), key=lambda x: str(x)):
            if "dict" in str(r) or "healthz" in str(r):
                print("%-28s %s" % (str(r), ",".join(sorted(r.methods - {"HEAD", "OPTIONS"}))))
        return 0
    print("词典服务监听 http://%s:%d" % (LISTEN_HOST, a.port), flush=True)
    app.run(host=LISTEN_HOST, port=a.port, threaded=True, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
