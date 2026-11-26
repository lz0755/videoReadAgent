import pytest

from eval.d1_metrics import event_hits, quality_gates


def test_both_endpoints_label_overlap_and_false_positives() -> None:
    reference = ("open", 5000, 10000)
    assert event_hits(reference, [("wrong", 5000, 10000), ("open", 7000, 12000)]) == (1, 1)
    assert event_hits(reference, [("open", 5000, 12001)]) == (0, 0)
    assert event_hits(reference, [("open", 11000, 11100)]) == (0, 1)
    assert event_hits(reference, []) == (0, 0)


def test_all_six_fixed_release_denominators_and_empty_facts() -> None:
    values = dict(
        correct=20,
        recall_at_five=22,
        appropriate_abstentions=7,
        localized=17,
        supported_claims=9,
        claims=10,
        valid_citations=1,
        citations=1,
    )
    passing = quality_gates(**values)
    assert len(passing) == 6 and all(passing.values())
    for key in (
        "correct",
        "recall_at_five",
        "appropriate_abstentions",
        "localized",
        "supported_claims",
        "valid_citations",
    ):
        assert sum(quality_gates(**(values | {key: values[key] - 1})).values()) == 5
    assert (
        sum(
            quality_gates(
                **(
                    values
                    | {"claims": 0, "supported_claims": 0, "citations": 0, "valid_citations": 0}
                )
            ).values()
        )
        == 4
    )
    with pytest.raises(ValueError):
        quality_gates(**(values | {"correct": 25}))
