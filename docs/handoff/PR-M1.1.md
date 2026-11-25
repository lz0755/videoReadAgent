# PR-M1.1 交接
- 状态：待审核（不自批为已合并）；关联验收：M1-A1、M1-A3
- 前置交接链接：`docs/handoff/PR-M0.3.md`、`docs/decisions/ADR-001.md`
- 基线 commit、Step A commit 与测试 diff hash：基线 `5e3d4db`（未执行 git commit）；Step A 冻结测试文件 SHA-256（Step B 全程零改动）：
  - `tests/architecture/test_import_boundaries.py`: `60A1B55916E4E83D816D8E721C32FBD415B2B8478A3F84B2556004CB17A2C2B2`
  - `tests/contract/test_runtime_modes.py`: `180B082DCA4B976FE3C14C3F7C62C87FA46F288790A34755C1B8AC5E6B1C2DC0`
  - `tests/canary/network_denied.py`: `6D3B180D930D59877E0136A2F3BB9E0E4BBB875EB60B56907243C8F39CC50BAB`
- 本次单一行为与允许修改路径：正式配置（`config/defaults.yaml`、`config/prices.yaml`）、空模块分层骨架（`src/video_agent/{domain,application,agent,adapters/{media,asr,llm,storage,storage/migrations},api,ui}/__init__.py`、`bootstrap.py`、`worker.py`）、离线 CLI `video-agent doctor`（`src/video_agent/cli.py`）、批准依赖与分层门配置（`pyproject.toml`、`uv.lock`）、隔离 CI 与网络 canary（`Dockerfile.ci`、`.github/workflows/ci.yml`、`tests/canary/network_denied.py`、`tests/architecture/test_import_boundaries.py`、`tests/contract/test_runtime_modes.py`）。
- 代码真相源链接及合同是否变化：`config/defaults.yaml`、`config/prices.yaml`、`pyproject.toml`（新增 `pydantic>=2.10.0`、`jinja2>=3.1.6`、`portalocker>=2.10.1`、`[project.scripts] video-agent`、`[tool.importlinter.contracts]`）、`src/video_agent/cli.py`。
- 有效红灯：
  - 命令：`uv run --no-sync pytest tests/architecture/test_import_boundaries.py tests/contract/test_runtime_modes.py -q --disable-socket --allow-unix-socket --tb=line`
  - 断言与退出码：exit 1（`4 failed, 8 passed in 0.30s`；缺失 `config/defaults.yaml`、`Dockerfile.ci` 及 Step 0 `NotImplementedError`，零 `ImportError`）
- 绿灯：
  - `uv run --no-sync pytest tests/architecture tests/contract/test_runtime_modes.py -q --disable-socket --allow-unix-socket` → exit 0（`12 passed`）
  - `uv run --no-sync video-agent doctor --json` → exit 0（`ok: true`, `mode: "offline"`, `cloud_credentials: notice`）
  - `uv run --no-sync lint-imports` → exit 0（`Contracts: 2 kept, 0 broken`）
  - `uv run --no-sync ruff check src experiments tests` / `uv run --no-sync mypy src/video_agent experiments tests` → exit 0
- Fixture Self Check、真实集成与断网验证结果：AST 架构门验证 4 类正/负夹具（合法相对导入、跨层相对导入、`TYPE_CHECKING` 偷渡、别名动态导入、未安装外部包拦截）；Python 层 `--disable-socket --allow-unix-socket` 全部通过；无 Key 导入和离线 `doctor` 零网络调用。
- 未运行检查及原因：
  1. `docker build -f Dockerfile.ci` 与 `docker run --network none` OS 级断网验收未在本机运行（本机 Docker Desktop 守护进程未启动：`open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified`）。当前仅验证 Python 层 `--disable-socket`，不宣称已完成 OS 级进程树无外联实测。
