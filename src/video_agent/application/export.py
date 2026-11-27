"""Render the same evidence and usage snapshot as a local Markdown report."""

import json
from typing import Any


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# 视频分析核对记录",
        "",
        f"分析 ID `{report['analysis_id']}`",
        f"状态 `{report['status']}` · 索引 `{report['index_state']}`",
        f"Schema `{report['schema_version']}`",
        "",
        "## 来源与版本",
        "",
        f"资产 `{report['source']['asset_id']}`",
        f"原视频 SHA-256 `{report['source']['sha256']}`",
        f"配置 `{report['versions']['config_hash']}`",
        f"Prompt bundle `{report['versions']['prompt_bundle_hash']}`",
        "",
        "## 覆盖",
        "",
    ]

    def literal(value: str) -> None:
        lines.extend("    " + line for line in value.splitlines())
        lines.append("")

    literal(json.dumps(report["coverage"], ensure_ascii=False))
    lines.extend(["## 时间轴", ""])
    for item in report["timeline"]:
        lines.append(f"{item['start_ms']}–{item['end_ms']} ms · {item['kind']}")
        lines.append("")
        literal(item["text"])
        literal(" ".join(item["evidence_ids"]))
    lines.extend(["## 原始证据", ""])
    for eid, value in report["evidence"].items():
        lines.append(f"- `{eid}`")
        lines.append("")
        literal(json.dumps(value, ensure_ascii=False))
    lines.extend(["## 问题与回答", ""])
    for question in report["questions"]:
        lines.extend([f"### {question['question_id']}", ""])
        literal(question["query_text"])
        if answer := question.get("answer"):
            lines.append(f"回答状态 `{answer['status']}`")
            lines.append("")
            for claim in answer["claims"]:
                citations = ", ".join(claim["evidence_ids"])
                literal(f"{claim['support_kind']} · {claim['text']} [{citations}]")
                for limitation in claim["limitations"]:
                    literal(limitation)
            if event := answer["localization"]:
                literal(event["label"])
                for caption, side in [("开始", "start_bracket"), ("结束", "end_bracket")]:
                    edge = event[side]
                    literal(
                        f"{caption} [{edge['lower_ms']}, {edge['upper_ms']}] ms"
                        f" · {edge['before_evidence_id']} / {edge['after_evidence_id']}"
                    )
            for gap in answer["gaps"]:
                literal(gap)
        else:
            lines.append(f"作业状态 `{question['status']}`")
        literal(json.dumps(question["usage"], ensure_ascii=False))
    lines.extend(["## 分析用量", ""])
    literal(json.dumps(report["usage"], ensure_ascii=False))
    return "\n".join(lines) + "\n"
