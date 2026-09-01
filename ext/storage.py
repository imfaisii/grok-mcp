"""Storage and media-inspection tools backed by the shared R2 layer.

`r2_put`/`r2_list`/`r2_delete` let the client manage user-supplied reference
media (product photos, character identity shots) in the same bucket the
generation tools write to. `inspect_media` runs an automated QC pass over a
generated image or video with Grok vision, using xAI structured outputs so
the result is always valid JSON against a fixed rubric schema.
"""

import base64
from pathlib import Path
from typing import List, Literal, Optional
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field
from xai_sdk import Client
from xai_sdk.chat import image, user

import r2
from src.utils import XAI_API_KEY

IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif"}
VIDEO_EXTS = {"mp4", "mov", "webm", "avi", "mkv", "m4v"}


def _validate_key(key: str) -> None:
    """Reject absolute paths, `..` traversal, empty segments, and overlong keys."""
    if not key or key.startswith("/") or len(key) > 512:
        raise ValueError(f"invalid key: {key!r}")
    for part in key.split("/"):
        if part in ("", ".", ".."):
            raise ValueError(f"invalid key: {key!r}")


async def _detect_kind(url: str) -> Literal["image", "video"]:
    """Decide image vs video from the URL extension, falling back to Content-Type."""
    ext = Path(urlparse(url).path).suffix.lower().lstrip(".")
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"

    ctype = ""
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        try:
            response = await client.head(url)
            ctype = response.headers.get("content-type", "")
        except Exception:
            ctype = ""
        if not ctype or ctype == "application/octet-stream":
            try:
                response = await client.get(url, headers={"Range": "bytes=0-0"})
                ctype = response.headers.get("content-type", ctype)
            except Exception:
                pass

    if ctype.startswith("video/"):
        return "video"
    if ctype.startswith("image/"):
        return "image"
    raise ValueError(f"could not determine if {url} is a video or image (ext={ext or 'none'}, content-type={ctype or 'unknown'})")


class Scores(BaseModel):
    anatomy: float = Field(ge=0, le=10)
    identity_match: float = Field(ge=0, le=10)
    style_match: float = Field(ge=0, le=10)
    text_garbage: float = Field(ge=0, le=10)
    overall: float = Field(ge=0, le=10)
    variation: Optional[float] = Field(ge=0, le=10)


class Defect(BaseModel):
    frame: int
    issue: str
    severity: Literal["minor", "major"]


