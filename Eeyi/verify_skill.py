#!/usr/bin/env python3
"""校验候选技能正文：覆盖 15 条取证结论、不含已被证伪的表述、统计 token 成本。"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import eeyi  # noqa: E402

GUIDE = HERE / "GUIDE.md"
CURRENT = HERE / ".." / ".aicode" / "skills" / "Eeyi" / "SKILL.md"
GLOBAL = Path.home() / ".aicode" / "skills" / "Eeyi" / "SKILL.md"

COVERAGE = {
    "C1 行号不可回查": ["折叠后", "不能回查"],
    "C1 错误位置需确认": ["无关路径"],
    "C2 摘要可能缺失": ["摘要不出现"],
    "C2 摘要非原文": ["含摘要行"],
    "C3 输出超预算": ["略超"],
    "C4 diff 被改写": ["会被改写", "不能当补丁"],
    "C5 长行截断": ["长行中部截断"],
    "C6 diff 行数不截断": ["不做行数截断"],
    "C7 堆栈判定偏宽": ["偏宽"],
    "C8 自定义异常落源码行": ["自定义异常"],
    "C9 位置可能无关": ["无关路径"],
    "C10 零丢失开关": ["--keep-all-errors"],
    "C11 同类归并": ["类型数"],
    "C12 严重度优先": ["fatal > error > warning"],
    "C13 json 截断": ["40 字符"],
    "C14 标记非连续": ["不是原文连续内容"],
    "C15 digest 小文件": ["150 行"],
    "估算非精确": ["±30%", "只做相对比较"],
    "工作流迭代上限": ["最多迭代 2 次"],
    "GUIDE 指路": ["GUIDE.md"],
}

FORBIDDEN = {
    "旧的错误说法：行号可回原文": "据此回原文对应行段",
    "旧的错误说法：diff 完整保留": "完整保留",
    "旧的错误说法：diff 不压缩": "不会压缩",
    "旧的非 ASCII 参数": "\u2011",
    "不可执行的省略号命令": "… digest",
    "模板变量缺失": None,
}


def main():
    text = eeyi.SKILL_TEMPLATE
    body = text.split("---", 2)[-1]

    print("== 覆盖检查 ==")
    missing = []
    for label, needles in COVERAGE.items():
        absent = [needle for needle in needles if needle not in body]
        state = "PASS" if not absent else "FAIL"
        if absent:
            missing.append(label)
        print(f"{state}  {label}" + (f"  缺: {absent}" if absent else ""))

    print("\n== 禁止表述 ==")
    offenders = []
    for label, needle in FORBIDDEN.items():
        if needle is None:
            hit = "{{EEYI}}" not in body
        else:
            hit = needle in body
        if hit:
            offenders.append(label)
        print(f"{'FAIL' if hit else 'PASS'}  {label}")

    print("\n== 成本 ==")
    candidate_tokens = eeyi.count_tokens(text)
    print(f"安装源正文: {len(text.splitlines())} 行 / ~{candidate_tokens} token")
    for label, path in (("项目级副本", CURRENT), ("全局副本", GLOBAL)):
        if path.is_file():
            installed = path.read_text(encoding="utf-8")
            normalized = installed.replace(str(path.parent / "eeyi.py"), "{{EEYI}}")
            current_tokens = eeyi.count_tokens(normalized)
            delta = candidate_tokens - current_tokens
            print(f"{label}: ~{current_tokens} token，候选 {delta:+d}")

    ok = not missing and not offenders
    print("\n== GUIDE 检查 ==")
    if GUIDE.is_file():
        guide = GUIDE.read_text(encoding="utf-8")
        guide_checks = {
            "无 U+2011": "\u2011" not in guide,
            "无『据此回原文』": "据此回原文" not in guide,
            "含 C1-C17 取证表": all(f"C{i}" in guide for i in range(1, 18)),
            "含迭代上限": "最多迭代 2 次" in guide,
            "含『不要调 max-lines 修堆栈』": "不要靠调大" in guide,
        }
        for label, passed in guide_checks.items():
            print(f"{'PASS' if passed else 'FAIL'}  {label}")
            if not passed:
                offenders.append(label)
        print(f"GUIDE 体积: {len(guide.splitlines())} 行 / ~{eeyi.count_tokens(guide)} token（按需读，不常驻）")
        ok = ok and all(guide_checks.values())
    else:
        print("FAIL  GUIDE.md 不存在")
        ok = False
    print("\n== 渲染模拟 ==")
    rendered = body.replace("{{EEYI}}", "/opt/eeyi/eeyi.py")
    render_ok = "{{EEYI}}" not in rendered and "/opt/eeyi/eeyi.py" in rendered
    front_ok = "name: Eeyi" in text and "description:" in text
    print(f"{'PASS' if render_ok else 'FAIL'}  模板变量可替换且无残留")
    print(f"{'PASS' if front_ok else 'FAIL'}  frontmatter 含 name/description")
    ok = ok and render_ok and front_ok
    print("\n结论:", "候选稿可用" if ok else "候选稿需修")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
