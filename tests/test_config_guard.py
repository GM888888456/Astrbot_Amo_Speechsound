# -*- coding: utf-8 -*-
"""配置自愈：升级/重装把配置清空后，要靠快照和旧配置把 key 补回来。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config_guard  # noqa: E402


class FakeConfig(dict):
    """够用的 AstrBotConfig 替身：config_path + save_config()。"""

    def __init__(self, config_path, values=None):
        super().__init__(values or {})
        self.config_path = config_path
        self.saved = 0

    def save_config(self, *args, **kwargs):
        self.saved += 1


class ConfigGuardCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.plugin_dir = os.path.join(self.root, "plugins", "Demo")
        self.data_dir = os.path.join(self.root, "plugin_data", "Demo")
        self.cfg_dir = os.path.join(self.root, "config")
        for path in (self.plugin_dir, self.data_dir, self.cfg_dir):
            os.makedirs(path, exist_ok=True)
        with open(os.path.join(self.plugin_dir, "_conf_schema.json"), "w", encoding="utf-8") as fh:
            json.dump({"hefeng_key": {"type": "string", "default": ""},
                       "weather_host": {"type": "string", "default": ""},
                       "enable_group_reply": {"type": "bool", "default": True}}, fh)
        self.cfg_path = os.path.join(self.cfg_dir, "Demo_config.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_snapshot_restores_blank_key(self):
        config = FakeConfig(self.cfg_path, {"hefeng_key": "abc123", "weather_host": "api.example.com"})
        config_guard.heal(config, plugin_name="Demo", plugin_dir=self.plugin_dir,
                          data_dir=self.data_dir)

        cleared = FakeConfig(self.cfg_path, {"hefeng_key": "", "weather_host": ""})
        report = config_guard.heal(cleared, plugin_name="Demo", plugin_dir=self.plugin_dir,
                                   data_dir=self.data_dir)
        self.assertEqual(cleared["hefeng_key"], "abc123")
        self.assertEqual(cleared["weather_host"], "api.example.com")
        self.assertEqual(sorted(report["restored"]), ["hefeng_key", "weather_host"])
        self.assertEqual(cleared.saved, 1, "补回后必须落盘")

    def test_removed_schema_key_is_not_resurrected(self):
        """schema 删掉的键＝功能已下线，不许从快照里复活。"""
        config = FakeConfig(self.cfg_path, {"hefeng_key": "abc123",
                                            "photo_daily_cap": 4})
        config_guard.heal(config, plugin_name="Demo", plugin_dir=self.plugin_dir,
                          data_dir=self.data_dir)

        # 从 schema 里删掉它，再清空配置重启
        schema_path = os.path.join(self.plugin_dir, "_conf_schema.json")
        with open(schema_path, "w", encoding="utf-8") as fh:
            json.dump({"hefeng_key": {"type": "string", "default": ""},
                       "weather_host": {"type": "string", "default": ""}}, fh)
        cleared = FakeConfig(self.cfg_path, {"hefeng_key": ""})
        report = config_guard.heal(cleared, plugin_name="Demo", plugin_dir=self.plugin_dir,
                                   data_dir=self.data_dir)
        self.assertNotIn("photo_daily_cap", cleared, "已下线的键不许复活")
        self.assertNotIn("photo_daily_cap", report["restored"])
        self.assertEqual(cleared["hefeng_key"], "abc123", "还在 schema 里的键照旧补回")

    def test_does_not_overwrite_user_value(self):
        config = FakeConfig(self.cfg_path, {"hefeng_key": "old"})
        config_guard.heal(config, plugin_name="Demo", plugin_dir=self.plugin_dir,
                          data_dir=self.data_dir)
        changed = FakeConfig(self.cfg_path, {"hefeng_key": "new"})
        config_guard.heal(changed, plugin_name="Demo", plugin_dir=self.plugin_dir,
                          data_dir=self.data_dir)
        self.assertEqual(changed["hefeng_key"], "new", "用户改过的值不能被快照盖回去")

    def test_orphan_config_migrates_after_dir_rename(self):
        """目录改名 → 新配置文件名是空的，旧文件里的同名键要搬过来。"""
        old_path = os.path.join(self.cfg_dir, "Demo_old_config.json")
        with open(old_path, "w", encoding="utf-8") as fh:
            json.dump({"hefeng_key": "from-old", "weather_host": "old.example.com"}, fh)
        fresh = FakeConfig(self.cfg_path, {"hefeng_key": "", "weather_host": ""})
        report = config_guard.heal(fresh, plugin_name="Demo", plugin_dir=self.plugin_dir,
                                   data_dir=self.data_dir)
        self.assertEqual(fresh["hefeng_key"], "from-old")
        self.assertIn("hefeng_key", report["from_orphan"])

    def test_snapshot_is_private(self):
        config = FakeConfig(self.cfg_path, {"hefeng_key": "secret"})
        config_guard.heal(config, plugin_name="Demo", plugin_dir=self.plugin_dir,
                          data_dir=self.data_dir)
        path = config_guard.snapshot_path(self.data_dir)
        self.assertTrue(os.path.exists(path))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600, "快照里有 key，必须 600")

    def test_plain_dict_config_is_ignored(self):
        """测试替身（没有 config_path 的裸 dict）不该被自愈碰，也不该写快照。"""
        class Plain(dict):
            pass

        cfg = Plain({"hefeng_key": ""})
        report = config_guard.heal(cfg, plugin_name="Demo", plugin_dir=self.plugin_dir,
                                   data_dir=self.data_dir)
        self.assertEqual(report["restored"], [])
        self.assertFalse(os.path.exists(config_guard.snapshot_path(self.data_dir)),
                         "没有真实配置路径时不能写快照")

    def test_data_dir_is_two_levels_up(self):
        plugin_dir = os.path.join(self.root, "data", "plugins", "Demo")
        path = config_guard.fallback_data_dir(plugin_dir, "Demo")
        self.assertEqual(path, os.path.join(self.root, "data", "plugin_data", "Demo"))


if __name__ == "__main__":
    unittest.main()
