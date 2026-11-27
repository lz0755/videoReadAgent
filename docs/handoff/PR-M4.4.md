# M4.4 回答引用归属与不足回答

AnswerValidator 同时检查问题、资产和分析，对每个 Claim 核对本次已提供的观察与磁盘解析的原始证据。视觉断言要求已观察的帧，语音断言要求转写，推断保留 inferred。定位输出另行核对两侧真实帧、实际 PTS、整数中点、区间宽度与顺序。[验证器](../../src/video_agent/agent/answer_validator.py) · [领域合同](../../src/video_agent/domain/models.py)

签名 894d832，测试冻结 7a6958b，测试 diff SHA-256 为 `ceadef9b19505eacfd60998b1e5f5f47920de088d797a191c05a898483eb0a45`。12 项在 validate 占位方法中失败，退出 1；64122aa 后全部通过，退出 0。独立审查者联合复跑答案验证和工具/Planner 专项 26 项通过。

```powershell
uv run --no-sync pytest -q --disable-socket --allow-unix-socket tests/contract/test_answer_evidence.py --tb=line
```

语义支持度交给 M6 的原始证据复核。SQLite 归属解析、QA 作业持久化和取消优先发布见贯通记录。[数据库与入口](PR-M4-integration.md) · [事件精修](PR-M4.5.md)

## 终止时复核与边界接触

在临近发布时删除一条已经通过校验的引用，旧控制器仍返回含该结论的 partial。让事件的开始上界和结束下界指向同一实际帧，旧验证器也接受了定位。独立审查确认两项红灯后冻结于 `b03c280`，测试 diff SHA-256 为 `37b3dc4a5d8f1548545136a7f7f6192b05e7ebf465d626b46f2327058cc4da5e`。生产提交 `dc58c26` 在所有确定性终止出口逐条重验已有结论，并要求两个边界之间有严格顺序。只要还有可核验的结论就返回 partial，全部失效则返回 not_answerable。复核直接使用已绑定的存储读取，不产生模型尝试。[回归测试](../../tests/contract/test_qa_final_citations.py)

```powershell
uv run --no-sync pytest -q --disable-socket --allow-unix-socket tests/contract/test_qa_final_citations.py tests/contract/test_answer_evidence.py tests/unit/agent --tb=line
.venv/Scripts/python.exe -B -m pytest -q --disable-socket --allow-unix-socket -p no:cacheprovider tests/contract/test_qa_final_citations.py tests/contract/test_answer_evidence.py tests/unit/agent/test_termination.py tests/unit/agent/test_controller.py tests/integration/test_event_localization.py --tb=line
```

两组复验分别通过 28 项和 36 项。独立审查者还验证了已有两条结论且其中一条失效的情况。回答保留一条可核验结论，全程一次模型尝试。首次整仓格式检查指出夹具调用需要换行，独立格式提交 `d14ddfc` 前后的测试 AST 完全相同，SHA-256 为 `94159d0673b876020084559ff1e4a3ef9068065e79a98fe1055aa2c8cecc78ca`，两项用例再次通过。
