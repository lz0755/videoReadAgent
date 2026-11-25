# PR-M0.2 交接
- 状态：待审核；关联验收：M0-A2（F01–F04 最小夹具工厂与独立自检）
- 前置交接链接：`docs/handoff/PR-M0.0.md`、`docs/handoff/PR-M0.1.md`
- 基线 commit、Step A commit 与测试 diff hash：基线 `d551c60`（未执行 git commit）；Step A 测试文件哈希（格式化前冻结基线）：
  - `tests/integration/test_fixture_self_check.py`: `DD97F2058C638AB70B41745A2FEDF86FF1226F874607B40B33B24978E0B7E71C`（未改动）
  - `tests/integration/test_media_probe.py`: `995CF5CEB7E055F0C5D066AFA48F3DD4BBBD0F5A8DAD79EBF5FD454506492640`（仅第 105 行经 `ruff format` 换行，断言零改动）
- 本次单一行为与允许修改路径：F01–F04 合成媒体夹具工厂、独立 `ffprobe`/PCM 自检清单与 `experiments/media_probe.py` 时间轴探针；修改路径：`tests/__init__.py`、`tests/fixtures/__init__.py`、`tests/fixtures/media_factory.py`、`tests/fixtures/manifests/f01_f04_manifest.json`、`experiments/media_probe.py`、`tests/integration/test_fixture_self_check.py`、`tests/integration/test_media_probe.py`、`docs/handoff/PR-M0.2.md`
- 代码真相源链接及合同是否变化：`tests/fixtures/media_factory.py`、`tests/fixtures/manifests/f01_f04_manifest.json`、`experiments/media_probe.py`；未建立或修改生产业务系统合同（未改动 `src/video_agent/`、`pyproject.toml`、`uv.lock`）
- 有效红灯：
  - 命令：`uv run --no-sync pytest tests/integration/test_fixture_self_check.py tests/integration/test_media_probe.py tests/unit/test_dataset_manifest.py -q --disable-socket --allow-unix-socket --tb=line`
  - 结果：exit 1（24 failed in 0.26s；零 `ImportError`，均为 Step 0 占位 `NotImplementedError` 与缺失清单断言失败）
- 绿灯：
  - `uv run --no-sync python -m tests.fixtures.media_factory --output-dir data/fixtures/f01_f04 --manifest-out tests/fixtures/manifests/f01_f04_manifest.json` → exit 0（`Generated & self-checked 4/4 fixtures: F01, F02, F03, F04`）
  - `uv run --no-sync python -m experiments.media_probe --measure-audio-onset data/fixtures/f01_f04/f01_cfr.mp4 data/fixtures/f01_f04/f02_vfr.mp4 data/fixtures/f01_f04/f03_nonzero_pts.mp4 data/fixtures/f01_f04/f04_audio_offset_200ms.mp4` → exit 0
  - `uv run --no-sync pytest -q -m "not provider_live and not model_local" --disable-socket --allow-unix-socket` → exit 0（`63 passed in 9.17s`）
  - `uv run --no-sync ruff check src experiments eval tests` → exit 0（`All checks passed!`）
  - `uv run --no-sync ruff format --check src/video_agent experiments eval tests` → exit 0（`18 files already formatted`；注：含遗留 `src/main.py` 时 `src` 级格式化仍提示 `src/main.py`）
  - `uv run --no-sync mypy src/video_agent experiments eval tests` → exit 0（`Success: no issues found in 18 source files`）
- Fixture Self Check、真实集成与断网验证结果：
  - 集成测试全程使用真实 `tmp_path` 与真实 `ffmpeg 8.1.2` / `ffprobe 8.1.2` 子进程，零 Mock `av` / `subprocess` / `sqlite3` / 文件系统；自检逻辑独立于 `experiments/media_probe.py`。
  - **F01** (`f01_cfr.mp4`, SHA-256 `f1d80964...`): 75 帧，`first_video_pts_ms=0`，恒定间隔 `[40]` ms，音轨对齐 `audio_stream_offset_ms=0`、`audio_tone_onset_ms=0`。
  - **F02** (`f02_vfr.mp4`, SHA-256 `cdf54271...`): 52 帧，真实多间隔 `[40, 100, 120]` ms（极差 80 ms）；将普通 CFR 传入 `self_check_f02_vfr` 确定触发 `FixtureSelfCheckError`。
  - **F03** (`f03_nonzero_pts.mp4`, SHA-256 `c5611764...`): 75 帧，解码首帧 `first_video_pts_ms=5000`、`last_video_pts_ms=7960`；将零起点文件传入 `self_check_f03_nonzero_pts` 触发 `FixtureSelfCheckError`。
  - **F04** (`f04_audio_offset_200ms.mp4`, SHA-256 `42d4f45d...`): 流首帧 `first_audio_pts_ms=178`（AAC 1024 采样 priming `-21.3ms`），解码到规范时间轴的 1000 Hz 音调起振实测 **`audio_tone_onset_ms=196` ms**（200 ± 20 ms 容差内）；将 0ms 对齐文件传入 `self_check_f04_audio_offset_200ms` 触发 `FixtureSelfCheckError`。
  - 全部测试在 `--disable-socket --allow-unix-socket` 下运行；原始 MP4 位于 `.gitignore` 覆盖的 `data/fixtures/f01_f04/`，未入库。
- 未运行检查及原因：
  1. PyAV 解码检查未运行（`pyav_available=false`；M0 未引入 `av` 依赖，留待 M1.5/M2 独立依赖 PR 并在 Python 3.14.6 上验证）。
  2. F05–F08 夹具属于 M2.0，不在 M0.2 范围。
  3. OS 级 `--network none` 容器断网验收属于 M1.1。
- 实际模型/HTTP/工具/图片调用数；estimated/confirmed/unknown：0 次模型调用、0 次 HTTP 调用、0 字节云端传输；`confirmed`（零云端费用）。
- 依赖锁、模型、Prompt bundle、Schema 与数据版本：`uv.lock` 未改动；FFmpeg `8.1.2-essentials_build-www.gyan.dev`；夹具清单 Schema `m0.2-fixture-manifest-v1`。
- 已知限制、恢复/回滚方式、独立审查结论：AAC 编码器引入约 21.3 ms priming 偏移，因此 F04 自检同时校验流 PTS（`200 ± 30 ms`）与解码 PCM 音调起振（`200 ± 20 ms`）；回滚可直接移除新增文件；待独立审查。
- 下一PR需要读取的文件和仍未解决问题：M0.3 读取 `AGENTS.md`、`docs/handoff/PR-M0.2.md`、`experiments/manifests/benchmark_subset_manifest.json`；M2.0 补齐 F05–F08 并接入 PyAV 双重校验。
