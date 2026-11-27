"""Local asynchronous commands and authenticated evidence representations."""

from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query
from starlette.responses import FileResponse, Response

from video_agent.adapters.storage.jobs import SQLiteJobs
from video_agent.application.jobs import AnalysisRequest, QuestionRequest

Key = Annotated[str | None, Header(alias="Idempotency-Key", min_length=1, max_length=128)]


def job_router(jobs: SQLiteJobs) -> APIRouter:
    router = APIRouter()

    @router.post("/v1/assets/{asset_id}/analyses", status_code=202)
    def submit_analysis(asset_id: str, request: AnalysisRequest, key: Key = None) -> dict[str, Any]:
        return jobs.submit_analysis(asset_id, request, key)

    @router.get("/v1/jobs/{job_id}")
    def job(job_id: str) -> dict[str, Any]:
        return jobs.job(job_id)

    @router.post("/v1/jobs/{job_id}/cancel")
    def cancel(job_id: str) -> dict[str, Any]:
        return jobs.cancel(job_id)

    @router.get("/v1/analyses/{analysis_id}")
    def analysis(
        analysis_id: str,
        offset: Annotated[int, Query(ge=0, le=2**31 - 1)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        start_ms: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
        end_ms: Annotated[int | None, Query(ge=1, le=2**63 - 1)] = None,
    ) -> dict[str, Any]:
        if end_ms is not None and start_ms >= end_ms:
            raise HTTPException(422, "INVALID_TIME_RANGE")
        return jobs.analysis(analysis_id, offset, limit, start_ms, end_ms)

    @router.post("/v1/analyses/{analysis_id}/questions", status_code=202)
    def submit_question(
        analysis_id: str, request: QuestionRequest, key: Key = None
    ) -> dict[str, Any]:
        return jobs.submit_question(analysis_id, request, key)

    @router.get("/v1/questions/{question_id}")
    def question(question_id: str) -> dict[str, Any]:
        return jobs.question(question_id)

    @router.get("/v1/evidence/{evidence_id}", response_model=None)
    def evidence(
        evidence_id: str,
        analysis_id: str,
        representation: Literal["meta", "frame", "source_video"] = "meta",
    ) -> dict[str, Any] | Response:
        result = jobs.evidence(analysis_id, evidence_id, representation)
        if not isinstance(result, Path):
            return result
        media_type = (
            "image/jpeg"
            if representation == "frame"
            else {
                ".mp4": "video/mp4",
                ".mov": "video/quicktime",
                ".webm": "video/webm",
                ".mkv": "video/x-matroska",
            }.get(result.suffix.lower(), "application/octet-stream")
        )
        return FileResponse(
            result,
            media_type=media_type,
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )

    return router
