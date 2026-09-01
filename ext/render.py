"""ffmpeg-based video rendering tools: slideshows, concatenation, captions,
frame extraction, subject cutouts and cover images.

Every tool is split into an internal `_render_x()` / `_x()` function that does
the local work (ffmpeg, ffprobe, Pillow, rembg) and needs no credentials, and
a thin `@mcp.tool()` wrapper that uploads the result to R2 via `r2.py` and
returns the standard envelope from `r2.result()`. This lets the local work be
exercised directly in tests without R2 or xAI credentials.
"""

import asyncio
import io
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import r2

FPS = 30
FFMPEG_TIMEOUT = 240
MAX_SLIDESHOW_SECONDS = 180
TARGET_SIZES = {"9:16": (1080, 1920), "1:1": (1080, 1080), "16:9": (1920, 1080)}

# Safe zones for 1080x1920: nothing in the top 250px or bottom 400px.
TOP_SAFE = 250
BOTTOM_SAFE = 400


# --------------------------------------------------------------------------
# ffmpeg / ffprobe plumbing
# --------------------------------------------------------------------------


async def _run_ffmpeg(args: list[str], timeout: int = FFMPEG_TIMEOUT) -> None:
    """Run ffmpeg with an argument list (never shell=True). Raises with the
    last ~20 lines of stderr on failure; the full stderr goes to our stderr."""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning", *args]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"ffmpeg timed out after {timeout}s: {' '.join(cmd[:6])}...")
    if proc.returncode != 0:
        print(stderr.decode(errors="replace"), file=sys.stderr)
        tail = "\n".join(stderr.decode(errors="replace").strip().splitlines()[-20:])
        raise RuntimeError(f"ffmpeg failed (exit {proc.returncode}):\n{tail}")


async def _ffprobe(path: Path) -> dict:
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"ffprobe timed out after 30s on {path.name}")
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path.name}: {stderr.decode(errors='replace')[-1000:]}")
    return json.loads(stdout)


async def _video_info(path: Path) -> dict:
    """width, height, duration, fps, has_audio for a local media file."""
    data = await _ffprobe(path)
    streams = data.get("streams", [])
    vstream = next((s for s in streams if s["codec_type"] == "video"), None)
    astream = next((s for s in streams if s["codec_type"] == "audio"), None)
    duration = float(data.get("format", {}).get("duration") or (vstream or {}).get("duration") or 0)
    fps = 0.0
    if vstream and vstream.get("r_frame_rate"):
        num, _, den = vstream["r_frame_rate"].partition("/")
        den = den or "1"
        fps = float(num) / float(den) if float(den) else float(num)
    return {
        "width": int(vstream["width"]) if vstream else 0,
        "height": int(vstream["height"]) if vstream else 0,
        "duration": duration,
        "fps": fps,
        "has_audio": astream is not None,
    }


async def _download(url: str, dest: Path) -> Path:
    """Fetch a remote file to a local path. Needs no credentials; enforces
    r2.MAX_INPUT_BYTES."""
    data = await r2.fetch(url)
    dest.write_bytes(data)
    return dest


async def _transcribe_video(video_path: Path, tmpdir: Path, job_id: Optional[str], key_prefix: Optional[str]) -> dict:
    """Extract a local video's audio track to mp3, upload it to R2 for a
    public URL, and transcribe it. xAI's /v1/stt rejects mp4 containers
    (`Could not detect audio format from file header`), so a video url can't
    be passed to stt_words directly - it needs a real audio container."""
    info = await _video_info(video_path)
    if not info["has_audio"]:
        raise ValueError(f"{video_path.name} has no audio track to transcribe; pass `words` or `srt` explicitly")

    mp3_path = tmpdir / "transcribe_audio.mp3"
    await _run_ffmpeg(["-i", str(video_path), "-vn", "-acodec", "libmp3lame", "-q:a", "2", str(mp3_path)])

    key = r2.media_key("transcribe_audio", "mp3", job_id=job_id, key_prefix=key_prefix)
    mp3_url = await r2.put_bytes(mp3_path.read_bytes(), key)

    try:
        from ext.audio import stt_words
    except ImportError as exc:
        raise RuntimeError("transcribing a video needs ext/audio.py (stt_words), which isn't available") from exc
    return await stt_words(mp3_url)


def _target_size(aspect_ratio: str) -> tuple[int, int]:
    if aspect_ratio not in TARGET_SIZES:
        raise ValueError(f"aspect_ratio must be one of {list(TARGET_SIZES)}, got {aspect_ratio!r}")
    return TARGET_SIZES[aspect_ratio]


def _escape_filter_path(path: Path) -> str:
    """Escape a filesystem path for use inside an ffmpeg filter option value
    (e.g. subtitles=... / ass=...)."""
    s = str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    return s


# --------------------------------------------------------------------------
# Caption generation
#
# ext.audio owns building caption text (ass_from_words / srt_from_words,
# reading the styled templates in caption_styles/*.ass) — lazily imported at
# each call site, same as stt_words. This module only adapts segment text
# into the same words shape and writes the result to a temp file.
# --------------------------------------------------------------------------


def _segments_to_words(segments: list[dict], starts: list[float], durations: list[float]) -> list[dict]:
    """One synthetic word per segment, spanning its slot in the timeline, so
    segment `text` can be fed through ext.audio's caption builders."""
    words = []
    for seg, start, dur in zip(segments, starts, durations):
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        words.append({"text": text, "start": start, "end": start + float(dur)})
    return words


def _write_text(tmpdir: Path, name: str, text: str) -> Path:
    path = tmpdir / name
    path.write_text(text)
    return path


_SENTENCE_END = re.compile(r"[.!?]$")


def _sentence_spans(words: list[dict]) -> list[tuple[float, float]]:
    spans = []
    start = None
    for w in words:
        if start is None:
            start = w["start"]
        if _SENTENCE_END.search(w["text"].strip()):
            spans.append((start, w["end"]))
            start = None
    if start is not None and words:
        spans.append((start, words[-1]["end"]))
    return spans


# --------------------------------------------------------------------------
# Motion filter graphs
# --------------------------------------------------------------------------


