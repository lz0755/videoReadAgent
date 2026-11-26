# M2 离线媒体工程验收

M2.0–M2.4 的六个子项已实现并通过独立审查。用户批准继续 M2 离线工程，M1.5 的正式人工评分仍为可答题 4/9，门槛为 7/9。下一工程依赖为 M3 的证据索引和持久预算。[上游决策](../decisions/ADR-002.md)

| 验收 | 通过的检查 | 对应交接 |
| --- | --- | --- |
| M2-A1 | 八类合成媒体逐项自检。真实 VFR、非零原点、旋转角标、长 GOP 回跳与半开边界均以实际 PTS 核对；两段已授权 HEVC 的抽样帧与从头解码一致。 | [夹具](PR-M2.0.md) · [视频时间轴](PR-M2.2.md) |
| M2-A2 | 单声道 16 kHz PCM 保留晚起音轨，80–90 秒窗口中的 2–3 秒对应原片 82–83 秒。无音轨、无语音、损坏、权限、缺依赖、取消和超时均有验收。spawn 子进程复用本地权重，句柄隔离、回收及重载均有真实进程检查。 | [音频](PR-M2.3a.md) · [本地 ASR](PR-M2.3b.md) |
| M2-A3 | 500 MiB 有效 MP4 完整复制并哈希，超 1 字节拒绝。配额包含既有产物。复制异常清理、缓存重开、缺失与损坏重算、跨资产内容复用均通过。默认播放映射为 preview_only，验证通过的常量偏移绑定到具体资产、内容和播放器。 | [受管导入](PR-M2.1.md) · [缓存](PR-M2.4.md) |

### 实际入口

安装环境中的 CLI 已通过以下合成样本导入，返回 ready、1000 ms 和与夹具清单一致的 SHA-256。用户可用本地文件路径及其授权记录替换样本参数。

```powershell
uv run --no-sync video-agent assets import data/fixtures/m2_p0/f05_rotation.mp4 --data-root data/m2-acceptance/cli --authorization synthetic-F05 --json
```

单视频和批量切片均使用同一媒体后端。批量评测使用固定资产 ID，再次打开时会复验所选文件、受管副本和当前导入上限。SQLite 为正式帧、WAV、原始文件和证据清单保存所有者、相对路径、长度及哈希。M1 的低层 ArtifactStore 仍可为端口调用准备尚未注册的输入。播放器测量不足时，可以在证据清单的 `playback_timeline_map` 看到降级原因所对应的 `preview_only` 状态。[组合根](../../src/video_agent/bootstrap.py) · [端到端测试](../../tests/integration/test_m2_entrypoints.py)

在同一受管目录运行下面三条命令。每次的 3 段 D0、12 道题均有结果，模式为 `offline_fake`，每次使用新的 analysis_id。后两次均重用了相同的 20 张帧和 2 个 WAV。结束后逐个核验了 37 条产物记录的文件哈希，SQLite 外键检查为 0 项错误。每次报告计入 16 次 Fake 调用、20 份模拟图像输入。云端推理、外部 HTTP、实际外发图片、云端费用均为零。

```powershell
uv run --no-sync python -m eval.slice_runner --manifest eval/manifests/slice.jsonl --offline --data-root data/m2-acceptance/work --output-dir data/m2-acceptance/first
uv run --no-sync python -m eval.slice_runner --manifest eval/manifests/slice.jsonl --offline --data-root data/m2-acceptance/work --output-dir data/m2-acceptance/reopened
uv run --no-sync python -m eval.slice_runner --manifest eval/manifests/slice.jsonl --offline --data-root data/m2-acceptance/work --output-dir data/m2-acceptance/final
```

### 红灯、审查和最终门禁

入口集成 Step A 提交为 `a64def2`。三项测试复现了原切片漏报旋转、WAV 带未定长度、固定资产重开产生不同产物。整库中间运行还暴露了旧版视觉端口在资产注册前准备测试图像时的兼容性错误，另有一项测试当时正命中 ASR 卸载签名占位。独立审查在 `404f842` 冻结了 8 个补充红灯，覆盖数据库登记缺失或错绑、反复导入时限额收紧以及资产 ID 的字面归属。文件记录与磁盘文件均可复验后才复用。补充测试和受影响旧用例均已通过。[目录回归](../../tests/integration/test_m2_catalog_reuse.py)

Windows、CPython 3.14.6、PyAV 19.0.2、FFmpeg 8.1.2、faster-whisper 1.2.1、SQLite 3.50.4。沿用现有依赖锁、M1 端口、schema 1.1 和 Prompt bundle。

```powershell
$env:UV_CACHE_DIR = Join-Path $PWD 'data/.uv-cache'
$env:MYPY_CACHE_DIR = Join-Path $PWD 'data/.m2-mypy'
$env:PYTEST_ADDOPTS = '-o cache_dir=data/.m2-final'
uv run --no-sync ruff check src experiments tests
uv run --no-sync ruff format --check src experiments tests
uv run --no-sync mypy src/video_agent experiments tests
uv run --no-sync lint-imports --no-cache
uv run --no-sync pytest -q -m "not provider_live and not model_local" --disable-socket --allow-unix-socket
uv run --no-sync pytest -q -m model_local tests/integration/test_asr_local.py --disable-socket --allow-unix-socket -o cache_dir=data/.m2-local --tb=line
```

以上命令均退出 0。默认离线套件 **278 passed，1 deselected**，其中包括 7 项 AST 架构测试。mypy 检查 92 个源文件，import-linter 两项合同通过。另行执行的 `model_local` 为 **1 passed**，使用已下载的 small 权重，目标语句实测 39.46–42.02 秒；模型在前段静音中还产生了一个无关短词。修改过的 `eval/` 也通过 Ruff 检查和格式检查。

本次验证覆盖 Windows 宿主。Docker 客户端查询本地引擎时缺少命名管道，`docker version --format '{{.Server.Version}}'` 退出 1，未执行操作系统级 `--network none` 验收。GPU、浏览器播放映射、付费质量复测和 M5 崩溃恢复均未运行。
