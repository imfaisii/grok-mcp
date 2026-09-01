"""Subject cutout generation: render on a plate, remove the background locally.

Ported from grokified.com/grok-mcp's http_server.py. Renders the subject with
Grok Imagine against a flat plate background, then removes that background
locally with rembg and crops to the subject's alpha bounds. Uploads the result
to R2 when configured; otherwise saves under FILES_DIR and returns a URL
served by the /files/{name} route in src/http_app.py.
"""

import io
import os
import uuid

import httpx
from PIL import Image
from rembg import new_session, remove
from xai_sdk import Client

import r2
from files import FILES_DIR
from src.utils import XAI_API_KEY

# compose, the Caddyfile and src/http_app.py all set DOMAIN; MCP_DOMAIN is
# the name the upstream fork used, kept so an existing deploy keeps working.
DOMAIN = (os.getenv("DOMAIN") or os.getenv("MCP_DOMAIN") or "").strip().lower()


_SESSION = None


def _get_session():
    global _SESSION
    if _SESSION is None:
        _SESSION = new_session("isnet-general-use")
    return _SESSION


PLATE_PROMPT = (
    "Render the subject completely on its own, centred, "
    "filling most of the frame, against a solid flat uniform matte "
    "background in one single deep muted colour that appears nowhere in "
    "the subject itself. Deep navy, deep teal and deep plum are good "
    "choices; pick whichever is furthest from the subject's own colours, "
    "and pick something else entirely if the subject is that colour. "
    "Never make the background white, off-white, cream, silver or any "
    "pale washed-out shade, because the subject's brightest highlights "
    "are almost white and must stay distinct from it. Never make it a "
    "vivid, neon or chroma-key colour either, because a green-screen "
    "backdrop comes with a coloured rim light on the subject and a cast "
    "shadow. No shadow, no gradient, no vignette, no reflection, no "
    "floor, no surface, no props and nothing else behind or beneath the "
    "subject."
)


def register(mcp):
    @mcp.tool()
    async def generate_no_background_image(
        prompt: str,
        model: str = "grok-imagine-image-2.0",
        aspect_ratio: str | None = None,
        resolution: str | None = None,
    ) -> str:
        """Generate a subject-only image with a transparent background.

        Renders the subject on a flat plate background with Grok Imagine, then
        removes the background locally and crops to the subject. Use for
        stickers, logo marks, product cutouts, and packshots you'll composite
        elsewhere.

        Args:
            prompt: The subject alone (no scene), e.g. "matte black wireless earbuds".
            model: Image model to use.
            aspect_ratio: Aspect ratio like `"16:9"`, `"1:1"`, or `"9:16"`.
            resolution: `"1k"` or `"2k"`.

        Returns:
            Markdown block with the hosted PNG URL and its pixel dimensions.
        """
        client = Client(api_key=XAI_API_KEY)

        params = {
            "model": model,
            "prompt": f"{prompt}\n\n{PLATE_PROMPT}",
            "n": 1,
            "image_format": "url",
        }
        if aspect_ratio:
            params["aspect_ratio"] = aspect_ratio
        if resolution:
            params["resolution"] = resolution

        images = client.image.sample_batch(**params)
        client.close()

        async with httpx.AsyncClient() as http_client:
            response = await http_client.get(images[0].url)
            response.raise_for_status()
            image_bytes = response.content

        cutout = remove(image_bytes, session=_get_session(), post_process_mask=True)
        img = Image.open(io.BytesIO(cutout)).convert("RGBA")

        alpha_bbox = img.getchannel("A").getbbox()
        if alpha_bbox:
            img = img.crop(alpha_bbox)

        filename = f"{uuid.uuid4()}.png"
        buffer = io.BytesIO()
        img.save(buffer, "PNG")
        data = buffer.getvalue()

        if r2.configured():
            url = await r2.put_bytes(data, f"grok-mcp/{filename}")
        else:
            FILES_DIR.mkdir(parents=True, exist_ok=True)
            (FILES_DIR / filename).write_bytes(data)
            url = f"https://{DOMAIN}/files/{filename}"

        return f"## Generated Image (no background)\n\n**Image:** {url}\n\n**Size:** {img.width}x{img.height}px\n"
