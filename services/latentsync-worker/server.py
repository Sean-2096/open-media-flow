"""OpenMediaFlow HTTP adapter for the official LatentSync 1.6 runtime.

Run this file inside an environment where the official ByteDance/LatentSync
repository, CUDA PyTorch and checkpoints are already installed. It deliberately
uses the same base64 contract as the local MuseTalk service so the control plane
does not depend on a model-specific API.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


app = FastAPI(title="OpenMediaFlow LatentSync Worker", version="1.0.0")
repo = Path(os.getenv("LATENTSYNC_REPO", "/opt/LatentSync")).expanduser().resolve()
checkpoint = Path(
    os.getenv("LATENTSYNC_CHECKPOINT", str(repo / "checkpoints/latentsync_unet.pt"))
).expanduser().resolve()
python_bin = os.getenv("LATENTSYNC_PYTHON", sys.executable)
inference_steps = max(20, min(50, int(os.getenv("LATENTSYNC_INFERENCE_STEPS", "20"))))
guidance_scale = max(1.0, min(3.0, float(os.getenv("LATENTSYNC_GUIDANCE_SCALE", "1.5"))))


class LipSyncRequest(BaseModel):
    video_b64: str
    audio_b64: str
    avatar_key: str | None = None


def _cuda_ready() -> bool:
    try:
        import torch

        return torch.cuda.is_available()
    except (ImportError, RuntimeError):
        return False


def _ready() -> bool:
    return (
        _cuda_ready()
        and (repo / "scripts/inference.py").is_file()
        and (repo / "configs/unet/stage2_512.yaml").is_file()
        and checkpoint.is_file()
    )


def _is_image(data: bytes) -> bool:
    return data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff")


def _run(command: list[str], *, timeout: int = 1800) -> None:
    try:
        subprocess.run(
            command,
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="LatentSync inference timed out") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc))[-1200:]
        raise HTTPException(status_code=500, detail=f"LatentSync failed: {detail}") from exc


@app.get("/health")
def health() -> dict[str, object]:
    ready = _ready()
    return {
        "ok": ready,
        "status": "ok" if ready else "not_ready",
        "provider": "latentsync-v1.6-512",
        "cuda": _cuda_ready(),
        "checkpoint": checkpoint.name,
    }


@app.post("/lipsync")
def lipsync(body: LipSyncRequest) -> dict[str, object]:
    if not _ready():
        raise HTTPException(status_code=503, detail="LatentSync CUDA runtime is not ready")
    started = time.monotonic()
    try:
        source_data = base64.b64decode(body.video_b64, validate=True)
        audio_data = base64.b64decode(body.audio_b64, validate=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid base64 media") from exc

    with tempfile.TemporaryDirectory(prefix="omf_latentsync_") as temp_value:
        temp = Path(temp_value)
        source = temp / ("source.png" if _is_image(source_data) else "source.mp4")
        raw_audio = temp / "audio-input"
        audio = temp / "audio.wav"
        video = temp / "video.mp4"
        output = temp / "output.mp4"
        source.write_bytes(source_data)
        raw_audio.write_bytes(audio_data)
        _run(["ffmpeg", "-y", "-v", "error", "-i", str(raw_audio), "-ar", "16000", "-ac", "1", str(audio)], timeout=120)
        if _is_image(source_data):
            duration = subprocess.check_output(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(audio)],
                text=True,
                timeout=30,
            ).strip()
            _run(
                ["ffmpeg", "-y", "-v", "error", "-loop", "1", "-i", str(source), "-t", duration, "-r", "25", "-pix_fmt", "yuv420p", str(video)],
                timeout=300,
            )
        else:
            video = source

        command = [
            python_bin,
            "-m",
            "scripts.inference",
            "--unet_config_path",
            "configs/unet/stage2_512.yaml",
            "--inference_ckpt_path",
            str(checkpoint),
            "--inference_steps",
            str(inference_steps),
            "--guidance_scale",
            str(guidance_scale),
            "--enable_deepcache",
            "--video_path",
            str(video),
            "--audio_path",
            str(audio),
            "--video_out_path",
            str(output),
        ]
        _run(command)
        if not output.is_file() or output.stat().st_size == 0:
            raise HTTPException(status_code=500, detail="LatentSync returned no video")
        result = output.read_bytes()

    return {
        "video_b64": base64.b64encode(result).decode("ascii"),
        "video_size_bytes": len(result),
        "provider": "latentsync-v1.6-512",
        "timing": {"total_s": round(time.monotonic() - started, 3)},
    }
