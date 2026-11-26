# PR-M1.5.1 交接

2026-11-26 已用 faster-whisper small 的真实权重完成固定三段 D0 的转写阶段。FFmpeg 产出的 16 kHz 单声道 PCM 直接作为 float32 波形输入模型，保留已有音轨时间映射。逐段结果和回归检查见[OpenAI Live 验收记录](PR-M1.5.3.md#2026-11-26-openai-live-验收)。

- 状态：待审核（不自批为已合并）；关联验收：M1.5-A1
- 前置交接链接：`docs/handoff/PR-M0.2.md`、`docs/handoff/PR-M1.2.md`、`docs/handoff/PR-M1.3.md`、`docs/handoff/PR-M1.7.md`
- 基线 commit、Step A commit 与测试 diff hash：基线 `a283f04`（工作区已有 `pyproject.toml`/`uv.lock` 新增 `av>=19.0.2` 与 `faster-whisper>=1.2.1`；未执行 git commit）；Step A 冻结测试文件 SHA-256（Step B 全程零改动）：
  - `tests/integration/test_slice_media.py`: `91116D0AE0943ADC721C8EBB99B7DE159EC47516F30008D9153B8A5E24F8B2AA`
- 本次单一行为与允许修改路径：实现 M1.5.1 真实文件 → PyAV 帧（保留真实 PTS、time_base 与非零原点、半开区间）+ FFmpeg 16kHz 单声道音频（保留音轨偏移、区分无音轨与损坏）+ 离线安全 `faster-whisper` 薄适配链及应用层 `extract_slice_media`；修改路径：`src/video_agent/adapters/media/thin_media.py`、`src/video_agent/adapters/asr/faster_whisper_thin.py`、`src/video_agent/application/slice.py`、`tests/integration/test_slice_media.py`。
- 代码真相源链接及合同是否变化：`src/video_agent/adapters/media/thin_media.py`、`src/video_agent/adapters/asr/faster_whisper_thin.py`、`src/video_agent/application/slice.py`；未改动 M1 `domain/models.py`、`domain/ports.py`、`domain/errors.py` 合同（全部薄实现严格满足既有 `ArtifactStore`、`MediaBackend`、`Transcriber` Protocol）。
- 有效红灯：
  - 命令：`uv run --no-sync pytest tests/integration/test_slice_media.py -q --disable-socket --allow-unix-socket --tb=line`
  - 断言与退出码：exit 1（`6 failed, 1 passed in 1.84s`；全部夹具 F01–F04 自检通过，6 项行为测试均命中 Step 0 `NotImplementedError`，零 `ImportError`）
- 绿灯：
  - `uv run --no-sync pytest tests/integration/test_slice_media.py tests/architecture -q --disable-socket --allow-unix-socket` → exit 0（`14 passed in 2.93s`）
  - `uv run --no-sync ruff check src experiments tests` → exit 0（`All checks passed!`）
  - `uv run --no-sync ruff format --check src/video_agent experiments tests` → exit 0（`60 files already formatted`）
  - `uv run --no-sync mypy src/video_agent experiments tests` → exit 0（`Success: no issues found in 60 source files`）
  - `uv run --no-sync lint-imports` → exit 0（`Analyzed 62 files, 139 dependencies. Contracts: 2 kept, 0 broken.`）
- Fixture Self Check、真实集成与断网验证结果：真实 `tmp_path` 文件、真实 FFmpeg 8.1.2 与 PyAV 19.0.2（零 Mock `av`/`subprocess`/文件系统）实测通过：F03 非零原点（~5000ms）保留 `origin_pts > 0` 与向前选帧（请求 10ms → 实选 40ms，请求 1010ms → 实选 1040ms）；F02 真实 VFR 保留非均匀间隔并对 `[0, 80)` 内无帧返回 `NO_FRAME_IN_RANGE`；F04 音轨晚 ~200ms 保留在 `AudioTimelineMap.audio_offset_ms`（`vad_filter=False`、`leading_silence_padded=False`）并传递至规范转写时间轴；真实无音轨 MP4 返回 `NO_AUDIO_STREAM` 且视觉链路继续（`audio_status="no_audio"`），与损坏 MP4（`CORRUPT_MEDIA`）及损坏音频转写错误严格分离；`FasterWhisperThinTranscriber` 缺本地模型目录时在 `--disable-socket` 下直接返回 `DEPENDENCY_MISSING` 且零隐式下载。
- 未运行检查及原因：
  1. `model_local` 真实本地 faster-whisper 权重推理未运行（本机未预置本地 Whisper 模型目录，遵循不隐式下载规则）。
  2. OS 级 `docker run --network none` 隔离验收留待 M1.5 全量收尾统一核验或标注限制。
- 实际模型/HTTP/工具/图片调用数；estimated/confirmed/unknown：0 次真实模型调用、0 次 HTTP 调用、0 张外发图片；`confirmed`（0.0 USD）。
- 依赖锁、模型、Prompt bundle、Schema 与数据版本：使用当前工作区已锁定的 `av==19.0.2` 与 `faster-whisper==1.2.1`；Schema `1.1`。
- 已知限制、恢复/回滚方式、独立审查结论：M1.5.1 薄媒体实现采用顺序流式解码，尚未加入关键帧 seek 优化、90° 旋转显式回溯、媒体缓存键与 `spawn` 持久 ASR 子进程（均归属 M2.1–M2.4 加固）；回滚可移除上述 3 个新增模块及对应集成测试；待独立审查。
- 下一PR需要读取的文件和仍未解决问题：M1.5.2 读取 `AGENTS.md`、`src/video_agent/domain/models.py`、`src/video_agent/domain/ports.py`、`src/video_agent/application/prompt_registry.py`、`src/video_agent/application/budget.py`、`src/video_agent/application/slice.py`、`src/video_agent/bootstrap.py`、`docs/handoff/PR-M1.5.1.md`。
