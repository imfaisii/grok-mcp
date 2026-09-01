"""Video tools with voice-locked reference-to-video, 1080p, and R2 storage.

Replaces upstream `generate_video` and `extend_video` (src/server.py, around
line 228) with versions that add `reference_audios` (preset-voice locking via
xAI's TTS voice roster), 1080p resolution, `file_id` reference images, and
permanent storage of the result in R2 (xAI's output URLs are temporary).
Also adds `list_voices`, a readonly lookup of the preset voice roster
`reference_audios` draws from.

Everything here talks to xAI's video REST endpoints directly over httpx
rather than through `xai_sdk.Client.video`: `reference_audios` has no SDK
example in xAI's docs (REST only), and `output.upload_url` — the presigned
R2 PUT that lets xAI upload the result straight into our bucket instead of a
download-then-reupload round trip — is documented on all three endpoints
(`/v1/videos/generations`, `/v1/videos/edits`, `/v1/videos/extensions`) but
not shown as an SDK parameter either. Routing every mode through REST keeps
one code path and lets `output.upload_url` apply everywhere.

Doc oddity, kept verbatim rather than "corrected": reference placeholders in
the prompt are inconsistently indexed. Image references are 1-indexed
(`<IMAGE_1>`, `<IMAGE_2>`, ...) while audio references are 0-indexed
(`<AUDIO_0>`, `<AUDIO_1>`, ...). xAI's own combined example reads: "The
person from <IMAGE_1> speaks to camera with the voice from <AUDIO_0>."
"""

import asyncio
from pathlib import Path
from typing import List, Optional

import httpx
from mcp.types import ToolAnnotations

import r2
from src.utils import encode_image_to_base64, encode_video_to_base64, XAI_API_KEY

READONLY = ToolAnnotations(readOnlyHint=True)

VIDEOS_URL = "https://api.x.ai/v1/videos"
TTS_VOICES_URL = "https://api.x.ai/v1/tts/voices"
POLL_INTERVAL_S = 5.0
POLL_TIMEOUT_S = 600.0  # 10 minutes, matches the xAI SDK's default


def _headers() -> dict:
    return {"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"}


def _encode_image_ref(path: str) -> str:
    b64 = encode_image_to_base64(path)
    ext = Path(path).suffix.lower().replace(".", "")
    return f"data:image/{ext};base64,{b64}"


def _encode_video_ref(path: str) -> str:
    b64 = encode_video_to_base64(path)
    ext = Path(path).suffix.lower().replace(".", "")
    return f"data:video/{ext};base64,{b64}"


def _build_reference_images(
    paths: Optional[List[str]], urls: Optional[List[str]], file_ids: Optional[List[str]]
) -> List[dict]:
    refs = []
    if paths:
        refs.extend({"url": _encode_image_ref(p)} for p in paths)
    if urls:
        refs.extend({"url": u} for u in urls)
    if file_ids:
        refs.extend({"file_id": f} for f in file_ids)
    return refs


def _build_reference_audios(voice_ids: Optional[List[str]]) -> Optional[List[dict]]:
    if not voice_ids:
        return None
    if len(voice_ids) > 3:
        raise ValueError(f"reference_audios accepts at most 3 voices, got {len(voice_ids)}")
    return [{"voice_id": v} for v in voice_ids]


def _validate_generate_mode(
    *,
    has_image: bool,
    has_video: bool,
    has_reference_images: bool,
    has_reference_audios: bool,
    resolution: Optional[str],
    model: str,
    duration: Optional[int],
) -> None:
    """Enforce xAI's mutually-exclusive request modes before calling out.

    Modes: text-to-video (nothing set), image-to-video (`image`),
    reference-to-video (`reference_images` and/or `reference_audios`), and
    edit-video (`video`). Only one mode is active per request. xAI's docs
    only spell out `image` + `reference_images` as a documented 400; the
    other combinations below (video + image, video + reference, image +
    reference_audios) are inferred from the same "one mode per request"
    rule and are not separately confirmed in the docs.
    """
    has_reference = has_reference_images or has_reference_audios

    if has_video and has_image:
        raise ValueError(
            "video_path/video_url together with image_path/image_url is not allowed: "
            "pick edit-video or image-to-video, not both."
        )
    if has_video and has_reference:
        raise ValueError(
            "video_path/video_url together with reference_image_*/reference_audios is not allowed: "
            "pick edit-video or reference-to-video, not both."
        )
    if has_image and has_reference:
        raise ValueError(
            "image_path/image_url together with reference_image_*/reference_audios is not allowed "
            "(xAI returns 400 for `image` + `reference_images`): pick image-to-video or reference-to-video, not both."
        )

    if duration is not None and not (1 <= duration <= 15):
        raise ValueError(f"duration must be between 1 and 15 seconds, got {duration}")

    if resolution == "1080p":
        if has_video:
            raise ValueError(
                "resolution=1080p is not allowed when editing a video: edit output resolution "
                "matches the input, capped at 720p."
            )
        if has_reference:
            raise ValueError(
                "resolution=1080p is not allowed for reference-to-video: xAI caps reference-to-video at 720p."
            )
        if model != "grok-imagine-video-1.5":
            raise ValueError(
                f"resolution=1080p is only documented for text-to-video and image-to-video on "
                f"grok-imagine-video-1.5, not {model!r}."
            )


