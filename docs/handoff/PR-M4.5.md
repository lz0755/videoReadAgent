# M4.5 事件定位与双边精修

M4-A3 的定位路径按每个问题独立计费。首次检索返回有真实帧引用的视觉观察，仓库在当前资产和分析中寻找其前后相邻的索引帧。若两侧均可由各自八秒以内的窗口覆盖，服务端分别调用 `inspect_clip`，默认使用全帧，每秒最多抽两帧。一次定位最多两次回看、32 个累计图像块，图片重发同样占用额度。[检索交接](PR-M4.1.md) · [工具执行器交接](PR-M4.3a.md)

事件视觉模板要求逐帧给出可见的活动状态、可见的非活动状态或无法判断，并引用本次发送的帧。闭区间两端来自状态发生变化的相邻实际 PTS，宽度上限为 4000 毫秒，估计值取整数中点。粗观察的原文作为预测事件描述。发生遮挡且无法确认状态、出现反向变化、两侧区间无法排序或余额不足时，答案保留可确认的一侧及其引用，说明缺口。已验证的回看观察、帧、裁剪父帧和覆盖缺口通过 M3 的融合和存储接口记录。[定位实现](../../src/video_agent/application/event_localization.py) · [回看实现](../../src/video_agent/application/frame_review.py) · [观察存储交接](PR-M3.3.md)

事件模式单独读取经哈希核验的模板注册表，视觉观察沿用既有 `VisionResponse`。模板 SHA-256 为 `73c0cd19564be6c2825e68034f7801a5ea73c5d7dfc98edfacd7d3bf11a1e61b`。模型、数据库 Schema、依赖及 D1 冻结文件均沿用上游版本。[事件注册表](../../prompts/vision/event_registry.json) · [事件视觉模板](../../prompts/vision/event_state.jinja2)

### 合成视频实测

FFmpeg 生成 23 秒、7 FPS 的合成视频。独立 PyAV 解码了全部 161 个视频帧并检查中心像素，从实际 PTS 得到首次出现于 4428 毫秒、首次消失于 14857 毫秒。已授权的测试素材通过正常导入、真实磁盘 SQLite、M3 索引和 FTS 查询。视觉端口 Fake 从每一个实际发送的图像产物读取像素。[测试夹具及验收断言](../../tests/integration/test_event_localization.py)

| 边界 | 实际证据闭区间（毫秒） | 区间中点（毫秒） |
| --- | --- | --- |
| 开始 | [4285, 4857] | 4571 |
| 结束 | [14285, 14857] | 14571 |

该请求使用一次检索、两次昂贵回看、两次视觉尝试，分别发送 13 个图像块。账本合计 26 图，Fake 费用为 confirmed 0 USD。测试另行覆盖不清晰、冲突、隔着遮挡的状态反转、过宽候选、第二次回看图像余额不足，以及两个请求时间落在同一实际帧的情况。自然语言问句的预测事件标签来自已核对的粗观察。正式 M6 仍须评估真实视觉语义、标签匹配和 20 道独立定位请求，本轮未运行留出集、provider_live 或 model_local。项目云端请求和 HTTP 传输均为 0。[D1 冻结交接](PR-D1.md)

### 测试冻结与复验

| 范围 | 测试提交和二进制差异 SHA-256 | 实际红灯与实现 |
| --- | --- | --- |
| 初次精修 | `7060a14` · `3afebfa5144dc18470d1fb60ea2042bd134029399fa51b0412feb9288169566f` | 已审签名 `f1a93e9`。夹具自检通过后 7 项在两个签名占位处抛出 NotImplementedError，退出 1。实现 `ddfacf6`，8 项通过。 |
| 审查回归 | `e642386` · `02e36a080e7a01d289ea19ce0d49f35821c4649ae0eacb5d3ef8274a651b070b` | 开始、结束两例跨不清晰帧的状态反转仍答为 answered，另一例把询问句写入标签。3 项失败，退出 1。修复 `a7d47e6`，11 项通过。 |

在仓库根目录执行以下命令。单跑夹具自检 1 项通过。第 2 条首次得到 7 failed、1 passed，修复后的最终结果为 11 passed，耗时 13.41 秒。第 3 条为回归的原始红灯。第 4 条在首次实现后通过 38 项。最后三条在修复后通过，退出码均为 0。

```powershell
uv run --no-sync pytest -q --disable-socket --allow-unix-socket tests/integration/test_event_localization.py::test_fixture_visible_edges_and_fractional_frame_times --tb=line
uv run --no-sync pytest -q --disable-socket --allow-unix-socket tests/integration/test_event_localization.py --tb=line
uv run --no-sync pytest -q --disable-socket --allow-unix-socket tests/integration/test_event_localization.py -k 'known_states or as_label' --tb=short
uv run --no-sync pytest -q --disable-socket --allow-unix-socket tests/integration/test_event_localization.py tests/security/test_agent_tools.py tests/contract/test_vision_wire.py tests/integration/test_pts_sampling.py --tb=line
uv run --no-sync ruff check src/video_agent/application/event_localization.py src/video_agent/application/frame_review.py tests/integration/test_event_localization.py
uv run --no-sync ruff format --check src/video_agent/application/event_localization.py src/video_agent/application/frame_review.py tests/integration/test_event_localization.py
uv run --no-sync mypy src/video_agent tests/integration/test_event_localization.py
```

开发时仅传测试文件执行 `uv run --no-sync mypy tests/integration/test_event_localization.py`，mypy 将可编辑安装的项目视为未声明类型，退出 1。上列包含 `src/video_agent` 的命令检查了 69 个源文件。Windows 上仅启用了 pytest-socket 防漏检查，未独立执行操作系统级断网。M5 可从本地 QA 入口读取已持久化答案及边界证据，M6 使用冻结的评测口径判分。
