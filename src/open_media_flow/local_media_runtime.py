from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="OpenMediaFlow Native Media Runtime", version="0.7.0")
api_key = os.getenv("LOCAL_MEDIA_RUNTIME_API_KEY", "") or os.getenv("OMF_API_KEY", "")
project_root = Path(__file__).resolve().parents[2]
media_root = Path(
    os.getenv("OMF_NATIVE_MEDIA_ROOT", str(project_root / "data/inbox"))
).expanduser().resolve()
audio_root = Path(
    os.getenv("OMF_NATIVE_AUDIO_DIR", str(media_root / "generated/audio"))
).expanduser().resolve()
interpolation_root = Path(
    os.getenv("OMF_NATIVE_INTERPOLATION_DIR", str(media_root / "generated/interpolated"))
).expanduser().resolve()
lip_sync_root = Path(
    os.getenv("OMF_NATIVE_LIP_SYNC_DIR", str(media_root / "generated/lip-sync"))
).expanduser().resolve()
musetalk_base_url = os.getenv(
    "OMF_MUSETALK_BASE_URL", "http://127.0.0.1:8091"
).rstrip("/")
latentsync_base_url = os.getenv("OMF_LATENTSYNC_BASE_URL", "").rstrip("/")
musetalk_runtime = Path(
    os.getenv(
        "OMF_MUSETALK_RUNTIME",
        str(project_root / "data/lip-sync/musetalk-mac"),
    )
).expanduser().resolve()
tts_provider = os.getenv("OMF_TTS_PROVIDER", "qwen3-mlx").strip().lower()
tts_model_path = Path(
    os.getenv(
        "OMF_TTS_MODEL_PATH",
        str(project_root / "data/models/qwen3-tts-1.7b-customvoice-8bit"),
    )
).expanduser().resolve()
tts_voice = os.getenv("OMF_TTS_VOICE", "Vivian").strip()
tts_language = os.getenv("OMF_TTS_LANGUAGE", "Chinese").strip()
tts_instruct = os.getenv(
    "OMF_TTS_INSTRUCT",
    "自然、有感染力的中文短视频旁白，语气亲切，节奏有停顿，避免播音腔",
).strip()
rife_executable = Path(
    os.getenv("OMF_RIFE_EXECUTABLE", str(Path(sys.executable).with_name("rife-mlx")))
).expanduser().resolve()
rife_weights_dir = Path(
    os.getenv("OMF_RIFE_WEIGHTS_DIR", str(project_root / "data/models/rife-4.25"))
).expanduser().resolve()

audio_root.mkdir(parents=True, exist_ok=True)
interpolation_root.mkdir(parents=True, exist_ok=True)
lip_sync_root.mkdir(parents=True, exist_ok=True)
_tts_model = None
_tts_lock = threading.Lock()
_lip_sync_lock = threading.Lock()
_lip_jobs_lock = threading.Lock()
_lip_jobs: dict[str, dict[str, object]] = {}


class SpeechRequest(BaseModel):
    task_id: str = Field(min_length=8, max_length=64)
    clip_id: str | None = Field(default=None, min_length=1, max_length=64)
    text: str = Field(min_length=2, max_length=10_000)
    voice: str | None = Field(default=None, max_length=100)
    language: str | None = Field(default=None, max_length=40)
    instruct: str | None = Field(default=None, max_length=500)
    rate: int = Field(default=190, ge=80, le=350)


class FrameInterpolationRequest(BaseModel):
    task_id: str = Field(min_length=8, max_length=64)
    shot_id: str = Field(min_length=1, max_length=64)
    source_path: str = Field(min_length=3, max_length=1_000)
    multiplier: int = Field(default=2, ge=2, le=4)
    scale: float = Field(default=1.0)


class LipSyncRequest(BaseModel):
    task_id: str = Field(min_length=8, max_length=64)
    shot_id: str = Field(min_length=1, max_length=64)
    video_path: str = Field(min_length=3, max_length=1_000)
    audio_path: str = Field(min_length=3, max_length=1_000)
    mode: str = Field(default="auto", pattern="^(auto|fast|quality)$")


def require_key(x_api_key: str = Header(default="")) -> None:
    if api_key and x_api_key != api_key:
        raise HTTPException(status_code=401, detail="invalid API key")


