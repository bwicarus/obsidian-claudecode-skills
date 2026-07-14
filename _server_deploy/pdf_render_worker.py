"""pdf_render_worker.py — PyMuPDF 页面渲染的**独立 worker 模块**(2026-07-14)。

为什么要单独一个文件(而不是把函数留在 pdf_reader.py 里):
  进程池的子进程会**按模块名 import** 目标函数所在的模块。若函数留在 pdf_reader.py,
  每个子进程就得把整条 Flask app 依赖链(Flask / assistant / 各 blueprint / sqlite …)全 import 一遍
  —— 在树莓派上又慢又吃内存。这个文件**只依赖 fitz**,子进程起得又快又轻。

为什么要用进程池(而不是留在请求线程里跑):
  ① **PyMuPDF 官方明确不支持多线程**(全局 MuPDF context,官方推荐多进程)。而 webapp 的 gunicorn
     线程数刚从 8 提到 32(SSE 舱壁事故的连带处理)—— 线程不安全的暴露面被放大了 4 倍。
  ② 渲一页在 Pi 上是 0.5-2s 的**纯 CPU**。32 个线程同时渲页会把 4 核的 Pi 直接压垮,还容易 OOM。
  用有界进程池(2 个 worker)同时解决"线程安全"和"CPU 过载"两件事。

⚠ 部署:改这个文件要跟 pdf_reader.py 一起 cp 到 /home/bwicarus/webapp/ 并重启 webapp。
"""


def render_page_jpg(ap: str, page: int, w: int, cf: str) -> bool:
    """渲一页 → JPEG 原子写到 cf。True=成功。跑在**独立进程**里(与请求线程隔离)。"""
    import fitz
    from pathlib import Path

    cfp = Path(cf)
    d = None
    try:
        d = fitz.open(ap)
        if page < 1 or page > d.page_count:
            return False
        p = d[page - 1]
        zoom = w / max(1.0, p.rect.width)
        pix = p.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        tmp = cfp.with_suffix(".jpg.tmp")
        tmp.write_bytes(pix.tobytes("jpg", jpg_quality=78))
        tmp.replace(cfp)
        return True
    except Exception:
        return False
    finally:
        try:
            if d:
                d.close()
        except Exception:
            pass
