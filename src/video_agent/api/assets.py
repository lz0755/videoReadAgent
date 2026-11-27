"""Bounded multipart ingestion. The filename is only display metadata."""

import asyncio
import uuid
from pathlib import PureWindowsPath

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect

from video_agent.application.ingest import AssetImporter
from video_agent.domain.models import VideoAsset

MULTIPART_ALLOWANCE = 65_536


class UploadParser(MultiPartParser):
    def __init__(self, request: Request, limit: int) -> None:
        super().__init__(
            request.headers, request.stream(), max_files=1, max_fields=1, max_part_size=4096
        )
        self.limit = limit
        self.file_bytes = 0
        self.header_bytes = 0
        self.complete = False

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._count_header(end - start)
        super().on_header_field(data, start, end)

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._count_header(end - start)
        super().on_header_value(data, start, end)

    def _count_header(self, size: int) -> None:
        self.header_bytes += size
        if self.header_bytes > MULTIPART_ALLOWANCE:
            raise HTTPException(413, "MEDIA_LIMIT_EXCEEDED")

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._current_part.file is not None:
            self.file_bytes += end - start
            if self.file_bytes > self.limit:
                raise HTTPException(413, "MEDIA_LIMIT_EXCEEDED")
        super().on_part_data(data, start, end)

    def on_end(self) -> None:
        self.complete = True

    async def parse(self) -> FormData:
        form = await super().parse()
        if not self.complete:
            for staged in self._files_to_close_on_error:
                staged.close()
            raise HTTPException(400, "INCOMPLETE_UPLOAD")
        return form


def upload_router(importer: AssetImporter, limit: int) -> APIRouter:
    router = APIRouter()

    @router.post("/v1/assets", status_code=201)
    async def upload(request: Request) -> VideoAsset:
        if (
            request.headers.get("content-type", "").partition(";")[0].strip().lower()
            != "multipart/form-data"
        ):
            raise HTTPException(415, "MULTIPART_REQUIRED")
        parser = UploadParser(request, limit)
        try:
            async with asyncio.timeout(300):
                form = await parser.parse()
        except TimeoutError as exc:
            raise HTTPException(408, "UPLOAD_TIMEOUT") from exc
        except (ClientDisconnect, MultiPartException, ValueError) as exc:
            raise HTTPException(400, "INCOMPLETE_UPLOAD") from exc

        try:
            file = form.get("file")
            record = form.get("authorization")
            if (
                not isinstance(file, UploadFile)
                or not isinstance(record, str)
                or not record.strip()
            ):
                raise HTTPException(422, "FILE_AND_AUTHORIZATION_REQUIRED")
            name = file.filename or ""
            if (
                not name
                or len(name) > 255
                or PureWindowsPath(name).name != name
                or name in {".", ".."}
                or ":" in name
                or any(ord(c) < 32 for c in name)
            ):
                raise HTTPException(422, "INVALID_FILENAME")
            return await run_in_threadpool(
                importer.register,
                file.file,
                name,
                asset_id="asset_" + uuid.uuid4().hex,
                authorization=record,
            )
        finally:
            await form.close()

    return router
