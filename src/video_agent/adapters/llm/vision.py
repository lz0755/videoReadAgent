"""Validate actual image payloads before reserving a vision request."""

from video_agent.adapters.media.roi import CatalogImages
from video_agent.domain.ports import VisionRequest


def check_images(catalog: CatalogImages, request: VisionRequest, *, max_images: int) -> None:
    raise NotImplementedError
