# -*- coding: utf-8 -*-
"""我想永远陪着你-语音（语音联动）插件。"""

try:  # 正常包内加载
    from .main import SpeechSoundPlugin, PLUGIN_NAME, PLUGIN_VERSION
except Exception:  # 单测/CI 按文件加载时没有包上下文，退回绝对导入
    from main import SpeechSoundPlugin, PLUGIN_NAME, PLUGIN_VERSION

__all__ = ["SpeechSoundPlugin", "PLUGIN_NAME", "PLUGIN_VERSION"]
