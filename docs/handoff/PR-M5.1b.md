# M5.1b 异步命令与受控证据

上传以外的七个 HTTP 操作负责提交分析、查询及取消作业、查询分页时间轴、提交及查询问题、取出证据。分析和问答返回 202。复用 M1 的 SQLite 事务和幂等约束，重开目录后原 `analysis_id`、`question_id` 和 `job_id` 仍可查询。`qa_job_id` 对应同一 `jobs.job_id`。问题待处理时只带状态，已完成时返回与其绑定的答案及原账本。[八个入口](PR-M5.md)

证据查询必须给出 `analysis_id`。元数据、JPEG 和源视频均按所属分析与资产解析。原视频按登记的 `source_artifact_id` 和内容哈希读取。真实样本的 `bytes=17-43` 响应为 206 且内容为源文件的第 17 至 43 字节，整文件与越界范围分别得到 200、416。另一分析得到 404，错误令牌得到 401。

红灯提交 `0dbe2cf` 的测试补丁 SHA-256 为 `59094e7fdac7ea5247b7e1ff71ea30b71b4955560a6b53182677e0a151c6e193`，尚无对应路由时返回 404。经独立审查校正自检 F05 的前置事实，M3 对无音轨媒体发布 `status=partial` 和 `index_state=ready`。`7a13667` 固定断言与 SQLite 实际状态一致，其补丁为 `87d3dae76e6102a9b1610f2822fde7dbc44b4c2ae454a2b7573a17a34a2daca4`。`1a6f0cf` 实现接口，以下 3 项通过。

~~~powershell
uv run --no-sync pytest -q tests/integration/test_api_contract.py tests/integration/test_evidence_delivery.py --disable-socket --allow-unix-socket
~~~

后续 Worker 消费这批持久作业，页面通过同一接口查看证据。故障恢复对已引用证据丢失有补充覆盖。[恢复交接](PR-M5.2b.md)
