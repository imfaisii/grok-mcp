"""Text-to-speech, speech-to-text, and caption generation.

Talks to xAI's TTS (`POST /v1/tts`) and STT (`POST /v1/stt`) REST endpoints
directly with httpx — the docs show no `xai_sdk` examples for either, only
raw REST. Long TTS text is split on sentence boundaries and stitched back
together with ffmpeg, since the API caps `text` at 15,000 characters.
Captions are built from STT word timestamps into SRT and ASS (Advanced
SubStation Alpha) text, with per-style ASS headers loaded from
`caption_styles/*.ass` so a new style is just a new file.
"""

import asyncio
import base64
import re
import subprocess
import tempfile
import uuid
from pathlib import Path

import httpx

import r2
from src.utils import XAI_API_KEY

API_BASE = "https://api.x.ai/v1"
TTS_MAX_CHARS = 15000
FFMPEG_TIMEOUT = 120

CAPTION_STYLES_DIR = Path(__file__).parent / "caption_styles"


def _headers() -> dict:
    return {"Authorization": f"Bearer {XAI_API_KEY}"}


# ---------------------------------------------------------------------------
# Text to speech
# ---------------------------------------------------------------------------

def _split_sentences(text: str, max_chars: int = TTS_MAX_CHARS) -> list[str]:
    """Split text into chunks under `max_chars`, breaking only at sentence ends."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    chunks = []
    current = ""
    for sentence in sentences:
        candidate = f"{current} {sentence}".strip() if current else sentence
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


async def _synthesize(text: str, voice_id: str, language: str, speed: float, output_format: dict) -> bytes:
    """One `POST /v1/tts` call. Returns raw audio bytes.

    The REST reference documents a JSON envelope (`{"audio": "<base64>", ...}`)
    but the quickstart examples pipe raw bytes straight to a file, and the
    docs' own character-timestamps section confirms both are real: plain
    calls get raw bytes, `with_timestamps=true` gets the JSON envelope. This
    checks the response `content-type` rather than assuming either shape.
    """
    body = {
        "text": text,
        "voice_id": voice_id,
        "language": language,
        "speed": speed,
        "output_format": output_format,
    }
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(f"{API_BASE}/tts", headers=_headers(), json=body)
        response.raise_for_status()
        if "application/json" in response.headers.get("content-type", ""):
            return base64.b64decode(response.json()["audio"])
        return response.content


async def _run_ffmpeg(args: list[str], cwd: Path) -> None:
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=FFMPEG_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("ffmpeg timed out")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {stderr.decode(errors='replace')[-500:]}")


async def _concat_audio(chunks: list[bytes], ext: str) -> bytes:
    """Stitch same-codec audio chunks together with ffmpeg's concat demuxer."""
    if len(chunks) == 1:
        return chunks[0]
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        list_lines = []
        for i, data in enumerate(chunks):
            part = tmp_path / f"part-{i}.{ext}"
            part.write_bytes(data)
            list_lines.append(f"file '{part.name}'")
        list_file = tmp_path / "list.txt"
        list_file.write_text("\n".join(list_lines))
        out_path = tmp_path / f"out.{ext}"
        await _run_ffmpeg(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(out_path)],
            cwd=tmp_path,
        )
        return out_path.read_bytes()


