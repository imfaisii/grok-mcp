"""Image generation with permanent R2 storage.

Replaces upstream `generate_image` (src/server.py, around line 165) with a
version that persists every generated image to R2 (xAI's output URLs are
temporary) and returns the standard media envelope the other ext media tools
use, instead of a markdown-only string.

Calls xAI the same way upstream does, through `xai_sdk.Client.image.sample_batch`.
Unlike video, images have no documented `output.upload_url` to skip the
round trip: xAI's Zero Data Retention FAQ spells out the asymmetry — "For
video, you must supply your own output.upload_url ... For images, return
base64 only." So each image here is downloaded (or decoded from base64,
whichever xAI already returned) and re-uploaded to R2 through `r2.put_bytes`.
"""

import io
from pathlib import Path
from typing import List, Optional

from PIL import Image
from xai_sdk import Client

import r2
from src.utils import encode_image_to_base64, XAI_API_KEY


def _encode_image_ref(path: str) -> str:
    b64 = encode_image_to_base64(path)
    ext = Path(path).suffix.lower().replace(".", "")
    return f"data:image/{ext};base64,{b64}"


def _cost_and_footer(usage, show_usage: bool) -> tuple:
    cost = r2.cost_usd(usage) if usage is not None else None
    if not show_usage or usage is None:
        return cost, ""
    parts = []
    total_tokens = getattr(usage, "total_tokens", None)
    if total_tokens:
        parts.append(f"**Tokens:** {total_tokens:,}")
    if cost is not None:
        parts.append(f"**Cost:** ${cost:.4f}")
    footer = "\n\n---\n" + " · ".join(parts) if parts else ""
    return cost, footer


def register(mcp):
    mcp._tool_manager._tools.pop("generate_image", None)

    @mcp.tool(name="generate_image")
    async def generate_image(
        prompt: str,
        model: str = "grok-imagine-image",
        image_paths: Optional[List[str]] = None,
        image_urls: Optional[List[str]] = None,
        n: int = 1,
        image_format: str = "url",
        aspect_ratio: Optional[str] = None,
        resolution: Optional[str] = None,
        job_id: Optional[str] = None,
        key_prefix: Optional[str] = None,
        show_usage: bool = False,
    ) -> dict:
        """Generate new images or edit existing ones with Grok Imagine, then store every result in R2.

        Pass `image_paths` and/or `image_urls` to edit images or use them as
        visual references. Multiple references are combined in a single call.
        xAI's output URL is temporary, so each image is copied to R2 and the
        envelope's `urls`/`r2_keys` point at the permanent copies.

        Args:
            prompt: Image description, or the edit instruction when references are provided.
            model: Image model (`grok-imagine-image` or `grok-imagine-image-pro`).
            image_paths: Local image files (JPG/PNG) used as edit sources or references.
            image_urls: Public image URLs used as edit sources or references.
            n: Number of images to generate (1-10).
            image_format: `"url"` (default) or `"base64"` — controls how xAI hands the
                image to this tool, not whether it gets persisted: every image is copied
                to R2 and its permanent URL returned either way. `"base64"` additionally
                fills in this result's `base64` field with the raw image data xAI
                returned, so a caller that wants the bytes inline skips a second fetch.
            aspect_ratio: Aspect ratio like `"16:9"`, `"1:1"`, or `"9:16"`.
            resolution: `"1k"` or `"2k"`.
            job_id: Groups this output with others under `{key_prefix or "jobs"}/{job_id}/` in R2.
            key_prefix: Overrides the default R2 key grouping prefix ("jobs" or "adhoc").
            show_usage: Include a token/cost footer in the markdown field (default False).

        Returns:
            The standard media envelope: `urls`, `r2_keys`, `width`, `height`, `bytes`,
            `cost_usd`, `model`, `request_id`, `moderated`, plus `base64` (a list lined up
            with `urls`, populated only when `image_format="base64"`) and a human-readable
            `markdown` block. `request_id` is always None — xAI's image API does not return
            one. `moderated` is True only when every requested image was blocked; a partial
            block is called out per image in `markdown` instead.
        """
        tool = "generate_image"
        refs = []
        if image_paths:
            refs.extend(_encode_image_ref(p) for p in image_paths)
        if image_urls:
            refs.extend(image_urls)

        params = {"prompt": prompt, "model": model, "n": n, "image_format": image_format}
        if refs:
            params["image_urls"] = refs
        if aspect_ratio:
            params["aspect_ratio"] = aspect_ratio
        if resolution:
            params["resolution"] = resolution

        client = Client(api_key=XAI_API_KEY)
        images = client.image.sample_batch(**params)
        client.close()

        cost, footer = _cost_and_footer(images[0].usage if images else None, show_usage)

        urls, keys, b64s, lines = [], [], [], []
        total_bytes = 0
        width = height = None
        blocked = 0

        for i, img in enumerate(images, start=1):
            if not img.respect_moderation:
                blocked += 1
                lines.append(f"**Image {i}:** blocked by moderation")
                continue

            data = img.image  # bytes; decodes xAI's base64 or downloads its temporary url
            key = r2.media_key(tool, "jpg", job_id=job_id, key_prefix=key_prefix)
            url = await r2.put_bytes(data, key)
            urls.append(url)
            keys.append(key)
            total_bytes += len(data)
            if width is None:
                with Image.open(io.BytesIO(data)) as im:
                    width, height = im.size
            if image_format == "base64":
                b64s.append(img.base64)
            lines.append(f"**Image {i}:** {url}")

        moderated = bool(images) and blocked == len(images)
        markdown = "## Generated Image(s)\n\n" + "\n\n".join(lines) + footer

        return r2.result(
            urls=urls,
            r2_keys=keys,
            markdown=markdown,
            width=width,
            height=height,
            nbytes=total_bytes if urls else None,
            cost=cost,
            model=images[0].model if images else model,
            moderated=moderated,
            base64=b64s or None,
        )