def _bg_chain(idx: int, n: int, motion: str, w: int, h: int, duration: float, fps: int) -> str:
    """Filter_complex snippet turning input [idx:v] into a WxH stream labeled
    [bg{n}], normalized to yuv420p."""
    label = f"bg{n}"
    up_w, up_h = int(w * 1.16), int(h * 1.16)
    fit = f"scale={up_w}:{up_h}:force_original_aspect_ratio=increase,crop={up_w}:{up_h}"

    if motion == "kenburns_in":
        return (
            f"[{idx}:v]scale={w * 3}:{h * 3}:force_original_aspect_ratio=increase,"
            f"zoompan=z='min(zoom+0.0018,1.4)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:"
            f"s={w}x{h}:fps={fps},setsar=1,format=yuv420p[{label}]"
        )
    if motion == "kenburns_out":
        return (
            f"[{idx}:v]scale={w * 3}:{h * 3}:force_original_aspect_ratio=increase,"
            f"zoompan=z='if(eq(on,1),1.4,max(zoom-0.0018,1.0))':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:"
            f"s={w}x{h}:fps={fps},setsar=1,format=yuv420p[{label}]"
        )
    if motion == "pan_left":
        return f"[{idx}:v]{fit},crop={w}:{h}:x='(in_w-{w})*(1-t/{duration})':y='(in_h-{h})/2',setsar=1,format=yuv420p[{label}]"
    if motion == "pan_right":
        return f"[{idx}:v]{fit},crop={w}:{h}:x='(in_w-{w})*(t/{duration})':y='(in_h-{h})/2',setsar=1,format=yuv420p[{label}]"
    if motion == "shake":
        return (
            f"[{idx}:v]{fit},crop={w}:{h}:"
            f"x='(in_w-{w})/2+10*sin(2*PI*t*3)':y='(in_h-{h})/2+10*cos(2*PI*t*2.6)',setsar=1,format=yuv420p[{label}]"
        )
    if motion == "slide_in_bottom":
        return (
            f"[{idx}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=rgba[fg{n}];"
            f"color=black:s={w}x{h}:d={duration}[cv{n}];"
            f"[cv{n}][fg{n}]overlay=x=0:y='if(lt(t,0.5),(1-t/0.5)*{h},0)':format=auto,setsar=1,format=yuv420p[{label}]"
        )
    # none
    return f"[{idx}:v]{fit},crop={w}:{h}:x='(in_w-{w})/2':y='(in_h-{h})/2',setsar=1,format=yuv420p[{label}]"


def _overlay_chain(idx: int, n: int, motion: str, w: int, h: int) -> tuple[str, str]:
    """Filter_complex snippet turning input [idx:v] (a transparent PNG) into an
    rgba stream labeled [ov{n}], plus the overlay= x/y expression to composite
    it onto the background, anchored bottom-center."""
    label = f"ov{n}"
    base = f"[{idx}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,format=rgba"
    if motion == "pop":
        chain = f"{base},fade=t=in:st=0:d=0.25:alpha=1[{label}]"
        expr = "x='(W-w)/2':y='H-h'"
    elif motion == "slide_in_bottom":
        chain = f"{base}[{label}]"
        expr = "x='(W-w)/2':y='if(lt(t,0.4),H-(t/0.4)*h,H-h)'"
    else:
        chain = f"{base}[{label}]"
        expr = "x='(W-w)/2':y='H-h'"
    return chain, expr


# --------------------------------------------------------------------------
# render_slideshow
# --------------------------------------------------------------------------


async def _render_slideshow(
    segments: list[dict],
    voiceover_url: Optional[str],
    music_url: Optional[str],
    music_volume_db: float,
    captions: Optional[dict],
    aspect_ratio: str,
    tmpdir: Path,
) -> Path:
    w, h = _target_size(aspect_ratio)

    if not segments:
        raise ValueError("segments must not be empty")

    durations = [s.get("duration") for s in segments]
    if any(d is None for d in durations):
        if not voiceover_url:
            raise ValueError("segment durations are required when voiceover_url is not given")
        try:
            from ext.audio import stt_words
        except ImportError as exc:
            raise RuntimeError(
                "auto-timing from voiceover needs ext/audio.py (stt_words), which isn't available; "
                "pass explicit segment durations instead"
            ) from exc
        transcript = await stt_words(voiceover_url)
        spans = _sentence_spans(transcript["words"])
        if len(spans) == len(segments) and spans:
            durations = [end - start for start, end in spans]
        else:
            total = transcript["duration"] or sum(s.get("duration") or 3.0 for s in segments)
            durations = [total / len(segments)] * len(segments)

    total_duration = sum(durations)
    if total_duration > MAX_SLIDESHOW_SECONDS:
        raise ValueError(f"slideshow duration {total_duration:.1f}s exceeds the {MAX_SLIDESHOW_SECONDS}s cap")

    # Download inputs, tracking ffmpeg input index for each.
    input_args: list[str] = []
    idx = 0
    bg_idx = []
    ov_idx: list[Optional[int]] = []
    for n, seg in enumerate(segments):
        img_path = tmpdir / f"bg{n}.img"
        await _download(seg["image_url"], img_path)
        input_args += ["-loop", "1", "-framerate", str(FPS), "-t", str(durations[n]), "-i", str(img_path)]
        bg_idx.append(idx)
        idx += 1
        if seg.get("overlay_image_url"):
            ov_path = tmpdir / f"ov{n}.png"
            await _download(seg["overlay_image_url"], ov_path)
            input_args += ["-loop", "1", "-framerate", str(FPS), "-t", str(durations[n]), "-i", str(ov_path)]
            ov_idx.append(idx)
            idx += 1
        else:
            ov_idx.append(None)

    voice_idx = None
    if voiceover_url:
        voice_path = tmpdir / "voice.audio"
        await _download(voiceover_url, voice_path)
        input_args += ["-i", str(voice_path)]
        voice_idx = idx
        idx += 1

    music_idx = None
    if music_url:
        music_path = tmpdir / "music.audio"
        await _download(music_url, music_path)
        input_args += ["-stream_loop", "-1", "-i", str(music_path)]
        music_idx = idx
        idx += 1

    filter_parts: list[str] = []
    seg_labels = []
    for n, seg in enumerate(segments):
        filter_parts.append(_bg_chain(bg_idx[n], n, seg.get("motion", "none"), w, h, durations[n], FPS))
        if ov_idx[n] is not None:
            ov_chain, ov_expr = _overlay_chain(ov_idx[n], n, seg.get("overlay_motion", "none"), w, h)
            filter_parts.append(ov_chain)
            filter_parts.append(f"[bg{n}][ov{n}]overlay={ov_expr}:format=auto,format=yuv420p[seg{n}]")
        else:
            filter_parts.append(f"[bg{n}]null[seg{n}]")
        seg_labels.append(f"seg{n}")

    transition = segments[0].get("transition", "cut") if segments else "cut"
    # transition applies per-segment boundary; use the first non-"cut" value if mixed, else cut.
    transitions = [s.get("transition", "cut") for s in segments[1:]] or ["cut"]

    if all(t == "cut" for t in transitions):
        concat_in = "".join(f"[{lbl}]" for lbl in seg_labels)
        filter_parts.append(f"{concat_in}concat=n={len(seg_labels)}:v=1:a=0[vconcat]")
        final_label = "vconcat"
        final_duration = total_duration
    else:
        cur_label = seg_labels[0]
        cur_duration = durations[0]
        for n in range(1, len(seg_labels)):
            t_name = transitions[n - 1] if n - 1 < len(transitions) else "cut"
            out_label = f"xf{n}"
            if t_name == "cut":
                filter_parts.append(f"[{cur_label}][{seg_labels[n]}]concat=n=2:v=1:a=0[{out_label}]")
                cur_duration = cur_duration + durations[n]
            else:
                xfade_name = "fadeblack" if t_name == "dip_black" else "fade"
                td = min(0.5, 0.4 * min(cur_duration, durations[n]))
                offset = max(0.0, cur_duration - td)
                filter_parts.append(
                    f"[{cur_label}][{seg_labels[n]}]xfade=transition={xfade_name}:duration={td}:offset={offset}[{out_label}]"
                )
                cur_duration = offset + durations[n]
            cur_label = out_label
        final_label = cur_label
        final_duration = cur_duration

    audio_label = None
    if voice_idx is not None:
        filter_parts.append(
            f"[{voice_idx}:a]apad=whole_dur={final_duration},atrim=0:{final_duration},asetpts=PTS-STARTPTS[voice]"
        )
        audio_label = "voice"
    if music_idx is not None:
        fade_start = max(0.0, final_duration - 1.0)
        filter_parts.append(
            f"[{music_idx}:a]atrim=0:{final_duration},asetpts=PTS-STARTPTS,"
            f"volume={music_volume_db}dB,afade=t=out:st={fade_start}:d=1[music]"
        )
        if audio_label:
            filter_parts.append(f"[{audio_label}][music]amix=inputs=2:duration=first:normalize=0[aout]")
            audio_label = "aout"
        else:
            audio_label = "music"

    if captions and captions.get("mode") == "burn":
        source = captions.get("source", "voiceover")
        style = captions.get("style", "bold_center")
        try:
            from ext.audio import ass_from_words
        except ImportError as exc:
            raise RuntimeError(
                "burning captions needs ext/audio.py (ass_from_words), which isn't available"
            ) from exc
        if source == "segments_text":
            starts = []
            acc = 0.0
            for d in durations:
                starts.append(acc)
                acc += d
            words = _segments_to_words(segments, starts, durations)
        else:
            if voice_idx is None:
                raise ValueError('captions source="voiceover" requires voiceover_url')
            try:
                from ext.audio import stt_words
            except ImportError as exc:
                raise RuntimeError(
                    "burning captions from the voiceover needs ext/audio.py (stt_words), which isn't available"
                ) from exc
            transcript = await stt_words(voiceover_url)
            words = transcript["words"]
        if words:
            ass_text = ass_from_words(words, style=style, width=w, height=h)
            ass_path = _write_text(tmpdir, "captions.ass", ass_text)
            filter_parts.append(f"[{final_label}]ass={_escape_filter_path(ass_path)}[vcap]")
            final_label = "vcap"

    out_path = tmpdir / "slideshow.mp4"
    cmd = [*input_args, "-filter_complex", ";".join(filter_parts), "-map", f"[{final_label}]"]
    if audio_label:
        cmd += ["-map", f"[{audio_label}]"]
    cmd += [
        "-r", str(FPS),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-color_range", "tv", "-preset", "veryfast",
        "-t", str(final_duration),
        "-movflags", "+faststart",
    ]
    if audio_label:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    else:
        cmd += ["-an"]
    cmd += [str(out_path)]

    await _run_ffmpeg(cmd)
    return out_path


