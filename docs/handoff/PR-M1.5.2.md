# PR-M1.5.2 交接

2026-11-26 已为两条 CLI 接入 `--live --provider openai`，模型固定为 `gpt-6-luna`。账户查询 HTTP 200，三段真实媒体的 ASR、视觉与 QA 均已执行，全部 12 题留有候选及回答。最新质量门、时间越界项和用量见[OpenAI Live 验收记录](PR-M1.5.3.md#2026-11-26-openai-live-验收)。

- 状态：待审核（不自批为已合并）；关联验收：M1.5-A1、M1.5-A2
- 前置交接链接：`docs/handoff/PR-M1.5.1.md`、`docs/handoff/PR-M1.6.md`、`docs/handoff/PR-M1.7.md`
- 基线 commit、Step A commit 与测试 diff hash：基线 `a283f04`（承接 M1.5.1；未执行 git commit）；Step A 冻结测试文件 SHA-256（Step B 全程零改动）：
  - `tests/integration/test_vertical_slice.py`: `6697410CC2AEC632485AC5B8F3191D8A4C09C84EE75BB79E01059565CE7C6E0E`
- 本次单一行为与允许修改路径：实现纯领域中文双字+英文词项分词器 `domain/tokenizer.py`、薄 DeepSeek OpenAI 兼容 Vision/Planner 适配器 `adapters/llm/deepseek.py`（零导入时网络/Key 查表、`sdk_max_retries=0`、48 MiB 与 ≥15 图 4096px 边界、空白 `U+0020` JSON 拦截）、应用层内存中文检索与多图观察/引用校验/问答编排 `application/slice.py`、组合根 `build_slice_container`（强制经由 `BudgetedVisionProvider`/`BudgetedPlannerProvider` 预留结算且禁静默回退 Fake）与 `video-agent slice` CLI 入口；修改路径：`src/video_agent/domain/tokenizer.py`、`src/video_agent/adapters/llm/deepseek.py`、`src/video_agent/application/slice.py`、`src/video_agent/bootstrap.py`、`src/video_agent/cli.py`、`tests/integration/test_vertical_slice.py`。
- 代码真相源链接及合同是否变化：`src/video_agent/domain/tokenizer.py`（`TOKENIZER_VERSION="zh_bigram_en_v1"`，将在 M3.5 索引写入与 M4.1 查询原样复用）、`src/video_agent/application/slice.py`、`src/video_agent/adapters/llm/deepseek.py`、`src/video_agent/bootstrap.py`、`src/video_agent/cli.py`；未改动既有 M1 模型/端口/Schema 合同。
- 有效红灯：
  - 命令：`uv run --no-sync pytest tests/integration/test_vertical_slice.py -q --disable-socket --allow-unix-socket --tb=line`
  - 断言与退出码：exit 1（`6 failed in 1.74s`；5 项 Step 0 `NotImplementedError` + 1 项 CLI 缺 `slice` 子命令，零 `ImportError`）
- 绿灯：
  - `uv run --no-sync pytest tests/integration/test_vertical_slice.py tests/architecture -q --disable-socket --allow-unix-socket` → exit 0（`13 passed in 2.55s`）
  - `uv run --no-sync ruff check src experiments tests` → exit 0（`All checks passed!`）
  - `uv run --no-sync ruff format --check src/video_agent experiments tests` → exit 0（`63 files already formatted`）
  - `uv run --no-sync mypy src/video_agent experiments tests` → exit 0（`Success: no issues found in 63 source files`）
  - `uv run --no-sync lint-imports` → exit 0（`Analyzed 68 files, 171 dependencies. Contracts: 2 kept, 0 broken.`）
- Fixture Self Check、真实集成与断网验证结果：F01 真实夹具自检通过；真实 PyAV 帧 + FFmpeg 音频 + `PromptRegistry` + `BudgetGuard` + 端口 Fake 全链路贯通；验证了“修改孔径”→`["修改","改孔","孔径"]`、单字“孔”受限回退、跨资产/跨 `analysis_id` 检索隔离、“孔径”可命中并输出可解析真实证据 ID、“火星表面气压”无证据返回 `not_answerable` 且零编造 Claim、Vision/Planner 编造引用拦截（`INVALID_CITATION`）、跨资产引用拦截（`CROSS_ASSET_REFERENCE`）、live 缺出口授权（`CLOUD_DISABLED`）与缺 Key（`CREDENTIAL_MISSING`）零静默切换 Fake、以及 `video-agent slice --offline --json` 完整输出。
- 未运行检查及原因：真实 `deepseek-flash` 云端 API 与本地 `faster-whisper` 权重推理未运行（无 `DEEPSEEK_API_KEY` 与云端出口授权；Fake 仅验证契约与链路，不构成真实视觉理解质量结论）。
- 实际模型/HTTP/工具/图片调用数；estimated/confirmed/unknown：0 次真实模型调用、0 次 HTTP 调用、0 张外发图片；`confirmed`（0.0 USD）。
- 依赖锁、模型、Prompt bundle、Schema 与数据版本：未改动 `pyproject.toml`/`uv.lock`；Schema `1.1`；Prompt registry 5 模板 SHA-256 校验通过。
- 已知限制、恢复/回滚方式、独立审查结论：`SliceSearchIndex` 为 M1.5 内存检索（M3.5 迁移至 SQLite `search_docs`/`search_fts`，不另建临时表）；`DeepSeekVisionProvider`/`DeepSeekPlannerProvider` 为薄实现，尚不含应用层瞬时重试/Schema 修复循环（归属 M3.1）；回滚可还原 `bootstrap.py`/`cli.py`/`application/slice.py` 并移除 `domain/tokenizer.py`、`adapters/llm/deepseek.py` 与测试；待独立审查。
- 下一PR需要读取的文件和仍未解决问题：M1.5.3 读取 `AGENTS.md`、`eval/manifest.py`、`eval/manifests/dev_seed.jsonl`、`eval/annotations/dev_seed/dev_seed_questions.json`、`src/video_agent/application/slice.py`、`src/video_agent/bootstrap.py`、`docs/decisions/ADR-001.md`、`docs/handoff/PR-M1.5.2.md`。

## 2026-11-26 CLI 证据回查（待审核）

单视频 `video-agent slice` 在普通输出和 `--json` 中给出 `evidence_manifest`。以本次 `--data-root` 为基准打开该 UTF-8 JSON。文件内的 `artifact_paths` 同样以该目录为基准，可在进程退出后打开原始资产、JPEG 帧和提取的 WAV。完整转写留在本地证据文件，默认终端只显示运行摘要和定位信息。

- `transcript_segments` 保留原文、证据 ID、时间范围和音轨偏移。每帧保留请求时间、实选 PTS、原始 PTS、time base 与原点。
- `observations` 记录观察类别、来源和证据 ID。`questions[].search_hits` 包含候选原文、来源类型、来源 ID、时间范围、分数及证据 ID。根据资产 ID 和分析 ID 可将答案逐条核回候选及帧或转写。
- 中途 QA 失败时，CLI 按输入顺序返回各题状态与错误码，进程返回 1。该题已检索到的候选保存在本地文件的 `question_errors` 中。程序库调用 `run_vertical_slice` 的默认行为仍为直接报告异常。
- 3 段 D0 的离线复核重新打开 3 份证据文件，验证了 20 张 JPEG、2 个 WAV、2 个 Fake 转写段、4 条 Fake 观察、9 条候选和答案中的 7 条引用。18 项新增回归、冻结哈希和完整测试命令见[切片运行器交接](PR-M1.5.3.md)。真实 ASR 与云端质量验收等待用户授权。
