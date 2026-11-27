# M4.3a 工具执行与嵌套计费

每条提议先校验，再从当前 question_id 的 BudgetGuard 扣一次工具额度。inspect 与视觉 compare 共用昂贵回看额度；执行器生成的服务端 parent_tool_call_id 随视觉请求及重试写入账本。网络尝试、实际发送图片与费用由既有 ProviderExecutor 和发送边界计数。[工具入口](../../src/video_agent/agent/executor.py) · [持久预算](PR-M3.0.md)

签名 2faa939，测试冻结 0c07be4，测试 diff SHA-256 为 `c46fd98444cafaca1637ec7669cad83f0c16ed876a3bbecb67a9e8710f176bce`。首次两个用例均在工具签名占位失败，退出 1。efa8186 后两项通过，独立复跑退出 0。

```powershell
uv run --no-sync pytest -q --disable-socket --allow-unix-socket tests/unit/agent/test_tool_executor.py --tb=line
```

剩余两次工具额度时，三个提议只触达两次端口。一次 inspect 首次发送 8 图遇限流，再发 8 图成功，账本记录 1 次工具、1 次昂贵工具、2 次 Fake 尝试、16 个图片块。随后一次 compare 用掉第二个昂贵名额，下一次 inspect 在回看前被拒。真实供应商调用为零。[Planner 终止](PR-M4.3b.md)