async def _post_video(path: str, payload: dict) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{VIDEOS_URL}/{path}", headers=_headers(), json=payload)
        resp.raise_for_status()
        return resp.json()["request_id"]


async def _poll_video(request_id: str) -> dict:
    deadline = asyncio.get_event_loop().time() + POLL_TIMEOUT_S
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            resp = await client.get(f"{VIDEOS_URL}/{request_id}", headers=_headers())
            resp.raise_for_status()
            data = resp.json()
            status = data.get("status")
            if status in ("done", "expired", "failed"):
                return data
            if asyncio.get_event_loop().time() > deadline:
                raise TimeoutError(
                    f"video request {request_id} did not complete within {POLL_TIMEOUT_S:.0f}s (status={status})"
                )
            await asyncio.sleep(POLL_INTERVAL_S)


def _is_moderation_message(message: Optional[str]) -> bool:
    return bool(message) and "moderat" in message.lower()


def _cost_and_footer(usage: Optional[dict], show_usage: bool) -> tuple:
    cost = r2.cost_usd(usage) if usage else None
    if not show_usage or not usage:
        return cost, ""
    parts = []
    total_tokens = usage.get("total_tokens")
    if total_tokens:
        parts.append(f"**Tokens:** {total_tokens:,}")
    if cost is not None:
        parts.append(f"**Cost:** ${cost:.4f}")
    footer = "\n\n---\n" + " · ".join(parts) if parts else ""
    return cost, footer


async def _finalize(
    data: dict, tool: str, job_id: Optional[str], key_prefix: Optional[str], show_usage: bool
) -> dict:
    """Turn a completed poll response into the standard R2 envelope.

    Persists the result to R2, preferring the `output.upload_url` PUT that
    xAI already performed (confirmed with `r2.head`) and falling back to
    downloading `video.url` when that object never landed.
    """
    status = data.get("status")
    model = data.get("model")
    request_id = data.get("request_id")
    usage = data.get("usage")
    cost, footer = _cost_and_footer(usage, show_usage)

    if status == "failed":
        error = data.get("error") or {}
        code = error.get("code")
        message = error.get("message")
        return r2.result(
            urls=[],
            r2_keys=[],
            markdown=f"## Video Failed\n\n\n**Error ({code}):** {message}\n\n" + footer,
            cost=cost,
            model=model,
            request_id=request_id,
            moderated=(code == "invalid_argument" and _is_moderation_message(message)),
            status=status,
        )

    if status == "expired":
        return r2.result(
            urls=[],
            r2_keys=[],
            markdown="## Video Expired\n\n\nThe request expired before it could be polled to completion.\n\n" + footer,
            cost=cost,
            model=model,
            request_id=request_id,
            status=status,
        )

    video = data.get("video") or {}
    duration = video.get("duration")
    source_url = video.get("url")

    # xAI reports a moderation failure as respect_moderation=false on the video.
    if video.get("respect_moderation") is False:
        return r2.result(
            urls=[],
            r2_keys=[],
            markdown="## Video Blocked by Moderation\n\n\nxAI reported `respect_moderation: false` for this result.\n\n" + footer,
            duration=duration,
            cost=cost,
            model=model,
            request_id=request_id,
            moderated=True,
            status=status,
            progress=data.get("progress"),
        )

    key = r2.media_key(tool, "mp4", job_id=job_id, key_prefix=key_prefix)
    nbytes = None
    permanent_url = None

    # When output.upload_url was sent, xAI PUTs straight into the bucket and may
    # never populate video.url, so the object landing is the success signal. A
    # missing url on its own must not be read as a moderation block.
    if r2.configured():
        head = r2.head(key)
        if head is not None:
            permanent_url = r2.public_url(key)
            nbytes = head["bytes"]
        elif source_url:
            permanent_url, nbytes = await r2.put_from_url(source_url, key)
    elif source_url:
        permanent_url = source_url

    if permanent_url is None:
        return r2.result(
            urls=[],
            r2_keys=[],
            markdown="## No Video Returned\n\n\nThe request reported status `done` but produced neither an uploaded object nor a video URL.\n\n" + footer,
            duration=duration,
            cost=cost,
            model=model,
            request_id=request_id,
            status=status,
            progress=data.get("progress"),
        )

    return r2.result(
        urls=[permanent_url],
        r2_keys=[key],
        markdown=f"## Video\n\n\n**URL:** {permanent_url}\n\n\n**Duration:** {duration}s\n\n" + footer,
        duration=duration,
        nbytes=nbytes,
        cost=cost,
        model=model,
        request_id=request_id,
        status=status,
        progress=data.get("progress"),
    )


