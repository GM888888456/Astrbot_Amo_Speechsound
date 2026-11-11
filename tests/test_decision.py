# -*- coding: utf-8 -*-
"""决策层测试：开口与否、切段、清洗、语气。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from decision import (  # noqa: E402
    SCENE_LABELS,
    SpeakContext,
    decide,
    detect_scene,
    instructions_for,
    sanitize_for_speech,
    score_speech,
    split_text,
)


def ctx(**kwargs):
    base = dict(text="", private=True, hour=21, quota_left=-1, seconds_since_last=1e9)
    base.update(kwargs)
    return SpeakContext(**base)


class SanitizeTests(unittest.TestCase):
    def test_strips_code_fence(self):
        text = "给你看\n```python\nprint(1)\n```\n好看吗"
        out = sanitize_for_speech(text)
        self.assertNotIn("print", out)
        self.assertIn("好看吗", out)

    def test_strips_url(self):
        out = sanitize_for_speech("链接在这 https://example.com/a?b=1 你点开看看")
        self.assertNotIn("http", out)
        self.assertIn("你点开看看", out)

    def test_strips_markdown_marks(self):
        out = sanitize_for_speech("**真的**吗 _嗯_")
        self.assertEqual(out, "真的吗 嗯")

    def test_strips_emoji(self):
        out = sanitize_for_speech("晚安啦😴🌙")
        self.assertEqual(out, "晚安啦")

    def test_strips_mentions_and_cq(self):
        out = sanitize_for_speech("@小明 [CQ:face,id=1] 你来了")
        self.assertEqual(out, "你来了")

    def test_keeps_plain_chat(self):
        text = "今天有点累，想早点睡。"
        self.assertEqual(sanitize_for_speech(text), text)


class SplitTests(unittest.TestCase):
    def test_short_text_single_segment(self):
        self.assertEqual(split_text("晚安", 80), ["晚安"])

    def test_splits_on_punctuation(self):
        segments = split_text("第一句。第二句！第三句？", 6)
        self.assertEqual(len(segments), 3)

    def test_hard_cut_when_no_punctuation(self):
        segments = split_text("啊" * 25, 10)
        self.assertTrue(all(len(item) <= 10 for item in segments))
        self.assertEqual("".join(segments), "啊" * 25)

    def test_empty(self):
        self.assertEqual(split_text("   ", 10), [])


class DecideTests(unittest.TestCase):
    def test_empty_blocked(self):
        result = decide(ctx(text="```code```"))
        self.assertFalse(result.allowed)

    def test_code_blocked(self):
        result = decide(ctx(text="看这个 https://x.com 挺好"))
        self.assertFalse(result.allowed)
        self.assertIn("代码", result.reason)

    def test_too_long_blocked(self):
        result = decide(ctx(text="嗯" * 200, max_chars=80))
        self.assertFalse(result.allowed)
        self.assertIn("太长", result.reason)

    def test_group_blocked_by_default(self):
        result = decide(ctx(text="晚安啦", private=False, group_enable=False))
        self.assertFalse(result.allowed)
        self.assertIn("群聊", result.reason)

    def test_group_allowed_when_enabled(self):
        result = decide(ctx(text="晚安啦", private=False, group_enable=True), manual=True)
        self.assertTrue(result.allowed)

    def test_quota_exhausted(self):
        result = decide(ctx(text="晚安啦", quota_left=0))
        self.assertFalse(result.allowed)
        self.assertIn("额度", result.reason)

    def test_manual_ignores_threshold(self):
        """Amo 主动调用时不该被阈值按住。"""
        result = decide(ctx(text="嗯", hour=14, private=True), manual=True)
        self.assertTrue(result.allowed)

    def test_manual_still_respects_cooldown_reason(self):
        result = decide(ctx(text="晚安啦", seconds_since_last=1, cooldown=240), manual=True)
        self.assertTrue(result.allowed)

    def test_auto_blocked_by_cooldown_in_group(self):
        # 冷却只对群聊生效
        result = decide(
            ctx(text="晚安啦", seconds_since_last=1, cooldown=240,
                private=False, group_enable=True),
            manual=False,
        )
        self.assertFalse(result.allowed)
        self.assertIn("冷却", result.reason)

    def test_private_ignores_cooldown(self):
        # 私聊默认不冷却：4 分钟前刚发过一条，这里照样能发
        result = decide(ctx(text="晚安啦", seconds_since_last=1, cooldown=240), manual=False)
        self.assertTrue(result.allowed)
        self.assertNotIn("冷却", result.reason)

    def test_private_custom_cooldown_still_works(self):
        # 真想给私聊限流，private_cooldown_seconds 一填就又生效
        result = decide(
            ctx(text="晚安啦", seconds_since_last=1, cooldown=240, private_cooldown=120),
            manual=False,
        )
        self.assertFalse(result.allowed)
        self.assertIn("冷却", result.reason)

    def test_auto_speaks_on_emotional_night_message(self):
        result = decide(
            ctx(text="晚安，今天好想你", hour=23, seconds_since_last=9999), manual=False
        )
        self.assertTrue(result.allowed)
        self.assertGreaterEqual(result.score, 0.6)

    def test_auto_silent_on_plain_question(self):
        result = decide(
            ctx(text="这个函数怎么调用？", hour=14, seconds_since_last=9999), manual=False
        )
        self.assertFalse(result.allowed)

    def test_score_in_range(self):
        score, tags = score_speech(ctx(text="晚安想你抱抱", hour=23), "晚安想你抱抱")
        self.assertGreater(score, 0.6)
        self.assertLessEqual(score, 1.0)
        self.assertTrue(tags)


class SceneTests(unittest.TestCase):
    def test_night_words(self):
        self.assertEqual(detect_scene("晚安，早点睡", ctx()), "sleep")

    def test_tender_words(self):
        self.assertEqual(detect_scene("想你了，抱抱", ctx()), "tender")

    def test_sad_words(self):
        self.assertEqual(detect_scene("今天有点难过", ctx()), "sad")

    def test_falls_back_to_place(self):
        self.assertEqual(detect_scene("我回来了", ctx(place="家")), "home")
        self.assertEqual(detect_scene("我出门了", ctx(place="公司")), "out")

    def test_default_scene(self):
        self.assertEqual(detect_scene("嗯", ctx()), "default")

    def test_every_scene_has_instructions(self):
        for scene in SCENE_LABELS:
            self.assertTrue(instructions_for(scene), scene)


if __name__ == "__main__":
    unittest.main()
