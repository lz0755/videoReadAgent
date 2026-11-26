"""Validate actual image payloads before reserving a vision request."""

import io

from PIL import Image

from video_agent.adapters.media.roi import CatalogImages
from video_agent.domain.errors import ErrorCode, ProviderError
from video_agent.domain.ports import VisionRequest


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
