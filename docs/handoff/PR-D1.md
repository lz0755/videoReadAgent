# D1 真实数据冻结关口

**D1 已于 2026-11-27 冻结。** 正式验证器对 24 段真实授权视频、96 道常规题及 20 个带边界帧的独立事件定位请求返回 `verified`。M4 只使用开发集，留出标注与证明帧由验收者本地保管。[冻结回执](../../eval/manifests/d1_freeze_receipt.json) · [M3 签收](PR-M3.md)

| SHA-256 绑定项 | 摘要 |
| --- | --- |
| 回执原始字节 | `c7df675473fe97ced31e7358e6299d1920b4bc363765a0d12ff12c8653b36fe4` |
| 独立复核汇总 | `86bfeb6e5b9c1f0bea9a17331aea36cd6de9c3cc4e02654b41ec973208562da4` |
| D1 源清单 | `dc78aac23451bb1e7f9824457b7c3e2692dd63f8bae8c6d7f9a55a2c21b226dc` |
| 本地私包 | `5386624419cbaae51abf549c8e90509af4ad90fa0b09a440ab61c3f6fd148625` |

## 开发集与留出集

| 冻结项 | 数量与依据 |
| --- | --- |
| 开发常规题 | 16 段、64 题，52 道可答、12 道不可答。D0 保留 24 题，本轮为其余 10 段增加 40 题，其中 32 道可答、8 道不可答。 |
| 留出常规题 | 8 段、32 题，24 道可答、8 道不可答。每段 4 题，两个隔离的标注子代理各负责 4 段。 |
| 留出事件 | 8 段中共 20 个可见事件，各有 1 个独立定位请求。起、止边界各有前后两帧，共 80 个边界帧引用。原尺寸 RGB PNG 可按主视频流 PTS 从原片重解码。 |

开发侧逐文件校验了 16 段视频的 SHA-256 和 64 题的引用范围。新增 40 题的 55 个证据区间中，视觉证据含 105 个不同的原帧，均按原始 PTS、时间基、时间原点和 RGB 哈希复现。两段音频证据另保留真实音轨时间与 PCM 哈希。M4 的本地输入为 `data/d1-dev/questions_64.json`，SHA-256 为 `c722af63b710b092fc6e5c099898728b650caaa905d64c0ff25f24386528b9c7`。开发证据索引 `data/d1-dev/source_witnesses.json` 的 SHA-256 为 `6cc2fc6ce8c10576f139b2e750a2de45d6b195c6b8e3fe11f790cdd7c9b504b2`。

## 素材及 D0 时间复核

按源视频、教程系列和同素材关系检查时，验收者在原清单中发现 1 对文件哈希不同、教程系列与课题相同的跨集合片段。开发片曾用于 M0。验收者先固定候选顺序，从同一授权基准盲选替代留出片，再用来自实际 PTS 的 2421 个帧指纹核查最终 24 段的 276 个视频对，其中 128 对跨集合。对筛出的 11 对跨集合候选逐一回看原片。最终源清单包含 14 段 H.264、10 段 HEVC。M0 原清单仍可按当时的哈希复核，D1 使用独立的源清单快照。[D1 源清单](../../eval/manifests/d1_source_manifest.json) · [M0 原清单](../../experiments/manifests/benchmark_subset_manifest.json)

D0 开发题 `vmme_001_q4` 的第一个证据区间原为 `[451838, 507054)` ms。最后一个主视频帧从 506960 ms 显示至 507000 ms，余下 54 ms 为音轨尾部。D1 明确记录修正区间 `[451838, 507000)` ms，并在开发题集应用同一修正。回执中的 `seed_refs_requiring_endpoint_review=1` 统计的是原始 D0 题集。原始题集 SHA-256 为 `35c27d9b962e3ab30a4b07bd1a44bfcc7e8a41816c0068cdb5b07dc5a61bd339`。[D0 交接](PR-M0.1.md)

## 独立复核

第三个隔离验收子代理按事先登记的样本回看原始证据，覆盖全部 24 段视频及 32/96 道常规题（33.33%）。其中 6 道来自 D0，10 道来自新增开发集，另有 16 道来自留出集。按可答性计算，已复核 22/76 道可答题、10/20 道不可答题。留出集单列为 8/24 道可答题和全部 8/8 道不可答题。抽中的常规题均通过。验收者还将开发侧 64 道题逐字段与冻结记录比对，全部一致。

验收者复核了全部 20 个事件和 80 个边界帧引用，并独立从原片提取了 100 个上下文帧。1 个淡出事件在初标终点之后仍有可见内容，原标注者修正了终点与对应两帧，验收者回看通过。8 段不足 30 秒的短片均有逐片决定，开发 5 段、留出 3 段均能支持各自四道题，三段留出短片也均能包围有明确起止的事件，因此全部保留。产品媒体规模按计划第 3.1 节验收。[项目计划](../../plans/视频理解Agent_项目计划书.md)

本次由用户授权的 Codex 子代理在不同会话中初标和复核。各会话共享本机工作区，本轮有记录的真人独立复核为 0/96。审核者使用原片预览、原始 PTS 及本地音频依据，部分语音用离线 Whisper small 辅核。项目云端视觉 Provider 调用为 0。开发侧只接收留出集的聚合结果与回执。[独立复核汇总](../../eval/manifests/d1_review_summary.json)

## 本地重验

验收者在仓库根目录执行下列命令。`frozen_root` 保存正式的 24 段授权素材、D1 源清单和 D0 两个冻结文件。`packet.json` 及其相对路径下的原帧由验收者保存在本机。仓库归档脱敏源清单和聚合回执，开发侧只使用上方的开发题集。

