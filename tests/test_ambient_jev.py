"""/api/ambient/*：环境旁听的 jev 判断 → 动作 → 落盘（jev 与 AI 都替换成桩）。"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from flask import Flask

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))
sys.path.insert(0, str(ROOT / "scripts"))

import ambient_jev  # noqa: E402
import jev_judge  # noqa: E402
from ambient_people import AmbientPeople  # noqa: E402
from kj.service import KJService  # noqa: E402


def answers(**choices):
    out = {}
    for name, choice in choices.items():
        criteria = (ambient_jev.QUESTIONS.get(name) or ambient_jev.ROUTE_QUESTION)["criteria"]
        probabilities = {k: (0.9 if k == choice else 0.1 / (len(criteria) - 1)) for k in criteria}
        out[name] = {"choice": choice, "probability": 0.9, "probabilities": probabilities}
    return out


WINDOW = {
    "windowId": "w1", "startedAt": 1, "endedAt": 2, "speakerCount": 2, "locale": "zh-CN",
    "utterances": [
        {"speaker": "我", "isUser": True, "text": "下周三几点交报告来着", "t0": 1.0, "t1": 2.5},
        {"speaker": "说话人2", "isUser": False, "text": "下午五点前，发到老师邮箱", "t0": 3.0, "t1": 5.0},
        {"speaker": "小王", "isUser": False, "text": "我也还没写完", "t0": 6.0, "t1": 7.0},
    ],
}


class AmbientJevTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="amb"))
        self.vault = self.tmp / "vault"
        self.old = (ambient_jev.CLAUDE_DIR, ambient_jev._predict, ambient_jev._ai, ambient_jev._spawn, ambient_jev._PEOPLE)
        ambient_jev.CLAUDE_DIR = self.tmp
        self.kj = KJService(self.tmp / "kj.db", self.tmp / "vault" / "KJ", actor="test")
        ambient_jev._PEOPLE = AmbientPeople(self.tmp / "state" / "ambient", self.kj)
        self.env = dict(__import__("os").environ)
        __import__("os").environ["OBSIDIAN_VAULT"] = str(self.vault)
        ambient_jev._spawn = lambda target, *args: target(*args)   # 后台动作同步跑，便于断言
        self.calls: list[dict] = []
        self.ai_prompts: list[str] = []
        ambient_jev._ai = lambda prompt: (self.ai_prompts.append(prompt) or "问：几点交\n答：下午五点前")
        app = Flask(__name__)
        app.config.update(TESTING=True, SECRET_KEY="test")
        ambient_jev.register_ambient(app)
        self.client = app.test_client()

    def tearDown(self) -> None:
        self.kj.close()
        (ambient_jev.CLAUDE_DIR, ambient_jev._predict, ambient_jev._ai, ambient_jev._spawn,
         ambient_jev._PEOPLE) = self.old
        __import__("os").environ.clear()
        __import__("os").environ.update(self.env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def login(self) -> None:
        with self.client.session_transaction() as s:
            s["user_id"] = 1

    def stub(self, primary: dict, route: str = "knowledge") -> None:
        def predict(state, questions):
            self.calls.append({"state": state, "questions": sorted(questions)})
            if "route" in questions:
                return answers(route=route)
            return answers(**primary)
        ambient_jev._predict = predict

    def log_events(self) -> list[dict]:
        path = self.tmp / "state" / "ambient" / "log.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_requires_login(self):
        self.assertEqual(self.client.post("/api/ambient/judge", json=WINDOW).status_code, 401)

    def test_state_carries_speakers_and_user_marker(self):
        self.login()
        self.stub(dict(meaningful="meaningful", danger="none", question="none", record="skip"))
        reply = self.client.post("/api/ambient/judge", json=WINDOW).get_json()
        self.assertTrue(reply["ok"], reply)
        state = self.calls[0]["state"]
        self.assertIn("[00:01 我] 下周三几点交报告来着", state)
        self.assertIn("[00:03 说话人2]", state)
        self.assertIn("[00:06 小王] 我也还没写完", state)
        self.assertIn("多人说话", state)
        self.assertIn("熟人", state)
        self.assertEqual(self.calls[0]["questions"], ["danger", "meaningful", "question", "record"])

    def test_question_routes_to_ai_and_lands_in_feed_and_vault(self):
        self.login()
        self.stub(dict(meaningful="meaningful", danger="none", question="question", record="transcript"))
        reply = self.client.post("/api/ambient/judge", json=WINDOW).get_json()
        self.assertEqual(reply["actions"], ["save_transcript", "route_question"])
        self.assertEqual(len(self.ai_prompts), 1)
        kinds = [row["kind"] for row in self.client.get("/api/ambient/feed").get_json()["entries"]]
        self.assertEqual(kinds, ["window", "answer"])
        note = next((self.vault / "AI助手专用" / "环境旁听").glob("*.md")).read_text(encoding="utf-8")
        self.assertIn("AI 解答", note)
        self.assertIn("下午五点前", note)

    def test_personal_question_is_recorded_not_answered(self):
        self.login()
        self.stub(dict(meaningful="meaningful", danger="none", question="question", record="skip"), route="personal")
        self.client.post("/api/ambient/judge", json=WINDOW)
        self.assertEqual(self.ai_prompts, [])
        entries = self.client.get("/api/ambient/feed").get_json()["entries"]
        self.assertEqual(entries[-1]["kind"], "question")
        self.assertEqual(entries[-1]["route"], "personal")

    def test_immediate_danger_asks_app_to_record(self):
        self.login()
        self.stub(dict(meaningful="meaningful", danger="immediate", question="none", record="skip"))
        reply = self.client.post("/api/ambient/judge", json=WINDOW).get_json()
        self.assertEqual(reply["actions"], ["danger_record", "save_audio"])

    def test_summary_after_enough_meaningful_windows(self):
        self.login()
        self.stub(dict(meaningful="meaningful", danger="none", question="none", record="skip"))
        for index in range(ambient_jev.SUMMARY_EVERY_WINDOWS):
            self.client.post("/api/ambient/judge", json=dict(WINDOW, windowId=f"w{index}"))
        context = self.client.get("/api/ambient/context").get_json()
        self.assertTrue(context["summary"])
        self.assertEqual(context["pending"], 0)
        self.assertEqual(len(self.ai_prompts), 1)   # 攒够才写，不是每窗都写

    def test_jev_unavailable_is_loud(self):
        self.login()

        def boom(state, questions):
            raise ambient_jev.JevUnavailable("credential_unavailable")
        ambient_jev._predict = boom
        response = self.client.post("/api/ambient/judge", json=WINDOW)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.log_events()[-1]["event"], "judge_failed")

    def test_rejects_empty_and_oversized(self):
        self.login()
        self.stub(dict(meaningful="noise", danger="none", question="none", record="skip"))
        self.assertEqual(self.client.post("/api/ambient/judge", json={"utterances": []}).status_code, 400)
        huge = dict(WINDOW, utterances=[{"speaker": "我", "text": "字" * 1200}] * 9)
        self.assertEqual(self.client.post("/api/ambient/judge", json=huge).status_code, 400)
        self.assertEqual(self.log_events()[-1]["event"], "judge_rejected")

    def test_noise_does_not_fill_feed(self):
        self.login()
        self.stub(dict(meaningful="noise", danger="none", question="none", record="skip"))
        self.client.post("/api/ambient/judge", json=WINDOW)
        self.assertEqual(self.client.get("/api/ambient/feed").get_json()["entries"], [])

    def test_source_is_recorded(self):
        self.login()
        self.stub(dict(meaningful="meaningful", danger="none", question="none", record="skip"))
        self.client.post("/api/ambient/judge", json=dict(WINDOW, source="ipad-call"))
        self.assertEqual(self.log_events()[-1]["source"], "ipad-call")

    def test_jev_module_is_standalone(self):
        # 不依赖语音进程：没有密钥文件时明确抛 JevUnavailable，而不是返回「没事」
        old = __import__("os").environ.get("JEV_KEY_FILE")
        __import__("os").environ["JEV_KEY_FILE"] = str(self.tmp / "missing.txt")
        try:
            with self.assertRaises(jev_judge.JevUnavailable):
                jev_judge.predict("状态", {"q": {"type": "choice", "criteria": {"a": "", "b": ""}}})
            with self.assertRaises(ValueError):
                jev_judge.predict("状态", {"q": {"type": "choice", "criteria": {"a": ""}}})
        finally:
            if old is None:
                __import__("os").environ.pop("JEV_KEY_FILE", None)
            else:
                __import__("os").environ["JEV_KEY_FILE"] = old

    def test_top_choice_rejects_unknown_labels(self):
        with self.assertRaises(ValueError):
            ambient_jev.top_choice({"probabilities": {"bogus": 1.0}}, {"none": ""})


if __name__ == "__main__":
    unittest.main()


class AmbientPeopleTests(AmbientJevTests):
    """声音块 ↔ KJ 人物 ↔ 时间轴。继承上面的桩（jev / AI / 临时 KJ）。"""

    def window(self, wid, slot_other="s1:1", other_label="说话人2", **extra):
        return dict(WINDOW, windowId=wid, startedAt=1_700_000_000_000, endedAt=1_700_000_010_000, utterances=[
            {"speaker": "我", "isUser": True, "text": "周末去爬山吗", "t0": 1, "t1": 2, "slotKey": "s1:0"},
            {"speaker": other_label, "isUser": False, "text": "好啊，周六早上八点", "t0": 3, "t1": 5, "slotKey": slot_other},
        ], **extra)

    def judge(self, body):
        self.stub(dict(meaningful="meaningful", danger="none", question="none", record="skip"))
        reply = self.client.post("/api/ambient/judge", json=body).get_json()
        self.assertTrue(reply["ok"], reply)
        return reply

    def timeline(self):
        return self.client.get("/api/ambient/timeline?from=1699990000000&to=1700090000000").get_json()

    def test_timeline_and_naming_blocks(self):
        self.login()
        self.judge(self.window("w1"))
        rows = self.timeline()["utterances"]
        self.assertEqual([r["name"] for r in rows], ["我", None])
        self.assertEqual(rows[1]["t0"], 1_700_000_003_000)
        person = self.client.post("/api/ambient/slots/assign", json={"slotKey": "s1:1", "name": "小王"}).get_json()["person"]
        node = self.kj.ledger.node(person["id"])
        self.assertEqual((node["name"], node["kind"]), ("小王", "person"))
        # 名字在读的时候解析：事后起名，历史也跟着变
        self.assertEqual(self.timeline()["utterances"][1]["name"], "小王")
        # 另一个块设成同一个名字 → 同一个人
        self.judge(self.window("w2", slot_other="s2:3"))
        again = self.client.post("/api/ambient/slots/assign", json={"slotKey": "s2:3", "name": "小王"}).get_json()["person"]
        self.assertEqual(again["id"], person["id"])
        self.assertEqual(sorted(again["slots"]), ["s1:1", "s2:3"])

    def test_known_person_gets_kj_record_markdown_and_jev_clue(self):
        self.login()
        pid = self.client.post("/api/ambient/slots/assign", json={"slotKey": "s1:1", "name": "小王"}).get_json()["person"]["id"]
        self.client.patch(f"/api/ambient/people/{pid}", json={"intro": "大学同学，在做芯片"})
        reply = self.judge(self.window("w1"))
        self.assertEqual(reply["names"]["s1:1"]["name"], "小王")
        state = self.calls[-1]["state"]
        self.assertIn("[00:03 小王] 好啊", state)
        self.assertIn("小王：大学同学，在做芯片", state)
        records = self.kj.ledger.records(pid)
        self.assertEqual([r["kind"] for r in records], ["conversation"])
        self.assertIn("小王：好啊，周六早上八点", records[0]["text"])
        page = next((self.tmp / "vault" / "KJ").rglob("*小王*.md")).read_text(encoding="utf-8")
        self.assertIn("大学同学，在做芯片", page)
        # 同一窗口重发不重复记
        self.judge(self.window("w1"))
        self.assertEqual(len(self.kj.ledger.records(pid)), 1)

    def test_rename_to_existing_merges_kj_nodes(self):
        self.login()
        a = self.client.post("/api/ambient/slots/assign", json={"slotKey": "s1:1", "name": "王老师"}).get_json()["person"]["id"]
        b = self.client.post("/api/ambient/slots/assign", json={"slotKey": "s2:1", "name": "老王"}).get_json()["person"]["id"]
        self.client.patch(f"/api/ambient/people/{a}", json={"intro": "数学老师", "profile": "关系：老师"})
        self.client.patch(f"/api/ambient/people/{b}", json={"intro": "住隔壁", "profile": "商量过：周六爬山"})
        merged = self.client.patch(f"/api/ambient/people/{b}", json={"name": "王老师"}).get_json()["person"]
        self.assertEqual(merged["id"], a)
        self.assertEqual(self.kj.ledger.node(b)["merged_into"], a)
        self.assertIn("数学老师", merged["intro"])
        self.assertIn("住隔壁", merged["intro"])
        self.assertIn("老王", merged["aliases"])
        self.assertEqual(sorted(merged["slots"]), ["s1:1", "s2:1"])
        # 两条 AI 整理收拢成一条
        profiles = [d for d in self.kj.ledger.definitions(a) if d["context_key"] == "ambient-profile"]
        self.assertEqual(len(profiles), 1)
        self.assertIn("周六爬山", profiles[0]["text"])
        self.assertIn("关系：老师", profiles[0]["text"])

    def test_app_identified_speaker_feeds_voiceprints(self):
        self.login()
        pid = self.client.post("/api/ambient/slots/assign", json={"slotKey": "s0:1", "name": "小王",
                                                                  "vector": [0.1, 0.2]}).get_json()["person"]["id"]
        self.judge(self.window("w1", speakers=[{"slotKey": "s1:1", "personId": pid, "vector": [0.3, 0.4]}]))
        prints = self.client.get("/api/ambient/voiceprints").get_json()["people"]
        self.assertEqual(prints[0]["name"], "小王")
        self.assertEqual(len(prints[0]["vectors"]), 2)
        self.assertEqual(self.timeline()["utterances"][1]["name"], "小王")

    def test_summarize_writes_profile_into_kj(self):
        self.login()
        pid = self.client.post("/api/ambient/slots/assign", json={"slotKey": "s1:1", "name": "小王"}).get_json()["person"]["id"]
        self.judge(self.window("w1"))
        ambient_jev._ai = lambda prompt: "关系：同学\n商量过：周六早上八点爬山\n近况：未知"
        reply = self.client.post(f"/api/ambient/people/{pid}/summarize").get_json()
        self.assertTrue(reply["ok"], reply)
        detail = self.client.get(f"/api/ambient/people/{pid}").get_json()
        self.assertIn("周六早上八点爬山", detail["person"]["profile"])
        self.assertEqual(detail["history"][0]["lines"][0]["name"], "我")

    def test_book_persons_are_not_listed(self):
        self.login()
        self.kj.create_node(name="高斯", kind="person")
        self.client.post("/api/ambient/slots/assign", json={"slotKey": "s1:1", "name": "小王"})
        names = [p["name"] for p in self.client.get("/api/ambient/people").get_json()["people"]]
        self.assertEqual(names, ["我", "小王"])

    def test_language_guess_votes_then_confirm(self):
        self.login()
        pid = self.client.post("/api/ambient/slots/assign", json={"slotKey": "s1:1", "name": "田中"}).get_json()["person"]["id"]
        for wid in ("w1", "w2"):
            body = self.window(wid)
            body["utterances"][1].update({"lang": "ja-JP", "langConfirmed": False})
            self.judge(body)
        person = self.client.get(f"/api/ambient/people/{pid}").get_json()["person"]
        self.assertEqual(person["languageVotes"], {"ja-JP": 2})
        self.assertEqual(person["languageGuess"], "ja-JP")
        tanaka = [u for u in self.timeline()["utterances"] if u["name"] == "田中"]
        self.assertEqual({u["lang"] for u in tanaka}, {"ja-JP"})
        confirmed = self.client.patch(f"/api/ambient/people/{pid}", json={"language": "ja-JP"}).get_json()["person"]
        self.assertEqual((confirmed["language"], confirmed["languageGuess"]), ("ja-JP", ""))
        prints = self.client.get("/api/ambient/voiceprints").get_json()["people"]
        self.assertEqual([(p["name"], p["language"]) for p in prints], [("田中", "ja-JP")])
        bad = self.client.patch(f"/api/ambient/people/{pid}", json={"language": "日本語"})
        self.assertEqual(bad.status_code, 400)

    def test_delete_person_frees_blocks_and_hides_until_reassigned(self):
        self.login()
        self.judge(self.window("w1"))
        pid = self.client.post("/api/ambient/slots/assign", json={"slotKey": "s1:1", "name": "小王"}).get_json()["person"]["id"]
        reply = self.client.delete(f"/api/ambient/people/{pid}").get_json()
        self.assertEqual(reply["slots"], 1)
        names = [p["name"] for p in self.client.get("/api/ambient/people").get_json()["people"]]
        self.assertNotIn("小王", names)
        self.assertEqual([r["name"] for r in self.timeline()["utterances"]], ["我", None])   # 块退回未定人
        self.assertEqual(self.client.delete("/api/ambient/people/me").status_code // 100, 4)  # 不能删「我」
        self.client.post("/api/ambient/slots/assign", json={"slotKey": "s1:1", "name": "小王"})
        names = [p["name"] for p in self.client.get("/api/ambient/people").get_json()["people"]]
        self.assertIn("小王", names)   # 又定回来就重新出现

    def test_delete_person_with_all_utterances_and_unnamed_block(self):
        self.login()
        self.judge(self.window("w1"))
        self.judge(self.window("w2", slot_other="s1:2"))
        pid = self.client.post("/api/ambient/slots/assign", json={"slotKey": "s1:1", "name": "小王"}).get_json()["person"]["id"]
        reply = self.client.delete(f"/api/ambient/people/{pid}?purge=1").get_json()
        self.assertEqual((reply["slots"], reply["utterances"]), (1, 1))
        texts = [(r["slotKey"], r["text"]) for r in self.timeline()["utterances"]]
        self.assertNotIn("s1:1", [k for k, _ in texts])
        reply = self.client.post("/api/ambient/slots/delete", json={"slotKey": "s1:2"}).get_json()
        self.assertEqual(reply["utterances"], 1)
        self.assertEqual({k for k, _ in (r for r in [(r["slotKey"], r["text"]) for r in self.timeline()["utterances"]])}, {"s1:0"})

    def test_history_newest_first_with_revised_rows_grouped_by_minute(self):
        self.login()
        pid = self.client.post("/api/ambient/slots/assign", json={"slotKey": "s1:1", "name": "田中"}).get_json()["person"]["id"]
        for i, wid in enumerate(["w1", "w2"]):
            body = self.window(wid)
            body["startedAt"] = 1_700_000_000_000 + i * 600_000
            body["endedAt"] = body["startedAt"] + 10_000
            self.judge(body)
        # 两条不对应任何窗口的补记，分别在两窗之前和之后
        for t in (1_699_999_000_000, 1_700_000_900_000):
            self.client.post("/api/ambient/revise", json={"slotKey": "s1:1", "t0": t, "t1": t + 2000,
                                                          "text": "補記", "lang": "ja-JP", "langConfirmed": False})
        history = self.client.get(f"/api/ambient/people/{pid}").get_json()["history"]
        starts = [h["t0"] for h in history]
        self.assertEqual(starts, sorted(starts, reverse=True))
        self.assertEqual(len(history), 4)

    def test_revise_before_window_supersedes_late_stream_rows(self):
        # 重转先到（App 空闲时后台转完就送），主线那一窗后到：同一块同一时段的主线残片不再记
        self.login()
        self.client.post("/api/ambient/slots/assign", json={"slotKey": "s1:1", "name": "田中"})
        reply = self.client.post("/api/ambient/revise", json={
            "slotKey": "s1:1", "t0": 1_700_000_003_000, "t1": 1_700_000_005_000,
            "text": "土曜日の朝八時にしよう", "lang": "ja-JP", "langConfirmed": False}).get_json()
        self.assertEqual(reply["replaced"], 0)
        self.judge(self.window("w1"))
        rows = self.timeline()["utterances"]
        tanaka = [r for r in rows if r["name"] == "田中"]
        self.assertEqual([r["text"] for r in tanaka], ["土曜日の朝八時にしよう"])
        self.assertEqual(rows[0]["text"], "周末去爬山吗")   # 别人的句子照记

    def test_revise_replaces_stream_text_for_that_block(self):
        self.login()
        self.client.post("/api/ambient/slots/assign", json={"slotKey": "s1:1", "name": "田中"})
        self.judge(self.window("w1"))
        reply = self.client.post("/api/ambient/revise", json={
            "slotKey": "s1:1", "t0": 1_700_000_003_000, "t1": 1_700_000_005_000,
            "text": "土曜日の朝八時にしよう", "lang": "ja-JP", "langConfirmed": False}).get_json()
        self.assertEqual(reply["replaced"], 1)
        rows = self.timeline()["utterances"]
        tanaka = [r for r in rows if r["name"] == "田中"]
        self.assertEqual([r["text"] for r in tanaka], ["土曜日の朝八時にしよう"])
        self.assertTrue(tanaka[0]["revised"])
        self.assertEqual(rows[0]["text"], "周末去爬山吗")   # 别人的句子不动
        person = self.client.get("/api/ambient/people").get_json()["people"][1]
        self.assertEqual(person["languageVotes"], {"ja-JP": 1})

