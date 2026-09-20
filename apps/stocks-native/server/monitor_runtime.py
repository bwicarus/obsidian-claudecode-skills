"""Gateway-owned monitoring worker; database lease permits zero-downtime cutovers."""
from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime
import hashlib
import logging
import time

from monitor_ai import explain_signal

log = logging.getLogger(__name__)


class MonitorRuntime:
    def __init__(self, app):
        self.app = app
        self.service = app["monitor"]
        self.delivery = app["notifications"]
        self.tasks = []
        self.leader = False
        self.last_poll = 0

    def start(self):
        self.tasks = [asyncio.create_task(self.run()), asyncio.create_task(self.analyze())]

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def run(self):
        while True:
            try:
                self.leader = await asyncio.to_thread(self.delivery.lease)
                if self.leader:
                    if time.monotonic() - self.last_poll >= 15:
                        self.last_poll = time.monotonic()
                        codes = await asyncio.to_thread(self.service.active_codes)
                        quotes = {}
                        for start in range(0, len(codes), 100):
                            quotes.update(await self.app["live"].quotes(codes[start:start+100]))
                        await asyncio.to_thread(self.service.evaluate_quotes, quotes)
                await self.deliver(remote=self.leader)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Never log data, tokens or model stderr.
                log.warning("Monitor iteration failed (%s)", type(exc).__name__)
            await asyncio.sleep(3)

    async def analyze(self):
        while True:
            try:
                if not self.leader:
                    await asyncio.sleep(3)
                    continue
                if not await asyncio.to_thread(self.delivery.lease, 'ai-execution', 150):
                    await asyncio.sleep(3)
                    continue
                job = await asyncio.to_thread(self.service.claim_ai_job)
                if not job:
                    await asyncio.to_thread(self.delivery.release, 'ai-execution')
                    await asyncio.sleep(3)
                    continue
                notification = job["notification"]
                job_id = job["jobId"]
                key = hashlib.sha256((job["ownerId"] + ":" + str(notification.get("code"))).encode()).hexdigest()
                # Persistent throttle survives gateway replacement, separate from rule cooldown.
                gate = await asyncio.to_thread(self.delivery.budgets, {'ai-global': 30, 'ai-stock:' + key: 300})
                if not gate:
                    await asyncio.to_thread(self.service.defer_ai_job, job_id, 60, "同股分析冷却中")
                    await asyncio.to_thread(self.delivery.release, 'ai-execution')
                    await asyncio.sleep(3)
                    continue
                try:
                    answer = await explain_signal(notification, self.app["state"])
                    await asyncio.to_thread(self.service.finish_ai_job, job_id, answer)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await asyncio.to_thread(self.service.finish_ai_job, job_id, "",
                                            error="AI 暂不可用，已保留触发条件和原始行情。")
                finally:
                    await asyncio.to_thread(self.delivery.release, 'ai-execution')
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Monitor analysis failed (%s)", type(exc).__name__)
                await asyncio.sleep(10)

    def voices(self, owner):
        result = []
        for entry in list(self.app["voices"].values()):
            if entry:
                ws, session = entry
                if not ws.closed and not session.closed and session.selection_owner == owner:
                    result.append(session)
        return sorted(result, key=lambda s: s.last_activity, reverse=True)

    async def deliver(self, remote=True):
        notifications = await asyncio.to_thread(self.service.pending_notifications)
        for notice in notifications:
            owner, notice_id = notice["ownerId"], notice["id"]
            explicit_call = notice.get("deliveryMode") == "call"
            if notice.get("status") == "resolved" or (not explicit_call and notice.get("status") != "unread"):
                continue
            try:
                created = datetime.fromisoformat(notice["createdAt"].replace("Z", "+00:00")).timestamp()
                age = self.delivery.clock() - created
            except (ValueError, KeyError):
                continue
            # Old unread events remain visible but never call hours later after a restart.
            if (explicit_call and age >= 600) or (not explicit_call and age > 300):
                if explicit_call and notice.get("delivery", {}).get("call") in (None, "queued", "queued_busy"):
                    await asyncio.to_thread(self.service.mark_delivery, owner, notice_id, {"call": "expired"})
                continue
            if notice.get("aiState") in ("pending", "running") and age < 20:
                continue
            devices = await asyncio.to_thread(self.delivery.devices, owner)
            voices = self.voices(owner)
            if voices and not explicit_call:
                session = voices[0]
                if session.can_announce_notification():
                    reserved = await asyncio.to_thread(self.delivery.reserve_delivery, owner, notice_id, "spoken", "account")
                    if reserved:
                        try:
                            await session.announce_notification(notice)
                            await asyncio.to_thread(self.service.mark_delivery, owner, notice_id, {"spoken": "submitted"})
                        except Exception:
                            await asyncio.to_thread(self.delivery.receipt, owner, notice_id, "spoken", "account", "failed")
                # A busy or connecting existing call must never be replaced by an incoming call.
            if not remote:
                continue
            for device in devices:
                foreground = bool(device["foreground"] and self.delivery.clock() - device["seen"] < 30)
                if foreground or not device["enabled"] or not device["push"] or not self.delivery.configured():
                    continue
                if await asyncio.to_thread(self.delivery.reserve_delivery, owner, notice_id, "push", device["device"]):
                    try:
                        success = await self.delivery.push(device, notice)
                    except Exception:
                        success = False
                    await asyncio.to_thread(self.delivery.receipt, owner, notice_id, "push", device["device"],
                                            "accepted" if success else "failed")
                    await asyncio.to_thread(self.service.mark_delivery, owner, notice_id, {"push": "accepted" if success else "failed"})
            if voices or await asyncio.to_thread(self.delivery.has_voice, owner):
                if explicit_call and notice.get("delivery", {}).get("call") in (None, "queued", "queued_busy"):
                    await asyncio.to_thread(self.service.mark_delivery, owner, notice_id, {"call": "queued_busy"})
                # Explicit requests wait for the current call to end; they must not
                # silently turn into speech or disconnect an existing voice session.
                continue
            if not explicit_call and (notice.get("severity") != "urgent" or any(
                    d["foreground"] and self.delivery.clock()-d["seen"] < 30 for d in devices)):
                continue
            if not self.delivery.configured():
                continue
            candidates = [d for d in devices if d["enabled"] and d["voip"]]
            if candidates:
                # A user may resolve a notice while this delivery pass awaits I/O.
                current = await asyncio.to_thread(self.service.get_notification, owner, notice_id)
                if not current or current.get("status") == "resolved" or (
                        not explicit_call and current.get("status") != "unread"):
                    continue
                if explicit_call and self.delivery.clock() - created >= 600:
                    if current.get("delivery", {}).get("call") in (None, "queued", "queued_busy"):
                        await asyncio.to_thread(self.service.mark_delivery, owner, notice_id, {"call": "expired"})
                    continue
                target = candidates[0]
                call = await asyncio.to_thread(self.delivery.create_call, owner, target["device"], notice_id)
                if call:
                    try:
                        success = await self.delivery.push(target, notice, call)
                    except Exception:
                        success = False
                    if not success:
                        await asyncio.to_thread(self.delivery.call_receipt, owner, target["device"], notice_id, call["callId"], "failed")
                    await asyncio.to_thread(self.service.mark_delivery, owner, notice_id, {"call": "push_accepted" if success else "failed"})
