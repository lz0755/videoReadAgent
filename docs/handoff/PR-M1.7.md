# PR-M1.7 交接
- 状态：待审核（不自批为已合并）；关联验收：M1-A2、M1-A3
- 前置交接链接：`docs/handoff/PR-M1.2.md`、`docs/handoff/PR-M1.3.md`、`docs/handoff/PR-M1.6.md`
- 基线 commit、Step A commit 与测试 diff hash：基线 `5e3d4db`（未执行 git commit）；Step A 冻结测试文件 SHA-256（Step B 全程零改动）：
  - `tests/unit/test_budget_guard.py`: `CAD1BE2D180B7ED816B28878E218AF84529F5664A140FD2245A9C5EDA11C1615`
- 本次单一行为与允许修改路径：基于单一 `domain/policies.py` 实现线程安全内存 `InMemoryBudgetStore` 与 `BudgetGuard`（工具层与 Provider 边界职责分离无双扣、执行前原子预留、重试/修复/unknown 完整入账、首启持久化 deadline 不重置、时钟回拨拦截、取消拦截、QA 最终综合预留槽位与 6 类 Fake 场景联动结算）；修改路径：`src/video_agent/adapters/storage/memory_budget.py`、`src/video_agent/application/budget.py`、`tests/unit/test_budget_guard.py`。
- 代码真相源链接及合同是否变化：`src/video_agent/application/budget.py`、`src/video_agent/adapters/storage/memory_budget.py`、`src/video_agent/domain/policies.py`（未改动既定端口与领域模型合同；M3.0 仅需替换持久化 `BudgetStore` 适配器即可复用全部计数语义）。
- 有效红灯：
  - 命令：`uv run --no-sync pytest tests/unit/test_budget_guard.py -q --disable-socket --allow-unix-socket --tb=line`
  - 断言与退出码：exit 1（`6 failed, 1 passed in 0.20s`；Step 0 `NotImplementedError`，零 `ImportError`）
- 绿灯：
  - `uv run --no-sync pytest tests/unit/test_budget_guard.py tests/architecture -q --disable-socket --allow-unix-socket` → exit 0（`14 passed`）
  - 全量离线测试：`uv run --no-sync pytest -q -m "not provider_live and not model_local" --disable-socket --allow-unix-socket` → exit 0（`137 passed in 15.25s`）
  - `uv run --no-sync ruff check src experiments tests` → exit 0（`All checks passed!`）
  - `uv run --no-sync ruff format --check src/video_agent experiments tests` → exit 0（`56 files already formatted`；注：全仓库 `src/` 中唯一未格式化文件仍为初始提交 `798b796` 遗留且未触碰的 `src/main.py`）
  - `uv run --no-sync mypy src/video_agent experiments tests` → exit 0（`Success: no issues found in 56 source files`）
  - `uv run --no-sync lint-imports` → exit 0（`Analyzed 56 files, 103 dependencies. Contracts: 2 kept, 0 broken.`）
- Fixture Self Check、真实集成与断网验证结果：已验证架构书 §9.2 必测例（1 次 `inspect_clip` + 8 图失败重试再发 8 图 → `tool_calls=1, expensive_tool_calls=1, provider_attempts=2, submitted_image_parts=16`）、单轮多工具超限拦截、双线程争抢最后 1 槽原子性、QA 最终综合预留与归零后禁止追加、`not_sent` 释放 vs `unknown` 不退零、模拟重启沿用首次 deadline、单次超时钳制 `min(45s, remaining)`、30s 时钟回拨拦截与取消拦截。
- 未运行检查及原因：
  1. SQLite 持久化 `BudgetStore` 属于 M3.0，不在 M1.7 范围。
  2. OS 级 `docker run --network none` 隔离验收未在本机运行（Docker Desktop 守护进程未启动）。
  3. M0-A1 真实云端 live 探针与 M1.5 真实云端垂直切片仍受 `ADR-001` 的 `pause_pending_live_verification` 门禁约束。
