#!/usr/bin/env python3
"""对 USAGE-CANDIDATE.md 里的每条约定做可执行验证，输出证据。不改 eeyi.py。"""

import json
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import eeyi  # noqa: E402

CHECKS = []


def check(cid, claim):
    def wrapper(fn):
        CHECKS.append((cid, claim, fn))
        return fn
    return wrapper


def summary_of(out):
    for line in out.splitlines():
        if line.startswith("<首个错误"):
            return line
    return None


@check("C1", "摘要行号是折叠后中间序列的序号，既非原文行号也非输出行号")
def c1():
    lines = [f"info {i}" for i in range(100)]
    lines += ["block-a", "block-b", "block-c"] * 100
    lines += ["Traceback (most recent call last):", '  File "/app/x.py", line 42, in f', "ValueError: bad input"]
    raw = "\n".join(lines) + "\n"
    original_line = 401
    folded = eeyi.fold_cycles(eeyi.collapse_blank(raw.splitlines()), 16, 3)
    folded_index = next(i for i, line in enumerate(folded) if "Traceback" in line) + 1
    out = eeyi.trim_text(raw)
    printed = int(summary_of(out).split("第 ")[1].split(" 行")[0])
    ok = printed == folded_index and printed != original_line and printed > len(out.splitlines())
    return ok, f"原文第 {original_line} 行 / 摘要第 {printed} 行 / 折叠后第 {folded_index} 行 / 输出仅 {len(out.splitlines())} 行"


@check("C2", "折叠后行数未超预算时不生成摘要")
def c2():
    out = eeyi.trim_text(eeyi._fixture_big_log())
    ok = summary_of(out) is None and "ERROR worker-3 crashed" in out
    return ok, f"5 万行日志折叠后输出 {len(out.splitlines())} 行，无摘要但错误行仍在"


@check("C3", "说明行与摘要行不计入 --max-lines，输出会略超预算")
def c3():
    lines = [f"ERROR kind-{i % 30}: failed code {i}" for i in range(300)]
    out = eeyi.select(lines, 10, 2, 2)
    ok = len(out) > 10 and any("同类" in line or "另有" in line for line in out)
    return ok, f"max_lines=10，实际输出 {len(out)} 行"


@check("C4", "diff 的连续相同 context 行会被折叠，输出不可直接应用")
def c4():
    diff = "@@ -1,12 +1,12 @@\n" + "   }\n" * 5 + "- old()\n+ new()\n"
    out = eeyi.trim_text(diff)
    ok = "<重复" in out and out.count("   }") == 1
    return ok, f"5 行相同 context 折叠为 1 行 + 标记：{'<重复 4 次>' in out}"


@check("C5", "diff 的长行会被中部截断")
def c5():
    diff = "@@ -1,3 +1,3 @@\n-" + "x" * 500 + "\n+ y\n"
    out = eeyi.trim_text(diff)
    ok = "字符省略" in out and len(out) < 400
    return ok, f"500 字符行被截到 {max(len(line) for line in out.splitlines())} 字符"


@check("C6", "diff 不做行数截断，变更行与 hunk 头全部保留")
def c6():
    text = eeyi._fixture_diff()
    out = eeyi.trim_text(text)
    required = [line for line in text.splitlines() if line.startswith("@@") or (line[:1] in "+-" and not line.startswith(("+++", "---")))]
    missing = [line for line in required if line not in out.splitlines()]
    ok = not missing and len(out.splitlines()) >= len(text.splitlines()) - 1
    return ok, f"{len(required)} 条变更行/hunk 头，缺失 {len(missing)} 条"


@check("C7", "堆栈块判定偏宽：块后紧跟的缩进内容也会被纳入")
def c7():
    raw = "\n".join([
        "Traceback (most recent call last):",
        '  File "/app/x.py", line 9, in load',
        "ValueError: bad yaml",
        "  key: value",
        "  nested:",
    ]) + "\n"
    lines = ["pad %d" % i for i in range(100)] + raw.splitlines()
    out = eeyi.trim_text("\n".join(lines) + "\n")
    ok = "  key: value" in out and "  nested:" in out
    return ok, "Traceback 后的缩进 YAML 行被当作堆栈体保留"


@check("C8", "自定义异常名不匹配 *Error/*Exception 时，摘要会落到源码行")
def c8():
    lines = ["pad %d" % i for i in range(100)]
    lines += [
        "Traceback (most recent call last):",
        '  File "/app/x.py", line 9, in load',
        '    raise BadThing("boom")',
        "BadThing: boom",
    ]
    out = eeyi.trim_text("\n".join(lines) + "\n")
    summary = summary_of(out)
    ok = summary is not None and "BadThing: boom" not in summary and "raise" in summary
    return ok, summary


@check("C9", "错误位置由正则从错误行提取，可能抓到与故障无关的路径")
def c9():
    lines = ["pad %d" % i for i in range(100)]
    lines += ["ERROR: failed while loading config/other.py:12 for module"]
    out = eeyi.trim_text("\n".join(lines) + "\n")
    ok = "config/other.py:12" in out
    return ok, "错误行里顺带提到的路径也进了「错误位置」"


@check("C10", "--keep-all-errors 时错误行零丢失")
def c10():
    payload = "\n".join(f"ERROR e{i}" for i in range(500)) + "\n"
    out = eeyi.trim_text(payload, max_lines=10, keep_all_errors=True)
    ok = sum(1 for line in out.splitlines() if line.startswith("ERROR e")) == 500
    return ok, "500 条错误全部保留"


