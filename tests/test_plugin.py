# -*- coding: utf-8 -*-
"""插件主逻辑测试：用一套 astrbot 桩件把 main.py 跑起来。

真正会出错的不是"能不能念"，而是记账、额度、清理、面板返回值这些边角。这些
在真机上不好复现，所以在这里用假框架验证。
"""

import asyncio
import base64
import json
import os
import shutil
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ---------------------------------------------------------------------------
# astrbot 桩件：只需要 main.py 用到的那几个符号
# ---------------------------------------------------------------------------


def _install_stubs():
    if "astrbot" in sys.modules and getattr(sys.modules["astrbot"], "_stub", False):
        return

    class _Logger:
        def info(self, *a, **k):
            pass

        warning = error = debug = exception = info

    class _Filter:
        class EventMessageType:
            ALL = "all"

        @staticmethod
        def _decorator(*a, **k):
            def wrap(func):
                return func

            return wrap

        llm_tool = _decorator
        command = _decorator
        on_decorating_result = _decorator
        on_llm_response = _decorator
        on_llm_request = _decorator

    class AstrMessageEvent:
        pass

    class Star:
        def __init__(self, context=None):
            self.context = context

    class Context:
        pass

    class Plain:
        def __init__(self, text=""):
            self.text = text

    class Record:
        def __init__(self, file=None, text=None, **kw):
            self.file = file
            self.text = text

    def json_response(payload):
        return payload

    def error_response(message):
        return {"code": 1, "message": message}

    class _Request:
        _payload = {}

        async def json(self, default=None):
            return self._payload

    web_request = _Request()

    astrbot = types.ModuleType("astrbot")
    astrbot._stub = True
    astrbot.logger = _Logger()

    api = types.ModuleType("astrbot.api")
    api.logger = astrbot.logger
    api.AstrBotConfig = dict

    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.filter = _Filter

    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Star = Star
    star_mod.Context = Context

    comp_mod = types.ModuleType("astrbot.api.message_components")
    comp_mod.Plain = Plain
    comp_mod.Record = Record

    web_mod = types.ModuleType("astrbot.api.web")
    web_mod.json_response = json_response
    web_mod.error_response = error_response
    web_mod.request = web_request

    for name, mod in (
        ("astrbot", astrbot),
        ("astrbot.api", api),
        ("astrbot.api.event", event_mod),
        ("astrbot.api.star", star_mod),
        ("astrbot.api.message_components", comp_mod),
        ("astrbot.api.web", web_mod),
    ):
        sys.modules[name] = mod


_install_stubs()

import main as plugin_main  # noqa: E402
from providers import SpeechError, SpeechResult  # noqa: E402


class FakeConfig(dict):
    def save_config(self):
        return True


class FakeContext:
    def __init__(self, root):
        self.root = root
        self.routes = []

    def register_web_api(self, route, handler, methods, desc):
        self.routes.append(route)

    def get_plugin_data_dir(self):
        return self.root

    def get_registered_star(self, name):
        return None

    def get_all_stars(self):
        return []


class FakeEvent:
    def __init__(self, message_str="", private=True, sender="10001"):
        self.message_str = message_str
        self._private = private
        self._sender = sender
        self.sent = []

    def is_private_chat(self):
        return self._private

    def get_sender_id(self):
        return self._sender

    def chain_result(self, chain):
        return list(chain)

    async def send(self, chain):
        self.sent.append(chain)

    def plain_result(self, text):
        return [text]

    def get_result(self):
        return None


class PluginTestBase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="amo-voice-")
        self.context = FakeContext(self.root)
        self.config = FakeConfig(
            {
                "enable": True,
                "provider": "edge",
                "api_key": "",
                "api_base": "",
                "max_chars": 80,
                "max_segments": 2,
                "daily_quota": 40,
                "cooldown_seconds": 0,
                "speak_threshold": 0.6,
                "group_enable": False,
                "dual_output": False,
                "keep_audio": False,
                "edge_fallback": True,
                "edge_voice": "zh-CN-XiaoxiaoNeural",
                "read_relation": False,
                "schedule_aware": False,
            }
        )
        self.plugin = plugin_main.SpeechSoundPlugin(self.context, self.config)
        self._calls = []

        async def fake_synth(text, scene, voice, instructions):
            self._calls.append((text, scene, voice, instructions))
            return (
                SpeechResult(audio=b"RIFF" + b"\0" * 64, fmt="riff-24khz", provider="edge", voice="v"),
                "edge",
            )

        self.plugin._synthesize = fake_synth  # type: ignore[assignment]
        # 兜底逻辑本身也要测，所以要留一份没被打桩的原方法
        self.real_synthesize = plugin_main.SpeechSoundPlugin._synthesize.__get__(
            self.plugin, plugin_main.SpeechSoundPlugin
        )

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)


