# PR-M0.1 交接
- 状态：待独立审核（含已知流程偏差：缺实现前独立 Step A 冻结记录，待审查者确认豁免或要求返工，**不自批为已合并**；离线探针、合同验证与 ≤20 GB MME-VideoOCR / VideoGUI / Video-MME v2 基准子集及 M0-A1 真实抽帧已完成；live 云端复测待提供凭据与出口授权）；关联验收：M0-A1、M0-A2（基准选材与哈希前置）、M0-A3
- 前置交接链接：`docs/handoff/PR-M0.0.md`
- 基线 commit、Step A commit 与测试 diff hash：基线 `d551c60`（承接本轮未提交 M0.0）；**流程偏差如实披露（`missing_pre_implementation_step_a_freeze`）**：M0.1 当时未拆独立 Step A 冻结提交，也未保存实现前红灯日志（本交接绝不补造虚假历史 Step A commit）。当前已冻结用于独立复核的测试文件 SHA-256 如下：
  - `tests/contract/test_provider_capabilities.py`: `E76B65A3FBE7628311AE9CC7A2834CE31C12144BEB6E6FAB85B5D4EEE47B67E3`
  - `tests/contract/test_benchmark_subset.py`: `A1B1583A3369C79D0AC67E6D9B7DB8184A4C0570D305CE94AB9CF7A24C4C06EC`
- 本次单一行为与允许修改路径：主 Provider（DeepSeek）能力与边界探针 + M0/M1.5/M6 ≤20 GB 受控基准子集抽取（MME-VideoOCR、VideoGUI、Video-MME v2）与 M0-A1 真实帧离线对照；修改路径：`.gitignore`、`experiments/dataset_curator.py`、`experiments/manifests/benchmark_subset_manifest.json`、`experiments/provider_probe.py`、`experiments/capabilities.json`、`experiments/reports/m0_1_offline_run_record.json`、`docs/decisions/ADR-001.md`、`tests/contract/test_provider_capabilities.py`、`tests/contract/test_benchmark_subset.py`、`docs/handoff/PR-M0.1.md`
- 代码真相源链接及合同是否变化：`experiments/provider_probe.py`、`experiments/dataset_curator.py`、`experiments/capabilities.json`、`experiments/manifests/benchmark_subset_manifest.json`、`docs/decisions/ADR-001.md`；未建立或改动生产业务系统合同（无 `domain/`、DB、API、UI）。注：M0-A3 补齐时仅在 `provider_probe.py` 增量补充 `serialized_request_body_sha256` 字段，不改变任何既有断言与能力结论。
- 有效红灯：**缺失实现前独立 Step A 红灯记录（已明确标为流程偏差）**。作为事后阻断核验（不可等同于实现前 Step A 红灯），测试套件包含以下负向/越界断言：
  - 禁止将 `documented_supported`/`not_tested` 误写为 `runtime_verified`；
  - 禁止将 `15 × 1920×1080` 普通图冒充已测 `4096 px` 边界（断言 `exercises_15_img_4096_boundary is False`）；
  - 强制拦截 `15 × 4097px`、`16 × 4097px`、`> 48 MiB` 序列化请求体、无授权 Files 上传、`U+0020` 空白 JSON 输出、429/503 重试与网络超时 `cost_status="unknown"`；
  - 强制拦截 `> 20 GB` 基准子集、Dev/Holdout 源分组重叠、缺失 SHA-256 或私有路径泄露。
- 绿灯：
  - `uv run --no-sync python -m experiments.dataset_curator --validate-manifest experiments/manifests/benchmark_subset_manifest.json` → exit 0（`total=0.4178 GB (cap=20.0 GB), artifacts=72, m0_a1_samples=11`）
  - `uv run --no-sync python -m experiments.provider_probe` → exit 0（基于 26 张真实抽取 M0-A1 帧生成 `experiments/capabilities.json` 与 `experiments/reports/m0_1_offline_run_record.json`）
  - `uv run --no-sync pytest tests/contract/test_provider_capabilities.py tests/contract/test_benchmark_subset.py -q -m "not provider_live and not model_local" --disable-socket --allow-unix-socket` → exit 0（`17 passed`）
  - `uv run --no-sync ruff check src experiments eval tests` → exit 0（`All checks passed!`）
  - `uv run --no-sync ruff format --check src/video_agent experiments eval tests` → exit 0（注：全量 `ruff format --check src experiments eval tests` 唯一差异为初始提交 `798b796` 遗留且未触碰的 `src/main.py`）
  - `uv run --no-sync mypy src/video_agent experiments eval tests` → exit 0
