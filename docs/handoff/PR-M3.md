# M3 证据时间轴与索引写入

**M3.0–M3.5 工程验收通过。M3 里程碑仍待 D1 正式数据冻结。** 本地分支为 `m3-evidence-index`，M2 基线为 `9cf4f7a`，本轮完整运行的代码提交为 `3b49d32ec292`。D1 仍缺 40 道开发题、32 道留出题、20 个可见事件及 20 个对应请求。8 段短片需要选材决定，1 条 D0 证据窗需要复核终点。M4 调优的前置条件尚未满足。[M2 交接](PR-M2.md) · [D1 验收记录与私包格式](PR-D1.md)

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
