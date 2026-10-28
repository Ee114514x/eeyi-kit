# Eeyi

[中文](README.md) · **English** · [日本語](README.ja.md)

[![test](https://github.com/Ee114514x/eeyi-kit/actions/workflows/test.yml/badge.svg)](https://github.com/Ee114514x/eeyi-kit/actions/workflows/test.yml)

Two zero-dependency, single-file tools for one problem: when an AI assistant reads a long log or a large file, it needs to spend less context without losing what actually matters.

| Tool | What it does | Reversible |
|---|---|---|
| **Eeyi** | Compresses long output into a summary: keeps head/tail, error lines and traceback blocks, folds repeated lines; also offers directory `digest`, JSON shape and token estimates | No |
| **EeyiRecall** | Compresses while keeping original line numbers and the exact original ranges it omitted, so any range can be fetched back | Yes |

They share a repository because they work on the same input together: **Eeyi decides whether a failure is worth chasing, EeyiRecall guarantees you can get the exact text back**. They are still installed as two separate skills, so the assistant picks whichever fits the situation.

> Command output is currently Chinese-only. See [Reading the output](#reading-the-output) for what each marker means.

## Working together

The same 200-line log (5 connection failures plus one traceback). First get a summary and decide whether it is worth digging in:

```text
$ <command> 2>&1 | python3 Eeyi/eeyi.py trim
eeyi trim: 200→38 行, ~882→236 token (+73%)
<首个错误：第 57 行 ERROR worker 57: connection refused to db-1>
<错误位置：app/jobs.py:88>
INFO step 1 ok
...
ERROR worker 57: connection refused to db-1
ERROR worker 58: connection refused to db-1
ERROR worker 59: connection refused to db-1
<省略 3 行>
ERROR worker 63: connection refused to db-1
```

Once you decide to chase it, switch to the reversible variant — every kept line carries its original line number, every omission names an original range:

```text
$ python3 EeyiRecall/recall.py trim < demo.log
recall trim: 200→36 行, ~882→239 token (+73%)
recall: 可回读区间 13-56, 60-62, 64-119, 122-122, 124-188
1| INFO step 1 ok
2| INFO step 2 ok
...
```

Fetch the range back, byte-exact:

```text
$ python3 EeyiRecall/recall.py fetch demo.log 119-124
recall fetch: 119-124 行，共 6 行
119| INFO step 119 ok
120| ERROR worker 120: connection refused to db-1
121| Traceback (most recent call last):
122|   File "app/jobs.py", line 88, in run
123|     raise RuntimeError("db down")
124| INFO step 124 ok
```

For pipes rather than files, add `--save` so there is something to fetch from later:

```bash
./build.sh 2>&1 | python3 EeyiRecall/recall.py trim --save build.log
# stderr: recall: 原文已存 build.log，可用 fetch build.log <区间> 回读
python3 EeyiRecall/recall.py fetch build.log 401-430
```

Inputs shorter than roughly 20 lines will report `未压缩` (not compressed): the line-number prefix costs more than the lines saved, so reading the raw text is simply faster.

## Install

Each script can register itself as a skill:

```bash
python3 Eeyi/eeyi.py install          # finds the nearest .aicode/skills
python3 EeyiRecall/recall.py install  # same, or pass --scope
```

Check whether the installed copy is still current — editing the source and forgetting to reinstall is the most common trap:

```bash
python3 Eeyi/eeyi.py install --check          # 一致 / 过期 / 未安装
python3 EeyiRecall/recall.py install --check
python3 Eeyi/eeyi.py --version                # both scripts carry a version
```

Both scripts also work as plain commands: content goes to stdout, statistics to stderr.

## Reading the output

| Marker | Meaning |
|---|---|
| `<首个错误：第 N 行 …>` | First error line (number refers to the folded sequence, not the source) |
| `<错误位置：file:line>` | Location parsed out of the error line |
| `<省略 N 行>` / `<省略 a-b 行>` | Lines omitted (Eeyi: count; EeyiRecall: an original range you can `fetch`) |
| `<第 x-y 行与第 a-b 行相同>` | Lines x-y are repetitions of the block at a-b |
| `未压缩` | Output is not smaller than the input; read the source instead |
| `可回读区间` | Ranges that `fetch` can still return verbatim |

## Layout

```
Eeyi/
  eeyi.py            main program (trim / digest / json / count / install / bench / selftest)
  GUIDE.md           full workflow, parameter strategy, C1–C17 pitfalls (Chinese only)
  verify_skill.py    skill text coverage and cost checks
  verify_usage.py    17 implementation guarantees, one with evidence each
  verify_tokens.py   calibrate token estimates with tiktoken (optional dependency)
EeyiRecall/
  recall.py          main program (trim / fetch / map / batch / check / install / selftest)
```

## Two hard rules

1. **A summary is an index, not the source.** Editing code, applying patches, checking spelling or indentation: always `fetch` the original text.
2. **Eeyi's `<首个错误：第 N 行>` cannot be used to locate source lines** — that number comes from the folded sequence. Use `<错误位置：file:line>` to locate, and Recall's `fetch` to read.

Other limits (token estimate error, digest dropping function bodies, long-line truncation) are documented one by one with reproduction steps in [Eeyi/GUIDE.md](Eeyi/GUIDE.md) (Chinese), and summarised in the SKILL template of [EeyiRecall](EeyiRecall/recall.py).

## Requirements

Python 3.8+, no third-party dependencies. `verify_tokens.py` optionally needs `tiktoken`. CI covers 3.9 and 3.12.

## Tests

```bash
python3 Eeyi/eeyi.py selftest         # 66 tests
python3 EeyiRecall/recall.py selftest # 24 tests
python3 Eeyi/verify_usage.py          # 17 implementation guarantees with evidence
```

Edge cases covered: empty input, a single 200k-character line, binary / non-UTF-8 data, CRLF, ANSI escapes.

Repeat detection is validated against a naive byte-exact reference implementation on randomised and adversarial inputs, so performance work cannot change which blocks get folded. A separate invariant test checks every `<第 x-y 行与第 a-b 行相同>` claim against the source text.

## License

MIT, see [LICENSE](LICENSE). © 2026 Eeyi09.
