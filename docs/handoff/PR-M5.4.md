# M5.4 从答案回看原帧并导出

端到端用授权合成 F05 建立真实受管副本，复用 M3 索引，验证 FTS 中存在查询词，再通过 CLI 提交问题。真实 Worker 进程执行离线问答，测试按 API 答案引用找到 JPEG，逐字节与磁盘登记的文件比较，并复算 SHA-256。已完成问答的显式重试会被拒绝。

`da4ad04` 冻结红灯，补丁 SHA-256 为 `22be152b95e24cb9aae661633207273cafc3523196ff48035b858d02fb7a6f25`。答案引用和真实字节核验已通过，缺少 `export` 时 CLI 退出码为 2。`bb883e4` 实现 `export --analysis-id ... --format json|markdown --output ...`。两种格式包含来源和版本、覆盖缺口、引用、问题及原账本；公开证据时间取自实际媒体记录。导出读取期间若数据库版本变化则报 `RESOURCE_BUSY`，操作者可重新导出。通过同目录临时文件原子发布成品。

~~~powershell
uv run --no-sync pytest -q tests/e2e --disable-socket --allow-unix-socket
uv run --no-sync pytest -q tests/integration/test_job_recovery.py tests/integration/test_worker_boundaries.py tests/integration/test_uncommitted_attempt.py --disable-socket --allow-unix-socket
~~~

另在空数据库从头启动正式 Uvicorn 服务，上传自检通过的 F05，通过 HTTP 提交分析和问题，每个作业由独立离线 Worker 进程领取。相同幂等键两次均返回原 ID。无音轨分析为 `partial`、索引为 `ready`，问答为 `answered`。引用 JPEG 的 SHA-256 与登记值一致，源视频范围取得 206；JSON 中的回答与 API 完全一致，Markdown 同时包含问题和引用。

E2E 使用本地 Fake，底层媒体、文件、数据库和 Worker 进程均走实际实现。页面组件用例访问真实回环 API，服务外联隔离另由 Linux 容器验收。M6 接手真实语义评测和发布门槛。[故障演练手册](../runbooks/recovery.md) · [整仓门禁](PR-M5.md)
