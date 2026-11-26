# M1.5-A2 开发切片复测

第一轮经人工确认，可答题为 4/9，恰当拒答为 3/3，引用合法为 9/9。复测沿用原 3 段 D0、12 道问题、可答性和 7/9 门槛。两轮使用独立的输出目录、运行 ID、分析 ID 和人工判定记录。[首轮评分](../../data/reports/m1_5_openai_live/slice_live_reviewed.json)

## 五道失分题

| 题序 | 首轮可核验的证据缺口 | 本轮调整及本地复验 |
| --- | --- | --- |
| 02 | 题目比较约 1 秒和 8.5 秒，首轮实际只取到 0、5 秒。 | 同一受管抽帧入口按 1 秒请求，实际覆盖 0–9 秒。视觉观察要求将动作与位置分别绑定到对应帧。 |
| 05、06、07 | 短暂的界面出现在 [2000, 5000) ms，首轮最近的实际帧为 5004 ms。 | 现有采样参数改为 1000 ms、每段最多 12 帧，实际取得 2502 ms 的界面帧。观察按可读的行列、标签、选中状态记录，并保留看不清的细节。 |
| 11 | 题目所指时段已有画面和语音，前五个候选全部是视觉观察。 | 对显式秒数定位，同时取端点附近的画面观察和原始转写。用首轮证据重新检索，前五条包含 [33200, 36760) 和 [43360, 45960) ms 两条原始转写。 |

检索只使用用户问题和当前资产、分析的证据。中英文显式秒数支持单点、区间和毫秒精度的小数。候选在附近一秒内查找，区间覆盖两端及中间，调用方给出的半开窗口始终限制候选范围。动作持续时间保留普通词项检索。候选继续显示证据本身的实际时间。视觉模板版本为 `vision_v2`，SHA-256 为 `167e2840e18c3a830067cd6c50506b6e2809052ade75082392cb7196cd2efc4b`。标注和参考答案仅供独立验收使用。[检索实现](../../src/video_agent/application/slice.py) · [视觉提示词](../../prompts/vision/observe.jinja2)

## 回归和预算

独立审查在 `fa88664` 冻结了第一个测试文件，红灯为 6 项失败、1 项通过。在 `6636443` 冻结了时点与持续时间的补充用例，红灯为 3 项失败、1 项通过。两份回归、旧切片入口及 Prompt Registry 合计 21 项转绿。

```powershell
uv run --no-sync pytest tests/unit/test_slice_time_evidence.py -q --disable-socket --allow-unix-socket --tb=line
uv run --no-sync pytest tests/unit/test_slice_duration_intent.py -q --disable-socket --allow-unix-socket --tb=line
uv run --no-sync pytest tests/unit/test_slice_duration_intent.py tests/unit/test_slice_time_evidence.py tests/integration/test_vertical_slice.py tests/unit/test_prompt_registry.py -q --disable-socket --allow-unix-socket --tb=line
uv run --no-sync python -m eval.slice_runner --manifest eval/manifests/slice.jsonl --offline --budget-config data/reports/m1_5_openai_r2/rehearsal_budget.yaml --sample-interval-ms 1000 --max-frames 12 --data-root data/reports/m1_5_openai_r2/rehearsal/work --output-dir data/reports/m1_5_openai_r2/rehearsal
```

离线演练完成 3 段、12 题，三段分别取到 10、12、12 帧，合计 21 次模拟调用。第二轮取得单次明确授权，模型为 OpenAI `gpt-6-luna`，推理最多 21 次，图像最多 34 份，每次最多 4 图，估算费用上限 0.10 美元，总时限 900 秒，单请求 45 秒，另限一次模型访问预检。启动器独占领取本轮额度，通过本机掩码窗口接收密钥。[演练报告](../../data/reports/m1_5_openai_r2/rehearsal/slice_offline_report.json) · [本轮授权](../../data/reports/m1_5_openai_r2/approved_budget.yaml)

本机从项目根目录启动本轮程序。启动器核验切片清单、标注、权重、视觉模板和预算文件的 SHA-256。收到密钥后，它独占领取本轮额度，查询模型访问权限，再执行 `eval.slice_runner --live --provider openai`。输出保存于 `data/reports/m1_5_openai_r2/`。初次打开窗口时为 `awaiting_local_credential`，此时模型查询和推理计数均为零。[当前启动状态](../../data/reports/m1_5_openai_r2/status.json)

```powershell
.\.venv\Scripts\python.exe data/reports/m1_5_openai_r2/run.py
```

生成报告后，启动器自动准备本轮 `manual_review.md` 和待填写的 `manual_judgments.json`，并在本机独立复核引用归属、PTS、音频边界、SQLite 登记与产物哈希。人工确认 12 项判定后，可用下面的命令重算本轮评分。程序同时检查报告 SHA-256、运行 ID 及全部题目 ID。

```powershell
.\.venv\Scripts\python.exe data/reports/m1_5_openai_r2/review.py --score
```

## 实际执行

