# M6.3 本机发布包与复现

从版本库根生成确定性 ZIP。归档只选择公共源码、锁、配置、Prompt、测试、清单及交付文档。在 ZIP 根目录，`SHA256SUMS` 记录每个源文件的精确字节。相同工作树在同一打包环境生成相同字节。输出文件须为新路径。

```powershell
python docs/runbooks/package_release.py --output dist/video-agent-m6.zip
Get-FileHash -Algorithm SHA256 -LiteralPath dist/video-agent-m6.zip
```

安装入口在 [README](../../README.md)，本地 API、页面、单 Worker 和 CLI 操作见[恢复手册](../runbooks/recovery.md)。本机准备的模型权重另供显式模型作业使用。真实供应商质量、GPU 资源及 macOS 兼容性尚待验证。

### Windows 干净安装和恢复

解压到空目录，逐个核对包内 SHA256SUMS。使用宿主机预热的 uv 依赖缓存，在解压目录内新建独立虚拟环境。首次离线安装因沙箱不能锁定宿主 uv 缓存而退出，获准使用该缓存后，相同安装命令离线完成，CPython 3.14.6、uv 0.13.9 安装 81 个锁定包。doctor 返回 ok=true，SQLite 3.50.4 启用 FTS5，迁移应用 1 和 2。先行版本的恢复、替换、双格式导出及评测冒烟共 28 passed，47.93 秒。

```powershell
uv sync --frozen --offline --no-editable --link-mode copy
$env:UV_CACHE_DIR='data/uv-run-cache'
uv run --no-sync video-agent doctor --data-root data/local --json
uv run --no-sync video-agent init --data-root data/local --writers-stopped
uv run --no-sync pytest -q tests/integration/test_job_recovery.py tests/integration/test_worker_lock.py tests/integration/test_uncommitted_attempt.py tests/integration/test_index_resume.py tests/integration/test_eval_runner.py tests/contract/test_m6_replay.py tests/e2e/test_local_delivery.py --disable-socket --allow-unix-socket -p no:cacheprovider --tb=line
```

### 性能基线与规模样本

在同一台 Windows 机器上，用原 6 段 D0 开发输入、相同离线策略和原 M0 程序复测。M0 原总耗时 3804 ms，本次首次复测 4393 ms（+15.5%）。D0 哈希校验 84 ms，夹具自检 2357 ms，包级解复用 776 ms，Provider 请求离线序列化 1172 ms；主进程峰值 124.07 MiB。解复用 RTF 由 0.00077 变为 0.00087。序列化阶段相对 M0 的 939 ms 增长 24.8%，该路径和依赖锁在 M6 未修改。

```powershell
uv run --no-sync python -m experiments.baseline_runner --output data/m6/baseline/current.json --fixtures-dir data/m6/baseline/fixtures
```

两次追加复测总耗时 4703、4696 ms，序列化分别为 1039、1033 ms。为定位超过 20% 的历史差异，又在同一解释器和同一批 D0 输入上交替运行 M5 基线提交 `305c1c6` 和 M6 工作树，各两次。M5 为 4992/4801 ms，M6 为 4599/5112 ms，两组平均为 4896.5 和 4855.5 ms。五次 M6 总耗时中位数为 4696 ms，较历史 M0 高 23.4%。夹具生成/自检的中位数由历史 2088 ms 增至 2691 ms，解复用由 688 ms 增至 852 ms。同机对照未检出 M6 专属的同量级回退。跨日差异的系统级原因尚未确定。此对照仅使用 Fake 与离线序列化。

```powershell
foreach ($trialName in @('control2','control3')) {
  uv run --no-sync python -m experiments.baseline_runner --output "data/m6/baseline/$trialName.json" --fixtures-dir "data/m6/baseline/$trialName-fixtures"
}
git archive --format=zip --output=data/m6/baseline/m5-control.zip 305c1c6 -- src eval experiments tests
python -m zipfile -e data/m6/baseline/m5-control.zip data/m6/baseline/m5-control
@'
import subprocess, sys
from pathlib import Path
root = Path.cwd()
dest = root / "data/m6/baseline"
program = """import sys
from pathlib import Path
code, root, dest, label = sys.argv[1:]
sys.path[:0] = [code, str(Path(code)/'src')]
from experiments import baseline_runner as runner
assert Path(runner.__file__).resolve().is_relative_to(Path(code))
runner.run_m0_baseline(repo_root=Path(root), output_path=Path(dest)/(label+'.json'),
                       fixtures_dir=Path(dest)/(label+'-fixtures'))
"""
for label, code in [("m5-a", dest/"m5-control"), ("m6-a", root),
                    ("m5-b", dest/"m5-control"), ("m6-b", root)]:
    subprocess.run([sys.executable, "-c", program, str(code), str(root), str(dest), label], check=True)
'@ | .\.venv\Scripts\python.exe -
```

目标规模实测使用本机生成的 H.264/AAC 样本。ffprobe 自检为 1920×1080、2 fps、1200 个视频包、600000 ms。SHA-256 为 `ac7ae783d4f066a691ee70ea01edd8674e01ee5d38a3c63a24336ab4cf527ad9`。M3 索引 30 个窗口、120 个唯一采样帧、149 个 Fake 图像发送部件，音轨状态 ok。索引 17.29 秒，RTF 0.0288；含一次 QA 的总耗时 17.64 秒。仅衡量此单色合成输入的 Fake 管线。

```powershell
ffmpeg -nostdin -v error -f lavfi -i 'color=c=blue:s=1920x1080:r=2:d=600' -f lavfi -i 'sine=frequency=440:sample_rate=16000:duration=600' -c:v libx264 -threads 2 -preset ultrafast -g 20 -pix_fmt yuv420p -c:a aac -b:a 32k -t 600 data/m6/target-1080p/generated.mp4
uv run --no-sync python -m eval.runner --manifest data/m6/target-1080p/manifest.json --media-root data/m6/target-1080p --offline --output-dir data/m6/target-1080p-run
```

生成前创建存放该样本的目录。在同一目录将下行另存为 `manifest.json`，其中 sha256 使用本机 ffmpeg 输出的实际摘要；不同编码器构建可能生成不同字节。

```json
{"fixture":true,"media_artifacts":[{"artifact_id":"target_600","split":"m0_dev","media_kind":"video","relative_path":"generated.mp4","sha256":"ac7ae783d4f066a691ee70ea01edd8674e01ee5d38a3c63a24336ab4cf527ad9","source_group_id":"generated","original_id":"target","benchmark":"generated","authorization":{"usage_scope":"synthetic local performance evaluation"}}],"regular_qa":[{"question_id":"target_qa","video_id":"target_600","question_text":"observed"}],"localization_requests":[]}
```