async def render_slideshow(
    segments: list[dict],
    voiceover_url: Optional[str] = None,
    music_url: Optional[str] = None,
    music_volume_db: float = -18,
    captions: Optional[dict] = None,
    aspect_ratio: str = "9:16",
    job_id: Optional[str] = None,
    key_prefix: Optional[str] = None,
) -> dict:
    """Render a slideshow video from image segments with motion, transitions,
    voiceover, music and optional burned captions.

    Args:
        segments: List of `{"image_url", "duration", "motion", "transition",
            "overlay_image_url", "overlay_motion", "text"}`. `motion` is one of
            `kenburns_in`, `kenburns_out`, `pan_left`, `pan_right`, `shake`,
            `slide_in_bottom`, `none`. `transition` (applied before this
            segment) is `cut`, `crossfade` or `dip_black`. `overlay_image_url`
            is a transparent PNG (e.g. a character cutout) composited over the
            background with its own `overlay_motion` (`slide_in_bottom`,
            `pop`, `none`).
        voiceover_url: Optional narration track. If segment `duration`s are
            omitted, segments are auto-timed to the voiceover's sentence
            boundaries (evenly distributed when sentence and segment counts differ).
        music_url: Optional background music, looped/trimmed to length, ducked
            via `music_volume_db` and faded out over the last second.
        music_volume_db: Music attenuation in dB (default -18).
        captions: `{"mode": "burn"|"none", "style": "bold_center"|"karaoke",
            "source": "voiceover"|"segments_text"}`.
        aspect_ratio: `"9:16"` (1080x1920, default), `"1:1"` or `"16:9"`.
        job_id: Groups this output with others from the same job in R2.
        key_prefix: R2 key prefix override.

    Returns:
        Standard media envelope with the hosted video URL.
    """
    with tempfile.TemporaryDirectory() as tmp:
        out_path = await _render_slideshow(
            segments, voiceover_url, music_url, music_volume_db, captions, aspect_ratio, Path(tmp)
        )
        info = await _video_info(out_path)
        key = r2.media_key("render_slideshow", "mp4", job_id=job_id, key_prefix=key_prefix)
        url = await r2.put_bytes(out_path.read_bytes(), key)
        return r2.result(
            urls=[url],
            r2_keys=[key],
            markdown=f"## Slideshow\n\n**Video:** {url}\n\n**Duration:** {info['duration']:.1f}s\n",
            duration=info["duration"],
            width=info["width"],
            height=info["height"],
            nbytes=out_path.stat().st_size,
        )


# --------------------------------------------------------------------------
# concat_videos
# --------------------------------------------------------------------------