def _safe_id(value: str, label: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise HTTPException(status_code=400, detail=f"invalid {label}")
    return value


def _relative_media_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(media_root).as_posix()
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="output is outside media root") from exc


def _resolve_source(value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = media_root / candidate
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(media_root)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="invalid media source path") from exc
    return resolved


def _qwen_ready() -> bool:
    return tts_model_path.is_dir() and (tts_model_path / "config.json").is_file()


def _rife_ready() -> bool:
    return (
        rife_executable.is_file()
        and rife_weights_dir.is_dir()
        and (rife_weights_dir / "model.safetensors").is_file()
    )


def _musetalk_ready() -> bool:
    return _lip_engine_ready(musetalk_base_url)


def _latentsync_ready() -> bool:
    return bool(latentsync_base_url) and _lip_engine_ready(latentsync_base_url)


def _lip_engine_ready(base_url: str) -> bool:
    request = urllib.request.Request(f"{base_url}/health")
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            result = json.loads(response.read().decode("utf-8"))
        return response.status == 200 and (
            result.get("ok") is True or result.get("status") == "ok"
        )
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return False


def _select_lip_engine(mode: str) -> tuple[str, str] | None:
    if mode in {"auto", "quality"} and _latentsync_ready():
        return "latentsync-v1.6-512", latentsync_base_url
    if mode in {"auto", "fast"} and _musetalk_ready():
        return "musetalk-v1.5-mps", musetalk_base_url
    return None


@app.get("/health")
def health(x_api_key: str = Header(default="")) -> dict[str, object]:
    require_key(x_api_key)
    tts_ready = tts_provider == "macos-say" or _qwen_ready()
    frame_ready = _rife_ready()
    muse_ready = _musetalk_ready()
    latent_ready = _latentsync_ready()
    lip_sync_ready = muse_ready or latent_ready
    engines = []
    if latent_ready:
        engines.append("latentsync-v1.6-512")
    if muse_ready:
        engines.append("musetalk-v1.5-mps")
    return {
        "status": "ok" if tts_ready and frame_ready else "degraded",
        "tts": tts_provider,
        "tts_ready": tts_ready,
        "tts_model": tts_model_path.name if tts_provider == "qwen3-mlx" else "system",
        "lip_sync": engines[0] if engines else "lip-sync-unavailable",
        "lip_sync_ready": lip_sync_ready,
        "lip_sync_engines": engines,
        "lip_sync_modes": ["auto", "fast", "quality"],
        "frame_interpolation": "rife-mlx",
        "frame_interpolation_ready": frame_ready,
    }


def _set_lip_job(job_id: str, **updates: object) -> None:
    with _lip_jobs_lock:
        job = _lip_jobs.get(job_id)
        if job is not None:
            job.update(updates)


