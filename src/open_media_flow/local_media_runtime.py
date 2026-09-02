from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="OpenMediaFlow Native Media Runtime", version="0.6.0")
api_key = os.getenv("LOCAL_MEDIA_RUNTIME_API_KEY", "") or os.getenv("OMF_API_KEY", "")
audio_root = Path(
    os.getenv(
        "OMF_NATIVE_AUDIO_DIR",
        str(Path.cwd() / "data/inbox/generated/audio"),
    )
).expanduser().resolve()
audio_root.mkdir(parents=True, exist_ok=True)


class SpeechRequest(BaseModel):
    task_id: str = Field(min_length=8, max_length=64)
    text: str = Field(min_length=2, max_length=10_000)
    voice: str = Field(default="Tingting", max_length=100)
    rate: int = Field(default=190, ge=80, le=350)


def require_key(x_api_key: str = Header(default="")) -> None:
    if api_key and x_api_key != api_key:
        raise HTTPException(status_code=401, detail="invalid API key")


@app.get("/health")
def health(x_api_key: str = Header(default="")) -> dict[str, str]:
    require_key(x_api_key)
    return {"status": "ok", "tts": "macos-say"}


@app.post("/v1/audio/speech")
def synthesize(body: SpeechRequest, x_api_key: str = Header(default="")) -> dict[str, str]:
    require_key(x_api_key)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", body.task_id):
        raise HTTPException(status_code=400, detail="invalid task id")
    task_dir = (audio_root / body.task_id).resolve()
    task_dir.mkdir(parents=True, exist_ok=True)
    source = task_dir / "narration.aiff"
    output = task_dir / "narration.mp3"
    try:
        subprocess.run(
            ["/usr/bin/say", "-v", body.voice, "-r", str(body.rate), "-o", str(source), body.text],
            check=True,
            capture_output=True,
            timeout=120,
        )
        subprocess.run(
            [
                "/opt/homebrew/bin/ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-codec:a",
                "libmp3lame",
                "-q:a",
                "2",
                str(output),
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(status_code=500, detail="local speech synthesis failed") from exc
    finally:
        source.unlink(missing_ok=True)
    relative = output.relative_to(audio_root.parents[1])
    return {"relative_path": relative.as_posix(), "provider": "macos-say"}
