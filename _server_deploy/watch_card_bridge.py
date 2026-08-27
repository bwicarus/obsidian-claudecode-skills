"""通话中把 AI 发的卡片送到手表。

## 为什么需要这个

用户 2026-08-28 实测：手表单独通话时让 AI 发卡片，报
`BW_READER_REALTIME_OUTPUT_SOURCE_OFFLINE`（"指定 Reader 页面来源当前不在线"）。
因为卡片投递要求有一个**已注册的 Reader 页面来源**（浏览器标签页或 App 里
开着阅读器），而只有手表在线时确实没有。

用户的判断是「把手表单独连接也放进去当作一种情况」。

## 怎么做到的（协议是读代码读出来的，不是猜的）

Windows 桥本来就有一条 **HTTP 取件型 lease**（`ReaderHttpPickup.cs`，
2026-08-26 为 iPad Safari 网页造的）：

> 「每次轮询都是**在场心跳**，桥据此向 router Attach 一个 lease」

也就是说 —— **长轮询本身就完成了注册**，不需要另外的 WebSocket 握手。
所以 Pi 只要做三件事：

1. `POST /reader-context/snapshot` 发一份快照，声明「手表这个来源在线」
   （⚠ 必须发：卡片是投给**快照指定的那一个** sourceInstanceId，
   `ReaderVisualDelivery.RequestAsync` 第 647 行，不是投给任意在线来源）
2. `POST /reader-output/pending` 长轮询取件（这一步顺带完成注册）
3. 取到卡片 → 送给手表 → `POST /reader-output/receipt` 回执

## ⚠ 三条刻意的边界

**一、只在通话中注册。** 没有手表在线时不注册 —— 否则 AI 会把卡片投给一个
根本没人看的地方，而它还以为送到了。

**二、只认卡片，别的一律如实说不支持。** Pi 不是阅读器：它没有页面、没有
字符层、渲染不了页图。收到 `reader_visual_image` 这类请求时**明确回不支持**，
而不是沉默或假装成功 —— 假装的代价是 AI 拿着一个不存在的能力继续往下推。

**三、Pi 现在经手卡片内容了。** 这是用户明确接受的扩大（此前它只经手音频）。
写在这里是因为**安全边界变了就必须有人知道**：Pi 是这条链路对外的那道门，
Windows 那头不会再校验第二次。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

WINDOWS_ORIGIN = "https://bwicarus-2.taile44d0c.ts.net"
# ⚠ 必须是 Pi 自己的名字：桥的 Origin 白名单认的是 Reader PWA 的 origin。
PI_ORIGIN = "https://bwicarus.taile44d0c.ts.net"

SNAPSHOT_URL = WINDOWS_ORIGIN + "/reader-context/snapshot"
PENDING_URL = WINDOWS_ORIGIN + "/reader-output/pending"
RECEIPT_URL = WINDOWS_ORIGIN + "/reader-output/receipt"

CONTEXT_CONTRACT = "reader-context-viewport/1"
# 长轮询的等待秒数。桥侧 clamp 到 [0,30]。
POLL_WAIT_SECONDS = 25
# 快照多久重发一次。⚠ 桥侧 lease 有 60 秒空闲超时，必须比它短。
SNAPSHOT_REFRESH_SECONDS = 20


class CardBridgeError(RuntimeError):
    """出声用的。⚠ 这个模块里**任何**放弃都要经过它，不许静默 return。"""


def _post(url: str, payload: dict, timeout: float) -> tuple[int, dict]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            # ⚠ Origin 是桥的第一道闸，少了就是 403。
            "Origin": PI_ORIGIN,
        })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            try:
                return response.status, json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return response.status, {"raw": raw[:200]}
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:200]
        return error.code, {"error": detail}


def build_watch_snapshot(source_id: str, *, title: str) -> dict:
    """一份「手表在线」的最小快照。

    ⚠ 字段集是**精确**的：`DirectContextSnapshot.ValidateViewport` 的允许集 =
    必填 ∪ {controlCorrelation}，**多一个字段就整条 400**。而 400 之后
    content.js 那类客户端会按节流重发同样的 body —— 一个字段级的拒绝会变成
    无限重试（见 contract-sites.json 的 web-snapshot-post-body）。
    所以这里宁可少给，不多给。
    """
    return {
        "viewport": {
            "contract": CONTEXT_CONTRACT,
            "sourceInstanceId": source_id,
            # 手表没有"文档"。给一个稳定且明显不是书的标识，
            # 免得下游把它当成一本可以翻页的书。
            "documentKey": "watch-voice-call",
            "url": "bwwatch://voice-call",
            "title": title,
            "beforeText": "",
            # ⚠ 如实说明这是什么表面。AI 读到这句就知道：这里只能收卡片，
            # 没有页面可翻、没有正文可读。**比让它自己猜要好。**
            "visibleText": "（Apple Watch 语音通话中。这个来源只能接收卡片，"
                           "没有可阅读的页面正文。）",
            "afterText": "",
            "selectionState": "none",
            "selection": "",
            "observedAtEpochMs": int(time.time() * 1000),
        }
    }


class WatchCardBridge:
    """通话期间把卡片从 Windows 取过来送给手表。

    生命周期跟**一路手表连接**绑定：手表来了才注册，走了就停。
    """

    def __init__(
        self,
        source_id: str,
        *,
        deliver: Callable[[dict], None],
        log: Callable[..., None],
    ) -> None:
        self.source_id = source_id
        self._deliver = deliver          # 送给手表的那条下行文本通道
        self._log = log
        self._stop = False
        self._last_snapshot = 0.0
        self.cards_delivered = 0
        self.unsupported = 0

    def stop(self) -> None:
        self._stop = True

    # ── 注册 ──

    def refresh_snapshot(self) -> bool:
        """重发快照当心跳。⚠ 失败要出声 —— 静默失败的表现是「AI 说来源不在线」，
        而那句话跟"根本没连"长得一模一样。"""
        status, payload = _post(
            SNAPSHOT_URL,
            build_watch_snapshot(self.source_id, title="Apple Watch 通话"),
            timeout=10)
        self._last_snapshot = time.time()
        if status != 200:
            self._log("card.snapshot_rejected", status=status,
                      detail=str(payload)[:160])
            return False
        return True

    # ── 取件 ──

    def poll_once(self) -> list[dict]:
        status, payload = _post(
            PENDING_URL,
            {"sourceInstanceId": self.source_id, "wait": POLL_WAIT_SECONDS},
            timeout=POLL_WAIT_SECONDS + 10)
        if status == 429:
            # 桥侧的来源数上限（16）。说清楚，别当成"没有卡片"。
            self._log("card.pickup_throttled", detail=str(payload)[:160])
            return []
        if status != 200:
            self._log("card.pickup_failed", status=status,
                      detail=str(payload)[:160])
            return []
        events = payload.get("events")
        return events if isinstance(events, list) else []

    def handle(self, event: dict) -> None:
        """一条取回来的事件。

        ⚠ **只认卡片。** Pi 没有页面、没有字符层、渲染不了页图，所以
        `reader_visual_image` 这类请求必须**明确回不支持**，而不是沉默 ——
        沉默的代价是 AI 拿着一个不存在的能力继续往下推，而它永远等不到答案。
        """
        kind = str(event.get("type") or event.get("kind") or "")
        request_id = event.get("requestId") or event.get("id")
        if "card" in kind.lower():
            self._deliver({
                "ev": "card",
                "card": event.get("card") or event.get("payload") or event,
            })
            self.cards_delivered += 1
            self._ack(request_id, accepted=True)
            return
        self.unsupported += 1
        self._log("card.unsupported_kind", kind=kind or "(空)")
        self._ack(
            request_id, accepted=False,
            reason="手表来源只能接收卡片，没有页面可渲染")

    def _ack(self, request_id: Any, *, accepted: bool,
             reason: Optional[str] = None) -> None:
        if not request_id:
            return
        body: dict[str, Any] = {
            "sourceInstanceId": self.source_id,
            "requestId": request_id,
            "accepted": accepted,
        }
        if reason:
            body["reason"] = reason
        status, payload = _post(RECEIPT_URL, body, timeout=10)
        if status != 200:
            # 回执发不出去 = 对面以为没送到。必须出声。
            self._log("card.receipt_failed", status=status,
                      detail=str(payload)[:160])

    # ── 主循环 ──

    def run(self) -> None:
        """跑在自己的线程里。⚠ 用阻塞 urllib 而不是 asyncio 是刻意的：
        这条链路**不能碰音频那条 50Hz 的节拍**，放进同一个事件循环里，
        一次慢请求就会让通话卡一下。分开跑，互不影响。"""
        self._log("card.bridge_start", source=self.source_id)
        if not self.refresh_snapshot():
            self._log("card.bridge_degraded",
                      why="首次快照被拒，卡片投递不可用（通话不受影响）")
        while not self._stop:
            if time.time() - self._last_snapshot > SNAPSHOT_REFRESH_SECONDS:
                self.refresh_snapshot()
            try:
                for event in self.poll_once():
                    if self._stop:
                        break
                    self.handle(event)
            except Exception as error:            # noqa: BLE001
                # ⚠ 卡片这条腿坏掉**不许影响通话**。出声然后退避重试。
                self._log("card.loop_error",
                          detail="%s: %s" % (type(error).__name__, error))
                time.sleep(3)
        self._log("card.bridge_stop", source=self.source_id,
                  delivered=self.cards_delivered, unsupported=self.unsupported)