async def _probe_duration(data: bytes, ext: str) -> float | None:
    """ffprobe the synthesized audio's duration, since a plain TTS call carries none."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"audio.{ext}"
        path.write_bytes(data)
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            return None
        if proc.returncode != 0:
            return None
        try:
            return round(float(stdout.decode().strip()), 2)
        except ValueError:
            return None


# ---------------------------------------------------------------------------
# Speech to text
# ---------------------------------------------------------------------------

async def _stt_call(audio_url: str, language: str | None = None, diarize: bool = False, keyterms: list[str] | None = None) -> dict:
    """One `POST /v1/stt` call against a hosted URL. Returns the raw JSON payload."""
    fields = [("url", audio_url)]
    if language:
        fields.append(("language", language))
    if diarize:
        fields.append(("diarize", "true"))
    for term in keyterms or []:
        fields.append(("keyterm", term))
    # The endpoint requires multipart/form-data even with no file attached.
    # httpx only emits multipart when parts go through `files`, so text
    # fields are passed as (name, (None, value)) tuples to force that.
    files = [(name, (None, value)) for name, value in fields]
    async with httpx.AsyncClient(timeout=300) as client:
        response = await client.post(f"{API_BASE}/stt", headers=_headers(), files=files)
        response.raise_for_status()
        return response.json()


def _format_stt(payload: dict) -> dict:
    words = [
        {"text": w["text"], "start": w["start"], "end": w["end"], "speaker": w.get("speaker")}
        for w in payload.get("words", [])
    ]
    return {
        "text": payload.get("text", ""),
        "language": payload.get("language"),
        "duration": payload.get("duration"),
        "words": words,
    }


async def stt_words(audio_url: str, language: str | None = None) -> dict:
    """Transcribe audio and return the transcript with word-level timestamps.

    Args:
        audio_url: Public URL of the audio file to transcribe.
        language: Optional BCP-47 language code (e.g. `en`) for text formatting.

    Returns:
        {"text", "language", "duration", "words": [{"text","start","end","speaker"}]}
    """
    payload = await _stt_call(audio_url, language=language)
    return _format_stt(payload)


# ---------------------------------------------------------------------------
# Captions
# ---------------------------------------------------------------------------

def _group_words(words: list, max_chars: int, max_words: int) -> list:
    """Group words into caption lines, breaking on `max_chars` or `max_words`."""
    groups: list = []
    current: list = []
    current_len = 0
    for word in words:
        added = len(word["text"]) + (1 if current else 0)
        if current and (current_len + added > max_chars or len(current) >= max_words):
            groups.append(current)
            current = []
            current_len = 0
            added = len(word["text"])
        current.append(word)
        current_len += added
    if current:
        groups.append(current)
    return groups


def _srt_timestamp(seconds: float) -> str:
    ms = round(seconds * 1000)
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    secs, ms = divmod(ms, 1_000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def srt_from_words(words: list, max_chars: int = 24, max_words: int = 4) -> str:
    """Build an SRT subtitle file from STT word timestamps.

    Args:
        words: List of `{"text","start","end"}` dicts in seconds, as from `stt_words`.
        max_chars: Max characters per caption line before it breaks.
        max_words: Max words per caption line before it breaks.

    Returns:
        SRT text: sequential index, `HH:MM:SS,mmm --> HH:MM:SS,mmm`, line text, blank line.
    """
    groups = _group_words(words, max_chars, max_words)
    lines = []
    for i, group in enumerate(groups, start=1):
        lines.append(str(i))
        lines.append(f"{_srt_timestamp(group[0]['start'])} --> {_srt_timestamp(group[-1]['end'])}")
        lines.append(" ".join(w["text"] for w in group))
        lines.append("")
    return "\n".join(lines)


def _ass_timestamp(seconds: float) -> str:
    cs = round(seconds * 100)
    hours, cs = divmod(cs, 360_000)
    minutes, cs = divmod(cs, 6_000)
    secs, cs = divmod(cs, 100)
    return f"{hours:d}:{minutes:02d}:{secs:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    """ASS override tags start with `{`; keep caption text from being read as one."""
    return text.replace("\\", "∖").replace("{", "(").replace("}", ")")


def ass_from_words(words: list, style: str = "bold_center", width: int = 1080, height: int = 1920) -> str:
    """Build an ASS (Advanced SubStation Alpha) caption file from STT word timestamps.

    Loads the style's header (script info, styling, safe-zone margins) from
    `caption_styles/{style}.ass` and appends generated `Dialogue` lines, so
    adding a style is a new template file, not a code change.

    Args:
        words: List of `{"text","start","end"}` dicts in seconds, as from `stt_words`.
        style: Template name under `caption_styles/` (`bold_center` or `karaoke`).
        width: Video width in pixels, written into the template's `PlayResX`.
        height: Video height in pixels, written into the template's `PlayResY`.

    Returns:
        Full `.ass` file text, ready to pass to ffmpeg's `ass=` filter.
    """
    template_path = CAPTION_STYLES_DIR / f"{style}.ass"
    if not template_path.exists():
        raise ValueError(f"unknown caption style: {style}")
    header = template_path.read_text().format(width=width, height=height)

    groups = _group_words(words, max_chars=24, max_words=4)
    lines = []
    for group in groups:
        start = _ass_timestamp(group[0]["start"])
        end = _ass_timestamp(group[-1]["end"])
        if style == "karaoke":
            text = "".join(
                f"{{\\kf{max(1, round((w['end'] - w['start']) * 100))}}}{_ass_escape(w['text'])} "
                for w in group
            ).rstrip()
        else:
            text = _ass_escape(" ".join(w["text"] for w in group))
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")
    return header + "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def register(mcp):
    @mcp.tool()
    async def tts(
        text: str,
        voice_id: str = "eve",
        language: str = "en",
        speed: float = 1.0,
        codec: str = "mp3",
        job_id: str | None = None,
        key_prefix: str | None = None,
    ) -> dict:
        """Convert text to speech with xAI's TTS API.

        Text over 15,000 characters (the API's per-call limit) is split on
        sentence boundaries, synthesized chunk by chunk, and stitched back
        together with ffmpeg — never split mid-sentence.

        Args:
            text: Text to speak. Long text is chunked automatically.
            voice_id: Voice to use, case-insensitive. Defaults to `eve`.
            language: BCP-47 language code, or `auto` to detect. Defaults to `en`.
            speed: Speech speed multiplier, 0.7-1.5. Defaults to 1.0.
            codec: Output audio codec: `mp3`, `wav`, `pcm`, `mulaw`, or `alaw`. Defaults to `mp3`.
            job_id: Optional job id to group this output with others under one R2 prefix.
            key_prefix: Optional R2 key prefix override.

        Returns:
            The standard media envelope (`urls`, `r2_keys`, `markdown`, `duration`, ...)
            plus `characters` (length of the input text).
        """
        output_format = {"codec": codec}
        chunks_text = _split_sentences(text) if len(text) > TTS_MAX_CHARS else [text]
        audio_chunks = [await _synthesize(chunk, voice_id, language, speed, output_format) for chunk in chunks_text]
        audio = await _concat_audio(audio_chunks, codec)
        duration = await _probe_duration(audio, codec)

        key = r2.media_key("tts", codec, job_id=job_id, key_prefix=key_prefix)
        url = await r2.put_bytes(audio, key)

        return r2.result(
            urls=[url],
            r2_keys=[key],
            markdown=f"**Audio:** {url}",
            duration=duration,
            nbytes=len(audio),
            characters=len(text),
        )

    @mcp.tool()
    async def stt(
        audio_url: str,
        language: str | None = None,
        diarize: bool = False,
        keyterms: list[str] | None = None,
    ) -> dict:
        """Transcribe an audio file with xAI's STT API.

        Word-level timestamps come back on every request. `diarize` adds a
        per-word speaker id; `keyterms` biases transcription toward given
        vocabulary (product names, proper nouns).

        Args:
            audio_url: Public URL of the audio file to transcribe.
            language: Optional BCP-47 language code, enables number/currency formatting.
            diarize: Tag each word with a speaker index. Defaults to False.
            keyterms: Up to 100 vocabulary terms (each <=50 chars) to bias transcription toward.

        Returns:
            {"text", "language", "duration", "words": [{"text","start","end","speaker"}]}
        """
        payload = await _stt_call(audio_url, language=language, diarize=diarize, keyterms=keyterms)
        return _format_stt(payload)

    @mcp.tool()
    async def make_captions(
        audio_url: str,
        style: str = "bold_center",
        job_id: str | None = None,
        key_prefix: str | None = None,
    ) -> dict:
        """Transcribe audio and generate SRT and ASS caption files.

        Runs STT for word timestamps, builds an SRT subtitle file and a
        styled ASS file (`bold_center` or `karaoke`), and uploads both to R2.

        Args:
            audio_url: Public URL of the audio file to caption.
            style: Caption style — a file name under `caption_styles/` (`bold_center` or `karaoke`).
            job_id: Optional job id to group both outputs under one R2 prefix. Generated when omitted.
            key_prefix: Optional R2 key prefix override.

        Returns:
            The standard media envelope plus `srt`/`ass` (the raw text) and
            `srt_url`/`ass_url` (also present in `urls`/`r2_keys`).
        """
        job_id = job_id or str(uuid.uuid4())
        transcript = await stt_words(audio_url)
        words = transcript["words"]
        srt_text = srt_from_words(words)
        ass_text = ass_from_words(words, style=style)

        srt_key = r2.media_key("caption", "srt", job_id=job_id, key_prefix=key_prefix)
        ass_key = r2.media_key("caption", "ass", n=1, job_id=job_id, key_prefix=key_prefix)
        srt_url = await r2.put_bytes(srt_text.encode("utf-8"), srt_key)
        ass_url = await r2.put_bytes(ass_text.encode("utf-8"), ass_key)

        return r2.result(
            urls=[srt_url, ass_url],
            r2_keys=[srt_key, ass_key],
            markdown=f"**SRT:** {srt_url}\n**ASS:** {ass_url}",
            duration=transcript["duration"],
            srt=srt_text,
            ass=ass_text,
            srt_url=srt_url,
            ass_url=ass_url,
        )
