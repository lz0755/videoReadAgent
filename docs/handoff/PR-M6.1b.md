# M6.1b 离线与正式评测入口

`eval.runner` 通过已注册资产调用 M3 索引和 M4 问答。两类任务共用同一磁盘账本。结果保存首次前五命中的原始证据、答案归属、事件边界前后帧及实际时间。输出目录独占，执行前登记全部请求，逐题封存进度。模型标识、采样、Prompt、Schema、代码、配置以及实际用量可追溯。[运行和评分格式](../runbooks/evaluation.md)

Live 须提供专属授权、绑定输出位置、八个媒体哈希、供应商/模型、整场预算、凭据和本地 ASR。运行器逐任务扣除已有预留和消耗；未知费用保留并阻止下一次发送。Fake 运行只出工程报告。评分器重新读取账本和答案，并按绑定资产/分析核对证据。发布前回归发现仅修改顶层模式标签的伪 live 收据会进入私包阶段。新增测试初次为 1 failed、5 passed，冻结摘要 `a1d504e0fc441bbd93079fed7bf756597c526f4cafb38f8632dca54e37d3bd4a`；已在读取私包之前检查冻结身份和模型，专项通过。

```powershell
uv run --no-sync pytest -q tests/unit/test_eval_release.py tests/contract/test_m6_replay.py --disable-socket --allow-unix-socket -p no:cacheprovider
uv run --no-sync python -m eval.runner --manifest eval/manifests/dev.jsonl --offline --output-dir data/m6/dev-engineering-01
uv run --no-sync python -m eval.runner --manifest eval/manifests/heldout.jsonl --event-manifest eval/manifests/heldout_events.jsonl --offline --output-dir data/m6/blind-engineering-01
```

开发集实测 64/64 请求得到工程输出，耗时 251.44 秒，主进程峰值 220.41 MiB。Fake 账本记录 183 次模型尝试和 550 个图像部件。无回看常规 QA 的 n=64，P50 为 0.223 秒，P95 为 0.364 秒。开发集输出中 13 个 answered、51 个 not_answerable。正式留出集的实际用量和人工成绩仍等待 M6 专属授权。

### 分组用量与实际失败

复核发现原报告的延迟已按请求类型划分，但用量只有整场合计。新增专项初次因缺少 `usage` 失败。修正后每组都从对应问题的持久账本统计。冻结摘要为 `df05e3d31f388ff80ab5fa6701c9386b94c8c31d90ced7d8d3091504e1dd03ad`。另一个测试发现未完成的人工复核会占用最终文件名，初次 1 failed。现在暂存复核和最终结果分别发布，最终结果原子写入并可按相同字节重验。测试摘要为 `7b7e14bc6bb03c4f7aa2be2c44868a43bb06bb6a35342b995c1adf0d418a1e50`。两项专项均已通过。

相同的两个离线命令分别将输出目录改为 `data/m6/dev-engineering-final`、`data/m6/blind-engineering-final`，重新执行了全部工程请求。运行时的源码内容摘要为 `629f2dbfc2f84c9695163cf246066dfc135fa765ff362d590682b30d99c1292e`。后续的人工复核文件发布修正由单独的封存/重验测试覆盖。

| Fake 演练 | 请求、结果和资源 |
| --- | --- |
| 16 段开发 | 64 个 QA 均有输出。13 个 answered，51 个 not_answerable。246.61 秒，主进程峰值 220.13 MiB。Fake 账本 183 次、550 个图像部件，模拟输入/输出 token 为 86784/13760。无回看 QA n=64，P50/P95 为 0.246/0.394 秒。13 个输出引用均通过归属检查。 |
| 8 段留出盲输入 | 32 个常规 QA、20 个事件请求均有输出，52 个均为 not_answerable。160.53 秒，主进程峰值 187.81 MiB。Fake 账本 108 次、360 个图像部件，模拟输入/输出 token 为 54272/7936。常规无回看 QA n=32，P50/P95 为 0.229/0.318 秒。事件 n=20，P50/P95 为 0.221/0.261 秒。带回看的常规 QA n=0。 |
| 失败案例 | 开发题 `vg_pr10_start_q2` 和留出盲编号 `d1q-134b038a0c70c350ae7dbdcc` 均触发 INVALID_CITATION，最终答案无 Claim。20 个事件的首次检索均为空，M4 返回 NO_FRAME_IN_RANGE，双边包围区间无法建立。 |

两次 Fake 演练的云端出站、转移图像和实际供应商费用均为零。正式指标中的首次召回、两端精度、正确性和语义支持度仍未测量。32 道常规题与 20 个定位请求的真实费用和时延均未知。
