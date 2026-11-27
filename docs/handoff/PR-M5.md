# M5 本机服务与恢复验收

M5 在 M4 的索引和问答之上接通了 FastAPI、单 Worker 和 Streamlit。空目录初始化后，可以用授权视频完成上传、提交分析、提问、按引用核对原帧，再导出 JSON 或 Markdown。实现基线为 `5f760ae`，本次验收的源码提交为 `e9a439b`。[本机启动与恢复手册](../runbooks/recovery.md)

| 计划项 | 实测结果 |
| --- | --- |
| M5-A1 上传与异步服务 | HTTP 和 CLI 使用同一受管导入与授权记录。页面最多接收 64 MiB，API 文件最多 500 MiB。真实回环 API 接受重复提交时返回相同 ID。未完成问题只返回状态。无音轨 F05 的分析为 partial、索引为 ready，并可继续问答。[上传](PR-M5.1a.md) · [异步接口](PR-M5.1b.md) |
| M5-A2 单 Worker 与恢复 | 第二真实进程被 OS 锁拒绝。提交后的窗口按文件和哈希复用，提交前中断且已经出站时等待人工确认。取消优先，原始截止时间、尝试与未知费用保留。存活计算子进程、引用丢失和 Windows 缓存短时共享冲突均有真实故障用例。[领取](PR-M5.2a.md) · [恢复](PR-M5.2b.md) |
| M5-A3 证据回看与导出 | 从 API 答案取出的 JPEG 字节与登记 SHA-256 一致。证据接口核对完整文件、有效与越界 Range，覆盖 200、206、416。页面展示原始 PTS 及实际选中毫秒，在当前鉴权方式下明确显示精确帧预览。两种导出包含版本、来源、覆盖和费用状态。[页面](PR-M5.3.md) · [端到端](PR-M5.4.md) |

## 八个业务接口

| 方法与路径 | 返回或操作 |
| --- | --- |
| `POST /v1/assets` | 授权 multipart 上传，201 与 `asset_id` |
| `POST /v1/assets/{asset_id}/analyses` | 202 与 `analysis_id`、`job_id` |
| `GET /v1/jobs/{job_id}` | 作业状态、attempt、进度、取消意图与错误码 |
| `POST /v1/jobs/{job_id}/cancel` | 记录取消意图 |
| `GET /v1/analyses/{analysis_id}` | 状态与按时间过滤、分页的时间轴 |
| `POST /v1/analyses/{analysis_id}/questions` | 202 与 `question_id`、`qa_job_id` |
| `GET /v1/questions/{question_id}` | 未完成时只返状态，完成后附对应回答及账本 |
| `GET /v1/evidence/{evidence_id}` | 指定 `analysis_id`，选 `meta`、`frame` 或 `source_video` |

`jobs retry` 和 `export` 为 CLI 子命令。显式确认重复计费风险只授权再次尝试。数据目录中既有账本、成功答案和历史截止时间仍然有效。各子 PR 的交接记录列出了独立审查后冻结的测试、补丁哈希、初次红灯和对应修复。

## 工程门禁

| 环境 | 结果 |
| --- | --- |
| Windows，CPython 3.14.6、FFmpeg 8.1.2、SQLite 3.50.4 | 512 passed，1 deselected，258.05 秒 |
| Linux，CPython 3.14.10、FFmpeg 7.1.5、SQLite 3.46.1 | 511 passed，1 deselected，224.93 秒 |
| Ruff / mypy | lint 通过，189 个文件格式通过，187 个 Python 源文件类型检查通过 |
| 架构 | 两项 import-linter 合同通过，AST 规则随两端的完整 pytest 套件运行 |
| 真实服务冒烟 | Streamlit 健康端点返回 `ok`，首页 HTTP 200。空数据库上的 Uvicorn、两次 Worker 和 CLI 双格式导出跑通，原帧摘要一致 |

逐项比对两端的收集清单，Windows 多一项既有的 `test_offline_slice_runner_cli_executes_pinned_slice_and_sanitizes_report[authorized_d0]`。该参数使用已在本机提供的授权 D0 素材，Linux 快照只包含版本库文件。

Windows 命令如下。执行时将 uv 缓存定向到工作区内的忽略目录，并把 pytest 缓存放在 `data/m5-full-cache` 中。离线自检返回 `ok=true`。部分基于 Windows 事件循环和真实本机服务的用例显式允许 loopback。操作系统断网另用下方容器实测。

~~~powershell
$env:UV_CACHE_DIR = 'data/m5-uv-cache'
uv run --no-sync ruff check src experiments tests
uv run --no-sync ruff format --check src experiments tests
uv run --no-sync mypy src/video_agent experiments tests
uv run --no-sync lint-imports
uv run --no-sync video-agent doctor --data-root data/m5-gates/doctor --json
uv run --no-sync pytest -q -m "not provider_live and not model_local" --disable-socket --allow-unix-socket --tb=line -o cache_dir=data/m5-full-cache
git diff --check 5f760ae e9a439b
~~~

Linux 验收只读挂载该源码提交的 230 个仓库文件。tar 的 SHA-256 为 `802cb2bab9c05bc39cb96e53e3b3592d01062eb6d76fb5b3633c1efdf8dbce3a`。准备依赖时，在已有 CI 基础镜像上用当前 `pyproject.toml` 和 `uv.lock` 执行 `uv sync --frozen --no-install-project`。实际运行镜像 ID 为 `sha256:6efff6ab7ef7cd0e63bba95f4302a4cf8ae9dfa6a193cf48c7317d9ef60bdc5d`。运行时指定当前快照为 Python 源路径，宿主媒体、运行数据库和凭据均未挂载。容器的 `NetworkMode` 实测为 `none`，独立 Python 进程和原生 curl 外联探针都被系统拒绝。

~~~powershell
git archive --format=tar --output=data/m5-gates/e9a439b-source.tar e9a439b -- .gitattributes .github .streamlit/config.toml AGENTS.md README.md Dockerfile.ci pyproject.toml uv.lock src config prompts experiments eval tests
$snapshot = (Resolve-Path -LiteralPath 'data/m5-gates/e9a439b-source.tar').Path
$inside = 'tar -xf /snapshot.tar && /app/.venv/bin/python tests/canary/network_denied.py && /app/.venv/bin/python -m video_agent.cli doctor --json && /app/.venv/bin/python -m pytest -q -m not\ provider_live\ and\ not\ model_local --disable-socket --allow-unix-socket --tb=line'
docker run --rm --pull=never --network none --cap-drop ALL --security-opt no-new-privileges --name video-agent-m5-e9a439b --mount "type=bind,source=$snapshot,target=/snapshot.tar,readonly" --workdir /work -e PYTHONPATH=/work/src:/work -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 -e PYTHONUTF8=1 video-agent-ci:m5-local-service sh -ec $inside
~~~

Windows 全量回归曾在并发原子替换命中 `WinError 5`。已保留冻结复现、修复和最终全量结果。修复后，对并发写同一目标与旧读句柄仍打开两种情形各重复 20 次，全部通过。[故障记录](PR-M5.2b.md)

本轮使用离线 Fake 检验工程行为。真实供应商、本地 ASR 权重、GPU 回收和人工浏览器播放均未运行。M6 继续使用已冻结的留出集评测问答语义与事件定位精度，配合专项模型作业记录费用、耗时与资源。[D1 冻结记录](PR-D1.md)