class InspectionResult(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    passed: bool = Field(alias="pass")
    scores: Scores
    defects: List[Defect]
    notes: str


def register(mcp):
    from mcp.types import ToolAnnotations

    readonly = ToolAnnotations(readOnlyHint=True)

    @mcp.tool()
    async def r2_put(
        key: str,
        source_url: Optional[str] = None,
        base64_data: Optional[str] = None,
        content_type: Optional[str] = None,
    ):
        """Copy a file into the R2 bucket under a caller-chosen key.

        Used to stage user-supplied reference media (product photos, character
        identity shots) under stable keys such as `characters/<id>/identity/01.jpg`
        or `products/<id>/front.jpg`, so later tools can point at a permanent URL.

        Args:
            key: Destination object key. No leading `/`, no `..` segments, max 512 chars.
            source_url: Public URL to copy from. Exactly one of `source_url`/`base64_data`.
            base64_data: Raw file bytes, base64-encoded. Exactly one of `source_url`/`base64_data`.
            content_type: Optional MIME type override. Guessed from the key's extension if omitted.

        Returns:
            A dict with `url` (permanent public URL), `key`, and `bytes` (size written).
        """
        _validate_key(key)
        if bool(source_url) == bool(base64_data):
            raise ValueError("provide exactly one of source_url or base64_data")
        if not r2.configured():
            raise RuntimeError("R2 is not configured")

        if source_url:
            url, nbytes = await r2.put_from_url(source_url, key, ctype=content_type)
        else:
            try:
                data = base64.b64decode(base64_data, validate=True)
            except Exception as exc:
                raise ValueError(f"base64_data is not valid base64: {exc}") from None
            if len(data) > r2.MAX_INPUT_BYTES:
                raise ValueError(f"data is {len(data)} bytes, over the {r2.MAX_INPUT_BYTES} byte limit")
            url = await r2.put_bytes(data, key, content_type)
            nbytes = len(data)

        return {"url": url, "key": key, "bytes": nbytes}

    @mcp.tool(annotations=readonly)
    def r2_list(prefix: str, limit: int = 200):
        """List objects in the R2 bucket under a key prefix.

        Args:
            prefix: Key prefix to list, e.g. `characters/abc123/`.
            limit: Maximum number of objects to return (default 200).

        Returns:
            A list of dicts, each with `key`, `url`, `bytes`, and `last_modified`.
        """
        if not r2.configured():
            raise RuntimeError("R2 is not configured")
        return r2.list_prefix(prefix, limit)

    @mcp.tool()
    def r2_delete(key: str, confirm: bool = False):
        """Permanently delete an object from the R2 bucket.

        This is not reversible. Refuses unless `confirm=True` is passed explicitly;
        never call this with `confirm=True` on a default/inferred argument.

        Args:
            key: Object key to delete.
            confirm: Must be `True` to actually delete. Defaults to `False`.

        Returns:
            A dict with `deleted` (bool) and either `key` or a `message` explaining the refusal.
        """
        if not confirm:
            return {"deleted": False, "message": "Refusing to delete without confirm=True. Re-call with confirm=True to permanently delete this key."}
        _validate_key(key)
        if not r2.configured():
            raise RuntimeError("R2 is not configured")
        r2.delete_key(key)
        return {"deleted": True, "key": key}

    @mcp.tool()
    async def inspect_media(
        url: str,
        rubric: str,
        frames: int = 6,
        model: str = "grok-4.6",
        reference_image_urls: Optional[List[str]] = None,
    ):
        """Run automated QC on a generated image or video with Grok vision.

        Images are inspected directly. Videos are first split into evenly-spaced
        frames (via the local ffmpeg-based extractor in `ext.render`), and each
        frame is sent to the model. When `reference_image_urls` are given, they
        are sent first and labeled as identity references so `identity_match` is
        judged against them. The response is forced into the fixed rubric schema
        below using xAI structured outputs, so the result is always valid JSON.

        Args:
            url: Public URL of the image or video to inspect. Video vs image is
                decided from the file extension, falling back to the HTTP Content-Type.
            rubric: Free-text instructions describing what to check for (e.g. brand
                guidelines, expected pose, forbidden text).
            frames: Number of evenly-spaced frames to sample from a video (ignored for images).
            model: Vision-capable Grok model (default `grok-4.6`).
            reference_image_urls: Optional public URLs of identity reference images.

        Returns:
            A dict matching the schema:
            `{"pass": bool, "scores": {"anatomy", "identity_match", "style_match",
            "text_garbage", "overall", "variation"} (each 0-10; "variation" is null
            when reference_image_urls is empty), "defects": [{"frame", "issue",
            "severity": "minor"|"major"}], "notes": str}`.
        """
        kind = await _detect_kind(url)

        content = []
        reference_image_urls = reference_image_urls or []
        for ref_url in reference_image_urls:
            content.append(image(image_url=ref_url, detail="high"))

        if kind == "video":
            try:
                from ext.render import extract_frames_local
            except ImportError as exc:
                raise RuntimeError(f"video inspection requires ext.render.extract_frames_local, which is not available: {exc}") from exc
            frame_bytes = await extract_frames_local(url, frames)
            for frame_data in frame_bytes:
                b64 = base64.b64encode(frame_data).decode("utf-8")
                content.append(image(image_url=f"data:image/jpeg;base64,{b64}", detail="high"))
            frame_count = len(frame_bytes)
        else:
            content.append(image(image_url=url, detail="high"))
            frame_count = 1

        if reference_image_urls:
            variation_instruction = (
                "Also score \"variation\" from 0 to 10: how much the candidate's "
                "expression, hair style and pose differ from the reference images "
                "(10 = clearly different, 0 = a copy of the reference). "
            )
            side_instruction = (
                "State which side of the picture each tattoo, piercing or prop is on, in the "
                "reference images and in the candidate, then compare; a mark that changes side "
                "is a major defect. "
            )
        else:
            variation_instruction = "Set \"variation\" to null; there are no reference images. "
            side_instruction = ""

        prompt = (
            f"{rubric}\n\n"
            f"The first {len(reference_image_urls)} image(s) above are identity reference "
            f"photos (ignore them for defects; use them only to judge identity_match). "
            f"The remaining {frame_count} image(s) are the subject to inspect, in order, "
            f"numbered as frame 1 through frame {frame_count}. "
            f"{side_instruction}"
            "Score each rubric dimension from 0 (fails) to 10 (perfect). List every defect "
            "you see with the 1-based frame number it appears in. "
            f"{variation_instruction}"
            "Set \"pass\" to true only if there are no major defects and overall >= 7."
        )
        content.append(prompt)

        client = Client(api_key=XAI_API_KEY)
        chat = client.chat.create(model=model)
        chat.append(user(*content))
        _response, parsed = chat.parse(InspectionResult)
        client.close()

        if not reference_image_urls:
            parsed.scores.variation = None

        return parsed.model_dump(by_alias=True)
