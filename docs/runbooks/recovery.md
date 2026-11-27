# 单机服务启动与恢复

下列命令从仓库根目录运行，使用同一数据目录 `data`。P0 只有一个计算 Worker。启动 API 和页面即可提交作业，有运行中的 Worker 才会消费队列。

## 首次启动或升级

1. 关闭此数据目录的 API、页面、Worker 和导入、分析、问答 CLI。升级已有库时，在全部退出后备份整个数据目录，保留数据库、账本、授权回执和受管产物。
2. 按锁文件安装环境。系统 PATH 需能找到 FFmpeg 和 FFprobe。执行离线自检，然后初始化或迁移数据库。

~~~powershell
uv sync --locked
uv run --no-sync video-agent doctor --data-root data --json
uv run --no-sync video-agent init --data-root data --writers-stopped
~~~

`--writers-stopped` 是操作者对所有写入口已关闭的确认。迁移还会尝试获取 `worker.lock`。服务发现旧库时先退出，由上述命令在停机状态升级。

3. 准备一个随机生成的本机令牌，例如密码管理器生成的 32 字节随机值，用可打印 ASCII 表示且不含空格。在 API 和页面各自的 PowerShell 窗口中，分别执行以下两行，输入同一个值。环境变量只在相应进程及其子进程中使用。

~~~powershell
$secret = Read-Host '输入本机 API 令牌' -AsSecureString
$env:VIDEO_AGENT_TOKEN = [System.Net.NetworkCredential]::new('', $secret).Password
~~~

4. 在三个终端分别以前台运行 API、Worker、页面。页面地址为 `http://127.0.0.1:8501`。API 默认地址为 `http://127.0.0.1:8000`。如用 `serve --port` 改端口，同步设置页面进程的 `VIDEO_AGENT_API_URL`。仓库级 Streamlit 配置从当前工作目录加载。

| 终端 | 命令 |
| --- | --- |
| API | `uv run --no-sync video-agent serve --data-root data` |
| Worker | `uv run --no-sync video-agent worker --data-root data --offline` |
| 页面 | `uv run --no-sync streamlit run src/video_agent/ui/app.py` |

默认演练使用离线 Fake，无需云端凭据。页面和提交请求中的执行模式应与 Worker 一致。云端作业另需显式 live 模式、凭据，并以 `--budget-config` 提供含云端传输授权及额度的配置。模式冲突会返回 `CAPABILITY_MISMATCH`。停止服务时先停止接单，待当前作业结束或取消生效，再用各终端的 Ctrl+C 退出。

## 导入、查询与导出

浏览器会缓存所选文件，入口上限为 **64 MiB**。更大的文件用本地 CLI 分块导入，在页面输入返回的 `asset_id`。FastAPI 接受 multipart 的 `file` 和 `authorization` 两个字段，实际文件上限为 **500 MiB**，请求体另限 **64 KiB** 的封装开销。两条路径均保存来源 SHA-256 和本地授权回执。页面可上传并提交分析，也可打开已有 `analysis_id` 查看状态、时间轴和证据。

以下命令演示 CLI 提交和 HTTP 查询。在调用 HTTP 的终端也按前述方法设置同一个令牌，并把 `$video`、`$receipt` 换成实际拥有处理授权的视频和授权记录。

~~~powershell
$asset = uv run --no-sync video-agent assets import $video --authorization $receipt --data-root data --json | ConvertFrom-Json
$analysis = uv run --no-sync video-agent analyses create --asset-id $asset.asset_id --profile software_tutorial --execution-mode offline --idempotency-key review-analysis-1 --data-root data | ConvertFrom-Json
$api = 'http://127.0.0.1:8000/v1'
$headers = @{ Authorization = 'Bearer ' + $env:VIDEO_AGENT_TOKEN }
Invoke-RestMethod -Headers $headers -Uri "$api/jobs/$($analysis.job_id)"
Invoke-RestMethod -Headers $headers -Uri "$api/analyses/$($analysis.analysis_id)?offset=0&limit=20"
~~~

索引状态为 `ready` 或 `partial` 时可以提问。相同提交意图复用原幂等键；新意图选择新键。HTTP 提交使用 `Idempotency-Key` 请求头。问题未结束时只返回作业状态，结束后核对回答状态、逐条引用和缺口。

~~~powershell
$q = uv run --no-sync video-agent questions create --analysis-id $analysis.analysis_id --text '画面中有什么？' --idempotency-key review-question-1 --data-root data | ConvertFrom-Json
Invoke-RestMethod -Headers $headers -Uri "$api/jobs/$($q.qa_job_id)"
Invoke-RestMethod -Headers $headers -Uri "$api/questions/$($q.question_id)"
uv run --no-sync video-agent export --analysis-id $analysis.analysis_id --format json --output review.json --data-root data
uv run --no-sync video-agent export --analysis-id $analysis.analysis_id --format markdown --output review.md --data-root data
~~~

