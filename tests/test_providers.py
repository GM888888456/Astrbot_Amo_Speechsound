# -*- coding: utf-8 -*-
"""后端层测试：音色归一化、错误翻译、请求体、Edge 兜底的边角。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import (  # noqa: E402
    BREEZE_DEFAULT_BASE,
    BreezeProvider,
    EdgeProvider,
    OpenAIProvider,
    SpeechError,
    SpeechRequest,
    SpeechResult,
    _normalize_voices,
    _parse_error,
    build_provider,
)


class NormalizeVoiceTests(unittest.TestCase):
    def test_breeze_shape(self):
        data = {
            "voices": [
                {
                    "voice_id": "voc_1",
                    "name": "小雨",
                    "language_code": "zh",
                    "gender": "female",
                    "age": "young",
                }
            ]
        }
        items = _normalize_voices(data)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "voc_1")
        self.assertEqual(items[0]["name"], "小雨")
        self.assertEqual(items[0]["language"], "zh")

    def test_list_shape_and_dedupe(self):
        data = [
            {"id": "a", "name": "A"},
            {"voice_id": "a", "name": "A again"},
            {"id": "b"},
        ]
        items = _normalize_voices(data)
        self.assertEqual([item["id"] for item in items], ["a", "b"])

    def test_voice_dict(self):
        data = {"voices": {"a": {"voice_id": "a", "name": "甲"}}}
        self.assertEqual(_normalize_voices(data)[0]["name"], "甲")

    def test_names_without_name_field(self):
        items = _normalize_voices([{"voice_id": "voc_x"}])
        self.assertEqual(items[0]["name"], "voc_x")

    def test_garbage_returns_empty(self):
        self.assertEqual(_normalize_voices("nope"), [])
        self.assertEqual(_normalize_voices({"voices": "nope"}), [])


class ErrorTests(unittest.TestCase):
    def test_parses_code_and_detail(self):
        body = b'{"code":"invalid_api_key","detail":"key not found"}'
        err = _parse_error(401, {}, body)
        self.assertEqual(err.status, 401)
        self.assertEqual(err.code, "invalid_api_key")
        self.assertIn("key not found", str(err))
        self.assertFalse(err.retryable)

    def test_429_is_retryable_with_retry_after(self):
        err = _parse_error(429, {"Retry-After": "12"}, b"{}")
        self.assertTrue(err.retryable)
        self.assertEqual(err.retry_after, 12.0)

    def test_500_is_retryable(self):
        self.assertTrue(_parse_error(503, {}, b"").retryable)

    def test_plain_text_body(self):
        err = _parse_error(502, {}, b"upstream boom")
        self.assertIn("upstream boom", str(err))


class BreezeTests(unittest.TestCase):
    def provider(self, **kwargs):
        settings = {"api_base": "", "api_key": "brz_x", "timeout": 30, "default_voice": "voc_1"}
        settings.update(kwargs)
        return BreezeProvider(settings)

    def test_default_base(self):
        self.assertEqual(self.provider().api_base, BREEZE_DEFAULT_BASE)

    def test_payload_omits_defaults(self):
        payload = self.provider()._payload(SpeechRequest(text="你好"))
        self.assertEqual(payload, {"text": "你好"})

    def test_payload_includes_instructions(self):
        payload = self.provider()._payload(
            SpeechRequest(text="晚安", instructions="困倦", speed=0.9)
        )
        self.assertEqual(payload["instructions"], "困倦")
        self.assertAlmostEqual(payload["voice_settings"]["speed"], 0.9)

    def test_missing_key_raises(self):
        with self.assertRaises(SpeechError):
            import asyncio

            asyncio.get_event_loop().run_until_complete(
                self.provider(api_key="").synthesize(SpeechRequest(text="你好"))
            )


class EdgeTests(unittest.TestCase):
    def test_voice_list_not_empty(self):
        provider = EdgeProvider({"timeout": 10})
        self.assertTrue(provider.VOICES)
        self.assertEqual(provider.default_voice, "zh-CN-XiaoxiaoNeural")

    def test_rate_string(self):
        self.assertEqual(EdgeProvider._rate(0.8), "-20%")
        self.assertEqual(EdgeProvider._rate(1.0), "+0%")
        self.assertEqual(EdgeProvider._rate(1.25), "+25%")

    def test_missing_package_gives_actionable_error(self):
        """没装 edge_tts 时要给出能照做的提示，而不是一串 traceback。"""
        import asyncio
        import importlib.util

        if importlib.util.find_spec("edge_tts") is not None:
            self.skipTest("环境里已装 edge_tts")
        provider = EdgeProvider({"timeout": 10})
        with self.assertRaises(SpeechError) as ctx:
            asyncio.get_event_loop().run_until_complete(
                provider.synthesize(SpeechRequest(text="你好", voice="zh-CN-XiaoxiaoNeural"))
            )
        self.assertFalse(ctx.exception.retryable)
        self.assertIn("edge", str(ctx.exception))


class OpenAIProviderTests(unittest.TestCase):
    def test_default_model(self):
        provider = OpenAIProvider({"api_key": "sk-x"})
        self.assertEqual(provider.default_model, "tts-1")
        self.assertTrue(provider.api_base.startswith("https://api.openai.com"))


class FactoryTests(unittest.TestCase):
    def test_build_by_name(self):
        self.assertIsInstance(build_provider("breeze", {}), BreezeProvider)
        self.assertIsInstance(build_provider("openai", {}), OpenAIProvider)
        self.assertIsInstance(build_provider("edge", {}), EdgeProvider)

    def test_unknown_falls_back_to_edge(self):
        self.assertIsInstance(build_provider("weird", {}), EdgeProvider)

    def test_extension_mapping(self):
        self.assertEqual(SpeechResult(b"", "riff-24khz", "edge", "v").extension, ".wav")
        self.assertEqual(SpeechResult(b"", "mp3", "x", "v").extension, ".mp3")


if __name__ == "__main__":
    unittest.main()
