"""Object storage (IS7): boto3 against the SeaweedFS S3 gateway.

* Writes and reads use ``S3_ENDPOINT`` (the internal compose hostname).
* Presigned URLs are signed for ``S3_PUBLIC_ENDPOINT`` so links work outside compose.
* ``MemoryStorage`` has the same interface and is used by unit tests.
* ``delete``, ``stat`` and ``open_stream`` back the signed download route
  ``GET /api/v1/files/{key}`` (see ``app.storage.links``), so links work through the one API port.
boto3 is synchronous, so the async wrappers run it in a worker thread.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from app.config import Settings
from app.errors import ErrorCode, IbxError


@dataclass(frozen=True)
class ObjectInfo:
    size: int
    content_type: str


class Storage(Protocol):
    async def ensure_bucket(self) -> None: ...
    async def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str: ...
    async def get_bytes(self, key: str) -> bytes: ...
    async def presign(self, key: str, expires_s: int | None = None) -> str: ...
    async def ping(self) -> bool: ...
    async def delete(self, key: str) -> None: ...
    async def stat(self, key: str) -> ObjectInfo | None: ...
    async def open_stream(self, key: str, chunk_size: int = 65536) -> tuple[ObjectInfo, AsyncIterator[bytes]]: ...


STREAM_CHUNK = 65536


class S3Storage:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.bucket = settings.S3_BUCKET
        self._client = self._make_client(settings.S3_ENDPOINT)
        self._public = self._make_client(settings.S3_PUBLIC_ENDPOINT)
        self._bucket_ready = False

    def _make_client(self, endpoint: str):  # noqa: ANN202 - boto3 client type is dynamic
        return boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=self.settings.S3_ACCESS_KEY.get_secret_value(),
            aws_secret_access_key=self.settings.S3_SECRET_KEY.get_secret_value(),
            region_name=self.settings.S3_REGION,
            config=BotoConfig(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                connect_timeout=3,
                read_timeout=10,
                retries={"max_attempts": 2},
            ),
        )

    async def _run(self, fn, *args, **kwargs):  # noqa: ANN001, ANN202
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except (BotoCoreError, ClientError) as exc:
            raise IbxError(ErrorCode.STORAGE_UNAVAILABLE, f"Object storage error: {type(exc).__name__}") from exc

    async def ensure_bucket(self) -> None:
        if self._bucket_ready:
            return

        def _ensure() -> None:
            try:
                self._client.head_bucket(Bucket=self.bucket)
            except ClientError:
                self._client.create_bucket(Bucket=self.bucket)

        await self._run(_ensure)
        self._bucket_ready = True

    async def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        await self.ensure_bucket()
        await self._run(self._client.put_object, Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
        return key

    async def get_bytes(self, key: str) -> bytes:
        response = await self._run(self._client.get_object, Bucket=self.bucket, Key=key)
        return await asyncio.to_thread(response["Body"].read)

    async def presign(self, key: str, expires_s: int | None = None) -> str:
        expires = expires_s or self.settings.S3_PRESIGN_EXPIRY_S
        return await self._run(
            self._public.generate_presigned_url,
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires,
        )

    async def delete(self, key: str) -> None:
        await self._run(self._client.delete_object, Bucket=self.bucket, Key=key)

    async def stat(self, key: str) -> ObjectInfo | None:
        def _head() -> ObjectInfo | None:
            try:
                head = self._client.head_object(Bucket=self.bucket, Key=key)
            except ClientError as exc:
                if str(exc.response.get("Error", {}).get("Code")) in ("404", "NoSuchKey", "NotFound"):
                    return None
                raise
            return ObjectInfo(int(head.get("ContentLength") or 0), head.get("ContentType") or "application/octet-stream")

        return await self._run(_head)

    async def open_stream(self, key: str, chunk_size: int = STREAM_CHUNK) -> tuple[ObjectInfo, AsyncIterator[bytes]]:
        """Start a GET and return (info, async chunk iterator). Raises NOT_FOUND for a missing key."""

        def _get():  # noqa: ANN202
            try:
                return self._client.get_object(Bucket=self.bucket, Key=key)
            except ClientError as exc:
                if str(exc.response.get("Error", {}).get("Code")) in ("404", "NoSuchKey", "NotFound"):
                    return None
                raise

        response = await self._run(_get)
        if response is None:
            raise IbxError(ErrorCode.NOT_FOUND, "No such file.")
        info = ObjectInfo(int(response.get("ContentLength") or 0), response.get("ContentType") or "application/octet-stream")
        body = response["Body"]

        async def chunks() -> AsyncIterator[bytes]:
            try:
                while True:
                    chunk = await asyncio.to_thread(body.read, chunk_size)
                    if not chunk:
                        break
                    yield chunk
            finally:
                await asyncio.to_thread(body.close)

        return info, chunks()

    async def ping(self) -> bool:
        try:
            await self.ensure_bucket()
            await self._run(self._client.head_bucket, Bucket=self.bucket)
            return True
        except IbxError:
            self._bucket_ready = False
            return False


class MemoryStorage:
    """In-process storage for tests. Presigned URLs are fake ``memory://`` links."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self._lock = threading.Lock()

    async def ensure_bucket(self) -> None:
        return None

    async def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        with self._lock:
            self.objects[key] = (data, content_type)
        return key

    async def get_bytes(self, key: str) -> bytes:
        with self._lock:
            if key not in self.objects:
                raise IbxError(ErrorCode.NOT_FOUND, f"No object {key}")
            return self.objects[key][0]

    async def presign(self, key: str, expires_s: int | None = None) -> str:
        return f"memory://ibx/{key}?expires={expires_s or 3600}"

    async def ping(self) -> bool:
        return True

    async def delete(self, key: str) -> None:
        with self._lock:
            self.objects.pop(key, None)

    async def stat(self, key: str) -> ObjectInfo | None:
        with self._lock:
            if key not in self.objects:
                return None
            data, content_type = self.objects[key]
        return ObjectInfo(len(data), content_type)

    async def open_stream(self, key: str, chunk_size: int = STREAM_CHUNK) -> tuple[ObjectInfo, AsyncIterator[bytes]]:
        with self._lock:
            if key not in self.objects:
                raise IbxError(ErrorCode.NOT_FOUND, "No such file.")
            data, content_type = self.objects[key]

        async def chunks() -> AsyncIterator[bytes]:
            for start in range(0, len(data), chunk_size):
                yield data[start : start + chunk_size]

        return ObjectInfo(len(data), content_type), chunks()
