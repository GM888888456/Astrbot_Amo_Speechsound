# -*- coding: utf-8 -*-
"""开口的决策层：该不该发语音、用什么音色、配什么语气。

这一层是整个插件的重点。语音不是"把文字念一遍"——文本可以写得很长、可以带
链接，念出来就是灾难。所以先过规则（0 成本，挡掉绝大多数不该发的），再打分
（也是本地算，不额外调模型），最后才合成。

设计原则：

1. **Amo 主动调用时只拦硬伤**（太长、群聊没开、额度用完）。她已经决定说了，
   插件不该再用阈值把她按住。
2. **兜底路径才用阈值**。用户明说"用语音说"但 Amo 只回了文字时，才由规则
   判断要不要补一条语音。
3. **每条拦截都留下原因**。面板里能查到"为什么这条没发出去"，否则调参会变成
   玄学。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# 文本清洗
# ---------------------------------------------------------------------------

_CODE_FENCE_RE = re.compile(r"```.*?```", re.S)
_INLINE_CODE_RE = re.compile(r"`[^`\n]{1,80}`")
_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_MD_LINK_RE = re.compile(r"\[([^\]]{1,40})\]\((?:[^)]+)\)")
_MD_MARK_RE = re.compile(r"[*_~`#>]+|^[-+]\s+|^\d+\.\s+", re.M)
_AT_RE = re.compile(r"@\S{1,20}")
_BRACKET_TAG_RE = re.compile(r"\[(?:CQ|图片|表情|语音)[^\]]*\]")
_SPACE_RE = re.compile(r"[ \t\u00a0]+")
_EMOJI_RE = re.compile(
    "[" "\U0001f300-\U0001faff" "\U00002600-\U000027bf" "\U0001f1e6-\U0001f1ff" "\ufe0f" "]+"
)

#: 念出来会尴尬、或者后端会读错的标记
_SPEECH_UNFRIENDLY = ("```", "http://", "https://", "\\n\\n", "| ---", "<br")


def sanitize_for_speech(text: str) -> str:
    """把模型输出清洗成"能念出来"的样子。

    去掉代码块、链接、Markdown 记号、@、emoji（念不出来），剩下的才是话。
    """
    text = text or ""
    text = _CODE_FENCE_RE.sub("", text)
    text = _INLINE_CODE_RE.sub("", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _URL_RE.sub("", text)
    text = _BRACKET_TAG_RE.sub("", text)
    text = _AT_RE.sub("", text)
    text = _MD_MARK_RE.sub("", text)
    text = _EMOJI_RE.sub("", text)
    text = text.replace("**", "").replace("__", "")
    text = _SPACE_RE.sub(" ", text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def split_text(text: str, limit: int) -> list[str]:
    """按标点切段，尽量不把一句话切成两半。

    后端有单请求字符上限，QQ 语音也有时长上限；宁可多切一段，也不要一句念到
    一半断掉。
    """
    limit = max(1, int(limit or 1))
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    parts = re.split(r"(?<=[。！？!?；;…\n])", text)
    segments: list[str] = []
    buffer = ""
    for part in parts:
        if not part:
            continue
        if len(buffer) + len(part) <= limit:
            buffer += part
            continue
        if buffer:
            segments.append(buffer)
            buffer = ""
        while len(part) > limit:
            segments.append(part[:limit])
            part = part[limit:]
        buffer = part
    if buffer:
        segments.append(buffer)
    return [item.strip() for item in segments if item.strip()]


# ---------------------------------------------------------------------------
# 决策
# ---------------------------------------------------------------------------

#: 情绪词 → 加分。这些是"有语气"的话，念出来比打字有感觉。
EMOTION_WORDS: dict[str, float] = {
    "晚安": 0.28, "早安": 0.2, "想你": 0.3, "想我": 0.18, "抱抱": 0.28,
    "亲亲": 0.28, "爱你": 0.3, "喜欢你": 0.26, "舍不得": 0.24, "等你": 0.2,
    "别走": 0.26, "陪你": 0.22, "心疼": 0.26, "难过": 0.24, "委屈": 0.24,
    "哭": 0.22, "开心": 0.16, "好开心": 0.22, "谢谢你": 0.18, "对不起": 0.2,
    "生日快乐": 0.3, "节日快乐": 0.24, "加油": 0.16, "晚安啦": 0.3,
    "我在": 0.22, "我在这": 0.24, "乖": 0.18, "辛苦": 0.18, "早点睡": 0.22,
}

#: 不适合念的内容：清单、代码、参数说明
CHATTY_BLOCK_WORDS = ("```", "def ", "import ", "SELECT ", "http://", "https://")

#: 场景 → 用哪个音色（面板里配）。识别按优先级从高到低。
SCENE_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("sleep", ("晚安", "睡了", "困", "睡觉", "做梦", "早点睡", "熬夜", "睡着了")),
    ("tender", ("想你", "抱抱", "亲亲", "爱你", "喜欢你", "心疼", "乖", "陪你")),
    ("sad", ("难过", "委屈", "哭", "伤心", "失落", "不开心", "想哭")),
    ("angry", ("哼", "讨厌", "生气", "不理你", "走开", "烦人")),
    ("happy", ("开心", "太好了", "哈哈", "嘿嘿", "好耶", "高兴")),
]

SCENE_LABELS = {
    "default": "日常",
    "sleep": "睡前",
    "tender": "撒娇",
    "sad": "难过",
    "angry": "生气",
    "happy": "开心",
    "home": "在家",
    "out": "出门",
}


@dataclass(slots=True)
class SpeakContext:
    """决策需要的全部上下文。由上层（插件）填，决策层不碰框架对象。"""

    text: str
    private: bool = True
    hour: int = 12
    quota_left: int = -1            # -1 = 不限
    seconds_since_last: float = 1e9  # 距上一条语音多久
    cooldown: int = 240
    group_enable: bool = False
    max_chars: int = 80          # 每段字数上限
    max_segments: int = 2        # 最多几段；总长上限 = max_chars * max_segments
    threshold: float = 0.6
    relation: str = ""              # 普通 / 熟识 / 亲近
    place: str = ""                 # 家 / 公司 / 外面
    activity: str = ""

    @property
    def at_home(self) -> bool:
        return "家" in self.place or "宿舍" in self.place


@dataclass(slots=True)
class Decision:
    allowed: bool
    reason: str
    score: float = 0.0
    scene: str = "default"
    text: str = ""
    tags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "score": round(self.score, 2),
            "scene": self.scene,
            "tags": list(self.tags),
        }


def detect_scene(text: str, ctx: SpeakContext) -> str:
    """先看这句话像什么情绪，再看人在哪。"""
    body = text or ""
    for scene, words in SCENE_RULES:
        if any(word in body for word in words):
            return scene
    if ctx.place:
        return "home" if ctx.at_home else "out"
    return "default"


def score_speech(ctx: SpeakContext, text: str) -> tuple[float, list[str]]:
    """本地打分，不花任何 tokens。返回 (分数, 命中的理由)。"""
    score = 0.0
    tags: list[str] = []

    if ctx.private:
        score += 0.20
        tags.append("私聊")
    elif ctx.group_enable:
        score -= 0.15
        tags.append("群聊")

    if ctx.hour >= 22 or ctx.hour < 2:
        score += 0.18
        tags.append("深夜")
    elif ctx.hour < 7:
        score += 0.10
        tags.append("清晨")

    length = len(text)
    if length <= 20:
        score += 0.18
        tags.append("短句")
    elif length <= 40:
        score += 0.10
    elif length > ctx.max_chars:
        score -= 0.30
        tags.append("偏长")

    hits = [word for word in EMOTION_WORDS if word in text]
    if hits:
        bonus = min(0.36, sum(EMOTION_WORDS[word] for word in hits))
        score += bonus
        tags.append("情绪:" + "/".join(hits[:3]))

    if text.rstrip().endswith(("？", "?")):
        score -= 0.12
        tags.append("疑问")
    if ctx.at_home:
        score += 0.08
        tags.append("在家")
    if ctx.relation in ("亲近", "恋慕", "恋人", "love", "close"):
        score += 0.12
        tags.append("关系:" + ctx.relation)

    return max(0.0, min(1.0, score)), tags


def decide(ctx: SpeakContext, *, manual: bool = False) -> Decision:
    """判断这条要不要发语音。

    ``manual=True`` 表示 Amo 主动调用（工具调用）：只拦硬伤，不拦阈值。
    """
    raw = ctx.text or ""
    clean = sanitize_for_speech(raw)
    scene = detect_scene(clean, ctx)

    if not clean:
        return Decision(False, "清洗后没有可念的字", scene=scene, text="", tags=["空"])
    if any(word in raw for word in CHATTY_BLOCK_WORDS):
        return Decision(False, "含代码或链接，念出来没意义", scene=scene, text=clean, tags=["含代码"])
    total_limit = max(1, int(ctx.max_chars)) * max(1, int(ctx.max_segments))
    if len(clean) > total_limit:
        return Decision(
            False,
            f"太长（{len(clean)} 字 > 上限 {total_limit} = 每段 {ctx.max_chars} × {ctx.max_segments} 段）",
            scene=scene,
            text=clean,
            tags=["超长"],
        )
    if not ctx.private and not ctx.group_enable:
        return Decision(False, "群聊默认不发语音", scene=scene, text=clean, tags=["群聊"])
    if ctx.quota_left == 0:
        return Decision(False, "今天的语音额度用完了", scene=scene, text=clean, tags=["额度"])

    score, tags = score_speech(ctx, clean)

    if manual:
        if ctx.cooldown > 0 and ctx.seconds_since_last < ctx.cooldown:
            score = max(score, 0.0)
        return Decision(True, "Amo 主动开口", max(score, 0.7), scene, clean, tags + ["主动"])

    if ctx.cooldown > 0 and ctx.seconds_since_last < ctx.cooldown:
        return Decision(
            False,
            f"冷却中：距上一条语音不到 {ctx.cooldown} 秒",
            score,
            scene,
            clean,
            tags + ["冷却"],
        )
    if score < ctx.threshold:
        return Decision(
            False,
            f"分数 {score:.2f} 低于阈值 {ctx.threshold:.2f}",
            score,
            scene,
            clean,
            tags,
        )
    return Decision(True, f"分数 {score:.2f} 过阈", score, scene, clean, tags)


def instructions_for(scene: str, activity: str = "") -> str:
    """给后端的表演指令。这决定了"像不像真人在说话"。"""
    base = {
        "sleep": "深夜犯困，气声多、语速慢、音量轻，像贴着耳边小声说。",
        "tender": "黏人撒娇，尾音上扬，语气软，带一点笑意。",
        "sad": "情绪低落，声音发闷，语速偏慢，句尾有点抖。",
        "angry": "在闹脾气，语气冲、短促，但不吼。",
        "happy": "心情很好，轻快明亮，带笑。",
        "home": "在家里放松地说话，随意、自然，不要播音腔。",
        "out": "在外面说话，音量正常，环境略吵，语速正常。",
        "default": "像日常聊天一样自然，不要播音腔，不要抑扬顿挫地朗读。",
    }.get(scene, "")
    if activity and scene in ("home", "out", "default"):
        base += f"（此刻正在{activity}）"
    return base
