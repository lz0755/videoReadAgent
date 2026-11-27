"""Authorize local requests before consuming any body bytes."""

import re
from hmac import compare_digest

from fastapi import HTTPException
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from video_agent.api.assets import MULTIPART_ALLOWANCE


class LocalRequests:
    def __init__(self, app: ASGIApp, *, token: str, file_limit: int) -> None:
        self.app = app
        self.expected = ("Bearer " + token).encode()
        self.file_limit = file_limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        hosts = headers.getlist("host")
        host = hosts[0].lower() if len(hosts) == 1 else ""
        origins = headers.getlist("origin")
        if not re.fullmatch(r"(?:127\.0\.0\.1|localhost|\[::1\])(?::[0-9]{1,5})?", host) or (
            origins and origins != [f"{scope['scheme']}://{host}"]
        ):
            await JSONResponse({"detail": "LOCAL_REQUEST_REQUIRED"}, 403)(scope, receive, send)
            return
        tokens = headers.getlist("authorization")
        if len(tokens) != 1 or not compare_digest(tokens[0].encode("latin1"), self.expected):
            await JSONResponse({"detail": "UNAUTHORIZED"}, 401)(scope, receive, send)
            return
        limit = MULTIPART_ALLOWANCE
        if scope["path"] == "/v1/assets":
            limit += self.file_limit
        received = 0

        async def bounded_receive() -> Message:
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > limit:
                raise HTTPException(413, "MEDIA_LIMIT_EXCEEDED")
            return message

        await self.app(scope, bounded_receive, send)
