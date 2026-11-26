"""Identity and port for committed observation windows."""

from collections.abc import Mapping
from typing import Protocol

from video_agent.application.observations import WindowResult
from video_agent.application.prompt_registry import canonical_json_sha256


def window_key(
    *,
    content_sha256: str,
    model: str,
    revision: str,
    prompt_bundle: str,
    schema: str,
    sampling: Mapping[str, object],
    crop: str,
    input_digest: str,
) -> str:
    return canonical_json_sha256(
        [
            "observe-v1",
            content_sha256,
            model,
            revision,
            prompt_bundle,
            schema,
            dict(sampling),
            crop,
            input_digest,
        ]
    )


class WindowStages(Protocol):
    def load(self, asset_id: str, analysis_id: str, item: str, key: str) -> WindowResult | None: ...
    def commit(self, item: str, key: str, result: WindowResult) -> None: ...
