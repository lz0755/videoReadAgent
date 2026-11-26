# M3.5 版本化索引与本地入口

`video-agent index` 串起受管素材、主视频流 PTS、音轨、窗口、视觉预算、证据存储以及 FTS。每条检索文档保留原文、共享 tokenizer 的词项、来源 ID 和时间，并限定在 asset_id 与 analysis_id 下。磁盘事务同时发布文档、FTS、索引版本、阶段记录和 ready 状态。取消、并发来源变化或事务失败会阻止发布。相同内容和版本可重复发布，损坏或孤立的当前索引记录会重建。[M3.4](PR-M3.4.md)

summarize 阶段用最多六条原文摘录生成本地短摘要，每条都能解析回观察、模态、实际时间和原始证据。全局视觉覆盖缺口来自所有已提交窗口的实际帧点。摘要文件与索引在同一事务登记，重开会验哈希。该实现额外模型尝试数为 0。输出表保留全部观察。

| Step A | 测试 diff SHA-256 | 红灯与实现 |
| --- | --- | --- |
| `eaf7f43` | `a09e3e846bfdc11f7bc7a247fc9e56f3ae0e310ac590af5386bb8d8077d4262b` | 签名 `1f4df79`，2 项写入与回滚用例退出 1，`9e9d84e` 实现 |
| `8cce679` | `9c02dbd1419c14321c047bb1c612887761d1200dd552edd9ed29c4a20b6078a4` | 已取消作业仍可发布，孤立 FTS 行重建重复；2 项红灯，`1a13fa2` 修复 |
| `d19d2cd` | `b581e928de084e9db26f77f16bfe5c2a869c8d601ef0c41df20aa2e84c0a94dd` | 真实导入后 CLI 无 index 命令，`332cbe2` 接入 |
| `5cf3fe2` | `d150b42d626819fc3b83c9013bf8eaab55ba8afef2df8a63551de55bca04e71c` | 4 项重开错误涉及配置、未知 ID、到期或文件损坏，`2995d4a` 修复 |
| `8d40075` | `fc888d4f091b374b6a1dd9a7ac41d88fd8cbf07e07e7254a59a115aef9655aa1` | 3 条文档写入后已提交摘要仍为 0，`f7312a2` 实现，`466c109` 调整 SQL 排版 |

```powershell
uv run --no-sync pytest -q -p no:cacheprovider --disable-socket --allow-unix-socket tests/integration/test_timeline_summary.py tests/integration/test_search_index_write.py tests/integration/test_index_entrypoint.py tests/integration/test_index_repair.py tests/integration/test_index_resume.py
```

10 项通过。用 46 秒合成视频调用正式 CLI，首次得到 3 窗、10 个独立采样、3 个 Fake 尝试和 12 个已发送到 Fake 的图像块。重开后 3 窗复用，新增尝试、图像发送和产物均为 0。另通过独立 SQL 检查两段重叠窗口的全局缺口及 5 条摘录的来源。正常入口命令和总体门禁见总交接。进入 M4 调优还依赖 D1 冻结。[M3](PR-M3.md) · [D1](PR-D1.md)
