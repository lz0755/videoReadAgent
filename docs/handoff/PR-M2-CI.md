# PR-M2-CI 干净检出的离线切片测试

提交 `e86ea86` 的 Docker 镜像在 `--network none`、无宿主挂载的环境中运行全量离线测试，结果为 262 项通过、7 项失败、9 项报错、1 项未选中。其中 13 项在读取私有 D0 视频时中断，预算拒绝、失败保留和凭证校验等断言尚未执行。另 3 项 PTS 合成夹具的跨版本处理见专项交接。[PTS 夹具](PR-M2.2-ci.md)

## 数据与入口

临时目录中的 F01、F02、F04 分别覆盖恒定帧率、可变帧率和音频延迟。测试用 FFmpeg 生成素材，复用各自的自检，以 PyAV 读取流时长，并从磁盘逐一校验三个不同的 SHA-256 和文件大小。独立 manifest 和注释按每段 3 道可答、1 道不可答绑定题号。视频 ID 使用 `synthetic_` 前缀，benchmark 为 `synthetic_ffmpeg_fixture`，授权记录写明本地生成。配置、价格表和已登记的 Prompt 使用仓库中的版本。[合成数据集](../../tests/fixtures/slice_dataset.py)

每次运行都覆盖完整的合成切片。有授权 D0 媒体目录时，额外运行真实 D0 的三段十二题，核对报告的素材 ID、SHA-256、benchmark、题号顺序、引用、费用计量和去敏。原有 D0 自检继续校验六段、二十四题、实际文件完整性及损坏或不完整数据集的失败行为。[切片报告测试](../../tests/unit/test_slice_report.py) · [D0 完整性测试](../../tests/unit/test_dataset_manifest.py)

`eval.slice_runner.main(argv, *, repo_root=None)` 将显式根传给 `run_slice_manifest`。省略参数时沿用仓库根。该入口通过同一个根加载注释、视频、配置和 Prompt；媒体摘要和授权仍通过既有严格校验。[入口](../../eval/slice_runner.py)

## 冻结与验证

签名骨架为 `583eec1`。测试提交 `4eb4f2d` 的真实红灯为 11 项失败、13 项通过，合成文件已通过自检。入口忽略显式根时，在错误位置查找 `eval/annotations/synthetic_slice.json`。六份测试及夹具按文件名排序、内容统一 LF 的 SHA-256 为 `b32b2ec277dce507e07121f3be90c12868256a8f76ea44815d05b3a1ed09eb69`。相对于 `e86ea86`，六份文件逐个 Git binary diff 拼接的 SHA-256 为 `d764e8f07cc38ec7a8601f66632a15a2668b31ed42b92f73c7885e4defea3456`。独立审核通过后修改入口的一行参数传递。

| 验证环境 | 结果 |
| --- | --- |
| Windows，已置备授权 D0 | 四个受影响测试文件 24 项通过，包含合成数据和 D0 两次完整切片运行。D0 完整性与架构测试另有 26 项通过。 |
| Linux，原红灯镜像，`--network none`，无凭证、无挂载 | 四个受影响文件加 D0 完整性测试共 42 项通过，耗时 9.56 秒。容器从标准输入接收下列七个文件，使用镜像内锁定的工具与依赖。 |
| 静态门 | Ruff 检查及格式通过，mypy 通过 94 个源文件；两项 import-linter 合同通过。 |

Windows 专项命令如下。在 `4eb4f2d` 上首条命令可重现入口红灯。

```powershell
.venv/Scripts/pytest.exe tests/integration/test_slice_repair.py tests/integration/test_slice_review_history.py tests/unit/test_openai_slice.py tests/unit/test_slice_report.py -q --tb=line --disable-socket --allow-unix-socket -o cache_dir=data/.m2-ci-tests
.venv/Scripts/pytest.exe tests/unit/test_dataset_manifest.py tests/architecture -q --tb=line --disable-socket --allow-unix-socket -o cache_dir=data/.m2-ci-arch
.venv/Scripts/ruff.exe check src experiments tests eval/slice_runner.py
.venv/Scripts/ruff.exe format --check src experiments tests eval/slice_runner.py
.venv/Scripts/mypy.exe src/video_agent experiments tests --cache-dir data/.m2-ci-mypy
.venv/Scripts/lint-imports.exe --no-cache
```

Linux 专项复验使用本机已缓存的原镜像，仅覆盖本次测试与入口。

```powershell
$dockerArgs = @('--config','data/reports/m2_os_gate/e86ea869a9fd/docker-client','--host','npipe:////./pipe/dockerDesktopLinuxEngine')
$ciPaths = @('eval/slice_runner.py','tests/conftest.py','tests/fixtures/slice_dataset.py','tests/integration/test_slice_repair.py','tests/integration/test_slice_review_history.py','tests/unit/test_openai_slice.py','tests/unit/test_slice_report.py')
$ciData = @{}
foreach ($ciPath in $ciPaths) { $ciData[$ciPath] = [Convert]::ToBase64String([IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $ciPath))) }
$ciScript = "from pathlib import Path; import base64, json, sys; payload=json.load(sys.stdin); [Path(name).write_bytes(base64.b64decode(content)) for name,content in payload.items()]; import pytest; raise SystemExit(pytest.main(['tests/integration/test_slice_repair.py','tests/integration/test_slice_review_history.py','tests/unit/test_openai_slice.py','tests/unit/test_slice_report.py','tests/unit/test_dataset_manifest.py','-q','--disable-socket','--allow-unix-socket','--tb=line']))"
$ciData | ConvertTo-Json -Compress | docker @dockerArgs run --rm --pull never --network none --workdir /app -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 -i video-agent-ci:m2-e86ea869a9fd .venv/bin/python -c $ciScript
```

专项使用离线提供方。云端质量以原有 M1.5 人工评分记录为准；完整镜像验收见 M2 汇总。[M2 交接](PR-M2.md)
