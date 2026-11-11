#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发版校验：metadata.yaml / CHANGELOG.md / README.md / main.py 四处版本号必须一致。

版本号规则（x.y.z）：
- 第二位、第三位只能是 0-9，**逢十进一**：3.8.9 → 3.9.0；3.9.9 → 4.0.0；
- 首位不做限制，可以是多位数：9.9.9 再进一位就是 10.0.0（不是 1.0.0.0）；
- 测试版：动第三位；发布版：动第二位、第三位归零；大版本：首位 +1。

用法：
    python tools/check_version.py          # 校验
    python tools/check_version.py --next test      # 打印下一个测试版号
    python tools/check_version.py --next release   # 打印下一个发布版号
    python tools/check_version.py --next major     # 打印下一个大版本号
退出码 0 = 通过，1 = 有问题（会打印原因）。
"""

from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(name):
    path = os.path.join(ROOT, name)
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        print("缺少文件: %s" % name)
        return ""


def metadata_version():
    text = read("metadata.yaml")
    match = re.search(r"^version:\s*([0-9]+\.[0-9]+\.[0-9]+)\s*$", text, re.M)
    return match.group(1) if match else ""


def changelog_version():
    text = read("CHANGELOG.md")
    match = re.search(r"^##\s*([0-9]+\.[0-9]+\.[0-9]+)", text, re.M)
    return match.group(1) if match else ""


def readme_version():
    text = read("README.md")
    match = re.search(r"^##\s*([0-9]+\.[0-9]+\.[0-9]+)", text, re.M)
    return match.group(1) if match else ""


def code_version():
    text = read("main.py")
    match = re.search(r'PLUGIN_VERSION\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"', text)
    return match.group(1) if match else ""


def next_version(version, kind="test"):
    """按规则进位：第三位 0-9 逢十进一，第二位同理；首位不限、可多位数。"""
    major, minor, patch = (int(part) for part in version.split("."))
    if kind in ("major", "大版本"):
        return "%d.0.0" % (major + 1)
    if kind in ("release", "发布", "minor"):
        minor += 1
        patch = 0
    else:
        patch += 1
        if patch > 9:
            patch = 0
            minor += 1
    if minor > 9:
        minor = 0
        major += 1
    return "%d.%d.%d" % (major, minor, patch)


def check_rules(version):
    """校验格式与进位规则；升位规则见 README「版本号规则」。"""
    parts = version.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return ["版本号应该是 x.y.z（三段数字）"]
    major, minor, patch = parts
    problems = []
    if len(minor) != 1:
        problems.append("第二位只能是 0-9（逢十进一：3.8.9 → 3.9.0）")
    if len(patch) != 1:
        problems.append("第三位只能是 0-9（逢十进一：3.8.9 → 3.9.0）")
    if major != str(int(major)):
        problems.append("首位不能有前导零")
    return problems


REPO_OWNER = "GM888888456"


def repo_problem():
    """仓库地址只认主仓库的规范写法：https://github.com/<owner>/<repo>/"""
    text = read("metadata.yaml")
    match = re.search(r"^repo:\s*(\S+)\s*$", text, re.M)
    if not match:
        return "metadata.yaml 缺少 repo 字段"
    url = match.group(1)
    pattern = r"^https://github\.com/%s/([A-Za-z0-9_.-]+)/$" % REPO_OWNER
    parsed = re.match(pattern, url)
    if not parsed:
        return ("repo 必须写成 https://github.com/%s/<仓库名>/（结尾带斜杠、指向主仓库），现在是 %s"
                % (REPO_OWNER, url))
    name = parsed.group(1)
    if name.endswith("_ui") or name.lower().endswith("_test"):
        return "repo 指向了测试仓库 %s，必须指向主仓库" % name
    return ""


def current_version():
    versions = {
        "metadata.yaml": metadata_version(),
        "CHANGELOG.md": changelog_version(),
        "README.md": readme_version(),
        "main.py": code_version(),
    }
    return versions


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] == "--next":
        kind = argv[1] if len(argv) > 1 else "test"
        versions = current_version()
        values = {value for value in versions.values() if value}
        if len(values) != 1:
            print("版本号不一致，先修一致再算下一个：")
            for name, value in versions.items():
                print("  %-16s %s" % (name, value))
            return 1
        print(next_version(values.pop(), kind))
        return 0

    versions = current_version()
    missing = [name for name, value in versions.items() if not value]
    if missing:
        print("读不到版本号: %s" % "、".join(missing))
        return 1
    distinct = set(versions.values())
    if len(distinct) != 1:
        print("版本号不一致：")
        for name, value in versions.items():
            print("  %-16s %s" % (name, value))
        return 1
    version = distinct.pop()
    problems = check_rules(version)
    if problems:
        print("版本号 %s 不符合仓库规则：" % version)
        for item in problems:
            print("  - " + item)
        return 1
    repo_issue = repo_problem()
    if repo_issue:
        print(repo_issue)
        return 1
    print("版本号校验通过：%s" % version)
    print("仓库地址规范：通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
