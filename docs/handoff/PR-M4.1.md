# M4.1 中文证据检索

M4-A1 使用已发布的 FTS5 文档。SQLiteSearch 在同一读事务里校验资产、分析、提交阶段及整套文档版本，按匹配分数、时间和来源取默认前五条。查询复用共享 tokenizer，MATCH 表达式由转义后的受控词项构造。返回原文、半开时间区间及可追溯的观察或转写引用。[写入端交接](PR-M3.5.md) · [读取实现](../../src/video_agent/adapters/storage/search.py)

单字回退先按资产、分析和可选时间范围限定候选。超过 500 条时返回带 scan_limit 的结构化错误，调用者可缩小时间范围。独立审查发现初始 DDL 的执行计划会遍历其他资产。用户批准迁移 002，同步更新 schema.sql 快照；真实 001 数据库升级后，原文及 FTS 文档保留，三条查询的 EXPLAIN 均按两键 SEARCH。[ADR-003](../decisions/ADR-003.md)

| 行为 | 红灯和冻结提交 | 测试 diff SHA-256 | 绿灯 |
| --- | --- | --- | --- |
| 首次查询 | 签名 ce286dd；12 项在 search 方法的占位失败。a950920 | `45bcda69830924d52e77647bd5b2af85ddaa212470828e8fe0ed81af3099d6e9` | c663491，12 项 |
| 作用域访问索引 | 3 项分别得到空迁移列表和两次 [1]，预期 [2] 或 [1, 2]。919fea7 | `997348d7e8de3766064b5efeff0b24ecf327f3a264161c8161a7bd8e4b344de6` | e075662，检索和迁移合计 16 项 |

c698a25 按审查意见拆行一处测试 SQL 字符串。前后完整测试文件 AST 的 SHA-256 均为 `7dcedce34db6a39d4156a613bc713c037e474ac0564e0a4f4d7e3f1a4415693d`。独立磁盘夹具三道检索题的 Recall@5 为 3/3。发布质量指标使用 M6 冻结留出集。[D1 验收](PR-D1.md)

```powershell
uv run --no-sync pytest -q --disable-socket --allow-unix-socket tests/integration/test_chinese_search.py --tb=line
uv run --no-sync pytest -q --disable-socket --allow-unix-socket tests/integration/test_search_scope_migration.py tests/integration/test_migrations.py --tb=line
uv run --no-sync pytest -q --disable-socket --allow-unix-socket tests/integration/test_search_scope_migration.py tests/integration/test_migrations.py tests/integration/test_chinese_search.py --tb=line
```

红灯均退出 1，绿灯退出 0。M4.2 在绑定作用域的工具内使用该端口。[时间查询兼容](PR-M4.1-time.md) · [里程碑验收](PR-M4.md)