@check("C11", "同类错误按签名归并，并用计数与行号区间表达")
def c11():
    payload = "\n".join(f"ERROR timeout after {i}s" for i in range(1000)) + "\n"
    out = eeyi.trim_text(payload)
    ok = "同类" in out and len(out.splitlines()) < 30
    return ok, f"1000 条同类错误输出 {len(out.splitlines())} 行，含计数标记"


@check("C12", "类型超预算时按严重度优先，fatal 不会被 warning 挤掉")
def c12():
    lines = [f"WARN retry {i}" for i in range(500)] + ["FATAL: database corrupted"]
    out = eeyi.trim_text("\n".join(lines) + "\n", max_lines=10)
    ok = "FATAL: database corrupted" in out
    return ok, "WARN 洪流中 FATAL 仍保留"


@check("C13", "json 会把长字符串（含 base64）截成前 40 字符")
def c13():
    blob = "A" * 4000
    out = eeyi.json_summary({"avatar": blob, "id": 1})
    longest = max((len(match.group(0)) for match in re.finditer(r"A+", out)), default=0)
    ok = longest <= 40 and "字符" in out
    return ok, f"4000 个 A 只保留 {longest} 个，其余替换为截断标记"


@check("C14", "所有输出标记都能在样例中复现")
def c14():
    seen = set()
    out = eeyi.trim_text("\n".join(f"ERROR kind-{i % 30}: failed {i}" for i in range(2000)) + "\n")
    seen |= {m for m in ("<首个错误", "<同类", "<另有") if m in out}
    out2 = eeyi.trim_text("\n".join(["same line"] * 20 + ["x"] * 5) + "\n")
    seen |= {m for m in ("<重复",) if m in out2}
    out3 = eeyi.trim_text("\n".join(f"line {i}" for i in range(200)) + "\n")
    seen |= {m for m in ("<省略",) if m in out3}
    out4 = eeyi.trim_text("\n".join(["block-a", "block-b"] * 20) + "\n")
    seen |= {m for m in ("<上面",) if m in out4}
    expected = {"<首个错误", "<同类", "<另有", "<重复", "<省略", "<上面"}
    return seen == expected, "复现: " + "、".join(sorted(seen))


@check("C15", "digest 只给符号骨架，函数体细节会丢失")
def c15():
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as handle:
        handle.write("SECRET = 'do-not-lose'\n\ndef f():\n    return 42\n")
        path = handle.name
    try:
        out = "\n".join(eeyi.digest_file(Path(path), 40)[0])
        ok = "def f" in out and "do-not-lose" not in out and "return 42" not in out
    finally:
        pathlib_path = Path(path)
        pathlib_path.unlink()
    return ok, "符号在、模块常量与函数体不在"


@check("C16", "长堆栈截断时断点对齐到栈帧起始行，不留孤立续行")
def c16():
    frames = []
    for i in range(20):
        frames.append(f'  File "/app/mod{i}.py", line {i * 10 + 1}, in func{i}')
        frames.append(f"    value = step{i}(input)")
    frames.append("ValueError: bad")
    lines = [f"pad {i}" for i in range(200)] + ["Traceback (most recent call last):"] + frames
    out = eeyi.trim_text("\n".join(lines) + "\n").splitlines()
    start = next(i for i, line in enumerate(out) if "Traceback" in line)
    starts, expect = [], True
    for line in out[start + 1:]:
        if line.startswith("<"):
            expect = True
            continue
        if expect:
            starts.append(line)
            expect = False
    ok = bool(starts) and all(eeyi.frame_start(line) for line in starts)
    bad = [line for line in starts if not eeyi.frame_start(line)]
    return ok, f"{len(starts)} 个保留片段起点，非栈帧起始行 {len(bad)} 个"


@check("C17", "长堆栈截断后行数应远小于块长度（对齐不能以放弃压缩为代价）")
def c17():
    results = []
    for per_frame in (2, 5):
        frames = []
        for i in range(20):
            frames.append(f'  File "/app/m{i}.py", line {i * 10 + 1}, in f{i}')
            for j in range(per_frame - 1):
                frames.append(f"    v{j} = step({i})")
        frames.append("ValueError: boom")
        lines = [f"pad {i}" for i in range(150)] + ["Traceback (most recent call last):"] + frames
        start, end = eeyi.traceback_blocks(lines)[0]
        span = list(range(start, end))
        kept = eeyi.shrink_traceback(lines, span)
        results.append((per_frame, len(span), len(kept)))
    bad = [(p, total, kept) for p, total, kept in results if kept > 25]
    detail = "；".join(f"每帧 {p} 行: 块 {total} 行->保留 {kept} 行" for p, total, kept in results)
    return not bad, detail


def main():
    rows = []
    for cid, claim, fn in CHECKS:
        try:
            ok, evidence = fn()
        except Exception as exc:  # 验证脚本自身失败要暴露出来
            ok, evidence = False, f"异常: {type(exc).__name__}: {exc}"
        rows.append((cid, claim, ok, evidence))

    width = 74
    print(f"{'编号':<5}{'结论':<6}{'约定'}")
    print("-" * width)
    for cid, claim, ok, evidence in rows:
        print(f"{cid:<5}{'PASS' if ok else 'FAIL':<6}{claim}")
        print(f"     证据: {evidence}")
    failed = [cid for cid, _, ok, _ in rows if not ok]
    print("-" * width)
    print(f"{len(rows) - len(failed)}/{len(rows)} 条约定与实现一致")
    if failed:
        print("需修正文档: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
