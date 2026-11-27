# M6.2 回归、隔离和故障验证

稳定编号对应的测试位置、操作系统 canary 和留存策略见[安全手册](../runbooks/security.md)。故障通过真实文件、磁盘 SQLite 和进程退出注入。T05 本地模型专项在 Windows 的已安装 faster-whisper small CPU 权重上通过（1 passed，6.91 秒）。Fake 与 SDK 回放交换端口的两条用例分别覆盖模糊数字和覆盖不足。401/403、429、503 的四条回放验证分类以及每次调用关闭 SDK 重试。

```powershell
$env:HF_HUB_OFFLINE='1'
$env:TRANSFORMERS_OFFLINE='1'
uv run --no-sync pytest -q -m model_local tests/integration/test_asr_local.py --disable-socket --allow-unix-socket -p no:cacheprovider --tb=short
uv run --no-sync pytest -q -m "not provider_live and not model_local" --disable-socket --allow-unix-socket --tb=line -o cache_dir=data/m6/pytest-cache
```

两项 import-linter 合同通过（134 个模块、639 条依赖）。AST 分层测试包含在完整测试集中。默认 Ruff 检查和 195 个文件的格式检查通过。仓库 mypy 严格检查 193 个文件，加入 M6 的四个评测文件和打包脚本后共 198 个文件通过。

```powershell
$env:UV_CACHE_DIR='data/.uv-cache'
uv run --no-sync ruff check src experiments tests
uv run --no-sync ruff format --check src experiments tests
uv run --no-sync mypy src/video_agent experiments tests
uv run --no-sync lint-imports
uv run --no-sync ruff check eval/manifest.py eval/metrics.py eval/report.py eval/runner.py docs/runbooks/package_release.py
uv run --no-sync ruff format --check eval/manifest.py eval/metrics.py eval/report.py eval/runner.py docs/runbooks/package_release.py
uv run --no-sync mypy src/video_agent experiments tests eval/manifest.py eval/metrics.py eval/report.py eval/runner.py docs/runbooks/package_release.py
uv run --no-sync pytest -q tests/unit/test_eval_publication.py tests/unit/test_eval_usage_groups.py tests/unit/test_eval_release.py tests/contract/test_m6_replay.py tests/unit/test_eval_metrics.py tests/integration/test_eval_runner.py --disable-socket --allow-unix-socket -p no:cacheprovider --tb=line
```

以上 M6 专项 30 passed。QA 适配器还对转写文本中诱导调用额外工具的回放抛出 SCHEMA_VALIDATION_FAILED。真实提供方任务没有运行。独立代码审查尚无签收记录。

### 操作系统断网与干净锁定环境

Docker inspect 返回网络模式 `none`，本地镜像 ID 为 `sha256:6efff6ab7ef7cd0e63bba95f4302a4cf8ae9dfa6a193cf48c7317d9ef60bdc5d`。唯一宿主挂载是只读公开源码 ZIP。独立 Python 进程与原生 curl 均收到系统级网络拒绝。容器在无网络状态下从镜像缓存新建 `/work/.venv`，安装 80 个锁定包后运行 doctor、迁移及完整回归。Python 3.14.10、FFmpeg 7.1.5、SQLite 3.46.1，doctor 返回 ok=true，迁移应用 1、2。

```powershell
python docs/runbooks/package_release.py --output data/m6/gates/scoring-runtime.zip
$snapshot = (Resolve-Path -LiteralPath 'data/m6/gates/scoring-runtime.zip').Path
$inside = '/app/.venv/bin/python -m zipfile -e /snapshot.zip /work && sha256sum --quiet -c SHA256SUMS && uv sync --frozen --offline --no-editable --python /usr/local/bin/python && .venv/bin/python tests/canary/network_denied.py && .venv/bin/video-agent doctor --data-root data --json && .venv/bin/video-agent init --data-root data --writers-stopped && .venv/bin/python -m pytest -q -m "not provider_live and not model_local" --disable-socket --allow-unix-socket --tb=line'
docker run --rm --pull=never --network none --cap-drop ALL --security-opt no-new-privileges --name video-agent-m6-release-check --mount "type=bind,source=$snapshot,target=/snapshot.zip,readonly" --workdir /work -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 -e UV_PYTHON_DOWNLOADS=never video-agent-ci:m5-local-service sh -ec $inside
```

受测快照 SHA-256 为 `f1b0c92a00e68fec64267b033861d72d28e7594c1bdcd78f2999302232e598a0`。交付包在此基础上补入验收结果。打包时核对可执行代码、测试、锁和配置的逐文件摘要。Docker API 和 Windows 宿主 uv 缓存的沙箱权限分别通过只读镜像/容器检查及离线安装授权解决。

Linux 在上述全新安装、无出站的环境中完成 **541 passed，1 deselected，256.09 秒**。被排除的本地权重专项已在 Windows 显式运行。Windows 工作区还包含一条使用已授权 D0 媒体的条件参数，纯源码包中没有这些私有输入。

Windows CPython 3.14.6、FFmpeg 8.1.2、SQLite 3.50.4 的最终离线全量为 **542 passed，1 deselected，282.96 秒**。前一轮在已有 pytest 缓存目录遇到 WinError 183 缓存写入告警；最终轮为缓存选用新目录，退出码为 0，无告警。两次运行均保留相同的 Python socket 限制。

```powershell
uv run --no-sync pytest -q -m "not provider_live and not model_local" --disable-socket --allow-unix-socket --tb=line -o cache_dir=data/m6/final-pytest-cache
```