- 实际模型/HTTP/工具/图片调用数；estimated/confirmed/unknown：0 次真实模型调用、0 次 HTTP 调用、0 张外发图片；`confirmed`（0.0 USD）。
- 依赖锁、模型、Prompt bundle、Schema 与数据版本：`uv.lock` 未改动；全部 M1 合同（Schema `1.1`、SQLite `001_initial`、5 份 Prompt 模板、P0 配置与单一预算 policy）均已落盘。
- 已知限制、恢复/回滚方式、独立审查结论：内存 `InMemoryBudgetStore` 仅用于 M1.5 垂直切片与离线测试，进程退出后不跨进程持久化（M3.0 接入 `budget_accounts`/`usage_records` 表）；回滚可移除 `budget.py`、`memory_budget.py` 及对应单测；M1 全部 6 个 PR（M1.1/M1.2/M1.3/M1.4/M1.6/M1.7）交接均保持 `待审核`，等待独立审查。
- 下一PR需要读取的文件和仍未解决问题：进入 M1.5 前须先完成 M0/M1 交接独立审查及 `ADR-001` 中 M0-A1 云端 live 核验（真实视频理解切片不可用 Fake 替代）；M1.5.1 读取 `AGENTS.md`、`src/video_agent/domain/models.py`、`src/video_agent/domain/ports.py`、`src/video_agent/application/prompt_registry.py`、`src/video_agent/application/budget.py`、`docs/handoff/PR-M1.7.md`。
- 独立审查返工修复（两轮 TDD，状态仍保持 `待审核`）：
  - Step A 冻结测试哈希（Step B 全程零改动）：`tests/unit/test_budget_guard.py`: `A64A599F9FFE750E8BA4B8C9CB22426C9BFCA0D6E3784FFE66B694526564A902`
  - 修复内容：`UsageRecord` 新增 `reserved_images`、`UsageLedger` 新增 `reserved_attempts` 与 `reserved_images`，`pending` 阶段 `images_sent=0`、`total_attempts=0`、`total_images_sent=0`，与已结算实际发送严格分离；`settle_success` 先校验 `images_sent`（不超过 `reserved_images`、`max_images_per_request` 与 `max_submitted_image_parts`）及非负整数 token 数并构造完整对象，全部通过后再落账并移除活跃预留，确保结算校验失败后预留保持活跃且后续仍可按 `settle_unknown` 记录未知结果。
  - 红/绿灯结果：Step A 回归测试行为红灯（`pending_ledger.total_attempts == 1 != 0`）；Step B 修复后全量门禁（含 `uv run --no-sync ruff format --check src experiments tests` 58 文件全通过）与 `143 passed` 全部绿灯；未伪造事后 Step A commit。

### 2026-11-25 独立复测 · 发送前的图片数量校验

结算回归测试通过。另通过 `build_offline_container` 注入正常的 `FakeVisionProvider`，将单请求及累计图片上限都设为 1，预留 1 张，然后把同一 `attempt_id` 传给携带 2 张图片的 `VisionRequest`。Fake 收到 2 张并返回 1 条观察。`settle_success(images_sent=2)` 随后抛出 `BUDGET_EXCEEDED`，账目仍为 `pending`。再调用 `settle_unknown`，账本只计入预留的 1 张。整个复现仅调用本地 Fake。

进入真实 Provider 接入前，发送入口须依据实际请求中的图片数核对预留。用上述输入验收时，超额请求应在调用适配器前被拒绝，Fake 的调用数应为 0。

### 2026-11-25 预留凭据复查

`build_offline_container` 将 `container.budget_guard` 与视觉发送入口接到同一本内存账本。调用方按分析或问题建立预算，预留后把 `attempt_id` 交给 `vision_provider.observe`，使用原预留结算。发送入口在锁内核对作业、Provider、模型和实际图片列表，并重新检查取消、截止时间及剩余超时。每次发送消耗一张凭据，重试须重新预留。

- 预留 1 张却提交 2 张时，入口返回 `BUDGET_EXCEEDED`，Fake 收到 0 次调用。两线程使用同一凭据时只有 1 次发送成功。错误模型、已取消和已过期的请求分别在发送前被拒绝。
- 预留 2 张、实际发出 1 张的成功、限流和结果未知三种场景都只记 1 次尝试及 1 张图片。已发出的请求不能按 `not_sent` 退款。未知结果保留 `unknown` 费用状态。
- `uv run --no-sync pytest tests/unit/test_budget_guard.py tests/unit/test_dataset_manifest.py -q --disable-socket --allow-unix-socket --tb=line` 在测试提交 `6e09885` 的结果为 11 失败、20 通过。在实现提交 `e9c6832` 上 31 项全部通过。完整冻结哈希、本机与容器全量结果见 `docs/handoff/PR-M1.1.md`。

**Planner 预算绕过的原始复现。** 在 `5305189`，`bootstrap.py` 将注入的 Planner 原样提供给调用方。已取消的 QA 在未预留 attempt 时调用 `container.planner_provider.decide`，Fake 收到 1 次调用并返回 `final_answer`，该问题账本的 `total_attempts` 仍为 0。下面的复现可在修复前后从仓库根目录用 `uv run --no-sync python` 运行。

```python
import asyncio

from video_agent.adapters.llm.fake import FakePlannerProvider
from video_agent.bootstrap import build_offline_container
from video_agent.domain.errors import BudgetError
from video_agent.domain.ports import PlannerRequest

fake = FakePlannerProvider()
app = build_offline_container(planner_provider=fake)
app.budget_guard.ensure_started("audit-q", "qa")
app.budget_guard.cancel("audit-q")
request = PlannerRequest(
    question_id="audit-q", analysis_id="audit-analysis", job_id="audit-job",
    asset_id="demo", prompt_text="offline probe", prompt_bundle_hash="h",
)
try:
    result = asyncio.run(app.planner_provider.decide(request)).action
except BudgetError as exc:
    result = exc.code.value
print(result, len(fake.calls), app.budget_guard.get_ledger("audit-q").total_attempts)
```

