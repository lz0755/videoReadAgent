# M3.1 窗口采样与覆盖

M3-A2 的采样策略通过独立审查。默认每 5 秒一个目标点，20 秒核心窗两侧各有最多 2 秒上下文。10 分钟共 120 个基础目标点，多个窗口可共享。唯一证据按实际 PTS 毫秒和裁剪像素范围计数。覆盖缺口记录窗口内没有采样证据的毫秒区间。[上游预算](PR-M3.0.md) · [实际 PTS 合同](PR-M2.2.md)

签名提交 `97f319f`。4 项测试在占位实现上退出 1，冻结于 `afeea45`，测试 diff SHA-256 为 `2f8aee7c7aa4ba4fb4f6d54c901d3d03d66155e3acb25f4b6b4858e99a2f46e6`。实现为 `27c20fd`。审查发现布尔和浮点时间可与整数去重，补充 2 项测试在 `1d12274` 冻结，diff SHA-256 为 `fca310fa177e967a1d9cff85bece5a7be73996fbfb5af3003a057003fa75d518`，红灯为未拒绝无效输入。修复为 `7a8213d`。

```powershell
uv run --no-sync pytest -q -p no:cacheprovider --disable-socket --allow-unix-socket tests/unit/test_sampling_policy.py tests/unit/test_sampling_coverage_types.py
```

6 项通过，退出 0。软件教程保留 1080p 范围内的原尺寸，支持横屏和竖屏。概览最长边 960 像素。云端调用和费用均为零。真实图像编码、来源与输入校验由 M3.2 验证。
