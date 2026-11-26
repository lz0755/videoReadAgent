# M3.4 窗口阶段缓存

阶段键涵盖源内容哈希、模型及 revision、Prompt bundle、Schema、采样配置、crop 和本次输入摘要。读取已提交窗口时重新校验清单、父帧、实际输入图像的在盘校验和与资产归属。窗口失效时连带失效其下游索引及摘要。重算采用新的物理产物绑定，以便历史证据仍能追溯。暂存文件或已写出但没有阶段提交的文件不能使窗口变为完成。[M3.3](PR-M3.3.md)

签名 `d5f3685`，测试冻结 `c60dadd`，diff SHA-256 为 `a98b557be5e5635f07c668ee3419dbafdbd4b88b1f49456187bf66abd643b72d`。5 项红灯在真实 SQLite 与受管图像夹具自检后触发未实现行为。实现为 `2ae65af`。独立复核另用事务触发器制造“文件先落盘、提交失败”的状态，重开时没有认领残留产物。

```powershell
uv run --no-sync pytest -q -p no:cacheprovider --disable-socket --allow-unix-socket tests/integration/test_stage_cache.py
```

5 项通过。这里验证单 Worker 的阶段恢复，下一阶段依靠完整的窗口集合发布索引。[M3.5](PR-M3.5.md)