### 2026-11-25 Planner 预算阻断项已修复

同一复现在修复前输出 `final_answer 1 0`，在 `f8797fa` 输出 `BUDGET_EXCEEDED 0 0`。提供有效预留后取消 QA，返回 `JOB_CANCELLED`。预留后耗尽任务时间，返回 `DEADLINE_EXCEEDED`。这些拒绝均发生在适配器调用前。

- `budget_guard` 在锁内核对 QA 的 `question_id`、`job_id`、Provider、模型、截止时间和凭据使用状态。底层收到的超时取请求上限、预留上限、当前剩余时间的最小值。同一模型兼任 Planner 与 Vision 时，同一凭据在两个端口间也只能使用一次。
- 内存账本为每个 `attempt_id` 单独记录是否已发送。零图片的 Planner 调用在进入适配器前即计为 1 次，共用存储的另一个 BudgetGuard 也可读到。已发送的调用不能按 `not_sent` 退款。调用方用原凭据结算，用新凭据重试。
- 模拟一次发送后超时、一次限流、一次成功，Fake 收到 3 次调用，账本留下 3 个独立 attempt 和 0 张实发图片。超时结果的费用为 `unknown`，预估费用保留。两个线程争用同一凭据时，只有 1 次底层调用。

本轮基线为 `5305189de4bc849409ef81094be09ab2267ca3f3`。Step A 提交为 `2903efed3ec676cf72c2d0fc02ff65b0375bc6e5`，测试 diff SHA-256 为 `af1d69335e749b30f6f09850f0d9ec2d2fd5c252f43994b6ccac2f285cbc2e7a`。Step B 提交为 `f8797facf03ad9273da55c436cd5f48327447b00`。`git diff --exit-code 2903efe f8797fa -- tests` 退出 0。

```sh
uv run --no-sync pytest tests/unit/test_budget_guard.py -k planner -q --disable-socket --allow-unix-socket --tb=line
uv run --no-sync ruff check src experiments tests
uv run --no-sync ruff format --check src experiments tests
uv run --no-sync mypy src/video_agent experiments tests
uv run --no-sync lint-imports
uv run --no-sync pytest -q -m "not provider_live and not model_local" --disable-socket --allow-unix-socket
uv run --no-sync pytest tests/contract tests/unit/domain tests/unit/test_prompt_registry.py tests/unit/test_budget_guard.py -q -m "not provider_live and not model_local" --disable-socket --allow-unix-socket
uv run --no-sync pytest tests/integration/test_sqlite_contract.py tests/integration/test_migrations.py tests/architecture -q --disable-socket --allow-unix-socket
```

定向命令在 Step A 退出 1，11 项失败，能观察到取消后仍发送、同一凭据两次发送及已发出仍记零。Step B 的 11 项全部通过。本机 Python 3.14.6 上述质量门全部通过，全量为 165 项，指南两组为 119 项与 15 项，分层合同 2 项均通过。单独运行 `uv run --no-sync mypy tests/unit/test_budget_guard.py` 时 mypy 未将 src 包作为输入，报 `import-untyped`。仓库规定的完整 mypy 命令检查 56 个源文件通过。`uv run --no-sync python -m eval.manifest --verify-disk` 确认 6 段开发素材的大小及哈希。

从实现提交提取 Dockerfile 声明的 86 个输入构建镜像，再按镜像摘要运行验收。Linux Python 3.14.10 下独立 Python 子进程与 curl 均被阻断，离线 doctor 通过，全量 165 项通过。

```sh
docker build --progress=plain --iidfile ../planner-image.id -f Dockerfile.ci .
docker run --rm --pull=never --network none --workdir /app \
  -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 \
  sha256:c0ad685b7e149c649ca6f97aaed610493c3f5f50a133f0572726924cdfb4f7cd sh -ec '
    git --version &&
    .venv/bin/python tests/canary/network_denied.py &&
    .venv/bin/python -m video_agent.cli doctor --json &&
    .venv/bin/python -m pytest -q -m "not provider_live and not model_local" --disable-socket --allow-unix-socket --tb=line
  '
```

本轮使用本地 Fake，真实模型请求和外发图片均为 0。单独的 `provider_live` 与 `model_local` 作业未运行。M1.1、M1.2、M1.3、M1.4、M1.6、M1.7 的正式签收仍待独立审核与合并，原六项交接缺失的 Step A 提交历史仍待审查处理。后续 SQLite BudgetStore 需原子保存使用记录与发送标记，保留同一快照的读取语义。

