"""Rewrite xAI/X media URLs so R2 is primary and the temp URL is aside.

Every media tool that sees an xAI (or X CDN) result URL must persist the
bytes to R2 and return that public URL as `url` / `urls`. The original
temporary URL is never the primary; it is returned as `temp_x_url` (and
`temp_x_urls` when there is more than one).
"""

from __future__ import annotations


def rewrite_media_fields(
    *,
    r2_urls: list[str],
    original_urls: list[str] | None = None,
) -> dict:
    """Map persisted R2 URLs to the envelope fields tools return.

    `r2_urls` are the permanent public URLs. `original_urls` are the xAI/X
    URLs those bytes came from (empty when the tool produced bytes itself,
    e.g. local TTS or a cutout).
    """
    originals = [u for u in (original_urls or []) if u]
    r2_urls = [u for u in r2_urls if u]
    fields: dict = {
        "urls": list(r2_urls),
        "url": r2_urls[0] if r2_urls else None,
        "temp_x_url": originals[0] if originals else None,
    }
    if len(originals) > 1:
        fields["temp_x_urls"] = originals
    return fields


def is_r2_url(url: str, public_base: str) -> bool:
    base = (public_base or "").rstrip("/")
    return bool(base) and url.startswith(base + "/")
