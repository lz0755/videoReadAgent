# M4 Planner 传输与回执

M4-A2 的文本 Planner 使用当前 QA 请求的注册提示词，将模型提议转换为 `ToolCallProposal` 或 `Answer`。DeepSeek JSON 对象和 OpenAI Structured Outputs 两条路径均在 SDK 边界通过离线合同测试。已授权的在线运行由 QA 组合入口将适配器交给 `ProviderExecutor`。该执行器负责发送、重试、截止时间和逐次记账。[预算执行器](PR-M3.0.md) · [Planner](../../src/video_agent/adapters/llm/qa_planner.py)

- 配置限定为 `deepseek-flash`、`gpt-6-luna` 各自的固定端点及凭据环境变量，创建 SDK 客户端前检查云端同意和凭据。超时取请求指定值，SDK 重试次数为 0。
- 模型可提交搜索、受限回看和两帧比较。工具参数先按各自严格类型解析，执行器随后检查当前资产、时间范围、ROI 权限和预算。服务端生成提议 ID，最终回答的资产、分析、问题和回执均绑定当前请求。引用接受 M4.4 的持久证据校验。
- OpenAI 的严格传输结构将 arguments 表示为 JSON 字符串，DeepSeek 也可提交参数对象。两条路径产出相同的参数字典。OpenAI 响应结构的序列化字节数在当前价格配置预留的 2048 token 开销以内。
- 坏 JSON、越权工具、错误字段和回答结构错误返回 `SCHEMA_VALIDATION_FAILED`。已返回的供应商请求 ID、输入输出用量随错误进入原账本，缺少用量时费用状态为 `unknown`。一个修复场景实际进入 SDK 替身 2 次，账本含 1 条失败、1 条成功、0 张图片，整体费用状态为 `unknown`。

## 两轮验证

基线为 `6e949f0`，签名提交为 `b966476`。Step A 的提交为 `b8f6af9`，仅新增一个合同测试文件。测试差异 SHA-256 为 `53d181f916a24ba9c6b636cfa05c1419c605b9ebea3d3fa19007ec54358a4b4b`。签名占位产生 20 个 `NotImplementedError`，测试退出 1，经独立复跑和审核后冻结。Step B 的提交为 `ec6d826`。检查冻结文件相对 Step A 的 Git 差异为空，退出 0。[冻结测试](../../tests/contract/test_qa_planner_wire.py)

```powershell
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --disable-socket --allow-unix-socket tests/contract/test_qa_planner_wire.py --tb=line
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --disable-socket --allow-unix-socket tests/contract/test_qa_planner_wire.py tests/unit/test_openai_slice.py tests/contract/test_vision_parse_receipt.py --tb=line
.venv/Scripts/ruff.exe check src/video_agent/adapters/llm/qa_planner.py tests/contract/test_qa_planner_wire.py
.venv/Scripts/ruff.exe format --check src/video_agent/adapters/llm/qa_planner.py tests/contract/test_qa_planner_wire.py
.venv/Scripts/mypy.exe src/video_agent/adapters/llm/qa_planner.py tests/contract/test_qa_planner_wire.py --follow-imports=normal
git diff --exit-code b8f6af9 ec6d826 -- tests/contract/test_qa_planner_wire.py
```

专项联跑为 26 passed、退出 0。独立审查者复核生产差异和冻结，另联跑 20 个 wire 测试与 5 个旧 OpenAI 切片测试，25 passed。Ruff、格式和 mypy 最终均退出 0。开发中 mypy 首轮指出工具类型映射无法推断，修正类型标注后通过。仓库级质量门归档在 M4 总交接。

本次真实云端请求、真实计费和外发图片均为 0。没有运行 provider_live、model_local 或 M6 留出评测。模型语义效果和实际端点对该 M4 响应结构的兼容性留待有授权的实测。
