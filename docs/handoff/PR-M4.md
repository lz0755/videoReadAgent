# M4 工程验收与交接

在本地已发布的 M3 分析上，`video-agent ask` 可以完成问答和事件定位。它用同一个问题 ID 记录首次检索、工具调用、模型用量和答案。结果包含 `question_id`、`qa_job_id`、`first_hits` 和可追溯的证据引用，可供 M5 的作业与查询接口使用。本轮从 M3 与 D1 冻结提交 `4a342b5` 开始，代码与测试验收点为 `d14ddfc`。[本地入口与持久化](PR-M4-integration.md) · [D1 交接](PR-D1.md)

## 在已发布分析上提问

在仓库根目录使用先前导入与索引返回的 `$assetId` 和 `$analysisId`。数据目录与索引时保持一致，命令默认使用 `data`。离线模式使用本地 Fake，返回的账本可核对每笔发送。

```powershell
uv run --no-sync video-agent ask --asset-id $assetId --analysis-id $analysisId --question "在哪里修改孔径？" --offline --json
uv run --no-sync video-agent ask --asset-id $assetId --analysis-id $analysisId --question "白色方块什么时候出现和消失？" --mode event_localization --offline --json
```

## 验收口径与结果

各子项先复现行为失败，再由用户授权的独立子代理审查并冻结测试。各交接记录保留对应的测试差异哈希、生产提交、专项命令与结果。额外获批的两项范围是既有检索表的作用域索引迁移，以及从正式本地入口运行和持久化问答。

| 计划项 | 可复验的结果 |
| --- | --- |
| M4-A1 作用域检索与工具 | 中文 bigram 和单字回退在磁盘 SQLite 上通过，单字最多检查当前作用域 500 条文档。旧库在迁移 002 后，执行计划使用作用域索引，原有文档和 FTS 均可查。指定时间的问题优先返回对应端点的实际帧、转写。白名单只暴露检索、片段回看和双帧比较。[查询](PR-M4.1.md) · [时间检索](PR-M4.1-time.md) · [迁移决策](../decisions/ADR-003.md) · [工具](PR-M4.2.md) |
| M4-A2 预算与终止 | 每问上限为 5 轮、6 次工具、2 次昂贵回看、32 个发送图片块、10 次模型尝试、180 秒。一次 8 图回看重试的账本记录 1 次工具、1 次昂贵回看、2 次模型尝试、16 图。第九次模型尝试提出回看且只剩最终综合额度时，控制器保留最后一次文字综合。取消与未确认外部结果阻止后续发送。[工具计费](PR-M4.3a.md) · [Planner](PR-M4.3b.md) · [适配器合同](PR-M4-planner-wire.md) |
| M4-A3 引用与双边精修 | 提交答案前核对本问题可见、属于指定分析的原始帧或转写。引用帧缺失或内容被等长改写会得到缺口。在确定性终止时重新核对中间结论。单侧覆盖不足、状态反转不明或预算耗尽时报告原因。事件精修成功要求四个严格有序的真实帧点，并保存两组边界引用。[回答引用](PR-M4.4.md) · [事件精修](PR-M4.5.md) · [正式入口](PR-M4-integration.md) |

在 7 fps 合成片上，独立解码得到首次出现和消失分别为 4428 ms、14857 ms。正式 `ask` 调用使用根据实际图片像素判断方块状态的视觉 Fake。一次检索、两次回看、26 个图片块给出开始区间 [4285, 4857] ms 和结束区间 [14285, 14857] ms。四个边界引用从 SQLite 查回的帧时间与像素状态一致。[合成片及独立解码](PR-M4.5.md) · [入口验收](PR-M4-integration.md)

三段授权开发视频的离线重放覆盖 12 道既有问题。对 9 道可答题，首次前五项在至少一个标注时段找到相应模态证据的为 9/9，覆盖全部标注时段及模态的为 8/9。57/57 个候选引用可解析到原帧或转写。重放使用既有输出，逐条校验后剔除了 4 条用帧引用证明听觉事实的旧观察，两段索引记录为 partial。这些数字衡量首次检索的时间和模态覆盖。留出集上的语义召回、问答正确率以及 20 个冻结事件的定位质量由 M6 单独评测。[重放方法与分母](PR-M4.1-time.md)

## 工程门禁

| 环境 | 实测结果 |
| --- | --- |
| Windows 默认离线套件 | 470 passed，1 deselected，209.94 秒 |
| Linux 断网容器默认套件 | 469 passed，1 deselected，147.95 秒。Windows 多收集一项依赖本机授权 D0 素材的参数用例。 |
| 静态与架构 | Ruff 通过，159 个文件格式通过，mypy 检查 157 个源文件通过。两项 import-linter 合同与默认套件中的 AST 架构测试通过。 |

Windows 本机使用 Python 3.14.6、FFmpeg 8.1.2。Linux 使用已有锁定依赖的本地镜像，Python 3.14.10、FFmpeg 7.1.5。两端均运行离线 doctor。以下命令为 Windows 整仓检查。

```powershell
uv run --no-sync ruff check src experiments tests
uv run --no-sync ruff format --check src experiments tests
uv run --no-sync mypy src/video_agent experiments tests
uv run --no-sync lint-imports
uv run --no-sync video-agent doctor --json
uv run --no-sync pytest -q -m "not provider_live and not model_local" --disable-socket --allow-unix-socket
git diff --check 4a342b5 d14ddfc
```

Linux 运行时只读挂载从 `d14ddfc` 导出的 199 个仓库文件。快照 SHA-256 为 `cc6ee22760191fdc7d6a1c654e873cb88c57eeca5e3279a35f711a09eb33d10e`。容器不接入宿主的媒体、运行数据库或凭据。运行期间 `docker inspect` 返回网络模式 `none`。独立 Python 子进程和系统 curl 的外连探针均被系统拒绝。

```powershell
New-Item -ItemType Directory -Force data/m4-gates | Out-Null
git archive --format=tar --output=data/m4-gates/d14ddfc-source.tar d14ddfc -- .gitattributes .github AGENTS.md README.md Dockerfile.ci pyproject.toml uv.lock src config prompts experiments eval tests
$snapshot = (Resolve-Path -LiteralPath 'data/m4-gates/d14ddfc-source.tar').Path
$image = 'sha256:0a84ed6b5f0073fc65b4081c4404e2a190422eff3557c45f2c9f714b01d37082'
$inside = 'tar -xf /snapshot.tar && /app/.venv/bin/python tests/canary/network_denied.py && /app/.venv/bin/python -m video_agent.cli doctor --json && /app/.venv/bin/python -m pytest -q -m not\ provider_live\ and\ not\ model_local --disable-socket --allow-unix-socket --tb=line'
docker run --rm --pull=never --network none --cap-drop ALL --security-opt no-new-privileges --name video-agent-m4-d14ddfc --mount "type=bind,source=$snapshot,target=/snapshot.tar,readonly" --workdir /work -e PYTHONPATH=/work/src:/work -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 $image sh -ec $inside
```

真实供应商和本地 ASR 模型专用作业不在本次离线验收内。没有向云端发送本项目的媒体或问题。M5 可继续接入问答状态、原始证据预览和单 Worker 恢复。发布质量门槛沿用 D1 冻结口径。[M3 索引与已有模型记录](PR-M3.md) · [D1](PR-D1.md)
