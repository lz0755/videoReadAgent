# PR-M0.3 交接
- 状态：待独立审核（**不自批为已合并**）；关联验收：M0-A2（D0：首批 6 段开发素材、24 题、授权/源分组/哈希与可核对依据）+ **M0-A3（同次运行关联基线报告 `experiments/reports/baseline_report.json` 与 D1 标注负责人及排期）**
- 前置交接链接：`docs/handoff/PR-M0.1.md`、`docs/handoff/PR-M0.2.md`
- 基线 commit、Step A commit 与测试 diff hash：基线 `d551c60`（未执行 git commit）；Step A 冻结测试文件 SHA-256：
  - `tests/unit/test_dataset_manifest.py`: `35D06BDBBF56C993666D05C698D91407D13D7A0E1A2E138CD670E8490519C3D4`（M0-A2 Step A 冻结基线；后续仅第 127/130 行集合推导经 `ruff format` 折叠为单行，断言零改动）
  - `tests/contract/test_baseline_report.py`: `68254D160115126400DDDD63FA6803F9BB9648880A737AE59C58044304AA432F`（M0-A3 Step A 冻结基线；Step B 全程**零改动**，哈希保持完全一致）
- 本次单一行为与允许修改路径：
  1. D0 6 段开发视频清单（`eval/manifests/dev_seed.jsonl`）、24 题可核对标注（`eval/annotations/dev_seed/dev_seed_questions.json`）及校验器（`eval/manifest.py`）；
  2. M0-A3 单次关联离线基线运行器与报告（`experiments/baseline_runner.py`、`experiments/reports/baseline_report.json`、`tests/contract/test_baseline_report.py`，并在 `experiments/provider_probe.py` 增量补充序列化请求体 SHA-256）、`docs/decisions/ADR-001.md`（补充 3.3 基线摘要与第 6 节 D1 负责人/排期）、`docs/handoff/PR-M0.0.md`、`docs/handoff/PR-M0.1.md`、`docs/handoff/PR-M0.3.md`。
- 代码真相源链接及合同是否变化：`eval/manifest.py`、`eval/manifests/dev_seed.jsonl`、`eval/annotations/dev_seed/dev_seed_questions.json`、`experiments/baseline_runner.py`、`experiments/reports/baseline_report.json`、`docs/decisions/ADR-001.md`；未改动 `src/video_agent/`、`pyproject.toml`、`uv.lock`。
- 有效红灯：
  - M0-A2：`uv run --no-sync pytest tests/integration/test_fixture_self_check.py tests/integration/test_media_probe.py tests/unit/test_dataset_manifest.py -q --disable-socket --allow-unix-socket --tb=line` → exit 1（`test_dataset_manifest.py` 12/12 全红：缺失 `dev_seed.jsonl` 断言失败 + 11 项 `NotImplementedError`，零 `ImportError`）
  - M0-A3：`uv run --no-sync pytest tests/contract/test_baseline_report.py -q --disable-socket --allow-unix-socket --tb=line` → exit 1（**8 failed in 0.08s**：7 项 `NotImplementedError` + 1 项缺失 `baseline_report.json` 断言失败，零 `ImportError`）
- 绿灯：
  - `uv run --no-sync python -m eval.manifest --verify-disk` → exit 0（`D0 dev_seed valid: videos=6, questions=24 (answerable=20, not_answerable=4), m1_5_slice=['vg_pr10_start', 'ocr_941', 'ocr_750'], on_disk_verified=6`）
  - `uv run --no-sync python -m experiments.baseline_runner` → exit 0（`run_id=m0-baseline-offline-20261125T105412Z, duration_ms=3804, peak_rss_mib=124.7, cost_status=confirmed -> experiments\reports\baseline_report.json`）
  - `uv run --no-sync pytest -q -m "not provider_live and not model_local" --disable-socket --allow-unix-socket` → exit 0（`71 passed`，含 M0-A3 8/8 合约测试全绿）
  - `uv run --no-sync ruff check src experiments eval tests` → exit 0（`All checks passed!`）
  - `uv run --no-sync ruff format --check src/video_agent experiments eval tests` → exit 0（注：全量 `ruff format --check src experiments eval tests` 唯一差异仍为初始提交 `798b796` 遗留且未触碰的 `src/main.py`）
  - `uv run --no-sync mypy src/video_agent experiments eval tests` → exit 0（`Success: no issues found in 20 source files`）
