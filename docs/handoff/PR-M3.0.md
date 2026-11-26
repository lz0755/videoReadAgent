# M3.0 持久预算账本

M3-A2 专项通过。基线为 `9cf4f7a`，接口提交为 `dc0064c`。两个执行器使用独立 SQLite 连接争最后一笔额度时，只有一个调用到达端口。账本沿用迁移 001 和 M1.7 的计数策略，每次预留、标记出站和结算分别用短事务完成。任务和账本保存首次 UTC 截止时间，发送审计记录保存模型、定价版本、操作标识及父工具调用。[M1.7](PR-M1.7.md) · [M2](PR-M2.md)

| 冻结提交 | 测试 diff SHA-256 | 红灯 |
| --- | --- | --- |
| `c74f769` | `f185aaa0556870ba692b209557f99d1a571e6c254082e155e90cb58912c30ff3` | 9 项在磁盘夹具自检后触发接口占位，退出 1 |
| `98ed07b` | `4f6a8e4b71773efaad8b966509b4e83717a19a44bceb30eb8a80dd85aae033d1` | 重开后回拨 30 秒仍发送。收到响应但缺 usage 被记作结果未知。2 项退出 1 |

另有 4 项重开后扩大数值授权的红灯，冻结于 `a65922c`，测试 diff SHA-256 为 `daa2d7c6e01c728d9aa16a9982488b866ca754c29d7babb5f4f884fbe69a5710`。`0193696` 把首次批准的尝试数、图片数、金额和时限绑定到持久账本。专项命令追加 `tests/integration/test_budget_initial_approval.py` 后为 38 项通过。

初次实现提交为 `8e99c5a` 和 `1fb8624`。独立审查在子进程进入 Fake 后强制退出，重开后可见 pending、8 张已计图片和未知费用，相同操作自动重发 0 次。确认费用后尚无 stage 提交也有回归。可重复以下专项命令，34 项通过，退出 0。Files 探针的 HTTP 和字节分账沿用 M0 的独立测试。

```powershell
uv run --no-sync pytest -q -p no:cacheprovider --disable-socket --allow-unix-socket tests/integration/test_budget_ledger.py tests/integration/test_budget_restart_clock.py tests/integration/test_budget_response_cost.py tests/unit/test_budget_guard.py
```

使用真实磁盘和 Fake 端口，外发 HTTP 和图片均为 0。真实计费未实测。正式索引入口在 M3.5 装配本执行器，M1.5 切片保留既有预算合同。
