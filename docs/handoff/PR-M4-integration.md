# M4 本地问答入口与持久化

M3 已发布的索引可以通过 `video-agent ask` 提问。命令要求给定资产和分析，并明确选择运行模式。默认输出答案，`--json` 还返回首次检索候选、问题 ID、问答作业 ID、计数器及用量账本。M5 可以复用异步入口 `ask_registered_asset`。[M3 索引](PR-M3.5.md) · [作用域索引迁移](../decisions/ADR-003.md)

```powershell
uv run --no-sync video-agent ask --asset-id <asset_id> --analysis-id <analysis_id> --question "孔径改为多少" --offline --json
uv run --no-sync video-agent ask --asset-id <asset_id> --analysis-id <analysis_id> --question "白色方块出现" --mode event_localization --offline --json
```

`--offline` 使用本地 Fake。使用 `--live` 时必须显式提供已有的 `--budget-config` 授权文件，文件须包含两项已启用的授权标志 `live_enabled`、`cloud_egress_authorized` 及数值预算，还需要配置对应模型的凭证。问答复核受管源文件的授权回执与 SHA-256。问答与索引共用单 Worker 锁。已有 v1 库先按顺序执行已批准的作用域索引迁移。

## 数据与预算

`questions.question_id` 是本次问答的预算作用域，`qa_job_id` 对应 `jobs.job_id`。首次检索通过同一个工具执行器扣一次工具额度，后续可复核的 `first_hits` 来自这次检索。未就绪或索引版本不符会返回 `INDEX_NOT_READY`，已就绪但没有相关文档的查询可以得到明确带缺口的答复。模型可见的引用由应用服务按相同的资产和分析查回，复核区域需匹配已记录的父帧与窗口。事件边界的外侧邻帧来自已发布索引中的视觉观察。

QA 采用独立持久账本，取配置、P0 上限和显式批准额度中的较严值。普通问答与视觉回看只在真正的 Provider 发送边界增加模型尝试与图片计数。提交答案时在同一写事务内检查取消并完成作业。专项复现了发送后取消时作业进入 `cancelled`、答案保持未发布且本次调用回执仍然入账。下游可用 `answers.question_id` 与 `jobs.question_id` 查回结果。[本地入口](../../src/video_agent/bootstrap.py) · [持久化](../../src/video_agent/adapters/storage/qa.py) · [用例](../../src/video_agent/application/qa.py)

## 两轮测试与复核

签名基线为 `a57c1fb`。表中 SHA-256 对应 `git diff --binary <提交>^ <提交> -- tests/integration/test_qa_entrypoint.py` 的输出字节。

| 提交 | 测试差异 SHA-256 | 实际结果 |
| --- | --- | --- |
| `4e216da` | `f9f37c92b03a3ca6e2d883407e3900288a734247c17b3a02d46e628e727951df` | 6 项红灯。5 次 CLI 因缺少 ask 退出 2，另 1 项命中 SQLiteQA.resolve 签名占位，pytest 退出 1。真实 F05、M3 索引、FTS 和外键自检通过。 |
| `92a884e` | `095006f497004a21f7b5e3bc01b8d58b396d92f550eacb5b707b223467f95fa4` | 首轮实现达到 5 通过、1 失败。已完成回看后的测试 SQL 读取了不存在的 images_sent 列。审核后将投影修正为 usage_json 中的同名字段，保留原计数断言。6 项复跑通过，生产提交为 `aa9447b`。 |
| `795d53f` | `64a5c2cccc5655568ecf64ffe8951854e081fff1895025f164073ed2fc6327f9` | 增补事件成功验收。临时源树保留 M4.5 前置代码并载入 `a57c1fb` 的 bootstrap、CLI 与 SQLiteQA，真实磁盘夹具通过后缺少 ask，pytest 退出 1。当前正式入口连同前六项复跑为 7 passed，11.28 秒，退出 0。 |
| `412b0be` | `62dfd4606cb0b9c769b669914581edafca70d6e9b4409d62fd0b1a7f82b00c59` | 2 项红灯，删除被引帧与等长改写后均错误发布 answered，pytest 退出 1。修复 `3634b07` 按绑定资产、产物类型、文件和目录登记的 SHA-256 逐条解析被引帧。两种故障均持久化为 not_answerable 和 INVALID_CITATION。修复被引帧后，无关文件损坏时仍成功回答。9 项完整专项通过，17.06 秒，退出 0。 |

实际运行的 Windows 专项命令如下。隔离回放的最后一条使用临时源树作为 PYTHONPATH，并先核对 CLI 模块来源。

```powershell
$env:UV_CACHE_DIR = 'data/.uv-cache'
uv run --no-sync pytest -q -p no:cacheprovider --disable-socket --allow-unix-socket tests/integration/test_qa_entrypoint.py --tb=line
uv run --no-sync pytest -q -p no:cacheprovider --disable-socket --allow-unix-socket tests/integration/test_qa_entrypoint.py -k unplayable --tb=short
uv run --no-sync pytest -q -p no:cacheprovider --disable-socket --allow-unix-socket tests/integration/test_qa_entrypoint.py --tb=short
uv run --no-sync ruff check src/video_agent/adapters/storage/qa.py src/video_agent/application/qa.py src/video_agent/bootstrap.py src/video_agent/cli.py tests/integration/test_qa_entrypoint.py
uv run --no-sync ruff format --check src/video_agent/adapters/storage/qa.py src/video_agent/application/qa.py src/video_agent/bootstrap.py src/video_agent/cli.py tests/integration/test_qa_entrypoint.py
uv run --no-sync mypy src/video_agent/adapters/storage/qa.py src/video_agent/application/qa.py src/video_agent/bootstrap.py src/video_agent/cli.py tests/integration/test_qa_entrypoint.py
.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider --disable-socket --allow-unix-socket --tb=line tests/integration/test_qa_entrypoint.py::test_published_event_uses_scoped_sql_neighbors_and_four_actual_boundary_frames
```

Ruff、格式和 mypy 的最终专项退出码均为 0。9 项用例中经正式 ask 创建的问答账本合计 15 次工具、3 次昂贵回看、13 次 Fake 模型尝试、28 个发往 Fake 的图片块，确认费用为 0。事件成功样例的 3 次工具包括检索和两侧回看，两次视觉尝试共发送 26 个图片块。开始区间为 [4285, 4857] ms，中点 4571 ms。结束区间为 [14285, 14857] ms，中点 14571 ms。四个边界引用均在 SQLite 中回溯到实际帧，并与独立解码的合成像素状态一致。[正式入口测试](../../tests/integration/test_qa_entrypoint.py)

本专项未运行云端模型、本地 ASR 或 OS 级断网验证，未读取留出标注。真实视频问答、语义支持和 20 项事件定位成绩由 M6 在冻结数据上验收。M5 可接入问答任务与页面。
