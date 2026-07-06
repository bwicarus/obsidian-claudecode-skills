"""reader_events.py — 阅读器实时事件总线(SSE pub/sub)。

自建页墨迹/正文/收藏夹结构一存 → publish「变了」给其它打开着的客户端 → 活跃侧收到即重拉+就地重渲(~1s);
不活跃侧忽略(后端已更新,回来 visibility 再同步)。webapp = 单 worker gthread(见 systemd)→ 进程内
pub/sub 直接通,无需跨进程总线;每条长连接占 1 gthread、5min 自动回收(EventSource 自动重连、连接常新)。

2026-07-06 结构拆分第 1 刀(体检 structure:零业务依赖,只用 stdlib + Flask Response)。
用法(pdf_reader.py):
    from reader_events import publish as _reader_publish, register_reader_events
    register_reader_events(bp)   # 挂 /api/reader-events(bp 前缀 /pdf)
事件形状:{kind, file, uid, t};kind ∈ ink/text/userpage-del/fav-changed/fav-built(见发布点)。
部署:cp 本文件到 /home/bwicarus/webapp/(跟 pdf_reader.py 同目录)+ restart webapp。
"""
import json
import queue
import threading
import time

from flask import Response

_SUBS = set()            # set[queue.Queue]:每个订阅的 SSE 连接一个队列
_LOCK = threading.Lock()


def publish(kind: str, file: str, uid):
    """向所有订阅者推一条变更事件。满队列(maxsize=128)的病态连接静默丢(15s 心跳会让死连接很快被回收)。"""
    ev = {"kind": kind, "file": file, "uid": (str(uid) if uid is not None else None), "t": int(time.time())}
    with _LOCK:
        subs = list(_SUBS)
    for q in subs:
        try:
            q.put_nowait(ev)
        except Exception:
            pass


def register_reader_events(bp):
    @bp.route("/api/reader-events")
    def pdf_reader_events():
        """阅读器变更事件流(SSE):客户端订阅,服务端在墨迹/正文保存后推 {kind,file,uid}。EventSource 自带断线重连。"""
        q = queue.Queue(maxsize=128)
        with _LOCK:
            _SUBS.add(q)

        def gen():
            try:
                yield "retry: 3000\n\n"          # EventSource 重连间隔 3s
                yield ": connected\n\n"
                t0 = time.time()
                while time.time() - t0 < 300:    # 5min 回收连接(gunicorn timeout 600 前),EventSource 自动重连、连接常新
                    try:
                        ev = q.get(timeout=15)
                        yield "event: change\ndata: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
                    except queue.Empty:
                        yield ": ka\n\n"          # 心跳(保活 + 检测死连接)
            except GeneratorExit:
                pass
            finally:
                with _LOCK:
                    _SUBS.discard(q)
        resp = Response(gen(), mimetype="text/event-stream")
        resp.headers["Cache-Control"] = "no-cache, no-transform"
        resp.headers["X-Accel-Buffering"] = "no"
        return resp
