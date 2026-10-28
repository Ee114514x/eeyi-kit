#!/usr/bin/env python3
"""EeyiRecall: reversible compression for Eeyi. Every kept line carries its original
line number, every omission names the original line range, so any summary can be turned
back into the exact source lines with `fetch`.

安装: python3 recall.py install [--scope <技能目录>]
自检: python3 recall.py selftest
"""

import argparse
import hashlib
import json
import math
import re
import shutil
import sys
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
KEY = re.compile(
    r"(?:error|errors|exception|traceback|failed|failure|fatal|panic|assert\w*|"
    r"warning|denied|refused|timed out|timeout|exit code|no such file|not found|"
    r"错误|失败|异常|报错|不通过|未通过|断言)",
    re.IGNORECASE,
)

SKILL_TEMPLATE = '''---
name: EeyiRecall
description: Eeyi 系列的可逆压缩工具。保留行带原文行号、省略处标原文区间，需要时按区间精确回读。当要看长日志、构建输出或大文件却不想整份进上下文时使用。
---

# EeyiRecall

`python3 "{{RECALL}}"`（零依赖，py3.8+，Windows 用 `python`）。stdout 是内容，stderr 是统计。

- 压缩：`<命令> 2>&1 | python3 "{{RECALL}}" trim`
- 按区间回读：`python3 "{{RECALL}}" fetch <文件> 401-430`
- 区间清单：`python3 "{{RECALL}}" map <文件>`（JSON）
- 批量紧凑读：`python3 "{{RECALL}}" batch a.py b.py`

## 必须知道

- 输出每行格式 `行号| 内容`，行号是**原文行号**，可直接拿去 `fetch`。
- `<省略 13-400 行>`、`<第 14-16 行与第 13 行相同>` 都是**原文区间**，不是相对量。
- 长行被截断时仍保留该行号，`fetch` 该行即可拿回完整内容。
- 回读前先 `check` 核对 sha256；文件变了，行号可能已失配。
- `trim` 仍是摘要：改代码、改配置必须 `fetch` 原文，不要用摘要当补丁。
- 与 Eeyi 分工：Eeyi 判断什么重要并给出定位摘要，Recall 负责可逆，两者可串联。
'''


def count_tokens(text):
    cjk = len(CJK.findall(text))
    ws = len(re.findall(r"\s", text))
    rest = len(text) - cjk - ws
    return math.ceil(cjk * 1.2 + rest * 0.3 + ws * 0.1)


def clip_line(line, max_col):
    if max_col < 4:
        raise ValueError("max_col 至少为 4")
    if len(line) <= max_col:
        return line
    keep = max_col // 2
    return f"{line[:keep]}…<省略 {len(line) - keep * 2} 字符>…{line[-keep:]}"


def repeats(lines, min_repeat=3, max_period=16):
    """返回 [(start, end, period)]，end 不含。只记录逐字相同的连续重复。"""
    found = []
    i = 0
    total = len(lines)
    while i < total:
        best = None
        for period in range(1, max_period + 1):
            if i + period * min_repeat > total:
                break
            block = lines[i:i + period]
            repeats_count = 1
            while i + (repeats_count + 1) * period <= total and lines[
                i + repeats_count * period:i + (repeats_count + 1) * period
            ] == block:
                repeats_count += 1
            if repeats_count >= min_repeat and (best is None or repeats_count * period > best[0] * best[1]):
                best = (repeats_count, period)
        if best is None:
            i += 1
            continue
        repeats_count, period = best
        found.append((i, i + repeats_count * period, period))
        i += repeats_count * period
    return found


