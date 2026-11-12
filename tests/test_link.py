# -*- coding: utf-8 -*-
"""连接状态探测：终端在不在、记忆库在不在，要说真话。"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import link  # noqa: E402


class Meta:
    def __init__(self, name, instance):
        self.name = name
        self.module_path = name
        self.star_cls = instance


class Terminal:
    PLUGIN_VERSION = "9.9.9"

    def __init__(self):
        self.config = {}

    def _data_bot_id(self):
        return "陪伴"


class Data:
    PLUGIN_VERSION = "2.0.0"

    def __init__(self):
        self.mem = object()


class Ctx:
    def __init__(self, stars):
        self.stars = list(stars)

    def get_registered_star(self, name):
        for meta in self.stars:
            if meta.name == name:
                return meta
        return None

    def get_all_stars(self):
        return list(self.stars)


class Holder:
    def __init__(self, ctx):
        self.context = ctx


class ProbeTests(unittest.TestCase):
    def test_both_connected(self):
        ctx = Ctx([Meta("Astrbot_Amo_forever", Terminal()),
                   Meta("Astr_Amo_data", Data())])
        info = link.probe(Holder(ctx))
        self.assertTrue(info["terminal_connected"])
        self.assertTrue(info["data_connected"])
        self.assertEqual(info["terminal_version"], "9.9.9")
        self.assertEqual(info["data_version"], "2.0.0")
        self.assertEqual(info["bot_id"], "陪伴")
        self.assertEqual(info["last_heartbeat"], 0)
        self.assertEqual(info["error"], "")

    def test_missing_terminal_is_reported(self):
        ctx = Ctx([Meta("Astr_Amo_data", Data())])
        holder = Holder(ctx)
        info = link.probe(holder)
        self.assertFalse(info["terminal_connected"])
        self.assertIn("终端", info["error"])
        self.assertEqual(info["last_heartbeat"], -1, "从没连上过")
        self.assertTrue(info["data_connected"], "记忆库仍在")
        # 第二次探测时终端补上：心跳归零
        ctx.stars.append(Meta("Astrbot_Amo_forever", Terminal()))
        info = link.probe(holder)
        self.assertTrue(info["terminal_connected"])
        self.assertEqual(info["last_heartbeat"], 0)

    def test_missing_data_is_reported(self):
        ctx = Ctx([Meta("Astrbot_Amo_forever", Terminal())])
        info = link.probe(Holder(ctx))
        self.assertTrue(info["terminal_connected"])
        self.assertFalse(info["data_connected"])
        self.assertIn("记忆库", info["error"])

    def test_never_raises_with_empty_context(self):
        info = link.probe(object())
        self.assertFalse(info["terminal_connected"])
        self.assertFalse(info["data_connected"])
        self.assertTrue(info["error"])

    def test_summary_reads_naturally(self):
        ctx = Ctx([Meta("Astrbot_Amo_forever", Terminal()),
                   Meta("Astr_Amo_data", Data())])
        text = link.summary(link.probe(Holder(ctx)))
        self.assertIn("终端 9.9.9 已连接", text)
        self.assertIn("记忆库 2.0.0 已连接", text)


if __name__ == "__main__":
    unittest.main()
