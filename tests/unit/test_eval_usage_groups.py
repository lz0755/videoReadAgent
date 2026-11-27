from eval.report import latency


def test_request_groups_keep_measured_usage_and_unknown_costs_separate() -> None:
    base = {
        "provider_attempts": 2,
        "submitted_image_parts": 3,
        "reported_input_tokens": 100,
        "reported_output_tokens": 10,
        "missing_token_records": 1,
        "confirmed_cost_usd": 0.1,
        "estimated_cost_usd": 0.2,
        "pending_reservations": 0,
        "unknown_cost_records": 1,
        "tool_calls": 2,
        "expensive_tool_calls": 1,
    }
    rows = {
        "q": {"mode": "qa", "elapsed_s": 2.0, "usage": base, "expensive_tool_calls": 1},
        "e": {"mode": "event_localization", "elapsed_s": 1.0},
    }
    groups = latency(rows)
    visual = groups["qa_with_review"]
    assert visual["n"] == 1 and visual["usage"]["provider_attempts"] == 2
    assert visual["usage"]["unknown_cost_usd"] is None
    event = groups["event_localization"]
    assert event["n"] == 1 and event["jobs_without_usage"] == 1 and event["usage"] is None
    assert groups["qa_without_review"]["n"] == 0
