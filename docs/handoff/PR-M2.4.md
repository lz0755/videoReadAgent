# PR-M2.4 媒体产物与 ASR 缓存

M2-A3 的缓存通过文件重开、配置变化、跨资产复用与磁盘损坏验收。独立审查者复跑 8 项通过，确认配置与文件哈希、归属校验和原子写入。组合根通过既有 `MediaBackend` 和 `Transcriber` 端口注入缓存。测试所用视频均为本地合成夹具。[八类夹具](PR-M2.0.md) · [应用层](../../src/video_agent/application/media_cache.py) · [文件存储](../../src/video_agent/adapters/storage/media_cache.py)

| 产物 | 复用与失效条件 |
| --- | --- |
| 帧 | 检查受管源的归属与 SHA-256。键包含源内容、片长、有序请求时间、半开区间和处理配置。原始内容确定所选流的 PTS、原点和显示矩阵，处理配置记录选择与编码规则。重开缓存后，原始 PTS、请求时间与实际时间均可查。同一图像的不同时间点保留各自的证据 ID。 |
| 音频 | 键包含源内容、片长和提取配置。复用时检查 WAV 全文件 SHA-256 并恢复对应的 `AudioTimelineMap`。 |
| 转写 | 键包含受管 WAV 内容、模型与运行配置、语言、VAD 和完整时间映射。本地权重身份函数流式计算目录内文件路径与内容的摘要，同名权重或配置替换后摘要变化。 |

同内容被导入另一资产时，复用已经校验的媒体字节。新资产获得自己的受管文件和证据 ID。后续命中会校验该资产已有的副本。调用方使用其它资产的原视频或 WAV 标识时返回 `CROSS_ASSET_REFERENCE`。受管源内容与注册哈希不一致时返回 `CORRUPT_MEDIA`。产物缺失或摘要有误会触发重算。缓存元数据先写临时文件，再原子替换，摘要覆盖键与数据。超过 16 MiB 的单条元数据直接走计算结果，避免加载过大的缓存记录。

## 冻结与复验

Step 0 接口提交为 `667d2bb`。主测试在 `358c326` 经独立审查冻结。6 条红灯均触发已声明的占位接口，初次绿灯为 6 项通过。审核随后复现了“两个合法资产、相同内容、调用方传错输入产物 ID”可以命中缓存的风险。归属回归在 `adce1cc` 独立冻结，修复前 2 项均因未抛出异常失败。复跑共 8 项通过，两个测试文件相对于各自冻结提交均无差异。[主测试](../../tests/integration/test_media_cache.py) · [输入归属回归](../../tests/integration/test_media_cache_ownership.py)

| 测试 | 工作树文件 SHA-256 |
| --- | --- |
| 主测试 | `92b3b9f0982727319ceb6a71c15b7f55ed235aaf4506291496b287bc16ee1047` |
| 输入归属回归 | `429582cbd5f507ccafaf7d947fa7547181fc1ee0338028632aff1b74d4114b83` |

主测试新增 Git binary diff 的 SHA-256 为 `8dfd0ddada003b84642b7cc00eff3eb6ce07b4c1e749b5fd0c4c57e63ccf0e98`。在项目根目录运行下列命令。前两条 pytest 分别用于复现已冻结的红灯，最后一条 pytest 在修复后验证 8 项。

```powershell
$env:UV_CACHE_DIR=Join-Path $PWD 'data/.uv-cache'
uv run --no-sync pytest tests/integration/test_media_cache.py -q --tb=line --disable-socket --allow-unix-socket -o cache_dir=data/.m2-cache-review
uv run --no-sync pytest tests/integration/test_media_cache_ownership.py -q --tb=line --disable-socket --allow-unix-socket -o cache_dir=data/.m2-cache-review
uv run --no-sync pytest tests/integration/test_media_cache.py tests/integration/test_media_cache_ownership.py -q --tb=line --disable-socket --allow-unix-socket -o cache_dir=data/.m2-cache-review
uv run --no-sync ruff check src/video_agent/application/media_cache.py src/video_agent/adapters/storage/media_cache.py tests/integration/test_media_cache.py tests/integration/test_media_cache_ownership.py
uv run --no-sync ruff format --check src/video_agent/application/media_cache.py src/video_agent/adapters/storage/media_cache.py tests/integration/test_media_cache.py tests/integration/test_media_cache_ownership.py
uv run --no-sync mypy src/video_agent/application/media_cache.py src/video_agent/adapters/storage/media_cache.py tests/integration/test_media_cache.py tests/integration/test_media_cache_ownership.py --cache-dir data/.m2-cache-mypy-target
uv run --no-sync lint-imports --no-cache
```

Ruff、mypy 和两项 import-linter 合同均通过。专项通过真实文件、FFmpeg、PyAV 和端口 ASR Fake 观测缓存行为。外部 HTTP、真实模型、外发图像和云端费用均为零。真实模型运行见 M2.3b。M1.5 可答题的既有人工评分仍为 4/9。M3 可复用已校验的媒体产物与映射，在对应的 analysis_id 下登记证据及阶段状态。