运行 `64ca4733ebd14036ac8a19e87e249b5a` 完成前两段的 8 题。第三段的受管帧和 WAV 已落盘，本地 ASR 在默认 120 秒截止时退出，错误码为 `DEADLINE_EXCEEDED`。该段分析账本为零次云请求、零图像，四题均为 `not_run`，原始报告保存在本轮目录及其 `failed_runs` 快照，诊断为 `pause`。

OpenAI 访问预检返回 HTTP 200。已完成部分有 14 个不同的响应 ID，发送 22 图，输入 68,785 tokens，输出 4,002 tokens，估算 0.0088795 美元，未知费用事件为 0。独立核验了 44 个登记产物和 10 次答案引用，SQLite 外键检查为零项错误。界面三题均引用 2502 ms 的真实帧。第 02 题只回答了动作起点。[本轮原始报告](../../data/reports/m1_5_openai_r2/slice_live_report.json) · [独立核验](../../data/reports/m1_5_openai_r2/acceptance_audit.json)

使用原来的 960,000 个 PCM 样本、同一模型权重与转写配置在本机重做第三段 ASR，耗时 96.151 秒，得到 21 段，边界均在 [0, 60000) ms 视频范围内，末段结束于 58240 ms。新建正式适配器复读时逐项命中同一缓存。单独对最后四题的离线演练计入 7 次模拟推理、12 图。真实云端补测与人工裁定分别记录。[本地 ASR 核验](../../data/reports/m1_5_openai_r2/local_asr_precompute_audit.json) · [剩余四题演练](../../data/reports/m1_5_openai_r2/rehearsal/remaining/slice_offline_report.json)

独立代理逐题预审了已回答部分，建议第 01、03、05、06、07 题正确，第 02 题错误，第 04、08 题拒答恰当。原始报告仍保留第 09–12 题的执行缺口，正式人工评分沿用已签核的历史记录。[本轮八题与待补题的逐项对照](../../data/reports/m1_5_openai_r2/manual_review.md)

## 第三段补测与原 12 题联合验收

用户单独批准第三段的 4 题，时限 600 秒，最多 7 次推理、12 图、每次 4 图、估算上限 0.03 美元，单请求 45 秒。启动器复核首次执行的失败行、内容哈希和本地 ASR 缓存后使用原受管目录。补测的模型元数据请求为 0 次，运行器退出 0。从领取额度到原始报告落盘约 53 秒。[补测批准](../../data/reports/m1_5_openai_r2/continuation/approved_budget.yaml) · [原始报告](../../data/reports/m1_5_openai_r2/continuation/slice_live_report.json)

| 使用的题目 | 真实运行 ID | 原始报告 SHA-256 | 推理 / 图像 | 输入 / 输出 tokens | 估算 USD |
| --- | --- | --- | --- | --- | --- |
| 01–08 | `64ca4733ebd14036ac8a19e87e249b5a` | `84b431d5a9c20819e4a65a10c26d3fb306a1ea0bf74bda68aa6cccb8b1bdfdcb` | 14 / 22 | 68,785 / 4,002 | 0.0088795 |
| 09–12 | `1eaf3e0196aa491ba41d84d4e7e9e1c9` | `806262148900ba3a6ffa4f5941f107416e68b9ba229a5fdd06ca9b3b15d5f5dc` | 7 / 12 | 37,072 / 3,535 | 0.0054747 |
| 合计 | 两次实际执行 | 按原清单题序 | 21 / 34 | 105,857 / 7,537 | 0.0143542 |

联合核验确认 3 个不同的 analysis_id、同一 Prompt bundle、21 个不同的真实响应 ID。34 张帧、21 段转写、49 条观察和 57 条检索候选可回查。19/19 次答案引用均来自本题候选并绑定其资产和分析。SQLite 登记的 45 个产物全部通过大小和哈希校验，外键异常为 0，未知费用事件为 0。首次执行中第三段的 4 个 `not_run` 仍在原失败快照中。[失败快照](../../data/reports/m1_5_openai_r2/failed_runs/64ca4733ebd14036ac8a19e87e249b5a/slice_live_report.json) · [联合核验](../../data/reports/m1_5_openai_r2/joint/acceptance_audit.json)

本机运行 `.\.venv\Scripts\python.exe data/reports/m1_5_openai_r2/joint/review.py` 可重建核对页。该页将原题、固定标注、模型主张和引用的实际帧或转写放在一起，按题记录各自的来源。复核文件同时绑定两份原始报告的 SHA-256 和联合报告的 SHA-256。人工判定齐备后，同一入口的 `--score` 模式调用项目已有的 `evaluate_slice_report` 重算固定门槛。[逐题复核页](../../data/reports/m1_5_openai_r2/joint/manual_review.md) · [联合报告](../../data/reports/m1_5_openai_r2/joint/slice_live_combined.json)

本次运行 `uv run --no-sync pytest tests/unit/test_slice_scoring_regressions.py -q --disable-socket --allow-unix-socket -o cache_dir=data/.m15-combined-tests --tb=line`，6 项评分回归全部通过。
