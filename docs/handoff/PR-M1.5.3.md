# PR-M1.5.3 交接
- 首轮 M1.5-A2 的 12 题人工评分已完成，可答题 4/9，门槛为 7/9。[首轮验收记录](#2026-11-26-openai-live-验收) · [第二轮修复和复测](PR-M1.5-A2-retest.md)
- 前置交接链接：`docs/handoff/PR-M1.5.1.md`、`docs/handoff/PR-M1.5.2.md`、`docs/handoff/PR-M0.3.md`、`docs/decisions/ADR-001.md`
- 基线 commit、Step A commit 与测试 diff hash：基线 `a283f04`（承接 M1.5.1–M1.5.2；未执行 git commit）；Step A 冻结测试文件 SHA-256（Step B 全程零改动）：
  - `tests/unit/test_slice_report.py`: `A7C6470C88AE1C29FCBBAAC28DD1FCF4BCDC2F150EACD0A4DEE7B9DA274A9D68`
  - 同步复核 M1.5.1 / M1.5.2 冻结测试文件零改动：
    - `tests/integration/test_slice_media.py`: `91116D0AE0943ADC721C8EBB99B7DE159EC47516F30008D9153B8A5E24F8B2AA`
    - `tests/integration/test_vertical_slice.py`: `6697410CC2AEC632485AC5B8F3191D8A4C09C84EE75BB79E01059565CE7C6E0E`
- 本次单一行为与允许修改路径：固定 M1.5 3 段 D0 开发集视频与 12 道固定题清单 `eval/manifests/slice.jsonl`，实现切片运行器与诊断门禁评估器 `eval/slice_runner.py`（禁止丢弃失败题、禁止事后改判可答性、禁止 Fake 冒充 live 质量或混合 `provider_mode`、强制 live 预算授权与 Key 门禁且零静默回退 Fake、输出脱敏 JSON 报告），在 `application/slice.py` 修复真实 29.97fps 容器尾帧安全边距 `tail_margin_ms=250`（由真实素材 `ocr_941.mp4` 暴露），并落盘 `docs/decisions/ADR-002.md` 与 `tests/unit/test_slice_report.py`。
- 代码真相源链接及合同是否变化：`eval/manifests/slice.jsonl`、`eval/slice_runner.py`、`src/video_agent/application/slice.py`、`docs/decisions/ADR-002.md`；未改动既有 M1 领域模型/端口/SQLite Schema 合同。
- 有效红灯：
  - 命令：`uv run --no-sync pytest tests/unit/test_slice_report.py -q --disable-socket --allow-unix-socket --tb=line`
  - 断言与退出码：exit 1（`6 failed in 0.05s`；全部 6 项命中 Step 0 `NotImplementedError: M1.5.3 Step 0 scaffold`，零 `ImportError`）
- 绿灯：
  - `uv run --no-sync pytest tests/integration/test_slice_media.py tests/integration/test_vertical_slice.py tests/unit/test_slice_report.py -q --disable-socket --allow-unix-socket` → exit 0（`19 passed in 13.66s`）
  - `uv run --no-sync python -m eval.slice_runner --manifest eval/manifests/slice.jsonl --offline` → exit 0（`mode=offline_fake, videos=3, questions=12, citation_validity=1.000, attempts=16, images=20, can_claim_model_quality=False, decision=pause`，生成脱敏报告 `data/reports/slice_offline_report.json`）
  - `uv run --no-sync ruff check src experiments eval tests` → exit 0（`All checks passed!`）
  - `uv run --no-sync ruff format --check src experiments eval tests` → exit 0（`69 files already formatted`）
  - `uv run --no-sync mypy src/video_agent experiments eval tests` → exit 0（`Success: no issues found in 67 source files`）
  - `uv run --no-sync lint-imports` → exit 0（`Analyzed 68 files, 171 dependencies. Contracts: 2 kept, 0 broken.`）
  - `uv run --no-sync pytest -q -m "not provider_live and not model_local" --disable-socket --allow-unix-socket` → exit 0（`184 passed in 28.99s`）
- Fixture Self Check、真实集成与断网验证结果：
  - 3 段真实 D0 1080p 视频磁盘 `byte_size` + `sha256` `3/3` 核验通过并经真实 PyAV + FFmpeg 贯通：
    - `vg_pr10_start`（`9400ms`，AAC，`audio_status="ok"`，实抽 2 帧 PTS `[0, 5000]`，总耗时 `1299ms`）
    - `ocr_941`（`30025ms`，无音轨，`audio_status="no_audio"`，实抽 6 帧真实 NTSC 29.97fps PTS `[0, 5004, 10008, 15012, 20016, 25020]`，总耗时 `1780ms`）
    - `ocr_750`（`60000ms`，AAC，`audio_status="ok"`，实抽 12 帧 PTS `[0, 5000, ..., 55000]`，总耗时 `5472ms`）
  - 报告反作弊守卫全部实测通过：丢题/事后改判 `answerability`/Fake 宣称 `is_live_quality_evidence=True`/混合 `provider_mode` 均报 `SliceReportError`；live 缺预算授权报 `CLOUD_DISABLED`、缺 Key 报 `CREDENTIAL_MISSING`（零静默回退 Fake）；输出 JSON 经私有绝对路径正则扫描确认零泄露。
  - 全量 184 项测试在 `--disable-socket --allow-unix-socket` 断网模式下全部通过。
- 未运行检查及原因：
  - `provider_live`（`eval/slice_runner.py --live`）：未运行；当前环境无 `DEEPSEEK_API_KEY` 及经批准的 `cloud_egress_authorized: true` 预算 YAML，因此 **M1.5-A2 真实视觉/问答准确率（≥75% 可答、100% 不可答拒答）尚未验证**，ADR-002 如实标记 `pause_pending_live_verification`。
  - `model_local`（`faster-whisper` 本地权重推理）：未运行；本地尚未预下载 Whisper 模型目录。
  - OS 级 Docker `--network none` 断网复核：未在当前 Windows 宿主机执行。
- 实际模型/HTTP/工具/图片调用数；estimated/confirmed/unknown：0 次真实云端模型调用、0 次真实外部 HTTP 调用、0 张真实外发图片；`confirmed`（`0.0 USD`）。（离线 Fake 切片内记录 16 次本地 Fake 尝试、20 份本地图片份数、0 次 `unknown` 事件。）
- 依赖锁、模型、Prompt bundle、Schema 与数据版本：保留既有 `pyproject.toml`/`uv.lock`（`av==19.0.2`、`faster-whisper==1.2.1`）；Schema `1.1`；Prompt bundle hash `7f3b0248a42e543c7f12c00f406d2cc0139809a1b4f0f93de9655258f5d83df0`；报告 Schema `m1.5-slice-report-v1`；D0 标注 `m0.3-dev-seed-annotations-v1`。
- 已知限制、恢复/回滚方式、独立审查结论：
  - 离线切片报告中 12 题全部标记 `manual_judgment="pending"` 且 `decision="pause"`，仅代表工程链路/真实媒体/引用/预算验证通过，不代表模型质量通过；
  - 需人工审查 `docs/decisions/ADR-002.md` 与 `docs/handoff/PR-M1.5.{1,2,3}.md`，并在具备 `DEEPSEEK_API_KEY` + 本地 Whisper 权重后运行 ADR-002 §6 给出的精确 `--live` 命令完成 M1.5-A2 闭环；
  - 回滚方式：删除 `eval/manifests/slice.jsonl`、`eval/slice_runner.py`、`docs/decisions/ADR-002.md`、`tests/unit/test_slice_report.py` 并还原 `src/video_agent/application/slice.py` 中 `tail_margin_ms` 行；待独立审查。
- 下一PR需要读取的文件和仍未解决问题：
  - M2.0 读取：`AGENTS.md`、`docs/decisions/ADR-002.md`、`docs/handoff/PR-M1.5.1.md`、`docs/handoff/PR-M1.5.2.md`、`docs/handoff/PR-M1.5.3.md`、`src/video_agent/adapters/media/thin_media.py`、`src/video_agent/adapters/storage/schema.sql`、`src/video_agent/domain/{models,ports,timeline}.py`；
  - 仍未解决问题：① 授权 live 运行 M0.1 探针与 M1.5 3 段切片以完成 `M0-A1` / `M1.5-A2` 质量闭环；② M2 专项验证 PyAV 解码 Video-MME v2 `hevc`（H.265）素材及完整播放代理 `PlaybackTimelineMap` 校验。

## 2026-11-26 验收返工（待审核）

工作区基线为 `a283f04`。本次修复评分、失败留存、整次运行的预算以及 CLI 本地回查。批准文件示例和报告兼容性记录在 [ADR-002 §4.1](../decisions/ADR-002.md)。单视频产物的位置和字段见 [PR-M1.5.2](PR-M1.5.2.md)。

### 行为与验收

| 触发条件 | 验证结果 |
| --- | --- |
| 9 道可答题全拒答，人工误标其中 7 道正确 | 正确数为 0。零引用的合法率为 `null`，决策为 `pause`。有引用时仍采用合法引用数除以总引用数。 |
| 无证据的回答、带主张或缺少缺口说明的拒答，或执行失败后误填正确 | 该题无法获得正确或诚实拒答分。报告保留题号。执行不完整或评分数据矛盾时暂停签收。 |
| 第 2 题返回非法引用 | 已完成的第 1 题和后续题目保留原顺序。失败行记录 `INVALID_CITATION`、本题实际模型尝试及空答案。视觉阶段失败时，该视频四题均记录为 `not_run` 并附原因。 |
| 重跑失败清单 | 失败报告保存在 `failed_runs/<run_id>/`，固定文件名原子替换为本次报告。重跑使用独立 `analysis_id`，旧报告仍指向原证据。运行错误的 CLI 退出码为 1，成功的离线运行退出码为 0。 |
| 两视频共享 3 次模型尝试或 1 份图像额度 | 分别只放行 1 次视觉加 2 次 QA，或 1 次视觉加 4 次 QA。超额时在调用 Provider 前拒绝。已发出的未知结果消耗尝试、实际图片及费用预留。 |
| 参考费用上限为 0、布尔值冒充数值、字符串冒充授权、跨题后总截止已到 | 有价请求零发送；无效字段在执行前报错；新的 QA 账户沿用整次运行截止点。模拟收费模型的视觉及纯文本未知结果均留下正数估算和 `unknown`。 |

### 两轮 TDD 记录

夹具先执行 `uv run --no-sync pytest tests/integration/test_fixture_self_check.py tests/unit/test_slice_report.py::test_slice_manifest_valid_and_matches_d0_annotations -q --disable-socket --allow-unix-socket`，7 项通过。`uv run --no-sync python -m eval.manifest --verify-disk` 核实 D0 的 6 个源文件和 24 道题。集成测试使用真实临时文件、FFmpeg、PyAV 和 F01 自检，模型故障在既有端口注入。

1. 首轮 Step A 只新增两份测试。运行 `uv run --no-sync pytest tests/unit/test_slice_scoring_regressions.py tests/integration/test_slice_repair.py -q --tb=line --disable-socket --allow-unix-socket`，退出 1，16 项均为行为失败。复现了错误的 7 分、非法引用或未知发送打断整批、限额文件被忽略、共享 deadline 重置、授权类型错误通过和缺少证据指针。Step B 保留冻结断言，复跑 16 项全绿。
2. 补充轮 Step A 增加单视频失败回查及重跑历史核验。原评分用例只新增一行有实际引用时的非空断言，原有“小于 1.0”断言逐字保留。运行 `uv run --no-sync pytest tests/integration/test_slice_review_history.py tests/unit/test_slice_report.py::test_slice_diagnostic_gate_thresholds_and_failure_retention -q --disable-socket --allow-unix-socket --tb=line`，退出 1，2 项行为失败、1 项通过。CLI 中途抛错，且同一视频重跑复用旧分析 ID。补充 Step B 后两项通过。
3. 初次仅检查新增测试文件的 mypy 命令遗漏 `src/video_agent`，出现 19 个模块类型错误。按仓库范围执行后消除。报告比率可为空引出的旧测试类型错误由上述新增非空断言解决。返工中的 Ruff 行宽、导入排序和临时文件上下文检查均已修正。

| 测试文件 | Step A 冻结并在对应 Step B 后复核的 SHA-256 |
| --- | --- |
| `tests/unit/test_slice_scoring_regressions.py` | `3D59EA7A4B3616D33D7FAF1B5F0D03BFCA02DEC0DF11DE86FF5CB945EBC964E6` |
| `tests/integration/test_slice_repair.py` | `E0E5CE59D3A1C44A8C4FF3D42D3A813557BEFFC1C7C07EB5E04905BD013BCAD4` |
| `tests/integration/test_slice_review_history.py` | `F02E7DFCEEE31126BC4B5DE2D72B8AA967A9FA2E60127823FF648F95C9AD748F` |
| `tests/unit/test_slice_report.py`（新增非空断言后） | `251D9708D8F8BA701F7B688F38E943485C90487A2E67C741835BC6C0A4EC76FE` |

按字节排除新增的非空断言后，原评分测试 SHA-256 仍为 `A7C6470C88AE1C29FCBBAAC28DD1FCF4BCDC2F150EACD0A4DEE7B9DA274A9D68`。原媒体与端到端测试的哈希和上方交接一致。以上为工作区冻结记录。

### 最终离线检查

| 命令 | 结果 |
| --- | --- |
| `uv run --no-sync ruff check src experiments tests` | 退出 0 |
| `uv run --no-sync ruff format --check src experiments tests` | 退出 0，69 个文件 |
| `uv run --no-sync mypy src/video_agent experiments tests` | 退出 0，67 个源文件 |
| `uv run --no-sync ruff check src experiments eval tests` | 退出 0 |
| `uv run --no-sync ruff format --check src experiments eval tests` | 退出 0，72 个文件 |
| `uv run --no-sync mypy src/video_agent experiments eval tests` | 退出 0，70 个源文件 |
| `uv run --no-sync lint-imports --no-cache` | 退出 0，2 条架构合同通过 |
| `uv run --no-sync pytest tests/unit/test_slice_scoring_regressions.py tests/integration/test_slice_repair.py tests/integration/test_slice_review_history.py tests/integration/test_slice_media.py tests/integration/test_vertical_slice.py tests/unit/test_slice_report.py -q --disable-socket --allow-unix-socket --tb=line` | 退出 0，37 项通过 |
| `uv run --no-sync pytest -q -m "not provider_live and not model_local" --disable-socket --allow-unix-socket` | 退出 0，202 项通过，包含 AST 架构测试 |
| `uv run --no-sync python -m eval.slice_runner --manifest eval/manifests/slice.jsonl --offline --data-root data/slice_review_work --output-dir data/reports/m1_5_repair` | 退出 0，3 段、12 题、16 次本地 Fake 尝试、20 份本地图像 |

核验报告位于 `data/reports/m1_5_repair/slice_offline_report.json`，运行 ID 为 `02fae76106d244029604255c82979eda`。重新打开产物后逐一核对了 3 份源文件 SHA-256、20 张 JPEG、2 个 16 kHz WAV，以及 9 条候选和 7/7 条引用的资产与分析归属。v1 旧报告和 v2 本次报告重评分均为 12 题待人工判定、`decision=pause`。运行产物保存在 Git 忽略的 `data/`。

真实模型请求、外部 HTTP 及图片外发均为 0，离线确认费用为 0 美元。用户要求待授权后再运行真实 ASR 和云端验收，本次也未执行操作系统级断网容器检查。M1.5 继续保持 `pause_pending_live_verification`。下一项依赖为素材外发授权、批准的数值预算、凭据和本地 ASR 权重就绪后运行既定清单，并对 12 道题逐题人工复核。

## 2026-11-26 OpenAI Live 验收

**A1 已生成真实模型产物，A2 人工验收未通过。** 用户逐题核对并确认了 12 道题。可答题 4/9 正确，要求至少 7/9。不可答题 3/3 恰当拒答，引用合法 9/9，评分器给出 `adjust_and_continue`。本次 `gpt-6-luna` 授权限额为整批 16 次推理、20 图、单次最多 8 图、估算费用 0.10 USD、总时限 600 秒及单次 45 秒。[正式评分报告](../../data/reports/m1_5_openai_live/slice_live_reviewed.json)

| Live 实测 | 核验结果 |
| --- | --- |
| 运行 | `bf68cd2805f94ca28446d668034491fd`，3/3 段、12/12 题执行完成，耗时 212.491 秒 |
| 账户与模型 | 官方模型元数据查询 1 次，HTTP 200。视觉与 QA 共 16 个不同的真实响应 ID，视觉响应记录的模型为 `gpt-6-luna` |
| 本地证据 | 20 帧，24 条原始转写，23 条结构化观察，48 条候选，12 份回答。全部答案的 9 个引用均能回查本次资产和分析 |
| 用量与估算 | 16 次推理、20 份图像，61,476 个输入 tokens、2,708 个输出 tokens，估算 0.0075016 USD，未知费用事件 0，尚未核对供应商账单 |
| 5 道失分题 | 操作顺序题 `vg_pr10_start_q2` 取回 2 条候选后拒答。小字 OCR 题 `ocr_941_q1/q2/q3` 各取回 5 条候选后拒答。`ocr_750_q3` 答出演员和片名，遗漏题目要求的剧情细节 |
| 时间边界 | `ocr_750` 的 `ev_asr_0024_59360ms` 原始区间为 [59,360, 61,360) ms，片长 60,000 ms。该段在视觉观察、48 条候选和最终主张中均被引用 0 次 |

原始报告为 `data/reports/m1_5_openai_live/slice_live_report.json`，SHA-256 为 `570d88c2632ced98a15fe343a42fb9bacfe4547aeb4a3ba877e7e53a94b56082`。同目录 `acceptance_audit.json` 保留了按资产和分析核验的计数与异常，`manual_review.md` 逐题列有模型原文、参考答案、候选及证据链接。所有原始报告和素材仅保存在 Git 忽略的本机目录。

本地模型为多语言 `Systran/faster-whisper-small`，版本 `536b0662742c02347bc0e980a01041f333bce120`，权重 SHA-256 为 `3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671`。已按发布的散列校验配置、词表、分词器和权重。推理采用 CPU int8、`local_files_only=True`、`vad_filter=False`，显式下载权重后在 Python 禁止对外连接的阶段运行。OpenAI 账户验证独立于公开权重下载。

| D0 视频（独立本地 ASR 预运行） | 帧 | 真实 ASR 结果 | 转写段 | 媒体与 ASR 耗时 |
| --- | ---: | --- | ---: | ---: |
| `vg_pr10_start` | 2 | 有音轨，模型返回空转写 | 0 | 57.93 秒 |
| `ocr_941` | 6 | 无音轨 | 0 | 2.44 秒 |
| `ocr_750` | 12 | 有转写，最后一段结束于 58,220 ms | 23 | 122.73 秒 |

本地摘要在 `data/reports/m1_5_openai_local/c42a266edc8746878e3e6211ebee25cf/summary.json`。同目录三份分视频 JSON 可回查资产、原始转写、真实帧 PTS 和音轨时间映射。重新打开核验了 3 份源文件 SHA-256、20 张 1080p JPEG、2 份 16 kHz PCM WAV，全部转写 ID 可回查且时间范围在对应视频内。此处未进行 ASR 语义准确率评测。

首次加载真实权重后，faster-whisper 1.2.1 以 `metadata_errors` 调用 PyAV 19.0.2 的 `av.open` 抛出 `TypeError`。适配器现在校验 FFmpeg 的 WAV 格式，按有符号 16 位小端 PCM 转为归一化 float32 采样，保留原有时间映射。NumPy 已在原锁文件中，本次将其列为直接依赖。空缓存下的 `uv lock --offline` 无法重新解析 av 的发行元数据，补齐原锁文件的直接依赖关系后，`uv lock --check --offline` 通过。

### 两轮 TDD 与复验

| 命令 | 结果 |
| --- | --- |
| `uv run --no-sync python -m eval.manifest --verify-disk` | 6/6 份 D0 源文件通过，固定切片从中选 3 份 |
| `uv run --no-sync pytest tests/integration/test_fixture_self_check.py -q --disable-socket --allow-unix-socket --tb=line` | 6 项通过 |
| `uv run --no-sync pytest tests/unit/test_faster_whisper_pcm.py -q --disable-socket --allow-unix-socket --tb=line` | Step A 退出 1，传入文件名而非 PCM，且错误采样率仍送入模型，2 项失败 |
| `uv run --no-sync pytest tests/unit/test_faster_whisper_pcm.py tests/integration/test_slice_media.py -q --disable-socket --allow-unix-socket --tb=line` | Step B 通过 9 项，真实本地推理随后完成 |
| `uv run --no-sync pytest tests/unit/test_openai_slice.py -q --disable-socket --allow-unix-socket --tb=line` | Step A 退出 1，两个 CLI 缺少选项、错绑凭据未拒绝、两个端口选错地址，5 项失败 |
| `uv run --no-sync pytest tests/unit/test_openai_slice.py tests/unit/test_faster_whisper_pcm.py -q --disable-socket --allow-unix-socket --tb=line` | Step B 通过 7 项 |
| `uv run --no-sync ruff check src experiments tests` 与 `uv run --no-sync ruff format --check src experiments tests` | 全部通过，72 个文件。对 `eval` 单独执行两项检查，3 个文件通过 |
| `uv run --no-sync mypy src/video_agent experiments tests` 与 `uv run --no-sync mypy src/video_agent experiments eval tests` | 70、73 个源文件均通过 |
| `uv run --no-sync lint-imports --no-cache` 与 `uv lock --check --offline` | 2 条架构合同通过，锁定的 57 个包通过一致性检查 |
| `uv run --no-sync pytest -q -m "not provider_live and not model_local" --disable-socket --allow-unix-socket` | **209 passed**，包括 AST 架构测试。Live 前 45.41 秒，Live 后复跑 42.79 秒 |

冻结测试的 SHA-256 为 `0A9CA1C30BD19F2F42464D5181753DC08D46141FBC3CDEC8D77305AE02E31FEF`（PCM）及 `7E6CF1BE31AA55F699441431A8FA16AA9131CA0494F2430FDA8989FDDA37BC96`（OpenAI）。两者在 Step B 后复核一致。公开 SDK 3.59.0 另经内存 HTTP transport 序列化了含合成 JPEG 的视觉请求与纯文本 QA 请求，验证了官方地址、模型、严格 JSON Schema、重试数 0 和输出上限。该检查未向 OpenAI 发送请求。

### 本次启动与人工复核

最初标准环境变量预检为 `credential_missing`。用户选择一次性本地掩码窗口后，实际入口为 `.\.venv\Scripts\python.exe data/reports/m1_5_openai_live/run.py --prompt-key`。启动器校验权重并查询官方模型元数据，以独占标记占用本次批准，再启动 `eval.slice_runner --live --provider openai`。预算与预检存于同目录的 `approved_budget.yaml` 和 `preflight.json`。本次调用额度已用完。

本次已执行的批量 CLI 命令如下，路径相对于项目根目录。

```powershell
.\.venv\Scripts\python.exe -m eval.slice_runner --manifest eval/manifests/slice.jsonl --live --provider openai --budget-config data/reports/m1_5_openai_live/approved_budget.yaml --asr-model-path data/models/faster-whisper-small/536b0662742c02347bc0e980a01041f333bce120 --data-root data/reports/m1_5_openai_live/work --output-dir data/reports/m1_5_openai_live
```

用户确认第 1、3、9、10 题正确，第 2、5、6、7、11 题错误，第 4、8、12 题拒答恰当。判定按固定 `question_id` 绑定到本次 `run_id` 和原始报告 SHA-256。[逐题人工判定](../../data/reports/m1_5_openai_live/manual_judgments.json) · [答案与证据对照表](../../data/reports/m1_5_openai_live/manual_review.md)

执行 `.\.venv\Scripts\python.exe data/reports/m1_5_openai_live/review.py --score` 后，本地重算得到 `quality_gate_passed=false`、`decision=adjust_and_continue`、`pending_manual_judgments=0`。复验核对全部 12 条判定、5 个失败案例、原始报告散列及用量账本。正式报告的 SHA-256 为 `47e19c757774c27d62c2064e8ab1ff0bf9b914918effe61cf565bd954f049e65`。原始报告中的 `pause` 记录了收集模型输出时尚待人工复核的状态。

签核后执行 `uv run --no-sync pytest tests/unit/test_slice_scoring_regressions.py -q --disable-socket --allow-unix-socket --tb=line`，6 项评分回归全部通过。
