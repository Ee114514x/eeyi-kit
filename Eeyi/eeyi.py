#!/usr/bin/env python3
"""Eeyi: keep agent context small without dropping information that matters.

安装: python3 eeyi.py install [--scope <技能目录>]
自检: python3 eeyi.py selftest
基准: python3 eeyi.py bench
"""

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sys
from pathlib import Path

__version__ = "0.2.0"

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
VERSION_MARK = re.compile(r"<!-- eeyi ([0-9.]+) sha256:[0-9a-f]{64} -->")

SKILL_TEMPLATE = '''---
name: Eeyi
description: 压缩长命令输出与代码阅读量（测试、构建、日志、git diff、目录结构、大 JSON、token 估算）。
---

# Eeyi

`python3 "{{EEYI}}"`（零依赖，py3.8+，Windows 用 `python`）。stdout 是内容，stderr 是统计。

- 长输出：`<命令> 2>&1 | python3 "{{EEYI}}" trim`
- 同法调 `digest <路径>`、`json <文件>`、`count <文件>`；先 `count` 估算再定 `--max-lines`，最多迭代 2 次（重传原文反而更费）。详见同目录 `GUIDE.md`，无则忽略。

## 必须知道

- `<…第 N 行>` 的行号是折叠后序号，不是原文行号，不能回查；回原文只按 `<错误位置：文件:行号>`（正则提取，可能是无关路径，需确认）。
- 所有 `<…>` 标记都不是原文连续内容（含摘要行）；精确字节、拼写、缩进一律回原文。折叠后没超预算时摘要不出现。
- git diff 不做行数截断，但会被改写（重复 context 折叠、长行中部截断），不能当补丁用；要可应用的 diff 读原文。
- 错误行优先；超预算时按 fatal > error > warning 保留并标注类型数；要全量加 `--keep-all-errors`（token 明显上涨）。
- 堆栈块整块保留，不受 `--max-lines` 影响；但判定偏宽，块内缩进行未必是异常；自定义异常不进块，摘要会落到源码行。
- 输出会略超 `--max-lines`（摘要、标记不计预算），不是出错。
- `json` 的长值（含 base64）截成前 40 字符，只是残片；`--max-str` 只放大首末样本，取具体值回原文。
- `digest` 是正则启发式；少于 150 行的文件直接读全文。`count` 是估算（按 GPT 系分词器校准，其它模型 ±30%），只做相对比较。
'''

KEY = re.compile(
    r"(?:error|errors|exception|traceback|failed|failure|fatal|panic|assert\w*|"
    r"warning|denied|refused|timed out|timeout|exit code|no such file|not found|"
    r"错误|失败|异常|报错|不通过|未通过|断言)",
    re.IGNORECASE,
)

CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")

UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
HEX = re.compile(r"\b[0-9a-f]{8,}\b", re.IGNORECASE)
TIMESTAMP = re.compile(r"\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?\b")
CLOCK = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
NUMBER = re.compile(r"(?<![A-Za-z_\-\d])\d+(?:\.\d+)?")

SEVERE_KEY = re.compile(r"\b(?:fatal|panic|segfault|core dumped|oom|killed|crash\w*)\b|崩溃|严重", re.IGNORECASE)
ERROR_KEY = re.compile(r"\b(?:error|exception|traceback|failed|failure|assert\w*)\b|错误|异常|失败|报错", re.IGNORECASE)
WARN_KEY = re.compile(r"\bwarn(?:ing)?\b|警告", re.IGNORECASE)


def error_signature(line):
    sig = UUID.sub("<uuid>", line.strip().lower())
    sig = TIMESTAMP.sub("<ts>", sig)
    sig = CLOCK.sub("<time>", sig)
    sig = HEX.sub("<hex>", sig)
    sig = NUMBER.sub("<n>", sig)
    return re.sub(r"\s+", " ", sig)[:160]


def severity(line):
    if SEVERE_KEY.search(line):
        return 3
    if ERROR_KEY.search(line):
        return 2
    if WARN_KEY.search(line):
        return 1
    return 0


TB_START = re.compile(
    r"^\s*(?:Traceback \(most recent call last\)|Caused by:|During handling of the above exception"
    r"|panic:|goroutine \d+ \[|Exception in thread|thread '[^']*' panicked)",
)
TB_BODY = re.compile(r"^\s+\S")
EXC_LINE = re.compile(r"^\s*(?:[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt)\b|panic:)")
POS_PY = re.compile(r'File "([^"]+)", line (\d+)')
POS_FILE = re.compile(
    r"([\w./\\-]+\.(?:py|pyi|js|mjs|cjs|ts|tsx|jsx|go|rs|java|kt|rb|php|c|cc|cpp|h|hpp|cs|swift|sh))[:(](\d+)(?::(\d+))?"
)
POS_PAREN = re.compile(r"\(([^()\s]+):(\d+):(\d+)\)")


def traceback_blocks(lines, max_block=80):
    blocks = []
    i = 0
    total = len(lines)
    while i < total:
        if not TB_START.match(lines[i]):
            i += 1
            continue
        j = i + 1
        while j < total and j - i < max_block:
            if TB_BODY.match(lines[j]):
                j += 1
            elif not lines[j].strip() and j + 1 < total and TB_BODY.match(lines[j + 1]):
                j += 1
            else:
                break
        if j < total and EXC_LINE.match(lines[j]):
            j += 1
        blocks.append((i, j))
        i = j
    return blocks


EXC_TAIL = re.compile(r"^[A-Za-z_][\w.]*(?::\s|$)")


def traceback_tail(lines, start, end, max_block=80, limit=2000):
    """块被 max_block 截断时，返回缩进区之后的第一行异常。"""
    if end - start < max_block or end >= len(lines) or not TB_BODY.match(lines[end]):
        return None
    i = end
    stop = min(len(lines), end + limit)
    while i < stop and TB_BODY.match(lines[i]):
        i += 1
    if i < stop and EXC_TAIL.match(lines[i]):
        return i
    return None


def error_locations(lines, indices, limit=6):
    found = []
    for i in indices:
        line = lines[i]
        for match in POS_PY.finditer(line):
            found.append(f"{match.group(1)}:{match.group(2)}")
        for match in POS_FILE.finditer(line):
            found.append(f"{match.group(1)}:{match.group(2)}")
        for match in POS_PAREN.finditer(line):
            found.append(f"{match.group(1)}:{match.group(2)}")
    unique = []
    for item in found:
        if item not in unique:
            unique.append(item)
    return unique[:limit]

