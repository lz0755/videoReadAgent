# M4.2 服务端绑定的证据工具

M4-A1 的 Planner 只可提出 search_evidence、inspect_clip 和 compare_frames。工具在执行前校验严格字段、时间、允许的命名区域及当前任务配置。服务端提供资产、分析和证据读取端口。inspect 最多取 8 秒、2 FPS、16 个请求点，compare 要求两个不同的合法时间点。工具结果保留可见观察、实际帧和采样缺口。[工具合同](../../src/video_agent/agent/tools.py) · [检索](PR-M4.1.md)

签名 0d5a0e0，Step A ff388a7，测试 diff SHA-256 为 `bf7cd7517e90ad7d53c79863bff244725771dc00ae95be53dbc3e62d52965152`。16 项红灯均来自有效调用进入工具签名后抛出 NotImplementedError，退出 1。

实现后首次运行 15 项通过，Windows Proactor 在进入最后一项 Fake 协程前创建自唤醒 socket，触发 SocketBlockedError。独立审查者复现后批准仅替换两处协程驱动，使用已有离线 _run_coro。修正提交 0e0cf2d，测试 diff SHA-256 为 `24611e8bdeb4eb275f4d0ae71162c9ca8781ed204d783ecca55229acae180be7`。最终实现 6b41c51，16 项通过，退出 0。

```powershell
uv run --no-sync pytest -q --disable-socket --allow-unix-socket tests/security/test_agent_tools.py --tb=line
```

该专项使用单元测试的受控端口。实际 SQLite、媒体解码、图片、计费与提交分别在后续子 PR 贯通。[逐工具计费](PR-M4.3a.md) · [本地入口](PR-M4-integration.md)
