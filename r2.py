"""Shared R2 storage for the media tools.

xAI output URLs expire, so every tool that produces media copies it here and
returns a permanent public URL. Keys are grouped per job so one run's outputs
sit together: {key_prefix or "jobs"}/{job_id}/{tool}-{n}.{ext}, or
adhoc/{uuid}/{tool}-{n}.{ext} when no job_id is given.
"""

import asyncio
import mimetypes
import os
import time
import uuid

import boto3
import httpx
from botocore.config import Config

ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID")
ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID")
SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
BUCKET = os.getenv("R2_BUCKET")
PUBLIC_BASE = (os.getenv("R2_PUBLIC_BASE") or "").rstrip("/")

MAX_INPUT_BYTES = 200 * 1024 * 1024

CONTENT_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "mp4": "video/mp4",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "aac": "audio/aac",
    "opus": "audio/opus",
    "srt": "text/plain; charset=utf-8",
    "ass": "text/plain; charset=utf-8",
    "json": "application/json",
}

_S3 = None


def configured() -> bool:
    return all([ACCOUNT_ID, ACCESS_KEY_ID, SECRET_ACCESS_KEY, BUCKET, PUBLIC_BASE])


def _s3():
    global _S3
    if _S3 is None:
        _S3 = boto3.client(
            "s3",
            endpoint_url=f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com",
            region_name="auto",
            aws_access_key_id=ACCESS_KEY_ID,
            aws_secret_access_key=SECRET_ACCESS_KEY,
            config=Config(retries={"max_attempts": 3, "mode": "standard"}),
        )
    return _S3


def content_type(ext: str) -> str:
    ext = ext.lower().lstrip(".")
    return CONTENT_TYPES.get(ext) or mimetypes.guess_type(f"x.{ext}")[0] or "application/octet-stream"


def public_url(key: str) -> str:
    return f"{PUBLIC_BASE}/{key.lstrip('/')}"


def _next_index(folder: str, tool: str) -> int:
    """The next unused `-n` suffix for `{folder}/{tool}-*`, based on what's already in R2.

    n used to default to 0 on every call, so repeated calls for the same
    job_id/tool (e.g. four `compose_cover` calls in one job) all wrote
    `{tool}-0.{ext}` and silently overwrote each other. Listing what's already
    there and continuing past it keeps repeated calls from colliding.

    This is check-then-act, not atomic: two calls for the same
    {folder}/{tool} racing at the same instant can still both list the same
    existing set and pick the same index. R2/S3 has no atomic counter or
    conditional-put here to close that window cheaply, so this covers the
    real failure mode (sequential calls within a job) and not true concurrency.
    """
    indices = []
    for obj in list_prefix(f"{folder}/{tool}-"):
        stem = obj["key"].rsplit("/", 1)[-1][len(tool) + 1:].split(".", 1)[0]
        if stem.isdigit():
            indices.append(int(stem))
    return max(indices, default=-1) + 1


def media_key(tool: str, ext: str, n: int | None = None, job_id: str | None = None, key_prefix: str | None = None) -> str:
    ext = ext.lower().lstrip(".")
    if job_id:
        folder = f"{(key_prefix or 'jobs').strip('/')}/{job_id}"
        if n is None:
            n = _next_index(folder, tool) if configured() else 0
        return f"{folder}/{tool}-{n}.{ext}"
    return f"{(key_prefix or 'adhoc').strip('/')}/{uuid.uuid4()}/{tool}-{n or 0}.{ext}"


def _put_bytes_sync(data: bytes, key: str, ctype: str | None = None) -> str:
    """Upload bytes and return the permanent public URL. Retries a few times."""
    if not configured():
        raise RuntimeError("R2 is not configured (need R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET, R2_PUBLIC_BASE)")
    ctype = ctype or content_type(key.rsplit(".", 1)[-1])
    last = None
    for attempt in range(3):
        try:
            _s3().put_object(Bucket=BUCKET, Key=key, Body=data, ContentType=ctype)
            return public_url(key)
        except Exception as exc:
            last = exc
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"R2 upload failed after 3 attempts: {last}")


async def put_bytes(data: bytes, key: str, ctype: str | None = None) -> str:
    """Upload bytes and return the permanent public URL. Retries a few times."""
    return await asyncio.to_thread(_put_bytes_sync, data, key, ctype)


