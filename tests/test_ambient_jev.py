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

import ambient_jev  # noqa: E402
import jev_judge  # noqa: E402


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
        self.old = (ambient_jev.CLAUDE_DIR, ambient_jev._predict, ambient_jev._ai, ambient_jev._spawn)
        ambient_jev.CLAUDE_DIR = self.tmp
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
        ambient_jev.CLAUDE_DIR, ambient_jev._predict, ambient_jev._ai, ambient_jev._spawn = self.old
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