def build(lines, max_lines=80, head=12, tail=12, max_col=300, min_repeat=3):
    """返回 (输出行列表, 保留区间, 省略区间)。行号从 1 开始。"""
    clipped = [clip_line(ANSI.sub("", line), max_col) for line in lines]
    total = len(clipped)
    if total == 0:
        return [], [], []

    runs = repeats(clipped, min_repeat)
    run_start = {start: (start, end, period) for start, end, period in runs}
    folded_rows = {row for start, end, period in runs for row in range(start + 1, end)}

    if head + tail > max_lines:
        scale = head + tail
        head = max_lines * head // scale if scale else 0
        tail = max_lines - head if scale else 0

    keep = set(range(min(head, total))) | set(range(max(0, total - tail), total))
    keyed = [i for i, line in enumerate(clipped) if KEY.search(line) and i not in folded_rows]
    budget = max(max_lines - len(keep), 1)
    keep |= set(keyed[:budget])

    out = []
    kept_ranges = []
    omitted_ranges = []
    i = 0
    while i < total:
        if i not in keep:
            j = i
            while j < total and j not in keep:
                j += 1
            omitted_ranges.append([i + 1, j])
            out.append(f"<省略 {i + 1}-{j} 行>")
            i = j
            continue

        out.append(f"{i + 1}| {clipped[i]}")
        kept_ranges.append(i + 1)

        run = run_start.get(i)
        if run:
            start, end, period = run
            if end - start > period:
                out.append(f"<第 {start + 1 + period}-{end} 行与第 {start + 1}-{start + period} 行相同>")
                omitted_ranges.append([start + 1 + period, end])
            i = end
            continue
        i += 1

    return out, _collapse(kept_ranges), omitted_ranges


def _collapse(numbers):
    ranges = []
    for number in numbers:
        if ranges and number == ranges[-1][1] + 1:
            ranges[-1][1] = number
        else:
            ranges.append([number, number])
    return ranges


def read_text(path):
    if path is None or path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8", errors="replace")


def fingerprint(path):
    data = Path(path).read_bytes()
    return hashlib.sha256(data).hexdigest()[:12], data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1)


def cmd_trim(args):
    raw = read_text(args.file)
    out, kept, omitted = build(
        raw.splitlines(), args.max_lines, args.head, args.tail, args.max_col, args.min_repeat
    )
    text = "\n".join(out)
    sys.stdout.write(text + ("\n" if text else ""))
    before, after = count_tokens(raw), count_tokens(text)
    saved = 0 if before == 0 else round(100 * (1 - after / before))
    sys.stderr.write(f"recall trim: {len(raw.splitlines())}→{len(out)} 行, ~{before}→{after} token ({saved:+d}%)\n")
    if omitted:
        sys.stderr.write(f"recall: 可回读区间 " + ", ".join(f"{a}-{b}" for a, b in omitted[:10]) + "\n")
    return 0


def parse_range(spec):
    if "-" in spec:
        start, end = spec.split("-", 1)
        return int(start), int(end)
    value = int(spec)
    return value, value


def cmd_fetch(args):
    path = Path(args.file)
    if not path.is_file():
        sys.stderr.write(f"recall fetch: 不是可读文件: {args.file}\n")
        return 2
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    start, end = parse_range(args.range)
    if start < 1 or end < start:
        sys.stderr.write(f"recall fetch: 区间无效: {args.range}\n")
        return 2
    if start > len(lines):
        sys.stderr.write(f"recall fetch: 起始行超出文件（共 {len(lines)} 行）\n")
        return 2
    end = min(end, len(lines))
    for number in range(start, end + 1):
        sys.stdout.write(f"{number}| {lines[number - 1]}\n")
    sys.stderr.write(f"recall fetch: {start}-{end} 行，共 {end - start + 1} 行\n")
    return 0


def cmd_map(args):
    raw = read_text(args.file)
    lines = raw.splitlines()
    out, kept, omitted = build(
        lines, args.max_lines, args.head, args.tail, args.max_col, args.min_repeat
    )
    payload = {
        "lines": len(lines),
        "kept": _collapse([int(line.split("|", 1)[0]) for line in out if "| " in line and line.split("|", 1)[0].isdigit()]),
        "omitted": omitted,
        "tokens": {"before": count_tokens(raw), "after": count_tokens("\n".join(out))},
    }
    if args.file not in (None, "-"):
        sha, _ = fingerprint(args.file)
        payload["sha256"] = sha
        payload["file"] = str(Path(args.file).resolve())
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return 0


def cmd_batch(args):
    for target in args.files:
        path = Path(target)
        if not path.is_file():
            print(f"## {target}  (无法读取)")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        out, _, omitted = build(text.splitlines(), args.max_lines, args.head, args.tail, args.max_col)
        print(f"## {target}  [{len(text.splitlines())} 行，可回读 {len(omitted)} 段]")
        print("\n".join(out))
        print()
    return 0


def cmd_check(args):
    path = Path(args.file)
    if not path.is_file():
        sys.stderr.write(f"recall check: 不是可读文件: {args.file}\n")
        return 2
    sha, lines = fingerprint(path)
    print(f"{sha}  {lines} 行  {path.name}")
    return 0


