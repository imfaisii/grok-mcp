import os
import json
import base64
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(".env" if Path(".env").exists() else "example.env")

XAI_API_KEY = os.getenv("XAI_API_KEY", "")


def encode_image_to_base64(image_path: str):
    path = Path(image_path)
    if not path.exists():
        raise FileNotFoundError(f"Image file not found: {image_path}")
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode("utf-8")


def encode_video_to_base64(video_path: str):
    path = Path(video_path)
    if not path.exists():
        raise FileNotFoundError(f"Video file not found: {video_path}")
    with open(video_path, "rb") as video_file:
        return base64.b64encode(video_file.read()).decode("utf-8")


def usage_footer(*responses):
    prompt_tokens = completion_tokens = reasoning_tokens = 0
    cost = 0.0
    has_cost = False
    for response in responses:
        usage = response.usage
        if usage:
            prompt_tokens += usage.prompt_tokens
            completion_tokens += usage.completion_tokens
            reasoning_tokens += usage.reasoning_tokens
        if response.cost_usd is not None:
            cost += response.cost_usd
            has_cost = True

    parts = []
    if prompt_tokens or completion_tokens:
        tokens = f"**Tokens:** {prompt_tokens:,} in / {completion_tokens:,} out"
        if reasoning_tokens:
            tokens += f" ({reasoning_tokens:,} reasoning)"
        parts.append(tokens)
    if has_cost:
        parts.append(f"**Cost:** ${cost:.4f}")
    if not parts:
        return ""
    return "\n\n---\n" + " · ".join(parts)

# Chat history lives in a private R2 bucket when R2_CHATS_BUCKET is set, so it
# survives container restarts (the Cloudflare container disk is ephemeral), and
# under ./chats otherwise. Never the public media bucket: r2.dev serves it to anyone.
CHATS_BUCKET = os.getenv("R2_CHATS_BUCKET")


def _chat_s3():
    import r2
    return r2._s3() if CHATS_BUCKET and r2.configured() else None


def load_history(session: str):
    s3 = _chat_s3()
    if s3:
        try:
            body = s3.get_object(Bucket=CHATS_BUCKET, Key=f"chats/{session}.json")["Body"].read()
        except s3.exceptions.NoSuchKey:
            return []
        return json.loads(body)
    path = Path("chats") / f"{session}.json"
    if path.exists():
        return json.loads(path.read_text())
    return []


def save_history(session: str, history: list):
    data = json.dumps(history, indent=2, ensure_ascii=False)
    s3 = _chat_s3()
    if s3:
        s3.put_object(Bucket=CHATS_BUCKET, Key=f"chats/{session}.json", Body=data.encode(), ContentType="application/json")
        return
    Path("chats").mkdir(exist_ok=True)
    (Path("chats") / f"{session}.json").write_text(data)


def list_sessions() -> list[str]:
    s3 = _chat_s3()
    if s3:
        names = []
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=CHATS_BUCKET, Prefix="chats/"):
            names += [o["Key"][len("chats/"):-len(".json")] for o in page.get("Contents", []) if o["Key"].endswith(".json")]
        return sorted(names)
    Path("chats").mkdir(exist_ok=True)
    return [p.stem for p in sorted(Path("chats").glob("*.json"))]


def delete_history(session: str) -> bool:
    """Delete a session's history. False when there was none."""
    if not load_history(session):
        return False
    s3 = _chat_s3()
    if s3:
        s3.delete_object(Bucket=CHATS_BUCKET, Key=f"chats/{session}.json")
    else:
        (Path("chats") / f"{session}.json").unlink()
    return True


def build_params(**kwargs):
    result = {}
    for key, value in kwargs.items():
        if value:
            result[key] = value
    return result