class WorkflowTests(PluginTestBase):
    def test_speaks_and_records(self):
        event = FakeEvent()
        result = asyncio.get_event_loop().run_until_complete(
            self.plugin.speak(event, "晚安啦，今天辛苦了")
        )
        self.assertTrue(result["ok"], result)
        # 私聊不占群聊额度（1.1.0 起私聊不限量），所以 count 不动
        self.assertEqual(self.plugin._state.get("count", 0), 0)
        self.assertTrue(event.sent)
        self.assertEqual(self.plugin._state["history"][0]["allowed"], True)

    def test_blocked_records_reason(self):
        event = FakeEvent()
        result = asyncio.get_event_loop().run_until_complete(
            self.plugin.speak(event, "嗯" * 200)
        )
        self.assertFalse(result["ok"])
        self.assertIn("太长", result["reason"])
        self.assertEqual(self.plugin._state["history"][0]["allowed"], False)

    def test_group_blocked(self):
        event = FakeEvent(private=False)
        result = asyncio.get_event_loop().run_until_complete(
            self.plugin.speak(event, "晚安啦")
        )
        self.assertFalse(result["ok"])
        self.assertIn("群聊", result["reason"])

    def test_quota_exhausted_in_group(self):
        """额度只管群聊：所有群共用一份，私聊不占。"""
        self.config["daily_quota"] = 1
        self.config["group_enable"] = True
        self.assertTrue(
            asyncio.get_event_loop().run_until_complete(
                self.plugin.speak(FakeEvent(private=False), "晚安")
            )["ok"]
        )
        second = asyncio.get_event_loop().run_until_complete(
            self.plugin.speak(FakeEvent(private=False), "晚安")
        )
        self.assertFalse(second["ok"])
        self.assertIn("额度", second["reason"])

    def test_total_limit_is_chars_times_segments(self):
        self.config["max_segments"] = 1
        self.config["max_chars"] = 12
        result = asyncio.get_event_loop().run_until_complete(
            self.plugin.speak(FakeEvent(), "第一句话真的。第二句话也是。")
        )
        self.assertFalse(result["ok"])
        self.assertIn("太长", result["reason"])

    def test_long_text_split_into_segments(self):
        self.config["max_chars"] = 10
        self.config["max_segments"] = 3
        result = asyncio.get_event_loop().run_until_complete(
            self.plugin.speak(FakeEvent(), "第一句话。第二句话。第三句话。")
        )
        self.assertTrue(result["ok"], result)
        self.assertGreater(len(self._calls), 1)

    def test_synthesis_error_is_reported_not_raised(self):
        async def boom(*args, **kwargs):
            raise SpeechError("HTTP 401：invalid key", status=401, retryable=False)

        self.plugin._synthesize = boom  # type: ignore[assignment]
        result = asyncio.get_event_loop().run_until_complete(
            self.plugin.speak(FakeEvent(), "晚安啦")
        )
        self.assertFalse(result["ok"])
        self.assertIn("401", result["reason"])

    def test_scene_voice_override(self):
        self.plugin._state["scene_voices"] = {"sleep": "voc_sleep"}
        asyncio.get_event_loop().run_until_complete(
            self.plugin.speak(FakeEvent(), "晚安，早点睡")
        )
        self.assertEqual(self._calls[0][2], "voc_sleep")

    def test_instructions_default_used(self):
        self.config["instructions_default"] = "小声一点"
        asyncio.get_event_loop().run_until_complete(self.plugin.speak(FakeEvent(), "晚安啦"))
        self.assertEqual(self._calls[0][3], "小声一点")

    def test_audio_file_created(self):
        asyncio.get_event_loop().run_until_complete(self.plugin.speak(FakeEvent(), "晚安啦"))
        files = os.listdir(self.plugin._audio_dir())
        self.assertTrue(any(name.startswith("voice-") for name in files))


class FallbackTests(PluginTestBase):
    def _failing_primary(self):
        class Primary:
            name = "breeze"

            async def synthesize(self, request):
                raise SpeechError("HTTP 503", status=503, retryable=True)

        self.plugin._provider = lambda: Primary()  # type: ignore[assignment]

    def test_falls_back_to_framework_tts(self):
        """主后端 5xx 时先用 AstrBot 已配置的 TTS，不逼用户装包。"""
        self._failing_primary()

        class FakeTTS:
            model = "edge_tts"

            async def get_audio(self, text):
                path = os.path.join(self.root, "framework.wav")
                with open(path, "wb") as fh:
                    fh.write(b"RIFF" + b"\0" * 32)
                return path

        fake = FakeTTS()
        fake.root = self.root

        async def getter(umo=None):
            return fake

        self.context.get_using_tts_provider_async = getter  # type: ignore[attr-defined]

        result, used = asyncio.get_event_loop().run_until_complete(
            self.real_synthesize("晚安", "sleep", "voc_1", "困")
        )
        self.assertEqual(used, "framework")
        self.assertTrue(result.audio.startswith(b"RIFF"))

    def test_non_retryable_error_does_not_fall_back(self):
        class Primary:
            name = "breeze"

            async def synthesize(self, request):
                raise SpeechError("HTTP 401", status=401, retryable=False)

        self.plugin._provider = lambda: Primary()  # type: ignore[assignment]
        with self.assertRaises(SpeechError):
            asyncio.get_event_loop().run_until_complete(
                self.real_synthesize("晚安", "sleep", "voc_1", "困")
            )


