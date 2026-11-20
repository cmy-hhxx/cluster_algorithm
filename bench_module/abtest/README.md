# abtest — 把 bench 档位装配成一次发版前后对比的「发版前」口径

`make_before_release.py` 干一件事：**拿 bench 的某一档，切出一份 AB 测试的 `02_before_release`。**

不重跑归因、不重跑聚类、不写任何上游目录。纯切片，30 秒级。

```
bench tier_1000.jsonl ─┬─→ 01_targets/questions.jsonl            目标 trace 清单
                       ├─→ 02_before_release/inputs/<date>.jsonl  eval_input 原行，逐字节不改
                       ├─→ 02_before_release/attribution/…        归因原行，逐字节不改
                       └─→ 02_before_release/cluster_tree/…      全局 item_index 按 trace 切片
                                      └─→ 00_manifest/ 05_compare/ README.md round.json
```

## 为什么不重跑

进 bench 的前提是「每条 problem 都能 join 到分类法叶」，所以这 1000 条的归因和落点必然齐全。
前两轮（1106 / 1112）要补漏评测 + 增量聚类，是因为那两轮是**人工挑 P0/P1 且未筛 join**；
现在 bench 本身就是从「归因 + 聚类」的联合产物里抽的，天然自洽。

## 跑法

```bash
python3 make_before_release.py \
    --bench-dir /mnt/aime_eval_ssd/3_dynamic_bench \
    --eval-input /mnt/aime_eval_ssd/1_aime/eval_input \
    --eval-output /mnt/aime_eval_ssd/1_aime/eval_output \
    --taxonomy /mnt/aime_eval_ssd/code/2_cluster_algorithm/taxonomy/inventory.json \
    --out /mnt/aime_eval_ssd/1_aime/ab_test/2_version1119_test \
    --model aime_expert --tier 1000
```

脚本会自己落到 `--out/99_provenance/`（含 `RUN.md` 与 `run.log`）。

## 三条对齐规则（脚本自动做，报表里逐条计数）

| 现象 | 处理 |
|---|---|
| 同一 `trace_id` 被多次评测，`item_index` 里留了超出本次归因条数的旧行 | 按 `problem_index < 归因条数` 截断，丢弃行数写 `stale_rows_dropped` |
| 树里那条 problem 的 severity 与本次归因不同 | severity 以**归因为准**，原值留 `severity_at_ingest` |
| 叶子行的 `src_rel` 指向同一 trace 的另一次评测 | 保留叶子（唯一可得），标 `leaf_vintage=other` |
| 原文只在另一模型的 `eval_input` 目录下 | 照 trace_id 取，`input_model` / `input_file` 记出处 |

这三条不是脚本的偏好，是上游的客观状态：聚类树「历史条目不重分类」，而归因文件后来被重写、
部分旧版本已被清理，所以**无法把每一条 problem 都追回到当初那一版归因**。

## 边界

- 只装配**发版前**（口径①）。口径②的重测输入要走批量推理的 slim 包格式，由推理侧出，
  不是本地切片能伪造的；③ 线上真实数据落盘后按同结构补。
- 换 `--tier` 会让 `01_targets` 的 trace 集合变，已落盘的 ②③ 必须重新对齐，别混用。
