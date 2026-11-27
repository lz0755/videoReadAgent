"""Parse proposals from the text planner at the server authority boundary."""

from pydantic import Field, StrictInt

from video_agent.application.evidence_service import EvidenceService, FrameSelection, SearchArgs
from video_agent.domain.config import QaConfig
from video_agent.domain.errors import DomainValidationError, ErrorCode
from video_agent.domain.models import RegionId, StrictModel, TimeRangeMs
from video_agent.domain.tokenizer import validate_search_query


class InspectArgs(TimeRangeMs):
    region_id: RegionId = "full"


class CompareArgs(StrictModel):
    first_ms: StrictInt = Field(ge=0)
    second_ms: StrictInt = Field(ge=0)
    region_id: RegionId = "full"


def tool_schema() -> dict[str, object]:
    return {
        "search_evidence": SearchArgs.model_json_schema(),
        "inspect_clip": InspectArgs.model_json_schema(),
        "compare_frames": CompareArgs.model_json_schema(),
    }


def validate_tool(
    name: str, arguments: dict[str, object], service: EvidenceService, config: QaConfig
) -> SearchArgs | FrameSelection:
    try:
        if name == "search_evidence":
            search = SearchArgs.model_validate(arguments)
            validate_search_query(search.query)
            if search.time_range:
                search.time_range.validate_within(service.duration_ms)
            return search
        if name == "inspect_clip":
            clip = InspectArgs.model_validate(arguments)
            window = TimeRangeMs(start_ms=clip.start_ms, end_ms=clip.end_ms)
            window.validate_within(service.duration_ms)
            if window.end_ms - window.start_ms > min(8000, config.max_clip_ms):
                raise ValueError("clip exceeds QA limit")
            stride = 1000 // min(2, config.inspect_fps)
            points = tuple(range(window.start_ms, window.end_ms, stride))
            region = clip.region_id
        elif name == "compare_frames":
            pair = CompareArgs.model_validate(arguments)
            points = (pair.first_ms, pair.second_ms)
            if points[0] == points[1] or max(points) >= service.duration_ms:
                raise ValueError("two distinct in-range points required")
            window = TimeRangeMs(
                start_ms=min(points), end_ms=min(service.duration_ms, max(points) + 500)
            )
            region = pair.region_id
        else:
            raise ValueError("unknown tool")
        if len(points) > min(16, config.max_images_per_request):
            raise DomainValidationError(ErrorCode.BUDGET_EXCEEDED, "too many requested frames")
        if region != "full" and not service.allow_region(region, window):
            raise ValueError("region lacks a bound hint for this window")
        return FrameSelection(window, points, region, name == "compare_frames")
    except ValueError as exc:
        raise DomainValidationError(
            ErrorCode.SCHEMA_VALIDATION_FAILED, "invalid tool arguments"
        ) from exc
