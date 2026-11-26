# PR-M2.1 本地受管导入

用户选择的文件经同一打开句柄按 8 MiB 复制并计算 SHA-256。FFprobe 与 PyAV 在受管副本上验证容器、视频流和展示时长。源文件和本地授权回执在同一 SQLite 事务登记，成功后返回 ready 资产。回执使用现有的 export 产物种类，通过 asset_id、SHA-256、时间和授权说明回查。[M2.0 前置](PR-M2.0.md) · [导入编排](../../src/video_agent/application/ingest.py) · [本地存储](../../src/video_agent/adapters/storage/filesystem.py)

```powershell
uv run --no-sync video-agent assets import .\example.mp4 --data-root .\data --authorization "本地授权记录编号" --json
uv run --no-sync pytest tests/security/test_import_paths.py -q --disable-socket --allow-unix-socket --tb=line -o cache_dir=data/.m2-import-test
```

第一条是使用示例。第二条已执行，独立审查复核同样得到 10 passed。测试覆盖真实 524288000 字节的有效 MP4、63 次复制进度、超限 1 字节、已占用配额、源变动、中断、损坏、超时长、实际 Windows junction 和重复 asset_id。原始文件删除后仍能打开受管源，并从 SQLite 取回源和回执各自的散列。默认检查实际剩余磁盘空间；可用 `--quota-bytes` 指定受管产物字节上限。

Step 0 提交 `667d2bb`。Step A 独立复核要求补测充足配额下已存在的资产 ID。修订后上述 pytest 命令退出 1，CLI 缺少 assets 子命令，另九项命中导入入口的签名占位。冻结提交 `cc8ce3d`，测试文件 SHA-256 为 `a74f8ba9e4ef649a2b296143f864644b3bd0f77fed875fd7035cd4b7ac7be304`。实现后原测试退出 0，Ruff、mypy 通过。SQLite 使用 M1 的 schema 和迁移。FFprobe 检查具有 30 秒截止时间。测试使用合成素材，真实模型、HTTP、外发图片与费用均为零。

受监督复制遇到异常会删除临时文件，下次独占导入也会清理中断留下的受管复制临时文件。成品提升与数据库提交之间遭到进程硬终止时，数据库不会发布半成品，孤立成品的通用回收属于 M5。下一依赖是 M2.2 时间轴与 M2.4 派生媒体缓存。