- Fixture Self Check、真实集成与断网验证结果：使用真实 `ffprobe`/`ffmpeg` 核验全部 24 段抽取视频的编码/时长/音轨并按真实 PTS 抽取 M0-A1 视频帧（例如 `ocr_941` 请求 `15000 ms` 保留真实 `actual_pts_ms=15013`），全部 72 个媒体产物与 26 张 M0-A1 帧均已写入 SHA-256 并在离线探针中校验；`data/` 已加入 `.gitignore`，M6 留出真值隔离存放于未跟踪 `data/datasets/manifests/m6_holdout_private.json`；默认测试在 `--disable-socket --allow-unix-socket` 下全绿；未运行真实云端与 OS 级断网
- 未运行检查及原因：
  1. `provider_live` 真实云端探针未运行（当前无 `DEEPSEEK_API_KEY` 与 `cloud_egress_authorized: true` 授权配置；本地下载公开学术基准不自动构成云端外发授权）。
  2. 所有未实测能力严格维持 `documented_supported` 或 `not_tested`，零项标记 `runtime_verified`。
- 实际模型/HTTP/工具/图片调用数；estimated/confirmed/unknown：0 次真实模型调用、0 次真实 Provider HTTP 调用、0 字节真实云端模型传输；`confirmed`（零云端推理费用）。注：已通过 HTTP Range 按需从 HuggingFace 公开数据集抽取 `0.4178 GB` 本地评测样本（未下载 17 GB / 105 GB 完整压缩包）。
- 依赖锁、模型、Prompt bundle、Schema 与数据版本：`uv.lock`（未新增生产/开发依赖）；配置模型 ID `deepseek-flash`（官方展示版本 `DeepSeek-V4.1-Flash-1017`，合同快照 `2026-11-25`）；基准子集清单版本 `m0-benchmark-subset-v1`（总大小 `417,760,729` bytes = `0.4178 GB` ≤ 20 GB；24 视频：6 `m0_dev` + 10 `d1_dev_extra` + 8 `m6_holdout`，3 段 M1.5 切片，48 张 VideoGUI Mid-Plan 截图，11 组 / 26 张 M0-A1 真实抽取帧）
- 已知限制、恢复/回滚方式、独立审查结论：
  - Video-MME v2 全部 10 段视频经 `ffprobe` 实测为 `hevc`（H.265）编码（VideoGUI 与 MME-VideoOCR 为 `h264`），M2 引入 PyAV 时需实测 HEVC 解码；
  - `detail="high"` 不保证软件小字必可读；2560×1600 真实 VideoGUI 截图离线实测显示策略 B（960 概览 + 网格预览 + 无网格原图 ROI 裁剪）的序列化请求体（279–413 KB）反而小于策略 A 单张原图（366–537 KB）且 ROI 尺寸低于 ~1300×1300 缩放阈值；
  - M6 留出集真值严格隔离，禁止在 M0/M1.5/M4 调优中读取；
  - **独立审查状态：待独立审查签字（含流程偏差处置选项）**：
    - [ ] 审查者确认是否豁免“M0.1 离线原型阶段缺独立 Step A 冻结记录”的流程偏差（已通过完整负向断言、文件 SHA-256 与后续 M0.2/M0.3/M0-A3 严格两轮 TDD 补齐纪律），或要求对 M0.1 执行返工重跑；
    - [x] 自查确认零 `runtime_verified` 冒充、零静默云端调用、零测试跳过/弱化、零私有路径或留出真值入仓；
  - 结论：`pause_pending_live_verification`（需补齐 live 凭据与显式云端外发授权后执行受控 live 探针，才能完成 M0-A1 云端闭环并进入 M1.5）。
- 下一PR需要读取的文件和仍未解决问题：
  - 若继续离线推进 M1.1：读取 `AGENTS.md`、`docs/decisions/ADR-001.md`、`experiments/reports/baseline_report.json`、`docs/handoff/PR-M0.3.md`。
  - 若补跑 M0.1 live：提供 `DEEPSEEK_API_KEY` 与显式授权预算 YAML，运行 `uv run --no-sync python -m experiments.provider_probe --live --budget-config <path> --tutorial-frame data/datasets/m0_a1_extracted/m0_a1_vg_sd6_gui.png`。

