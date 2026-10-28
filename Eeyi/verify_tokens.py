#!/usr/bin/env python3
"""用真实分词器校准 count_tokens 的估算误差。

需要 tiktoken（可选，不随 Eeyi 分发）：
    python3 -m venv /tmp/tkenv && /tmp/tkenv/bin/pip install tiktoken
    /tmp/tkenv/bin/python verify_tokens.py
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import eeyi  # noqa: E402

try:
    import tiktoken
except ImportError:
    print("跳过：未安装 tiktoken。安装方式见本文件顶部。")
    sys.exit(0)

CL = tiktoken.get_encoding("cl100k_base")
O2 = tiktoken.get_encoding("o200k_base")

WS = Path.home() / ".aicode" / "skills" / "Eeyi" / "SKILL.md"
PROJECT_WS = HERE.parent / ".aicode" / "skills" / "Eeyi" / "SKILL.md"
GL = WS
CAND = None
CORPUS = HERE.parent / "dsh-ops"


def installed_skill():
    for path in (PROJECT_WS, WS):
        if path.is_file():
            return path
    return None


PATH_PLACEHOLDER = "{{EEYI}}"


def samples():
    items = {
        "构建日志原文": eeyi._fixture_build_log(),
        "构建日志压缩后": eeyi.trim_text(eeyi._fixture_build_log()),
        "测试输出压缩后": eeyi.trim_text(eeyi._fixture_test_output()),
        "海量错误压缩后": eeyi.trim_text(eeyi._fixture_error_storm()),
        "中文说明段落": "压缩进入上下文的工具输出与代码阅读量，当命令输出很长时使用，避免把整份日志塞进上下文。",
        "纯英文段落": "Compress tool output and code reading before it enters the context window, so long logs stay affordable.",
    }
    if CAND is not None and CAND.is_file():
        items["候选正文（同路径）"] = CAND.read_text(encoding="utf-8").replace(PATH_PLACEHOLDER, "PATH")
    skill = installed_skill()
    anchor = str(skill.parent / "eeyi.py") if skill else "PATH"
    items["安装源正文（同路径）"] = eeyi.SKILL_TEMPLATE.replace(PATH_PLACEHOLDER, anchor)
    for label, path in (("技能正文（项目级）", PROJECT_WS), ("技能正文（全局）", WS)):
        if path.is_file():
            items[label] = path.read_text(encoding="utf-8")
    ts = CORPUS / "src" / "panel" / "collect.ts"
    if ts.is_file():
        items["真实 TS 代码 100 行"] = "\n".join(ts.read_text(encoding="utf-8").splitlines()[:100])
    lockfile = CORPUS / "package-lock.json"
    if lockfile.is_file():
        items["真实 lockfile 摘要"] = eeyi.json_summary(json.loads(lockfile.read_text(encoding="utf-8")))
    return items


def main():
    print(f"{'样本':<24}{'估算':>7}{'cl100k':>8}{'o200k':>8}{'误差/ cl100k':>14}")
    print("-" * 64)
    errors = []
    for name, text in samples().items():
        est = eeyi.count_tokens(text)
        a, b = len(CL.encode(text)), len(O2.encode(text))
        errors.append(abs(est - a) / a if a else 0)
        print(f"{name:<24}{est:>7}{a:>8}{b:>8}{(est - a) / a * 100:>13.0f}%")
    print("-" * 64)
    print(f"平均绝对误差（对 cl100k）: {sum(errors) / len(errors) * 100:.0f}%")

    skill = installed_skill()
    if skill is not None:
        installed = skill.read_text(encoding="utf-8")
        candidate = eeyi.SKILL_TEMPLATE.replace(PATH_PLACEHOLDER, str(skill.parent / "eeyi.py"))
        print("\n安装源相对已装副本（替换前 -> 替换后）:")
        for label, fn in (("估算", eeyi.count_tokens), ("cl100k", lambda t: len(CL.encode(t))), ("o200k", lambda t: len(O2.encode(t)))):
            old, new = fn(installed), fn(candidate)
            print(f"  {label:<7} {old:>5} -> {new:>5}，省 {old - new}（{(old - new) / old * 100:.0f}%）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
