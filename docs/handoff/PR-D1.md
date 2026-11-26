# D1 真实数据冻结关口

**正式冻结待验收者补齐标注。** 该工作区有 16 段开发视频、8 段留出视频，以及 D0 的 6 段开发视频对应的 24 道已核验问题。无包审计逐文件检查授权、字节数、SHA-256、视频尺寸、主流时间轴和源分组。正式源清单 SHA-256 为 `430a75e93bdf862e9d48e2926b5256ff83ebf379d36d670cd5bea4c3b793295e`。缺少私包时程序输出聚合差额，退出 1。[项目计划第七节](../../plans/视频理解Agent_项目计划书.md) · [D0](PR-M0.1.md)

| 项目 | 已核对 | 正式冻结还缺 |
| --- | --- | --- |
| 开发常规 QA | 24 题，其中 20 可答、4 不可答 | 10 段开发视频的 40 题 |
| 留出常规 QA | 8 段视频的源身份和磁盘哈希 | 32 题，其中 24 可答、8 不可答 |
| 缓慢事件 | 已冻结计分口径及包验证程序 | 8 段留出视频上的 20 个可见事件，逐事件起止、四张边界帧及 20 个独立定位请求 |
| 素材适用性 | 14 段 H.264、10 段 HEVC。开发 5 段、留出 3 段短于 30 秒 | 验收者记录短片取舍及教程系列、同素材关系，核对重复片段和事件是否可标 |
| 参考答案复核 | 单独 D1 冻结包及复核回执尚未提供 | 按计划记录第二审查者覆盖比例，目标至少 20%。无法达到时如实披露 |

现有基准原始标注的聚合行数为开发 55、留出 28。这些行缺少完整的项目可答性、证据区间和拒答依据。D0 原冻结以容器时长为准，另有 **1 条引用的终点超出主视频流 54 ms**。D1 需要验收者核对实际终点。所有留出题文、答案及私有证明图像均由验收者本地保管。

## 本地验证

工程侧可在仓库根目录执行无包审计。正式冻结时验收者将私包和证明帧保存在本机，仅传递聚合回执。

```powershell
uv run --no-sync python -m eval.d1
uv run --no-sync python -m eval.d1 --packet <私包JSON> --receipt <回执JSON>
```

验证成功退出 0。回执绑定原素材清单、D0 清单及题集、私包、验证程序和评测脚本的 SHA-256。评测机器须保留冻结时使用的精确文件字节。回执通过同目录临时文件原子发布，可重放相同内容。若目标已有另一份回执，返回退出 1 并保留原内容。

## 私包内容

| 私包字段 | 验收者填写的内容 |
| --- | --- |
| `groups` | 以现有 24 个 artifact_id 为键，每段填写 `series` 与 `related` 的稳定分组。相同源视频、原始 ID、系列、同素材不能跨开发和留出。 |
| `questions` | 72 条，固定在 10 段新增开发视频和 8 段留出视频，每段四条。沿用 D0 的 question_id、video_id、question_text、category、verification_basis、answerability、reference_answer、expected_gaps、evidence_refs。可答题的原始证据窗用于首次召回评测；不可答题填 null 答案、可核证据与缺口。 |
| `events` | 20 条，含不同的 event_id、video_id、label、start_ms、end_ms、start_frames、end_frames。每个边界由两个有序真实帧包围。每张图提供 path、sha256、source_sha256、raw_pts、time_base、origin_pts、origin_time_base、actual_pts_ms。时间基为整数分子分母对；图像是所给 PTS 从原主视频流直接解码的原生尺寸无损 RGB PNG。路径相对私包目录。 |
| `requests` | 20 个独立 request_id，一对一绑定 event_id 和相同 video_id，含 `mode=event_localization` 及 query_text。 |
| `review` | coverage_note 记录人员、复核覆盖或实际限制。含短片时另填 short_clip_decision，写明保留的依据或经审定的替代方案。程序校验材料结构；事件语义及人工复核仍由验收者判断。 |
| `seed_evidence_corrections` | 仅在复核原 D0 超过主视频终点的条目时提供。每条列出 question_id、ref_index（从 0 开始）、original_end_ms、end_ms 及 reason。仅收窄该条越界终点，保留原时间窗、题文、答案和冻结文件。 |

## 已冻结的计分口径

首次检索取前 5 个候选，可答题正确至少 20/24、首次召回至少 22/24；不可答题适当拒答至少 7/8。可核事实支持率至少 90%，引用完整性 100%，两者报告原始计数，空分母不能过关。20 个事件各有一个定位请求，以同标签和正重叠匹配，优先最高 IoU，同分取更早起点、终点。起止误差均不超过 2000 ms 才得分，至少 17/20 通过。未检出、请求失败或预算不足计零，额外预测另计误检。[计分实现](../../eval/d1_metrics.py)

```powershell
uv run --no-sync pytest -q -p no:cacheprovider --disable-socket --allow-unix-socket tests/integration/test_d1_freeze.py tests/integration/test_d1_integrity.py tests/integration/test_d1_seed_correction.py tests/unit/test_d1_metrics.py
```

15 项通过。最初的 9 项在签名占位和缺漏 CLI 上退出 1，测试冻结于 `ef9b900`，diff SHA-256 为 `70730c7159e57aa6568d4a74def97c996752d23b28f25ba756391e041d0a3fb3`，实现为 `0e3fbef`。独立审查确认 `1b7e594`（`af1ab6671bc3c76e714fb1968f78c1e9e2236ed155d5d04d86ee375be13a57c1`）只修改真实时间基的等价类型表达及两个列表注解，原断言保持一致。

后续 4 项回归在同一事件换 ID、授权被否、虚报时长或虚报尺寸时未抛错，冻结于 `b55c0cd`（`dbb8c1130963d27ef1145f67b7139b5285f4fc6a4a615f7ebeb816f2995ea294`）。2 项终点修正回归冻结于 `e11e42e`（`800fc9fc275a44bde2516c3793ebd822c552c548f3da68a0f2839bc1edf2a1a7`）。`3b49d32` 修正了这些行为。独立审查用自造的 24 个 FFmpeg/PyAV 合成视频跑通完整成功回执、同内容重放、不同内容拒绝；另用带音尾视频验证超时引用必须显式校正。真实 D1 质量评分待正式冻结并运行 M6。[M3 交接](PR-M3.md)