class PanelTests(PluginTestBase):
    def test_routes_registered(self):
        self.assertTrue(self.context.routes)
        self.assertTrue(any("status" in route for route in self.context.routes))

    def test_status_payload(self):
        data = asyncio.get_event_loop().run_until_complete(self.plugin.api_status())
        self.assertEqual(data["code"], 0)
        self.assertEqual(data["data"]["name"], plugin_main.PLUGIN_NAME)
        self.assertIn("quota_left", data["data"])

    def test_config_hides_key(self):
        self.config["api_key"] = "brz_secret_value"
        data = asyncio.get_event_loop().run_until_complete(self.plugin.api_config())
        self.assertEqual(data["data"]["api_key"], "")
        self.assertNotIn("brz_secret_value", json.dumps(data))
        self.assertIn("…", data["data"]["api_key_masked"])

    def test_test_endpoint_returns_data_uri(self):
        data = asyncio.get_event_loop().run_until_complete(self.plugin.api_test())
        self.assertEqual(data["code"], 0)
        self.assertTrue(data["data"]["audio"].startswith("data:audio/"))
        raw = data["data"]["audio"].split(",", 1)[1]
        self.assertTrue(base64.b64decode(raw).startswith(b"RIFF"))

    def test_scene_assignment_persists(self):
        plugin_main.web_request._payload = {"scene": "sleep", "voice": "voc_1"}
        data = asyncio.get_event_loop().run_until_complete(self.plugin.api_set_scene_voice())
        self.assertEqual(data["code"], 0)
        self.assertEqual(self.plugin._state["scene_voices"]["sleep"], "voc_1")
        with open(self.plugin._state_path(), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["scene_voices"]["sleep"], "voc_1")

    def test_unknown_scene_rejected(self):
        plugin_main.web_request._payload = {"scene": "nope", "voice": "voc_1"}
        data = asyncio.get_event_loop().run_until_complete(self.plugin.api_set_scene_voice())
        self.assertEqual(data["code"], 1)


class ConfigTests(PluginTestBase):
    def test_key_left_blank_not_overwritten(self):
        self.config["api_key"] = "brz_keep"
        plugin_main.web_request._payload = {"api_key": "", "speed": 1.1}
        asyncio.get_event_loop().run_until_complete(self.plugin.api_save_config())
        self.assertEqual(self.config["api_key"], "brz_keep")
        self.assertAlmostEqual(self.config["speed"], 1.1)

    def test_masked_key_not_overwritten(self):
        self.config["api_key"] = "brz_keep"
        masked = self.plugin._mask_key()
        plugin_main.web_request._payload = {"api_key": masked}
        asyncio.get_event_loop().run_until_complete(self.plugin.api_save_config())
        self.assertEqual(self.config["api_key"], "brz_keep")

    def test_bool_coercion(self):
        plugin_main.web_request._payload = {"auto_fallback": "true", "group_enable": 0}
        asyncio.get_event_loop().run_until_complete(self.plugin.api_save_config())
        self.assertIs(self.config["auto_fallback"], True)
        self.assertIs(self.config["group_enable"], False)


class ClearKeyTests(PluginTestBase):
    def test_clear_key(self):
        self.config["api_key"] = "brz_secret"
        self.plugin._drop_provider_cache()
        data = asyncio.get_event_loop().run_until_complete(self.plugin.api_clear_key())
        self.assertEqual(data["code"], 0)
        self.assertEqual(self.config["api_key"], "")

    def test_clear_key_persists(self):
        self.config["api_key"] = "brz_secret"
        asyncio.get_event_loop().run_until_complete(self.plugin.api_clear_key())
        self.assertTrue(self.config.save_config())


class HelperTests(PluginTestBase):
    def test_mask_key(self):
        self.config["api_key"] = "brz_1234567890abcdef"
        self.assertEqual(self.plugin._mask_key(), "brz_…cdef")

    def test_empty_mask(self):
        self.assertEqual(self.plugin._mask_key(), "")

    def test_quota_left_unlimited(self):
        self.config["daily_quota"] = 0
        self.assertEqual(self.plugin._quota_left(), -1)

    def test_user_asked_for_voice(self):
        self.assertTrue(self.plugin._user_asked_for_voice(FakeEvent("用语音说这句")))
        self.assertFalse(self.plugin._user_asked_for_voice(FakeEvent("今天吃什么")))

    def test_prune_keeps_recent(self):
        for index in range(plugin_main.AUDIO_KEEP + 4):
            path = os.path.join(self.plugin._audio_dir(), "voice-%02d.wav" % index)
            with open(path, "wb") as fh:
                fh.write(b"x")
            os.utime(path, (1000 + index, 1000 + index))
        self.plugin._prune_audio()
        self.assertEqual(len(os.listdir(self.plugin._audio_dir())), plugin_main.AUDIO_KEEP)


if __name__ == "__main__":
    unittest.main()
