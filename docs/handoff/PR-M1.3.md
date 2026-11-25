# PR-M1.3 交接
- 状态：待审核（不自批为已合并）；关联验收：M1-A3
- 前置交接链接：`docs/handoff/PR-M1.1.md`、`docs/handoff/PR-M1.2.md`
- 基线 commit、Step A commit 与测试 diff hash：基线 `5e3d4db`（未执行 git commit）；Step A 测试文件 SHA-256：
  - `tests/contract/test_ports.py`: Step A 冻结基线 `10E9D8147B47A82FC8D6788FB3F316559511D1D124ED8FD3AD149A973D63B538`；绿灯后仅由 `ruff check --fix` 整理导入顺序与 `datetime.UTC` 别名，断言零改动，最终 SHA-256 `3C3C9ED0041A35DD3015F1E2BD99DA2BC8B3EEE2BC0152B69F478ABC48520AFD`
  - `tests/contract/test_fakes.py`: Step A 冻结基线 `7BBFAA3FFE77EF288DA0DC70AEC823185FCB1F07CF0F2330E39B41FACB39112F`；绿灯后仅将辅助函数 `_run_coro` 改为 PEP 695 泛型语法以满足 `ruff UP047`，断言零改动，最终 SHA-256 `DB26B8FF3DCAA08AB23ABE98027F8C26A4561A452D527E88AB2FAF6C834CCC8A`
- 本次单一行为与允许修改路径：声明全部核心 `Protocol` 端口与传输无关 DTO、实现覆盖 6 类故障场景的异步 Fake LLM 与 Fake ASR 适配器、并在 `bootstrap.py` 建立离线组合根；修改路径：`src/video_agent/domain/ports.py`、`src/video_agent/adapters/llm/fake.py`、`src/video_agent/adapters/asr/fake.py`、`src/video_agent/bootstrap.py`、`tests/contract/test_ports.py`、`tests/contract/test_fakes.py`。
- 代码真相源链接及合同是否变化：`src/video_agent/domain/ports.py`（首次建立全部端口合同：`Clock`、`MediaBackend`、`Transcriber`、`VisionProvider`、`PlannerProvider`、`VideoProvider`、`ArtifactStore`、`EvidenceRepository`、`SearchPort`、`BudgetStore`）。
- 有效红灯：
  - 命令：`uv run --no-sync pytest tests/contract/test_ports.py tests/contract/test_fakes.py -q --disable-socket --allow-unix-socket --tb=line`
  - 断言与退出码：exit 1（`10 failed, 1 passed in 0.14s`；DTO 边界未拦截 + Step 0 `NotImplementedError`，零 `ImportError`）
- 绿灯：
  - `uv run --no-sync pytest tests/contract/test_ports.py tests/contract/test_fakes.py tests/architecture -q --disable-socket --allow-unix-socket` → exit 0（`18 passed`）
  - `uv run --no-sync lint-imports` / `uv run --no-sync ruff check src experiments tests` / `uv run --no-sync mypy src/video_agent experiments tests` → exit 0
- Fixture Self Check、真实集成与断网验证结果：在 Windows 下 `--disable-socket --allow-unix-socket` 会拦截 `ProactorEventLoop` 的内部 AF_INET self-pipe，测试采用纯协程步进 `_run_coro` 验证异步边界且零绕过 `--disable-socket`；无 API Key 下组合根可正常装配。
- 未运行检查及原因：不包含真实云端 SDK/本地模型权重加载（归属 M1.5 / M2.3b）。
- 实际模型/HTTP/工具/图片调用数；estimated/confirmed/unknown：0 次真实模型调用、0 次 HTTP 调用、0 张外发图片；`confirmed`（0.0 USD）。
- 依赖锁、模型、Prompt bundle、Schema 与数据版本：`uv.lock` 未改动；端口 DTO 沿用 Schema `1.1`。
- 已知限制、恢复/回滚方式、独立审查结论：Fake 结果仅验证契约与故障路径，严禁当作真实视频理解质量证据；回滚可还原 `bootstrap.py` 并移除新增端口/Fake 文件；待独立审查。
- 下一PR需要读取的文件和仍未解决问题：M1.4 读取 `AGENTS.md`、`src/video_agent/domain/errors.py`、`src/video_agent/domain/models.py`、`docs/handoff/PR-M1.3.md`。
- 独立审查返工修复（两轮 TDD，状态仍保持 `待审核`）：
  - Step A 冻结测试哈希（Step B 全程零改动）：`tests/contract/test_fakes.py`: `6EB6C0979286E660DF411E7327E52DC5347C7B4AD001C513567A16ACA0F4CDC4`
  - 修复内容：`FakeVisionProvider.__init__` 校验正整数 `duration_ms`，`FakeVisionProvider.observe` 在构造观察前强制校验所有帧 `0 <= actual_pts_ms < duration_ms` 并执行 `time_range.validate_within(duration_ms)`，对右端点（如 `1000 ms`）或越界帧返回 `ProviderError(ErrorCode.INVALID_TIME_RANGE)`；合法末帧（`999 ms`）输出合法 `[999, 1000)`。
  - 红/绿灯结果：Step A 回归测试在行为层红灯（未抛出 `ProviderError`）；Step B 修复后全量门禁与 `143 passed` 全部绿灯。初版绿灯后的 ruff 导入/泛型清理记录仍保留供独立审查核验；未伪造事后 Step A commit。

### 2026-11-25 Planner 与 Vision 共用预算校验

`build_offline_container` 将默认及注入的 Planner 都装配为 `BudgetedPlannerProvider`。Planner、Vision 和调用方使用同一实例 `container.budget_guard`。QA 通过 `question_id` 建立预算并按 `job_id`、Provider、模型预留，随后将 `attempt_id` 传入 `decide`。成功和失败都用原凭据结算，每次重试须取得新凭据。

`BudgetStore.record_usage` 支持在写入记录时标记实际发送。`list_usage_with_dispatch` 从同一快照读取记录及发送标记，使零图片的 Planner 调用在结算前就能计入尝试次数。现有 `list_usage` 仍按首次插入顺序返回使用记录。该信息由内存适配器保存，后续 M3.0 持久化适配器需要实现同一合同。

原复现现在返回 `BUDGET_EXCEEDED`，Fake 调用数为 0。实现提交 `f8797fa` 通过 11 项 Planner 专项回归。Windows 和 Linux 断网容器的全量离线测试均通过 165 项。冻结差异、验收命令与待审核状态见 [M1.7 交接](PR-M1.7.md#2026-11-25-planner-预算阻断项已修复)。

