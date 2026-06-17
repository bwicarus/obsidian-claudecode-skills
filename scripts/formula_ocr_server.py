"""PC 侧常驻公式 OCR 服务(在 4090 的 venv-pix 里跑)。Pi webapp 的「本地公式 OCR」按钮经 Tailscale 调它。

启动(PC,venv-pix):
  set FORMULA_OCR_KEY=<可选密钥>
  C:\\Users\\bwica\\formula-ocr\\venv-pix\\Scripts\\python.exe scripts\\formula_ocr_server.py
默认监听 0.0.0.0:8765(Tailscale 内 Pi 可达)。启动即预载 pix2tex 模型(常驻,后续请求秒回)。

路由:
  GET  /health            → {ok, model_loaded}
  POST /ocr  {crops:[{idx, png_b64}], key?} → {ok, results:[{idx, latex}]}
"""
import os, io, gc, time, base64, threading
from flask import Flask, request, jsonify
from PIL import Image

app = Flask(__name__)
_model = None
_lock = threading.Lock()
_last_used = [0.0]                       # 末次用模型时刻 → 闲置卸载用
API_KEY = os.environ.get("FORMULA_OCR_KEY", "")
PORT = int(os.environ.get("FORMULA_OCR_PORT", "8765"))
# 惰性加载 + 闲置自动卸载:启动不预载;首次 /ocr 才加载模型(~15s);闲置 IDLE_UNLOAD_SEC 秒(默认 5min)
# 没人点 OCR 就把模型从内存/显存卸掉 → 回到几乎零占用(仅 Flask)。下次点 OCR 再重新加载。设 0 = 关闭自动卸载(常驻)。
IDLE_UNLOAD_SEC = int(os.environ.get("FORMULA_OCR_IDLE_SEC", "300"))


def _get_model():
    global _model
    if _model is None:
        from pix2tex.cli import LatexOCR
        _model = LatexOCR()
    _last_used[0] = time.time()
    return _model


def _unload():
    global _model
    with _lock:
        if _model is None:
            return
        _model = None
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
        print("[idle] model unloaded, freed RAM/VRAM", flush=True)


def _idle_watch():
    while True:
        time.sleep(30)
        if IDLE_UNLOAD_SEC > 0 and _model is not None and (time.time() - _last_used[0]) > IDLE_UNLOAD_SEC:
            _unload()


@app.get("/health")
def health():
    return jsonify({"ok": True, "model_loaded": _model is not None, "idle_unload_sec": IDLE_UNLOAD_SEC})


@app.post("/ocr")
def ocr():
    body = request.get_json(force=True, silent=True) or {}
    if API_KEY and (body.get("key") or request.headers.get("X-Key")) != API_KEY:
        return jsonify({"ok": False, "error": "auth"}), 401
    crops = body.get("crops") or []
    out = []
    with _lock:                      # pix2tex 模型非线程安全 → 串行
        m = _get_model()
        _last_used[0] = time.time()
        for c in crops:
            latex = None
            try:
                img = Image.open(io.BytesIO(base64.b64decode(c["png_b64"]))).convert("RGB")
                latex = m(img)
            except Exception as e:
                print("ocr err", c.get("idx"), repr(e), flush=True)
            out.append({"idx": c.get("idx"), "latex": latex})
    return jsonify({"ok": True, "results": out})


if __name__ == "__main__":
    # 惰性加载:不在启动时预载模型 → 空闲(没人点 OCR)时几乎零占用(仅 Flask ~60MB,0 显存)。
    # 首次 /ocr 才加载(~15s),闲置 IDLE_UNLOAD_SEC 后自动卸载释放内存/显存。
    threading.Thread(target=_idle_watch, daemon=True).start()
    print(f"formula-ocr server ready on 0.0.0.0:{PORT} (lazy-load, idle-unload={IDLE_UNLOAD_SEC}s)", flush=True)
    app.run(host="0.0.0.0", port=PORT, threaded=True)
