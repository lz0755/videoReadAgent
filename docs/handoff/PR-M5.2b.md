# M5.2b 作业恢复、取消与账本

只有持有 OS 锁的 Worker 会处理遗留作业。取消意图优先。未取消的作业核查已提交的阶段、引用文件和外部尝试；仍可安全继续的作业保留原 ID、截止时间和历史用量。已成功返回但缺少可核验阶段提交的发送同样需要显式确认，重采样改变 operation key 也不会自动再次发送。已提交问答的引用在读取和启动恢复时重新核验。

| 独立冻结点 | 失败与验证 |
| --- | --- |
| `7a0a83d` · SHA-256 `f954149c4f578dbb4112077e8e92d294bdaba846e321265229436abcaac18761` | 10 项红灯覆盖阶段提交前后硬退出、取消、原 deadline、未知发送、锁与遗留计算子进程等。修复 `7af433a`。 |
| `edde1ea` · `8057a35089fa1a6ec212dd3aafa388d9f6500831233e71652269b361031bb56b` | 已付费响应与未发布窗口错配、丢失引用仍返回成功、旧库直接计算、取消与超时竞争。`bb883e4` 修复。审查者独立复跑其中 4 项和 4 项进程故障，共 8 项通过。 |

~~~powershell
uv run --no-sync pytest -q tests/integration/test_job_recovery.py tests/integration/test_worker_boundaries.py tests/integration/test_uncommitted_attempt.py tests/integration/test_worker_lock.py tests/integration/test_job_queue.py --disable-socket --allow-unix-socket
~~~

故障夹具使用真实 FFmpeg/PyAV、磁盘 SQLite 和独立进程。父进程在阶段事务的两侧硬退出。在受信计算端口启动的真实存活子进程能证明父锁已经释放、替代 Worker 返回 `RESOURCE_BUSY` 且没有发布最终阶段。真实 GPU/模型进程的资源回收未在此次平台演练中测量。

Windows 全量门禁还在既有 `test_cache_records_are_atomic_and_bound_to_key` 遇到 `WinError 5`。用真实未关闭的读句柄确定性复现后，独立冻结 `ff90904`，补丁 SHA-256 为 `c090371b9472f06011395ca3c849db2957c79e40667dfdd9cb88dbe0de834007`。`00c5117` 对 Windows 的瞬时共享冲突最多等待 0.5 秒，再次尝试原子替换。旧读者取得完整旧记录，重新打开时可取得完整新记录，临时文件得到清理。以下 7 项通过。

~~~powershell
uv run --no-sync pytest -q tests/integration/test_cache_publish.py tests/integration/test_media_cache.py --disable-socket --allow-unix-socket
~~~

`jobs retry` 沿用原作业。遇到不确定出站尝试须再传 `--confirm-external-risk`，明确接受重复计费风险；旧账本状态保持可追溯。缺失已引用帧时保留旧答案行作为历史，查询报告失败。启动、退出与人工解除计算标记的条件见手册。[恢复手册](../runbooks/recovery.md)

普通重试碰到未知结果时，CLI 在标准错误中解释旧请求可能计费、再次发起可能重复计费，并给出确认选项。冻结点 `32b4473` 的补丁 SHA-256 为 `3d55a94a5a852e32884d71bf84c46102cdccbba0081a70f3a008a2d655b39a89`。旧实现在此场景只输出错误码，修复 `e9a439b` 后专项通过。测试还确认原作业状态、尝试 ID 和费用状态保持一致。

~~~powershell
uv run --no-sync pytest -q tests/integration/test_retry_notice.py --disable-socket --allow-unix-socket
~~~