def register(mcp):
    mcp._tool_manager._tools.pop("generate_video", None)
    mcp._tool_manager._tools.pop("extend_video", None)

    @mcp.tool(name="generate_video")
    async def generate_video(
        prompt: str,
        model: str = "grok-imagine-video-1.5",
        image_path: Optional[str] = None,
        image_url: Optional[str] = None,
        video_path: Optional[str] = None,
        video_url: Optional[str] = None,
        reference_image_paths: Optional[List[str]] = None,
        reference_image_urls: Optional[List[str]] = None,
        reference_image_file_ids: Optional[List[str]] = None,
        reference_audios: Optional[List[str]] = None,
        duration: Optional[int] = None,
        aspect_ratio: Optional[str] = None,
        resolution: Optional[str] = None,
        job_id: Optional[str] = None,
        key_prefix: Optional[str] = None,
        show_usage: bool = False,
    ) -> dict:
        """Generate or edit videos with Grok Imagine, then store the result in R2.

        Text-to-video by default. Provide an image to animate (image-to-video),
        a source video to edit (edit-video), or reference images/a preset voice
        to guide a new video around a subject and/or voice (reference-to-video).
        Only one mode per call — mixing them raises a clear error instead of
        letting xAI 400. xAI's output URL is temporary, so the result is copied
        to R2 and the envelope's `urls`/`r2_keys` point at the permanent copy.

        Args:
            prompt: Video description, or the edit instruction for video editing.
            model: Video model (default `grok-imagine-video-1.5`).
            image_path: Local image to use as the starting frame (image-to-video).
            image_url: Public image URL to use as the starting frame (image-to-video).
            video_path: Local video to edit (.mp4, ≤ 8.7s retained on output).
            video_url: Public video URL to edit (.mp4, ≤ 8.7s retained on output).
            reference_image_paths: Local images as subject/style references (reference-to-video).
            reference_image_urls: Public image URLs as subject/style references (reference-to-video).
            reference_image_file_ids: xAI Files API `file_id`s as references; can be mixed with
                the path/url references above. The maximum reference image count is not
                documented by xAI, so none is enforced here — the API's own error surfaces.
            reference_audios: Up to 3 preset voice ids (e.g. `["eve"]`), same ids the TTS API
                and `list_voices` use. Locks the subject's voice on `grok-imagine-video-1.5`;
                works with or without reference images. In the prompt, refer to voices as
                `<AUDIO_0>`, `<AUDIO_1>`, `<AUDIO_2>` and to images as `<IMAGE_1>`, `<IMAGE_2>`, ...
                (xAI's own docs index these two 0- and 1-indexed respectively).
            duration: Video length in seconds (1-15, default 8; ignored when editing).
            aspect_ratio: Aspect ratio like `"16:9"` or `"9:16"` (ignored when editing).
            resolution: `"480p"`, `"720p"`, or `"1080p"`. 1080p is only documented for
                text-to-video and image-to-video on `grok-imagine-video-1.5`; reference-to-video
                is capped at 720p and edit output resolution matches the input, capped at 720p.
            job_id: Groups this output with others under `{key_prefix or "jobs"}/{job_id}/` in R2.
            key_prefix: Overrides the default R2 key grouping prefix ("jobs" or "adhoc").
            show_usage: Include a token/cost footer in the markdown field (default False).

        Returns:
            The standard media envelope: `urls`, `r2_keys`, `duration`, `bytes`, `cost_usd`,
            `model`, `request_id`, `moderated`, `status`, `progress`, and a human-readable
            `markdown` block.
        """
        has_image = bool(image_path or image_url)
        has_video = bool(video_path or video_url)
        reference_images = _build_reference_images(
            reference_image_paths, reference_image_urls, reference_image_file_ids
        )
        audio_refs = _build_reference_audios(reference_audios)

        _validate_generate_mode(
            has_image=has_image,
            has_video=has_video,
            has_reference_images=bool(reference_images),
            has_reference_audios=bool(audio_refs),
            resolution=resolution,
            model=model,
            duration=duration,
        )

        tool = "generate_video"
        key = r2.media_key(tool, "mp4", job_id=job_id, key_prefix=key_prefix)
        payload = {"model": model, "prompt": prompt}

        if has_video:
            path = "edits"
            payload["video"] = {"url": _encode_video_ref(video_path) if video_path else video_url}
        else:
            path = "generations"
            if has_image:
                payload["image"] = {"url": _encode_image_ref(image_path) if image_path else image_url}
            if reference_images:
                payload["reference_images"] = reference_images
            if audio_refs:
                payload["reference_audios"] = audio_refs
            if duration:
                payload["duration"] = duration
            if aspect_ratio:
                payload["aspect_ratio"] = aspect_ratio
            if resolution:
                payload["resolution"] = resolution

        if r2.configured():
            payload["output"] = {"upload_url": r2.presigned_put(key, "video/mp4")}

        request_id = await _post_video(path, payload)
        data = await _poll_video(request_id)
        data.setdefault("request_id", request_id)
        return await _finalize(data, tool, job_id, key_prefix, show_usage)

    @mcp.tool(name="extend_video")
    async def extend_video(
        prompt: str,
        video_url: Optional[str] = None,
        video_path: Optional[str] = None,
        video_file_id: Optional[str] = None,
        model: str = "grok-imagine-video",
        duration: Optional[int] = None,
        job_id: Optional[str] = None,
        key_prefix: Optional[str] = None,
        show_usage: bool = False,
    ) -> dict:
        """Extend an existing video with a follow-up prompt, then store the result in R2.

        Continues the source video seamlessly from its last frame. `duration` sets
        the length of the extension segment only, not the total output. For example,
        a 10 second input plus `duration=5` yields a 15 second final video. The
        documented range for the extension segment is 2-10 seconds (default 6); the
        input video's length range is not documented by xAI, so it is not enforced
        here. `reference_audios` is not documented for `/v1/videos/extensions`, so
        this tool does not accept or send it — voice locking is `generate_video`-only.

        Args:
            prompt: What should happen in the extended segment.
            video_url: Public URL of the source video (.mp4). Provide exactly one of
                `video_url`, `video_path`, `video_file_id`.
            video_path: Local video file to extend (.mp4).
            video_file_id: xAI Files API `file_id` of the source video.
            model: Video model (default `grok-imagine-video`, the model xAI's docs
                use for editing and extension).
            duration: Length of the extension in seconds (2-10, default 6).
            job_id: Groups this output with others under `{key_prefix or "jobs"}/{job_id}/` in R2.
            key_prefix: Overrides the default R2 key grouping prefix ("jobs" or "adhoc").
            show_usage: Include a token/cost footer in the markdown field (default False).

        Returns:
            The standard media envelope: `urls`, `r2_keys`, `duration`, `bytes`, `cost_usd`,
            `model`, `request_id`, `moderated`, `status`, `progress`, and a human-readable
            `markdown` block. `duration` is the extension segment length that xAI reports,
            not the length of the stored file, which is the source video plus that segment.
        """
        sources = [s for s in (video_url, video_path, video_file_id) if s]
        if len(sources) != 1:
            raise ValueError(
                f"provide exactly one of video_url, video_path, video_file_id, got {len(sources)}"
            )
        if duration is not None and not (2 <= duration <= 10):
            raise ValueError(f"duration must be between 2 and 10 seconds, got {duration}")

        tool = "extend_video"
        key = r2.media_key(tool, "mp4", job_id=job_id, key_prefix=key_prefix)

        if video_path:
            video_field = {"url": _encode_video_ref(video_path)}
        elif video_file_id:
            video_field = {"file_id": video_file_id}
        else:
            video_field = {"url": video_url}

        payload = {"model": model, "prompt": prompt, "video": video_field}
        if duration:
            payload["duration"] = duration
        if r2.configured():
            payload["output"] = {"upload_url": r2.presigned_put(key, "video/mp4")}

        request_id = await _post_video("extensions", payload)
        data = await _poll_video(request_id)
        data.setdefault("request_id", request_id)
        return await _finalize(data, tool, job_id, key_prefix, show_usage)

    @mcp.tool(name="list_voices", annotations=READONLY)
    async def list_voices() -> list:
        """List the preset voice roster `reference_audios` accepts (same ids as the TTS API).

        Returns:
            A list of `{voice_id, name, description, language}` objects — only the
            fields the API actually returns for each voice, in its own order.
        """
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(TTS_VOICES_URL, headers=_headers())
            resp.raise_for_status()
            return resp.json().get("voices", [])
