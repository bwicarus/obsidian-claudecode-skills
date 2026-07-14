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

from flask import Response, request, session

_SUBS = set()            # set[queue.Queue]:每个订阅的 SSE 连接一个队列
_LOCK = threading.Lock()

# ── 133(全站宕机事故):舱壁隔离 —— SSE **永远不许吃光线程池** ──────────────────────
# 事故:gthread worker 每条 SSE 长连接**独占 1 个线程**直到流结束(原设 300s)。客户端断线后
#   生成器往往察觉不到(小心跳写进 socket 缓冲不报错)→ 线程被"僵尸流"钉死整整 5 分钟;
#   而前端 EventSource 每 3s 重连一次 → 僵尸以每 3s 一个的速度堆积 →
#   **--threads 8 在 ~24s 内被自己人占满** → 全站(连 /login)零响应 → 前端更狂重连 = 自噬正反馈。
#   py-spy 实证:8/8 线程全停在 reader_events.gen() 的 q.get()。
# 修:① 总量闸 + 每用户闸,超了立刻 503(EventSource 3s 后自己重来,零成本)——保证任何时候
#     都有 threads-_SSE_MAX_TOTAL 个线程给普通请求用;② 流寿命 300s→120s,僵尸最多躺 2 分钟。
# ⚠ 改 --threads 时同步复查 _SSE_MAX_TOTAL,必须**显著小于**线程数,否则舱壁形同虚设。
_SSE_MAX_TOTAL = 12      # 全局并发 SSE 上限(对 --threads 32 → 至少留 20 个线程给普通请求)
_SSE_MAX_PER_UID = 4     # 单用户上限:一个人开一堆标签页也压不垮别人(本项目单用户,兼防自己)
_SSE_LIFE_S = 120        # 单条流寿命:到点主动收(EventSource 无缝重连),僵尸驻留上限
_SSE_N = {}              # uid → 当前在线流数
_SSE_TOTAL = 0


def _sse_uid():
    # ⚠ 本站 session 的用户键是 **user_id**(全站 25 处)。曾误写 session["user"]/["uid"] → 全部取不到 →
    #   回落 request.remote_addr,而请求都经 nginx 反代 = 恒为 127.0.0.1 → "每用户 4 条"实际退化成
    #   **全站共 4 条**、总闸 12 成了死代码(压测日志 uid=127.0.0.1 本人=4/4 就是证据)。
    try:
        return str(session.get("user_id") or session.get("username") or request.remote_addr or "?")
    except Exception:
        return "?"


def publish(kind: str, file: str, uid, extra: dict | None = None):
    """向所有订阅者推一条变更事件。满队列(maxsize=128)的病态连接静默丢(15s 心跳会让死连接很快被回收)。
    extra:附加字段直接并入事件(如 client-action 的 {"action":{fn,args}},MCP 遥控用)。"""
    ev = {"kind": kind, "file": file, "uid": (str(uid) if uid is not None else None), "t": int(time.time())}
    if extra:
        ev.update(extra)
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
        """阅读器变更事件流(SSE):客户端订阅,服务端在墨迹/正文保存后推 {kind,file,uid}。EventSource 自带断线重连。
        ⚠ 舱壁(133):每条流独占 1 个 gthread 线程 → 必须上并发闸,详见文件头。"""
        global _SSE_TOTAL
        uid = _sse_uid()
        # ① 上闸:名额在**建流之前**占掉(占不到就 503,连线程都不占用)
        with _LOCK:
            if _SSE_TOTAL >= _SSE_MAX_TOTAL or _SSE_N.get(uid, 0) >= _SSE_MAX_PER_UID:
                import sys
                sys.stderr.write(f"[sse] 拒绝(舱壁):uid={uid} 总={_SSE_TOTAL}/{_SSE_MAX_TOTAL} "
                                 f"本人={_SSE_N.get(uid, 0)}/{_SSE_MAX_PER_UID}\n")
                r = Response("retry: 5000\n\n: busy\n\n", mimetype="text/event-stream", status=503)
                r.headers["Retry-After"] = "5"
                return r
            _SSE_TOTAL += 1
            _SSE_N[uid] = _SSE_N.get(uid, 0) + 1
            q = queue.Queue(maxsize=128)
            _SUBS.add(q)

        # ⚠ 释放必须**幂等且两条路都兜**:生成器若从未被迭代过(客户端在响应发出前就断了),
        #   Python 的 generator.close() **不会执行 finally**(帧还没启动)→ 名额永久泄漏 → 闸门迟早自锁死。
        #   故:gen 的 finally 走一条,Werkzeug 的 call_on_close 再兜一条(响应被关闭必调,不管迭代没迭代)。
        _done = {"v": False}

        def _release():
            global _SSE_TOTAL
            with _LOCK:
                if _done["v"]:
                    return
                _done["v"] = True
                _SUBS.discard(q)
                _SSE_TOTAL -= 1
                n = _SSE_N.get(uid, 1) - 1
                if n > 0:
                    _SSE_N[uid] = n
                else:
                    _SSE_N.pop(uid, None)

        def gen():
            try:
                yield "retry: 3000\n\n"          # EventSource 重连间隔 3s
                yield ": connected\n\n"
                t0 = time.time()
                while time.time() - t0 < _SSE_LIFE_S:   # 到点主动收(EventSource 无缝重连)→ 僵尸流驻留上限
                    try:
                        ev = q.get(timeout=15)
                        yield "event: change\ndata: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
                    except queue.Empty:
                        yield ": ka\n\n"          # 心跳(保活 + 检测死连接)
            except GeneratorExit:
                pass
            finally:
                _release()   # ② 释放名额:GeneratorExit / 客户端断开 / 到寿,三条路都过这里
        resp = Response(gen(), mimetype="text/event-stream")
        resp.headers["Cache-Control"] = "no-cache, no-transform"
        resp.headers["X-Accel-Buffering"] = "no"
        resp.call_on_close(_release)   # 兜底:生成器没被迭代过也一定释放(见上)
        return resp
