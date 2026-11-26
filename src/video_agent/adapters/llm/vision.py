"""Validate actual image payloads before reserving a vision request."""

import io

from PIL import Image
from pydantic import Field, ValidationError

from video_agent.adapters.llm.openai_slice import _Observation
from video_agent.adapters.media.roi import CatalogImages
from video_agent.domain.errors import ErrorCode, ProviderError
from video_agent.domain.models import Observation, RegionId, StrictModel
from video_agent.domain.ports import VisionProvider, VisionRequest, VisionResponse


class _WireObservation(_Observation):
    region_hint: RegionId | None = None


class _WireResponse(StrictModel):
    observations: list[_WireObservation] = Field(min_length=1)


class _FullResponse(StrictModel):
    observations: list[Observation] = Field(min_length=1)


class StrictVisionProvider:
    def __init__(self, provider: VisionProvider) -> None:
        self.provider_name = provider.provider_name
        self.model_id = provider.model_id
        self._provider = provider

    async def observe(self, request: VisionRequest) -> VisionResponse:
        result = await self._provider.observe(request)
        try:
            if self.provider_name == "fake":
                parsed = _FullResponse.model_validate_json(result.raw_text)
                if parsed.observations != result.observations:
                    raise ValueError("candidate differs from response")
            else:
                wire = _WireResponse.model_validate_json(result.raw_text)
                for item, obs in zip(wire.observations, result.observations, strict=True):
                    if (
                        item.kind,
                        item.text,
                        item.evidence_ids,
                        item.start_ms,
                        item.end_ms,
                        item.region_hint,
                    ) != (
                        obs.kind,
                        obs.text,
                        obs.evidence_ids,
                        obs.time_range.start_ms,
                        obs.time_range.end_ms,
                        obs.region_hint,
                    ):
                        raise ValueError("candidate differs from response")
        except (ValidationError, ValueError) as exc:
            raise ProviderError(
                ErrorCode.SCHEMA_VALIDATION_FAILED, "invalid vision response schema"
            ) from exc
        return result


def check_images(catalog: CatalogImages, request: VisionRequest, *, max_images: int) -> None:
    if len(request.images) > max_images:
        raise ProviderError(ErrorCode.BUDGET_EXCEEDED, "too many image parts")
    encoded_bytes = len(request.prompt_text.encode("utf-8"))
    max_side = 4096 if len(request.images) >= 15 else 8192
    for image in request.images:
        raw = catalog.read(request.asset_id, image.artifact_id)
        with Image.open(io.BytesIO(raw)) as pixels:
            if (
                pixels.size != (image.width, image.height)
                or max(pixels.size) > max_side
                or Image.MIME.get(pixels.format or "") != image.mime_type
            ):
                raise ProviderError(
                    ErrorCode.CAPABILITY_MISMATCH, "image metadata or dimensions invalid"
                )
            pixels.verify()
        encoded_bytes += 4 * ((len(raw) + 2) // 3)
    if encoded_bytes > 48 * 1024**2:
        raise ProviderError(ErrorCode.CAPABILITY_MISMATCH, "encoded request exceeds byte limit")
