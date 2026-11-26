# PR-M2.0 八类合成媒体的独立自检

**Step A 和 Step B 均通过独立审核。** 基线提交为 `c030a6ed16a2e653f73d1c6bf6c68520fa05a802`。本 PR 对应 M2-A1 的夹具前提。M1.5 的正式评分为可答题 4/9，要求至少 7/9。ADR-002 第六节允许先行 M2.0 离线工程。[切片决策](../decisions/ADR-002.md) · [M1.5 交接](PR-M1.5.3.md) · [原四类夹具与冻结容差](PR-M0.2.md)

### 实测结果

| 样本 | 直接观测 |
| --- | --- |
| F01 恒定帧率 | 75 帧，相邻展示时间差均为 40 ms。第 0、1、2 秒各自的白色方块可从解码像素读出，音调在 0 ms 起振。 |
| F02 可变帧率 | 52 帧，实际相邻展示时间差为 40、100、120 ms。 |
| F03 非零原点 | 首帧为 5000 ms，末帧为 7960 ms。 |
| F04 晚起音轨 | 解码音频首帧为 178 ms，PCM 音调在 196 ms 起振。两项分别落在原定的 200 ± 30 ms 和 200 ± 20 ms 内。 |
| F05 旋转 | ffprobe 读出 90° 显示矩阵，PyAV 解码的红角标在左上，FFmpeg 按矩阵显示后红角标在左下。 |
| F06 无音轨 | ffprobe 和 PyAV 均读到 0 个音轨，视频首帧可解码。 |
| F07 长 GOP 与 B 帧 | 关键帧在 0、2000、4000 ms，有 90 个 B 帧。请求 3020 ms 时先落在 2000 ms，前向解码与从头顺序解码均选中 3040 ms，两个结果逐像素相同。 |
| F08 截断 | 保留 F07 的前 41525 个字节，完整文件为 83051 字节。前缀逐字节匹配，ffprobe 报缺失 moov，PyAV 抛出 InvalidDataError。 |

每次测试都会重新生成并检查媒体。本机在第二个目录独立重建后，8 个媒体文件的 SHA-256 与清单全部一致。另有四次反例测试，逐个用普通含音轨的 CFR 文件替换 F05–F08，四种冒名输入均被对应的自检拒绝。生成命令、版本、内容哈希、观测和断言均在[本次清单](../../tests/fixtures/manifests/f01_f08_manifest.json)。[生成入口](../../tests/fixtures/media_factory.py) · [独立观测](../../tests/fixtures/p0_media.py) · [验收测试](../../tests/integration/test_fixture_self_check.py)

### 复验与红灯

当前 Windows 沙箱的默认 uv 缓存锁拒绝访问。在项目内的忽略目录使用下列设置可复验。

```powershell
$env:UV_CACHE_DIR = Join-Path $PWD 'data/.uv-cache'
uv run --no-sync python -m tests.fixtures.media_factory --suite p0 --output-dir data/fixtures/m2_p0 --manifest-out data/fixtures/m2_p0/recheck.json
uv run --no-sync pytest tests/integration/test_fixture_self_check.py -q --disable-socket --allow-unix-socket --tb=line -o cache_dir=data/.m2-pytest
uv run --no-sync ruff check src experiments tests
uv run --no-sync ruff format --check src experiments tests
uv run --no-sync mypy src/video_agent experiments tests --cache-dir data/.m2-mypy
uv run --no-sync lint-imports --no-cache
uv run --no-sync pytest tests/architecture -q --disable-socket --allow-unix-socket -o cache_dir=data/.m2-architecture
uv run --no-sync pytest -q -m "not provider_live and not model_local" --disable-socket --allow-unix-socket -o cache_dir=data/.m2-pytest --tb=line
```

首次生成时 `--manifest-out` 使用了 `tests/fixtures/manifests/f01_f08_manifest.json`。生成命令退出 0，8/8 自检通过。基线的六项旧自检通过。在添加 P0 工厂前，新测试曾因 CLI 缺少 `--suite p0` 在夹具准备阶段退出。补齐并证明夹具后，上述定向测试退出 1，11 项通过、1 项行为失败。对实测 90° 样本，`probe_media_timeline(...).to_dict().get("rotation_degrees")` 返回 `None`。下一轮生产白名单仅为 `experiments/media_probe.py`，涉及显示矩阵、关键帧时间和 B 帧数的输出。

完整默认测试退出 1，214 项通过，唯一失败是同一处旋转属性断言。Ruff 检查和格式检查、mypy、import-linter 分别退出 0。import-linter 保持两项合同，AST 架构测试 7 项通过。仅使用本地生成的合成视频。模型推理、外部 HTTP 调用、外发图片和云端费用均为零。未运行 `provider_live`、`model_local` 或操作系统级断网容器。

### 冻结测试差异

按 Git 路径排序合并四个文件的 `git diff --binary` 后，SHA-256 为 `e2c997283626117feaea67142bd4f3499274e3747d4b5bfab45f07079cf582f0`。已跟踪文件对比 HEAD，新文件使用 `git diff --no-index --binary -- /dev/null <path>`。独立审核后暂存差异的哈希与此记录相同，Step A 提交为 `b2f6232`。

| 文件 | 本地文件 SHA-256 |
| --- | --- |
| `tests/fixtures/manifests/f01_f08_manifest.json` | `e7c614fc5ba018a8713211d1d164f40e155a11b5c2d8b643103b4d513047bc41` |
| `tests/fixtures/media_factory.py` | `ba8032cbd77339416b056951c13b58ce288f47f43ea638d89283e3e271b92d50` |
| `tests/fixtures/p0_media.py` | `9e8a33b667006e0585a6114c3644356ba75c13d4746d3a980cafd16a436aacc8` |
| `tests/integration/test_fixture_self_check.py` | `b400514adcbbc5c214c9d4998beda51349282804bbc44ae91b70de89c3728c7a` |

### 实现与复核

探针从 ffprobe 的显示矩阵、解码帧类型和关键帧标记读取旋转角、B 帧数与相对原点的关键帧时间。`uv run --no-sync pytest tests/integration/test_fixture_self_check.py tests/integration/test_media_probe.py -q --disable-socket --allow-unix-socket -o cache_dir=data/.m2-pytest --tb=line` 退出 0，18 项通过，独立审查复跑结果一致。用户批准后续 M2 离线工程，上游 M1.5 质量门保持 4/9。下一依赖为 M2.1 受管导入和 M2.2 实际 PTS 采样。
