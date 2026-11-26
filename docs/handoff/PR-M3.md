# M3 证据时间轴与索引写入

**M3.0–M3.5 工程验收通过并已从 `m3-evidence-index` 合入 `main`；M3 里程碑正式签收与 M4 正式调优仍处于人工阻断状态（待 D1 正式数据冻结）。** M2 基线为 `9cf4f7a`，本轮完整运行的代码提交为 `3b49d32ec292`。D1 仍缺 40 道开发题、32 道留出题、20 个可见事件及 20 个对应请求，另有 8 段短片需选材决定、1 条 D0 证据窗需复核终点。在人工完成 D1 冻结并生成有效回执前，不视为 M3 验收通过，不得进入 M4 正式调优。[M2 交接](PR-M2.md) · [D1 验收记录与私包格式](PR-D1.md)

| 子 PR | 交付与独立核对 |
| --- | --- |
| [M3.0](PR-M3.0.md) | SQLite 持久预留、尝试、已发送图像、金额与 deadline；并发争余额、进程中断、显式重试及首次授权 |
| [M3.1](PR-M3.1.md) | 场景 profile、20 秒窗口、2 秒上下文、0.2 FPS 基础采样、真实覆盖缺口 |
| [M3.2](PR-M3.2.md) | 原图及 ROI、图像实际尺寸、当前窗口归属、原始响应结构和错误响应计费回执 |
| [M3.3](PR-M3.3.md) | 保存原帧、裁剪父证据、ASR、观察、证据边与 provenance，分开保留看见、听见和推断 |
| [M3.4](PR-M3.4.md) | 已提交窗口哈希校验、依赖失效及重开复用 |
| [M3.5](PR-M3.5.md) | 中文 FTS5 写入，带原始引用与覆盖缺口的本地摘录摘要，原子发布与正式 CLI |

### 从导入到重开

以下命令使用同一 data-root，适用于本机已授权的视频。第一条返回 asset_id，第二条新建分析并返回 analysis_id，第三条沿用已持久化的预算和截止时间。软件教学可选 `software_tutorial`，讲解与动作概览可选 `overview`。

```powershell
uv run --no-sync video-agent assets import <视频文件> --authorization <授权记录> --json
uv run --no-sync video-agent index --asset-id <asset_id> --profile software_tutorial --offline --json
uv run --no-sync video-agent index --asset-id <asset_id> --profile software_tutorial --offline --analysis-id <analysis_id> --json
```

`--offline` 明确使用本地 Fake 视觉和 ASR。正式入口在 46 秒合成视频上的实测为 3 窗、10 个独立采样、12 个发往 Fake 的图像块、3 次尝试与 3 条检索文档。已提交摘要有 3 条可回溯摘录与 10 段视觉采样缺口。第二次运行复用 3/3 窗，新增尝试和产物均为 0。未知外部结果只有在用户显式指定原 analysis_id 并使用 `--retry` 后才可再次尝试，仍受原额度约束。

### 质量门禁

| 环境 | 已执行结果 |
| --- | --- |
| Windows | CPython 3.14.6、FFmpeg 8.1.2。默认离线套件 **365 passed，1 deselected**，144.53 秒。独立本地 Whisper small 用既有权重和合成语音，**1 passed**。 |
| Linux Docker | CPython 3.14.10、FFmpeg 7.1.5。默认套件 **364 passed，1 deselected**，102.83 秒。干净镜像少一项依赖本机授权 D0 素材的测试。 |
| 架构与静态检查 | Ruff、格式、mypy 通过，扩展到 eval 后 mypy 检查 136 个源文件。两项 import-linter 合同通过，AST 架构测试随默认套件执行。 |
| 隔离 | 构建上下文由提交中的 167 个受控文件组成，镜像运行时 `docker inspect` 返回 `none 0`。独立 Python 进程和原生 curl 均不能外连。容器及本机离线 doctor 均通过。 |

```powershell
uv run --no-sync ruff check src experiments tests
uv run --no-sync ruff format --check src experiments tests
uv run --no-sync mypy src/video_agent experiments tests
uv run --no-sync ruff check src experiments eval tests
uv run --no-sync ruff format --check src experiments eval tests
uv run --no-sync mypy src/video_agent experiments eval tests
uv run --no-sync lint-imports
uv run --no-sync pytest -q -m "not provider_live and not model_local" --disable-socket --allow-unix-socket -o cache_dir=data/.m3-pytest --tb=line
uv run --no-sync pytest -q -p no:cacheprovider -m model_local tests/integration/test_asr_local.py --disable-socket --allow-unix-socket --tb=line
uv run --no-sync python -m eval.d1
```

