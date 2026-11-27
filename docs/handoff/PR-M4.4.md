# M4.4 回答引用归属与不足回答

AnswerValidator 同时检查问题、资产和分析，对每个 Claim 核对本次已提供的观察与磁盘解析的原始证据。视觉断言要求已观察的帧，语音断言要求转写，推断保留 inferred。定位输出另行核对两侧真实帧、实际 PTS、整数中点、区间宽度与顺序。[验证器](../../src/video_agent/agent/answer_validator.py) · [领域合同](../../src/video_agent/domain/models.py)

签名 894d832，测试冻结 7a6958b，测试 diff SHA-256 为 `ceadef9b19505eacfd60998b1e5f5f47920de088d797a191c05a898483eb0a45`。12 项在 validate 占位方法中失败，退出 1；64122aa 后全部通过，退出 0。独立审查者联合复跑答案验证和工具/Planner 专项 26 项通过。

```powershell
uv run --no-sync pytest -q --disable-socket --allow-unix-socket tests/contract/test_answer_evidence.py --tb=line
```

语义支持度交给 M6 的原始证据复核。SQLite 归属解析、QA 作业持久化和取消优先发布见贯通记录。[数据库与入口](PR-M4-integration.md) · [事件精修](PR-M4.5.md)