async def put_from_url(source_url: str, key: str, ctype: str | None = None) -> tuple[str, int]:
    """Copy a remote file into R2. Returns (public_url, bytes)."""
    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
        async with client.stream("GET", source_url) as response:
            response.raise_for_status()
            declared = response.headers.get("content-length")
            if declared and int(declared) > MAX_INPUT_BYTES:
                raise ValueError(f"source is {int(declared)} bytes, over the {MAX_INPUT_BYTES} byte limit")
            chunks = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > MAX_INPUT_BYTES:
                    raise ValueError(f"source exceeds the {MAX_INPUT_BYTES} byte limit")
                chunks.append(chunk)
            data = b"".join(chunks)
            ctype = ctype or response.headers.get("content-type")
    return await put_bytes(data, key, ctype), total


async def fetch(source_url: str) -> bytes:
    """Download a remote file into memory, respecting the size cap."""
    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
        response = await client.get(source_url)
        response.raise_for_status()
        if len(response.content) > MAX_INPUT_BYTES:
            raise ValueError(f"source exceeds the {MAX_INPUT_BYTES} byte limit")
        return response.content


def presigned_put(key: str, ctype: str | None = None, expires: int = 3600) -> str:
    """A signed PUT URL, so xAI can upload a result straight into the bucket.

    The video API accepts one as `output.upload_url`, which saves downloading
    the result only to upload it again.
    """
    if not configured():
        raise RuntimeError("R2 is not configured")
    return _s3().generate_presigned_url(
        "put_object",
        Params={"Bucket": BUCKET, "Key": key, "ContentType": ctype or content_type(key.rsplit(".", 1)[-1])},
        ExpiresIn=expires,
    )


def head(key: str) -> dict | None:
    """Object metadata, or None when it is not there."""
    try:
        meta = _s3().head_object(Bucket=BUCKET, Key=key.lstrip("/"))
    except Exception:
        return None
    return {"bytes": meta["ContentLength"], "content_type": meta.get("ContentType")}


def list_prefix(prefix: str, limit: int = 200) -> list[dict]:
    out = []
    paginator = _s3().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix.lstrip("/")):
        for obj in page.get("Contents", []):
            out.append({
                "key": obj["Key"],
                "url": public_url(obj["Key"]),
                "bytes": obj["Size"],
                "last_modified": obj["LastModified"].isoformat(),
            })
            if len(out) >= limit:
                return out
    return out


def delete_key(key: str) -> None:
    _s3().delete_object(Bucket=BUCKET, Key=key.lstrip("/"))


def cost_usd(usage) -> float | None:
    """xAI reports cost in ticks of 1e-10 USD."""
    ticks = None
    if isinstance(usage, dict):
        ticks = usage.get("cost_in_usd_ticks")
    else:
        ticks = getattr(usage, "cost_in_usd_ticks", None)
    return ticks / 1e10 if isinstance(ticks, (int, float)) else None


def result(
    urls: list[str],
    r2_keys: list[str],
    markdown: str,
    duration: float | None = None,
    width: int | None = None,
    height: int | None = None,
    nbytes: int | None = None,
    cost: float | None = None,
    model: str | None = None,
    request_id: str | None = None,
    moderated: bool = False,
    temp_x_url: str | None = None,
    temp_x_urls: list[str] | None = None,
    **extra,
) -> dict:
    """The standard envelope every media tool returns.

    `urls` / `url` are permanent R2 public URLs. Any xAI/X temp URL belongs
    in `temp_x_url` (and `temp_x_urls` when there are several), never in `url`.
    """
    from media import rewrite_media_fields

    originals: list[str] = []
    if temp_x_urls:
        originals.extend(temp_x_urls)
    elif temp_x_url:
        originals.append(temp_x_url)
    fields = rewrite_media_fields(r2_urls=urls, original_urls=originals)
    payload = {
        **fields,
        "r2_keys": r2_keys,
        "duration": duration,
        "width": width,
        "height": height,
        "bytes": nbytes,
        "cost_usd": cost,
        "model": model,
        "request_id": request_id,
        "moderated": moderated,
        "markdown": markdown,
    }
    payload.update(extra)
    return payload
