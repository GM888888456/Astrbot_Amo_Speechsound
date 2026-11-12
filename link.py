# -*- coding: utf-8 -*-
"""挂载插件的连接状态：我自己有没有连上终端、记忆库。

这段代码属于**每个挂载插件自己**，不放在终端插件里。终端挂了的时候，
挂载插件的面板照样要能说清楚「是谁没连上」，而不是等终端来告诉它。

面板只认这几个字段，别改名字：

    terminal_connected / terminal_name / terminal_version
    data_connected / bot_id / last_heartbeat / error
"""

from __future__ import annotations

import sys
import time

TERMINAL_NAME = "Astrbot_Amo_forever"
DATA_NAME = "Astr_Amo_data"

TERMINAL_NAMES = ("Astrbot_Amo_forever", "astrbot_plugin_amo_forever", "amo_forever")
DATA_NAMES = ("Astr_Amo_data", "astrbot_plugin_amo_data", "Astr_Amo_data_plugin")

TERMINAL_MARKERS = ("amo_forever",)
DATA_MARKERS = ("amo_data", "astr_amo_data")

DEFAULT_BOT_ID = "陪伴"


def _instance(meta, require_mem=False):
    """AstrBot 各版本把实例放在不同属性上，挨个试。"""
    if meta is None:
        return None
    for attr in ("star_cls", "instance", "star", "plugin"):
        value = getattr(meta, attr, None)
        if value is None:
            continue
        if not require_mem or hasattr(value, "mem"):
            return value
    return None


def _find(context, names, markers, require_mem=False):
    """先按名字精确找，再按模块名兜底。找不到返回 None，不抛异常。"""
    getter = getattr(context, "get_registered_star", None)
    if callable(getter):
        for name in names:
            try:
                found = _instance(getter(name), require_mem)
            except Exception:
                found = None
            if found is not None:
                return found
    lister = getattr(context, "get_all_stars", None)
    if callable(lister):
        try:
            metas = list(lister() or [])
        except Exception:
            metas = []
        for meta in metas:
            marker = ((getattr(meta, "name", "") or "") + " "
                      + (getattr(meta, "module_path", "") or "")).lower()
            if not any(word in marker for word in markers):
                continue
            found = _instance(meta, require_mem)
            if found is not None:
                return found
    return None


def version_of(plugin) -> str:
    """读插件版本号：实例属性 → 模块全局。读不到返回空串。"""
    if plugin is None:
        return ""
    for attr in ("PLUGIN_VERSION", "VERSION", "plugin_version"):
        value = getattr(plugin, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    module = sys.modules.get(getattr(plugin.__class__, "__module__", ""))
    for name in ("PLUGIN_VERSION", "VERSION"):
        value = getattr(module, name, None) if module is not None else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def bot_id_of(terminal, data) -> str:
    """和终端用同一个 bot_id；读不到就用默认的。"""
    if terminal is not None:
        getter = getattr(terminal, "_data_bot_id", None)
        if callable(getter):
            try:
                value = str(getter() or "").strip()
                if value:
                    return value
            except Exception:
                pass
        conf = getattr(terminal, "config", None) or {}
        for key in ("data_bot_id", "bot_id", "persona_name"):
            value = str(conf.get(key) or "").strip()
            if value:
                return value
    if data is not None:
        conf = getattr(data, "config", None) or {}
        value = str(conf.get("bot_id") or "").strip()
        if value:
            return value
    return DEFAULT_BOT_ID


def probe(plugin) -> dict:
    """探一次连接状态。纯读，不改任何业务状态。"""
    now = time.time()
    context = getattr(plugin, "context", None)
    terminal = _find(context, TERMINAL_NAMES, TERMINAL_MARKERS)
    data = _find(context, DATA_NAMES, DATA_MARKERS, require_mem=True)

    payload = {
        "terminal_connected": terminal is not None,
        "terminal_name": TERMINAL_NAME,
        "terminal_version": version_of(terminal),
        "data_connected": data is not None,
        "data_name": DATA_NAME,
        "data_version": version_of(data),
        "bot_id": bot_id_of(terminal, data),
        "last_heartbeat": 0,
        "checked_at": int(now),
        "error": "",
    }

    if terminal is None:
        payload["error"] = "没找到终端插件（Astrbot_Amo_forever）"
    elif data is None:
        payload["error"] = "终端在，但没找到记忆库（Astr_Amo_data）"

    if terminal is not None:
        plugin._link_last_ok = now
        payload["last_heartbeat"] = 0
    else:
        last = float(getattr(plugin, "_link_last_ok", 0) or 0)
        payload["last_heartbeat"] = int(now - last) if last else -1
    return payload


def summary(payload: dict) -> str:
    """给日志/指令用的一行摘要。"""
    if payload.get("terminal_connected"):
        text = "终端 %s 已连接" % (payload.get("terminal_version") or "?")
    else:
        text = "终端未连接"
    if payload.get("data_connected"):
        text += "，记忆库 %s 已连接" % (payload.get("data_version") or "?")
    else:
        text += "，记忆库未连接"
    text += "，bot_id %s" % (payload.get("bot_id") or DEFAULT_BOT_ID)
    if payload.get("error"):
        text += "（%s）" % payload["error"]
    return text