最后一条在本次真实材料上返回 `blocked`，退出 1。所有测试及静态门禁的最终退出码均为 0。测试先红后绿的提交、差异 SHA-256、失败原因和专项复跑命令保存在各子 PR 交接中。23 个测试文件与各自最终审定版本逐字节一致。补充验收曾发现供应商响应解析失败时漏记 usage、旧授权能被新额度放宽，以及原始引用超出主视频流等问题，均有独立复验记录。[预算](PR-M3.0.md) · [视觉回执](PR-M3.2.md) · [数据冻结](PR-D1.md)

独立 Docker 验收使用镜像 `sha256:0a84ed6b5f0073fc65b4081c4404e2a190422eff3557c45f2c9f714b01d37082`。构建前从代码提交导出 Dockerfile 所需的目录和配置，排除了本机 data 目录。运行命令如下。

```powershell
docker build --progress=plain --iidfile data/m3-gates/image-3b49d32ec292.iid -f data/m3-gates/ci-3b49d32ec292/Dockerfile.ci data/m3-gates/ci-3b49d32ec292
docker run --rm --pull=never --network none --workdir /app --name video-agent-m3-3b49d32 -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 sha256:0a84ed6b5f0073fc65b4081c4404e2a190422eff3557c45f2c9f714b01d37082 sh -ec '.venv/bin/python tests/canary/network_denied.py && .venv/bin/python -m video_agent.cli doctor --json && .venv/bin/python -m pytest -q -m "not provider_live and not model_local" --disable-socket --allow-unix-socket --tb=line'
```

M3 本轮实际云请求与云端计费均为 0。`provider_live`、真实留出 QA、真实事件边界定位和 M6 质量评分尚未运行。正式签收需要验收者提交私包并冻结有效回执。

### 人工阻断记录（合入主干时未解除）

- **记录日期与原因**：2026-11-26，经用户确认先行记录人工阻断项并将已通过工程验收的代码（M3.0–M3.5 与 D1 校验/计分程序）合入 `main`。本次合入**仅代表工程代码落盘主干，不构成 M3-A3 或 M3 里程碑最终签收**。
- **实时审计状态**：`uv run --no-sync python -m eval.d1` 退出码 `1`，返回 `status: "blocked"`、`reason: "private_packet_missing"`。已核验 24 段视频（16 dev + 8 heldout）的源清单（`source_manifest_sha256`: `430a75e93bdf862e9d48e2926b5256ff83ebf379d36d670cd5bea4c3b793295e`）、D0 种子题集 24 题（`dev_seed_qa_sha256`: `35c27d9b962e3ab30a4b07bd1a44bfcc7e8a41816c0068cdb5b07dc5a61bd339`）、校验程序（`verifier_sha256`: `8566729b852154ec858b85db173b8a5ff929d3f8caeb070f3249e5d80cc893b0`）与计分实现（`metrics_sha256`: `48dfbde87b1c483300bf3a90f85b07feb9668aa0e1433cecafe85f19a4d95b39`）。

| 阻断项 | 缺口 / 待人工决定事项 | 责任与处理方式 |
| --- | --- | --- |
| D1 开发集常规 QA | 缺 **40 题**（10 段新增 dev 视频 × 4 题；含 D0 共 16 段 64 题） | 验收者按 [PR-D1.md](PR-D1.md) 补齐 `questions`，含可答性、参考答案、证据区间与缺口 |
| D1 留出集常规 QA | 缺 **32 题**（8 段 heldout 视频 × 4 题，须为 24 可答 + 8 不可答） | 验收者补齐并本地隔离保管，不进入公开仓库 |
| D1 缓慢事件定位集 | 缺 **20 个可见事件**（每事件含起止边界前后共 4 张主视频流原生无损 RGB PNG 及精确 PTS/time_base）与 **20 个独立 `event_localization` 请求** | 验收者在 8 段 heldout 上人工标注并一对一绑定请求，不混入 96 道常规 QA |
| 源分组与 8 段短片决定 | 24 段视频缺 `groups`（`series`/`related` 防泄漏分组）；其中 **8 段短于 30 秒**（dev 5 段、heldout 3 段） | 验收者核对同源/同系列关系并在 `review.short_clip_decision` 写明短片保留依据或替代方案 |
| D0 越界终点人工复核 | **1 条** D0 种子引用终点超出主视频流 54 ms | 验收者核对实际终点，在 `seed_evidence_corrections` 中显式收窄该条终点 |
| 人工复核覆盖与冻结回执 | 尚未提交私包、复核覆盖说明（目标 ≥ 20%，单人限制须披露）及聚合冻结回执 | 运行 `uv run --no-sync python -m eval.d1 --packet <私包JSON> --receipt <回执JSON>` 并归档退出 0 的聚合回执 |

- **解阻条件**：上述 6 项人工阻断全部完成后，执行 `uv run --no-sync python -m eval.d1 --packet <私包JSON> --receipt <回执JSON>` 返回退出码 `0`、保存聚合冻结回执，并更新 [PR-D1.md](PR-D1.md) 与本文档，方可正式签收 M3 并启动 M4 调优。
