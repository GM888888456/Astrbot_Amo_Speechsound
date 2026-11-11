# -*- coding: utf-8 -*-
"""语音后端：把「文字 → 音频字节」这件事收敛成一个统一接口。

三个实现：

* ``breeze``  —— Breeze Blue（Breeze TTS 2）。表演感最好，支持 ``instructions``
  语气指令，是主后端；需要使用者自己的 API Key。
* ``openai``  —— 任何 OpenAI 兼容的 ``POST /audio/speech``。
* ``edge``    —— 微软 Edge 朗读接口。免费、零配置，只用来兜底：Breeze 挂了、
  额度烧完、超时，换它把话念出来，而不是让用户看到一串报错。

设计上刻意不依赖 AstrBot 的 TTS provider 体系：框架升级、provider 改名都不会
把这里带崩，插件只把框架当发送通道。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

try:
    import aiohttp
except Exception:  # pragma: no cover - 没装 aiohttp 时上层会给出可读错误
    aiohttp = None  # type: ignore

BREEZE_DEFAULT_BASE = "https://api.breeze.blue"
OPENAI_DEFAULT_BASE = "https://api.openai.com/v1"

_MAX_REDIRECTS = 3
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

#: 各后端原生的音频输出格式。默认全给 wav：QQ/NapCat 那条链路对 wav 最稳，
#: 让 NapCat 自己去转 SILK，比自己生成 SILK 再发要可靠。
BREEZE_FORMAT = "wav"
OPENAI_FORMAT = "wav"
EDGE_FORMAT = "riff-24khz-16bit-mono-pcm"

#: Edge 朗读接口的固定客户端令牌（公开值，微软各客户端都在用）。
EDGE_TRUSTED_TOKEN = "6A5AA1D4EAFF4E9FB37E23D68491D6F4"
EDGE_WS_URL = (
    "wss://speech.platform.bing.com/consumer/speech/synthesize/readaloud/edge/v1"
)
EDGE_ORIGIN = "chrome-extension://jdiccldimpdaibmpdkjnbmckianbfold"
WIN_EPOCH = 11644473600


class SpeechError(Exception):
    """后端返回的错误，或调用后端时出的错。"""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        retry_after: float | None = None,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.retry_after = retry_after
        self.retryable = retryable


@dataclass(slots=True)
class SpeechRequest:
    """一次合成请求。字段取各家的并集，后端负责挑自己用得上的。"""

    text: str
    voice: str = ""
    instructions: str | None = None
    language: str | None = None
    model: str | None = None
    speed: float = 1.0
    volume: float = 1.0
    output_format: str = ""

    def format_for(self, default: str) -> str:
        return (self.output_format or default or "").strip()


@dataclass(slots=True)
class SpeechResult:
    """合成结果：音频字节 + 实际用的格式 + 是哪个后端出的。"""

    audio: bytes
    fmt: str
    provider: str
    voice: str
    elapsed: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def extension(self) -> str:
        fmt = (self.fmt or "").lower()
        if "wav" in fmt or "riff" in fmt or "pcm" in fmt:
            return ".wav"
        if "mpeg" in fmt or "mp3" in fmt:
            return ".mp3"
        if "opus" in fmt:
            return ".opus"
        if "ogg" in fmt:
            return ".ogg"
        return ".wav"


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is None:
        port = {"http": 80, "https": 443}.get(scheme, 0)
    return scheme, (parsed.hostname or "").lower(), port


def _short(value: str, limit: int = 300) -> str:
    value = (value or "").strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _parse_error(status: int, headers: Any, body: bytes) -> SpeechError:
    """把后端错误响应翻成一句人能看懂的话。"""
    code: str | None = None
    detail = ""
    try:
        data = json.loads(body.decode("utf-8")) if body else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        data = None

    if isinstance(data, dict):
        raw_code = data.get("code")
        if raw_code:
            code = str(raw_code)
        fragments: list[str] = []
        for key in ("error", "detail", "message"):
            value = data.get(key)
            if not value:
                continue
            fragments.append(
                value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            )
        detail = " - ".join(dict.fromkeys(fragments))
    elif body:
        detail = body.decode("utf-8", "ignore")

    message = f"HTTP {status}"
    if code:
        message += f" ({code})"
    if detail:
        message += "：" + _short(detail.strip())

    retry_after: float | None = None
    raw_retry = headers.get("Retry-After") if headers else None
    if raw_retry:
        try:
            retry_after = float(raw_retry)
        except (TypeError, ValueError):
            retry_after = None

    # 4xx 里除了 408/429 都是「配置错了」，重试没意义，也不该拿兜底盖过去
    retryable = status >= 500 or status in (408, 429)
    return SpeechError(
        message, status=status, code=code, retry_after=retry_after, retryable=retryable
    )


class _HTTPProvider:
    """带 key 的 HTTP 后端共用的请求骨架。"""

    name = "http"
    default_format = "wav"

    def __init__(self, settings: dict[str, Any]) -> None:
        self.api_base = str(settings.get("api_base") or "").strip().rstrip("/")
        self.api_key = str(settings.get("api_key") or "").strip()
        self.timeout = float(settings.get("timeout") or 30)

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> tuple[int, Any, bytes]:
        """手动跟重定向，且只在同源内跟。

        aiohttp 自动跟重定向会把所有 header 一起带过去，跨站一跳就等于把
        API Key 送给了别人。这里改成自己跟，越界直接拒绝。
        """
        if aiohttp is None:
            raise SpeechError("缺少 aiohttp，无法请求语音后端。", retryable=False)

        allowed = _origin(self.api_base or url)
        current_method = method
        current_url = url
        current_params = params
        current_payload = payload

        for _ in range(_MAX_REDIRECTS + 1):
            async with aiohttp.ClientSession() as session:
                async with session.request(
                    current_method,
                    current_url,
                    params=current_params,
                    json=current_payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                    allow_redirects=False,
                ) as response:
                    body = await response.read()
                    location = response.headers.get("Location")
                    if response.status not in _REDIRECT_STATUSES or not location:
                        return response.status, response.headers, body

                    next_url = urljoin(str(response.url), location)
                    if _origin(next_url) != allowed:
                        raise SpeechError(
                            "后端返回了跨站重定向，为保护 API Key 已拒绝跟随。",
                            retryable=False,
                        )
                    current_params = None
                    if response.status == 303 or (
                        response.status in (301, 302) and current_method != "GET"
                    ):
                        current_method = "GET"
                        current_payload = None
                    current_url = next_url

        raise SpeechError("后端重定向次数过多。")

    def _wrap_transport(self, exc: BaseException) -> SpeechError:
        if isinstance(exc, SpeechError):
            return exc
        if isinstance(exc, asyncio.TimeoutError):
            return SpeechError("语音后端超时。")
        return SpeechError(f"连不上语音后端：{_short(str(exc), 120)}")

    async def synthesize(self, request: SpeechRequest) -> SpeechResult:
        raise NotImplementedError

    async def list_voices(self) -> list[dict[str, Any]]:
        return []


class BreezeProvider(_HTTPProvider):
    """Breeze Blue：``POST {base}/v1/text-to-speech/{voice_id}``。"""

    name = "breeze"
    default_format = BREEZE_FORMAT

    def __init__(self, settings: dict[str, Any]) -> None:
        super().__init__(settings)
        self.api_base = (self.api_base or BREEZE_DEFAULT_BASE).rstrip("/")
        self.default_voice = str(settings.get("default_voice") or "").strip()

    def _headers(self) -> dict[str, str]:
        return {
            "xi-api-key": self.api_key,
            "user-agent": "astrbot-plugin-amo-speechsound/1.0.0",
        }

    def _payload(self, request: SpeechRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {"text": request.text}
        if request.model:
            payload["model_id"] = request.model
        if request.language:
            payload["language_code"] = request.language
        if request.instructions:
            payload["instructions"] = request.instructions

        voice_settings: dict[str, Any] = {}
        if abs(request.speed - 1.0) > 1e-6:
            voice_settings["speed"] = request.speed
        if abs(request.volume - 1.0) > 1e-6:
            voice_settings["volume"] = request.volume
        if voice_settings:
            payload["voice_settings"] = voice_settings
        return payload

    async def synthesize(self, request: SpeechRequest) -> SpeechResult:
        if not self.api_key:
            raise SpeechError("没填 Breeze Blue 的 API Key。", retryable=False)
        voice = request.voice or self.default_voice
        if not voice:
            raise SpeechError("没选音色，先去面板拉取音色再试。", retryable=False)

        fmt = request.format_for(self.default_format)
        url = f"{self.api_base}/v1/text-to-speech/{quote(voice, safe='')}"
        headers = {**self._headers(), "accept": "audio/*"}
        started = time.time()
        try:
            status, resp_headers, body = await self._request(
                "POST",
                url,
                params={"output_format": fmt, "delivery": "sync"},
                payload=self._payload(request),
                headers=headers,
            )
        except SpeechError:
            raise
        except BaseException as exc:  # noqa: BLE001 - 统一翻译成 SpeechError
            raise self._wrap_transport(exc) from exc

        if status != 200:
            raise _parse_error(status, resp_headers, body)
        if not body:
            raise SpeechError("风格后端返回了空音频。", status=200)
        return SpeechResult(
            audio=body,
            fmt=fmt,
            provider=self.name,
            voice=voice,
            elapsed=time.time() - started,
            extra={"history_item_id": resp_headers.get("history-item-id")},
        )

    async def list_voices(
        self, *, search: str = "", language: str = "", limit: int = 50
    ) -> list[dict[str, Any]]:
        """拉账号可用的音色。面板只让选、不让手填 voice_id。"""
        if not self.api_key:
            raise SpeechError("没填 Breeze Blue 的 API Key。", retryable=False)
        params: dict[str, Any] = {"page_size": max(1, min(int(limit or 50), 100))}
        if search:
            params["search"] = search
        if language:
            params["language_code"] = language
        headers = {**self._headers(), "accept": "application/json"}
        try:
            status, resp_headers, body = await self._request(
                "GET", f"{self.api_base}/v1/voices", params=params, headers=headers
            )
        except SpeechError:
            raise
        except BaseException as exc:  # noqa: BLE001
            raise self._wrap_transport(exc) from exc

        if status != 200:
            raise _parse_error(status, resp_headers, body)
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SpeechError("音色列表不是合法 JSON。") from exc
        return _normalize_voices(data)


def _normalize_voices(data: Any) -> list[dict[str, Any]]:
    """把各家五花八门的音色列表压成 {id, name, language, gender, age, desc}。"""
    items: list[Any] = []
    if isinstance(data, dict):
        for key in ("voices", "data", "items", "result"):
            value = data.get(key)
            if isinstance(value, list):
                items = value
                break
        if not items and isinstance(data.get("voices"), dict):
            items = list(data["voices"].values())
    elif isinstance(data, list):
        items = data

    voices: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        vid = str(
            item.get("voice_id") or item.get("id") or item.get("voiceId") or ""
        ).strip()
        if not vid or vid in seen:
            continue
        seen.add(vid)
        voices.append(
            {
                "id": vid,
                "name": str(
                    item.get("name") or item.get("display_name") or vid
                ).strip(),
                "language": str(
                    item.get("language_code") or item.get("language") or ""
                ).strip(),
                "gender": str(item.get("gender") or "").strip(),
                "age": str(item.get("age") or "").strip(),
                "desc": _short(
                    str(
                        item.get("description")
                        or item.get("preview_text")
                        or item.get("labels", {}).get("description", "")
                        if isinstance(item.get("labels"), dict)
                        else item.get("description") or ""
                    ),
                    120,
                ),
                "preview": str(item.get("preview_url") or "").strip(),
            }
        )
    return voices


class OpenAIProvider(_HTTPProvider):
    """OpenAI 兼容：``POST {base}/audio/speech``。"""

    name = "openai"
    default_format = OPENAI_FORMAT

    def __init__(self, settings: dict[str, Any]) -> None:
        super().__init__(settings)
        self.api_base = (self.api_base or OPENAI_DEFAULT_BASE).rstrip("/")
        self.default_voice = str(settings.get("default_voice") or "").strip()
        self.default_model = str(settings.get("model_id") or "").strip() or "tts-1"

    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self.api_key}",
            "user-agent": "astrbot-plugin-amo-speechsound/1.0.0",
        }

    async def synthesize(self, request: SpeechRequest) -> SpeechResult:
        if not self.api_key:
            raise SpeechError("没填 API Key。", retryable=False)
        voice = request.voice or self.default_voice
        if not voice:
            raise SpeechError("没选音色，先去面板拉取音色再试。", retryable=False)

        fmt = request.format_for(self.default_format)
        payload: dict[str, Any] = {
            "model": request.model or self.default_model,
            "input": request.text,
            "voice": voice,
            "response_format": fmt,
        }
        if abs(request.speed - 1.0) > 1e-6:
            payload["speed"] = request.speed

        started = time.time()
        try:
            status, resp_headers, body = await self._request(
                "POST",
                f"{self.api_base}/audio/speech",
                payload=payload,
                headers={**self._headers(), "accept": "audio/*"},
            )
        except SpeechError:
            raise
        except BaseException as exc:  # noqa: BLE001
            raise self._wrap_transport(exc) from exc

        if status != 200:
            raise _parse_error(status, resp_headers, body)
        if not body:
            raise SpeechError("后端返回了空音频。", status=200)
        return SpeechResult(
            audio=body,
            fmt=fmt,
            provider=self.name,
            voice=voice,
            elapsed=time.time() - started,
        )

    async def list_voices(self) -> list[dict[str, Any]]:
        """OpenAI 兼容网关大多没有音色列表接口，回一份常用内置音色。"""
        return [
            {"id": "alloy", "name": "alloy", "language": "multi", "gender": "", "age": "", "desc": "中性", "preview": ""},
            {"id": "echo", "name": "echo", "language": "multi", "gender": "男", "age": "", "desc": "温和男声", "preview": ""},
            {"id": "fable", "name": "fable", "language": "zh" if False else "multi", "gender": "", "age": "", "desc": "叙事感", "preview": ""},
            {"id": "onyx", "name": "onyx", "language": "multi", "gender": "男", "age": "", "desc": "低沉男声", "preview": ""},
            {"id": "nova", "name": "nova", "language": "multi", "gender": "女", "age": "", "desc": "清亮女声", "preview": ""},
            {"id": "shimmer", "name": "shimmer", "language": "multi", "gender": "女", "age": "", "desc": "柔和女声", "preview": ""},
        ]


class EdgeProvider(_HTTPProvider):
    """微软 Edge 朗读：免费、零配置，只做兜底。

    走的是客户端同一条 WebSocket 协议。它没有 ``instructions``、没有情绪，
    音色也是固定的，所以定位很明确：**主后端不行时，别让用户看到报错。**
    """

    name = "edge"
    default_format = EDGE_FORMAT

    #: 常用中文音色。面板里列出来给人选，不额外请求微软的列表接口。
    VOICES = [
        {"id": "zh-CN-XiaoxiaoNeural", "name": "晓晓", "language": "zh-CN", "gender": "女", "age": "青年", "desc": "温暖、自然，通用首选", "preview": ""},
        {"id": "zh-CN-XiaoyiNeural", "name": "晓伊", "language": "zh-CN", "gender": "女", "age": "青年", "desc": "明亮、活泼", "preview": ""},
        {"id": "zh-CN-YunxiNeural", "name": "云希", "language": "zh-CN", "gender": "男", "age": "青年", "desc": "清爽男声", "preview": ""},
        {"id": "zh-CN-YunxiaNeural", "name": "云夏", "language": "zh-CN", "gender": "男", "age": "少年", "desc": "少年感", "preview": ""},
        {"id": "zh-CN-YunjianNeural", "name": "云健", "language": "zh-CN", "gender": "男", "age": "青年", "desc": "沉稳", "preview": ""},
        {"id": "zh-CN-liaoning-XiaobeiNeural", "name": "晓北", "language": "zh-CN", "gender": "女", "age": "青年", "desc": "东北口音", "preview": ""},
        {"id": "zh-CN-shaanxi-XiaoniNeural", "name": "晓妮", "language": "zh-CN", "gender": "女", "age": "青年", "desc": "陕西口音", "preview": ""},
        {"id": "zh-TW-HsiaoChenNeural", "name": "曉臻", "language": "zh-TW", "gender": "女", "age": "青年", "desc": "台湾腔", "preview": ""},
    ]

    def __init__(self, settings: dict[str, Any]) -> None:
        super().__init__(settings)
        self.default_voice = (
            str(settings.get("edge_voice") or "").strip() or "zh-CN-XiaoxiaoNeural"
        )

    async def list_voices(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.VOICES]

    @staticmethod
    def _sec_ms_gec() -> str:
        """微软用来防滥用的时间片令牌；算法与官方客户端一致，300 秒一跳。"""
        ticks = (time.time() + WIN_EPOCH) * 10_000_000
        ticks -= ticks % 3_000_000_000  # 300 秒
        payload = f"{ticks:.0f}{EDGE_TRUSTED_TOKEN}"
        return hashlib.sha256(payload.encode("ascii")).hexdigest().upper()

    @staticmethod
    def _ssml(text: str, voice: str, speed: float, volume: float) -> str:
        def esc(value: str) -> str:
            return (
                value.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;")
            )

        rate = f"{int(round((speed - 1.0) * 100)):+d}%" if abs(speed - 1.0) > 1e-6 else "+0%"
        vol = f"{int(round((volume - 1.0) * 100)):+d}%" if abs(volume - 1.0) > 1e-6 else "+0%"
        lang = voice.split("-")[0] + "-" + voice.split("-")[1] if voice.count("-") >= 2 else "zh-CN"
        return (
            "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='"
            + lang
            + "'>"
            + f"<voice name='{esc(voice)}'>"
            + f"<prosody rate='{rate}' volume='{vol}'>{esc(text)}</prosody>"
            + "</voice></speak>"
        )

    async def synthesize(self, request: SpeechRequest) -> SpeechResult:
        if aiohttp is None:
            raise SpeechError("缺少 aiohttp，Edge 兜底不可用。")
        voice = request.voice or self.default_voice
        fmt = request.format_for(self.default_format)
        started = time.time()

        url = (
            EDGE_WS_URL
            + "?TrustedClientToken=" + EDGE_TRUSTED_TOKEN
            + "&Sec-MS-GEC=" + self._sec_ms_gec()
            + "&Sec-MS-GEC-Version=1-130.0.2849.68"
            + "&ConnectionId=" + uuid.uuid4().hex
        )
        headers = {
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
            "Origin": EDGE_ORIGIN,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
            ),
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "en-US,en;q=0.9",
        }

        # 每条消息前面有一段纯文本头，用 \r\n\r\n 与负载分开
        config_msg = (
            "Content-Type:application/json; charset=utf-8\r\n"
            "Path:speech.config\r\n\r\n"
            + json.dumps(
                {
                    "context": {
                        "synthesis": {
                            "audio": {
                                "metadataoptions": {
                                    "sentenceBoundaryEnabled": "false",
                                    "wordBoundaryEnabled": "false",
                                },
                                "outputFormat": fmt,
                            }
                        }
                    }
                },
                separators=(",", ":"),
            )
        )
        ssml = self._ssml(request.text, voice, request.speed, request.volume)
        ssml_msg = (
            f"X-RequestId:{uuid.uuid4().hex}\r\n"
            "Content-Type:application/ssml+xml\r\n"
            f"X-Timestamp:{time.strftime('%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)', time.gmtime())}Z\r\n"
            "Path:ssml\r\n\r\n"
            + ssml
        )

        chunks: list[bytes] = []
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientWSTimeout(ws_close=self.timeout),
                ) as ws:
                    await ws.send_str(config_msg)
                    await ws.send_str(ssml_msg)
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.BINARY:
                            # 二进制帧：头 2 字节大端是头部长度，其后跳过头部
                            data = msg.data
                            if len(data) < 2:
                                continue
                            header_len = int.from_bytes(data[:2], "big")
                            if header_len + 2 <= len(data):
                                chunks.append(data[header_len + 2 :])
                        elif msg.type == aiohttp.WSMsgType.TEXT:
                            if "Path:turn.end" in msg.data:
                                break
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            raise SpeechError("Edge 朗读连接中断。")
        except SpeechError:
            raise
        except BaseException as exc:  # noqa: BLE001
            raise self._wrap_transport(exc) from exc

        audio = b"".join(chunks)
        if not audio:
            raise SpeechError("Edge 朗读返回了空音频。")
        return SpeechResult(
            audio=audio,
            fmt=fmt,
            provider=self.name,
            voice=voice,
            elapsed=time.time() - started,
        )


PROVIDERS = {
    "breeze": BreezeProvider,
    "openai": OpenAIProvider,
    "edge": EdgeProvider,
}


def build_provider(name: str, settings: dict[str, Any]):
    """按名字建后端；不认识的配置一律退回 edge（免费、能出声）。"""
    key = (name or "").strip().lower()
    factory = PROVIDERS.get(key) or EdgeProvider
    return factory(settings)


def provider_names() -> list[str]:
    return list(PROVIDERS)