async def _concat_videos(
    video_urls: list[str],
    transition: str,
    music_url: Optional[str],
    music_volume_db: float,
    captions: Optional[dict],
    normalize: bool,
    tmpdir: Path,
    job_id: Optional[str] = None,
    key_prefix: Optional[str] = None,
) -> Path:
    if len(video_urls) < 1:
        raise ValueError("video_urls must not be empty")

    w, h = TARGET_SIZES["9:16"]
    input_args = []
    infos = []
    for n, url in enumerate(video_urls):
        path = tmpdir / f"clip{n}.mp4"
        await _download(url, path)
        info = await _video_info(path)
        infos.append(info)

    if not normalize and infos[0]["width"] and infos[0]["height"]:
        # Keep the source frame. Talking clips come back from reference-to-video
        # at 720p, and padding them up to 1080x1920 only softens them.
        w, h = infos[0]["width"], infos[0]["height"]

    idx = 0
    seg_labels = []
    filter_parts = []
    for n, url in enumerate(video_urls):
        path = tmpdir / f"clip{n}.mp4"
        input_args += ["-i", str(path)]
        info = infos[n]
        vin = idx
        filter_parts.append(
            f"[{vin}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={FPS},format=yuv420p[v{n}]"
        )
        if info["has_audio"]:
            filter_parts.append(f"[{vin}:a]aformat=sample_rates=44100:channel_layouts=stereo[a{n}]")
        else:
            input_args += ["-f", "lavfi", "-t", str(info["duration"] or 1), "-i", "anullsrc=r=44100:cl=stereo"]
            filter_parts.append(f"[{idx + 1}:a]anull[a{n}]")
            idx += 1
        idx += 1
        seg_labels.append(n)

    if transition == "crossfade":
        cur_v, cur_a = "v0", "a0"
        cur_dur = infos[0]["duration"]
        for n in range(1, len(seg_labels)):
            td = min(0.5, 0.4 * min(cur_dur, infos[n]["duration"]))
            offset = max(0.0, cur_dur - td)
            out_v, out_a = f"xv{n}", f"xa{n}"
            filter_parts.append(f"[{cur_v}][v{n}]xfade=transition=fade:duration={td}:offset={offset}[{out_v}]")
            filter_parts.append(f"[{cur_a}][a{n}]acrossfade=d={td}[{out_a}]")
            cur_dur = offset + infos[n]["duration"]
            cur_v, cur_a = out_v, out_a
        final_v, final_a, final_dur = cur_v, cur_a, cur_dur
    else:
        # concat wants the streams interleaved per segment ([v0][a0][v1][a1]...),
        # not all video labels followed by all audio labels.
        pairs = "".join(f"[v{n}][a{n}]" for n in seg_labels)
        filter_parts.append(f"{pairs}concat=n={len(seg_labels)}:v=1:a=1[vc][ac]")
        final_v, final_a = "vc", "ac"
        final_dur = sum(i["duration"] for i in infos)

    music_idx = None
    if music_url:
        music_path = tmpdir / "music.audio"
        await _download(music_url, music_path)
        input_args += ["-stream_loop", "-1", "-i", str(music_path)]
        music_idx = idx
        idx += 1
        fade_start = max(0.0, final_dur - 1.0)
        filter_parts.append(
            f"[{music_idx}:a]atrim=0:{final_dur},asetpts=PTS-STARTPTS,"
            f"volume={music_volume_db}dB,afade=t=out:st={fade_start}:d=1[music]"
        )
        filter_parts.append(f"[{final_a}][music]amix=inputs=2:duration=first:normalize=0[aout]")
        final_a = "aout"

    if captions and captions.get("mode") == "burn":
        style = captions.get("style", "bold_center")
        words = captions.get("words")
        srt = captions.get("srt")
        if not words and not srt:
            # Caption the first clip's own audio track as a reasonable default source.
            # xAI's /v1/stt rejects mp4 containers, so extract+upload mp3 first.
            transcript = await _transcribe_video(tmpdir / "clip0.mp4", tmpdir, job_id, key_prefix)
            words = transcript["words"]
            if not words:
                raise ValueError(
                    "the audio track has no recognisable speech, so there is nothing to caption; "
                    "pass `words` or `srt` explicitly"
                )
        if words:
            try:
                from ext.audio import ass_from_words
            except ImportError as exc:
                raise RuntimeError("burning captions needs ext/audio.py (ass_from_words), which isn't available") from exc
            ass_text = ass_from_words(words, style=style, width=w, height=h)
            ass_path = _write_text(tmpdir, "captions.ass", ass_text)
            filter_parts.append(f"[{final_v}]ass={_escape_filter_path(ass_path)}[vcap]")
            final_v = "vcap"
        elif srt:
            srt_path = _write_text(tmpdir, "captions.srt", srt)
            filter_parts.append(f"[{final_v}]subtitles={_escape_filter_path(srt_path)}[vcap]")
            final_v = "vcap"

    out_path = tmpdir / "concat.mp4"
    cmd = [
        *input_args,
        "-filter_complex", ";".join(filter_parts),
        "-map", f"[{final_v}]", "-map", f"[{final_a}]",
        "-r", str(FPS),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-color_range", "tv", "-preset", "veryfast",
        "-c:a", "aac", "-b:a", "128k",
        "-t", str(final_dur),
        "-movflags", "+faststart",
        str(out_path),
    ]
    await _run_ffmpeg(cmd)
    return out_path


async def concat_videos(
    video_urls: list[str],
    transition: str = "cut",
    music_url: Optional[str] = None,
    music_volume_db: float = -18,
    captions: Optional[dict] = None,
    normalize: bool = True,
    job_id: Optional[str] = None,
    key_prefix: Optional[str] = None,
) -> dict:
    """Stitch video clips together, keeping each clip's own audio.

    Clips of differing resolutions are scaled and padded to a common 9:16
    (1080x1920) frame, or to the first clip's frame when `normalize` is False. Each clip's dialogue/ambience audio is preserved; a
    music bed and burned captions are optional.

    Args:
        video_urls: Public URLs of the clips, in order.
        transition: `"cut"` or `"crossfade"` (video crossfade + audio crossfade).
        music_url: Optional background music, ducked via `music_volume_db`.
        music_volume_db: Music attenuation in dB (default -18).
        captions: `{"mode": "burn"|"none", "style": "bold_center"|"karaoke",
            "words": [...], "srt": "..."}`. `words`/`srt` are used directly if
            given, else transcribed from the first clip's audio.
        normalize: Scale/pad every clip to 1080x1920 (default True). Set False
            to keep the first clip's own frame size, which is what you want when
            every clip is a 720p reference-to-video talking clip.
        job_id: Groups this output with others from the same job in R2.
        key_prefix: R2 key prefix override.

    Returns:
        Standard media envelope with the hosted video URL.
    """
    with tempfile.TemporaryDirectory() as tmp:
        out_path = await _concat_videos(
            video_urls, transition, music_url, music_volume_db, captions, normalize, Path(tmp), job_id, key_prefix
        )
        info = await _video_info(out_path)
        key = r2.media_key("concat_videos", "mp4", job_id=job_id, key_prefix=key_prefix)
        url = await r2.put_bytes(out_path.read_bytes(), key)
        return r2.result(
            urls=[url],
            r2_keys=[key],
            markdown=f"## Concatenated Video\n\n**Video:** {url}\n\n**Duration:** {info['duration']:.1f}s\n",
            duration=info["duration"],
            width=info["width"],
            height=info["height"],
            nbytes=out_path.stat().st_size,
        )


# --------------------------------------------------------------------------
# trim_video
# --------------------------------------------------------------------------


async def _trim_video(video_url: str, start_s: float, end_s: float, tmpdir: Path) -> Path:
    if start_s < 0 or end_s <= start_s:
        raise ValueError(f"end_s ({end_s}) must be greater than start_s ({start_s}), and start_s must be >= 0")

    src_path = tmpdir / "src.mp4"
    await _download(video_url, src_path)
    info = await _video_info(src_path)
    if info["duration"] and start_s >= info["duration"]:
        raise ValueError(f"start_s ({start_s}) is past the end of the source ({info['duration']:.2f}s)")

    cmd = [
        "-i", str(src_path),
        "-ss", str(start_s), "-to", str(end_s),
        "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-preset", "veryfast",
    ]
    if info["has_audio"]:
        fade_start = max(start_s, end_s - 0.3)
        cmd += ["-af", f"afade=t=out:st={fade_start}:d=0.3", "-c:a", "aac", "-b:a", "128k"]
    else:
        cmd += ["-an"]

    out_path = tmpdir / "trimmed.mp4"
    cmd += ["-movflags", "+faststart", str(out_path)]
    await _run_ffmpeg(cmd)
    return out_path


async def trim_video(
    video_url: str,
    end_s: float,
    start_s: float = 0.0,
    job_id: Optional[str] = None,
    key_prefix: Optional[str] = None,
) -> dict:
    """Cut a video down to `[start_s, end_s]`.

    Built for trimming the drifting tail off a reference-to-video clip once
    the subject stops resembling the reference, but works on any clip. The
    end is trimmed with a 0.3s audio fade-out so the cut doesn't leave an
    audible click; the fade start is clamped to `start_s` for a clip shorter
    than 0.3s. Video is re-encoded (libx264, source resolution kept); a
    source with no audio track is trimmed with `-an`, no fade applied.

    Args:
        video_url: Public URL of the source clip.
        end_s: End of the trimmed range, in seconds. Must be greater than `start_s`.
        start_s: Start of the trimmed range, in seconds (default 0.0).
        job_id: Groups this output with others from the same job in R2.
        key_prefix: R2 key prefix override.

    Returns:
        Standard media envelope with the hosted video URL. `cost_usd` is 0 -
        this is ffmpeg only, no xAI call.
    """
    with tempfile.TemporaryDirectory() as tmp:
        out_path = await _trim_video(video_url, start_s, end_s, Path(tmp))
        info = await _video_info(out_path)
        key = r2.media_key("trim_video", "mp4", job_id=job_id, key_prefix=key_prefix)
        url = await r2.put_bytes(out_path.read_bytes(), key)
        return r2.result(
            urls=[url],
            r2_keys=[key],
            markdown=f"## Trimmed Video\n\n**Video:** {url}\n\n**Duration:** {info['duration']:.1f}s\n",
            duration=info["duration"],
            width=info["width"],
            height=info["height"],
            nbytes=out_path.stat().st_size,
            cost=0.0,
        )