- Fixture Self Check、真实集成与断网验证结果：
  - 在同一次离线基线运行（`run_id = m0-baseline-offline-20261125T105412Z`）中实测并关联：
    - 代码与依赖版本：git `d551c60` + 工作区 31 文件树哈希 `bff64c20...10f`，`uv.lock` 哈希 `89da7437...4e0`；
    - 脱敏机器规格：Windows 11 AMD64 / 16 核 / 47.19 GiB RAM / CPython 3.14.6 / FFmpeg 8.1.2（自动校验零私有绝对路径）；
    - 输入与请求体哈希：6 段 D0 dev 视频 + 4 清单 + 26 张 M0-A1 帧合并哈希 `e1bf9511...63a`；8 组边界用例 + 8 组 ROI A/B + 3 组多图顺序序列化 JSON 请求体合并哈希 `bfc47944...7fe`；
    - 阶段耗时与解复用 RTF：总计 `3,804 ms`（D0 校验 `86 ms`、F01–F04 生成与自检 `2,088 ms`、6 段 dev 视频 `888.8 s` 包级 PTS 解复用 `688 ms` [`aggregate_probe_rtf=0.00077`]、Provider 离线序列化 `939 ms`；显式声明 `extrapolates_to_10min_full_pipeline=false`）；
    - 峰值内存：Windows `GetProcessMemoryInfo` 实测进程峰值 RSS `124.7 MiB` + Python `tracemalloc` 峰值 `5.22 MiB`。
  - 24 题（20 `answerable` + 4 `not_answerable`，M1.5 切片 12 题含 3 不可答）全部严格整数毫秒 `[start_ms, end_ms)` 且与 `m6_holdout` 零重叠。
- 未运行检查及原因：
  1. `provider_live` 真实云端调用未运行（缺 `DEEPSEEK_API_KEY` 与显式 `cloud_egress_authorized: true`），故 `live_model_usage_status = "not_tested_live"`，零项能力标为 `runtime_verified`。
  2. 单人项目尚未执行第二位独立审查者 ≥20% 标注复核（已在 `dev_seed_questions.json` 与 `baseline_report.json` 中披露，并在 D1-4 排期中落实 ≥20% 抽检或 ≥7 天冷却期延迟盲复核）。
  3. 完整 10 分钟 ASR + 视觉 + FTS 管线 RTF 尚未运行（归属 M2/M3，未用离线 ffprobe 耗时外推）。
- 实际模型/HTTP/工具/图片调用数；estimated/confirmed/unknown：0 次模型调用、0 次 Provider HTTP 调用、0 张外发图片、0 字节云端传输；`confirmed_cost_usd = 0.0`，`cost_status = "confirmed"`（离线零费用已确认）；`live_model_usage_status = "not_tested_live"`。
- 依赖锁、模型、Prompt bundle、Schema 与数据版本：`uv.lock` 未改动；标注 Schema `m0.3-dev-seed-annotations-v1`；基线报告 Schema `m0-baseline-report-v1`；D1 标注计划已落实负责人 `lz0755`、独立预算 `2–4` 人天、4 阶段排期（`D1-1` 至 `D1-4`，`M3_exit` 硬冻结 24 视频 / 96 题 / 20 留出事件）。
- 已知限制、恢复/回滚方式、独立审查结论：
  - `vg_ae10_start` 与 `vg_pr10_start` 为 VideoGUI 任务素材而非软件界面录屏（GUI 小字由 `ocr_941` 与 M0-A1 高分 VideoGUI 截图覆盖）；`vmme_001`/`vmme_087` 为 `hevc` 编码（M2 需验 PyAV）。
  - M0.0–M0.3 全部交接文档均保持 `待独立审核`，其中 M0.1 缺实现前 Step A 冻结记录的流程偏差已如实披露于 `docs/handoff/PR-M0.1.md`，待审查者确认处置。
  - 结论：M0-A2 与 M0-A3 离线部分已闭环；M0-A1 云端实测与进入 M1.5 真实云端链路仍遵循 `pause_pending_live_verification`。
- 下一PR需要读取的文件和仍未解决问题：
  - 独立审查 M0.0–M0.3 交接文档（尤其确认 PR-M0.1 流程偏差是否豁免）后，可按需切分/提交 git commits。
  - 若离线推进 M1.1 工程骨架与质量门：读取 `AGENTS.md`、`docs/decisions/ADR-001.md`、`experiments/reports/baseline_report.json`、`docs/handoff/PR-M0.2.md`、`docs/handoff/PR-M0.3.md`。
  - 若完成 M0-A1 live 闭环：提供 `DEEPSEEK_API_KEY` 与显式授权预算文件执行 `experiments.provider_probe --live`。
