"""ASR 假转写判据三处同步守卫。

背景(用户实测 2026-07-19):realtime 2.1 WebRTC 通话里,噪音一响就冒出一条
「关键词:Anki、笔迹、振假名、生词、假名」的**用户气泡**。根因是转写模型的
prompt-copy 幻觉——噪音让 VAD 误开一个没人声的音频轮,转写模型就把我们喂的热词
prompt 原样复读成"用户说的话"。

判据散在**三个文件**里:
  ① assistant.py            —— prompt 本体(喂给转写模型的那串)
  ② voice_realtime_relay.py —— _ASR_PROMPT_MIRROR(relay 侧闸门的判据)
  ③ rc-voicecall.js         —— VC_ASR_MIRROR(前端显示侧的判据)

事故正是③漂了:prompt 早换成「关键词:…」,而前端还只认旧 prompt 的锚点
「学习伴读通话/常说:这一页」,一个都不命中 → 前端过滤全程失效。
WebRTC 下转写事件是**直连数据通道回浏览器**的,relay 的闸只管"要不要生成回答",
管不住前端显示,所以③失效就等于没防护。

本测试断言三者一致,让下次改 prompt 忘了同步会直接红。
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSISTANT = ROOT / "_server_deploy" / "assistant.py"
RELAY = ROOT / "_server_deploy" / "voice_realtime_relay.py"
FRONT = ROOT / "_server_deploy" / "static" / "pdf" / "rc-voicecall.js"


def _strip_punct(s: str) -> str:
    return re.sub(r"[\s,，。、:：;；/·!！?？…\-]+", "", s or "")


class TestAsrGhostSync(unittest.TestCase):
    def setUp(self):
        self.assistant = ASSISTANT.read_text("utf-8")
        self.relay = RELAY.read_text("utf-8")
        self.front = FRONT.read_text("utf-8")

    def _asr_prompt(self) -> str:
        m = re.search(r'"model":\s*"gpt-4o-mini-transcribe",\s*\n\s*"prompt":\s*"([^"]+)"',
                      self.assistant)
        self.assertIsNotNone(m, "没在 assistant.py 找到转写 prompt(结构变了?)")
        return m.group(1)

    def _relay_mirror(self) -> str:
        m = re.search(r"_ASR_PROMPT_MIRROR = \((.*?)\)\s*#", self.relay, re.S)
        self.assertIsNotNone(m, "没在 relay 找到 _ASR_PROMPT_MIRROR")
        return "".join(re.findall(r'"([^"]*)"', m.group(1)))

    def _front_mirror(self) -> str:
        m = re.search(r"var VC_ASR_MIRROR = (.*?);", self.front, re.S)
        self.assertIsNotNone(m, "没在 rc-voicecall.js 找到 VC_ASR_MIRROR")
        return "".join(re.findall(r"'([^']*)'", m.group(1)))

    def test_prompt_included_in_relay_mirror(self):
        """喂出去的 prompt 必须在 relay 的判据串里 —— 否则 relay 认不出自己被复读。"""
        self.assertIn(self._asr_prompt(), self._relay_mirror())

    def test_front_mirror_matches_relay(self):
        """前端判据串必须与 relay 逐字一致(事故就是这里漂了)。"""
        self.assertEqual(self._front_mirror(), self._relay_mirror())

    def test_front_constants_match_relay(self):
        """锚点表与 LCS 阈值也要一致,否则两边裁决结果会分叉。"""
        m = re.search(r"_GHOST_LCS_MIN = (\d+)", self.relay)
        f = re.search(r"var VC_GHOST_LCS_MIN = (\d+)", self.front)
        self.assertEqual(m.group(1), f.group(1))
        r_anchor = re.search(r"_ASR_GHOST_ANCHORS = \((.*?)\)", self.relay, re.S).group(1)
        f_anchor = re.search(r"var VC_ASR_ANCHORS = \[(.*?)\]", self.front, re.S).group(1)
        self.assertEqual(re.findall(r'"([^"]*)"', r_anchor),
                         re.findall(r"'([^']*)'", f_anchor))

    def test_actual_ghost_is_caught(self):
        """用户实测那条假转写,必须被判为假(回归用例)。"""
        ghost = "关键词:Anki、笔迹、振假名、生词、假名"
        a, b = _strip_punct(ghost), _strip_punct(self._relay_mirror())
        import difflib
        m = difflib.SequenceMatcher(None, a, b, autojunk=False).find_longest_match(0, len(a), 0, len(b))
        self.assertGreaterEqual(m.size, 10, "用户实测的假转写居然没被判据命中")

    def test_real_speech_not_killed(self):
        """反向守卫:用户真会说的短句绝不能被误杀(高精度硬拒原则)。"""
        import difflib
        b = _strip_punct(self._relay_mirror())
        for say in ("下一页", "翻到第12页", "这一页讲了什么", "帮我做卡片",
                    "这页的关键词是什么", "读一下这段"):
            a = _strip_punct(say)
            m = difflib.SequenceMatcher(None, a, b, autojunk=False).find_longest_match(0, len(a), 0, len(b))
            self.assertLess(m.size, 10, f"真实语句「{say}」被误判成假转写")


if __name__ == "__main__":
    unittest.main()
