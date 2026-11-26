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