# --------------------------------------------------------------------------
# add_captions
# --------------------------------------------------------------------------


async def _add_captions(
    video_url: str, style: str, words: Optional[list[dict]], srt: Optional[str], tmpdir: Path,
    job_id: Optional[str] = None, key_prefix: Optional[str] = None,
) -> Path:
    src_path = tmpdir / "src.mp4"
    await _download(video_url, src_path)
    info = await _video_info(src_path)

    if not words and not srt:
        # xAI's /v1/stt rejects mp4 containers, so extract+upload mp3 first.
        transcript = await _transcribe_video(src_path, tmpdir, job_id, key_prefix)
        words = transcript["words"]
        if not words:
            raise ValueError(
                "the audio track has no recognisable speech, so there is nothing to caption; "
                "pass `words` or `srt` explicitly"
            )

    if words:
        try:
            from ext.audio import ass_from_words
        except ImportError as exc:
            raise RuntimeError("add_captions needs ext/audio.py (ass_from_words), which isn't available") from exc
        ass_text = ass_from_words(words, style=style, width=info["width"], height=info["height"])
        ass_path = _write_text(tmpdir, "captions.ass", ass_text)
        vf = f"ass={_escape_filter_path(ass_path)}"
    else:
        srt_path = _write_text(tmpdir, "captions.srt", srt)
        vf = f"subtitles={_escape_filter_path(srt_path)}"

    out_path = tmpdir / "captioned.mp4"
    cmd = [
        "-i", str(src_path),
        "-vf", vf,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
    ]
    cmd += ["-c:a", "copy"] if info["has_audio"] else ["-an"]
    cmd += ["-movflags", "+faststart", str(out_path)]
    await _run_ffmpeg(cmd)
    return out_path


async def add_captions(
    video_url: str,
    style: str = "bold_center",
    words: Optional[list[dict]] = None,
    srt: Optional[str] = None,
    job_id: Optional[str] = None,
    key_prefix: Optional[str] = None,
) -> dict:
    """Burn captions onto an existing video.

    Args:
        video_url: Public URL of the source video.
        style: `"bold_center"` or `"karaoke"`.
        words: Word timings `[{"text","start","end","speaker"}]`. Used
            directly if given.
        srt: Raw SRT subtitle text. Used if `words` is not given.
        job_id: Groups this output with others from the same job in R2.
        key_prefix: R2 key prefix override.

    Returns:
        Standard media envelope with the hosted, captioned video URL. If
        neither `words` nor `srt` is given, the video's audio is transcribed
        automatically.
    """
    with tempfile.TemporaryDirectory() as tmp:
        out_path = await _add_captions(video_url, style, words, srt, Path(tmp), job_id, key_prefix)
        info = await _video_info(out_path)
        key = r2.media_key("add_captions", "mp4", job_id=job_id, key_prefix=key_prefix)
        url = await r2.put_bytes(out_path.read_bytes(), key)
        return r2.result(
            urls=[url],
            r2_keys=[key],
            markdown=f"## Captioned Video\n\n**Video:** {url}\n\n**Duration:** {info['duration']:.1f}s\n",
            duration=info["duration"],
            width=info["width"],
            height=info["height"],
            nbytes=out_path.stat().st_size,
        )


# --------------------------------------------------------------------------
# extract_frames
# --------------------------------------------------------------------------


async def _extract_frames(video_url: str, count: int, tmpdir: Path) -> list[Path]:
    src_path = tmpdir / "src.mp4"
    await _download(video_url, src_path)
    info = await _video_info(src_path)
    duration = info["duration"]
    if duration <= 0:
        raise ValueError("could not determine video duration")

    count = max(1, count)
    timestamps = [duration * i / count for i in range(count)]
    if not timestamps or abs(timestamps[-1] - duration) > 0.05:
        timestamps.append(max(0.0, duration - 0.05))

    out_paths = []
    for i, ts in enumerate(timestamps):
        out_path = tmpdir / f"frame{i:02d}.jpg"
        cmd = ["-ss", f"{ts:.3f}", "-i", str(src_path), "-frames:v", "1", "-q:v", "2", str(out_path)]
        await _run_ffmpeg(cmd, timeout=30)
        out_paths.append(out_path)
    return out_paths


async def extract_frames_local(video_url: str, count: int = 6) -> list[bytes]:
    """Extract evenly spaced frames from a video, plus the last frame, as JPEG
    bytes. Needs no credentials. Depended on by `ext.storage`'s
    `inspect_media` via a lazy import — keep this name and signature stable."""
    with tempfile.TemporaryDirectory() as tmp:
        frame_paths = await _extract_frames(video_url, count, Path(tmp))
        return [p.read_bytes() for p in frame_paths]


async def extract_frames(
    video_url: str,
    count: int = 6,
    job_id: Optional[str] = None,
    key_prefix: Optional[str] = None,
) -> dict:
    """Extract evenly spaced frames from a video, plus the last frame.

    Useful for QA review and thumbnail picking.

    Args:
        video_url: Public URL of the source video.
        count: Number of evenly spaced frames to extract (the last frame is
            always included in addition, unless already covered).
        job_id: Groups these outputs with others from the same job in R2.
        key_prefix: R2 key prefix override.

    Returns:
        Standard media envelope with the hosted frame URLs.
    """
    frames = await extract_frames_local(video_url, count)
    urls, keys, total_bytes = [], [], 0
    for i, data in enumerate(frames):
        key = r2.media_key("extract_frames", "jpg", n=i, job_id=job_id, key_prefix=key_prefix)
        urls.append(await r2.put_bytes(data, key))
        keys.append(key)
        total_bytes += len(data)
    markdown = "## Extracted Frames\n\n" + "\n".join(f"**Frame {i}:** {u}" for i, u in enumerate(urls))
    return r2.result(urls=urls, r2_keys=keys, markdown=markdown, nbytes=total_bytes)


# --------------------------------------------------------------------------
# cutout
# --------------------------------------------------------------------------


async def _cutout(image_url: str, tmpdir: Path) -> tuple[Path, tuple[int, int, int, int]]:
    # Lazy import: rembg is a heavy optional dependency (only installed in the
    # deploy image), keeping it out of module import time.
    from PIL import Image
    from rembg import new_session, remove

    src_bytes = await r2.fetch(image_url)
    session = new_session("isnet-general-use")
    cut_bytes = remove(src_bytes, session=session, post_process_mask=True)

    img = Image.open(io.BytesIO(cut_bytes)).convert("RGBA")
    bbox = img.getchannel("A").getbbox()
    if bbox:
        img = img.crop(bbox)
    else:
        bbox = (0, 0, img.width, img.height)

    out_path = tmpdir / "cutout.png"
    img.save(out_path, "PNG")
    return out_path, bbox