def _score_lip_sync(video: Path, audio: Path) -> tuple[float, float]:
    python = musetalk_runtime / ".venv/bin/python"
    script = project_root / "scripts/lip-sync-quality.py"
    result = subprocess.run(
        [str(python), str(script), str(video), str(audio)],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    return float(payload["sync_score"]), float(payload["face_coverage"])


def _run_lip_sync_job(
    job_id: str,
    task_id: str,
    shot_id: str,
    video: Path,
    audio: Path,
    provider: str,
    provider_base_url: str,
) -> None:
    started_at = time.monotonic()
    output = lip_sync_root / task_id / f"{shot_id}.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        _set_lip_job(job_id, progress=10, stage="preparing")
        payload = json.dumps(
            {
                "video_b64": base64.b64encode(video.read_bytes()).decode("ascii"),
                "audio_b64": base64.b64encode(audio.read_bytes()).decode("ascii"),
                "avatar_key": f"{task_id}-{shot_id}",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{provider_base_url}/lipsync",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _lip_sync_lock:
            _set_lip_job(job_id, progress=20, stage="inference")
            with urllib.request.urlopen(request, timeout=1_800) as response:
                result = json.loads(response.read().decode("utf-8"))
        video_b64 = str(result.get("video_b64") or "")
        if not video_b64:
            raise RuntimeError(f"{provider} returned no video")
        output.write_bytes(base64.b64decode(video_b64))
        _set_lip_job(job_id, progress=90, stage="quality_check")
        sync_score, face_coverage = _score_lip_sync(output, audio)
        _set_lip_job(
            job_id,
            state="complete",
            progress=100,
            stage="complete",
            elapsed_seconds=round(time.monotonic() - started_at),
            relative_path=_relative_media_path(output),
            sync_score=sync_score,
            face_coverage=face_coverage,
        )
    except Exception as exc:  # background jobs must persist their terminal error
        output.unlink(missing_ok=True)
        detail = str(exc)
        if isinstance(exc, urllib.error.HTTPError):
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except OSError:
                pass
        _set_lip_job(
            job_id,
            state="failed",
            stage="failed",
            elapsed_seconds=round(time.monotonic() - started_at),
            error=detail[-800:],
        )


def _split_text(text: str, max_chars: int = 120) -> list[str]:
    sentences = [part.strip() for part in re.split(r"(?<=[。！？!?；;])", text) if part.strip()]
    chunks: list[str] = []
    current = ""
    for sentence in sentences or [text.strip()]:
        if current and len(current) + len(sentence) > max_chars:
            chunks.append(current)
            current = ""
        while len(sentence) > max_chars:
            chunks.append(sentence[:max_chars])
            sentence = sentence[max_chars:]
        current += sentence
    if current:
        chunks.append(current)
    return chunks


def _load_qwen_model():
    global _tts_model
    if _tts_model is None:
        if not _qwen_ready():
            raise RuntimeError(f"Qwen3-TTS model is missing: {tts_model_path}")
        from mlx_audio.tts.utils import load

        _tts_model = load(str(tts_model_path))
    return _tts_model


def _write_wav(path: Path, samples, sample_rate: int) -> None:
    import numpy as np

    pcm = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm.tobytes())


def _synthesize_qwen(body: SpeechRequest, wav_output: Path) -> None:
    import numpy as np

    model = _load_qwen_model()
    voice = body.voice or tts_voice
    language = body.language or tts_language
    instruct = body.instruct if body.instruct is not None else tts_instruct
    pieces = []
    sample_rate = 24_000
    for chunk in _split_text(body.text):
        results = list(
            model.generate(
                text=chunk,
                voice=voice,
                lang_code=language,
                instruct=instruct or None,
                split_pattern="",
                temperature=0.75,
                top_p=0.9,
                repetition_penalty=1.08,
                verbose=False,
            )
        )
        if not results:
            raise RuntimeError("Qwen3-TTS returned no audio")
        for result in results:
            pieces.append(np.asarray(result.audio, dtype=np.float32).reshape(-1))
            sample_rate = int(result.sample_rate)
        pieces.append(np.zeros(int(sample_rate * 0.11), dtype=np.float32))
    if not pieces:
        raise RuntimeError("Qwen3-TTS returned empty audio")
    _write_wav(wav_output, np.concatenate(pieces), sample_rate)


def _synthesize_macos(body: SpeechRequest, wav_output: Path) -> None:
    source = wav_output.with_suffix(".aiff")
    try:
        subprocess.run(
            [
                "/usr/bin/say",
                "-v",
                body.voice or "Tingting",
                "-r",
                str(body.rate),
                "-o",
                str(source),
                body.text,
            ],
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
                str(wav_output),
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
    finally:
        source.unlink(missing_ok=True)


@app.post("/v1/audio/speech")
def synthesize(body: SpeechRequest, x_api_key: str = Header(default="")) -> dict[str, str]:
    require_key(x_api_key)
    _safe_id(body.task_id, "task id")
    if body.clip_id:
        _safe_id(body.clip_id, "clip id")
    task_dir = (audio_root / body.task_id).resolve()
    task_dir.mkdir(parents=True, exist_ok=True)
    output_stem = body.clip_id or "narration"
    wav_output = task_dir / f"{output_stem}.wav"
    output = task_dir / f"{output_stem}.mp3"
    try:
        with _tts_lock:
            if tts_provider == "qwen3-mlx":
                _synthesize_qwen(body, wav_output)
            elif tts_provider == "macos-say":
                _synthesize_macos(body, wav_output)
            else:
                raise RuntimeError(f"unsupported TTS provider: {tts_provider}")
        subprocess.run(
            [
                "/opt/homebrew/bin/ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(wav_output),
                "-af",
                "loudnorm=I=-16:TP=-1.5:LRA=11",
                "-codec:a",
                "libmp3lame",
                "-q:a",
                "2",
                str(output),
            ],
            check=True,
            capture_output=True,
            timeout=180,
        )
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        raise HTTPException(status_code=500, detail=f"local speech synthesis failed: {exc}") from exc
    finally:
        wav_output.unlink(missing_ok=True)
    return {"relative_path": _relative_media_path(output), "provider": tts_provider}


@app.post("/v1/video/interpolate")
def interpolate(
    body: FrameInterpolationRequest,
    x_api_key: str = Header(default=""),
) -> dict[str, object]:
    require_key(x_api_key)
    _safe_id(body.task_id, "task id")
    _safe_id(body.shot_id, "shot id")
    if body.scale not in {0.25, 0.5, 1.0, 2.0, 4.0}:
        raise HTTPException(status_code=400, detail="invalid interpolation scale")
    if not _rife_ready():
        raise HTTPException(status_code=503, detail="RIFE-MLX runtime is not ready")
    source = _resolve_source(body.source_path)
    task_dir = (interpolation_root / body.task_id).resolve()
    task_dir.mkdir(parents=True, exist_ok=True)
    output = task_dir / f"{body.shot_id}-{body.multiplier}x.mp4"
    try:
        subprocess.run(
            [
                str(rife_executable),
                "-i",
                str(source),
                "-o",
                str(output),
                "--multi",
                str(body.multiplier),
                "--scale",
                str(body.scale),
                "--weights_dir",
                str(rife_weights_dir),
            ],
            check=True,
            capture_output=True,
            timeout=1_200,
        )
    except subprocess.TimeoutExpired as exc:
        output.unlink(missing_ok=True)
        raise HTTPException(status_code=504, detail="frame interpolation timed out") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        output.unlink(missing_ok=True)
        detail = getattr(exc, "stderr", b"")
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        raise HTTPException(
            status_code=500,
            detail=f"frame interpolation failed: {str(detail)[-500:]}",
        ) from exc
    return {
        "relative_path": _relative_media_path(output),
        "provider": "rife-mlx",
        "multiplier": body.multiplier,
    }


@app.post("/v1/video/lip-sync")
def lip_sync(
    body: LipSyncRequest,
    x_api_key: str = Header(default=""),
) -> dict[str, str]:
    require_key(x_api_key)
    task_id = _safe_id(body.task_id, "task id")
    shot_id = _safe_id(body.shot_id, "shot id")
    selected = _select_lip_engine(body.mode)
    if selected is None:
        detail = (
            "LatentSync quality runtime is not ready"
            if body.mode == "quality"
            else "No compatible lip-sync runtime is ready"
        )
        raise HTTPException(status_code=503, detail=detail)
    provider, provider_base_url = selected
    video = _resolve_source(body.video_path)
    audio = _resolve_source(body.audio_path)
    job_id = uuid.uuid4().hex
    with _lip_jobs_lock:
        _lip_jobs[job_id] = {
            "state": "processing",
            "progress": 0,
            "stage": "queued",
            "started_at": time.monotonic(),
            "provider": provider,
            "requested_mode": body.mode,
        }
    threading.Thread(
        target=_run_lip_sync_job,
        args=(job_id, task_id, shot_id, video, audio, provider, provider_base_url),
        daemon=True,
        name=f"lip-sync-{job_id[:8]}",
    ).start()
    return {"job_id": job_id, "provider": provider}


@app.get("/v1/video/lip-sync/{job_id}")
def lip_sync_status(job_id: str, x_api_key: str = Header(default="")) -> dict[str, object]:
    require_key(x_api_key)
    _safe_id(job_id, "job id")
    with _lip_jobs_lock:
        job = dict(_lip_jobs.get(job_id) or {})
    if not job:
        raise HTTPException(status_code=404, detail="lip-sync job not found")
    started_at = job.pop("started_at", None)
    if started_at is not None and job.get("state") == "processing":
        job["elapsed_seconds"] = round(time.monotonic() - float(started_at))
    return job
