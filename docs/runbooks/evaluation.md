# M6 固定评测

执行者从仓库或源码发布包根目录运行命令。工程演练使用 Fake。正式运行使用冻结的 D1 盲输入，先封存输出，再由验收者打开金标。D1 回执同时绑定媒体清单、标注私包及事件计分算法。[D1 交接](../handoff/PR-D1.md)

## 开发集与盲输入

开发导出默认为 `data/d1-dev/questions_64.json`，盲输入默认为 `data/d1-evaluator/m6_blind_inputs.json`。两者均须匹配 D1 发布的 SHA-256。素材根目录默认为 `data/d1-evaluator/frozen_root`，可用 `--inputs` 和 `--media-root` 指向验收者提供的同内容副本。仓库内三个 JSONL 文件仅选择 D1 集合。开发集读取器向索引和问答传递视频与题干。

```powershell
uv run --no-sync python -m eval.runner --manifest eval/manifests/dev.jsonl --offline
uv run --no-sync python -m eval.runner --manifest eval/manifests/heldout.jsonl --event-manifest eval/manifests/heldout_events.jsonl --offline --output-dir data/m6/blind-engineering
```

默认模式为 offline。省略输出目录时，工程演练在 `data/m6` 下创建唯一目录。每段视频导入时核对授权和文件哈希，建立 M3 索引，每道题使用 M4 的实际入口。事件请求先检索、再检查两侧边界。工程报告用 `mode=offline_fake`、`quality=null` 表示用途。没有私有数据的安装可以运行 README 中的合成媒体验收。

## 正式运行

1. 验收者固定代码、配置、模型和输出目录。从 `config/examples/m6-budget.yaml` 复制一份至私有目录，登记授予这次外传的依据、供应商、模型和准确的八个媒体 SHA-256。D1 的本地研究授权与早期探针预算各有其适用范围。云端会收到选中画面、问题及上下文。
2. 填写经批准的整场尝试数、图片发送数、美元估算上限、总时限和单请求限制，并显式打开两个授权开关。费用上限约束发送前的估算，实际账单由供应商确认。配置与预算中的模型须一致。凭据仅从该供应商指定的环境变量读取。本地准备 faster-whisper 的完整权重目录，保留权重内容摘要。
3. 在新的目录执行一次。以下命令假设验收者已设置 `M6_ASR_DIR`、准备好私有预算，且使用默认配置的供应商。其他已接通的供应商用 `--config` 指向对应完整 AppConfig。

```powershell
uv run --no-sync python -m eval.runner --manifest eval/manifests/heldout.jsonl --event-manifest eval/manifests/heldout_events.jsonl --live --budget data/m6-approval.yaml --asr-model "$env:M6_ASR_DIR" --output-dir data/m6/acceptance_01
```

输出目录以独占方式创建。每次索引或问答从同一磁盘账本计算剩余尝试、图像、费用和时间。未知费用、待结算预留、额度用尽或超时会阻止后续相关发送。失败、停机和未执行的请求都保留编号。若进程意外退出，保留原目录、账本与已有输出，核对供应商回执；另建运行需要新的验收决定，并披露先前运行和留出集暴露情况。

## 报告与人工评分

| 本地文件 | 用途 |
| --- | --- |
| `report.json` | 模式、代码与锁哈希、三种 Prompt bundle、模型和权重标识、采样、预算、媒体摘要、索引 RTF、费用及未完成请求。无回看、带回看、事件三组各报最近秩 P50/P95、样本数及对应尝试、图像、token、费用和缺账数。RSS 为主进程高水位，子进程和 GPU 显存未单独采集。 |
| `trials.json` | 私有逐请求输出，含绑定的资产、分析、QA 作业、首次检索、回答、引用和边界包围区间。两个边界各有实际前后帧。完成报告记录该文件的 SHA-256。 |
| `managed/` | 可重新核对原片、帧、转写、已提交阶段和 SQLite 尝试账本。使用本机服务按 `asset_id`、`analysis_id` 和证据 ID 回看。 |
| `quality.json` | 正式评分结果、每项计数、错误编号与失败原因。初评或二审覆盖不足时按复核文件摘要生成 `quality-review-*.json`，补齐后可用同一批已封存预测完成评分。相同内容可重验，冲突的最终评分会保留首次发布文件。 |

整场执行耗时从输入预检和建库完成后起算，索引 RTF 以单片导入及索引耗时除以实际媒体时长。每个有返回的请求保留答案状态和故障码，因此“有输出”可以包含明确拒答或预算终止。

验收者对每个有输出的常规问题回看原证据。可答题判关键事实、数值与顺序，完整正确填 `correct`，其余填 `incorrect`。不可答题填 `appropriate` 或 `inappropriate`。逐条 Claim 拆分所有可核事实，每个布尔值对应一个命题在原始证据中是否有充分支持。inferred、带限定词的断言、gaps 中的可核事实均纳入。每个 Claim 的列表至少容纳一项；空 Claim 列表用 `[]`。评分时重读数据库和证据，已有文件损坏或归属不符会影响引用完整性。

复核文件由验收者写入私有目录。下面是单题的格式示例，实际文件按盲输入 ID 收录。至少 7/32 道常规题应有同意结论的第二次复核。同一人的隔天盲复核另计，需要后一天日期和 `"blind":true`，报告披露其独立性限制。未复核完毕时可查看工程报告和私有输出，在封存最终复核文件后再发布评分。

```json
{
  "trials_sha256": "<report.json 中的摘要>",
  "reviews": {
    "<question_id>": {
      "verdict": "incorrect",
      "assertions_by_claim": [[true, false]],
      "other_assertions": [],
      "error_tags": ["20_vs_200"],
      "reviewer": "reader-a",
      "reviewed_on": "2026-11-27",
      "second_review": {"reviewer":"reader-b","reviewed_on":"2026-11-28","agreed":true}
    }
  }
}
```

```powershell
uv run --no-sync python -m eval.report --run-dir data/m6/acceptance_01 --packet data/d1-evaluator/packet.json --reviews data/m6-private/reviews.json
```

常规正确至少 20/24，首次检索至少 22/24，适当拒答至少 7/8。首次前五候选中的实际帧须落入标注半开区间，实际转写区间须与相应模态的标注相交。事实支持率至少 90%，引用归属完整性为 100%，分母为零不能达标。事件按请求一对一匹配同标签及正时间交集，采用 D1 的最高 IoU 和同分裁决；起、止误差同时在 ±2000 ms 内才算命中，至少 17/20，多余预测另报。未检出、partial、执行失败和超预算保留在原分母。

场景切片来自冻结的 benchmark、题目类别及源任务类别。D1 缺少独立讲解场景标签和逐字语音真值，所以讲解专项与 CER/WER 报未测。使用留出失败修改模型或 Prompt 后，须记录暴露并使用新的独立留出集进行下一次正式验收。
