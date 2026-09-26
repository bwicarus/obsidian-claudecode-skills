"""ambient_people.py — 旁听里的「人」：声音块 ↔ KJ 人物节点 ↔ 时间轴（2026-09-26）。

用户 2026-09-26：
  「时间轴，不同的块表示不同的发音者，点块查看登记的资料（姓名、自定义介绍——给 jev 当线索），
    还有一个供 AI 总结写入的位置（和我的关系、曾经商量过什么），这个人的对话历史……
    不同的块被设置为一个名字就当作一个人……之后可能做一个独立 App，通过相同的记录文件格式共用资料」
  「KJ 节点本来就包括人的登记，不如一起联动，文字信息格式化后写在 Obsidian 里」

所以分两处存，各存各擅长的：

  KJ（文字，渲染到 Obsidian ``KJ/``）——人物节点 kind=person
    name / aliases       名字与别名（改名、合并都走 KJ 的 update_node / merge_node）
    summary              用户自己写的介绍（jev 线索）
    definition           context_key="ambient-profile" 的那一条 = AI 总结（关系 / 商量过什么 / 近况），
                         可被新版本 supersede，用户也能直接改
    records kind=conversation  每段有他在场的对话一条（按窗口去重）

  旁听存储 ``state/ambient/``（KJ 不适合放的：向量与时间）
    speakers.json        contract "bw-ambient-speakers/1"
                         persons: {personId: {voiceprints: [{vector, from, addedAt}], updatedAt}}
                         slots:   {slotKey: {personId|null, firstSeen, lastSeen, utterances, vector?}}
    timeline/<日期>.jsonl contract "bw-utterance/1"，一句一行：
                         {id, t0, t1 (epoch ms), windowId, source, slotKey, isUser, text}

personId = KJ 节点 id，或保留值 "me"（用户本人，不建 KJ 节点）。
slotKey = App 的「分离器会话:槽位」，一个声音块。**名字永远在读的时候解析**（slot → person → KJ 名字），
所以事后给块起名 / 改名 / 合并，整条历史自动跟着变，不回写文件。

格式说明见 references/ambient-people-format.md —— 以后的独立 App 按同一份格式读写。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

ME = "me"
PROFILE_CONTEXT = "ambient-profile"
SPEAKERS_CONTRACT = "bw-ambient-speakers/1"
UTTERANCE_CONTRACT = "bw-utterance/1"
MAX_VOICEPRINTS = 12
SOURCE = {"kind": "audio", "ref": "ambient"}
LANGUAGE_RE = re.compile(r"[a-z]{2,3}(-[A-Za-z]{2,4})?")


class PeopleError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class AmbientPeople:
    def __init__(self, root: Path, kj):
        """root = state/ambient；kj = KJService（文字资料写这里，渲染进 Obsidian）。"""
        self.root = Path(root)
        self.kj = kj
        self._lock = threading.RLock()
        (self.root / "timeline").mkdir(parents=True, exist_ok=True)

    # ─────────────── speakers.json ───────────────

    @property
    def _speakers_path(self) -> Path:
        return self.root / "speakers.json"

    def _load(self) -> dict:
        try:
            data = json.loads(self._speakers_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data.setdefault("contract", SPEAKERS_CONTRACT)
        data.setdefault("persons", {})
        data.setdefault("slots", {})
        return data

    def _save(self, data: dict) -> None:
        tmp = self._speakers_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self._speakers_path)

    # ─────────────── KJ 人物 ───────────────

    def _kj(self):
        self.kj.actor = "ambient"
        return self.kj

    def _check(self, res: dict) -> dict:
        if not res.get("ok", True):
            raise PeopleError(res.get("code") or "kj_error", res.get("error") or "KJ 登记失败")
        return res

    def find_person(self, name: str) -> str | None:
        """按名字 / 别名找活跃的 person 节点（只认 kind=person，别把同名概念当成人）。"""
        name = (name or "").strip()
        if not name:
            return None
        row = self.kj.ledger.db.execute(
            "SELECT DISTINCT n.id FROM nodes n LEFT JOIN node_aliases a ON a.node_id=n.id "
            "WHERE n.status='active' AND n.kind='person' AND (lower(n.name)=lower(?) OR lower(a.alias)=lower(?)) "
            "ORDER BY n.created_at LIMIT 1", (name, name)).fetchone()
        return row["id"] if row else None

    def ensure_person(self, name: str) -> str:
        name = (name or "").strip()
        if not name:
            raise PeopleError("missing_name", "名字不能为空")
        if name == "我":
            return ME
        found = self.find_person(name)
        if found:
            return found
        res = self._check(self._kj().create_node(name=name, kind="person", source=SOURCE))
        return res["node_id"]

    def _alive(self, person_id: str | None) -> str | None:
        """跟随 KJ 合并找到最终节点；节点没了返回 None。"""
        if not person_id or person_id == ME:
            return person_id
        row = self.kj.ledger.resolve(person_id)
        return row["id"] if row is not None and row["kind"] == "person" else None

    def profile_definitions(self, person_id: str) -> list[dict]:
        """当前有效的 AI 总结（正常只有一条；合并节点后可能暂时多条）。"""
        return [d for d in self.kj.ledger.definitions(person_id) if d.get("context_key") == PROFILE_CONTEXT]

    def profile_definition(self, person_id: str) -> dict | None:
        defs = self.profile_definitions(person_id)
        return defs[-1] if defs else None

    def person(self, person_id: str, *, with_voiceprints: bool = False) -> dict:
        person_id = self._alive(person_id) or ""
        data = self._load()
        if person_id == ME:
            info = {"id": ME, "name": "我", "isUser": True, "intro": "", "profile": "", "aliases": []}
        else:
            row = self.kj.ledger.node(person_id) if person_id else None
            if row is None:
                raise PeopleError("person_not_found", "没有这个人")
            profile = self.profile_definition(person_id)
            info = {"id": person_id, "name": row["name"], "isUser": False, "intro": row["summary"] or "",
                    "profile": profile["text"] if profile else "",
                    "profileUpdatedAt": profile["created_at"] if profile else None,
                    "aliases": [a["alias"] for a in self.kj.ledger.aliases(person_id)]}
        entry = data["persons"].get(person_id, {})
        info["voiceprints"] = len(entry.get("voiceprints", []))
        info["slots"] = sorted(k for k, v in data["slots"].items() if self._alive(v.get("personId")) == person_id)
        # 语言：登记的（人物页确认过）+ 推测票数（App 没登记语言时逐段推测，累计在他的声音块上）
        info["language"] = entry.get("language") or ""
        votes: dict[str, int] = {}
        for key in info["slots"]:
            for lang, n in (data["slots"][key].get("langVotes") or {}).items():
                votes[lang] = votes.get(lang, 0) + int(n)
        info["languageVotes"] = votes
        info["languageGuess"] = max(votes, key=votes.get) if votes and not info["language"] else ""
        if with_voiceprints:
            info["vectors"] = [v["vector"] for v in entry.get("voiceprints", [])]
        return info

    def people(self) -> list[dict]:
        rows = self.kj.ledger.db.execute(
            "SELECT id FROM nodes WHERE status='active' AND kind='person' ORDER BY updated_at DESC").fetchall()
        data = self._load()
        known = {r["id"] for r in rows}
        # 只列旁听里出现过的人（KJ 里书中人物也是 person，不该混进来）
        hidden = set(data.get("hidden") or [])
        ids = [pid for pid in known if pid not in hidden and (pid in data["persons"]
               or any(self._alive(v.get("personId")) == pid for v in data["slots"].values()))]
        return [self.person(ME)] + [self.person(pid) for pid in ids]

    # ─────────────── 编辑 ───────────────

    def update_person(self, person_id: str, *, name: str | None = None, intro: str | None = None,
                      profile: str | None = None, language: str | None = None) -> dict:
        person_id = self._alive(person_id)
        if not person_id or person_id == ME:
            raise PeopleError("person_not_found" if not person_id else "readonly", "「我」不能改名，也没有 KJ 页")
        with self._lock:
            if name is not None and name.strip():
                other = self.find_person(name)
                if other and other != person_id:
                    return self.merge(person_id, into=other)
                self._check(self._kj().update_node(person_id, name=name.strip()))
            if intro is not None:
                self._check(self._kj().update_node(person_id, summary=intro.strip()))
            if profile is not None and profile.strip():
                self.write_profile(person_id, profile.strip(), by="user")
            if language is not None:
                self.set_language(person_id, language)
        return self.person(person_id)

    def set_language(self, person_id: str, language: str) -> None:
        """登记（确认）这个人说的语言，如 "ja-JP"；空串 = 取消，回到逐段推测。"""
        language = (language or "").strip()
        if language and not LANGUAGE_RE.fullmatch(language):
            raise PeopleError("bad_language", f"看不懂的语言代码：{language}")
        with self._lock:
            data = self._load()
            entry = data["persons"].setdefault(person_id, {"voiceprints": []})
            if language:
                entry["language"] = language
            else:
                entry.pop("language", None)
            entry["updatedAt"] = _now()
            self._save(data)

    def write_profile(self, person_id: str, text: str, *, by: str) -> None:
        """写 AI 总结（用户手改也走这里）：取代当前全部有效的那几条，保证只剩一条。"""
        previous = [d["id"] for d in self.profile_definitions(person_id)]
        source = dict(SOURCE, author=by)
        self._check(self._kj().add_definition(person_id, text=text[:4000], source=source, context_key=PROFILE_CONTEXT,
                                              decision="supersede" if previous else "",
                                              supersedes=previous if previous else ""))

    def merge(self, person_id: str, *, into: str) -> dict:
        """两个人其实是同一个：KJ 合并节点，声纹与声音块并过去。"""
        person_id, into = self._alive(person_id), self._alive(into)
        if not person_id or not into or person_id == into:
            raise PeopleError("bad_merge", "合并对象不对")
        with self._lock:
            if person_id != ME and into != ME:
                source_row = self.kj.ledger.node(person_id)
                target_row = self.kj.ledger.node(into)
                self._check(self._kj().merge_node(person_id, into=into, reason="旁听：同一个人"))
                # KJ 合并会搬记录 / 定义 / 关系 / 卡片，但不搬介绍，也不收拢两条 AI 总结 —— 这里补齐
                intro_a, intro_b = (target_row["summary"] or "").strip(), (source_row["summary"] or "").strip()
                if intro_b and intro_b not in intro_a:
                    merged_intro = (intro_a + "\n" + intro_b).strip() if intro_a else intro_b
                    self._check(self._kj().update_node(into, summary=merged_intro[:2000]))
                if source_row["name"] != target_row["name"]:
                    aliases = [a["alias"] for a in self.kj.ledger.aliases(into)]
                    if source_row["name"] not in aliases:
                        self._check(self._kj().update_node(into, aliases=aliases + [source_row["name"]]))
                profiles = self.profile_definitions(into)
                if len(profiles) > 1:
                    combined = "\n\n".join(d["text"] for d in profiles)
                    self.write_profile(into, combined, by="merge")
            data = self._load()
            source_entry = data["persons"].pop(person_id, {})
            moved = source_entry.get("voiceprints", [])
            target = data["persons"].setdefault(into, {"voiceprints": []})
            if source_entry.get("language") and not target.get("language"):
                target["language"] = source_entry["language"]
            target["voiceprints"] = (target.get("voiceprints", []) + moved)[-MAX_VOICEPRINTS:]
            target["updatedAt"] = _now()
            for slot in data["slots"].values():
                if slot.get("personId") == person_id:
                    slot["personId"] = into
            self._save(data)
        return self.person(into)

    def _purge_rows(self, slot_keys: set) -> int:
        """时间轴上删掉这些声音块的全部句子（调用方持锁）。返回删掉的句数。"""
        if not slot_keys:
            return 0
        removed = 0
        for path in sorted((self.root / "timeline").glob("*.jsonl")):
            lines = path.read_text(encoding="utf-8").splitlines()
            keep = []
            for line in lines:
                try:
                    row = json.loads(line)
                except ValueError:
                    keep.append(line)
                    continue
                if row.get("slotKey") in slot_keys:
                    removed += 1
                else:
                    keep.append(line)
            if len(keep) != len(lines):
                tmp = path.with_suffix(".tmp")
                tmp.write_text("".join(line + "\n" for line in keep), encoding="utf-8")
                tmp.replace(path)
        return removed

    def delete_slot(self, slot_key: str) -> dict:
        """删掉一个声音块（还没定人的「说话人N」）的全部句子。"""
        slot_key = (slot_key or "").strip()
        if not slot_key:
            raise PeopleError("missing_slot", "缺少声音块编号")
        with self._lock:
            data = self._load()
            data["slots"].pop(slot_key, None)
            self._save(data)
            rows = self._purge_rows({slot_key})
        return {"slotKey": slot_key, "utterances": rows}

    def delete_person(self, person_id: str, *, purge: bool = False) -> dict:
        """从旁听里删掉这个人（2026-09-27 用户：「每个说话人打开后可以进行删除」）：声纹删掉、他名下的声音块
        退回未定人、人物列表里不再出现。KJ 人物页与过去的对话记录不动（KJ 是只增账本，没有删节点），
        要彻底删就在 Obsidian 里删那一页。以后再把某个声音块定成同名的人，会重新出现。"""
        person_id = self._alive(person_id)
        if not person_id or person_id == ME:
            raise PeopleError("bad_delete", "不能删除「我」或不存在的人")
        with self._lock:
            data = self._load()
            removed = len(data["persons"].pop(person_id, {}).get("voiceprints", []))
            owned = {k for k, v in data["slots"].items() if self._alive(v.get("personId")) == person_id}
            freed = len(owned)
            for key in owned:
                if purge:
                    data["slots"].pop(key, None)       # 连同他的话一起删：声音块也不留
                else:
                    data["slots"][key]["personId"] = None
            hidden = [h for h in (data.get("hidden") or []) if h != person_id] + [person_id]
            data["hidden"] = hidden[-500:]
            self._save(data)
            rows = self._purge_rows(owned) if purge else 0
        return {"deleted": person_id, "voiceprints": removed, "slots": freed, "utterances": rows}

    def assign_slot(self, slot_key: str, *, name: str | None = None, person_id: str | None = None,
                    vector: list | None = None) -> dict:
        """给一个声音块定人：已有同名的人 → 就是他（「不同的块设成同一个名字就当作一个人」）；否则新建。
        这个块的声纹向量并进这个人的声纹库，之后 App 就能靠比对认出他。"""
        slot_key = (slot_key or "").strip()
        if not slot_key:
            raise PeopleError("missing_slot", "缺少声音块编号")
        with self._lock:
            target = self._alive(person_id) if person_id else self.ensure_person(name or "")
            if not target:
                raise PeopleError("person_not_found", "没有这个人")
            data = self._load()
            slot = data["slots"].setdefault(slot_key, {"firstSeen": _now(), "lastSeen": _now(), "utterances": 0})
            slot["personId"] = target
            if target in (data.get("hidden") or []):
                data["hidden"] = [h for h in data["hidden"] if h != target]   # 又被定回来：重新出现
            vec = vector or slot.get("vector")
            if vec and target != ME:
                self._add_voiceprint(data, target, vec, source=slot_key)
            self._save(data)
        return self.person(target)

    def unassign_slot(self, slot_key: str) -> None:
        with self._lock:
            data = self._load()
            if slot_key in data["slots"]:
                data["slots"][slot_key]["personId"] = None
                self._save(data)

    def add_voiceprint(self, person_id: str, vector: list, *, source: str) -> None:
        with self._lock:
            data = self._load()
            self._add_voiceprint(data, person_id, vector, source=source)
            self._save(data)

    @staticmethod
    def _add_voiceprint(data: dict, person_id: str, vector: list, *, source: str) -> None:
        if not isinstance(vector, list) or not vector or len(vector) > 1024:
            return
        entry = data["persons"].setdefault(person_id, {"voiceprints": []})
        prints = [v for v in entry.get("voiceprints", []) if v.get("from") != source]
        prints.append({"vector": [round(float(x), 6) for x in vector], "from": source, "addedAt": _now()})
        entry["voiceprints"] = prints[-MAX_VOICEPRINTS:]
        entry["updatedAt"] = _now()

    def voiceprints(self) -> list[dict]:
        """App 下载用：每个人的名字与全部声纹向量。"""
        data = self._load()
        out = []
        for pid, entry in data["persons"].items():
            alive = self._alive(pid)
            if not alive:
                continue
            name = "我" if alive == ME else self.kj.ledger.node(alive)["name"]
            # 没声纹的人也列出来：App 要按人查登记的语言
            out.append({"personId": alive, "name": name, "language": entry.get("language") or "",
                        "vectors": [v["vector"] for v in entry.get("voiceprints", [])]})
        return out

    # ─────────────── 收窗口 ───────────────

    def ingest(self, window: dict, speakers: list[dict]) -> dict:
        """记时间轴 + 更新声音块 + 给在场的熟人各记一条 KJ 对话记录。返回 {slotKey: personId|None}。"""
        started = int(window.get("startedAt") or _now())
        with self._lock:
            data = self._load()
            for sp in speakers or []:
                key = str(sp.get("slotKey") or "")
                if not key:
                    continue
                slot = data["slots"].setdefault(key, {"personId": None, "firstSeen": started, "utterances": 0})
                slot["lastSeen"] = int(window.get("endedAt") or started)
                if isinstance(sp.get("vector"), list):
                    slot["vector"] = [round(float(x), 6) for x in sp["vector"]]
                claimed = self._alive(sp.get("personId")) if sp.get("personId") else None
                if claimed and not slot.get("personId"):
                    # App 靠声纹比对认出了他：块归他，并把这次的向量补进声纹库（越认越准）
                    slot["personId"] = claimed
                    if slot.get("vector") and claimed != ME:
                        self._add_voiceprint(data, claimed, slot["vector"], source=key)
            rows = []
            for u in window["utterances"]:
                key = u.get("slotKey") or ""
                if key:
                    slot = data["slots"].setdefault(key, {"personId": None, "firstSeen": started, "lastSeen": started,
                                                          "utterances": 0})
                    slot["utterances"] = slot.get("utterances", 0) + 1
                    lang = str(u.get("lang") or "")
                    if lang and not u.get("langConfirmed", True) and LANGUAGE_RE.fullmatch(lang):
                        votes = slot.setdefault("langVotes", {})
                        votes[lang] = votes.get(lang, 0) + 1
                    if u.get("isUser") and not data["slots"][key].get("personId"):
                        data["slots"][key]["personId"] = ME
                rows.append({"contract": UTTERANCE_CONTRACT, "id": uuid.uuid4().hex[:12],
                             "t0": started + int(float(u.get("t0") or 0) * 1000),
                             "t1": started + int(float(u.get("t1") or 0) * 1000),
                             "windowId": window["windowId"], "source": window.get("source") or "",
                             "slotKey": key, "isUser": bool(u.get("isUser")), "label": u.get("speaker") or "",
                             "text": u.get("text") or "", "lang": str(u.get("lang") or ""),
                             "langConfirmed": bool(u.get("langConfirmed", True))})
            self._save(data)
            path = self.root / "timeline" / (time.strftime("%Y-%m-%d", time.localtime(started / 1000)) + ".jsonl")
            # 逐段重转可能先于主线这一窗到（它在 App 空闲时后台跑，主线要等认人、凑窗）：那一段已按
            # 说话人的语言重转过，主线用「我的语言」转出来的同一段是错语言的残片（「Ye」「Whas」）——不再记，
            # 否则时间轴上同一句一真一假并存（2026-09-27 实测）。只看非本人的句子。
            revised = self._revised_spans(path, {r["slotKey"] for r in rows if r["slotKey"] and not r["isUser"]})
            dropped = [r for r in rows if not r["isUser"] and any(
                r["slotKey"] == key and r["t0"] < t1 + 300 and r["t1"] > t0 - 300 for key, t0, t1 in revised)]
            if dropped:
                rows = [r for r in rows if r not in dropped]
            self.last_superseded = len(dropped)   # 调用方记日志（本模块不写 log）
            with path.open("a", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            mapping = {k: self._alive(v.get("personId")) for k, v in data["slots"].items()
                       if k in {u.get("slotKey") for u in window["utterances"]}}
        self._record_conversation(window, rows, mapping)
        return mapping

    @staticmethod
    def _revised_spans(path: Path, keys: set) -> list:
        if not keys or not path.is_file():
            return []
        spans = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("revised") and row.get("slotKey") in keys:
                spans.append((row["slotKey"], int(row.get("t0") or 0), int(row.get("t1") or 0)))
        return spans

    def revise(self, slot_key: str, t0: int, t1: int, text: str, *, lang: str = "", confirmed: bool = True) -> int:
        """逐段重转的事后修正（App 空闲时后台转完才送来）：把这个声音块在 [t0, t1] 前后 0.3 秒内的句子
        换成一句新的（保留原窗口编号，标 revised）。返回替换掉的句数；一句都没对上也照样补记这一句。"""
        slot_key = (slot_key or "").strip()
        text = (text or "").strip()
        if not slot_key or not text or t1 < t0:
            raise PeopleError("bad_revision", "缺少声音块编号 / 文字，或时间不对")
        if lang and not LANGUAGE_RE.fullmatch(lang):
            raise PeopleError("bad_language", f"看不懂的语言代码：{lang}")
        pad = 300
        with self._lock:
            path = self.root / "timeline" / (time.strftime("%Y-%m-%d", time.localtime(t0 / 1000)) + ".jsonl")
            rows = []
            if path.is_file():
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
            hit = [r for r in rows if r.get("slotKey") == slot_key
                   and int(r.get("t0") or 0) < t1 + pad and int(r.get("t1") or 0) > t0 - pad]
            keep = [r for r in rows if r not in hit]
            # 没对上（重转先到）：标签借同一声音块别的句子的，别让时间轴上出现没名字的块
            first = hit[0] if hit else next((r for r in reversed(rows) if r.get("slotKey") == slot_key and r.get("label")), {})
            keep.append({"contract": UTTERANCE_CONTRACT, "id": uuid.uuid4().hex[:12], "t0": int(t0), "t1": int(t1),
                         "windowId": (first.get("windowId") if hit else None) or "revise", "source": first.get("source") or "",
                         "slotKey": slot_key, "isUser": bool(first.get("isUser")), "label": first.get("label") or "",
                         "text": text[:1200], "lang": lang, "langConfirmed": bool(confirmed), "revised": True})
            keep.sort(key=lambda r: int(r.get("t0") or 0))
            tmp = path.with_suffix(".tmp")
            tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep), encoding="utf-8")
            tmp.replace(path)
            if lang and not confirmed:
                data = self._load()
                slot = data["slots"].setdefault(slot_key, {"personId": None, "firstSeen": int(t0), "lastSeen": int(t1),
                                                           "utterances": 0})
                votes = slot.setdefault("langVotes", {})
                votes[lang] = votes.get(lang, 0) + 1
                self._save(data)
        return len(hit)

    def _record_conversation(self, window: dict, rows: list[dict], mapping: dict) -> None:
        present = {pid for pid in mapping.values() if pid and pid != ME}
        if not present:
            return
        transcript = self.render_lines(rows, mapping)
        for pid in present:
            self._kj().add_record(pid, text=transcript[:6000], kind="conversation",
                                  source=dict(SOURCE, window=window["windowId"]),
                                  occurred_at=int(window.get("startedAt") or _now()),
                                  dedupe_key=f"ambient:{window['windowId']}:{pid}")

    # ─────────────── 读 ───────────────

    def _names(self) -> dict:
        names = {ME: "我"}
        for row in self.kj.ledger.db.execute("SELECT id, name FROM nodes WHERE kind='person'"):
            names[row["id"]] = row["name"]
        return names

    def render_lines(self, rows: list[dict], mapping: dict | None = None) -> str:
        names = self._names()
        slots = self._load()["slots"]
        out = []
        for row in rows:
            pid = (mapping or {}).get(row["slotKey"]) or self._alive(slots.get(row["slotKey"], {}).get("personId"))
            label = names.get(pid) or row.get("label") or "?"
            out.append(f"{time.strftime('%H:%M:%S', time.localtime(row['t0'] / 1000))} {label}：{row['text']}")
        return "\n".join(out)

    def _read_days(self, t_from: int, t_to: int) -> list[dict]:
        rows = []
        day = t_from - (t_from % 86_400_000) - 86_400_000
        seen = set()
        while day <= t_to + 86_400_000:
            name = time.strftime("%Y-%m-%d", time.localtime(day / 1000)) + ".jsonl"
            day += 86_400_000
            if name in seen:
                continue
            seen.add(name)
            path = self.root / "timeline" / name
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if t_from <= int(row.get("t0") or 0) <= t_to:
                    rows.append(row)
        rows.sort(key=lambda r: r["t0"])
        return rows

    def timeline(self, t_from: int, t_to: int) -> dict:
        """时间轴：每句带解析后的 personId / 名字；另附出现过的声音块与人。"""
        rows = self._read_days(t_from, t_to)
        slots = self._load()["slots"]
        names = self._names()
        speakers = {}
        for row in rows:
            pid = self._alive(slots.get(row["slotKey"], {}).get("personId"))
            if pid is None and row.get("isUser"):
                pid = ME
            row["personId"] = pid
            row["name"] = names.get(pid) if pid else None
            key = row["slotKey"] or ("user" if row.get("isUser") else "?")
            speakers.setdefault(key, {"slotKey": key, "personId": pid, "name": row["name"],
                                      "label": row.get("label") or "?"})
        return {"utterances": rows, "speakers": list(speakers.values())}

    def history(self, person_id: str, *, limit: int = 300) -> list[dict]:
        """这个人说过的话所在的全部窗口（含同窗口里所有人的话），新的在前。"""
        person_id = self._alive(person_id)
        slots = self._load()["slots"]
        mine = {k for k, v in slots.items() if self._alive(v.get("personId")) == person_id}
        files = sorted((self.root / "timeline").glob("*.jsonl"), reverse=True)
        windows: dict[str, list[dict]] = {}
        order: list[str] = []
        for path in files:
            day_rows = []
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    day_rows.append(json.loads(line))
                except ValueError:
                    continue
            # 逐段重转补记的句子没有所属窗口（windowId="revise"），按分钟各自成段 —— 以前它们全挤进一个
            # 「revise」窗口，跨了整场对话，把历史的先后顺序搅乱（2026-09-27 用户：「希望从新到旧」）
            def group(row: dict) -> str:
                wid = row.get("windowId") or ""
                return wid if wid and wid != "revise" else "revise:" + str(int(row.get("t0") or 0) // 60_000)
            hit = {group(r) for r in day_rows if r.get("slotKey") in mine or (person_id == ME and r.get("isUser"))}
            for row in day_rows:
                key = group(row)
                if key in hit:
                    if key not in windows:
                        windows[key] = []
                        order.append(key)
                    windows[key].append(row)
            if sum(len(v) for v in windows.values()) >= limit:
                break
        out = []
        for wid in sorted(order, key=lambda w: max(int(r.get("t0") or 0) for r in windows[w]), reverse=True):
            rows = sorted(windows[wid], key=lambda r: r["t0"])
            out.append({"windowId": wid, "t0": rows[0]["t0"], "t1": rows[-1]["t1"],
                        "lines": self.timeline(rows[0]["t0"], rows[-1]["t0"])["utterances"]})
        return out

    def clues(self, labels: list[str]) -> list[str]:
        """给 jev 的线索：本段里出现的熟人的介绍与 AI 总结。"""
        out = []
        for label in dict.fromkeys(labels):
            if label in ("我", "?", "") or label.startswith("说话人"):
                continue
            pid = self.find_person(label)
            if not pid:
                continue
            info = self.person(pid)
            parts = [p for p in (info["intro"], info["profile"]) if p]
            if parts:
                out.append(f"{info['name']}：" + "；".join(parts)[:500])
        return out


def _now() -> int:
    return round(time.time() * 1000)


def default_root() -> Path:
    base = Path(os.environ.get("CLAUDE_PROJECT", str(Path(__file__).resolve().parents[1])))
    return base / "state" / "ambient"