async def cutout(
    image_url: str,
    job_id: Optional[str] = None,
    key_prefix: Optional[str] = None,
) -> dict:
    """Remove the background from an image, keeping only the main subject.

    Uses rembg (isnet-general-use) and crops to the subject's alpha bounding
    box. Produces a transparent PNG suitable as a character cutout for
    `render_slideshow`'s `overlay_image_url`.

    Args:
        image_url: Public URL of the source image.
        job_id: Groups this output with others from the same job in R2.
        key_prefix: R2 key prefix override.

    Returns:
        Standard media envelope with the hosted transparent PNG URL and `bbox`
        (the crop box `[left, top, right, bottom]` in the source image).
    """
    with tempfile.TemporaryDirectory() as tmp:
        out_path, bbox = await _cutout(image_url, Path(tmp))
        key = r2.media_key("cutout", "png", job_id=job_id, key_prefix=key_prefix)
        url = await r2.put_bytes(out_path.read_bytes(), key)
        from PIL import Image
        img = Image.open(out_path)
        return r2.result(
            urls=[url],
            r2_keys=[key],
            markdown=f"## Cutout\n\n**Image:** {url}\n",
            width=img.width,
            height=img.height,
            nbytes=out_path.stat().st_size,
            bbox=list(bbox),
        )


# --------------------------------------------------------------------------
# compose_cover
# --------------------------------------------------------------------------

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVu-Sans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]


def _find_font(size: int):
    from PIL import ImageFont

    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


_WEIGHT_RANK = {"Regular": 400, "Medium": 500, "Semibold": 600, "Bold": 700, "Heavy": 800}

