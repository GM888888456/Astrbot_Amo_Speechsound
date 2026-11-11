# -*- coding: utf-8 -*-
"""面板与发版检查：结构、四页标签、桥接解包、四处版本一致。"""

import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIEWS = os.path.join(ROOT, "views", "dashboard")


class PanelFilesTests(unittest.TestCase):
    def test_files_exist(self):
        for name in ("index.html", "app.js", "style.css"):
            path = os.path.join(VIEWS, name)
            self.assertTrue(os.path.isfile(path), name)

    def test_four_tabs(self):
        html = open(os.path.join(VIEWS, "index.html"), encoding="utf-8").read()
        tabs = re.findall(r'data-tab="([a-z]+)"', html)
        self.assertEqual(tabs, ["status", "config", "voices", "history"])

    def test_unwrap_handles_bridge_layers(self):
        """桥接会剥一层 {code,data}，面板必须两种形态都认，否则全是 undefined。"""
        js = open(os.path.join(VIEWS, "app.js"), encoding="utf-8").read()
        self.assertIn("function unwrap(", js)
        self.assertIn('"data" in value && "code" in value', js)

    def test_no_manual_voice_id_input(self):
        """音色只能拉取后选，不给手抄 voice_id 的输入框。"""
        html = open(os.path.join(VIEWS, "index.html"), encoding="utf-8").read()
        self.assertIn('id="pullVoices"', html)
        self.assertIn("<select id=\"default_voice\"", html)

    def test_feedback_group_present(self):
        html = open(os.path.join(VIEWS, "index.html"), encoding="utf-8").read()
        self.assertIn("1055679275", html)

    def test_js_syntax(self):
        js = os.path.join(VIEWS, "app.js")
        if not subprocess.run(["which", "node"], capture_output=True).returncode == 0:
            self.skipTest("没有 node，跳过 JS 语法检查")
        result = subprocess.run(["node", "--check", js], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class ReleaseTests(unittest.TestCase):
    def test_check_version_passes(self):
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "check_version.py")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_feedback_group_in_readme(self):
        readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
        self.assertIn("1055679275", readme)


if __name__ == "__main__":
    unittest.main()
