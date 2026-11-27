# M3 证据时间轴与索引写入

**M3 已于 2026-11-27 验收通过。** M3.0–M3.5 已完成工程验收，D1 的 24 段、96 道常规题和另行计分的 20 个事件定位请求已全部冻结。正式回执为 `verified`，M3-A3 的数据条件已满足。M4 调优只读取本地 `data/d1-dev/questions_64.json`。留出集由独立验收子代理保管，模型质量在 M6 评测。[D1 验收记录](PR-D1.md) · [冻结回执](../../eval/manifests/d1_freeze_receipt.json)

M2 基线为 `9cf4f7a`。通过工程验收的代码为 `3b49d32ec292`，已从 `m3-evidence-index` 合入 `main`。[M2 交接](PR-M2.md)

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

### D1 冻结当日的工程复验

Windows 的默认离线套件通过 365 项，1 项按标记排除，耗时 164.77 秒。另跑 D1 数据完整性、终点修正和计分专项，15 项通过，耗时 46.18 秒。Ruff、格式、mypy、两项 import-linter 合同均通过。此次只冻结数据、字节哈希及交接文档。

Windows 复跑两次真实数据冻结命令。相同的 Git 评测文件在只读、断网的 Linux 容器中生成逐字节相同的 `verified` 回执。独立验收子代理复核 32/96 道常规题、20/20 个事件及 8/8 段短片，本轮有记录的真人复核为零。[独立复核汇总](../../eval/manifests/d1_review_summary.json)

```powershell
uv run --no-sync pytest -q -m "not provider_live and not model_local" --disable-socket --allow-unix-socket -o cache_dir=data/.d1-freeze-pytest --tb=line
uv run --no-sync pytest -q -p no:cacheprovider --disable-socket --allow-unix-socket tests/integration/test_d1_freeze.py tests/integration/test_d1_integrity.py tests/integration/test_d1_seed_correction.py tests/unit/test_d1_metrics.py
```

### M3.0–M3.5 工程门禁

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

工程合入时，最后一条是针对 M0 原清单的无包审计，返回 `private_packet_missing` 并退出 1。工程测试及静态门禁退出码均为 0。23 个测试文件与各自最终审定版本逐字节一致。各子 PR 交接记录红灯失败原因、冻结差异、专项复跑及独立复验，包括供应商响应解析失败时的用量记录、重试预算与主视频流终点。[预算](PR-M3.0.md) · [视觉回执](PR-M3.2.md) · [D1 交接](PR-D1.md)

独立 Docker 验收使用镜像 `sha256:0a84ed6b5f0073fc65b4081c4404e2a190422eff3557c45f2c9f714b01d37082`。构建前从代码提交导出 Dockerfile 所需的目录和配置，排除了本机 data 目录。运行命令如下。

```powershell
docker build --progress=plain --iidfile data/m3-gates/image-3b49d32ec292.iid -f data/m3-gates/ci-3b49d32ec292/Dockerfile.ci data/m3-gates/ci-3b49d32ec292
docker run --rm --pull=never --network none --workdir /app --name video-agent-m3-3b49d32 -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 sha256:0a84ed6b5f0073fc65b4081c4404e2a190422eff3557c45f2c9f714b01d37082 sh -ec '.venv/bin/python tests/canary/network_denied.py && .venv/bin/python -m video_agent.cli doctor --json && .venv/bin/python -m pytest -q -m "not provider_live and not model_local" --disable-socket --allow-unix-socket --tb=line'
```

### 2026-11-26 工程合入记录

工程代码 M3.0–M3.5 与 D1 校验及计分程序先行合入 `main`。当日无包审计核对了 24 段源素材和 24 道 D0 题，原源清单 SHA-256 为 `430a75e93bdf862e9d48e2926b5256ff83ebf379d36d670cd5bea4c3b793295e`。当时待补的项目是开发 40 题、留出 32 题、20 个带边界帧的事件及其定位请求、8 段短片取舍、1 条越界引用和独立复核回执。详细阻断记录保存在提交 `edd4db1`。[D1 正式验收记录](PR-D1.md)

工程验收实际项目云端请求与计费均为 0。M6 的留出问答正确率、检索召回、拒答与事件定位成绩在运行正式质量评测时报告。