SKIP_DIRS = {
    ".git", "node_modules", "target", "dist", "build", "out", "__pycache__",
    ".venv", "venv", ".next", ".turbo", "coverage", ".pytest_cache",
}

SYMBOLS = {
    "py": (r"^(?:class\s+\w+|(?:async\s+)?def\s+\w+|@\w+|from\s+\S+\s+import|import\s+)",),
    "ts": (
        r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|class|interface|type|enum)\s+\w+",
        r"^(?:export\s+)?(?:const|let|var)\s+\w+",
        r"^import\s",
    ),
    "js": (
        r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|class)\s+\w+",
        r"^(?:export\s+)?(?:const|let|var)\s+\w+",
        r"^import\s",
    ),
    "rs": (
        r"^(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?(?:fn|struct|enum|trait|impl|mod|const|static|type|macro_rules!)\b",
        r"^use\s",
    ),
    "go": (r"^(?:func|type|var|const)\s", r"^import\s", r"^package\s"),
    "java": (r"^(?:public|protected|private|internal|abstract|final|static|data|sealed)\b[^=]*\(", r"^(?:class|interface|enum|record|object)\s", r"^import\s"),
    "md": (r"^#{1,4}\s",),
    "toml": (r"^\[[^\]]+\]",),
    "yml": (r"^[A-Za-z_][\w.-]*:",),
}

EXT_ALIAS = {
    "pyi": "py", "mjs": "js", "cjs": "js", "jsx": "js", "tsx": "ts",
    "kt": "java", "kts": "java", "yaml": "yml",
}

def count_tokens(text):
    # 系数按 GPT-4/4o 分词器实测校准；中文密集文本误差约 ±10%，代码/JSON 约 ±30%，其它模型另有差异
    cjk = len(CJK.findall(text))
    ws = len(re.findall(r"\s", text))
    rest = len(text) - cjk - ws
    return math.ceil(cjk * 1.2 + rest * 0.3 + ws * 0.1)


def read_text(path):
    if path is None or path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8", errors="replace")


def collapse_blank(lines):
    out = []
    for line in lines:
        if line.strip() == "" and out and out[-1] == "":
            continue
        out.append(line)
    return out


def fold_cycles(lines, max_period=16, min_repeats=3):
    out = []
    i = 0
    n = len(lines)
    while i < n:
        best = None
        for period in range(1, max_period + 1):
            if i + period * min_repeats > n:
                break
            if lines[i] != lines[i + period]:
                continue
            block = lines[i:i + period]
            repeats = 1
            while i + (repeats + 1) * period <= n and lines[
                i + repeats * period:i + (repeats + 1) * period
            ] == block:
                repeats += 1
            if repeats >= min_repeats and (best is None or repeats * period > best[0] * best[1]):
                best = (repeats, period)
        if best is None:
            out.append(lines[i])
            i += 1
            continue
        repeats, period = best
        out.extend(lines[i:i + period])
        if period == 1:
            out.append(f"<重复 {repeats - 1} 次>")
        else:
            out.append(f"<上面 {period} 行重复 {repeats} 次>")
        i += repeats * period
    return out


def clip_line(line, max_col):
    if max_col < 4:
        raise ValueError("max_col 至少为 4")
    if len(line) <= max_col:
        return line
    keep = max_col // 2
    return f"{line[:keep]}…<{len(line) - keep * 2} 字符省略>…{line[-keep:]}"


DIFF_HUNK = re.compile(r"^@@ -\d+")


def looks_like_diff(lines):
    if len(lines) < 20:
        return False
    if not any(DIFF_HUNK.match(line) for line in lines[:60]):
        return False
    body = sum(1 for line in lines if line[:1] in ("+", "-", " "))
    return body >= len(lines) * 0.8


def looks_like_single_line_json(text):
    stripped = text.strip()
    if len(stripped) < 200 or "\n" in stripped:
        return False
    if stripped[0] not in "[{":
        return False
    try:
        json.loads(stripped)
    except json.JSONDecodeError:
        return False
    return True


def frame_start(line):
    """栈帧起始行：Python 的 File "…"、Go/Java/JS 的 file:line、.NET 的 at …"""
    return (
        bool(POS_PY.search(line) or POS_FILE.search(line) or POS_PAREN.search(line))
        or line.lstrip().startswith("at ")
    )


def shrink_traceback(lines, span, head_units=15, tail_units=8):
    """长栈块截断时把断点对齐到栈帧起始行，避免留下没有 File 头的续行。"""
    cut = head_units
    while cut > 1 and not frame_start(lines[span[cut]]):
        cut -= 1
    tail_start = max(cut, len(span) - tail_units)
    while tail_start > cut and not frame_start(lines[span[tail_start]]):
        tail_start += 1
    if cut <= 1 or tail_start >= len(span):
        return span[:head_units] + span[-tail_units:]
    return span[:cut] + span[tail_start:]


