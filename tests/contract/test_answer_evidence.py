import pytest

from tests.contract.test_vision_provider import candidate, inputs_for
from video_agent.agent.answer_validator import AnswerValidator
from video_agent.application.evidence_service import ToolResult
from video_agent.domain.errors import VideoAgentError
from video_agent.domain.models import Answer, Claim, FrameEvidence, Question, TranscriptSegment


def question() -> Question:
    return Question(
        question_id="q", asset_id="asset", analysis_id="a", query_text="what?", created_at_utc="now"
    )


def proposed() -> Answer:
    return Answer(
        answer_id="answer",
        question_id="q",
        asset_id="asset",
        analysis_id="a",
        status="answered",
        claims=[Claim(text="label", support_kind="observed", evidence_ids=["frame"])],
        gaps=[],
        trace_id="trace",
    )


@pytest.mark.parametrize(
    "fault",
    [
        "asset",
        "analysis",
        "question",
        "invented",
        "unseen",
        "other-analysis",
        "dangling",
        "wrong-source",
        "inferred",
        "heard",
        "wrong-asset-payload",
    ],
)
def test_rejects_fabricated_foreign_and_misattributed_citations(fault: str) -> None:
    context = ToolResult(observations=[candidate()])
    frame = inputs_for().frames[0]
    source: FrameEvidence | TranscriptSegment | None = frame
    answer = proposed()
    if fault in {"asset", "analysis", "question"}:
        answer = answer.model_copy(update={fault + "_id": "other"})
    elif fault == "invented":
        answer = answer.model_copy(
            update={"claims": [answer.claims[0].model_copy(update={"evidence_ids": ["invented"]})]}
        )
    elif fault == "unseen":
        context = ToolResult()
    elif fault == "other-analysis":
        context = ToolResult(
            observations=[candidate().model_copy(update={"analysis_id": "previous"})]
        )
    elif fault == "dangling":
        source = None
    elif fault == "wrong-source":
        source = inputs_for().transcripts[0].model_copy(update={"evidence_id": "frame"})
    elif fault == "wrong-asset-payload":
        source = frame.model_copy(update={"asset_id": "other"})
    else:
        context = ToolResult(observations=[candidate().model_copy(update={"kind": fault})])
    with pytest.raises(VideoAgentError):
        AnswerValidator(question(), 100, lambda _: source).validate(answer, context)


def test_valid_observed_heard_inferred_and_honest_gap() -> None:
    inputs = inputs_for()
    speech = inputs.transcripts[0]
    sources: dict[str, FrameEvidence | TranscriptSegment] = {
        "frame": inputs.frames[0],
        "speech": speech,
    }
    validator = AnswerValidator(question(), 100, sources.get)
    ctx = ToolResult(
        observations=[
            candidate(),
            candidate().model_copy(
                update={
                    "observation_id": "o2",
                    "kind": "heard",
                    "evidence_ids": ["speech"],
                    "text": speech.text,
                }
            ),
        ]
    )
    props = proposed()
    validator.validate(props, ctx)
    validator.validate(
        props.model_copy(
            update={
                "claims": [
                    Claim(text="words", support_kind="heard", evidence_ids=["speech"]),
                    Claim(
                        text="inference", support_kind="inferred", evidence_ids=["frame", "speech"]
                    ),
                ]
            }
        ),
        ctx,
    )
    gap = Answer(
        answer_id="a",
        question_id="q",
        asset_id="asset",
        analysis_id="a",
        trace_id="trace",
        status="not_answerable",
        claims=[],
        gaps=["No readable frame in this interval."],
    )
    validator.validate(gap, ToolResult())