- 实际模型/HTTP/工具/图片调用数；estimated/confirmed/unknown：0 次模型调用、0 次 Provider HTTP 调用、0 张外发图片；`confirmed`（0.0 USD；`uv sync` 为显式依赖准备阶段，已与离线测试分离）。
- 依赖锁、模型、Prompt bundle、Schema 与数据版本：`uv.lock` 新增锁定 `jinja2==3.1.6`、`markupsafe==3.0.4`、`portalocker==4.4.2`（`pydantic==2.14.4` 已由原锁解析）。
- 已知限制、恢复/回滚方式、独立审查结论：需在启用 Docker 的 CI 或本机启动 Docker Desktop 后执行 `.github/workflows/ci.yml` 中的 `os-network-isolated-acceptance` 作业以完成 OS 断网闭环；回滚可还原 `pyproject.toml`/`uv.lock` 并移除新增骨架文件；待独立审查。
- 下一PR需要读取的文件和仍未解决问题：M1.2 读取 `AGENTS.md`、`config/defaults.yaml`、`config/prices.yaml`、`docs/handoff/PR-M1.1.md`。
- 独立审查返工修复（两轮 TDD，状态仍保持 `待审核`）：
  - Step A 冻结测试哈希（Step B 零改动，除 `test_baseline_report.py` 夹具路径修正外）：
    - `tests/contract/test_runtime_modes.py`: `EAB74FA3B7ED7D00082CA9C694CF6A97F98CAB2973E93C87B2D94CEC730A30D0`
    - `tests/unit/test_dataset_manifest.py`: `A3136510D2D321508B7FEBD3855E6A2088322BE07B238C425B98E088B3E544D2`
    - `tests/contract/test_baseline_report.py`: `4C08D3A1109581BC1697260250F19C00F6D17128C9556BBB692A7FE673C315F9`（仅修正合成夹具复制路径 `experiments/capabilities.json`，零断言改动）
  - 修复内容：`Dockerfile.ci` 补齐 `COPY Dockerfile.ci`、`COPY .github`、`AGENTS.md`、`.gitattributes`；`.github/workflows/ci.yml` 格式门统一为 `uv run --no-sync ruff format --check src experiments tests` 并格式化历史遗留的 `src/main.py`；`src/video_agent/cli.py` 使用严格 `AppConfig.model_validate` 校验配置；无本机 `data/` 的干净检出由合成 FFmpeg/Pillow 夹具完成全量离线测试。
  - 验证结果：严格按 `Dockerfile.ci` `COPY` 清单复制到无 `data/`、无 `.git/` 的临时目录复测，`video-agent doctor --json` 退出 0 且 `pytest -q -m "not provider_live and not model_local" --disable-socket --allow-unix-socket` 全量 `143 passed in 13.12s`；`uv run --no-sync ruff format --check src experiments tests` 退出 0（`58 files already formatted`）。本机 Docker 守护进程仍未启动，真实 `--network none` 容器内 Python/curl 探针实测仍待在可用 Docker 环境或 CI runner 补跑。
  - 提交流水说明：当前工作区仍基于 `5e3d4db` 未做 git commit，不伪造事后 Step A 提交，待审查者确认拆分或提交策略。

### 2026-11-25 独立复测 · 真实 Docker 断网验收

复测使用 `5e3d4db` 上的工作区内容，按 `Dockerfile.ci` 的 `COPY` 清单准备了 86 个输入文件。构建上下文没有本机素材或 Git 元数据。清单摘要为 `4e2034f7eb741371113323608a7f564245bf919ebd113bbd05744a64efc72d08`，计算方式为按相对路径排序，将每项的路径、NUL、文件 SHA-256 小写十六进制和 LF 拼接后计算 SHA-256。构建阶段允许下载依赖。验收容器使用 `--network none`，没有宿主目录或凭据挂载。

| 环境 | 结果 |
| --- | --- |
| Windows CPython 3.14.6 工作区 | 全量离线测试 143 通过，1 条 pytest 缓存写入警告。Ruff 检查、全量格式检查、mypy 和两条 import-linter 合同通过。 |
| 全新 Windows 虚拟环境，同一份源码，无本机素材 | `uv sync --frozen --offline --no-editable` 成功，离线测试 143 通过。 |
| 仓库 Dockerfile 构建的 Linux 镜像，CPython 3.14.10，`sha256:05f671dc8e938de44b5dc42b52cd3fda4fb99b466ce7442cf617ff0f1c83634e` | Python 子进程和 curl 断网探针退出 0，doctor 退出 0。pytest 退出 1，141 通过、2 失败。基线报告重建和 Prompt 的 Git 检出测试均报 `FileNotFoundError: git`。 |
| 仅在构建阶段额外安装 Git 的临时对照镜像，`sha256:672336ffd3634439776cde82a4b2aeb68e6b567de5256c923d99c68bfa1b5b7c` | 相同断网探针、doctor、pytest 均退出 0，143 通过。仓库镜像仍需补齐 Git 并重新验收。 |

Windows 复跑了 `AGENTS.md` 的四条质量命令和 `uv run --no-sync lint-imports`。指南 M1 小节的两条 pytest 命令分别通过 104 项和 15 项，上轮问题对应的 6 项回归测试单独运行也全部通过。附加执行的 `git diff --check -- pyproject.toml src/main.py tests/contract/test_baseline_report.py tests/unit/test_dataset_manifest.py uv.lock` 退出 1，报告 `pyproject.toml:88` 有一个新增的文件末尾空行。

