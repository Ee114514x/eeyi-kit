# Eeyi

[![test](https://github.com/Ee114514x/eeyi-kit/actions/workflows/test.yml/badge.svg)](https://github.com/Ee114514x/eeyi-kit/actions/workflows/test.yml)

Eeyi 系列：两个零依赖的单文件工具，解决同一个问题——AI 助手读长内容时既要省上下文，又不能丢掉关键信息。

| 工具 | 作用 | 可逆 |
|---|---|---|
| **Eeyi** | 把长输出压成摘要：保留头尾、错误行、堆栈块，重复行折叠；另有目录 digest、JSON 结构、token 估算 | 否 |
| **EeyiRecall** | 压缩时保留原文行号与省略区间，需要时按区间精确回读 | 是 |

放在一个仓库，是因为它们对同一份输入配合工作：**Eeyi 判断值不值得追，EeyiRecall 保证要追就能精确拿回来**。安装仍是两个独立技能，助手按场景分别触发。

## 配合用法

同一份 200 行日志（5 条连接失败 + 一条堆栈）。先看摘要，判断要不要深挖：

```text
$ <命令> 2>&1 | python3 Eeyi/eeyi.py trim
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

决定要追时，换可回读的版本——每行带原文行号，省略处标原文区间：

```text
$ python3 EeyiRecall/recall.py trim < demo.log
recall trim: 200→36 行, ~882→239 token (+73%)
recall: 可回读区间 13-56, 60-62, 64-119, 122-122, 124-188
1| INFO step 1 ok
2| INFO step 2 ok
...
```

按区间拿回原文，逐字可用：

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

如果是管道而不是文件，加上 `--save` 把原文落盘，回读才有目标：

```bash
./build.sh 2>&1 | python3 EeyiRecall/recall.py trim --save build.log
# stderr: recall: 原文已存 build.log，可用 fetch build.log <区间> 回读
python3 EeyiRecall/recall.py fetch build.log 401-430
```

## 安装

各自的脚本都能把自己注册成技能：

```bash
python3 Eeyi/eeyi.py install          # 自动找最近的 .aicode/skills
python3 EeyiRecall/recall.py install  # 同上，或用 --scope 指定
```

装完之后可以随时核对技能目录里的副本是不是最新（源码改了但没重装是最常见的坑）：

```bash
python3 Eeyi/eeyi.py install --check          # 一致 / 过期 / 未安装
python3 EeyiRecall/recall.py install --check
python3 Eeyi/eeyi.py --version                # 两边都有版本号
```

不装成技能也能直接当命令用，管道里读 stdout 即可，统计信息走 stderr。

## 目录

```
Eeyi/
  eeyi.py           主程序（trim / digest / json / count / install / bench / selftest）
  GUIDE.md          完整工作流、参数策略、C1–C17 读输出的坑与取证
  verify_skill.py   技能正文覆盖与成本校验
  verify_usage.py   17 条实现约定，逐条输出证据
  verify_tokens.py  用 tiktoken 校准 token 估算（可选依赖）
EeyiRecall/
  recall.py         主程序（trim / fetch / map / batch / check / install / selftest）
```

## 两条硬规则

1. **摘要是索引，不是原文。** 改代码、打补丁、核对拼写与缩进，一律 `fetch` 原文。
2. **Eeyi 的 `<首个错误：第 N 行>` 不能用来定位原文**，那是折叠后序列的行号；定位看 `<错误位置：文件:行号>`，回读用 Recall 的 `fetch`。

其余限制（token 估算误差、digest 丢函数体、长行截断等）在 [Eeyi/GUIDE.md](Eeyi/GUIDE.md) 里逐条写了取证方式，在 [EeyiRecall](EeyiRecall/recall.py) 的 SKILL 模板里也有对应说明。

## 环境

Python 3.8+，零第三方依赖。`verify_tokens.py` 需要可选的 `tiktoken`。CI 覆盖 3.9 与 3.12。

## 自检

```bash
python3 Eeyi/eeyi.py selftest         # 65 项
python3 EeyiRecall/recall.py selftest # 23 项
python3 Eeyi/verify_usage.py          # 17 条实现约定，输出证据
```

边界用例已覆盖：空输入、超长单行（20 万字符）、二进制/非 UTF-8、CRLF、ANSI 转义。

重复区间检测有一份逐字比对的朴素实现作为基准，随机与对抗输入上逐一比对，保证性能优化不改变任何折叠结果；另有一条不变量测试，逐条核对输出里的「与第 x-y 行相同」在原文中确实是该块的整数倍重复。

## 许可

MIT，见 [LICENSE](LICENSE)。© 2026 Eeyi09。
