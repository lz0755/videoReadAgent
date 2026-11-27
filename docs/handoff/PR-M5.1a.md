# M5.1a 本机授权上传

`POST /v1/assets` 在验证回环 Host、Origin 和 Bearer 令牌后读取 multipart。每次 ASGI receive 累计请求体字节，解析器另外累计文件实际字节。默认文件上限为 524288000 字节，封装另有 65536 字节。认证失败、越界、中断和截断有独立错误。文件名只作显示用，导入器生成受管位置，按内容哈希与授权回执登记。[接入合同](../decisions/ADR-004.md)

CLI 与 HTTP 导入共用导入锁，在常驻 Worker 持有计算锁时仍能导入。文件超过解析器内存阈值后使用临时磁盘，再沿 M2 同文件系统暂存与发布路径登记。此处没有测量 500 MiB HTTP 请求的峰值内存。边界测试使用可调小限额及真实文件，原有 CLI 导入测试保留完整 500 MiB 样本。

| 独立审查后冻结的用例 | 红灯与修复 |
| --- | --- |
| `b1dc26f` · SHA-256 `0097c2b88951a67687a31201ef8f6a7c484a6cae694832aab90d20b805774cc7` | 已有工厂签名，上传行为尚未实现。`031586d` 接通授权、限额、清理与导入锁。 |
| `662d613` · `115b37a2a696c009ca14150e7fcc13cc49606a1badc528cca788fd3d9919b314` | multipart 缺终止边界时落盘句柄仍打开。`e5c2be7` 补齐正常 EOF 时的清理。 |
| `327e0d3` · `9d9f4032ae36b475a5183563c15537de1c15a5ef6c14e4e503868d33fa26dc3c` | 空令牌及含 DEL 的令牌均未在工厂启动阶段拒绝，两例为 `DID NOT RAISE`。`688cb43` 在监听前验证。此冻结同时包括页面默认值。 |

~~~powershell
uv run --no-sync pytest -q tests/integration/test_fixture_self_check.py --disable-socket --allow-unix-socket
uv run --no-sync pytest -q tests/integration/test_upload_limits.py tests/integration/test_upload_spool.py tests/security/test_upload_paths.py tests/security/test_service_startup.py tests/security/test_import_paths.py --disable-socket --allow-unix-socket
~~~

夹具自检 12 项通过，上传专项及完整 CLI 样本均纳入整仓门禁。M5.1b 使用返回的受管资产 ID 提交分析。[整仓结果](PR-M5.md)