在构建上下文中执行的命令如下。第二条使用第一条生成的镜像摘要。

```sh
docker build --progress=plain --iidfile ../m1-audit-image.id -f Dockerfile.ci .
docker run --rm --pull=never --network none --workdir /app \
  -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 \
  "$(cat ../m1-audit-image.id)" sh -ec '
    .venv/bin/python tests/canary/network_denied.py &&
    .venv/bin/python -m video_agent.cli doctor --json &&
    .venv/bin/python -m pytest -q -m "not provider_live and not model_local" --disable-socket --allow-unix-socket --tb=line
  '
```

另在临时目录放入 5 个故意损坏的占位文件并缺少第 6 个文件，`tests/unit/test_dataset_manifest.py::test_repo_dev_seed_manifest_and_24_questions_valid` 仍通过。该测试在素材不全时改验合成夹具，用 `verify_files_on_disk=True` 检查同一批文件则报字节数不符。干净检出与已提供但不完整的素材需要分别验证。本轮未运行 `provider_live` 或 `model_local`。

### 2026-11-25 镜像、素材与 M1 验收复查

本地审查分支 `review/m1-acceptance-fixes-f7837279` 以 `6cce019` 保存修复前的工作区。测试提交为 `6e09885466caa87e029243af933f3adc258cac2f`，相对基线的 `git diff --binary 6cce019 6e09885 -- tests` 的 SHA-256 为 `1950c104a63d6cb44d5a999a4969548bc8ceb993d7f5096da3f4649d00d7bfaf`。实现提交为 `e9c68324741fe5d5960365cd807a9028bdb4b6cc`。`git diff --exit-code 6e09885 e9c6832 -- tests` 退出 0。

- 原镜像在断网下运行基线报告与 Prompt 检出两项原有测试，均因找不到 `git` 失败。修复后的镜像安装 Git 2.47.3，从实现提交提取 Dockerfile 声明的 86 个构建输入。镜像使用 Python 3.14.10，构建阶段下载系统包及锁定依赖。
- 验收容器未挂载宿主目录。独立 Python 子进程和 curl 的外联均被阻断，离线 `doctor` 通过，全量 pytest 通过 154 项。镜像内的干净检出验证清单与 24 题，并用合成文件实际执行大小和哈希校验。
- 本机 Python 3.14.6 执行 `uv run --no-sync python -m eval.manifest --verify-disk`，6 段实际开发素材全部通过大小及 SHA-256 校验。任一清单指定的素材目录存在就要求校验全部文件。临时检出放入 5 个损坏文件并缺少第 6 个时，仓库自己的素材验收测试退出 1 并报告 `byte_size mismatch`。回归还覆盖空目录、单个文件缺失、大小不符及等长内容损坏。

在干净构建目录执行的构建和容器命令如下。

```sh
docker build --progress=plain --iidfile ../m1-audit-image.id -f Dockerfile.ci .
docker run --rm --pull=never --network none --workdir /app \
  -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 \
  sha256:89e7a12971ff59ced36d29e74bcaad2e90fc26fe91abbc5f7e3150676179d4bf sh -ec '
    git --version &&
    .venv/bin/python tests/canary/network_denied.py &&
    .venv/bin/python -m video_agent.cli doctor --json &&
    .venv/bin/python -m pytest -q -m "not provider_live and not model_local" --disable-socket --allow-unix-socket --tb=line
  '
```

本机质量门和 M1 指南规定的两组命令均退出 0。

```sh
uv run --no-sync ruff check src experiments tests
uv run --no-sync ruff format --check src experiments tests
uv run --no-sync mypy src/video_agent experiments tests
uv run --no-sync lint-imports
uv run --no-sync pytest -q -m "not provider_live and not model_local" --disable-socket --allow-unix-socket
uv run --no-sync pytest tests/contract tests/unit/domain tests/unit/test_prompt_registry.py tests/unit/test_budget_guard.py -q -m "not provider_live and not model_local" --disable-socket --allow-unix-socket
uv run --no-sync pytest tests/integration/test_sqlite_contract.py tests/integration/test_migrations.py tests/architecture -q --disable-socket --allow-unix-socket
```

本机全量通过 154 项，两组 M1 测试分别通过 108 项和 15 项，分层合同通过 2 项。上面的测试未调用云端 Provider 或加载本地模型。Planner 的复现和剩余签收条件见 `docs/handoff/PR-M1.7.md` 本日的预留凭据复查。

