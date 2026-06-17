"""PC 侧常驻公式 OCR 服务(在 4090 的 venv-pix 里跑)。Pi webapp 的「本地公式 OCR」按钮经 Tailscale 调它。

启动(PC,venv-pix):
  set FORMULA_OCR_KEY=<可选密钥>
  C:\\Users\\bwica\\formula-ocr\\venv-pix\\Scripts\\python.exe scripts\\formula_ocr_server.py
默认监听 0.0.0.0:8765(Tailscale 内 Pi 可达)。启动即预载 pix2tex 模型(常驻,后续请求秒回)。

路由:
  GET  /health            → {ok, model_loaded}
  POST /ocr  {crops:[{idx, png_b64}], key?} → {ok, results:[{idx, latex}]}
"""
import os, io, base64, threading
from flask import Flask, request, jsonify
from PIL import Image

app = Flask(__name__)
_model = None
_lock = threading.Lock()
API_KEY = os.environ.get("FORMULA_OCR_KEY", "")
PORT = int(os.environ.get("FORMULA_OCR_PORT", "8765"))


def _get_model():
    global _model
    if _model is None:
        from pix2tex.cli import LatexOCR
        _model = LatexOCR()
    return _model


@app.get("/health")
def health():
    return jsonify({"ok": True, "model_loaded": _model is not None})


@app.post("/ocr")
def ocr():
    body = request.get_json(force=True, silent=True) or {}
    if API_KEY and (body.get("key") or request.headers.get("X-Key")) != API_KEY:
        return jsonify({"ok": False, "error": "auth"}), 401
    crops = body.get("crops") or []
    out = []
    with _lock:                      # pix2tex 模型非线程安全 → 串行
        m = _get_model()
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
    print("preloading pix2tex model ...", flush=True)
    _get_model()
    print(f"formula-ocr server ready on 0.0.0.0:{PORT}", flush=True)
    app.run(host="0.0.0.0", port=PORT, threaded=True)