# Font selection for the rich layout (compose_cover's eyebrow/title/subtitle/
# note/footer slots). Each family is tried in turn; the first one with a
# usable face wins. A "variable" entry is a single variable font whose named
# instances cover our weights directly (e.g. macOS's San Francisco, used
# when testing locally). Otherwise the dict maps the weights actually
# shipped as separate static files - Linux only ships Regular/Bold for these
# families - and the shipped weight nearest the requested one is used.
# Inter and JetBrains Mono ship the full weight range and are installed by
# the Dockerfile, so they come first on Linux; the Liberation/DejaVu entries
# below are the fallback when only the base font packages are present.
_SANS_FAMILIES = [
    {"variable": "/System/Library/Fonts/SFNS.ttf"},
    {
        "Regular": "/usr/share/fonts/opentype/inter/Inter-Regular.otf",
        "Medium": "/usr/share/fonts/opentype/inter/Inter-Medium.otf",
        "Semibold": "/usr/share/fonts/opentype/inter/Inter-SemiBold.otf",
        "Bold": "/usr/share/fonts/opentype/inter/InterDisplay-Bold.otf",
        "Heavy": "/usr/share/fonts/opentype/inter/InterDisplay-ExtraBold.otf",
    },
    {
        "Regular": "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "Bold": "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    },
    {
        "Regular": "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "Bold": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    },
    {
        "Regular": "/System/Library/Fonts/Supplemental/Arial.ttf",
        "Bold": "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    },
]

_MONO_FAMILIES = [
    {"variable": "/System/Library/Fonts/SFNSMono.ttf"},
    {
        "Regular": "/usr/share/fonts/truetype/jetbrains-mono/JetBrainsMono-Regular.ttf",
        "Medium": "/usr/share/fonts/truetype/jetbrains-mono/JetBrainsMono-Medium.ttf",
        "Semibold": "/usr/share/fonts/truetype/jetbrains-mono/JetBrainsMono-SemiBold.ttf",
        "Bold": "/usr/share/fonts/truetype/jetbrains-mono/JetBrainsMono-Bold.ttf",
        "Heavy": "/usr/share/fonts/truetype/jetbrains-mono/JetBrainsMono-ExtraBold.ttf",
    },
    {
        "Regular": "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "Bold": "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    },
    {
        "Regular": "/usr/share/fonts/truetype/liberation2/LiberationMono-Regular.ttf",
        "Bold": "/usr/share/fonts/truetype/liberation2/LiberationMono-Bold.ttf",
    },
]


def _find_weighted_font(weight: str, size: int, mono: bool = False):
    """Resolve a font by logical weight (Regular/Medium/Semibold/Bold/Heavy),
    falling back to the nearest available face. Tries each family in order
    and never raises - degrades to PIL's built-in font if nothing exists."""
    from PIL import ImageFont

    target = _WEIGHT_RANK.get(weight, _WEIGHT_RANK["Regular"])
    for family in _MONO_FAMILIES if mono else _SANS_FAMILIES:
        variable_path = family.get("variable")
        if variable_path:
            if not Path(variable_path).exists():
                continue
            try:
                font = ImageFont.truetype(variable_path, size)
                names = [n.decode() if isinstance(n, bytes) else n for n in font.get_variation_names()]
                ranked = [n for n in names if n in _WEIGHT_RANK]
                pick = min(ranked, key=lambda n: abs(_WEIGHT_RANK[n] - target))
                font.set_variation_by_name(pick)
                return font
            except Exception:
                continue

        candidates = [(abs(_WEIGHT_RANK.get(wname, 400) - target), path) for wname, path in family.items() if Path(path).exists()]
        if not candidates:
            continue
        try:
            return ImageFont.truetype(min(candidates)[1], size)
        except Exception:
            continue

    return ImageFont.load_default(size=size)


def _wrap_text(draw, text: str, font, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        box = draw.textbbox((0, 0), candidate, font=font)
        if box[2] - box[0] <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _note_lines(note) -> list[str]:
    """Normalize `note` into a line list: a list of strings is used as-is; a
    string is split on newlines. Never word-wrapped - the caller controls
    where lines break."""
    if not note:
        return []
    if isinstance(note, str):
        return note.split("\n")
    return list(note)


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    h = color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _tracked_width(draw, text: str, font, track: float) -> float:
    """Total width `text` would occupy if drawn by `_draw_tracked` with the
    same track. PIL has no letter-spacing primitive, so this sums per-glyph
    advances the same way the draw call steps through them."""
    if not text:
        return 0.0
    return sum(draw.textlength(ch, font=font) for ch in text) + track * (len(text) - 1)


def _draw_tracked(draw, xy, text: str, font, fill, track: float) -> None:
    """Draw `text` with manual letter spacing: PIL has no tracking, so each
    character is drawn separately and advanced by textlength(ch) + track."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + track


def _apply_scrim(img, mode: str, w: int, h: int):
    """Darken the background so type stays legible. 'band' is the original
    hard rectangle; 'left'/'top' fade a blurred dark gradient in from that
    edge instead, so text clears the image without a visible rectangle."""
    from PIL import Image, ImageDraw, ImageFilter

    if mode == "none":
        return img
    if mode == "band":
        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        band_top = max(TOP_SAFE, int(h * 0.35))
        band_bottom = h - BOTTOM_SAFE
        odraw.rectangle([0, band_top, w, band_bottom], fill=(0, 0, 0, 110))
        return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

    # left / top: soft blurred gradient instead of a hard-edged band
    mask = Image.new("L", (w, h), 0)
    mdraw = ImageDraw.Draw(mask)
    if mode == "left":
        fade = max(1, int(w * 0.62))
        for x in range(min(w, fade + 1)):
            mdraw.line([(x, 0), (x, h)], fill=max(0, int(190 * (1 - x / fade))))
    else:  # "top"
        fade = max(1, int(h * 0.55))
        for y in range(min(h, fade + 1)):
            mdraw.line([(0, y), (w, y)], fill=max(0, int(180 * (1 - y / fade))))
    blur_radius = max(8, int(w * 0.014))
    mask = mask.filter(ImageFilter.GaussianBlur(blur_radius))
    dark = Image.new("RGB", (w, h), (6, 6, 9))
    return Image.composite(dark, img.convert("RGB"), mask)


async def _compose_cover_async(
    background_url: Optional[str], title: str, subtitle: Optional[str], style: str, w: int, h: int, tmpdir: Path
) -> Path:
    from PIL import Image, ImageDraw

    if background_url:
        bg_path = tmpdir / "bg.img"
        await _download(background_url, bg_path)
        img = Image.open(bg_path).convert("RGB")
        # cover-fit to WxH
        src_ratio = img.width / img.height
        dst_ratio = w / h
        if src_ratio > dst_ratio:
            new_h = h
            new_w = int(h * src_ratio)
        else:
            new_w = w
            new_h = int(w / src_ratio)
        img = img.resize((new_w, new_h))
        left = (new_w - w) // 2
        top = (new_h - h) // 2
        img = img.crop((left, top, left + w, top + h))
    else:
        img = Image.new("RGB", (w, h), (20, 20, 24))

    draw = ImageDraw.Draw(img, "RGBA")

    # Darken for legibility under the safe zone where text sits.
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    band_top = max(TOP_SAFE, int(h * 0.35))
    band_bottom = h - BOTTOM_SAFE
    odraw.rectangle([0, band_top, w, band_bottom], fill=(0, 0, 0, 110))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    max_text_width = int(w * 0.86)
    title_size = max(48, int(w * 0.09))
    title_font = _find_font(title_size)
    title_lines = _wrap_text(draw, title, title_font, max_text_width)

    subtitle_lines = []
    subtitle_font = None
    if subtitle:
        subtitle_size = max(28, int(w * 0.045))
        subtitle_font = _find_font(subtitle_size)
        subtitle_lines = _wrap_text(draw, subtitle, subtitle_font, max_text_width)

    line_gap = int(title_size * 0.18)
    title_line_h = title_font.getbbox("Ag")[3] + line_gap
    sub_line_h = (subtitle_font.getbbox("Ag")[3] + line_gap) if subtitle_font else 0

    block_h = title_line_h * len(title_lines) + sub_line_h * len(subtitle_lines) + (20 if subtitle_lines else 0)
    safe_top = TOP_SAFE
    safe_bottom = h - BOTTOM_SAFE
    y = safe_top + max(0, (safe_bottom - safe_top - block_h) // 2)

    for line in title_lines:
        box = draw.textbbox((0, 0), line, font=title_font)
        x = (w - (box[2] - box[0])) // 2
        draw.text((x, y), line, font=title_font, fill=(255, 255, 255), stroke_width=3, stroke_fill=(0, 0, 0))
        y += title_line_h

    if subtitle_lines:
        y += 20
        for line in subtitle_lines:
            box = draw.textbbox((0, 0), line, font=subtitle_font)
            x = (w - (box[2] - box[0])) // 2
            draw.text((x, y), line, font=subtitle_font, fill=(230, 230, 230), stroke_width=2, stroke_fill=(0, 0, 0))
            y += sub_line_h

    out_path = tmpdir / "cover.png"
    img.save(out_path, "PNG")
    return out_path


# Hero size as a fraction of canvas width. A wide 16:9 frame is much shorter
# than it is wide, so the same fraction used for a tall frame would overflow
# vertically; 1:1 is interpolated between the two.
_HERO_SCALE = {"16:9": 0.10, "1:1": 0.14, "9:16": 0.18}


def _render_rich_cover(
    img,
    title: str,
    eyebrow: Optional[str],
    subtitle: Optional[str],
    note,
    footer: Optional[str],
    rule: bool,
    align: str,
    accent: str,
    scrim: str,
    aspect_ratio: str,
):
    """Pure four-slot editorial layout: eyebrow / hero title / subtitle /
    note, plus an optional accent rule and footer. Draws onto `img` (already
    the target size) and returns the composed image. No I/O, no network, no
    R2 - every size scales off `img`'s width, so this works at any resolution.
    """
    from PIL import ImageDraw

    w, h = img.size
    img = _apply_scrim(img, scrim, w, h)
    draw = ImageDraw.Draw(img)

    accent_rgb = _hex_to_rgb(accent)
    margin = int(w * 0.08)
    # TOP_SAFE/BOTTOM_SAFE are absolute pixels tuned for a 1080x1920 frame, so
    # on a short canvas an unscaled 400px bottom zone eats a third of the height
    # and pushes the footer into the middle of the frame. Scale them by height.
    safe_scale = h / 1920
    top_safe = int(TOP_SAFE * safe_scale)
    bottom_safe = int(BOTTOM_SAFE * safe_scale)
    safe_top = top_safe
    max_text_width = w - 2 * margin

    eyebrow_size = max(14, int(w * 0.013))
    hero_size = max(40, int(w * _HERO_SCALE.get(aspect_ratio, 0.14)))
    subtitle_size = max(24, int(w * 0.033))
    note_size = max(16, int(w * 0.021))
    footer_size = eyebrow_size
    footer_gap = int(footer_size * 0.8)
    # Reserve the footer's own strip out of the safe region *before* the
    # flow block is centered in it, so a tall note can't grow into the
    # footer - they'd otherwise both gravitate toward the same bottom edge.
    safe_bottom = h - bottom_safe - (footer_size + footer_gap if footer else 0)

    f_eyebrow = _find_weighted_font("Semibold", eyebrow_size, mono=True)
    f_hero = _find_weighted_font("Heavy", hero_size)
    f_subtitle = _find_weighted_font("Semibold", subtitle_size)
    f_note = _find_weighted_font("Regular", note_size)
    f_footer = _find_weighted_font("Medium", footer_size, mono=True)

    title_lines = _wrap_text(draw, title, f_hero, max_text_width)
    subtitle_lines = _wrap_text(draw, subtitle, f_subtitle, max_text_width) if subtitle else []
    note_lines = _note_lines(note)

    hero_line_h = f_hero.getbbox("Ag")[3] + int(hero_size * 0.18)
    subtitle_line_h = f_subtitle.getbbox("Ag")[3] + int(subtitle_size * 0.18)
    note_line_h = int(note_size * 1.4)
    gap_eyebrow = int(eyebrow_size * 1.6)
    gap_hero = int(hero_size * 0.06)
    gap_rule = int(subtitle_size * 0.6)
    rule_w = max(4, int(w * 0.055))
    show_rule = rule and bool(subtitle_lines) and bool(note_lines)

    block_h = hero_line_h * len(title_lines) + gap_hero
    if eyebrow:
        block_h += eyebrow_size + gap_eyebrow
    if subtitle_lines:
        block_h += subtitle_line_h * len(subtitle_lines)
    if show_rule:
        block_h += gap_rule + 4 + gap_rule
    if note_lines:
        block_h += note_line_h * len(note_lines)

    y = safe_top + max(0, (safe_bottom - safe_top - block_h) // 2)
    eyebrow_track = eyebrow_size * 0.25
    hero_track = -hero_size * 0.02
    footer_track = footer_size * 0.25

    if eyebrow:
        tw = _tracked_width(draw, eyebrow, f_eyebrow, eyebrow_track)
        x = margin if align == "left" else (w - int(tw)) // 2
        _draw_tracked(draw, (x, y), eyebrow, f_eyebrow, accent_rgb, eyebrow_track)
        y += eyebrow_size + gap_eyebrow

    for line in title_lines:
        tw = _tracked_width(draw, line, f_hero, hero_track)
        x = margin if align == "left" else (w - int(tw)) // 2
        _draw_tracked(draw, (x, y), line, f_hero, (255, 255, 255), hero_track)
        y += hero_line_h
    y += gap_hero

    for line in subtitle_lines:
        box = draw.textbbox((0, 0), line, font=f_subtitle)
        x = margin if align == "left" else (w - (box[2] - box[0])) // 2
        draw.text((x, y), line, font=f_subtitle, fill=(255, 255, 255))
        y += subtitle_line_h

    if show_rule:
        y += gap_rule
        x = margin if align == "left" else (w - rule_w) // 2
        draw.rectangle([x, y, x + rule_w, y + 4], fill=accent_rgb)
        y += 4 + gap_rule

    for line in note_lines:
        box = draw.textbbox((0, 0), line, font=f_note)
        x = margin if align == "left" else (w - (box[2] - box[0])) // 2
        draw.text((x, y), line, font=f_note, fill=(154, 160, 172))
        y += note_line_h

    if footer:
        tw = _tracked_width(draw, footer, f_footer, footer_track)
        x = margin if align == "left" else (w - int(tw)) // 2
        fy = h - bottom_safe - footer_size
        _draw_tracked(draw, (x, fy), footer, f_footer, (120, 126, 140), footer_track)

    return img


async def _compose_cover_rich_async(
    background_url: Optional[str],
    title: str,
    eyebrow: Optional[str],
    subtitle: Optional[str],
    note,
    footer: Optional[str],
    rule: bool,
    align: str,
    accent: str,
    scrim: str,
    aspect_ratio: str,
    w: int,
    h: int,
    tmpdir: Path,
) -> Path:
    from PIL import Image

    if background_url:
        bg_path = tmpdir / "bg.img"
        await _download(background_url, bg_path)
        img = Image.open(bg_path).convert("RGB")
        # cover-fit to WxH
        src_ratio = img.width / img.height
        dst_ratio = w / h
        if src_ratio > dst_ratio:
            new_h = h
            new_w = int(h * src_ratio)
        else:
            new_w = w
            new_h = int(w / src_ratio)
        img = img.resize((new_w, new_h))
        left = (new_w - w) // 2
        top = (new_h - h) // 2
        img = img.crop((left, top, left + w, top + h))
    else:
        img = Image.new("RGB", (w, h), (20, 20, 24))

    img = _render_rich_cover(img, title, eyebrow, subtitle, note, footer, rule, align, accent, scrim, aspect_ratio)

    out_path = tmpdir / "cover.png"
    img.save(out_path, "PNG")
    return out_path


async def compose_cover(
    background_url: Optional[str],
    title: str,
    subtitle: Optional[str] = None,
    style: str = "bold_center",
    aspect_ratio: str = "9:16",
    job_id: Optional[str] = None,
    key_prefix: Optional[str] = None,
    eyebrow: Optional[str] = None,
    note: Optional[str | list[str]] = None,
    footer: Optional[str] = None,
    rule: bool = False,
    align: str = "center",
    accent: str = "#A78BFA",
    scrim: str = "band",
) -> dict:
    """Compose a thumbnail/cover image with real, server-side editorial text.

    Two layouts share this one tool:

    - Legacy (default): centered, word-wrapped `title` + `subtitle` over a
      dark band, unchanged from before. Used whenever none of the rich-layout
      arguments below are touched.
    - Rich: a four-slot editorial block - `eyebrow` / hero `title` /
      `subtitle` / `note` - with weighted fonts, manual letter spacing, an
      optional accent rule and a footer line. Activated automatically as
      soon as any of `eyebrow`, `note`, `footer`, `rule`, `align`, `accent`
      or `scrim` is set to something other than its default.

    Uses Pillow for real text metrics (wrapping, centering, letter spacing)
    rather than ffmpeg drawtext. Text is kept clear of the top 250px and
    bottom 400px safe zones on a 1080x1920 frame (scaled proportionally for
    other aspect ratios).

    Args:
        background_url: Optional public image URL, cover-fit to the frame.
            A solid dark background is used if omitted.
        title: Main title text (wrapped). Also the rich layout's hero line.
        subtitle: Optional smaller text below the title.
        style: Reserved for future style variants (currently unused beyond `bold_center`).
        aspect_ratio: `"9:16"` (1080x1920, default), `"1:1"` or `"16:9"`.
        job_id: Groups this output with others from the same job in R2.
        key_prefix: R2 key prefix override.
        eyebrow: Small, letter-spaced, accent-colored label above the title.
        note: Muted small print drawn below the rule. A list of lines or a
            string with `\\n` - never auto-wrapped, so the caller controls
            where it breaks.
        footer: Small mono line (slide index, domain) pinned above the
            bottom safe zone.
        rule: Draw a thin accent-colored rule between the subtitle and note
            (only drawn when both are present).
        align: `"left"` or `"center"` (default).
        accent: Accent hex color for the eyebrow, rule and footer.
        scrim: `"band"` (default, today's hard rectangle), `"left"` or
            `"top"` (soft blurred gradient from that edge) or `"none"`.

    Returns:
        Standard media envelope with the hosted PNG cover URL.
    """
    w, h = _target_size(aspect_ratio)
    rich = any(
        [
            eyebrow is not None,
            note is not None,
            footer is not None,
            rule,
            align != "center",
            scrim != "band",
            accent != "#A78BFA",
        ]
    )
    with tempfile.TemporaryDirectory() as tmp:
        if rich:
            out_path = await _compose_cover_rich_async(
                background_url, title, eyebrow, subtitle, note, footer, rule, align, accent, scrim, aspect_ratio, w, h, Path(tmp)
            )
        else:
            out_path = await _compose_cover_async(background_url, title, subtitle, style, w, h, Path(tmp))
        from PIL import Image
        img = Image.open(out_path)
        key = r2.media_key("compose_cover", "png", job_id=job_id, key_prefix=key_prefix)
        url = await r2.put_bytes(out_path.read_bytes(), key)
        return r2.result(
            urls=[url],
            r2_keys=[key],
            markdown=f"## Cover\n\n**Image:** {url}\n",
            width=img.width,
            height=img.height,
            nbytes=out_path.stat().st_size,
        )


# --------------------------------------------------------------------------


def register(mcp):
    mcp.tool()(render_slideshow)
    mcp.tool()(concat_videos)
    mcp.tool()(trim_video)
    mcp.tool()(add_captions)
    mcp.tool()(extract_frames)
    mcp.tool()(cutout)
    mcp.tool()(compose_cover)
