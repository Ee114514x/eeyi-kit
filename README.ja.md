# Eeyi

[中文](README.md) · [English](README.en.md) · **日本語**

[![test](https://github.com/Ee114514x/eeyi-kit/actions/workflows/test.yml/badge.svg)](https://github.com/Ee114514x/eeyi-kit/actions/workflows/test.yml)

長いログや大きなファイルを AI アシスタントが読むときの、同じ問題に対する 2 つの依存ゼロ・単一ファイルツールです。コンテキストを節約しつつ、判断に必要な情報は落としません。

| ツール | 役割 | 可逆 |
|---|---|---|
| **Eeyi** | 長い出力を要約に圧縮：先頭・末尾、エラー行、スタックトレースを保持し、繰り返し行を畳む。ディレクトリ `digest`、JSON 構造、トークン推定も提供 | いいえ |
| **EeyiRecall** | 圧縮しても元の行番号と省略した元範囲を保持し、任意の範囲を正確に取り戻せる | はい |

同じリポジトリにあるのは、**同じ入力に対して連携するから**です。Eeyi が「追う価値があるか」を判断し、EeyiRecall が「追うと決めたら原文を正確に取り戻せる」ことを保証します。インストールは従来どおり 2 つの独立したスキルで、アシスタントが状況に応じて使い分けます。

> コマンドの出力は現在中国語のみです。各マーカーの意味は[出力の読み方](#出力の読み方)を参照してください。

## 連携の例

200 行のログ（接続失敗 5 件とスタックトレース 1 件）。まず要約を見て、掘り下げる価値があるか判断します:

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

追うと決めたら、可逆版に切り替えます。残した行には元の行番号が付き、省略部分には元の範囲が明示されます:

```text
$ python3 EeyiRecall/recall.py trim < demo.log
recall trim: 200→36 行, ~882→239 token (+73%)
recall: 可回读区间 13-56, 60-62, 64-119, 122-122, 124-188
1| INFO step 1 ok
2| INFO step 2 ok
...
```

範囲を指定して逐語的に取り戻します:

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

ファイルではなくパイプの場合は `--save` で原文を保存しておきます:

```bash
./build.sh 2>&1 | python3 EeyiRecall/recall.py trim --save build.log
# stderr: recall: 原文已存 build.log，可用 fetch build.log <区间> 回读
python3 EeyiRecall/recall.py fetch build.log 401-430
```

およそ 20 行未満の入力では `未压缩`（未圧縮）と表示されます。行番号の接頭辞のコストが削減分を上回るため、原文をそのまま読むほうが速いという意味です。

## インストール

各スクリプトが自分自身をスキルとして登録できます:

```bash
python3 Eeyi/eeyi.py install          # 最寄りの .aicode/skills を探す
python3 EeyiRecall/recall.py install  # 同上、--scope で指定も可
```

ソースを直して再インストールを忘れるのが最も多い落とし穴なので、コピーが最新かいつでも確認できます:

```bash
python3 Eeyi/eeyi.py install --check          # 一致 / 过期 / 未安装
python3 EeyiRecall/recall.py install --check
python3 Eeyi/eeyi.py --version                # 両方ともバージョンを持つ
```

スキルとして入れなくても、通常のコマンドとして使えます。内容は stdout、統計は stderr に出ます。

## 出力の読み方

| マーカー | 意味 |
|---|---|
| `<首个错误：第 N 行 …>` | 最初のエラー行（番号は畳んだ後の並びであり、原文の行番号ではない） |
| `<错误位置：file:line>` | エラー行から抽出した位置 |
| `<省略 N 行>` / `<省略 a-b 行>` | 省略した行（Eeyi は件数、EeyiRecall は `fetch` できる元範囲） |
| `<第 x-y 行与第 a-b 行相同>` | x-y 行は a-b 行のブロックの繰り返し |
| `未压缩` | 出力が入力より小さくない。原文を読むべき |
| `可回读区间` | `fetch` で逐語的に取り戻せる範囲 |

## 構成

```
Eeyi/
  eeyi.py            メイン（trim / digest / json / count / install / bench / selftest）
  GUIDE.md           ワークフロー全体、パラメータ方針、C1–C17 の落とし穴（中国語のみ）
  verify_skill.py    スキル本文の網羅性とコストの検証
  verify_usage.py    17 の実装上の約束を証拠付きで出力
  verify_tokens.py   tiktoken でトークン推定を較正（任意依存）
EeyiRecall/
  recall.py          メイン（trim / fetch / map / batch / check / install / selftest）
```

## 2 つの必ず守るルール

1. **要約は索引であり、原文ではない。** コード修正、パッチ適用、綴りやインデントの確認は必ず `fetch` で原文を取る。
2. **Eeyi の `<首个错误：第 N 行>` は原文の位置特定に使えない** — 畳んだ後の並びの番号です。位置は `<错误位置：file:line>`、読み戻しは Recall の `fetch` を使う。

その他の制限（トークン推定の誤差、digest が関数本体を落とす、長い行の切り詰めなど）は [Eeyi/GUIDE.md](Eeyi/GUIDE.md)（中国語）に再現手順つきで記載しています。[EeyiRecall](EeyiRecall/recall.py) の SKILL テンプレートにも要点があります。

## 動作環境

Python 3.8 以上、サードパーティ依存なし。`verify_tokens.py` のみ任意で `tiktoken` を使用します。CI は 3.9 と 3.12 をカバーしています。

## テスト

```bash
python3 Eeyi/eeyi.py selftest         # 66 件
python3 EeyiRecall/recall.py selftest # 24 件
python3 Eeyi/verify_usage.py          # 17 の実装上の約束を証拠付きで出力
```

境界ケース：空入力、20 万文字の単一行、バイナリ / 非 UTF-8、CRLF、ANSI エスケープをカバーしています。

繰り返し区間の検出は、逐語比較の素朴な実装を基準としてランダム入力と adversarial な入力で突き合わせているため、性能改善が畳む対象を変えることはありません。さらに、出力中の `<第 x-y 行与第 a-b 行相同>` が原文で本当にそのブロックの整数倍の繰り返しになっているかを検証する不変条件テストがあります。

## ライセンス

MIT。[LICENSE](LICENSE) を参照。© 2026 Eeyi09。
