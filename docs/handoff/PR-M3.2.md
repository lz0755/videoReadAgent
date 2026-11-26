# M3.2 视觉输入、裁剪及响应验证

软件教学保留 1080p 范围内的原始尺寸，概览最长边 960。服务端从受管原帧解析 full 或 r1c1 至 r3c3，按实际像素外扩并裁剪，保留父证据、实际 PTS、几何和图像哈希。网格辅助图不能充当事实证据。索引默认发送全帧，只有当前模型对应能力已实测时才设置 high。小字的原图、缩放及 ROI 对照沿用 M0 的八组实验记录。[对照实验](PR-M0.3.md) · [采样](PR-M3.1.md)

视觉端口同时检查原始响应和候选观察，验证类型、所属分析、当前窗口、引用来源与实际时间。编码文件须匹配声明的尺寸、格式和大小。上游已返回请求 ID 或 usage 时，即使响应 JSON 解析失败，首尝和修复各自收支入账。此处计数使用离线端口和 SDK 响应夹具。M3 未复测云端小字识别率。

| Step A | 测试 diff SHA-256 | 红灯与实现 |
| --- | --- | --- |
| `f1b17f1` | `49433ef1a64553f817059a19961c1f08c99f277a5978e415e2db44669b0df060` | Step 0 为 `5ca98b3`，11 项真实像素与引用相关用例退出 1，`8afa9d0` 实现 |
| `320faf7` | `b52d9daefa381c3850a71a4f7e0e44a83a838b08daf0e8c057c71d8034b720c4` | 两项错误接受了无效上游结构和伪装成原图的 crop，`17890a7` 修复 |
| `226cb66` | `1da7e9fe75a95398b37278429222d9ac0cecd633967fa83158612f9645e4095c` | 两项丢失已返回的计费回执，`8e08ca6` 修复 |
| `88863a0` | `990f33d6246aa9eea922c0cf1e9def9f5e375e67359d0e61ca0819120880cffc` | SDK 成功响应中的非法 JSON 在提取 usage 前失败，`0193696` 修复 |

```powershell
uv run --no-sync pytest -q -p no:cacheprovider --disable-socket --allow-unix-socket tests/contract/test_vision_provider.py tests/integration/test_roi_evidence.py tests/contract/test_vision_wire.py tests/integration/test_vision_invalid_usage.py tests/contract/test_vision_parse_receipt.py
```

16 项通过，退出 0。下游用已验证的观察与原始证据持久化关联。[M3.3](PR-M3.3.md)
