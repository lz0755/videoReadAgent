# PR-M2.2 展示时间、选帧与播放映射

对应 M2-A1、M2-A3。M1.5 的 4/9 质量成绩按用户决定单独保留。[夹具与前置自检](PR-M2.0.md) · [切片适配器](PR-M1.5.1.md) · [领域模型](PR-M1.2.md) · [既有端口](PR-M1.3.md)

## Step A

两个新入口的签名已由独立审查者审核。注册资产、证据、时间区间和播放记录沿用 M1 模型。测试在磁盘临时目录生成并自检 F01–F08，额外生成四帧精细时间轴、重复 PTS 的 MKV、没有 PTS 的 H.264 裸流和实际 3000 → 3040 ms 取帧样本。长 GOP 检查通过 CPython profiler 观察对真实 PyAV 容器的 seek 调用。

```powershell
$env:UV_CACHE_DIR = Join-Path $PWD 'data/.uv-cache'
uv run --no-sync pytest tests/integration/test_pts_sampling.py -q --disable-socket --allow-unix-socket --tb=line -o cache_dir=data/.m22-pytest
```

Step A 运行退出 1，8 项均抵达签名占位并抛出 `NotImplementedError`。没有夹具、依赖或导入错误。[冻结测试](../../tests/integration/test_pts_sampling.py)

- 文件 SHA-256 为 `d5650f5d99b1087f8e87db820d5e14293a6a19338abf9341a4ee9e7a433adf1b`。
- 单文件 `git diff --no-index --binary -- /dev/null tests/integration/test_pts_sampling.py` 差异 SHA-256 为 `f5650fe3dbb3b4d25ca9449a2af3f25bb57d5eda2ff35b3640c8a4b6734875c7`。
- Step 0 签名提交为 `667d2bb`，首次独立复验红灯后冻结的测试提交为 `358c326`。首次生产实现对该提交的测试差异为空。

## Step B

`extract_frames` 从主视频首帧读取原点。对后段请求回跳到较早关键帧，再按照原始有理数时刻顺序解码。缺少可靠 seek 索引或实际落点超过请求时，从已知原点顺序解码。每次调用独立打开容器，逐帧读取，选中的图像即时写入受管存储，返回所请求的证据记录。同一实选帧可供多个请求复用。JPEG 应用实际显示矩阵的正交旋转并在证据中写入角度。遇到缺失、重复、倒退的 PTS 时返回 `TIMESTAMP_UNRELIABLE`。[流式选帧实现](../../src/video_agent/adapters/media/pyav_decoder.py)

`build_playback_map(asset)` 默认给出 `preview_only`。受信播放器的验证集须匹配播放器标识、资产、源产物与文件 SHA-256。首、中、尾三个测量点须在片内，覆盖各自目标位置且允许同一常量偏移。变更播放器、来源或实测关系后回到帧预览。当前验证仅覆盖测试中的 PyAV 展示时钟，浏览器实测与导航属于 M5。[播放映射实现](../../src/video_agent/application/media_timeline.py)

| 样本 | 已复核结果 |
| --- | --- |
| F02 / F03 | VFR 的下一帧等于区间末端时返回 `NO_FRAME_IN_RANGE`。F03 保留 5000 ms 原点，1010 ms 请求选中规范时间 1040 ms、原始展示时间 6040 ms。 |
| 精细时间轴 | 原始 999.9 ms 位于 [999, 1000)，公开值为 999 ms。下一帧恰为 1000 ms，另一区间选中它。稀疏样本请求 3000 ms 实选 3040 ms。 |
| F05 / F07 | 旋转后的 48 × 80 JPEG 红色角标在左下，证据保留 90°。长 GOP 实测调用了容器 seek，两个位于帧间的请求各自保留请求时间并都指向 3040 ms 的 JPEG。 |
| 无 PTS / 重复 PTS | 原始 H.264 无帧时间戳，MKV 的实际值为 0、500、500、1500 ms。两者均以时间不可靠报错。 |
| 已授权 HEVC 开发样本 | 在核对本地研究授权、文件长度及 SHA-256 后，`vmme_001` 请求 253501 ms 实选 253520 ms，`vmme_087` 请求 138617 ms 实选 138633 ms。两个结果的原始 PTS 和规范毫秒均与独立从头顺序解码相同。所有临时帧随检验清理。[授权与散列](../../eval/manifests/dev_seed.jsonl) |

