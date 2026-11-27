# 视频理解 Agent

视频理解 Agent 是面向单用户的本机应用。导入有处理授权的视频后，可以按时间轴查找画面和语音，对同一次分析反复提问，并从回答中的引用回看证据。分析结果、证据、覆盖缺口和用量均可导出为 JSON 或 Markdown。

第一次使用请先[安装与初始化](#安装与初始化)，再用[命令行](#命令行快速开始)跑通样片，或[启动本机页面](#在页面中分析视频)分析自己的视频。开发和接入请看[技术指南](#技术指南)。

## 使用文档

### 安装与初始化

从源码仓库或解压后的发布包进入含有 `pyproject.toml` 的目录。准备 CPython 3.14、uv，以及可从 PATH 调用的 FFmpeg 和 FFprobe。Windows 验收使用 Python 3.14.6。首次安装需要依赖仓库或已备齐的 uv 缓存。下文命令使用 PowerShell。

```powershell
uv sync --frozen --no-editable
$dataRoot = 'data/local'
uv run --no-sync video-agent doctor --data-root $dataRoot --json
uv run --no-sync video-agent init --data-root $dataRoot --writers-stopped
```

确认 `doctor` 的 `ok` 为 `true`。它会检查 FFmpeg、FFprobe、SQLite FTS5、配置和数据目录。离线运行无需云端密钥。`init` 用于首次建库或升级，`--writers-stopped` 表示已关闭使用该目录的写入进程。升级已有数据前，先停机备份整个目录。以下示例统一使用 `data/local`，CLI 省略 `--data-root` 时默认使用 `data`。

### 命令行快速开始

下面生成一段 30 秒的无声测试画面。离线模式用内置 Fake 生成观察和回答，视频解码、抽帧、检索和导出都在本机实际执行。整个示例在上一步的同一个终端运行，直接计算时请保持该数据目录的常驻 Worker 处于停止状态。

```powershell
$video = Join-Path $dataRoot ("demo-{0}.mp4" -f [guid]::NewGuid())
ffmpeg -nostdin -v error -f lavfi -i "testsrc2=size=320x180:rate=5:duration=30" -an -c:v libx264 -pix_fmt yuv420p $video
$receipt = '本机生成的测试画面，授权本地处理'
$asset = uv run --no-sync video-agent assets import $video --authorization $receipt --data-root $dataRoot --json | ConvertFrom-Json
$index = uv run --no-sync video-agent index --asset-id $asset.asset_id --profile overview --offline --data-root $dataRoot --json | ConvertFrom-Json
$index | Select-Object analysis_id, analysis_status, index_state
```

处理自己的视频时，跳过生成样片的两行，将 `$video` 指向原文件，并把 `$receipt` 换成该素材的实际授权记录。导入会保存受管副本、来源 SHA-256 和授权回执。P0 面向 30 秒到 10 分钟、最高 1080p 的讲解、软件教程和缓慢动作，优先使用 MP4（H.264，AAC 或无音轨）。单文件上限为 500 MiB，超过页面上传上限 64 MiB 的文件可通过 CLI 分块导入。

```powershell
$qa = uv run --no-sync video-agent ask --asset-id $asset.asset_id --analysis-id $index.analysis_id --question 'observed' --offline --data-root $dataRoot --json | ConvertFrom-Json
$qa.answer | ConvertTo-Json -Depth 8

$report = Join-Path $dataRoot $index.analysis_id
uv run --no-sync video-agent export --analysis-id $index.analysis_id --format json --output "$report.json" --data-root $dataRoot
uv run --no-sync video-agent export --analysis-id $index.analysis_id --format markdown --output "$report.md" --data-root $dataRoot
```

样片没有音轨，索引完成后会看到 `analysis_status=partial` 和 `index_state=ready`。示例问题 `observed` 能检索到 Fake 写入的观察，返回 `answer.status=answered`，`claims[].evidence_ids` 中给出所引帧的 ID。两个导出文件位于 `data/local`，以 `analysis_id` 命名。保留这个 ID，之后可再次提问或打开同一条时间轴。

### 在页面中分析视频

从项目根目录打开三个 PowerShell 终端。准备一个本机专用的随机令牌，例如由密码管理器生成的 32 字节随机值，以无空格的可打印 ASCII 表示。在 **API 和页面两个终端**分别执行以下命令，输入同一令牌。

```powershell
$secret = Read-Host '输入本机 API 令牌' -AsSecureString
$env:VIDEO_AGENT_TOKEN = [System.Net.NetworkCredential]::new('', $secret).Password
```

| 终端 | 从项目根目录运行 | 用途 |
| --- | --- | --- |
| API | `uv run --no-sync video-agent serve --data-root data/local` | 在 `127.0.0.1:8000` 接收本机请求 |
| Worker | `uv run --no-sync video-agent worker --data-root data/local --offline` | 消费队列，每个数据目录只运行一个 Worker |
| 页面 | `uv run --no-sync streamlit run src/video_agent/ui/app.py` | 通过 API 提交任务和查看证据 |

1. 打开 `http://127.0.0.1:8501`，选择文件，勾选「我有权在本机处理这个视频」，点击「导入视频」。已经用 CLI 导入的文件可以直接填写「资产 ID」。
2. 在「分析场景」选择 `overview`（概览）或 `software_tutorial`（软件操作），执行模式选「本机离线演示（Fake）」。点击「提交分析」后保存分析 ID，点击「刷新状态」查看进度。也可以直接填写已有的分析 ID。
3. 等索引状态为 `ready` 或 `partial` 后提交问题。`qa` 适合“画面中显示了哪个菜单？”，`event_localization` 适合“拖动操作何时开始、何时结束？”。刷新后，在「查看引用」中核对原帧或转写区间。显示 `preview_only` 时，页面提供帧预览和时间信息。
4. 阅读 `support_kind` 区分画面观察（`observed`）、语音内容（`heard`）和推断（`inferred`）。`partial` 和 `not_answerable` 的 `gaps` 说明还缺哪些证据。判断某个动作是否发生时，请先确认相关时段有足够覆盖。

如调整 API 端口，使用 `serve --port`，并在页面终端设置对应的 `VIDEO_AGENT_API_URL`。停止服务前等当前作业结束或取消生效，再在各终端按 Ctrl+C。[启动、备份与恢复手册](docs/runbooks/recovery.md)

### 使用真实模型

先确认该素材允许向所选供应商发送的画面、问题与上下文。准备本地 faster-whisper 权重，将 `VIDEO_AGENT_ASR_MODEL_PATH` 设为权重目录。在 Worker 终端通过配置指定的环境变量提供密钥，默认为 `DEEPSEEK_API_KEY`。

将下列 Worker 预算模板保存为 `data/worker-approval.yaml`。模板默认关闭出站。取得本次授权后，将两个开关设为 `true`，并逐项填写获批的模型尝试数、图片数、美元估算上限和时限。

```yaml
authorization:
  live_enabled: false
  cloud_egress_authorized: false
budget:
  max_provider_attempts: 0
  max_submitted_image_parts: 0
  max_images_per_request: 1
  max_estimated_cost_usd: 0
  deadline_seconds: 180
  request_timeout_seconds: 45
```

待该目录的离线队列处理完毕，停止离线 Worker，再执行以下命令。在页面为获准素材提交一项新分析，执行模式选择「已授权的云端模型」。

```powershell
uv run --no-sync video-agent worker --data-root data/local --live --budget-config data/worker-approval.yaml --asr-model-path "$env:VIDEO_AGENT_ASR_MODEL_PATH"
```

## 技术指南

### 从视频到证据

| 环节 | 实现与约束 |
| --- | --- |
| 导入与提交 | 导入器按块复制文件并登记 `asset_id`、哈希和授权。FastAPI 将分析或问答写入磁盘 SQLite，Worker 持有数据目录的独占锁后执行。`analysis_id` 标识一次分析，同一资产可有多次分析。 |
| 建立索引 | FFprobe 探测媒体，FFmpeg 提取音频，PyAV 按真实 PTS 选帧。分析按窗口生成带引用的观察，SQLite FTS5 索引观察与转写。无音轨或覆盖不足会留下可查的限制。 |
| 回答与定位 | 问答绑定 `asset_id` 和 `analysis_id`。Agent 可调用 `search_evidence` 检索、`inspect_clip` 回看短片段、`compare_frames` 对比原帧。发布前校验每条主张的证据归属。 |
| 回看与导出 | 帧证据分别保存请求时间 `requested_ms` 和实际选中时间 `actual_pts_ms`，可由 `raw_pts`、`time_base` 及时间原点复核。转写区间采用整数毫秒 `[start_ms, end_ms)`。成功定位的事件分别给出 `start_bracket` 与 `end_bracket`，每个边界都有前后证据和估计时间。 |

在本例的数据目录里，`agent.sqlite3` 保存作业、索引、引用和用量账本，`artifacts/` 保存受管视频、原帧和分析阶段产物。HTTP 客户端用 `evidence_id` 加 `analysis_id` 获取相应证据。

领域模型与端口位于 `domain`。`application` 和 `agent` 通过注入的端口处理任务，`adapters` 实现媒体、存储和供应商接口，由 `bootstrap.py` 组装。修改证据字段先查领域模型，修改模型接入先查端口，调整运行额度先查配置。[领域模型](src/video_agent/domain/models.py) · [端口](src/video_agent/domain/ports.py) · [索引](src/video_agent/application/indexing.py) · [问答](src/video_agent/application/qa.py) · [组装入口](src/video_agent/bootstrap.py)

默认分析窗口为 20 秒，每侧上下文 2 秒，基础采样率为 0.2 帧/秒。每次分析最多取 192 张不同采样帧，累计发送最多 720 个图片部件，时限 3600 秒。单次问答最多 5 轮、6 次工具调用，其中最多 2 次回看或对比，时限 180 秒。ASR 默认 `vad_filter=false`。每次发送前预留额度，重试和结构修复也计入预算，费用可能是已确认、估算或未知。[默认配置](config/defaults.yaml) · [提示词注册表](prompts/registry.json) · [数据库结构](src/video_agent/adapters/storage/schema.sql)

### 本机 HTTP API

所有请求均使用 `Authorization: Bearer <本机令牌>`。提交分析和问题会返回 HTTP 202。客户端可在这两类请求中携带 `Idempotency-Key`，网络重试时沿用原键和原请求即可取回已有 ID，同一键与不同请求内容冲突时返回 409。

| 方法与路径 | 请求或结果 |
| --- | --- |
| `POST /v1/assets` | multipart 表单字段 `file`、`authorization`，成功返回 201 和包含 `asset_id` 的资产 |
| `POST /v1/assets/{asset_id}/analyses` | JSON `{"profile":"overview","execution_mode":"offline"}`，返回 `analysis_id` 和 `job_id` |
| `GET /v1/jobs/{job_id}` | 查询作业状态、进度和错误码，问答作业在这里使用 `qa_job_id` |
| `POST /v1/jobs/{job_id}/cancel` | 提交取消意图，继续查询作业直到终态 |
| `GET /v1/analyses/{analysis_id}` | 按 `offset`、`limit` 分页读时间轴，支持 `start_ms`、`end_ms` 时间筛选 |
| `POST /v1/analyses/{analysis_id}/questions` | JSON `{"query_text":"画面中显示了哪个菜单？","mode":"qa"}`，返回 `question_id` 和 `qa_job_id` |
| `GET /v1/questions/{question_id}` | 运行中返回状态，生成回答后还包含 `answer` 与 `usage` |
| `GET /v1/evidence/{evidence_id}` | 必传 `analysis_id`，`representation` 可选 `meta`（默认）、`frame`（JPEG）或 `source_video`（支持 Range） |

若已跑通上述命令行样片并启动 API，可在保留 `$index`、`$qa` 的终端设置同一令牌，再读取回答引用的第一条证据。

```powershell
$api = 'http://127.0.0.1:8000/v1'
$headers = @{ Authorization = "Bearer $env:VIDEO_AGENT_TOKEN" }
$evidenceId = $qa.answer.claims[0].evidence_ids[0]
Invoke-RestMethod -Headers $headers -Uri "$api/evidence/$($evidenceId)?analysis_id=$($index.analysis_id)"
```

### 常见问题

| 现象 | 检查或处理 |
| --- | --- |
| 任务停在 `queued`，或报 `CAPABILITY_MISMATCH` | 检查 Worker 是否在运行、数据目录是否与 API 相同，以及 Worker 模式是否与提交的分析一致。 |
| `WORKER_ALREADY_RUNNING` | 直接执行的 `index`、`ask` 和常驻 Worker 共用一把锁。已有 Worker 时，可在页面提交，或用 `analyses create`、`questions create` 入队。 |
| `INDEX_NOT_READY` 或页面暂时不能提问 | 刷新分析，查看 `index_state` 和作业 `error_code`。达到 `ready` 或 `partial` 后即可提问。 |
| 页面或 API 返回 401 | 在 API 和客户端所在终端设置同一 `VIDEO_AGENT_TOKEN`，修改后重启对应服务。 |
| `EXTERNAL_RESULT_UNKNOWN` | 先按原作业核对用量账本与供应商记录，再决定是否接受可能重复计费的重试。[中断处置](docs/runbooks/recovery.md) |

### 开发验证与 M6 验收

从源码开发时，在项目根目录运行 `uv sync --frozen` 以可编辑方式安装，随后修改 `src/` 即可由 CLI 和测试加载。默认测试使用本地素材或合成夹具，`model_local` 和 `provider_live` 分别需要另行准备的权重及云端授权。

```powershell
uv run --no-sync ruff check src experiments tests
uv run --no-sync ruff format --check src experiments tests
uv run --no-sync mypy src/video_agent experiments tests
uv run --no-sync lint-imports
uv run --no-sync pytest -q -m "not provider_live and not model_local" --disable-socket --allow-unix-socket
```

单独验证 M6 工程流程时，下面的测试会自建合成视频，覆盖普通问答、事件请求和报告输出。

```powershell
uv run --no-sync pytest -q tests/integration/test_eval_runner.py --disable-socket --allow-unix-socket
```

M6 的正式质量验收包含 32 道留出常规题和 20 个事件请求。当前交接已记录离线工程结果，真实模型的正式评测与人工复核仍待这批素材的云端授权。[评测与计分手册](docs/runbooks/evaluation.md) · [隐私、断网验证与数据删除](docs/runbooks/security.md) · [M6 发布判断与实测平台](docs/handoff/PR-M6.md)
