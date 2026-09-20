"""Short-lived scheduled work, independent of any voice connection or App presence."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
from pathlib import Path

from schedules import _iso
from stock_research_workflow import AI_INSTRUCTIONS, WorkflowInputError, execute_workflow

log = logging.getLogger(__name__)
QUOTE_FIELDS = ("code", "name", "price", "changeAmount", "changePct", "open", "high", "low", "prevClose",
                "volume", "turnover", "turnoverRate", "volumeRatio", "amplitude", "quoteTime", "quoteSource")


async def analyze_scheduled(task, snapshot, state_root):
    """One bounded read-only text job; no realtime, tools or persistent conversation."""
    work = Path(state_root) / "schedule-ai"
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "OPENAI_BASE_URL")}
    process = await asyncio.create_subprocess_exec(
        os.environ.get("STOCKS_CODEX", "/opt/codex/0.155.1/bin/codex"),
        "-c", 'forced_login_method="chatgpt"', "-c", "features.plugins=false",
        "-c", "features.memories=false", "-c", "features.shell_tool=false", "-c", 'web_search="disabled"',
        "app-server", "--listen", "stdio://", cwd=str(work), env=env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, limit=4 * 1024 * 1024)
    counter, events, messages = 0, [], {}

    async def send(value):
        process.stdin.write((json.dumps(value, ensure_ascii=False) + "\n").encode())
        await process.stdin.drain()

    async def receive():
        while raw := await process.stdout.readline():
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if "method" in obj and "id" in obj:
                await send({"id": obj["id"], "error": {"code": -32601, "message": "Scheduled analysis has no tools"}})
                continue
            return obj
        raise RuntimeError("定时分析连接已关闭")

    async def rpc(method, params):
        nonlocal counter
        counter += 1
        request_id = counter
        await send({"id": request_id, "method": method, "params": params})
        while True:
            obj = await receive()
            if obj.get("id") == request_id:
                if "error" in obj:
                    raise RuntimeError("定时分析请求失败")
                return obj.get("result", {})
            events.append(obj)

    async def run():
        await rpc("initialize", {"clientInfo": {"name": "stocks_schedule", "version": "1"},
                                 "capabilities": {"experimentalApi": True}})
        await send({"method": "initialized"})
        response = await rpc("thread/start", {
            "cwd": str(work), "model": os.environ.get("STOCKS_MONITOR_MODEL", "gpt-5.6-sol"),
            "modelProvider": "openai", "approvalPolicy": "never", "sandbox": "read-only", "ephemeral": True,
            "environments": [], "config": {"model_reasoning_effort": "low", "features.shell_tool": False,
                "features.plugins": False, "features.memories": False, "web_search": "disabled", "mcp_servers": {}},
            "developerInstructions": AI_INSTRUCTIONS})
        thread_id = response["thread"]["id"]
        response = await rpc("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": json.dumps(
            {"task": {"title": task["title"], "prompt": task["prompt"]}, "input": snapshot}, ensure_ascii=False, allow_nan=False)}]})
        turn_id = response["turn"]["id"]
        while True:
            obj = events.pop(0) if events else await receive()
            params = obj.get("params") or {}
            if params.get("threadId") != thread_id:
                continue
            if obj.get("method") == "item/completed" and params.get("turnId") == turn_id:
                item = params.get("item") or {}
                if item.get("type") == "agentMessage" and item.get("phase") in ("final", "final_answer"):
                    messages[item.get("id", "final")] = item.get("text", "")
            if obj.get("method") == "turn/completed" and params.get("turn", {}).get("id") == turn_id:
                turn = params["turn"]
                for item in turn.get("items", []):
                    if item.get("type") == "agentMessage" and item.get("phase") in ("final", "final_answer"):
                        messages[item.get("id", "final")] = item.get("text", "")
                if turn.get("status") != "completed":
                    raise RuntimeError("定时分析未完成")
                answer = "\n".join(value.strip() for value in messages.values() if value.strip())
                if not answer:
                    raise RuntimeError("定时分析未返回正文")
                if len(answer) > 80000:
                    raise RuntimeError("定时分析结果超过上限")
                return answer

    try:
        return await asyncio.wait_for(run(), 120)
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 4)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()


class ScheduleRuntime:
    def __init__(self, app, *, analyze=analyze_scheduled):
        self.app, self.service, self.monitor = app, app["schedules"], app["monitor"]
        self.analyze = analyze
        self.tasks = []

    def start(self):
        if not self.tasks:
            self.tasks = [asyncio.create_task(self.tick_loop()), asyncio.create_task(self.work_loop())]

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []

    async def tick_loop(self):
        while True:
            try:
                await asyncio.to_thread(self.service.tick)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Schedule tick failed (%s)", type(exc).__name__)
            await asyncio.sleep(3)

    async def work_loop(self):
        while True:
            job = None
            try:
                job = await asyncio.to_thread(self.service.claim)
                if job:
                    await self.execute(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Scheduled work failed (%s)", type(exc).__name__)
            finally:
                if job:
                    await asyncio.to_thread(self.service.abandon, job)
            await asyncio.sleep(3)

    async def execute(self, job):
        """Renew only this claim. Cancellation/revision change stops an in-flight AI job."""
        operation = asyncio.create_task(self._perform(job))
        try:
            while not operation.done():
                done, _ = await asyncio.wait({operation}, timeout=15)
                if done:
                    break
                if not await asyncio.to_thread(self.service.renew, job):
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)
                    return
            await operation
        finally:
            if not operation.done():
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)

    async def _perform(self, job):
        if not job["ready"]:
            task, error, result = job["task"], None, {}
            if task["kind"] == "reminder":
                body = task["prompt"]
            else:
                try:
                    body, error, result = await execute_workflow(self, job)
                except asyncio.CancelledError:
                    raise
                except WorkflowInputError as exc:
                    error, result = "workflow_input_unavailable", job.get("progress") or {}
                    body = "定时任务「" + task["title"] + "」未完成：" + str(exc)[:500]
                # A local report write failure is intentionally retried from the
                # saved output/payload, without regenerating or billing AI again.
            if not await asyncio.to_thread(self.service.prepare, job, body, error=error, result=result):
                return
        # MonitorRuntime already owns visual / active voice / PushKit / CallKit delivery.
        # This only creates its durable finished notification, never a realtime session.
        await asyncio.to_thread(self.service.publish, job, self.monitor)

    async def snapshot(self, task, scheduled_at):
        try:
            quotes = await asyncio.wait_for(self.app["live"].quotes(task["codes"]), 20)
        except Exception as exc:
            raise LookupError("报价不可用") from exc
        data, missing = {}, []
        for code in task["codes"]:
            raw = quotes.get(code) if isinstance(quotes, dict) else None
            if not isinstance(raw, dict):
                missing.append(code)
                continue
            clean = {}
            for key in QUOTE_FIELDS:
                value = raw.get(key)
                if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                    if isinstance(value, float) and not math.isfinite(value):
                        continue
                    clean[key] = value[:300] if isinstance(value, str) else value
            if not clean.get("quoteTime") or not isinstance(clean.get("price"), (int, float)) or clean["price"] <= 0:
                missing.append(code)
                continue
            data[code] = clean
        if not data:
            raise LookupError("没有可用报价")
        return {"scheduledAt": scheduled_at, "fetchedAt": _iso(self.service.clock()), "quotes": data,
                "missingCodes": missing, "sourcePolicy": "最新可用报价，行情时间以各 quoteTime 为准；闭市报价不等于实时行情"}
