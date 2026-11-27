"""M4 text-only PlannerProvider for the configured Chat Completions endpoint."""

from video_agent.domain.ports import PlannerRequest, PlannerResponse


class QAPlannerProvider:
    def __init__(
        self,
        *,
        provider_name: str,
        model_id: str,
        api_key_env: str,
        cloud_upload_enabled: bool = False,
    ) -> None:
        self.provider_name = provider_name
        self.model_id = model_id
        self._key_env = api_key_env
        self._consent = cloud_upload_enabled

    async def decide(self, request: PlannerRequest) -> PlannerResponse:
        raise NotImplementedError