def select(lines, max_lines, head, tail, keep_all_errors=False):
    if len(lines) <= max_lines:
        return lines
    if head + tail > max_lines:
        total = head + tail
        head = max_lines * head // total if total else 0
        tail = max_lines - head if total else 0
    keep = set(range(min(head, len(lines))))
    keep |= set(range(max(0, len(lines) - tail), len(lines)))
    notes = []

    block_rows = set()
    blocks = traceback_blocks(lines)
    for start, end in blocks:
        block_rows |= set(range(start, end))
    keep -= block_rows
    tails = {}
    for start, end in blocks:
        span = list(range(start, end))
        if len(span) > 25:
            span = shrink_traceback(lines, span)
        keep |= set(span)
        root = traceback_tail(lines, start, end)
        if root is not None:
            tails[start] = root
            keep.add(root)

    keyed = [i for i, line in enumerate(lines) if KEY.search(line) and i not in block_rows]

    if keyed:
        budget = len(keyed) if keep_all_errors else max(max_lines - len(keep), 1)
        if len(keyed) <= budget:
            keep |= set(keyed)
        else:
            groups = {}
            for i in keyed:
                groups.setdefault(error_signature(lines[i]), []).append(i)
            ranked = sorted(groups.items(), key=lambda item: (-severity(lines[item[1][0]]), item[1][0]))
            chosen = ranked[:max(1, budget // 2)]
            for _, idxs in chosen:
                keep.add(idxs[0])
                if len(idxs) > 1:
                    keep.add(idxs[-1])
                if len(idxs) > 2:
                    notes.append(f"<同类 {len(idxs)} 条：第 {idxs[0] + 1}-{idxs[-1] + 1} 行>")
            rest = ranked[len(chosen):]
            if rest:
                notes.insert(0, f"<另有 {len(rest)} 类错误未逐行列出（共 {sum(len(idxs) for _, idxs in rest)} 行，含在上面省略段内）>")

    out = []
    last = -1
    for i in sorted(keep):
        if i > last + 1:
            out.append(f"<省略 {i - last - 1} 行>")
        out.append(lines[i])
        last = i
    if last < len(lines) - 1:
        out.append(f"<省略 {len(lines) - 1 - last} 行>")

    error_rows = sorted(set(keyed) | block_rows)
    summary = []
    if error_rows:
        first = error_rows[0]
        block = next((b for b in traceback_blocks(lines) if b[0] <= first < b[1]), None)
        if block:
            root = tails.get(block[0])
            detail = lines[root] if root is not None else next(
                (lines[k] for k in range(block[1] - 1, block[0], -1) if lines[k].strip()),
                lines[first],
            )
            summary.append(f"<首个错误：第 {first + 1} 行，异常 {detail.strip()[:100]}>")
        else:
            summary.append(f"<首个错误：第 {first + 1} 行 {lines[first].strip()[:100]}>")
        locations = error_locations(lines, error_rows)
        if locations:
            summary.append("<错误位置：" + ", ".join(locations) + ">")

    out.extend(notes[:20])
    if len(notes) > 20:
        out.append(f"<另有 {len(notes) - 20} 条同类说明>")
    return summary + out


def trim_text(raw, max_lines=80, head=12, tail=12, max_col=300, min_repeat=3, max_period=16, keep_all_errors=False):
    lines = [clip_line(ANSI.sub("", line), max_col) for line in raw.splitlines()]
    lines = collapse_blank(lines)
    lines = fold_cycles(lines, max_period, min_repeat)
    limit = len(lines) if looks_like_diff(lines) else max_lines
    return "\n".join(select(lines, limit, head, tail, keep_all_errors))


def cmd_trim(args):
    raw = read_text(args.file)
    out = trim_text(
        raw, args.max_lines, args.head, args.tail,
        args.max_col, args.min_repeat, args.max_period, args.keep_all_errors,
    )
    sys.stdout.write(out + ("\n" if out else ""))

    before_tokens = count_tokens(raw)
    after_tokens = count_tokens(out)
    saved = 0 if before_tokens == 0 else round(100 * (1 - after_tokens / before_tokens))
    sys.stderr.write(
        f"eeyi trim: {len(raw.splitlines())}→{len(out.splitlines())} 行, "
        f"~{before_tokens}→{after_tokens} token ({saved:+d}%)\n"
    )
    if looks_like_single_line_json(raw):
        sys.stderr.write("eeyi: 整份输入是单行 JSON，需要字段结构请用 json 子命令，避免行内截断产生拼接值\n")
    return 0


def rules_for(path):
    ext = path.suffix.lower().lstrip(".")
    return SYMBOLS.get(EXT_ALIAS.get(ext, ext))


def digest_file(path, max_symbols):
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    rules = rules_for(path)
    hits = []
    if rules:
        patterns = [re.compile(r) for r in rules]
        hits = [(i, line.replace("\x1b", "")) for i, line in enumerate(lines, 1)
                if any(p.match(line.strip()) for p in patterns)]
    shown = hits[:max_symbols]
    rel = path.name
    head = f"{rel}  [{len(lines)} 行]"
    body = [f"  {rel}:{i} {clip_line(line.strip().rstrip('{').strip(), 160)}" for i, line in shown]
    if len(hits) > len(shown):
        body.append(f"  <另有 {len(hits) - len(shown)} 个符号>")
    return [head] + body, len(hits)


def digest_tree(root, depth, max_entries):
    entries = []
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        rel_dir = Path(dirpath).relative_to(root)
        level = 0 if str(rel_dir) == "." else len(rel_dir.parts)
        if level > depth:
            dirnames[:] = []
            continue
        for name in sorted(filenames):
            p = Path(dirpath) / name
            try:
                size = p.stat().st_size
            except OSError:
                continue
            entries.append((str(p.relative_to(root)), size))
    entries.sort()
    shown = entries[:max_entries]
    out = [f"{root.name}/  [{len(entries)} 个文件]"]
    out += [f"  {name}  {size}B" for name, size in shown]
    if len(entries) > len(shown):
        out.append(f"  <另有 {len(entries) - len(shown)} 个文件>")
    return out


def cmd_digest(args):
    target = Path(args.path)
    if target.is_dir():
        out = digest_tree(target, args.depth, args.max_entries)
    else:
        out, _ = digest_file(target, args.max_symbols)
    text = "\n".join(out)
    sys.stdout.write(text + "\n")
    sys.stderr.write(f"eeyi digest: ~{count_tokens(text)} token\n")
    return 0


def cmd_count(args):
    rows = []
    for path in args.files:
        text = read_text(path)
        rows.append((path, len(text), count_tokens(text)))
    for path, chars, tokens in rows:
        print(f"{tokens:>8} token  {chars:>9} 字符  {path}")
    if len(rows) > 1:
        print(f"{sum(r[2] for r in rows):>8} token  合计")
    return 0


SCALAR = (str, int, float, bool)


def json_shape(value, max_keys, max_str, depth=0):
    if depth >= 4:
        if value is None or isinstance(value, (int, float, bool)):
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, str):
            return json_shape(value, max_keys, max_str, 0)
        if isinstance(value, list) and all(item is None or isinstance(item, SCALAR) for item in value[:10]):
            first = json_shape(value[0], max_keys, max_str, 0) if value else "null"
            return f"array[{len(value)}] of {first}"
        return f"<{type(value).__name__}>"
    if isinstance(value, dict):
        items = list(value.items())
        if len(items) >= 4 and sum(1 for _, v in items if isinstance(v, dict)) >= len(items) * 0.8:
            merged = {}
            for _, inner in items[:50]:
                for key, sample in inner.items():
                    merged.setdefault(key, sample)
            return f"object[{len(items)}] of " + json_shape(merged, max_keys, max_str, depth + 1)
        parts = [f'"{k}": {json_shape(v, max_keys, max_str, depth + 1)}' for k, v in items[:max_keys]]
        if len(items) > max_keys:
            parts.append(f"<另有 {len(items) - max_keys} 个键>")
        return "{" + ", ".join(parts) + "}"
    if isinstance(value, list):
        if not value:
            return "array[0]"
        if all(isinstance(item, dict) for item in value[:50]):
            merged = {}
            for item in value[:50]:
                for key, sample in item.items():
                    merged.setdefault(key, sample)
            return f"array[{len(value)}] of " + json_shape(merged, max_keys, max_str, depth + 1)
        return f"array[{len(value)}] of " + json_shape(value[0], max_keys, max_str, depth + 1)
    if isinstance(value, str):
        limit = min(max_str, 40)
        if len(value) <= limit:
            return json.dumps(value, ensure_ascii=False)
        return json.dumps(value[:limit], ensure_ascii=False)[:-1] + f'…<{len(value) - limit} 字符>"'
    return json.dumps(value, ensure_ascii=False)


def json_brief(value, max_str):
    text = json.dumps(value, ensure_ascii=False)
    if len(text) <= max_str * 4:
        return text
    return text[:max_str * 2] + f"…<{len(text) - max_str * 2} 字符>"


def json_summary(data, max_keys=12, max_str=60):
    out = ["结构: " + json_shape(data, max_keys, max_str)]
    if isinstance(data, list) and data:
        out.append("首条: " + json_brief(data[0], max_str))
        out.append("末条: " + json_brief(data[-1], max_str))
    elif isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, list) and value:
                out.append(f"{key} 首条: " + json_brief(value[0], max_str))
                out.append(f"{key} 末条: " + json_brief(value[-1], max_str))
                break
    return "\n".join(out)


