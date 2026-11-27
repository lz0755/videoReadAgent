# M6.1a 固定数据与计分

三个公开入口选择 D1 开发、留出常规题和留出事件。读取器核对冻结回执、源清单、原始视频字节、盲输入及原计分器。验收阶段再读取与回执匹配的私包，检查原视频、系列、关联素材和哈希的分组。开发集保持 16 段 64 题；留出集保持 8 段 32 题，其中 24 道可答，另有 20 个事件请求。所有定位均复用 D1 的最高 IoU 一对一算法及双边 ±2000 ms 门槛。

先执行夹具自检，PyAV 实测无音轨、8 个帧时间戳依次为 0 至 1750 ms，步长 250 ms。随后在签名占位上运行下列两文件，得到 14 failed、1 passed，失败来自缺失行为和错误 CLI 返回。冻结测试字节的 SHA-256 为 `42c00f7363d8af07fe03ba278236114176e134a9af0673de8ac2bf0e51183d4c`（计分）及 `776463675fb45c60bdee4fb5fb51bd0b3234dd0bc207138de9304739e19ca83b`（运行器）。实现后相同 15 项通过。

```powershell
uv run --no-sync pytest -q tests/integration/test_eval_runner.py::test_fixture_properties --disable-socket --allow-unix-socket -p no:cacheprovider
uv run --no-sync pytest -q tests/unit/test_eval_metrics.py tests/integration/test_eval_runner.py --disable-socket --allow-unix-socket -p no:cacheprovider --tb=line
uv run --no-sync python -m eval.d1 --repo-root data/d1-evaluator/frozen_root --packet data/d1-evaluator/packet.json --receipt eval/manifests/d1_freeze_receipt.json
```

D1 重验返回 `verified` 和原有回执摘要 `c7df675473fe97ced31e7358e6299d1920b4bc363765a0d12ff12c8653b36fe4`。下一依赖为 M6.1b 的实际运行和人工复核。公开交接只记录计数与摘要。
