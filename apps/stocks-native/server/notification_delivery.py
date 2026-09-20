"""Account-bound device presence, durable delivery receipts and single-attempt calls."""
from __future__ import annotations

import contextlib
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid


class NotificationDelivery:
    def __init__(self, state_dir, clock=time.time):
        self.path = Path(state_dir) / "notification-delivery.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.clock = clock
        self.instance = str(uuid.uuid4())
        with self.db() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS devices(
                device TEXT PRIMARY KEY, owner TEXT NOT NULL, push TEXT, voip TEXT,
                environment TEXT NOT NULL DEFAULT 'production', enabled INTEGER NOT NULL DEFAULT 0,
                foreground INTEGER NOT NULL DEFAULT 0, seen REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS receipts(
                owner TEXT, notice TEXT, channel TEXT, target TEXT, outcome TEXT, updated REAL,
                attempts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(owner,notice,channel,target));
            CREATE TABLE IF NOT EXISTS calls(
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, device TEXT NOT NULL, notice TEXT NOT NULL,
                status TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL, answered REAL,
                UNIQUE(owner,notice));
            CREATE TABLE IF NOT EXISTS leases(name TEXT PRIMARY KEY, instance TEXT, expires REAL);
            CREATE TABLE IF NOT EXISTS voices(device TEXT PRIMARY KEY, owner TEXT, instance TEXT, seen REAL);
            """)
        if os.name != "nt":
            self.path.chmod(0o600)

    @contextlib.contextmanager
    def db(self):
        c = sqlite3.connect(self.path, timeout=5)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def lease(self, name="monitor", seconds=30):
        now = self.clock()
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM leases WHERE name=?", (name,)).fetchone()
            if row and row["instance"] != self.instance and row["expires"] > now:
                return False
            c.execute("INSERT OR REPLACE INTO leases VALUES(?,?,?)", (name, self.instance, now + seconds))
            return True

    def register(self, owner, device, payload):
        def token(name):
            value = payload.get(name)
            if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[a-fA-F0-9]{32,256}", value)):
                raise ValueError("推送设备标识格式不正确")
            return value
        push, voip = token("pushToken"), token("voipToken")
        environment = payload.get("environment", "production")
        if environment not in ("production", "sandbox"):
            raise ValueError("推送环境无效")
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            old = c.execute("SELECT * FROM devices WHERE device=?", (device,)).fetchone()
            same = old and old["owner"] == owner
            if same:
                if "pushToken" not in payload:
                    push = old["push"]
                if "voipToken" not in payload:
                    voip = old["voip"]
            if old and not same:
                c.execute("UPDATE calls SET status='ended' WHERE device=? AND status IN ('ringing','answered')", (device,))
            enabled = bool(payload.get("notificationsEnabled", old["enabled"] if same else False))
            if "pushToken" in payload and "voipToken" in payload and push is None and voip is None:
                enabled = False
                c.execute("UPDATE calls SET status='ended' WHERE device=? AND owner=?", (device, owner))
            c.execute("INSERT OR REPLACE INTO devices VALUES(?,?,?,?,?,?,?,?)", (
                device, owner, push, voip, environment, int(enabled), old["foreground"] if same else 0, self.clock()))
        return {"success": True, "pushConfigured": self.configured()}

    def budget(self, name, seconds):
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT expires FROM leases WHERE name=?", (name,)).fetchone()
            if row and row["expires"] > self.clock():
                return False
            c.execute("INSERT OR REPLACE INTO leases VALUES(?,?,?)", (name, "budget", self.clock()+seconds))
            return True

    def release(self, name):
        with self.db() as c:
            c.execute("DELETE FROM leases WHERE name=? AND instance=?", (name, self.instance))

    def budgets(self, limits):
        now = self.clock()
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            for name in limits:
                row = c.execute('SELECT expires FROM leases WHERE name=?', (name,)).fetchone()
                if row and row['expires'] > now:
                    return False
            for name, seconds in limits.items():
                c.execute('INSERT OR REPLACE INTO leases VALUES(?,?,?)', (name, 'budget', now+seconds))
            return True

    def voice_presence(self, owner, device, connected=True):
        with self.db() as c:
            if connected:
                c.execute('INSERT OR REPLACE INTO voices VALUES(?,?,?,?)', (device, owner, self.instance, self.clock()))
            else:
                c.execute('DELETE FROM voices WHERE device=? AND owner=? AND instance=?', (device, owner, self.instance))

    def has_voice(self, owner):
        with self.db() as c:
            return c.execute('SELECT 1 FROM voices WHERE owner=? AND seen>?', (owner, self.clock()-30)).fetchone() is not None

    def presence(self, owner, device, foreground):
        if not isinstance(foreground, bool):
            raise ValueError("foreground 必须为布尔值")
        with self.db() as c:
            row = c.execute("SELECT owner FROM devices WHERE device=?", (device,)).fetchone()
        if row and row['owner'] != owner:
            return {"success": False, "reason": "device_binding_changed"}
        if not row:
            self.register(owner, device, {})
        with self.db() as c:
            c.execute("UPDATE devices SET foreground=?,seen=? WHERE device=? AND owner=?",
                      (int(foreground), self.clock(), device, owner))
        return {"success": True}

    def devices(self, owner):
        with self.db() as c:
            return [dict(r) for r in c.execute("SELECT * FROM devices WHERE owner=? ORDER BY seen DESC LIMIT 20", (owner,))]

    def call_status(self, owner, notification=None, *, push_configured=None):
        """Model-visible capability and receipts, without device IDs or push credentials."""
        now = self.clock()
        eligible = any(d["enabled"] and d["voip"] for d in self.devices(owner))
        configured = self.configured() if push_configured is None else push_configured
        active_voice = self.has_voice(owner)
        result = {"available": bool(configured and eligible), "activeVoice": active_voice,
                  "reason": None if configured and eligible else
                  "push_not_configured" if not configured else "no_registered_call_device",
                  "singleAttempt": True, "waitForCurrentVoice": True, "maxWaitSeconds": 600}
        if notification is None:
            return result
        nid = notification["id"]
        with self.db() as c:
            row = c.execute("SELECT * FROM calls WHERE owner=? AND notice=?", (owner, nid)).fetchone()
        result["notificationId"] = nid
        result["answered"] = bool(row and row["answered"] is not None)
        speech = self.receipt_for(owner, nid, "spoken", "account")
        result["audioSubmitted"] = bool(speech and speech["outcome"] == "submitted")
        if row:
            state = row["status"]
            if state == "ringing":
                state = "missed" if now >= row["expires"] else (
                    "push_accepted" if notification.get("delivery", {}).get("call") == "push_accepted" else "submitting")
            elif state == "answered" and now >= (row["answered"] or row["created"]) + 600:
                state = "ended"
            result.update(callId=row["id"], state=state)
        elif notification.get("status") == "resolved":
            result["state"] = "cancelled"
        else:
            created = datetime.fromisoformat(notification["createdAt"].replace("Z", "+00:00")).timestamp()
            result["expiresAt"] = created + 600
            result["state"] = ("expired" if now >= created + 600 else "unavailable" if not result["available"]
                               else "waiting_for_current_voice" if active_voice else "queued")
        return result

    def receipt(self, owner, notice, channel, target, outcome):
        if channel not in ("visual", "push", "spoken", "call"):
            raise ValueError("通知通道无效")
        with self.db() as c:
            c.execute("""INSERT INTO receipts(owner,notice,channel,target,outcome,updated,attempts)
                      VALUES(?,?,?,?,?,?,1) ON CONFLICT(owner,notice,channel,target)
                      DO UPDATE SET outcome=excluded.outcome,updated=excluded.updated""",
                      (owner, notice, channel, target, outcome, self.clock()))

    def receipt_for(self, owner, notice, channel, target):
        with self.db() as c:
            row = c.execute("SELECT * FROM receipts WHERE owner=? AND notice=? AND channel=? AND target=?",
                            (owner, notice, channel, target)).fetchone()
            return dict(row) if row else None

    def reserve_delivery(self, owner, notice, channel, target):
        now = self.clock()
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM receipts WHERE owner=? AND notice=? AND channel=? AND target=?",
                            (owner, notice, channel, target)).fetchone()
            if row:
                # Speech and incoming calls are never automatically replayed.
                if channel != "push" or row["outcome"] != "failed" or row["attempts"] >= 3 or now-row["updated"] < 60:
                    return False
                c.execute("UPDATE receipts SET outcome='submitted',updated=?,attempts=attempts+1 WHERE owner=? AND notice=? AND channel=? AND target=?",
                          (now, owner, notice, channel, target))
            else:
                c.execute("INSERT INTO receipts VALUES(?,?,?,?,?,?,1)", (owner, notice, channel, target, "submitted", now))
            return True

    def create_call(self, owner, device, notice):
        now = self.clock()
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            binding = c.execute("SELECT owner,enabled,voip FROM devices WHERE device=?", (device,)).fetchone()
            if not binding or binding['owner'] != owner or not binding['enabled'] or not binding['voip']:
                return None
            if c.execute("SELECT 1 FROM voices WHERE owner=? AND seen>?", (owner, now-30)).fetchone():
                return None
            c.execute("UPDATE calls SET status='missed' WHERE status='ringing' AND expires<=?", (now,))
            c.execute("UPDATE calls SET status='ended' WHERE status='answered' AND answered+600<=?", (now,))
            if c.execute("SELECT 1 FROM calls WHERE owner=? AND (notice=? OR status IN ('ringing','answered'))",
                         (owner, notice)).fetchone():
                return None
            call = {"callId": str(uuid.uuid4()), "notificationId": notice, "expiresAt": now + 45}
            c.execute("INSERT INTO calls VALUES(?,?,?,?,?,?,?,NULL)", (call["callId"], owner, device, notice, "ringing", now, now+45))
            return call

    def call(self, owner, device, call_id):
        with self.db() as c:
            row = c.execute("SELECT * FROM calls WHERE id=? AND owner=? AND device=?", (call_id, owner, device)).fetchone()
            binding = c.execute("SELECT owner FROM devices WHERE device=?", (device,)).fetchone()
        if not row or not binding or binding["owner"] != owner:
            return {"valid": False}
        expiry = row["answered"] + 600 if row["answered"] is not None else row["expires"]
        return {"valid": row["status"] in ("ringing", "answered") and self.clock() < expiry,
                "notificationId": row["notice"], "expiresAt": expiry, "status": row["status"]}

    def call_receipt(self, owner, device, notice, call_id, outcome):
        if outcome not in ("answered", "declined", "ended", "missed", "failed"):
            raise ValueError("来电回执无效")
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM calls WHERE id=? AND owner=? AND device=? AND notice=?",
                            (call_id, owner, device, notice)).fetchone()
            if not row:
                raise ValueError("来电不存在")
            if outcome == "answered":
                if row["status"] == "answered":
                    return {"success": True}
                if row["status"] != "ringing" or self.clock() >= row["expires"]:
                    raise ValueError("来电已结束")
                c.execute("UPDATE calls SET status='answered',answered=? WHERE id=?", (self.clock(), call_id))
            elif row["status"] in ("ringing", "answered"):
                c.execute("UPDATE calls SET status=? WHERE id=?", (outcome, call_id))
        self.receipt(owner, notice, "call", device, outcome)
        return {"success": True}

    @staticmethod
    def configured():
        return all(os.environ.get(k) for k in ("STOCKS_APNS_KEY_FILE", "STOCKS_APNS_KEY_ID", "STOCKS_APNS_TEAM_ID"))

    async def push(self, device, notification, call=None):
        """APNs accepted is a transport receipt, never 'user has seen/heard'."""
        if not self.configured():
            return False
        import httpx
        import jwt
        token = device["voip"] if call else device["push"]
        if not token:
            return False
        column = 'voip' if call else 'push'
        with self.db() as c:
            binding = c.execute(f"SELECT 1 FROM devices WHERE device=? AND owner=? AND {column}=? AND enabled=1",
                                (device['device'], device['owner'], token)).fetchone()
        if not binding:
            return False
        now = int(self.clock())
        key = Path(os.environ["STOCKS_APNS_KEY_FILE"]).read_text()
        authorization = jwt.encode({"iss": os.environ["STOCKS_APNS_TEAM_ID"], "iat": now}, key,
                                   algorithm="ES256", headers={"kid": os.environ["STOCKS_APNS_KEY_ID"]})
        topic = "space.bwicarus.stocksnative" + (".voip" if call else "")
        headers = {"authorization": "bearer " + authorization, "apns-topic": topic,
                   "apns-push-type": "voip" if call else "alert", "apns-priority": "10",
                   "apns-expiration": "0" if call else str(now+300),
                   "apns-collapse-id": notification["id"][:64]}
        payload = {"notificationId": notification["id"], "code": notification.get("code"),
                   "title": notification["title"][:120],
                   "aps": {"alert": {"title": notification["title"][:120], "body": notification["body"][:350]},
                           "sound": "default", "thread-id": "stocks-monitor"}}
        if call:
            payload.update(call)
            payload["aps"] = {"content-available": 1}
        host = "api.push.apple.com" if device["environment"] == "production" else "api.sandbox.push.apple.com"
        async with httpx.AsyncClient(http2=True, timeout=10) as client:
            response = await client.post(f"https://{host}/3/device/{token}", headers=headers, json=payload)
        try:
            reason = response.json().get('reason')
        except (ValueError, AttributeError):
            reason = None
        if response.status_code == 410 or (response.status_code == 400 and reason in ('BadDeviceToken', 'DeviceTokenNotForTopic')):
            with self.db() as c:
                c.execute(f"UPDATE devices SET {column}=NULL WHERE device=? AND owner=? AND {column}=?",
                          (device["device"], device["owner"], token))
        return response.status_code == 200
