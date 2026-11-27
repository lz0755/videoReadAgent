# M4.3b Planner 状态与终止

Controller 接收已绑定的工具执行器、PromptRegistry、计费决策端口及答案验证函数。Planner 接收文字观察、模态、证据 ID、时间、覆盖缺口和可用额度。最终综合占用已有轮数与模型尝试额度。取消、超时、未确认请求或额度耗尽时，保留已通过验证的中间 Claim 并返回 partial；没有此类 Claim 时返回 not_answerable 与原因。[控制器](../../src/video_agent/agent/controller.py) · [逐工具计费](PR-M4.3a.md)

| 冻结点 | 实测红灯 | 测试 diff SHA-256 | 绿灯 |
| --- | --- | --- | --- |
| f629301，签名 2654d7b | 11 项在 Controller.run 占位失败，覆盖最后一轮、最后一次模型尝试、批量工具中途取消和 unknown | `8388d9c7c0fd6adbdb2d235fa93d101799b5da58dbdff78d32a2566892de44d0` | 6e949f0 |
| 1558bd0 | 第九次请求提出视觉回看时仅出现一次普通决策，原实现丢掉了尚可使用的最终综合 | `9f49ffe7988e903940f687b9dc917aab12bdbb64d37b75be3da81caba6965a1a` | a4d12ce |

```powershell
uv run --no-sync pytest -q --disable-socket --allow-unix-socket tests/unit/agent/test_controller.py tests/unit/agent/test_termination.py --tb=line
uv run --no-sync pytest -q --disable-socket --allow-unix-socket tests/unit/agent/test_final_reservation.py tests/contract/test_answer_evidence.py --tb=line
uv run --no-sync pytest -q --disable-socket --allow-unix-socket tests/unit/agent --tb=line
```

红灯退出 1。最终 14 项 Planner 和工具用例通过，退出 0。末槽回归实测视觉发送 0 次，最后一次文字综合完成，账本为 10/10。真实云端兼容性与语义质量另行评测。[发送适配器](PR-M4-planner-wire.md) · [引用验证](PR-M4.4.md)
