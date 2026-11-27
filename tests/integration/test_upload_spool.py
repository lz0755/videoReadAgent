import asyncio

import pytest
from fastapi import HTTPException, Request
from starlette.types import Message

from video_agent.api.assets import UploadParser

pytestmark = pytest.mark.allow_hosts(["127.0.0.1", "::1"])


def test_eof_inside_spooled_file_closes_its_real_disk_handle() -> None:
    body = (
        b'--b\r\nContent-Disposition: form-data; name="file"; filename="clip.mp4"\r\n\r\n'
        + bytes(1048577)
    )

    async def receive() -> Message:
        return {"type": "http.request", "body": body, "more_body": False}

    async def check() -> None:
        parser = UploadParser(
            Request(
                {
                    "type": "http",
                    "headers": [(b"content-type", b"multipart/form-data; boundary=b")],
                },
                receive,
            ),
            len(body),
        )
        with pytest.raises(HTTPException) as failure:
            await parser.parse()
        assert failure.value.status_code == 400
        unfinished = parser._current_part.file
        assert unfinished is not None and unfinished.file.closed

    asyncio.run(check())