```powershell
uv run --no-sync python -m eval.d1 --repo-root data/d1-evaluator/frozen_root --packet data/d1-evaluator/packet.json --receipt eval/manifests/d1_freeze_receipt.json
```

回执绑定源清单、D0 清单及题集、私包、验证程序和计分程序的 SHA-256。相同输入支持重放。已有回执与本次结果不同时，命令退出 1 并保留首次发布的文件。受哈希绑定的仓库文件使用 LF，便于 Windows 与 Linux 比对精确字节。验收者另存仅含 32 道常规题和 20 个定位请求的盲测输入，每条使用不透露可答性或事件标签的独立 ID，参考答案和边界真值由验收者保管。

Windows 使用相同私包连续执行两次，两次均退出 0，回执内容、文件标识和修改时间一致。Linux 首轮使用历史镜像内置的评测脚本，数据核验通过，回执中仅两项源码摘要不同。将仓库相同版本的两个 LF 文件换算为 CRLF 后，精确复现了旧镜像的两项摘要。

验收者把与 Git 字节一致的 `eval/__init__.py`、`eval/manifest.py`、`eval/d1.py`、`eval/d1_metrics.py` 放入 `frozen_root/eval/`，用原镜像的解释器和依赖执行下列命令。容器只读挂载验收目录，网络模式为 `none`。

```powershell
$freezeMount = 'type=bind,source=' + (Resolve-Path -LiteralPath 'data/d1-evaluator').Path + ',target=/freeze,readonly'
docker run --rm --pull=never --network none --read-only --cap-drop ALL --security-opt no-new-privileges --log-driver none --mount $freezeMount --workdir /freeze/frozen_root -e PYTHONPATH=/app sha256:0a84ed6b5f0073fc65b4081c4404e2a190422eff3557c45f2c9f714b01d37082 /app/.venv/bin/python -B -m eval.d1 --repo-root /freeze/frozen_root --packet /freeze/packet.json
```

最终 Linux 验证退出 0，18 个公开字段与 Windows 回执一致，输出的 840 字节和终止换行也完全相同。[跨平台复核记录](../../eval/manifests/d1_review_summary.json)

## 私包结构

| 字段 | 冻结内容 |
| --- | --- |
| `groups` | 每段视频的 `artifact_id`、`series` 和 `related` 分组，用于校验两个集合的来源隔离。 |
| `questions` | 72 道新增常规题。与单独冻结的 24 道 D0 种子题合成 96 题。每题含唯一 ID、视频、题文、类别、核验依据、可答性、参考答案、预期缺口及证据区间。不可答题的参考答案为 `null`。 |
| `events` | 20 个独立事件，每条含视频、标签、可见起止时间及两组边界帧。每帧记录相对路径、PNG 和源片的 SHA-256、`raw_pts`、`time_base`、`origin_pts`、`origin_time_base` 和 `actual_pts_ms`。 |
| `requests` | 20 个独立的 `event_localization` 请求，每个请求绑定一个同视频事件。 |
| `review` | 复核者、实际覆盖、方法限制以及短片的选材依据。 |
| `seed_evidence_corrections` | 逐条记录 D0 题号、证据序号、原终点、复核终点和理由。 |

## 已冻结的计分口径

首次检索取前 5 个候选。24 道可答题至少答对 20 道，首次检索至少命中 22 道。8 道不可答题至少适当拒答 7 道。可核事实支持率至少 90%，引用完整性须为 100%。两者均报告分子和分母，分母为零时无法通过。20 个事件各有一个定位请求，按同标签、正重叠匹配，取最高 IoU，同分时依次取更早起点、终点。起止误差均不超过 2000 ms 才得分，至少 17/20 通过。未检出、请求失败和预算不足均计零，额外预测另计误检。[计分实现](../../eval/d1_metrics.py)

```powershell
uv run --no-sync pytest -q -p no:cacheprovider --disable-socket --allow-unix-socket tests/integration/test_d1_freeze.py tests/integration/test_d1_integrity.py tests/integration/test_d1_seed_correction.py tests/unit/test_d1_metrics.py
```

15 项通过。最初的 9 项在签名占位和缺漏 CLI 上退出 1，测试冻结于 `ef9b900`，diff SHA-256 为 `70730c7159e57aa6568d4a74def97c996752d23b28f25ba756391e041d0a3fb3`，实现为 `0e3fbef`。独立审查确认 `1b7e594`（`af1ab6671bc3c76e714fb1968f78c1e9e2236ed155d5d04d86ee375be13a57c1`）只修改真实时间基的等价类型表达及两个列表注解，原断言保持一致。

后续 4 项回归在同一事件换 ID、授权被否、虚报时长或虚报尺寸时未抛错，冻结于 `b55c0cd`（`dbb8c1130963d27ef1145f67b7139b5285f4fc6a4a615f7ebeb816f2995ea294`）。2 项终点修正回归冻结于 `e11e42e`（`800fc9fc275a44bde2516c3793ebd822c552c548f3da68a0f2839bc1edf2a1a7`）。`3b49d32` 修正了这些行为。独立审查使用 24 段 FFmpeg/PyAV 合成视频验过首次冻结、同内容重放和不同内容拒绝，也用带音轨尾段的视频验证越界引用的显式校正。M6 使用冻结的 32 道留出常规题和 20 个独立定位请求执行一次正式模型评测。[M3 交接](PR-M3.md)
