# PR-M1.5 本地 ASR 截止时间

第二轮 live 复测的第三段完成 WAV 提取后，本地 ASR 在监督器原有的 120 秒上限处报 `DEADLINE_EXCEEDED`。该轮批准的总时限为 900 秒，第三段当时尚未发出云请求。[复测记录](PR-M1.5-A2-retest.md)

正常 live 入口在 WAV 就绪后读取当前 `analysis_id` 和整轮两者较短的剩余时间。时限已到就直接返回；转写中到点，通过 asyncio 取消本地协程，受监督的适配器结束子进程并回收资源。同步缓存命中或本地阶段结束时再次核对剩余时间。组合根用应用的 `analysis.deadline_seconds` 配置监督器的固定上限。独立调用的 `FasterWhisperTranscriber(timeout_seconds=...)` 仍支持用户指定固定时限。预算读取不产生提供方调用预留。转写参数及内容缓存键沿用现行合同。[预算守卫](../../src/video_agent/application/budget.py) · [切片入口](../../src/video_agent/application/slice.py) · [组合根](../../src/video_agent/bootstrap.py)

## 冻结与复验

Step A 在 `e0f0345` 冻结，单文件 LF SHA-256 为 `f6a312c4293dc54b9948a2e37c64750cbd68fa1346503831e25569bf46b3e106`，新增 Git binary diff 的 SHA-256 为 `0cffab5f67fe035a5ae47315570d3a080ebd404b71f2a86742f624f3da89d1cf`。独立审查复现四项红灯。受控单调时钟走到 130 秒时，真实监督器报超时。其余三项分别让分析时限剩 60 毫秒、跨视频的整体时限剩 60 毫秒及 WAV 完成时预算已用尽，旧实现持续等待至测试看门狗超时。[冻结测试](../../tests/unit/test_live_asr_deadline.py)

修复后四项通过。130 秒调用在已审批时间内完成，随后的视觉与问答请求各自获得 45 秒上限。两种 60 毫秒场景均取消已开始的本地计算，已过期场景产生零次转写调用，三个场景的云调用预留均为零。输入为自检通过的合成 MP4 和真实受管 WAV；130 秒由测试时钟模拟，未调用云服务或模型权重。

```powershell
.venv/Scripts/pytest.exe -q tests/unit/test_live_asr_deadline.py --disable-socket --allow-unix-socket --tb=line -o cache_dir=data/.m15-asr-deadline-pytest
.venv/Scripts/pytest.exe -q tests/unit/test_budget_guard.py tests/contract/test_asr.py tests/contract/test_asr_reload.py tests/integration/test_slice_media.py tests/integration/test_media_cache.py tests/integration/test_vertical_slice.py tests/architecture --disable-socket --allow-unix-socket --tb=line -o cache_dir=data/.m15-asr-deadline-regressions
.venv/Scripts/ruff.exe check src experiments tests
.venv/Scripts/ruff.exe format --check src experiments tests
.venv/Scripts/mypy.exe src/video_agent experiments tests --cache-dir data/.m15-asr-deadline-mypy
.venv/Scripts/lint-imports.exe --no-cache
```

Windows 回归另有 60 项通过，覆盖真实 spawn 子进程的取消及超时、独立媒体提取、离线直接驱动、内容缓存、预算及架构。Ruff、格式、96 个源文件的类型检查和两项 import-linter 合同通过。本补丁的专项验证与上游真实质量复测分别登记。
