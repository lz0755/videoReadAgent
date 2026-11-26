# PR-M2.3a 音轨抽取与规范时间映射

本项对应 M2-A2。Windows 上的定向真实媒体测试共 11 项通过。独立审查代理复验了原始九项和 Windows 进程回收的两项追加回归。M1.5 的可答题评分仍为 4/9，门槛为 7/9；用户批准继续 M2 离线工程。[前置媒体切片](PR-M1.5.1.md) · [八类夹具](PR-M2.0.md)

### 输入、输出与时间合同

`FFmpegAudioExtractor.extract_audio(asset, range_ms=...)` 从已绑定的受管源取主视频和音轨首个解码 PTS，以有理数计算相对偏移。FFmpeg 输出 16 kHz、单声道、16 位 PCM WAV。父进程核对 WAV 的实际末样本和请求上限后才登记音频产物。无音轨返回 `NO_AUDIO_STREAM`，坏音轨返回 `CORRUPT_MEDIA`。缺 FFmpeg、权限拒绝、窗口无覆盖、超时和取消分别有可区分的代码或权限原因。[抽取实现](../../src/video_agent/adapters/media/ffmpeg_audio.py)

完整的晚起音轨不补前导静音。未补静音时 `extracted_start_ms` 表示从音轨起点裁掉的时间，映射时加一次真实音轨偏移。如果显式窗口需要前导静音，`extracted_start_ms` 就是规范窗口起点。映射会与有效视频范围求交，完全落在范围外时返回空覆盖。F04 的媒体首音频 PTS 在 178 ms，波形起振在约 200 ms。另一个可独立复验的样本有 200 ms 音轨延迟，80–90 s 窗口再包含本地 2–3 s 的音调，返回的规范区间为 82–83 s。[映射实现](../../src/video_agent/application/audio_timeline.py) · [波形与错误分支测试](../../tests/integration/test_audio_timeline.py)

### 红灯、修复与复验

Step 0 提交为 `667d2bb`，独立审核后的 Step A 提交为 `358c326`。该提交中本项测试二进制差异的 SHA-256 为 `b3fe46670c10fa5ecd27b606ce18107691c788533337f57a15c39f458771f530`。真实 90 秒标记音轨、F04、损坏的 AAC 音频包和无音轨对照先经独立解码自检。原九项退出 1，均进入已审核签名的行为占位。实现后同一文件九项通过。

独立审查发现 Windows 上 PATH 中的 FFmpeg 是 Chocolatey ShimGen 启动代理。只终止代理时，两个测试分别观察到实际 PCM 文件在取消和超时后 350 ms 内继续增长。追加 Step A 提交为 `3349ec3`，本项测试二进制差异的 SHA-256 为 `64a4470a71ee9f5363060ebcc98fcec69f2ebb795757af166d4ff04b335aaf06`。最初尝试调用 taskkill 时系统拒绝终止，返回了 `RESOURCE_BUSY`。最终实现先将等待启动信号的进程绑定进 Windows Job Object，再启动 FFmpeg。退出时终止整棵树并确认活动进程数为零，最后释放句柄。两种中断的输出均停止增长且可以立即删除。POSIX 使用独立进程组，此分支仅经代码审查。[进程回收回归](../../tests/integration/test_audio_supervision.py)

```powershell
$env:UV_CACHE_DIR = Join-Path $PWD 'data/.uv-cache'
uv run --no-sync pytest tests/integration/test_audio_timeline.py -q --disable-socket --allow-unix-socket --tb=line -o cache_dir=data/.audio-pytest
uv run --no-sync pytest tests/integration/test_audio_supervision.py -q --disable-socket --allow-unix-socket --tb=line -o cache_dir=data/.audio-pytest
uv run --no-sync pytest tests/integration/test_audio_supervision.py tests/integration/test_audio_timeline.py -q --disable-socket --allow-unix-socket --tb=line -o cache_dir=data/.audio-pytest
```

第三条命令退出 0，11 项通过。回归使用真实 FFmpeg、ffprobe、PyAV、磁盘文件和波形。操作系统级断网容器和 POSIX 运行环境没有在本机执行。云端模型调用、外部 HTTP、外发图片和费用均为零。下一依赖为 M2.3b 的受监督转写和 M2.4 的媒体缓存。
