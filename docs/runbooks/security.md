# 本地发布验证与数据管理

八个业务接口、页面与 Worker 在同一台机器上使用。API 绑定回环地址并校验本机令牌；访问证据时提交绑定的 analysis_id。发布包包含源码、依赖锁、公开清单、合成媒体生成器与手册。真实素材、帧、私有标注、凭据及模型权重由使用者管理。

## 两层断网检查

Windows 从源码发布包的独立解压目录运行下列命令。Docker 构建上下文只含包内公开文件。pytest-socket 检查 Python 层，真实本机服务测试显式允许 loopback。完整操作系统隔离使用下一个容器命令，先在可安装依赖的准备阶段构建镜像，运行时关闭网络。

```powershell
uv run --no-sync pytest -q -m "not provider_live and not model_local" --disable-socket --allow-unix-socket
uv run --no-sync lint-imports
docker build -f Dockerfile.ci -t video-agent-ci:m6 .
docker run --rm --pull=never --network none --cap-drop ALL --security-opt no-new-privileges video-agent-ci:m6 sh -ec '.venv/bin/python tests/canary/network_denied.py && .venv/bin/python -m video_agent.cli doctor --json && .venv/bin/python -m pytest -q --disable-socket --allow-unix-socket -m "not provider_live and not model_local"'
```

canary 要求独立 Python 解释器和原生 curl 访问同一公网 IP 时均收到操作系统拒绝，单纯 DNS 错误或超时均会使检查失败。本次对应源码包和本地镜像的运行记录在 M6.2 交接。

## 稳定回归索引

以下文件在 `tests/` 下。运行上方全量命令覆盖其离线测试。T05 的真实 CPU 模型专项另行执行。

| 编号 | 验证情形 | 测试位置 |
| --- | --- | --- |
| T01 | VFR 实际 PTS | `integration/test_pts_sampling.py` |
| T02 | 音轨晚 200 ms | `integration/test_media_probe.py`、`integration/test_slice_media.py` |
| T03 | 长 GOP seek 解码到目标 | `integration/test_pts_sampling.py` |
| T04 | 缺失或重复 PTS | `integration/test_pts_sampling.py` |
| T05 | 40 秒静音、原轴和关闭 VAD | `contract/test_asr.py`、`integration/test_asr_local.py` |
| T06 | 分片偏移只加一次 | `integration/test_audio_timeline.py` |
| T07 | 无音轨、坏音轨、FFmpeg 不可用 | `integration/test_audio_timeline.py`、`integration/test_slice_media.py` |
| T08 | 单轮三工具只剩两次 | `unit/test_budget_guard.py`、`unit/agent/test_tool_executor.py` |
| T09 | 两个真实 SQLite 连接竞争余额 | `integration/test_budget_ledger.py` |
| T10 | 外部结果未知，重启保留费用 | `integration/test_budget_ledger.py`、`integration/test_uncommitted_attempt.py` |
| T11 | 401/403、429、503，SDK 及应用重试 | `contract/test_m6_replay.py`、`contract/test_provider_capabilities.py`、`unit/test_budget_guard.py` |
| T12 | 阶段提交前后退出 | `integration/test_job_recovery.py`、`integration/test_cache_publish.py` |
| T13 | 第二进程争用 OS 锁 | `integration/test_worker_lock.py` |
| T14 | 模型、Prompt 内容、Schema、采样变更 | `integration/test_stage_cache.py`、`unit/test_prompt_registry.py` |
| T15 | 编造、跨资产、跨分析的引用 | `contract/test_answer_evidence.py`、`contract/test_qa_final_citations.py` |
| T16 | 不可信内容、额外参数及未知工具 | `security/test_agent_tools.py`、`contract/test_qa_planner_wire.py` |
| T17 | 中文两字、单字回退和分析隔离 | `integration/test_chinese_search.py` |
| T18 | 相同图像、不同原始时刻 | `integration/test_media_cache.py` |
| T19 | 20/200 模糊读数，保留不确定性 | `contract/test_m6_replay.py`、`unit/test_eval_release.py`、`integration/test_observation_pipeline.py` |
| T20 | 缺少覆盖时的 gap 与事实判分 | `contract/test_m6_replay.py`、`integration/test_timeline_summary.py`、`unit/test_eval_release.py` |
| T21 | 下一帧等于半开区间末端 | `integration/test_pts_sampling.py` |
| T22 | 非零原点、播放映射与帧预览 | `integration/test_pts_sampling.py`、`unit/ui/test_playback.py` |
| T23 | 重发、ROI、视觉重试的每次图片计数 | `integration/test_budget_ledger.py`、`unit/agent/test_tool_executor.py` |
| T24 | 最后模型尝试与截止时间 | `unit/agent/test_final_reservation.py`、`unit/agent/test_termination.py` |

夹具独立自检、真实磁盘迁移/外键、上传限额、取消、子进程存活、事件双边包围及分层检查分别见 `integration/test_fixture_self_check.py`、`integration/test_migrations.py`、`integration/test_sqlite_contract.py`、`integration/test_upload_limits.py`、`integration/test_job_recovery.py`、`integration/test_event_localization.py`、`architecture/test_import_boundaries.py`。回放检验端口接线和答案结构，语义判断须在正式运行中核对原始证据。

## 数据保留和删除

为每次验收选用独立目录。仅授予操作者和验收者访问权。将素材处理授权、源哈希、预算和费用账本按双方约定期限保留；原片、裁剪、缓存、转写、问题、逐题输出和 SQLite 副本都属于私有内容。备份时复制已停止写入的完整受管目录，记录校验和。

删除前停止使用该目录的全部 API、页面、Worker、CLI 和计算子进程。核对目标的绝对路径、授权范围和备份要求，确认后在文件管理器删除指定运行目录及相应备份。单独删除索引或缓存可能留下可检索文本、其他帧或跨表引用。评测私包与独立保存的原片由验收者另行处理。出站后生成的供应商日志和账单依当次授权与对方保留规则处理。