```powershell
uv run --no-sync pytest tests/integration/test_pts_sampling.py tests/integration/test_slice_media.py tests/architecture -q --disable-socket --allow-unix-socket --tb=line -o cache_dir=data/.m22-pytest
uv run --no-sync ruff check src/video_agent/adapters/media/pyav_decoder.py src/video_agent/application/media_timeline.py tests/integration/test_pts_sampling.py
uv run --no-sync mypy src/video_agent experiments tests --cache-dir data/.m22-mypy
uv run --no-sync lint-imports --no-cache
```

22 项测试通过，Ruff、mypy 与两个 import-linter 合同均通过。首轮 mypy 指出 PyAV 的 `AVRational` 标注无法直接作为 Python `Fraction` 返回，以分子、分母显式构造后通过。另用 FFmpeg 生成 0°、90°、180°、270°、27° 的实际显示矩阵，前四个记录了相应角度，27° 得到 `CAPABILITY_MISMATCH`。本子项没有模型推理、外部 HTTP 请求或云端费用。未运行浏览器播放对照或操作系统级断网容器。M2.4 使用实际证据及产物绑定缓存。

## 独立审查复现与修复

第一次实现会因宽达 10000 ms 的测量容差误认可两个播放器记录集。其一的三个样本全在影片开头 80 ms，其二的播放器时刻从 2500 ms 退到 1500 ms。追加 Step A 保留原测试的每一行，在真实 5000 ms 的 F07 上分别复现这两个问题。执行 `uv run --no-sync pytest tests/integration/test_pts_sampling.py -q -k large_measurement --disable-socket --allow-unix-socket --tb=line -o cache_dir=data/.m22-pytest` 得到 2 项行为失败、8 项未选中。

独立审核后冻结的补测提交是 `3349ec3`，测试文件 SHA-256 为 `fc7173ea4ae9d9b4554263c625db61404eabb69f938d6dbb383c8b254b470057`，相对 `358c326` 的追加差异 SHA-256 为 `dcaf18ad1f75341cb092ba38b4a83f6f12519feef6e157124051270e892f140c`。修复后，每条记录还须属于与其标注位置最近的规范时间锚点，并按规范源时间对应严格递增的播放器时刻。复跑选帧、旧切片和架构命令得到 24 项通过，独立审查已通过。本轮生产修复对该冻结提交的测试差异为空。

另一次审查复现了首、中、尾覆盖正常且播放器时刻单调，但三个实测偏移分别为 0、1000、2000 ms 的反例。宽容差会错误放行这一明显漂移。追加运行 `uv run --no-sync pytest tests/integration/test_pts_sampling.py -q -k independent_precision --disable-socket --allow-unix-socket --tb=line -o cache_dir=data/.m22-pytest` 得 1 项失败，8、11、13 ms 的正常测量抖动对照通过。独立审查冻结文件 SHA-256 为 `663e15c09c14ff33a0a749bec1ef715308542a93e3b02b4e9facec19c286914d`，相对 `3349ec3` 的测试差异 SHA-256 为 `6c0e120a7d052a57f0a2c7c78521d796976850cb9eacee271d3e01ac85a82db4`。应用将单条测量的偏移拟合误差限为测量自身容差与 M1 默认 50 ms 中较小者。所有测量必须共同支持同一个偏移。复跑选帧、旧切片和架构共 26 项通过。
