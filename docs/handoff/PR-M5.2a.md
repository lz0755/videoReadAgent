# M5.2a 单 Worker 与原作业消费

`video-agent worker --offline` 在整个生命周期独占本数据目录的 OS 锁。`--once` 恢复并最多处理一项后退出。`BEGIN IMMEDIATE` 内原子领取、写入 attempt、首次开始时间和截止时间，随后在短事务之外执行已有索引或问答用例。恢复和重试沿用已持久化时间。CLI 分析、问答及离线迁移共用计算锁，导入操作使用独立导入锁。

`analyses create` 和 `questions create` 与 HTTP 共用提交与幂等语义。Worker 把 M3、M4 的输出写回原作业和问题，没有第二份答案。离线 Fake 和显式 live 的工作模式进入配置指纹，配置或模式不一致时返回错误。

先审查 `0910390` 的消费签名，红灯用例 `b64c824` 校验无消费者和无相应 CLI 时的行为。补丁 SHA-256 为 `79555f164c02e11c23adce28f9f1848630e5f4f262649d39749f0184b0188e36`。实现 `9fbe26b` 后以下 4 项通过，其中两个真实进程验证第二 Worker 快速返回 `WORKER_ALREADY_RUNNING`，并核对锁文件的身份。

~~~powershell
uv run --no-sync pytest -q tests/integration/test_job_queue.py tests/integration/test_worker_lock.py --disable-socket --allow-unix-socket
~~~

持锁领取是 M5.2b 重启处理的前置条件。[恢复交接](PR-M5.2b.md)