def cmd_json(args):
    raw = read_text(args.file)
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        sys.stderr.write(f"eeyi json: 无法解析 JSON: {exc}\n")
        return 1

    text = json_summary(data, args.max_keys, args.max_str)
    sys.stdout.write(text + "\n")
    before = count_tokens(raw)
    after = count_tokens(text)
    saved = 0 if before == 0 else round(100 * (1 - after / before))
    sys.stderr.write(f"eeyi json: ~{before}→{after} token ({saved:+d}%)\n")
    return 0


def default_scope():
    for parent in [Path.cwd(), *Path.cwd().parents]:
        if (parent / ".aicode").is_dir():
            return parent / ".aicode" / "skills"
    return Path.home() / ".aicode" / "skills"


def cmd_install(args):
    source = Path(__file__).resolve()
    scope = Path(args.scope).expanduser().resolve() if args.scope else default_scope()
    dest = scope / "Eeyi"
    script = dest / "eeyi.py"

    if getattr(args, "check", False):
        if not script.is_file():
            print(f"未安装: {dest}")
            return 1
        same = hashlib.sha256(source.read_bytes()).digest() == hashlib.sha256(script.read_bytes()).digest()
        skill = dest / "SKILL.md"
        installed = "未知"
        if skill.is_file():
            found = VERSION_MARK.search(skill.read_text(encoding="utf-8", errors="replace"))
            if found:
                installed = found.group(1)
        print(f"源码: {source} (v{__version__})")
        print(f"副本: {script} (v{installed})")
        print("状态: 一致" if same else "状态: 过期，请重新运行 install 覆盖")
        return 0 if same else 1

    for target in (scope, dest):
        if target.exists() and not target.is_dir():
            sys.stderr.write(f"eeyi install: 目标不是目录: {target}\n")
            return 2
    dest.mkdir(parents=True, exist_ok=True)
    if script != source:
        shutil.copyfile(source, script)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    body = SKILL_TEMPLATE.replace("{{EEYI}}", str(script))
    body += f"\n<!-- eeyi {__version__} sha256:{digest} -->\n"
    (dest / "SKILL.md").write_text(body, encoding="utf-8")
    print(f"已安装技能: {dest}")
    print(f"  技能正文: {dest / 'SKILL.md'}")
    print(f"  伴随脚本: {script}")
    print(f"  版本: {__version__}")
    print("新会话生效；核对副本可用 install --check，自检用 selftest")
    return 0


def ensure_utf8_streams():
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if encoding not in ("utf8", ""):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass


