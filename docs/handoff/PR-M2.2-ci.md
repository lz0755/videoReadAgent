# PR-M2.2-ci 跨版本合成 PTS 夹具

Linux 系统级断网验收在 FFmpeg 7.1.5、PyAV 19.0.2 上复现了三项 M2.2 失败。原冻结文件 SHA-256 为 `663e15c09c14ff33a0a749bec1ef715308542a93e3b02b4e9facec19c286914d`。[M2.2 原始交接](PR-M2.2.md)

| 合成素材 | 修复前 Linux PyAV 可解码 PTS，毫秒 | 修复后可解码 PTS，毫秒 | 修复后容器时长，毫秒 |
| --- | --- | --- | --- |
| 高精度 MP4 | 0，999.9，1000 | 0，999.9，1000，1010 | 1510 |
| 稀疏 MP4 | 0，2960，3040 | 0，2960，3040，3120 | 3620 |
| 重复 PTS 的 MKV | 0，500，500，1500 | 0，500，500，1500 | 2000 |

原 Linux MP4 的容器分别结束于 1010 ms 和 3120 ms，末帧消失。原 MKV 容器结束于 1500 ms，使合法性检查先于重复 PTS 检查拒绝 1600 ms 的测试窗口。独立审查通过后，合成命令为 2 fps 测试源明确提供了每包 500 ms 的持续时间。输出位流过滤器 `setts=duration=5000` 使用编码器的 1/10000 秒时间基，保留原始 PTS 和 DTS。修复后测试文件的 LF 规范化 SHA-256 为 `2f32f9bac3aac8aac15ad1e84ac793a4e66628627408251eb3eb20ad58291441`。原有 10 个测试函数和参数化装饰器的 AST SHA-256 在修复前后均为 `4f13ac12725bb5dad2c49eeb556da6b4ff35a672d9ade13967f40fc9cd469507`。[合成器和冻结断言](../../tests/integration/test_pts_sampling.py)

## 复验

Windows 上使用 FFmpeg 8.1.2 和 PyAV 19.0.2 运行以下命令，专项 12 项通过，Ruff 检查与格式通过，仓库类型检查通过。单独用 `mypy tests/integration/test_pts_sampling.py` 检查时，本地应用包按第三方模块解析并报五条 `import-untyped`。仓库指定的命令将 `src/video_agent` 加入检查并通过了 94 个源文件。

```powershell
$env:UV_CACHE_DIR = Join-Path $PWD 'data/.uv-cache'
uv run --no-sync pytest tests/integration/test_pts_sampling.py -q --disable-socket --allow-unix-socket --tb=line -o cache_dir=data/.m22-pytest
uv run --no-sync ruff check tests/integration/test_pts_sampling.py
uv run --no-sync ruff format --check tests/integration/test_pts_sampling.py
uv run --no-sync mypy src/video_agent experiments tests
```

Linux 复验沿用发生红灯的本地镜像，使用标准输入替换临时容器中的测试文件。容器关闭网络、无主机挂载，原 12 项全部通过，全量 OS 隔离作业由 M2 验收线运行。

```powershell
$dockerArgs = @('--config', 'data/reports/m2_os_gate/e86ea869a9fd/docker-client', '--host', 'npipe:////./pipe/dockerDesktopLinuxEngine')
$image = 'video-agent-ci:m2-e86ea869a9fd'
$pytestScript = "from pathlib import Path; import sys, pytest; Path('tests/integration/test_pts_sampling.py').write_bytes(sys.stdin.buffer.read()); raise SystemExit(pytest.main(['tests/integration/test_pts_sampling.py','-q','--disable-socket','--allow-unix-socket','--tb=line']))"
Get-Content -Raw -Encoding UTF8 -LiteralPath tests/integration/test_pts_sampling.py | docker @dockerArgs run --rm --pull never --network none --workdir /app -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 -i $image .venv/bin/python -c $pytestScript
```
