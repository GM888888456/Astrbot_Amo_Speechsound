# -*- coding: utf-8 -*-
"""配置自愈：插件升级/重装之后，别让人重新填一遍 API key。

为什么会丢（AstrBot 侧的行为，不是我们的 bug）：

1. 配置文件名由**插件目录名**拼成（``config/<目录名>_config.json``）。目录一改名，
   就是一份全新的空配置，旧文件留在那儿当孤儿。
2. 每次加载都会拿 ``_conf_schema.json`` 的默认值重建配置再覆盖回去
   （``conf.clear(); conf.update(new_conf)``）—— schema 里没有的键会被删掉。
3. 卸载时如果勾了「同时删除配置」，文件是直接删的。

所以插件这边只能自救：**把读到过的值在「不会被升级覆盖、也不会被 schema 重写」的地方
留一份快照**，下次启动发现某项空了就补回去。快照放插件数据目录
（``plugin_data/<插件名>/config_snapshot.json``，0600），既不在插件目录里，也不在配置
文件里，升级和重写都碰不到它。

三条原则：

1. **只补空，不覆盖。** 用户改了值以当前值为准，只有空的/缺的才从快照补。
2. **只认同名同键。** 不做模糊匹配，避免把 A 插件的 key 塞进 B 插件。
3. **日志只说键名，不说值。** 这些多半是 API key。
"""
from __future__ import annotations

import glob
import json
import os
from typing import Any

SNAPSHOT_NAME = "config_snapshot.json"

#: 视为「没配」的值。空列表/空字典也算没配，因为 schema 默认值就是那样。
EMPTY_VALUES = (None, "")


def _is_empty(value: Any) -> bool:
    if value in EMPTY_VALUES:
        return True
    if isinstance(value, (list, dict)) and not value:
        return True
    return False


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def snapshot_path(data_dir: str) -> str:
    return os.path.join(str(data_dir or ""), SNAPSHOT_NAME)


def load_snapshot(data_dir: str) -> dict:
    path = snapshot_path(data_dir)
    if not os.path.exists(path):
        return {}
    return _read_json(path)


def save_snapshot(data_dir: str, values: dict) -> bool:
    """写快照。0600 —— 里面可能有 API key。"""
    directory = str(data_dir or "")
    if not directory:
        return False
    try:
        os.makedirs(directory, exist_ok=True)
        path = snapshot_path(directory)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(values, fh, ensure_ascii=False, indent=2, sort_keys=True)
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def schema_keys(plugin_dir: str) -> list:
    """插件自己的 schema 键（配置能被 AstrBot 保留的那些）。"""
    path = os.path.join(str(plugin_dir or ""), "_conf_schema.json")
    return list(_read_json(path).keys())


def orphan_values(config_path: str, keys: list, own_name: str = "") -> dict:
    """从同目录下别的 ``*_config.json``（含 ``.bak*``）里捞同名的非空值。

    这就是「目录改名导致配置搬家」那一类事故的补救：新文件是空的，旧文件还在，
    键名一样，值就该搬过来。
    """
    if not config_path:
        return {}
    folder = os.path.dirname(os.path.abspath(config_path))
    own = own_name or os.path.basename(config_path)
    found: dict = {}
    files = []
    for pattern in ("*_config.json", "*_config.json.bak*", "*.json.bak*"):
        files.extend(glob.glob(os.path.join(folder, pattern)))
    # 越新的越优先：后扫到的覆盖前面
    for path in sorted(set(files),
                       key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0):
        if os.path.basename(path) == own:
            continue
        data = _read_json(path)
        for key in keys:
            value = data.get(key)
            if not _is_empty(value):
                found[key] = value
    return found


def heal(config, *, plugin_name: str, plugin_dir: str, data_dir: str,
         logger=None, extra_keys: tuple = ()) -> dict:
    """补回空掉的配置项，然后刷新快照。返回恢复明细（只有键名）。"""
    report = {"restored": [], "from_snapshot": [], "from_orphan": [],
              "snapshot_written": False, "keys": 0}
    if config is None:
        return report

    # 没有真实配置路径（测试里的裸 dict、或者 AstrBot 还没落盘）：
    # 既没有地方补，也不该写快照——否则测试之间会互相污染。
    config_path = str(getattr(config, "config_path", "") or "")
    if not config_path or not callable(getattr(config, "save_config", None)):
        return report

    keys = list(schema_keys(plugin_dir))
    for key in extra_keys:
        if key not in keys:
            keys.append(key)
    # 快照里的键也算：万一 schema 漏了某个键，至少这一轮还能补
    snap = load_snapshot(data_dir)
    for key in snap:
        if key not in keys:
            keys.append(key)
    report["keys"] = len(keys)
    if not keys:
        return report

    orphans = orphan_values(config_path, keys, os.path.basename(config_path))

    for key in keys:
        if not _is_empty(config.get(key)):
            continue
        if not _is_empty(snap.get(key)):
            config[key] = snap[key]
            report["restored"].append(key)
            report["from_snapshot"].append(key)
        elif not _is_empty(orphans.get(key)):
            config[key] = orphans[key]
            report["restored"].append(key)
            report["from_orphan"].append(key)

    if report["restored"]:
        saver = getattr(config, "save_config", None)
        if callable(saver):
            try:
                saver()
            except Exception:
                pass
        if logger is not None:
            try:
                logger.warning(
                    "[%s] 配置自愈：%d 项被补回（快照 %d / 旧配置文件 %d）：%s"
                    % (plugin_name, len(report["restored"]),
                       len(report["from_snapshot"]), len(report["from_orphan"]),
                       ", ".join(report["restored"]))
                )
            except Exception:
                pass

    # 刷新快照：当前非空值全存一份（键用 schema 集合，避免越存越多陌生键）
    values = {key: config.get(key) for key in keys if not _is_empty(config.get(key))}
    report["snapshot_written"] = save_snapshot(data_dir, values)
    return report


def remember(config, *, plugin_dir: str, data_dir: str,
             extra_keys: tuple = ()) -> bool:
    """只刷新快照（配置变了以后调一次）。没有真实 config_path 时什么都不做。"""
    if not str(getattr(config, "config_path", "") or ""):
        return False
    keys = list(schema_keys(plugin_dir))
    for key in extra_keys:
        if key not in keys:
            keys.append(key)
    values = {}
    for key in keys:
        try:
            value = config.get(key)
        except Exception:
            continue
        if not _is_empty(value):
            values[key] = value
    return save_snapshot(data_dir, values)


def fallback_data_dir(plugin_dir: str, plugin_name: str) -> str:
    """算插件数据目录：``<AstrBot 数据目录>/plugin_data/<插件名>``。

    注意层级：插件目录是 ``<data>/plugins/<插件名>``，**要上跳两级**才到 ``<data>``；
    ``plugin_data`` 和 ``plugins`` 是兄弟目录，不是它的子目录。
    """
    path = os.path.abspath(str(plugin_dir or ""))
    plugins_root = os.path.dirname(path)
    data_root = os.path.dirname(plugins_root)
    target = os.path.join(data_root, "plugin_data", str(plugin_name or ""))
    # 兜底：万一目录结构和预期不同，退回到旧算法里真实存在的那一个
    if os.path.isdir(target):
        return target
    legacy = os.path.join(plugins_root, "plugin_data", str(plugin_name or ""))
    if os.path.isdir(legacy):
        return legacy
    return target
