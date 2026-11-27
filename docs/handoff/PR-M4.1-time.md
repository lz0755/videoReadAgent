# M4.1 指定时点的检索兼容性

磁盘检索和原切片共用中英文秒点、区间以及持续时间的意图解析。对于“在 1 秒和 8.5 秒”一类问题，SQLite 在已发布索引的一个读快照内按当前资产和分析取数，优先保留两个端点各自的画面观察及原始转写。画面邻近度使用该观察所引用帧的真实 PTS，语音邻近度使用原转写的半开区间。显式时间范围与原有向两边延伸一秒的候选范围求交。“是否在 80 秒内完成”继续走普通词项检索。[M4.1](PR-M4.1.md) · [原切片复测](PR-M1.5-A2-retest.md) · [共同解析](../../src/video_agent/application/retrieval.py)

每个端点的每种来源最多读取请求的 `limit` 行，词项检索也先在绑定范围内取有限候选。先覆盖两个端点，再补另一模态、词项候选和剩余邻近文档，按文档去重后返回至多 10 项。索引结构、版本和 D1 评测阈值沿用原合同。[查询实现](../../src/video_agent/adapters/storage/search.py)

## 冻结和复验

基线 `e642386`，Step A `f00705d`，Step B `be2a8a6`。测试文件差异 SHA-256 为 `0e2dd259c07e51c73dbc6e241690aeb99593b88816f768a69980f40dd524f61d`，测试自冻结后差异为空。夹具使用 FFmpeg 生成 10 秒视频，以 PyAV 从 925、4800、8400 ms 解码出实际帧 1000、5000、8500 ms，给两个资产的共三个分析各发布 7 条文档。FTS 和外键自检通过。原实现对两端、单点和夹紧时间范围后的原始转写返回空，6 failed、2 passed，退出 1。独立审查者复跑确认红灯后冻结。[回归测试](../../tests/integration/test_search_time_evidence.py)

```powershell
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --disable-socket --allow-unix-socket tests/integration/test_search_time_evidence.py --tb=line
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --disable-socket --allow-unix-socket tests/integration/test_search_time_evidence.py tests/integration/test_chinese_search.py tests/unit/test_slice_time_evidence.py tests/unit/test_slice_duration_intent.py --tb=line
.venv/Scripts/ruff.exe check src/video_agent/adapters/storage/search.py src/video_agent/application/retrieval.py src/video_agent/application/slice.py tests/integration/test_search_time_evidence.py
.venv/Scripts/ruff.exe format --check src/video_agent/adapters/storage/search.py src/video_agent/application/retrieval.py src/video_agent/application/slice.py tests/integration/test_search_time_evidence.py
.venv/Scripts/mypy.exe src/video_agent/adapters/storage/search.py src/video_agent/application/retrieval.py src/video_agent/application/slice.py tests/integration/test_search_time_evidence.py --follow-imports=normal
git diff --exit-code f00705d be2a8a6 -- tests/integration/test_search_time_evidence.py
```

联合回归 31 passed，独立审查者复验同样为 31 passed，并核对了查询作用域、实际多模态证据及冻结差异。真实索引另测 9 位、100 位整数秒，均返回 0 行，260 位被查询长度上限拒绝。Ruff、格式、mypy 和冻结差异检查最终均退出 0。第一次 Ruff 检查指出两行 SQL 超过 100 字符，拆行后通过。此次离线测试的模型请求、外发图片和真实费用均为 0。

## 三段授权开发切片的首次检索重放

使用原 M1.5 联合复测所引用的本地证据导出，并从既有清单独立核对三段授权源文件哈希及 34 张帧图的原始目录哈希。源文件、导出和旧数据库均来自 `data/reports/m1_5_openai_r2`。逐题按原 12 道问题查询，开发标注仅用于统计。[三段开发清单](../../eval/manifests/slice.jsonl) · [开发标注](../../eval/annotations/dev_seed/dev_seed_questions.json)

旧导出共有 49 条视觉模型观察、21 条原始转写。首次原样重放时，2 段包含以画面引用证明听觉事实的历史观察，M3 校验返回 `INVALID_CITATION`。逐条用相同的 `validate_window` 检查全部观察，保留 45 条、剔除 4 条，保留全部转写、三个视频及全部问题。在临时磁盘目录重新导入源片、登记哈希核对过的帧，用 `fuse_window`、`StageCache.commit` 和 `SQLiteSearchIndex.publish` 发布 1 个完整与 2 个 partial 索引，共 87 条检索文档。原始导出、参考答案和标注保留原样。

| 聚合项 | 结果 |
| --- | --- |
| 首次 `SQLiteSearch.search(..., limit=5)` 取得候选 | 12/12 题，包括 9 可答、3 不可答 |
| 返回候选中的引用逐次解析到原帧或转写 | 57/57 |
| 可答题至少一个标注时段内有相应模态的候选原始证据 | 9/9，其中含显式时点的 3/3 |
| 可答题每个标注时段所列的全部模态均有候选原始证据 | 8/9 |

画面统计使用真实帧点落入标注半开区间，语音统计使用原始转写与标注有正长度交集。混合模态在最后一项要求两类证据均出现。9/9 和 8/9 衡量时间与模态覆盖。该重放未复核首次候选的语义充分性，也未重新调用 Planner、ASR 或视觉模型。M6 的 22/24 首次语义召回门槛和其余留出指标仍待正式评测。
