# PR-M2.3b 受监督的本地 faster-whisper

本项对应 M2-A2。真实本地模型已通过一项固定长静音样本的时间验证。10 项默认合同覆盖进程复用、句柄隔离、取消、超时、缺失或损坏输入。独立审查代理分别复跑了默认合同和显式模型任务。上游 M1.5 正式质量门仍为可答题 4/9，门槛为 7/9。[音轨与裁剪合同](PR-M2.3a.md) · [上游诊断](../decisions/ADR-002.md)

### 入口和边界

`FasterWhisperTranscriber` 实现现有异步 `Transcriber` 端口。首次请求用 `multiprocessing.get_context("spawn")` 启动计算进程。子进程只获得本地模型配置、读取 WAV 所需的信息和 IPC 端点，按执行单元复用模型。父进程掌握产物存储、绑定视频时长、取消及截止时间，并构造正式的转写结果。对同一执行单元的并发推理返回 `RESOURCE_BUSY`。取消或超时会完全回收已启动的子进程。[实现](../../src/video_agent/adapters/asr/faster_whisper.py)

加载前检查本地模型文件和 `tokenizer.json`。子进程设置 Hub 离线模式，使用 `local_files_only=True`。模型接收未压缩时间轴的 16 kHz 波形和 `vad_filter=False`。分段时间先受 WAV 实际样本数限制，再按来源映射和受管视频时长裁剪。新旧 WAV 均可读取，包括旧切片由 FFmpeg 管道写出的开放长度 RIFF 头。全零 PCM 返回无转写。权限、坏 WAV、缺权重、内存不足、超时和取消保留独立结果。[合同测试](../../tests/contract/test_asr.py)

### 红灯、实测与局限

Step 0 为 `28760fa`，审核后的 Step A 为 `adce1cc`。这两份测试文件对应的二进制差异 SHA-256 为 `ab1f5b1b78f33f16858ca61ea1de4d42c4e55d193dfb72bf56456008fecb0dcf`。默认 10 项和显式本地 1 项起初均行为失败。模型任务在推理前先检查预置权重、数字静音、实际波形开始及结束时刻。默认模型替身经真实 spawn 和 IPC 执行。父进程同时持有磁盘 SQLite 事务、WorkerLock 和显式可继承的文件，子进程使用新解释器，无法观察到父连接或该文件描述符。连续两次请求命中同一 PID 和模型实例，实际进入计算后再取消或超时的子进程均已回收。

本地任务使用预置的 faster-whisper small，修订号为 `536b0662742c02347bc0e980a01041f333bce120`。输入由本机语音合成器生成，讲话始于约 40 s。本地模型首次运行把目标词识别错，未通过冻结断言。检查解码参数后，允许首个词位于 Whisper 整个 30 秒推理窗内，并启用词时间戳。目标语句实测为 **39.46–42.02 s**。模型在前段静音中还产生一个无关短词；这个样本只验证了目标语句的位置。[显式模型测试](../../tests/integration/test_asr_local.py)

```powershell
$env:UV_CACHE_DIR = Join-Path $PWD 'data/.uv-cache'
uv run --no-sync pytest tests/contract/test_asr.py -q --disable-socket --allow-unix-socket --tb=line -o cache_dir=data/.audio-pytest
uv run --no-sync pytest tests/integration/test_asr_local.py -q -m model_local --disable-socket --allow-unix-socket --tb=line -o cache_dir=data/.asr-local-pytest
```

两条命令分别退出 0，默认 10 项、显式 1 项通过。本次实现调试对该合成样本运行了两次真实验收推理和五次本地参数对照，独立审查另复跑一次。未下载权重，云端推理、外部 HTTP、外发图片与云端费用均为零。Windows 已实测；操作系统级断网容器、GPU 和父 Worker 异常退出后的恢复演练待对应环境和后续里程碑。下一依赖为 M2.4 的内容、模型与映射版本缓存。

### 同目录权重更新

M2.4 接线时还发现权重内容变化后需要释放空闲模型，同时保留服务实例。追加红灯以 `3bbbf06` 冻结，单文件测试二进制差异 SHA-256 为 `4aa31b1fe686ee3f1efd1039eb37d00129f3ee95dfcaf9ea64852f25e9d97186`。首次真实 spawn 成功后，新增的 `unload_model()` 签名占位使测试退出 1。实现后验证了旧进程确已退出，同目录下更新的权重在后续请求首次使用时启动新进程。卸载前以同一把请求锁确认空闲，工作中返回 `RESOURCE_BUSY`；服务关闭后调用转写仍返回 `JOB_CANCELLED`。[重载回归](../../tests/contract/test_asr_reload.py)

```powershell
uv run --no-sync pytest tests/contract/test_asr_reload.py tests/contract/test_asr.py -q --disable-socket --allow-unix-socket --tb=line -o cache_dir=data/.audio-pytest
```

此命令退出 0，11 项通过。原两个测试文件的冻结断言保留。另实测旧切片媒体入口 7 项通过。