def main(argv=None):
    parser = argparse.ArgumentParser(prog="eeyi", description=__doc__)
    parser.add_argument("--version", action="version", version=f"eeyi {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("trim", help="压缩命令输出，保留错误行、头尾与原有行")
    p.add_argument("file", nargs="?")
    p.add_argument("--max-lines", type=int, default=80)
    p.add_argument("--head", type=int, default=12)
    p.add_argument("--tail", type=int, default=12)
    p.add_argument("--max-col", type=int, default=300)
    p.add_argument("--min-repeat", type=int, default=3)
    p.add_argument("--max-period", type=int, default=16)
    p.add_argument("--keep-all-errors", action="store_true", help="错误/告警行超预算时也不丢弃，只折叠同类重复")
    p.set_defaults(func=cmd_trim)

    p = sub.add_parser("digest", help="输出目录结构或文件符号骨架")
    p.add_argument("path")
    p.add_argument("--depth", type=int, default=1)
    p.add_argument("--max-entries", type=int, default=60)
    p.add_argument("--max-symbols", type=int, default=40)
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser("count", help="估算文本 token")
    p.add_argument("files", nargs="+")
    p.set_defaults(func=cmd_count)

    p = sub.add_parser("json", help="把 JSON 压成结构 + 首末样本")
    p.add_argument("file", nargs="?")
    p.add_argument("--max-keys", type=int, default=12)
    p.add_argument("--max-str", type=int, default=60)
    p.set_defaults(func=cmd_json)

    p = sub.add_parser("install", help="把技能装到技能目录，脚本一并复制")
    p.add_argument("--scope", help="技能目录；默认当前项目有 .aicode 就装项目级，否则装全局")
    p.add_argument("--check", action="store_true", help="不安装，只比对已安装副本与源码是否一致")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("bench", help="跑压缩率、信息保真与耗时基准")
    p.add_argument("--corpus", help="代码语料目录，缺省时自动找当前目录或上级的 dsh-ops")
    p.set_defaults(func=cmd_bench)

    p = sub.add_parser("selftest", help="跑内置单元测试")
    p.set_defaults(func=cmd_selftest)

    args = parser.parse_args(argv)
    ensure_utf8_streams()

    if args.cmd == "trim":
        if args.max_col < 4:
            parser.error("--max-col 至少为 4")
        if args.max_lines < 1:
            parser.error("--max-lines 至少为 1")
        if args.head < 0 or args.tail < 0:
            parser.error("--head/--tail 不能为负")
        if args.min_repeat < 2:
            parser.error("--min-repeat 至少为 2")
        if args.max_period < 1:
            parser.error("--max-period 至少为 1")
    if args.cmd == "json":
        if args.max_keys < 1 or args.max_str < 4:
            parser.error("--max-keys 至少为 1，--max-str 至少为 4")

    for path in [getattr(args, "file", None)] + list(getattr(args, "files", []) or []):
        if path not in (None, "-") and not Path(path).is_file():
            parser.error(f"不是可读文件: {path}")
    if args.cmd == "digest" and not Path(args.path).exists():
        parser.error(f"路径不存在: {args.path}")
    return args.func(args)


# ---------- 基准 ----------

def _fixture_test_output():
    lines = ["$ pytest -q tests/", "collected 180 items"]
    for i in range(1, 60):
        lines.append(f"tests/test_module_{i:02d}.py ...{'F' if i % 20 == 0 else '.'}")
        lines.append(f"  [cache] collected {i * 7} fixtures in 0.0{i % 9}s")
    lines += [
        "",
        "=================================== FAILURES ===================================",
        "E   AssertionError: expected 42, got 41",
        "tests/test_module_20.py:88: AssertionError",
        "E   ConnectionError: upstream refused connection after 3 retries",
        "ERROR tests/test_module_40.py - RuntimeError: worker exited with code 137",
        "",
        "short test summary info",
        "FAILED tests/test_module_20.py::test_total - AssertionError",
        "ERROR tests/test_module_40.py - RuntimeError",
        "2 failed, 1 error, 177 passed in 41.28s",
    ]
    return "\n".join(lines) + "\n"


def _fixture_build_log():
    block = [
        "webpack 5.94.0 compiling...",
        "  [ts-loader] src/index.ts 3.4 KiB [built]",
        "  [ts-loader] src/util.ts 1.1 KiB [built]",
        "  cache hit for chunk vendors~main",
    ]
    lines = block * 60 + [
        "ERROR in src/broken.ts:12:3",
        "TS2304: Cannot find name 'undefind'.",
        "webpack 5.94.0 compiled with 1 error in 8123 ms",
    ]
    return "\n".join(lines) + "\n"


def _fixture_long_line():
    payload = "".join(f'"k{i}":"value-{i}"' for i in range(400))
    return '{"records":[' + payload + '],\n"status":"ok",\n"note":"truncated payload for bench"}\n'


def _fixture_diff():
    lines = [
        "diff --git a/src/app.ts b/src/app.ts",
        "index 1a2b3c4..5d6e7f8 100644",
        "--- a/src/app.ts",
        "+++ b/src/app.ts",
    ]
    for hunk in range(6):
        start = hunk * 40 + 1
        lines.append(f"@@ -{start},18 +{start},20 @@ export function handler{hunk}()")
        lines += [f"   const config{hunk}_{i} = load({i});" for i in range(6)]
        lines += [f"-  legacyCall{hunk}({i});" for i in range(4)]
        lines += [f"+  modernCall{hunk}({i});" for i in range(5)]
        lines += ["   // context line"]
    return "\n".join(lines) + "\n"


def _fixture_records():
    records = [
        {
            "id": i,
            "name": f"user-{i}",
            "email": f"user{i}@example.com",
            "roles": ["admin", "dev"] if i % 3 else ["dev"],
            "meta": {"created": f"2026-01-{(i % 28) + 1:02d}", "score": round(i * 1.5, 2)},
        }
        for i in range(400)
    ]
    return json.dumps({"records": records, "total": 400, "status": "ok"}, ensure_ascii=False)


def _fixture_big_log():
    block = [
        f"2026-10-28 00:{i:02d}:{(i * 7) % 60:02d} worker-{i % 8} tick={i} stage=parse batch={i * 3}"
        for i in range(12)
    ]
    return "\n".join(block * 4200 + ["ERROR worker-3 crashed: OOM killed"]) + "\n"


def _fixture_error_storm():
    lines = [f"ERROR kind-{i % 60}: failed with code {i}" for i in range(3000)]
    lines += ["WARN retry scheduled"] * 2000
    lines.append("FATAL: database corrupted")
    return "\n".join(lines) + "\n"


def _fixture_traceback():
    lines = [f"2026-10-28 00:00:{i:02d} INFO step {i} ok" for i in range(400)]
    lines += [
        "Traceback (most recent call last):",
        '  File "/app/service/handler.py", line 42, in handle',
        "    result = db.query(sql)",
        '  File "/app/service/db.py", line 88, in query',
        "    raise OperationalError('connection lost')",
        "OperationalError: connection lost",
        "",
        "During handling of the above exception, another exception occurred:",
        "",
        "Traceback (most recent call last):",
        '  File "/app/service/handler.py", line 47, in handle',
        "    raise RuntimeError('request failed')",
        "RuntimeError: request failed",
    ]
    return "\n".join(lines) + "\n"


def _bench_corpus(explicit=None):
    if explicit:
        return Path(explicit).expanduser()
    for candidate in (Path.cwd() / "dsh-ops", Path(__file__).resolve().parent.parent / "dsh-ops"):
        if candidate.is_dir():
            return candidate
    return None


def cmd_bench(args):
    import time

    rows = []

    def record(name, transform, text, required=(), check_keys=True):
        start = time.perf_counter()
        out = transform(text)
        ms = max(1, round((time.perf_counter() - start) * 1000))
        out_lines = out.splitlines()
        keys = []
        if check_keys:
            for line in text.splitlines():
                stripped = line.strip()
                if KEY.search(line) and stripped not in keys:
                    keys.append(stripped)
        missing = [key for key in keys if not any(key[:60] in line for line in out_lines)]
        checks = [(f"关键行 {len(keys) - len(missing)}/{len(keys)}", not missing)] if keys else []
        checks += [(f"含「{item[:36]}」", item in out) for item in required]
        rows.append({"name": name, "before": count_tokens(text), "after": count_tokens(out), "ms": ms, "checks": checks})

    record("trim 测试输出", trim_text, _fixture_test_output(), [
        "FAILED tests/test_module_20.py::test_total",
        "RuntimeError: worker exited with code 137",
        "2 failed, 1 error, 177 passed",
    ])
    record("trim 构建日志", trim_text, _fixture_build_log(), ["TS2304", "src/broken.ts:12:3", "compiled with 1 error"])
    record("trim 超长单行 JSON", trim_text, _fixture_long_line(), ['"status":"ok"'])
    record("json 400 条记录", json_summary_json, _fixture_records(), ["records", "email", "meta", "user-399"], check_keys=False)
    record("trim 5 万行日志", trim_text, _fixture_big_log(), ["OOM killed"])
    record(
        "trim 海量错误",
        trim_text,
        _fixture_error_storm(),
        ["FATAL: database corrupted", "另有", "同类"],
        check_keys=False,
    )
    record(
        "trim 堆栈根因",
        trim_text,
        _fixture_traceback(),
        ["首个错误：第 401 行", "handler.py:42", "db.py:88", "OperationalError: connection lost"],
    )

    diff_text = _fixture_diff()
    start = time.perf_counter()
    diff_out = trim_text(diff_text)
    diff_ms = max(1, round((time.perf_counter() - start) * 1000))
    changed = [
        line for line in diff_text.splitlines()
        if line.startswith("@@") or (line[:1] in ("+", "-") and not line.startswith(("+++", "---")))
    ]
    lost = [line for line in changed if not any(line[:60] in out_line for out_line in diff_out.splitlines())]
    rows.append({
        "name": "trim git diff",
        "before": count_tokens(diff_text),
        "after": count_tokens(diff_out),
        "ms": diff_ms,
        "checks": [
            (f"diff 变更行与 hunk 头 {len(changed) - len(lost)}/{len(changed)}", not lost),
            ("保留 diff --git 头", "diff --git" in diff_out),
        ],
    })

    corpus = _bench_corpus(args.corpus)
    lockfile = corpus / "package-lock.json" if corpus else None
    if lockfile and lockfile.is_file():
        text = lockfile.read_text(encoding="utf-8", errors="replace")
        record(
            "json 真实 lockfile",
            lambda raw: json_summary(json.loads(raw)),
            text,
            ["packages", "lockfileVersion", "x64", "@dsh-ops/bash-win32-x64"],
            check_keys=False,
        )

    if corpus and corpus.is_dir():
        sources = sorted(
            (path for path in corpus.rglob("*") if path.suffix in {".ts", ".rs"} and path.is_file()),
            key=lambda path: path.stat().st_size,
            reverse=True,
        )[:12]
        before = after = ms = 0
        empty = []
        for path in sources:
            text = path.read_text(encoding="utf-8", errors="replace")
            start = time.perf_counter()
            out_lines, _ = digest_file(path, 40)
            ms = max(ms, round((time.perf_counter() - start) * 1000))
            before += count_tokens(text)
            after += count_tokens("\n".join(out_lines))
            if len(out_lines) < 2:
                empty.append(path.name)
        rows.append({
            "name": f"digest 代码骨架 ({len(sources)} 文件)",
            "before": before,
            "after": after,
            "ms": ms,
            "checks": [(f"{len(sources)} 个文件都提取到符号", not empty)],
        })

    print(f"{'场景':<28}{'原始':>8}{'压缩后':>8}{'节省':>7}{'耗时':>8}")
    print("-" * 60)
    total_before = total_after = 0
    for row in rows:
        saved = 0 if row["before"] == 0 else round(100 * (1 - row["after"] / row["before"]))
        print(f"{row['name']:<28}{row['before']:>8}{row['after']:>8}{str(saved) + '%':>7}{str(row['ms']) + 'ms':>8}")
        total_before += row["before"]
        total_after += row["after"]
    saved = 0 if total_before == 0 else round(100 * (1 - total_after / total_before))
    print("-" * 60)
    print(f"{'合计':<28}{total_before:>8}{total_after:>8}{str(saved) + '%':>7}")

    checks = [check for row in rows for check in row["checks"]]
    failures = [desc for desc, ok in checks if not ok]
    print()
    print(f"断言: {len(checks) - len(failures)}/{len(checks)} 通过（耗时为纯处理时间，不含解释器启动）")
    for desc in failures:
        print(f"  失败: {desc}")
    return 1 if failures else 0


def json_summary_json(raw):
    return json_summary(json.loads(raw))


# ---------- 自测 ----------

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
        def test_count_empty(self):
            self.assertEqual(count_tokens(""), 0)

        def test_count_cjk_costs_more(self):
            self.assertGreater(count_tokens("错误"), count_tokens("ee"))

        def test_count_monotonic(self):
            self.assertLess(count_tokens("a" * 10), count_tokens("a" * 100))

        def test_clip_short(self):
            self.assertEqual(clip_line("abc", 10), "abc")

        def test_clip_keeps_both_ends(self):
            out = clip_line("HEAD" + "x" * 500 + "TAIL", 40)
            self.assertTrue(out.startswith("HEAD"))
            self.assertTrue(out.endswith("TAIL"))

        def test_clip_zero_width_raises(self):
            with self.assertRaises(ValueError):
                clip_line("abcdef", 0)

        def test_fold_single_line(self):
            self.assertEqual(fold_cycles(["a"] * 4), ["a", "<重复 3 次>"])

        def test_fold_block(self):
            block = ["p", "q", "r", "s"]
            self.assertEqual(fold_cycles(block * 5), block + ["<上面 4 行重复 5 次>"])

        def test_fold_twelve_line_block(self):
            block = [f"line-{i}" for i in range(12)]
            self.assertEqual(len(fold_cycles(block * 3)), 13)

        def test_fold_no_repeat(self):
            lines = ["a", "b", "c", "d"]
            self.assertEqual(fold_cycles(lines), lines)

        def test_fold_below_min(self):
            self.assertEqual(fold_cycles(["a", "a", "b"], min_repeats=3), ["a", "a", "b"])

        def test_select_short_untouched(self):
            self.assertEqual(select(["a", "b", "c"], 10, 1, 1), ["a", "b", "c"])

        def test_select_keeps_error_beyond_window(self):
            lines = [f"filler {i}" for i in range(200)]
            lines[100] = "ERROR: something exploded"
            self.assertTrue(any("exploded" in line for line in select(lines, 20, 2, 2)))

        def test_select_marks_omissions(self):
            out = select([f"line {i}" for i in range(50)], 10, 2, 2)
            self.assertTrue(any(line.startswith("<省略") for line in out))

        def test_select_never_drops_all_errors(self):
            lines = [f"ERROR {i}" for i in range(40)] + ["tail"]
            out = select(lines, 5, 1, 1)
            self.assertGreater(sum(1 for line in out if line.startswith("ERROR")), 0)
            self.assertIn("tail", out)

        def test_diff_detection_plain(self):
            self.assertFalse(looks_like_diff(["hello"] * 30))

        def test_diff_detection_real(self):
            lines = ["diff --git a/x b/x", "--- a/x", "+++ b/x", "@@ -1,3 +1,4 @@"]
            lines += [" context"] * 10 + ["+added"] * 6 + ["-removed"] * 6
            self.assertTrue(looks_like_diff(lines))

        def test_json_scalar(self):
            self.assertEqual(json_shape(True, 12, 60), "true")

        def test_json_homogeneous_mapping(self):
            out = json_shape({f"pkg-{i}": {"version": "1.0.0"} for i in range(20)}, 12, 60)
            self.assertIn("object[20] of", out)
            self.assertIn("version", out)

        def test_json_array_of_objects(self):
            out = json_shape([{"id": i, "name": f"n{i}"} for i in range(10)], 12, 60)
            self.assertIn("array[10] of", out)
            self.assertIn("id", out)

        def test_json_scalar_array_keeps_sample(self):
            self.assertIn('"x64"', json_shape(["x64", "arm64"], 12, 60))

        def test_json_long_string_marked(self):
            out = json_shape("y" * 200, 12, 60)
            self.assertIn("字符", out)
            self.assertTrue(out.startswith('"'))

        def test_single_line_json_detected(self):
            self.assertTrue(looks_like_single_line_json("[" + ",".join(['{"a":1}'] * 50) + "]"))

        def test_plain_log_not_json(self):
            self.assertFalse(looks_like_single_line_json("just a log line\n"))

        def test_trim_keeps_error(self):
            payload = "".join(f"noise {i}\n" for i in range(500)) + "Traceback: boom\n"
            self.assertIn("Traceback: boom", trim_text(payload))
            self.assertLess(len(trim_text(payload).splitlines()), 40)

        def test_trim_empty(self):
            self.assertEqual(trim_text(""), "")

        def test_trim_diff_not_truncated(self):
            text = _fixture_diff()
            self.assertEqual(len(trim_text(text).splitlines()), len(text.splitlines()))

        def test_error_signature_normalizes_numbers(self):
            self.assertEqual(
                error_signature("ERROR: timeout at 12:30"),
                error_signature("ERROR: timeout at 13:45"),
            )

        def test_severity_order(self):
            self.assertGreater(severity("FATAL: boom"), severity("ERROR: boom"))
            self.assertGreater(severity("ERROR: boom"), severity("WARN: boom"))

        def test_error_storm_folds_repeats(self):
            lines = [f"ERROR kind-{i % 20}: failed code {i}" for i in range(2000)]
            out = select(lines, 20, 2, 2)
            self.assertLess(len(out), 40)
            self.assertTrue(any("同类" in line or "另有" in line for line in out))

        def test_error_storm_keeps_fatal(self):
            lines = [f"WARN retry {i}" for i in range(500)] + ["FATAL: database corrupted"]
            self.assertTrue(any("FATAL" in line for line in select(lines, 10, 1, 1)))

        def test_keep_all_errors_option(self):
            lines = [f"ERROR e{i}" for i in range(500)]
            out = select(lines, 10, 1, 1, keep_all_errors=True)
            self.assertEqual(sum(1 for line in out if line.startswith("ERROR")), 500)

        def test_traceback_blocks_detected(self):
            self.assertTrue(traceback_blocks(_fixture_traceback().splitlines()))

        def test_traceback_block_not_split(self):
            out = trim_text(_fixture_traceback())
            self.assertIn('File "/app/service/db.py", line 88', out)
            self.assertIn("OperationalError: connection lost", out)

        def test_error_source_summary(self):
            out = trim_text(_fixture_traceback())
            self.assertIn("首个错误：第 401 行", out)
            self.assertIn("错误位置：", out)
            self.assertIn("handler.py:42", out)
            self.assertIn("db.py:88", out)

        def test_select_clamps_windows_to_budget(self):
            out = select([f"line {i}" for i in range(100)], 5, 12, 12)
            self.assertLessEqual(len(out), 8)

        def test_long_traceback_keeps_exception(self):
            frames = []
            for i in range(100):
                frames.append(f'  File "/app/m{i}.py", line {i + 1}, in f{i}')
                frames.append(f"    step{i}()")
            lines = [f"pad {i}" for i in range(200)]
            lines += ["Traceback (most recent call last):"] + frames + ["BadThing: boom"]
            lines += [f"after {i}" for i in range(2000)]
            out = trim_text("\n".join(lines) + "\n")
            self.assertIn("BadThing: boom", out)
            self.assertIn("异常 BadThing: boom", out)

    class CliTest(unittest.TestCase):
        def test_trim_stdin(self):
            proc = run_cli(["trim"], "a\nb\n")
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "a\nb\n")

        def test_trim_empty_input(self):
            proc = run_cli(["trim"], "")
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "")

        def test_version_flag(self):
            self.assertIn("eeyi", run_cli(["--version"]).stdout)

        def test_crlf_normalized(self):
            self.assertEqual(run_cli(["trim"], "a\r\nb\r\n").stdout, "a\nb\n")

        def test_ansi_stripped(self):
            self.assertEqual(run_cli(["trim"], "\x1b[31mred\x1b[0m\n").stdout, "red\n")

        def test_very_long_single_line(self):
            proc = run_cli(["trim", "--max-col", "40"], "x" * 200000 + "\n")
            self.assertEqual(proc.returncode, 0)
            self.assertLess(len(proc.stdout), 200)

        def test_missing_file(self):
            self.assertEqual(run_cli(["trim", "/nonexistent/file.txt"]).returncode, 2)

        def test_directory_rejected(self):
            proc = run_cli(["trim", str(Path(__file__).resolve().parent)])
            self.assertEqual(proc.returncode, 2)
            self.assertNotIn("Traceback", proc.stderr)

        def test_invalid_arguments(self):
            for argv in (["trim", "--max-col", "0"], ["trim", "--max-lines", "-5"], ["trim", "--min-repeat", "1"]):
                self.assertEqual(run_cli(argv, "abc\n").returncode, 2, argv)

        def test_binary_file(self):
            with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as handle:
                handle.write(bytes(range(256)) * 20)
                path = handle.name
            try:
                proc = run_cli(["trim", path])
                self.assertEqual(proc.returncode, 0)
                self.assertNotIn("Traceback", proc.stderr)
            finally:
                os.unlink(path)

        def test_single_line_json_hint(self):
            payload = "[" + ",".join('{"a":1}' for _ in range(40)) + "]"
            self.assertIn("json 子命令", run_cli(["trim", "--max-col", "40"], payload).stderr)

        def test_plain_log_no_hint(self):
            self.assertNotIn("json 子命令", run_cli(["trim"], "error: x\nerror: x\n").stderr)

        def test_json_valid(self):
            proc = run_cli(["json"], '[{"a":1},{"a":2}]')
            self.assertEqual(proc.returncode, 0)
            self.assertIn("array[2]", proc.stdout)

        def test_json_invalid(self):
            proc = run_cli(["json"], "{not json")
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(proc.stdout, "")

        def test_json_scalar_root(self):
            proc = run_cli(["json"], "42")
            self.assertEqual(proc.returncode, 0)
            self.assertIn("42", proc.stdout)

        def test_json_deep_nesting_bounded(self):
            payload = "1"
            for _ in range(30):
                payload = '{"k":' + payload + "}"
            proc = run_cli(["json"], payload)
            self.assertEqual(proc.returncode, 0)
            self.assertLess(len(proc.stdout), 400)

        def test_json_huge_integer_reports_error(self):
            proc = run_cli(["json"], '{"n":' + "9" * 5000 + "}")
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(proc.stdout, "")
            self.assertNotIn("Traceback", proc.stderr)

        def test_json_extreme_nesting_reports_error(self):
            payload = "1"
            for _ in range(50000):
                payload = '{"k":' + payload + "}"
            proc = run_cli(["json"], payload)
            self.assertEqual(proc.returncode, 1)
            self.assertNotIn("Traceback", proc.stderr)

        def test_digest_python_symbols(self):
            with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as handle:
                handle.write("import os\n\nclass A:\n    pass\n\ndef f():\n    return 1\n")
                path = handle.name
            try:
                proc = run_cli(["digest", path])
                self.assertIn("class A", proc.stdout)
                self.assertIn("def f", proc.stdout)
            finally:
                os.unlink(path)

        def test_digest_directory(self):
            with tempfile.TemporaryDirectory() as folder:
                Path(folder, "a.txt").write_text("x", encoding="utf-8")
                Path(folder, "sub").mkdir()
                Path(folder, "sub", "b.txt").write_text("y", encoding="utf-8")
                proc = run_cli(["digest", folder])
                self.assertEqual(proc.returncode, 0)
                self.assertIn("a.txt", proc.stdout)
                self.assertNotIn("Traceback", proc.stderr)

        def test_digest_missing(self):
            self.assertEqual(run_cli(["digest", "/nonexistent/path"]).returncode, 2)

        def test_digest_max_symbols(self):
            with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as handle:
                for i in range(60):
                    handle.write(f"def f{i}():\n    pass\n")
                path = handle.name
            try:
                proc = run_cli(["digest", path, "--max-symbols", "5"])
                self.assertIn("另有", proc.stdout)
            finally:
                os.unlink(path)

        def test_trim_keep_all_errors_flag(self):
            payload = "".join(f"ERROR e{i}\n" for i in range(500))
            proc = run_cli(["trim", "--max-lines", "10", "--keep-all-errors"], payload)
            kept = [line for line in proc.stdout.splitlines() if line.startswith("ERROR e")]
            self.assertEqual(len(kept), 500)

        def test_count_file(self):
            with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as handle:
                handle.write("hello world\n")
                path = handle.name
            try:
                proc = run_cli(["count", path])
                self.assertEqual(proc.returncode, 0)
                self.assertIn("token", proc.stdout)
            finally:
                os.unlink(path)

    class InstallTest(unittest.TestCase):
        def test_installs_skill_and_script(self):
            with tempfile.TemporaryDirectory() as scope:
                proc = run_cli(["install", "--scope", scope])
                self.assertEqual(proc.returncode, 0)
                skill = Path(scope, "Eeyi", "SKILL.md")
                script = Path(scope, "Eeyi", "eeyi.py")
                self.assertTrue(skill.is_file())
                self.assertTrue(script.is_file())
                body = skill.read_text(encoding="utf-8")
                self.assertIn(str(script), body)
                self.assertNotIn("{{EEYI}}", body)
                run = subprocess.run([sys.executable, str(script), "trim"], input="a\n", capture_output=True, text=True)
                self.assertEqual(run.returncode, 0)

        def test_install_idempotent(self):
            with tempfile.TemporaryDirectory() as scope:
                self.assertEqual(run_cli(["install", "--scope", scope]).returncode, 0)
                second = run_cli(["install", "--scope", scope])
                self.assertEqual(second.returncode, 0)
                self.assertNotIn("Traceback", second.stderr)

        def test_install_rejects_file_scope(self):
            with tempfile.TemporaryDirectory() as folder:
                target = Path(folder, "not-a-dir")
                target.write_text("x", encoding="utf-8")
                proc = run_cli(["install", "--scope", str(target)])
                self.assertEqual(proc.returncode, 2)
                self.assertNotIn("Traceback", proc.stderr)

        def test_install_check_reports_stale(self):
            with tempfile.TemporaryDirectory() as scope:
                self.assertEqual(run_cli(["install", "--scope", scope]).returncode, 0)
                fresh = run_cli(["install", "--scope", scope, "--check"])
                self.assertEqual(fresh.returncode, 0)
                self.assertIn("一致", fresh.stdout)
                Path(scope, "Eeyi", "eeyi.py").write_text("# old\n", encoding="utf-8")
                stale = run_cli(["install", "--scope", scope, "--check"])
                self.assertEqual(stale.returncode, 1)
                self.assertIn("过期", stale.stdout)

    suite = unittest.TestSuite()
    loader = unittest.TestLoader()
    for case in (CoreTest, CliTest, InstallTest):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