输出到已有的本地目录。导出会原子替换指定的目标文件，包含资产哈希、配置与 Prompt 版本、覆盖限制、时间轴、问题、引用和用量。`cost_status=unknown` 会保留在导出中。报告包含分析内容，应与受管媒体采用相同的访问范围。

引用下拉框可核对真实 JPEG、原始 PTS、实际选中毫秒和请求毫秒，语音证据使用原始区间。源视频可经已鉴权的 evidence 接口按 Range 读取。原生播放器无法附带此处的 Bearer 请求头，页面会注明 `preview_only` 和原因。

## 作业中断后

记录 `analysis_id`、`job_id`，问答另记 `question_id`、`qa_job_id`。重启时让同一模式的 Worker 打开原数据目录。`worker --once --offline` 会先恢复，然后最多领取一个作业，适合复核故障。持有生命周期锁的 Worker 可以重用哈希正确且已提交的阶段。未提交残留由新尝试重算。

| 现象 | 处置 |
| --- | --- |
| `WORKER_ALREADY_RUNNING` | 等待同一目录的前一个 Worker 或直接计算 CLI 退出。OS 释放锁后再启动。保留 `worker.lock` 和 `import.lock` 的文件身份。 |
| 用户要取消 queued/running 作业 | 对 `POST /v1/jobs/{job_id}/cancel` 提交意图，再查询作业直到 `cancelled` 或已进入其他终态。Worker 在发送和发布边界检查取消。已经发出的调用可能计费。 |
| `EXTERNAL_RESULT_UNKNOWN` | 检查原作业和导出账本，结合供应商记录核对已出站的尝试。确定接受可能再次计费以后，再执行下方显式重试。 |
| `DEADLINE_EXCEEDED` | 原作业的起始时间、绝对截止时间和消耗仍有效。若业务上确实需要重新计算，应作为新意图单独授权预算。 |
| `STORAGE_ERROR` 或 `INVALID_CITATION`，此前作业曾完成 | 检查受管源、原帧和阶段文件，优先从同一资产的备份恢复相同字节。Worker 会按实际文件及哈希核对发布状态。旧答案和历史用量保留，缺失引用的回答不可作为已核验结果。 |
| `RESOURCE_BUSY` 且存在 `compute-active` | 先按下一节核对旧计算子进程和资源，再尝试恢复。导出时若目录正被写入也可能返回此码，可待目录稳定后重新导出。 |

~~~powershell
Invoke-RestMethod -Method Post -Headers $headers -Uri "$api/jobs/$($q.qa_job_id)/cancel"
uv run --no-sync video-agent jobs retry $jobId --data-root data
uv run --no-sync video-agent jobs retry $jobId --confirm-external-risk --data-root data
~~~

最后两行是两种情形的命令。普通可重试失败使用第一条，结果不明且已明确接受风险时使用第二条。显式重试沿用原问题、作业、账本和截止时间。成功或已取消的作业会拒绝重试。

### 计算中断留下的标记

`compute-active` 记录启动计算的父进程和作业标识。父进程退出会释放 OS 锁，其子进程可能仍在运行。先停止所有服务与直接计算 CLI，在操作系统进程树中确认该作业的旧 FFmpeg、ASR 和其他计算子进程均已退出，检查其使用的设备已空闲。仅凭父 PID 已消失无法作出这项确认。无法区分进程归属时保留标记，可在可安排的本机重启且服务未自动启动后重新核实。

只有完成上述核实，才在原数据目录的 OS 锁下清除这一项标记。下面的维护命令固定操作 `data/compute-active`，如数据目录不同，应先核实并替换 `root` 的取值。

~~~powershell
@'
from pathlib import Path
from video_agent.adapters.storage.worker_lock import WorkerLock

root = Path("data").resolve()
if not (root / "agent.sqlite3").is_file():
    raise SystemExit("catalog not found")
with WorkerLock(root):
    (root / "compute-active").unlink(missing_ok=True)
'@ | uv run --no-sync python -
~~~

随后启动单个 Worker，通过原作业 ID 回查。人工清除计算标记只表示设备可再使用，已有取消意图、未知外部费用和截止时间继续生效。

自动演练在临时数据目录中运行，涵盖父进程在阶段提交前后退出、子进程仍存活、发送后取消、重启时遇到未知费用、已引用帧丢失。复验命令和实际平台结果见工程交接。[M5 验收记录](../handoff/PR-M5.md)

M6 复验可在空目录安装发布包后执行下列命令。两个真实进程竞争同一目录锁，验证阶段提交前后中断，并核对未知外部结果和原截止时间。

```powershell
uv run --no-sync pytest -q tests/integration/test_job_recovery.py tests/integration/test_worker_lock.py tests/integration/test_uncommitted_attempt.py tests/integration/test_index_resume.py --disable-socket --allow-unix-socket
```

正式评测中断时保留整场运行目录和 SQLite 账本。逐题登记的 `running` 或 `not_run` 状态供验收者核对，已有输出目录会拒绝再次启动。数据保留与删除步骤见[本地发布验证与数据管理](security.md)。
