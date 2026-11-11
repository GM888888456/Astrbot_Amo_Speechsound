# -*- coding: utf-8 -*-
"""我想和你说说话（Astrbot_Amo_Speechsound）：给陪伴插件配一副声音。

这是「我想永远陪伴你」「我想永远记住你」「我想和你度日常」之后的第四块：
**只负责出声**。不参与聊天判定、不改 AstrBot 核心链路。

它和一般 TTS 插件的区别在于"谁决定说话"：

* **Amo 主动调用**（LLM 工具 ``amo_speak``）：她已经决定要说了，插件只拦硬伤
  ——太长、群聊没开、额度用完——其余放行，并替她挑音色、配语气。
* **兜底转语音**（默认关闭）：用户明说"用语音说"但 Amo 只回了文字时，由规则
  判断要不要补一条，避免她该出声时哑了。

音色不在插件里，在云端。插件内置的是**音色目录**：面板里点"拉取"从账号拉回
可用音色，再按场景分配（睡前/撒娇/难过/生气/在家/出门）。这样既不用手抄
voice_id，也不会把别人的音色偷偷打包分发。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from datetime import datetime
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:  # AstrBot 4.x
    from astrbot.api.message_components import Plain, Record
except Exception:  # pragma: no cover - 老版本路径
    from astrbot.core.message.components import Plain, Record  # type: ignore

try:  # 面板后端：AstrBot 4.x 的插件 Web API
    from astrbot.api.web import error_response, json_response, request as web_request
except Exception:  # pragma: no cover - 老版本没有该模块
    error_response = json_response = web_request = None

try:
    from .decision import (
        SCENE_LABELS,
        Decision,
        SpeakContext,
        decide,
        detect_scene,
        instructions_for,
        sanitize_for_speech,
        split_text,
    )
    from .providers import (
        SpeechError,
        SpeechRequest,
        SpeechResult,
        build_provider,
        BreezeProvider,
        EdgeProvider,
    )
except Exception:  # pragma: no cover - 直接以脚本方式加载时
    from decision import (  # type: ignore
        SCENE_LABELS,
        Decision,
        SpeakContext,
        decide,
        detect_scene,
        instructions_for,
        sanitize_for_speech,
        split_text,
    )
    from providers import (  # type: ignore
        SpeechError,
        SpeechRequest,
        SpeechResult,
        build_provider,
        BreezeProvider,
        EdgeProvider,
    )

PLUGIN_NAME = "Astrbot_Amo_Speechsound"
PLUGIN_VERSION = "1.0.0"
DISPLAY_NAME = "我想和你说说话"

#: 面板可以改的字段（白名单）。加字段时两边一起改。
WEB_EDITABLE = (
    "enable", "provider", "api_base", "api_key", "model_id", "default_voice",
    "language_code", "instructions_default", "speed", "volume", "timeout",
    "max_chars", "max_segments", "speak_threshold", "auto_fallback",
    "group_enable", "daily_quota", "cooldown_seconds", "dual_output",
    "keep_audio", "edge_fallback", "edge_voice", "read_relation", "schedule_aware",
)
WEB_BOOL_KEYS = frozenset(
    {
        "enable", "auto_fallback", "group_enable", "dual_output",
        "keep_audio", "edge_fallback", "read_relation", "schedule_aware",
    }
)
WEB_INT_KEYS = frozenset({"timeout", "max_chars", "max_segments", "daily_quota", "cooldown_seconds"})
WEB_FLOAT_KEYS = frozenset({"speed", "volume", "speak_threshold"})

HISTORY_LIMIT = 60
AUDIO_KEEP = 8  # keep_audio 打开时保留几个文件


def _now() -> float:
    return time.time()


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


class SpeechSoundPlugin(Star):
    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self._started_at = _now()
        self._state: dict[str, Any] = {}
        self._voices: dict[str, Any] = {"fetched_at": 0, "provider": "", "items": []}
        self._provider_cache: tuple[str, Any] | None = None
        self._pending_voice: dict[str, str] = {}
        self._load_state()
        self._load_voices()
        self._register_web_apis()

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------

    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _bool(self, key: str, default: bool) -> bool:
        value = self._cfg(key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on", "开")

    def _int(self, key: str, default: int) -> int:
        try:
            return int(float(self._cfg(key, default)))
        except Exception:
            return default

    def _float(self, key: str, default: float) -> float:
        try:
            return float(self._cfg(key, default))
        except Exception:
            return default

    def _str(self, key: str, default: str = "") -> str:
        value = self._cfg(key, default)
        return default if value is None else str(value)

    def _mask_key(self) -> str:
        key = self._str("api_key").strip()
        if not key:
            return ""
        if len(key) <= 8:
            return key[:2] + "…"
        return key[:4] + "…" + key[-4:]

    async def _persist_config(self) -> bool:
        """把配置写回 AstrBot；配置对象不支持保存时只留内存，不报错。"""
        cfg = self.config
        saver = getattr(cfg, "save_config_async", None)
        if callable(saver):
            try:
                return bool(await saver())
            except Exception as exc:
                logger.warning("[amo_voice] 配置落盘失败: %s" % exc)
        saver = getattr(cfg, "save_config", None)
        if callable(saver):
            try:
                await asyncio.to_thread(saver)
                return True
            except Exception as exc:
                logger.warning("[amo_voice] 配置落盘失败: %s" % exc)
        return False

    # ------------------------------------------------------------------
    # 数据目录（上架规范：持久化数据放 data/plugin_data/<插件名>/）
    # ------------------------------------------------------------------

    def _data_dir(self) -> str:
        base = ""
        for getter in ("get_plugin_data_dir", "plugin_data_dir"):
            try:
                value = getattr(self.context, getter, None)
                value = value() if callable(value) else value
                value = str(value or "").strip()
                if value:
                    base = value
                    break
            except Exception:
                continue
        if not base:
            base = os.path.join(os.getcwd(), "data", "plugin_data")
        path = os.path.join(base, PLUGIN_NAME)
        try:
            os.makedirs(path, exist_ok=True)
        except Exception:
            pass
        return path

    def _audio_dir(self) -> str:
        path = os.path.join(self._data_dir(), "audio")
        try:
            os.makedirs(path, exist_ok=True)
        except Exception:
            pass
        return path

    def _state_path(self) -> str:
        return os.path.join(self._data_dir(), "state.json")

    def _voices_path(self) -> str:
        return os.path.join(self._data_dir(), "voices.json")

    # ------------------------------------------------------------------
    # 状态 / 音色缓存
    # ------------------------------------------------------------------

    def _load_state(self) -> None:
        try:
            with open(self._state_path(), encoding="utf-8") as fh:
                data = json.load(fh)
            self._state = data if isinstance(data, dict) else {}
        except Exception:
            self._state = {}
        self._state.setdefault("date", _today())
        self._state.setdefault("count", 0)
        self._state.setdefault("last_ts", 0)
        self._state.setdefault("scene_voices", {})
        self._state.setdefault("history", [])
        if self._state.get("date") != _today():
            self._state["date"] = _today()
            self._state["count"] = 0

    def _save_state(self) -> None:
        try:
            tmp = self._state_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._state, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self._state_path())
        except Exception as exc:
            logger.debug("[amo_voice] 状态写入失败: %s" % exc)

    def _load_voices(self) -> None:
        try:
            with open(self._voices_path(), encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                self._voices = data
        except Exception:
            pass

    def _save_voices(self) -> None:
        try:
            tmp = self._voices_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._voices, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self._voices_path())
        except Exception as exc:
            logger.debug("[amo_voice] 音色缓存写入失败: %s" % exc)

    def _quota_left(self) -> int:
        quota = self._int("daily_quota", 40)
        if quota <= 0:
            return -1
        if self._state.get("date") != _today():
            self._state["date"] = _today()
            self._state["count"] = 0
            self._save_state()
        return max(0, quota - int(self._state.get("count") or 0))

    def _bump_usage(self) -> None:
        if self._state.get("date") != _today():
            self._state["date"] = _today()
            self._state["count"] = 0
        self._state["count"] = int(self._state.get("count") or 0) + 1
        self._state["last_ts"] = _now()
        self._save_state()

    def _remember(self, entry: dict[str, Any]) -> None:
        items = self._state.setdefault("history", [])
        items.insert(0, entry)
        del items[HISTORY_LIMIT:]
        self._save_state()

    def _scene_voice(self, scene: str) -> str:
        mapping = self._state.get("scene_voices") or {}
        voice = str(mapping.get(scene) or "").strip()
        if voice:
            return voice
        return self._str("default_voice").strip()

    # ------------------------------------------------------------------
    # 后端
    # ------------------------------------------------------------------

    def _provider(self):
        name = self._str("provider", "breeze").strip().lower() or "breeze"
        stamp = "|".join(
            [
                name,
                self._str("api_base"),
                self._str("api_key"),
                self._str("model_id"),
                self._str("default_voice"),
                self._str("edge_voice"),
                str(self._int("timeout", 30)),
            ]
        )
        if self._provider_cache and self._provider_cache[0] == stamp:
            return self._provider_cache[1]
        settings = {
            "api_base": self._str("api_base"),
            "api_key": self._str("api_key"),
            "timeout": self._int("timeout", 30),
            "model_id": self._str("model_id"),
            "default_voice": self._str("default_voice"),
            "edge_voice": self._str("edge_voice", "zh-CN-XiaoxiaoNeural"),
        }
        provider = build_provider(name, settings)
        self._provider_cache = (stamp, provider)
        return provider

    def _drop_provider_cache(self) -> None:
        self._provider_cache = None

    # ------------------------------------------------------------------
    # 上下文：关系 / 日程（能读到就更好，读不到不影响出声）
    # ------------------------------------------------------------------

    COMPANION_NAMES = ("Astrbot_Amo_forever", "astrbot_plugin_amo_forever", "amo_forever")

    @staticmethod
    def _star_instance(meta):
        if meta is None:
            return None
        for attr in ("star_cls", "instance", "star", "plugin"):
            value = getattr(meta, attr, None)
            if value is not None:
                return value
        return None

    def _companion(self):
        try:
            for name in self.COMPANION_NAMES:
                star = self._star_instance(self.context.get_registered_star(name))
                if star is not None:
                    return star
        except Exception:
            pass
        try:
            for meta in self.context.get_all_stars():
                marker = (getattr(meta, "name", "") or "") + " " + (
                    getattr(meta, "module_path", "") or ""
                )
                if "amo_forever" not in marker.lower():
                    continue
                star = self._star_instance(meta)
                if star is not None:
                    return star
        except Exception:
            pass
        return None

    def _companion_snapshot(self, event: AstrMessageEvent) -> dict[str, str]:
        """从陪伴插件读「此刻在哪、在做什么、关系多近」。任何一个读不到都返回空串。"""
        out = {"place": "", "activity": "", "relation": ""}
        if not (self._bool("schedule_aware", True) or self._bool("read_relation", True)):
            return out
        companion = self._companion()
        if companion is None:
            return out

        if self._bool("schedule_aware", True):
            getter = getattr(companion, "current_activity", None)
            if callable(getter):
                try:
                    activity = getter() or {}
                    if isinstance(activity, dict):
                        out["place"] = str(activity.get("place") or "")
                        out["activity"] = str(activity.get("activity") or "")
                except Exception:
                    pass

        if self._bool("read_relation", True):
            try:
                data = getattr(companion, "data", None)
                users = data.get("users") if isinstance(data, dict) else None
                uid = str(event.get_sender_id() or "")
                user = users.get(uid) if isinstance(users, dict) else None
                if isinstance(user, dict):
                    score = int(float(user.get("relationship_score") or 0))
                    stage = str(
                        user.get("relationship_phase_key") or user.get("relationship_stage") or ""
                    ).lower()
                    if "love" in stage or "couple" in stage or score >= 80:
                        out["relation"] = "恋慕"
                    elif "close" in stage or score >= 40:
                        out["relation"] = "亲近"
                    elif "familiar" in stage or score >= 15:
                        out["relation"] = "熟识"
                    elif score < 0:
                        out["relation"] = "抵触"
                    else:
                        out["relation"] = "普通"
            except Exception:
                pass
        return out

    def _context_for(self, event: AstrMessageEvent, text: str) -> SpeakContext:
        snap = self._companion_snapshot(event)
        now = datetime.now()
        last_ts = float(self._state.get("last_ts") or 0)
        return SpeakContext(
            text=text,
            private=bool(event.is_private_chat()),
            hour=now.hour,
            quota_left=self._quota_left(),
            seconds_since_last=(_now() - last_ts) if last_ts else 1e9,
            cooldown=self._int("cooldown_seconds", 240),
            group_enable=self._bool("group_enable", False),
            max_chars=self._int("max_chars", 80),
            max_segments=max(1, self._int("max_segments", 2)),
            threshold=self._float("speak_threshold", 0.6),
            relation=snap["relation"],
            place=snap["place"],
            activity=snap["activity"],
        )

    # ------------------------------------------------------------------
    # 合成
    # ------------------------------------------------------------------

    async def _framework_tts(self, text: str):
        """用 AstrBot 里已经配好的 TTS provider 兜底。

        比 Edge 更适合当第一顺位兜底：用户既然在 AstrBot 里配过，就用它，不额外
        要求装包，也不锁定某一家。
        """
        getter = getattr(self.context, "get_using_tts_provider_async", None)
        if not callable(getter):
            return None
        try:
            provider = await getter(None)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[amo_voice] 读取框架 TTS provider 失败: %s" % exc)
            return None
        if provider is None:
            return None
        try:
            path = await provider.get_audio(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[amo_voice] 框架 TTS 合成失败: %s" % exc)
            return None
        if not path:
            return None
        try:
            with open(str(path), "rb") as fh:
                data = fh.read()
        except OSError as exc:
            logger.warning("[amo_voice] 读不到框架 TTS 的音频: %s" % exc)
            return None
        if not data:
            return None
        ext = os.path.splitext(str(path))[1].lower().lstrip(".") or "wav"
        return SpeechResult(
            audio=data,
            fmt=ext,
            provider="framework",
            voice=str(getattr(provider, "model", "") or ""),
        )

    async def _synthesize(self, text: str, scene: str, voice: str, instructions: str):
        """主后端合成；失败且开了兜底，先试框架自带 TTS，再试 Edge。"""
        provider = self._provider()
        request = SpeechRequest(
            text=text,
            voice=voice,
            instructions=instructions or None,
            language=self._str("language_code") or None,
            model=self._str("model_id") or None,
            speed=self._float("speed", 1.0),
            volume=self._float("volume", 1.0),
        )
        try:
            return await provider.synthesize(request), provider.name
        except SpeechError as exc:
            fallback_ok = (
                self._bool("edge_fallback", True)
                and provider.name != "edge"
                and exc.retryable
            )
            if not fallback_ok:
                raise
            logger.warning("[amo_voice] %s 合成失败，准备兜底：%s" % (provider.name, exc))

            result = await self._framework_tts(text)
            if result is not None:
                return result, "framework"

            edge = EdgeProvider(
                {
                    "timeout": self._int("timeout", 30),
                    "edge_voice": self._str("edge_voice", "zh-CN-XiaoxiaoNeural"),
                }
            )
            result = await edge.synthesize(
                SpeechRequest(text=text, voice=edge.default_voice, speed=self._float("speed", 1.0))
            )
            return result, "edge"

    def _write_audio(self, audio: bytes, ext: str) -> str:
        token = f"{int(_now() * 1000)}-{os.getpid()}"
        path = os.path.join(self._audio_dir(), f"voice-{token}{ext}")
        with open(path, "wb") as fh:
            fh.write(audio)
        self._prune_audio()
        return path

    def _prune_audio(self) -> None:
        if self._bool("keep_audio", False):
            return
        try:
            files = [
                os.path.join(self._audio_dir(), name)
                for name in os.listdir(self._audio_dir())
                if name.startswith("voice-")
            ]
            files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            for path in files[AUDIO_KEEP:]:
                try:
                    os.remove(path)
                except OSError:
                    pass
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------

    async def _send_voice(self, event: AstrMessageEvent, path: str, text: str) -> None:
        chain = [Record(file=path, text=text)]
        if self._bool("dual_output", False):
            chain = [Plain(text=text), *chain]
        await event.send(event.chain_result(chain))

    async def speak(
        self,
        event: AstrMessageEvent,
        text: str,
        *,
        instructions: str | None = None,
        scene: str | None = None,
        manual: bool = True,
        source: str = "tool",
    ) -> dict[str, Any]:
        """对外唯一入口：判断 → 合成 → 发送 → 记账。

        返回 ``{ok, reason, scene, provider, ms}``，失败不抛异常，交给上层决定
        要不要把原因告诉模型。
        """
        if not self._bool("enable", True):
            return {"ok": False, "reason": "语音功能已关闭"}

        ctx = self._context_for(event, text)
        decision: Decision = decide(ctx, manual=manual)
        if scene:
            decision.scene = scene
        if not decision.allowed:
            logger.info("[amo_voice] 不放行：%s｜%s" % (decision.reason, decision.text[:40]))
            self._remember(
                {
                    "ts": _now(),
                    "allowed": False,
                    "reason": decision.reason,
                    "text": decision.text[:120],
                    "scene": decision.scene,
                    "source": source,
                }
            )
            return {"ok": False, "reason": decision.reason, **decision.as_dict()}

        spoken = decision.text
        segments = split_text(spoken, self._int("max_chars", 80))
        max_segments = max(1, self._int("max_segments", 2))
        if len(segments) > max_segments:
            reason = f"要分 {len(segments)} 段，超过上限 {max_segments}"
            self._remember(
                {
                    "ts": _now(),
                    "allowed": False,
                    "reason": reason,
                    "text": spoken[:120],
                    "scene": decision.scene,
                    "source": source,
                }
            )
            return {"ok": False, "reason": reason}

        instructions = (instructions or "").strip() or (
            self._str("instructions_default").strip()
            or instructions_for(decision.scene, ctx.activity)
        )
        voice = self._scene_voice(decision.scene)

        started = _now()
        paths: list[str] = []
        try:
            for segment in segments:
                result, used = await self._synthesize(
                    segment, decision.scene, voice, instructions
                )
                paths.append(self._write_audio(result.audio, result.extension))
            for path in paths:
                await self._send_voice(event, path, spoken)
        except SpeechError as exc:
            logger.warning("[amo_voice] 合成失败：%s" % exc)
            self._remember(
                {
                    "ts": _now(),
                    "allowed": False,
                    "reason": "合成失败：" + str(exc),
                    "text": spoken[:120],
                    "scene": decision.scene,
                    "source": source,
                }
            )
            return {"ok": False, "reason": str(exc)}
        except Exception as exc:  # noqa: BLE001
            logger.exception("[amo_voice] 发送失败")
            return {"ok": False, "reason": "发送失败：%s" % exc}

        elapsed = int((_now() - started) * 1000)
        self._bump_usage()
        entry = {
            "ts": _now(),
            "allowed": True,
            "reason": decision.reason,
            "text": spoken[:120],
            "scene": decision.scene,
            "source": source,
            "provider": used if segments else "",
            "ms": elapsed,
            "segments": len(segments),
            "voice": voice,
            "score": round(decision.score, 2),
        }
        self._remember(entry)
        if not self._bool("dual_output", False):
            # 让 on_decorating_result 知道这段文字已经用语音发过了，别再重复发一遍
            try:
                self._pending_voice[str(id(event))] = spoken
            except Exception:
                pass
        logger.info(
            "[amo_voice] 发出语音 %d 段 / %d ms / %s / %s"
            % (len(segments), elapsed, decision.scene, used if segments else "?")
        )
        return {
            "ok": True,
            "reason": decision.reason,
            "scene": decision.scene,
            "provider": used if segments else "",
            "ms": elapsed,
        }

    # ------------------------------------------------------------------
    # LLM 工具：Amo 主动开口
    # ------------------------------------------------------------------

    @filter.llm_tool(name="amo_speak")
    async def amo_speak(
        self,
        event: AstrMessageEvent,
        text: str = "",
        mood: str = "",
        scene: str = "",
    ) -> str:
        """把一句话用你自己的声音说出来，发成语音消息。

        什么时候用：
        - 用户明确说「发语音」「用语音说」「念给我听」；
        - 这句话本身就是给耳朵听的：晚安、想你、哄人、道歉、生日祝福、安慰；
        - 你此刻的情绪比文字更适合用声音表达。

        什么时候别用：
        - 内容里有链接、代码、清单、参数说明（念出来是折磨）；
        - 同一段对话刚发过语音，别连着刷；
        - 群里没人要你发语音时别发，很招人烦。

        Args:
            text(string): 你要说的话，只放要念的内容，不要带括号里的舞台说明。
            mood(string): 语气描述，例如「困倦、气声、慢慢说」「开心，带笑」。留空由插件按场景自动配。
            scene(string): 场景，可选 sleep/tender/sad/angry/happy/home/out，留空自动判断。
        """
        if not text.strip():
            return "没有要念的内容。"
        result = await self.speak(
            event,
            text,
            instructions=mood.strip() or None,
            scene=scene.strip() or None,
            manual=True,
            source="tool",
        )
        if result.get("ok"):
            return f"已经把这句话用语音发出去了（{result.get('scene')}）。不要再重复发同一句。"
        return f"这条没能发成语音：{result.get('reason')}。用文字回复就行。"

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------

    @filter.command("speak", alias={"语音"})
    async def cmd_speak(self, event: AstrMessageEvent):
        """/speak <文本>：手动合成一条语音，方便调试音色。"""
        raw = str(event.message_str or "")
        text = raw.split(" ", 1)[1].strip() if " " in raw else ""
        if not text:
            yield event.plain_result("用法：/speak 要说的话")
            return
        result = await self.speak(event, text, manual=True, source="command")
        if not result.get("ok"):
            yield event.plain_result("没发出去：" + str(result.get("reason")))

    # ------------------------------------------------------------------
    # 兜底：Amo 没调工具、但用户明说要语音
    # ------------------------------------------------------------------

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        try:
            result = event.get_result()
            if not result or not result.chain:
                return
            try:
                if not result.is_llm_result():
                    return
            except Exception:
                pass

            text = "".join(
                comp.text for comp in result.chain if isinstance(comp, Plain)
            ).strip()

            # 这一段已经用语音发过了：把文字撤掉，避免语音+文字各来一遍
            spoken = self._pending_voice.pop(str(id(event)), "")
            if spoken and not self._bool("dual_output", False):
                if text and (text in spoken or spoken in text):
                    result.chain = []
                    return

            if not self._bool("auto_fallback", False):
                return
            if not text:
                return
            if not self._user_asked_for_voice(event):
                return

            decision = decide(self._context_for(event, text), manual=False)
            if not decision.allowed:
                return
            if await self.speak(event, text, manual=False, source="fallback"):
                # speak 内部已经把语音发出去了；这条文字留着，别让用户等
                return
        except Exception as exc:  # noqa: BLE001
            logger.debug("[amo_voice] 兜底判断失败: %s" % exc)

    @staticmethod
    def _user_asked_for_voice(event: AstrMessageEvent) -> bool:
        try:
            body = str(getattr(event, "message_str", "") or "")
        except Exception:
            return False
        words = ("语音", "发条音", "念给我", "读给我", "读一下", "说句话", "说给我听", "用声音")
        return any(word in body for word in words)

    # ------------------------------------------------------------------
    # 面板接口
    # ------------------------------------------------------------------

    @staticmethod
    def _json(payload):
        if json_response is not None:
            try:
                return json_response(payload)
            except Exception:
                pass
        return payload

    @staticmethod
    def _fail(message):
        if error_response is not None:
            try:
                return error_response(message)
            except Exception:
                pass
        return {"code": 1, "message": message}

    def _register_web_apis(self) -> None:
        routes = [
            ("/status", self.api_status, ["GET"]),
            ("/config", self.api_config, ["GET"]),
            ("/config/save", self.api_save_config, ["POST"]),
            ("/config/clear_key", self.api_clear_key, ["POST"]),
            ("/voices", self.api_voices, ["GET"]),
            ("/voices/pull", self.api_pull_voices, ["POST"]),
            ("/voices/scene", self.api_set_scene_voice, ["POST"]),
            ("/history", self.api_history, ["GET"]),
            ("/test", self.api_test, ["POST"]),
        ]
        for path, handler, methods in routes:
            try:
                self.context.register_web_api(
                    "/" + PLUGIN_NAME + path, handler, methods, path
                )
            except TypeError:
                try:
                    self.context.register_web_api(
                        "/" + PLUGIN_NAME + path, methods, path, handler
                    )
                except Exception as exc:
                    logger.warning("[amo_voice] 面板接口 %s 兼容注册失败: %s" % (path, exc))
            except Exception as exc:
                logger.warning("[amo_voice] 面板接口 %s 注册失败: %s" % (path, exc))
        logger.info(
            "[amo_voice] %s %s 已加载：面板 %d 条接口，音色 %d 个"
            % (DISPLAY_NAME, PLUGIN_VERSION, len(routes), len(self._voices.get("items") or []))
        )

    async def api_status(self):
        items = self._voices.get("items") or []
        return self._json(
            {
                "code": 0,
                "data": {
                    "name": PLUGIN_NAME,
                    "version": PLUGIN_VERSION,
                    "display_name": DISPLAY_NAME,
                    "enabled": self._bool("enable", True),
                    "provider": self._str("provider", "breeze"),
                    "api_base": self._str("api_base"),
                    "api_key_masked": self._mask_key(),
                    "has_key": bool(self._str("api_key").strip()),
                    "default_voice": self._str("default_voice"),
                    "voice_count": len(items),
                    "voices_age": int(_now() - float(self._voices.get("fetched_at") or 0)),
                    "quota_left": self._quota_left(),
                    "daily_quota": self._int("daily_quota", 40),
                    "used_today": int(self._state.get("count") or 0),
                    "last_ts": float(self._state.get("last_ts") or 0),
                    "history_size": len(self._state.get("history") or []),
                    "uptime_seconds": int(_now() - self._started_at),
                    "companion": bool(self._companion()),
                },
            }
        )

    async def api_config(self):
        data = {key: self._cfg(key, None) for key in WEB_EDITABLE}
        data["api_key"] = ""
        data["api_key_masked"] = self._mask_key()
        return self._json({"code": 0, "data": data})

    async def api_save_config(self):
        payload: dict[str, Any] = {}
        if web_request is not None:
            try:
                payload = await web_request.json(default={}) or {}
            except Exception:
                payload = {}
        if not isinstance(payload, dict):
            return self._fail("配置格式不对")

        data: dict[str, Any] = {}
        for key in WEB_EDITABLE:
            if key not in payload:
                continue
            value = payload[key]
            try:
                if key in WEB_BOOL_KEYS:
                    value = str(value).strip().lower() in ("1", "true", "yes", "on") if not isinstance(value, bool) else value
                elif key in WEB_INT_KEYS:
                    value = int(float(value))
                elif key in WEB_FLOAT_KEYS:
                    value = float(value)
                else:
                    value = str("" if value is None else value)
            except Exception:
                continue
            data[key] = value

        key_value = str(data.get("api_key", "") or "")
        if not key_value or "…" in key_value or key_value == self._mask_key():
            data.pop("api_key", None)
        if not data:
            return self._fail("没有要保存的字段")

        try:
            if hasattr(self.config, "update"):
                self.config.update(data)
        except Exception as exc:
            return self._fail("保存失败：%s" % exc)
        self._drop_provider_cache()
        saved = await self._persist_config()
        return self._json({"code": 0, "data": {"saved": saved, "applied": sorted(data)}})

    async def api_clear_key(self):
        """面板里清空已保存的密钥。

        密钥留空等于「不修改」，所以清空必须走单独一条路，否则换了后端之后
        旧 key 会一直带着。
        """
        try:
            self.config.update({"api_key": ""})
        except Exception as exc:
            return self._fail("清除失败：%s" % exc)
        self._drop_provider_cache()
        saved = await self._persist_config()
        return self._json({"code": 0, "data": {"saved": saved, "cleared": True}})

    async def api_voices(self):
        return self._json(
            {
                "code": 0,
                "data": {
                    "provider": self._voices.get("provider") or self._str("provider"),
                    "fetched_at": self._voices.get("fetched_at") or 0,
                    "items": self._voices.get("items") or [],
                    "scene_voices": self._state.get("scene_voices") or {},
                    "scene_labels": SCENE_LABELS,
                    "default_voice": self._str("default_voice"),
                    "edge_voice": self._str("edge_voice"),
                },
            }
        )

    async def api_pull_voices(self):
        provider = self._provider()
        try:
            if isinstance(provider, BreezeProvider):
                items = await provider.list_voices(
                    language=self._str("language_code"), limit=100
                )
            else:
                items = await provider.list_voices()
        except SpeechError as exc:
            return self._fail("拉取失败：%s" % exc)
        except Exception as exc:  # noqa: BLE001
            return self._fail("拉取失败：%s" % exc)

        self._voices = {
            "fetched_at": _now(),
            "provider": provider.name,
            "items": items,
        }
        self._save_voices()
        return self._json(
            {"code": 0, "data": {"count": len(items), "provider": provider.name}}
        )

    async def api_set_scene_voice(self):
        payload: dict[str, Any] = {}
        if web_request is not None:
            try:
                payload = await web_request.json(default={}) or {}
            except Exception:
                payload = {}
        scene = str(payload.get("scene") or "").strip()
        voice = str(payload.get("voice") or "").strip()
        if scene not in SCENE_LABELS:
            return self._fail("未知场景：%s" % scene)
        mapping = self._state.setdefault("scene_voices", {})
        if voice:
            mapping[scene] = voice
        else:
            mapping.pop(scene, None)
        self._state["scene_voices"] = mapping
        self._save_state()
        if scene == "default":
            self.config.update({"default_voice": voice})
            self._drop_provider_cache()
            await self._persist_config()
        return self._json({"code": 0, "data": {"scene": scene, "voice": voice}})

    async def api_history(self):
        return self._json({"code": 0, "data": {"items": self._state.get("history") or []}})

    async def api_test(self):
        """面板试听：合成一小段，直接回 base64，前端 <audio> 放。"""
        payload: dict[str, Any] = {}
        if web_request is not None:
            try:
                payload = await web_request.json(default={}) or {}
            except Exception:
                payload = {}
        text = str(payload.get("text") or "你好呀，我是来试音的。").strip()
        scene = str(payload.get("scene") or "").strip() or "default"
        if len(text) > self._int("max_chars", 80):
            text = text[: self._int("max_chars", 80)]
        companion_activity = ""
        instructions = (
            self._str("instructions_default").strip()
            or instructions_for(scene, companion_activity)
        )
        try:
            result, used = await self._synthesize(
                text, scene, self._scene_voice(scene), instructions
            )
        except SpeechError as exc:
            return self._fail("试听失败：%s" % exc)
        except Exception as exc:  # noqa: BLE001
            return self._fail("试听失败：%s" % exc)
        mime = "audio/wav" if result.extension == ".wav" else "audio/mpeg"
        encoded = base64.b64encode(result.audio).decode("ascii")
        return self._json(
            {
                "code": 0,
                "data": {
                    "audio": f"data:{mime};base64,{encoded}",
                    "bytes": len(result.audio),
                    "provider": used,
                    "voice": result.voice,
                    "scene": scene,
                    "elapsed_ms": int(result.elapsed * 1000),
                },
            }
        )

    async def terminate(self) -> None:
        self._save_state()
        logger.info("[amo_voice] 已退出，状态已落盘")
