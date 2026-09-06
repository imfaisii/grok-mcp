"""Regression check: the key xAI was told to PUT into must be the key _finalize reads.

Reproduces the "No Video Returned" bug: _finalize used to recompute the key with
media_key(n=None), which calls _next_index and returns the NEXT free index once
the upload has landed -- so it looked for generate_video-1.mp4 while the file sat
at generate_video-0.mp4.
"""
import asyncio, sys, types

import r2
from ext import video

UPLOADED = "jobs/testjob/generate_video-0.mp4"


def _install_fakes():
    """R2 with exactly one object: the video xAI already uploaded."""
    r2.configured = lambda: True
    r2.public_url = lambda key: f"https://cdn.test/{key}"
    r2.head = lambda key: {"bytes": 123} if key == UPLOADED else None
    # _next_index lists what already exists -- after the upload, that is -0,
    # so a fresh media_key() call now hands back -1.
    r2.list_prefix = lambda prefix, limit=200: (
        [{"key": UPLOADED}] if UPLOADED.startswith(prefix) else []
    )


def main():
    _install_fakes()

    # Sanity: the bug's precondition. A recomputed key no longer matches.
    recomputed = r2.media_key("generate_video", "mp4", job_id="testjob")
    assert recomputed != UPLOADED, "precondition broken: media_key is idempotent here"

    # xAI reports done, and video.url is the presigned PUT url (never GETtable).
    data = {
        "status": "done",
        "model": "grok-imagine-video-1.5",
        "request_id": "req-1",
        "video": {"duration": 8, "url": "https://r2.test/put?X-Amz-Signature=abc"},
    }

    out = asyncio.run(
        video._finalize(data, "generate_video", False, key=UPLOADED)
    )

    assert out["urls"] == [f"https://cdn.test/{UPLOADED}"], f"lost the video: {out['urls']}"
    assert out["r2_keys"] == [UPLOADED], f"wrong key: {out['r2_keys']}"
    assert out["bytes"] == 123
    print("ok: _finalize resolves the uploaded key")


if __name__ == "__main__":
    main()