def default_scope(source):
    for parent in [Path.cwd(), *Path.cwd().parents]:
        if (parent / ".aicode").is_dir():
            return parent / ".aicode" / "skills"
    return Path.home() / ".aicode" / "skills"


def cmd_install(args):
    source = Path(__file__).resolve()
    scope = Path(args.scope).expanduser().resolve() if args.scope else default_scope(source)
    dest = scope / "EeyiRecall"
    dest.mkdir(parents=True, exist_ok=True)
    script = dest / "recall.py"
    if script != source:
        shutil.copyfile(source, script)
    body = SKILL_TEMPLATE.replace("{{RECALL}}", str(script))
    (dest / "SKILL.md").write_text(body, encoding="utf-8")
    print(f"已安装技能: {dest}")
    print(f"  技能正文: {dest / 'SKILL.md'}")
    print(f"  伴随脚本: {script}")
    print("新会话生效；自检: python3 上面脚本 selftest")
    return 0


def cmd_selftest(args):
    import subprocess
    import tempfile
    import unittest

    def run_cli(argv, text=""):
        return subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), *argv],
            input=text, capture_output=True, text=True, timeout=120,
        )

    class CoreTest(unittest.TestCase):
        def test_empty(self):
            self.assertEqual(build([]), ([], [], []))

        def test_kept_lines_carry_original_numbers(self):
            lines = [f"line {i}" for i in range(100)]
            out, kept, _ = build(lines, max_lines=10, head=2, tail=2)
            numbered = [int(line.split("|", 1)[0]) for line in out if "| " in line]
            self.assertEqual(numbered[0], 1)
            self.assertEqual(numbered[1], 2)
            self.assertEqual(numbered[-1], 100)
            self.assertEqual(numbered[-2], 99)

        def test_omitted_range_is_absolute(self):
            lines = [f"line {i}" for i in range(100)]
            out, _, omitted = build(lines, max_lines=10, head=2, tail=2)
            text = "\n".join(out)
            self.assertIn("<省略 3-98 行>", text)
            self.assertEqual(omitted[0], [3, 98])

        def test_repeat_marks_range(self):
            lines = ["same"] * 10 + ["tail"]
            out, _, _ = build(lines, max_lines=40, head=2, tail=2)
            text = "\n".join(out)
            self.assertIn("1| same", text)
            self.assertIn("行与第 1-1 行相同", text)

        def test_long_line_keeps_line_number(self):
            lines = ["x" * 600]
            out, _, _ = build(lines, max_lines=10, max_col=40)
            self.assertTrue(out[0].startswith("1| "))
            self.assertIn("省略", out[0])

        def test_error_line_kept_beyond_window(self):
            lines = [f"noise {i}" for i in range(200)]
            lines[150] = "ERROR: exploded"
            out, _, _ = build(lines, max_lines=20, head=2, tail=2)
            self.assertTrue(any("exploded" in line for line in out))

        def test_kept_and_omitted_are_complementary(self):
            lines = [f"line {i}" for i in range(50)]
            out, kept, omitted = build(lines, max_lines=10, head=2, tail=2)
            covered = {n for a, b in kept for n in range(a, b + 1)}
            covered |= {n for a, b in omitted for n in range(a, b + 1)}
            self.assertEqual(covered, set(range(1, 51)))

    class CliTest(unittest.TestCase):
        def test_fetch_roundtrip(self):
            with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False, encoding="utf-8") as handle:
                handle.write("\n".join(f"line {i}" for i in range(1, 201)) + "\n")
                path = handle.name
            try:
                proc = run_cli(["fetch", path, "42-44"])
                self.assertEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout.splitlines(), ["42| line 42", "43| line 43", "44| line 44"])
            finally:
                Path(path).unlink()

        def test_fetch_invalid_range(self):
            with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as handle:
                handle.write("a\nb\n")
                path = handle.name
            try:
                self.assertEqual(run_cli(["fetch", path, "5-9"]).returncode, 2)
                self.assertEqual(run_cli(["fetch", path, "0-2"]).returncode, 2)
            finally:
                Path(path).unlink()

        def test_map_is_json_and_covers_all_lines(self):
            with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as handle:
                handle.write("\n".join(f"line {i}" for i in range(1, 101)) + "\n")
                path = handle.name
            try:
                proc = run_cli(["map", path, "--max-lines", "10"])
                payload = json.loads(proc.stdout)
                covered = {n for a, b in payload["kept"] for n in range(a, b + 1)}
                covered |= {n for a, b in payload["omitted"] for n in range(a, b + 1)}
                self.assertEqual(covered, set(range(1, 101)))
                self.assertIn("sha256", payload)
            finally:
                Path(path).unlink()

        def test_check_reports_hash(self):
            with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as handle:
                handle.write("x\ny\n")
                path = handle.name
            try:
                proc = run_cli(["check", path])
                self.assertEqual(proc.returncode, 0)
                self.assertRegex(proc.stdout, r"^[0-9a-f]{12}\s+2 行")
            finally:
                Path(path).unlink()

        def test_trim_stdin_and_stderr_split(self):
            proc = run_cli(["trim"], "\n".join(f"line {i}" for i in range(100)) + "\n")
            self.assertEqual(proc.returncode, 0)
            self.assertIn("| ", proc.stdout)
            self.assertIn("recall trim", proc.stderr)

        def test_missing_file(self):
            self.assertEqual(run_cli(["trim", "/nonexistent/x.log"]).returncode, 2)

        def test_batch_marks_each_file(self):
            with tempfile.TemporaryDirectory() as folder:
                Path(folder, "a.txt").write_text("a1\na2\n", encoding="utf-8")
                Path(folder, "b.txt").write_text("b1\n", encoding="utf-8")
                proc = run_cli(["batch", str(Path(folder, "a.txt")), str(Path(folder, "b.txt"))])
                self.assertIn("## ", proc.stdout)
                self.assertIn("a1", proc.stdout)
                self.assertIn("b1", proc.stdout)

    class InstallTest(unittest.TestCase):
        def test_install_creates_skill_and_script(self):
            with tempfile.TemporaryDirectory() as scope:
                proc = run_cli(["install", "--scope", scope])
                self.assertEqual(proc.returncode, 0)
                skill = Path(scope, "EeyiRecall", "SKILL.md")
                script = Path(scope, "EeyiRecall", "recall.py")
                self.assertTrue(skill.is_file() and script.is_file())
                body = skill.read_text(encoding="utf-8")
                self.assertIn(str(script), body)
                self.assertNotIn("{{RECALL}}", body)

    suite = unittest.TestSuite()
    loader = unittest.TestLoader()
    for case in (CoreTest, CliTest, InstallTest):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="recall", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("trim", help="可逆压缩：保留行带原文行号，省略处标原文区间")
    p.add_argument("file", nargs="?")
    p.add_argument("--max-lines", type=int, default=80)
    p.add_argument("--head", type=int, default=12)
    p.add_argument("--tail", type=int, default=12)
    p.add_argument("--max-col", type=int, default=300)
    p.add_argument("--min-repeat", type=int, default=3)
    p.set_defaults(func=cmd_trim)

    p = sub.add_parser("fetch", help="按原文区间回读")
    p.add_argument("file")
    p.add_argument("range")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("map", help="输出保留/省略区间清单（JSON）")
    p.add_argument("file", nargs="?")
    p.add_argument("--max-lines", type=int, default=80)
    p.add_argument("--head", type=int, default=12)
    p.add_argument("--tail", type=int, default=12)
    p.add_argument("--max-col", type=int, default=300)
    p.add_argument("--min-repeat", type=int, default=3)
    p.set_defaults(func=cmd_map)

    p = sub.add_parser("batch", help="批量紧凑读多个文件")
    p.add_argument("files", nargs="+")
    p.add_argument("--max-lines", type=int, default=80)
    p.add_argument("--head", type=int, default=12)
    p.add_argument("--tail", type=int, default=12)
    p.add_argument("--max-col", type=int, default=300)
    p.set_defaults(func=cmd_batch)

    p = sub.add_parser("check", help="输出文件指纹与行数")
    p.add_argument("file")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("install", help="安装为技能")
    p.add_argument("--scope")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("selftest")
    p.set_defaults(func=cmd_selftest)

    args = parser.parse_args(argv)
    if getattr(args, "max_lines", 1) < 1:
        parser.error("--max-lines 至少为 1")
    if getattr(args, "max_col", 4) < 4:
        parser.error("--max-col 至少为 4")
    file_arg = getattr(args, "file", None)
    if file_arg not in (None, "-") and not Path(file_arg).is_file():
        parser.error(f"不是可读文件: {file_arg}")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
