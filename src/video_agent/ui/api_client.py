"""Server-side Streamlit client for the eight authenticated local API operations."""

import re
from typing import Any, BinaryIO
from urllib.parse import urlsplit

import httpx

MAX_UPLOAD = 67_108_864


class ReviewError(Exception):
    """A display-safe message for the local reviewer."""


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ReviewError("请输入有效的本地记录 ID。")
    return value


class ReviewClient:
    def __init__(self, url: str, token: str) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or not token
        ):
            raise ReviewError("请配置本机 API 地址和请求令牌。")
        self.http = httpx.Client(
            base_url=url,
            headers={"Authorization": "Bearer " + token},
            trust_env=False,
            follow_redirects=False,
            timeout=90,
        )

    def close(self) -> None:
        self.http.close()

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self.http.request(method, path, **kwargs)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ReviewError(f"本机 API 拒绝了请求（HTTP {exc.response.status_code}）。") from None
        except httpx.HTTPError:
            raise ReviewError("无法连接本机 API。请检查服务状态。") from None
        return response

    def upload(self, file: BinaryIO, name: str, size: int, authorization: str) -> str:
        if size > MAX_UPLOAD:
            raise ReviewError("超过 64 MiB，请通过 CLI 导入。")
        response = self.request(
            "POST",
            "/v1/assets",
            files={"file": (name, file)},
            data={"authorization": authorization},
        )
        return str(response.json()["asset_id"])

    def submit(self, path: str, payload: dict[str, str], key: str) -> dict[str, Any]:
        result: dict[str, Any] = self.request(
            "POST", path, json=payload, headers={"Idempotency-Key": key}
        ).json()
        return result

    def evidence(self, analysis: str, eid: str, representation: str) -> httpx.Response:
        return self.request(
            "GET",
            "/v1/evidence/" + identifier(eid),
            params={"analysis_id": identifier(analysis), "representation": representation},
        )
