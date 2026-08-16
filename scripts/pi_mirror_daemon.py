"""把 Pi 上的阅读器数据镜像到本机，事件推送驱动（架构第 17 条）。

为什么是推不是拉：延迟的来源从来不是"经过了 Pi"，是"拉还是推"。拉取模式下
Windows 侧的副本滞后 0–15 分钟；订阅 Pi 既有的 SSE 事件总线（/pdf/api/reader-events，
高亮/便签/阅读位置的保存点都会广播一声"变了"）之后，滞后降到秒以内。

三个刻意的选择，与 kg_mirror 同一套哲学：

  · **事件只当铃铛用**。事件不带内容，收到后按 (域, 书) 去重拉对应资源 ——
    错过事件不丢数据，只是晚一点；周期性追赶（和重连后的追赶）兜住一切遗漏。
    所以这个守护挂了也只是退化回拉取模式，不会出错。

  · **失败必须出声**。SSE 断线、拉取失败都写进 manifest 的状态字段并打 stderr。
    《silent-failure-lessons》第五条：无控制台设备上沉默等于不可诊断 ——
    这里的"控制台"就是 manifest 文件，读镜像的人第一眼看它。

  · **同步中不许下否定性结论**（第 17 条铁律）。manifest 记录每个域的
    syncedAt 与 sse 连接状态；读取方（scripts/lib/pi_mirror.py）强制带新鲜度，
    刚开机追赶没完时查不到只能说"还在同步"，不能说"你没划过"。

服务端舱壁会每 ~5 分钟回收 SSE 长连接（防僵尸流吃光线程池，见 reader_events.py），
所以正常运行就是"连上 → 几分钟后被回收 → 立刻重连"的循环；把 EOF 当日常，
不当故障。每次重连后做一次轻量追赶（positions 一个小 JSON）补上断线窗口。

用法：
  python pi_mirror_daemon.py --once     # 只做一轮追赶（开机/调试用）
  python pi_mirror_daemon.py            # 守护：追赶 + 订阅事件持续镜像
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# 复用 KG 同步的凭据查找（env → 本检出 .env → C:\claude\.env）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from sync_kg_from_pi import _env_file_value, project_root  # noqa: E402
import sync_kg_from_pi  # noqa: E402

CONTRACT = "reader-pi-mirror/1"
MANIFEST_NAME = "_mirror.json"
DEFAULT_TIMEOUT = 30
SSE_READ_TIMEOUT = 40           # 服务端 15s 一次心跳；40s 没动静=连接已死
DEBOUNCE_SECONDS = 2.0          # 同一 (域,书) 的连环事件合并成一次拉取
KG_REFRESH_SECONDS = 30 * 60    # KG 走修订号短路，周期重查很便宜
CATCHUP_RECENT_BOOKS = 10       # 追赶时预拉最近读过的前 N 本，其余按事件懒拉

# 事件 kind → (镜像域, 拉取端点)。端点都是既有 GET，?file= 语义一致。
KIND_TO_DOMAIN = {
    "hl": ("hl", "/pdf/api/highlights"),
    "epub-hl": ("epub-hl", "/pdf/api/epub-highlights"),
    "note": ("note", "/pdf/api/notes"),
    "ink": ("ink", None),      # 墨迹体量大且分析用不到矢量笔画，只记时间戳不拉内容
    "text": ("text", None),    # 自建页正文同理：记"变过"，用到时再说
    "pos": ("pos", None),      # 位置走整份 positions.json，单独处理
    "error-report": ("error-report", None),   # 问题报告:按 id 增量拉,单独处理
}


def mirror_dir() -> Path:
    return project_root() / "state" / "pi-mirror"


def _book_key(rel: str) -> str:
    return hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]


def _now() -> int:
    return int(time.time())


# ── manifest：镜像必须能回答"我这份是什么时候的" ──────────────────────────────

def load_manifest(directory: Path) -> dict:
    path = directory / MANIFEST_NAME
    if not path.is_file():
        return {"contract": CONTRACT, "sse": {}, "books": {}, "positionsSyncedAt": 0}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict) and "books" in value:
            return value
    except Exception:
        pass
    return {"contract": CONTRACT, "sse": {}, "books": {}, "positionsSyncedAt": 0}


def save_manifest(directory: Path, manifest: dict) -> None:
    manifest["contract"] = CONTRACT
    manifest["updatedAtEpochSeconds"] = _now()
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / (MANIFEST_NAME + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, directory / MANIFEST_NAME)


def _set_sse_status(manifest: dict, status: str, error: str | None = None) -> None:
    sse = manifest.setdefault("sse", {})
    sse["status"] = status
    sse["changedAtEpochSeconds"] = _now()
    if error:
        sse["lastError"] = error[:300]
    elif status == "connected":
        sse.pop("lastError", None)


# ── HTTP ─────────────────────────────────────────────────────────────────────

class PiClient:
    def __init__(self, base: str, token: str):
        self.base = base
        self.token = token

    def get_json(self, path: str, params: dict | None = None,
                 timeout: int = DEFAULT_TIMEOUT):
        url = urllib.parse.urljoin(self.base, path)
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, method="GET")
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("Accept", "application/json")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def open_sse(self, path: str):
        url = urllib.parse.urljoin(self.base, path)
        request = urllib.request.Request(url, method="GET")
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("Accept", "text/event-stream")
        return urllib.request.urlopen(request, timeout=SSE_READ_TIMEOUT)


# ── SSE 解析（纯函数，可测）────────────────────────────────────────────────────

def parse_sse_events(lines):
    """把 SSE 行流解析成事件 dict 流。只认 data: 行；心跳(注释/空事件)产出 None
    让调用方知道连接还活着。"""
    data_parts: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r\n") if isinstance(raw, str) else raw.decode(
            "utf-8", "replace").rstrip("\r\n")
        if line == "":
            if data_parts:
                joined = "\n".join(data_parts)
                data_parts = []
                try:
                    yield json.loads(joined)
                except Exception:
                    yield None   # 解析不动也要出声"有动静"，供心跳判活
            else:
                yield None
            continue
        if line.startswith(":"):
            yield None           # 服务端心跳注释
            continue
        if line.startswith("data:"):
            data_parts.append(line[5:].lstrip())
        # event:/id:/retry: 行不需要——服务端只发 change 事件


# ── 去抖聚合（纯函数状态机，可测）──────────────────────────────────────────────

class PullPlanner:
    """把连环事件合并成待拉清单。同一 (域, 书) 在 DEBOUNCE_SECONDS 内只拉一次。"""

    def __init__(self, debounce: float = DEBOUNCE_SECONDS):
        self.debounce = debounce
        self._pending: dict[tuple, float] = {}   # (domain, rel) -> 到期时刻

    def note_event(self, event: dict, now: float) -> None:
        kind = str(event.get("kind") or "")
        if kind not in KIND_TO_DOMAIN:
            return                                  # 未知/无关事件：忽略
        domain, _ = KIND_TO_DOMAIN[kind]
        rel = str(event.get("file") or "")
        if domain in ("pos", "error-report"):
            self._pending[(domain, "")] = now + self.debounce
        elif rel:
            self._pending[(domain, rel)] = now + self.debounce

    def due(self, now: float) -> list[tuple]:
        ready = [k for k, t in self._pending.items() if t <= now]
        for k in ready:
            del self._pending[k]
        return ready

    def pending_count(self) -> int:
        return len(self._pending)


# ── 各域拉取 ──────────────────────────────────────────────────────────────────

def pull_positions(client: PiClient, directory: Path, manifest: dict) -> None:
    payload = client.get_json("/pdf/api/reading-pos")
    positions = payload.get("positions")
    if not isinstance(positions, dict):
        raise RuntimeError("reading-pos 响应缺 positions")
    tmp = directory / f"positions.json.tmp-{os.getpid()}"
    tmp.write_text(json.dumps(positions, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, directory / "positions.json")
    manifest["positionsSyncedAt"] = _now()


def pull_book_domain(client: PiClient, directory: Path, manifest: dict,
                     domain: str, rel: str) -> None:
    endpoint = KIND_TO_DOMAIN.get(domain, (None, None))[1]
    entry = manifest.setdefault("books", {}).setdefault(
        _book_key(rel), {"rel": rel, "domains": {}})
    entry["rel"] = rel
    dom = entry.setdefault("domains", {}).setdefault(domain, {})
    if endpoint is None:
        # ink/text：只记"变过"。内容大且当前没有读取方；真要读时再扩。
        dom["changedAt"] = _now()
        dom["status"] = "noted-only"
        return
    payload = client.get_json(endpoint, {"file": rel})
    book_dir = directory / "books" / _book_key(rel)
    book_dir.mkdir(parents=True, exist_ok=True)
    (book_dir / "meta.json").write_text(
        json.dumps({"rel": rel}, ensure_ascii=False), encoding="utf-8")
    tmp = book_dir / f"{domain}.json.tmp-{os.getpid()}"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, book_dir / f"{domain}.json")
    dom["syncedAt"] = _now()
    dom["status"] = "ok"
    dom.pop("lastError", None)


def pull_error_reports(client: PiClient, directory: Path, manifest: dict) -> None:
    """把新的问题报告拉进 error-reports/。「特定文件夹」就是它——调试侧 AI 直接读。"""
    since = int(manifest.get("errorReportsSyncedAt") or 0)
    # 往回多要 5 分钟:上一轮拉取与报告落盘之间的时钟缝隙不该丢报告
    payload = client.get_json("/pdf/api/error-reports",
                              {"since": max(0, since - 300)})
    out_dir = directory / "error-reports"
    for meta in payload.get("reports") or []:
        rid = str(meta.get("id") or "")
        if not rid or meta.get("error"):
            continue
        target = out_dir / f"{rid}.json"
        if target.is_file():
            continue
        full_payload = client.get_json(f"/pdf/api/error-report/{rid}")
        report = full_payload.get("report")
        if not isinstance(report, dict):
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp = out_dir / f"{rid}.json.tmp-{os.getpid()}"
        tmp.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, target)
        print(f"  ⚑ 新问题报告 {rid}: {str(report.get('what') or '')[:60]}",
              file=sys.stderr)
    manifest["errorReportsSyncedAt"] = _now()


def catch_up(client: PiClient, directory: Path, manifest: dict,
             *, full: bool) -> None:
    """追赶：positions 必拉（小）；full 时另拉最近 N 本书的三个域。"""
    pull_positions(client, directory, manifest)
    try:
        pull_error_reports(client, directory, manifest)
    except Exception as error:
        # 端点可能还没部署(灰度期);出声但不拦追赶
        print(f"  ! 问题报告拉取: {type(error).__name__}: {error}", file=sys.stderr)
    if not full:
        return
    positions = json.loads(
        (directory / "positions.json").read_text(encoding="utf-8"))
    recent = sorted(positions.items(),
                    key=lambda kv: -(kv[1].get("ts") or 0))[:CATCHUP_RECENT_BOOKS]
    for rel, meta in recent:
        domains = (("epub-hl", "note") if meta.get("kind") == "epub"
                   else ("hl", "note"))
        for domain in domains:
            try:
                pull_book_domain(client, directory, manifest, domain, rel)
            except Exception as error:
                # 单本失败不拦整轮追赶，但要在 manifest 里出声
                entry = manifest.setdefault("books", {}).setdefault(
                    _book_key(rel), {"rel": rel, "domains": {}})
                dom = entry.setdefault("domains", {}).setdefault(domain, {})
                dom["status"] = "error"
                dom["lastError"] = f"{type(error).__name__}: {str(error)[:160]}"
                print(f"  ! 追赶 {rel} {domain}: {dom['lastError']}",
                      file=sys.stderr)


def refresh_kg() -> None:
    """KG 走既有同步脚本（修订号短路，未变时近乎零成本）。"""
    try:
        base = (os.environ.get("BW_PI_BASE", "")
                or _env_file_value("BW_PI_BASE")
                or "https://bwicarus.taile44d0c.ts.net")
        token = os.environ.get("BW_PI_TOKEN", "") or _env_file_value("BW_PI_TOKEN")
        sync_kg_from_pi.sync(base, token, DEFAULT_TIMEOUT, None)
    except Exception as error:
        print(f"  ! KG 同步失败: {type(error).__name__}: {error}", file=sys.stderr)


# ── 主循环 ───────────────────────────────────────────────────────────────────

def run(client: PiClient, directory: Path, *, once: bool) -> int:
    directory.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(directory)
    _set_sse_status(manifest, "catching-up")
    save_manifest(directory, manifest)

    try:
        catch_up(client, directory, manifest, full=True)
        refresh_kg()
    except Exception as error:
        _set_sse_status(manifest, "catchup-failed",
                        f"{type(error).__name__}: {error}")
        save_manifest(directory, manifest)
        print(f"追赶失败：{type(error).__name__}: {error}", file=sys.stderr)
        if once:
            return 1
    else:
        _set_sse_status(manifest, "caught-up" if once else "connecting")
        save_manifest(directory, manifest)
    if once:
        print(f"追赶完成。镜像目录 {directory}")
        return 0

    # 读线程 + 带超时的主循环。第一版把「检查去抖到期」放在收到下一行 SSE 之后，
    # 实测端到端 16.1s —— 恰好是服务端心跳间隔 15s + 1s:事件到达后计划 2s 拉取,
    # 但下一次循环要等下一个心跳才发生。铃铛响了,看表的人在打盹。
    # 现在读线程只管把事件塞队列,主循环 0.5s 醒一次看表,到期就拉。
    import queue as _queue
    import threading

    def _reader_thread(stream, out_q: _queue.Queue):
        try:
            text = io.TextIOWrapper(stream, encoding="utf-8")
            for event in parse_sse_events(text):
                out_q.put(("ev", event))
            out_q.put(("eof", None))
        except Exception as error:            # 含 40s 无心跳的 socket 超时
            out_q.put(("err", error))

    planner = PullPlanner()
    backoff = 1.0
    last_kg = time.time()
    while True:
        try:
            stream = client.open_sse("/pdf/api/reader-events")
        except KeyboardInterrupt:
            _set_sse_status(manifest, "stopped")
            save_manifest(directory, manifest)
            return 0
        except Exception as error:
            msg = f"{type(error).__name__}: {str(error)[:200]}"
            _set_sse_status(manifest, "reconnecting", msg)
            save_manifest(directory, manifest)
            print(f"SSE 连接失败({msg})，{backoff:.0f}s 后重连", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
            continue

        _set_sse_status(manifest, "connected")
        save_manifest(directory, manifest)
        backoff = 1.0
        q: _queue.Queue = _queue.Queue()
        t = threading.Thread(target=_reader_thread, args=(stream, q), daemon=True)
        t.start()
        disconnected = None
        try:
            while disconnected is None:
                try:
                    kind, payload = q.get(timeout=0.5)
                except _queue.Empty:
                    kind, payload = ("tick", None)
                now = time.time()
                if kind == "ev" and payload is not None:
                    manifest.setdefault("sse", {})[
                        "lastEventAtEpochSeconds"] = _now()
                    planner.note_event(payload, now)
                elif kind in ("eof", "err"):
                    disconnected = payload or EOFError("stream ended")
                for domain, rel in planner.due(now):
                    try:
                        if domain == "pos":
                            pull_positions(client, directory, manifest)
                        elif domain == "error-report":
                            pull_error_reports(client, directory, manifest)
                        else:
                            pull_book_domain(client, directory, manifest,
                                             domain, rel)
                    except Exception as error:
                        print(f"  ! 拉取 {domain} {rel}: "
                              f"{type(error).__name__}: {error}",
                              file=sys.stderr)
                    save_manifest(directory, manifest)
                if now - last_kg >= KG_REFRESH_SECONDS:
                    last_kg = now
                    refresh_kg()
        except KeyboardInterrupt:
            _set_sse_status(manifest, "stopped")
            save_manifest(directory, manifest)
            return 0
        finally:
            try:
                stream.close()
            except Exception:
                pass
        # 服务端 ~5 分钟回收长连接是**日常**,不是故障;照样出声但语气不同
        msg = f"{type(disconnected).__name__}: {str(disconnected)[:200]}"
        _set_sse_status(manifest, "reconnecting", msg)
        save_manifest(directory, manifest)
        print(f"SSE 断开({msg})，{backoff:.0f}s 后重连", file=sys.stderr)
        time.sleep(backoff)
        backoff = min(backoff * 2, 60.0)
        # 每次重连前做轻量追赶,补上断线窗口错过的事件
        try:
            catch_up(client, directory, manifest, full=False)
            save_manifest(directory, manifest)
        except Exception as error:
            print(f"  ! 重连追赶失败: {type(error).__name__}: {error}",
                  file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Pi → Windows 阅读器数据镜像守护")
    parser.add_argument(
        "--base",
        default=(os.environ.get("BW_PI_BASE", "") or _env_file_value("BW_PI_BASE")
                 or "https://bwicarus.taile44d0c.ts.net"))
    parser.add_argument(
        "--token",
        default=os.environ.get("BW_PI_TOKEN", "") or _env_file_value("BW_PI_TOKEN"))
    parser.add_argument("--once", action="store_true",
                        help="只做一轮追赶，不订阅事件")
    args = parser.parse_args()
    if not args.token:
        print("缺少 API token：设置 BW_PI_TOKEN、写进 .env，或传 --token。",
              file=sys.stderr)
        return 1
    client = PiClient(args.base, args.token)
    return run(client, mirror_dir(), once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
